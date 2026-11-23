"""One approved, isolated live acceptance workflow; no network during preparation.

Only newly reserved test records are written. Persistent stage markers and the
underlying operation receipts distinguish resumption from a repeated write.
Deletion is deliberately not attempted without an authoritative assignment guard.
"""
from __future__ import annotations

import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import uuid

from engine.create_adapter import run_create, request_lock, validate_plan
from engine.create_pilot import DEFAULT_BASE
from environments import normalize_origin
from engine.validate_pilot import create_plan
from event_updates import EventUpdateService, _atomic_json
from operations import CalendarService, OperationError


class AcceptanceError(ValueError):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _folder(state_dir, acceptance_id):
    if not isinstance(acceptance_id, str) or not re.fullmatch(r"[a-f0-9]{32}", acceptance_id):
        raise AcceptanceError("Invalid live acceptance request ID.")
    return Path(state_dir).resolve() / "live_acceptance" / acceptance_id


def _config(config):
    try:
        return normalize_origin(config.get("environment", DEFAULT_BASE))
    except (AttributeError, TypeError, ValueError) as exc:
        raise AcceptanceError("The acceptance environment is invalid: " + str(exc)) from exc


def _clean(value, authorization):
    secrets = [authorization] if authorization else []
    if isinstance(authorization, str) and authorization.startswith("Basic "):
        secrets.append(authorization[6:])
    try:
        credentials = base64.b64decode(authorization.removeprefix("Basic "), validate=True).decode()
        username, separator, password = credentials.partition(":")
        if separator:
            secrets.extend((credentials, password))
    except (ValueError, UnicodeError, AttributeError):
        pass
    def redact(item):
        if isinstance(item, str):
            for secret in sorted((s for s in secrets if s), key=len, reverse=True):
                item = item.replace(secret, "[REDACTED]")
            return item
        if isinstance(item, dict):
            return {key: redact(child) for key, child in item.items()}
        if isinstance(item, list):
            return [redact(child) for child in item]
        return item
    return redact(value)


def prepare_live_acceptance(config, state_dir):
    """Reserve concrete names and persist a preview; no credentials or requests."""
    environment = _config(config)
    acceptance_id = uuid.uuid4().hex
    short = acceptance_id[:12].upper()
    prefix = "K12AT_" + short
    plan = deepcopy(create_plan(Path(__file__).parent / "engine" / "K12_SAMPLE_A_Create_Pilot.xlsx"))
    plan["environment"] = environment
    event_codes, calendar_keys = {}, {}
    for index, event in enumerate(plan["events"], 1):
        event_codes[event["ShortCode"]] = prefix + "_E" + str(index)
        event["ShortCode"] = event_codes[event["ShortCode"]]
        event["Name"] = "K12 Acceptance " + short + " - Event " + str(index)
    for index, calendar in enumerate(plan["calendars"]):
        label = "AB"[index]
        calendar_keys[calendar["key"]] = prefix + "_" + label
        calendar["key"] = calendar_keys[calendar["key"]]
        calendar["Name"] = "K12 Acceptance " + short + " - Calendar " + label
        calendar["Description"] = "Isolated launcher acceptance " + acceptance_id + "; no worker eligibility requested."
    for link in plan["associations"]:
        link["event_code"] = event_codes[link["event_code"]]
        link["calendar_key"] = calendar_keys[link["calendar_key"]]
    plan["intent"] = "isolated_live_acceptance_with_verified_resume"
    validate_plan(plan)
    if (len(plan["events"]), len(plan["calendars"]), len(plan["associations"])) != (3, 2, 4):
        raise AcceptanceError("The bundled acceptance sample changed; expected 3 events, 2 calendars, and 4 links.")
    event = plan["events"][0]
    shifted = {key: (datetime.fromisoformat(event[key]) + timedelta(days=1)).isoformat() + "+00:00"
               for key in ("StartDateTimeLocal", "EndDateTimeLocal")}
    enabled = (config.get("administrator_asserted_full_calendar_visibility") is True
               and config.get("administrator_asserted_no_legacy_event_coverage") is True)
    preview = {"schema": "K12-LIVE-ACCEPTANCE-1", "acceptance_id": acceptance_id,
               "preview_id": acceptance_id, "environment": environment, "prepared_at": _now(),
               "operation": "isolated_live_acceptance", "plan": plan,
               "counts": {"events": 3, "calendars": 2, "links": 4},
               "calendar_names": [row["Name"] for row in plan["calendars"]],
               "event_codes": [row["ShortCode"] for row in plan["events"]],
               "calendar_description_test": {"calendar_key": plan["calendars"][0]["key"],
                   "original": plan["calendars"][0]["Description"],
                   "temporary": plan["calendars"][0]["Description"] + " [Acceptance correction verified]"},
               "event_date_test": {"enabled": enabled, "event_code": event["ShortCode"],
                   "original_start": event["StartDateTimeLocal"] + "+00:00", "original_end": event["EndDateTimeLocal"] + "+00:00",
                   "temporary_start": shifted["StartDateTimeLocal"], "temporary_end": shifted["EndDateTimeLocal"]},
               "steps": ["Read current calendars and save a comparison baseline", "Create these 3 events, 2 calendars, and 4 links",
                         "Verify the created values and associations", "Change Calendar A description and restore it",
                         "Change and restore the first test event dates" if enabled else "Skip global event date writes: administrator scope assertions are absent",
                         "Rerun the unchanged request with zero writes", "Compare preexisting calendars with the baseline"],
               "deletion": {"status": "blocked", "attempted": False,
                            "reason": "No authoritative complete worker-assignment check and atomic deletion guard are available."},
               "retention": "The 3 events and 2 calendars remain visible for review. This workflow never deletes records or assigns workers."}
    if config.get("profile_id") is not None:
        preview["profile_id"] = config["profile_id"]
    _atomic_json(_folder(state_dir, acceptance_id) / "preview.json", preview)
    return preview


def _read_json(path, environment=DEFAULT_BASE):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AcceptanceError("The saved acceptance state cannot be read: " + path.name) from exc
    if not isinstance(value, dict) or value.get("environment") != environment:
        raise AcceptanceError("Saved acceptance state belongs to another environment or is invalid.")
    return value


def _inventory(service, environment=DEFAULT_BASE):
    snapshot = service.read()
    if snapshot.get("complete") is not True or snapshot.get("environment") != environment:
        raise AcceptanceError("The calendar inventory is incomplete; the acceptance workflow stopped.")
    return snapshot


def _record_fingerprints(snapshot, exclude=()):
    result = {}
    for calendar in snapshot["calendars"]:
        cid = str(calendar["CalendarId"])
        if cid in exclude:
            continue
        clean = {key: calendar.get(key) for key in ("CalendarId", "Name", "StartDate", "EndDate", "Description")}
        clean["events"] = sorted(({key: row.get(key) for key in
            ("CalendarCalEventId", "CalendarEventId", "CalendarId", "Name", "ShortCode", "StartDateTime", "EndDateTime", "AllDayFlag", "HalfDayForElapsed", "CategoryCode", "EventType")}
            for row in calendar.get("events", [])), key=lambda row: str(row["CalendarCalEventId"]))
        clean["subscribers"] = sorted(({key: row.get(key) for key in ("CalendarSubscriberId", "CalendarId", "ListId", "EvaluateRuleMembershipFlag")}
                                        for row in calendar.get("subscribers", [])), key=lambda row: json.dumps(row, sort_keys=True))
        result[cid] = hashlib.sha256(json.dumps(clean, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return result


def _verify_created(snapshot, preview, journal):
    plan = preview["plan"]
    available = {str(row["CalendarId"]): row for row in snapshot["calendars"]}
    expected_events = {row["ShortCode"]: row for row in plan["events"]}
    ids = {}
    for calendar in plan["calendars"]:
        cid = str(journal["calendars"][calendar["key"]]["CalendarId"])
        ids[calendar["key"]] = cid
        actual = available.get(cid)
        if actual is None or any(actual.get(key) != calendar[key] for key in ("Name", "StartDate", "EndDate", "Description")):
            raise AcceptanceError("A newly created calendar differs from its approved values.")
        if actual.get("subscribers"):
            raise AcceptanceError("A test calendar unexpectedly has eligibility rule subscriptions.")
        wanted = {link["event_code"] for link in plan["associations"] if link["calendar_key"] == calendar["key"]}
        if len(actual["events"]) != len(wanted) or {row.get("ShortCode") for row in actual["events"]} != wanted:
            raise AcceptanceError("A newly created calendar has unexpected event associations.")
        for row in actual["events"]:
            event = expected_events[row["ShortCode"]]
            eid = str(journal["events"][row["ShortCode"]]["CalendarEventId"])
            expected = {"CalendarEventId": eid, "Name": event["Name"], "CategoryCode": "PH", "HalfDayForElapsed": "N", "AllDayFlag": False}
            if any((str(row.get(key)) if key == "CalendarEventId" else row.get(key)) != value for key, value in expected.items()):
                raise AcceptanceError("A newly created event differs from its approved values.")
            for target, source in (("StartDateTime", "StartDateTimeLocal"), ("EndDateTime", "EndDateTimeLocal")):
                if datetime.fromisoformat(str(row[target]).replace("Z", "+00:00")) != datetime.fromisoformat(event[source] + "+00:00"):
                    raise AcceptanceError("A newly created event has an unexpected date boundary.")
    return ids


def run_live_acceptance(preview, authorization, config, state_dir, progress_callback=None):
    """Execute an already approved preview, or safely resume its saved stages."""
    environment = _config(config)
    if not isinstance(preview, dict):
        raise AcceptanceError("Prepare and approve a concrete acceptance preview first.")
    if (preview.get("environment") != environment
            or preview.get("profile_id") != config.get("profile_id")
            or not isinstance(preview.get("plan"), dict)
            or preview["plan"].get("environment") != environment):
        raise AcceptanceError("The acceptance preview belongs to another environment. Select its original environment or prepare a new preview.")
    folder = _folder(state_dir, preview.get("acceptance_id"))
    saved = _read_json(folder / "preview.json", environment)
    if saved != preview:
        raise AcceptanceError("The acceptance preview changed; prepare and approve it again.")
    progress = progress_callback or (lambda message: None)
    with request_lock(folder):
        report_path = folder / "acceptance_report.json"
        report = _read_json(report_path, environment) if report_path.exists() else {
            "schema": "K12-LIVE-ACCEPTANCE-REPORT-1", "environment": environment,
            "acceptance_id": preview["acceptance_id"], "status": "running", "started_at": _now(),
            "calendar_names": preview["calendar_names"], "event_codes": preview["event_codes"],
            "steps": {}, "deletion": preview["deletion"], "record_retention": preview["retention"]}
        if report_path.exists() and report.get("profile_id") != config.get("profile_id"):
            raise AcceptanceError("The saved acceptance report belongs to another environment profile.")
        if config.get("profile_id") is not None:
            report["profile_id"] = config["profile_id"]
        for receipt_key in ("creation", "unchanged_rerun"):
            receipt = report.get(receipt_key)
            if receipt is not None and (not isinstance(receipt, dict) or receipt.get("environment") != environment
                    or (receipt.get("profile_id") is not None and config.get("profile_id") is not None
                        and receipt["profile_id"] != config["profile_id"])):
                raise AcceptanceError("The saved acceptance creation receipt belongs to another environment or is invalid.")
        report["report_path"] = str(report_path)
        def save():
            report["updated_at"] = _now()
            _atomic_json(report_path, _clean(report, authorization))
        def begin(name):
            report["current_stage"] = name
            progress("Live acceptance: " + name.replace("_", " ") + "...")
            save()
        def passed(name, **details):
            report["steps"][name] = {"status": "passed", **details}
            save()
        def complete(name):
            return report["steps"].get(name, {}).get("status") == "passed"
        service = CalendarService(authorization, config, folder / "read", progress)
        try:
            prior_error = str(report.get("error", ""))
            report["status"] = "running"
            # A stopped report can contain a safe, user-facing detail from an
            # earlier attempt.  Do not let it describe the next attempt.
            for key in ("error", "detail", "possible_temporary_changes", "stopped_at"):
                report.pop(key, None)
            if not complete("baseline_read"):
                begin("baseline_read")
                baseline = _inventory(service, environment)
                passed("baseline_read", fingerprints=_record_fingerprints(baseline), visible_calendars=len(baseline["calendars"]))
            if not complete("create"):
                begin("create")
                creation = run_create(preview["plan"], authorization, folder / "creation", progress, config=config)
                if (creation.get("environment") != environment
                        or (creation.get("profile_id") is not None and config.get("profile_id") is not None
                            and creation["profile_id"] != config["profile_id"])):
                    raise AcceptanceError("The creation receipt belongs to another environment; further acceptance writes are blocked.")
                report["creation"] = creation
                save()
                if creation.get("status") != "applied_verified":
                    raise AcceptanceError("Creation is " + str(creation.get("status")) + ". Resume this same acceptance request; preserve its creation journal.")
                passed("create", counts=creation.get("result_counts"), receipt_path=creation.get("receipt_path"))
            journal = report["creation"]["journal"]
            if not complete("created_readback"):
                begin("created_readback")
                ids = _verify_created(_inventory(service, environment), preview, journal)
                report["owned_calendar_ids"] = ids
                report["owned_event_ids"] = {code: str(item["CalendarEventId"]) for code, item in journal["events"].items()}
                passed("created_readback", counts=preview["counts"])

            def calendar_change(stage_name, description):
                if complete(stage_name):
                    return
                begin(stage_name)
                cid = report["owned_calendar_ids"][preview["calendar_description_test"]["calendar_key"]]
                stage_folder = folder / stage_name
                calendar_service = CalendarService(authorization, config, stage_folder, progress)
                stage = report["steps"].get(stage_name, {})
                expected_before = (descriptions["temporary"] if stage_name == "calendar_description_restore" else descriptions["original"])
                recovery = None
                if stage.get("status") == "write_started":
                    receipts = list(stage_folder.glob("Calendar_Edit_*.json"))
                    if len(receipts) == 1:
                        result = calendar_service.reconcile_calendar_edit(receipts[0])
                        if result.get("status") != "reconciled_applied":
                            raise AcceptanceError("The calendar test receipt reconciled as " + str(result.get("status")) + "; automatic PATCH retry is blocked.")
                    elif len(receipts) == 0 and "FileNotFoundError" in prior_error:
                        # A missing receipt normally means the outcome is
                        # unknowable.  The narrow exception below is only for
                        # the persisted pre-dispatch journal failure that this
                        # workflow records as FileNotFoundError: the calendar
                        # service writes the receipt before its first PATCH.
                        # Re-read the exact description before either treating
                        # it as applied or making one fresh, ETag-guarded plan.
                        failed_stage = deepcopy(stage)
                        change = calendar_service.plan_calendar_edit(cid, {"Description": description})
                        observed = change["before"].get("Description")
                        recovery = {"status": "pre_dispatch_journal_failure_reconciled",
                                    "checked_at": _now(), "receipt_count": 0,
                                    "observed_description": observed,
                                    "initial_stage": failed_stage}
                        if observed == description:
                            passed(stage_name, receipt_path=None,
                                   result_status="reconciled_applied_without_receipt", recovery=recovery)
                            return
                        if observed != expected_before:
                            raise AcceptanceError("The test calendar description changed outside this workflow. Restoration must not overwrite later work.")
                        report["steps"][stage_name] = {"status": "write_started", "before": change["before"],
                                                             "expected": change["after"], "recovery": recovery}
                        save()
                        result = calendar_service.apply_calendar_edit(change)
                        if result.get("status") not in ("updated_verified", "unchanged_verified"):
                            raise AcceptanceError("The calendar correction did not produce verified readback.")
                    else:
                        raise AcceptanceError("A calendar test write has an unresolved outcome. Its existing stage receipt must be reconciled; no PATCH was repeated.")
                else:
                    change = calendar_service.plan_calendar_edit(cid, {"Description": description})
                    if change["before"].get("Description") != expected_before:
                        raise AcceptanceError("The test calendar description changed outside this workflow. Restoration must not overwrite later work.")
                    report["steps"][stage_name] = {"status": "write_started", "before": change["before"], "expected": change["after"]}
                    save()
                    result = calendar_service.apply_calendar_edit(change)
                    if result.get("status") not in ("updated_verified", "unchanged_verified"):
                        raise AcceptanceError("The calendar correction did not produce verified readback.")
                details = {"receipt_path": result.get("receipt_path"), "result_status": result["status"]}
                if recovery is not None:
                    details["recovery"] = recovery
                passed(stage_name, **details)

            descriptions = preview["calendar_description_test"]
            calendar_change("calendar_description_edit", descriptions["temporary"])
            calendar_change("calendar_description_restore", descriptions["original"])
            if preview["event_date_test"]["enabled"]:
                event_service = EventUpdateService(authorization, config, folder / "event_dates", progress)
                for stage_name, restoring in (("event_date_edit", False), ("event_date_restore", True)):
                    if complete(stage_name):
                        continue
                    begin(stage_name)
                    stage = report["steps"].get(stage_name, {})
                    if stage.get("status") == "write_started":
                        result = event_service.reconcile_event_edit(stage["preview"]["preview_id"])
                    else:
                        test = preview["event_date_test"]
                        if restoring:
                            change = event_service.prepare_restore(report["steps"]["event_date_edit"]["receipt_id"])
                        else:
                            change = event_service.plan_event_edit(report["owned_event_ids"][test["event_code"]], test["temporary_start"], test["temporary_end"])
                        affected = {str(row["CalendarId"]) for row in change["affected_calendars"]}
                        expected_affected = {report["owned_calendar_ids"][link["calendar_key"]] for link in preview["plan"]["associations"] if link["event_code"] == test["event_code"]}
                        if affected != expected_affected:
                            raise AcceptanceError("The test event is now shared beyond its approved test calendars. No date write is allowed.")
                        report["steps"][stage_name] = {"status": "write_started", "preview": change}
                        save()
                        result = event_service.apply_event_edit(change)
                    if result.get("state") != "verified":
                        raise AcceptanceError("The event correction did not produce verified readback.")
                    passed(stage_name, receipt_id=result["receipt_id"])
            else:
                for name in ("event_date_edit", "event_date_restore"):
                    report["steps"][name] = {"status": "skipped", "reason": "Administrator assertions for global event scope were absent when this preview was approved."}
                save()
            if not complete("unchanged_rerun"):
                begin("unchanged_rerun")
                repeated = run_create(preview["plan"], authorization, folder / "creation", progress, config=config)
                if (repeated.get("environment") != environment
                        or (repeated.get("profile_id") is not None and config.get("profile_id") is not None
                            and repeated["profile_id"] != config["profile_id"])):
                    raise AcceptanceError("The rerun receipt belongs to another environment; acceptance cannot verify its results.")
                report["unchanged_rerun"] = repeated
                save()
                if repeated.get("status") != "applied_verified" or repeated.get("write_requests") != 0:
                    raise AcceptanceError("The unchanged rerun did not verify zero write requests.")
                passed("unchanged_rerun", write_requests=0, receipt_path=repeated.get("receipt_path"))
            begin("final_readback")
            final = _inventory(service, environment)
            ids = _verify_created(final, preview, journal)
            passed("final_readback", counts=preview["counts"])
            before = report["steps"]["baseline_read"]["fingerprints"]
            after = _record_fingerprints(final, exclude=set(ids.values()))
            changed = sorted(cid for cid, digest in before.items() if after.get(cid) != digest)
            if changed:
                report["changed_preexisting_calendar_ids"] = changed
                raise AcceptanceError("Preexisting calendar data changed during the test. This may include external changes; acceptance cannot claim it stayed unchanged.")
            passed("preexisting_data_comparison", unchanged_calendars=len(before))
            report.update(status="completed_with_limitations", completed_at=_now(),
                          conclusion="Enabled live tests passed. Deletion was blocked and any skipped tests were not certified.",
                          recovery_required=False)
            for key in ("error", "detail", "possible_temporary_changes", "stopped_at"):
                report.pop(key, None)
        except Exception as error:
            stage = report.get("current_stage", "unknown")
            report.update(status="stopped", stopped_at=_now(),
                          error="Stopped during %s (%s). The test did not finish; resume this saved request to reconcile existing receipts." % (stage, type(error).__name__),
                          recovery_required=True)
            # These are known local exceptions with operator guidance, not raw
            # server bodies. Still redact credentials before storing/returning.
            if isinstance(error, (AcceptanceError, ValueError, OperationError)):
                report["detail"] = str(error)
            report["possible_temporary_changes"] = []
            if report["steps"].get("calendar_description_edit") and not complete("calendar_description_restore"):
                report["possible_temporary_changes"].append("Calendar A may retain the test description until its restoration stage verifies.")
            if report["steps"].get("event_date_edit") and not complete("event_date_restore"):
                report["possible_temporary_changes"].append("The first test event may retain shifted dates until its restoration stage verifies.")
        save()
        return _clean(report, authorization)
