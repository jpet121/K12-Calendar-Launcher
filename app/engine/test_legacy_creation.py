"""Controller tests use no network and never prompt for secrets."""
import contextlib
import gzip
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from email.message import Message
import io
import urllib.error

from . import create_pilot as app


class CreatePilotTests(unittest.TestCase):
    def test_plan_revalidates_workbook_without_credentials_or_network(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(app, "credentials") as credentials, \
                patch.object(app, "Client") as client, contextlib.redirect_stdout(io.StringIO()):
            code = app.main(["plan", "--output-dir", folder])
            report = json.loads(next(Path(folder).glob("K12_SAMPLE_A_Create_Pilot_*.json")).read_text())
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "plan_ready")
        self.assertEqual(report["plan"]["counts"], {"new_events": 3, "new_calendars": 2, "selected_links": 4})
        credentials.assert_not_called(); client.assert_not_called()

    def test_apply_without_switch_stops_before_credentials_or_network(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(app, "credentials") as credentials, \
                patch.object(app, "Client") as client, contextlib.redirect_stdout(io.StringIO()):
            code = app.main(["apply", "--output-dir", folder])
        self.assertEqual(code, 1)
        credentials.assert_not_called(); client.assert_not_called()

    def test_stored_windows_credential_is_used_without_prompting(self):
        with patch.object(app, "read_windows_oracle_credential", return_value=("approved.user", "secret")), \
                patch("builtins.input") as prompt, patch.object(app, "read_secret") as secret:
            value = app.credentials()
        self.assertTrue(value.startswith("Basic "))
        prompt.assert_not_called(); secret.assert_not_called()

    def test_sign_in_stores_then_proves_authentication_with_read_only_request(self):
        class SignInClient:
            def __init__(self, authorization): self.authorization, self.requests = authorization, []
            def source_master_lookup_link(self): self.requests.append({"method": "GET", "http_status": 200}); return None
        with tempfile.TemporaryDirectory() as folder, patch.object(app, "Client", SignInClient), \
                patch.object(app, "establish_windows_credential", return_value="Basic TEST_ONLY"), contextlib.redirect_stdout(io.StringIO()):
            code = app.main(["sign-in", "--output-dir", folder])
            report = json.loads(next(Path(folder).glob("K12_SAMPLE_A_Create_Pilot_*.json")).read_text())
        self.assertEqual(code, 0); self.assertEqual(report["status"], "signed_in_verified")
        self.assertEqual(report["authentication"]["proof"], "GET existing HOLIDAY_A association returned HTTP 200")

    def test_event_hdl_omits_update_only_calendar_event_id(self):
        plan = app.create_plan(app.ROOT / "K12_SAMPLE_A_Create_Pilot.xlsx")
        with tempfile.TemporaryDirectory() as folder:
            archive, text = app.make_event_archive(Path(folder), plan["events"])
        self.assertTrue(archive.name.endswith(".zip"))
        self.assertNotIn("CalendarEventId", text)
        self.assertIn("DEMO_PILOT_MLK27", text)
        self.assertIn("2027/01/18 00:00:00", text)

    def test_hdl_actions_use_oracle_action_content_type_only(self):
        class CaptureClient(app.Client):
            def __init__(self): self.calls = []
            def post(self, resource, payload, content_type="application/json"):
                self.calls.append((resource, content_type)); return {"result": {"ContentId": "UCMFA1", "RequestId": "1"}}
        plan = app.create_plan(app.ROOT / "K12_SAMPLE_A_Create_Pilot.xlsx")
        with tempfile.TemporaryDirectory() as folder:
            archive, _ = app.make_event_archive(Path(folder), plan["events"])
            CaptureClient().upload_and_start(archive, "test")
        self.assertEqual([content_type for _, content_type in CaptureClient().calls], [])
        # A separate instance keeps the recorded calls available for the assertion.
        client = CaptureClient()
        with tempfile.TemporaryDirectory() as folder:
            archive, _ = app.make_event_archive(Path(folder), plan["events"])
            client.upload_and_start(archive, "test")
        self.assertEqual([resource.rsplit("/", 1)[-1] for resource, _ in client.calls], ["uploadFile", "createFileDataSet"])
        self.assertEqual([content_type for _, content_type in client.calls], [app.HDL_ACTION_CONTENT_TYPE, app.HDL_ACTION_CONTENT_TYPE])

    def test_content_id_reconciliation_uses_a_quoted_filter(self):
        class DataSetClient(app.Client):
            def __init__(self): self.calls = []
            def data_set(self, request_id):
                error = app.PilotError("HTTP 404"); error.http_status = 404
                raise error
            def get(self, resource, params):
                self.calls.append((resource, params))
                return {"items": [{"ContentId": "UCMFA07750705", "RequestId": 123, "DataSetStatusCode": "ORA_COMPLETE", "ImportStatusCode": "SUCCESS", "LoadStatusCode": "SUCCESS", "ObjectLoadErrorCount": 0}], "hasMore": False}
            def business_objects(self, request_id): return [{"BusinessObjectName": "CalendarEvent", "LoadStatusCode": "SUCCESS", "ObjectLoadErrorCount": 0}]
        client = DataSetClient()
        journal = {"master_submission": {"ContentId": "UCMFA07750705", "RequestId": "123", "state": "submitted"}}
        with tempfile.TemporaryDirectory() as folder:
            app.monitor_master_load(client, journal, {}, Path(folder), timeout=1)
        self.assertEqual(client.calls[0][1]["q"], "ContentId='UCMFA07750705'")

    def test_master_load_accepts_oracle_success_status(self):
        class SuccessClient(app.Client):
            def __init__(self): pass
            def data_set(self, request_id):
                return {"RequestId": 1, "DataSetStatusCode": "ORA_SUCCESS", "ImportStatusCode": "SUCCESS", "LoadStatusCode": "SUCCESS", "ObjectLoadErrorCount": 0}
            def business_objects(self, request_id):
                return [{"BusinessObjectName": "Calendar Event", "LoadStatusCode": "SUCCESS", "ObjectLoadErrorCount": 0}]
        journal = {"master_submission": {"ContentId": "UCMFA07750705", "RequestId": "1", "state": "submitted"}}
        with tempfile.TemporaryDirectory() as folder:
            app.monitor_master_load(SuccessClient(), journal, {}, Path(folder), timeout=1)
        self.assertEqual(journal["master_submission"]["state"], "loaded")

    def test_journal_rejects_other_environment(self):
        with tempfile.TemporaryDirectory() as folder:
            app.journal_path(Path(folder)).write_text(json.dumps({"environment": "https://other.example"}), encoding="utf-8")
            with self.assertRaisesRegex(app.PilotError, "different environment"):
                app.load_journal(Path(folder))

    def test_http_400_keeps_method_query_and_safe_oracle_fields(self):
        class Opener:
            def open(self, request, timeout):
                headers = Message(); headers["x-oracle-dms-ecid"] = "ecid-1"
                raise urllib.error.HTTPError(request.full_url, 400, "Bad Request", headers,
                                             io.BytesIO(b'{"o:errorDetails":[{"code":"JBO-27024","message":"Bad links value"}]}'))
        client = app.Client("Basic TEST_ONLY"); client.opener = Opener()
        with self.assertRaisesRegex(app.PilotError, "JBO-27024"):
            client.get(app.CALENDARS + "/810000331861655/child/AbsenceCalendarEvent/810000331861676",
                       {"fields": app.EVENT_FIELDS, "links": "all"})
        log = client.requests[0]
        self.assertEqual(log["method"], "GET")
        self.assertEqual(log["query_parameters"]["links"], "all")
        self.assertEqual(log["http_status"], 400)
        self.assertEqual(log["ecid"], "ecid-1")
        self.assertIn("JBO-27024", log["oracle_error"])

    def test_plain_text_400_keeps_content_type_and_redacted_excerpt(self):
        class Opener:
            def open(self, request, timeout):
                headers = Message(); headers["Content-Type"] = "text/plain"
                raise urllib.error.HTTPError(request.full_url, 400, "Bad Request", headers,
                                             io.BytesIO(b"Bad links=all Authorization: Basic secret-value"))
        client = app.Client("Basic TEST_ONLY"); client.opener = Opener()
        with self.assertRaisesRegex(app.PilotError, "no parseable"):
            client.get(app.CALENDARS, {"limit": 1, "offset": 0})
        self.assertEqual(client.requests[0]["content_type"], "text/plain")
        self.assertIn("<redacted>", client.requests[0]["response_body_excerpt"])

    def test_flow_instance_status_get_omits_fields_and_onlydata(self):
        class StatusClient(app.Client):
            def __init__(self): self.calls = []
            def get(self, resource, params): self.calls.append((resource, params)); return {"Status": "COMPLETED", "FlowName": "Refresh"}
        client = StatusClient(); client.flow_instance("810000331861758")
        self.assertEqual(client.calls, [(app.API + "/flowInstances/810000331861758", {})])

    def test_gzip_error_includes_endpoint_and_decoded_diagnostic(self):
        class Opener:
            def open(self, request, timeout):
                headers = Message(); headers["Content-Type"] = "application/json"; headers["Content-Encoding"] = "gzip"
                body = gzip.compress(b'{"title":"Unsupported field"}')
                raise urllib.error.HTTPError(request.full_url, 400, "Bad Request", headers, io.BytesIO(body))
        client = app.Client("Basic TEST_ONLY"); client.opener = Opener()
        with self.assertRaisesRegex(app.PilotError, r"HTTP 400 GET endpoint=https://tenant-a"):
            client.get(app.API + "/flowInstances/1", {"fields": "Unsupported"})
        self.assertIn("Unsupported field", client.requests[0]["oracle_error"])

    def test_source_check_uses_paged_collection_without_q_or_links_filter(self):
        class SourceClient(app.Client):
            def __init__(self): self.calls, self.advertised_resources = [], set()
            def get(self, resource, params):
                self.calls.append((resource, params))
                return {"items": [{"ShortCode": "HOLIDAY_A", "CalendarEventId": 810000331861667,
                                   "CalendarCalEventId": 810000331861676, "links": [{"rel": "self", "href": "https://tenant-a.example.invalid/x"}]}], "hasMore": False}
        client = SourceClient()
        self.assertIsNone(client.source_master_lookup_link())
        resource, params = client.calls[0]
        self.assertEqual(resource, app.CALENDARS + "/810000331861655/child/AbsenceCalendarEvent")
        self.assertEqual(params["limit"], 100); self.assertEqual(params["offset"], 0)
        self.assertNotIn("q", params); self.assertNotIn("links", params); self.assertNotIn("onlyData", params)

    def test_key_map_metadata_reads_the_two_configured_flow_ids(self):
        class PagedClient(app.Client):
            def __init__(self): self.calls, self.advertised_resources = [], set()
            def get(self, resource, params):
                self.calls.append((resource, dict(params)))
                if resource in (app.API + "/flowPatterns/" + app.REFRESH_FLOW_ID, app.API + "/flowPatterns/" + app.KEY_MAP_FLOW_ID):
                    return {"FlowId": int(resource.rsplit("/", 1)[-1]), "FlowPattern": "Integration Object User Key Refresh" if resource.endswith(app.REFRESH_FLOW_ID) else "Integration Object User Key Map Extract", "BaseFlowName": "X", "FlowStatus": "A", "LDGRequired": "N"}
                if resource.endswith("/child/flowParameters"):
                    return {"items": [{"FlowParameterId": 9, "FlowParameter": "Business Object"}],
                            "offset": 0, "limit": 100, "hasMore": False}
                raise AssertionError(resource)
        result = PagedClient().key_map_flow_metadata()
        self.assertEqual(result["search_result"], "matching_extracts_found")
        self.assertEqual([row["FlowId"] for row in result["flows"]], [app.REFRESH_FLOW_ID, app.KEY_MAP_FLOW_ID])

    def test_pagination_rejects_repeated_page(self):
        class RepeatClient(app.Client):
            def __init__(self): self.advertised_resources = set()
            def get(self, resource, params):
                return {"items": [{"FlowId": 1}], "offset": 0, "hasMore": True}
        with self.assertRaisesRegex(app.PilotError, "repeated"):
            RepeatClient().paged_get(app.API + "/flowPatterns", "FlowId")

    def test_key_map_xml_requires_explicit_calendar_event_id(self):
        xml = app.ET.fromstring("""<Root><Integration_Object><Object_Name>CalendarEvent</Object_Name>
            <Integration_Key><Key_Details><Surrogate_Id>810000331861667</Surrogate_Id></Key_Details>
            <User_Keys><User_Key_Attribute><Name>ShortCode</Name><Value>HOLIDAY_A</Value></User_Key_Attribute></User_Keys>
            </Integration_Key></Integration_Object></Root>""")
        self.assertEqual(app.resolve_calendar_event_ids(xml, ["HOLIDAY_A"]), {"HOLIDAY_A": "810000331861667"})
        with self.assertRaisesRegex(app.PilotError, "missing or ambiguous identity"):
            app.resolve_calendar_event_ids(app.ET.fromstring("<Root><Integration_Object><Object_Name>CalendarEvent</Object_Name><Integration_Key><User_Keys><User_Key_Attribute><Name>ShortCode</Name><Value>HOLIDAY_A</Value></User_Key_Attribute></User_Keys></Integration_Key></Integration_Object></Root>"), ["HOLIDAY_A"])

    def test_extract_result_handles_escaped_soap_xml(self):
        soap = ("<soap:Envelope xmlns:soap='http://schemas.xmlsoap.org/soap/envelope/'><soap:Body>"
                "<fetchExtractOutputResponse><result>&lt;Root&gt;&lt;Integration_Object&gt;&lt;Object_Name&gt;CalendarEvent&lt;/Object_Name&gt;"
                "&lt;Integration_Key&gt;&lt;Key_Details&gt;&lt;Surrogate_Id&gt;810000331861667&lt;/Surrogate_Id&gt;&lt;/Key_Details&gt;"
                "&lt;User_Keys&gt;&lt;User_Key_Attribute&gt;&lt;Name&gt;ShortCode&lt;/Name&gt;&lt;Value&gt;HOLIDAY_A&lt;/Value&gt;"
                "&lt;/User_Key_Attribute&gt;&lt;/User_Keys&gt;&lt;/Integration_Key&gt;&lt;/Integration_Object&gt;&lt;/Root&gt;</result>"
                "</fetchExtractOutputResponse></soap:Body></soap:Envelope>").encode()
        root = app.extract_result(soap)
        self.assertEqual(app.resolve_calendar_event_ids(root, ["HOLIDAY_A"])["HOLIDAY_A"], "810000331861667")

    def test_empty_archiver_response_uses_only_actual_completed_checklist_name_for_flow(self):
        class ExtractClient:
            def __init__(self): self.requests, self.calls = [], []
            def flow_instance(self, flow_id):
                return {"FlowInstanceId": int(flow_id), "FlowName": "RecordedMap", "Status": "COMPLETED"}
            def flow_checklists(self, flow_id):
                self.calls.append(("checklists", flow_id))
                return [{"Status": "COMPLETED", "ChecklistName": "Integration Object User Key Map Extract",
                         "FlowTaskInstanceId": 99}]
            def fetch_extract_output(self, instance, task, mode):
                self.calls.append(("fetch", instance, task, mode))
                if mode == "ARCHIVER":
                    raise app.EmptyExtractOutput("empty")
                return app.ET.fromstring("<Root><Integration_Object><Object_Name>CalendarEvent</Object_Name><Integration_Key><Key_Details><Surrogate_Id>810000331861667</Surrogate_Id></Key_Details><User_Keys><User_Key_Attribute><Name>ShortCode</Name><Value>HOLIDAY_A</Value></User_Key_Attribute></User_Keys></Integration_Key></Integration_Object></Root>")
        client = ExtractClient()
        with tempfile.TemporaryDirectory() as folder:
            journal = {"environment": app.DEFAULT_BASE, "events": {}, "calendars": {}, "links": {},
                       "key_map_submissions": [{"FlowId": app.KEY_MAP_FLOW_ID, "FlowInstanceId": "22", "FlowInstanceName": "RecordedMap",
                                                  "kind": "key_map_extract", "generation": "baseline", "status": "completed"}]}
            resolved = app.resolve_with_key_map(client, journal, {}, Path(folder), ["HOLIDAY_A"], resume_only=True)
        self.assertEqual(resolved, {"HOLIDAY_A": "810000331861667"})
        self.assertEqual(client.calls, [("fetch", "RecordedMap", "", "ARCHIVER"), ("checklists", "22"),
                                        ("fetch", "RecordedMap", "Integration Object User Key Map Extract", "FLOW")])

    def test_apply_dispatches_to_the_guarded_pilot_controller(self):
        class FallbackClient:
            def __init__(self, authorization): self.authorization, self.requests = authorization, []
            def source_master_lookup_link(self): return None
        with tempfile.TemporaryDirectory() as folder, patch.object(app, "Client", FallbackClient), \
                patch.object(app, "credentials", return_value="Basic TEST_ONLY"), \
                patch.object(app, "apply_pilot", side_effect=lambda client, plan, journal, report, output: report.update(status="applied_verified")) as guarded_apply, \
                contextlib.redirect_stdout(io.StringIO()):
            code = app.main(["apply", "--apply", "--output-dir", folder])
            report = json.loads(next(Path(folder).glob("K12_SAMPLE_A_Create_Pilot_*.json")).read_text())
        self.assertEqual(code, 0); self.assertEqual(report["status"], "applied_verified")
        guarded_apply.assert_called_once()

    def test_cli_keymap_resolve_requires_apply_and_accepts_only_known_id(self):
        class ResolveClient:
            def __init__(self, authorization): self.authorization, self.requests = authorization, []
            def source_master_lookup_link(self): return None
        with tempfile.TemporaryDirectory() as folder, patch.object(app, "Client", ResolveClient), \
                patch.object(app, "credentials", return_value="Basic TEST_ONLY"), \
                patch.object(app, "resolve_with_key_map", return_value={"HOLIDAY_A": "810000331861667"}) as resolver, \
                contextlib.redirect_stdout(io.StringIO()):
            code = app.main(["keymap-resolve", "--apply", "--output-dir", folder])
            report = json.loads(next(Path(folder).glob("K12_SAMPLE_A_Create_Pilot_*.json")).read_text())
        self.assertEqual(code, 0); self.assertEqual(report["status"], "keymap_xml_verified")
        self.assertEqual(resolver.call_args.args[-1], ["HOLIDAY_A"])

    def test_cli_reconciles_recorded_refresh_without_resubmitting_it(self):
        recorded_calls = []
        class ReconcileClient:
            def __init__(self, authorization): self.authorization, self.requests, self.calls, self.names = authorization, [], recorded_calls, {"810000331861758": "RecordedRefresh"}
            def source_master_lookup_link(self): return None
            def flow_instance(self, flow_instance_id):
                self.calls.append(("flow_instance", flow_instance_id))
                return {"Status": "COMPLETED", "FlowName": self.names[flow_instance_id], "links": [{"rel": "self", "href": "https://example.invalid/flow"}]}
            def key_map_flow_metadata(self):
                return {"flows": [
                    {"FlowId": app.REFRESH_FLOW_ID, "FlowPattern": "Integration Object User Key Refresh", "FlowStatus": "A",
                     "parameters": [{"FlowParameterId": 10, "FlowParameter": "Business Object", "Display": "M"}]},
                    {"FlowId": app.KEY_MAP_FLOW_ID, "FlowPattern": "Integration Object User Key Map Extract", "FlowStatus": "A",
                     "parameters": [{"FlowParameterId": 11, "FlowParameter": "Business Object", "Display": "M"}]},
                ]}
            def flow_metadata_by_id(self, metadata, flow_id, words):
                return ({}, "10" if flow_id == app.REFRESH_FLOW_ID else "11")
            def submit_flow(self, flow_id, instance_name, parameter_id):
                self.calls.append(("submit", flow_id))
                if flow_id != app.KEY_MAP_FLOW_ID: raise AssertionError("Refresh was resubmitted")
                self.names["22"] = instance_name
                return {"FlowId": flow_id, "FlowInstanceId": "22", "FlowInstanceName": instance_name, "kind": "key_map_extract"}
            def flow_checklists(self, flow_instance_id):
                return [{"Status": "COMPLETED", "ChecklistName": "Integration Object User Key Map Extract"}]
            def fetch_extract_output(self, instance_name, task_name, mode):
                self.calls.append(("fetch", instance_name, task_name, mode))
                return app.ET.fromstring("<Root><Integration_Object><Object_Name>CalendarEvent</Object_Name><Integration_Key><Key_Details><Surrogate_Id>810000331861667</Surrogate_Id></Key_Details><User_Keys><User_Key_Attribute><Name>ShortCode</Name><Value>HOLIDAY_A</Value></User_Key_Attribute></User_Keys></Integration_Key></Integration_Object></Root>")
        with tempfile.TemporaryDirectory() as folder:
            app.journal_path(Path(folder)).write_text(json.dumps({"environment": app.DEFAULT_BASE, "events": {}, "calendars": {}, "links": {},
                "key_map_submissions": [{"FlowId": app.REFRESH_FLOW_ID, "FlowInstanceId": "810000331861758", "FlowInstanceName": "RecordedRefresh", "kind": "refresh"}]}), encoding="utf-8")
            with patch.object(app, "Client", ReconcileClient), patch.object(app, "credentials", return_value="Basic TEST_ONLY"), contextlib.redirect_stdout(io.StringIO()):
                code = app.main(["keymap-resolve", "--apply", "--output-dir", folder])
            report = json.loads(next(Path(folder).glob("K12_SAMPLE_A_Create_Pilot_*.json")).read_text())
        self.assertEqual(code, 0); self.assertEqual(report["status"], "keymap_xml_verified")
        self.assertEqual(recorded_calls[0], ("flow_instance", "810000331861758"))
        self.assertNotIn(("submit", app.REFRESH_FLOW_ID), recorded_calls)
        self.assertIn(("submit", app.KEY_MAP_FLOW_ID), recorded_calls)


if __name__ == "__main__": unittest.main()
