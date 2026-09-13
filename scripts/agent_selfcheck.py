"""Verify autonomous file edits reach a real browser without any Run request.

Onshape Simulation and SkyCiv already provide analysis; EARL adds autonomous
operation, enforcement, notification and record. This check is fixture-backed.
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(ROOT / ".tools" / "playwright"))

import requests
from playwright.sync_api import sync_playwright, expect
from earl.watch.state import Ledger, atomic_json
from earl.watch.runner import ChangeSource


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--skip-ngrok-notice", action="store_true")
    args = parser.parse_args()
    output = ROOT / "out" / "qa"
    output.mkdir(parents=True, exist_ok=True)
    ledger, watcher = Ledger(), None
    fixture = ROOT / "out" / "agent" / "change.json"
    prior = json.loads(fixture.read_text()) if fixture.exists() else {"scenario": "reinforce-chord"}
    headers = {"ngrok-skip-browser-warning": "EARL autonomous verification"} if args.skip_ngrok_notice else {}
    requests.get(args.url + "/api/agent/status", headers=headers, timeout=10).raise_for_status()
    streams, errors, records = [], [], []

    def saved_edit(scenario):
        old_count = len(ledger.read()["runs"])
        subprocess.run([sys.executable, "scripts/mutate_fixture.py", scenario], cwd=ROOT, check=True,
                       stdout=subprocess.DEVNULL)
        fingerprint = ChangeSource(scenario=scenario).fingerprint()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            for record in reversed(ledger.read()["runs"][old_count:]):
                if record["fingerprint"] == fingerprint and record["notification_status"] not in {"PENDING", "SENDING"}:
                    return record
            time.sleep(.1)
        raise AssertionError(f"Watcher did not finish fixture edit {scenario}")

    log = (output / "agent-selfcheck.log").open("w")
    try:
        if not ledger.status()["running"]:
            watcher = subprocess.Popen([sys.executable, "-m", "earl", "watch", "--mode", "fixture", "--interval", "0.3"],
                                       cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                       env={**os.environ, "DEMO_MODE": "true"})
        elif ledger.status()["mode"] != "fixture":
            raise RuntimeError("Do not mutate a live watcher's source for a fixture selfcheck")
        saved_edit("reinforce-chord")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1366, "height": 768}, extra_http_headers=headers)
            page.on("request", lambda request: streams.append(request.url) if "/api/stream" in request.url else None)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(args.url)
            expect(page.locator("#follow-agent")).to_be_checked()
            for scenario, outcome, notice in (("thin-compression", "ESCALATED", "ESCALATION"),
                                               ("thin-compression", "ESCALATED", "SUPPRESSED"),
                                               ("reinforce-chord", "APPROVED", "CLEARED")):
                record = saved_edit(scenario)
                records.append(record["run_id"])
                expect(page.locator("#agent-rows tr").first).to_have_attribute("data-run-id", record["run_id"], timeout=15000)
                expect(page.locator("#verdict")).to_have_text(outcome, timeout=15000)
                expect(page.locator("#agent-rows tr").first).to_contain_text(notice)
                expect(page.frame_locator("#artifact-frame").locator("h1")).to_have_text(outcome)
                if notice == "ESCALATION":
                    page.screenshot(path=str(output / "agent-desktop.png"), full_page=True)
                    for name, width, height in (("tablet", 820, 1180), ("mobile", 390, 844), ("small-mobile", 320, 700)):
                        page.set_viewport_size({"width": width, "height": height})
                        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), name
                        page.screenshot(path=str(output / f"agent-{name}.png"), full_page=True)
                    page.set_viewport_size({"width": 1366, "height": 768})
            assert not streams, streams
            assert not errors, errors
            browser.close()
        report = {"url": args.url, "trigger": "synthetic file mutations", "live_onshape": False,
                  "autonomous_run_ids": records, "manual_run_requests": streams, "page_errors": errors,
                  "checks": ["new escalation", "identical escalation suppressed", "recovery cleared",
                             "main viewer and artifacts follow agent without Run", "responsive screenshots"]}
        atomic_json(output / "agent-browser-report.json", report)
        print(json.dumps(report, indent=2))
    finally:
        if watcher is not None and watcher.poll() is None:
            watcher.send_signal(signal.SIGINT)
            watcher.wait(timeout=30)
        atomic_json(fixture, prior)
        log.close()


if __name__ == "__main__":
    main()
