import json
from pathlib import Path
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from zipfile import ZipFile, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import workbooks as app

SAMPLE = ROOT / "templates" / "Calendar_Build_Sample.xlsx"
NS = app.NS["s"]


class WorkbookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "test.xlsx"

    def edit(self, changes=None, parts=None):
        """Mutate saved sample input as an adversarial Excel upload fixture."""
        with ZipFile(SAMPLE) as z:
            content = {name: z.read(name) for name in z.namelist()}
        for tab, address, value in changes or []:
            part = f"xl/worksheets/sheet{tab}.xml"
            root = ET.fromstring(content[part])
            data = root.find(f"{{{NS}}}sheetData")
            row_num = "".join(char for char in address if char.isdigit())
            row = data.find(f"{{{NS}}}row[@r='{row_num}']")
            if row is None:
                row = ET.SubElement(data, f"{{{NS}}}row", r=row_num)
            cell = row.find(f"{{{NS}}}c[@r='{address}']")
            if cell is None:
                cell = ET.SubElement(row, f"{{{NS}}}c", r=address)
            for child in list(cell):
                cell.remove(child)
            cell.attrib.pop("t", None)
            if isinstance(value, tuple) and value[0] == "formula":
                ET.SubElement(cell, f"{{{NS}}}f").text = value[1]
            elif isinstance(value, str):
                cell.set("t", "inlineStr")
                ET.SubElement(ET.SubElement(cell, f"{{{NS}}}is"), f"{{{NS}}}t").text = value
            elif value is not None:
                ET.SubElement(cell, f"{{{NS}}}v").text = str(value)
            content[part] = ET.tostring(root)
        content.update(parts or {})
        with ZipFile(self.path, "w", ZIP_DEFLATED) as z:
            for name, data in content.items():
                z.writestr(name, data)
        return self.path

    def invalid(self, changes, message):
        with self.assertRaisesRegex(app.WorkbookError, message):
            app.load_plan(self.edit(changes))

    def test_sample_load_has_exact_counts_and_inclusive_date_conversion(self):
        plan = app.load_plan(SAMPLE)
        self.assertEqual(plan["counts"], {"new_events": 3, "new_calendars": 2, "selected_links": 4})
        self.assertEqual(plan["request_id"], "DEMO_SCHOOL_DEMO")
        self.assertEqual(plan["events"][0]["StartDateTimeLocal"], "2027-01-18T00:00:00")
        self.assertEqual(plan["events"][0]["EndDateTimeLocal"], "2027-01-19T00:00:00")
        self.assertEqual(plan["events"][1]["EndDateTimeLocal"], "2027-04-16T00:00:00")
        self.assertFalse(plan["events"][0]["expected_readback_AllDayFlag"])

    def test_blank_template_requires_inputs(self):
        with self.assertRaisesRegex(app.WorkbookError, "Excel date"):
            app.load_plan(ROOT / "templates" / "Calendar_Build_Blank.xlsx")

    def test_added_event_row_is_processed(self):
        cells, _ = app.read_cells(SAMPLE)
        changes = [(1, f"{c}12", cells["Event matrix"].get(f"{c}11", "")) for c in "ABCDFGHIJ"]
        changes += [(1, "A12", "K12RT_EXTRA"), (1, "B12", "Extra approved event")]
        plan = app.load_plan(self.edit(changes))
        self.assertEqual(len(plan["events"]), 4)
        self.assertEqual(len(plan["associations"]), 5)

    def test_calendar_rows_and_columns_match_by_key_when_reordered(self):
        plan = app.load_plan(self.edit([(1, "I7", "DEMO_SCHOOL_B"), (1, "J7", "DEMO_SCHOOL_A")]))
        spring = [a for a in plan["associations"] if a["event_code"].endswith("SB27")]
        self.assertEqual(spring[0]["calendar_key"], "DEMO_SCHOOL_B")

    def test_formulas_in_inputs_rejected_not_cached(self):
        for address in ("A9", "B9", "C9", "F9", "G9", "H9", "I9"):
            with self.subTest(address=address):
                self.invalid([(1, address, ("formula", "1"))], "literal input")

    def test_duplicate_case_insensitive_event_code(self):
        self.invalid([(1, "A10", "demo_school_mlk27")], "unique")

    def test_duplicate_calendar_name(self):
        self.invalid([(2, "E10", "sample school - calendar a 2026-2027")], "unique")

    def test_partial_calendar_row_and_extra_data_are_not_ignored(self):
        self.invalid([(2, "A11", "invalid key with spaces")], "start with a letter")
        self.invalid([(2, "G9", "unmapped value")], "unexpected data")

    def test_unknown_calendar_column_and_missing_key(self):
        self.invalid([(1, "K9", "X")], "no calendar key")
        self.invalid([(1, "I7", "UNKNOWN")], "matching Calendars")

    def test_invalid_calendar_selection(self):
        self.invalid([(1, "I9", "Yes")], "X or leave blank")

    def test_orphan_event(self):
        self.invalid([(1, "I9", ""), (1, "J9", "")], "at least one")

    def test_no_implicit_removal_on_blank(self):
        plan = app.load_plan(self.edit([(1, "J9", "")]))
        self.assertEqual(plan["blank_selection"], "no_removal")
        self.assertEqual(len(plan["associations"]), 3)

    def test_dates_outside_selected_calendar(self):
        self.invalid([(1, "D9", 47000)], "outside selected")

    def test_reversed_dates(self):
        self.invalid([(1, "D9", 45000)], "precedes")

    def test_date_text_and_subday_time_are_rejected(self):
        self.invalid([(1, "C9", "2027-01-18")], "Excel date")
        self.invalid([(1, "C9", 46405.5)], "Excel date")

    def test_nan_date_rejected(self):
        self.invalid([(1, "C9", float("nan"))], "Excel date")

    def test_year_range_rejected(self):
        self.invalid([(1, "C9", 100000)], "supported calendar years")

    def test_flags_preserved_for_caller_capability_check(self):
        plan = app.load_plan(self.edit([(1, "F9", "Yes"), (1, "G9", "Yes")]))
        self.assertTrue(plan["events"][0]["expected_readback_AllDayFlag"])
        self.assertEqual(plan["events"][0]["HalfDayForElapsed"], "Y")

    def test_invalid_flags_and_category(self):
        self.invalid([(1, "F9", "true")], "Yes or No")
        self.invalid([(1, "H9", "Vacation")], "Public holiday")

    def test_hdl_delimiters_control_chars_and_formula_text_rejected(self):
        for value in ("bad|name", "bad\nname", "=HYPERLINK(bad)"):
            with self.subTest(value=value):
                self.invalid([(1, "B9", value)], "delimiter|formula marker")

    def test_request_id_path_traversal_rejected(self):
        self.invalid([(2, "B3", "../other")], "start with a letter")

    def test_request_id_is_a_portable_windows_folder_name(self):
        for value in ("NUL", "COM1", "request:alternate", "request."):
            with self.subTest(value=value):
                self.invalid([(2, "B3", value)], "simple request ID")

    def test_legacy_build01_request_id_survives_hashed_upload_filename(self):
        path = self.edit([(2, "A3", "Build 01 request"), (2, "B3", ""),
                          (2, "A27", "Workbook format: K12-CALENDAR-AB-1."),
                          (2, "A9", "SCHOOL01_A_2627"), (2, "A10", "SCHOOL01_B_2627"),
                          (1, "I7", "SCHOOL01_A_2627"), (1, "J7", "SCHOOL01_B_2627")])
        hashed = path.with_name("1234567890abcdef.xlsx")
        path.rename(hashed)
        self.assertEqual(app.load_plan(hashed)["request_id"], "SCHOOL01_2627")

    def test_1904_epoch_reads_same_dates(self):
        cells, _ = app.read_cells(SAMPLE)
        changes = [(tab, address, value - 1462) for tab, name in [(1, "Event matrix"), (2, "Calendars")]
                   for address, value in cells[name].items()
                   if address[0] in ("C", "D") and isinstance(value, (int, float)) and int(address[1:]) >= 9]
        with ZipFile(SAMPLE) as z:
            root = ET.fromstring(z.read("xl/workbook.xml"))
        props = root.find(f"{{{NS}}}workbookPr")
        if props is None:
            props = ET.SubElement(root, f"{{{NS}}}workbookPr")
        props.set("date1904", "1")
        plan = app.load_plan(self.edit(changes, {"xl/workbook.xml": ET.tostring(root)}))
        self.assertEqual(plan["events"], app.load_plan(SAMPLE)["events"])

    def test_malformed_zip_is_clear_error(self):
        self.path.write_bytes(b"not a zip")
        with self.assertRaisesRegex(app.WorkbookError, "damaged"):
            app.load_plan(self.path)

    def test_entity_and_archive_traversal_rejected(self):
        for part, content in [("xl/workbook.xml", b'<!DOCTYPE x [<!ENTITY e "evil">]><x/>'), ("../escape", b"unsafe")]:
            with self.subTest(part=part):
                with self.assertRaisesRegex(app.WorkbookError, "entities|archive member"):
                    app.load_plan(self.edit(parts={part: content}))

    def test_external_relationship_rejected(self):
        value = b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="X" Target="https://example.test" TargetMode="External"/></Relationships>'
        with self.assertRaisesRegex(app.WorkbookError, "External"):
            app.load_plan(self.edit(parts={"xl/_rels/workbook.xml.rels": value}))

    def test_snapshot_export_preserves_exact_values_and_escapes_csv(self):
        snapshot = {"environment": app.ENVIRONMENT, "calendars": [{"CalendarId": "810000331861000", "Name": "=unsafe", "StartDate": "2027-01-01", "EndDate": None, "events": [{"CalendarEventId": "810000331861001", "CalendarCalEventId": "810000331861002", "StartDateTime": "2027-01-18T00:00:00+00:00", "EndDateTime": "2027-01-19T00:00:00+00:00", "AllDayFlag": False, "HalfDayForElapsed": "N"}]}]}
        path = app.export_calendar_data(snapshot, Path(self.tmp.name) / "export.zip")
        with ZipFile(path) as z:
            self.assertEqual(json.loads(z.read("snapshot.json")), snapshot)
            self.assertIn("'=unsafe", z.read("calendars.csv").decode("utf-8-sig"))
            text = z.read("calendar_events.csv").decode("utf-8-sig")
            self.assertIn('"false"', text)
            self.assertIn("2027-01-19T00:00:00+00:00", text)
        self.assertFalse(path.with_suffix(".zip.tmp").exists())


if __name__ == "__main__":
    unittest.main()
