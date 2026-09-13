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
    watch = commands.add_parser("watch", help="Watch continuously; fixture mode needs no credentials")
    watch.add_argument("--mode", choices=("fixture", "poll", "webhook"), default="fixture")
    watch.add_argument("--interval", type=float, default=None)
    watch.add_argument("--fixture-file", type=str, default=None)
    watch.add_argument("--allow-live", action="store_true", help="Also requires DEMO_MODE=false")
    watch.add_argument("--ticks", type=int, default=None, help="Stop after N ticks (smoke testing)")
    administration = watch.add_mutually_exclusive_group()
    administration.add_argument("--register-webhook", metavar="HTTPS_URL")
    administration.add_argument("--unregister-webhook", metavar="ID")
    administration.add_argument("--list-webhooks", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "watch":
        from pathlib import Path
        from earl.watch.watcher import run_watch
        try:
            if args.ticks is not None and args.ticks < 1:
                raise ValueError("ticks must be positive")
            run_watch(mode=args.mode, interval=args.interval, allow_live=args.allow_live,
                      fixture_file=Path(args.fixture_file) if args.fixture_file else None, ticks=args.ticks,
                      register_url=args.register_webhook, unregister_id=args.unregister_webhook,
                      list_hooks=args.list_webhooks)
        except (ValueError, RuntimeError, OSError) as exc:
            parser.error(str(exc))
        return 0
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
