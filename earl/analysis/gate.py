"""The fast gate -- Stage 2 entry point, and the Track B half of Sprint 4. [Track B]

plan.md, stage 2: "the cheap, fast, always-on gate that runs on every change".
This module is the ONE function the rest of the pipeline calls to turn a
DependencyGraph into a Decision. It strings the Track B modules together in a
fixed order and, more importantly, it is where the plan's "LLM proposes, code
disposes" rule is made structural rather than hoped for:

  * THE PLAN CANNOT NARROW THE EVALUATION (R8).  A planner (rule-based or
    LLM) chooses which load case to lead with and whether to ADD self-weight.
    The gate always solves the planned case with the CONTRACT self-weight
    flag; a plan that asks for self-weight only adds a second variant, and
    a plan that asks for none changes nothing.  `focus_member_ids` is never
    read to decide anything -- Biject evaluates every member in graph.members.
    With `evaluate_all_load_cases` (the default) every other load case is
    solved too and Biject takes the worst, so a planner picking the mild
    case cannot hide a member that fails under the harsh one.  A planner that
    names an unknown load case, or crashes, is replaced by the rule-based
    plan and the substitution is written into the sanity note.
  * THE THRESHOLD IS CHECKED FIRST AND MISCONFIGURATION RAISES (R9).  A
    threshold below MIN_ALLOWED_THRESHOLD (or non-finite) is a ValueError
    before anything is solved.  It is not a solver crash and must never
    become an ERROR decision that someone downstream shrugs at.
  * A CRASH IS AN ERROR DECISION, NEVER AN APPROVAL OR A FINDING (R10).
    SolverError / ValueError from validation, planning or solving become
    `Outcome.ERROR` with a non-empty message and every member marked
    NOT_EVALUATED (safety_factor None, so `Decision.validate()` still holds
    and `violating_member_ids` is empty).  The decision is returned, not
    raised, so the caller always gets a contract object it can log and send.
  * THE BEFORE-STATE IS ISOLATED (R11).  When a before-graph is given it is
    solved per load case in its own try/except; a failure there sets
    stress_before to None and leaves a note.  It can never turn the analysis
    of the AFTER state into an ERROR.
  * EVERY RETURNED DECISION HAS PASSED `decision.validate()`.  The invariants
    in the contract are the backstop; the gate never bypasses them.

The function is deliberately free of any parameter that could force an
outcome: there is no `approve=`, no `skip_members=`, no per-call threshold
floor.  The only knobs are which planner proposes the load case, whether to
solve the other cases too, and the threshold, which itself has a hard floor.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..config import safety_factor_threshold
from ..contracts import (
    Decision,
    DependencyGraph,
    MemberResult,
    MemberStatus,
    Outcome,
    SanityChecks,
)
from .biject import MIN_ALLOWED_THRESHOLD, Verdict, evaluate
from .orchestrator import AnalysisPlan, Planner, RuleBasedPlanner
from .sanity import run_sanity_checks
from .solver import (
    SOLVER_NAME,
    SOLVER_VERSION,
    SolveResult,
    SolverError,
    effective_self_weight,
    solve,
)

# The errors that mean "the analysis could not be completed" and become an
# ERROR decision.  Anything else is a programming error and propagates.
ANALYSIS_ERRORS = (SolverError, ValueError)


def utc_now_iso() -> str:
    """ISO-8601 UTC timestamp, the format every Decision carries."""
    return datetime.now(timezone.utc).isoformat()


def resolve_threshold(threshold: float | None) -> float:
    """The threshold the gate will use: the argument, else the configured
    SAFETY_FACTOR_THRESHOLD.  Raises ValueError when it is not a finite number
    or is below MIN_ALLOWED_THRESHOLD -- plan.md's "SF < 1.0 can never be
    auto-approved" lives in code, and a misconfiguration is not a finding."""
    raw = safety_factor_threshold() if threshold is None else threshold
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"safety-factor threshold must be a number, got {raw!r}") from None
    if not math.isfinite(value):
        raise ValueError(f"safety-factor threshold must be finite, got {raw!r}")
    if value < MIN_ALLOWED_THRESHOLD:
        raise ValueError(
            f"safety-factor threshold {value} is below the floor {MIN_ALLOWED_THRESHOLD}; "
            "SF < 1.0 can never be auto-approved (plan.md), so this floor lives in code"
        )
    return value


def error_message(exc: BaseException) -> str:
    """R10: never empty, always names the exception type."""
    return f"{type(exc).__name__}: {str(exc) or 'no message'}"


def not_evaluated_results(graph: DependencyGraph) -> list[MemberResult]:
    """Every member as NOT_EVALUATED with safety_factor None, in graph order.
    This is what an ERROR decision carries: no number, so nothing to compare
    against the threshold, so `Decision.validate()` passes and nothing reads
    as a pass or a fail."""
    affected = set(graph.affected_member_ids)
    return [
        MemberResult(
            member_id=m.id,
            status=MemberStatus.NOT_EVALUATED,
            is_affected=m.id in affected,
            onshape_id=m.onshape_id,
        )
        for m in graph.members
    ]


def error_decision(
    graph: DependencyGraph,
    exc: BaseException,
    *,
    threshold: float,
    decision_id: str | None = None,
    load_case_id: str | None = None,
    sanity_checks: SanityChecks | None = None,
    evaluated_at: str | None = None,
) -> Decision:
    """An `Outcome.ERROR` Decision for `graph`, validated.  Used by the gate
    for every analysis failure; other stages may use it the same way."""
    decision = Decision(
        id=decision_id or f"dec-{graph.id}",
        graph_id=graph.id,
        change_id=graph.change.id,
        outcome=Outcome.ERROR,
        change_description=graph.change.description,
        safety_factor_threshold=threshold,
        member_results=not_evaluated_results(graph),
        violating_member_ids=[],
        sanity_checks=sanity_checks or SanityChecks(),
        load_case_id=load_case_id,
        solver=SOLVER_NAME,
        solver_version=SOLVER_VERSION,
        evaluated_at=evaluated_at or utc_now_iso(),
        error_message=error_message(exc),
    )
    decision.validate()
    return decision


# --------------------------------------------------------------------------
# Internals of one gate run
# --------------------------------------------------------------------------

@dataclass
class GateRun:
    """Everything the gate computed on the way to a Decision.  Not part of
    the contract; exposed so scripts and tests can inspect the plan and the
    raw SolveResults behind a verdict."""

    plan: AnalysisPlan
    planned_result: SolveResult
    # Every SolveResult of the AFTER graph that Biject saw, planned case first.
    results: list[SolveResult]
    # Before-state results keyed by load case id (only the ones that solved).
    before_results: dict[str, SolveResult] | None
    verdict: Verdict
    notes: list[str] = field(default_factory=list)


def _plan(graph: DependencyGraph, planner: Planner | None, notes: list[str]) -> AnalysisPlan:
    """Ask the planner; fall back to the rule-based plan when it names a
    load case the graph does not have or crashes with something other than
    the "no load cases" ValueError (which is a real analysis error and is
    left to propagate to the ERROR path)."""
    rules = RuleBasedPlanner()
    if planner is None:
        return rules.plan(graph)

    try:
        plan = planner.plan(graph)
    except ValueError:
        raise
    except Exception as e:  # a broken planner must not block the analysis
        notes.append(f"planner failed ({error_message(e)}); using rule-based plan")
        return rules.plan(graph)

    known = [lc.id for lc in graph.load_cases]
    if plan.load_case_id not in known:
        notes.append(
            f"planner named unknown load case {plan.load_case_id!r} "
            f"(known: {known}); using rule-based plan"
        )
        return rules.plan(graph)
    return plan


def _solve_after(
    graph: DependencyGraph, plan: AnalysisPlan, evaluate_all_load_cases: bool
) -> tuple[SolveResult, list[SolveResult]]:
    """R8: the planned case with the CONTRACT flag always; the plan's
    self-weight request adds a variant; every other load case when asked."""
    planned = solve(graph, plan.load_case_id)
    results = [planned]

    wants_weight = effective_self_weight(graph, plan.load_case_id, plan.include_self_weight)
    if wants_weight and not planned.include_self_weight:
        results.append(solve(graph, plan.load_case_id, include_self_weight=True))

    if evaluate_all_load_cases:
        for lc in graph.load_cases:
            if lc.id != plan.load_case_id:
                results.append(solve(graph, lc.id))
    return planned, results


def _solve_before(
    before: DependencyGraph,
    graph: DependencyGraph,
    load_case_ids: list[str],
    notes: list[str],
) -> dict[str, SolveResult]:
    """R11: one isolated solve per load case id present in BOTH graphs.
    Failures become notes, never errors."""
    out: dict[str, SolveResult] = {}
    try:
        before.units.assert_si()
        before.validate()
    except ValueError as e:
        for lc_id in load_case_ids:
            notes.append(f"before-state not analysed for {lc_id!r}: {error_message(e)}")
        return out

    before_ids = {lc.id for lc in before.load_cases}
    for lc_id in load_case_ids:
        if lc_id not in before_ids:
            notes.append(
                f"before-state not analysed for {lc_id!r}: load case not present in "
                f"before graph {before.id!r}"
            )
            continue
        try:
            out[lc_id] = solve(before, lc_id)
        except ANALYSIS_ERRORS as e:
            notes.append(f"before-state not analysed for {lc_id!r}: {error_message(e)}")
    return out


def _append_note(checks: SanityChecks, notes: list[str]) -> None:
    if not notes:
        return
    extra = "; ".join(notes)
    checks.note = f"{checks.note}; {extra}" if checks.note else extra


def run_gate(
    graph: DependencyGraph,
    *,
    threshold: float,
    planner: Planner | None = None,
    before: DependencyGraph | None = None,
    evaluate_all_load_cases: bool = True,
) -> tuple[GateRun, SanityChecks]:
    """The analysis behind `run_fast_gate`, without the Decision assembly and
    without the ERROR wrapping: raises SolverError / ValueError.  `threshold`
    must already have been through `resolve_threshold`."""
    graph.units.assert_si()
    graph.validate()
    notes: list[str] = []

    plan = _plan(graph, planner, notes)
    planned, results = _solve_after(graph, plan, evaluate_all_load_cases)

    before_results: dict[str, SolveResult] | None = None
    if before is not None:
        seen: list[str] = []
        for r in results:
            if r.load_case_id not in seen:
                seen.append(r.load_case_id)
        before_results = _solve_before(before, graph, seen, notes)

    checks = run_sanity_checks(graph, plan.load_case_id, planned)
    verdict = evaluate(graph, results, threshold, before=before_results)
    _append_note(checks, notes)
    return GateRun(plan, planned, results, before_results, verdict, notes), checks


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def run_fast_gate(
    graph: DependencyGraph,
    *,
    threshold: float | None = None,
    planner: Planner | None = None,
    before: DependencyGraph | None = None,
    evaluate_all_load_cases: bool = True,
    decision_id: str | None = None,
) -> Decision:
    """Stage 2: DependencyGraph in, validated Decision out.

    Order: threshold (ValueError on misconfiguration, raised) -> SI + validate
    -> plan -> solve the planned case (+ self-weight variant if the plan adds
    it, + every other load case when `evaluate_all_load_cases`) -> before
    graph per load case (isolated) -> sanity checks on the planned case ->
    Biject over every result -> Decision -> `decision.validate()`.

    SolverError / ValueError from validation, planning or solving become an
    ERROR decision (never raised).  `decision.load_case_id` is the planned
    case; the member notes name the governing case when it differs.
    """
    threshold = resolve_threshold(threshold)     # R9: before anything else
    evaluated_at = utc_now_iso()
    decision_id = decision_id or f"dec-{graph.id}"

    try:
        run, checks = run_gate(
            graph,
            threshold=threshold,
            planner=planner,
            before=before,
            evaluate_all_load_cases=evaluate_all_load_cases,
        )
    except ANALYSIS_ERRORS as e:
        return error_decision(
            graph, e, threshold=threshold, decision_id=decision_id, evaluated_at=evaluated_at
        )

    verdict = run.verdict
    decision = Decision(
        id=decision_id,
        graph_id=graph.id,
        change_id=graph.change.id,
        outcome=verdict.outcome,
        change_description=graph.change.description,
        safety_factor_threshold=verdict.threshold,
        member_results=verdict.member_results,
        violating_member_ids=list(verdict.violating_member_ids),
        sanity_checks=checks,
        load_case_id=run.plan.load_case_id,
        solver=SOLVER_NAME,
        solver_version=SOLVER_VERSION,
        evaluated_at=evaluated_at,
    )
    decision.validate()
    return decision


__all__ = [
    "ANALYSIS_ERRORS",
    "GateRun",
    "error_decision",
    "error_message",
    "not_evaluated_results",
    "resolve_threshold",
    "run_gate",
    "run_fast_gate",
    "utc_now_iso",
]
