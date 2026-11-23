"""SAMPLE-A HOLIDAY_A controlled CalendarEvent HDL test.

Preview and Verify issue GET requests only. Apply and Restore submit one HCM Data
Loader ZIP and independently verify the exact association afterward.
"""
import argparse
import base64
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

if __package__:
    from .export_calendars import DEFAULT_BASE, NoRedirect, ReadError, read_secret, record_id
else:
    from export_calendars import DEFAULT_BASE, NoRedirect, ReadError, read_secret, record_id

API = "/hcmRestApi/resources/11.13.18.05"
ROOT = API + "/absenceCalendars"
DATASETS = API + "/dataLoadDataSets"
SCRIPT_DIR = Path(__file__).resolve().parent
BASELINE_DIR = SCRIPT_DIR / "baseline"
TARGET = {"CalendarId": "810000331861655", "CalendarEventId": "810000331861667",
          "CalendarCalEventId": "810000331861676", "ShortCode": "HOLIDAY_A",
          "StartDateTime": "2027-01-18T00:00:00+00:00",
          "EndDateTime": "2027-01-19T00:00:00+00:00", "AllDayFlag": False,
          "HalfDayForElapsed": "N", "CategoryCode": "PH"}
PROPOSED = {"StartDateTime": "2027-01-19T00:00:00+00:00",
            "EndDateTime": "2027-01-20T00:00:00+00:00"}
PRESERVED = ("Name", "Description", "ShortCode", "AllDayFlag", "HalfDayForElapsed",
             "CategoryCode", "EventCategoryCode", "EventType")
FIELDS = ",".join((*TARGET, *PRESERVED))
ASSOCIATION = ROOT + "/" + TARGET["CalendarId"] + "/child/AbsenceCalendarEvent/" + TARGET["CalendarCalEventId"]
RESOURCE_RE = re.compile(re.escape(API) + r"(?:/absenceCalendars(?:/[0-9]+/child/AbsenceCalendarEvent(?:/[0-9]+)?)?|/dataLoadDataSets(?:/[0-9]+(?:/child/(?:businessObjects|messages))?)?(?:/action/(?:uploadFile|createFileDataSet))?)")
TEMPORARY_STATUS_CODES = (404, 429, 500, 502, 503, 504)
NATIVE_SCREEN_EVIDENCE = {
    "source": "User-confirmed Oracle native Import and Load Data screen",
    "content_ids": ["UCMFA07750085", "UCMFA07750087"],
    "result": "completed; import 100%; load 100%; zero failures",
    "recorded_for": "HOLIDAY_A 2027-01-19 through 2027-01-20 demonstration state",
}


class TemporaryStatusError(ReadError):
    """A read can be retried; it never authorizes another submission."""


class OracleClient:
    """TLS-only client constrained to this event's reads and HDL endpoints."""
    def __init__(self, authorization):
        self.authorization = authorization
        self.requests = []
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ssl.create_default_context()), NoRedirect())

    def _request(self, method, resource, params=None, payload=None, temporary_codes=()):
        if not RESOURCE_RE.fullmatch(resource):
            raise ReadError("Request outside the approved event-test scope was blocked.")
        url = DEFAULT_BASE + resource
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"Authorization": self.authorization, "Accept": "application/json",
                   "REST-Framework-Version": "4"}
        body = None
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/vnd.oracle.adf.action+json"
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        self.requests.append({"method": method, "resource": resource})
        try:
            with self.opener.open(request, timeout=30) as response:
                if response.status not in (200, 201, 202):
                    raise ReadError("Unexpected HTTP %s from Oracle." % response.status)
                raw = response.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise ReadError("Oracle response exceeded the 2 MiB limit.")
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise ReadError("Expected an Oracle JSON object.")
                return data
        except urllib.error.HTTPError as error:
            if method == "GET" and error.code in temporary_codes:
                raise TemporaryStatusError("HTTP %s on %s is temporarily unavailable." % (error.code, resource)) from None
            messages = {400: "Oracle rejected this request; inspect the saved status fields.",
                        401: "Authentication was not accepted.",
                        403: "Missing REST or HDL privilege for this endpoint.",
                        404: "The approved resource was not found or is hidden."}
            raise ReadError("HTTP %s on %s: %s" % (error.code, resource, messages.get(error.code, "Oracle returned an error."))) from None
        except (urllib.error.URLError, ssl.SSLError, OSError, ValueError):
            raise ReadError("TLS, network, or JSON failure while contacting Oracle.") from None

    def get(self, resource, params=None, temporary_codes=()):
        return self._request("GET", resource, params, temporary_codes=temporary_codes)

    def action(self, resource, payload):
        return self._request("POST", resource, payload=payload)

    def target(self):
        return self.get(ASSOCIATION, {"fields": FIELDS, "links": "self,canonical"})

    def visible_associations(self):
        calendars = self.get(ROOT, {"fields": "CalendarId,Name", "onlyData": "true", "limit": 100, "offset": 0})
        items = calendars.get("items")
        if not isinstance(items, list) or calendars.get("hasMore") is not False:
            raise ReadError("Visible calendar inventory was not complete in one bounded page.")
        matches = []
        for calendar in items:
            calendar_id = record_id(calendar.get("CalendarId"))
            data = self.get(ROOT + "/" + calendar_id + "/child/AbsenceCalendarEvent", {
                "q": "CalendarEventId=" + TARGET["CalendarEventId"], "fields": FIELDS,
                "onlyData": "true", "limit": 10, "offset": 0})
            rows = data.get("items")
            if not isinstance(rows, list) or data.get("hasMore") is not False:
                raise ReadError("Visible association scan was incomplete.")
            for row in rows:
                if record_id(row.get("CalendarEventId")) == TARGET["CalendarEventId"]:
                    matches.append({"CalendarId": calendar_id, "Name": calendar.get("Name"),
                                    "CalendarCalEventId": record_id(row.get("CalendarCalEventId"))})
        return {"visible_calendar_count": len(items), "matching_visible_associations": matches,
                "scope_limit": "Visible absence-calendar associations only; not proof of every enterprise use."}

    def upload_and_start(self, archive, label):
        content = base64.b64encode(archive.read_bytes()).decode("ascii")
        uploaded = self.action(DATASETS + "/action/uploadFile", {"content": content, "fileName": archive.name})
        content_id = uploaded.get("result", {}).get("ContentId")
        if not isinstance(content_id, str) or not content_id:
            raise ReadError("uploadFile did not return ContentId.")
        started = self.action(DATASETS + "/action/createFileDataSet", {
            "contentId": content_id, "fileAction": "IMPORT_AND_LOAD", "dataSetName": label})
        request_id = started.get("result", {}).get("RequestId")
        if isinstance(request_id, bool) or not str(request_id).isdigit():
            raise ReadError("createFileDataSet did not return RequestId.")
        return {"ContentId": content_id, "RequestId": str(request_id)}

    def data_set(self, request_id):
        fields = ("RequestId,DataSetId,DataSetName,DataSetStatusCode,DataSetStatusMeaning,"
                  "TransferStatusCode,ImportStatusCode,LoadStatusCode,ImportPercentageComplete,"
                  "LoadPercentageComplete,ObjectTotalCount,ObjectSuccessCount,ObjectLoadErrorCount")
        return self.get(DATASETS + "/" + request_id, {"onlyData": "true", "fields": fields}, TEMPORARY_STATUS_CODES)

    def data_sets_by_content_id(self, content_id):
        """Use Oracle's documented collection filter after an individual status 404."""
        fields = ("RequestId,ContentId,DataSetId,DataSetName,DataSetStatusCode,DataSetStatusMeaning,"
                  "TransferStatusCode,ImportStatusCode,LoadStatusCode,ImportPercentageComplete,"
                  "LoadPercentageComplete,ObjectTotalCount,ObjectSuccessCount,ObjectLoadErrorCount")
        data = self.get(DATASETS, {"q": "ContentId=" + content_id, "onlyData": "true", "fields": fields,
                                   "links": "self,canonical", "limit": 10, "offset": 0}, TEMPORARY_STATUS_CODES)
        if not isinstance(data.get("items"), list):
            raise ReadError("Oracle returned invalid data-load collection data.")
        return data

    def business_objects(self, request_id):
        fields = ("DataSetBusObjId,BusinessObjectName,TransferStatusCode,ImportStatusCode,LoadStatusCode,"
                  "FileLineTotalCount,FileLineImportErrorCount,ObjectTotalCount,ObjectSuccessCount,ObjectLoadErrorCount")
        data = self.get(DATASETS + "/" + request_id + "/child/businessObjects",
                        {"onlyData": "true", "fields": fields, "limit": 100, "offset": 0})
        if not isinstance(data.get("items"), list):
            raise ReadError("Oracle returned invalid business-object monitoring data.")
        return data["items"]

    def messages(self, request_id):
        fields = "DatFileName,BusinessObjectDiscriminator,OriginatingProcessCode,FileLine,MessageTypeCode,MessageText"
        data = self.get(DATASETS + "/" + request_id + "/child/messages",
                        {"onlyData": "true", "fields": fields, "orderBy": "DatFileName,FileLine", "limit": 100, "offset": 0})
        if not isinstance(data.get("items"), list):
            raise ReadError("Oracle returned invalid HDL message data.")
        return data["items"]


def safe_row(row):
    result = {field: row.get(field) for field in (*TARGET, *PRESERVED)}
    for field in ("CalendarId", "CalendarEventId", "CalendarCalEventId"):
        result[field] = record_id(result[field])
    result["links"] = [{"rel": link.get("rel"), "href": link.get("href")} for link in row.get("links", [])
                       if isinstance(link, dict) and link.get("rel") in ("self", "canonical")]
    return result


def compare(row, expected):
    return {key: {"expected": value, "actual": row.get(key)} for key, value in expected.items()
            if row.get(key) != value}


def find_baseline(value):
    if value:
        return Path(value)
    candidates = list(BASELINE_DIR.glob("K12_SAMPLE_A_Calendar_Discovery_*.json"))
    if not candidates:
        raise ReadError("The bundled SAMPLE-A discovery JSON is missing; specify --baseline.")
    return max(candidates, key=lambda item: item.stat().st_mtime)


def audit_baseline(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("environment") != DEFAULT_BASE or data.get("status") != "complete_for_visible_calendars":
        raise ReadError("Baseline is not a complete SAMPLE-A visible-calendar export.")
    matches, source = [], None
    for detail in data.get("details", []):
        calendar = detail.get("calendar", {})
        for event in detail.get("events", {}).get("items", []):
            if (str(event.get("CalendarEventId")) == TARGET["CalendarEventId"] and
                    str(event.get("CalendarCalEventId")) == TARGET["CalendarCalEventId"]):
                matches.append({"CalendarId": str(calendar.get("CalendarId")), "Name": calendar.get("Name"),
                                "CalendarCalEventId": str(event.get("CalendarCalEventId"))})
                source = event
    if len(matches) != 1 or matches[0]["CalendarId"] != TARGET["CalendarId"]:
        raise ReadError("Baseline does not prove exactly one matching visible association.")
    baseline = safe_row(source)
    if compare(baseline, TARGET):
        raise ReadError("Saved baseline conflicts with the approved target.")
    return {"visible_calendar_count": len(data.get("details", [])), "matching_visible_associations": matches,
            "baseline_event": baseline,
            "scope_limit": "Visible absence-calendar associations only; not proof of every enterprise use."}


def hdl_datetime(rest_value):
    if not isinstance(rest_value, str) or not rest_value.endswith("+00:00"):
        raise ReadError("Expected a UTC REST timestamp before producing HDL date-time text.")
    return rest_value[:19].replace("-", "/").replace("T", " ")


def make_archive(output_dir, action, interval):
    dat = "METADATA|CalendarEvent|CalendarEventId|StartDateTime|EndDateTime\nMERGE|CalendarEvent|%s|%s|%s\n" % (
        TARGET["CalendarEventId"], hdl_datetime(interval["StartDateTime"]), hdl_datetime(interval["EndDateTime"]))
    path = output_dir / ("HOLIDAY_A_" + action + "_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f") + "_CalendarEvent.zip")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("CalendarEvent.dat", dat)
    return path, dat


def terminal(data):
    return data.get("DataSetStatusCode") in ("ORA_COMPLETE", "ORA_IN_ERROR", "ORA_CANCELLED", "ORA_TERMINATED")


def successful(data, objects):
    if data.get("DataSetStatusCode") != "ORA_COMPLETE" or data.get("ImportStatusCode") != "SUCCESS" or data.get("LoadStatusCode") != "SUCCESS":
        return False
    if data.get("ObjectLoadErrorCount", 0) not in (0, None):
        return False
    event_objects = [item for item in objects if item.get("BusinessObjectName") == "CalendarEvent"]
    return len(event_objects) == 1 and event_objects[0].get("LoadStatusCode") == "SUCCESS" and event_objects[0].get("ObjectLoadErrorCount", 0) in (0, None)


def monitor(client, request_id, timeout):
    deadline = time.monotonic() + timeout
    while True:
        status = client.data_set(request_id)
        if terminal(status):
            objects = client.business_objects(request_id)
            return {"data_set": status, "business_objects": objects}
        if time.monotonic() >= deadline:
            raise ReadError("HDL monitor timed out; reconcile this RequestId before any retry or restoration.")
        time.sleep(5)


def pending_path(output_dir):
    return output_dir / ".HOLIDAY_A_pending_submission.json"


def read_pending(output_dir):
    path = pending_path(output_dir)
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ReadError("Pending-submission record cannot be read; do not submit another load.") from None
    if not isinstance(value, dict):
        raise ReadError("Pending-submission record is invalid; do not submit another load.")
    return value


def save_pending(output_dir, content_id, request_id, state, detail=None):
    existing = read_pending(output_dir) or {}
    value = dict(existing)
    value.setdefault("original_submission", {"ContentId": str(existing.get("ContentId") or content_id),
                                              "RequestId": str(existing.get("RequestId") or request_id)})
    value["ContentId"] = str(content_id)
    value["RequestId"] = str(request_id)
    value["state"] = state
    value["updated_utc"] = datetime.now(timezone.utc).isoformat()
    if detail:
        value["rest_reconciliation"] = detail
    with pending_path(output_dir).open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=True)
    return value


def native_evidence_allows_restore(pending):
    evidence = pending.get("native_screen_evidence") if isinstance(pending, dict) else None
    return (isinstance(evidence, dict) and evidence.get("result") == NATIVE_SCREEN_EVIDENCE["result"] and
            "UCMFA07750085" in evidence.get("content_ids", []))


def retry_submission_status(client, request_id, attempts, delay_seconds):
    attempts_log = []
    for attempt in range(1, attempts + 1):
        try:
            data_set = client.data_set(request_id)
            attempts_log.append({"attempt": attempt, "outcome": "available"})
            result = {"attempts": attempts_log, "data_set": data_set}
            try:
                result["business_objects"] = client.business_objects(request_id)
            except ReadError as error:
                result["business_object_read_error"] = str(error)
            try:
                result["messages"] = client.messages(request_id)
            except ReadError as error:
                result["message_read_error"] = str(error)
            return result
        except TemporaryStatusError as error:
            attempts_log.append({"attempt": attempt, "outcome": "temporarily_unavailable", "error": str(error)})
            if attempt < attempts:
                time.sleep(delay_seconds)
    return {"attempts": attempts_log, "status_lookup": "unavailable"}


def safe_data_set(row):
    fields = ("RequestId", "ContentId", "DataSetId", "DataSetName", "DataSetStatusCode", "DataSetStatusMeaning",
              "TransferStatusCode", "ImportStatusCode", "LoadStatusCode", "ImportPercentageComplete",
              "LoadPercentageComplete", "ObjectTotalCount", "ObjectSuccessCount", "ObjectLoadErrorCount")
    value = {field: row.get(field) for field in fields}
    value["links"] = [{"rel": link.get("rel"), "href": link.get("href")} for link in row.get("links", [])
                      if isinstance(link, dict) and link.get("rel") in ("self", "canonical")]
    return value


def fallback_status_by_content_id(client, content_id):
    collection = client.data_sets_by_content_id(content_id)
    matches = [item for item in collection["items"] if str(item.get("ContentId")) == content_id]
    result = {"query": "ContentId=" + content_id, "returned_count": len(matches)}
    if len(matches) != 1:
        result["status_lookup"] = "unavailable"
        return result
    data_set = safe_data_set(matches[0])
    returned_request_id = str(data_set.get("RequestId"))
    if not returned_request_id.isdigit():
        raise ReadError("The ContentId collection result did not contain a numeric RequestId.")
    result.update({"status_lookup": "available_by_content_id", "data_set": data_set,
                   "returned_request_id": returned_request_id})
    try:
        result["business_objects"] = client.business_objects(returned_request_id)
    except ReadError as error:
        result["business_object_read_error"] = str(error)
    try:
        result["messages"] = client.messages(returned_request_id)
    except ReadError as error:
        result["message_read_error"] = str(error)
    return result


def reconcile(client, content_id, request_id, attempts, delay_seconds, baseline):
    status_lookup = retry_submission_status(client, request_id, attempts, delay_seconds)
    result = {"submission": {"ContentId": content_id, "original_RequestId": request_id},
              "status_lookup": status_lookup}
    if status_lookup.get("status_lookup") == "unavailable":
        try:
            result["content_id_lookup"] = fallback_status_by_content_id(client, content_id)
        except TemporaryStatusError as error:
            result["content_id_lookup"] = {"status_lookup": "unavailable", "error": str(error)}
        except ReadError as error:
            result["content_id_lookup"] = {"status_lookup": "unavailable", "error": str(error)}
    try:
        current = safe_row(client.target())
        result["current"] = current
        result["current_vs_original"] = compare(current, {**TARGET, **{key: baseline[key] for key in PRESERVED}})
        result["current_vs_demonstration"] = compare(current, {**TARGET, **PROPOSED,
                                                               **{key: baseline[key] for key in PRESERVED}})
    except ReadError as error:
        result["target_read_error"] = str(error)
    try:
        result["visible_association_check"] = client.visible_associations()
    except ReadError as error:
        result["visible_association_read_error"] = str(error)
    lookup = result.get("content_id_lookup") if result.get("content_id_lookup", {}).get("status_lookup") == "available_by_content_id" else status_lookup
    if lookup.get("status_lookup") == "unavailable":
        result["state"] = "submitted; outcome unverified"
    elif (successful(lookup["data_set"], lookup.get("business_objects", [])) and
          not result.get("current_vs_demonstration")):
        result["state"] = "applied_verified"
    else:
        result["state"] = "submission requires manual review"
    return result


def write_report(output_dir, report):
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / ("HOLIDAY_A_Event_Test_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f") + ".json")
    with path.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, ensure_ascii=True)
    print("File: " + str(path))


def credentials(oauth):
    if oauth:
        return "Bearer " + read_secret("Approved OAuth token (hidden): ")
    username = input("Oracle REST username: ").strip()
    if not username or any(char in username for char in ":\r\n"):
        raise ReadError("A REST username without a colon or line break is required.")
    password = read_secret("Oracle REST password (hidden): ")
    authorization = "Basic " + base64.b64encode((username + ":" + password).encode("utf-8")).decode("ascii")
    del password
    return authorization


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", default="preview", choices=("preview", "apply", "verify", "restore", "reconcile"))
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--oauth", action="store_true")
    parser.add_argument("--apply", action="store_true", help="Required for Apply or Restore submission")
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument("--content-id", help="Existing HDL ContentId; required for read-only reconcile")
    parser.add_argument("--request-id", help="Existing HDL RequestId; required for read-only reconcile")
    parser.add_argument("--status-attempts", type=int, default=3, help="Bounded GET attempts for a temporarily hidden data set")
    parser.add_argument("--retry-delay-seconds", type=int, default=2)
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args(argv)
    report = {"operation": args.action, "environment": DEFAULT_BASE, "target": TARGET,
              "proposed_interval": PROPOSED, "restoration_interval": {key: TARGET[key] for key in PROPOSED},
              "write_interface": "HCM Data Loader CalendarEvent via uploadFile/createFileDataSet IMPORT_AND_LOAD", "status": "failed"}
    client = None
    try:
        if args.timeout_seconds < 5:
            raise ReadError("--timeout-seconds must be at least 5.")
        if not 1 <= args.status_attempts <= 5 or not 1 <= args.retry_delay_seconds <= 10:
            raise ReadError("--status-attempts must be 1-5 and --retry-delay-seconds must be 1-10.")
        baseline_path = find_baseline(args.baseline)
        report["baseline"] = str(baseline_path)
        report["affected_calendar_check"] = audit_baseline(baseline_path)
        if args.action in ("apply", "restore") and not args.apply:
            raise ReadError("%s requires --apply. No Oracle write was attempted." % args.action)
        pending = read_pending(args.output_dir)
        if args.action == "apply" and pending and pending.get("state") not in ("applied_verified", "restored_verified"):
            raise ReadError("Prior submission is unresolved (%s); reconcile it before another write." % pending.get("state", "unknown"))
        if args.action == "reconcile":
            if not args.content_id or not re.fullmatch(r"UCMFA[0-9A-Za-z_-]+", args.content_id) or not args.request_id or not args.request_id.isdigit():
                raise ReadError("reconcile requires a UCMFA ContentId and numeric RequestId.")
        authorization = credentials(args.oauth)
        client = OracleClient(authorization)
        del authorization
        if args.action == "reconcile":
            report["reconciliation"] = reconcile(client, args.content_id, args.request_id, args.status_attempts,
                                                    args.retry_delay_seconds, report["affected_calendar_check"]["baseline_event"])
            report["pending_submission"] = save_pending(args.output_dir, args.content_id, args.request_id,
                                                          report["reconciliation"]["state"], report["reconciliation"])
            report["status"] = report["reconciliation"]["state"].replace("; ", "_").replace(" ", "_")
        else:
            if args.action == "restore" and pending and pending.get("state") not in ("applied_verified", "restored_verified"):
                content_id, request_id = str(pending.get("ContentId", "")), str(pending.get("RequestId", ""))
                if not re.fullmatch(r"UCMFA[0-9A-Za-z_-]+", content_id) or not request_id.isdigit():
                    raise ReadError("The pending submission lacks a usable ContentId or RequestId; no restore was submitted.")
                report["prior_submission_reconciliation"] = reconcile(client, content_id, request_id, args.status_attempts,
                                                                        args.retry_delay_seconds, report["affected_calendar_check"]["baseline_event"])
                report["pending_submission"] = save_pending(args.output_dir, content_id, request_id,
                                                              report["prior_submission_reconciliation"]["state"],
                                                              report["prior_submission_reconciliation"])
                pending = read_pending(args.output_dir)
                if (pending.get("state") != "applied_verified" and
                        not native_evidence_allows_restore(pending)):
                    raise ReadError("Prior submission remains unresolved after read-only reconciliation; no restore was submitted.")
            interval = PROPOSED if args.action in ("apply", "verify") else {key: TARGET[key] for key in PROPOSED}
            current = safe_row(client.target())
            report["current_before"] = current
            preserved = {key: report["affected_calendar_check"]["baseline_event"][key] for key in PRESERVED}
            live_associations = client.visible_associations()
            report["visible_association_check"] = live_associations
            association_conflict = (len(live_associations["matching_visible_associations"]) != 1 or
                                    live_associations["matching_visible_associations"][0]["CalendarCalEventId"] != TARGET["CalendarCalEventId"])
            if args.action == "restore":
                original_conflicts = compare(current, {**TARGET, **preserved})
                demonstration_conflicts = compare(current, {**TARGET, **PROPOSED, **preserved})
                if association_conflict:
                    original_conflicts["visible_associations"] = {"expected": report["affected_calendar_check"]["matching_visible_associations"], "actual": live_associations["matching_visible_associations"]}
                    demonstration_conflicts["visible_associations"] = original_conflicts["visible_associations"]
                report["current_vs_original"] = original_conflicts
                report["current_vs_demonstration"] = demonstration_conflicts
                if not original_conflicts:
                    report["already_restored"] = True
                    report["status"] = "restored_verified"
                    if pending:
                        save_pending(args.output_dir, pending["ContentId"], pending["RequestId"], "restored_verified")
                elif demonstration_conflicts:
                    report["conflicts"] = demonstration_conflicts
                    report["status"] = "conflict"
                else:
                    report["conflicts"] = {}
            else:
                expected_before = {**TARGET, **(PROPOSED if args.action == "verify" else {key: TARGET[key] for key in PROPOSED}), **preserved}
                conflicts = compare(current, expected_before)
                if association_conflict:
                    conflicts["visible_associations"] = {"expected": report["affected_calendar_check"]["matching_visible_associations"], "actual": live_associations["matching_visible_associations"]}
                report["conflicts"] = conflicts
                if conflicts:
                    report["status"] = "conflict"
            if report["status"] == "failed" and args.action == "preview":
                archive, dat = make_archive(args.output_dir, "Preview", PROPOSED)
                report.update({"status": "preview_ready", "archive": str(archive), "CalendarEvent.dat": dat, "payload": None})
            elif report["status"] == "failed" and args.action == "verify":
                report["status"] = "verified"
            elif report["status"] == "failed":
                archive, dat = make_archive(args.output_dir, args.action.capitalize(), interval)
                report["archive"] = str(archive)
                report["CalendarEvent.dat"] = dat
                report["submission"] = client.upload_and_start(archive, "HOLIDAY_A " + args.action.capitalize())
                save_pending(args.output_dir, report["submission"]["ContentId"], report["submission"]["RequestId"], "submitted; outcome unverified")
                report["monitor"] = monitor(client, report["submission"]["RequestId"], args.timeout_seconds)
                if not successful(report["monitor"]["data_set"], report["monitor"]["business_objects"]):
                    raise ReadError("HDL completed without a clean CalendarEvent load; no retry was attempted.")
                readback = safe_row(client.target())
                report["readback"] = readback
                readback_conflicts = compare(readback, {**TARGET, **interval, **preserved})
                report["readback_conflicts"] = readback_conflicts
                report["status"] = "applied_verified" if not readback_conflicts and args.action == "apply" else ("restored_verified" if not readback_conflicts else "readback_conflict")
                save_pending(args.output_dir, report["submission"]["ContentId"], report["submission"]["RequestId"], report["status"])
    except (ReadError, OSError, ValueError, json.JSONDecodeError) as error:
        report["error"] = str(error)
    if client:
        report["requests"] = client.requests
        client.authorization = ""
    write_report(args.output_dir, report)
    if report.get("error"):
        print("Error: " + report["error"])
    reconciliation = report.get("reconciliation") or report.get("prior_submission_reconciliation")
    if isinstance(reconciliation, dict):
        lookup = reconciliation.get("content_id_lookup") or reconciliation.get("status_lookup", {})
        if lookup.get("error"):
            print("Status lookup: " + lookup["error"])
        for message in lookup.get("messages", []):
            if isinstance(message, dict):
                text = str(message.get("MessageText", ""))[:1000]
                print("Oracle HDL message: " + text)
    print("Result: " + report["status"])
    return 0 if report["status"] in ("preview_ready", "verified", "applied_verified", "restored_verified") else 1


if __name__ == "__main__":
    sys.exit(main())
