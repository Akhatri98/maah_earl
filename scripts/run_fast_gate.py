"""Stage 2 from the command line: DependencyGraph in, Decision out.

Runs the fast gate (earl.analysis.gate.run_fast_gate) on a graph, prints the
verdict member by member, and optionally writes the Decision JSON and the
SVG picture the demo and the ECN use.

Defaults are the project's demo change (earl.analysis.benchmark, R1): the
10-bar truss at its published optimum with member m7 thinned from 7.457 to
3.500 in^2. The edited member survives; its neighbour m5 fails -- the
downstream domino the whole project is about. When no --graph is given the
demo's before-state (the unchanged optimum) is used as --before so the
Decision carries stress_before as well as stress_after.

Fully offline unless META_MUSE_KEY is set and --no-llm is NOT given, in
which case the LLM planner proposes the load case (the verdict path is
identical either way -- the plan cannot narrow the evaluation).

Run:  python3 scripts/run_fast_gate.py --no-llm --out decision.json --svg truss.svg
      python3 scripts/run_fast_gate.py --graph graph.json --before before.json
Exit: 0 APPROVED, 2 ESCALATED, 1 ERROR (or a misconfigured threshold).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from earl.analysis.benchmark import demo_before_graph, demo_change_graph  # noqa: E402
from earl.analysis.gate import run_fast_gate  # noqa: E402
from earl.analysis.orchestrator import RuleBasedPlanner, default_planner  # noqa: E402
from earl.analysis.visualize import save_truss_svg  # noqa: E402
from earl.contracts import Decision, DependencyGraph, Outcome  # noqa: E402

EXIT_APPROVED = 0
EXIT_ERROR = 1
EXIT_ESCALATED = 2


def _load_graph(path: str) -> DependencyGraph:
    """Decode only -- deliberately NO graph.validate() here.  The gate turns a
    validation ValueError into an ERROR decision (R10) that is still printed
    and written to --out/--svg, and a bad --before graph only costs a note
    (R11).  Validating here would trade both for a traceback and no files."""
    return DependencyGraph.from_json(Path(path).read_text(encoding="utf-8"))


def _fmt(value: float | None, unit_scale: float = 1.0, digits: int = 3) -> str:
    if value is None:
        return "n/a"
    return f"{value / unit_scale:.{digits}f}"


def print_summary(decision: Decision) -> None:
    print(f"decision   : {decision.id}  outcome={decision.outcome.value.upper()}")
    print(f"graph      : {decision.graph_id}  change={decision.change_id}")
    print(f"change     : {decision.change_description}")
    print(f"threshold  : SF >= {decision.safety_factor_threshold:g}   "
          f"load case: {decision.load_case_id}   solver: {decision.solver} {decision.solver_version}")
    if decision.error_message:
        print(f"error      : {decision.error_message}")

    sc = decision.sanity_checks
    residual = "n/a" if sc.max_residual_force is None else f"{sc.max_residual_force:.2e} N"
    print(f"sanity     : equilibrium={sc.equilibrium_ok} (max residual {residual})  "
          f"linearity={sc.linearity_ok}")
    if sc.note:
        print(f"             note: {sc.note}")

    print(f"\n{'member':<7} {'status':<14} {'SF':>10} {'stress_before':>14} {'stress_after':>13} "
          f"{'capacity':>10} {'affected':>8}  note")
    print(f"{'':<7} {'':<14} {'':>10} {'(MPa)':>14} {'(MPa)':>13} {'(MPa)':>10}")
    for r in decision.member_results:
        sf = "n/a" if r.safety_factor is None else (
            f"{r.safety_factor:.3f}" if r.safety_factor < 1e5 else f"{r.safety_factor:.1e}"
        )
        print(f"{r.member_id:<7} {r.status.value:<14} {sf:>10} {_fmt(r.stress_before, 1e6):>14} "
              f"{_fmt(r.stress_after, 1e6):>13} {_fmt(r.capacity, 1e6):>10} "
              f"{'yes' if r.is_affected else 'no':>8}  {r.note or ''}")

    gov = decision.governing_member
    if gov is not None:
        print(f"\ngoverning  : {gov.member_id} SF={gov.safety_factor:.4f}")
    print(f"violating  : {decision.violating_member_ids}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--graph", help="DependencyGraph JSON (default: the m7 demo change)")
    parser.add_argument("--before", help="before-state DependencyGraph JSON for stress_before "
                                         "(default: the unchanged optimum when --graph is not given)")
    parser.add_argument("--threshold", type=float, default=None,
                        help="safety-factor threshold (default: SAFETY_FACTOR_THRESHOLD from .env, "
                             "else 1.0; values below 1.0 are rejected)")
    parser.add_argument("--no-llm", action="store_true",
                        help="use the deterministic RuleBasedPlanner even if META_MUSE_KEY is set")
    parser.add_argument("--planned-only", action="store_true",
                        help="evaluate only the planned load case (default: every load case, worst wins)")
    parser.add_argument("--out", help="write the Decision JSON here")
    parser.add_argument("--svg", help="write the truss picture (SVG) here")
    parser.add_argument("--decision-id", default=None, help="Decision id (default: dec-<graph id>)")
    args = parser.parse_args(argv)

    if args.graph:
        graph = _load_graph(args.graph)
        before = _load_graph(args.before) if args.before else None
    else:
        graph = demo_change_graph()
        before = _load_graph(args.before) if args.before else demo_before_graph()

    planner = RuleBasedPlanner() if args.no_llm else default_planner()
    print(f"planner    : {type(planner).__name__}")

    try:
        decision = run_fast_gate(
            graph,
            threshold=args.threshold,
            planner=planner,
            before=before,
            evaluate_all_load_cases=not args.planned_only,
            decision_id=args.decision_id,
        )
    except ValueError as e:
        # R9: a misconfigured threshold is raised, never turned into a Decision.
        print(f"misconfiguration: {e}", file=sys.stderr)
        return EXIT_ERROR

    print_summary(decision)

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(decision.to_json(), encoding="utf-8")
        print(f"\nwrote      : {out}")
    if args.svg:
        try:
            print(f"wrote      : {save_truss_svg(graph, decision, args.svg)}")
        except (KeyError, ValueError) as e:
            # A graph malformed enough to fail validation (a member naming a
            # node that does not exist) cannot be drawn either; the Decision
            # JSON above is the deliverable, the picture is best effort.
            print(f"svg not written: {type(e).__name__}: {e}", file=sys.stderr)

    if decision.outcome is Outcome.APPROVED:
        return EXIT_APPROVED
    if decision.outcome is Outcome.ESCALATED:
        return EXIT_ESCALATED
    return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
