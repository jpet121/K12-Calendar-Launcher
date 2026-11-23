"""Build 01 workbook adapter. Standard library; no Oracle calls or cached formulas."""
from datetime import datetime, timedelta
import hashlib
import math
import re

if __package__:
    from .validate_pilot import ENVIRONMENT, read_cells
else:
    from validate_pilot import ENVIRONMENT, read_cells


def create_build_plan(path):
    sheets, epoch1904 = read_cells(path)
    if set(sheets) != {"Event matrix", "Calendars"}:
        raise ValueError("Keep the two supplied worksheets: Event matrix and Calendars.")
    matrix, setup = sheets["Event matrix"], sheets["Calendars"]

    def literal(cells, address):
        value = cells.get(address, "")
        if isinstance(value, dict):
            raise ValueError(address + ": enter a value; formulas are not accepted in input cells.")
        return value

    def text(cells, address, limit=240):
        value = literal(cells, address)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(address + ": text is required.")
        value = value.strip()
        if len(value) > limit or any(ord(c) < 32 or c == "|" for c in value):
            raise ValueError(address + ": invalid text length, control character, or HDL delimiter.")
        return value

    def day(cells, address):
        value = literal(cells, address)
        if not isinstance(value, (int, float)) or not math.isfinite(value) or value != int(value) or not 1 <= value <= 2958464:
            raise ValueError(address + ": enter an Excel date with no time of day.")
        epoch = datetime(1904, 1, 1) if epoch1904 else datetime(1899, 12, 30)
        return epoch + timedelta(days=int(value))

    headers = {"A8": "Event code", "B8": "Event name", "C8": "First day",
               "D8": "Last day\n(inclusive)", "F8": "All day", "G8": "Half day", "H8": "Category"}
    if any(matrix.get(k) != v for k, v in headers.items()):
        raise ValueError("The Event matrix columns changed; use the supplied Build 01 workbook.")
    if any(setup.get(k) != v for k, v in {"A8": "Calendar key", "B8": "Matrix label", "C8": "Start date", "D8": "End date", "E8": "Name in Oracle", "F8": "Description"}.items()):
        raise ValueError("The Calendars columns changed; use the supplied Build 01 workbook.")
    calendars = []
    for row in (9, 10):
        key, name = text(setup, f"A{row}", 48), text(setup, f"E{row}")
        if not re.fullmatch(r"SCHOOL01_[A-Z0-9_]+", key) or not name.startswith("K12 BUILD 01 - "):
            raise ValueError("Keep Build 01 calendar keys and names in their separate namespace.")
        start, end = day(setup, f"C{row}"), day(setup, f"D{row}")
        if (start, end) != (datetime(2026, 8, 4), datetime(2027, 6, 4)):
            raise ValueError("Build 01 calendar period must remain August 4, 2026 through June 4, 2027.")
        calendars.append({"key": key, "Name": name, "StartDate": start.date().isoformat(),
                          "EndDate": end.date().isoformat(), "Description": text(setup, f"F{row}", 500)})
    if len({c["key"] for c in calendars}) != 2 or len({c["Name"].casefold() for c in calendars}) != 2:
        raise ValueError("Calendar keys and Oracle names must be unique.")
    columns = {col: text(matrix, col + "7", 48) for col in ("I", "J")}
    if set(columns.values()) != {c["key"] for c in calendars}:
        raise ValueError("Matrix calendar keys must match the two calendar definitions.")
    events, associations = [], []
    for row in range(9, 28):
        code, name = text(matrix, f"A{row}", 48), text(matrix, f"B{row}", 80)
        if not re.fullmatch(r"SCHOOL01_[A-Z0-9_]+", code) or not name.startswith("K12 BUILD 01 - "):
            raise ValueError(f"Row {row}: use the separate Build 01 event namespace.")
        start, last = day(matrix, f"C{row}"), day(matrix, f"D{row}")
        if not datetime(2026, 8, 4) <= start <= last <= datetime(2027, 6, 4):
            raise ValueError(f"Row {row}: first/last days must be ordered and fit the calendar period.")
        all_day = literal(matrix, f"F{row}")
        if all_day not in ("Yes", "No"):
            raise ValueError(f"F{row}: All day must be Yes or No.")
        if literal(matrix, f"G{row}") != "No" or literal(matrix, f"H{row}") != "Public holiday":
            raise ValueError(f"Row {row}: this approved build uses Half day No and Public holiday.")
        events.append({"ShortCode": code, "Name": name, "Category": "PH",
                       "StartDateTimeLocal": start.isoformat(), "EndDateTimeLocal": (last + timedelta(days=1)).isoformat(),
                       "HalfDayForElapsed": "N", "expected_readback_AllDayFlag": all_day == "Yes"})
        links = 0
        for col, key in columns.items():
            marker = literal(matrix, f"{col}{row}")
            if marker not in ("", "X"):
                raise ValueError(f"{col}{row}: use X or blank.")
            if marker == "X":
                associations.append({"event_code": code, "calendar_key": key}); links += 1
        if not links:
            raise ValueError(f"Row {row}: select at least one calendar for every event.")
    # Extra rows/columns must never disappear silently during a load.
    for address, value in matrix.items():
        match = re.fullmatch(r"([A-Z]+)([0-9]+)", address)
        if value != "" and match and int(match[2]) >= 9 and (int(match[2]) > 27 or match[1] not in set("ABCDEFGHIJ")):
            raise ValueError(address + ": extra matrix data outside this 19-event build.")
    for address, value in setup.items():
        match = re.fullmatch(r"([A-Z]+)([0-9]+)", address)
        if value != "" and match and int(match[2]) >= 11:
            if match[1] != "A" or int(match[2]) < 13 or str(value).startswith("SCHOOL01_"):
                raise ValueError("Extra calendar data: this build supports the two approved calendars.")
    if len({e["ShortCode"].casefold() for e in events}) != 19:
        raise ValueError("Event codes must be unique.")
    if {a["calendar_key"] for a in associations} != set(columns.values()):
        raise ValueError("Both calendars need selected events.")
    return {"schema": "K12-CALENDAR-AB-1", "status": "offline_validated", "oracle_writes_submitted": False,
            "environment": ENVIRONMENT, "workbook_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "intent": "create_only_with_verified_resume", "end_time": "exclusive",
            "time_interpretation": "local midnight; inclusive last day becomes next midnight; verify Oracle readback",
            "counts": {"new_events": len(events), "new_calendars": len(calendars), "selected_links": len(associations)},
            "events": events, "calendars": calendars, "associations": associations, "rules": [], "blank_selection": "no_removal"}


def require_supported_loader(plan):
    unsupported = [e["ShortCode"] for e in plan["events"] if e.get("expected_readback_AllDayFlag") is not False]
    if unsupported:
        raise ValueError("All day=Yes is not implemented by the proven CalendarEvent loader: " + ", ".join(unsupported) +
                         ". The workbook is unchanged and no Oracle request was sent. Retaining Yes requires a verified write route; changing to No requires the request owner's decision.")
