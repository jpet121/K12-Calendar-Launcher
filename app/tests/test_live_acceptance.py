from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

import live_acceptance as app


AUTH = "Basic dXNlcjpzdXBlci1zZWNyZXQ="


class World:
    def __init__(self):
        self.calendars = [{"CalendarId": "99", "Name": "Preexisting calendar", "Description": "Do not change",
                           "StartDate": "2026-01-01", "EndDate": "2027-12-31", "events": [], "subscribers": []}]
        self.journal = None
        self.creates = 0
        self.patches = 0
        self.event_writes = 0
        self.create_calls = 0
        self.create_pending = False
        self.lose_patch_response = False
        self.mutate_on_restore = False
        self.event_receipts = {}
        self.outside_shared_use = False
        self.rerun_writes = 0
        self.fail_prewrite_once = False
        self.environment = app.DEFAULT_BASE
        self.service_calls = 0
        self.receipt_environment = None
        self.received_plans = []

    def create(self, plan, authorization, output_dir, progress_callback, config=None):
        self.create_calls += 1
        self.received_plans.append(deepcopy(plan))
        environment = self.receipt_environment or (config.get("environment", app.DEFAULT_BASE) if config else app.DEFAULT_BASE)
        if self.create_pending:
            self.create_pending = False
            return {"environment": environment, "status": "pending", "write_requests": 0, "error": "Pending job"}
        if self.journal:
            return {"environment": environment, "status": "applied_verified", "write_requests": self.rerun_writes,
                    "journal": deepcopy(self.journal), "result_counts": {"events": 3, "calendars": 2, "links": 4}}
        self.journal = {"events": {}, "calendars": {}, "links": {}}
        events = {}
        for index, event in enumerate(plan["events"]):
            eid = str(1000 + index)
            self.journal["events"][event["ShortCode"]] = {"CalendarEventId": eid}
            events[event["ShortCode"]] = {"CalendarEventId": eid, "Name": event["Name"], "ShortCode": event["ShortCode"],
                "StartDateTime": event["StartDateTimeLocal"] + "+00:00", "EndDateTime": event["EndDateTimeLocal"] + "+00:00",
                "AllDayFlag": False, "HalfDayForElapsed": "N", "CategoryCode": "PH", "EventType": "EVENT"}
        for index, calendar in enumerate(plan["calendars"]):
            cid = str(2000 + index)
            row = dict(calendar, CalendarId=cid, events=[], subscribers=[])
            self.journal["calendars"][calendar["key"]] = {"CalendarId": cid}
            for link_index, link in enumerate(plan["associations"]):
                if link["calendar_key"] == calendar["key"]:
                    row["events"].append(dict(events[link["event_code"]], CalendarId=cid, CalendarCalEventId=str(3000 + link_index)))
            self.calendars.append(row)
        self.creates += 1
        return {"environment": environment, "status": "applied_verified", "journal": deepcopy(self.journal), "write_requests": 9,
                "result_counts": {"events": 3, "calendars": 2, "links": 4}}

    def calendar_factory(self, auth, config, state_dir, callback):
        self.service_calls += 1
        return FakeCalendarService(self, state_dir)

    def event_factory(self, auth, config, state_dir, callback):
        return FakeEventService(self)


class FakeCalendarService:
    def __init__(self, world, folder):
        self.world = world
        self.folder = Path(folder)

    def read(self):
        return {"complete": True, "environment": self.world.environment, "calendars": deepcopy(self.world.calendars)}

    def plan_calendar_edit(self, cid, changes):
        row = next(c for c in self.world.calendars if c["CalendarId"] == cid)
        if self.world.mutate_on_restore and "restore" in self.folder.name:
            row["Description"] = "External edit"
        return {"before": deepcopy(row), "after": dict(row, **changes), "changes": changes, "calendar_id": cid}

    def apply_calendar_edit(self, preview):
        self.folder.mkdir(parents=True, exist_ok=True)
        if self.world.fail_prewrite_once:
            self.world.fail_prewrite_once = False
            raise FileNotFoundError("Receipt path is unavailable")
        path = self.folder / "Calendar_Edit_1.json"
        receipt = {"status": "outcome_unknown", "calendar_id": preview["calendar_id"], "before": preview["before"],
                   "expected": preview["after"], "receipt_path": str(path)}
        path.write_text(json.dumps(receipt))
        row = next(c for c in self.world.calendars if c["CalendarId"] == preview["calendar_id"])
        row.update(preview["changes"])
        self.world.patches += 1
        if self.world.lose_patch_response:
            self.world.lose_patch_response = False
            raise ValueError("Lost response: super-secret")
        receipt["status"] = "updated_verified"
        path.write_text(json.dumps(receipt))
        return receipt

    def reconcile_calendar_edit(self, path):
        receipt = json.loads(Path(path).read_text())
        row = next(c for c in self.world.calendars if c["CalendarId"] == receipt["calendar_id"])
        receipt["status"] = "reconciled_applied" if row == receipt["expected"] else "reconciled_conflict"
        return receipt


class FakeEventService:
    def __init__(self, world):
        self.world = world

    def plan_event_edit(self, eid, start, end):
        uses = [calendar for calendar in self.world.calendars if any(e["CalendarEventId"] == eid for e in calendar["events"])]
        event = next(e for e in uses[0]["events"] if e["CalendarEventId"] == eid)
        affected = [{"CalendarId": c["CalendarId"]} for c in uses]
        if self.world.outside_shared_use:
            affected.append({"CalendarId": "99"})
        return {"preview_id": uuid.uuid4().hex, "CalendarEventId": eid, "old": deepcopy(event),
                "new": dict(event, StartDateTime=start, EndDateTime=end), "affected_calendars": affected}

    def apply_event_edit(self, preview):
        for calendar in self.world.calendars:
            for event in calendar["events"]:
                if event["CalendarEventId"] == preview["CalendarEventId"]:
                    event["StartDateTime"] = preview["new"]["StartDateTime"]
                    event["EndDateTime"] = preview["new"]["EndDateTime"]
        self.world.event_writes += 1
        result = {"state": "verified", "receipt_id": preview["preview_id"], "preview": preview}
        self.world.event_receipts[result["receipt_id"]] = result
        return result

    def prepare_restore(self, receipt_id):
        previous = self.world.event_receipts[receipt_id]["preview"]
        return self.plan_event_edit(previous["CalendarEventId"], previous["old"]["StartDateTime"], previous["old"]["EndDateTime"])

    def reconcile_event_edit(self, receipt_id):
        return self.world.event_receipts[receipt_id]


class LiveAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = {"environment": app.DEFAULT_BASE}
        self.world = World()
        for name, replacement in (("run_create", self.world.create), ("CalendarService", self.world.calendar_factory), ("EventUpdateService", self.world.event_factory)):
            patcher = patch.object(app, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def prepare(self):
        return app.prepare_live_acceptance(self.config, self.tmp.name)

    def run_preview(self, preview):
        return app.run_live_acceptance(preview, AUTH, self.config, self.tmp.name)

    def test_prepare_reserves_concrete_unique_names_without_oracle(self):
        first, second = self.prepare(), self.prepare()
        self.assertEqual(first["counts"], {"events": 3, "calendars": 2, "links": 4})
        self.assertNotEqual(first["calendar_names"], second["calendar_names"])
        self.assertEqual(self.world.create_calls, 0)
        self.assertFalse(first["event_date_test"]["enabled"])
        self.assertFalse(first["deletion"]["attempted"])

    def test_complete_workflow_restores_description_and_zero_write_rerun(self):
        preview = self.prepare()
        report = self.run_preview(preview)
        self.assertEqual(report["status"], "completed_with_limitations", report)
        self.assertEqual(self.world.creates, 1)
        self.assertEqual(self.world.patches, 2)
        self.assertEqual(self.world.event_writes, 0)
        self.assertEqual(report["steps"]["event_date_edit"]["status"], "skipped")
        self.assertEqual(report["steps"]["unchanged_rerun"]["write_requests"], 0)
        self.assertEqual(report["deletion"]["status"], "blocked")
        self.assertEqual(self.world.calendars[0]["Description"], "Do not change")
        self.assertEqual(self.world.calendars[1]["Description"], preview["calendar_description_test"]["original"])
        second = self.run_preview(preview)
        self.assertEqual(second["status"], "completed_with_limitations")
        self.assertEqual((self.world.creates, self.world.patches), (1, 2))

    def test_alternate_environment_reaches_all_acceptance_stages(self):
        self.config.update(environment="https://alternate-test.example.com", profile_id="alternate-test")
        self.world.environment = self.config["environment"]
        preview = self.prepare()
        self.assertEqual(preview["environment"], self.config["environment"])
        self.assertEqual(preview["plan"]["environment"], self.config["environment"])
        self.assertEqual(preview["profile_id"], "alternate-test")
        report = self.run_preview(preview)
        self.assertEqual(report["status"], "completed_with_limitations", report)
        self.assertEqual(report["environment"], self.config["environment"])
        self.assertEqual(report["profile_id"], "alternate-test")
        self.assertEqual(self.world.patches, 2)
        self.assertEqual(len(self.world.received_plans), 2)
        self.assertTrue(all(plan["environment"] == self.config["environment"] for plan in self.world.received_plans))
        second = self.run_preview(preview)
        self.assertEqual(second["status"], "completed_with_limitations", second)
        self.assertEqual((self.world.creates, self.world.patches), (1, 2))

    def test_cross_environment_preview_stops_before_service_or_write(self):
        preview = self.prepare()
        self.config["environment"] = "https://alternate-test.example.com"
        with self.assertRaisesRegex(app.AcceptanceError, "another environment"):
            self.run_preview(preview)
        self.assertEqual((self.world.service_calls, self.world.create_calls, self.world.patches), (0, 0, 0))

    def test_cross_profile_preview_same_origin_stops_before_service_or_write(self):
        self.config["profile_id"] = "first"
        preview = self.prepare()
        self.config["profile_id"] = "second"
        with self.assertRaisesRegex(app.AcceptanceError, "another environment"):
            self.run_preview(preview)
        self.assertEqual((self.world.service_calls, self.world.create_calls, self.world.patches), (0, 0, 0))

    def test_saved_report_from_another_origin_stops_before_service_or_write(self):
        preview = self.prepare()
        folder = app._folder(self.tmp.name, preview["acceptance_id"])
        (folder / "acceptance_report.json").write_text(json.dumps({"environment": "https://alternate-test.example.com"}))
        with self.assertRaisesRegex(app.AcceptanceError, "another environment"):
            self.run_preview(preview)
        self.assertEqual((self.world.service_calls, self.world.create_calls, self.world.patches), (0, 0, 0))

    def test_nested_cross_environment_creation_receipt_stops_before_service_or_write(self):
        preview = self.prepare()
        folder = app._folder(self.tmp.name, preview["acceptance_id"])
        (folder / "acceptance_report.json").write_text(json.dumps({
            "environment": app.DEFAULT_BASE,
            "creation": {"environment": "https://alternate-test.example.com"}}))
        with self.assertRaisesRegex(app.AcceptanceError, "another environment"):
            self.run_preview(preview)
        self.assertEqual((self.world.service_calls, self.world.create_calls, self.world.patches), (0, 0, 0))

    def test_saved_report_from_another_profile_stops_before_service_or_write(self):
        self.config["profile_id"] = "first"
        preview = self.prepare()
        folder = app._folder(self.tmp.name, preview["acceptance_id"])
        (folder / "acceptance_report.json").write_text(json.dumps({
            "environment": app.DEFAULT_BASE, "profile_id": "second"}))
        with self.assertRaisesRegex(app.AcceptanceError, "another environment profile"):
            self.run_preview(preview)
        self.assertEqual((self.world.service_calls, self.world.create_calls, self.world.patches), (0, 0, 0))

    def test_nested_cross_profile_receipt_stops_before_service_or_write(self):
        self.config["profile_id"] = "first"
        preview = self.prepare()
        folder = app._folder(self.tmp.name, preview["acceptance_id"])
        (folder / "acceptance_report.json").write_text(json.dumps({
            "environment": app.DEFAULT_BASE, "profile_id": "first",
            "creation": {"environment": app.DEFAULT_BASE, "profile_id": "second"}}))
        with self.assertRaisesRegex(app.AcceptanceError, "another environment"):
            self.run_preview(preview)
        self.assertEqual((self.world.service_calls, self.world.create_calls, self.world.patches), (0, 0, 0))

    def test_cross_environment_inventory_stops_before_create(self):
        preview = self.prepare()
        self.world.environment = "https://alternate-test.example.com"
        report = self.run_preview(preview)
        self.assertEqual(report["status"], "stopped")
        self.assertEqual(report["current_stage"], "baseline_read")
        self.assertEqual((self.world.create_calls, self.world.patches), (0, 0))

    def test_cross_environment_returned_creation_receipt_stops_before_edit(self):
        preview = self.prepare()
        self.world.receipt_environment = "https://alternate-test.example.com"
        report = self.run_preview(preview)
        self.assertEqual(report["status"], "stopped")
        self.assertEqual(report["current_stage"], "create")
        self.assertIn("another environment", report["detail"])
        self.assertEqual(self.world.patches, 0)

    def test_admin_enabled_event_test_applies_and_restores_owned_master(self):
        self.config.update(administrator_asserted_full_calendar_visibility=True, administrator_asserted_no_legacy_event_coverage=True)
        preview = self.prepare()
        report = self.run_preview(preview)
        self.assertEqual(report["status"], "completed_with_limitations", report)
        self.assertEqual(self.world.event_writes, 2)
        self.assertEqual(report["steps"]["event_date_restore"]["status"], "passed")

    def test_tampered_preview_blocks_before_oracle(self):
        preview = self.prepare()
        preview["plan"]["calendars"][0]["Name"] = "Existing production calendar"
        with self.assertRaisesRegex(app.AcceptanceError, "preview changed"):
            self.run_preview(preview)
        self.assertEqual(self.world.create_calls, 0)

    def test_pending_creation_resumes_same_reserved_records(self):
        preview = self.prepare()
        self.world.create_pending = True
        first = self.run_preview(preview)
        self.assertEqual(first["status"], "stopped")
        self.assertEqual(first["current_stage"], "create")
        second = self.run_preview(preview)
        self.assertEqual(second["status"], "completed_with_limitations", second)
        self.assertEqual(self.world.creates, 1)

    def test_lost_patch_response_reconciles_without_duplicate_patch(self):
        preview = self.prepare()
        self.world.lose_patch_response = True
        first = self.run_preview(preview)
        self.assertEqual(first["status"], "stopped")
        self.assertTrue(first["possible_temporary_changes"])
        self.assertNotIn("super-secret", Path(first["report_path"]).read_text())
        self.assertEqual(self.world.patches, 1)
        second = self.run_preview(preview)
        self.assertEqual(second["status"], "completed_with_limitations", second)
        self.assertEqual(self.world.patches, 2)
        self.assertNotIn("detail", second)

    def test_prewrite_receipt_failure_rechecks_original_then_resumes_once(self):
        preview = self.prepare()
        self.world.fail_prewrite_once = True
        first = self.run_preview(preview)
        self.assertEqual(first["status"], "stopped")
        self.assertEqual(first["current_stage"], "calendar_description_edit")
        self.assertEqual(self.world.patches, 0)
        second = self.run_preview(preview)
        self.assertEqual(second["status"], "completed_with_limitations", second)
        self.assertEqual(self.world.patches, 2)
        recovery = second["steps"]["calendar_description_edit"]["recovery"]
        self.assertEqual(recovery["status"], "pre_dispatch_journal_failure_reconciled")
        self.assertEqual(recovery["receipt_count"], 0)

    def test_missing_receipt_without_proven_prewrite_failure_remains_blocked(self):
        preview = self.prepare()
        folder = app._folder(self.tmp.name, preview["acceptance_id"])
        report = {"schema": "K12-LIVE-ACCEPTANCE-REPORT-1", "environment": app.DEFAULT_BASE,
                  "acceptance_id": preview["acceptance_id"], "status": "stopped",
                  "calendar_names": preview["calendar_names"], "event_codes": preview["event_codes"],
                  "steps": {"baseline_read": {"status": "passed", "fingerprints": {}, "visible_calendars": 1},
                            "create": {"status": "passed", "counts": {"events": 3, "calendars": 2, "links": 4}},
                            "created_readback": {"status": "passed", "counts": preview["counts"]},
                            "calendar_description_edit": {"status": "write_started"}},
                  "creation": self.world.create(preview["plan"], AUTH, folder / "creation", lambda _: None),
                  "owned_calendar_ids": {preview["plan"]["calendars"][0]["key"]: "2000",
                                           preview["plan"]["calendars"][1]["key"]: "2001"},
                  "record_retention": preview["retention"], "deletion": preview["deletion"],
                  "error": "Stopped during calendar_description_edit (ValueError)."}
        (folder / "acceptance_report.json").write_text(json.dumps(report), encoding="utf-8")
        result = self.run_preview(preview)
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["current_stage"], "calendar_description_edit")
        self.assertEqual(self.world.patches, 0)

    def test_restore_does_not_overwrite_an_external_description_change(self):
        preview = self.prepare()
        self.world.mutate_on_restore = True
        report = self.run_preview(preview)
        self.assertEqual(report["status"], "stopped")
        self.assertEqual(report["current_stage"], "calendar_description_restore")
        self.assertEqual(self.world.patches, 1)
        self.assertEqual(self.world.calendars[1]["Description"], "External edit")

    def test_event_test_never_updates_master_shared_outside_test_calendars(self):
        self.config.update(administrator_asserted_full_calendar_visibility=True, administrator_asserted_no_legacy_event_coverage=True)
        preview = self.prepare()
        self.world.outside_shared_use = True
        report = self.run_preview(preview)
        self.assertEqual(report["status"], "stopped")
        self.assertEqual(report["current_stage"], "event_date_edit")
        self.assertEqual(self.world.event_writes, 0)

    def test_nonzero_unchanged_writes_fail_acceptance(self):
        preview = self.prepare()
        self.world.rerun_writes = 1
        report = self.run_preview(preview)
        self.assertEqual(report["status"], "stopped")
        self.assertEqual(report["current_stage"], "unchanged_rerun")

    def test_no_expanded_event_permission_after_approval(self):
        preview = self.prepare()
        self.config.update(administrator_asserted_full_calendar_visibility=True, administrator_asserted_no_legacy_event_coverage=True)
        report = self.run_preview(preview)
        self.assertEqual(report["status"], "completed_with_limitations")
        self.assertEqual(self.world.event_writes, 0)


if __name__ == "__main__":
    unittest.main()
