"""The whole thing, from the command line: graph -> gate -> SkyCiv -> ECN -> Gmail.

Sprint 4's smoke test. Defaults are fully OFFLINE and run the demo change
(earl.analysis.benchmark: the 10-bar truss design with m7 thinned from 8.203
to 6.000 in^2, the design itself as the before-state). Everything the run
produces is written to --out: the graph, the Decision, the ECN as JSON, text
and Markdown, the SVG, the e-mail as an .eml, and the delivery receipt.

    python3 scripts/run_pipeline.py                         # demo, offline, ECN to ./artifacts/demo
    python3 scripts/run_pipeline.py --graph g.json --before b.json
    python3 scripts/run_pipeline.py --onshape-fixture       # Track A graphs from the recorded Onshape read (barArea 1 -> 0.75 in^2)
    python3 scripts/run_pipeline.py --skyciv                # LIVE Stage 3 (needs SKYCIV_* creds; metered)
    python3 scripts/run_pipeline.py --send                  # LIVE Stage 4 (needs GMAIL_* creds)
    python3 scripts/run_pipeline.py --llm                   # LLM planner (needs META_MUSE_KEY)

Without --send the message is written to <out>/outbox/<ECN>.eml instead of
being sent, so the demo can be rehearsed without credentials and the artefact
is identical either way.

Exit: 0 APPROVED, 2 ESCALATED, 1 ERROR (or a misconfigured threshold).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from earl.analysis.benchmark import demo_before_graph, demo_change_graph  # noqa: E402
from earl.analysis.orchestrator import RuleBasedPlanner, default_planner  # noqa: E402
from earl.artifacts.skyciv_client import SkyCivClient  # noqa: E402
from earl.config import GmailConfig, skyciv_report_dir  # noqa: E402
from earl.contracts import ChangeEvent, ChangeKind, DependencyGraph, OnshapeRef, Outcome  # noqa: E402
from earl.delivery.gmail import GmailSender, OutboxSender  # noqa: E402
from earl.ingestion.benchmark import IN, benchmark_spec  # noqa: E402
from earl.ingestion.changes import change_id, describe_variable_change  # noqa: E402
from earl.ingestion.graph_builder import build_graph  # noqa: E402
from earl.pipeline import run_pipeline  # noqa: E402

EXIT_APPROVED = 0
EXIT_ERROR = 1
EXIT_ESCALATED = 2

FIXTURE_ASSEMBLY = ROOT / "tests" / "fixtures" / "onshape" / "assemblies_d_w_e.json"


def _load_graph(path: str) -> DependencyGraph:
    graph = DependencyGraph.from_json(Path(path).read_text(encoding="utf-8"))
    graph.validate()
    return graph


FIXTURE_LOAD_KIPS = 10.0      # the recorded truss is 1 in^2 bars; 100 kips fails it before any change
FIXTURE_AREA_BEFORE_IN2 = 1.0
FIXTURE_AREA_AFTER_IN2 = 0.75


def _onshape_fixture_graphs(area_after_in2: float) -> tuple[DependencyGraph, DependencyGraph]:
    """Track A's real path -- `graph_builder.build_graph()` on the recorded
    Onshape assembly -- for a `barArea` VARIABLE_EDIT, the pipeline's primary
    change signal. Returns (after, before).

    The recorded document drives every bar from one `barArea` variable, so
    the loads are scaled to a 1 in^2 truss (10 kips rather than the
    benchmark's 100, under which a 1 in^2 truss fails before any change)."""
    assembly = json.loads(FIXTURE_ASSEMBLY.read_text(encoding="utf-8"))
    before_expr, after_expr = f"{FIXTURE_AREA_BEFORE_IN2:g} in^2", f"{area_after_in2:g} in^2"
    kind, description = describe_variable_change("barArea", before_expr, after_expr)
    change = ChangeEvent(
        id=change_id("variable", "barArea", before_expr, after_expr),
        kind=kind,
        description=description,
        target_id="barArea",
        value_before=before_expr,
        value_after=after_expr,
    )
    source = OnshapeRef(
        document_id="74352477fea92ae1dadf61d6",
        workspace_id="7121b248aa290fdccdc1afa6",
        element_id="72f445c1fb5399de58418375",
        branch_name=f"earl-eval-{change.id}",
    )
    load = FIXTURE_LOAD_KIPS * 4448.222
    after = build_graph(
        assembly, graph_id=f"graph-fixture-{change.id}", change=change, source=source,
        spec=benchmark_spec(area=area_after_in2 * IN ** 2, load=load),
    ).graph
    before = build_graph(
        assembly, graph_id=f"graph-fixture-{change.id}-before", change=change, source=source,
        spec=benchmark_spec(area=FIXTURE_AREA_BEFORE_IN2 * IN ** 2, load=load),
    ).graph
    return after, before


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    src = parser.add_mutually_exclusive_group()
    src.add_argument("--graph", help="DependencyGraph JSON (default: the demo change)")
    src.add_argument("--onshape-fixture", nargs="?", const=FIXTURE_AREA_AFTER_IN2, type=float,
                     metavar="AREA_IN2",
                     help="build before/after graphs from the recorded Onshape assembly via Track A's "
                          "graph_builder, for a barArea variable edit 1 in^2 -> AREA_IN2 "
                          f"(default {FIXTURE_AREA_AFTER_IN2})")
    parser.add_argument("--before", help="before-state DependencyGraph JSON (default: the demo design when "
                                         "no --graph/--onshape-fixture is given)")
    parser.add_argument("--threshold", type=float, default=None,
                        help="safety-factor threshold (default: SAFETY_FACTOR_THRESHOLD, else 1.0; floor 1.0)")
    parser.add_argument("--llm", action="store_true", help="use the LLM planner (needs META_MUSE_KEY)")
    parser.add_argument("--skyciv", action="store_true", help="run Stage 3 against the live SkyCiv API")
    parser.add_argument("--skyciv-always", action="store_true", help="SkyCiv even for APPROVED (full paper trail)")
    parser.add_argument("--send", action="store_true", help="send the ECN through Gmail (needs GMAIL_* creds)")
    parser.add_argument("--notify-approved", action="store_true", help="also mail APPROVED outcomes")
    parser.add_argument("--to", default=None, help="recipient override (default: GMAIL_NOTIFY_RECIPIENT)")
    parser.add_argument("--out", default="artifacts/demo", help="output directory (default: artifacts/demo)")
    parser.add_argument("--quiet", action="store_true", help="do not print the ECN")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    # -- the graph -----------------------------------------------------------
    if args.graph:
        graph = _load_graph(args.graph)
        before = _load_graph(args.before) if args.before else None
    elif args.onshape_fixture is not None:
        graph, fixture_before = _onshape_fixture_graphs(args.onshape_fixture)
        before = _load_graph(args.before) if args.before else fixture_before
    else:
        graph = demo_change_graph()
        before = _load_graph(args.before) if args.before else demo_before_graph()

    planner = default_planner() if args.llm else RuleBasedPlanner()

    # -- Stage 3 -----------------------------------------------------------------
    skyciv = None
    if args.skyciv:
        skyciv = SkyCivClient.from_env(record_dir=Path(args.out) / "skyciv")

    # -- Stage 4 -----------------------------------------------------------------
    out = Path(args.out)
    if args.send:
        config = GmailConfig.from_env()
        sender = GmailSender(config)
        sender_address, recipient = config.sender, (args.to or config.recipient)
    else:
        sender = OutboxSender(out / "outbox")
        configured = GmailConfig.configured()
        sender_address = GmailConfig.from_env().sender if configured else "earl@localhost"
        recipient = args.to or (GmailConfig.from_env().recipient if configured else "engineer@localhost")

    notify_on = {Outcome.ESCALATED, Outcome.ERROR} | ({Outcome.APPROVED} if args.notify_approved else set())

    try:
        result = run_pipeline(
            graph,
            before=before,
            threshold=args.threshold,
            planner=planner,
            skyciv=skyciv,
            skyciv_always=args.skyciv_always,
            report_dir=skyciv_report_dir() or (out / "reports"),
            sender=sender,
            sender_address=sender_address,
            recipient=recipient,
            notify_on=notify_on,
        )
    except ValueError as e:
        print(f"misconfiguration: {e}", file=sys.stderr)      # R9
        return EXIT_ERROR

    written = result.write(out)

    if not args.quiet:
        print(result.ecn_text)
        print()
    d = result.decision
    print(f"outcome    : {d.outcome.value.upper()}  ({result.ecn.status.headline})")
    gov = d.governing_member
    if gov is not None:
        print(f"governing  : {gov.member_id} SF={gov.safety_factor:.3f}  violating={d.violating_member_ids}")
    print(f"stage 3    : {'SkyCiv ' + ('attached' if d.skyciv_report else 'unavailable') if result.skyciv_attempted else 'not run'}")
    if result.delivery is not None:
        r = result.delivery
        print(f"stage 4    : {r.channel} -> {r.to}  delivered={r.delivered}  "
              f"{'id=' + str(r.message_id) if r.message_id else ''}{('error=' + r.error) if r.error else ''}")
        print(f"attachments: {', '.join(r.attachments)}")
    else:
        print("stage 4    : not delivered (outcome not in notify set, or no sender)")
    for note in result.notes:
        print(f"note       : {note}")
    print("wrote      : " + ", ".join(str(p) for p in written.values()))

    if d.outcome is Outcome.APPROVED:
        return EXIT_APPROVED
    if d.outcome is Outcome.ESCALATED:
        return EXIT_ESCALATED
    return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
