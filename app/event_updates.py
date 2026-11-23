"""Reviewed date corrections for existing CalendarEvent masters.

The HDL transport is the tenant-proven sparse MERGE by CalendarEventId.  An
uncertain POST is never repeated. Receipts remain available for read-only
reconciliation and inverse corrections must be reviewed as new previews.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid
import zipfile

from engine import create_pilot as engine
from environments import normalize_origin


class EventUpdateError(ValueError):
    pass


class EventUpdatePending(EventUpdateError):
    """The recorded submission needs reconciliation, never a repeat POST."""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _id(value, label="Oracle ID"):
    if isinstance(value, bool) or not re.fullmatch(r"[1-9][0-9]*", str(value)):
        raise EventUpdateError(label + " must be a positive numeric Oracle ID.")
    return str(value)


def _timestamp(value, *, oracle=False):
    if oracle and isinstance(value, str):
        value = re.sub(r"\.0+(?=Z$|\+00:00$)", "", value).replace("Z", "+00:00")
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T00:00:00\+00:00", value):
        raise EventUpdateError("Date boundaries must be exact UTC midnight timestamps, for example 2027-01-18T00:00:00+00:00.")
    try:
        datetime.fromisoformat(value)
    except ValueError as exc:
        raise EventUpdateError("A date boundary is not a valid calendar date.") from exc
    return value


def _atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


@contextmanager
def _event_lock(path):
    """Kernel locks release after a process crash; a stale file is harmless."""
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    stream.seek(0, 2)
    if stream.tell() == 0:
        stream.write(b"0")
        stream.flush()
    stream.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        stream.close()
        raise EventUpdateError("Another correction for this event is running. Wait for its result.") from exc
    try:
        yield
    finally:
        if os.name == "nt":
            import msvcrt
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


class EventUpdateService:
    def __init__(self, authorization, config, state_dir, progress_callback=None, *, client=None, calendar_service=None):
        self.config = dict(config)
        try:
            self.environment = normalize_origin(self.config.get("environment"))
        except ValueError as error:
            raise EventUpdateError(str(error)) from None
        self.config["environment"] = self.environment
        self.folder = Path(state_dir).resolve() / "event_edits"
        self.folder.mkdir(parents=True, exist_ok=True)
        self.client = client if client is not None else engine.Client(authorization, config=self.config)
        self.progress_callback = progress_callback
        self.calendar_service = calendar_service
        self.authorization = authorization
        self.state_dir = Path(state_dir)

    def _require_scope_assertions(self):
        required = ("administrator_asserted_full_calendar_visibility", "administrator_asserted_no_legacy_event_coverage")
        if any(self.config.get(key) is not True for key in required):
            raise EventUpdateError(
                "Shared event date editing is disabled until an administrator confirms full calendar visibility "
                "and confirms that the events have no legacy coverage outside these calendars. "
                "Account-visible pagination alone cannot establish safe global impact.")
        return {"assurance_type": "administrator_assertion_not_technical_proof", **{key: True for key in required}}

    def _progress(self, text):
        if self.progress_callback:
            self.progress_callback(text)

    def _scan(self, event_id):
        if self.calendar_service is None:
            from operations import CalendarService
            self.calendar_service = CalendarService(self.authorization, self.config, self.state_dir, self.progress_callback)
        self._progress("Reading all visible calendars to identify shared event use...")
        snapshot = self.calendar_service.read()
        if snapshot.get("complete") is not True or snapshot.get("environment") != self.environment:
            raise EventUpdateError("The visible calendar inventory is incomplete or belongs to another environment; no event correction is allowed.")
        calendars = snapshot.get("calendars")
        if not isinstance(calendars, list):
            raise EventUpdateError("The calendar inventory is invalid.")
        master = None
        affected = []
        associations = []
        seen_calendars = set()
        seen_links = set()
        for calendar in calendars:
            calendar_id = _id(calendar.get("CalendarId"), "CalendarId")
            if calendar_id in seen_calendars:
                raise EventUpdateError("The calendar inventory contains duplicate calendars.")
            seen_calendars.add(calendar_id)
            events = calendar.get("events")
            if not isinstance(events, list):
                raise EventUpdateError("A calendar has no complete event inventory.")
            found = False
            for row in events:
                if str(row.get("CalendarEventId")) != event_id:
                    continue
                if _id(row.get("CalendarId"), "Association CalendarId") != calendar_id:
                    raise EventUpdateError("An event association has an inconsistent CalendarId.")
                link_id = _id(row.get("CalendarCalEventId"), "CalendarCalEventId")
                if link_id in seen_links or found:
                    raise EventUpdateError("The event has duplicated or ambiguous calendar associations.")
                seen_links.add(link_id)
                found = True
                fields = ("ShortCode", "Name", "CategoryCode", "AllDayFlag", "HalfDayForElapsed", "EventType")
                if any(key not in row for key in fields):
                    raise EventUpdateError("Event readback is missing fields required for a safe correction.")
                current = {key: row[key] for key in fields}
                current.update(CalendarEventId=event_id,
                               StartDateTime=_timestamp(row.get("StartDateTime"), oracle=True),
                               EndDateTime=_timestamp(row.get("EndDateTime"), oracle=True))
                if current["AllDayFlag"] is not False or current["HalfDayForElapsed"] != "N" or current["CategoryCode"] != "PH":
                    raise EventUpdateError("Date editing currently supports Public holiday events with All day = No and Half day = No only; no write was submitted.")
                if current["EventType"] != "EVENT":
                    raise EventUpdateError("Only CalendarEvent master associations can be corrected.")
                if master is not None and current != master:
                    raise EventUpdateError("Associations disagree about this shared event; no write was submitted.")
                master = current
                associations.append({"CalendarId": calendar_id, "CalendarCalEventId": link_id})
            if found:
                if not isinstance(calendar.get("Name"), str) or not calendar["Name"]:
                    raise EventUpdateError("An affected calendar has no readable name.")
                affected.append({key: calendar.get(key) for key in ("Name", "StartDate", "EndDate")})
                affected[-1]["CalendarId"] = calendar_id
        if master is None:
            raise EventUpdateError("This event was not found in any visible calendar. Its shared impact cannot be reviewed.")
        return {"event": master, "affected_calendars": sorted(affected, key=lambda row: row["CalendarId"]),
                "associations": sorted(associations, key=lambda row: (row["CalendarId"], row["CalendarCalEventId"]))}

    def plan_event_edit(self, calendar_event_id, new_start, new_end):
        scope_assertions = self._require_scope_assertions()
        event_id = _id(calendar_event_id, "CalendarEventId")
        new_start, new_end = _timestamp(new_start), _timestamp(new_end)
        if new_end <= new_start:
            raise EventUpdateError("The exclusive end must be later than the start.")
        observed = self._scan(event_id)
        start_day = datetime.fromisoformat(new_start).date()
        last_day = (datetime.fromisoformat(new_end) - timedelta(days=1)).date()
        for calendar in observed["affected_calendars"]:
            try:
                start = datetime.strptime(str(calendar["StartDate"]), "%Y-%m-%d").date()
                end = datetime.strptime(str(calendar["EndDate"]), "%Y-%m-%d").date() if calendar["EndDate"] else None
            except ValueError as exc:
                raise EventUpdateError("An affected calendar has unreadable date boundaries.") from exc
            if start_day < start or (end and last_day > end):
                raise EventUpdateError("The proposed event lies outside affected calendar " + calendar["Name"] + ".")
        before = observed["event"]
        after = dict(before, StartDateTime=new_start, EndDateTime=new_end)
        preview = {"schema": "K12-EVENT-DATE-EDIT-1", "preview_id": uuid.uuid4().hex,
                   "environment": self.environment, "created_utc": _now(), "operation": "event_date_edit",
                   "CalendarEventId": event_id, "old": before, "new": after,
                   "affected_calendars": observed["affected_calendars"], "associations": observed["associations"],
                   "calendar_count": len(observed["affected_calendars"]), "association_count": len(observed["associations"]),
                   "shared_impact": len(observed["affected_calendars"]) > 1,
                   "scope": "All calendars visible to the signed-in account; uses hidden by Oracle data security cannot be enumerated.",
                   "scope_assertions": scope_assertions,
                   "end_boundary": "exclusive", "unchanged": before == after,
                   "observed_fingerprint": _digest(observed)}
        _atomic_json(self.folder / "previews" / (preview["preview_id"] + ".json"), preview)
        return preview

    def _receipt_path(self, receipt_id):
        if not isinstance(receipt_id, str) or not re.fullmatch(r"[a-f0-9]{32}", receipt_id):
            raise EventUpdateError("Invalid event correction receipt ID.")
        return self.folder / "receipts" / (receipt_id + ".json")

    def _read_json(self, path):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise EventUpdateError("The saved event correction state cannot be read: " + path.name) from exc
        if not isinstance(value, dict) or value.get("environment") != self.environment:
            raise EventUpdateError("Saved event correction state is invalid or belongs to another environment.")
        return value

    def _save(self, receipt):
        # A gateway or Oracle diagnostic can echo supplied credentials inside a
        # string field. Scrub the known values recursively before retaining any
        # status object in memory or writing its receipt. Never save the secrets.
        sensitive = []
        if isinstance(self.authorization, str) and self.authorization:
            sensitive.append(self.authorization)
            if self.authorization.startswith("Basic "):
                encoded = self.authorization[6:]
                sensitive.append(encoded)
                try:
                    decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
                    _, separator, password = decoded.partition(":")
                    if separator and password:
                        sensitive.append(password)
                except (ValueError, UnicodeError):
                    pass
        sensitive.sort(key=len, reverse=True)

        def clean(value):
            if isinstance(value, dict):
                return {clean(key): clean(item) for key, item in value.items()}
            if isinstance(value, list):
                return [clean(item) for item in value]
            if isinstance(value, str):
                for secret in sensitive:
                    value = value.replace(secret, "[redacted]")
                return re.sub(r"Basic\s+[A-Za-z0-9+/=]+", "Basic [redacted]", value, flags=re.IGNORECASE)
            return value

        sanitized = clean(receipt)
        receipt.clear()
        receipt.update(sanitized)
        receipt["updated_utc"] = _now()
        _atomic_json(self._receipt_path(receipt["receipt_id"]), receipt)

    def _reviewed_preview(self, preview):
        if not isinstance(preview, dict):
            raise EventUpdateError("A saved reviewed preview is required.")
        preview_id = preview.get("preview_id")
        self._receipt_path(preview_id)
        saved = self._read_json(self.folder / "previews" / (preview_id + ".json"))
        if saved != preview:
            raise EventUpdateError("The preview changed after it was prepared. Prepare and review a fresh preview.")
        return saved

    def _check_other_pending(self, event_id, receipt_id):
        for path in (self.folder / "receipts").glob("*.json"):
            other = self._read_json(path)
            if other.get("receipt_id") != receipt_id and other.get("CalendarEventId") == event_id and other.get("state") not in ("verified", "no_change", "conflict_before_submission"):
                raise EventUpdatePending("An earlier correction for this event is unresolved. Reconcile receipt " + other["receipt_id"] + " before another correction.")

    def _fresh_unchanged(self, preview):
        if _digest(self._scan(preview["CalendarEventId"])) != preview["observed_fingerprint"]:
            raise EventUpdateError("The event or its calendar associations changed after preview. Nothing was submitted; prepare a fresh preview.")

    def apply_event_edit(self, preview):
        self._require_scope_assertions()
        preview = self._reviewed_preview(preview)
        event_id, receipt_id = preview["CalendarEventId"], preview["preview_id"]
        with _event_lock(self.folder / "locks" / (event_id + ".lock")):
            self._check_other_pending(event_id, receipt_id)
            path = self._receipt_path(receipt_id)
            if path.exists():
                receipt = self._read_json(path)
                if receipt.get("preview") != preview:
                    raise EventUpdateError("The receipt does not match the reviewed preview.")
            else:
                self._fresh_unchanged(preview)
                receipt = {"schema": "K12-EVENT-DATE-RECEIPT-1", "receipt_id": receipt_id,
                           "environment": self.environment, "CalendarEventId": event_id,
                           "created_utc": _now(), "preview": preview, "state": "prepared"}
                self._save(receipt)
            try:
                state = receipt["state"]
                if state == "prepared":
                    self._fresh_unchanged(preview)
                    if preview["unchanged"]:
                        receipt.update(state="no_change", status="no_change", verified_utc=_now())
                        self._save(receipt)
                        return receipt
                    self._upload(receipt)
                if receipt["state"] == "uploaded":
                    try:
                        self._fresh_unchanged(preview)
                    except EventUpdateError:
                        receipt["state"] = "conflict_before_submission"
                        self._save(receipt)
                        raise
                    self._submit(receipt)
                return self._reconcile(receipt)
            except Exception as exc:
                # Server-returned error text may echo credentials. Persist only
                # our state and exception type, never a raw transport exception.
                receipt["last_error"] = "Stopped in state %s (%s); no automatic repeat POST." % (receipt["state"], type(exc).__name__)
                self._save(receipt)
                raise

    def _upload(self, receipt):
        new = receipt["preview"]["new"]
        dat = "METADATA|CalendarEvent|CalendarEventId|StartDateTime|EndDateTime\nMERGE|CalendarEvent|%s|%s|%s\n" % (
            receipt["CalendarEventId"], engine.hdl_datetime(new["StartDateTime"]), engine.hdl_datetime(new["EndDateTime"]))
        archive = self.folder / ("EventCorrection_" + receipt["receipt_id"] + ".zip")
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as handle:
            handle.writestr("CalendarEvent.dat", dat)
        payload = archive.read_bytes()
        receipt.update(state="upload_pending", archive_file=archive.name, archive_sha256=hashlib.sha256(payload).hexdigest(), hdl=dat)
        self._save(receipt)  # Durable marker must precede the first POST.
        self._progress("Uploading the reviewed event date correction...")
        response = self.client.post(engine.DATASETS + "/action/uploadFile",
                                    {"content": base64.b64encode(payload).decode("ascii"), "fileName": archive.name},
                                    engine.HDL_ACTION_CONTENT_TYPE)
        content_id = response.get("result", {}).get("ContentId")
        if not isinstance(content_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", content_id):
            raise EventUpdatePending("Upload returned no usable ContentId. Its outcome is uncertain; receipt " + receipt["receipt_id"] + " blocks repeat upload.")
        receipt.update(state="uploaded", ContentId=content_id)
        self._save(receipt)

    def _submit(self, receipt):
        receipt["state"] = "submission_pending"
        self._save(receipt)
        self._progress("Submitting the existing-event correction to HCM Data Loader...")
        response = self.client.post(engine.DATASETS + "/action/createFileDataSet",
                                    {"contentId": receipt["ContentId"], "fileAction": "IMPORT_AND_LOAD",
                                     "dataSetName": "K12 Event correction " + receipt["receipt_id"]},
                                    engine.HDL_ACTION_CONTENT_TYPE)
        request_id = response.get("result", {}).get("RequestId")
        if isinstance(request_id, bool) or not str(request_id).isdigit():
            raise EventUpdatePending("Load returned no RequestId. Reconcile ContentId " + receipt["ContentId"] + "; no repeat load is allowed.")
        receipt.update(state="submitted", RequestId=str(request_id), submitted_utc=_now())
        self._save(receipt)

    def _status(self, receipt):
        content_id = receipt["ContentId"]
        page = self.client.get(engine.DATASETS, {"q": "ContentId='" + content_id + "'", "limit": 10, "offset": 0})
        if not isinstance(page.get("items"), list) or page.get("hasMore") is not False:
            raise EventUpdatePending("HDL ContentId lookup is incomplete. The recorded load will not be repeated.")
        matches = [row for row in page["items"] if str(row.get("ContentId")) == content_id]
        if len(matches) != 1:
            raise EventUpdatePending("HDL ContentId lookup did not return exactly one load. Retry read-only reconciliation later.")
        status = matches[0]
        receipt["status_RequestId"] = _id(status.get("RequestId"), "HDL status RequestId")
        receipt["last_data_set_status"] = status
        self._save(receipt)
        return status

    def _business_objects(self, request_id):
        rows, offset = [], 0
        for _ in range(100):
            page = self.client.get(engine.DATASETS + "/" + request_id + "/child/businessObjects", {"limit": 100, "offset": offset})
            items = page.get("items")
            if not isinstance(items, list) or not isinstance(page.get("hasMore"), bool):
                raise EventUpdatePending("HDL business-object verification is incomplete.")
            rows.extend(items)
            if page["hasMore"] is False:
                return rows
            if not items:
                break
            offset += len(items)
        raise EventUpdatePending("HDL business-object pagination was incomplete.")

    def _verify(self, receipt):
        preview = receipt["preview"]
        fresh = self._scan(receipt["CalendarEventId"])
        expected = {"event": preview["new"], "affected_calendars": preview["affected_calendars"], "associations": preview["associations"]}
        if fresh != expected:
            receipt.update(state="readback_mismatch", readback=fresh)
            self._save(receipt)
            raise EventUpdateError("Oracle readback differs from the reviewed correction or shared calendar impact. No further write was made; inspect receipt " + receipt["receipt_id"] + ".")
        receipt.update(state="verified", status="verified", verified_utc=_now(), readback=fresh)
        self._save(receipt)
        self._progress("Event dates verified across %s visible calendars." % preview["calendar_count"])
        return receipt

    def _reconcile(self, receipt):
        state = receipt["state"]
        if state == "no_change":
            self._fresh_unchanged(receipt["preview"])
            return receipt
        if state in ("verified", "loaded", "readback_mismatch"):
            return self._verify(receipt)
        if state in ("upload_pending", "prepared", "uploaded", "conflict_before_submission"):
            raise EventUpdatePending("Receipt " + receipt["receipt_id"] + " is " + state + "; no load was repeated. An uncertain upload cannot be automatically retried.")
        if state == "load_failed":
            raise EventUpdateError("The recorded HDL load failed. No automatic resubmission is permitted; inspect receipt " + receipt["receipt_id"] + ".")
        if state not in ("submission_pending", "submitted"):
            raise EventUpdateError("Unrecognized saved correction state; no writes allowed.")
        deadline = time.monotonic() + min(600, max(0, float(self.config.get("poll_timeout_seconds", 300))))
        while True:
            status = self._status(receipt)
            code = status.get("DataSetStatusCode")
            self._progress("Event correction load %s: %s" % (receipt["ContentId"], code or "pending"))
            if code in ("ORA_COMPLETE", "ORA_SUCCESS", "ORA_IN_ERROR", "ORA_CANCELLED", "ORA_TERMINATED"):
                objects = self._business_objects(receipt["status_RequestId"])
                receipt["business_objects"] = objects
                event_objects = [row for row in objects if re.sub(r"\s+", "", str(row.get("BusinessObjectName", ""))) == "CalendarEvent"]
                okay = (code in ("ORA_COMPLETE", "ORA_SUCCESS") and status.get("ImportStatusCode") == "SUCCESS"
                        and status.get("LoadStatusCode") == "SUCCESS" and status.get("ObjectLoadErrorCount", 0) in (0, None)
                        and len(event_objects) == 1 and event_objects[0].get("LoadStatusCode") == "SUCCESS"
                        and event_objects[0].get("ObjectLoadErrorCount", 0) in (0, None))
                receipt["state"] = "loaded" if okay else "load_failed"
                self._save(receipt)
                if not okay:
                    raise EventUpdateError("HCM Data Loader did not report a successful event correction. No retry was submitted; inspect receipt " + receipt["receipt_id"] + ".")
                return self._verify(receipt)
            if time.monotonic() >= deadline:
                raise EventUpdatePending("The saved load is still pending. Reconcile receipt " + receipt["receipt_id"] + "; it will not be submitted again.")
            time.sleep(min(5, max(0, deadline - time.monotonic())))

    def reconcile_event_edit(self, receipt_id):
        receipt = self._read_json(self._receipt_path(receipt_id))
        with _event_lock(self.folder / "locks" / (receipt["CalendarEventId"] + ".lock")):
            return self._reconcile(receipt)  # GETs only, including uncertain submit.

    def prepare_restore(self, receipt_id):
        receipt = self._read_json(self._receipt_path(receipt_id))
        if receipt.get("state") != "verified":
            raise EventUpdateError("Only a verified event correction can supply restoration dates.")
        original = receipt["preview"]
        current = self._scan(receipt["CalendarEventId"])
        expected = {"event": original["new"], "affected_calendars": original["affected_calendars"], "associations": original["associations"]}
        if current != expected:
            raise EventUpdateError("The event or its shared associations changed since this correction. Review a new date edit instead of overwriting later work.")
        preview = self.plan_event_edit(receipt["CalendarEventId"], original["old"]["StartDateTime"], original["old"]["EndDateTime"])
        if (preview["old"] != original["new"] or preview["affected_calendars"] != original["affected_calendars"]
                or preview["associations"] != original["associations"]):
            raise EventUpdateError("The event changed while preparing restoration. Prepare a new reviewed correction; restoration was not submitted.")
        preview["operation"] = "restore_event_dates"
        preview["restores_receipt_id"] = receipt_id
        _atomic_json(self.folder / "previews" / (preview["preview_id"] + ".json"), preview)
        return preview
