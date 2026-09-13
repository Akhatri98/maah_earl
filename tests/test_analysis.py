"""Physics checks with analytic expectations and independent reference values."""

import unittest
import contextlib
import io
import json
from copy import deepcopy
from math import pi
from pathlib import Path
from unittest.mock import patch

from earl.analysis.benchmark import benchmark_graph, validate_benchmark
from earl.analysis.capacity import member_capacity
from earl.analysis.sanity import check
from earl.analysis.solver import SolveError, _solve_local, solve
from earl.contracts import SupportType
from earl.units import IN, PSI
from scripts.benchmark_reference import reference
from scripts.contract_selfcheck import build_graph
from scripts.published_reference_selfcheck import SOURCE_SHA256


class SolverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.graph = benchmark_graph()
        cls.solved = _solve_local(cls.graph, "lc_1")

    def test_ten_bar_benchmark_stresses_and_displacements(self):
        self.assertTrue(validate_benchmark()["passed"])

    def test_matches_independent_published_stiffness_formulation(self):
        other = reference()
        for mid, expected in other["stress_psi"].items():
            self.assertAlmostEqual(self.solved.stresses[mid] / PSI, expected, places=6)
        for nid, expected in other["displacements_in"].items():
            for actual, target in zip(self.solved.displacements[nid], expected):
                self.assertAlmostEqual(actual / IN, target, places=9)

    def test_matches_recorded_unmodified_book_reference_execution(self):
        path = Path(__file__).parent / "fixtures" / "benchmark" / "published_reference.json"
        reference = json.loads(path.read_text())
        self.assertFalse(reference["source_modified"])
        self.assertEqual(reference["source_sha256"], SOURCE_SHA256)
        self.assertEqual(validate_benchmark()["published_reference_sha256"], SOURCE_SHA256)
        for mid, expected in reference["stress_psi"].items():
            self.assertAlmostEqual(self.solved.stresses[mid] / PSI, expected, places=6)
        for nid, expected in reference["displacements_in"].items():
            for actual, target in zip(self.solved.displacements[nid], expected):
                self.assertAlmostEqual(actual / IN, target, places=9)

    def test_tension_compression_sign_and_support_displacements(self):
        self.assertGreater(self.solved.axial_forces["m1"], 0)
        self.assertLess(self.solved.axial_forces["m3"], 0)
        self.assertEqual(self.solved.displacements["n5"], [0.0, 0.0, 0.0])
        self.assertEqual(self.solved.displacements["n6"], [0.0, 0.0, 0.0])

    def test_equilibrium_and_linearity_on_benchmark(self):
        checks = check(self.graph, self.solved)
        self.assertTrue(checks.equilibrium_ok)
        self.assertTrue(checks.linearity_ok)
        self.assertLess(checks.max_residual_force, 1e-6)

    def test_corrupted_force_and_nonlinear_result_are_detected(self):
        wrong = deepcopy(self.solved)
        wrong.axial_forces["m1"] += 1000
        self.assertFalse(check(self.graph, wrong).equilibrium_ok)
        wrong = deepcopy(self.solved)
        wrong.doubled_stresses["m2"] *= 1.1
        self.assertFalse(check(self.graph, wrong).linearity_ok)

    def test_singular_structure_is_error(self):
        graph = build_graph()
        graph.members = [m for m in graph.members if m.id in ("m1", "m2", "m3", "m4")]
        graph.edges = []
        graph.affected_member_ids = [m.id for m in graph.members]
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SolveError):
            _solve_local(graph, "lc_1")

    def test_unsupported_physics_is_rejected(self):
        graph = build_graph()
        graph.load_cases[0].point_loads[0].fz = 1
        with self.assertRaises(SolveError):
            _solve_local(graph, "lc_1")

    def test_worker_timeout_is_enforced(self):
        with self.assertRaisesRegex(SolveError, "terminated"):
            solve(self.graph, timeout=0.001)


class CapacityTests(unittest.TestCase):
    def test_euler_compression_is_not_yield_only(self):
        graph = build_graph()
        result = member_capacity(graph, "m3", -1000.0)
        section, material = graph.sections[0], graph.materials[0]
        expected = pi**2 * material.elastic_modulus * section.iy / (360*IN)**2
        self.assertAlmostEqual(result.force_capacity, expected)
        self.assertLess(result.force_capacity, result.yield_capacity)
        self.assertEqual(result.mode, "euler_buckling")

    def test_tension_uses_yield_and_zero_load_is_explicit(self):
        graph = build_graph()
        result = member_capacity(graph, "m1", 1000)
        self.assertEqual(result.force_capacity, result.yield_capacity)
        self.assertEqual(result.mode, "yield")
        zero = member_capacity(graph, "m1", 0)
        self.assertTrue(zero.zero_demand)
        self.assertIsNone(zero.safety_factor)

    def test_missing_inertia_is_not_evaluated(self):
        graph = build_graph()
        graph.sections[0].iy = 0
        result = member_capacity(graph, "m1", -1000)
        self.assertIsNone(result.force_capacity)
        self.assertEqual(result.mode, "not_evaluated")

    def test_buckling_capacity_scales_as_area_squared(self):
        graph = build_graph()
        first = member_capacity(graph, "m5", -1000)
        after = graph.change.apply(graph)
        second = member_capacity(after, "m5", -1000)
        self.assertAlmostEqual(second.euler_capacity / first.euler_capacity, 0.16)
