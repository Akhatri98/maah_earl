"""Opt-in SkyCiv verification. Fixture fallback is never a live success.

Onshape Simulation and SkyCiv already provide analysis. EARL adds an autonomous
acceptance gate, notification, and record, not improved physics.
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from earl.config import PROJECT_ROOT, demo_mode, load_env
from earl.pipeline import run
from earl.watch.crosscheck import finalize
from earl.watch.state import atomic_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-live", action="store_true")
    parser.add_argument("--record-fixture", action="store_true", help="Export only a validated real API response")
    args = parser.parse_args()
    load_env()
    permitted = bool(args.allow_live and not demo_mode() and os.environ.get("SKYCIV_API_USERNAME")
                     and os.environ.get("SKYCIV_API_KEY"))
    root = PROJECT_ROOT / "out"
    result = run(scenario="reinforce-chord", allow_live=False, out_root=root)
    result = finalize(result, root, allow_live=permitted)
    report, cross = result.decision.skyciv_report, result.decision.cross_check
    live = report.provenance == "live SkyCiv API" and cross.performed
    evidence = {"live_verified": live, "live_permitted": permitted, "provenance": report.provenance,
                "run_id": result.run_id, "outcome": result.decision.outcome.value,
                "cross_check": cross.to_dict(),
                "note": "No tolerance tuning. Disagreement is escalated." if live else
                        "NOT VERIFIED LIVE: missing permission/credentials or unavailable API; fallback is not independent live evidence."}
    if args.record_fixture and live:
        key = f"{report.model_fingerprint}-{cross.member_id}.json"
        recorded = json.loads((root / "recordings" / "skyciv" / key).read_text())
        raw = json.dumps(recorded)
        if any(os.environ.get(name) and os.environ[name] in raw for name in ("SKYCIV_API_KEY", "SKYCIV_API_USERNAME")):
            raise SystemExit("Response contains credential text; retained locally, not exported to fixtures.")
        atomic_json(PROJECT_ROOT / "tests" / "fixtures" / "skyciv" / "responses" / key, recorded)
        evidence["recorded_fixture"] = key
    atomic_json(root / "agent" / "skyciv-verification.json", evidence)
    print(json.dumps(evidence, indent=2))
    return 0 if live else 2


if __name__ == "__main__":
    raise SystemExit(main())
