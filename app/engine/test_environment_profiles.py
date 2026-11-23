"""Portable creation profile regressions; all HTTP responses are local fakes."""
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

from . import create_adapter as adapter
from . import create_pilot as core
from .test_creation_adapter import AUTH, InMemoryOracle, make_plan


ORIGIN_A = "https://fusion-test-a.example"
ORIGIN_B = "https://fusion-test-b.example"
REFRESH_NAME = "Integration Object User Key Refresh"
MAP_NAME = "Integration Object User Key Map Extract"


def profile(origin=ORIGIN_A, identity="test-a", **integration):
    return {"environment": origin, "profile_id": identity, "integration": integration}


def xml_keys(rows=(), object_name="CalendarEvent"):
    root = ET.Element("Root")
    obj = ET.SubElement(root, "Integration_Object")
    ET.SubElement(obj, "Object_Name").text = object_name
    for code, event_id in rows:
        key = ET.SubElement(obj, "Integration_Key")
        details = ET.SubElement(key, "Key_Details")
        ET.SubElement(details, "Surrogate_Id").text = event_id
        keys = ET.SubElement(key, "User_Keys")
        attr = ET.SubElement(keys, "User_Key_Attribute")
        ET.SubElement(attr, "Name").text = "ShortCode"
        ET.SubElement(attr, "Value").text = code
    return root


def wsdl(endpoint):
    return ('''<definitions xmlns="http://schemas.xmlsoap.org/wsdl/"
       xmlns:soap="http://schemas.xmlsoap.org/wsdl/soap/" xmlns:tns="urn:test">
       <binding name="ExtractBinding"><operation name="fetchExtractOutput">
       <soap:operation soapAction="urn:fetchExtractOutput"/></operation></binding>
       <service name="Extract"><port name="ExtractPort" binding="tns:ExtractBinding">
       <soap:address location="%s"/></port></service></definitions>''' % endpoint).encode()


class Response(io.BytesIO):
    def __init__(self, value):
        if isinstance(value, dict):
            value = json.dumps(value).encode()
        super().__init__(value)
        self.status = 200
        self.headers = {"Content-Type": "application/json" if value.startswith(b"{") else "text/xml"}


class CaptureTransport:
    def __init__(self, handler):
        self.calls, self.handler = [], handler

    def open(self, request, timeout):
        self.calls.append(request)
        return Response(self.handler(request))


def flow_row(identity, name, active="A"):
    return {"FlowId": identity, "FlowPattern": name, "FlowStatus": active, "LDGRequired": "N"}


def flow_transport(rows):
    by_id = {str(row["FlowId"]): row for row in rows}
    def handler(request):
        path = urlsplit(request.full_url).path
        if path.endswith("/child/flowParameters"):
            flow_id = path.split("/")[-3]
            return {"items": [{"FlowParameterId": int(flow_id) + 100, "FlowParameter": "Business Object", "Display": "M"}],
                    "hasMore": False, "offset": 0, "limit": 100}
        if path == core.API + "/flowPatterns":
            return {"items": rows, "hasMore": False, "offset": 0, "limit": 100}
        if path.startswith(core.API + "/flowPatterns/"):
            return by_id[path.rsplit("/", 1)[-1]]
        raise AssertionError("Unexpected transport request: " + request.full_url)
    return CaptureTransport(handler)


class PortableTransportTests(unittest.TestCase):
    def test_two_profiles_route_rest_and_soap_to_their_own_origins(self):
        for origin, identity in ((ORIGIN_A, "test-a"), (ORIGIN_B, "test-b")):
            with self.subTest(origin=origin):
                client = core.Client(AUTH, config=profile(origin, identity))
                body = ('''<s:Envelope xmlns:s="%s"><s:Body><fetchExtractOutputResponse>
                    <result>&lt;Root/&gt;</result></fetchExtractOutputResponse></s:Body></s:Envelope>'''
                    % core.SOAP_ENVELOPE_NS).encode()
                def handler(request):
                    if request.full_url.endswith(core.SOAP_WSDL_PATH):
                        return wsdl(origin + core.SOAP_SERVICE_PATH)
                    if request.get_method() == "POST":
                        return body
                    return {"items": [], "hasMore": False}
                transport = CaptureTransport(handler)
                client.opener = transport
                client.get(core.CALENDARS, {})
                self.assertEqual(client.fetch_extract_output("saved-map", "", "ARCHIVER").tag, "Root")
                self.assertEqual([r.get_method() for r in transport.calls], ["GET", "GET", "POST"])
                self.assertTrue(all(r.full_url.startswith(origin + "/") for r in transport.calls))
                self.assertTrue(all(r.get_header("Authorization") == AUTH for r in transport.calls))
                self.assertNotIn(core.DEFAULT_BASE, json.dumps(client.requests))

    def test_cross_origin_and_invalid_soap_endpoints_stop_before_post(self):
        endpoints = [ORIGIN_B + core.SOAP_SERVICE_PATH,
                     "http://fusion-test-a.example" + core.SOAP_SERVICE_PATH,
                     ORIGIN_A + ":444" + core.SOAP_SERVICE_PATH,
                     ORIGIN_A + core.SOAP_SERVICE_PATH + "?redirect=true",
                     ORIGIN_A + "/unrelated-service"]
        for endpoint in endpoints:
            with self.subTest(endpoint=endpoint):
                client = core.Client(AUTH, config=profile())
                transport = CaptureTransport(lambda request: wsdl(endpoint))
                client.opener = transport
                with self.assertRaises(core.PilotError):
                    client.fetch_extract_output("saved-map", "", "ARCHIVER")
                self.assertEqual([r.get_method() for r in transport.calls], ["GET"])
                self.assertTrue(transport.calls[0].full_url.startswith(ORIGIN_A + "/"))

    def test_absolute_rest_resource_cannot_redirect_credentials(self):
        client = core.Client(AUTH, config=profile())
        transport = CaptureTransport(lambda request: {"items": []})
        client.opener = transport
        with self.assertRaises(core.PilotError):
            client.get(ORIGIN_B + core.CALENDARS, {})
        self.assertEqual(transport.calls, [])

    def test_explicit_profile_does_not_read_sample_a_reference_association(self):
        client = core.Client(AUTH, config=profile())
        transport = CaptureTransport(lambda request: {"items": [], "hasMore": False})
        client.opener = transport
        self.assertIsNone(client.source_master_lookup_link())
        self.assertFalse(any(core.SOURCE_CALENDAR in r.full_url for r in transport.calls))
        self.assertFalse(any(core.DEFAULT_BASE in r.full_url for r in transport.calls))

    def test_explicit_profile_missing_origin_never_falls_back_to_sample_a(self):
        with self.assertRaises((ValueError, core.PilotError)):
            core.Client(AUTH, config={"profile_id": "test-a", "integration": {}})

    def test_soap_endpoint_must_preserve_explicit_profile_port(self):
        selected = ORIGIN_A + ":8443"
        client = core.Client(AUTH, config=profile(selected))
        transport = CaptureTransport(lambda request: wsdl(selected + core.SOAP_SERVICE_PATH))
        client.opener = transport
        self.assertEqual(client.soap_contract()["endpoint"], selected + core.SOAP_SERVICE_PATH)
        self.assertTrue(transport.calls[0].full_url.startswith(selected + "/"))
        client.opener = CaptureTransport(lambda request: wsdl(ORIGIN_A + core.SOAP_SERVICE_PATH))
        with self.assertRaises(core.PilotError):
            client.fetch_extract_output("saved-map", "", "ARCHIVER")
        self.assertEqual([r.get_method() for r in client.opener.calls], ["GET"])

    def test_legacy_no_config_retains_existing_origin(self):
        client = core.Client(AUTH)
        transport = CaptureTransport(lambda request: {"items": [], "hasMore": False})
        client.opener = transport
        client.get(core.CALENDARS, {})
        self.assertEqual(transport.calls[0].full_url, core.DEFAULT_BASE + core.CALENDARS)

    def test_profile_specific_flow_ids_never_fall_back_to_sample_a_ids(self):
        client = core.Client(AUTH, config=profile(refresh_flow_id="9101", key_map_flow_id="9102"))
        transport = flow_transport([flow_row(9101, REFRESH_NAME), flow_row(9102, MAP_NAME)])
        client.opener = transport
        result = client.key_map_flow_metadata()
        self.assertEqual({str(row["FlowId"]) for row in result["flows"]}, {"9101", "9102"})
        urls = " ".join(r.full_url for r in transport.calls)
        self.assertNotIn(core.REFRESH_FLOW_ID, urls)
        self.assertNotIn(core.KEY_MAP_FLOW_ID, urls)
        self.assertTrue(all(r.full_url.startswith(ORIGIN_A + "/") for r in transport.calls))
        for flow_id, words in (("9101", ("refresh",)), ("9102", ("map extract",))):
            _, parameter = client.flow_metadata_by_id(result, flow_id, words)
            self.assertEqual(str(parameter), str(int(flow_id) + 100))

    def test_unconfigured_flows_are_discovered_by_exact_active_names(self):
        client = core.Client(AUTH, config=profile())
        transport = flow_transport([flow_row(9201, REFRESH_NAME), flow_row(9202, MAP_NAME),
                                    flow_row(9290, MAP_NAME + " Copy"), flow_row(9291, "Other flow")])
        client.opener = transport
        result = client.key_map_flow_metadata()
        self.assertEqual({str(row["FlowId"]) for row in result["flows"]}, {"9201", "9202"})
        self.assertFalse(any(core.REFRESH_FLOW_ID in r.full_url or core.KEY_MAP_FLOW_ID in r.full_url
                             for r in transport.calls))

    def test_ambiguous_or_inactive_flow_discovery_cannot_enable_submission(self):
        cases = [
            [flow_row(9201, REFRESH_NAME), flow_row(9202, MAP_NAME), flow_row(9203, MAP_NAME)],
            [flow_row(9201, REFRESH_NAME), flow_row(9202, MAP_NAME, "I")],
            [flow_row(9201, REFRESH_NAME), flow_row(9202, MAP_NAME + " Copy")],
        ]
        for rows in cases:
            with self.subTest(rows=rows):
                client = core.Client(AUTH, config=profile())
                transport = flow_transport(rows)
                client.opener = transport
                with self.assertRaises(core.PilotError):
                    metadata = client.key_map_flow_metadata()
                    for row in metadata["flows"]:
                        client.flow_metadata_by_id(metadata, str(row["FlowId"]), ("integration object",))
                self.assertTrue(all(request.get_method() == "GET" for request in transport.calls))


class PortableKeyMapTests(unittest.TestCase):
    def test_valid_empty_calendar_event_inventory_allows_new_codes(self):
        self.assertEqual(core.resolve_calendar_event_ids(xml_keys(), ["NEW_EVENT"], require_all=False), {})
        self.assertEqual(core.resolve_calendar_event_ids(xml_keys([("OTHER_EVENT", "456")]),
                                                        ["NEW_EVENT"], require_all=False), {})
        with self.assertRaises(core.PilotError):
            core.resolve_calendar_event_ids(xml_keys(), ["NEW_EVENT"], require_all=True)

    def test_wrong_object_or_unrecognized_structure_is_not_zero_event_proof(self):
        for root in (xml_keys(object_name="Worker"), ET.fromstring("<Root/>"),
                     ET.fromstring("<Root><CalendarEventId>123</CalendarEventId></Root>")):
            with self.subTest(xml=ET.tostring(root)):
                with self.assertRaises(core.PilotError):
                    core.resolve_calendar_event_ids(root, ["NEW_EVENT"], require_all=False)

    def test_malformed_nonmatching_rows_cannot_prove_requested_code_absent(self):
        mutations = (
            lambda key: key.remove(key.find("Key_Details")),
            lambda key: key.remove(key.find("User_Keys")),
            lambda key: setattr(key.find("Key_Details/Surrogate_Id"), "text", "not-an-id"),
            lambda key: setattr(key.find("User_Keys/User_Key_Attribute/Name"), "text", "UnexpectedKey"),
            lambda key: key.append(deepcopy(key.find("Key_Details"))),
            lambda key: key.append(deepcopy(key.find("User_Keys"))),
        )
        for mutate in mutations:
            root = xml_keys([("OTHER_EVENT", "456")])
            mutate(root.find("Integration_Object/Integration_Key"))
            with self.subTest(xml=ET.tostring(root)):
                with self.assertRaises(core.PilotError):
                    core.resolve_calendar_event_ids(root, ["NEW_EVENT"], require_all=False)

    def test_generic_key_map_resume_requires_no_mlk_anchor(self):
        client = core.Client(AUTH, config=profile())
        client.flow_instance = lambda identity: {"FlowInstanceId": identity, "FlowName": "PortableMap", "Status": "COMPLETED"}
        client.fetch_extract_output = lambda instance, task, mode: xml_keys([("NEW_EVENT", "777")])
        journal = {"environment": ORIGIN_A, "profile_id": "test-a", "events": {}, "calendars": {}, "links": {},
                   "key_map_submissions": [{"FlowId": "9202", "FlowInstanceId": "44", "FlowInstanceName": "PortableMap",
                                             "kind": "key_map_extract", "generation": "baseline", "status": "completed"}]}
        with tempfile.TemporaryDirectory() as folder:
            result = core.resolve_with_key_map(client, journal, {}, Path(folder), ["NEW_EVENT"], resume_only=True)
        self.assertEqual(result, {"NEW_EVENT": "777"})
        self.assertNotIn(core.KNOWN_CODE, journal["key_map_submissions"][0]["resolved_codes"])


class PortableJournalTests(unittest.TestCase):
    def test_config_plan_origin_mismatch_is_rejected_before_client(self):
        plan = make_plan()
        plan["environment"] = ORIGIN_A
        with tempfile.TemporaryDirectory() as folder, patch.object(core, "Client") as client:
            report = adapter.run_create(plan, AUTH, Path(folder), config=profile(ORIGIN_B, "test-b"))
        self.assertEqual(report["status"], "failed")
        client.assert_not_called()
        self.assertEqual(report["write_requests"], 0)

    def test_two_environment_builds_and_unchanged_resume_are_independent(self):
        with tempfile.TemporaryDirectory() as folder:
            for origin, identity in ((ORIGIN_A, "test-a"), (ORIGIN_B, "test-b")):
                with self.subTest(origin=origin):
                    service = InMemoryOracle()
                    plan = make_plan(); plan["environment"] = origin
                    config = profile(origin, identity)
                    target = Path(folder) / identity
                    with patch.object(core, "Client", side_effect=service.client), \
                            patch.object(core, "resolve_with_key_map", side_effect=service.resolve):
                        first = adapter.run_create(plan, AUTH, target, config=config)
                        self.assertEqual(first["status"], "applied_verified", first.get("error"))
                        second = adapter.run_create(plan, AUTH, target, config=config)
                    self.assertEqual(second["status"], "applied_verified", second.get("error"))
                    self.assertEqual(second["write_requests"], 0)
                    self.assertEqual(first["environment"], origin)
                    journal = json.loads(core.journal_path(target).read_text())
                    self.assertEqual(journal["environment"], origin)
                    self.assertEqual(journal["profile_id"], identity)

    def test_reusing_same_request_folder_for_other_origin_or_profile_blocks_before_client(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder)
            plan = make_plan(); plan["environment"] = ORIGIN_A
            service = InMemoryOracle()
            with patch.object(core, "Client", side_effect=service.client), \
                    patch.object(core, "resolve_with_key_map", side_effect=service.resolve):
                first = adapter.run_create(plan, AUTH, target, config=profile())
            self.assertEqual(first["status"], "applied_verified", first.get("error"))
            saved = core.journal_path(target).read_bytes()
            for origin, identity in ((ORIGIN_B, "test-b"), (ORIGIN_A, "other-profile")):
                with self.subTest(origin=origin, identity=identity), patch.object(core, "Client") as client:
                    changed = deepcopy(plan); changed["environment"] = origin
                    report = adapter.run_create(changed, AUTH, target, config=profile(origin, identity))
                    self.assertEqual(report["status"], "failed")
                    self.assertEqual(report["write_requests"], 0)
                    client.assert_not_called()
                    self.assertEqual(core.journal_path(target).read_bytes(), saved)


if __name__ == "__main__":
    unittest.main()
