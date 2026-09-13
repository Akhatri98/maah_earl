"""Sanity-check tests: equilibrium and linearity on the 10-bar truss. [Track B]

The equilibrium check must be sharp enough to catch a corrupted result and
loose enough to pass a correct one with lumped self-weight; the linearity
check must be exactly zero because scaling lives in the load combination.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from earl.analysis.benchmark import LOAD_CASE_ID, build_ten_bar_graph  # noqa: E402
from earl.analysis.sanity import (  # noqa: E402
    EQUILIBRIUM_REL_TOL,
    LINEARITY_REL_TOL,
    equilibrium_check,
    linearity_check,
    run_sanity_checks,
)
from earl.analysis.solver import SolverError, solve  # noqa: E402
from earl.contracts import SanityChecks  # noqa: E402


class TestEquilibrium(unittest.TestCase):
    def setUp(self):
        self.graph = build_ten_bar_graph()
        self.result = solve(self.graph, LOAD_CASE_ID)

    def test_ok_on_ten_bar(self):
        ok, residual = equilibrium_check(self.graph, self.result)
        self.assertTrue(ok)
        largest = 100.0 * 4448.2216
        self.assertLessEqual(residual, EQUILIBRIUM_REL_TOL * largest)

    def test_corrupted_result_fails(self):
        """Flip one member's sign: the joints it touches no longer balance."""
        self.result.member_forces["m5"].axial_force *= -1.0
        ok, residual = equilibrium_check(self.graph, self.result)
        self.assertFalse(ok)
        self.assertGreater(residual, 1.0)

    def test_dropped_reaction_fails(self):
        self.result.reactions["n5"].fy = 0.0
        ok, _ = equilibrium_check(self.graph, self.result)
        self.assertFalse(ok)

    def test_scaled_result_balances(self):
        result = solve(self.graph, LOAD_CASE_ID, load_scale=2.0)
        ok, _ = equilibrium_check(self.graph, result)
        self.assertTrue(ok)

    def test_self_weight_case_ok(self):
        graph = build_ten_bar_graph()
        graph.load_cases[0].include_self_weight = True
        result = solve(graph, LOAD_CASE_ID)
        self.assertTrue(result.include_self_weight)
        ok, _ = equilibrium_check(graph, result)
        self.assertTrue(ok)

    def test_self_weight_flag_mismatch_is_caught(self):
        """A result that claims no self-weight but was solved with it cannot
        balance -- the lumped weights are part of the check, not decoration."""
        graph = build_ten_bar_graph()
        graph.load_cases[0].include_self_weight = True
        result = solve(graph, LOAD_CASE_ID)
        result.include_self_weight = False
        ok, _ = equilibrium_check(graph, result)
        self.assertFalse(ok)

    def test_wrong_graph_rejected(self):
        other = build_ten_bar_graph(graph_id="graph-other")
        with self.assertRaises(ValueError):
            equilibrium_check(other, self.result)


class TestLinearity(unittest.TestCase):
    def test_ok_without_self_weight(self):
        ok, deviation = linearity_check(build_ten_bar_graph(), LOAD_CASE_ID)
        self.assertTrue(ok)
        self.assertLessEqual(deviation, LINEARITY_REL_TOL)

    def test_ok_with_self_weight(self):
        graph = build_ten_bar_graph()
        graph.load_cases[0].include_self_weight = True
        ok, deviation = linearity_check(graph, LOAD_CASE_ID)
        self.assertTrue(ok)
        self.assertLessEqual(deviation, LINEARITY_REL_TOL)

    def test_ok_with_self_weight_override(self):
        ok, _ = linearity_check(build_ten_bar_graph(), LOAD_CASE_ID, include_self_weight=True)
        self.assertTrue(ok)

    def test_nonlinear_solver_detected(self):
        """Inject a solver whose stress does not scale with the load."""
        def bent(graph, load_case_id, *, load_scale=1.0, **_):
            result = solve(graph, load_case_id, load_scale=load_scale)
            for f in result.member_forces.values():
                f.stress *= 1.0 + 0.01 * (load_scale - 1.0)
            return result

        ok, deviation = linearity_check(build_ten_bar_graph(), LOAD_CASE_ID, solve_fn=bent)
        self.assertFalse(ok)
        self.assertAlmostEqual(deviation, 0.01, delta=1e-9)

    def test_zero_force_members_skipped(self):
        """A determinate two-bar with a zero-force third member: no 0/0."""
        graph = build_ten_bar_graph()

        def zeroed(graph, load_case_id, *, load_scale=1.0, **_):
            result = solve(graph, load_case_id, load_scale=load_scale)
            result.member_forces["m6"].stress = 0.0
            return result

        ok, _ = linearity_check(graph, LOAD_CASE_ID, solve_fn=zeroed)
        self.assertTrue(ok)


class TestRunSanityChecks(unittest.TestCase):
    def test_returns_contract_object(self):
        graph = build_ten_bar_graph()
        result = solve(graph, LOAD_CASE_ID)
        checks = run_sanity_checks(graph, LOAD_CASE_ID, result)
        self.assertIsInstance(checks, SanityChecks)
        self.assertTrue(checks.equilibrium_ok)
        self.assertTrue(checks.linearity_ok)
        self.assertIsNotNone(checks.max_residual_force)
        self.assertIsNone(checks.note)
        SanityChecks.from_dict(checks.to_dict())   # survives the contract round-trip

    def test_with_self_weight_override(self):
        graph = build_ten_bar_graph()
        result = solve(graph, LOAD_CASE_ID, include_self_weight=True)
        checks = run_sanity_checks(graph, LOAD_CASE_ID, result)
        self.assertTrue(checks.equilibrium_ok)
        self.assertTrue(checks.linearity_ok)

    def test_corrupted_result_reported_in_note(self):
        graph = build_ten_bar_graph()
        result = solve(graph, LOAD_CASE_ID)
        result.member_forces["m1"].axial_force = 0.0
        checks = run_sanity_checks(graph, LOAD_CASE_ID, result)
        self.assertFalse(checks.equilibrium_ok)
        self.assertIn("equilibrium", checks.note)

    def test_load_case_mismatch_rejected(self):
        graph = build_ten_bar_graph()
        result = solve(graph, LOAD_CASE_ID)
        with self.assertRaises(ValueError):
            run_sanity_checks(graph, "other", result)

    def test_solver_failure_becomes_note_not_exception(self):
        graph = build_ten_bar_graph()
        result = solve(graph, LOAD_CASE_ID)
        # Corrupt the graph AFTER solving so the linearity re-solve fails.
        graph.members[0].section_id = "ghost"
        with self.assertRaises(ValueError):
            solve(graph, LOAD_CASE_ID)
        checks = run_sanity_checks(graph, LOAD_CASE_ID, result)
        self.assertIsNone(checks.linearity_ok)
        self.assertIn("linearity check not run", checks.note)
        self.assertIsNotNone(checks.note)

    def test_unstable_graph_for_linearity_is_a_note(self):
        graph = build_ten_bar_graph()
        result = solve(graph, LOAD_CASE_ID)
        graph.members = [m for m in graph.members if m.id not in ("m9", "m10", "m5", "m2")]
        with self.assertRaises(SolverError):
            solve(graph, LOAD_CASE_ID)
        checks = run_sanity_checks(graph, LOAD_CASE_ID, result)
        self.assertIsNone(checks.linearity_ok)
        self.assertIn("unstable", checks.note.lower())


if __name__ == "__main__":
    unittest.main()
