"""PyNite adapter tests: hand-checkable structures, offline. [Track B]

Every expected number here comes from statics done by hand, not from a
previous solver run, so a wrong sign or a wrong node mapping in the adapter
fails against physics rather than against itself.
"""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from earl.analysis.solver import (  # noqa: E402
    G_ACCEL,
    PLANAR_TOL,
    MemberForce,
    SolveResult,
    SolverError,
    build_model,
    check_finite,
    check_planar_loads,
    check_stability,
    member_length,
    planar_axis,
    restrained_dofs,
    solve,
)
from earl.contracts import (  # noqa: E402
    ChangeEvent,
    ChangeKind,
    DependencyGraph,
    LoadCase,
    Material,
    Member,
    Node,
    OnshapeRef,
    PointLoad,
    Section,
    SupportType,
    UnitSystem,
    Units,
)

E = 2.0e11        # Pa
FY = 2.5e8        # Pa
AREA = 1.0e-3     # m^2


def make_graph(nodes, members, loads, *, graph_id="graph-test", density=0.0,
               inertia=0.0, include_self_weight=False, load_case_id="lc"):
    """Small helper: one material, one section, one load case."""
    return DependencyGraph(
        id=graph_id,
        change=ChangeEvent("chg", ChangeKind.LOAD_ADDED, "test change", "p1"),
        source=OnshapeRef("doc", "ws", "el"),
        nodes=nodes,
        members=[Member(mid, a, b, "sec", "mat") for mid, a, b in members],
        materials=[Material("mat", "steel", E, FY, density=density)],
        sections=[Section("sec", "bar", AREA, iy=inertia, iz=inertia, j=inertia)],
        load_cases=[LoadCase(load_case_id, "case", loads, include_self_weight=include_self_weight)],
    )


def two_bar_graph(theta_deg: float = 30.0, P: float = 10_000.0, L: float = 2.0):
    """R17: bars inclined theta ABOVE the horizontal, apex loaded with -P in y."""
    theta = math.radians(theta_deg)
    return make_graph(
        nodes=[
            Node("left", -L * math.cos(theta), 0.0, support=SupportType.PIN),
            Node("right", L * math.cos(theta), 0.0, support=SupportType.PIN),
            Node("apex", 0.0, L * math.sin(theta)),
        ],
        members=[("mL", "left", "apex"), ("mR", "right", "apex")],
        loads=[PointLoad("p1", "apex", fy=-P)],
    )


class TestTwoBarHandCheck(unittest.TestCase):
    """Symmetric two-bar truss: each member carries F = -P / (2 sin theta)."""

    def test_member_forces_match_hand_calc(self):
        P, theta = 10_000.0, math.radians(30.0)
        result = solve(two_bar_graph(30.0, P), "lc")
        expected = -P / (2.0 * math.sin(theta))     # -10 000 N, compression
        for mid in ("mL", "mR"):
            f = result.force(mid)
            self.assertAlmostEqual(f.axial_force, expected, delta=1e-6 * abs(expected))
            self.assertLess(f.axial_force, 0.0, "compression must be negative")
            self.assertAlmostEqual(f.stress, expected / AREA, delta=1e-6 * abs(expected / AREA))
            self.assertAlmostEqual(f.area, AREA)
            self.assertAlmostEqual(f.length, 2.0)

    def test_theta_matters(self):
        """30 deg and 45 deg must not coincide -- guards a hard-coded reading."""
        f30 = solve(two_bar_graph(30.0), "lc").force("mL").axial_force
        f45 = solve(two_bar_graph(45.0), "lc").force("mL").axial_force
        self.assertAlmostEqual(f30, -10_000.0, delta=1e-3)
        self.assertAlmostEqual(f45, -10_000.0 / math.sqrt(2.0), delta=1e-3)

    def test_reactions_balance_the_load(self):
        result = solve(two_bar_graph(30.0, 10_000.0), "lc")
        total_fy = sum(r.fy for r in result.reactions.values())
        self.assertAlmostEqual(total_fy, 10_000.0, delta=1e-6)
        self.assertAlmostEqual(result.reactions["left"].fy, 5_000.0, delta=1e-6)
        self.assertAlmostEqual(result.reactions["apex"].fy, 0.0, delta=1e-9)

    def test_apex_moves_down(self):
        result = solve(two_bar_graph(30.0), "lc")
        self.assertLess(result.displacements["apex"].dy, 0.0)
        self.assertAlmostEqual(result.displacements["apex"].dx, 0.0, delta=1e-12)

    def test_result_metadata(self):
        result = solve(two_bar_graph(), "lc")
        self.assertEqual(result.graph_id, "graph-test")
        self.assertEqual(result.load_case_id, "lc")
        self.assertEqual(result.solver, "PyNite")
        self.assertTrue(result.solver_version)
        self.assertFalse(result.include_self_weight)
        self.assertEqual(result.load_scale, 1.0)
        self.assertAlmostEqual(result.max_abs_stress()[1], 10_000.0 / AREA, delta=1e-3)

    def test_graph_is_not_mutated(self):
        graph = two_bar_graph()
        before = graph.to_dict()
        solve(graph, "lc", include_self_weight=True)
        self.assertEqual(graph.to_dict(), before)


class TestSignConvention(unittest.TestCase):
    """A bar pulled along its axis must report a POSITIVE force."""

    def pulled_bar(self, P=1_000.0):
        return make_graph(
            nodes=[Node("a", 0.0, 0.0, support=SupportType.PIN),
                   Node("b", 3.0, 0.0, support=SupportType.ROLLER_X)],
            members=[("m", "a", "b")],
            loads=[PointLoad("p1", "b", fx=P)],
        )

    def test_tension_positive(self):
        f = solve(self.pulled_bar(1_000.0), "lc").force("m")
        self.assertAlmostEqual(f.axial_force, 1_000.0, delta=1e-6)
        self.assertAlmostEqual(f.stress, 1_000.0 / AREA, delta=1e-3)

    def test_compression_negative(self):
        f = solve(self.pulled_bar(-1_000.0), "lc").force("m")
        self.assertAlmostEqual(f.axial_force, -1_000.0, delta=1e-6)

    def test_elongation_is_pl_over_ea(self):
        result = solve(self.pulled_bar(1_000.0), "lc")
        self.assertAlmostEqual(result.displacements["b"].dx, 1_000.0 * 3.0 / (E * AREA), delta=1e-15)


class TestSupports(unittest.TestCase):
    def test_roller_allows_movement_along_its_axis(self):
        """ROLLER_X: reaction only in Y, node free to slide in X.

        A two-bar truss on a roller is a mechanism (3 free DOFs, 2 bars), so
        a base member closes the triangle; it then carries the horizontal
        thrust P / (2 tan theta) in tension, by hand."""
        P, theta = 10_000.0, math.radians(30.0)
        graph = two_bar_graph(30.0, P)
        graph.node("right").support = SupportType.ROLLER_X
        graph.members.append(Member("mB", "left", "right", "sec", "mat"))
        result = solve(graph, "lc")
        self.assertAlmostEqual(result.reactions["right"].fx, 0.0, delta=1e-9)
        self.assertAlmostEqual(result.reactions["right"].fy, P / 2.0, delta=1e-6)
        self.assertNotEqual(result.displacements["right"].dx, 0.0)
        self.assertAlmostEqual(result.displacements["right"].dy, 0.0, delta=1e-15)
        # Statically determinate: the bars are unchanged, the base takes the thrust.
        self.assertAlmostEqual(result.force("mL").axial_force, -P / (2 * math.sin(theta)), delta=1e-6)
        self.assertAlmostEqual(result.force("mB").axial_force, P / (2 * math.tan(theta)), delta=1e-6)

    def test_two_bar_on_a_roller_is_a_mechanism(self):
        graph = two_bar_graph()
        graph.node("right").support = SupportType.ROLLER_X
        with self.assertRaises(SolverError) as ctx:
            solve(graph, "lc")
        self.assertIn("unstable", str(ctx.exception).lower())

    def test_pin_holds_both_translations(self):
        result = solve(two_bar_graph(), "lc")
        for nid in ("left", "right"):
            self.assertAlmostEqual(result.displacements[nid].dx, 0.0, delta=1e-15)
            self.assertAlmostEqual(result.displacements[nid].dy, 0.0, delta=1e-15)

    def test_fixed_behaves_like_pin_for_a_truss(self):
        graph = two_bar_graph()
        for nid in ("left", "right"):
            graph.node(nid).support = SupportType.FIXED
        result = solve(graph, "lc")
        self.assertAlmostEqual(result.force("mL").axial_force, -10_000.0, delta=1e-6)

    def test_roller_y_frees_y(self):
        graph = make_graph(
            nodes=[Node("a", 0.0, 0.0, support=SupportType.PIN),
                   Node("b", 0.0, 2.0, support=SupportType.ROLLER_Y)],
            members=[("m", "a", "b")],
            loads=[PointLoad("p1", "b", fy=500.0)],
        )
        result = solve(graph, "lc")
        self.assertAlmostEqual(result.force("m").axial_force, 500.0, delta=1e-6)
        self.assertAlmostEqual(result.reactions["b"].fy, 0.0, delta=1e-9)


class TestErrors(unittest.TestCase):
    def test_unknown_load_case(self):
        with self.assertRaises(SolverError) as ctx:
            solve(two_bar_graph(), "nope")
        self.assertIn("nope", str(ctx.exception))

    def test_unknown_load_case_in_build_model(self):
        with self.assertRaises(SolverError):
            build_model(two_bar_graph(), "nope")

    def test_non_si_units_rejected(self):
        graph = two_bar_graph()
        graph.units = Units(system=UnitSystem.IMPERIAL, length="in", force="lbf", stress="psi", area="in^2")
        with self.assertRaises(ValueError):
            solve(graph, "lc")

    def test_invalid_graph_rejected(self):
        graph = two_bar_graph()
        graph.members[0].start_node = "ghost"
        with self.assertRaises(ValueError):
            solve(graph, "lc")

    def test_empty_structure(self):
        graph = make_graph(
            nodes=[Node("a", 0.0, 0.0, support=SupportType.PIN)], members=[], loads=[],
        )
        with self.assertRaises(SolverError):
            solve(graph, "lc")

    def test_unstable_structure_mentions_unstable(self):
        """R4: bar a(0,0) PIN -> b(3,0) FREE, lateral load: a mechanism in Y."""
        graph = make_graph(
            nodes=[Node("a", 0.0, 0.0, support=SupportType.PIN), Node("b", 3.0, 0.0)],
            members=[("m", "a", "b")],
            loads=[PointLoad("p1", "b", fy=-1_000.0)],
        )
        with self.assertRaises(SolverError) as ctx:
            check_stability(graph)
        self.assertIn("unstable", str(ctx.exception).lower())
        with self.assertRaises(SolverError) as ctx:
            solve(graph, "lc")
        self.assertIn("unstable", str(ctx.exception).lower())

    def test_check_stability_passes_on_stable_structure(self):
        check_stability(two_bar_graph())     # must not raise

    def test_check_stability_counts_modes(self):
        """Two free nodes with no triangulation -> two zero-stiffness modes."""
        graph = make_graph(
            nodes=[Node("a", 0.0, 0.0, support=SupportType.PIN), Node("b", 3.0, 0.0),
                   Node("c", 6.0, 0.0)],
            members=[("m1", "a", "b"), ("m2", "b", "c")],
            loads=[PointLoad("p1", "c", fy=-1.0)],
        )
        with self.assertRaises(SolverError) as ctx:
            check_stability(graph)
        self.assertIn("2 zero-stiffness mode", str(ctx.exception))

    def test_zero_length_member(self):
        graph = make_graph(
            nodes=[Node("a", 0.0, 0.0, support=SupportType.PIN), Node("b", 0.0, 0.0)],
            members=[("m", "a", "b")],
            loads=[PointLoad("p1", "b", fy=-1.0)],
        )
        with self.assertRaises(SolverError):
            solve(graph, "lc")

    def test_duplicate_load_case_id_rejected(self):
        """S4: Graph.validate() does not check load case ids; solving 'the
        first one' would tie the verdict to list order, so it is an error."""
        graph = two_bar_graph()
        graph.load_cases.append(LoadCase("lc", "twin", [PointLoad("p9", "apex", fy=-1.0)]))
        graph.validate()                      # the contract lets this through
        for fn in (lambda: solve(graph, "lc"), lambda: build_model(graph, "lc")):
            with self.assertRaises(SolverError) as ctx:
                fn()
            self.assertIn("duplicate load case id 'lc'", str(ctx.exception))
        # A different, unique id on the same graph is still solvable.
        graph.load_cases[1].id = "lc2"
        solve(graph, "lc")
        solve(graph, "lc2")


class TestCheckFinite(unittest.TestCase):
    """S3: NaN/inf/non-positive numerics must be named, never solved."""

    def assert_rejected(self, graph, *needles):
        with self.assertRaises(SolverError) as ctx:
            check_finite(graph)
        for needle in needles:
            self.assertIn(needle, str(ctx.exception))
        with self.assertRaises(SolverError) as ctx:
            solve(graph, "lc")
        for needle in needles:
            self.assertIn(needle, str(ctx.exception))

    def test_nan_yield_strength(self):
        graph = two_bar_graph()
        graph.materials[0].yield_strength = math.nan
        self.assert_rejected(graph, "material 'mat'", "yield_strength")

    def test_infinite_elastic_modulus(self):
        graph = two_bar_graph()
        graph.materials[0].elastic_modulus = math.inf
        self.assert_rejected(graph, "material 'mat'", "elastic_modulus")

    def test_non_positive_elastic_modulus(self):
        graph = two_bar_graph()
        graph.materials[0].elastic_modulus = 0.0
        self.assert_rejected(graph, "material 'mat'", "elastic_modulus")

    def test_negative_or_nan_density(self):
        graph = two_bar_graph()
        graph.materials[0].density = -1.0
        self.assert_rejected(graph, "material 'mat'", "density")
        graph.materials[0].density = math.nan
        self.assert_rejected(graph, "material 'mat'", "density")

    def test_zero_area(self):
        graph = two_bar_graph()
        graph.sections[0].area = 0.0
        self.assert_rejected(graph, "section 'sec'", "area")

    def test_nan_area_and_negative_inertia(self):
        graph = two_bar_graph()
        graph.sections[0].area = math.nan
        self.assert_rejected(graph, "section 'sec'", "area")
        graph.sections[0].area = AREA
        graph.sections[0].iz = -1e-6
        self.assert_rejected(graph, "section 'sec'", "iz")
        graph.sections[0].iz = math.inf
        self.assert_rejected(graph, "section 'sec'", "iz")

    def test_nan_coordinate(self):
        graph = two_bar_graph()
        graph.node("apex").y = math.nan
        self.assert_rejected(graph, "node 'apex'", "y=nan")
        graph.node("apex").y = 1.0
        graph.node("left").z = math.inf
        self.assert_rejected(graph, "node 'left'", "z=inf")

    def test_nan_load(self):
        graph = two_bar_graph()
        graph.load_cases[0].point_loads[0].fy = math.nan
        self.assert_rejected(graph, "load 'p1'", "load case 'lc'", "fy=nan")

    def test_loads_in_other_cases_are_checked_too(self):
        graph = two_bar_graph()
        graph.load_cases.append(LoadCase("other", "o", [PointLoad("p2", "apex", fx=math.inf)]))
        self.assert_rejected(graph, "load 'p2'", "load case 'other'")

    def test_clean_graph_passes(self):
        check_finite(two_bar_graph())         # must not raise


class TestPlanarLoads(unittest.TestCase):
    """S1: a load along the planar (restrained-everywhere) axis at a node the
    contract leaves free is a mechanism, not a zero-force structure."""

    def test_out_of_plane_load_at_free_node_is_unstable(self):
        graph = two_bar_graph(30.0, 10_000.0)
        graph.load_cases[0].point_loads.append(PointLoad("pz", "apex", fz=-500.0))
        self.assertEqual(planar_axis(graph), "DZ")
        for fn in (lambda: check_planar_loads(graph, "lc"), lambda: solve(graph, "lc")):
            with self.assertRaises(SolverError) as ctx:
                fn()
            msg = str(ctx.exception)
            self.assertIn("unstable", msg.lower())
            self.assertIn("'pz'", msg)
            self.assertIn("'apex'", msg)
            self.assertIn("out-of-plane", msg)

    def test_out_of_plane_load_at_pin_node_still_solves(self):
        graph = two_bar_graph(30.0, 10_000.0)
        graph.load_cases[0].point_loads.append(PointLoad("pz", "left", fz=-500.0))
        check_planar_loads(graph, "lc")       # PIN restrains DZ by contract
        result = solve(graph, "lc")
        self.assertAlmostEqual(result.force("mL").axial_force, -10_000.0, delta=1e-6)
        self.assertAlmostEqual(result.reactions["left"].fz, 500.0, delta=1e-6)

    def test_zero_component_is_not_a_load(self):
        graph = two_bar_graph(30.0, 10_000.0)
        graph.load_cases[0].point_loads.append(PointLoad("pz", "apex", fz=0.0))
        check_planar_loads(graph, "lc")       # must not raise

    def test_roller_that_frees_the_planar_axis_is_unrestrained(self):
        """XZ-plane bar: ROLLER_Y frees DY, which is the planar axis here
        (b is lifted in z so the structure is a plane, not a line)."""
        graph = make_graph(
            nodes=[Node("a", 0.0, 0.0, 0.0, support=SupportType.PIN),
                   Node("b", 3.0, 0.0, 1.0, support=SupportType.ROLLER_Y)],
            members=[("m", "a", "b")],
            loads=[PointLoad("py", "b", fy=-100.0)],
        )
        self.assertEqual(planar_axis(graph), "DY")
        with self.assertRaises(SolverError) as ctx:
            solve(graph, "lc")
        self.assertIn("unstable", str(ctx.exception).lower())

    def xz_truss(self, density=0.0):
        graph = two_bar_graph(30.0, 10_000.0)
        for n in graph.nodes:                 # y -> z, load along -z
            n.y, n.z = 0.0, n.y
        graph.load_cases[0].point_loads[0] = PointLoad("p1", "apex", fz=-10_000.0)
        graph.materials[0].density = density
        self.assertEqual(planar_axis(graph), "DY")
        return graph

    def test_self_weight_on_xz_truss_is_unstable(self):
        """Gravity is -Y; on an XZ truss it is out of plane at the free apex."""
        graph = self.xz_truss(density=7_850.0)
        with self.assertRaises(SolverError) as ctx:
            solve(graph, "lc", include_self_weight=True)
        msg = str(ctx.exception)
        self.assertIn("unstable", msg.lower())
        self.assertIn("self-weight", msg)
        self.assertIn("'apex'", msg)
        # The contract flag is checked the same way as the override (R8).
        graph.load_cases[0].include_self_weight = True
        with self.assertRaises(SolverError):
            solve(graph, "lc")

    def test_xz_truss_without_self_weight_still_solves(self):
        """Density > 0 alone is harmless: no self-weight, no out-of-plane load."""
        result = solve(self.xz_truss(density=7_850.0), "lc")
        self.assertFalse(result.include_self_weight)
        self.assertAlmostEqual(result.force("mL").axial_force, -10_000.0, delta=1e-6)

    def test_xz_truss_with_zero_density_solves_with_self_weight_requested(self):
        """The pre-existing XZ case: density 0 means self-weight is ineffective."""
        result = solve(self.xz_truss(density=0.0), "lc", include_self_weight=True)
        self.assertFalse(result.include_self_weight)
        self.assertAlmostEqual(result.force("mL").axial_force, -10_000.0, delta=1e-6)

    def test_self_weight_on_fully_pinned_xz_bar_solves(self):
        """Both ends restrain DY by contract, so the weight goes to reactions."""
        rho = 7_850.0
        graph = make_graph(
            nodes=[Node("a", 0.0, 0.0, 0.0, support=SupportType.PIN),
                   Node("b", 0.0, 0.0, 4.0, support=SupportType.PIN)],
            members=[("m", "a", "b")],
            loads=[],
            density=rho,
            include_self_weight=True,
        )
        self.assertEqual(planar_axis(graph), "DY")
        result = solve(graph, "lc")
        half = rho * AREA * 4.0 * G_ACCEL / 2.0
        self.assertAlmostEqual(result.reactions["a"].fy, half, delta=1e-9)
        self.assertAlmostEqual(result.reactions["b"].fy, half, delta=1e-9)

    def test_3d_truss_is_not_checked(self):
        R, H = 1.0, 2.0
        feet = [
            Node(f"f{i}", R * math.cos(a), 0.0, R * math.sin(a), support=SupportType.PIN)
            for i, a in enumerate((0.0, 2 * math.pi / 3, 4 * math.pi / 3))
        ]
        graph = make_graph(
            nodes=feet + [Node("apex", 0.0, H, 0.0)],
            members=[(f"leg{i}", f"f{i}", "apex") for i in range(3)],
            loads=[PointLoad("p1", "apex", fx=100.0, fy=-3_000.0, fz=50.0)],
        )
        self.assertIsNone(planar_axis(graph))
        check_planar_loads(graph, "lc")       # must not raise
        solve(graph, "lc")


class TestPlanarTolerance(unittest.TestCase):
    """S2: planar detection is relative, so export noise does not turn a
    planar truss into an out-of-plane mechanism."""

    def test_tiny_z_noise_is_still_planar(self):
        graph = two_bar_graph(30.0, 10_000.0)
        graph.node("apex").z = 1e-17
        self.assertEqual(planar_axis(graph), "DZ")
        self.assertIn("DZ", restrained_dofs(graph)["apex"])
        result = solve(graph, "lc")
        self.assertAlmostEqual(result.force("mL").axial_force, -10_000.0, delta=1e-6)

    def test_tolerance_scales_with_the_structure(self):
        """A 1 km truss with 1e-7 m of z scatter is planar; the same scatter
        relative to a 1 m floor is not the point -- it is the ratio."""
        graph = two_bar_graph(30.0, 10_000.0, L=1_000.0)
        extent = max(n.x for n in graph.nodes) - min(n.x for n in graph.nodes)
        graph.node("apex").z = 0.5 * PLANAR_TOL * extent
        self.assertEqual(planar_axis(graph), "DZ")
        graph.node("apex").z = 2.0 * PLANAR_TOL * extent
        self.assertIsNone(planar_axis(graph))

    def test_floor_applies_to_tiny_structures(self):
        """Extent < 1 m: the tolerance is PLANAR_TOL * 1.0, not smaller."""
        graph = two_bar_graph(30.0, 10_000.0, L=0.01)
        graph.node("apex").z = 0.5 * PLANAR_TOL
        self.assertEqual(planar_axis(graph), "DZ")
        graph.node("apex").z = 2.0 * PLANAR_TOL
        self.assertIsNone(planar_axis(graph))

    def test_same_predicate_everywhere(self):
        """restrained_dofs (check_stability) and build_model must agree with
        planar_axis, otherwise a near-planar truss is unstable in one and
        over-restrained in the other."""
        graph = two_bar_graph(30.0, 10_000.0)
        graph.node("apex").z = 1e-17
        check_stability(graph)                # would raise without the tolerance
        model = build_model(graph, "lc")
        self.assertTrue(model.nodes["apex"].support_DZ)


class TestSectionsAndGeometry(unittest.TestCase):
    def test_zero_inertia_sections_solve(self):
        graph = two_bar_graph()
        sec = graph.sections[0]
        self.assertEqual((sec.iy, sec.iz, sec.j), (0.0, 0.0, 0.0))
        result = solve(graph, "lc")
        self.assertAlmostEqual(result.force("mL").axial_force, -10_000.0, delta=1e-6)

    def test_nonzero_inertia_gives_same_axial(self):
        graph = make_graph(
            nodes=two_bar_graph().nodes,
            members=[("mL", "left", "apex"), ("mR", "right", "apex")],
            loads=[PointLoad("p1", "apex", fy=-10_000.0)],
            inertia=1e-6,
        )
        result = solve(graph, "lc")
        self.assertAlmostEqual(result.force("mL").axial_force, -10_000.0, delta=1e-6)

    def test_3d_truss_solves(self):
        """Tripod: three legs from ground pins to an apex, loaded downward.
        Symmetric, so each leg carries P / (3 sin(elevation))."""
        R, H, P = 1.0, 2.0, 3_000.0
        feet = [
            Node(f"f{i}", R * math.cos(a), 0.0, R * math.sin(a), support=SupportType.PIN)
            for i, a in enumerate((0.0, 2 * math.pi / 3, 4 * math.pi / 3))
        ]
        graph = make_graph(
            nodes=feet + [Node("apex", 0.0, H, 0.0)],
            members=[(f"leg{i}", f"f{i}", "apex") for i in range(3)],
            loads=[PointLoad("p1", "apex", fy=-P)],
        )
        self.assertIsNone(planar_axis(graph))
        result = solve(graph, "lc")
        leg = math.sqrt(R * R + H * H)
        expected = -P / (3.0 * (H / leg))
        for i in range(3):
            self.assertAlmostEqual(result.force(f"leg{i}").axial_force, expected, delta=1e-6)
        self.assertAlmostEqual(result.displacements["apex"].dx, 0.0, delta=1e-12)
        self.assertAlmostEqual(result.displacements["apex"].dz, 0.0, delta=1e-12)
        self.assertLess(result.displacements["apex"].dy, 0.0)

    def test_planar_axis_detection(self):
        self.assertEqual(planar_axis(two_bar_graph()), "DZ")
        graph = two_bar_graph()
        for n in graph.nodes:             # rotate the truss into the y-z plane
            n.x, n.z = 0.0, n.x
        self.assertEqual(planar_axis(graph), "DX")

    def test_truss_in_xz_plane_solves(self):
        """Planar rule must pick Y, not Z, when y is the constant coordinate."""
        graph = two_bar_graph(30.0, 10_000.0)
        for n in graph.nodes:             # y -> z, load along -z
            n.y, n.z = 0.0, n.y
        graph.load_cases[0].point_loads[0] = PointLoad("p1", "apex", fz=-10_000.0)
        self.assertEqual(planar_axis(graph), "DY")
        result = solve(graph, "lc")
        self.assertAlmostEqual(result.force("mL").axial_force, -10_000.0, delta=1e-6)

    def test_member_length(self):
        graph = two_bar_graph(30.0, L=2.0)
        self.assertAlmostEqual(member_length(graph, graph.member("mL")), 2.0)


class TestLoadScaleAndSelfWeight(unittest.TestCase):
    def test_load_scale_doubles_everything(self):
        one = solve(two_bar_graph(), "lc", load_scale=1.0)
        two = solve(two_bar_graph(), "lc", load_scale=2.0)
        self.assertEqual(two.load_scale, 2.0)
        for mid in ("mL", "mR"):
            self.assertEqual(two.force(mid).axial_force, 2.0 * one.force(mid).axial_force)

    def test_self_weight_reactions_equal_loads_plus_weight(self):
        P, rho = 10_000.0, 7_850.0
        graph = two_bar_graph(30.0, P)
        graph.materials[0].density = rho
        graph.load_cases[0].include_self_weight = True
        result = solve(graph, "lc")
        self.assertTrue(result.include_self_weight)
        total_weight = sum(rho * AREA * member_length(graph, m) * G_ACCEL for m in graph.members)
        total_fy = sum(r.fy for r in result.reactions.values())
        self.assertAlmostEqual(total_fy, P + total_weight, delta=1e-6 * (P + total_weight))
        self.assertGreater(total_weight, 0.0)

    def test_self_weight_off_by_default(self):
        graph = two_bar_graph(30.0, 10_000.0)
        graph.materials[0].density = 7_850.0
        result = solve(graph, "lc")
        self.assertFalse(result.include_self_weight)
        self.assertAlmostEqual(sum(r.fy for r in result.reactions.values()), 10_000.0, delta=1e-6)

    def test_self_weight_ignored_when_density_zero(self):
        graph = two_bar_graph(30.0, 10_000.0)
        graph.load_cases[0].include_self_weight = True
        result = solve(graph, "lc")
        self.assertFalse(result.include_self_weight)
        self.assertAlmostEqual(sum(r.fy for r in result.reactions.values()), 10_000.0, delta=1e-6)

    def test_override_is_additive_only(self):
        """R8: include_self_weight=True adds weight; False can never remove it."""
        graph = two_bar_graph(30.0, 10_000.0)
        graph.materials[0].density = 7_850.0
        added = solve(graph, "lc", include_self_weight=True)
        self.assertTrue(added.include_self_weight)
        self.assertGreater(sum(r.fy for r in added.reactions.values()), 10_000.0)

        graph.load_cases[0].include_self_weight = True
        kept = solve(graph, "lc", include_self_weight=False)
        self.assertTrue(kept.include_self_weight)
        self.assertAlmostEqual(
            sum(r.fy for r in kept.reactions.values()),
            sum(r.fy for r in added.reactions.values()),
        )

    def test_self_weight_lumped_symmetrically(self):
        """A horizontal bar on two pins: each end takes half the weight."""
        rho = 7_850.0
        graph = make_graph(
            nodes=[Node("a", 0.0, 0.0, support=SupportType.PIN),
                   Node("b", 4.0, 0.0, support=SupportType.PIN)],
            members=[("m", "a", "b")],
            loads=[],
            density=rho,
            include_self_weight=True,
        )
        result = solve(graph, "lc")
        half = rho * AREA * 4.0 * G_ACCEL / 2.0
        self.assertAlmostEqual(result.reactions["a"].fy, half, delta=1e-9)
        self.assertAlmostEqual(result.reactions["b"].fy, half, delta=1e-9)
        self.assertAlmostEqual(result.force("m").axial_force, 0.0, delta=1e-9)


class TestSolveResult(unittest.TestCase):
    def test_force_lookup_and_max_stress(self):
        result = SolveResult(
            graph_id="g", load_case_id="lc",
            member_forces={
                "a": MemberForce("a", 10.0, 1.0e6, 1e-5, 1.0),
                "b": MemberForce("b", -30.0, -3.0e6, 1e-5, 1.0),
            },
            displacements={}, reactions={},
        )
        self.assertEqual(result.force("b").axial_force, -30.0)
        self.assertEqual(result.max_abs_stress(), ("b", 3.0e6))
        with self.assertRaises(KeyError):
            result.force("zzz")


if __name__ == "__main__":
    unittest.main()
