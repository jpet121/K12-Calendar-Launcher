# Public edition verification

## Windows and Linux

The public workflow passed all **232 offline regression tests on each platform**. Windows ran them through the shipped command launcher. Linux ran the same self-test through Python. Both jobs validated the sample workbook as three new events, two new calendars, and four selected links. The self-test uses local HTTP requests and simulated Oracle responses. It recorded zero live Oracle requests. [Public Windows and Linux run](https://github.com/jpet121/K12-Calendar-Launcher/actions/runs/45662542827)

The browser check started the real local service and opened all five tabs in installed Chrome while disconnected from Oracle. It checked the inventory, workbook, edit, delete check, and recovery panels. Chrome recorded no page exceptions and the test observed no external requests. The browser versions were 157.0.8103.79 on Windows and 157.0.8103.78 on Linux. [Browser check source](../tools/check_offline_ui.py)

Live tenant acceptance is open. The offline results cover validation, previews, simulated operations, and recovery controls. Confirmation of live writes, permissions, and tenant configuration requires an authorized Oracle environment.

## Reproduce

On Windows, run `RUN-SELF-TEST.cmd` from the extracted repository. On Linux, run `python app/launcher.py --self-test`. The self-test writes its result to `app/state/self_test_result.json`. [Workbook instructions](../docs/run-locally.md)

To repeat the browser check, install Chrome and the Playwright Python package, then run `python tools/check_offline_ui.py` from the repository root. Playwright is a test dependency. The launcher itself uses the Python standard library.

## Earlier records and public data

The original 1.0.3-rc1 public source passed 232 offline tests on Python 3.12.14 under Linux. Its JavaScript passed a syntax check. The internal baseline recorded 13 UI behavior checks. The five-tab public check above covers disconnected navigation. [Original offline result](offline-test-result.json)

The public configuration starts with zero saved profiles. Test fixtures use fictional hosts and identifiers. The inventory screenshot has covered record IDs. Keep private configuration, operator workbooks, tenant exports, and runtime state on the operator's machine.
