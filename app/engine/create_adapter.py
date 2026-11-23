"""Credential-free UI adapter for the profile-configured creation engine.

The caller owns workbook presentation, credentials, and the stable request folder.
This module owns validation, a process lock, durable resume, and final readback.
It never reads stored credentials and never prompts or opens a browser.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import time
import uuid
from zipfile import BadZipFile
import xml.etree.ElementTree as ET

from . import create_pilot as core
from environments import normalize_origin


def _text(value, label, limit):
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(label + " must contain text without surrounding whitespace.")
    if len(value) > limit or any(ord(c) < 32 or c in "|\x7f" for c in value):
        raise ValueError(label + " contains a delimiter, control character, or exceeds its maximum length.")
    return value


def validate_create_plan(plan):
    """Defence in depth: validate every record before constructing an Oracle client."""
    if not isinstance(plan, dict):
        raise ValueError("The creation plan must be a JSON object.")
    normalize_origin(plan.get("environment"))
    for collection in ("events", "calendars", "associations"):
        if not isinstance(plan.get(collection), list) or not plan[collection]:
            raise ValueError("Creation requires a nonempty " + collection + " collection.")
        if any(not isinstance(row, dict) for row in plan[collection]):
            raise ValueError(collection + " contains a non-object row.")
    events, calendars = {}, {}
    folded_codes, folded_names = set(), set()
    for event in plan["events"]:
        code = _text(event.get("ShortCode"), "Event code", 60)
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", code):
            raise ValueError("Event codes must start with a letter and use letters, digits, and underscores.")
        if code.casefold() in folded_codes:
            raise ValueError("Duplicate event code: " + code)
        if event.get("CalendarEventId") is not None:
            raise ValueError("Create accepts new event identities only; use the explicit update operation for existing events.")
        folded_codes.add(code.casefold())
        _text(event.get("Name"), "Event name", 80)
        if event.get("expected_readback_AllDayFlag") is not False:
            raise ValueError("All day=Yes has no verified CalendarEvent write route in this release: " + code +
                             ". The requested value was not changed and no Oracle request was sent.")
        if event.get("HalfDayForElapsed") != "N" or event.get("Category") != "PH":
            raise ValueError("The verified loader supports Public holiday (PH) and Half day=No only: " + code)
        bounds = []
        for field in ("StartDateTimeLocal", "EndDateTimeLocal"):
            value = event.get(field)
            if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", value):
                raise ValueError(code + ": " + field + " requires YYYY-MM-DDTHH:MM:SS without a time-zone suffix.")
            bounds.append(datetime.fromisoformat(value))
        if bounds[1] <= bounds[0]:
            raise ValueError("Event exclusive end must follow its start: " + code)
        events[code] = bounds
    for calendar in plan["calendars"]:
        key = _text(calendar.get("key"), "Calendar key", 60)
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", key) or key in calendars:
            raise ValueError("Calendar keys must be unique and use letters, digits, and underscores.")
        name = _text(calendar.get("Name"), "Calendar name", 240)
        if name.casefold() in folded_names or calendar.get("CalendarId") is not None:
            raise ValueError("Create requires distinct new calendar names and no existing CalendarId.")
        folded_names.add(name.casefold())
        description = calendar.get("Description", "")
        if description != "":
            _text(description, "Calendar description", 2000)
        if "Description" not in calendar:
            raise ValueError("Calendar Description is required; use an empty string if it is intentionally blank.")
        bounds = []
        for field in ("StartDate", "EndDate"):
            value = calendar.get(field)
            if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError(key + ": " + field + " requires YYYY-MM-DD.")
            bounds.append(date.fromisoformat(value))
        if bounds[1] < bounds[0]:
            raise ValueError("Calendar end precedes its start: " + key)
        calendars[key] = bounds
    links, used_events, used_calendars = set(), set(), set()
    for link in plan["associations"]:
        code, key = link.get("event_code"), link.get("calendar_key")
        if not isinstance(code, str) or not isinstance(key, str) or code not in events or key not in calendars:
            raise ValueError("Every association must reference an event and calendar in this request.")
        if (key, code) in links:
            raise ValueError("Duplicate calendar/event association: " + key + ":" + code)
        start, end = events[code]
        first, last = calendars[key]
        if start.date() < first or end > datetime.combine(last + timedelta(days=1), datetime.min.time()):
            raise ValueError("Event interval is outside its selected calendar period: " + key + ":" + code)
        links.add((key, code)); used_events.add(code); used_calendars.add(key)
    if used_events != set(events) or used_calendars != set(calendars):
        raise ValueError("Every new event and calendar requires at least one selected association for independent readback.")
    return plan


def _validate_authorization(authorization):
    if not isinstance(authorization, str) or not authorization.startswith("Basic "):
        raise ValueError("Oracle Basic authorization must be supplied by the local credential prompt.")
    try:
        value = base64.b64decode(authorization[6:], validate=True).decode("utf-8")
        username, separator, password = value.partition(":")
        valid = bool(username and separator and password) and not any(c in username for c in "\r\n")
        del value, username, password
    except (ValueError, UnicodeError):
        valid = False
    if not valid:
        raise ValueError("Oracle authorization is malformed; enter credentials again.")


def validate_plan(plan) -> None:
    """Public preview validation; no filesystem access, credentials, or Oracle calls."""
    validate_create_plan(deepcopy(plan))


def _validate_journal(journal, plan):
    signature = core.plan_signature(plan)
    if journal.get("plan_signature") not in (None, signature):
        raise ValueError("Workbook data differs from this request's saved state. Resume with its original workbook; use a new request for a new build.")
    for collection in ("events", "calendars", "links"):
        if not isinstance(journal.get(collection), dict):
            raise ValueError("The saved creation journal is invalid: " + collection)
    has_identity = any(journal[part] for part in ("events", "calendars", "links")) or journal.get("master_submission")
    if has_identity and journal.get("plan_signature") is None:
        raise ValueError("Saved Oracle identities have no plan signature. Their ownership must be reconciled before creation.")
    event_codes = {event["ShortCode"] for event in plan["events"]}
    calendar_keys = {calendar["key"] for calendar in plan["calendars"]}
    if set(journal["events"]) - event_codes or set(journal["calendars"]) - calendar_keys:
        raise ValueError("Saved creation identities belong to a different request.")
    journal["plan_signature"] = signature


@contextmanager
def request_lock(folder):
    """OS-released lock prevents two launcher processes writing the same request."""
    folder.mkdir(parents=True, exist_ok=True)
    handle = (folder / "creation.lock").open("a+b")
    acquired = False
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0"); handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            raise core.PilotError("This request is already running in another launcher window. Wait for that run to finish.") from None
        yield
    finally:
        if acquired:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def run_create(plan: dict, authorization: str, output_dir: Path, progress_callback=None, config=None) -> dict:
    """Create or resume, independently verify, and automatically check a zero-write rerun.

    Successful status is ``applied_verified``. Other statuses are ``pending``,
    ``interrupted``, and ``failed``; inspect ``error`` and preserved ``journal``.
    No exception text contains the supplied authorization or password.
    """
    started = time.monotonic()
    folder = Path(output_dir).resolve()
    report = {"operation": "create", "environment": None, "status": "failed", "started_utc": core.now()}
    client = None
    storage_token = core.STORAGE_AUTHORIZATION.set(authorization)
    try:
        working_plan = deepcopy(plan)
        json.dumps(working_plan, allow_nan=False)
        validate_create_plan(working_plan)
        _validate_authorization(authorization)
        config = {} if config is None else dict(config)
        config["environment"] = normalize_origin(config.get("environment"))
        if normalize_origin(working_plan["environment"]) != config["environment"]:
            raise ValueError("Creation plan and selected environment do not match. Review the workbook in the selected environment again.")
        working_plan["environment"] = config["environment"]
        report["environment"] = config["environment"]
        if config.get("profile_id"):
            report["profile_id"] = config["profile_id"]
        if config.get("profile_name"):
            report["profile_name"] = config["profile_name"]
        timeouts = {}
        for field, default, low, high in (("request_timeout_seconds", 30, 5, 60), ("poll_timeout_seconds", 300, 30, 600)):
            value = config.get(field, default)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
                raise ValueError(field + " must be between " + str(low) + " and " + str(high) + " seconds.")
            timeouts[field] = value
        report["effective_timeouts"] = timeouts
        report["plan"] = working_plan
        with request_lock(folder):
            journal = core.load_journal(folder, config["environment"], config.get("profile_id"))
            report["journal"] = journal
            _validate_journal(journal, working_plan)
            core.save_journal(folder, journal)
            client = core.Client(authorization, config=config)
            client.diagnostic_dir = folder
            client.show_progress = False
            client.progress_callback = progress_callback
            client.run_started = started
            client.request_timeout_seconds = timeouts["request_timeout_seconds"]
            client.soap_timeout_seconds = timeouts["request_timeout_seconds"]
            client.poll_timeout_seconds = timeouts["poll_timeout_seconds"]
            client.baseline_generation = "baseline:" + uuid.uuid4().hex
            core.progress(client, "Validating existing records and resuming this calendar request...")
            core.apply_pilot(client, working_plan, journal, report, folder)
            report["initial_build_status"] = report["status"]
            report["status"] = "repeatability_check_incomplete"
            before = len(client.requests)
            client.creation_requests_disabled = True
            core.progress(client, "Checking the unchanged rerun with writes disabled...")
            repeated = {}
            core.apply_pilot(client, working_plan, journal, repeated, folder)
            if any(request.get("method") != "GET" for request in client.requests[before:]):
                raise core.PilotError("Unchanged rerun attempted a write; repeatability was not verified.")
            report["repeatability"] = {"status": "verified", "write_requests": 0, "result_counts": repeated["result_counts"]}
            report["status"] = "applied_verified"
    except core.OraclePending as error:
        report["status"] = "pending"; report["error"] = str(error)
    except KeyboardInterrupt:
        report["status"] = "interrupted"
        report["error"] = "Stopped locally. Oracle work may continue; reopen this same request to reconcile and resume."
    except (core.ReadError, OSError, ValueError, TypeError, KeyError, BadZipFile, ET.ParseError) as error:
        report["status"] = "failed"; report["error"] = str(error)
    except Exception as error:
        # Keep partial state reviewable even when an unanticipated response shape
        # or UI callback causes an internal error. Never retry a write here.
        report["status"] = "failed"
        report["error"] = "Unexpected " + type(error).__name__ + "; remaining writes stopped. Preserve the request folder for reconciliation."
    finally:
        if client is not None:
            report["requests"] = client.requests
            client.authorization = ""
            report["last_stage"] = getattr(client, "last_stage", "")
        report["requests"] = report.get("requests", [])
        report["write_requests"] = sum(request.get("method") != "GET" for request in report["requests"])
        report["elapsed_seconds"] = round(time.monotonic() - started, 2)
        report = core.redact_storage(report, authorization)
        # Keep the established prefix so uncertain-upload reconciliation can find
        # these receipts without guessing or repeating a submitted request.
        receipt = folder / ("K12_SAMPLE_A_Create_Pilot_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f") + ".json")
        report["receipt_path"] = str(receipt)
        try:
            folder.mkdir(parents=True, exist_ok=True)
            receipt.write_text(json.dumps(report, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
        except (OSError, TypeError, ValueError):
            report["status"] = "failed"
            report["error"] = "The run receipt could not be saved. Preserve this request folder and inspect its journal before retrying."
        core.STORAGE_AUTHORIZATION.reset(storage_token)
    return report
