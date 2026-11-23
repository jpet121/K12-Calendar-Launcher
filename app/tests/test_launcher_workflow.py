"""Localhost HTTP workflow tests: real launcher and services, mocked Oracle only.

These are offline integration tests, not evidence of a live Oracle acceptance run.
"""
import base64
import copy
import http.client
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE))
import launcher
import operations
from engine import create_pilot as core
from engine.test_creation_adapter import InMemoryOracle


class ManagementTransport:
    """Expose the creation test's same Oracle world through real read/edit paths."""

    def __init__(self, world):
        self.world = world
        self.versions = {}
        self.requests = []
        self.lose_next_patch_response = False
        self.pause_next_read = None

    def request(self, method, resource, *, params=None, payload=None, headers=None):
        self.requests.append({"method": method, "resource": resource,
                              "payload": copy.deepcopy(payload), "headers": copy.deepcopy(headers)})
        if self.pause_next_read and method == "GET":
            entered, released = self.pause_next_read
            self.pause_next_read = None
            entered.set()
            if not released.wait(5):
                raise operations.OperationError("Test read gate timed out")
        assert resource.startswith(operations.CALENDARS), resource
        suffix = resource[len(operations.CALENDARS):].strip("/")
        cid = suffix.split("/")[0] if suffix else None
        if method == "PATCH":
            assert headers == {"If-Match": self.versions.get(cid, '"1"')}, headers
            assert set(payload) <= set(operations.EDIT_FIELDS), payload
            self.world.calendars[cid].update(copy.deepcopy(payload))
            current = int(self.versions.get(cid, '"1"').strip('"'))
            self.versions[cid] = '"%s"' % (current + 1)
            if self.lose_next_patch_response:
                self.lose_next_patch_response = False
                raise operations.OperationError("Simulated lost response after committed PATCH", code="transport_error")
            return operations.Response(200, copy.deepcopy(self.world.calendars[cid]), {"ETag": self.versions[cid]})
        assert method == "GET", "Unexpected write: " + method
        if suffix and "/" not in suffix:
            return operations.Response(200, copy.deepcopy(self.world.calendars[cid]),
                                       {"ETag": self.versions.get(cid, '"1"')})
        if not suffix:
            rows = list(self.world.calendars.values())
            key = "CalendarId"
        elif suffix.endswith("AbsenceCalendarEvent"):
            master = {row["CalendarEventId"]: row for row in self.world.events.values()}
            rows = [{**master[link["CalendarEventId"]], **link}
                    for link in self.world.links.values() if link["CalendarId"] == cid]
            key = "CalendarCalEventId"
        else:
            assert suffix.endswith("AbsenceCalendarSubscriber"), suffix
            rows, key = [], "CalendarSubscriberId"
        rows = sorted(rows, key=lambda row: int(row[key]))
        offset = (params or {}).get("offset", 0)
        # Deliberately paginate one row at a time to exercise complete reads.
        items = copy.deepcopy(rows[offset:offset + 1])
        return operations.Response(200, {"items": items, "count": len(items), "offset": offset,
                                         "hasMore": offset + len(items) < len(rows)}, {})


class LauncherHTTPWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "templates").mkdir()
        shutil.copy2(PACKAGE / "templates" / "Calendar_Build_Sample.xlsx", self.root / "templates")
        self.world = InMemoryOracle()
        self.transport = ManagementTransport(self.world)
        self.config = launcher.load_config(PACKAGE / "tests" / "fixture-profile.json", "sample-a")
        self.config["administrator_asserted_full_calendar_visibility"] = False
        self.config["administrator_asserted_no_legacy_event_coverage"] = False
        self.auth = "Basic " + base64.b64encode(b"offline-operator:offline-test-password").decode()

        def service_factory(authorization, config, state_dir, callback):
            self.assertEqual(authorization, self.auth)
            return operations.CalendarService(authorization, config, state_dir, callback, client=self.transport)

        self.app = launcher.Application(self.root, self.config, service_factory=service_factory)
        self.app.select_environment("sample-a")
        self.patches = [patch.object(core, "Client", side_effect=self.world.client),
                        patch.object(core, "resolve_with_key_map", side_effect=self.world.resolve)]
        for replacement in self.patches:
            replacement.start()
        self.server = launcher.make_server(self.app)
        self.server_thread = threading.Thread(target=self.server.serve_forever,
                                              kwargs={"poll_interval": 0.01}, daemon=True)
        self.server_thread.start()
        self.origin = "http://127.0.0.1:" + str(self.server.server_port)
        self.gates = []

    def tearDown(self):
        for gate in self.gates:
            gate.set()
        if self.app.thread:
            self.app.thread.join(timeout=5)
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=5)
        for replacement in reversed(self.patches):
            replacement.stop()
        self.temporary.cleanup()

    def request(self, method, path, data=None, headers=None):
        actual_headers = {"X-Launcher-Token": self.app.token}
        if method == "POST":
            actual_headers.update({"Origin": self.origin, "Content-Type": "application/json"})
        actual_headers.update(headers or {})
        actual_headers = {key: value for key, value in actual_headers.items() if value is not None}
        content = json.dumps(data).encode() if data is not None else None
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request(method, path, body=content, headers=actual_headers)
            response = connection.getresponse()
            raw = response.read()
            value = json.loads(raw) if "application/json" in response.getheader("Content-Type", "") else raw
            return response.status, value, dict(response.getheaders())
        finally:
            connection.close()

    def wait(self, job_id, *, error=False):
        end = time.monotonic() + 10
        while time.monotonic() < end:
            status, state, _ = self.request("GET", "/api/state")
            self.assertEqual(status, 200)
            self.assertEqual(state["job_id"], job_id)
            if not state["busy"]:
                if error:
                    self.assertTrue(state["error"], state)
                else:
                    self.assertIsNone(state["error"], state)
                encoded = json.dumps(state)
                self.assertNotIn("offline-test-password", encoded)
                self.assertNotIn(self.auth, encoded)
                return state
            time.sleep(0.01)
        self.fail("Local asynchronous launcher operation did not complete")

    def action(self, action, *, error=False, **data):
        status, accepted, _ = self.request("POST", "/api/action", {"action": action, **data})
        self.assertEqual(status, 202, accepted)
        return self.wait(accepted["job_id"], error=error)

    def connect(self):
        state = self.action("connect", username="offline-operator", password="offline-test-password")
        self.assertTrue(state["signed_in"])
        self.assertEqual(state["result"]["status"], "connected")
        return state

    def create_sample(self):
        preview = self.action("preview_create", example=True)["result"]
        self.assertEqual(preview["preview"]["blockers"], [])
        self.assertEqual(preview["preview"]["counts"], {"new_events": 3, "new_calendars": 2, "selected_links": 4})
        result = self.action("apply_create", preview_id=preview["preview_id"])["result"]
        self.assertEqual(result["status"], "applied_verified", result)
        return preview, result

    def test_connect_build_repeat_export_edit_reconcile_delete_guard_disconnect(self):
        connected = self.connect()
        self.assertEqual(connected["result"]["snapshot"]["counts"]["calendars"], 0)
        self.assertEqual(self.action("read")["result"]["status"], "read_verified")
        preview, first = self.create_sample()
        self.assertEqual(first["report"]["result_counts"], {"events": 3, "calendars": 2, "links": 4})
        self.assertGreater(first["report"]["write_requests"], 0)
        self.assertEqual(first["report"]["repeatability"]["write_requests"], 0)
        second = self.action("apply_create", preview_id=preview["preview_id"],
                             plan={"calendars": [{"Name": "Unreviewed replacement"}]})["result"]
        self.assertEqual(second["status"], "applied_verified")
        self.assertEqual(second["report"]["write_requests"], 0)
        self.assertEqual((len(self.world.events), len(self.world.calendars), len(self.world.links)), (3, 2, 4))

        exported = self.action("export")["result"]
        self.assertEqual(exported["status"], "exported")
        status, archive_bytes, headers = self.request("GET", "/api/download/" + exported["download_id"])
        self.assertEqual(status, 200)
        self.assertIn(exported["filename"], headers["Content-Disposition"])
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            snapshot = json.loads(archive.read("snapshot.json"))
            self.assertEqual(snapshot["counts"]["links"], 4)
            self.assertTrue(snapshot["complete"])
            self.assertEqual(set(archive.namelist()), {"snapshot.json", "calendars.csv", "calendar_events.csv", "READ_ME.txt"})

        cid = snapshot["calendars"][0]["CalendarId"]
        edit = self.action("preview_calendar_edit", calendar_id=cid,
                           changes={"Description": "Reviewed through the launcher"})["result"]
        edited = self.action("apply_calendar_edit", preview_id=edit["preview_id"],
                             changes={"Description": "Unreviewed injection"})["result"]
        self.assertEqual(edited["status"], "updated_verified")
        self.assertEqual(self.world.calendars[cid]["Description"], "Reviewed through the launcher")
        self.assertTrue(edited["recovery_receipt_id"])
        self.assertEqual(len([r for r in self.transport.requests if r["method"] == "PATCH"]), 1)
        reconciled = self.action("reconcile_calendar", receipt_id=edited["recovery_receipt_id"])["result"]
        self.assertEqual(reconciled["status"], "reconciled_applied")
        self.assertEqual(len([r for r in self.transport.requests if r["method"] == "PATCH"]), 1)

        deletion = self.action("check_delete", calendar_id=cid)["result"]
        self.assertFalse(deletion["preview"]["allowed"])
        self.assertIsNone(deletion["preview"]["assignment_count"])
        blocked = self.action("apply_delete", error=True, preview_id=deletion["preview_id"], assigned_users=0)
        self.assertIn("Deletion is blocked", blocked["error"])
        self.assertFalse(any(r["method"] == "DELETE" for r in self.transport.requests))
        self.assertEqual(len(self.world.calendars), 2)
        disconnected = self.action("disconnect")
        self.assertFalse(disconnected["signed_in"])
        self.assertEqual(disconnected["result"]["status"], "signed_out")
        self.assertIsNone(self.app.service)
        self.assertEqual(self.app.authorization, "")
        self.assertIn("Sign in", self.action("read", error=True)["error"])
        for path in (self.root / "state").rglob("*.json"):
            saved = path.read_text(encoding="utf-8")
            self.assertNotIn("offline-test-password", saved, path.name)
            self.assertNotIn(self.auth, saved, path.name)

    def test_uncertain_edit_receipt_is_reachable_and_reconciles_without_retry(self):
        self.connect()
        self.create_sample()
        cid = next(iter(self.world.calendars))
        edit = self.action("preview_calendar_edit", calendar_id=cid, changes={"Description": "Committed despite lost response"})["result"]
        self.transport.lose_next_patch_response = True
        failed = self.action("apply_calendar_edit", error=True, preview_id=edit["preview_id"])
        receipt_id = failed["result"]["recovery_receipt_id"]
        status, receipt_bytes, _ = self.request("GET", "/api/download/" + failed["result"]["receipt_download"]["download_id"])
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(receipt_bytes)["status"], "outcome_unknown")
        result = self.action("reconcile_calendar", receipt_id=receipt_id)["result"]
        self.assertEqual(result["status"], "reconciled_applied")
        same = self.action("preview_calendar_edit", calendar_id=cid,
                           changes={"Description": "Committed despite lost response"})["result"]
        self.assertEqual(self.action("apply_calendar_edit", preview_id=same["preview_id"])["result"]["status"], "unchanged_verified")
        self.assertEqual(len([r for r in self.transport.requests if r["method"] == "PATCH"]), 1)

    def test_server_rejects_foreign_origin_missing_token_and_host_before_execution(self):
        cases = [("GET", "/api/state", None, {"X-Launcher-Token": None}),
                 ("GET", "/api/state", None, {"X-Launcher-Token": "incorrect"}),
                 ("GET", "/api/state", None, {"Host": "attacker.invalid"}),
                 ("POST", "/api/action", {"action": "connect"}, {"Origin": "https://attacker.invalid"}),
                 ("POST", "/api/action", {"action": "connect"}, {"Origin": None}),
                 ("POST", "/api/action", {"action": "connect"}, {"X-Launcher-Token": None})]
        for method, path, body, headers in cases:
            with self.subTest(method=method, headers=headers):
                self.assertEqual(self.request(method, path, body, headers)[0], 403)
        self.assertEqual(self.transport.requests, [])
        self.assertEqual(self.world.instances, [])
        self.assertIsNone(self.app.job_id)

    def test_async_busy_rejects_parallel_operation_and_close(self):
        entered, released = threading.Event(), threading.Event()
        self.gates.append(released)
        self.transport.pause_next_read = (entered, released)
        status, accepted, _ = self.request("POST", "/api/action", {"action": "connect", "username": "offline-operator", "password": "offline-test-password"})
        self.assertEqual(status, 202)
        self.assertTrue(entered.wait(3), "Background operation was not started")
        self.assertTrue(self.request("GET", "/api/state")[1]["busy"])
        self.assertEqual(self.request("POST", "/api/action", {"action": "disconnect"})[0], 409)
        self.assertEqual(self.request("POST", "/api/close", {})[0], 409)
        released.set()
        self.assertTrue(self.wait(accepted["job_id"])["signed_in"])

    def test_acceptance_api_uses_stored_preview_not_client_supplied_plan(self):
        import live_acceptance
        self.connect()
        prepared = self.action("prepare_acceptance")["result"]
        self.assertEqual(prepared["kind"], "acceptance")
        expected = copy.deepcopy(prepared["preview"])
        self.assertEqual(expected["counts"], {"events": 3, "calendars": 2, "links": 4})
        self.assertFalse(expected["event_date_test"]["enabled"])
        observed = []

        def accept(preview, authorization, config, state_dir, callback):
            observed.append(copy.deepcopy(preview))
            self.assertEqual(authorization, self.auth)
            self.assertEqual(preview, expected)
            callback("Offline acceptance dispatch test")
            return {"status": "completed_with_limitations", "acceptance_id": preview["acceptance_id"],
                    "deletion": {"status": "blocked"}}

        with patch.object(live_acceptance, "run_live_acceptance", side_effect=accept) as run:
            result = self.action("run_acceptance", preview_id=prepared["preview_id"],
                                 preview={"plan": {"events": ["unreviewed"]}})["result"]
            self.assertEqual(result["status"], "completed_with_limitations")
            self.assertEqual(run.call_count, 1)
            self.action("run_acceptance", error=True, preview_id="unknown-preview")
            self.assertEqual(run.call_count, 1)
        self.assertEqual(observed, [expected])
        self.assertEqual(self.world.instances, [])


if __name__ == "__main__":
    unittest.main()
