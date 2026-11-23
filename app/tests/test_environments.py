"""Profile configuration and transport boundaries without Oracle requests."""
from copy import deepcopy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from environments import normalize_origin, resolve_config, available_profiles, state_key
from operations import CalendarClient, CalendarService, CALENDARS, OperationError
from event_updates import EventUpdateService, EventUpdateError


class ProfileTests(unittest.TestCase):
    def config(self):
        return {"active_profile": "a", "profiles": {
            "a": {"name": "First tenant", "environment": "https://tenant-a.example"},
            "b": {"name": "Second tenant", "environment": "https://tenant-b.example", "integration": {}}}}

    def test_origin_canonicalization_and_invalid_addresses(self):
        self.assertEqual(normalize_origin("https://TENANT.example:443/"), "https://tenant.example")
        for invalid in ("http://tenant.example", "https://user:password@tenant.example", "https://tenant.example/path",
                        "https://tenant.example?", "https://tenant.example#", "https://tenant.example\\bad",
                        " https://tenant.example", "https://tenant.example\n", "https://tenant.example:99999", "https://a..b"):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                normalize_origin(invalid)

    def test_profiles_resolve_and_integration_ids_do_not_leak(self):
        value = self.config()
        value["profiles"]["a"]["integration"] = {"refresh_flow_id": "12", "key_map_flow_id": "13"}
        first, second = resolve_config(value, "a"), resolve_config(value, "b")
        self.assertEqual(first["environment"], "https://tenant-a.example")
        self.assertEqual(second["environment"], "https://tenant-b.example")
        self.assertEqual(second["integration"], {})
        self.assertEqual([p["id"] for p in available_profiles(value)], ["a", "b"])
        with self.assertRaises(ValueError): resolve_config(value, "missing")

    def test_scope_assertions_never_inherit_across_profiles(self):
        value = self.config()
        value["administrator_asserted_full_calendar_visibility"] = True
        value["profiles"]["a"]["administrator_asserted_full_calendar_visibility"] = True
        self.assertTrue(resolve_config(value, "a")["administrator_asserted_full_calendar_visibility"])
        self.assertFalse(resolve_config(value, "b")["administrator_asserted_full_calendar_visibility"])

    def test_state_isolation_tracks_origin_and_profile_identity(self):
        first = resolve_config(self.config(), "a")
        second = deepcopy(first); second["environment"] = "https://tenant-b.example"
        third = deepcopy(first); third["profile_id"] = "after-refresh"
        self.assertEqual(len({state_key(first), state_key(second), state_key(third)}), 3)
        second["environment"] = "https://TENANT-A.example:443/"
        self.assertEqual(state_key(first), state_key(second))

    def test_malformed_profiles_and_ids_rejected(self):
        with self.assertRaises(ValueError): resolve_config({"profiles": None})
        for change in ({"refresh_flow_id": "bad"}, {"key_map_flow_id": True}, {"reference_event": {}}):
            value = self.config(); value["profiles"]["b"]["integration"] = change
            with self.assertRaises(ValueError): resolve_config(value, "b")
        value = self.config(); value["profiles"]["b"].pop("environment")
        with self.assertRaises(ValueError): available_profiles(value)

    def test_calendar_transport_targets_selected_origin(self):
        observed = []
        class Reply(io.BytesIO):
            status = 200
            headers = {"Content-Type": "application/json"}
        class Opener:
            def open(self, request, timeout):
                observed.append((request.full_url, request.get_header("Authorization")))
                return Reply(b'{"items":[]}')
        with patch("operations.urllib.request.build_opener", return_value=Opener()):
            for key in ("a", "b"):
                client = CalendarClient("Basic local-" + key, resolve_config(self.config(), key))
                client.request("GET", CALENDARS)
        self.assertEqual(observed, [("https://tenant-a.example" + CALENDARS, "Basic local-a"),
                                    ("https://tenant-b.example" + CALENDARS, "Basic local-b")])

    def test_conflicting_environment_aliases_stop_transport(self):
        with self.assertRaises(OperationError):
            CalendarClient("Basic dummy", {"environment":"https://tenant-a.example", "base_url":"https://tenant-b.example"})

    def test_foreign_event_receipt_and_disabled_dates_stop_without_oracle(self):
        with tempfile.TemporaryDirectory() as folder:
            service = EventUpdateService("Basic dummy", {"environment":"https://tenant-b.example"}, folder, client=object())
            path = Path(folder)/"foreign.json"
            path.write_text(json.dumps({"environment":"https://tenant-a.example"}))
            with self.assertRaises(EventUpdateError): service._read_json(path)
            with self.assertRaisesRegex(EventUpdateError, "disabled"):
                service._require_scope_assertions()

    def test_foreign_calendar_receipt_stops_before_reads(self):
        class NoCalls:
            def request(self, *args, **kwargs): raise AssertionError("No Oracle call allowed")
        with tempfile.TemporaryDirectory() as folder:
            service = CalendarService("Basic dummy", {"environment":"https://tenant-b.example"}, folder, client=NoCalls())
            path = Path(folder)/"Calendar_Edit_foreign.json"
            path.write_text(json.dumps({"schema":"K12-CALENDAR-MUTATION-1", "operation":"calendar_edit", "environment":"https://tenant-a.example"}))
            with self.assertRaises(OperationError): service.reconcile_calendar_edit(path)

if __name__ == "__main__": unittest.main()
