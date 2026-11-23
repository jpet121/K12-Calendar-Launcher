"""Independent release holdouts: hostile inputs and preservation of write boundaries.

These use synthetic protocol data and never contact Oracle. Successful execution
is evidence for local behavior only, not tenant permissions or Windows execution.
"""
from copy import deepcopy
from datetime import datetime
import base64
import csv
import importlib
import io
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
from zipfile import ZipFile, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import workbooks
from engine import create_adapter
import launcher
import operations
import event_updates

NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/package/2006/relationships"
DOCREL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def excel(day):
    return (datetime.fromisoformat(day) - datetime(1899, 12, 30)).days


def matrix_fixture():
    headers = ["Event code", "Event name", "First day", "Last day (inclusive)", "Days", "All day", "Half day", "Category"]
    events = {f"{chr(65+i)}8": value for i, value in enumerate(headers)}
    events.update({"I7": "Calendar_A", "J7": "Calendar_B", "A9": "HOLDOUT_NY", "B9": "A <named> event & holiday", "C9": excel("2027-01-01"), "D9": excel("2027-01-01"), "F9": "No", "G9": "No", "H9": "Public holiday", "I9": "X", "J9": "X"})
    headers = ["Calendar key", "Matrix label", "Start date", "End date", "Name in Oracle", "Description"]
    calendars = {f"{chr(65+i)}8": value for i, value in enumerate(headers)}
    calendars.update({"A3": "Request ID", "B3": "Holdout_001"})
    for row, label in ((9, "A"), (10, "B")):
        calendars.update({f"A{row}": f"Calendar_{label}", f"B{row}": f"Calendar {label}", f"C{row}": excel("2026-08-04"), f"D{row}": excel("2027-06-04"), f"E{row}": f"QA Calendar {label}", f"F{row}": ""})
    return {"Event matrix": events, "Calendars": calendars}


def workbook(path, sheets=None, extra=None):
    sheets = matrix_fixture() if sheets is None else sheets
    wb = ET.Element(f"{{{NS}}}workbook")
    ws = ET.SubElement(wb, f"{{{NS}}}sheets")
    rels = ET.Element(f"{{{REL}}}Relationships")
    members = {}
    for number, (name, cells) in enumerate(sheets.items(), 1):
        rid = f"rId{number}"
        ET.SubElement(ws, f"{{{NS}}}sheet", {"name": name, "sheetId": str(number), f"{{{DOCREL}}}id": rid})
        ET.SubElement(rels, f"{{{REL}}}Relationship", {"Id": rid, "Type": DOCREL + "/worksheet", "Target": f"worksheets/sheet{number}.xml"})
        sheet = ET.Element(f"{{{NS}}}worksheet")
        data = ET.SubElement(sheet, f"{{{NS}}}sheetData")
        for address, value in cells.items():
            row = ET.SubElement(data, f"{{{NS}}}row", {"r": ''.join(c for c in address if c.isdigit())})
            cell = ET.SubElement(row, f"{{{NS}}}c", {"r": address})
            if isinstance(value, dict):
                ET.SubElement(cell, f"{{{NS}}}f").text = value.get("formula", "1+1")
                ET.SubElement(cell, f"{{{NS}}}v").text = "1"
            elif isinstance(value, str):
                cell.set("t", "inlineStr")
                ET.SubElement(ET.SubElement(cell, f"{{{NS}}}is"), f"{{{NS}}}t").text = value
            else:
                ET.SubElement(cell, f"{{{NS}}}v").text = str(value)
        members[f"xl/worksheets/sheet{number}.xml"] = ET.tostring(sheet)
    members["xl/workbook.xml"] = ET.tostring(wb)
    members["xl/_rels/workbook.xml.rels"] = ET.tostring(rels)
    members.update(extra or {})
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        for name, contents in members.items():
            archive.writestr(name, contents)
    return path


class WorkbookHoldouts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "request.xlsx"

    def test_inclusive_one_day_becomes_exactly_next_midnight(self):
        plan = workbooks.load_plan(workbook(self.path))
        self.assertEqual(plan["events"][0]["StartDateTimeLocal"], "2027-01-01T00:00:00")
        self.assertEqual(plan["events"][0]["EndDateTimeLocal"], "2027-01-02T00:00:00")
        self.assertEqual(len(plan["associations"]), 2)
        self.assertEqual(plan["events"][0]["Name"], "A <named> event & holiday")

    def test_formula_in_literal_date_is_not_cached_value(self):
        sheets = matrix_fixture()
        sheets["Event matrix"]["C9"] = {"formula": "DATE(2027,1,1)"}
        with self.assertRaises(workbooks.WorkbookError):
            workbooks.load_plan(workbook(self.path, sheets))

    def test_hidden_extra_selection_column_is_rejected(self):
        sheets = matrix_fixture()
        sheets["Event matrix"]["K9"] = "X"
        with self.assertRaises(workbooks.WorkbookError):
            workbooks.load_plan(workbook(self.path, sheets))

    def test_spreadsheet_lowercase_duplicate_identity_is_rejected(self):
        sheets = matrix_fixture()
        event = sheets["Event matrix"]
        for address, value in list(event.items()):
            if address.endswith("9"):
                event[address[:-1] + "10"] = value
        event["A10"] = "holdout_ny"
        with self.assertRaises(workbooks.WorkbookError):
            workbooks.load_plan(workbook(self.path, sheets))

    def test_hdl_delimiter_in_event_name_is_rejected(self):
        sheets = matrix_fixture()
        sheets["Event matrix"]["B9"] = "Holiday|MERGE|CalendarEvent"
        with self.assertRaises(workbooks.WorkbookError):
            workbooks.load_plan(workbook(self.path, sheets))

    def test_external_relationship_is_rejected_even_if_unused(self):
        external = f'<Relationships xmlns="{REL}"><Relationship Id="evil" TargetMode="External" Target="https://evil.invalid/"/></Relationships>'
        with self.assertRaises(workbooks.WorkbookError):
            workbooks.load_plan(workbook(self.path, extra={"xl/worksheets/_rels/sheet1.xml.rels": external}))

    def test_utf16_xml_entity_is_rejected_before_resolution(self):
        bad = '<!DOCTYPE workbook [<!ENTITY secret SYSTEM "file:///etc/passwd">]><workbook>&secret;</workbook>'.encode("utf-16")
        with self.assertRaises(workbooks.WorkbookError):
            workbooks.read_cells(workbook(self.path, extra={"xl/workbook.xml": bad}))

    def test_zip_path_traversal_member_is_rejected_without_extraction(self):
        with self.assertRaises(workbooks.WorkbookError):
            workbooks.read_cells(workbook(self.path, extra={"../escaped.txt": "not extracted"}))
        self.assertFalse((self.path.parent.parent / "escaped.txt").exists())

    def test_all_day_true_is_preserved_then_blocked_before_oracle(self):
        sheets = matrix_fixture()
        sheets["Event matrix"]["F9"] = "Yes"
        plan = workbooks.load_plan(workbook(self.path, sheets))
        self.assertIs(plan["events"][0]["expected_readback_AllDayFlag"], True)
        with patch.object(create_adapter.core, "Client") as client:
            receipt = create_adapter.run_create(plan, "Basic " + base64.b64encode(b"fixture:secret").decode(), self.path.parent / "run", config={"environment": launcher.SAMPLE_A})
        client.assert_not_called()
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["write_requests"], 0)
        self.assertIn("All day=Yes", receipt["error"])

    def test_export_preserves_exact_json_and_neutralizes_csv_formulas(self):
        snapshot = {"complete": True, "calendars": [{"CalendarId": "810000000000001", "Name": "=HYPERLINK(\"https://evil.invalid\")", "Description": "  @SUM(1,1)", "events": [{"CalendarEventId": "810000000000002", "CalendarCalEventId": "810000000000003", "ShortCode": "QA", "Name": "+cmd", "StartDateTime": "2027-01-01T00:00:00-06:00", "EndDateTime": "2027-01-02T00:00:00-06:00", "AllDayFlag": False}]}]}
        path = workbooks.export_calendar_data(snapshot, self.path.with_suffix(".zip"))
        with ZipFile(path) as archive:
            self.assertEqual(json.loads(archive.read("snapshot.json")), snapshot)
            calendars = list(csv.DictReader(io.StringIO(archive.read("calendars.csv").decode("utf-8-sig"))))
            events = list(csv.DictReader(io.StringIO(archive.read("calendar_events.csv").decode("utf-8-sig"))))
        self.assertTrue(calendars[0]["Name"].startswith("'="))
        self.assertTrue(calendars[0]["Description"].startswith("'"))
        self.assertEqual(events[0]["Name"], "'+cmd")
        self.assertEqual(events[0]["StartDateTime"], "2027-01-01T00:00:00-06:00")
        self.assertEqual(events[0]["AllDayFlag"], "false")


class JournalHoldouts(unittest.TestCase):
    def plan(self):
        with tempfile.TemporaryDirectory() as folder:
            return workbooks.load_plan(workbook(Path(folder) / "valid.xlsx"))

    def test_changed_request_cannot_reuse_original_oracle_identity(self):
        plan = self.plan()
        journal = {"events": {}, "calendars": {}, "links": {}, "plan_signature": create_adapter.core.plan_signature(plan)}
        changed = deepcopy(plan)
        changed["events"][0]["Name"] += " changed"
        with self.assertRaises(ValueError):
            create_adapter._validate_journal(journal, changed)

    def test_owned_identity_without_signature_is_not_adopted(self):
        journal = {"events": {"HOLDOUT_NY": {"CalendarEventId": "810000000000001"}}, "calendars": {}, "links": {}}
        with self.assertRaises(ValueError):
            create_adapter._validate_journal(journal, self.plan())

    def test_failure_receipt_does_not_persist_server_echo_of_credentials(self):
        auth = "Basic " + base64.b64encode(b"qa_user:qa_secret_unique").decode()
        class Client:
            def __init__(self, authorization):
                self.authorization = authorization
                self.requests = [{"method": "GET", "oracle_error": "Denied " + authorization + " password=qa_secret_unique"}]
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(create_adapter.core, "Client", Client), patch.object(create_adapter.core, "apply_pilot", side_effect=create_adapter.core.PilotError("Denied " + auth + " password=qa_secret_unique")):
                result = create_adapter.run_create(self.plan(), auth, Path(folder), config={"environment": launcher.SAMPLE_A})
            rendered = json.dumps(result) + Path(result["receipt_path"]).read_text()
        self.assertEqual(result["status"], "failed")
        self.assertNotIn(auth, rendered)
        self.assertNotIn("qa_secret_unique", rendered)
        self.assertNotIn(auth.removeprefix("Basic "), rendered)

    def test_request_lock_prevents_two_active_runs(self):
        with tempfile.TemporaryDirectory() as folder:
            with create_adapter.request_lock(Path(folder)):
                with self.assertRaises(create_adapter.core.PilotError):
                    with create_adapter.request_lock(Path(folder)):
                        self.fail("second lock acquired")
            with create_adapter.request_lock(Path(folder)):
                pass


class MaintenanceFixture:
    def __init__(self):
        self.row = {"CalendarId": "100", "Name": "QA Calendar", "Description": "original", "StartDate": "2026-08-04", "EndDate": "2027-06-04", "LastUpdateDate": "2026-09-30T00:00:00Z"}
        self.etag = '"revision-1"'
        self.calls = []
        self.lose_patch_response = False
        self.events = []

    def request(self, method, resource, *, params=None, payload=None, headers=None):
        self.calls.append((method, resource, deepcopy(payload), deepcopy(headers)))
        if method == "PATCH":
            assert headers == {"If-Match": self.etag}
            self.row.update(payload)
            self.etag = '"revision-2"'
            if self.lose_patch_response:
                raise operations.OperationError("Connection ended after write", code="transport_error")
            return operations.Response(200, deepcopy(self.row), {"ETag": self.etag})
        if resource.endswith("/child/AbsenceCalendarEvent"):
            return operations.Response(200, {"items": deepcopy(self.events), "hasMore": False}, {})
        if resource.endswith("/child/AbsenceCalendarSubscriber"):
            return operations.Response(200, {"items": [], "hasMore": False}, {})
        if resource == operations.CALENDARS:
            return operations.Response(200, {"items": [deepcopy(self.row)], "hasMore": False}, {})
        return operations.Response(200, deepcopy(self.row), {"ETag": self.etag})


class MaintenanceHoldouts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.client = MaintenanceFixture()
        self.service = operations.CalendarService("Basic fixture", {"environment": operations.DEFAULT_BASE}, Path(self.temp.name), client=self.client)

    def writes(self):
        return [call for call in self.client.calls if call[0] != "GET"]

    def test_changed_etag_blocks_patch_even_with_identical_fields(self):
        plan = self.service.plan_calendar_edit("100", {"Description": "reviewed"})
        self.client.etag = '"unseen-revision"'
        with self.assertRaises(operations.ConflictError):
            self.service.apply_calendar_edit(plan)
        self.assertEqual(self.writes(), [])

    def test_tampered_calendar_preview_never_reaches_patch(self):
        plan = self.service.plan_calendar_edit("100", {"Description": "reviewed"})
        plan["changes"]["Description"] = "browser injection"
        with self.assertRaises(operations.OperationError):
            self.service.apply_calendar_edit(plan)
        self.assertEqual(self.writes(), [])

    def test_lost_patch_response_is_reconciled_using_only_get(self):
        plan = self.service.plan_calendar_edit("100", {"Description": "reviewed"})
        self.client.lose_patch_response = True
        with self.assertRaises(operations.OperationError) as failure:
            self.service.apply_calendar_edit(plan)
        self.assertEqual(len(self.writes()), 1)
        with self.assertRaises(operations.OperationError):
            self.service.plan_calendar_edit("100", {"Description": "another"})
        result = self.service.reconcile_calendar_edit(failure.exception.receipt_path)
        self.assertEqual(result["status"], "reconciled_applied")
        self.assertEqual(len(self.writes()), 1)

    def test_empty_rule_subscribers_never_prove_no_workers(self):
        plan = self.service.plan_delete_calendar("100")
        self.assertFalse(plan["allowed"])
        self.assertIsNone(plan["assignment_count"])
        for override in (plan, {**plan, "allowed": True, "assignment_count": 0, "assignment_proof": "user says so"}):
            with self.assertRaises(operations.OperationError):
                self.service.apply_delete_calendar(override)
        self.assertEqual(self.writes(), [])

    def test_paginated_duplicate_ids_do_not_create_complete_snapshot(self):
        class RepeatClient:
            def request(inner, *args, **kwargs):
                return operations.Response(200, {"items": [deepcopy(self.client.row)], "hasMore": True}, {})
        self.service.client = RepeatClient()
        with self.assertRaises(operations.OperationError) as failure:
            self.service.read()
        self.assertEqual(failure.exception.code, "incomplete_read")
        self.assertFalse((Path(self.temp.name) / "last_calendar_read.json").exists())


class EventReceiptHoldouts(unittest.TestCase):
    def test_status_diagnostic_cannot_write_encoded_or_plain_credentials(self):
        auth = "Basic " + base64.b64encode(b"qa_user:event_secret_unique").decode()
        class Client:
            def get(self, *args):
                return {"items": [{"ContentId": "UCM_TEST", "RequestId": "301", "DataSetStatusCode": "ORA_RUNNING", "ErrorMessage": "echo " + auth + " password=event_secret_unique"}], "hasMore": False}
        with tempfile.TemporaryDirectory() as folder:
            service = event_updates.EventUpdateService(auth, {"environment": launcher.SAMPLE_A}, Path(folder), client=Client())
            receipt = {"receipt_id": "a" * 32, "environment": launcher.SAMPLE_A, "CalendarEventId": "201", "ContentId": "UCM_TEST"}
            service._status(receipt)
            rendered = json.dumps(receipt) + service._receipt_path(receipt["receipt_id"]).read_text()
        self.assertNotIn(auth, rendered)
        self.assertNotIn(auth.removeprefix("Basic "), rendered)
        self.assertNotIn("event_secret_unique", rendered)


class LauncherHoldouts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {"environment": launcher.SAMPLE_A, "request_timeout_seconds": 30, "poll_timeout_seconds": 300}
        self.app = launcher.Application(self.root, self.config)
        self.app.select_environment("sample-a")

    def test_browser_provided_plan_changes_cannot_modify_reviewed_create(self):
        path = workbook(self.root / "fixture.xlsx")
        preview = self.app.execute("preview_create", {"content_base64": base64.b64encode(path.read_bytes()).decode(), "filename": "../../evil.xlsx"})
        seen = []
        def runner(plan, *args):
            seen.append(plan)
            return {"status": "applied_verified"}
        self.app.create_runner = runner
        self.app.authorization = "Basic fixture"
        self.app.execute("apply_create", {"preview_id": preview["preview_id"], "plan": {"events": []}, "changes": {"environment": "https://evil.invalid"}})
        self.assertEqual(seen[0]["events"][0]["ShortCode"], "HOLDOUT_NY")
        self.assertEqual(seen[0]["environment"], launcher.SAMPLE_A)
        with self.assertRaises(ValueError):
            self.app.execute("apply_create", {"preview_id": "invented-by-browser"})
        self.assertEqual(len(seen), 1)
        self.assertEqual(len(list((self.app.state_dir / "inputs").glob("*.xlsx"))), 1)
        self.assertFalse((self.root.parent / "evil.xlsx").exists())

    def test_failed_sign_in_never_exposes_encoded_credentials(self):
        password = "unique-test-password"
        auth = "Basic " + base64.b64encode(("qa_user:" + password).encode()).decode()
        def failed_service(authorization, *args):
            raise RuntimeError("Service rejected " + authorization)
        self.app.service_factory = failed_service
        self.app._run("connect", {"username": "qa_user", "password": password})
        state = json.dumps(self.app.public_state())
        self.assertNotIn(password, state)
        self.assertNotIn(auth, state)
        self.assertNotIn(auth.removeprefix("Basic "), state)
        self.assertFalse(self.app.public_state()["signed_in"])

    def test_failed_sign_in_masks_bare_base64_credential_variant(self):
        encoded = base64.b64encode(b"qa_user:bare_echo_secret").decode()
        def failed_service(authorization, *args):
            raise RuntimeError("Credential echo " + authorization.removeprefix("Basic "))
        self.app.service_factory = failed_service
        self.app._run("connect", {"username": "qa_user", "password": "bare_echo_secret"})
        self.assertNotIn(encoded, json.dumps(self.app.public_state()))

    def test_progress_and_returned_result_mask_nested_secret_values(self):
        secret = "nested_echo_secret"
        self.app.authorization = "Basic " + base64.b64encode(("qa_user:" + secret).encode()).decode()
        encoded = self.app.authorization.removeprefix("Basic ")
        class Fake:
            def read(inner):
                self.app.progress("Echo " + secret + " " + encoded)
                return {"ErrorMessage": {"nested": [secret, encoded, self.app.authorization]}}
        self.app.service = Fake()
        self.app._run("read", {})
        rendered = json.dumps(self.app.public_state())
        self.assertNotIn(secret, rendered)
        self.assertNotIn(encoded, rendered)

    def test_failed_reconnect_clears_prior_account_state(self):
        self.app.event_service = object()
        self.app.snapshot = {"old-account": "data"}
        self.app.plans["old-plan"] = {"kind": "event_edit"}
        def failed_service(*args):
            raise RuntimeError("Authentication failed")
        self.app.service_factory = failed_service
        self.app._run("connect", {"username": "new_user", "password": "test-only"})
        self.assertIsNone(self.app.event_service)
        self.assertIsNone(self.app.snapshot)
        self.assertEqual(self.app.plans, {})

    def test_http_requires_token_host_origin_and_bounded_json(self):
        server = launcher.make_server(self.app)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_port
        host = "127.0.0.1:" + str(port)
        good = {"Host": host, "Origin": "http://" + host, "X-Launcher-Token": self.app.token, "Content-Type": "application/json"}
        def request(method, path, body=None, headers=None):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                connection.request(method, path, body=body, headers=headers or {})
                response = connection.getresponse()
                return response.status, response.read(), dict(response.getheaders())
            finally:
                connection.close()
        self.assertEqual(server.server_address[0], "127.0.0.1")
        self.assertEqual(request("GET", "/api/state")[0], 403)
        self.assertEqual(request("GET", "/api/state", headers={**good, "Host": "evil.invalid"})[0], 403)
        self.assertEqual(request("POST", "/api/action", '{"action":"read"}', {**good, "Origin": "https://evil.invalid"})[0], 403)
        self.assertEqual(request("POST", "/api/action", '{"action":"read"}', {k:v for k,v in good.items() if k != "Origin"})[0], 403)
        self.assertEqual(request("POST", "/api/action", "[]", good)[0], 400)
        self.assertEqual(request("POST", "/api/action", "", {**good, "Content-Length": str(launcher.MAX_BODY + 1)})[0], 413)
        status, body, headers = request("GET", "/api/state", headers=good)
        self.assertEqual(status, 200)
        self.assertNotIn(self.app.token.encode(), body)
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(request("GET", "/api/download/../../config.json", headers=good)[0], 404)


if __name__ == "__main__":
    unittest.main()
