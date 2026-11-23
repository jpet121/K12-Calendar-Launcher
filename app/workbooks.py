"""Portable workbook input and exact snapshot export. Python standard library only.

Inputs use the two-tab calendar X matrix. Input dates are inclusive; returned
Oracle event boundaries are exclusive. Export archives are read-only evidence,
not an input workbook and never silently convert Oracle timestamps.
"""
from __future__ import annotations

import csv
from datetime import datetime, timedelta
import hashlib
import io
import json
import math
from pathlib import Path
import posixpath
import re
import xml.etree.ElementTree as ET
from zipfile import ZipFile, BadZipFile, ZIP_DEFLATED

ENVIRONMENT = "https://tenant-a.example.invalid"
NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
RID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
MAX_FILE = 16 * 1024 * 1024
MAX_EXPANDED = 64 * 1024 * 1024
MAX_ROWS = 10000
MAX_CALENDARS = 100
KEY = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,79}\Z")
REQUEST_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,79}\Z")
WINDOWS_RESERVED = re.compile(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])\Z", re.IGNORECASE)
CELL = re.compile(r"([A-Z]{1,3})([1-9][0-9]{0,6})\Z")


class WorkbookError(ValueError):
    """Invalid or unsupported workbook, before any Oracle call."""


def _xml(data):
    # Reject DTDs/entities, including UTF-16 encodings, before stdlib parsing.
    compact = data.replace(b"\x00", b"").upper()
    if b"<!DOCTYPE" in compact or b"<!ENTITY" in compact:
        raise WorkbookError("XML entities and document types are not permitted.")
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise WorkbookError("The workbook contains malformed XML.") from exc


def _column(address):
    match = CELL.fullmatch(address or "")
    if not match:
        raise WorkbookError("The workbook has an invalid cell address.")
    result = 0
    for char in match.group(1):
        result = result * 26 + ord(char) - 64
    row = int(match.group(2))
    if result > 16384 or row > 1048576:
        raise WorkbookError("The workbook cell address exceeds Excel limits.")
    return result, row


def _letters(number):
    answer = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        answer = chr(65 + remainder) + answer
    return answer


def read_cells(path: Path):
    path = Path(path)
    if path.suffix.lower() != ".xlsx":
        raise WorkbookError("Choose an .xlsx workbook; macros and other formats are not accepted.")
    if path.stat().st_size > MAX_FILE:
        raise WorkbookError("Workbook is larger than the 16 MB input limit.")
    try:
        with ZipFile(path) as archive:
            members = archive.infolist()
            names = [m.filename for m in members]
            if len(names) != len(set(names)) or len(names) > 2000:
                raise WorkbookError("Duplicate or excessive workbook archive members.")
            if sum(m.file_size for m in members) > MAX_EXPANDED:
                raise WorkbookError("Expanded workbook exceeds 64 MB.")
            for item in members:
                if item.flag_bits & 1:
                    raise WorkbookError("Encrypted workbooks are not supported.")
                if item.file_size > 16 * 1024 * 1024:
                    raise WorkbookError("A workbook member exceeds the input size limit.")
                if ".." in item.filename.split("/") or "\\" in item.filename or item.filename.startswith("/"):
                    raise WorkbookError("Invalid workbook archive member path.")
                if "vbaproject" in item.filename.lower() or "externallinks/" in item.filename.lower():
                    raise WorkbookError("Macros and external workbook links are not accepted.")
                if item.filename.endswith(".rels"):
                    for relation in _xml(archive.read(item.filename)):
                        if relation.get("TargetMode") == "External":
                            raise WorkbookError("External workbook relationships are not accepted.")
            shared = []
            if "xl/sharedStrings.xml" in names:
                shared = ["".join(n.itertext()) for n in _xml(archive.read("xl/sharedStrings.xml")).findall("s:si", NS)]
            wb = _xml(archive.read("xl/workbook.xml"))
            props = wb.find("s:workbookPr", NS)
            epoch1904 = props is not None and props.get("date1904") in ("1", "true")
            relations = {}
            for item in _xml(archive.read("xl/_rels/workbook.xml.rels")):
                if item.get("Id") in relations:
                    raise WorkbookError("Duplicate workbook relationship identifier.")
                relations[item.get("Id")] = item.get("Target", "")
            sheets = {}
            for sheet in wb.findall("s:sheets/s:sheet", NS):
                name = sheet.get("name")
                if name in sheets:
                    raise WorkbookError("Duplicate worksheet name.")
                target = relations[sheet.get(RID)]
                member = target.lstrip("/") if target.startswith("/") else posixpath.normpath("xl/" + target)
                if not member.startswith("xl/worksheets/"):
                    raise WorkbookError("Invalid worksheet relationship path.")
                cells = {}
                for cell in _xml(archive.read(member)).findall(".//s:sheetData/s:row/s:c", NS):
                    address = cell.get("r")
                    _column(address)
                    if address in cells:
                        raise WorkbookError("Duplicate worksheet cell address.")
                    if cell.find("s:f", NS) is not None:
                        value = {"formula": True}
                    else:
                        value = cell.findtext("s:v", default="", namespaces=NS)
                        kind = cell.get("t")
                        if kind == "s":
                            index = int(value)
                            if index < 0 or index >= len(shared):
                                raise WorkbookError("Invalid shared string reference.")
                            value = shared[index]
                        elif kind == "inlineStr":
                            element = cell.find("s:is", NS)
                            value = "" if element is None else "".join(element.itertext())
                        elif kind == "e":
                            value = {"error": value}
                        elif kind == "b":
                            value = {"boolean": value}
                        elif kind not in ("str", "d") and value:
                            value = float(value)
                    cells[address] = value
                sheets[name] = cells
            return sheets, epoch1904
    except WorkbookError:
        raise
    except (BadZipFile, KeyError, IndexError, ValueError, RuntimeError) as exc:
        raise WorkbookError("The workbook is damaged or has an unsupported Excel structure.") from exc


def _literal(cells, address, label):
    value = cells.get(address, "")
    if isinstance(value, dict):
        raise WorkbookError(f"{label} ({address}): use a literal input, not a formula or Excel error.")
    return value


def _text(cells, address, label, limit=240, optional=False):
    value = _literal(cells, address, label)
    if optional and value == "":
        return ""
    if not isinstance(value, str) or not value.strip():
        raise WorkbookError(f"{label} ({address}): enter a text value.")
    value = value.strip()
    if len(value) > limit or any(ord(char) < 32 or char == "|" for char in value):
        raise WorkbookError(f"{label} ({address}): invalid length, control character, or HDL delimiter.")
    if value[0] in "=+@":
        raise WorkbookError(f"{label} ({address}): text may not begin with a spreadsheet formula marker.")
    return value


def _key(cells, address, label, limit=80):
    value = _text(cells, address, label, limit)
    if not KEY.fullmatch(value):
        raise WorkbookError(f"{label} ({address}): start with a letter and use letters, digits, underscore, dot, colon, or hyphen.")
    return value


def _day(cells, address, label, epoch1904):
    value = _literal(cells, address, label)
    if not isinstance(value, (int, float)) or not math.isfinite(value) or value != int(value):
        raise WorkbookError(f"{label} ({address}): enter an Excel date with no time, not text.")
    if not 1 <= value <= 2958464 or (not epoch1904 and value == 60):
        raise WorkbookError(f"{label} ({address}): date is outside the supported range.")
    try:
        epoch = datetime(1904, 1, 1) if epoch1904 else datetime(1899, 12, 30)
        result = epoch + timedelta(days=value)
    except OverflowError as exc:
        raise WorkbookError(f"{label} ({address}): date is outside the supported range.") from exc
    if not 2000 <= result.year <= 2100:
        raise WorkbookError(f"{label} ({address}): supported calendar years are 2000 through 2100.")
    return result


def _yes_no(cells, address, label):
    value = _literal(cells, address, label)
    if value not in ("Yes", "No"):
        raise WorkbookError(f"{label} ({address}): select Yes or No.")
    return value == "Yes"


def _header(cells, expected, tab):
    for column, name in enumerate(expected, 1):
        actual = cells.get(f"{_letters(column)}8", "")
        if not isinstance(actual, str) or " ".join(actual.split()).casefold() != name.casefold():
            raise WorkbookError(f"{tab} row 8 must keep the supplied column headings ({name}).")


def load_plan(path: Path, *, environment: str = ENVIRONMENT) -> dict:
    """Read every populated input row; reject the entire plan on any input error."""
    from environments import normalize_origin
    environment = normalize_origin(environment)
    path = Path(path)
    sheets, epoch = read_cells(path)
    if set(sheets) != {"Event matrix", "Calendars"}:
        raise WorkbookError("Use exactly the Event matrix and Calendars worksheets.")
    matrix, setup = sheets["Event matrix"], sheets["Calendars"]
    _header(matrix, ["Event code", "Event name", "First day", "Last day (inclusive)", "Days", "All day", "Half day", "Category"], "Event matrix")
    _header(setup, ["Calendar key", "Matrix label", "Start date", "End date", "Name in Oracle", "Description"], "Calendars")
    legacy_notes = isinstance(setup.get("A27"), str) and setup["A27"].startswith("Workbook format: K12-CALENDAR-AB-1.")
    if setup.get("A3") == "Request ID":
        request_id = _key(setup, "B3", "Request ID")
    elif legacy_notes and matrix.get("I7") == "SCHOOL01_A_2627" and matrix.get("J7") == "SCHOOL01_B_2627":
        request_id = "SCHOOL01_2627"
    else:
        raise WorkbookError("Enter a stable Request ID in Calendars B3, with the label Request ID in A3.")
    if not REQUEST_ID.fullmatch(request_id) or WINDOWS_RESERVED.fullmatch(request_id):
        raise WorkbookError("Use a simple request ID (letters, digits, underscores, hyphens) in Calendars B3.")
    for address, value in setup.items():
        col, row = _column(address)
        if row >= 9 and col > 6 and value != "":
            raise WorkbookError(f"Calendars {address}: unexpected data outside the supplied input columns.")
    cal_rows = sorted({row for address, value in setup.items() for col, row in [_column(address)]
                       if row >= 9 and value != "" and (2 <= col <= 6 or (col == 1 and not (legacy_notes and row >= 13)))})
    if not 1 <= len(cal_rows) <= MAX_CALENDARS:
        raise WorkbookError("Enter between 1 and 100 calendar definitions.")
    calendars = []
    for row in cal_rows:
        key = _key(setup, f"A{row}", "Calendar key")
        _text(setup, f"B{row}", "Matrix label", 80)
        start = _day(setup, f"C{row}", "Calendar start", epoch)
        end = _day(setup, f"D{row}", "Calendar end", epoch)
        if end < start:
            raise WorkbookError(f"{key}: calendar end precedes its start.")
        calendars.append({"key": key, "Name": _text(setup, f"E{row}", "Calendar name"),
                          "StartDate": start.date().isoformat(), "EndDate": end.date().isoformat(),
                          "Description": _text(setup, f"F{row}", "Calendar description", 500, optional=True)})
    for field in ("key", "Name"):
        if len({item[field].casefold() for item in calendars}) != len(calendars):
            raise WorkbookError(f"Calendar {field} values must be unique, ignoring letter case.")
    by_key = {item["key"]: item for item in calendars}
    columns = {col: _key(matrix, address, "Matrix calendar key") for address, value in matrix.items()
               for col, row in [_column(address)] if row == 7 and col >= 9 and value != ""}
    if len(columns) > MAX_CALENDARS or set(columns.values()) != set(by_key) or len(set(columns.values())) != len(columns):
        raise WorkbookError("Matrix row 7 must contain each calendar key once, matching Calendars exactly.")
    populated = [(col, row) for address, value in matrix.items() if value != "" for col, row in [_column(address)] if row >= 9 and col != 5]
    for col, row in populated:
        if col > 8 and col not in columns:
            raise WorkbookError(f"{_letters(col)}{row}: selection column has no calendar key in row 7.")
    rows = sorted({row for _, row in populated})
    if not 1 <= len(rows) <= MAX_ROWS:
        raise WorkbookError("Enter between 1 and 10000 event rows.")
    events, associations = [], []
    for row in rows:
        code = _key(matrix, f"A{row}", "Event code", 48)
        start = _day(matrix, f"C{row}", "Event first day", epoch)
        last = _day(matrix, f"D{row}", "Event last day", epoch)
        if last < start:
            raise WorkbookError(f"{code}: last day precedes first day.")
        category = _text(matrix, f"H{row}", "Category", 80)
        if category not in ("Public holiday", "PH"):
            raise WorkbookError(f"{code}: this release accepts Public holiday (PH) only.")
        events.append({"ShortCode": code, "Name": _text(matrix, f"B{row}", "Event name", 80),
                       "Category": "PH", "StartDateTimeLocal": start.isoformat(),
                       "EndDateTimeLocal": (last + timedelta(days=1)).isoformat(),
                       "HalfDayForElapsed": "Y" if _yes_no(matrix, f"G{row}", "Half day") else "N",
                       "expected_readback_AllDayFlag": _yes_no(matrix, f"F{row}", "All day")})
        linked = 0
        for col, key in columns.items():
            address = f"{_letters(col)}{row}"
            marker = _literal(matrix, address, "Calendar selection")
            if marker not in ("", "X"):
                raise WorkbookError(f"{address}: select X or leave blank.")
            if marker == "X":
                calendar = by_key[key]
                if not calendar["StartDate"] <= start.date().isoformat() <= last.date().isoformat() <= calendar["EndDate"]:
                    raise WorkbookError(f"{code}: event dates fall outside selected calendar {key}.")
                associations.append({"event_code": code, "calendar_key": key})
                linked += 1
        if not linked:
            raise WorkbookError(f"{code}: select at least one calendar with X.")
    if len({item["ShortCode"].casefold() for item in events}) != len(events):
        raise WorkbookError("Event codes must be unique, ignoring letter case.")
    if {item["calendar_key"] for item in associations} != set(by_key):
        raise WorkbookError("Each calendar must have at least one selected event.")
    return {"schema": "K12-CALENDAR-MATRIX-1", "status": "offline_validated",
            "request_id": request_id, "environment": environment,
            "workbook_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "oracle_writes_submitted": False, "intent": "create_only_with_verified_resume",
            "end_time": "exclusive", "time_interpretation": "local midnight; input first/last days inclusive",
            "events": events, "calendars": calendars, "associations": associations, "rules": [],
            "blank_selection": "no_removal", "counts": {"new_events": len(events), "new_calendars": len(calendars), "selected_links": len(associations)}}


def _csv(rows, fields):
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore", quoting=csv.QUOTE_ALL)
    writer.writeheader()
    for row in rows:
        safe = {}
        for field in fields:
            value = row.get(field, "")
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False, sort_keys=True)
            elif value is None:
                value = ""
            elif isinstance(value, bool):
                value = "true" if value else "false"
            value = str(value)
            if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")):
                value = "'" + value
            safe[field] = value
        writer.writerow(safe)
    return output.getvalue().encode("utf-8-sig")


def export_calendar_data(snapshot: dict, path: Path) -> Path:
    """Write a .zip of exact JSON and spreadsheet-safe, read-only CSV views."""
    path = Path(path)
    if path.suffix.lower() != ".zip":
        raise WorkbookError("Calendar export path must end in .zip.")
    calendars = snapshot.get("calendars")
    if not isinstance(calendars, list):
        raise WorkbookError("Snapshot must contain a calendars list.")
    calendar_fields = ["CalendarId", "Name", "StartDate", "EndDate", "Description"]
    event_fields = ["CalendarId", "CalendarName", "CalendarCalEventId", "CalendarEventId", "ShortCode", "Name", "StartDateTime", "EndDateTime", "AllDayFlag", "HalfDayForElapsed", "CategoryCode", "Category", "EventType"]
    event_rows = []
    for calendar in calendars:
        events = calendar.get("events", [])
        if not isinstance(events, list):
            raise WorkbookError("Each calendar's events must be a complete list.")
        for event in events:
            event_rows.append({**event, "CalendarId": calendar.get("CalendarId", ""), "CalendarName": calendar.get("Name", "")})
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        with ZipFile(temporary, "w", ZIP_DEFLATED) as archive:
            archive.writestr("snapshot.json", json.dumps(snapshot, ensure_ascii=False, indent=2).encode("utf-8"))
            archive.writestr("calendars.csv", _csv(calendars, calendar_fields))
            archive.writestr("calendar_events.csv", _csv(event_rows, event_fields))
            archive.writestr("READ_ME.txt", "Read-only Oracle snapshot. snapshot.json preserves all values and JSON types exactly.\nCSV timestamps retain Oracle offsets and exclusive end boundaries; they are not inclusive input dates.\nCSV text beginning with formula markers is prefixed with an apostrophe for spreadsheet safety.\nDo not use these CSVs as launcher input or remove their identifiers. Use the calendar build template for new requests.\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return path
