"""The whole pipeline, end to end. [Sprint 4 -- both tracks]

sprint_timeline.md, Sprint 4:

    "Wire the two tracks together end to end: dependency graph -> fast gate ->
     Biject decision -> (escalated) SkyCiv report -> ECN -> Gmail delivery.
     This is where Track A's graph output first meets Track B's real decision
     object instead of the mock."

This module is that wire. It owns no physics and no judgement: every step is
a call into one track's public entry point, in a fixed order, and the only
thing it adds is the hand-off itself -- copying the walk's provenance and the
graph's Onshape source onto the Decision (contract 0.2.0) so the ECN can be
written from the Decision alone.

    DependencyGraph (Track A)
        │  walk_and_apply            fixed BFS; affected_* populated
        ▼
    run_fast_gate (Track B)          PyNite + Biject -> validated Decision
        │  enrich_decision           + source, hops_from_change, reached_via
        ▼
    escalate (Track B, ESCALATED)    SkyCiv report + cross-check attached
        │
        ▼
    build_ecn / render (Track A)     the templated notice
        │
        ▼
    deliver (Track A)                Gmail, or an .eml in the outbox
        │
        ▼
    merge hook (APPROVED only)       the ONLY path to Onshape Main

Two things are deliberately structural here rather than conventional:

  * The merge hook runs only when `Decision.validate()` has passed on an
    APPROVED outcome, and it is the caller's callable -- nothing in this
    module (and nothing an LLM can reach) holds a merge method. plan.md:
    "Merge-to-main permission doesn't exist for the model at all."
  * Every stage after the gate degrades rather than aborts. SkyCiv down,
    Gmail down, a renderer exception -- each lands in `PipelineResult.notes`
    and, where the contract has a slot, on the Decision itself. The verdict
    was computed before any of them ran, and a delivery failure must never
    turn into a missing verdict.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .analysis.gate import run_fast_gate
from .analysis.orchestrator import Planner
from .analysis.visualize import render_truss_svg
from .artifacts.escalation import escalate
from .artifacts.skyciv_client import DEFAULT_DESIGN_CODE, SkyCivClient
from .contracts import Decision, DependencyGraph, Outcome
from .delivery.ecn import ECN, ECNStatus, build_ecn
from .delivery.gmail import DeliveryReceipt, Sender, deliver
from .delivery.render import render_markdown, render_text
from .ingestion.walker import WalkResult, walk_and_apply

# Outcomes that put a message in someone's inbox by default. An APPROVED
# change is logged (ECN written) but not mailed unless asked: plan.md sends
# the report "when a decision is needed".
DEFAULT_NOTIFY_ON: frozenset[Outcome] = frozenset({Outcome.ESCALATED, Outcome.ERROR})

MergeHook = Callable[[Decision], str | None]


@dataclass
class PipelineResult:
    """Everything one run produced, in the order it was produced."""

    graph: DependencyGraph
    decision: Decision
    ecn: ECN
    walk: WalkResult | None = None
    ecn_text: str = ""
    ecn_markdown: str = ""
    svg: str | None = None
    delivery: DeliveryReceipt | None = None
    merged: str | None = None            # what the merge hook returned, if it ran
    skyciv_attempted: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def outcome(self) -> Outcome:
        return self.decision.outcome

    @property
    def status(self) -> ECNStatus:
        return self.ecn.status

    def write(self, out_dir: str | Path) -> dict[str, Path]:
        """Persist the ledger entry: graph, decision, ECN (json/txt/md), SVG,
        delivery receipt. Returns the paths written, keyed by kind."""
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        written: dict[str, Path] = {}

        def put(kind: str, name: str, text: str) -> None:
            path = out / name
            path.write_text(text, encoding="utf-8")
            written[kind] = path

        put("graph", f"{self.graph.id}.graph.json", self.graph.to_json())
        put("decision", f"{self.decision.id}.json", self.decision.to_json())
        put("ecn_json", f"{self.ecn.id}.json", self.ecn.to_json())
        put("ecn_text", f"{self.ecn.id}.txt", self.ecn_text)
        put("ecn_markdown", f"{self.ecn.id}.md", self.ecn_markdown)
        if self.svg:
            put("svg", f"{self.ecn.id}.svg", self.svg)
        if self.delivery is not None:
            put("delivery", f"{self.ecn.id}.delivery.json", self.delivery.to_json())
        if self.notes:
            put("notes", f"{self.ecn.id}.notes.json", json.dumps(self.notes, indent=2))
        return written


# --------------------------------------------------------------------------
# The hand-off: what Track B's Decision needs from Track A's graph and walk
# --------------------------------------------------------------------------

def enrich_decision(
    decision: Decision,
    graph: DependencyGraph,
    walk: WalkResult | None,
) -> Decision:
    """Copy the Onshape source and the walk's per-member provenance onto the
    Decision (contract 0.2.0), so the ECN can be written from it alone.

    No new computation: `source` is the graph's, `hops_from_change` and
    `reached_via` are `walker.Reach.distance` / `.via`. Members the walk did
    not reach keep None in both, which the ECN reports as "not reached" --
    never as downstream.
    """
    if decision.graph_id != graph.id:
        raise ValueError(
            f"decision {decision.id!r} answers graph {decision.graph_id!r}, "
            f"not {graph.id!r}"
        )
    decision.source = graph.source
    if walk is not None:
        for result in decision.member_results:
            reach = walk.reach(result.member_id)
            if reach is None:
                result.hops_from_change = None
                result.reached_via = None
            else:
                result.hops_from_change = reach.distance
                result.reached_via = reach.via
            result.is_affected = result.member_id in graph.affected_member_ids
    decision.validate()
    return decision


# --------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------

def run_pipeline(
    graph: DependencyGraph,
    *,
    before: DependencyGraph | None = None,
    threshold: float | None = None,
    planner: Planner | None = None,
    walk_graph: bool = True,
    skyciv: SkyCivClient | None = None,
    skyciv_always: bool = False,
    report_dir: str | Path | None = None,
    design_code: str | None = DEFAULT_DESIGN_CODE,
    sender: Sender | None = None,
    sender_address: str | None = None,
    recipient: str | None = None,
    notify_on: frozenset[Outcome] | set[Outcome] = DEFAULT_NOTIFY_ON,
    merge_hook: MergeHook | None = None,
    render_svg: bool = True,
    ecn_id: str | None = None,
    decision_id: str | None = None,
) -> PipelineResult:
    """Graph in, delivered ECN out. See the module docstring for the order.

    `threshold=None` reads SAFETY_FACTOR_THRESHOLD (floor 1.0, raised on
    misconfiguration before anything is solved -- R9). `skyciv=None` skips
    Stage 3 and the ECN says so. `sender=None` skips delivery and the ECN is
    only returned/written. `merge_hook` runs only for a validated APPROVED
    decision and receives the Decision; whatever it returns is recorded.
    """
    notes: list[str] = []

    # -- Stage 1 (Track A): the graph and the traversal --------------------
    graph.units.assert_si()
    graph.validate()
    walk: WalkResult | None = None
    if walk_graph:
        walk = walk_and_apply(graph)
        notes.extend(f"walk: {n}" for n in walk.notes)
        if not walk.member_ids:
            notes.append(
                "walk: the change reached no member; every member is evaluated "
                "regardless, and any FAIL will be flagged as a missed dependency"
            )

    # -- Stage 2 (Track B): the fast gate ----------------------------------
    decision = run_fast_gate(
        graph,
        threshold=threshold,
        planner=planner,
        before=before,
        decision_id=decision_id,
    )
    decision = enrich_decision(decision, graph, walk)

    # -- Stage 3 (Track B): the trusted artifact, escalations only ----------
    skyciv_attempted = False
    if skyciv is not None and decision.outcome is not Outcome.ERROR and (
        decision.outcome is Outcome.ESCALATED or skyciv_always
    ):
        skyciv_attempted = True
        decision = escalate(
            graph,
            decision,
            skyciv,
            report_dir=Path(report_dir) if report_dir is not None else None,
            always=skyciv_always,
            design_code=design_code,
        )
        # `escalate` copies through the contract's JSON round-trip, which
        # keeps source/hops (they are contract fields), but re-assert anyway.
        decision = enrich_decision(decision, graph, walk)
        if decision.skyciv_report is None:
            notes.append("skyciv: unavailable; the ECN cites no external report")
    elif skyciv is None and decision.outcome is Outcome.ESCALATED:
        notes.append("skyciv: no client configured; escalation carries no SkyCiv report")

    # -- Stage 4 (Track A): the notice --------------------------------------
    ecn = build_ecn(decision, graph=graph, walk=walk, ecn_id=ecn_id)
    ecn_text = render_text(ecn)
    ecn_markdown = render_markdown(ecn)

    svg: str | None = None
    if render_svg:
        try:
            svg = render_truss_svg(graph, decision, title=ecn.title)
        except Exception as e:  # a picture must never cost the notice
            notes.append(f"svg: not rendered ({type(e).__name__}: {e})")

    result = PipelineResult(
        graph=graph,
        decision=decision,
        ecn=ecn,
        walk=walk,
        ecn_text=ecn_text,
        ecn_markdown=ecn_markdown,
        svg=svg,
        skyciv_attempted=skyciv_attempted,
        notes=notes,
    )

    # -- Stage 4 (Track A): delivery ------------------------------------------
    if sender is not None and decision.outcome in set(notify_on):
        if not recipient or not sender_address:
            notes.append(
                "delivery: skipped -- sender_address and recipient are required"
            )
        else:
            result.delivery = deliver(
                ecn,
                decision,
                sender,
                sender_address=sender_address,
                recipient=recipient,
                svg=svg,
            )
            if result.delivery.error:
                notes.append(f"delivery: FAILED {result.delivery.error}")
    elif sender is not None:
        notes.append(
            f"delivery: not sent for outcome {decision.outcome.value}; ECN logged only"
        )

    # -- the only road to Main --------------------------------------------------
    if merge_hook is not None:
        if decision.outcome is Outcome.APPROVED and ecn.status is ECNStatus.APPROVED:
            decision.validate()                      # the gate, re-checked here
            result.merged = merge_hook(decision)
            notes.append(f"merge: hook ran for APPROVED decision {decision.id}")
        else:
            notes.append(
                f"merge: withheld -- outcome {decision.outcome.value}; nothing merges"
            )

    return result


__all__ = [
    "DEFAULT_NOTIFY_ON",
    "MergeHook",
    "PipelineResult",
    "enrich_decision",
    "run_pipeline",
]
