"""Mocked Track B output, for building the ECN against. [Track A, Sprint 3A]

sprint_timeline.md, Sprint 3A:

    "ECN template -- agent-generated from the decision-object schema, built and
     tested against **mocked** decision output so it doesn't block on Track B"

This is that mock. It is the Track B stand-in and nothing else: no solver runs
here, and this module is expected to be retired at Sprint 4 when the real
`earl.analysis` output replaces it.

## The numbers are real, not invented

Every stress and safety factor below was computed with a direct-stiffness
solve of the 10-bar truss and then frozen into a literal. Fabricated numbers
would have been faster and would have quietly taught the ECN the wrong
magnitudes -- and a demo whose numbers do not survive contact with Track B's
solver is worse than no demo.

The solve reproduces the published Haftka & Gurdal / Rajan optimum weight of
5060.9 lb to within 0.1 lb, which is the same benchmark Track B validates
PyNite against in Sprint 1B. So when the real solver arrives, these values are
what it should reproduce.

## Areas: the published optimum, NOT a uniform 1 in^2

`earl/ingestion/benchmark.py` currently gives every member `DEFAULT_AREA =
1 in^2` while keeping the benchmark's 100-kip loads and 25-ksi allowable. At
that combination EVERY member of the truss fails before any change is made
(safety factors 0.12 to 0.70), because the 10-bar benchmark is a weight-
MINIMIZATION problem whose answer is a set of ten different areas spanning
0.1 to 30.5 in^2 -- a uniform 1 in^2 is the starting point of that
optimization, not its result.

This mock therefore uses the published optimum areas, under which the truss
starts safe (governing member m5 at safety factor 1.000, an active stress
constraint exactly as the literature reports). That is the only baseline where
"approve vs escalate" has two possible answers. See the note in the Sprint 3A
handover; the fix belongs to `benchmark.py`, not here.

## The scenarios

`escalated()` is the one the demo should lead on. An engineer shrinks **m7**,
and m7 itself remains fine (safety factor 1.10) -- while **m5**, which they did
not touch, drops to 0.75. That is a dropped domino in the literal sense
plan.md means: the unsafe member is not the changed one, so checking "is the
thing I edited still OK?" returns yes and misses it.
"""

from __future__ import annotations

from ..contracts.decision import (
    CrossCheck,
    Decision,
    MemberResult,
    MemberStatus,
    Outcome,
    SanityChecks,
    SkyCivReport,
)
from ..contracts.graph import (
    ChangeEvent,
    ChangeKind,
    DependencyGraph,
    Edge,
    EdgeKind,
    LoadCase,
    Material,
    Member,
    Node,
    OnshapeRef,
    PointLoad,
    Section,
    SupportType,
)

# -- constants shared with earl/ingestion/benchmark.py ----------------------

IN = 0.0254
PSI_TO_PA = 6894.757
KIP_TO_N = 4448.222

E_ALUMINIUM = 1.0e7 * PSI_TO_PA          # 6.895e10 Pa
YIELD_STRENGTH = 50_000 * PSI_TO_PA      # 3.4474e8 Pa -- Fy
ALLOWABLE_STRESS = 25_000 * PSI_TO_PA    # 1.7237e8 Pa -- member capacity (Biject)
DENSITY = 2768.0
BENCHMARK_LOAD = 100.0 * KIP_TO_N        # 444_822 N

# Published stress-constrained optimum, in^2 (Haftka & Gurdal / Rajan).
OPTIMUM_AREAS: dict[str, float] = {
    "m1": 30.52, "m2": 0.10, "m3": 23.20, "m4": 15.22, "m5": 0.10,
    "m6": 0.55, "m7": 7.46, "m8": 21.04, "m9": 21.53, "m10": 0.10,
}

CONNECTIVITY: list[tuple[str, str, str]] = [
    ("m1", "n5", "n3"), ("m2", "n3", "n1"), ("m3", "n6", "n4"),
    ("m4", "n4", "n2"), ("m5", "n3", "n4"), ("m6", "n1", "n2"),
    ("m7", "n5", "n4"), ("m8", "n6", "n3"), ("m9", "n3", "n2"),
    ("m10", "n4", "n1"),
]

NODE_POSITIONS: dict[str, tuple[float, float]] = {
    "n1": (720 * IN, 360 * IN), "n2": (720 * IN, 0.0),
    "n3": (360 * IN, 360 * IN), "n4": (360 * IN, 0.0),
    "n5": (0.0, 360 * IN), "n6": (0.0, 0.0),
}

SUPPORTS = {"n5": SupportType.PIN, "n6": SupportType.PIN}
LOADED_NODES = ("n2", "n4")

# -- frozen solver output ---------------------------------------------------
# (member, stress_before Pa, stress_after Pa, axial_force_after N, safety factor)
_Row = tuple[str, float, float, float, float]

_APPROVED_ROWS: list[_Row] = [
    ("m1", 4.577602e7, 6.971452e7, 8.995405e5, 2.472497),
    ("m2", -9.035888e6, -3.481660e6, -2.246228e2, 49.507682),
    ("m3", -5.865597e7, -5.877639e7, -8.797482e5, 2.932622),
    ("m4", -4.536001e7, -4.532351e7, -4.450468e5, 3.803080),
    ("m5", 1.722923e8, 1.499090e8, 9.671528e3, 1.149824),
    ("m6", -1.642889e6, -6.330291e5, -2.246228e2, 272.292253),
    ("m7", 1.272684e8, 1.277980e8, 6.150783e5, 1.348761),
    ("m8", -4.756224e7, -4.737446e7, -6.430688e5, 3.638436),
    ("m9", 4.534806e7, 4.531158e7, 6.293912e5, 3.804081),
    ("m10", 1.277868e7, 4.923811e6, 3.176646e2, 35.007218),
]

_ESCALATED_ROWS: list[_Row] = [
    ("m1", 4.577602e7, 4.600946e7, 9.059393e5, 3.746380),
    ("m2", -9.035888e6, -2.319995e7, -1.496768e3, 7.429710),
    ("m3", -5.865597e7, -5.834888e7, -8.733493e5, 2.954108),
    ("m4", -4.536001e7, -4.545307e7, -4.463189e5, 3.792240),
    ("m5", 1.722923e8, 2.293728e8, 1.479821e4, 0.751479),
    ("m6", -1.642889e6, -4.218173e6, -1.496768e3, 40.863406),
    ("m7", 1.272684e8, 1.565578e8, 6.060289e5, 1.100992),
    ("m8", -4.756224e7, -4.804112e7, -6.521181e5, 3.587946),
    ("m9", 4.534806e7, 4.544110e7, 6.311903e5, 3.793238),
    ("m10", 1.277868e7, 3.280969e7, 2.116750e3, 5.253599),
]

_EDGE_ROWS: list[_Row] = [
    ("m1", 4.577602e7, 4.572161e7, 9.002715e5, 3.769966),
    ("m2", -9.035888e6, 8.548821e6, 5.515358e2, 20.162887),
    ("m3", -5.865597e7, -5.872755e7, -8.790171e5, 2.935061),
    ("m4", -4.536001e7, -4.524447e7, -4.442706e5, 3.809724),
    ("m5", 1.722923e8, 1.732711e8, 1.117876e4, 0.994793),
    ("m6", -1.642889e6, 1.554331e6, 5.515358e2, 110.895881),
    ("m7", 1.272684e8, 1.275832e8, 6.140444e5, 1.351032),
    ("m8", -4.756224e7, -4.745062e7, -6.441027e5, 3.632596),
    ("m9", 4.534806e7, 8.115474e7, 6.282935e5, 2.123954),
    ("m10", 1.277868e7, -1.208986e7, -7.799894e2, 14.257314),
]


# --------------------------------------------------------------------------
# Graph (so the ECN can be exercised with real traversal provenance)
# --------------------------------------------------------------------------

def _topology_edges() -> list[Edge]:
    """Members sharing a node depend on each other, both directions.

    Mirrors `graph_builder.topology_edges()` rather than importing it, because
    that function takes a `TrussTopology` recovered from an Onshape read and
    this mock has no Onshape payload behind it.
    """
    at_node: dict[str, list[str]] = {}
    for member_id, a, b in CONNECTIVITY:
        at_node.setdefault(a, []).append(member_id)
        at_node.setdefault(b, []).append(member_id)

    edges: list[Edge] = []
    seen: set[tuple[str, str]] = set()
    for sharers in at_node.values():
        ordered = sorted(sharers)
        for i, a in enumerate(ordered):
            for b in ordered[i + 1:]:
                for pair in ((a, b), (b, a)):
                    if pair not in seen:
                        seen.add(pair)
                        edges.append(Edge(pair[0], pair[1], EdgeKind.TOPOLOGY))
    return edges


def mock_graph(
    *,
    change: ChangeEvent | None = None,
    areas: dict[str, float] | None = None,
) -> DependencyGraph:
    """The 10-bar truss as a contract-valid DependencyGraph.

    Each member gets its OWN section, because the published optimum gives ten
    different areas. The contract already carries `sections` as a list and
    `Member.section_id` per member, so this needs no contract change -- but note
    that `graph_builder.build_graph()` currently assigns one shared section to
    every member, which cannot express this.
    """
    areas = areas or OPTIMUM_AREAS
    change = change or escalated_change()

    nodes = [
        Node(
            id=node_id,
            x=pos[0],
            y=pos[1],
            z=0.0,
            support=SUPPORTS.get(node_id, SupportType.FREE),
        )
        for node_id, pos in NODE_POSITIONS.items()
    ]

    sections = [
        Section(id=f"sec_{mid}", name=f"{areas[mid]:.2f} in^2 bar",
                area=areas[mid] * IN ** 2)
        for mid, _, _ in CONNECTIVITY
    ]

    members = [
        Member(
            id=mid,
            start_node=a,
            end_node=b,
            section_id=f"sec_{mid}",
            material_id="mat_al",
            onshape_id=f"onshape-part-{mid}",
        )
        for mid, a, b in CONNECTIVITY
    ]

    graph = DependencyGraph(
        id="graph-mock-10bar",
        change=change,
        source=OnshapeRef(
            document_id="74352477fea92ae1dadf61d6",
            workspace_id="7121b248aa290fdccdc1afa6",
            element_id="72f445c1fb5399de58418375",
            branch_name=f"earl-eval-{change.id}",
        ),
        nodes=nodes,
        members=members,
        materials=[
            Material(
                id="mat_al",
                name="Benchmark aluminium (E = 10^7 psi)",
                elastic_modulus=E_ALUMINIUM,
                yield_strength=YIELD_STRENGTH,
                density=DENSITY,
                allowable_stress=ALLOWABLE_STRESS,
            )
        ],
        sections=sections,
        load_cases=[
            LoadCase(
                id="lc_1",
                name="Benchmark load case (100 kip down at n2 and n4)",
                point_loads=[
                    PointLoad(id=f"p_{n}", node_id=n, fy=-BENCHMARK_LOAD)
                    for n in LOADED_NODES
                ],
            )
        ],
        edges=_topology_edges(),
    )
    graph.validate()
    return graph


# --------------------------------------------------------------------------
# Change events
# --------------------------------------------------------------------------

def escalated_change() -> ChangeEvent:
    return ChangeEvent(
        id="chg-m7-trim",
        kind=ChangeKind.FEATURE_EDIT,
        description=(
            "Reduced the cross-section of diagonal member m7 from 7.46 in^2 to "
            "6.00 in^2 to save weight."
        ),
        target_id="m7",
        value_before="7.46 in^2",
        value_after="6.00 in^2",
    )


def approved_change() -> ChangeEvent:
    return ChangeEvent(
        id="chg-m1-trim",
        kind=ChangeKind.FEATURE_EDIT,
        description=(
            "Reduced the cross-section of top chord member m1 from 30.52 in^2 "
            "to 20.00 in^2."
        ),
        target_id="m1",
        value_before="30.52 in^2",
        value_after="20.00 in^2",
    )


def edge_change() -> ChangeEvent:
    return ChangeEvent(
        id="chg-m9-trim",
        kind=ChangeKind.FEATURE_EDIT,
        description=(
            "Reduced the cross-section of diagonal member m9 from 21.53 in^2 "
            "to 12.00 in^2."
        ),
        target_id="m9",
        value_before="21.53 in^2",
        value_after="12.00 in^2",
    )


# --------------------------------------------------------------------------
# Decisions
# --------------------------------------------------------------------------

def _results(rows: list[_Row], threshold: float) -> list[MemberResult]:
    out: list[MemberResult] = []
    for member_id, before, after, force, sf in rows:
        out.append(
            MemberResult(
                member_id=member_id,
                status=MemberStatus.FAIL if sf < threshold else MemberStatus.PASS,
                stress_before=before,
                stress_after=after,
                axial_force_after=force,
                capacity=ALLOWABLE_STRESS,
                safety_factor=sf,
                utilization=1.0 / sf,
                is_affected=True,   # indeterminate truss: everything redistributes
                onshape_id=f"onshape-part-{member_id}",
            )
        )
    return out


def _decision(
    *,
    decision_id: str,
    change_id: str,
    description: str,
    rows: list[_Row],
    outcome: Outcome,
    threshold: float = 1.0,
    report: SkyCivReport | None = None,
    cross_check: CrossCheck | None = None,
    residual: float = 2.589e-10,
    evaluated_at: str = "2026-09-13T14:22:11Z",
) -> Decision:
    results = _results(rows, threshold)
    violating = [r.member_id for r in results
                 if r.safety_factor is not None and r.safety_factor < threshold]

    decision = Decision(
        id=decision_id,
        graph_id="graph-mock-10bar",
        change_id=change_id,
        outcome=outcome,
        change_description=description,
        safety_factor_threshold=threshold,
        member_results=results,
        violating_member_ids=violating,
        sanity_checks=SanityChecks(
            equilibrium_ok=True,
            max_residual_force=residual,
            linearity_ok=True,
        ),
        cross_check=cross_check or CrossCheck(),
        skyciv_report=report,
        load_case_id="lc_1",
        solver="PyNite",
        solver_version="1.1.2",
        evaluated_at=evaluated_at,
    )
    decision.validate()
    return decision


def escalated() -> Decision:
    """The dropped domino: m7 is changed, m7 stays safe, m5 does not."""
    cross = CrossCheck(member_id="m5", pynite_value=2.293728e8,
                       skyciv_value=2.276310e8, tolerance=0.05)
    cross.evaluate()
    return _decision(
        decision_id="dec-m7-trim",
        change_id="chg-m7-trim",
        description=(
            "Reduced the cross-section of diagonal member m7 from 7.46 in^2 to "
            "6.00 in^2 to save weight."
        ),
        rows=_ESCALATED_ROWS,
        outcome=Outcome.ESCALATED,
        report=SkyCivReport(
            report_id="SKYCIV-2026-0913-0044",
            url="https://platform.skyciv.com/reports/2026-0913-0044",
            local_path="artifacts/SKYCIV-2026-0913-0044.pdf",
            generated_at="2026-09-13T14:22:39Z",
            design_code="AISC 360-16",
        ),
        cross_check=cross,
    )


def approved() -> Decision:
    """A genuine auto-approval: every member stays above the threshold."""
    return _decision(
        decision_id="dec-m1-trim",
        change_id="chg-m1-trim",
        description=(
            "Reduced the cross-section of top chord member m1 from 30.52 in^2 "
            "to 20.00 in^2."
        ),
        rows=_APPROVED_ROWS,
        outcome=Outcome.APPROVED,
        residual=4.531e-10,
        evaluated_at="2026-09-13T14:05:02Z",
    )


def threshold_edge() -> Decision:
    """m5 lands at 0.995 -- below 1.0 by half a percent.

    The case that justifies enforcing the cutoff in code: an engineer eyeballing
    "about one" waves this through, and the rule does not.
    """
    cross = CrossCheck(member_id="m5", pynite_value=1.732711e8,
                       skyciv_value=1.729400e8, tolerance=0.05)
    cross.evaluate()
    return _decision(
        decision_id="dec-m9-trim",
        change_id="chg-m9-trim",
        description=(
            "Reduced the cross-section of diagonal member m9 from 21.53 in^2 "
            "to 12.00 in^2."
        ),
        rows=_EDGE_ROWS,
        outcome=Outcome.ESCALATED,
        report=SkyCivReport(
            report_id="SKYCIV-2026-0913-0051",
            url="https://platform.skyciv.com/reports/2026-0913-0051",
            generated_at="2026-09-13T14:41:10Z",
            design_code="AISC 360-16",
        ),
        cross_check=cross,
        residual=1.295e-10,
        evaluated_at="2026-09-13T14:40:55Z",
    )


def cross_check_disagreement() -> Decision:
    """Same escalation, but the two solvers do not agree.

    decision.py: disagreement "must still be delivered, never swallowed". This
    is the fixture that proves the ECN does not bury it.
    """
    cross = CrossCheck(member_id="m5", pynite_value=2.293728e8,
                       skyciv_value=1.982400e8, tolerance=0.05)
    cross.evaluate()
    decision = escalated()
    decision.id = "dec-m7-trim-disagree"
    decision.cross_check = cross
    decision.validate()
    return decision


def solver_error() -> Decision:
    """Outcome.ERROR: the solver did not run.

    Must never render as a safety finding -- the ECN maps this to BLOCKED, not
    to HELD.
    """
    return Decision(
        id="dec-m7-trim-error",
        graph_id="graph-mock-10bar",
        change_id="chg-m7-trim",
        outcome=Outcome.ERROR,
        change_description=(
            "Reduced the cross-section of diagonal member m7 from 7.46 in^2 to "
            "6.00 in^2 to save weight."
        ),
        member_results=[],
        violating_member_ids=[],
        sanity_checks=SanityChecks(note="not reached"),
        load_case_id="lc_1",
        solver="PyNite",
        solver_version="1.1.2",
        evaluated_at="2026-09-13T14:22:11Z",
        error_message=(
            "stiffness matrix is singular: node n4 is unrestrained in Y after "
            "the change removed its only bracing member"
        ),
    )


ALL: dict[str, callable] = {
    "approved": approved,
    "escalated": escalated,
    "threshold_edge": threshold_edge,
    "cross_check_disagreement": cross_check_disagreement,
    "solver_error": solver_error,
}


def all_decisions() -> dict[str, Decision]:
    """Every mock, by name -- what the test suite iterates over."""
    return {name: build() for name, build in ALL.items()}
