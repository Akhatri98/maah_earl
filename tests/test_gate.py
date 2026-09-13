"""Fast-gate tests: graph in, validated Decision out, and no way to fool it. [Track B]

Runs the real solver and Biject on the R1 demo (optimum with m7 thinned):
the edited member passes and its neighbour m5 fails -- the downstream domino.
The rest pins the guardrails: the threshold floor raises before any solve,
every crash is an ERROR decision that still validates, the plan can neither
narrow the evaluation nor drop self-weight, and a broken before-state can
only cost a stress_before, never the verdict.
Every test passes threshold=1.0 explicitly -- never the developer's .env.
"""

from __future__ import annotations

import math
import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from earl.analysis.benchmark import (  # noqa: E402
    LOAD_CASE_ID,
    demo_before_graph,
    demo_change_graph,
)
from earl.analysis.gate import (  # noqa: E402
    error_decision,
    not_evaluated_results,
    resolve_threshold,
    run_fast_gate,
)
from earl.analysis.orchestrator import AnalysisPlan  # noqa: E402
from earl.analysis.solver import SOLVER_VERSION, solve  # noqa: E402
from earl.contracts import (  # noqa: E402
    ChangeEvent,
    ChangeKind,
    Decision,
    DependencyGraph,
    LoadCase,
    Material,
    Member,
    MemberStatus,
    Node,
    OnshapeRef,
    Outcome,
    PointLoad,
    Section,
    SupportType,
    Units,
    UnitSystem,
)

REL_TOL = 1e-2
EXPECTED_M5_SF = 0.735                     # capacity = 25 ksi allowable (Sprint 4)
EXPECTED_M5_STRESS_BEFORE = 1.5671e8       # Pa (~22.73 ksi; optimum x 1.10 margin)


def _rel_close(actual: float, expected: float, tol: float = REL_TOL) -> bool:
    return abs(actual - expected) <= tol * abs(expected)


class FakePlanner:
    """Returns exactly the plan it is given -- an LLM that says whatever."""

    def __init__(self, plan: AnalysisPlan):
        self._plan = plan
        self.calls = 0

    def plan(self, graph: DependencyGraph) -> AnalysisPlan:
        self.calls += 1
        return self._plan


class CrashingPlanner:
    def plan(self, graph: DependencyGraph) -> AnalysisPlan:
        raise TypeError("planner exploded")


def unstable_bar_graph() -> DependencyGraph:
    """R4: one bar a(0,0) PIN -> b(3,0) FREE with a transverse load at b.
    A mechanism: b can swing about a."""
    return DependencyGraph(
        id="graph-unstable-bar",
        change=ChangeEvent("chg-bar", ChangeKind.FEATURE_EDIT, "single bar", "m1"),
        source=OnshapeRef("doc", "ws", "el"),
        nodes=[Node("a", 0.0, 0.0, support=SupportType.PIN), Node("b", 3.0, 0.0)],
        members=[Member("m1", "a", "b", "sec", "mat")],
        materials=[Material("mat", "steel", 2.0e11, 2.5e8)],
        sections=[Section("sec", "bar", 1e-3)],
        load_cases=[LoadCase("lc", "lateral", [PointLoad("p", "b", fy=-1000.0)])],
    )


def _extra_load_case(source: LoadCase, lc_id: str, factor: float) -> LoadCase:
    return LoadCase(
        lc_id,
        f"{source.name} x{factor}",
        [PointLoad(f"{lc_id}_{p.node_id}", p.node_id, p.fx * factor, p.fy * factor, p.fz * factor)
         for p in source.point_loads],
        include_self_weight=source.include_self_weight,
    )


class TestDemoChange(unittest.TestCase):
    """The R1 numbers, end to end."""

    @classmethod
    def setUpClass(cls):
        cls.graph = demo_change_graph()
        cls.decision = run_fast_gate(cls.graph, threshold=1.0)

    def test_escalated_on_m5(self):
        d = self.decision
        self.assertIs(d.outcome, Outcome.ESCALATED)
        self.assertEqual(d.violating_member_ids, ["m5"])
        self.assertEqual(d.governing_member.member_id, "m5")
        self.assertTrue(_rel_close(d.governing_member.safety_factor, EXPECTED_M5_SF))
        self.assertIs(d.result("m5").status, MemberStatus.FAIL)
        self.assertIs(d.result("m7").status, MemberStatus.PASS)   # the edited member survives
        self.assertTrue(d.result("m5").is_affected)

    def test_every_member_evaluated(self):
        ids = [r.member_id for r in self.decision.member_results]
        self.assertEqual(ids, [m.id for m in self.graph.members])
        for r in self.decision.member_results:
            self.assertIsNot(r.status, MemberStatus.NOT_EVALUATED)
            self.assertIsNotNone(r.safety_factor)
            self.assertIsNotNone(r.stress_after)
            self.assertIsNotNone(r.capacity)

    def test_validates_and_round_trips(self):
        self.decision.validate()
        again = Decision.from_json(self.decision.to_json())
        again.validate()
        self.assertEqual(again.to_dict(), self.decision.to_dict())

    def test_decision_metadata(self):
        d = self.decision
        self.assertEqual(d.id, f"dec-{self.graph.id}")
        self.assertEqual(d.graph_id, self.graph.id)
        self.assertEqual(d.change_id, self.graph.change.id)
        self.assertEqual(d.change_description, self.graph.change.description)
        self.assertEqual(d.load_case_id, LOAD_CASE_ID)
        self.assertEqual(d.solver, "PyNite")
        self.assertEqual(d.solver_version, SOLVER_VERSION)
        self.assertEqual(d.safety_factor_threshold, 1.0)
        self.assertIsNone(d.error_message)
        stamp = datetime.fromisoformat(d.evaluated_at)
        self.assertIsNotNone(stamp.tzinfo)
        self.assertEqual(stamp.utcoffset().total_seconds(), 0.0)

    def test_sanity_checks_ran_and_passed(self):
        checks = self.decision.sanity_checks
        self.assertTrue(checks.equilibrium_ok)
        self.assertTrue(checks.linearity_ok)
        self.assertIsNotNone(checks.max_residual_force)

    def test_no_before_means_stress_before_none_without_noise(self):
        r = self.decision.result("m5")
        self.assertIsNone(r.stress_before)
        self.assertNotIn("before-state", r.note or "")

    def test_decision_id_override(self):
        d = run_fast_gate(self.graph, threshold=1.0, decision_id="dec-custom")
        self.assertEqual(d.id, "dec-custom")


class TestBeforeState(unittest.TestCase):
    def test_before_graph_populates_stress_before(self):
        d = run_fast_gate(demo_change_graph(), threshold=1.0, before=demo_before_graph())
        self.assertIs(d.outcome, Outcome.ESCALATED)
        r = d.result("m5")
        self.assertIsNotNone(r.stress_before)
        self.assertTrue(_rel_close(r.stress_before, EXPECTED_M5_STRESS_BEFORE))
        self.assertNotIn("no before-state", r.note or "")
        for res in d.member_results:
            self.assertIsNotNone(res.stress_before)

    def test_before_missing_load_case_is_not_an_error(self):
        """R11: a new load case on the after graph has no before-state."""
        graph = demo_change_graph()
        graph.load_cases.insert(0, _extra_load_case(graph.load_cases[0], "lc_extra", 1.0))
        d = run_fast_gate(graph, threshold=1.0, before=demo_before_graph())
        self.assertIsNot(d.outcome, Outcome.ERROR)
        self.assertEqual(d.load_case_id, "lc_extra")
        r = d.result("m5")
        self.assertIsNone(r.stress_before)
        self.assertIn("no before-state for governing case 'lc_extra'", r.note)
        self.assertIn("before-state not analysed for 'lc_extra'", d.sanity_checks.note)
        d.validate()

    def test_unsolvable_before_graph_never_errors_the_after(self):
        before = demo_before_graph()
        before.node("n6").support = SupportType.FREE      # now a mechanism
        d = run_fast_gate(demo_change_graph(), threshold=1.0, before=before)
        self.assertIs(d.outcome, Outcome.ESCALATED)
        self.assertEqual(d.violating_member_ids, ["m5"])
        self.assertIsNone(d.result("m5").stress_before)
        self.assertIn("before-state not analysed for 'lc_benchmark'", d.sanity_checks.note)
        self.assertIn("unstable", d.sanity_checks.note.lower())
        self.assertTrue(d.sanity_checks.equilibrium_ok)   # the after checks still ran

    def test_before_graph_in_wrong_units_is_a_note_not_an_error(self):
        before = demo_before_graph()
        before.units = Units(system=UnitSystem.IMPERIAL)
        d = run_fast_gate(demo_change_graph(), threshold=1.0, before=before)
        self.assertIs(d.outcome, Outcome.ESCALATED)
        self.assertIn("before-state not analysed", d.sanity_checks.note)


class TestApproved(unittest.TestCase):
    def test_optimum_is_approved(self):
        d = run_fast_gate(demo_before_graph(), threshold=1.0)
        self.assertIs(d.outcome, Outcome.APPROVED)
        self.assertEqual(d.violating_member_ids, [])
        self.assertTrue(all(r.status is MemberStatus.PASS for r in d.member_results))
        self.assertGreater(d.governing_member.safety_factor, 1.0)
        d.validate()


class TestThreshold(unittest.TestCase):
    """R9: misconfiguration raises; it never becomes a Decision."""

    def test_below_floor_raises(self):
        with self.assertRaises(ValueError):
            run_fast_gate(demo_change_graph(), threshold=0.5)

    def test_non_finite_raises(self):
        with self.assertRaises(ValueError):
            run_fast_gate(demo_change_graph(), threshold=math.nan)
        with self.assertRaises(ValueError):
            run_fast_gate(demo_change_graph(), threshold=math.inf)

    def test_non_numeric_raises(self):
        with self.assertRaises(ValueError):
            resolve_threshold("high")   # type: ignore[arg-type]

    def test_floor_itself_is_allowed(self):
        self.assertEqual(resolve_threshold(1.0), 1.0)
        self.assertEqual(resolve_threshold(1.5), 1.5)

    def test_higher_threshold_escalates_more(self):
        d = run_fast_gate(demo_before_graph(), threshold=2.5)
        self.assertIs(d.outcome, Outcome.ESCALATED)
        self.assertIn("m5", d.violating_member_ids)     # SF ~ 1.10 on the demo design
        self.assertEqual(d.safety_factor_threshold, 2.5)
        d.validate()


class TestErrorDecisions(unittest.TestCase):
    """R10: a crash is an ERROR decision with a message, never an approval."""

    def _assert_error_shape(self, d: Decision, graph: DependencyGraph):
        self.assertIs(d.outcome, Outcome.ERROR)
        self.assertTrue(d.error_message)
        self.assertEqual(d.violating_member_ids, [])
        self.assertEqual(len(d.member_results), len(graph.members))
        for r in d.member_results:
            self.assertIs(r.status, MemberStatus.NOT_EVALUATED)
            self.assertIsNone(r.safety_factor)
        self.assertEqual(d.graph_id, graph.id)
        self.assertEqual(d.change_id, graph.change.id)
        self.assertIsNotNone(d.evaluated_at)
        d.validate()
        Decision.from_json(d.to_json()).validate()

    def test_no_load_cases(self):
        graph = demo_change_graph()
        graph.load_cases = []
        d = run_fast_gate(graph, threshold=1.0)
        self._assert_error_shape(d, graph)
        self.assertIn("no load cases", d.error_message)
        self.assertTrue(d.error_message.startswith("ValueError"))

    def test_unstable_structure(self):
        graph = unstable_bar_graph()
        d = run_fast_gate(graph, threshold=1.0)
        self._assert_error_shape(d, graph)
        self.assertIn("unstable", d.error_message.lower())
        self.assertTrue(d.error_message.startswith("SolverError"))

    def test_wrong_units(self):
        graph = demo_change_graph()
        graph.units = Units(system=UnitSystem.IMPERIAL)
        d = run_fast_gate(graph, threshold=1.0)
        self._assert_error_shape(d, graph)
        self.assertIn("SI", d.error_message)

    def test_malformed_graph(self):
        graph = demo_change_graph()
        graph.members[0].start_node = "nope"
        d = run_fast_gate(graph, threshold=1.0)
        self._assert_error_shape(d, graph)

    def test_error_message_never_empty(self):
        graph = demo_change_graph()
        d = error_decision(graph, ValueError(""), threshold=1.0)
        self.assertEqual(d.error_message, "ValueError: no message")
        self.assertEqual(len(not_evaluated_results(graph)), len(graph.members))
        d.validate()


class TestPlanCannotSteerTheVerdict(unittest.TestCase):
    """R8: the plan proposes; it cannot narrow evaluation or drop self-weight."""

    def test_fake_planner_cannot_hide_the_failure(self):
        graph = demo_change_graph()
        graph.load_cases[0].include_self_weight = True      # the contract says: with weight
        planner = FakePlanner(AnalysisPlan(LOAD_CASE_ID, include_self_weight=False,
                                           focus_member_ids=[], rationale="look away",
                                           source="llm"))
        d = run_fast_gate(graph, threshold=1.0, planner=planner)
        self.assertEqual(planner.calls, 1)
        self.assertIs(d.outcome, Outcome.ESCALATED)
        self.assertEqual(d.violating_member_ids, ["m5"])
        self.assertEqual(len(d.member_results), len(graph.members))

        with_weight = solve(graph, LOAD_CASE_ID).force("m5").stress
        without_weight = solve(demo_change_graph(), LOAD_CASE_ID).force("m5").stress
        self.assertNotAlmostEqual(with_weight, without_weight, delta=1.0)
        self.assertAlmostEqual(d.result("m5").stress_after, with_weight, delta=1.0)

    def test_plan_adding_self_weight_is_additive(self):
        planner = FakePlanner(AnalysisPlan(LOAD_CASE_ID, include_self_weight=True,
                                           focus_member_ids=["m1"], rationale="", source="llm"))
        graph = demo_change_graph()
        d = run_fast_gate(graph, threshold=1.0, planner=planner)
        self.assertIs(d.outcome, Outcome.ESCALATED)
        contract_only = abs(solve(graph, LOAD_CASE_ID).force("m5").stress)
        with_weight = abs(solve(graph, LOAD_CASE_ID, include_self_weight=True).force("m5").stress)
        worst = max(contract_only, with_weight)
        self.assertAlmostEqual(abs(d.result("m5").stress_after), worst, delta=1.0)

    def test_planner_naming_unknown_load_case_falls_back_to_rules(self):
        planner = FakePlanner(AnalysisPlan("lc_nope", False, [], "", "llm"))
        d = run_fast_gate(demo_change_graph(), threshold=1.0, planner=planner)
        self.assertIs(d.outcome, Outcome.ESCALATED)
        self.assertEqual(d.load_case_id, LOAD_CASE_ID)
        self.assertIn("lc_nope", d.sanity_checks.note)
        self.assertIn("rule-based", d.sanity_checks.note)

    def test_crashing_planner_falls_back_to_rules(self):
        d = run_fast_gate(demo_change_graph(), threshold=1.0, planner=CrashingPlanner())
        self.assertIs(d.outcome, Outcome.ESCALATED)
        self.assertEqual(d.violating_member_ids, ["m5"])
        self.assertIn("planner failed", d.sanity_checks.note)
        self.assertIn("TypeError", d.sanity_checks.note)

    def test_plan_has_no_verdict_fields(self):
        for name in ("threshold", "safety_factor_threshold", "outcome", "status", "capacity"):
            self.assertFalse(hasattr(AnalysisPlan(LOAD_CASE_ID, False), name))


class TestAllLoadCases(unittest.TestCase):
    def _two_case_graph(self) -> DependencyGraph:
        graph = demo_before_graph()                       # APPROVED under lc_benchmark
        graph.load_cases.append(_extra_load_case(graph.load_cases[0], "lc_heavy", 2.5))
        return graph

    def test_worst_load_case_governs(self):
        d = run_fast_gate(self._two_case_graph(), threshold=1.0)
        self.assertIs(d.outcome, Outcome.ESCALATED)
        self.assertIn("m5", d.violating_member_ids)
        self.assertEqual(d.load_case_id, LOAD_CASE_ID)   # the planned case leads
        self.assertIn("governing load case 'lc_heavy'", d.result("m5").note)
        d.validate()

    def test_planned_case_only_when_asked(self):
        d = run_fast_gate(self._two_case_graph(), threshold=1.0, evaluate_all_load_cases=False)
        self.assertIs(d.outcome, Outcome.APPROVED)
        self.assertIn("governing load case 'lc_benchmark'", d.result("m5").note)

    def test_before_state_for_governing_case(self):
        graph = self._two_case_graph()
        before = self._two_case_graph()
        before.id = "graph-two-case-before"
        d = run_fast_gate(graph, threshold=1.0, before=before)
        r = d.result("m5")
        self.assertIsNotNone(r.stress_before)
        self.assertAlmostEqual(r.stress_before, r.stress_after, delta=1.0)   # same structure


if __name__ == "__main__":
    unittest.main()
