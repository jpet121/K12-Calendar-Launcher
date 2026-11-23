# K–12 Calendar Launcher | Oracle HCM

**Assign holidays to many Oracle HCM calendars in one workbook.**

A school district or another organization may maintain up to 30 calendars. When the standard workflow requires one assignment at a time, even a single shared holiday can mean repeating the same task 30 times. Some employee groups follow different schedules, so assigning every event to every calendar would also be wrong.

The launcher lets a calendar administrator define each event once. In Excel, each row is an event and each column is a calendar. Mark **X** where the event belongs. For example, a district can mark a winter holiday across all 30 calendars and a second holiday across just four. The administrator can review all 34 assignments together and submit one build from the launcher.

The workbook builds **new calendars, new events, and their links**. A blank cell leaves existing links alone. The demonstrated build supports the **Public holiday** category. The sample is small enough to check by hand: three events, two calendars, and four assignments.

![Calendar inventory showing the workflow tabs, summary counts, search field, and redacted record IDs](docs/images/calendar-inventory.png)

*Inventory view from the working launcher. Screenshot supplied and redacted by Jan Petreczko. Cropped for public release.*

**[Try the workbook offline](docs/run-locally.md)** · **[Open the sample](app/templates/Calendar_Build_Sample.xlsx)** · **[See the design](docs/design.md)** · **[View verification](evidence/verification.md)**

## What you can do

| Workflow | What it does |
| --- | --- |
| **Read and export** | Search the calendars your Oracle account can see, inspect their events, and export the current inventory. |
| **Create from workbook** | Plan event assignments across calendars in Excel. Validate the workbook, review the proposed calendars and links, then submit the build. |
| **Edit** | Correct an existing calendar’s name, description, or date range. For a shared event, review the affected calendars before changing its dates. One event date change affects every calendar that uses it. The launcher checks the current record again before applying the correction. |
| **Delete check** | Select a calendar to check deletion eligibility. This edition blocks deletion because it cannot reliably verify that zero workers are assigned. |
| **Recovery** | After an interrupted build, reopen the same workbook with the same request ID. The launcher checks saved progress before continuing. For a correction with an uncertain result, compare the saved receipt with Oracle. You can also preview a change to restore earlier event dates. |

## Run it

Use Windows with Python 3.10 or newer. From the extracted folder, run **RUN-SELF-TEST.cmd**, then **START-CALENDAR-LAUNCHER.cmd**. The interface opens in the local browser. Live operations require an authorized Oracle HCM environment and account. The offline example requires neither.

## How it works

A Python service binds to the local computer. It reads workbook cells directly and serves the browser interface. The operator selects the Oracle environment. Separate state folders hold each environment’s journals and receipts. Runtime dependencies are included in the Python standard library.

## What the tests show

The public edition includes offline regression tests for workbook rules, environment selection, simulated Oracle operations and recovery. The verification page distinguishes those results from live tenant and Windows workstation checks.
