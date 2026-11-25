# K–12 Calendar Launcher · Bulk event assignments for Oracle HCM

**One district holiday can apply to 30 employee calendars. Spring break might apply to only four.** The administrator needs a way to enter each event once and review exactly which calendars receive it.

K–12 Calendar Launcher turns an Excel workbook into a build for Oracle Fusion Cloud Human Capital Management (HCM). Each row is an event. Each calendar has a column. Mark **X** at each intersection you want to create. A single workbook describes the new calendars, new events, and their assignments. A blank cell leaves existing assignments alone.

**[Download the sample workbook](https://github.com/jpet121/K12-Calendar-Launcher/raw/refs/heads/main/app/templates/Calendar_Build_Sample.xlsx)** · [Download the blank template](https://github.com/jpet121/K12-Calendar-Launcher/raw/refs/heads/main/app/templates/Calendar_Build_Blank.xlsx) · [Download the launcher (.zip)](https://github.com/jpet121/K12-Calendar-Launcher/archive/refs/heads/main.zip)

## Try the sample before connecting to Oracle

Open the workbook and check the four selected assignments. The sample uses Oracle's **Public holiday** category for all three entries.

| Event | Calendar A | Calendar B |
| --- | :---: | :---: |
| Martin Luther King Day | X | X |
| Spring Break | X | |
| Memorial Day | | X |

The offline validator reports **3 new events, 2 new calendars, and 4 selected links**. You can reproduce that result without an Oracle account. [Validate the workbook locally](docs/run-locally.md#try-the-workbook-offline)

## What you can do after the build

![The working Calendar Launcher shows 13 calendars, 87 distinct events, and 139 event links. It has tabs for inventory, creation, edits, deletion checks, and recovery. Record IDs are redacted.](docs/images/calendar-inventory.png)

The inventory above shows why the counts differ. A distinct event can appear on more than one calendar. This is Jan Petreczko's redacted inventory capture. The smaller workbook above is a separate, fictional example.

| Task | What the launcher does |
| --- | --- |
| **Read and export** | Search the calendars your Oracle account can see. Inspect their events and export the inventory. |
| **Build** | Validate a workbook and preview its proposed records and links. An authorized operator reviews and submits the build. |
| **Edit** | Correct a calendar name, description, or date range. For a shared event date change, review all affected calendars. The launcher reads the record again before applying the correction. |
| **Check deletion** | Check whether a calendar can be deleted. This edition blocks deletion because it cannot verify that zero workers are assigned. |
| **Recover** | Reopen the same workbook with its original request ID after an interruption. The launcher checks saved progress. For an uncertain edit, compare the saved receipt with Oracle. You can also preview a change to restore earlier event dates. |

## Run it on Windows

1. Install Python 3.10 or later. Extract the complete launcher ZIP to a writable folder.
2. Run **RUN-SELF-TEST.cmd**. The tests use fictional data and require no Oracle sign-in.
3. Run **START-CALENDAR-LAUNCHER.cmd**. The interface opens in your browser.
4. For live work, select your authorized Oracle environment. Check the target and proposed changes before you submit them.

The local Python service keeps a separate record of operations for each environment. Keep private configuration, workbooks, exports, and recovery records on the operator's machine. [Setup and sign-in](docs/run-locally.md) · [Design and recovery controls](docs/design.md)

## Verification

[![Offline checks](https://github.com/jpet121/K12-Calendar-Launcher/actions/workflows/verify.yml/badge.svg?branch=main)](https://github.com/jpet121/K12-Calendar-Launcher/actions/workflows/verify.yml)

The public suite exercises workbook validation, simulated Oracle operations, shared-event edits, and interruption recovery. The Windows job runs the self-test through the shipped launcher. Testing changes against a real Oracle tenant requires a separate, authorized acceptance run. [Verified Windows and Linux jobs](https://github.com/jpet121/K12-Calendar-Launcher/actions/runs/45662542827) · [Verification scope](evidence/verification.md)
