"""Offline reader for the supplied three-event/two-calendar workbook. No network calls.

This is a bounded input adapter for integration into the existing Oracle client.
It does not create, update, or restore Oracle records. Python standard library only.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import posixpath
import sys
import xml.etree.ElementTree as ET
from zipfile import ZipFile, BadZipFile

ENVIRONMENT = "https://tenant-a.example.invalid"
PREFIX = "DEMO_PILOT_"
NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
RID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
ROOT = Path(__file__).resolve().parent


def read_cells(path: Path):
    """Read literals directly; calculated display headers are deliberately ignored."""
    with ZipFile(path) as archive:
        shared = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared = ["".join(x.itertext()) for x in
                      ET.fromstring(archive.read("xl/sharedStrings.xml")).findall("s:si", NS)]
        wb = ET.fromstring(archive.read("xl/workbook.xml"))
        props = wb.find("s:workbookPr", NS)
        epoch1904 = props is not None and props.get("date1904") in ("1", "true")
        relations = {x.get("Id"): x.get("Target") for x in
                     ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))}
        sheets = {}
        for sheet in wb.findall("s:sheets/s:sheet", NS):
            target = relations[sheet.get(RID)]
            member = target.lstrip("/") if target.startswith("/") else posixpath.normpath("xl/" + target)
            cells = {}
            for cell in ET.fromstring(archive.read(member)).findall(".//s:sheetData/s:row/s:c", NS):
                if cell.find("s:f", NS) is not None:
                    cells[cell.get("r")] = {"formula": True}
                    continue
                value = cell.findtext("s:v", default="", namespaces=NS)
                if cell.get("t") == "s":
                    value = shared[int(value)]
                elif cell.get("t") == "inlineStr":
                    value = "".join(cell.find("s:is", NS).itertext())
                elif cell.get("t") not in ("str", "e", "d") and value:
                    value = float(value)
                cells[cell.get("r")] = value
            sheets[sheet.get("name")] = cells
    return sheets, epoch1904


def create_plan(path: Path):
    sheets, _ = read_cells(path)
    if str(sheets.get("Calendars", {}).get("A27", "")).startswith("Workbook format: K12-CALENDAR-AB-1."):
        if __package__:
            from .build_input import create_build_plan
        else:
            from build_input import create_build_plan
        return create_build_plan(path)
    return _create_pilot_plan(path)


def _create_pilot_plan(path: Path):
    sheets, epoch1904 = read_cells(path)
    contract = json.loads((ROOT / "Input_Contract.json").read_text(encoding="utf-8"))
    if set(sheets) != set(contract):
        raise ValueError("Use the two supplied pilot worksheets; unexpected or missing sheet.")
    for tab, specification in contract.items():
        editable = set(specification["input_cells"])
        fixed = specification["fixed_cells"]
        for address, value in sheets[tab].items():
            if value != "" and address not in editable and fixed.get(address) != value:
                raise ValueError(f"{tab}!{address}: extra data or changed template content; this adapter accepts only the supplied input cells.")
        if any(sheets[tab].get(address) != value for address, value in fixed.items()):
            raise ValueError(f"{tab}: template structure changed; use the supplied input cells.")
    matrix, setup = sheets["Event matrix"], sheets["Calendars"]

    def literal(cells, address, label):
        value = cells.get(address, "")
        if isinstance(value, dict):
            raise ValueError(f"{label}: use a literal value, not a formula ({address}).")
        return value

    def string(cells, address, label, limit=240):
        value = literal(cells, address, label)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label}: a text value is required ({address}).")
        value = value.strip()
        if len(value) > limit or any(char in value for char in "|\r\n"):
            raise ValueError(f"{label}: invalid length or delimiter ({address}).")
        return value

    def moment(cells, address, label):
        value = literal(cells, address, label)
        if not isinstance(value, (int, float)) or not 1 <= value <= 2958465:
            raise ValueError(f"{label}: enter an Excel date/time, not text ({address}).")
        epoch = datetime(1904, 1, 1) if epoch1904 else datetime(1899, 12, 30)
        stamp = epoch + timedelta(seconds=round(value * 86400))
        if stamp.time() != datetime.min.time():
            raise ValueError(f"{label}: this pilot requires midnight ({address}).")
        return stamp

    calendars = []
    for row in (9, 10):
        key = string(setup, f"A{row}", "Calendar key")
        name = string(setup, f"B{row}", "Calendar name")
        if not key.startswith(PREFIX) or not name.startswith("Sample pilot - "):
            raise ValueError("Calendar must remain in the pilot namespace.")
        start = moment(setup, f"C{row}", "Calendar start")
        end = moment(setup, f"D{row}", "Calendar end")
        if start.date() != datetime(2026, 8, 4).date() or end.date() != datetime(2027, 6, 4).date():
            raise ValueError("This bounded pilot uses the source calendar period 08/04/2026–06/04/2027.")
        calendars.append({"key": key, "Name": name, "StartDate": start.date().isoformat(),
                          "EndDate": end.date().isoformat(),
                          "Description": string(setup, f"E{row}", "Calendar description", 500)})
    if len({c["key"] for c in calendars}) != 2 or len({c["Name"].casefold() for c in calendars}) != 2:
        raise ValueError("Calendar keys and names must be unique.")
    by_key = {c["key"]: c for c in calendars}
    columns = {col: string(matrix, f"{col}7", "Matrix calendar key") for col in ("I", "J")}
    if len(set(columns.values())) != 2 or set(columns.values()) != set(by_key):
        raise ValueError("Matrix calendar keys must match the two Calendars records exactly.")

    events, associations = [], []
    for row in (9, 10, 11):
        code = string(matrix, f"A{row}", "Event code", 48)
        name = string(matrix, f"B{row}", "Event name", 80)
        if not code.startswith(PREFIX) or not name.startswith("Sample pilot - "):
            raise ValueError("Event must remain in the pilot namespace.")
        start = moment(matrix, f"C{row}", "Event start")
        end = moment(matrix, f"D{row}", "Event end")
        if end <= start:
            raise ValueError(f"{code}: end must be after start.")
        if not datetime(2026, 8, 4) <= start < end <= datetime(2027, 6, 5):
            raise ValueError(f"{code}: event must fit the pilot calendar period.")
        for col, expected in (("F", "No"), ("G", "No"), ("H", "Public holiday")):
            if literal(matrix, f"{col}{row}", f"{code} flags/category") != expected:
                raise ValueError(f"{code}: pilot supports All day No, Half day No, Public holiday only.")
        events.append({"ShortCode": code, "Name": name, "Category": "PH",
                       "StartDateTimeLocal": start.isoformat(), "EndDateTimeLocal": end.isoformat(),
                       "HalfDayForElapsed": "N", "expected_readback_AllDayFlag": False})
        linked = 0
        for col, key in columns.items():
            marker = literal(matrix, f"{col}{row}", f"{code} selection")
            if marker not in ("", "X"):
                raise ValueError(f"{col}{row}: use X or blank.")
            if marker == "X":
                associations.append({"event_code": code, "calendar_key": key})
                linked += 1
        if not linked:
            raise ValueError(f"{code}: select at least one calendar for this pilot.")
    if len({e["ShortCode"].casefold() for e in events}) != 3:
        raise ValueError("Event codes must be unique.")
    if {a["calendar_key"] for a in associations} != set(by_key):
        raise ValueError("Both calendars need at least one selected event.")
    return {
        "schema": "K12-CREATE-PILOT-1", "status": "offline_validated", "oracle_writes_submitted": False,
        "environment": ENVIRONMENT, "workbook_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "intent": "create_only_with_verified_resume", "end_time": "exclusive",
        "time_interpretation": "local wall-clock; verify Oracle readback; no implicit UTC conversion",
        "counts": {"new_events": len(events), "new_calendars": len(calendars), "selected_links": len(associations)},
        "events": events, "calendars": calendars, "associations": associations,
        "rules": [], "blank_selection": "no_removal",
        "integration_needed": ["reuse current local HDL writer", "resolve new master IDs before linking",
                               "calendar and link creation", "live readback and unchanged rerun"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workbook", nargs="?", type=Path, default=ROOT / "K12_SAMPLE_A_Create_Pilot.xlsx")
    parser.add_argument("--output", type=Path, default=ROOT / "Pilot_Preview.json")
    args = parser.parse_args()
    try:
        plan = create_plan(args.workbook)
        args.output.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError, KeyError, ET.ParseError, BadZipFile) as error:
        print(f"INPUT ERROR: {error}. No Oracle request was sent.", file=sys.stderr)
        return 2
    count = plan["counts"]
    print(f"OFFLINE INPUT CHECK PASSED: {count['new_events']} events, {count['new_calendars']} calendars, {count['selected_links']} links.")
    print(f"Preview: {args.output}\nNo Oracle request was sent. Integrate with the existing writer using LOCAL_CODEX_TASK.md.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
