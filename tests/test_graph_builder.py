"""Sprint 2A tests: mate connectors, topology recovery, and graph assembly.

Runs offline against the recorded fixture, which now holds the assembly
definition fetched with `includeMateConnectors=true` -- all 20 connectors, not
just the 13 the nine mates happen to consume.
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
    OnshapeRef,
    SupportType,
)
from earl.ingestion.benchmark import benchmark_spec  # noqa: E402
from earl.ingestion.graph_builder import (  # noqa: E402
    build_graph,
    build_id_map,
    topology_edges,
    translate_edges,
)
from earl.ingestion.parsers import (  # noqa: E402
    MateConnector,
    derive_topology,
    parse_instances,
    parse_mate_connectors,
)

RECORDED = ROOT / "tests" / "fixtures" / "onshape"

# The published 10-bar truss connectivity. Mirrors truss_modelling_spec.md.
SPEC_MEMBERS = {
    "m1": {"n5", "n3"}, "m2": {"n3", "n1"}, "m3": {"n6", "n4"},
    "m4": {"n4", "n2"}, "m5": {"n3", "n4"}, "m6": {"n1", "n2"},
    "m7": {"n5", "n4"}, "m8": {"n6", "n3"}, "m9": {"n3", "n2"},
    "m10": {"n4", "n1"},
}


def load_assembly() -> dict:
    return json.loads(
        (RECORDED / "assemblies_d_w_e.json").read_text(encoding="utf-8")
    )


def a_change() -> ChangeEvent:
    return ChangeEvent(
        id="chg-test",
        kind=ChangeKind.VARIABLE_EDIT,
        description="barArea reduced from 1 in^2 to 0.4 in^2",
        target_id="m5",
        value_before="1 in^2",
        value_after="0.4 in^2",
    )


def a_source() -> OnshapeRef:
    return OnshapeRef(
        document_id="74352477fea92ae1dadf61d6",
        workspace_id="7121b248aa290fdccdc1afa6",
        element_id="72f445c1fb5399de58418375",
        branch_name="earl-eval-chg-test",
    )


class TestMateConnectors(unittest.TestCase):
    """The fix for the gap that made topology unrecoverable: mates name only
    13 of the 20 connectors, so seven member endpoints had no coordinate."""

    def setUp(self):
        self.connectors = parse_mate_connectors(load_assembly())

    def test_all_twenty_connectors_present(self):
        self.assertEqual(len(self.connectors), 20)

    def test_every_connector_is_named(self):
        """Our FeatureScript naming survives into the API response."""
        self.assertTrue(all(c.is_named for c in self.connectors))

    def test_two_connectors_per_member(self):
        per_member: dict[str, int] = {}
        for c in self.connectors:
            per_member[c.member_name] = per_member.get(c.member_name, 0) + 1
        self.assertEqual(len(per_member), 10)
        self.assertTrue(all(n == 2 for n in per_member.values()))

    def test_covers_the_seven_connectors_no_mate_names(self):
        """These are exactly the endpoints lost when reading mates alone."""
        unmated = {"m3_n6", "m4_n2", "m5_n4", "m6_n2", "m8_n6", "m9_n2", "m10_n1"}
        found = {f"{c.member_name}_{c.node_name}" for c in self.connectors}
        self.assertTrue(unmated.issubset(found))

    def test_missing_key_yields_empty_not_error(self):
        """A response fetched without includeMateConnectors must parse to an
        empty list, not raise -- and the emptiness must be detectable."""
        self.assertEqual(parse_mate_connectors({"parts": [{"partId": "X"}]}), [])
        self.assertEqual(parse_mate_connectors({}), [])


class TestTopologyRecovery(unittest.TestCase):
    def setUp(self):
        self.topology = derive_topology(parse_mate_connectors(load_assembly()))

    def test_recovers_six_nodes(self):
        self.assertEqual(len(self.topology.node_positions), 6)

    def test_node_coordinates_are_the_benchmark_bays(self):
        """360 in and 720 in bays are 9.144 m and 18.288 m."""
        pos = self.topology.node_positions
        self.assertEqual(tuple(round(v, 3) for v in pos["n5"]), (0.0, 9.144, 0.0))
        self.assertEqual(tuple(round(v, 3) for v in pos["n3"]), (9.144, 9.144, 0.0))
        self.assertEqual(tuple(round(v, 3) for v in pos["n1"]), (18.288, 9.144, 0.0))
        self.assertEqual(tuple(round(v, 3) for v in pos["n6"]), (0.0, 0.0, 0.0))

    def test_member_connectivity_matches_the_published_benchmark(self):
        recovered = {m: set(e) for m, e in self.topology.member_ends.items()}
        self.assertEqual(recovered, SPEC_MEMBERS)

    def test_complete_and_validates(self):
        self.assertTrue(self.topology.is_complete)
        self.topology.validate()

    def test_empty_input_is_flagged_not_silently_empty(self):
        empty = derive_topology([])
        self.assertFalse(empty.is_complete)
        self.assertIn("includeMateConnectors", empty.unresolved[0])
        with self.assertRaises(ValueError):
            empty.validate()

    def test_node_identity_comes_from_position_not_name(self):
        """Two connectors at one point are one node even if named differently;
        the contradiction is reported rather than smoothed over."""
        clashing = [
            MateConnector("P1", "f.m1_n1", (0.0, 0.0, 0.0), "m1", "n1"),
            MateConnector("P2", "f.m2_n9", (0.0, 0.0, 0.0), "m2", "n9"),
            MateConnector("P1", "f.m1_n2", (1.0, 0.0, 0.0), "m1", "n2"),
            MateConnector("P2", "f.m2_n3", (2.0, 0.0, 0.0), "m2", "n3"),
        ]
        topology = derive_topology(clashing)
        self.assertEqual(len(topology.node_positions), 3)
        self.assertTrue(
            any("conflicting node names" in u for u in topology.unresolved)
        )

    def test_member_with_one_endpoint_is_reported_not_dropped(self):
        """A member that vanishes from the graph is a dropped domino."""
        lone = [MateConnector("P1", "f.m1_n1", (0.0, 0.0, 0.0), "m1", "n1")]
        topology = derive_topology(lone)
        self.assertNotIn("m1", topology.member_ends)
        self.assertTrue(any("m1" in u for u in topology.unresolved))

    def test_unnamed_connectors_fall_back_to_coordinates(self):
        """Node identity must survive connectors that do not follow our
        FeatureScript naming -- Onshape guarantees no such convention."""
        anonymous = [
            MateConnector("P1", "f.someMateConnector", (0.0, 0.0, 0.0)),
            MateConnector("P1", "f.someMateConnector", (1.0, 0.0, 0.0)),
            MateConnector("P2", "f.someMateConnector", (1.0, 0.0, 0.0)),
            MateConnector("P2", "f.someMateConnector", (2.0, 0.0, 0.0)),
        ]
        topology = derive_topology(anonymous)
        topology.validate()
        self.assertEqual(len(topology.node_positions), 3)
        self.assertEqual(len(topology.member_ends), 2)


class TestIdTranslation(unittest.TestCase):
    """Onshape speaks occurrence ids, the contract speaks member ids. Edges
    handed across untranslated fail graph.validate()."""

    def setUp(self):
        assembly = load_assembly()
        self.instances = parse_instances(assembly)
        self.topology = derive_topology(parse_mate_connectors(assembly))
        self.id_map = build_id_map(self.instances, self.topology)

    def test_every_occurrence_maps_to_a_member(self):
        self.assertEqual(len(self.id_map.occurrence_to_member), 10)
        self.assertEqual(self.id_map.unmapped_occurrences, [])

    def test_mapping_is_one_to_one(self):
        self.assertEqual(
            sorted(self.id_map.member_to_occurrence),
            sorted(self.id_map.occurrence_to_member.values()),
        )

    def test_translates_occurrence_edges_to_member_edges(self):
        occ = list(self.id_map.occurrence_to_member)
        edges = [Edge(occ[0], occ[1], EdgeKind.MATE)]
        translated, problems = translate_edges(edges, self.id_map)
        self.assertEqual(problems, [])
        self.assertEqual(len(translated), 1)
        self.assertIn(translated[0].source_id, SPEC_MEMBERS)
        self.assertIn(translated[0].target_id, SPEC_MEMBERS)

    def test_untranslatable_edge_is_reported_not_silently_dropped(self):
        edges = [Edge("M_unknown", "M_alsoUnknown", EdgeKind.MATE)]
        translated, problems = translate_edges(edges, self.id_map)
        self.assertEqual(translated, [])
        self.assertEqual(len(problems), 1)
        self.assertIn("no member", problems[0])


class TestTopologyEdges(unittest.TestCase):
    def test_members_sharing_a_node_are_coupled_both_ways(self):
        topology = derive_topology(parse_mate_connectors(load_assembly()))
        edges = topology_edges(topology)
        pairs = {(e.source_id, e.target_id) for e in edges}
        # m1 (n5-n3) and m2 (n3-n1) meet at n3.
        self.assertIn(("m1", "m2"), pairs)
        self.assertIn(("m2", "m1"), pairs)
        # m6 (n1-n2) and m3 (n6-n4) share no node.
        self.assertNotIn(("m6", "m3"), pairs)

    def test_all_edges_are_tagged_topology(self):
        topology = derive_topology(parse_mate_connectors(load_assembly()))
        self.assertTrue(
            all(e.kind is EdgeKind.TOPOLOGY for e in topology_edges(topology))
        )


class TestBenchmarkSpec(unittest.TestCase):
    """Supports and loads are not Onshape concepts, so they come from here."""

    def setUp(self):
        self.spec = benchmark_spec()

    def test_supports_are_pinned_at_n5_and_n6(self):
        self.assertIs(self.spec.support_for("n5"), SupportType.PIN)
        self.assertIs(self.spec.support_for("n6"), SupportType.PIN)

    def test_unlisted_nodes_are_free(self):
        self.assertIs(self.spec.support_for("n1"), SupportType.FREE)

    def test_loads_are_100_kip_down_at_n2_and_n4(self):
        loads = {p.node_id: p.fy for p in self.spec.load_case.point_loads}
        self.assertEqual(sorted(loads), ["n2", "n4"])
        for fy in loads.values():
            self.assertAlmostEqual(fy, -444_822.2, places=1)

    def test_material_is_the_published_benchmark_not_a36_steel(self):
        """E = 10^7 psi and 25 ksi allowable are aluminium. Pairing that E
        with steel's 36 ksi yield reads ~44% safer than the benchmark."""
        psi = 6894.757
        self.assertAlmostEqual(
            self.spec.material.elastic_modulus / psi / 1e6, 10.0, places=2
        )
        self.assertAlmostEqual(
            self.spec.material.yield_strength / psi / 1000, 25.0, places=2
        )

    def test_area_tracks_the_barArea_variable(self):
        resized = benchmark_spec(area=0.4 * 0.0254**2)
        self.assertAlmostEqual(resized.section.area, 0.4 * 0.0254**2)


class TestBuildGraph(unittest.TestCase):
    """End to end: recorded Onshape response -> contract-valid DependencyGraph."""

    def setUp(self):
        self.result = build_graph(
            load_assembly(),
            graph_id="graph-test-001",
            change=a_change(),
            source=a_source(),
        )
        self.graph = self.result.graph

    def test_build_is_clean(self):
        self.assertTrue(self.result.is_clean, self.result.problems)

    def test_graph_passes_contract_validation(self):
        self.graph.validate()

    def test_has_the_full_truss(self):
        self.assertEqual(len(self.graph.nodes), 6)
        self.assertEqual(len(self.graph.members), 10)

    def test_members_are_ordered_m1_to_m10_not_lexicographically(self):
        self.assertEqual(
            [m.id for m in self.graph.members],
            [f"m{n}" for n in range(1, 11)],
        )

    def test_supports_were_injected_from_the_spec(self):
        """Without this the graph is unconstrained and validate() rejects it."""
        pinned = {n.id for n in self.graph.nodes if n.support is SupportType.PIN}
        self.assertEqual(pinned, {"n5", "n6"})

    def test_loads_were_injected_and_land_on_real_nodes(self):
        node_ids = {n.id for n in self.graph.nodes}
        loads = self.graph.load_cases[0].point_loads
        self.assertEqual(len(loads), 2)
        for load in loads:
            self.assertIn(load.node_id, node_ids)

    def test_edges_use_member_ids_not_occurrence_ids(self):
        member_ids = {m.id for m in self.graph.members}
        for edge in self.graph.edges:
            self.assertIn(edge.source_id, member_ids)
            self.assertIn(edge.target_id, member_ids)

    def test_carries_both_mate_and_topology_provenance(self):
        kinds = {e.kind for e in self.graph.edges}
        self.assertIn(EdgeKind.MATE, kinds)
        self.assertIn(EdgeKind.TOPOLOGY, kinds)

    def test_mate_edges_keep_their_label_where_they_overlap_topology(self):
        """A mate is the stronger provenance claim for an escalation to cite."""
        mate_pairs = {
            (e.source_id, e.target_id)
            for e in self.graph.edges
            if e.kind is EdgeKind.MATE
        }
        topo_pairs = {
            (e.source_id, e.target_id)
            for e in self.graph.edges
            if e.kind is EdgeKind.TOPOLOGY
        }
        self.assertEqual(mate_pairs & topo_pairs, set())
        self.assertEqual(len(mate_pairs), 18)

    def test_members_carry_onshape_provenance(self):
        """An escalation must point at a real CAD entity, not an index."""
        for member in self.graph.members:
            self.assertTrue(member.onshape_id)

    def test_survives_json_round_trip(self):
        restored = DependencyGraph.from_json(self.graph.to_json())
        restored.validate()
        self.assertEqual(restored.to_dict(), self.graph.to_dict())

    def test_strict_build_raises_when_connectors_are_missing(self):
        """A response fetched without includeMateConnectors must fail loudly,
        not produce an empty-but-valid-looking graph."""
        stripped = load_assembly()
        for part in stripped["parts"]:
            part.pop("mateConnectors", None)
        with self.assertRaises(ValueError) as ctx:
            build_graph(
                stripped,
                graph_id="graph-test-002",
                change=a_change(),
                source=a_source(),
            )
        self.assertIn("includeMateConnectors", str(ctx.exception))


class TestBenchmarkConstantsDoNotDrift(unittest.TestCase):
    """`scripts/contract_selfcheck.py` and `earl/ingestion/benchmark.py` both
    state the 10-bar benchmark in SI. Two independent statements of the same
    constants is exactly how a units or material mismatch creeps in -- the
    failure mode the contracts README calls out by name -- so pin them
    together rather than trusting both to be edited at once.
    """

    @classmethod
    def setUpClass(cls):
        import importlib.util

        path = ROOT / "scripts" / "contract_selfcheck.py"
        spec = importlib.util.spec_from_file_location("_selfcheck", path)
        cls.selfcheck = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.selfcheck)

    def test_constants_agree(self):
        from earl.ingestion import benchmark as b

        pairs = [
            ("elastic modulus", self.selfcheck.E_AL, b.E_ALUMINIUM),
            ("allowable stress", self.selfcheck.ALLOWABLE, b.ALLOWABLE_STRESS),
            ("density", self.selfcheck.DENSITY, b.DENSITY),
            ("area", self.selfcheck.AREA, b.DEFAULT_AREA),
            ("load", self.selfcheck.P, b.BENCHMARK_LOAD),
        ]
        for label, stated, canonical in pairs:
            with self.subTest(constant=label):
                rel = abs(stated - canonical) / max(abs(stated), abs(canonical))
                self.assertLess(
                    rel, 2e-3,
                    f"{label}: selfcheck says {stated:g}, benchmark.py says "
                    f"{canonical:g} ({rel:.1%} apart)",
                )

    def test_material_id_matches(self):
        from earl.ingestion import benchmark as b

        graph = self.selfcheck.build_graph()
        self.assertEqual(graph.materials[0].id, b.MATERIAL_ID)

    def test_selfcheck_material_is_not_labelled_steel(self):
        """E = 10^7 psi is aluminium. Labelling it steel is the mismatch that
        made capacity read ~44% high."""
        graph = self.selfcheck.build_graph()
        self.assertNotIn("steel", graph.materials[0].name.lower())


if __name__ == "__main__":
    unittest.main()
