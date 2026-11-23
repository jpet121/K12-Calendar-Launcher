"""Export Oracle organization-calendar configuration using GET requests only.

SAMPLE-A K12 discovery package. Python 3.9+, standard library only.
"""
import argparse
import base64
from datetime import datetime, timezone
import getpass
import json
from pathlib import Path
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
import warnings

DEFAULT_BASE = "https://tenant-a.example.invalid"
VERSION = "1.1.2-sample-a"
ALL_CALENDAR_LIMIT = 50
ROOT = "/hcmRestApi/resources/11.13.18.05/absenceCalendars"
CALENDAR_FIELDS = "CalendarId,Name,Description,StartDate,EndDate,LastUpdateDate"
EVENT_FIELDS = ("CalendarCalEventId,CalendarEventId,CalendarId,Name,ShortCode,"
                "Description,StartDateTime,EndDateTime,AllDayFlag,HalfDayForElapsed,"
                "Category,CategoryCode,EventCategoryCode,EventType,LastUpdateDate")
RULE_FIELDS = ("CalendarSubscriberId,CalendarId,ListId,Name,Description,ActiveFlag,"
               "EvaluateRuleMembershipFlag,RuleLastRunDate,RuleRunStatus,"
               "RuleRunStatusCode,LastUpdateDate")
RESOURCE_RE = re.compile(re.escape(ROOT) +
    r"(?:/[0-9]+/child/(?:AbsenceCalendarEvent|AbsenceCalendarSubscriber))?")


class ReadError(Exception):
    """Safe diagnostic text; never contains credentials or response bodies."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ReadError("Redirect blocked. Check the REST host and authentication method.")


def normalize_base(value):
    value = value.strip().rstrip("/")
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ReadError("Enter an HTTPS hostname, with no page path or credentials.") from None
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.path or parsed.query or parsed.fragment
            or port not in (None, 443) or not re.fullmatch(r"[A-Za-z0-9.-]+", parsed.hostname)):
        raise ReadError("Enter an HTTPS hostname, with no page path or credentials.")
    return "https://" + parsed.hostname.lower()


def read_secret(prompt):
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            value = getpass.getpass(prompt)
        except getpass.GetPassWarning:
            raise ReadError("Hidden input is unavailable. Run from a local terminal.") from None
    if not value or any(char in value for char in "\r\n"):
        raise ReadError("A nonempty credential without line breaks is required.")
    return value


def record_id(value):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ReadError("Oracle returned a missing or invalid record identifier.")
    result = str(value)
    if not re.fullmatch(r"[0-9]+", result):
        raise ReadError("Oracle returned a missing or invalid record identifier.")
    return result


class Client:
    def __init__(self, base, authorization, ca_file=None):
        self.base = normalize_base(base)
        self._authorization = authorization
        context = ssl.create_default_context(cafile=ca_file)
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=context), NoRedirect())
        self.reads = []
        self.auth_failed = False

    def get(self, resource, params):
        if not RESOURCE_RE.fullmatch(resource):
            raise ReadError("Request outside the calendar export scope was blocked.")
        request = urllib.request.Request(
            self.base + resource + "?" + urllib.parse.urlencode(params),
            headers={"Authorization": self._authorization, "Accept": "application/json",
                     "REST-Framework-Version": "4"}, method="GET")
        log = {"method": "GET", "resource": resource}
        self.reads.append(log)
        try:
            with self.opener.open(request, timeout=30) as response:
                log["http_status"] = response.status
                if response.status != 200:
                    raise ReadError("Unexpected successful HTTP status; export stopped.")
                if "json" not in response.headers.get("Content-Type", "").lower():
                    raise ReadError("Expected JSON but received another format, possibly a sign-in page.")
                raw = response.read(8 * 1024 * 1024 + 1)
                if len(raw) > 8 * 1024 * 1024:
                    raise ReadError("Response exceeded the 8 MiB page limit.")
                try:
                    data = json.loads(raw)
                except (ValueError, UnicodeError):
                    raise ReadError("Oracle returned invalid JSON.") from None
                if not isinstance(data, dict):
                    raise ReadError("Expected a JSON collection object.")
                return data
        except urllib.error.HTTPError as error:
            log["http_status"] = error.code
            self.auth_failed = error.code == 401
            messages = {400: "Oracle rejected the query. Return this report for schema review.",
                        401: "REST authentication was not accepted; check the approved REST credentials or OAuth method.",
                        403: "Access was denied by Oracle or an access gateway; calendar permissions may be missing.",
                        404: "The calendar resource was not found or was hidden; verify host, release, and access.",
                        429: "Request limit reached. No automatic retry was attempted."}
            raise ReadError("HTTP %s: %s" % (error.code, messages.get(
                error.code, "Oracle or a gateway returned an error."))) from None
        except (urllib.error.URLError, ssl.SSLError, OSError):
            raise ReadError("Connection failed. Check VPN/network access, host, and trusted TLS certificates.") from None

    def collection(self, resource, fields, key, row_limit=5000):
        rows, seen, offset, pages = [], set(), 0, 0
        result = {"resource": resource, "status": "failed", "items": rows, "pages": 0}
        try:
            if self.auth_failed:
                raise ReadError("Skipped after authentication failed earlier in this run.")
            while True:
                data = self.get(resource, {"fields": fields, "onlyData": "true",
                    "limit": min(100, row_limit - len(rows)), "offset": offset,
                    "orderBy": key + ":asc"})
                batch = data.get("items")
                if (not isinstance(batch, list) or any(not isinstance(item, dict) for item in batch)
                        or type(data.get("hasMore")) is not bool or type(data.get("count")) is not int
                        or data["count"] != len(batch) or type(data.get("offset")) is not int
                        or data["offset"] != offset):
                    raise ReadError("Unexpected collection or pagination metadata; completeness is unverified.")
                ids = [record_id(item.get(key)) for item in batch]
                if len(ids) != len(set(ids)) or seen.intersection(ids):
                    raise ReadError("Repeated record IDs detected during pagination; completeness is unverified.")
                if len(rows) + len(batch) > row_limit:
                    raise ReadError("Oracle exceeded the requested row limit; export is incomplete.")
                allowed = fields.split(",")
                rows.extend({name: item[name] for name in allowed if name in item} for item in batch)
                seen.update(ids)
                pages += 1
                result["pages"] = pages
                if not data["hasMore"]:
                    result["status"] = "complete"
                    return result
                if not batch or len(rows) >= row_limit or pages >= 100:
                    raise ReadError("More rows may exist. The bounded export stopped before completion.")
                offset += len(batch)
        except ReadError as error:
            result["status"] = "partial" if rows else "failed"
            result["error"] = str(error)
            return result


def choose_calendars(rows):
    rows = sorted(rows, key=lambda item: (str(item.get("Name") or "").casefold(), record_id(item["CalendarId"])))
    for index, row in enumerate(rows, 1):
        label = "".join(char if char.isprintable() else " " for char in str(row.get("Name") or "(unnamed)"))
        print("%d. %s  [%s to %s]" % (index, label, row.get("StartDate"), row.get("EndDate") or "open-ended"))
    while True:
        choice = input("\nChoose up to 5 numbers, comma-separated; Enter uses the first 5: ").strip()
        if not choice:
            return rows[:5], "First five by name for initial inspection; representativeness is unverified."
        try:
            indexes = [int(value.strip()) for value in choice.split(",")]
            if (not 1 <= len(indexes) <= 5 or len(set(indexes)) != len(indexes)
                    or any(index < 1 or index > len(rows) for index in indexes)):
                raise ValueError
            return [rows[index - 1] for index in indexes], "User selected calendar list entries."
        except ValueError:
            print("Enter one to five different numbers from the list.")


def export_selected(client, selected):
    details = []
    for calendar in selected:
        calendar_id = record_id(calendar["CalendarId"])
        print("Reading event and rule associations for calendar " + calendar_id + "...")
        prefix = ROOT + "/" + calendar_id + "/child/"
        entry = {"calendar": calendar,
                 "events": client.collection(prefix + "AbsenceCalendarEvent", EVENT_FIELDS, "CalendarCalEventId"),
                 "rules": client.collection(prefix + "AbsenceCalendarSubscriber", RULE_FIELDS, "CalendarSubscriberId")}
        for kind in ("events", "rules"):
            for row in entry[kind]["items"]:
                if str(row.get("CalendarId")) != calendar_id:
                    entry[kind]["status"] = "failed"
                    entry[kind]["error"] = "A child record did not match the requested calendar."
                    entry[kind]["items"] = []
                    break
        details.append(entry)
    return details


def k12_2026_2027_candidates(rows):
    """Identify candidates by visible name/description and date overlap; never select one automatically."""
    start, end = "2026-01-01", "2027-12-31"
    candidates = []
    for row in rows:
        text = (str(row.get("Name") or "") + " " + str(row.get("Description") or "")).casefold()
        row_start, row_end = str(row.get("StartDate") or ""), str(row.get("EndDate") or "9999-12-31")
        if "k12" in text and row_start <= end and row_end >= start:
            candidates.append({field: row.get(field) for field in CALENDAR_FIELDS.split(",")})
    return candidates


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--oauth", action="store_true", help="Prompt for an approved OAuth bearer token instead of a password")
    parser.add_argument("--ca-file", help="Optional approved CA certificate PEM file; TLS verification remains enabled")
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--all-calendars", action="store_true", help="Read all visible calendars, up to 50, without a selection prompt")
    args = parser.parse_args(argv)
    report = {"format": "k12-calendar-discovery-v1", "exporter_version": VERSION,
              "started_utc": datetime.now(timezone.utc).isoformat(), "status": "failed",
              "operation": "GET-only configuration discovery", "details": [],
              "limitations": [
                  "Calendar/event/rule identifiers are discovered only from this SAMPLE-A response.",
                  "Event data comes from calendar associations; unlinked master events are not exported.",
                  "Rules are existing associations and reported status, not criteria definitions or verified worker membership.",
                  "No worker, position, assignment, address, or eligibility membership collection is queried.",
                  "This is GET-only evidence, not a change preview or approval to write."]}
    client = None
    print("SAMPLE-A K12 Calendar Export - read-only configuration\n")
    try:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        base = normalize_base(input("SAMPLE-A HTTPS base URL [" + DEFAULT_BASE + "]: ") or DEFAULT_BASE)
        report["environment"] = base
        if base != DEFAULT_BASE:
            raise ReadError("This package is restricted to the displayed SAMPLE-A hostname. No credentials were requested.")
        print("Reading from: " + base)
        if args.oauth:
            authorization = "Bearer " + read_secret("Approved OAuth token (hidden): ")
        else:
            username = input("Oracle REST username: ").strip()
            if not username or any(char in username for char in ":\r\n"):
                raise ReadError("A REST username without a colon or line break is required.")
            password = read_secret("Oracle REST password (hidden): ")
            authorization = "Basic " + base64.b64encode((username + ":" + password).encode("utf-8")).decode("ascii")
            del password
        client = Client(base, authorization, args.ca_file)
        del authorization
        print("Reading calendar list...")
        inventory = client.collection(ROOT, CALENDAR_FIELDS, "CalendarId", row_limit=2000)
        report["calendar_inventory"] = inventory
        report["k12_2026_2027_candidates"] = k12_2026_2027_candidates(inventory["items"])
        print("Visible 2026-2027 K12 candidates: %d (not auto-selected)." % len(report["k12_2026_2027_candidates"]))
        if inventory.get("error"):
            print(inventory["error"])
        if inventory["items"]:
            if args.all_calendars:
                if inventory["status"] != "complete":
                    raise ReadError("Calendar inventory is incomplete. The all-calendar scan stopped before child reads.")
                if len(inventory["items"]) > ALL_CALENDAR_LIMIT:
                    raise ReadError("More than 50 calendars are visible. Return this report to set a bounded inspection scope.")
                selected = sorted(inventory["items"], key=lambda item: record_id(item["CalendarId"]))
                report["selection"] = "All calendars returned by the complete inventory, within account visibility."
                print("Reading all %d visible calendars..." % len(selected))
            else:
                selected, report["selection"] = choose_calendars(inventory["items"])
            report["details"] = export_selected(client, selected)
            statuses = [inventory["status"]] + [entry[kind]["status"] for entry in report["details"] for kind in ("events", "rules")]
            report["status"] = (("complete_for_visible_calendars" if args.all_calendars else "complete_for_selected_calendars")
                                if all(status == "complete" for status in statuses) else "partial")
        elif inventory["status"] == "complete":
            report["status"] = "no_visible_calendars"
    except ReadError as error:
        report["status"] = "failed"
        report["error"] = str(error)
        print(str(error))
    except (EOFError, KeyboardInterrupt):
        report["status"] = "failed"
        report["error"] = "Run cancelled before export completed."
    except Exception as error:
        report["status"] = "failed"
        report["error"] = "Unexpected local error: " + type(error).__name__
    if client:
        report["requests"] = client.reads
        client._authorization = ""
    report["finished_utc"] = datetime.now(timezone.utc).isoformat()
    output = args.output_dir / ("K12_SAMPLE_A_Calendar_Discovery_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f") + ".json")
    try:
        with output.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2, ensure_ascii=True)
    except OSError:
        print("Could not save the export. Run from a writable folder.")
        return 2
    print("\nResult: " + report["status"])
    print("File: " + str(output))
    print("Return this JSON report for a read-only review and change preview.")
    return 0 if report["status"] in ("complete_for_selected_calendars", "complete_for_visible_calendars") else 1


if __name__ == "__main__":
    sys.exit(main())
