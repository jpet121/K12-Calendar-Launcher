"""Offline integration tests for the maintenance service and HTTP contract."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import operations as app


def calendar(identifier="10", name="Calendar A"):
    return {"CalendarId": identifier, "Name": name, "Description": "Example", "StartDate": "2027-01-01",
            "EndDate": "2027-12-31", "LastUpdateDate": "2026-09-30T12:00:00Z"}


def event(identifier="101", master="301", parent="10"):
    return {"CalendarCalEventId": identifier, "CalendarEventId": master, "CalendarId": parent,
            "Name": "Holiday", "ShortCode": "HOL27", "StartDateTime": "2027-01-18T00:00:00+00:00",
            "EndDateTime": "2027-01-19T00:00:00+00:00", "AllDayFlag": False, "HalfDayForElapsed": "N"}


class FakeClient:
    def __init__(self):
        self.calendars = {"10": calendar()}
        self.events = {"10": [event()]}
        self.subscribers = {"10": []}
        self.versions = {"10": "\"1\""}
        self.requests = []
        self.page_size = 1
        self.after_commit_error = False
        self.reject_patch = None
        self.patch_override = None
        self.etags = True
        self.context_etag = False

    def request(self, method, resource, *, params=None, payload=None, headers=None):
        self.requests.append({"method": method, "resource": resource, "params": copy.deepcopy(params),
                              "payload": copy.deepcopy(payload), "headers": copy.deepcopy(headers)})
        suffix = resource[len(app.CALENDARS):].strip("/")
        cid = suffix.split("/")[0] if suffix else None
        if method == "PATCH":
            if self.reject_patch:
                raise app.OperationError("Rejected", code="http_error", http_status=self.reject_patch)
            if headers.get("If-Match") != self.versions[cid]:
                raise app.OperationError("Changed", code="conflict", http_status=412)
            self.calendars[cid].update(self.patch_override or payload)
            self.versions[cid] = "\"2\""
            if self.after_commit_error:
                raise app.OperationError("Connection interrupted", code="transport_error")
            return app.Response(200, copy.deepcopy(self.calendars[cid]), {"ETag": self.versions[cid]})
        if suffix and "/" not in suffix:
            value = copy.deepcopy(self.calendars[cid])
            if self.context_etag:
                value["@context"] = {"headers": {"ETag": self.versions[cid]}}
            return app.Response(200, value, {"ETag": self.versions[cid]} if self.etags else {})
        if not suffix:
            rows = list(self.calendars.values())
        elif suffix.endswith("AbsenceCalendarEvent"):
            rows = self.events[cid]
        else:
            rows = self.subscribers[cid]
        offset = params.get("offset", 0)
        batch = copy.deepcopy(rows[offset:offset + self.page_size])
        return app.Response(200, {"items": batch, "count": len(batch), "offset": offset,
                                  "hasMore": offset + len(batch) < len(rows)}, {})


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.client = FakeClient()
        self.service = app.CalendarService("Basic fake", {"environment": app.DEFAULT_BASE}, self.temp.name, client=self.client)

    def writes(self):
        return [call for call in self.client.requests if call["method"] != "GET"]

    def test_complete_read_paginates_calendars_and_children_preserving_identity(self):
        self.client.calendars["20"] = calendar("20", "Calendar B")
        self.client.events["10"].append(event("102", "302"))
        self.client.events["20"] = [event("201", "301", "20")]
        self.client.subscribers["20"] = [{"CalendarSubscriberId": "41", "CalendarId": "20", "ListId": "61"}]
        result = self.service.read()
        self.assertTrue(result["complete"])
        self.assertEqual(result["assignment_coverage"], "unknown")
        self.assertEqual(result["counts"], {"calendars": 2, "events": 2, "links": 3, "rule_subscriptions": 1})
        self.assertEqual(result["calendars"][1]["subscribers"][0]["ListId"], "61")
        self.assertTrue((Path(self.temp.name) / "last_calendar_read.json").exists())
        self.assertEqual(self.writes(), [])

    def test_missing_pagination_evidence_is_not_complete(self):
        self.client.request = lambda *args, **kw: app.Response(200, {"items": []}, {})
        with self.assertRaises(app.OperationError) as caught:
            self.service.read()
        self.assertEqual(caught.exception.code, "incomplete_read")

    def test_repeated_page_is_not_reported_complete(self):
        self.client.request = lambda *args, **kw: app.Response(200, {"items": [calendar()], "hasMore": True}, {})
        with self.assertRaises(app.OperationError):
            self.service.read()

    def test_wrong_parent_in_child_collection_blocks_read(self):
        self.client.events["10"][0]["CalendarId"] = "20"
        with self.assertRaises(app.OperationError) as caught:
            self.service.read()
        self.assertEqual(caught.exception.code, "identity_mismatch")

    def test_edit_is_conditional_single_patch_and_verified(self):
        preview = self.service.plan_calendar_edit("10", {"Description": "Approved description"})
        result = self.service.apply_calendar_edit(preview)
        self.assertEqual(result["status"], "updated_verified")
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(self.writes()[0]["headers"], {"If-Match": "\"1\""})
        self.assertEqual(self.writes()[0]["payload"], {"Description": "Approved description"})
        receipt = json.loads(Path(result["receipt_path"]).read_text())
        self.assertEqual(receipt["verified"]["Description"], "Approved description")
        self.assertNotIn("Basic fake", json.dumps(receipt))

    def test_unchanged_edit_sends_zero_patches(self):
        preview = self.service.plan_calendar_edit("10", {"Name": "Calendar A"})
        result = self.service.apply_calendar_edit(preview)
        self.assertEqual(result["status"], "unchanged_verified")
        self.assertEqual(self.writes(), [])

    def test_unchanged_read_does_not_require_etag(self):
        self.client.etags = False
        result = self.service.apply_calendar_edit(self.service.plan_calendar_edit("10", {"Name": "Calendar A"}))
        self.assertEqual(result["status"], "unchanged_verified")

    def test_changed_calendar_without_etag_blocks_before_write(self):
        self.client.etags = False
        with self.assertRaises(app.OperationError) as caught:
            self.service.plan_calendar_edit("10", {"Description": "New"})
        self.assertEqual(caught.exception.code, "etag_required")
        self.assertEqual(self.writes(), [])

    def test_framework_context_etag_is_supported(self):
        self.client.etags = False
        self.client.context_etag = True
        result = self.service.apply_calendar_edit(self.service.plan_calendar_edit("10", {"Description": "New"}))
        self.assertEqual(result["status"], "updated_verified")

    def test_tampered_preview_cannot_change_patch(self):
        preview = self.service.plan_calendar_edit("10", {"Description": "New"})
        preview["changes"]["Name"] = "Unreviewed"
        with self.assertRaises(app.OperationError):
            self.service.apply_calendar_edit(preview)
        self.assertEqual(self.writes(), [])

    def test_calendar_concurrent_edit_blocks_before_patch(self):
        preview = self.service.plan_calendar_edit("10", {"Description": "New"})
        self.client.calendars["10"]["Name"] = "Changed outside launcher"
        with self.assertRaises(app.ConflictError):
            self.service.apply_calendar_edit(preview)
        self.assertEqual(self.writes(), [])

    def test_etag_change_alone_blocks_before_patch(self):
        preview = self.service.plan_calendar_edit("10", {"Description": "New"})
        self.client.versions["10"] = "\"7\""
        with self.assertRaises(app.ConflictError):
            self.service.apply_calendar_edit(preview)

    def test_changed_child_associations_require_new_preview(self):
        preview = self.service.plan_calendar_edit("10", {"Description": "New"})
        self.client.events["10"].append(event("102", "302"))
        with self.assertRaises(app.ConflictError):
            self.service.apply_calendar_edit(preview)
        self.assertEqual(self.writes(), [])

    def test_http412_never_retries(self):
        preview = self.service.plan_calendar_edit("10", {"Description": "New"})
        self.client.reject_patch = 412
        with self.assertRaises(app.OperationError) as caught:
            self.service.apply_calendar_edit(preview)
        self.assertEqual(caught.exception.code, "write_rejected")
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(json.loads(Path(caught.exception.receipt_path).read_text())["status"], "rejected")

    def test_lost_response_preserves_receipt_and_reconciles_without_second_patch(self):
        preview = self.service.plan_calendar_edit("10", {"Description": "New"})
        self.client.after_commit_error = True
        with self.assertRaises(app.OperationError) as caught:
            self.service.apply_calendar_edit(preview)
        path = caught.exception.receipt_path
        self.assertEqual(caught.exception.code, "outcome_unknown")
        with self.assertRaises(app.OperationError) as blocked:
            self.service.plan_calendar_edit("10", {"Description": "New"})
        self.assertEqual(blocked.exception.code, "reconciliation_required")
        result = self.service.reconcile_calendar_edit(path)
        self.assertEqual(result["status"], "reconciled_applied")
        self.client.after_commit_error = False
        result = self.service.apply_calendar_edit(self.service.plan_calendar_edit("10", {"Description": "New"}))
        self.assertEqual(result["status"], "unchanged_verified")
        self.assertEqual(len(self.writes()), 1)

    def test_mismatched_readback_records_uncertain_outcome(self):
        preview = self.service.plan_calendar_edit("10", {"Description": "New"})
        self.client.patch_override = {"Description": "Other"}
        with self.assertRaises(app.OperationError) as caught:
            self.service.apply_calendar_edit(preview)
        self.assertEqual(caught.exception.code, "outcome_unknown")
        result = self.service.reconcile_calendar_edit(caught.exception.receipt_path)
        self.assertEqual(result["status"], "reconciled_conflict")

    def test_journal_failure_before_request_prevents_patch(self):
        preview = self.service.plan_calendar_edit("10", {"Description": "New"})
        with patch.object(app, "_atomic_json", side_effect=OSError("Disk unavailable")):
            with self.assertRaises(OSError):
                self.service.apply_calendar_edit(preview)
        self.assertEqual(self.writes(), [])

    def test_compact_edit_receipt_and_temp_paths_fit_a_deep_acceptance_folder(self):
        # The live acceptance stage path that exposed this regression is 214
        # characters.  Verify its receipt and temporary sibling stay under
        # the legacy Windows 260-character limit before attempting a PATCH.
        base = Path(self.temp.name)
        filler = "x" * max(1, 214 - len(str(base)) - 1)
        deep = base / filler
        service = app.CalendarService("Basic fake", {"environment": app.DEFAULT_BASE}, deep, client=self.client)
        result = service.apply_calendar_edit(service.plan_calendar_edit("10", {"Description": "New"}))
        receipt = Path(result["receipt_path"])
        temporary = receipt.parent / (".tmp-" + "f" * 32)
        self.assertLess(len(str(receipt)), 260)
        self.assertLess(len(str(temporary)), 260)
        self.assertEqual(len(self.writes()), 1)

    def test_period_cannot_exclude_an_existing_event(self):
        with self.assertRaises(app.OperationError) as caught:
            self.service.plan_calendar_edit("10", {"StartDate": "2027-02-01"})
        self.assertEqual(caught.exception.code, "event_outside_period")

    def test_exclusive_midnight_end_fits_calendar_last_day(self):
        preview = self.service.plan_calendar_edit("10", {"EndDate": "2027-01-18"})
        result = self.service.apply_calendar_edit(preview)
        self.assertEqual(result["status"], "updated_verified")

    def test_invalid_dates_and_disallowed_fields_are_rejected(self):
        for changes in ({"StartDate": "2027-02-30"}, {"StartDate": "2028-01-01"}, {"Name": " "},
                        {"CalendarId": "20"}, {"AbsenceCalendarEvent": []}, {"Description": "x" * 2001}):
            with self.subTest(changes=changes):
                with self.assertRaises(app.OperationError):
                    self.service.plan_calendar_edit("10", changes)
        self.assertEqual(self.writes(), [])

    def test_rename_cannot_collide_with_visible_calendar(self):
        self.client.calendars["20"] = calendar("20", "Calendar B")
        with self.assertRaises(app.OperationError) as caught:
            self.service.plan_calendar_edit("10", {"Name": "calendar b"})
        self.assertEqual(caught.exception.code, "name_collision")

    def test_description_blank_is_normalized_to_null(self):
        preview = self.service.plan_calendar_edit("10", {"Description": ""})
        self.assertIsNone(preview["changes"]["Description"])

    def test_empty_rules_do_not_authorize_deletion(self):
        preview = self.service.plan_delete_calendar("10")
        self.assertFalse(preview["allowed"])
        self.assertIsNone(preview["assignment_count"])
        self.assertEqual(preview["rule_subscriptions"], 0)
        preview.update(allowed=True, assignment_count=0)
        with self.assertRaises(app.OperationError) as caught:
            self.service.apply_delete_calendar(preview)
        self.assertEqual(caught.exception.code, "assignment_proof_required")
        self.assertEqual(self.writes(), [])

    def test_completed_preview_cannot_be_applied_twice(self):
        preview = self.service.plan_calendar_edit("10", {"Description": "New"})
        self.service.apply_calendar_edit(preview)
        with self.assertRaises(app.OperationError):
            self.service.apply_calendar_edit(preview)
        self.assertEqual(len(self.writes()), 1)

    def test_reconcile_rejects_outside_receipt(self):
        with self.assertRaises(app.OperationError):
            self.service.reconcile_calendar_edit(Path(self.temp.name).parent / "Calendar_Edit_fake.json")


class WireResponse:
    def __init__(self, status=200, value=None, headers=None):
        self.status = status
        self.headers = {"Content-Type": "application/json", **(headers or {})}
        self.raw = json.dumps(value or {}).encode()

    def read(self, limit):
        return self.raw[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class TransportTests(unittest.TestCase):
    def test_packaged_request_timeout_setting_is_honored(self):
        client = app.CalendarClient("Basic fake", {"environment": app.DEFAULT_BASE, "request_timeout_seconds": 60})
        with patch.object(client.opener, "open", return_value=WireResponse()) as opened:
            client.request("GET", app.CALENDARS)
        self.assertEqual(opened.call_args.kwargs["timeout"], 60)
        with self.assertRaises(app.OperationError):
            app.CalendarClient("Basic fake", {"environment": app.DEFAULT_BASE, "request_timeout_seconds": 60, "timeout_seconds": 30})

    def test_host_lock_and_route_lock(self):
        with self.assertRaises(app.OperationError):
            app.CalendarClient("Basic fake", {"base_url": "http://example.com"})
        selected = app.CalendarClient("Basic fake", {"base_url": "https://other-pod.example"})
        self.assertEqual(selected.environment, "https://other-pod.example")
        client = app.CalendarClient("Basic fake", {"environment": app.DEFAULT_BASE})
        for method, resource in (("GET", "https://example.com/"), ("DELETE", app.CALENDARS + "/10"),
                                 ("POST", app.CALENDARS), ("GET", app.CALENDARS + "/../workers")):
            with self.subTest(method=method, resource=resource):
                with self.assertRaises(app.OperationError):
                    client.request(method, resource)

    def test_patch_sends_exact_if_match_and_preserves_response_etag(self):
        client = app.CalendarClient("Basic fake", {"environment": app.DEFAULT_BASE})
        with patch.object(client.opener, "open", return_value=WireResponse(value=calendar(), headers={"ETag": '"2"'})) as opened:
            result = client.request("PATCH", app.CALENDARS + "/10", payload={"Description": "New"}, headers={"If-Match": '"1"'})
        request = opened.call_args.args[0]
        self.assertEqual(request.get_header("If-match"), '"1"')
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(request.get_header("Authorization"), "Basic fake")
        self.assertEqual(result.headers["ETag"], '"2"')
        self.assertNotIn("Basic fake", json.dumps(client.requests))

    def test_wildcard_or_injected_etag_is_rejected(self):
        client = app.CalendarClient("Basic fake", {"environment": app.DEFAULT_BASE})
        for etag in (None, "*", "\r\nAuthorization: wrong"):
            with self.assertRaises(app.OperationError):
                client.request("PATCH", app.CALENDARS + "/10", payload={"Name": "New"}, headers={"If-Match": etag})

    def test_http403_does_not_claim_missing_privilege_or_echo_body(self):
        client = app.CalendarClient("Basic secret", {"environment": app.DEFAULT_BASE})
        error = urllib.error.HTTPError(app.DEFAULT_BASE, 403, "Basic secret", {}, None)
        with patch.object(client.opener, "open", side_effect=error) as opened:
            with self.assertRaises(app.OperationError) as caught:
                client.request("GET", app.CALENDARS)
        self.assertEqual(opened.call_count, 1)
        self.assertNotIn("secret", str(caught.exception))
        self.assertNotIn("missing privilege", str(caught.exception))

    def test_redirect_is_blocked(self):
        with self.assertRaises(app.OperationError) as caught:
            app._NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://example.com")
        self.assertEqual(caught.exception.code, "redirect_blocked")


if __name__ == "__main__":
    unittest.main()
