"""Fast-gate tests: graph in, validated Decision out, and no way to fool it. [Track B]

Runs the real solver and Biject on the R1 demo (optimum with m7 thinned):
the edited member passes and its neighbour m5 fails -- the downstream domino.
The rest pins the guardrails: the threshold floor raises before any solve,
every crash is an ERROR decision that still validates, the plan can neither
narrow the evaluation nor drop self-weight, a broken before-state can only
cost a stress_before, never the verdict, a failed sanity check can never
coexist with APPROVED/ESCALATED, and duplicate load case ids are an ERROR
rather than a silent narrowing.  The last section drives the two Track B
scripts that sit on top of the gate (scripts/run_fast_gate.py and the pure
parts of scripts/skyciv_smoke.py) -- offline, with fake clients.
Every test passes threshold=1.0 explicitly -- never the developer's .env.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from earl.analysis.benchmark import (  # noqa: E402
    LOAD_CASE_ID,
    demo_before_graph,
    demo_change_graph,
)
from earl.analysis.gate import (  # noqa: E402
    check_unique_load_case_ids,
    error_decision,
    not_evaluated_results,
    resolve_threshold,
    run_fast_gate,
    run_gate,
    sanity_failure,
)
from earl.analysis.orchestrator import AnalysisPlan  # noqa: E402
from earl.analysis.solver import SOLVER_VERSION, solve  # noqa: E402
from earl.artifacts.skyciv_client import (  # noqa: E402
    FN_MODEL_SET,
    FN_MODEL_SOLVE,
    FN_REPORT_ALT,
    FN_SESSION_START,
    SkyCivRun,
)
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
    SanityChecks,
    Section,
    SupportType,
    Units,
    UnitSystem,
)

import run_fast_gate as gate_script  # noqa: E402  (scripts/run_fast_gate.py)
import skyciv_smoke  # noqa: E402  (scripts/skyciv_smoke.py; pure parts only)

REL_TOL = 1e-2
EXPECTED_M5_SF = 0.806
EXPECTED_M5_STRESS_BEFORE = 1.7239e8       # Pa (~25.003 ksi)


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
        self.assertIn("m5", d.violating_member_ids)     # SF ~ 2.0 at the optimum
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


class TestSelfWeightVariantBeforeState(unittest.TestCase):
    """G2: the plan-added self-weight variant is keyed "<lc>+self_weight" on
    both sides, so its stress_before comes from a before solve WITH weight."""

    def setUp(self):
        # Demo contract: lc_benchmark has include_self_weight False, the
        # aluminium has density > 0, so a plan asking for weight ADDS a variant.
        self.graph = demo_change_graph()
        self.before = demo_before_graph()
        self.assertFalse(self.graph.load_cases[0].include_self_weight)
        self.assertTrue(all(m.density > 0 for m in self.graph.materials))
        self.planner = FakePlanner(AnalysisPlan(LOAD_CASE_ID, include_self_weight=True,
                                                focus_member_ids=[], rationale="", source="llm"))

    def test_before_dict_is_keyed_by_variant(self):
        run, checks = run_gate(self.graph, threshold=1.0, planner=self.planner, before=self.before)
        self.assertEqual(set(run.before_results), {LOAD_CASE_ID, f"{LOAD_CASE_ID}+self_weight"})
        self.assertFalse(run.before_results[LOAD_CASE_ID].include_self_weight)
        self.assertTrue(run.before_results[f"{LOAD_CASE_ID}+self_weight"].include_self_weight)
        self.assertNotIn("before-state not analysed", checks.note or "")

    def test_governing_note_names_the_variant_and_before_matches_it(self):
        d = run_fast_gate(self.graph, threshold=1.0, planner=self.planner, before=self.before)
        self.assertIs(d.outcome, Outcome.ESCALATED)
        r = d.result("m5")
        # With weight every demo member is loaded harder, so the variant governs.
        self.assertIn(
            f"governing load case {LOAD_CASE_ID!r} (plan-added self-weight variant)", r.note
        )
        self.assertNotIn("no before-state", r.note)
        with_weight = solve(self.before, LOAD_CASE_ID, include_self_weight=True).force("m5").stress
        without = solve(self.before, LOAD_CASE_ID).force("m5").stress
        self.assertNotAlmostEqual(with_weight, without, delta=1.0)
        self.assertAlmostEqual(r.stress_before, with_weight, delta=1.0)
        self.assertNotAlmostEqual(r.stress_before, without, delta=1.0)
        d.validate()

    def test_variant_before_failure_is_isolated_per_variant(self):
        """R11 per variant: when only the WITH-weight before solve fails, the
        contract variant keeps its before-state, the self-weight variant loses
        its own with a note naming the variant key, and the verdict stands."""
        from earl.analysis import gate as gate_module
        from earl.analysis.solver import SolverError
        real_solve = gate_module.solve
        before = self.before

        def flaky(graph, lc_id, **kw):
            if graph is before and kw.get("include_self_weight"):
                raise SolverError("weight solve exploded")
            return real_solve(graph, lc_id, **kw)

        with mock.patch.object(gate_module, "solve", side_effect=flaky):
            run, checks = run_gate(self.graph, threshold=1.0, planner=self.planner, before=before)
        key = f"{LOAD_CASE_ID}+self_weight"
        self.assertEqual(set(run.before_results), {LOAD_CASE_ID})
        self.assertIn(f"before-state not analysed for {key!r}: SolverError: weight solve exploded",
                      "; ".join(run.notes))
        self.assertIs(run.verdict.outcome, Outcome.ESCALATED)
        m5 = run.verdict.result("m5")
        self.assertIsNone(m5.stress_before)
        self.assertIn(f"no before-state for governing case {key!r}", m5.note)

    def test_weightless_before_graph_gives_weightless_variant_before(self):
        """A before graph with no density cannot carry weight; solve() applies
        none and the "+self_weight" key honestly holds the weightless numbers
        (result.include_self_weight False) rather than a fabricated heavier one."""
        before = demo_before_graph()
        for m in before.materials:
            m.density = 0.0
        run, _ = run_gate(self.graph, threshold=1.0, planner=self.planner, before=before)
        key = f"{LOAD_CASE_ID}+self_weight"
        self.assertEqual(set(run.before_results), {LOAD_CASE_ID, key})
        self.assertFalse(run.before_results[key].include_self_weight)
        self.assertAlmostEqual(
            run.before_results[key].force("m5").stress,
            run.before_results[LOAD_CASE_ID].force("m5").stress, delta=1.0,
        )


class TestDuplicateLoadCaseIds(unittest.TestCase):
    """G1: two load cases under one id would be solved once and the rest
    silently dropped; instead it is an ERROR decision."""

    def _dup_graph(self) -> DependencyGraph:
        graph = demo_before_graph()
        graph.load_cases.append(_extra_load_case(graph.load_cases[0], LOAD_CASE_ID, 2.5))
        return graph

    def test_check_raises_and_names_the_id(self):
        with self.assertRaises(ValueError) as ctx:
            check_unique_load_case_ids(self._dup_graph())
        self.assertIn("duplicate load case ids", str(ctx.exception))
        self.assertIn(LOAD_CASE_ID, str(ctx.exception))
        check_unique_load_case_ids(demo_before_graph())     # unique: no raise

    def test_gate_returns_error_decision(self):
        graph = self._dup_graph()
        graph.validate()                                    # the contract lets it through
        d = run_fast_gate(graph, threshold=1.0)
        self.assertIs(d.outcome, Outcome.ERROR)
        self.assertTrue(d.error_message.startswith("ValueError: duplicate load case ids"))
        self.assertIn(LOAD_CASE_ID, d.error_message)
        self.assertEqual(d.violating_member_ids, [])
        self.assertTrue(all(r.status is MemberStatus.NOT_EVALUATED for r in d.member_results))
        d.validate()

    def test_duplicate_in_before_graph_is_only_a_note(self):
        d = run_fast_gate(demo_change_graph(), threshold=1.0, before=self._dup_graph())
        self.assertIs(d.outcome, Outcome.ESCALATED)
        self.assertEqual(d.violating_member_ids, ["m5"])
        self.assertIsNone(d.result("m5").stress_before)
        self.assertIn("duplicate load case ids", d.sanity_checks.note)


class TestSanityFailureIsError(unittest.TestCase):
    """G3: a solve the gate itself flagged as nonsense is neither an approval
    nor a finding -- ERROR, with Biject's numbers kept for the reviewer."""

    def _gate_with_checks(self, checks: SanityChecks, graph: DependencyGraph | None = None) -> Decision:
        with mock.patch("earl.analysis.gate.run_sanity_checks", return_value=checks) as fake:
            d = run_fast_gate(graph or demo_change_graph(), threshold=1.0)
        self.assertEqual(fake.call_count, 1)
        return d

    def test_equilibrium_failure_becomes_error_keeping_results(self):
        checks = SanityChecks(equilibrium_ok=False, max_residual_force=123.0, linearity_ok=True,
                              note="equilibrium residual 1.230e+02 N exceeds tolerance")
        d = self._gate_with_checks(checks)
        self.assertIs(d.outcome, Outcome.ERROR)
        self.assertTrue(d.error_message.startswith("sanity check failed: equilibrium residual"))
        self.assertIn("1.230e+02 N", d.error_message)
        # The Biject numbers are kept, consistently: m5 still FAIL and listed.
        self.assertEqual(d.violating_member_ids, ["m5"])
        self.assertIs(d.result("m5").status, MemberStatus.FAIL)
        self.assertTrue(_rel_close(d.result("m5").safety_factor, EXPECTED_M5_SF))
        self.assertIs(d.result("m7").status, MemberStatus.PASS)
        self.assertFalse(d.sanity_checks.equilibrium_ok)
        self.assertEqual(d.sanity_checks.max_residual_force, 123.0)
        d.validate()
        Decision.from_json(d.to_json()).validate()

    def test_linearity_failure_quotes_the_deviation(self):
        checks = SanityChecks(equilibrium_ok=True, max_residual_force=0.0, linearity_ok=False,
                              note="linearity deviation 2.500e-03 exceeds tolerance")
        d = self._gate_with_checks(checks, demo_before_graph())     # would be APPROVED
        self.assertIs(d.outcome, Outcome.ERROR)
        self.assertEqual(
            d.error_message, "sanity check failed: linearity deviation 2.500e-03 exceeds tolerance"
        )
        self.assertEqual(d.violating_member_ids, [])
        self.assertTrue(all(r.status is MemberStatus.PASS for r in d.member_results))
        d.validate()

    def test_both_failing_names_both(self):
        checks = SanityChecks(equilibrium_ok=False, max_residual_force=None, linearity_ok=False)
        msg = sanity_failure(checks)
        self.assertEqual(
            msg,
            "sanity check failed: equilibrium residual exceeds tolerance; "
            "linearity deviation exceeds tolerance",
        )

    def test_checks_that_could_not_run_are_not_a_failure(self):
        """None means "not run" (the reason is in the note); only False fails."""
        self.assertIsNone(sanity_failure(SanityChecks()))
        self.assertIsNone(sanity_failure(SanityChecks(equilibrium_ok=True, linearity_ok=None)))
        checks = SanityChecks(equilibrium_ok=None, linearity_ok=None, note="equilibrium check not run")
        d = self._gate_with_checks(checks)
        self.assertIs(d.outcome, Outcome.ESCALATED)
        self.assertIsNone(d.error_message)
        self.assertIn("equilibrium check not run", d.sanity_checks.note)

    def test_real_checks_pass_so_demo_is_not_error(self):
        d = run_fast_gate(demo_change_graph(), threshold=1.0)
        self.assertIs(d.outcome, Outcome.ESCALATED)
        self.assertIsNone(d.error_message)


# --------------------------------------------------------------------------
# The scripts on top of the gate (offline)
# --------------------------------------------------------------------------

def _run_script(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = gate_script.main(argv)
    return code, out.getvalue(), err.getvalue()


class TestRunFastGateScript(unittest.TestCase):
    """G4: a malformed --graph is an ERROR decision (exit 1) with the files
    written, not a traceback before the gate runs."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _write_graph(self, graph: DependencyGraph, name: str) -> str:
        path = self.dir / name
        path.write_text(graph.to_json(), encoding="utf-8")
        return str(path)

    def test_load_graph_does_not_validate(self):
        graph = demo_change_graph()
        graph.members[0].start_node = "nope"
        with self.assertRaises(ValueError):
            graph.validate()
        loaded = gate_script._load_graph(self._write_graph(graph, "bad.json"))
        self.assertEqual(loaded.members[0].start_node, "nope")

    def test_malformed_graph_is_error_decision_with_files(self):
        graph = demo_change_graph()
        graph.members[0].start_node = "nope"
        out, svg = self.dir / "decision.json", self.dir / "truss.svg"
        code, stdout, stderr = _run_script([
            "--no-llm", "--threshold", "1.0",
            "--graph", self._write_graph(graph, "bad.json"),
            "--out", str(out), "--svg", str(svg),
        ])
        self.assertEqual(code, gate_script.EXIT_ERROR)
        self.assertTrue(out.exists())
        decision = Decision.from_json(out.read_text(encoding="utf-8"))
        decision.validate()
        self.assertIs(decision.outcome, Outcome.ERROR)
        self.assertIn("unknown node 'nope'", decision.error_message)
        self.assertIn("outcome=ERROR", stdout)
        # A member pointing at a node that does not exist cannot be drawn;
        # that is reported, not raised, and the JSON above is still written.
        self.assertFalse(svg.exists())
        self.assertIn("svg not written", stderr)

    def test_duplicate_load_case_ids_write_both_files(self):
        graph = demo_before_graph()
        graph.load_cases.append(_extra_load_case(graph.load_cases[0], LOAD_CASE_ID, 2.5))
        out, svg = self.dir / "decision.json", self.dir / "truss.svg"
        code, stdout, _ = _run_script([
            "--no-llm", "--threshold", "1.0",
            "--graph", self._write_graph(graph, "dup.json"),
            "--out", str(out), "--svg", str(svg),
        ])
        self.assertEqual(code, gate_script.EXIT_ERROR)
        decision = Decision.from_json(out.read_text(encoding="utf-8"))
        self.assertIs(decision.outcome, Outcome.ERROR)
        self.assertIn("duplicate load case ids", decision.error_message)
        self.assertTrue(svg.exists())
        self.assertIn("<svg", svg.read_text(encoding="utf-8"))

    def test_malformed_before_graph_is_a_note(self):
        before = demo_before_graph()
        before.members[0].start_node = "nope"
        code, stdout, _ = _run_script([
            "--no-llm", "--threshold", "1.0",
            "--before", self._write_graph(before, "before.json"),
        ])
        self.assertEqual(code, gate_script.EXIT_ESCALATED)
        self.assertIn("before-state not analysed", stdout)

    def test_demo_exit_codes(self):
        code, stdout, _ = _run_script(["--no-llm", "--threshold", "1.0"])
        self.assertEqual(code, gate_script.EXIT_ESCALATED)
        self.assertIn("violating  : ['m5']", stdout)
        code, _, stderr = _run_script(["--no-llm", "--threshold", "0.5"])
        self.assertEqual(code, gate_script.EXIT_ERROR)
        self.assertIn("misconfiguration", stderr)


class _FakeSkyCivClient:
    """Records the one POST retry_report_alt makes; never touches the network."""

    def __init__(self, status: int = 0):
        self.calls: list[dict[str, Any]] = []
        self.status = status

    def call(self, functions, *, label, session_id=None):
        self.calls.append({"functions": functions, "label": label, "session_id": session_id})
        return {
            "response": {"data": "ok"},
            "functions": [
                {"function": f["function"], "status": self.status,
                 "data": {"link": "https://example.invalid/report.pdf"}
                 if f["function"] == FN_REPORT_ALT else None,
                 "msg": "no"}
                for f in functions
            ],
        }


def _skyciv_run(session_id: str | None) -> SkyCivRun:
    return SkyCivRun(session_id=session_id, member_results={}, report_url=None,
                     design_results={}, design_code=None, raw={}, api_calls=1,
                     warnings=["S3D.results.getReport failed: nope"])


class TestSkyCivSmokeScript(unittest.TestCase):
    """G5: the pure parts of scripts/skyciv_smoke.py, with a fake client."""

    def _call(self, session_id: str | None, session_open: bool, status: int = 0):
        client = _FakeSkyCivClient(status)
        graph = demo_before_graph()
        with contextlib.redirect_stdout(io.StringIO()):
            url = skyciv_smoke.retry_report_alt(
                client, graph, LOAD_CASE_ID, _skyciv_run(session_id), session_open=session_open
            )
        self.assertEqual(len(client.calls), 1)
        return url, client.calls[0]

    def test_closed_session_is_rebuilt_in_one_call(self):
        """Call 1 without a design check starts its session with keep_open=False;
        the id it returned is dead, so the model is set and solved again."""
        url, call = self._call("dead-session", session_open=False)
        names = [f["function"] for f in call["functions"]]
        self.assertEqual(names, [FN_SESSION_START, FN_MODEL_SET, FN_MODEL_SOLVE, FN_REPORT_ALT])
        self.assertIsNone(call["session_id"])
        self.assertIn("s3d_model", call["functions"][1]["arguments"])
        self.assertEqual(url, "https://example.invalid/report.pdf")

    def test_open_session_is_reused(self):
        url, call = self._call("live-session", session_open=True)
        self.assertEqual([f["function"] for f in call["functions"]], [FN_REPORT_ALT])
        self.assertEqual(call["session_id"], "live-session")
        self.assertEqual(url, "https://example.invalid/report.pdf")

    def test_open_but_unknown_session_is_rebuilt(self):
        _, call = self._call(None, session_open=True)
        self.assertEqual(call["functions"][0]["function"], FN_SESSION_START)
        self.assertIsNone(call["session_id"])

    def test_failed_alt_report_returns_none(self):
        url, _ = self._call("live-session", session_open=True, status=1)
        self.assertIsNone(url)

    def test_report_dir_default_sees_env_loaded_from_dotenv(self):
        """load_env() runs before the parser, so a SKYCIV_REPORT_DIR that only
        exists in .env becomes the --report-dir default.  Credentials are
        deliberately absent so main() stops with EXIT_NO_CREDENTIALS before
        any client is built."""
        captured: list[argparse.Namespace] = []
        original = argparse.ArgumentParser.parse_args

        def spy(self, args=None, namespace=None):
            ns = original(self, args, namespace)
            captured.append(ns)
            return ns

        def fake_load_env(*a, **k):
            os.environ["SKYCIV_REPORT_DIR"] = "/from/dotenv"
            return {"SKYCIV_REPORT_DIR": "/from/dotenv"}

        env = {k: v for k, v in os.environ.items()
               if k not in ("SKYCIV_API_USERNAME", "SKYCIV_API_KEY", "SKYCIV_REPORT_DIR")}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(skyciv_smoke, "load_env", side_effect=fake_load_env) as le, \
                mock.patch.object(argparse.ArgumentParser, "parse_args", spy), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            code = skyciv_smoke.main([])
        self.assertEqual(code, skyciv_smoke.EXIT_NO_CREDENTIALS)
        self.assertEqual(le.call_count, 1)
        self.assertIn("refusing to run", err.getvalue())
        self.assertEqual(captured[0].report_dir, "/from/dotenv")

    def test_explicit_report_dir_wins_over_env(self):
        captured: list[argparse.Namespace] = []
        original = argparse.ArgumentParser.parse_args

        def spy(self, args=None, namespace=None):
            ns = original(self, args, namespace)
            captured.append(ns)
            return ns

        env = {k: v for k, v in os.environ.items()
               if k not in ("SKYCIV_API_USERNAME", "SKYCIV_API_KEY")}
        env["SKYCIV_REPORT_DIR"] = "/from/env"
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(skyciv_smoke, "load_env", return_value={}), \
                mock.patch.object(argparse.ArgumentParser, "parse_args", spy), \
                contextlib.redirect_stderr(io.StringIO()):
            code = skyciv_smoke.main(["--report-dir", "/explicit"])
        self.assertEqual(code, skyciv_smoke.EXIT_NO_CREDENTIALS)
        self.assertEqual(captured[0].report_dir, "/explicit")


if __name__ == "__main__":
    unittest.main()
