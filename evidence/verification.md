# Public edition verification

The 1.0.3-rc1 public source passed 232 offline tests on Python 3.12.14 under Linux. The self-test uses simulated Oracle responses and local HTTP requests. It recorded zero live Oracle requests. [Machine-readable result](offline-test-result.json)

The sample workbook validated as three events, two calendars, and four requested links. The blank template is for new work. The browser script passed a JavaScript syntax check.

The internal baseline recorded 13 UI behavior checks. The public build has not repeated those checks on a Windows workstation. A local Chromium smoke run could not start in the packaging environment because its browser executable was missing. Live Oracle acceptance and Windows launch verification for this public edition remain open.

## Reproduce

Run RUN-SELF-TEST.cmd on Windows, or run python launcher.py --self-test from app/. See the offline example in [Run the launcher](../docs/run-locally.md). The self-test writes its local result under app/state/.

## Publication review

The public configuration starts with zero saved profiles. The package excludes original operator handoffs, tenant exports, private configuration, cached bytecode and runtime state. Test fixtures use fictional hosts and record identifiers. The public screenshot shows a cropped inventory view with record IDs covered. Review exported workbooks and local state separately before sharing them.
