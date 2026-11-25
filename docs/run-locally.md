# Run the launcher

Download the [launcher ZIP](https://github.com/jpet121/K12-Calendar-Launcher/archive/refs/heads/main.zip). Extract the complete ZIP to a writable folder. Install Python 3.10 or later if needed. [Python for Windows](https://www.python.org/downloads/windows/)

## Try the workbook offline

Open PowerShell inside the extracted repository and run:

```powershell
cd app
py -3 -c "from pathlib import Path; from workbooks import load_plan; print(load_plan(Path('templates/Calendar_Build_Sample.xlsx'))['counts'])"
```

Expect `new_events: 3`, `new_calendars: 2`, and `selected_links: 4`. Martin Luther King Day appears on both calendars. Spring Break appears only on A. Memorial Day appears only on B. This reads the workbook locally. Run the self-test from the repository root with RUN-SELF-TEST.cmd.

## Open the interface

Double-click START-CALENDAR-LAUNCHER.cmd. Leave the console open. Start with an approved test tenant. Confirm the address with your Oracle administrator, paste it into the launcher and sign in. Review the proposed records before applying a change. A submitted build can create persistent Oracle records.

## Private configuration

The distributed configuration contains zero saved environments. Paste an authorized URL at runtime. Administrators can also create app/config.json with private profiles; the startup code prefers this file over app/config.example.json. Integration settings and any optional reference event belong to the corresponding profile. Obtain actual flow IDs and record IDs from your administrator. The example.invalid hosts and generated identifiers in the regression fixtures are for offline tests.

Keep the local launch URL, app/config.json, app/state, workbooks containing real requests, and exported data private. Closing the console does not cancel a job already submitted to Oracle.
