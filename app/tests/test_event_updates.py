import base64
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from event_updates import EventUpdateError, EventUpdatePending, EventUpdateService, _event_lock
from engine import create_pilot as engine


OLD_START = "2027-01-18T00:00:00+00:00"
OLD_END = "2027-01-19T00:00:00+00:00"
NEW_START = "2027-01-19T00:00:00+00:00"
NEW_END = "2027-01-20T00:00:00+00:00"


def snapshot():
    event = {"CalendarEventId": "100", "ShortCode": "REVIEWED_EVENT", "Name": "Shared event",
             "StartDateTime": OLD_START, "EndDateTime": OLD_END,
             "AllDayFlag": False, "HalfDayForElapsed": "N", "CategoryCode": "PH", "EventType": "EVENT"}
    calendars = []
    for cid, linkid in (("200", "201"), ("300", "301")):
        row = dict(event, CalendarId=cid, CalendarCalEventId=linkid)
        calendars.append({"CalendarId": cid, "Name": "Calendar " + cid, "StartDate": "2026-08-01",
                          "EndDate": "2027-06-01", "Description": "", "events": [row], "subscribers": []})
    return {"environment": engine.DEFAULT_BASE, "complete": True, "calendars": calendars}


class FakeCalendars:
    def __init__(self):
        self.value = snapshot()
        self.reads = 0

    def read(self):
        self.reads += 1
        return deepcopy(self.value)


class FakeHDL:
    def __init__(self, calendar_service, state_dir):
        self.calendar_service = calendar_service
        self.state_dir = Path(state_dir)
        self.posts = []
        self.gets = []
        self.fail_upload = False
        self.fail_submit = False
        self.pending = False
        self.has_more = False
        self.load_failed = False
        self.apply_readback = True
        self.content = None
        self.submitted = False

    def post(self, resource, payload, content_type):
        receipts = list((self.state_dir / "event_edits" / "receipts").glob("*.json"))
        state = json.loads(max(receipts, key=lambda path: path.stat().st_mtime_ns).read_text())["state"]
        self.posts.append((resource, deepcopy(payload), content_type, state))
        if resource.endswith("/uploadFile"):
            if self.fail_upload:
                raise OSError("simulated lost upload response")
            with zipfile.ZipFile(io.BytesIO(base64.b64decode(payload["content"]))) as archive:
                self.content = archive.read("CalendarEvent.dat").decode()
            return {"result": {"ContentId": "UCM_123"}}
        if resource.endswith("/createFileDataSet"):
            self.submitted = True
            if self.fail_submit:
                raise OSError("simulated lost submit response")
            return {"result": {"RequestId": "111"}}
        raise AssertionError(resource)

    def get(self, resource, params):
        self.gets.append((resource, deepcopy(params)))
        if resource == engine.DATASETS:
            assert params["q"] == "ContentId='UCM_123'"
            if self.apply_readback and not self.pending and not self.load_failed:
                _, _, eid, start, end = self.content.splitlines()[1].split("|")
                for calendar in self.calendar_service.value["calendars"]:
                    for event in calendar["events"]:
                        if event["CalendarEventId"] == eid:
                            event["StartDateTime"] = start.replace("/", "-").replace(" ", "T") + "+00:00"
                            event["EndDateTime"] = end.replace("/", "-").replace(" ", "T") + "+00:00"
            return {"items": [{"ContentId": "UCM_123", "RequestId": "222",
                               "DataSetStatusCode": "ORA_IN_PROGRESS" if self.pending else "ORA_IN_ERROR" if self.load_failed else "ORA_SUCCESS",
                               "ImportStatusCode": "SUCCESS", "LoadStatusCode": "ERROR" if self.load_failed else "SUCCESS",
                               "ObjectLoadErrorCount": 1 if self.load_failed else 0}], "hasMore": self.has_more}
        if resource.endswith("/222/child/businessObjects"):
            return {"items": [{"BusinessObjectName": "Calendar Event", "LoadStatusCode": "SUCCESS", "ObjectLoadErrorCount": 0}], "hasMore": False}
        raise AssertionError(resource)


class EventUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.calendars = FakeCalendars()
        self.client = FakeHDL(self.calendars, self.tmp.name)
        self.service = EventUpdateService("Basic SHOULD_NOT_BE_SAVED", {"environment": engine.DEFAULT_BASE, "poll_timeout_seconds": 0,
                                          "administrator_asserted_full_calendar_visibility": True,
                                          "administrator_asserted_no_legacy_event_coverage": True},
                                          self.tmp.name, client=self.client, calendar_service=self.calendars)

    def plan(self):
        return self.service.plan_event_edit("100", NEW_START, NEW_END)

    def receipt(self, plan):
        return json.loads(self.service._receipt_path(plan["preview_id"]).read_text())

    def test_shared_edit_uses_sparse_id_merge_and_verifies_every_calendar(self):
        plan = self.plan()
        self.assertEqual(plan["calendar_count"], 2)
        self.assertTrue(plan["shared_impact"])
        self.assertEqual(plan["old"]["StartDateTime"], OLD_START)
        self.assertEqual([row["CalendarId"] for row in plan["affected_calendars"]], ["200", "300"])
        receipt = self.service.apply_event_edit(plan)
        self.assertEqual(receipt["state"], "verified")
        self.assertEqual(receipt["RequestId"], "111")
        self.assertEqual(receipt["status_RequestId"], "222")
        self.assertEqual([post[3] for post in self.client.posts], ["upload_pending", "submission_pending"])
        self.assertTrue(all(post[2] == engine.HDL_ACTION_CONTENT_TYPE for post in self.client.posts))
        self.assertEqual(self.client.content, "METADATA|CalendarEvent|CalendarEventId|StartDateTime|EndDateTime\nMERGE|CalendarEvent|100|2027/01/19 00:00:00|2027/01/20 00:00:00\n")
        self.assertNotIn("SHOULD_NOT_BE_SAVED", self.service._receipt_path(plan["preview_id"]).read_text())
        self.assertTrue(all(c["events"][0]["StartDateTime"] == NEW_START for c in self.calendars.value["calendars"]))
        self.assertGreaterEqual(self.calendars.reads, 5)

    def test_global_event_edit_is_disabled_without_exact_admin_assertions(self):
        for key in ("administrator_asserted_full_calendar_visibility", "administrator_asserted_no_legacy_event_coverage"):
            for value in (None, False, "true", 1):
                with self.subTest(key=key, value=value):
                    self.service.config[key] = value
                    with self.assertRaisesRegex(EventUpdateError, "disabled until an administrator"):
                        self.plan()
                self.service.config[key] = True
        self.assertEqual(self.calendars.reads, 0)
        self.assertEqual(self.client.posts, [])

    def test_admin_scope_change_blocks_apply_but_not_readonly_reconcile(self):
        plan = self.plan()
        self.client.pending = True
        with self.assertRaises(EventUpdatePending):
            self.service.apply_event_edit(plan)
        self.service.config["administrator_asserted_full_calendar_visibility"] = False
        with self.assertRaisesRegex(EventUpdateError, "disabled until an administrator"):
            self.service.apply_event_edit(plan)
        self.client.pending = False
        self.assertEqual(self.service.reconcile_event_edit(plan["preview_id"])["state"], "verified")
        self.assertEqual(len(self.client.posts), 2)

    def test_raw_network_error_is_never_written_to_receipt(self):
        plan = self.plan()
        def fail(*args):
            raise OSError("Basic SHOULD_NOT_BE_SAVED password=very-secret")
        self.client.post = fail
        with self.assertRaises(OSError):
            self.service.apply_event_edit(plan)
        text = self.service._receipt_path(plan["preview_id"]).read_text()
        self.assertNotIn("SHOULD_NOT_BE_SAVED", text)
        self.assertNotIn("very-secret", text)

    def test_unchanged_rerun_performs_zero_additional_posts(self):
        plan = self.plan()
        self.service.apply_event_edit(plan)
        self.service.apply_event_edit(plan)
        self.assertEqual(len(self.client.posts), 2)

    def test_no_change_plan_performs_no_posts(self):
        plan = self.service.plan_event_edit("100", OLD_START, OLD_END)
        self.assertEqual(self.service.apply_event_edit(plan)["status"], "no_change")
        self.assertEqual(self.client.posts, [])

    def test_inconsistent_shared_values_stop_before_write(self):
        self.calendars.value["calendars"][1]["events"][0]["Name"] = "Different"
        with self.assertRaisesRegex(EventUpdateError, "Associations disagree"):
            self.plan()
        self.assertEqual(self.client.posts, [])

    def test_incomplete_scan_or_unsupported_flags_stop_before_write(self):
        self.calendars.value["complete"] = False
        with self.assertRaisesRegex(EventUpdateError, "incomplete"):
            self.plan()
        self.calendars.value["complete"] = True
        self.calendars.value["calendars"][0]["events"][0]["AllDayFlag"] = True
        with self.assertRaisesRegex(EventUpdateError, "All day"):
            self.plan()
        self.assertEqual(self.client.posts, [])

    def test_strict_date_input_and_calendar_bounds(self):
        for start in ("2027-01-19", "2027-01-19T00:00:00Z", "2027-01-19T12:00:00+00:00", "2027-01-19T00:00:00-06:00", "2027-02-30T00:00:00+00:00"):
            with self.subTest(start=start), self.assertRaises(EventUpdateError):
                self.service.plan_event_edit("100", start, NEW_END)
        with self.assertRaisesRegex(EventUpdateError, "outside"):
            self.service.plan_event_edit("100", "2027-06-02T00:00:00+00:00", "2027-06-03T00:00:00+00:00")
        with self.assertRaisesRegex(EventUpdateError, "later"):
            self.service.plan_event_edit("100", NEW_END, NEW_START)

    def test_changed_or_tampered_preview_never_uploads(self):
        plan = self.plan()
        altered = deepcopy(plan)
        altered["new"]["StartDateTime"] = OLD_START
        with self.assertRaisesRegex(EventUpdateError, "preview changed"):
            self.service.apply_event_edit(altered)
        self.calendars.value["calendars"][1]["Name"] = "Renamed calendar"
        with self.assertRaisesRegex(EventUpdateError, "changed after preview"):
            self.service.apply_event_edit(plan)
        self.assertEqual(self.client.posts, [])

    def test_upload_response_lost_blocks_retries_and_new_plan(self):
        plan = self.plan()
        self.client.fail_upload = True
        with self.assertRaises(OSError):
            self.service.apply_event_edit(plan)
        self.assertEqual(self.receipt(plan)["state"], "upload_pending")
        self.client.fail_upload = False
        with self.assertRaises(EventUpdatePending):
            self.service.apply_event_edit(plan)
        another = self.plan()
        with self.assertRaisesRegex(EventUpdatePending, "earlier correction"):
            self.service.apply_event_edit(another)
        self.assertEqual(len(self.client.posts), 1)

    def test_lost_submit_response_reconciles_by_content_id_with_zero_posts(self):
        plan = self.plan()
        self.client.fail_submit = True
        with self.assertRaises(OSError):
            self.service.apply_event_edit(plan)
        self.assertEqual(self.receipt(plan)["state"], "submission_pending")
        receipt = self.service.reconcile_event_edit(plan["preview_id"])
        self.assertEqual(receipt["state"], "verified")
        self.assertNotIn("RequestId", receipt)
        self.assertEqual(receipt["status_RequestId"], "222")
        self.assertEqual(len(self.client.posts), 2)

    def test_pending_load_resumes_without_new_write(self):
        plan = self.plan()
        self.client.pending = True
        with self.assertRaisesRegex(EventUpdatePending, "still pending"):
            self.service.apply_event_edit(plan)
        self.client.pending = False
        self.assertEqual(self.service.apply_event_edit(plan)["state"], "verified")
        self.assertEqual(len(self.client.posts), 2)

    def test_incomplete_status_lookup_does_not_claim_success(self):
        plan = self.plan()
        self.client.has_more = True
        with self.assertRaisesRegex(EventUpdatePending, "incomplete"):
            self.service.apply_event_edit(plan)
        self.assertEqual(self.receipt(plan)["state"], "submitted")

    def test_failed_load_blocks_automatic_resubmission(self):
        plan = self.plan()
        self.client.load_failed = True
        with self.assertRaisesRegex(EventUpdateError, "did not report"):
            self.service.apply_event_edit(plan)
        with self.assertRaisesRegex(EventUpdateError, "failed"):
            self.service.apply_event_edit(plan)
        self.assertEqual(len(self.client.posts), 2)

    def test_successful_loader_with_wrong_readback_is_not_verified(self):
        plan = self.plan()
        self.client.apply_readback = False
        with self.assertRaisesRegex(EventUpdateError, "readback differs"):
            self.service.apply_event_edit(plan)
        self.assertEqual(self.receipt(plan)["state"], "readback_mismatch")
        self.assertEqual(len(self.client.posts), 2)

    def test_restore_is_a_new_reviewed_inverse_preview_not_a_write(self):
        plan = self.plan()
        receipt = self.service.apply_event_edit(plan)
        restore = self.service.prepare_restore(receipt["receipt_id"])
        self.assertEqual(restore["operation"], "restore_event_dates")
        self.assertEqual(restore["old"]["StartDateTime"], NEW_START)
        self.assertEqual(restore["new"]["StartDateTime"], OLD_START)
        self.assertEqual(len(self.client.posts), 2)
        restored = self.service.apply_event_edit(restore)
        self.assertEqual(restored["state"], "verified")
        self.assertTrue(all(c["events"][0]["StartDateTime"] == OLD_START for c in self.calendars.value["calendars"]))

    def test_restore_does_not_overwrite_later_external_edit(self):
        plan = self.plan()
        receipt = self.service.apply_event_edit(plan)
        self.calendars.value["calendars"][0]["events"][0]["Name"] = "Later change"
        with self.assertRaises(EventUpdateError):
            self.service.prepare_restore(receipt["receipt_id"])
        self.assertEqual(len(self.client.posts), 2)

    def test_restore_rechecks_changes_between_its_two_reads(self):
        plan = self.plan()
        receipt = self.service.apply_event_edit(plan)
        read = self.calendars.read
        calls = 0
        def racing_read():
            nonlocal calls
            calls += 1
            if calls == 2:
                for calendar in self.calendars.value["calendars"]:
                    calendar["events"][0]["Name"] = "Concurrent rename"
            return read()
        self.calendars.read = racing_read
        with self.assertRaisesRegex(EventUpdateError, "changed while preparing"):
            self.service.prepare_restore(receipt["receipt_id"])
        self.assertEqual(len(self.client.posts), 2)

    def test_event_lock_prevents_concurrent_submit(self):
        plan = self.plan()
        with _event_lock(self.service.folder / "locks" / "100.lock"):
            with self.assertRaisesRegex(EventUpdateError, "Another correction"):
                self.service.apply_event_edit(plan)
        self.assertEqual(self.client.posts, [])


if __name__ == "__main__":
    unittest.main()
