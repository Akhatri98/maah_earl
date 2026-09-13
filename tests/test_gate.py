import unittest
import json
from copy import deepcopy

from earl.analysis.capacity import capacities
from earl.analysis.gate import attach_cross_check, evaluate, error_decision
from earl.analysis.sanity import check
from earl.analysis.solver import _solve_local
from earl.contracts import CrossCheck, EscalationReason, MemberStatus, Outcome, SkyCivReport
from tests.test_mapping_delta import FIXTURES
from earl.ingestion.truss_map import map_assembly


class GateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.before_graph = map_assembly(*[
            json.loads((FIXTURES / name).read_text())
            for name in ("truss_assembly.json", "truss_variables.json")
        ])
        cls.before = _solve_local(cls.before_graph, "lc_1")
        cls.before.sanity_checks = check(cls.before_graph, cls.before)

    def after(self, member="m8", area_ratio=0.4):
        change = deepcopy(self.before_graph.change)
        change.target_id = member
        change.numeric_after = change.numeric_before * area_ratio
        graph = change.apply(self.before_graph)
        solved = _solve_local(graph, "lc_1")
        solved.sanity_checks = check(graph, solved)
        return graph, solved

    def test_initial_model_and_benign_change_approve(self):
        graph, solved = self.after("m1", 1.1)
        decision = evaluate(self.before_graph, graph, self.before, solved)
        self.assertIs(decision.outcome, Outcome.APPROVED)
        self.assertEqual(len(decision.member_results), 10)

    def test_thin_compression_member_escalates_below_hard_floor(self):
        graph, solved = self.after()
        decision = evaluate(self.before_graph, graph, self.before, solved)
        self.assertIs(decision.outcome, Outcome.ESCALATED)
        self.assertLess(decision.result("m8").safety_factor, 1.0)
        self.assertIs(decision.escalation_reason, EscalationReason.BELOW_HARD_FLOOR)
        decision.outcome = Outcome.APPROVED
        with self.assertRaises(ValueError):
            decision.validate()

    def test_failed_sanity_is_error_not_escalation(self):
        graph, solved = self.after()
        solved.sanity_checks.equilibrium_ok = False
        decision = evaluate(self.before_graph, graph, self.before, solved)
        self.assertIs(decision.outcome, Outcome.ERROR)
        self.assertIsNotNone(decision.error_message)

    def test_missing_capacity_is_explicit_legal_escalation(self):
        graph, solved = self.after("m1", 1.1)
        caps = capacities(graph, solved.axial_forces)
        del caps["m1"]
        decision = evaluate(self.before_graph, graph, self.before, solved, after_capacities=caps)
        self.assertIs(decision.outcome, Outcome.ESCALATED)
        self.assertIs(decision.result("m1").status, MemberStatus.NOT_EVALUATED)
        self.assertIs(decision.escalation_reason, EscalationReason.NOT_EVALUATED)

    def test_cross_check_disagreement_cannot_be_swallowed(self):
        graph, solved = self.after("m1", 1.1)
        decision = evaluate(self.before_graph, graph, self.before, solved)
        cross = CrossCheck(member_id="m8", pynite_value=100, skyciv_value=150)
        cross.evaluate()
        final = attach_cross_check(decision, SkyCivReport("test-report"), cross)
        self.assertIs(final.outcome, Outcome.ESCALATED)
        self.assertIs(final.escalation_reason, EscalationReason.CROSS_CHECK_DISAGREEMENT)
        self.assertIs(decision.outcome, Outcome.APPROVED)

    def test_results_for_a_different_model_are_rejected(self):
        graph, solved = self.after()
        with self.assertRaises(ValueError):
            evaluate(self.before_graph, graph, self.before, self.before)

    def test_stale_capacity_cannot_approve_a_changed_model(self):
        graph, solved = self.after()
        stale = capacities(self.before_graph, self.before.axial_forces)
        with self.assertRaisesRegex(ValueError, "capacity"):
            evaluate(self.before_graph, graph, self.before, solved, after_capacities=stale)

    def test_solver_failure_only_produces_error(self):
        decision = error_decision(decision_id="e", graph_id="g", change_id="c",
                                  description="Remove a member", message="Solver timed out")
        self.assertIs(decision.outcome, Outcome.ERROR)
        self.assertEqual(decision.violating_member_ids, [])
        decision.outcome = Outcome.ESCALATED
        with self.assertRaises(ValueError):
            decision.validate()
