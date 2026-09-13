"""Command line entry point; numerical and acceptance logic stays in pipeline."""

from __future__ import annotations

import argparse
import json
import sys

from earl.contracts import Outcome
from earl.pipeline import run_pipeline


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m earl")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Run one structural regression")
    run.add_argument("--scenario", default="thin-compression")
    run.add_argument("--param", help="Validated parameter object as JSON")
    run.add_argument("--description", help="Interpret a natural-language edit")
    run.add_argument("--live", action="store_true", help="Allow optional APIs only when DEMO_MODE=false")
    run.add_argument("--json", action="store_true", help="Emit the final record as JSON")
    commands.add_parser("eval", help="Reproduce the twenty-case evaluation offline")
    args = parser.parse_args(argv)
    if args.command == "eval":
        from earl.eval.harness import evaluate
        results = evaluate(write=True)
        print(json.dumps(results["summary"], indent=2))
        return 0
    try:
        param = json.loads(args.param) if args.param else None
        stream = run_pipeline(scenario=args.scenario, param=param, description=args.description, allow_live=args.live)
        while True:
            try:
                event = next(stream)
                if not args.json:
                    print(f"{event.stage:>8}  {event.status:7}  {event.duration_ms:8.1f} ms  {event.line}", flush=True)
            except StopIteration as completed:
                result = completed.value
                break
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    result.decision.validate()
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, allow_nan=False))
    else:
        print(f"\n{result.decision.outcome.value.upper()} | run {result.run_id}")
        if result.artifacts.get("delivery"):
            print(f"ECN:   {result.artifacts['delivery']['ecn_path']}")
            print(f"Email: {result.artifacts['delivery']['email_path']}")
    return {Outcome.APPROVED: 0, Outcome.ESCALATED: 2, Outcome.ERROR: 1}[result.decision.outcome]


if __name__ == "__main__":
    sys.exit(main())
