"""10-bar benchmark tests: published-optimum validation and the demo graphs. [Track B]

`validate_solver()` is the Sprint 1B gate: nothing downstream is trusted
until it passes. The equal-area force pin is a regression guard on our own
output, deliberately separate from the published checks.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from earl.analysis.benchmark import (  # noqa: E402
    DISPLACEMENT_LIMIT_IN,
    EQUAL_AREA_MEMBER_FORCES_LB,
    IN,
    KIP,
    KSI,
    LB_PER_IN3,
    LBF,
    LOAD_CASE_ID,
    PUBLISHED_OPTIMUM_WEIGHT_LB,
    STRESS_LIMIT_KSI,
    TEN_BAR_OPTIMUM_AREAS_IN2,
    BenchmarkReport,
    build_ten_bar_graph,
    demo_before_graph,
    demo_change_graph,
    truss_weight_lb,
    validate_solver,
)
from earl.analysis.solver import solve  # noqa: E402
from earl.contracts import ChangeKind, EdgeKind, SupportType, UnitSystem  # noqa: E402


class TestValidateSolver(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = validate_solver()

    def test_passed(self):
        self.assertIsInstance(self.report, BenchmarkReport)
        failed = [c for c in self.report.checks if not c.passed]
        self.assertTrue(self.report.passed, f"failed checks: {failed}")

    def test_covers_every_published_fact(self):
        names = " ".join(c.name for c in self.report.checks)
        for keyword in ("weight", "m5", "n1", "feasibility", "equilibrium", "linearity"):
            self.assertIn(keyword, names)
        self.assertEqual(self.report.solver_version, solve(build_ten_bar_graph(), LOAD_CASE_ID).solver_version)

    def test_table_renders(self):
        table = self.report.to_table()
        self.assertIn("ALL CHECKS PASSED", table)
        self.assertIn("PyNite", table)

    def test_report_fails_when_a_check_fails(self):
        report = validate_solver()
        report.checks[0].passed = False
        self.assertFalse(report.passed)


class TestPublishedOptimum(unittest.TestCase):
    def setUp(self):
        self.graph = build_ten_bar_graph(areas_in2=TEN_BAR_OPTIMUM_AREAS_IN2)
        self.result = solve(self.graph, LOAD_CASE_ID)

    def test_weight(self):
        self.assertAlmostEqual(truss_weight_lb(self.graph), PUBLISHED_OPTIMUM_WEIGHT_LB,
                               delta=1e-3 * PUBLISHED_OPTIMUM_WEIGHT_LB)

    def test_active_stress_constraint_is_m5(self):
        self.assertAlmostEqual(abs(self.result.force("m5").stress) / KSI, STRESS_LIMIT_KSI,
                               delta=2e-3 * STRESS_LIMIT_KSI)

    def test_active_displacement_constraint_is_n1(self):
        self.assertAlmostEqual(-self.result.displacements["n1"].dy / IN, DISPLACEMENT_LIMIT_IN,
                               delta=2e-3 * DISPLACEMENT_LIMIT_IN)

    def test_feasible(self):
        _, worst = self.result.max_abs_stress()
        self.assertLessEqual(worst / KSI, STRESS_LIMIT_KSI * (1 + 2e-3))


class TestTenBarGraph(unittest.TestCase):
    def setUp(self):
        self.graph = build_ten_bar_graph()

    def test_validates_and_is_si(self):
        self.graph.validate()
        self.assertIs(self.graph.units.system, UnitSystem.SI)
        self.graph.units.assert_si()

    def test_shape(self):
        self.assertEqual(len(self.graph.nodes), 6)
        self.assertEqual(len(self.graph.members), 10)
        self.assertEqual(len(self.graph.sections), 10)
        self.assertEqual([s.id for s in self.graph.sections], [f"sec_{m.id}" for m in self.graph.members])
        self.assertEqual(self.graph.node("n5").support, SupportType.PIN)
        self.assertEqual(self.graph.node("n6").support, SupportType.PIN)
        self.assertEqual(self.graph.node("n1").support, SupportType.FREE)

    def test_conversion_constants(self):
        self.assertAlmostEqual(IN, 0.0254)
        self.assertAlmostEqual(KIP, 4448.2216)
        self.assertAlmostEqual(KSI, 6.894757e6)
        self.assertAlmostEqual(LB_PER_IN3, 27679.9)
        self.assertAlmostEqual(LBF * 1000.0, KIP)

    def test_si_values(self):
        self.assertAlmostEqual(self.graph.node("n1").x, 720 * 0.0254)
        self.assertAlmostEqual(self.graph.node("n1").y, 360 * 0.0254)
        mat = self.graph.materials[0]
        self.assertEqual(mat.id, "mat_al")
        self.assertAlmostEqual(mat.elastic_modulus, 6.894757e10, delta=1.0)
        self.assertAlmostEqual(mat.yield_strength, 3.447379e8, delta=100.0)
        self.assertAlmostEqual(mat.density, 2767.99, delta=0.01)
        self.assertAlmostEqual(self.graph.sections[0].area, 1.0 * 0.0254 ** 2)
        lc = self.graph.load_cases[0]
        self.assertEqual(lc.id, LOAD_CASE_ID)
        self.assertEqual([pl.node_id for pl in lc.point_loads], ["n2", "n4"])
        self.assertAlmostEqual(lc.point_loads[0].fy, -444_822.16, delta=0.01)
        self.assertFalse(lc.include_self_weight)

    def test_per_member_areas(self):
        graph = build_ten_bar_graph(areas_in2=TEN_BAR_OPTIMUM_AREAS_IN2)
        self.assertAlmostEqual(graph.section("sec_m1").area, 30.52 * 0.0254 ** 2)
        self.assertAlmostEqual(graph.section("sec_m5").area, 0.100 * 0.0254 ** 2)
        with self.assertRaises(ValueError):
            build_ten_bar_graph(areas_in2=[1.0, 2.0])

    def test_loads_and_id_overrides(self):
        graph = build_ten_bar_graph(loads_kips=(50.0, 25.0), graph_id="g-x")
        self.assertEqual(graph.id, "g-x")
        self.assertAlmostEqual(graph.load_cases[0].point_loads[0].fy, -50.0 * KIP)
        self.assertAlmostEqual(graph.load_cases[0].point_loads[1].fy, -25.0 * KIP)

    def test_json_round_trip(self):
        from earl.contracts import DependencyGraph
        again = DependencyGraph.from_json(self.graph.to_json())
        self.assertEqual(again.to_dict(), self.graph.to_dict())


class TestEqualAreaRegressionPin(unittest.TestCase):
    def test_forces_match_pin(self):
        result = solve(build_ten_bar_graph(), LOAD_CASE_ID)
        self.assertEqual(set(EQUAL_AREA_MEMBER_FORCES_LB), set(result.member_forces))
        for mid, expected_lb in EQUAL_AREA_MEMBER_FORCES_LB.items():
            actual_lb = result.force(mid).axial_force / LBF
            self.assertAlmostEqual(actual_lb, expected_lb, delta=1e-6 * abs(expected_lb),
                                   msg=f"{mid}: {actual_lb} vs pin {expected_lb}")

    def test_pin_has_expected_signs(self):
        """Top chord m1/m2 in tension, bottom chord m3/m4 in compression."""
        self.assertGreater(EQUAL_AREA_MEMBER_FORCES_LB["m1"], 0.0)
        self.assertLess(EQUAL_AREA_MEMBER_FORCES_LB["m3"], 0.0)


class TestDemoGraphs(unittest.TestCase):
    def test_demo_change_graph_validates_and_is_si(self):
        graph = demo_change_graph()
        graph.validate()
        graph.units.assert_si()
        self.assertIs(graph.units.system, UnitSystem.SI)

    def test_demo_change_event(self):
        change = demo_change_graph().change
        self.assertEqual(change.id, "chg-demo-m7")
        self.assertIs(change.kind, ChangeKind.FEATURE_EDIT)
        self.assertEqual(change.target_id, "m7")
        self.assertEqual(change.value_before, "7.457 in^2")
        self.assertEqual(change.value_after, "3.500 in^2")
        self.assertIn("m7", change.description)

    def test_demo_areas(self):
        graph = demo_change_graph()
        self.assertAlmostEqual(graph.section("sec_m7").area, 3.5 * IN ** 2)
        for mid, area in zip([m.id for m in graph.members], TEN_BAR_OPTIMUM_AREAS_IN2):
            if mid != "m7":
                self.assertAlmostEqual(graph.section(f"sec_{mid}").area, area * IN ** 2)

    def test_demo_dependency_walk(self):
        graph = demo_change_graph()
        self.assertEqual(graph.affected_member_ids, ["m7", "m1", "m8", "m3", "m4", "m5", "m10", "m2"])
        self.assertEqual(graph.affected_node_ids, ["n4", "n5"])
        topo = {e.target_id for e in graph.edges if e.kind is EdgeKind.TOPOLOGY}
        path = {e.target_id for e in graph.edges if e.kind is EdgeKind.LOAD_PATH}
        self.assertEqual(topo, {"m1", "m8", "m3", "m4", "m5", "m10"})
        self.assertEqual(path, {"m5", "m2"})
        self.assertTrue(all(e.source_id == "m7" for e in graph.edges))

    def test_demo_physics_r1(self):
        """The edited member survives, its neighbour m5 does not (SF ~ 0.81)."""
        result = solve(demo_change_graph(), LOAD_CASE_ID)
        fy = 50.0 * KSI
        self.assertAlmostEqual(abs(result.force("m5").stress) / KSI, 62.05, delta=0.01 * 62.05)
        self.assertAlmostEqual(abs(result.force("m7").stress) / KSI, 37.5, delta=0.01 * 37.5)
        self.assertLess(fy / abs(result.force("m5").stress), 1.0)
        self.assertGreater(fy / abs(result.force("m7").stress), 1.0)
        # R1 quotes "SF >= 3.4" for the rest; PyNite's exact minimum is 3.365 (m6).
        for mid in result.member_forces:
            if mid not in ("m5", "m7"):
                self.assertGreaterEqual(fy / abs(result.force(mid).stress), 3.3)

    def test_demo_before_graph(self):
        before = demo_before_graph()
        after = demo_change_graph()
        before.validate()
        self.assertEqual([n.id for n in before.nodes], [n.id for n in after.nodes])
        self.assertEqual([m.id for m in before.members], [m.id for m in after.members])
        self.assertEqual(before.load_cases[0].id, LOAD_CASE_ID)
        self.assertEqual(before.change.id, after.change.id)
        self.assertNotEqual(before.id, after.id)
        self.assertAlmostEqual(before.section("sec_m7").area, 7.457 * IN ** 2)
        stress_before = abs(solve(before, LOAD_CASE_ID).force("m5").stress)
        self.assertAlmostEqual(stress_before, 1.7239e8, delta=1e-2 * 1.7239e8)


if __name__ == "__main__":
    unittest.main()
