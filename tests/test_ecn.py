"""Sprint 3A tests: ECN generation from the decision contract.

The ECN is the document a human signs, so the properties under test above all
others are:

**It never overstates safety.** An APPROVED notice cannot be produced for a
decision with a member below threshold, an ERROR cannot render as a finding,
and a cross-check disagreement cannot be quietly dropped.

**It is templated, not freeform.** Same decision, byte-identical output; every
section present whether or not it has content.

**It degrades honestly.** Built from a decision alone -- no graph, no walk --
it says what it does not know rather than inventing provenance.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from earl.contracts.common import Units, UnitSystem  # noqa: E402
from earl.contracts.decision import (  # noqa: E402
    CrossCheck,
    Decision,
    MemberResult,
    MemberStatus,
    Outcome,
    SkyCivReport,
)
from earl.delivery import mock_decisions as mk  # noqa: E402
from earl.delivery.ecn import ECN, ECNStatus, build_ecn  # noqa: E402
from earl.delivery.render import render_markdown, render_text  # noqa: E402
from earl.delivery import units as fmt  # noqa: E402
from earl.ingestion import walk_and_apply  # noqa: E402


def enriched(decision, change=None):
    """Build an ECN with graph + walk, the way the pipeline will at Sprint 4."""
    graph = mk.mock_graph(change=change or mk.escalated_change())
    walk = walk_and_apply(graph)
    return build_ecn(decision, graph=graph, walk=walk)


# --------------------------------------------------------------------------
# The mocks themselves
# --------------------------------------------------------------------------

class TestMockDecisions(unittest.TestCase):
    """The mock is Track B's stand-in; if it is not contract-valid, every ECN
    test below is testing the wrong thing."""

    def test_every_mock_validates(self):
        for name, decision in mk.all_decisions().items():
            with self.subTest(name=name):
                decision.validate()

    def test_mock_graph_validates(self):
        mk.mock_graph().validate()

    def test_approved_mock_has_no_member_below_threshold(self):
        decision = mk.approved()
        self.assertEqual(decision.outcome, Outcome.APPROVED)
        self.assertEqual(decision.violating_member_ids, [])
        for r in decision.member_results:
            self.assertGreaterEqual(r.safety_factor, 1.0)

    def test_escalated_mock_is_a_genuine_dropped_domino(self):
        """The changed member stays safe; a DIFFERENT member goes unsafe.

        This is the property the whole demo rests on -- if the failing member
        were m7 itself, checking the thing you edited would have caught it and
        the project's premise would not be demonstrated.
        """
        decision = mk.escalated()
        self.assertEqual(decision.violating_member_ids, ["m5"])
        self.assertGreater(decision.result("m7").safety_factor, 1.0)
        self.assertLess(decision.result("m5").safety_factor, 1.0)
        self.assertNotEqual(decision.governing_member.member_id, "m7")

    def test_threshold_edge_is_only_just_below(self):
        decision = mk.threshold_edge()
        sf = decision.result("m5").safety_factor
        self.assertLess(sf, 1.0)
        self.assertGreater(sf, 0.99)

    def test_areas_are_the_published_optimum_not_uniform(self):
        """Guards the fix described in mock_decisions' docstring: a uniform
        1 in^2 makes every member fail before any change, and the approve path
        becomes unreachable."""
        self.assertNotEqual(len(set(mk.OPTIMUM_AREAS.values())), 1)
        graph = mk.mock_graph()
        self.assertEqual(len({s.area for s in graph.sections}), 8)


# --------------------------------------------------------------------------
# The safety properties
# --------------------------------------------------------------------------

class TestNeverOverstatesSafety(unittest.TestCase):

    def test_escalated_renders_as_held_and_requires_signature(self):
        ecn = enriched(mk.escalated())
        self.assertIs(ecn.status, ECNStatus.HELD)
        self.assertTrue(ecn.requires_signature)
        self.assertIn("HELD", render_text(ecn))
        self.assertIn("does not merge", render_text(ecn))

    def test_approved_renders_as_approved(self):
        ecn = enriched(mk.approved(), change=mk.approved_change())
        self.assertIs(ecn.status, ECNStatus.APPROVED)
        self.assertFalse(ecn.requires_signature)
        self.assertIn("MAY MERGE", render_text(ecn))

    def test_error_renders_as_blocked_not_as_a_finding(self):
        """decision.py keeps ERROR distinct from ESCALATED so a crash cannot
        read as a safety finding. The ECN must preserve that."""
        ecn = enriched(mk.solver_error())
        self.assertIs(ecn.status, ECNStatus.BLOCKED)
        text = render_text(ecn)
        self.assertIn("ANALYSIS INCOMPLETE", text)
        self.assertIn("NOT approved", text)
        self.assertIn("tooling failure", text)
        self.assertNotIn("HELD - ENGINEERING REVIEW", text)
        self.assertNotIn("MAY MERGE", text)
        self.assertTrue(ecn.requires_signature)

    def test_error_ecn_carries_the_error_message(self):
        ecn = enriched(mk.solver_error())
        self.assertIn("singular", ecn.disposition)

    def test_refuses_to_approve_with_a_member_below_threshold(self):
        """A decision that reaches the ECN in a self-inconsistent APPROVED
        state must fail loudly rather than print an approval.

        The contract's own guardrail is what catches this, because build_ecn()
        calls Decision.validate() before reading anything -- which is the
        intended order.
        """
        decision = mk.approved()
        decision.member_results[0].safety_factor = 0.4
        decision.member_results[0].status = MemberStatus.FAIL
        decision.violating_member_ids = [decision.member_results[0].member_id]

        with self.assertRaises(ValueError) as ctx:
            build_ecn(decision)
        self.assertIn("can never be auto-approved", str(ctx.exception))

    def test_status_check_is_independent_of_the_contract_guardrail(self):
        """_status_for() re-checks the approve/violate rule itself.

        Decision.validate() already rejects this combination, so the only way
        to prove the ECN's own check is real is to call it directly -- and it
        is worth proving, because it decides the word at the top of a document
        a human signs.
        """
        from earl.delivery.ecn import _status_for

        decision = mk.approved()
        decision.member_results[0].safety_factor = 0.4   # validate() not run
        with self.assertRaises(ValueError) as ctx:
            _status_for(decision)
        self.assertIn("refusing to write an APPROVED ECN", str(ctx.exception))

    def test_validate_runs_before_anything_is_read(self):
        """A decision claiming ESCALATED with nothing below threshold is
        rejected by the contract, and the ECN must not paper over it."""
        decision = mk.approved()
        decision.outcome = Outcome.ESCALATED
        with self.assertRaises(ValueError):
            build_ecn(decision)

    def test_non_si_units_are_refused_not_silently_misread(self):
        decision = mk.escalated()
        decision.units = Units(system=UnitSystem.IMPERIAL)
        with self.assertRaises(ValueError):
            build_ecn(decision)


class TestCrossCheckIsNeverSwallowed(unittest.TestCase):

    def test_disagreement_is_stated_prominently(self):
        ecn = enriched(mk.cross_check_disagreement())
        # Asserted on the note rather than the rendered text: the renderer
        # wraps, so a phrase can legitimately straddle two lines.
        self.assertIn("DISAGREE", ecn.cross_check_note)
        self.assertIn("should not be relied on", ecn.cross_check_note)
        self.assertIn("DISAGREE", render_text(ecn))

    def test_agreement_is_stated_too(self):
        ecn = enriched(mk.escalated())
        self.assertIn("AGREE", ecn.cross_check_note)
        self.assertNotIn("DISAGREE", ecn.cross_check_note)

    def test_escalation_without_a_cross_check_says_so(self):
        decision = mk.escalated()
        decision.cross_check = CrossCheck()
        ecn = build_ecn(decision)
        self.assertIn("NOT PERFORMED", ecn.cross_check_note)
        self.assertIn("no independent second-solver", ecn.cross_check_note)

    def test_escalation_without_a_report_flags_the_gap(self):
        decision = mk.escalated()
        decision.skyciv_report = None
        ecn = build_ecn(decision)
        joined = " ".join(ecn.evidence)
        self.assertIn("No SkyCiv report", joined)
        self.assertIn("open item", joined)

    def test_report_with_no_url_or_file_is_flagged_as_unopenable(self):
        decision = mk.escalated()
        decision.skyciv_report = SkyCivReport(report_id="SKYCIV-X")
        ecn = build_ecn(decision)
        self.assertIn("WARNING", " ".join(ecn.evidence))


# --------------------------------------------------------------------------
# Templated, not freeform
# --------------------------------------------------------------------------

class TestDeterminism(unittest.TestCase):

    def test_same_decision_renders_byte_identical_text(self):
        for name in mk.ALL:
            with self.subTest(name=name):
                a = render_text(enriched(mk.ALL[name]()))
                b = render_text(enriched(mk.ALL[name]()))
                self.assertEqual(a, b)

    def test_same_decision_renders_byte_identical_markdown(self):
        a = render_markdown(enriched(mk.escalated()))
        b = render_markdown(enriched(mk.escalated()))
        self.assertEqual(a, b)

    def test_ecn_id_is_derived_not_random(self):
        self.assertEqual(build_ecn(mk.escalated()).id, "ECN-CHG-M7-TRIM")
        self.assertEqual(
            build_ecn(mk.escalated()).id, build_ecn(mk.escalated()).id
        )


class TestEverySectionIsAlwaysPresent(unittest.TestCase):

    SECTIONS = (
        "1. CHANGE",
        "2. SOURCE",
        "3. DISPOSITION",
        "4. GOVERNING MEMBER",
        "5. AFFECTED ITEMS",
        "6. SUPPORTING EVIDENCE",
        "7. INDEPENDENT CROSS-CHECK",
        "8. SOLVER SANITY CHECKS",
    )

    def test_text_carries_every_section_for_every_scenario(self):
        for name in mk.ALL:
            text = render_text(enriched(mk.ALL[name]()))
            for section in self.SECTIONS:
                with self.subTest(name=name, section=section):
                    self.assertIn(section, text)

    def test_sections_appear_in_order(self):
        text = render_text(enriched(mk.escalated()))
        positions = [text.index(s) for s in self.SECTIONS]
        self.assertEqual(positions, sorted(positions))

    def test_markdown_carries_the_same_sections(self):
        md = render_markdown(enriched(mk.escalated()))
        for heading in ("## 1. Change", "## 2. Source", "## 3. Disposition",
                        "## 4. Governing member", "## 6. Supporting evidence",
                        "## 7. Independent cross-check",
                        "## 8. Solver sanity checks"):
            with self.subTest(heading=heading):
                self.assertIn(heading, md)

    def test_empty_sections_say_so_rather_than_vanish(self):
        """The error scenario has no members and no report; the sections still
        appear, stating that."""
        text = render_text(enriched(mk.solver_error()))
        self.assertIn("5. AFFECTED ITEMS", text)
        self.assertIn("No members were evaluated", text)
        self.assertIn("No external analysis report", text)


# --------------------------------------------------------------------------
# Degrading honestly without the optional inputs
# --------------------------------------------------------------------------

class TestDecisionOnly(unittest.TestCase):
    """Sprint 3A's actual contract: a Decision alone must produce a valid ECN."""

    def test_builds_from_a_decision_with_no_graph_and_no_walk(self):
        ecn = build_ecn(mk.escalated())
        self.assertIs(ecn.status, ECNStatus.HELD)
        self.assertEqual(ecn.governing.member_id, "m5")
        self.assertTrue(render_text(ecn))

    def test_missing_source_is_stated_not_faked(self):
        ecn = build_ecn(mk.escalated())
        self.assertIsNone(ecn.source)
        text = render_text(ecn)
        self.assertIn("No Onshape source reference", text)
        self.assertTrue(any("no dependency graph" in n.lower() for n in ecn.notes))

    def test_provenance_degrades_to_a_claim_it_can_support(self):
        ecn = build_ecn(mk.escalated())
        for line in ecn.lines:
            self.assertNotIn("hop", (line.provenance_short or ""))
            self.assertIn("downstream", line.provenance_short or "")

    def test_walk_supplies_real_traversal_provenance(self):
        ecn = enriched(mk.escalated())
        m5 = next(l for l in ecn.lines if l.member_id == "m5")
        self.assertEqual(m5.provenance_short, "1 hop via topology")
        self.assertIn("m7 -> m5", m5.provenance)
        m7 = next(l for l in ecn.lines if l.member_id == "m7")
        self.assertEqual(m7.provenance_short, "change target")
        self.assertTrue(m7.is_change_target)


# --------------------------------------------------------------------------
# Presentation
# --------------------------------------------------------------------------

class TestOrderingAndUnits(unittest.TestCase):

    def test_failures_are_listed_first(self):
        ecn = enriched(mk.escalated())
        self.assertEqual(ecn.lines[0].member_id, "m5")
        self.assertTrue(ecn.lines[0].failed)

    def test_passing_members_ascend_by_safety_factor(self):
        ecn = enriched(mk.escalated())
        passing = [l.safety_factor for l in ecn.lines if not l.failed]
        self.assertEqual(passing, sorted(passing))

    def test_governing_member_is_the_lowest_safety_factor(self):
        ecn = enriched(mk.escalated())
        self.assertEqual(ecn.governing.member_id, "m5")
        self.assertEqual(
            ecn.governing.safety_factor,
            min(l.safety_factor for l in ecn.lines),
        )

    def test_stress_is_shown_in_both_si_and_imperial(self):
        text = render_text(enriched(mk.escalated()))
        self.assertIn("MPa", text)
        self.assertIn("ksi", text)

    def test_output_is_ascii_only(self):
        """A Windows console codepage turns non-ASCII into replacement
        characters, and mojibake in a live demo reads as a broken calculation."""
        for name in mk.ALL:
            for render in (render_text, render_markdown):
                with self.subTest(name=name, render=render.__name__):
                    out = render(enriched(mk.ALL[name]()))
                    self.assertTrue(out.isascii(), f"non-ascii in {name}")

    def test_text_lines_stay_within_the_page_width(self):
        for name in mk.ALL:
            for line in render_text(enriched(mk.ALL[name]())).splitlines():
                with self.subTest(name=name, line=line):
                    self.assertLessEqual(len(line), 80)

    def test_missing_numbers_render_as_a_dash_not_none(self):
        decision = mk.escalated()
        decision.member_results[0].utilization = None
        text = render_text(build_ecn(decision))
        self.assertNotIn("None", text)

    def test_units_helpers_reject_nothing_and_invent_nothing(self):
        si = mk.escalated().units
        self.assertEqual(fmt.stress(None, si), "-")
        self.assertEqual(fmt.ratio(None), "-")
        self.assertIn("MPa", fmt.stress(1.724e8, si))
        self.assertIn("ksi", fmt.stress(1.724e8, si))

    def test_imperial_payload_leads_with_imperial(self):
        """The contract says consumers must CHECK units, not assume them."""
        imperial = Units(system=UnitSystem.IMPERIAL)
        rendered = fmt.stress(1.724e8, imperial)
        self.assertLess(rendered.index("ksi"), rendered.index("MPa"))


# --------------------------------------------------------------------------
# The ECN as a ledger record
# --------------------------------------------------------------------------

class TestSerialization(unittest.TestCase):

    def test_round_trips_through_json(self):
        original = enriched(mk.escalated())
        restored = ECN.from_json(original.to_json())

        self.assertEqual(restored.id, original.id)
        self.assertIs(restored.status, ECNStatus.HELD)
        self.assertEqual(len(restored.lines), len(original.lines))
        self.assertIs(restored.lines[0].status, MemberStatus.FAIL)
        self.assertIsNotNone(restored.source)
        self.assertEqual(restored.source.branch_name, original.source.branch_name)
        self.assertEqual(render_text(restored), render_text(original))

    def test_json_is_plain_types(self):
        payload = json.loads(enriched(mk.escalated()).to_json())
        self.assertEqual(payload["status"], "held")
        self.assertEqual(payload["lines"][0]["status"], "fail")

    def test_round_trips_when_optional_blocks_are_absent(self):
        original = build_ecn(mk.solver_error())
        restored = ECN.from_json(original.to_json())
        self.assertIsNone(restored.source)
        self.assertEqual(restored.lines, [])
        self.assertIs(restored.status, ECNStatus.BLOCKED)


if __name__ == "__main__":
    unittest.main()
