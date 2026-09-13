"""Sprint 2A tests: the dependency graph walker.

plan.md's completeness claim rests on this traversal being a fixed algorithm
rather than a model decision, so the two properties under test above all else
are COMPLETENESS (every edge is crossed) and DETERMINISM (same input, same
output, every run).
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from earl.contracts.graph import (  # noqa: E402
    ChangeEvent,
    ChangeKind,
    DependencyGraph,
    Edge,
    EdgeKind,
    LoadCase,
    Material,
    Member,
    Node,
    OnshapeRef,
    PointLoad,
    Section,
    SupportType,
)
from earl.ingestion.graph_builder import build_graph  # noqa: E402
from earl.ingestion.walker import (  # noqa: E402
    apply_walk,
    seed_ids,
    walk,
    walk_and_apply,
)

RECORDED = ROOT / "tests" / "fixtures" / "onshape"

SOURCE = OnshapeRef(
    document_id="74352477fea92ae1dadf61d6",
    workspace_id="7121b248aa290fdccdc1afa6",
    element_id="72f445c1fb5399de58418375",
    branch_name="earl-eval-test",
)


def load_assembly() -> dict:
    return json.loads(
        (RECORDED / "assemblies_d_w_e.json").read_text(encoding="utf-8")
    )


def member_change(target: str = "m5") -> ChangeEvent:
    return ChangeEvent(
        id="chg-member",
        kind=ChangeKind.FEATURE_EDIT,
        description=f"{target} cross-section reduced from 1 in^2 to 0.4 in^2",
        target_id=target,
        value_before="1 in^2",
        value_after="0.4 in^2",
    )


def variable_change(target: str = "barArea") -> ChangeEvent:
    return ChangeEvent(
        id="chg-var",
        kind=ChangeKind.VARIABLE_EDIT,
        description=f"{target} reduced from 1 in^2 to 0.4 in^2",
        target_id=target,
        value_before="1 in^2",
        value_after="0.4 in^2",
    )


def truss(change: ChangeEvent) -> DependencyGraph:
    return build_graph(
        load_assembly(), graph_id="g-walk", change=change, source=SOURCE
    ).graph


def toy_graph(change: ChangeEvent, edges: list[Edge]) -> DependencyGraph:
    """A tiny hand-built graph for cases the real truss cannot exhibit --
    notably a member that nothing connects to."""
    return DependencyGraph(
        id="g-toy",
        change=change,
        source=SOURCE,
        nodes=[
            Node("n1", 0.0, 0.0, support=SupportType.PIN),
            Node("n2", 1.0, 0.0),
            Node("n3", 2.0, 0.0),
            Node("n4", 3.0, 0.0),
        ],
        members=[
            Member("m1", "n1", "n2", "s", "mat"),
            Member("m2", "n2", "n3", "s", "mat"),
            Member("m3", "n3", "n4", "s", "mat"),
        ],
        materials=[Material("mat", "test", 1.0, 1.0)],
        sections=[Section("s", "test", 1.0)],
        load_cases=[LoadCase("lc", "test", [PointLoad("p", "n4", fy=-1.0)])],
        edges=edges,
    )


class TestSeeding(unittest.TestCase):
    def test_member_target_seeds_itself(self):
        seeds, problems = seed_ids(truss(member_change("m5")))
        self.assertEqual(seeds, ["m5"])
        self.assertEqual(problems, [])

    def test_variable_target_seeds_from_the_variable(self):
        """A variable is not an entity, but VARIABLE_REF edges hang off it."""
        seeds, problems = seed_ids(truss(variable_change("barArea")))
        self.assertEqual(seeds, ["barArea"])
        self.assertEqual(problems, [])

    def test_target_matching_nothing_is_flagged_not_silently_empty(self):
        """A walk with no seed reports nothing affected -- which would read
        exactly like a safe change."""
        graph = toy_graph(member_change("m99"), edges=[])
        seeds, problems = seed_ids(graph)
        self.assertEqual(seeds, [])
        self.assertEqual(len(problems), 1)
        self.assertIn("nothing to start from", problems[0])

    def test_non_variable_change_on_a_non_entity_is_flagged(self):
        change = ChangeEvent(
            id="c", kind=ChangeKind.FEATURE_EDIT,
            description="odd", target_id="someVariable",
        )
        graph = toy_graph(
            change, [Edge("someVariable", "m1", EdgeKind.VARIABLE_REF)]
        )
        seeds, problems = seed_ids(graph)
        self.assertEqual(seeds, ["someVariable"])
        self.assertTrue(any("not variable_edit" in p for p in problems))


class TestCompleteness(unittest.TestCase):
    """The truss is connected and statically indeterminate, so a change really
    does reach every member. Over-reporting is the safe direction: a dropped
    domino is a member whose unsafe state went UNreported."""

    def test_member_change_reaches_every_member(self):
        result = walk(truss(member_change("m5")))
        self.assertEqual(len(result.member_ids), 10)
        self.assertEqual(result.unreached_member_ids, [])

    def test_variable_change_reaches_every_member(self):
        result = walk(truss(variable_change("barArea")))
        self.assertEqual(len(result.member_ids), 10)
        self.assertEqual(result.unreached_member_ids, [])

    def test_every_node_is_touched(self):
        result = walk(truss(member_change("m5")))
        self.assertEqual(result.node_ids, ["n1", "n2", "n3", "n4", "n5", "n6"])

    def test_disconnected_member_is_reported_as_unreached(self):
        """The one case the real truss cannot show. An unreachable member must
        surface, not vanish."""
        graph = toy_graph(
            member_change("m1"), [Edge("m1", "m2", EdgeKind.TOPOLOGY)]
        )
        result = walk(graph)
        self.assertEqual(result.member_ids, ["m1", "m2"])
        self.assertEqual(result.unreached_member_ids, ["m3"])


class TestDistanceAndProvenance(unittest.TestCase):
    """Set membership does not discriminate on a connected truss; distance and
    edge kind do, and that is what the ECN and the demo lean on."""

    def setUp(self):
        self.result = walk(truss(member_change("m5")))

    def test_change_target_is_distance_zero(self):
        self.assertEqual(self.result.distance("m5"), 0)
        self.assertTrue(self.result.reach("m5").is_seed)

    def test_members_sharing_a_node_are_one_hop(self):
        """m5 spans n3-n4; m1, m2, m8, m9 meet it at n3, m3, m4, m7, m10 at n4."""
        self.assertEqual(
            self.result.at_distance(1),
            ["m1", "m2", "m3", "m4", "m7", "m8", "m9", "m10"],
        )

    def test_the_only_distant_member_is_the_one_sharing_no_node(self):
        """m6 spans n1-n2 and m5 spans n3-n4, so m6 is genuinely two hops."""
        self.assertEqual(self.result.at_distance(2), ["m6"])
        self.assertEqual(self.result.max_distance, 2)

    def test_reach_records_the_edge_kind_that_found_it(self):
        self.assertIsNone(self.result.reach("m5").via)
        self.assertIn(
            self.result.reach("m6").via, (EdgeKind.MATE, EdgeKind.TOPOLOGY)
        )

    def test_path_starts_at_the_seed_and_ends_at_the_entity(self):
        path = self.result.reach("m6").path
        self.assertEqual(path[0], "m5")
        self.assertEqual(path[-1], "m6")

    def test_explain_is_human_readable(self):
        self.assertIn("change target", self.result.explain("m5"))
        self.assertIn("2 hop(s)", self.result.explain("m6"))
        self.assertIn("not downstream", self.result.explain("m404"))

    def test_variable_edit_puts_every_member_one_hop_out(self):
        result = walk(truss(variable_change("barArea")))
        self.assertEqual(result.max_distance, 1)
        self.assertEqual(
            result.reach("m6").via, EdgeKind.VARIABLE_REF
        )


class TestDeterminism(unittest.TestCase):
    """The baseline this is measured against may answer differently across
    runs. 'The same way every time' has to be a property of the code."""

    def test_repeated_walks_are_identical(self):
        graph = truss(member_change("m5"))
        first = walk(graph)
        for _ in range(5):
            again = walk(graph)
            self.assertEqual(again.member_ids, first.member_ids)
            self.assertEqual(again.node_ids, first.node_ids)
            self.assertEqual(
                {k: (v.distance, v.via, v.path) for k, v in again.reached.items()},
                {k: (v.distance, v.via, v.path) for k, v in first.reached.items()},
            )

    def test_edge_order_does_not_change_the_result(self):
        """A graph rebuilt with its edges shuffled must walk identically."""
        graph = truss(member_change("m5"))
        baseline = walk(graph)

        shuffled = DependencyGraph.from_dict(graph.to_dict())
        shuffled.edges = list(reversed(shuffled.edges))
        other = walk(shuffled)

        self.assertEqual(other.member_ids, baseline.member_ids)
        self.assertEqual(
            {k: v.distance for k, v in other.reached.items()},
            {k: v.distance for k, v in baseline.reached.items()},
        )

    def test_members_are_ordered_m1_to_m10_not_lexicographically(self):
        result = walk(truss(variable_change("barArea")))
        self.assertEqual(result.member_ids, [f"m{n}" for n in range(1, 11)])


class TestNarrowedWalksAnnounceThemselves(unittest.TestCase):
    """A walk that quietly stops early is how a domino gets dropped, so any
    restriction has to be recorded on the result."""

    def test_max_hops_is_recorded_and_enforced(self):
        result = walk(truss(member_change("m5")), max_hops=1)
        self.assertNotIn("m6", result.member_ids)
        self.assertEqual(result.unreached_member_ids, ["m6"])
        self.assertTrue(any("NOT a complete" in n for n in result.notes))

    def test_edge_kind_filter_is_recorded(self):
        result = walk(
            truss(member_change("m5")), edge_kinds=frozenset({EdgeKind.MATE})
        )
        self.assertTrue(any("NOT a complete" in n for n in result.notes))

    def test_unrestricted_walk_has_no_such_note(self):
        result = walk(truss(member_change("m5")))
        self.assertEqual(
            [n for n in result.notes if "NOT a complete" in n], []
        )


class TestApplyToGraph(unittest.TestCase):
    def test_graph_starts_with_empty_affected_sets(self):
        """Which is why the walker exists: Track B would otherwise receive a
        graph with nothing marked affected."""
        graph = truss(member_change("m5"))
        self.assertEqual(graph.affected_member_ids, [])
        self.assertEqual(graph.affected_node_ids, [])

    def test_walk_and_apply_populates_and_revalidates(self):
        graph = truss(member_change("m5"))
        walk_and_apply(graph)
        self.assertEqual(len(graph.affected_member_ids), 10)
        self.assertEqual(len(graph.affected_node_ids), 6)
        graph.validate()

    def test_walk_itself_does_not_mutate_the_graph(self):
        graph = truss(member_change("m5"))
        walk(graph)
        self.assertEqual(graph.affected_member_ids, [])

    def test_applied_graph_survives_json_round_trip(self):
        graph = truss(member_change("m5"))
        walk_and_apply(graph)
        restored = DependencyGraph.from_json(graph.to_json())
        restored.validate()
        self.assertEqual(restored.affected_member_ids, graph.affected_member_ids)

    def test_apply_never_names_an_unknown_member(self):
        """graph.validate() rejects affected_member_ids naming a stranger."""
        graph = truss(member_change("m5"))
        result = walk(graph)
        apply_walk(graph, result)
        member_ids = {m.id for m in graph.members}
        self.assertTrue(set(graph.affected_member_ids).issubset(member_ids))

    def test_variable_seed_is_not_leaked_into_affected_members(self):
        """'barArea' is reached by the walk but is not a member."""
        graph = truss(variable_change("barArea"))
        walk_and_apply(graph)
        self.assertNotIn("barArea", graph.affected_member_ids)
        self.assertNotIn("barArea", graph.affected_node_ids)
        graph.validate()


if __name__ == "__main__":
    unittest.main()
