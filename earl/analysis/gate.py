"""Biject: deterministic structural acceptance policy, with no LLM or I/O.

This module is the enforcement layer. It compares computed results with two
thresholds; it never interprets prose or offers a manual override. Failed or
untrusted analysis is ERROR. A failed design target, incomplete capacity
evaluation, or independent solver disagreement is ESCALATED. Only complete,
sanity-checked results above both thresholds can be APPROVED.
"""

from __future__ import annotations

from copy import deepcopy

from earl.contracts import (
    CrossCheck, Decision, DependencyGraph, EscalationReason, MemberResult,
    MemberStatus, Outcome, SkyCivReport,
)
from .capacity import CapacityResult, capacities
from .sanity import combine
from .solver import SOLVER_VERSION, SolveResult, model_fingerprint


def error_decision(*, decision_id: str, graph_id: str, change_id: str,
                   description: str, message: str) -> Decision:
    decision = Decision(id=decision_id, graph_id=graph_id, change_id=change_id,
                        change_description=description, outcome=Outcome.ERROR,
                        error_message=message, solver_version=SOLVER_VERSION)
    decision.validate()
    return decision


def _policy(decision: Decision) -> None:
    """A comparison-only policy shared by first verdict and artifact recheck."""
    if decision.error_message:
        decision.outcome = Outcome.ERROR
        decision.escalation_reason = None
        return
    below_floor = [r for r in decision.member_results if r.safety_factor is not None
                   and r.safety_factor < decision.hard_floor]
    below_target = [r for r in decision.member_results if r.safety_factor is not None
                    and r.safety_factor < decision.threshold]
    decision.violating_member_ids = [r.member_id for r in below_target]
    incomplete = any(r.status is MemberStatus.NOT_EVALUATED for r in decision.member_results)
    reason = None
    if decision.cross_check.agrees is False:
        reason = EscalationReason.CROSS_CHECK_DISAGREEMENT
    elif below_floor:
        reason = EscalationReason.BELOW_HARD_FLOOR
    elif below_target:
        reason = EscalationReason.BELOW_DESIGN_TARGET
    elif incomplete:
        reason = EscalationReason.NOT_EVALUATED
    decision.escalation_reason = reason
    decision.outcome = Outcome.ESCALATED if reason else Outcome.APPROVED


def evaluate(
    before_graph: DependencyGraph, after_graph: DependencyGraph,
    before: SolveResult, after: SolveResult, *, decision_id: str = "decision",
    before_capacities: dict[str, CapacityResult] | None = None,
    after_capacities: dict[str, CapacityResult] | None = None,
) -> Decision:
    """Consume validated models/solves and produce a validated Decision."""
    before_graph.validate()
    after_graph.validate()
    before.validate()
    after.validate()
    if before.load_case_id != after.load_case_id:
        raise ValueError("before and after must use the same selected load case")
    for graph, result in ((before_graph, before), (after_graph, after)):
        if result.model_fingerprint != model_fingerprint(graph, result.load_case_id):
            raise ValueError("gate cannot use results for another model")
    if before_graph.hard_floor != after_graph.hard_floor or before_graph.design_target != after_graph.design_target:
        raise ValueError("a design edit cannot weaken acceptance policy")
    checks = combine(before.sanity_checks, after.sanity_checks)
    if not checks.equilibrium_ok or not checks.linearity_ok:
        decision = error_decision(
            decision_id=decision_id, graph_id=after_graph.id,
            change_id=after_graph.change.id, description=after_graph.change.description,
            message="Equilibrium or load-linearity verification failed or was not completed.",
        )
        decision.sanity_checks = checks
        decision.validate()
        return decision

    before_caps = before_capacities if before_capacities is not None else capacities(before_graph, before.axial_forces)
    after_caps = after_capacities if after_capacities is not None else capacities(after_graph, after.axial_forces)
    decision = Decision(
        id=decision_id, graph_id=after_graph.id, change_id=after_graph.change.id,
        outcome=Outcome.ERROR, change_description=after_graph.change.description,
        hard_floor=after_graph.hard_floor, design_target=after_graph.design_target,
        safety_factor_threshold=after_graph.design_target, sanity_checks=checks,
        load_case_id=after.load_case_id, solver_version=after.solver_version,
        expected_member_ids=[m.id for m in after_graph.members],
        source_provenance=after_graph.provenance,
    )
    for member in after_graph.members:
        cap = after_caps.get(member.id)
        previous = before_caps.get(member.id)
        sf = cap.safety_factor if cap else None
        force = after.axial_forces.get(member.id)
        if force is None or cap is None or cap.force_capacity is None:
            status = MemberStatus.NOT_EVALUATED
        else:
            status = MemberStatus.FAIL if sf is not None and sf < decision.threshold else MemberStatus.PASS
        try:
            old_area = before_graph.section(before_graph.member(member.id).section_id).area
        except KeyError:
            old_area = None
        decision.member_results.append(MemberResult(
            member_id=member.id, status=status,
            stress_before=before.stresses.get(member.id), stress_after=after.stresses.get(member.id),
            axial_force_after=force, capacity=cap.stress_capacity if cap else None,
            safety_factor=sf, utilization=(1 / sf if sf and sf > 0 else
                                          0.0 if cap and cap.zero_demand else None),
            is_affected=member.id in after_graph.affected_member_ids,
            onshape_id=member.onshape_id,
            note=cap.note if cap else "Capacity was not evaluated.",
            safety_factor_before=previous.safety_factor if previous else None,
            capacity_before=previous.stress_capacity if previous else None,
            area_before=old_area, area_after=after_graph.section(member.section_id).area,
            capacity_mode=cap.mode if cap else "not_evaluated",
            slenderness=cap.slenderness if cap else None,
            zero_demand=cap.zero_demand if cap else False,
        ))
    _policy(decision)
    decision.validate()
    return decision


def attach_cross_check(decision: Decision, report: SkyCivReport,
                       cross_check: CrossCheck) -> Decision:
    """Disagreement may escalate an approval; it can never clear a violation."""
    decision.validate()
    result = deepcopy(decision)
    result.skyciv_report = report
    result.cross_check = cross_check
    _policy(result)
    result.validate()
    return result
