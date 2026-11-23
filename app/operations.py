"""Calendar read/maintenance service. Python 3.9+, standard library only.

Mutations use a fresh singular-resource GET and Oracle's If-Match contract.
Deletion is intentionally unavailable until authoritative assignment and atomic
deletion semantics have been established; rule subscribers are not workers.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
import uuid

from environments import normalize_origin


DEFAULT_BASE = "https://tenant-a.example.invalid"
API = "/hcmRestApi/resources/11.13.18.05"
CALENDARS = API + "/absenceCalendars"
CALENDAR_FIELDS = ("CalendarId", "Name", "Description", "StartDate", "EndDate", "LastUpdateDate")
EDIT_FIELDS = ("Name", "Description", "StartDate", "EndDate")
EVENT_FIELDS = ("CalendarCalEventId", "CalendarEventId", "CalendarId", "Name", "ShortCode",
                "Description", "StartDateTime", "EndDateTime", "AllDayFlag", "HalfDayForElapsed",
                "Category", "CategoryCode", "EventCategoryCode", "EventType", "LastUpdateDate")
SUBSCRIBER_FIELDS = ("CalendarSubscriberId", "CalendarId", "ListId", "Name", "Description",
                     "ActiveFlag", "EvaluateRuleMembershipFlag", "RuleLastRunDate", "RuleRunStatus",
                     "RuleRunStatusCode", "LastUpdateDate")
ROUTE = re.compile(re.escape(CALENDARS) + r"(?:/[0-9]+(?:/child/(?:AbsenceCalendarEvent|AbsenceCalendarSubscriber))?)?")
ITEM_ROUTE = re.compile(re.escape(CALENDARS) + r"/[0-9]+")


class OperationError(Exception):
    """User-safe error. No credentials or raw server responses are included."""

    def __init__(self, message, *, code="operation_failed", http_status=None, receipt_path=None):
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.receipt_path = receipt_path


class ConflictError(OperationError):
    def __init__(self, message="The calendar changed after review. Read it again and prepare a new preview."):
        super().__init__(message, code="conflict")


@dataclass
class Response:
    status: int
    data: dict
    headers: dict


def _now():
    return datetime.now(timezone.utc).isoformat()


def _id(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not re.fullmatch(r"[0-9]+", str(value)):
        raise OperationError("Oracle returned a missing or invalid record identifier.", code="invalid_identifier")
    return str(value)


def _environment(config):
    try:
        candidates = [normalize_origin(config[key]) for key in ("environment", "base_url", "oracle_base_url") if key in config]
    except ValueError as error:
        raise OperationError(str(error), code="environment_mismatch") from None
    if candidates and len(set(candidates)) != 1:
        raise OperationError("Environment URL settings conflict.", code="environment_mismatch")
    if not candidates:
        raise OperationError("Select an Oracle environment before continuing.", code="environment_required")
    return candidates[0]


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep the temporary sibling short.  Some legitimate state folders are
    # deeply nested, and appending an identifier to an already long receipt
    # name can exceed Windows' legacy path limit before durability is proven.
    temp = path.parent / (".tmp-" + uuid.uuid4().hex)
    try:
        with temp.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise OperationError("Oracle redirected the request. No credentials were forwarded.", code="redirect_blocked")


class CalendarClient:
    """One request per call; no automatic write retry and no cross-host routing."""

    def __init__(self, authorization, config=None):
        self.config = config or {}
        self.environment = _environment(self.config)
        if not isinstance(authorization, str) or not authorization or any(c in authorization for c in "\r\n"):
            raise OperationError("A valid local authentication session is required.", code="authentication_required")
        self._authorization = authorization
        self.requests = []
        self.timeout = self.config.get("request_timeout_seconds", self.config.get("timeout_seconds", 30))
        if ("request_timeout_seconds" in self.config and "timeout_seconds" in self.config
                and self.config["request_timeout_seconds"] != self.config["timeout_seconds"]):
            raise OperationError("Network timeout settings conflict.", code="invalid_configuration")
        if type(self.timeout) not in (int, float) or not 1 <= self.timeout <= 60:
            raise OperationError("Network timeout must be between 1 and 60 seconds.", code="invalid_configuration")
        context = ssl.create_default_context(cafile=self.config.get("ca_file") or None)
        self.opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context), _NoRedirect())

    def request(self, method, resource, *, params=None, payload=None, headers=None):
        if not ROUTE.fullmatch(resource) or method not in ("GET", "PATCH"):
            raise OperationError("Request outside the supported calendar maintenance scope was blocked.", code="scope_blocked")
        if method == "PATCH" and (not ITEM_ROUTE.fullmatch(resource) or not isinstance(payload, dict)
                                   or not payload or set(payload) - set(EDIT_FIELDS)):
            raise OperationError("Only a calendar's name, description and period can be updated.", code="scope_blocked")
        extra = headers or {}
        if set(extra) - {"If-Match"}:
            raise OperationError("Unsupported request header.", code="scope_blocked")
        if method == "PATCH" and not _valid_etag(extra.get("If-Match")):
            raise OperationError("Oracle did not supply a usable ETag; update is blocked.", code="etag_required")
        request_headers = {"Authorization": self._authorization, "Accept": "application/json",
                           "REST-Framework-Version": "4", **extra}
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        url = self.environment + resource
        if params:
            url += "?" + urllib.parse.urlencode(params)
        log = {"method": method, "resource": resource, "query_parameters": copy.deepcopy(params or {})}
        self.requests.append(log)
        try:
            with self.opener.open(urllib.request.Request(url, data=body, headers=request_headers, method=method), timeout=self.timeout) as response:
                log["http_status"] = response.status
                if response.status not in ((200,) if method == "GET" else (200, 204)):
                    raise OperationError("Oracle returned an unexpected status; reconcile before repeating the operation.", code="unexpected_response")
                if response.status == 204:
                    return Response(204, {}, dict(response.headers))
                if "json" not in response.headers.get("Content-Type", "").lower():
                    raise OperationError("Oracle returned a non-JSON response; check the REST sign-in method.", code="unexpected_response")
                raw = response.read(8 * 1024 * 1024 + 1)
                if len(raw) > 8 * 1024 * 1024:
                    raise OperationError("Oracle response exceeded the page size limit.", code="incomplete_read")
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise OperationError("Oracle returned an invalid JSON resource.", code="unexpected_response")
                return Response(response.status, value, dict(response.headers))
        except urllib.error.HTTPError as error:
            log["http_status"] = error.code
            reasons = {400: "Oracle rejected the request.", 401: "Oracle did not accept the credentials.",
                       403: "Oracle or an access gateway denied this operation; the cause is not established.",
                       404: "Calendar not found or not visible to this account.",
                       412: "The calendar changed; prepare a fresh preview.",
                       429: "Oracle request limit reached. No automatic retry was attempted."}
            raise OperationError("HTTP %s: %s" % (error.code, reasons.get(error.code, "Oracle or a gateway returned an error.")),
                                 code="conflict" if error.code == 412 else "http_error", http_status=error.code) from None
        except (urllib.error.URLError, OSError, ssl.SSLError, ValueError, UnicodeError):
            raise OperationError("Network, TLS, or JSON failure. No automatic retry was attempted.", code="transport_error") from None


def _valid_etag(value):
    return isinstance(value, str) and bool(value.strip()) and value.strip() != "*" and not any(c in value for c in "\r\n")


def _etag(response):
    for key, value in response.headers.items():
        if key.lower() == "etag" and _valid_etag(value):
            return value
    # Framework 6+ can return this in the resource context. Never use a
    # collection ETag or a last-updated timestamp as a replacement.
    context = response.data.get("@context", {})
    value = context.get("headers", {}).get("ETag") if isinstance(context, dict) and isinstance(context.get("headers", {}), dict) else None
    return value if _valid_etag(value) else None


def _projection(row, fields):
    if not isinstance(row, dict):
        raise OperationError("Oracle returned an invalid calendar resource.", code="unexpected_response")
    value = {key: copy.deepcopy(row.get(key)) for key in fields}
    for key in fields:
        if key.endswith("Id") and value.get(key) is not None:
            value[key] = _id(value[key])
    return value


def _calendar(row, expected_id=None):
    result = _projection(row, CALENDAR_FIELDS)
    result["CalendarId"] = _id(result["CalendarId"])
    if expected_id is not None and result["CalendarId"] != expected_id:
        raise OperationError("Oracle returned a different calendar ID.", code="identity_mismatch")
    if not isinstance(result["Name"], str) or not result["Name"] or not result["StartDate"]:
        raise OperationError("Oracle calendar data is missing its name or start date.", code="incomplete_read")
    return result


def _changes(before, changes):
    if not isinstance(changes, dict) or not changes or set(changes) - set(EDIT_FIELDS):
        raise OperationError("Choose a calendar name, description, start date or end date to edit.", code="invalid_input")
    after = {field: before.get(field) for field in EDIT_FIELDS}
    for field, value in changes.items():
        if field in ("Description", "EndDate") and value == "":
            value = None
        if field in ("Name", "Description"):
            if value is None and field == "Description":
                after[field] = None
                continue
            maximum = 240 if field == "Name" else 2000
            if not isinstance(value, str) or len(value) > maximum or (field == "Name" and not value.strip()) or "\x00" in value:
                raise OperationError("%s must be text of at most %s characters%s." % (field, maximum, ", with a nonblank name" if field == "Name" else ""), code="invalid_input")
        else:
            if value is None and field == "EndDate":
                after[field] = None
                continue
            try:
                if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                    raise ValueError()
                date.fromisoformat(value)
            except ValueError:
                raise OperationError("%s must be a real date in YYYY-MM-DD format." % field, code="invalid_input") from None
        after[field] = value
    if after.get("EndDate") and after["EndDate"] < after["StartDate"]:
        raise OperationError("Calendar end date cannot precede its start date.", code="invalid_input")
    return after, {field: after[field] for field in EDIT_FIELDS if after[field] != before.get(field)}


class CalendarService:
    """Account-visible reads and reviewed, conditional calendar header edits.

    The optional client is a test seam: request(method, resource, *, params=None,
    payload=None, headers=None) -> Response(status, data, headers).
    """

    def __init__(self, authorization, config, state_dir, progress_callback=None, *, client=None):
        self.config = dict(config or {})
        self.environment = _environment(self.config)
        self.state_dir = Path(state_dir)
        self.client = client or CalendarClient(authorization, self.config)
        self.progress = progress_callback or (lambda message: None)
        self._plans = {}

    def _collection(self, resource, key, fields):
        rows, seen, offset = [], set(), 0
        for _ in range(100):
            response = self.client.request("GET", resource, params={"limit": 100, "offset": offset, "orderBy": key + ":asc"})
            data = response.data
            items = data.get("items")
            if (not isinstance(items, list) or any(not isinstance(row, dict) for row in items)
                    or type(data.get("hasMore")) is not bool
                    or ("offset" in data and (type(data["offset"]) is not int or data["offset"] != offset))
                    or ("count" in data and (type(data["count"]) is not int or data["count"] != len(items)))):
                raise OperationError("Oracle collection pagination is incomplete or inconsistent.", code="incomplete_read")
            identifiers = [_id(row.get(key)) for row in items]
            if len(identifiers) != len(set(identifiers)) or seen.intersection(identifiers):
                raise OperationError("Repeated IDs during pagination; read completeness is unverified.", code="incomplete_read")
            seen.update(identifiers)
            rows.extend(_projection(row, fields) for row in items)
            if data["hasMore"] is False:
                return rows
            if not items:
                raise OperationError("Oracle pagination made no progress.", code="incomplete_read")
            offset += len(items)
        raise OperationError("Oracle returned more than 100 pages; read completeness is unverified.", code="incomplete_read")

    def _item(self, calendar_id):
        cid = _id(calendar_id)
        response = self.client.request("GET", CALENDARS + "/" + cid)
        return _calendar(response.data, cid), _etag(response)

    def _children(self, cid):
        prefix = CALENDARS + "/" + cid + "/child/"
        events = self._collection(prefix + "AbsenceCalendarEvent", "CalendarCalEventId", EVENT_FIELDS)
        subscribers = self._collection(prefix + "AbsenceCalendarSubscriber", "CalendarSubscriberId", SUBSCRIBER_FIELDS)
        for event in events:
            event["CalendarEventId"] = _id(event.get("CalendarEventId"))
        for row in events + subscribers:
            if row.get("CalendarId") is not None and row["CalendarId"] != cid:
                raise OperationError("Oracle returned an association for another calendar.", code="identity_mismatch")
        return events, subscribers

    def read(self):
        self.progress("Reading calendars visible to this account...")
        headers = self._collection(CALENDARS, "CalendarId", CALENDAR_FIELDS)
        calendars = []
        for row in headers:
            value = _calendar(row)
            self.progress("Reading events and rule subscriptions: " + value["Name"])
            value["events"], value["subscribers"] = self._children(value["CalendarId"])
            calendars.append(value)
        masters = {event["CalendarEventId"] for calendar in calendars for event in calendar["events"]}
        result = {"schema": "K12-CALENDAR-READ-1", "status": "read_verified", "environment": self.environment,
                  "captured_at": _now(), "scope": "All calendars visible to the authenticated account",
                  "complete": True, "assignment_coverage": "unknown", "calendars": calendars,
                  "counts": {"calendars": len(calendars), "events": len(masters),
                             "links": sum(len(row["events"]) for row in calendars),
                             "rule_subscriptions": sum(len(row["subscribers"]) for row in calendars)}}
        _atomic_json(self.state_dir / "last_calendar_read.json", result)
        return result

    def plan_calendar_edit(self, calendar_id, changes):
        cid = _id(calendar_id)
        for path in self.state_dir.glob("Calendar_Edit_*.json"):
            try:
                old = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raise OperationError("A previous calendar-edit receipt cannot be read. Restore it before another edit.", code="invalid_receipt", receipt_path=str(path)) from None
            if (old.get("calendar_id") == cid and old.get("environment") == self.environment
                    and old.get("status") in ("submission_pending", "submitted_awaiting_readback", "outcome_unknown")):
                raise OperationError("A previous edit of this calendar has an uncertain outcome. Reconcile that receipt before another edit.", code="reconciliation_required", receipt_path=str(path))
        before, etag = self._item(cid)
        after, patch = _changes(before, changes)
        if patch and not etag:
            raise OperationError("Oracle supplied no ETag for this calendar. A concurrency-safe update is unavailable; nothing was written.", code="etag_required")
        if "Name" in patch:
            others = self._collection(CALENDARS, "CalendarId", CALENDAR_FIELDS)
            if any(row["CalendarId"] != cid and (row.get("Name") or "").casefold() == after["Name"].casefold() for row in others):
                raise OperationError("Another visible calendar already uses that name.", code="name_collision")
        events, subscribers = self._children(cid)
        if "StartDate" in patch or "EndDate" in patch:
            for row in events:
                try:
                    first = datetime.fromisoformat(row["StartDateTime"].replace("Z", "+00:00")).date()
                    end = datetime.fromisoformat(row["EndDateTime"].replace("Z", "+00:00"))
                    # End is exclusive in this package. Subtract the smallest
                    # representable interval so midnight belongs to prior day.
                    last = (end - timedelta(microseconds=1)).date()
                    within = first >= date.fromisoformat(after["StartDate"]) and (not after["EndDate"] or last <= date.fromisoformat(after["EndDate"]))
                except (KeyError, TypeError, ValueError):
                    raise OperationError("An associated event has an unreadable date; period changes are blocked.", code="incomplete_read") from None
                if not within:
                    raise OperationError("The proposed calendar period excludes associated event %s. Keep the existing period or review the event first." % row.get("ShortCode"), code="event_outside_period")
        plan = {"schema": "K12-CALENDAR-EDIT-1", "plan_id": uuid.uuid4().hex, "operation": "calendar_edit",
                "environment": self.environment, "calendar_id": cid, "prepared_at": _now(),
                "before": before, "after": after, "changes": patch, "unchanged": not patch, "etag": etag,
                "association_fingerprint": hashlib.sha256(json.dumps([events, subscribers], sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                "affected_event_count": len(events), "rule_subscription_count": len(subscribers),
                "worker_assignments": "not determined; this operation does not change eligibility rules"}
        self._plans[plan["plan_id"]] = copy.deepcopy(plan)
        return copy.deepcopy(plan)

    def _stored_plan(self, preview, operation):
        if not isinstance(preview, dict) or preview.get("operation") != operation:
            raise OperationError("Prepare a fresh preview before applying a change.", code="preview_required")
        plan = self._plans.get(preview.get("plan_id"))
        if plan is None or plan != preview or plan.get("environment") != self.environment:
            raise OperationError("The preview has expired or changed. Prepare it again.", code="preview_required")
        return copy.deepcopy(plan)

    def apply_calendar_edit(self, preview):
        plan = self._stored_plan(preview, "calendar_edit")
        cid = plan["calendar_id"]
        current, etag = self._item(cid)
        if current != plan["before"] or etag != plan["etag"]:
            raise ConflictError()
        events, subscribers = self._children(cid)
        fingerprint = hashlib.sha256(json.dumps([events, subscribers], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if fingerprint != plan["association_fingerprint"]:
            raise ConflictError("Calendar events or rule subscriptions changed after preview. Prepare a fresh preview.")
        if not plan["unchanged"] and not _valid_etag(etag):
            raise OperationError("The fresh Oracle ETag is unavailable; nothing was written.", code="etag_required")
        receipt = {"schema": "K12-CALENDAR-MUTATION-1", "operation": "calendar_edit", "environment": self.environment,
                   "calendar_id": cid, "started_at": _now(), "before": plan["before"], "expected": plan["after"],
                   "changes": plan["changes"], "etag": etag, "status": "unchanged_verified" if plan["unchanged"] else "submission_pending",
                   "write_attempts": 0 if plan["unchanged"] else 1, "requests": []}
        # The calendar-edit prefix is retained for recovery discovery, but
        # the leaf stays compact so deeply nested accepted workflows can
        # durably journal before the conditional PATCH.
        path = self.state_dir / ("Calendar_Edit_" + uuid.uuid4().hex[:12] + ".json")
        # Durability before HTTP is mandatory: never write if journaling fails.
        _atomic_json(path, receipt)
        if plan["unchanged"]:
            receipt["verified"] = current
            receipt["completed_at"] = _now()
            _atomic_json(path, receipt)
            del self._plans[plan["plan_id"]]
            return {**receipt, "receipt_path": str(path)}
        resource = CALENDARS + "/" + cid
        self.progress("Applying the reviewed calendar change...")
        try:
            response = self.client.request("PATCH", resource, payload=plan["changes"], headers={"If-Match": etag})
            receipt["http_status"] = response.status
            receipt["status"] = "submitted_awaiting_readback"
            _atomic_json(path, receipt)
            verified, new_etag = self._item(cid)
            if any(verified.get(field) != plan["after"][field] for field in EDIT_FIELDS):
                raise OperationError("Oracle readback did not match the reviewed change. Reconcile this receipt before editing again.", code="readback_mismatch")
            receipt.update(status="updated_verified", verified=verified, new_etag=new_etag, completed_at=_now())
            _atomic_json(path, receipt)
            del self._plans[plan["plan_id"]]
            return {**receipt, "receipt_path": str(path)}
        except Exception as error:
            rejected = isinstance(error, OperationError) and error.http_status in (400, 401, 403, 404, 405, 409, 412, 422)
            receipt["status"] = "rejected" if rejected else "outcome_unknown"
            receipt["error_code"] = getattr(error, "code", "local_error")
            if isinstance(error, OperationError):
                receipt["error"] = str(error)
            receipt["completed_at"] = _now()
            try:
                _atomic_json(path, receipt)
            except OSError:
                pass  # The durable submission_pending receipt remains intact.
            self._plans.pop(plan["plan_id"], None)
            raise OperationError((str(error) if isinstance(error, OperationError) else "A local error interrupted the change.") +
                                 (" Nothing was automatically retried." if rejected else " The outcome is uncertain; use Reconcile before another edit."),
                                 code="write_rejected" if rejected else "outcome_unknown", http_status=getattr(error, "http_status", None), receipt_path=str(path)) from None

    def reconcile_calendar_edit(self, receipt_path):
        path = Path(receipt_path).resolve()
        if path.parent != self.state_dir.resolve() or not path.name.startswith("Calendar_Edit_") or path.suffix != ".json":
            raise OperationError("Select a calendar-edit receipt from this launcher's state folder.", code="invalid_receipt")
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise OperationError("The calendar-edit receipt cannot be read.", code="invalid_receipt") from None
        if receipt.get("schema") != "K12-CALENDAR-MUTATION-1" or receipt.get("environment") != self.environment or receipt.get("operation") != "calendar_edit":
            raise OperationError("The receipt does not belong to this calendar operation and environment.", code="invalid_receipt")
        current, etag = self._item(receipt.get("calendar_id"))
        if all(current.get(field) == receipt.get("expected", {}).get(field) for field in EDIT_FIELDS):
            status = "reconciled_applied"
        elif all(current.get(field) == receipt.get("before", {}).get(field) for field in EDIT_FIELDS):
            status = "reconciled_original_state"
        else:
            status = "reconciled_conflict"
        receipt.update(status=status, reconciled_at=_now(), observed=current, observed_etag=etag)
        _atomic_json(path, receipt)
        return {**receipt, "receipt_path": str(path)}

    def plan_delete_calendar(self, calendar_id):
        cid = _id(calendar_id)
        calendar, etag = self._item(cid)
        events, subscribers = self._children(cid)
        return {"operation": "calendar_delete", "calendar_id": cid, "calendar": calendar,
                "status": "blocked", "allowed": False, "events": len(events), "rule_subscriptions": len(subscribers),
                "assignment_count": None, "assignment_proof": "unavailable",
                "reason": "Deletion is unavailable: the supported calendar REST resources do not prove that no users are assigned. Empty rule subscriptions are not proof of zero users, and no atomic assignment check with deletion has been validated.",
                "required_capability": "An authoritative, complete assignment check plus a supported deletion operation that prevents an assignment race."}

    def apply_delete_calendar(self, preview):
        raise OperationError("Deletion is blocked because zero assigned users cannot be verified and enforced atomically. No DELETE request was sent.", code="assignment_proof_required")

    def plan_event_edit(self, event_id, changes):
        _id(event_id)
        raise OperationError("Shared-event editing requires a complete impact scan and a verified master-event update contract. Use the proven event maintenance workflow; this launcher does not submit an unverified shared-event edit.", code="event_edit_unavailable")
