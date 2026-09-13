"""Selection-required baseline, specified in RELIABILITY.md before evaluation.

It gets the same model and numerical tool, but must name members to verify
before receiving results. Selection is unconstrained: choosing every member
is legal. The default is a declared local rule, NOT a measured LLM agent.
The full stiffness system is solved for physical correctness; only selected
member result/capacity objects cross this verification API. Global solver
sanity checks necessarily recover all stresses inside the numerical worker.
"""

from __future__ import annotations

from dataclasses import dataclass

from earl.analysis import llm, solver
from earl.analysis.capacity import member_capacity
from earl.analysis.gate import error_decision
from earl.contracts import (
    Decision, DependencyGraph, EscalationReason, MemberResult, MemberStatus, Outcome, TargetKind,
)


@dataclass(frozen=True)
class Selection:
    member_ids: list[str]
    provenance: str
    rationale: str


def _physical_model(graph: DependencyGraph) -> dict:
    model = graph.to_dict()
    return {key: model[key] for key in ("nodes", "members", "sections", "materials", "load_cases",
                                       "units", "hard_floor", "design_target")}


def select_members(before: DependencyGraph, after: DependencyGraph, *, allow_live: bool = False) -> Selection:
    before.validate()
    after.validate()
    ids = {m.id for m in after.members}
    delta = {key: value for key, value in after.change.to_dict().items()
             if key not in {"description", "value_before", "value_after"}}
    answer = llm._provider("baseline_select", {
        "change": delta,
        "before_model": _physical_model(before),
        "after_model": _physical_model(after),
    }, "Select which surviving members to request numerical verification for after this edit. "
       "You may select any subset, including every member. Return JSON {member_ids: [IDs]}. "
       "Only select verification scope; do not predict results or make acceptance decisions.", allow_live=allow_live)
    if (isinstance(answer, dict) and set(answer) == {"member_ids"} and isinstance(answer["member_ids"], list)
            and all(isinstance(mid, str) and mid in ids for mid in answer["member_ids"])):
        selected = [m.id for m in after.members if m.id in set(answer["member_ids"])]
        return Selection(selected, "provider/recorded member selection", "No subset size limit was imposed.")
    change = after.change
    touched_nodes = set()
    if change.target_kind is TargetKind.MEMBER:
        target = before.member(change.target_id)
        touched_nodes.update((target.start_node, target.end_node))
    elif change.target_kind is TargetKind.LOAD:
        for graph in (before, after):
            for case in graph.load_cases:
                for load in case.point_loads:
                    if load.id == change.target_id:
                        touched_nodes.add(load.node_id)
    else:
        return Selection([m.id for m in after.members], "rule-based baseline", "Global edit: select all members.")
    selected = [m.id for m in after.members if m.id == change.target_id or
                {m.start_node, m.end_node} & touched_nodes]
    return Selection(selected, "rule-based baseline", "Changed member and incident members, or members at old/new load nodes.")


def verify_selected(graph: DependencyGraph, selection: Selection) -> Decision:
    """The same bounded solver; no unselected member numbers are returned."""
    graph.validate()
    selected = set(selection.member_ids)
    if not selected <= {m.id for m in graph.members}:
        raise ValueError("baseline selected an unknown member")
    try:
        solved = solver.solve(graph, graph.change.load_case_id)
    except solver.SolveError as exc:
        return error_decision(decision_id=f"baseline-{graph.change.id}", graph_id=graph.id,
                              change_id=graph.change.id, description=graph.change.description, message=str(exc))
    if solved.sanity_checks.equilibrium_ok is not True or solved.sanity_checks.linearity_ok is not True:
        return error_decision(decision_id=f"baseline-{graph.change.id}", graph_id=graph.id,
                              change_id=graph.change.id, description=graph.change.description, message="Solver sanity failed.")
    decision = Decision(id=f"baseline-{graph.change.id}", graph_id=graph.id, change_id=graph.change.id,
                        change_description=graph.change.description, outcome=Outcome.ESCALATED,
                        hard_floor=graph.hard_floor, design_target=graph.design_target,
                        safety_factor_threshold=graph.design_target, sanity_checks=solved.sanity_checks,
                        expected_member_ids=[m.id for m in graph.members], load_case_id=solved.load_case_id,
                        source_provenance=selection.provenance, solver_version=solved.solver_version)
    for member in graph.members:
        if member.id not in selected:
            decision.member_results.append(MemberResult(member.id, MemberStatus.NOT_EVALUATED,
                                                        note="Not selected for verification; no member results exposed."))
            continue
        force = solved.axial_forces[member.id]
        cap = member_capacity(graph, member.id, force)
        status = MemberStatus.NOT_EVALUATED if cap.force_capacity is None else (
            MemberStatus.FAIL if cap.safety_factor is not None and cap.safety_factor < decision.threshold else MemberStatus.PASS)
        decision.member_results.append(MemberResult(
            member.id, status, stress_after=solved.stresses[member.id], axial_force_after=force,
            capacity=cap.stress_capacity, safety_factor=cap.safety_factor, capacity_mode=cap.mode,
            zero_demand=cap.zero_demand, is_affected=True, note=cap.note,
        ))
    # The comparison module, not the selector, sets the acceptance policy.
    from earl.analysis.gate import _policy
    _policy(decision)
    decision.validate()
    return decision
