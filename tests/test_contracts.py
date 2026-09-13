"""Sprint 0 contract tests, as unittest cases.

Mirrors scripts/contract_selfcheck.py so the guardrails are covered by the
normal test run, not only by a script someone has to remember to invoke.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from contract_selfcheck import build_decision, build_graph  # noqa: E402

from earl.contracts import (  # noqa: E402
    ChangeKind,
    Decision,
    DependencyGraph,
    MemberStatus,
    Outcome,
    SupportType,
    UnitSystem,
)


class TestDependencyGraph(unittest.TestCase):
    def setUp(self):
        self.graph = build_graph()

    def test_benchmark_shape(self):
        """10-bar truss: 6 nodes, 10 members."""
        self.assertEqual(len(self.graph.nodes), 6)
        self.assertEqual(len(self.graph.members), 10)

    def test_validates(self):
        self.graph.validate()

    def test_json_round_trip_preserves_enums(self):
        again = DependencyGraph.from_json(self.graph.to_json())
        self.assertEqual(again.to_dict(), self.graph.to_dict())
        self.assertIsInstance(again.change.kind, ChangeKind)
        self.assertIsInstance(again.nodes[4].support, SupportType)

    def test_json_round_trip_preserves_nested_lists(self):
        """Regression: `from __future__ import annotations` turns every
        field type into a string, which once left list[PointLoad] as dicts."""
        again = DependencyGraph.from_json(self.graph.to_json())
        load = again.load_cases[0].point_loads[0]
        self.assertEqual(load.node_id, "n2")
        self.assertAlmostEqual(load.fy, -444_822.0)

    def test_units_are_declared(self):
        self.assertIs(self.graph.units.system, UnitSystem.SI)
        self.graph.units.assert_si()

    def test_rejects_unknown_node_reference(self):
        self.graph.members[0].start_node = "nope"
        with self.assertRaises(ValueError):
            self.graph.validate()

    def test_rejects_degenerate_member(self):
        self.graph.members[0].end_node = self.graph.members[0].start_node
        with self.assertRaises(ValueError):
            self.graph.validate()

    def test_rejects_unsupported_structure(self):
        """An unconstrained model would solve to garbage rather than fail."""
        for node in self.graph.nodes:
            node.support = SupportType.FREE
        with self.assertRaises(ValueError):
            self.graph.validate()

    def test_rejects_affected_id_that_names_no_member(self):
        self.graph.affected_member_ids.append("m99")
        with self.assertRaises(ValueError):
            self.graph.validate()


class TestDecision(unittest.TestCase):
    def setUp(self):
        self.graph = build_graph()
        self.decision = build_decision(self.graph)

    def test_validates(self):
        self.decision.validate()

    def test_json_round_trip_preserves_optional_nested_objects(self):
        again = Decision.from_json(self.decision.to_json())
        self.assertEqual(again.to_dict(), self.decision.to_dict())
        self.assertIsInstance(again.outcome, Outcome)
        self.assertIsNotNone(again.skyciv_report)
        self.assertEqual(again.skyciv_report.design_code, "AISC 360-16")

    def test_governing_member_is_the_weakest(self):
        self.assertEqual(self.decision.governing_member.member_id, "m5")

    def test_carries_change_description_for_the_ecn(self):
        """Track A must be able to write the ECN without re-joining the graph."""
        self.assertEqual(self.decision.change_description, self.graph.change.description)


class TestDecisionGuardrails(unittest.TestCase):
    """The rules plan.md says hold regardless of what any model decided."""

    def setUp(self):
        self.decision = build_decision(build_graph())

    def test_cannot_approve_below_threshold(self):
        self.decision.outcome = Outcome.APPROVED
        with self.assertRaises(ValueError) as ctx:
            self.decision.validate()
        self.assertIn("can never be auto-approved", str(ctx.exception))

    def test_cannot_escalate_without_a_failing_member(self):
        for r in self.decision.member_results:
            r.safety_factor, r.status = 2.0, MemberStatus.PASS
        self.decision.violating_member_ids = []
        with self.assertRaises(ValueError):
            self.decision.validate()

    def test_violating_ids_must_match_results(self):
        self.decision.violating_member_ids = ["m1", "m5"]
        with self.assertRaises(ValueError):
            self.decision.validate()

    def test_member_below_threshold_cannot_be_marked_pass(self):
        self.decision.result("m5").status = MemberStatus.PASS
        with self.assertRaises(ValueError):
            self.decision.validate()

    def test_error_outcome_requires_a_message(self):
        self.decision.outcome = Outcome.ERROR
        self.decision.error_message = None
        with self.assertRaises(ValueError):
            self.decision.validate()

    def test_threshold_is_respected_when_raised(self):
        """A stricter threshold must newly catch members that passed at 1.0."""
        self.decision.safety_factor_threshold = 2.0
        self.decision.violating_member_ids = ["m5"]
        with self.assertRaises(ValueError):
            self.decision.validate()


class TestCrossCheck(unittest.TestCase):
    def test_close_values_agree(self):
        d = build_decision(build_graph())
        self.assertTrue(d.cross_check.agrees)
        self.assertLess(d.cross_check.relative_difference, 0.05)

    def test_divergent_solvers_disagree(self):
        """Disagreement must surface, not be swallowed."""
        d = build_decision(build_graph())
        d.cross_check.skyciv_value = d.cross_check.pynite_value * 1.4
        d.cross_check.evaluate()
        self.assertFalse(d.cross_check.agrees)

    def test_missing_second_solver_marks_check_not_performed(self):
        d = build_decision(build_graph())
        d.cross_check.skyciv_value = None
        d.cross_check.evaluate()
        self.assertFalse(d.cross_check.performed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
