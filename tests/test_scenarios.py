"""Scenario-set tests: the eval measures what it claims to. [Track A, Sprint 5A]

These are not unit tests of a builder -- they are the guard on the eval's
integrity. A scenario set can flatter either agent by accident, so the
properties the set is CHOSEN for are pinned here against the real solver:
the safe/unsafe split, the per-change-kind balance, and the fact that the
failing member is usually not the edited one.

If a solver change or an area tweak breaks one of these, the eval has
quietly stopped measuring what plan.md says it measures, and that must be a
test failure rather than a slightly different number on a slide.
Every test passes threshold=1.0 explicitly -- never the developer's .env.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from earl.analysis.scoreboard import ground_truth  # noqa: E402
from earl.contracts import ChangeKind, EdgeKind  # noqa: E402
from earl.eval.scenarios import (  # noqa: E402
    SCENARIOS,
    add_load,
    base_area_in2,
    move_load,
    remove,
    resize,
    scale,
    scenario,
    scenario_ids,
)
from earl.ingestion.walker import walk  # noqa: E402

THRESHOLD = 1.0


def _truth(scenario_obj) -> list[str]:
    return ground_truth(scenario_obj.build().after, threshold=THRESHOLD)


class TestSetShape(unittest.TestCase):
    """The set is the size and shape plan.md describes."""

    def test_twenty_scenarios_with_unique_ids(self):
        self.assertEqual(len(SCENARIOS), 20)
        self.assertEqual(len(set(scenario_ids())), 20)

    def test_every_change_kind_the_plan_names_is_present(self):
        kinds = {s.kind for s in SCENARIOS}
        self.assertEqual(
            kinds,
            {
                ChangeKind.FEATURE_EDIT,
                ChangeKind.MEMBER_REMOVED,
                ChangeKind.LOAD_ADDED,
                ChangeKind.LOAD_MOVED,
                ChangeKind.VARIABLE_EDIT,
            },
        )

    def test_lookup_by_full_id_and_by_prefix(self):
        self.assertIs(scenario("s01-thin-m7-demo"), SCENARIOS[0])
        self.assertIs(scenario("s01"), SCENARIOS[0])
        with self.assertRaises(KeyError):
            scenario("s99")


class TestEveryScenarioIsUsable(unittest.TestCase):
    """Each scenario builds a valid, solvable, walkable pair of graphs."""

    def test_graphs_validate_and_differ(self):
        for s in SCENARIOS:
            with self.subTest(s.id):
                graphs = s.build()
                graphs.before.validate()
                graphs.after.validate()
                self.assertNotEqual(graphs.before.id, graphs.after.id)

    def test_the_walk_reaches_every_member(self):
        """The trap from sprint3a_handover.md section 4: a graph with no
        edges, or a target that names nothing, walks to nothing. Every
        scenario must seed on a real entity and reach the whole truss."""
        for s in SCENARIOS:
            with self.subTest(s.id):
                after = s.build().after
                result = walk(after)
                self.assertEqual(result.notes, [], f"{s.id}: {result.notes}")
                self.assertEqual(
                    sorted(result.member_ids),
                    sorted(m.id for m in after.members),
                )

    def test_affected_sets_are_left_for_the_walk_to_fill(self):
        """A scenario that shipped its own affected set would be marking its
        own homework, and would hand the baseline the traversal it lacks."""
        for s in SCENARIOS:
            with self.subTest(s.id):
                graphs = s.build()
                self.assertEqual(graphs.after.affected_member_ids, [])
                self.assertEqual(graphs.before.affected_member_ids, [])

    def test_each_build_is_independent(self):
        """Two builds must not share mutable state -- the harness relies on
        this to keep the system's walk out of the baseline's graph."""
        first = scenario("s01").build().after
        first.affected_member_ids = ["m1"]
        second = scenario("s01").build().after
        self.assertEqual(second.affected_member_ids, [])

    def test_every_scenario_names_its_evaluation_branch(self):
        for s in SCENARIOS:
            with self.subTest(s.id):
                source = s.build().after.source
                self.assertEqual(source.branch_name, f"earl-eval-{s.id}")


class TestGroundTruthProperties(unittest.TestCase):
    """The properties the set exists to have, checked against the solver."""

    @classmethod
    def setUpClass(cls):
        cls.truth = {s.id: _truth(s) for s in SCENARIOS}

    def test_split_is_eleven_unsafe_nine_safe(self):
        unsafe = [sid for sid, t in self.truth.items() if t]
        safe = [sid for sid, t in self.truth.items() if not t]
        self.assertEqual(len(unsafe), 11, f"unsafe: {sorted(unsafe)}")
        self.assertEqual(len(safe), 9, f"safe: {sorted(safe)}")

    def test_escalate_everything_would_pay_for_it(self):
        """The safe scenarios are what make a blanket-escalation policy
        expensive. Without them it scores zero dropped dominoes -- a perfect
        headline number (sprint3a_handover.md section 3)."""
        safe = [s for s in SCENARIOS if not self.truth[s.id]]
        members = len(scenario("s01").build().after.members)
        self.assertGreaterEqual(len(safe) * members, 80)

    def test_every_change_kind_appears_both_safe_and_unsafe(self):
        """So neither agent can score well by learning 'removals are bad'."""
        for kind in {s.kind for s in SCENARIOS}:
            of_kind = [s for s in SCENARIOS if s.kind is kind]
            with self.subTest(kind.value):
                self.assertTrue(
                    any(self.truth[s.id] for s in of_kind),
                    f"{kind.value} has no unsafe scenario",
                )
                self.assertTrue(
                    any(not self.truth[s.id] for s in of_kind),
                    f"{kind.value} has no safe scenario",
                )

    def test_the_failing_member_is_usually_not_the_edited_one(self):
        """'Check what you touched' must lose. Ten of the eleven unsafe
        scenarios fail a member the change does not name."""
        downstream = [
            s.id
            for s in SCENARIOS
            if self.truth[s.id] and s.target_id not in self.truth[s.id]
        ]
        self.assertEqual(len(downstream), 10, f"downstream-only: {sorted(downstream)}")

    def test_the_demo_scenario_is_the_one_the_readme_describes(self):
        """s01 is the m7 -> m5 domino the whole project is built around."""
        self.assertEqual(self.truth["s01-thin-m7-demo"], ["m5"])

    def test_thickening_a_member_can_still_break_a_neighbour(self):
        """The counterintuitive cases, pinned: adding material is not safe by
        construction on an indeterminate truss."""
        self.assertEqual(self.truth["s06-thicken-m1"], ["m5"])
        self.assertEqual(self.truth["s07-thicken-m8"], ["m5"])

    def test_the_annotated_truths_in_the_module_still_hold(self):
        """Spot-check the comments next to the set against the solver, so a
        solver change cannot silently rewrite what the eval claims."""
        expected = {
            "s02-thin-m3": ["m5"],
            "s03-thin-m1-severe": ["m1", "m5"],
            "s09-remove-m7": ["m10", "m2", "m5", "m6"],
            "s13-load-n1-down": ["m6"],
            "s14-load-n1-lateral": ["m10", "m2"],
            "s17-move-n4-to-n1": ["m10", "m2", "m6"],
            "s19-var-load-110": ["m5"],
            "s04-thin-m4-half": [],
            "s12-remove-m5": [],
            "s20-var-area-110": [],
        }
        for scenario_id, unsafe in expected.items():
            with self.subTest(scenario_id):
                self.assertEqual(sorted(self.truth[scenario_id]), sorted(unsafe))


class TestConstructors(unittest.TestCase):
    """Each constructor produces the change the ECN will describe."""

    def test_resize_records_both_values_and_the_direction(self):
        thinner = resize("t", "m7", 6.0)
        self.assertIs(thinner.kind, ChangeKind.FEATURE_EDIT)
        self.assertEqual(thinner.target_id, "m7")
        self.assertIn("Reduced", thinner.description)
        self.assertEqual(thinner.value_after, "6 in^2")
        self.assertIn("Increased", resize("t", "m7", 99.0).description)

    def test_scale_is_resize_relative_to_the_design_area(self):
        halved = scale("t", "m4", 0.5)
        self.assertAlmostEqual(halved.areas_in2["m4"], base_area_in2("m4") * 0.5, places=3)

    def test_removal_drops_the_member_and_its_section(self):
        after = remove("t", "m7").build().after
        self.assertNotIn("m7", [m.id for m in after.members])
        self.assertNotIn("sec_m7", [s.id for s in after.sections])
        self.assertEqual(len(after.members), 9)

    def test_removal_keeps_the_before_state_intact(self):
        before = remove("t", "m7").build().before
        self.assertIn("m7", [m.id for m in before.members])

    def test_a_removed_member_still_seeds_the_walk_without_complaint(self):
        """The removed id is no longer an entity, which is the truth -- and
        `walker.seed_ids` accepts it for MEMBER_REMOVED rather than warning."""
        result = walk(remove("t", "m7").build().after)
        self.assertEqual(result.seeds, ("m7",))
        self.assertEqual(result.notes, [])

    def test_added_load_targets_the_node_and_emits_load_path_edges(self):
        s = add_load("t", "n1", down_kips=25.0)
        after = s.build().after
        self.assertIs(s.kind, ChangeKind.LOAD_ADDED)
        self.assertEqual(s.target_id, "n1")
        load_path = [e for e in after.edges if e.kind is EdgeKind.LOAD_PATH]
        self.assertTrue(load_path)
        self.assertTrue(all(e.source_id == "n1" for e in load_path))

    def test_added_load_appears_only_in_the_after_state(self):
        graphs = add_load("t", "n1", down_kips=25.0).build()
        ids = lambda g: {p.id for p in g.load_cases[0].point_loads}  # noqa: E731
        self.assertIn("p_n1_added", ids(graphs.after))
        self.assertNotIn("p_n1_added", ids(graphs.before))

    def test_add_load_with_no_load_is_refused(self):
        with self.assertRaises(ValueError):
            add_load("t", "n1")

    def test_moved_load_changes_its_node_and_seeds_from_both_ends(self):
        s = move_load("t", "p_n4", "n4", "n1")
        graphs = s.build()
        after_node = {p.id: p.node_id for p in graphs.after.load_cases[0].point_loads}
        before_node = {p.id: p.node_id for p in graphs.before.load_cases[0].point_loads}
        self.assertEqual(after_node["p_n4"], "n1")
        self.assertEqual(before_node["p_n4"], "n4")
        sources = {e.source_id for e in graphs.after.edges if e.kind is EdgeKind.LOAD_PATH}
        self.assertEqual(sources, {"n1", "n4"})

    def test_variable_edits_emit_variable_ref_edges_from_the_variable(self):
        after = scenario("s20-var-area-110").build().after
        refs = [e for e in after.edges if e.kind is EdgeKind.VARIABLE_REF]
        self.assertEqual(len(refs), len(after.members))
        self.assertTrue(all(e.source_id == "barArea" for e in refs))


if __name__ == "__main__":
    unittest.main()
