"""Browser-URL connection regressions. All Oracle responses are offline mocks."""
import base64
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE))
sys.path.insert(0, str(PACKAGE / "tests"))
import launcher
import operations
from environments import ASSERTIONS, normalize_origin, origin_from_url, url_profile_id
import test_launcher_workflow as workflow

SAMPLE_B = "https://tenant-b.example.invalid"
# Synthetic browser-session values: the actual operator's session is never bundled.
BROWSER_URL = SAMPLE_B + ("/hcmUI/faces/FuseWelcome?fndThemeName=Vision_Default"
    "&_adf.ctrl-state=synthetic-session&_adf.no-new-window-redirect=true"
    "&_afrLoop=123456789&_afrWindowMode=2&_afrWindowId=null&_afrFS=16"
    "&_afrMT=screen&_afrMFW=1920&_afrMFH=945&_afrMFDW=1920&_afrMFDH=1080"
    "&_afrMFC=8&_afrMFCI=0&_afrMFM=0&_afrMFR=96&_afrMFG=0&_afrMFS=0&_afrMFO=0")


class BrowserURLTests(unittest.TestCase):
    def test_browser_url_keeps_only_origin(self):
        self.assertEqual(origin_from_url(BROWSER_URL), SAMPLE_B)
        self.assertEqual(origin_from_url("  HTTPS://TENANT.example:443/a?session=x#anchor  "), "https://tenant.example")
        self.assertEqual(origin_from_url("https://tenant.example:8443/some/path"), "https://tenant.example:8443")

    def test_routing_origin_remains_strict(self):
        with self.assertRaises(ValueError):
            normalize_origin(BROWSER_URL)

    def test_malformed_or_credential_urls_never_echo_session(self):
        for value in ("http://tenant.example", "https://name:secret@tenant.example/path",
                      "https://@tenant.example", "https://tenant.example\\path", "https://tenant.example\n",
                      "https://tenant.example/\tpath", "https://tenant.example:0", "https://tenant.example:99999",
                      "https://tenant.example:", "https://a..b/path", "https://-a.example/path",
                      "https://a_.example", "https://" + "a" * 64 + ".example", "https://tenant example/path",
                      "//tenant.example/path", "https://[::1]/", "https://tenant.example\x7f/path"):
            with self.subTest(value=value), self.assertRaises(ValueError) as caught:
                origin_from_url(value)
            self.assertNotIn("secret", str(caught.exception))

    def test_profile_id_uses_canonical_origin(self):
        self.assertEqual(url_profile_id(SAMPLE_B), url_profile_id(SAMPLE_B.upper() + ":443/"))
        self.assertNotEqual(url_profile_id(SAMPLE_B), url_profile_id(launcher.SAMPLE_A))


class PastedURLHTTPTests(unittest.TestCase):
    tearDown = workflow.LauncherHTTPWorkflowTests.tearDown
    request = workflow.LauncherHTTPWorkflowTests.request
    wait = workflow.LauncherHTTPWorkflowTests.wait
    action = workflow.LauncherHTTPWorkflowTests.action

    def setUp(self):
        workflow.LauncherHTTPWorkflowTests.setUp(self)
        self.observed = []
        def capture(auth, config, folder, progress):
            self.observed.append({"auth": auth, "config": copy.deepcopy(config), "folder": folder})
            return operations.CalendarService(auth, config, folder, progress, client=self.transport)
        self.app.service_factory = capture
        self.root_config = self.root / "config.json"
        self.root_config.write_text(json.dumps(self.app.config_source), encoding="utf-8")
        self.original_config = self.root_config.read_bytes()

    def connect_url(self, value=BROWSER_URL, **options):
        return self.action("connect", environment_url=value, username="sample-b-test-user", password="sample-b-test-password",
                           expected_profile_id=self.app.config["profile_id"], expected_environment=self.app.config["environment"], **options)

    def test_paste_connect_reaches_new_target_with_new_credentials_only(self):
        old_state = self.app.state_dir
        self.app.authorization = self.auth
        self.app.service = object()
        self.app.plans["old"] = {"kind": "create"}
        self.app.downloads["old"] = self.root_config
        state = self.connect_url()
        self.assertEqual(state["environment"], SAMPLE_B)
        self.assertTrue(state["signed_in"])
        self.assertNotEqual(self.app.state_dir, old_state)
        self.assertEqual(self.app.plans, {})
        self.assertEqual(self.app.downloads, {})
        self.assertEqual(len(self.observed), 1)
        call = self.observed[0]
        self.assertEqual(call["config"]["environment"], SAMPLE_B)
        self.assertEqual(call["config"]["integration"], {})
        self.assertFalse(any(call["config"][key] for key in ASSERTIONS))
        self.assertEqual(call["auth"], "Basic " + base64.b64encode(b"sample-b-test-user:sample-b-test-password").decode())
        self.assertNotEqual(call["auth"], self.auth)
        self.assertEqual(self.root_config.read_bytes(), self.original_config)
        serialized = json.dumps(state)
        self.assertNotIn("synthetic-session", serialized)
        self.assertNotIn("sample-b-test-password", serialized)

    def test_saved_profile_restart_contains_no_browser_or_authentication_data(self):
        self.connect_url()
        old_dir, identity = self.app.state_dir, self.app.config["profile_id"]
        saved = self.root / "state" / "saved_environments.json"
        serialized = saved.read_text()
        for forbidden in ("synthetic-session", "FuseWelcome", "_afr", "password", "sample-b-test-user", "authorization", "?"):
            self.assertNotIn(forbidden, serialized)
        profile = json.loads(serialized)["profiles"][identity]
        self.assertEqual(profile["environment"], SAMPLE_B)
        self.assertEqual(profile["integration"], {})
        restarted = launcher.Application(self.root)
        self.assertIsNone(restarted.config)
        self.assertIsNone(restarted.state_dir)
        self.assertEqual(restarted.public_state()["environment"], "")
        restarted.select_environment(identity)
        self.assertEqual(restarted.state_dir, old_dir)
        self.assertEqual(len(restarted.public_state()["profiles"]), 2)
        self.assertFalse(restarted.authorization)

    def test_browser_url_variants_reuse_profile_and_state(self):
        self.connect_url()
        identity, state_dir = self.app.config["profile_id"], self.app.state_dir
        self.connect_url(SAMPLE_B.upper() + ":443/other?session=changed#fragment")
        self.assertEqual(self.app.config["profile_id"], identity)
        self.assertEqual(self.app.state_dir, state_dir)
        self.assertEqual(len(self.app.public_state()["profiles"]), 2)

    def test_connect_and_build_runner_use_pasted_target_and_receipt_origin(self):
        self.connect_url()
        preview = self.action("preview_create", example=True)["result"]
        calls = []
        def run(plan, auth, output, progress):
            calls.append((copy.deepcopy(plan), auth, output))
            output.mkdir(parents=True)
            path = output / "report.json"
            path.write_text(json.dumps({"environment": plan["environment"], "status": "applied_verified"}))
            return {"status": "applied_verified", "report_path": str(path)}
        self.app.create_runner = run
        result = self.action("apply_create", preview_id=preview["preview_id"])["result"]
        self.assertEqual(calls[0][0]["environment"], SAMPLE_B)
        self.assertTrue(calls[0][2].is_relative_to(self.app.state_dir))
        self.assertEqual(result["environment"], SAMPLE_B)
        self.assertNotIn("synthetic-session", json.dumps(result))
        for path in (self.root / "state").rglob("*.json"):
            self.assertNotIn("synthetic-session", path.read_text())

    def test_invalid_url_clears_old_session_and_never_authenticates(self):
        self.app.authorization = self.auth
        self.app.service = object()
        state = self.connect_url("https://user:secret@tenant.example", error=True)
        self.assertFalse(state["signed_in"])
        self.assertEqual(self.observed, [])
        self.assertEqual(self.transport.requests, [])
        self.assertEqual(state["environment"], launcher.SAMPLE_A)

    def test_persistence_failure_is_before_any_oracle_request(self):
        self.app.authorization = self.auth
        with patch("launcher.os.replace", side_effect=PermissionError("no write access")):
            state = self.connect_url(error=True)
        self.assertIn("could not be saved", state["error"])
        self.assertFalse(state["signed_in"])
        self.assertEqual(self.observed, [])
        self.assertEqual(self.transport.requests, [])
        self.assertEqual(state["environment"], launcher.SAMPLE_A)
        self.assertEqual(list((self.root / "state").glob("saved_environments.*.tmp")), [])

    def test_stale_tab_target_guard_runs_before_url_connect(self):
        self.connect_url()
        status, result, _ = self.request("POST", "/api/action", {"action": "connect", "environment_url": launcher.SAMPLE_A,
            "username": "old-user", "password": "old-secret", "expected_profile_id": "sample-a",
            "expected_environment": launcher.SAMPLE_A})
        self.assertEqual(status, 400)
        self.assertIn("environment changed", result["error"])
        self.assertEqual(len(self.observed), 1)
        self.assertEqual(self.app.config["environment"], SAMPLE_B)

    def test_existing_matching_admin_profile_keeps_its_integration(self):
        source = self.app.source_with_profiles(self.app.config_source)
        source["profiles"]["sample-b-approved"] = {"name": "Approved SAMPLE_B", "environment": SAMPLE_B,
                                               "integration": {"refresh_flow_id": "123", "key_map_flow_id": "456"}}
        self.app.config_source = source
        self.connect_url()
        self.assertEqual(self.app.config["profile_id"], "sample-b-approved")
        self.assertEqual(self.app.config["integration"]["key_map_flow_id"], "456")
        self.assertEqual(self.app.saved_profiles, {})

    def test_ambiguous_profiles_require_selection_without_guessing(self):
        source = self.app.source_with_profiles(self.app.config_source)
        for identity in ("sample-b-one", "sample-b-two"):
            source["profiles"][identity] = {"name": identity, "environment": SAMPLE_B}
        self.app.config_source = source
        state = self.connect_url(error=True)
        self.assertIn("Several saved profiles", state["error"])
        self.assertEqual(self.observed, [])
        self.action("select_environment", profile_id="sample-b-two")
        self.connect_url()
        self.assertEqual(self.app.config["profile_id"], "sample-b-two")

    def test_admin_profile_id_conflict_blocks_restart(self):
        self.connect_url()
        identity = self.app.config["profile_id"]
        source = self.app.source_with_profiles(json.loads(self.original_config))
        source["profiles"][identity] = {"name": "Different", "environment": "https://different.example"}
        self.root_config.write_text(json.dumps(source))
        with self.assertRaisesRegex(ValueError, "conflicts"):
            launcher.Application(self.root)

    def test_invalid_saved_profiles_fail_closed_instead_of_falling_back(self):
        self.connect_url()
        path = self.root / "state" / "saved_environments.json"
        saved = json.loads(path.read_text())
        identity = self.app.config["profile_id"]
        cases = []
        item = copy.deepcopy(saved); item["profiles"][identity]["environment"] += "/browser?session=secret"; cases.append(item)
        item = copy.deepcopy(saved); item["profiles"][identity]["integration"] = {"refresh_flow_id": "1"}; cases.append(item)
        item = copy.deepcopy(saved); item["profiles"][identity][ASSERTIONS[0]] = True; cases.append(item)
        item = copy.deepcopy(saved); item["profiles"][identity]["password"] = "secret"; cases.append(item)
        item = copy.deepcopy(saved); item["profiles"]["url-wrong"] = item["profiles"].pop(identity); cases.append(item)
        item = copy.deepcopy(saved); item["active_profile"] = "missing"; cases.append(item)
        for item in cases:
            path.write_text(json.dumps(item))
            with self.subTest(item=item), self.assertRaises(ValueError):
                launcher.Application(self.root)


if __name__ == "__main__":
    unittest.main()
