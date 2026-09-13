"""Cross-check tests: PyNite (Decision) vs. SkyCiv (SkyCivRun). [Track B, 3B]

Built from contract objects only -- no solver, no network.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from contract_selfcheck import AREA, build_decision, build_graph  # noqa: E402

from earl.artifacts.crosscheck import cross_check, cross_check_table  # noqa: E402
from earl.artifacts.skyciv_client import SkyCivMemberResult, SkyCivRun  # noqa: E402
from earl.contracts import CrossCheck, Decision  # noqa: E402


def make_decision() -> Decision:
    """The self-check escalation, with axial forces filled in (F = sigma * A)."""
    decision = build_decision(build_graph())
    for r in decision.member_results:
        r.axial_force_after = r.stress_after * AREA
    # Distinct, signed numbers so a wrong member cannot accidentally agree.
    decision.result("m5").axial_force_after = -250_000.0
    decision.result("m1").axial_force_after = 400_000.0
    decision.validate()
    return decision


def make_run(scale: float = 1.0, *, members: list[str] | None = None) -> SkyCivRun:
    decision = make_decision()
    results = {}
    for r in decision.member_results:
        if members is not None and r.member_id not in members:
            continue
        results[r.member_id] = SkyCivMemberResult(
            member_id=r.member_id, axial_force=r.axial_force_after * scale
        )
    return SkyCivRun(
        session_id="s", member_results=results, report_url=None,
        design_results={}, design_code=None, raw={}, api_calls=1,
    )


class TestCrossCheck(unittest.TestCase):
    def test_defaults_to_governing_member_and_agrees_within_tolerance(self):
        decision = make_decision()
        check = cross_check(decision, make_run(1.03))
        self.assertIsInstance(check, CrossCheck)
        self.assertEqual(check.member_id, "m5")
        self.assertTrue(check.performed)
        self.assertTrue(check.agrees)
        self.assertAlmostEqual(check.relative_difference, 0.03 / 1.03, places=6)
        self.assertEqual(check.tolerance, 0.05)

    def test_compares_magnitudes(self):
        """A sign flip on SkyCiv's side must not read as a 200 % disagreement."""
        decision = make_decision()
        run = make_run(-1.0)
        check = cross_check(decision, run)
        self.assertTrue(check.agrees)
        self.assertEqual(check.pynite_value, 250_000.0)
        self.assertEqual(check.skyciv_value, 250_000.0)

    def test_disagreement_surfaces(self):
        decision = make_decision()
        check = cross_check(decision, make_run(1.4))
        self.assertTrue(check.performed)
        self.assertFalse(check.agrees)
        self.assertGreater(check.relative_difference, 0.25)

    def test_explicit_member_and_tolerance(self):
        decision = make_decision()
        check = cross_check(decision, make_run(1.03), member_id="m1", tolerance=0.01)
        self.assertEqual(check.member_id, "m1")
        self.assertEqual(check.pynite_value, 400_000.0)
        self.assertFalse(check.agrees)
        self.assertEqual(check.tolerance, 0.01)

    def test_missing_skyciv_member_not_performed_but_named(self):
        decision = make_decision()
        check = cross_check(decision, make_run(members=["m1"]))
        self.assertEqual(check.member_id, "m5")
        self.assertFalse(check.performed)
        self.assertIsNone(check.agrees)
        self.assertEqual(check.pynite_value, 250_000.0)
        self.assertIsNone(check.skyciv_value)

    def test_missing_pynite_force_not_performed(self):
        decision = make_decision()
        decision.result("m5").axial_force_after = None
        check = cross_check(decision, make_run())
        self.assertFalse(check.performed)
        self.assertEqual(check.member_id, "m5")

    def test_unknown_member_not_performed(self):
        decision = make_decision()
        check = cross_check(decision, make_run(), member_id="m99")
        self.assertFalse(check.performed)
        self.assertEqual(check.member_id, "m99")

    def test_no_governing_member(self):
        decision = make_decision()
        for r in decision.member_results:
            r.safety_factor = None
        check = cross_check(decision, make_run())
        self.assertFalse(check.performed)
        self.assertIsNone(check.member_id)


class TestCrossCheckTable(unittest.TestCase):
    def test_every_member_present_on_both_sides(self):
        decision = make_decision()
        table = cross_check_table(decision, make_run(1.01))
        self.assertEqual([c.member_id for c in table], [r.member_id for r in decision.member_results])
        self.assertTrue(all(c.performed and c.agrees for c in table))

    def test_only_shared_members(self):
        decision = make_decision()
        decision.result("m2").axial_force_after = None
        table = cross_check_table(decision, make_run(members=["m1", "m2", "m5"]), tolerance=0.5)
        self.assertEqual([c.member_id for c in table], ["m1", "m5"])
        self.assertTrue(all(c.tolerance == 0.5 for c in table))


if __name__ == "__main__":
    unittest.main(verbosity=2)
