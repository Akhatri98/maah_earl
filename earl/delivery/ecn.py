"""Engineering Change Notice generation. [Track A, Sprint 3A]

plan.md, stage 4:

    "The ECN is generated directly by the agent from the pipeline's own
     decision (what changed, what's affected, approved or escalated) and
     references the SkyCiv report as its supporting evidence -- mirroring how
     a real ECN in practice typically attaches a calc report rather than
     containing the numbers itself."

and, on why it is not written by a model:

    "The report and ECN are templated, not generated freeform, so they're
     complete and consistent every run."

So there is no LLM anywhere in this module. The ECN is a fixed set of sections
filled from the Decision; the same decision renders byte-identical output every
time. "Complete and consistent every run" is a property of the code.

## What this takes as input, and why

Sprint 3A builds against a MOCKED decision (mock_decisions.py) so Track A does
not block on Track B. Decision is the only required input.

The graph and the walk are OPTIONAL enrichment. That is a deliberate response
to a gap in the Sprint 0 contract: decision.py states the design rule that a
Decision "must be SUFFICIENT TO WRITE THE ECN on its own", but two things a
real ECN wants are not on it --

  1. the Onshape document and branch the change was evaluated in (OnshapeRef
     lives only on DependencyGraph), and
  2. HOW each member came to be affected -- walker.Reach carries `distance`
     and `via`, and walker.explain() is documented as producing "one
     human-readable line for the ECN", but neither survives the contract
     boundary. MemberResult.is_affected is a bare bool, and on a statically
     indeterminate truss every member is affected, so that bool says nothing.

Rather than amend a shared contract unilaterally mid-sprint, both are accepted
as optional arguments and the ECN degrades honestly without them: it says the
provenance is unavailable instead of inventing a reason.

Sprint 4 closed that gap: contract 0.2.0 carries `Decision.source` and
`MemberResult.hops_from_change` / `reached_via`, and `earl.pipeline` fills them
from the graph and the walk before the decision crosses the boundary. So a
Decision alone IS now sufficient. The graph and walk remain accepted -- a walk
still gives the full path ("m7 -> m5"), which the contract fields do not carry.

## The one thing this module must never do

Render an approval the decision did not make. build_ecn() calls
Decision.validate() before reading anything -- the contracts README asks for it
"on produce AND on consume", and this is the consume side -- and then
independently re-checks the approve/violate relationship in _status_for().
Two checks for one rule is deliberate: this is the document a human signs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from ..contracts.common import CONTRACT_VERSION, SI_UNITS, Serializable, Units
from ..contracts.decision import Decision, MemberResult, MemberStatus, Outcome
from ..contracts.graph import DependencyGraph, OnshapeRef
from . import units as fmt


class ECNStatus(str, Enum):
    """The disposition a human reads first.

    Mirrors Outcome but is deliberately a separate vocabulary: Outcome is the
    pipeline's verdict, this is what the document says about the change. ERROR
    maps to BLOCKED rather than to anything resembling a finding -- a solver
    that did not run must never read as "reviewed and held".
    """

    APPROVED = "approved"
    HELD = "held"
    BLOCKED = "blocked"

    @property
    def headline(self) -> str:
        return {
            ECNStatus.APPROVED: "APPROVED - CHANGE MAY MERGE",
            ECNStatus.HELD: "HELD - ENGINEERING REVIEW REQUIRED",
            ECNStatus.BLOCKED: "BLOCKED - ANALYSIS INCOMPLETE",
        }[self]


@dataclass
class ECNLine(Serializable):
    """One row of the affected-items table: a member, its verdict, its
    provenance."""

    member_id: str
    status: MemberStatus
    safety_factor: float | None = None
    utilization: float | None = None
    stress_before: float | None = None
    stress_after: float | None = None
    capacity: float | None = None
    onshape_id: str | None = None
    # Two forms on purpose. The full string carries the whole path and belongs
    # in the governing-member block; the short one has to fit a table column
    # without being truncated mid-word, which is how a provenance claim turns
    # into an unreadable fragment.
    provenance: str | None = None        # "m5 is 1 hop(s) downstream via topology (m7 -> m5)"
    provenance_short: str | None = None  # "1 hop via topology"
    is_change_target: bool = False
    is_governing: bool = False

    @property
    def failed(self) -> bool:
        return self.status is MemberStatus.FAIL


@dataclass
class ECN(Serializable):
    """A complete Engineering Change Notice, ready to render or to log.

    Serializable because EARL is a *Ledger*: the JSON form is the change record
    the eval harness replays and the audit trail the project claims to keep,
    not merely an intermediate on the way to the text renderer.
    """

    id: str
    status: ECNStatus
    title: str

    decision_id: str
    change_id: str
    graph_id: str

    change_description: str = ""
    change_target: str | None = None
    value_before: str | None = None
    value_after: str | None = None

    disposition: str = ""
    safety_factor_threshold: float = 1.0
    violating_member_ids: list[str] = field(default_factory=list)
    lines: list[ECNLine] = field(default_factory=list)

    evidence: list[str] = field(default_factory=list)
    cross_check_note: str = ""
    sanity_note: str = ""

    source: OnshapeRef | None = None
    raised_at: str = ""
    solver: str = ""
    originator: str = "EARL (automated)"
    notes: list[str] = field(default_factory=list)

    units: Units = field(default_factory=lambda: SI_UNITS)
    schema_version: str = CONTRACT_VERSION

    @property
    def governing(self) -> ECNLine | None:
        for line in self.lines:
            if line.is_governing:
                return line
        return None

    @property
    def failed_lines(self) -> list[ECNLine]:
        return [line for line in self.lines if line.failed]

    @property
    def requires_signature(self) -> bool:
        return self.status is not ECNStatus.APPROVED


# --------------------------------------------------------------------------
# Building
# --------------------------------------------------------------------------

def _status_for(decision: Decision) -> ECNStatus:
    """Map the outcome, then independently re-check the rule behind it.

    Decision.validate() has already run by the time this is called. Checking
    again is not defensiveness for its own sake: this function decides the word
    a human sees at the top of a document they sign, so it does not delegate
    that to an upstream call having been made correctly.
    """
    if decision.outcome is Outcome.ERROR:
        return ECNStatus.BLOCKED

    below = [
        r for r in decision.member_results
        if r.safety_factor is not None
        and r.safety_factor < decision.safety_factor_threshold
    ]
    if decision.outcome is Outcome.APPROVED:
        if below:
            raise ValueError(
                f"refusing to write an APPROVED ECN for decision {decision.id!r}: "
                f"members {sorted(r.member_id for r in below)} are below the "
                f"threshold {decision.safety_factor_threshold}"
            )
        return ECNStatus.APPROVED
    return ECNStatus.HELD


def _disposition(decision: Decision, status: ECNStatus, failed: int) -> str:
    """The one-paragraph plain statement of what happens to this change."""
    threshold = decision.safety_factor_threshold
    if status is ECNStatus.BLOCKED:
        return (
            "The analysis did not complete, so no structural determination has "
            "been made. This change is NOT approved and must not merge. This is "
            "a tooling failure, not a finding about the structure: "
            f"{decision.error_message or 'no detail supplied'}"
        )
    if status is ECNStatus.APPROVED:
        return (
            "All evaluated members meet the safety-factor threshold of "
            f"{threshold:.2f}. The change is auto-approved and the evaluation "
            "branch may merge. This notice is logged as the change record."
        )
    plural = "member falls" if failed == 1 else "members fall"
    return (
        f"{failed} {plural} below the safety-factor threshold of "
        f"{threshold:.2f}. The change is HELD: nothing merges, and the "
        "evaluation branch stays unmerged pending a human decision. The "
        "computed values behind this are given below and in the supporting "
        "analysis."
    )


def _provenance(
    member_id: str,
    walk: object | None,
    graph: DependencyGraph | None,
    result: MemberResult,
) -> tuple[str | None, str | None]:
    """How this member came to be affected: (full, short).

    Prefers the walk (distance + edge kind + path), then the decision's own
    `hops_from_change` / `reached_via` (contract 0.2.0), then the graph's flat
    affected list, then the bare bool -- saying plainly when it does not know,
    rather than implying a dependency it cannot evidence.
    """
    if walk is not None and hasattr(walk, "reach"):
        reach = walk.reach(member_id)
        if reach is None:
            return ("not reached by the dependency walk", "not reached")
        full = walk.explain(member_id) if hasattr(walk, "explain") else None
        if getattr(reach, "is_seed", False):
            return (full, "change target")
        hops = getattr(reach, "distance", None)
        via = getattr(reach, "via", None)
        via_name = via.value if via is not None else "dependency"
        plural = "hop" if hops == 1 else "hops"
        return (full, f"{hops} {plural} via {via_name}")

    if result.hops_from_change is not None:
        hops = result.hops_from_change
        if hops == 0:
            return (f"{member_id} is the change target", "change target")
        via_name = result.reached_via.value if result.reached_via is not None else "dependency"
        plural = "hop" if hops == 1 else "hops"
        return (
            f"{member_id} is {hops} hop(s) downstream via {via_name}",
            f"{hops} {plural} via {via_name}",
        )

    if graph is not None and graph.affected_member_ids:
        downstream = member_id in graph.affected_member_ids
        return (
            "downstream of the change" if downstream
            else "not downstream of the change",
            "downstream" if downstream else "not downstream",
        )
    if result.is_affected:
        return ("downstream of the change (no traversal detail available)",
                "downstream")
    return (None, None)


def _evidence(decision: Decision) -> list[str]:
    """What a reviewer can go and read. Never fabricated."""
    report = decision.skyciv_report
    if report is None:
        if decision.outcome is Outcome.ESCALATED:
            return [
                "No SkyCiv report is attached to this notice. An escalated "
                "change is expected to carry one; treat its absence as an open "
                "item, not as a clean result."
            ]
        return ["No external analysis report was generated for this change."]

    lines = [f"SkyCiv analysis report {report.report_id}"]
    if report.design_code:
        lines.append(f"Design code: {report.design_code}")
    if report.url:
        lines.append(f"URL: {report.url}")
    if report.local_path:
        lines.append(f"File: {report.local_path}")
    if report.generated_at:
        lines.append(f"Generated: {report.generated_at}")
    if not report.url and not report.local_path:
        lines.append(
            "WARNING: the report is referenced by id only - no URL or file was "
            "supplied, so this notice cites evidence a reviewer cannot open."
        )
    return lines


def _cross_check_note(decision: Decision) -> str:
    """PyNite vs SkyCiv.

    decision.py says disagreement "must still be delivered, never swallowed",
    so a mismatch is stated as prominently as a match -- and an escalation with
    no cross-check at all is called out rather than left blank.
    """
    cc = decision.cross_check
    if not cc.performed:
        if decision.outcome is Outcome.ESCALATED:
            return (
                "NOT PERFORMED. This change was escalated on the internal "
                "solver alone, with no independent second-solver confirmation."
            )
        return "Not performed for this change."

    member = cc.member_id or "the governing member"
    delta = fmt.percent(cc.relative_difference, decimals=2)
    tol = fmt.percent(cc.tolerance, decimals=2)
    pynite = fmt.stress(cc.pynite_value, decision.units)
    skyciv = fmt.stress(cc.skyciv_value, decision.units)

    if cc.agrees:
        return (
            f"AGREE on {member}: internal solver {pynite} vs SkyCiv {skyciv} - "
            f"{delta} apart, within the {tol} tolerance."
        )
    return (
        f"*** DISAGREE on {member}: internal solver {pynite} vs SkyCiv "
        f"{skyciv} - {delta} apart, outside the {tol} tolerance. The two "
        "solvers do not confirm each other on this change, and the numbers "
        "below should not be relied on until that is resolved. ***"
    )


def _sanity_note(decision: Decision) -> str:
    checks = decision.sanity_checks

    def render(label: str, ok: bool | None, detail: str = "") -> str:
        if ok is None:
            return f"{label}: not run"
        return f"{label}: {'OK' if ok else 'FAILED'}{detail}"

    residual = ""
    if checks.max_residual_force is not None:
        residual = (
            f" (max joint residual "
            f"{fmt.force(checks.max_residual_force, decision.units)})"
        )

    parts = [
        render("Equilibrium (sum F = 0 at every joint)", checks.equilibrium_ok, residual),
        render("Linearity (2x load gives exactly 2x stress)", checks.linearity_ok),
    ]
    if checks.note:
        parts.append(checks.note)
    return "; ".join(parts)


def build_ecn(
    decision: Decision,
    *,
    graph: DependencyGraph | None = None,
    walk: object | None = None,
    ecn_id: str | None = None,
    title: str | None = None,
    originator: str = "EARL (automated)",
    now: str | None = None,
) -> ECN:
    """Turn a decision into a complete ECN.

    `graph` and `walk` are optional enrichment (see the module docstring); the
    decision alone is sufficient to produce a valid, honest notice.
    """
    # Consume-side guardrail. Runs before anything is read off the decision.
    decision.validate()
    decision.units.assert_si()

    status = _status_for(decision)
    governing = decision.governing_member

    lines: list[ECNLine] = []
    for result in _ordered(decision.member_results):
        full, short = _provenance(result.member_id, walk, graph, result)
        lines.append(
            ECNLine(
                member_id=result.member_id,
                status=result.status,
                safety_factor=result.safety_factor,
                utilization=result.utilization,
                stress_before=result.stress_before,
                stress_after=result.stress_after,
                capacity=result.capacity,
                onshape_id=result.onshape_id,
                provenance=full,
                provenance_short=short,
                is_change_target=(
                    graph is not None
                    and graph.change.target_id == result.member_id
                ),
                is_governing=(
                    governing is not None
                    and result.member_id == governing.member_id
                ),
            )
        )

    change = graph.change if graph is not None else None
    # The decision's own copy wins when both are present: it is the one that
    # was validated at the boundary, and the graph may be a different revision.
    source = decision.source or (graph.source if graph is not None else None)
    carries_hops = any(r.hops_from_change is not None for r in decision.member_results)

    notes: list[str] = []
    if graph is None and source is None and not carries_hops:
        notes.append(
            "No dependency graph was supplied with this decision, so the "
            "Onshape source and the traversal provenance are omitted."
        )
    elif graph is None:
        notes.append(
            "No dependency graph was supplied; the Onshape source and the "
            "per-member provenance are taken from the decision itself."
        )
    elif walk is None and not carries_hops:
        notes.append(
            "No traversal result was supplied, so affected members are listed "
            "without the dependency path that reached them."
        )

    return ECN(
        id=ecn_id or f"ECN-{decision.change_id.upper()}",
        status=status,
        title=title or _title(decision, status),
        decision_id=decision.id,
        change_id=decision.change_id,
        graph_id=decision.graph_id,
        change_description=(
            decision.change_description or (change.description if change else "")
        ),
        change_target=change.target_id if change else None,
        value_before=change.value_before if change else None,
        value_after=change.value_after if change else None,
        disposition=_disposition(decision, status, len(decision.violating_member_ids)),
        safety_factor_threshold=decision.safety_factor_threshold,
        violating_member_ids=list(decision.violating_member_ids),
        lines=lines,
        evidence=_evidence(decision),
        cross_check_note=_cross_check_note(decision),
        sanity_note=_sanity_note(decision),
        source=source,
        raised_at=now or decision.evaluated_at or _utc_now(),
        solver=" ".join(x for x in (decision.solver, decision.solver_version) if x),
        originator=originator,
        notes=notes,
        units=decision.units,
    )


def _title(decision: Decision, status: ECNStatus) -> str:
    verb = {
        ECNStatus.APPROVED: "Auto-approved",
        ECNStatus.HELD: "Held for review",
        ECNStatus.BLOCKED: "Blocked",
    }[status]
    summary = decision.change_description.strip() or f"change {decision.change_id}"
    return f"{verb}: {summary}"


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _ordered(results: list[MemberResult]) -> list[MemberResult]:
    """Failures first, then ascending safety factor, then member id.

    Reading order is a safety property here: the members that failed are what
    the notice exists to communicate, so they are never buried below a page of
    passing rows.
    """
    def key(r: MemberResult) -> tuple:
        failed = 0 if r.status is MemberStatus.FAIL else 1
        sf = r.safety_factor if r.safety_factor is not None else float("inf")
        digits = "".join(c for c in r.member_id if c.isdigit())
        return (failed, sf, int(digits) if digits else 0, r.member_id)

    return sorted(results, key=key)
