"""Creation workflow tests with an in-memory Oracle service and real journals."""
import base64
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from types import SimpleNamespace
import zipfile
import xml.etree.ElementTree as ET

from . import create_adapter as adapter
from . import create_pilot as core


AUTH = core.make_basic_authorization("test-user", "not-a-live-password")


def make_plan(event_count=3):
    events = [{"ShortCode": "TEST_EVENT_%02d" % n, "Name": "Test event %d" % n,
               "Category": "PH", "HalfDayForElapsed": "N", "expected_readback_AllDayFlag": False,
               "StartDateTimeLocal": "2027-01-%02dT00:00:00" % (n + 1),
               "EndDateTimeLocal": "2027-01-%02dT00:00:00" % (n + 2)} for n in range(event_count)]
    calendars = [{"key": key, "Name": "Test Calendar " + key, "StartDate": "2027-01-01",
                  "EndDate": "2027-12-31", "Description": "Test request"} for key in ("A", "B")]
    links = [{"event_code": event["ShortCode"], "calendar_key": calendar["key"]}
             for calendar in calendars for event in events]
    return {"environment": core.DEFAULT_BASE, "events": events, "calendars": calendars, "associations": links}


class InMemoryOracle:
    """Exercise real HDL serialization, state transitions, and independent readback."""
    def __init__(self):
        self.events = {}; self.calendars = {}; self.links = {}; self.uploaded = []
        self.instances = []; self.generations = []; self.fail_link_once = False

    def client(self, authorization, config=None):
        client = FakeClient(self, authorization)
        client.environment = (config or {}).get("environment", core.DEFAULT_BASE)
        client.profile_id = (config or {}).get("profile_id", "")
        client.reference_event = (config or {}).get("integration", {}).get("reference_event")
        self.instances.append(client)
        return client

    def resolve(self, client, journal, report, folder, codes, require_all=True, generation="baseline", resume_only=False):
        self.generations.append(generation)
        mappings = {core.KNOWN_CODE: "810000331861667"}
        mappings.update({code: row["CalendarEventId"] for code, row in self.events.items()})
        if require_all and set(codes) - set(mappings):
            raise core.PilotError("Expected created event is absent from refreshed key map.")
        return {code: mappings[code] for code in codes if code in mappings}


class FakeClient:
    def __init__(self, service, authorization):
        self.service = service; self.authorization = authorization; self.requests = []

    def record(self, method, resource, content_type=None):
        if getattr(self, "creation_requests_disabled", False) and method != "GET":
            raise core.PilotError("Writes are disabled for the repeatability check.")
        entry = {"method": method, "resource": resource, "http_status": 200}
        if content_type: entry["request_content_type"] = content_type
        self.requests.append(entry)

    def calendars_by_name(self, name):
        self.record("GET", core.CALENDARS)
        return [deepcopy(row) for row in self.service.calendars.values() if row["Name"] == name]

    def get(self, resource, params):
        self.record("GET", resource)
        return deepcopy(self.service.calendars[resource.rsplit("/", 1)[-1]])

    def post(self, resource, payload, content_type="application/json"):
        self.record("POST", resource, content_type)
        if resource.endswith("/uploadFile"):
            with zipfile.ZipFile(io.BytesIO(base64.b64decode(payload["content"]))) as archive:
                self.service.uploaded = archive.read("CalendarEvent.dat").decode().splitlines()[1:]
            return {"result": {"ContentId": "UCM_TEST_1"}}
        if resource.endswith("/createFileDataSet"):
            for line in self.service.uploaded:
                _, _, code, name, category, start, end, half_day = line.split("|")
                self.service.events[code] = {"CalendarEventId": str(1000 + len(self.service.events)), "ShortCode": code,
                    "Name": name, "CategoryCode": category, "HalfDayForElapsed": half_day, "AllDayFlag": False,
                    "StartDateTime": start.replace("/", "-").replace(" ", "T") + "+00:00",
                    "EndDateTime": end.replace("/", "-").replace(" ", "T") + "+00:00"}
            return {"result": {"RequestId": "9000"}}
        raise AssertionError(resource)

    def data_set(self, request_id):
        self.record("GET", core.DATASETS + "/" + request_id)
        return {"DataSetStatusCode": "ORA_SUCCESS", "ImportStatusCode": "SUCCESS", "LoadStatusCode": "SUCCESS", "ObjectLoadErrorCount": 0}

    def business_objects(self, request_id):
        self.record("GET", core.DATASETS + "/" + request_id + "/child/businessObjects")
        return [{"BusinessObjectName": "Calendar Event", "LoadStatusCode": "SUCCESS", "ObjectLoadErrorCount": 0}]

    def create_calendar(self, payload):
        self.record("POST", core.CALENDARS)
        key = str(2000 + len(self.service.calendars))
        row = {"CalendarId": key, **payload}; self.service.calendars[key] = row
        return deepcopy(row)

    def create_link(self, calendar_id, event_id):
        self.record("POST", core.CALENDARS + "/" + calendar_id + "/child/AbsenceCalendarEvent")
        association_id = str(3000 + len(self.service.links))
        self.service.links[association_id] = {"CalendarId": calendar_id, "CalendarEventId": event_id, "CalendarCalEventId": association_id}
        if self.service.fail_link_once:
            self.service.fail_link_once = False
            raise core.PilotError("Simulated lost response after link creation.")

    def all_calendar_events(self, calendar_id):
        self.record("GET", core.CALENDARS + "/" + calendar_id + "/child/AbsenceCalendarEvent")
        events = {row["CalendarEventId"]: row for row in self.service.events.values()}
        return [{**deepcopy(events[link["CalendarEventId"]]), **deepcopy(link)} for link in self.service.links.values() if link["CalendarId"] == calendar_id]

    def events_for_calendar(self, calendar_id, code):
        return [row for row in self.all_calendar_events(calendar_id) if row["ShortCode"] == code]


class CreationAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name)
        self.service = InMemoryOracle()
        self.client_patch = patch.object(core, "Client", side_effect=self.service.client)
        self.resolve_patch = patch.object(core, "resolve_with_key_map", side_effect=self.service.resolve)
        self.client_mock = self.client_patch.start(); self.resolve_patch.start()

    def tearDown(self):
        self.resolve_patch.stop(); self.client_patch.stop(); self.temporary.cleanup()

    def run_build(self, plan=None):
        return adapter.run_create(plan or make_plan(), AUTH, self.folder, config={"environment": adapter.core.DEFAULT_BASE})

    def test_variable_count_build_then_unchanged_rerun_has_zero_posts(self):
        plan = make_plan(19)
        plan["associations"] = plan["associations"][:32]
        stages = []
        first = adapter.run_create(plan, AUTH, self.folder, stages.append, config={"environment": adapter.core.DEFAULT_BASE})
        self.assertEqual(first["status"], "applied_verified", first.get("error"))
        self.assertEqual(first["result_counts"], {"events": 19, "calendars": 2, "links": 32})
        self.assertEqual(first["repeatability"]["write_requests"], 0)
        second = self.run_build(plan)
        self.assertEqual(second["status"], "applied_verified", second.get("error"))
        self.assertEqual(second["write_requests"], 0)
        self.assertEqual(len(self.service.events), 19); self.assertEqual(len(self.service.calendars), 2)
        self.assertEqual(len(self.service.links), 32); self.assertTrue(stages)
        self.assertEqual(self.service.instances[-1].authorization, "")
        receipt = Path(second["receipt_path"]).read_text()
        self.assertNotIn(AUTH, receipt); self.assertNotIn("not-a-live-password", receipt)
        actions = [request for request in first["requests"] if "/action/" in request["resource"]]
        self.assertEqual(len(actions), 2)
        self.assertTrue(all(request["request_content_type"] == core.HDL_ACTION_CONTENT_TYPE for request in actions))

    def test_all_day_rejected_before_client_or_journal(self):
        plan = make_plan(); plan["events"][0]["expected_readback_AllDayFlag"] = True
        report = self.run_build(plan)
        self.assertEqual(report["status"], "failed"); self.assertIn("All day=Yes", report["error"])
        self.client_mock.assert_not_called(); self.assertFalse(core.journal_path(self.folder).exists())

    def test_loader_cannot_silently_drop_half_day_or_category(self):
        for field, value in (("HalfDayForElapsed", "Y"), ("Category", "OTHER")):
            plan = make_plan(); plan["events"][0][field] = value
            self.assertEqual(self.run_build(plan)["status"], "failed")
        self.client_mock.assert_not_called()

    def test_duplicate_links_and_orphan_events_fail_preview_without_network(self):
        plan = make_plan(); plan["associations"].append(deepcopy(plan["associations"][0]))
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            adapter.validate_plan(plan)
        plan = make_plan(); plan["associations"] = [x for x in plan["associations"] if x["event_code"] != "TEST_EVENT_00"]
        with self.assertRaisesRegex(ValueError, "Every new event"):
            adapter.validate_plan(plan)
        self.client_mock.assert_not_called()

    def test_delimiter_and_period_overflow_rejected_before_network(self):
        plan = make_plan(); plan["events"][0]["Name"] = "Injected|field"
        self.assertEqual(self.run_build(plan)["status"], "failed")
        plan = make_plan(); plan["events"][0]["EndDateTimeLocal"] = "2028-01-01T00:00:01"
        self.assertEqual(self.run_build(plan)["status"], "failed")
        self.client_mock.assert_not_called()

    def test_changed_workbook_stops_before_client_on_resume(self):
        self.assertEqual(self.run_build()["status"], "applied_verified")
        self.client_mock.reset_mock()
        plan = make_plan(); plan["events"][0]["Name"] = "Changed by another user"
        report = self.run_build(plan)
        self.assertEqual(report["status"], "failed"); self.assertIn("differs", report["error"])
        self.client_mock.assert_not_called()

    def test_lost_association_response_resumes_without_duplicate(self):
        self.service.fail_link_once = True
        first = self.run_build()
        self.assertEqual(first["status"], "failed"); self.assertEqual(len(self.service.links), 1)
        second = self.run_build()
        self.assertEqual(second["status"], "applied_verified", second.get("error"))
        self.assertEqual(len(self.service.links), 6)
        self.assertEqual(second["write_requests"], 5)
        self.assertFalse(any("/action/" in row["resource"] and row["method"] == "POST" for row in second["requests"]))

    def test_uncertain_calendar_post_is_not_repeated_or_adopted_by_name(self):
        original = FakeClient.create_calendar
        def lose_response(client, payload):
            original(client, payload)
            raise core.PilotError("Simulated lost calendar response")
        with patch.object(FakeClient, "create_calendar", lose_response):
            first = self.run_build()
        self.assertEqual(first["status"], "failed"); self.assertEqual(len(self.service.calendars), 1)
        second = self.run_build()
        self.assertEqual(second["status"], "failed"); self.assertIn("unresolved prior create", second["error"])
        self.assertEqual(second["write_requests"], 0); self.assertEqual(len(self.service.calendars), 1)

    def test_fresh_baseline_generation_prevents_reusing_old_absence_snapshot(self):
        with patch.object(core, "start_master_load", side_effect=core.PilotError("Paused before upload")):
            first = self.run_build()
        self.assertEqual(first["status"], "failed")
        self.service.events["TEST_EVENT_00"] = {"CalendarEventId": "8800"}
        second = self.run_build()
        self.assertEqual(second["status"], "failed"); self.assertIn("no matching creation journal", second["error"])
        self.assertEqual(second["write_requests"], 0)
        self.assertNotEqual(self.service.generations[0], self.service.generations[1])

    def test_creation_lock_blocks_concurrent_apply_before_network(self):
        with adapter.request_lock(self.folder):
            report = self.run_build()
        self.assertEqual(report["status"], "failed"); self.assertIn("already running", report["error"])
        self.client_mock.assert_not_called()

    def test_pending_master_keeps_submission_and_resume_does_not_upload_again(self):
        real_monitor = core.monitor_master_load
        with patch.object(core, "monitor_master_load", side_effect=core.OraclePending("Still running")):
            first = self.run_build()
        self.assertEqual(first["status"], "pending")
        second = self.run_build()
        self.assertEqual(second["status"], "applied_verified", second.get("error"))
        self.assertFalse(any("/action/" in row["resource"] and row["method"] == "POST" for row in second["requests"]))

    def test_temporarily_hidden_content_id_is_polled_without_resubmission(self):
        class DelayedVisibilityClient:
            poll_timeout_seconds = 30

            def __init__(self):
                self.data_set_calls = 0
                self.collection_calls = 0

            def data_set(self, request_id):
                self.data_set_calls += 1
                error = core.PilotError("temporary hidden data set")
                error.http_status = 404
                raise error

            def get(self, resource, params):
                self.collection_calls += 1
                self.last_resource, self.last_params = resource, params
                if resource != core.DATASETS:
                    raise AssertionError(resource)
                if self.collection_calls == 1:
                    return {"items": [], "hasMore": False}
                return {"items": [{"ContentId": "UCM_TEST_42", "RequestId": "9001",
                                   "DataSetStatusCode": "ORA_SUCCESS", "ImportStatusCode": "SUCCESS",
                                   "LoadStatusCode": "SUCCESS", "ObjectLoadErrorCount": 0}], "hasMore": False}

            def business_objects(self, request_id):
                self.business_object_request_id = request_id
                return [{"BusinessObjectName": "Calendar Event", "LoadStatusCode": "SUCCESS", "ObjectLoadErrorCount": 0}]

        client = DelayedVisibilityClient()
        journal = {"master_submission": {"state": "submitted", "ContentId": "UCM_TEST_42", "RequestId": "9000"}}
        with patch.object(core.time, "sleep") as sleep:
            core.monitor_master_load(client, journal, {}, self.folder)
        self.assertEqual(journal["master_submission"]["state"], "loaded")
        self.assertEqual(journal["master_submission"]["last_status_lookup"]["state"], "not_visible_by_content_id")
        self.assertEqual((client.data_set_calls, client.collection_calls), (2, 2))
        self.assertEqual(client.last_params["q"], "ContentId='UCM_TEST_42'")
        self.assertEqual(client.business_object_request_id, "9001")
        sleep.assert_called_once_with(5)

    def test_persistently_hidden_content_id_becomes_pending_without_resubmission(self):
        class HiddenContentClient:
            poll_timeout_seconds = 10

            def __init__(self):
                self.data_set_calls = 0
                self.collection_calls = 0
                self.post_calls = 0

            def data_set(self, request_id):
                self.data_set_calls += 1
                error = core.PilotError("temporary hidden data set")
                error.http_status = 404
                raise error

            def get(self, resource, params):
                self.collection_calls += 1
                self.last_resource, self.last_params = resource, params
                if resource != core.DATASETS:
                    raise AssertionError(resource)
                return {"items": [], "hasMore": False}

            def post(self, *args, **kwargs):
                self.post_calls += 1
                raise AssertionError("The saved submitted load must never be submitted again.")

        client = HiddenContentClient()
        journal = {"master_submission": {"state": "submitted", "ContentId": "UCM_TEST_43", "RequestId": "9002"}}
        clock = [0.0]

        def monotonic():
            return clock[0]

        def advance(seconds):
            clock[0] += seconds

        with patch.object(core.time, "monotonic", side_effect=monotonic), \
                patch.object(core.time, "sleep", side_effect=advance) as sleep:
            with self.assertRaisesRegex(core.OraclePending, "not visible by ContentId"):
                core.monitor_master_load(client, journal, {}, self.folder)
        self.assertEqual((client.data_set_calls, client.collection_calls), (3, 3))
        self.assertEqual([entry.args for entry in sleep.call_args_list], [(5,), (5,)])
        self.assertEqual(client.post_calls, 0)
        self.assertEqual(client.last_params["q"], "ContentId='UCM_TEST_43'")
        self.assertEqual(journal["master_submission"]["state"], "submitted")
        self.assertEqual(journal["master_submission"]["ContentId"], "UCM_TEST_43")
        self.assertEqual(journal["master_submission"]["RequestId"], "9002")
        self.assertEqual(journal["master_submission"]["last_status_lookup"]["state"], "not_visible_by_content_id")

    def test_saved_readback_drift_blocks_success_and_new_writes(self):
        self.assertEqual(self.run_build()["status"], "applied_verified")
        self.service.events["TEST_EVENT_00"]["AllDayFlag"] = True
        result = self.run_build()
        self.assertEqual(result["status"], "failed"); self.assertIn("AllDayFlag", result["error"])
        self.assertEqual(result["write_requests"], 0)

    def test_foreign_environment_and_malformed_auth_fail_without_network(self):
        plan = make_plan(); plan["environment"] = "https://other.example"
        self.assertEqual(self.run_build(plan)["status"], "failed")
        report = adapter.run_create(make_plan(), "Basic invalid!", self.folder)
        self.assertEqual(report["status"], "failed"); self.client_mock.assert_not_called()

    def test_oracle_null_optional_description_matches_intentionally_blank_input(self):
        plan = make_plan()
        for calendar in plan["calendars"]:
            calendar["Description"] = ""
        original = FakeClient.create_calendar
        def create_blank(client, payload):
            row = original(client, payload)
            client.service.calendars[row["CalendarId"]]["Description"] = None
            return row
        with patch.object(FakeClient, "create_calendar", create_blank):
            result = self.run_build(plan)
        self.assertEqual(result["status"], "applied_verified", result.get("error"))

    def test_case_only_existing_user_key_cannot_look_like_a_new_event(self):
        xml = ET.fromstring("""<Root><Integration_Object><Object_Name>CalendarEvent</Object_Name>
          <Integration_Key><Key_Details><Surrogate_Id>9001</Surrogate_Id></Key_Details>
          <User_Keys><User_Key_Attribute><Name>ShortCode</Name><Value>test_event_00</Value>
          </User_Key_Attribute></User_Keys></Integration_Key></Integration_Object></Root>""")
        with self.assertRaisesRegex(core.PilotError, "letter case"):
            core.resolve_calendar_event_ids(xml, ["TEST_EVENT_00"], require_all=False)

    def test_configured_timeouts_reach_creation_client_and_invalid_values_block(self):
        result = adapter.run_create(make_plan(), AUTH, self.folder, config={"environment": adapter.core.DEFAULT_BASE, "request_timeout_seconds": 17, "poll_timeout_seconds": 45})
        self.assertEqual(result["status"], "applied_verified", result.get("error"))
        client = self.service.instances[-1]
        self.assertEqual((client.request_timeout_seconds, client.soap_timeout_seconds, client.poll_timeout_seconds), (17, 17, 45))
        self.client_mock.reset_mock()
        invalid = adapter.run_create(make_plan(), AUTH, self.folder, config={"environment": adapter.core.DEFAULT_BASE, "request_timeout_seconds": 0})
        self.assertEqual(invalid["status"], "failed"); self.client_mock.assert_not_called()


class EngineBoundaryTests(unittest.TestCase):
    def test_rest_and_soap_requests_use_configured_network_timeouts(self):
        client = core.Client(AUTH)
        client.request_timeout_seconds = 17; client.soap_timeout_seconds = 19
        client.opener = MagicMock()
        response = io.BytesIO(b'{"items":[]}'); response.status = 200; response.headers = {}
        client.opener.open.return_value = response
        client.get(core.CALENDARS, {})
        self.assertEqual(client.opener.open.call_args.kwargs["timeout"], 17)
        client.soap_contract = lambda: {"endpoint": core.DEFAULT_BASE + core.SOAP_SERVICE_PATH, "soap_action": core.SOAP_ACTION}
        response = io.BytesIO(('''<s:Envelope xmlns:s="%s"><s:Body><fetchExtractOutputResponse>
          <result>&lt;Root/&gt;</result></fetchExtractOutputResponse></s:Body></s:Envelope>''' % core.SOAP_ENVELOPE_NS).encode())
        response.status = 200; response.headers = {}
        client.opener.open.return_value = response
        self.assertEqual(client.fetch_extract_output("saved-flow", "", "ARCHIVER").tag, "Root")
        self.assertEqual(client.opener.open.call_args.kwargs["timeout"], 19)

    def test_known_secret_echo_is_removed_from_journal_and_soap_diagnostic_files(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            token = core.STORAGE_AUTHORIZATION.set(AUTH)
            try:
                core.save_journal(folder, {"diagnostic": {"message": AUTH + " bare=" + AUTH[6:] + " password=not-a-live-password"}})
            finally:
                core.STORAGE_AUTHORIZATION.reset(token)
            envelope = ET.Element("{" + core.SOAP_ENVELOPE_NS + "}Envelope")
            body = ET.SubElement(envelope, "{" + core.SOAP_ENVELOPE_NS + "}Body")
            ET.SubElement(body, "faultstring").text = "Echo " + AUTH + " not-a-live-password " + AUTH[6:]
            log = {}
            core.capture_soap_response(SimpleNamespace(authorization=AUTH, diagnostic_dir=folder), ET.tostring(envelope), log)
            evidence = core.journal_path(folder).read_text() + Path(log["response_body_file"]).read_text() + json.dumps(log)
            for secret in (AUTH, AUTH[6:], "not-a-live-password"):
                self.assertNotIn(secret, evidence)

    def test_flow_wait_uses_client_poll_timeout_without_resubmission(self):
        client = SimpleNamespace(poll_timeout_seconds=0, flow_instance=lambda identity: {"FlowInstanceId": identity, "FlowName": "test-flow", "Status": "RUNNING"})
        with patch.object(core.time, "sleep") as sleep:
            with self.assertRaises(core.OraclePending):
                core.wait_for_flow(client, {"FlowInstanceId": "55", "FlowInstanceName": "test-flow"})
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
