"""Biject tests: the verdict is a function of the numbers and nothing else. [Track B]

The demo and optimum cases use the real benchmark graphs and the real solver,
so the threshold enforcement is exercised end to end (R1 numbers, relative
tolerance 1e-2). The edge cases (zero force, buckling, missing inertia,
worst-over-load-cases, before-state lookup) use hand-built SolveResults on a
small triangle, so each rule is pinned independently of PyNite.
Every test passes threshold=1.0 explicitly -- never the developer's .env.
"""

from __future__ import annotations

import inspect
import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from earl.analysis.benchmark import (  # noqa: E402
    KSI,
    LOAD_CASE_ID,
    demo_before_graph,
    demo_change_graph,
)
from earl.analysis.biject import (  # noqa: E402
    EFFECTIVE_LENGTH_FACTOR,
    INERTIA_NOT_PROVIDED_NOTE,
    MAX_SAFETY_FACTOR,
    MIN_ALLOWED_THRESHOLD,
    UNAFFECTED_FAIL_NOTE,
    ZERO_FORCE_NOTE,
    Capacity,
    Verdict,
    evaluate,
    evaluate_member,
    member_capacity,
    zero_force_floor,
)
from earl.analysis.solver import MemberForce, SolveResult, member_length, solve  # noqa: E402
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

THRESHOLD = 1.0
E_STEEL = 2.0e11
FY = 2.5e8
AREA = 1.0e-3          # m^2
LC_A, LC_B = "lc_a", "lc_b"


def _triangle(*, iy: float = 0.0, iz: float = 0.0, affected: tuple[str, ...] = ("m1",)) -> DependencyGraph:
    """A closed 3-member triangle (pin + roller + apex) that validates. The
    hand-built SolveResults below never need it to be solved."""
    return DependencyGraph(
        id="graph-tri",
        change=ChangeEvent("chg-tri", ChangeKind.FEATURE_EDIT, "thinned m1", "m1"),
        source=OnshapeRef("doc", "ws", "el"),
        nodes=[
            Node("n1", 0.0, 0.0, support=SupportType.PIN),
            Node("n2", 4.0, 0.0, support=SupportType.ROLLER_X),
            Node("n3", 2.0, 1.5),
        ],
        members=[
            Member("m1", "n1", "n3", "sec", "mat", onshape_id="part-m1"),
            Member("m2", "n3", "n2", "sec", "mat", onshape_id="part-m2"),
            Member("m3", "n1", "n2", "sec", "mat"),
        ],
        materials=[Material("mat", "steel", E_STEEL, FY, density=0.0)],
        sections=[Section("sec", "bar", AREA, iy=iy, iz=iz)],
        load_cases=[
            LoadCase(LC_A, "case A", [PointLoad("p1", "n3", fy=-1.0e5)]),
            LoadCase(LC_B, "case B", [PointLoad("p2", "n3", fy=-2.0e5)]),
        ],
        affected_member_ids=list(affected),
    )


def _result(graph: DependencyGraph, forces: dict[str, float], lc: str = LC_A) -> SolveResult:
    """A SolveResult with the given axial forces (N, tension positive) and
    consistent stress/area/length, for members present in `forces`."""
    member_forces = {}
    for mid, f in forces.items():
        m = graph.member(mid)
        area = graph.section(m.section_id).area
        member_forces[mid] = MemberForce(mid, f, f / area, area, member_length(graph, m))
    return SolveResult(
        graph_id=graph.id, load_case_id=lc, member_forces=member_forces,
        displacements={}, reactions={},
    )


def _all(graph: DependencyGraph, **forces: float) -> dict[str, float]:
    """Forces for every member of the graph, defaulting to a safe 1 kN tension."""
    return {m.id: forces.get(m.id, 1.0e3) for m in graph.members}


class TestThresholdFloor(unittest.TestCase):
    def setUp(self):
        self.graph = _triangle()
        self.result = _result(self.graph, _all(self.graph))

    def test_floor_constant(self):
        self.assertEqual(MIN_ALLOWED_THRESHOLD, 1.0)

    def test_below_floor_rejected(self):
        for bad in (0.5, 0.999, 0.0, -1.0):
            with self.assertRaises(ValueError):
                evaluate(self.graph, self.result, bad)

    def test_non_finite_rejected(self):
        for bad in (math.nan, math.inf, -math.inf):
            with self.assertRaises(ValueError):
                evaluate(self.graph, self.result, bad)

    def test_evaluate_member_enforces_floor_too(self):
        with self.assertRaises(ValueError):
            evaluate_member(self.graph, "m1", self.result.force("m1"), 0.9, is_affected=True)

    def test_floor_itself_accepted(self):
        verdict = evaluate(self.graph, self.result, 1.0)
        self.assertEqual(verdict.threshold, 1.0)

    def test_no_parameter_can_force_approval(self):
        """The verdict is (graph, results, threshold, before) and nothing else."""
        params = set(inspect.signature(evaluate).parameters)
        self.assertEqual(params, {"graph", "results", "threshold", "before"})


class TestDemoChange(unittest.TestCase):
    """R1: thinning m7 overloads its NEIGHBOUR m5 -- the downstream domino."""

    @classmethod
    def setUpClass(cls):
        cls.graph = demo_change_graph()
        cls.result = solve(cls.graph, LOAD_CASE_ID)
        cls.verdict = evaluate(cls.graph, cls.result, THRESHOLD)

    def test_escalated_with_m5_only(self):
        self.assertIs(self.verdict.outcome, Outcome.ESCALATED)
        self.assertEqual(self.verdict.violating_member_ids, ["m5"])

    def test_m5_fails_with_expected_numbers(self):
        m5 = self.verdict.result("m5")
        self.assertIs(m5.status, MemberStatus.FAIL)
        self.assertAlmostEqual(m5.safety_factor, 0.735, delta=0.735 * 1e-2)
        self.assertAlmostEqual(abs(m5.stress_after) / KSI, 34.03, delta=34.03 * 1e-2)
        self.assertTrue(m5.is_affected)

    def test_edited_member_m7_passes(self):
        m7 = self.verdict.result("m7")
        self.assertIs(m7.status, MemberStatus.PASS)
        self.assertAlmostEqual(m7.safety_factor, 1.107, delta=1.107 * 1e-2)

    def test_every_member_evaluated(self):
        self.assertEqual(
            [r.member_id for r in self.verdict.member_results],
            [m.id for m in self.graph.members],
        )
        self.assertTrue(all(r.safety_factor is not None for r in self.verdict.member_results))

    def test_capacity_is_a_stress_and_reproduces_sf(self):
        """R6: capacity (Pa) / |stress_after| == safety_factor, and the
        capacity is the declared ALLOWABLE, not the yield (Sprint 4)."""
        mat = self.graph.material("mat_al")
        fd = mat.design_stress
        self.assertEqual(fd, mat.allowable_stress)
        self.assertLess(fd, mat.yield_strength)
        for r in self.verdict.member_results:
            self.assertAlmostEqual(r.capacity, fd)      # allowable-governed, inertia absent
            self.assertAlmostEqual(r.capacity / abs(r.stress_after), r.safety_factor, places=9)
            self.assertAlmostEqual(r.utilization, 1.0 / r.safety_factor, places=12)
            self.assertIn(INERTIA_NOT_PROVIDED_NOTE, r.note)

    def test_governing_load_case_in_note(self):
        for r in self.verdict.member_results:
            self.assertIn(LOAD_CASE_ID, r.note)
        self.assertEqual(self.verdict.governing_load_case_ids["m5"], LOAD_CASE_ID)

    def test_before_supplies_stress_before(self):
        before = solve(demo_before_graph(), LOAD_CASE_ID)
        verdict = evaluate(self.graph, self.result, THRESHOLD, before={LOAD_CASE_ID: before})
        m5 = verdict.result("m5")
        self.assertAlmostEqual(m5.stress_before, 1.5671e8, delta=1.5671e8 * 1e-2)
        self.assertNotIn("no before-state", m5.note)

    def test_without_before_stress_before_is_none(self):
        self.assertIsNone(self.verdict.result("m5").stress_before)

    def test_verdict_assembles_into_a_valid_decision(self):
        decision = Decision(
            id="dec-test", graph_id=self.graph.id, change_id=self.graph.change.id,
            outcome=self.verdict.outcome, safety_factor_threshold=self.verdict.threshold,
            member_results=self.verdict.member_results,
            violating_member_ids=self.verdict.violating_member_ids,
        )
        decision.validate()
        self.assertEqual(decision.governing_member.member_id, "m5")


class TestOptimumApproved(unittest.TestCase):
    def test_all_pass(self):
        graph = demo_before_graph()
        verdict = evaluate(graph, solve(graph, LOAD_CASE_ID), THRESHOLD)
        self.assertIs(verdict.outcome, Outcome.APPROVED)
        self.assertEqual(verdict.violating_member_ids, [])
        self.assertTrue(all(r.status is MemberStatus.PASS for r in verdict.member_results))
        # The demo design is the optimum x 1.10, so its governing member
        # starts 10 % inside the allowable.
        self.assertAlmostEqual(verdict.result("m5").safety_factor, 1.10, delta=1.10 * 1e-2)


class TestZeroForce(unittest.TestCase):
    def setUp(self):
        self.graph = _triangle()

    def test_exactly_zero_is_capped(self):
        result = _result(self.graph, _all(self.graph, m3=0.0))
        r = evaluate(self.graph, result, THRESHOLD).result("m3")
        self.assertEqual(r.safety_factor, MAX_SAFETY_FACTOR)
        self.assertEqual(r.utilization, 0.0)
        self.assertIs(r.status, MemberStatus.PASS)
        self.assertIn(ZERO_FORCE_NOTE, r.note)

    def test_relative_floor_scales_with_largest_force(self):
        """R7: |F| <= 1e-9 x max|F| counts as zero; just above it does not."""
        result = _result(self.graph, _all(self.graph, m1=1.0e6, m3=1.0e-3 * 0.5))
        self.assertAlmostEqual(zero_force_floor(result), 1.0e-3)
        capped = evaluate(self.graph, result, THRESHOLD).result("m3")
        self.assertEqual(capped.safety_factor, MAX_SAFETY_FACTOR)
        self.assertIn(ZERO_FORCE_NOTE, capped.note)

        result = _result(self.graph, _all(self.graph, m1=1.0e6, m3=1.0e-3 * 2.0))
        live = evaluate(self.graph, result, THRESHOLD).result("m3")
        self.assertNotIn(ZERO_FORCE_NOTE, live.note)
        # Tiny but non-zero force: SF would exceed the cap, so it is capped too.
        self.assertEqual(live.safety_factor, MAX_SAFETY_FACTOR)
        self.assertGreater(live.utilization, 0.0)

    def test_cap_is_json_safe(self):
        self.assertTrue(math.isfinite(MAX_SAFETY_FACTOR))
        self.assertEqual(MAX_SAFETY_FACTOR, 1.0e6)


class TestCapacity(unittest.TestCase):
    def test_missing_inertia_uses_yield_and_notes_it(self):
        graph = _triangle()
        cap = member_capacity(graph, "m1")
        self.assertIsInstance(cap, Capacity)
        self.assertAlmostEqual(cap.tension, FY * AREA)
        self.assertAlmostEqual(cap.compression, FY * AREA)
        self.assertEqual(cap.governing, "yield")
        self.assertEqual(cap.note, INERTIA_NOT_PROVIDED_NOTE)

    def test_small_inertia_buckling_governs_compression_only(self):
        """A slender bar: Euler Pcr < Fy*A, so compression capacity is Pcr/A
        while tension keeps the yield capacity."""
        i_small = 1.0e-9
        graph = _triangle(iy=i_small, iz=i_small)
        length = member_length(graph, graph.member("m1"))
        pcr = math.pi ** 2 * E_STEEL * i_small / (EFFECTIVE_LENGTH_FACTOR * length) ** 2
        self.assertLess(pcr, FY * AREA)

        cap = member_capacity(graph, "m1")
        self.assertEqual(cap.governing, "buckling")
        self.assertAlmostEqual(cap.compression, pcr)
        self.assertAlmostEqual(cap.tension, FY * AREA)
        self.assertIn("buckling", cap.note)

        force = -0.5 * pcr
        result = _result(graph, _all(graph, m1=force))
        comp = evaluate(graph, result, THRESHOLD).result("m1")
        self.assertAlmostEqual(comp.capacity, pcr / AREA)
        self.assertAlmostEqual(comp.safety_factor, 2.0)
        self.assertIs(comp.status, MemberStatus.PASS)

        result = _result(graph, _all(graph, m1=-1.5 * pcr))
        self.assertIs(evaluate(graph, result, THRESHOLD).result("m1").status, MemberStatus.FAIL)

        result = _result(graph, _all(graph, m1=+1.5 * pcr))
        tens = evaluate(graph, result, THRESHOLD).result("m1")
        self.assertAlmostEqual(tens.capacity, FY)
        self.assertIs(tens.status, MemberStatus.PASS)

    def test_large_inertia_yield_governs(self):
        graph = _triangle(iy=1.0e-3, iz=1.0e-3)
        cap = member_capacity(graph, "m1")
        self.assertEqual(cap.governing, "yield")
        self.assertAlmostEqual(cap.compression, FY * AREA)
        self.assertIsNone(cap.note)

    def test_i_min_is_the_smaller_axis(self):
        graph = _triangle(iy=1.0e-3, iz=1.0e-9)
        self.assertEqual(member_capacity(graph, "m1").governing, "buckling")


class TestAffectedNote(unittest.TestCase):
    def test_fail_outside_affected_ids_is_surfaced(self):
        graph = _triangle(affected=("m1",))
        overload = -2.0 * FY * AREA
        result = _result(graph, _all(graph, m1=overload, m2=overload))
        verdict = evaluate(graph, result, THRESHOLD)
        self.assertEqual(verdict.violating_member_ids, ["m1", "m2"])
        m1, m2 = verdict.result("m1"), verdict.result("m2")
        self.assertTrue(m1.is_affected)
        self.assertNotIn(UNAFFECTED_FAIL_NOTE, m1.note)
        self.assertFalse(m2.is_affected)
        self.assertIn(UNAFFECTED_FAIL_NOTE, m2.note)
        self.assertIs(m2.status, MemberStatus.FAIL)     # surfaced, never hidden

    def test_pass_outside_affected_ids_has_no_note(self):
        graph = _triangle(affected=("m1",))
        r = evaluate(graph, _result(graph, _all(graph)), THRESHOLD).result("m2")
        self.assertNotIn(UNAFFECTED_FAIL_NOTE, r.note)

    def test_onshape_id_copied(self):
        graph = _triangle()
        verdict = evaluate(graph, _result(graph, _all(graph)), THRESHOLD)
        self.assertEqual(verdict.result("m1").onshape_id, "part-m1")
        self.assertIsNone(verdict.result("m3").onshape_id)


class TestWorstOverLoadCases(unittest.TestCase):
    def setUp(self):
        self.graph = _triangle()
        self.a = _result(self.graph, _all(self.graph, m1=0.5 * FY * AREA), LC_A)   # SF 2 in A
        self.b = _result(self.graph, _all(self.graph, m1=2.0 * FY * AREA), LC_B)   # SF 0.5 in B

    def test_worst_case_governs(self):
        verdict = evaluate(self.graph, [self.a, self.b], THRESHOLD)
        m1 = verdict.result("m1")
        self.assertIs(m1.status, MemberStatus.FAIL)
        self.assertAlmostEqual(m1.safety_factor, 0.5)
        self.assertIn(LC_B, m1.note)
        self.assertEqual(verdict.governing_load_case_ids["m1"], LC_B)
        self.assertIs(verdict.outcome, Outcome.ESCALATED)
        self.assertEqual(verdict.violating_member_ids, ["m1"])

    def test_order_of_results_does_not_matter(self):
        one = evaluate(self.graph, [self.a, self.b], THRESHOLD)
        two = evaluate(self.graph, [self.b, self.a], THRESHOLD)
        self.assertEqual(one.result("m1").safety_factor, two.result("m1").safety_factor)
        self.assertEqual(one.violating_member_ids, two.violating_member_ids)

    def test_single_result_equals_list_of_one(self):
        one = evaluate(self.graph, self.b, THRESHOLD)
        lst = evaluate(self.graph, [self.b], THRESHOLD)
        self.assertEqual([r.to_dict() for r in one.member_results],
                         [r.to_dict() for r in lst.member_results])

    def test_before_comes_from_governing_case(self):
        before_a = _result(self.graph, _all(self.graph, m1=1.0e3), LC_A)
        before_b = _result(self.graph, _all(self.graph, m1=7.0e3), LC_B)
        verdict = evaluate(self.graph, [self.a, self.b], THRESHOLD,
                           before={LC_A: before_a, LC_B: before_b})
        self.assertAlmostEqual(verdict.result("m1").stress_before, 7.0e3 / AREA)
        # m2 governs in A (both cases give the same SF; the first wins).
        self.assertAlmostEqual(verdict.result("m2").stress_before, 1.0e3 / AREA)

    def test_before_missing_governing_case_gives_none_and_note(self):
        before_a = _result(self.graph, _all(self.graph), LC_A)
        verdict = evaluate(self.graph, [self.a, self.b], THRESHOLD, before={LC_A: before_a})
        m1 = verdict.result("m1")
        self.assertIsNone(m1.stress_before)
        self.assertIn(f"no before-state for governing case {LC_B!r}", m1.note)
        self.assertIsNotNone(verdict.result("m2").stress_before)

    def test_before_missing_member_gives_none_and_note(self):
        partial = _result(self.graph, {"m2": 1.0e3, "m3": 1.0e3}, LC_A)
        verdict = evaluate(self.graph, self.a, THRESHOLD, before={LC_A: partial})
        self.assertIsNone(verdict.result("m1").stress_before)
        self.assertIn("no before-state", verdict.result("m1").note)
        self.assertIsNotNone(verdict.result("m2").stress_before)


class TestViolatingIds(unittest.TestCase):
    def test_sorted_and_match_fail_statuses(self):
        graph = _triangle()
        overload = 3.0 * FY * AREA
        result = _result(graph, _all(graph, m3=-overload, m1=overload))
        verdict = evaluate(graph, result, THRESHOLD)
        self.assertEqual(verdict.violating_member_ids, ["m1", "m3"])
        self.assertEqual(verdict.violating_member_ids, sorted(verdict.violating_member_ids))
        self.assertEqual(
            set(verdict.violating_member_ids),
            {r.member_id for r in verdict.member_results if r.status is MemberStatus.FAIL},
        )
        Decision(
            id="dec-tri", graph_id=graph.id, change_id=graph.change.id,
            outcome=verdict.outcome, safety_factor_threshold=verdict.threshold,
            member_results=verdict.member_results,
            violating_member_ids=verdict.violating_member_ids,
        ).validate()

    def test_threshold_above_one_moves_the_line(self):
        graph = _triangle()
        result = _result(graph, _all(graph, m1=0.8 * FY * AREA))      # SF 1.25
        self.assertIs(evaluate(graph, result, 1.0).outcome, Outcome.APPROVED)
        strict = evaluate(graph, result, 1.5)
        self.assertIs(strict.outcome, Outcome.ESCALATED)
        self.assertEqual(strict.violating_member_ids, ["m1"])


class TestInputGuards(unittest.TestCase):
    def setUp(self):
        self.graph = _triangle()

    def test_empty_results_rejected(self):
        with self.assertRaises(ValueError):
            evaluate(self.graph, [], THRESHOLD)

    def test_result_for_another_graph_rejected(self):
        other = _result(self.graph, _all(self.graph))
        other.graph_id = "graph-other"
        with self.assertRaises(ValueError):
            evaluate(self.graph, other, THRESHOLD)

    def test_result_missing_a_member_rejected(self):
        partial = _result(self.graph, {"m1": 1.0e3, "m2": 1.0e3})
        with self.assertRaises(ValueError):
            evaluate(self.graph, partial, THRESHOLD)

    def test_non_si_graph_rejected(self):
        self.graph.units = Units(system=UnitSystem.IMPERIAL, length="in", force="lbf",
                                 stress="psi", area="in^2")
        with self.assertRaises(ValueError):
            evaluate(self.graph, _result(self.graph, _all(self.graph)), THRESHOLD)

    def test_verdict_shape(self):
        verdict = evaluate(self.graph, _result(self.graph, _all(self.graph)), THRESHOLD)
        self.assertIsInstance(verdict, Verdict)
        self.assertEqual(verdict.threshold, THRESHOLD)
        self.assertEqual(len(verdict.member_results), len(self.graph.members))


if __name__ == "__main__":
    unittest.main()
