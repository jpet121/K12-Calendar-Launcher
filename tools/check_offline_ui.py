"""Open the disconnected launcher in installed Chrome and check its five workflow tabs."""
import os
from pathlib import Path
from queue import Empty, Queue
import subprocess
import sys
from threading import Thread
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main():
    process = subprocess.Popen(
        [sys.executable, "-u", str(ROOT / "app/launcher.py"), "--no-browser"],
        cwd=ROOT, stdout=subprocess.PIPE, encoding="utf-8",
    )
    try:
        lines = Queue()

        def read_output():
            for line in process.stdout:
                if line.startswith("Local launcher link (do not share): "):
                    lines.put(line.split(": ", 1)[1].strip())
                    return
            lines.put(None)

        Thread(target=read_output, daemon=True).start()
        try:
            url = lines.get(timeout=20)
        except Empty as error:
            raise RuntimeError("The launcher did not print its local URL.") from error
        if not url or urlsplit(url).hostname != "127.0.0.1":
            raise RuntimeError("The launcher did not start on loopback.")
        with sync_playwright() as pw:
            executable = os.environ.get("CALENDAR_TEST_CHROME")
            launch = {"executable_path": executable} if executable else {"channel": "chrome"}
            browser = pw.chromium.launch(**launch)
            context = browser.new_context()
            external = []
            errors = []

            def local_only(route):
                if urlsplit(route.request.url).netloc == urlsplit(url).netloc:
                    route.continue_()
                else:
                    external.append(urlsplit(route.request.url).hostname)
                    route.abort()

            context.route("**/*", local_only)
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(url, wait_until="networkidle")
            expect(page).to_have_title("Calendar Launcher · Choose environment")
            for tab in ("read", "create", "edit", "delete", "recovery"):
                page.locator(f'button[data-tab="{tab}"]').click()
                assert page.locator(f"#panel-{tab}").is_visible(), tab
            assert not external, external
            assert not errors, errors
            print(f"Chrome {browser.version} opened all five workflow tabs offline.")
            browser.close()
    finally:
        process.terminate()
        process.wait(timeout=10)


if __name__ == "__main__":
    main()
