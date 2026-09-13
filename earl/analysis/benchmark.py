"""10-bar truss benchmark -- solver validation and the demo structure. [Track B, Sprint 1B]

plan.md (Validation): "Before trusting any output, the PyNite pipeline is
validated against the 10-bar truss benchmark from the structural optimization
literature (Haftka & Gurdal / Rajan formulation)."

The problem is published in IMPERIAL units (360 in bays, E = 1e4 ksi, P = 100
kips, 0.1 lb/in^3, allowable 25 ksi, deflection limit 2 in) while the contract
is SI. The conversion happens ONCE, in this module, so there is exactly one
place where an inch can go wrong (contracts/README.md, "Units are not
optional").

    n5 (0,360) PIN ---m1--- n3 (360,360) ---m2--- n1 (720,360)
       |    \\  m8      m7  /   |    \\  m9    m10  /   |
      m3       \\   /        m5       \\   /        m6
       |    /     \\          |     /     \\          |
    n6 (0,0) PIN ---m3--- n4 (360,0) ---m4--- n2 (720,0)
                                 |                     |
                               P down                P down

What is validated, and against what:

  * Checks 1-4 use the published Case 1 optimum (the classic
    TEN_BAR_OPTIMUM_AREAS_IN2 design, weight 5060.85 lb) whose active
    constraints are independently known: member 5 at the 25 ksi stress limit
    and the vertical displacement of node 1 at the 2 in limit. Reproducing
    both to 0.2 % proves geometry, member numbering, E, load placement and
    the stiffness assembly all at once.
  * Check 5 runs the project's own sanity checks (equilibrium, linearity)
    on the equal-area structure.
  * EQUAL_AREA_MEMBER_FORCES_LB is a REGRESSION pin: our own PyNite output at
    all-1.0 in^2, NOT a published value. It exists so a future change to the
    adapter that shifts a force is noticed, not so the numbers are trusted.

Capacity convention (Sprint 4 agreement with Track A): the material carries
BOTH numbers -- `yield_strength` = 50 ksi (nominal 2024-T3, what SkyCiv's
design check and any buckling check want) and `allowable_stress` = 25 ksi
(the benchmark's published allowable) -- and Biject computes capacity from
the allowable. A member is therefore FAIL when it exceeds the allowable the
literature designed it to, not when it yields.

That puts the published optimum exactly on the threshold (m5 sits at 25.003
ksi, SF 0.9999 by PyNite), which is correct for a stress-constrained optimum
but useless as a "safe before-state". The DEMO structure is therefore the
optimum with a uniform 10 % design margin (`DEMO_AREAS_IN2` = optimum x 1.10):
m5 at SF 1.10, every other member higher. Solver VALIDATION still uses the
exact published optimum.

The demo change (R1) is a FEATURE_EDIT thinning m7 from 8.203 in^2 to
6.000 in^2 on that design. The truss is statically indeterminate, so the
edited member survives (SF ~ 1.11) while its neighbour m5 fails (SF ~ 0.73) --
a real downstream domino, which is the narrative plan.md is built around.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..contracts import (
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
from .sanity import (
    EQUILIBRIUM_REL_TOL,
    LINEARITY_REL_TOL,
    equilibrium_check,
    linearity_check,
)
from .solver import SOLVER_VERSION, G_ACCEL, SolveResult, member_length, solve

# -- imperial -> SI, the ONLY place these appear ----------------------------
IN = 0.0254                 # in -> m
KIP = 4448.2216             # kip -> N
KSI = 6.894757e6            # ksi -> Pa
LB_PER_IN3 = 27679.9        # lb/in^3 -> kg/m^3
LBF = KIP / 1000.0          # lbf -> N

# -- the published problem ---------------------------------------------------
BAY_IN = 360.0
E_KSI = 1.0e4                       # 6.894757e10 Pa
YIELD_KSI = 50.0                    # nominal 2024-T3 yield -> Material.yield_strength
DENSITY_LB_PER_IN3 = 0.1            # 2767.99 kg/m^3
STRESS_LIMIT_KSI = 25.0             # the benchmark ALLOWABLE -> Material.allowable_stress (capacity)
DISPLACEMENT_LIMIT_IN = 2.0
PUBLISHED_OPTIMUM_WEIGHT_LB = 5060.85
# Case 1 optimum, members m1..m10 (in^2).
TEN_BAR_OPTIMUM_AREAS_IN2 = [30.52, 0.100, 23.20, 15.22, 0.100, 0.551, 7.457, 21.04, 21.53, 0.100]
# The demo's as-designed structure: the optimum with a uniform 10 % design
# margin, so the governing member starts at SF 1.10 rather than on the knife
# edge of its active constraint (see the module docstring).
DEMO_MARGIN = 1.10
DEMO_AREAS_IN2 = [round(a * DEMO_MARGIN, 3) for a in TEN_BAR_OPTIMUM_AREAS_IN2]

MATERIAL_ID = "mat_al"
LOAD_CASE_ID = "lc_benchmark"
BENCHMARK_GRAPH_ID = "graph-ten-bar"
DEMO_MEMBER = "m7"
DEMO_AREA_BEFORE_IN2 = DEMO_AREAS_IN2[6]      # 8.203 in^2
DEMO_AREA_AFTER_IN2 = 6.000

NODE_COORDS_IN: dict[str, tuple[float, float]] = {
    "n1": (2 * BAY_IN, BAY_IN),
    "n2": (2 * BAY_IN, 0.0),
    "n3": (BAY_IN, BAY_IN),
    "n4": (BAY_IN, 0.0),
    "n5": (0.0, BAY_IN),
    "n6": (0.0, 0.0),
}
SUPPORTED_NODES = ("n5", "n6")
CONNECTIVITY: list[tuple[str, str, str]] = [
    ("m1", "n5", "n3"), ("m2", "n3", "n1"), ("m3", "n6", "n4"),
    ("m4", "n4", "n2"), ("m5", "n3", "n4"), ("m6", "n1", "n2"),
    ("m7", "n5", "n4"), ("m8", "n6", "n3"), ("m9", "n3", "n2"),
    ("m10", "n4", "n1"),
]
MEMBER_IDS = [mid for mid, _, _ in CONNECTIVITY]
LOADED_NODES = ("n2", "n4")

# REGRESSION PIN -- our own PyNite output at all-1.0 in^2, tension positive,
# in lbf. Not a published value; see the module docstring.
EQUAL_AREA_MEMBER_FORCES_LB: dict[str, float] = {
    "m1": 195364.99, "m2": 40124.63, "m3": -204635.01, "m4": -59875.37,
    "m5": 35489.62, "m6": 40124.63, "m7": 147976.25, "m8": -134866.46,
    "m9": 84676.56, "m10": -56744.80,
}


# --------------------------------------------------------------------------
# Graph construction (SI)
# --------------------------------------------------------------------------

def _default_change() -> ChangeEvent:
    return ChangeEvent(
        id="chg-benchmark-none",
        kind=ChangeKind.VARIABLE_EDIT,
        description="10-bar benchmark baseline; no change applied",
        target_id="benchmark",
    )


def _source() -> OnshapeRef:
    # The benchmark is defined by the literature, not by an Onshape document;
    # the reference is a labelled placeholder so a Decision built on it never
    # points at a real workspace.
    return OnshapeRef(document_id="benchmark", workspace_id="benchmark", element_id="ten-bar-truss")


def build_ten_bar_graph(
    *,
    areas_in2: list[float] | None = None,
    loads_kips: tuple[float, float] = (100.0, 100.0),
    graph_id: str = BENCHMARK_GRAPH_ID,
    change: ChangeEvent | None = None,
) -> DependencyGraph:
    """The 10-bar truss as a SI DependencyGraph.

    `areas_in2` lists m1..m10 (default all 1.0 in^2); every member gets its
    own Section (id f"sec_{mid}") so per-member areas are expressible in the
    contract. `loads_kips` are the downward loads at (n2, n4).
    """
    areas = list(areas_in2) if areas_in2 is not None else [1.0] * len(MEMBER_IDS)
    if len(areas) != len(MEMBER_IDS):
        raise ValueError(f"areas_in2 needs {len(MEMBER_IDS)} values, got {len(areas)}")
    if len(loads_kips) != len(LOADED_NODES):
        raise ValueError(f"loads_kips needs {len(LOADED_NODES)} values, got {len(loads_kips)}")

    nodes = [
        Node(nid, x * IN, y * IN,
             support=SupportType.PIN if nid in SUPPORTED_NODES else SupportType.FREE)
        for nid, (x, y) in NODE_COORDS_IN.items()
    ]
    sections = [
        Section(f"sec_{mid}", f"{area} in^2 bar", area * IN ** 2)
        for mid, area in zip(MEMBER_IDS, areas)
    ]
    members = [
        Member(mid, a, b, section_id=f"sec_{mid}", material_id=MATERIAL_ID)
        for mid, a, b in CONNECTIVITY
    ]
    material = Material(
        MATERIAL_ID, "Aluminium 2024-T3",
        elastic_modulus=E_KSI * KSI,
        yield_strength=YIELD_KSI * KSI,
        density=DENSITY_LB_PER_IN3 * LB_PER_IN3,
        allowable_stress=STRESS_LIMIT_KSI * KSI,
    )
    load_case = LoadCase(
        id=LOAD_CASE_ID,
        name="Benchmark load case",
        point_loads=[
            PointLoad(f"p_{nid}", nid, fy=-kips * KIP)
            for nid, kips in zip(LOADED_NODES, loads_kips)
        ],
    )
    return DependencyGraph(
        id=graph_id,
        change=change or _default_change(),
        source=_source(),
        nodes=nodes,
        members=members,
        materials=[material],
        sections=sections,
        load_cases=[load_case],
    )


def _demo_change() -> ChangeEvent:
    return ChangeEvent(
        id="chg-demo-m7",
        kind=ChangeKind.FEATURE_EDIT,
        description=(
            f"Reduced cross-section of member {DEMO_MEMBER} from "
            f"{DEMO_AREA_BEFORE_IN2} in^2 to {DEMO_AREA_AFTER_IN2:.3f} in^2"
        ),
        target_id=DEMO_MEMBER,
        value_before=f"{DEMO_AREA_BEFORE_IN2} in^2",
        value_after=f"{DEMO_AREA_AFTER_IN2:.3f} in^2",
    )


def topology_edges() -> list[Edge]:
    """TOPOLOGY edges between every pair of members sharing a node, both
    directions -- the same edge set Track A's `graph_builder.topology_edges()`
    derives from the Onshape mate connectors, so a walk over this graph
    behaves exactly like a walk over a real ingested one: rings of 1 hop
    (shares a node with the change) and 2 hops (the rest of the truss)."""
    members_at: dict[str, list[str]] = {}
    for mid, a, b in CONNECTIVITY:
        members_at.setdefault(a, []).append(mid)
        members_at.setdefault(b, []).append(mid)
    edges: list[Edge] = []
    seen: set[tuple[str, str]] = set()
    for sharers in members_at.values():
        ordered = sorted(sharers, key=MEMBER_IDS.index)
        for i, a in enumerate(ordered):
            for b in ordered[i + 1:]:
                for pair in ((a, b), (b, a)):
                    if pair not in seen:
                        seen.add(pair)
                        edges.append(Edge(pair[0], pair[1], EdgeKind.TOPOLOGY))
    return edges


# What the fixed walk from m7 reaches, and how far. Kept as constants so the
# tests pin the expected traversal rather than recomputing it.
DEMO_AFFECTED_MEMBER_IDS = ["m1", "m2", "m3", "m4", "m5", "m6", "m7", "m8", "m9", "m10"]
DEMO_ONE_HOP_MEMBER_IDS = ["m1", "m3", "m4", "m5", "m10"]   # share n5 or n4 with m7 (+ m2 via load path)
DEMO_TWO_HOP_MEMBER_IDS = ["m6", "m8", "m9"]


def demo_change_graph() -> DependencyGraph:
    """R1: the demo design (optimum x 1.10) with m7 (n5-n4) thinned to
    6.000 in^2. Edges are the full node-sharing TOPOLOGY set (as Track A's
    graph builder emits) plus LOAD_PATH edges m7 -> m5 and m7 -> m2 for the
    members that pick up its load. `affected_*` are what the fixed BFS from
    m7 reaches -- every member, because a 10-bar truss is fully connected:
    five at 1 hop (share n5 or n4), m2 at 1 hop via load path, m6, m8 and m9
    at 2 hops.
    Expected verdict: m5 FAIL (SF ~ 0.73), m7 PASS (SF ~ 1.11), rest PASS
    (SF >= 3.2)."""
    areas = list(DEMO_AREAS_IN2)
    areas[MEMBER_IDS.index(DEMO_MEMBER)] = DEMO_AREA_AFTER_IN2
    graph = build_ten_bar_graph(areas_in2=areas, graph_id="graph-demo-m7", change=_demo_change())
    graph.edges = topology_edges() + [
        Edge(DEMO_MEMBER, "m5", EdgeKind.LOAD_PATH),
        Edge(DEMO_MEMBER, "m2", EdgeKind.LOAD_PATH),
    ]
    graph.affected_member_ids = list(DEMO_AFFECTED_MEMBER_IDS)
    graph.affected_node_ids = [nid for nid in NODE_COORDS_IN]
    return graph


def demo_before_graph() -> DependencyGraph:
    """The demo design, unchanged -- the before-state of `demo_change_graph()`.
    Same node/member/load-case ids and the same ChangeEvent (it is the state
    that change is measured against), so stress_before lines up member for
    member. stress_before(m5) ~ 22.73 ksi ~ 1.5672e8 Pa (SF 1.10)."""
    return build_ten_bar_graph(
        areas_in2=list(DEMO_AREAS_IN2),
        graph_id="graph-demo-m7-before",
        change=_demo_change(),
    )


# --------------------------------------------------------------------------
# Validation report
# --------------------------------------------------------------------------

@dataclass
class BenchmarkCheck:
    name: str
    expected: float
    actual: float
    tolerance: float        # relative
    passed: bool
    detail: str


@dataclass
class BenchmarkReport:
    checks: list[BenchmarkCheck]
    solver_version: str

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    def to_table(self) -> str:
        rows = [f"{'check':<34} {'expected':>12} {'actual':>12} {'tol':>8}  result"]
        for c in self.checks:
            rows.append(
                f"{c.name:<34} {c.expected:>12.4f} {c.actual:>12.4f} {c.tolerance:>8.0e}  "
                f"{'PASS' if c.passed else 'FAIL'}  {c.detail}"
            )
        rows.append(f"solver PyNite {self.solver_version}: "
                    f"{'ALL CHECKS PASSED' if self.passed else 'CHECKS FAILED'}")
        return "\n".join(rows)


def _relative_check(name: str, expected: float, actual: float, tol: float, detail: str) -> BenchmarkCheck:
    passed = abs(actual - expected) <= tol * abs(expected)
    return BenchmarkCheck(name, expected, actual, tol, passed, detail)


def _upper_bound_check(name: str, limit: float, actual: float, tol: float, detail: str) -> BenchmarkCheck:
    passed = actual <= limit * (1.0 + tol)
    return BenchmarkCheck(name, limit, actual, tol, passed, detail)


def truss_weight_lb(graph: DependencyGraph) -> float:
    """Total weight from the graph's own densities, areas and lengths."""
    newtons = sum(
        graph.material(m.material_id).density
        * graph.section(m.section_id).area
        * member_length(graph, m)
        * G_ACCEL
        for m in graph.members
    )
    return newtons / LBF


def _max_abs_displacement_in(result: SolveResult) -> float:
    return max(
        max(abs(d.dx), abs(d.dy), abs(d.dz)) for d in result.displacements.values()
    ) / IN


def validate_solver() -> BenchmarkReport:
    """Run the published-optimum checks (1-4) and the sanity checks on the
    equal-area structure (5). See the module docstring for what each proves."""
    checks: list[BenchmarkCheck] = []

    optimum = build_ten_bar_graph(areas_in2=list(TEN_BAR_OPTIMUM_AREAS_IN2), graph_id="graph-ten-bar-optimum")
    checks.append(_relative_check(
        "optimum weight (lb)", PUBLISHED_OPTIMUM_WEIGHT_LB, truss_weight_lb(optimum), 1e-3,
        "geometry, member numbering and density",
    ))

    opt_result = solve(optimum, LOAD_CASE_ID)
    m5_stress_ksi = abs(opt_result.force("m5").stress) / KSI
    checks.append(_relative_check(
        "optimum |stress| m5 (ksi)", STRESS_LIMIT_KSI, m5_stress_ksi, 2e-3,
        "active stress constraint of the Case 1 optimum",
    ))
    n1_drop_in = -opt_result.displacements["n1"].dy / IN
    checks.append(_relative_check(
        "optimum n1 drop (in)", DISPLACEMENT_LIMIT_IN, n1_drop_in, 2e-3,
        "active displacement constraint of the Case 1 optimum (downward)",
    ))
    _, worst_stress = opt_result.max_abs_stress()
    checks.append(_upper_bound_check(
        "optimum feasibility: max |stress| (ksi)", STRESS_LIMIT_KSI, worst_stress / KSI, 2e-3,
        "every member within the 25 ksi allowable",
    ))
    checks.append(_upper_bound_check(
        "optimum feasibility: max |disp| (in)", DISPLACEMENT_LIMIT_IN, _max_abs_displacement_in(opt_result), 2e-3,
        "every node within the 2 in limit",
    ))

    equal = build_ten_bar_graph()
    eq_result = solve(equal, LOAD_CASE_ID)
    eq_ok, residual = equilibrium_check(equal, eq_result)
    checks.append(BenchmarkCheck(
        "equal-area equilibrium residual (N)", 0.0, residual, EQUILIBRIUM_REL_TOL, eq_ok,
        "sum F = 0 at every joint, from graph geometry",
    ))
    lin_ok, deviation = linearity_check(equal, LOAD_CASE_ID)
    checks.append(BenchmarkCheck(
        "equal-area linearity deviation", 0.0, deviation, LINEARITY_REL_TOL, lin_ok,
        "2x load => exactly 2x stress in every member",
    ))

    return BenchmarkReport(checks=checks, solver_version=SOLVER_VERSION)
