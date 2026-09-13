"""The eval replay from the command line: 20 scenarios, system vs. baseline.

Sprint 5A's deliverable, and the source of the demo's headline number.
Defaults are fully offline and run the SYSTEM only, which needs no
credentials:

    python3 scripts/run_eval.py                          # system over all 20 scenarios
    python3 scripts/run_eval.py --agents system,baseline # LIVE baseline (needs META_MUSE_KEY)
    python3 scripts/run_eval.py --replay artifacts/eval/transcripts   # baseline from a recording
    python3 scripts/run_eval.py --scenario s01 --verbose # one scenario, with its tool calls
    python3 scripts/run_eval.py --list                   # the scenario set and nothing else

A live baseline run records every model turn under <out>/transcripts, so the
same run can be replayed later with --replay and reproduce its numbers exactly
(the tools are pure functions of the scenario, so nothing drifts). That is how
the demo shows real measured numbers without a network call on stage.

Exit: 0 if the system dropped no dominoes, 1 if it dropped any, 2 on a
misconfiguration.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from earl.analysis.scoreboard import AGENT_BASELINE, AGENT_SYSTEM  # noqa: E402
from earl.eval.agents import SystemAgent  # noqa: E402
from earl.eval.baseline import BaselineAgent  # noqa: E402
from earl.eval.consistency import run_consistency  # noqa: E402
from earl.eval.harness import run_eval, write_run  # noqa: E402
from earl.eval.llm import LLMUnavailable, MuseToolClient, ReplayClient  # noqa: E402
from earl.eval.scenarios import SCENARIOS, scenario  # noqa: E402

EXIT_CLEAN = 0
EXIT_DROPPED = 1
EXIT_MISCONFIGURED = 2


def _list_scenarios() -> int:
    print(f"{len(SCENARIOS)} scenarios:\n")
    print(f"{'id':<22} {'change kind':<16} target  description")
    for s in SCENARIOS:
        print(f"{s.id:<22} {s.kind.value:<16} {s.target_id:<7} {s.description}")
    return EXIT_CLEAN


def _build_agents(args) -> list:
    names = [n.strip() for n in args.agents.split(",") if n.strip()]
    agents = []
    for name in names:
        if name == AGENT_SYSTEM:
            agents.append(SystemAgent(threshold=args.threshold))
        elif name == AGENT_BASELINE:
            agents.append(_baseline(args))
        else:
            raise SystemExit(
                f"unknown agent {name!r}; choose from {AGENT_SYSTEM}, {AGENT_BASELINE}"
            )
    return agents


def _baseline(args) -> BaselineAgent:
    if args.replay:
        replay_dir = Path(args.replay)

        def client_for(scenario_id: str) -> ReplayClient:
            path = replay_dir / f"{scenario_id}.json"
            if not path.exists():
                raise LLMUnavailable(f"no recording for {scenario_id} at {path}")
            return ReplayClient.from_file(path)

        return BaselineAgent(client_for=client_for, transcript_dir=None)

    return BaselineAgent(
        client=MuseToolClient.from_env(model=args.model),
        transcript_dir=Path(args.out) / "transcripts",
    )


def _consistency(args, agents, scenarios, names) -> int:
    """Consistency mode: does each agent give the same answer twice?

    Exit 0 when every agent was stable on every scenario, 1 when any answer
    changed between runs -- so CI (and the demo) can tell the difference.
    """
    def progress(scenario_obj, results) -> None:
        marks = []
        for result in results:
            if result.stable:
                marks.append(f"{result.agent}=stable({result.correct_runs}/{len(result.runs)} correct)")
            else:
                shown = " | ".join(
                    ("(none)" if not a else ",".join(a)) for a in result.distinct
                )
                marks.append(f"{result.agent}=UNSTABLE [{shown}]")
        truth = ",".join(results[0].truth) if results and results[0].truth else "-"
        print(f"{scenario_obj.id:<22} truth={truth:<22}  " + "  ".join(marks))

    try:
        report = run_consistency(
            agents, scenarios, repeats=args.repeat,
            threshold=args.threshold, on_scenario=progress,
        )
    except LLMUnavailable as e:
        print(f"\nrun stopped: {e}", file=sys.stderr)
        return EXIT_MISCONFIGURED

    print()
    print(report.to_markdown())
    path = report.write(args.out)
    print(f"\nwrote: {path}, {path.with_name('consistency.md')}")

    unstable = [r for r in report.results if not r.stable]
    return EXIT_DROPPED if unstable else EXIT_CLEAN


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--agents", default=AGENT_SYSTEM,
                        help=f"comma-separated: {AGENT_SYSTEM}, {AGENT_BASELINE} "
                             f"(default: {AGENT_SYSTEM} -- the baseline needs a model)")
    parser.add_argument("--scenario", action="append", default=None,
                        help="run only this scenario id (repeatable); default: all 20")
    parser.add_argument("--replay", default=None, metavar="DIR",
                        help="run the baseline from recorded transcripts in DIR "
                             "instead of calling a model")
    parser.add_argument("--model", default=None,
                        help="Muse model id for the baseline agent (default: "
                             "LLM_MODEL, else the orchestrator's default)")
    parser.add_argument("--threshold", type=float, default=1.0,
                        help="safety-factor threshold for truth and for the system "
                             "(default 1.0, the floor)")
    parser.add_argument("--out", default="artifacts/eval",
                        help="output directory (default: artifacts/eval)")
    parser.add_argument("--repeat", type=int, default=None, metavar="N",
                        help="consistency mode: run every scenario N times and "
                             "report which answers changed between runs "
                             "(plan.md: 'missing something OR inconsistent "
                             "across runs')")
    parser.add_argument("--verbose", action="store_true",
                        help="print each agent's answer per scenario")
    parser.add_argument("--list", action="store_true",
                        help="print the scenario set and exit")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if args.list:
        return _list_scenarios()

    scenarios = (
        tuple(scenario(s) for s in args.scenario) if args.scenario else SCENARIOS
    )

    try:
        agents = _build_agents(args)
    except LLMUnavailable as e:
        print(f"cannot run the baseline: {e}", file=sys.stderr)
        return EXIT_MISCONFIGURED

    names = [a.name for a in agents]
    print(f"{len(scenarios)} scenario(s), agents: {', '.join(names)}, "
          f"threshold {args.threshold:g}\n")

    if args.repeat is not None:
        return _consistency(args, agents, scenarios, names)

    def progress(outcome) -> None:
        if not outcome.scorable:
            print(f"{outcome.scenario.id:<22} TRUTH UNAVAILABLE: {outcome.truth_error}")
            return
        truth = outcome.truth_unsafe_member_ids or ["-"]
        line = f"{outcome.scenario.id:<22} truth={','.join(truth):<22}"
        for name in names:
            answer = outcome.answers[name]
            reported = sorted(answer.unsafe_member_ids) or ["-"]
            missed = sorted(set(outcome.truth_unsafe_member_ids) - set(answer.unsafe_member_ids))
            mark = f"  DROPPED {','.join(missed)}" if missed else ""
            line += f"  {name}={','.join(reported)}{mark}"
        print(line)
        if args.verbose:
            for name in names:
                answer = outcome.answers[name]
                print(f"    {name:<9} {answer.outcome.value:<10} "
                      f"{answer.detail}  {(answer.note or '')[:80]}")

    try:
        run = run_eval(agents, scenarios, threshold=args.threshold,
                       on_scenario=progress)
    except LLMUnavailable as e:
        print(f"\nrun stopped: {e}", file=sys.stderr)
        return EXIT_MISCONFIGURED
    except ValueError as e:
        print(f"misconfiguration: {e}", file=sys.stderr)
        return EXIT_MISCONFIGURED

    print()
    print(run.summary())

    written = write_run(run, args.out)
    print()
    print("wrote: " + ", ".join(str(p) for p in written.values()))

    board = run.board
    try:
        dropped = board.score_for(AGENT_SYSTEM).dropped_dominoes
    except KeyError:
        return EXIT_CLEAN
    return EXIT_CLEAN if dropped == 0 else EXIT_DROPPED


if __name__ == "__main__":
    raise SystemExit(main())
