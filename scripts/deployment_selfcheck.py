"""Verify real public HTTP, incremental SSE, and delivered artifacts."""

import argparse
import json
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from earl.contracts import Decision, Outcome
from earl.pipeline import STAGES, StageEvent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    args = parser.parse_args()
    url = args.url.rstrip("/")
    session = requests.Session()
    session.headers["ngrok-skip-browser-warning"] = "earl-deployment-verification"
    health = session.get(url + "/healthz", timeout=10)
    health.raise_for_status()
    assert health.json()["branch"] == "astra"
    root = session.get(url + "/", timeout=10)
    root.raise_for_status()
    assert "EARL | Structural CI" in root.text
    started = time.monotonic()
    events, arrival_ms = [], []
    with session.get(url + "/api/stream", params={"scenario": "thin-compression"}, timeout=(10, 30), stream=True) as response:
        response.raise_for_status()
        assert response.headers["Content-Type"].startswith("text/event-stream")
        for raw in response.iter_lines(chunk_size=1, decode_unicode=True):
            if raw.startswith("data: "):
                event = StageEvent.from_json(raw[6:])
                event.validate()
                events.append(event)
                arrival_ms.append(round((time.monotonic() - started) * 1000, 1))
    assert [e.stage for e in events] == list(STAGES)
    assert arrival_ms[3] > arrival_ms[0] + 100, "Proxy buffered the whole stream instead of incremental events"
    final = events[-1].data
    decision = Decision.from_dict(final["decision"])
    assert decision.outcome is Outcome.ESCALATED
    assert decision.governing_member.safety_factor < 1
    for name in ("ecn_url", "email_url", "trace_url", "report_url", "decision_url"):
        artifact = session.get(url + final["artifacts"][name], timeout=10)
        artifact.raise_for_status()
        if name == "ecn_url":
            assert "ESCALATED" in artifact.text and "NOT PERFORMED" in artifact.text
    report = {"url": url, "health": health.json(), "run_id": final["run_id"],
              "sse_stages": [e.stage for e in events], "sse_arrival_ms": arrival_ms,
              "outcome": decision.outcome.value, "governing_member": decision.governing_member.member_id,
              "safety_factor": decision.governing_member.safety_factor,
              "artifact_endpoints": "all HTTP 200", "independent_cross_check": decision.cross_check.performed}
    output = Path(__file__).resolve().parents[1] / "out" / "qa" / "deployment-report.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
