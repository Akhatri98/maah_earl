"""Sprint 0 self-check: prove both contracts are usable and round-trip cleanly.

Builds the 10-bar truss (the project's validated benchmark and demo structure)
as a DependencyGraph, builds a matching Decision, and checks that:

  1. both validate,
  2. both survive a JSON round-trip with types intact,
  3. the non-negotiable rule actually fires -- an APPROVED decision carrying a
     member below the safety threshold is REJECTED.

Run:  .venv/Scripts/python.exe scripts/contract_selfcheck.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from earl.contracts import (  # noqa: E402
    ChangeEvent,
    ChangeKind,
    CrossCheck,
    Decision,
    DependencyGraph,
    Edge,
    EdgeKind,
    LoadCase,
    Material,
    Member,
    MemberResult,
    MemberStatus,
    Node,
    OnshapeRef,
    Outcome,
    PointLoad,
    SanityChecks,
    Section,
    SkyCivReport,
    SupportType,
)

# -- 10-bar truss, converted to SI ------------------------------------------
# Classic benchmark is stated in imperial (360 in bays, E = 1e7 psi, P = 100
# kips). The project contract is SI, so it is converted once, here, rather
# than being half-converted in two places later.
IN = 0.0254            # in -> m
E_STEEL = 6.895e10     # 1e7 psi -> Pa
YIELD = 2.48e8         # ~36 ksi -> Pa
AREA = 1.0 * IN**2     # 1 in^2 -> m^2
P = 444_822.0          # 100 kips -> N


def build_graph() -> DependencyGraph:
    nodes = [
        Node("n1", 720 * IN, 360 * IN),
        Node("n2", 720 * IN, 0.0),
        Node("n3", 360 * IN, 360 * IN),
        Node("n4", 360 * IN, 0.0),
        Node("n5", 0.0, 360 * IN, support=SupportType.PIN),
        Node("n6", 0.0, 0.0, support=SupportType.PIN),
    ]

    connectivity = [
        ("m1", "n5", "n3"), ("m2", "n3", "n1"), ("m3", "n6", "n4"),
        ("m4", "n4", "n2"), ("m5", "n3", "n4"), ("m6", "n1", "n2"),
        ("m7", "n5", "n4"), ("m8", "n6", "n3"), ("m9", "n3", "n2"),
        ("m10", "n4", "n1"),
    ]
    members = [
        Member(mid, a, b, section_id="sec_1in2", material_id="mat_steel",
               onshape_id=f"onshape-part-{mid}")
        for mid, a, b in connectivity
    ]

    graph = DependencyGraph(
        id="graph-selfcheck-001",
        change=ChangeEvent(
            id="chg-001",
            kind=ChangeKind.FEATURE_EDIT,
            description="Reduced cross-section of member m5 from 1.0 in^2 to 0.4 in^2",
            target_id="m5",
            value_before="1.0 in^2",
            value_after="0.4 in^2",
        ),
        source=OnshapeRef(
            document_id="74352477fea92ae1dadf61d6",
            workspace_id="7121b248aa290fdccdc1afa6",
            element_id="72f445c1fb5399de58418375",   # Assembly 1
            branch_name="earl-eval-chg-001",
        ),
        nodes=nodes,
        members=members,
        materials=[Material("mat_steel", "A36 Steel", E_STEEL, YIELD, density=7850.0)],
        sections=[Section("sec_1in2", "1 in^2 bar", AREA)],
        load_cases=[
            LoadCase(
                id="lc_1",
                name="Benchmark load case",
                point_loads=[
                    PointLoad("p1", "n2", fy=-P),
                    PointLoad("p2", "n4", fy=-P),
                ],
            )
        ],
        edges=[
            Edge("m5", "m9", EdgeKind.TOPOLOGY),
            Edge("m5", "m10", EdgeKind.TOPOLOGY),
            Edge("m5", "m1", EdgeKind.LOAD_PATH),
            Edge("m5", "m3", EdgeKind.LOAD_PATH),
        ],
        affected_member_ids=["m5", "m9", "m10", "m1", "m3"],
        affected_node_ids=["n3", "n4"],
    )
    return graph


def build_decision(graph: DependencyGraph) -> Decision:
    """An escalation: m5 was thinned, so it drops below the threshold."""
    results = []
    for m in graph.members:
        affected = m.id in graph.affected_member_ids
        if m.id == "m5":
            sf = 0.62
            status = MemberStatus.FAIL
        elif affected:
            sf = 1.85
            status = MemberStatus.PASS
        else:
            sf = 3.10
            status = MemberStatus.PASS
        results.append(
            MemberResult(
                member_id=m.id,
                status=status,
                stress_before=1.10e8,
                stress_after=YIELD / sf,
                safety_factor=sf,
                utilization=1 / sf,
                capacity=YIELD,
                is_affected=affected,
                onshape_id=m.onshape_id,
            )
        )

    cross = CrossCheck(member_id="m5", pynite_value=4.0e8, skyciv_value=4.03e8)
    cross.evaluate()

    return Decision(
        id="dec-001",
        graph_id=graph.id,
        change_id=graph.change.id,
        outcome=Outcome.ESCALATED,
        change_description=graph.change.description,
        member_results=results,
        violating_member_ids=["m5"],
        sanity_checks=SanityChecks(
            equilibrium_ok=True, max_residual_force=1.2e-9, linearity_ok=True
        ),
        cross_check=cross,
        skyciv_report=SkyCivReport(
            report_id="sc-report-001",
            url="https://platform.skyciv.com/report/sc-report-001",
            design_code="AISC 360-16",
        ),
        load_case_id="lc_1",
        solver="PyNite",
    )


def main() -> int:
    ok = True

    graph = build_graph()
    graph.validate()
    print(f"graph      : {len(graph.nodes)} nodes, {len(graph.members)} members, "
          f"{len(graph.affected_member_ids)} affected  [validate OK]")

    regraph = DependencyGraph.from_json(graph.to_json())
    regraph.validate()
    assert regraph.to_dict() == graph.to_dict(), "graph round-trip mismatch"
    assert isinstance(regraph.nodes[4].support, SupportType), "enum lost in round-trip"
    assert isinstance(regraph.change.kind, ChangeKind), "enum lost in round-trip"
    print("graph      : JSON round-trip OK (enums and nested types intact)")

    decision = build_decision(graph)
    decision.validate()
    gov = decision.governing_member
    print(f"decision   : {decision.outcome.value}, governing member "
          f"{gov.member_id} SF={gov.safety_factor}  [validate OK]")

    redecision = Decision.from_json(decision.to_json())
    redecision.validate()
    assert redecision.to_dict() == decision.to_dict(), "decision round-trip mismatch"
    assert isinstance(redecision.outcome, Outcome), "enum lost in round-trip"
    assert redecision.skyciv_report is not None, "optional nested object lost"
    print("decision   : JSON round-trip OK (optional nested objects intact)")

    print(f"cross-check: PyNite vs SkyCiv differ by "
          f"{decision.cross_check.relative_difference:.2%} -> "
          f"agrees={decision.cross_check.agrees}")

    # The rule that must never be bypassable.
    bad = build_decision(graph)
    bad.outcome = Outcome.APPROVED
    try:
        bad.validate()
    except ValueError as e:
        print(f"guardrail  : APPROVED-below-threshold correctly REJECTED\n"
              f"             -> {e}")
    else:
        ok = False
        print("guardrail  : *** FAILED -- unsafe approval was accepted ***")

    # Escalation must be backed by a real number, not a vibe.
    unjustified = build_decision(graph)
    for r in unjustified.member_results:
        r.safety_factor, r.status = 2.0, MemberStatus.PASS
    unjustified.violating_member_ids = []
    try:
        unjustified.validate()
    except ValueError as e:
        print(f"guardrail  : unjustified ESCALATED correctly REJECTED\n"
              f"             -> {e}")
    else:
        ok = False
        print("guardrail  : *** FAILED -- baseless escalation was accepted ***")

    print("\nALL CHECKS PASSED" if ok else "\nCHECKS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
