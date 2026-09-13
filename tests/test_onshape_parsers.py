"""Sprint 1A tests for the Onshape ingestion layer.

Runs entirely offline against recorded and synthetic fixtures -- no API calls,
so the suite is free to run and does not eat the annual request budget.

Two fixture sets, on purpose:
  * `fixtures/onshape/`   -- real responses recorded from the live document,
                             holding the modelled 10-bar truss: 10 members and
                             9 Fastened mates.
  * `fixtures/synthetic/` -- hand-built to Onshape's documented shape, covering
                             cases the real document does not currently exhibit
                             (shared parts, group mates, populated variables).
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from earl.contracts.graph import EdgeKind  # noqa: E402
from earl.ingestion.parsers import (  # noqa: E402
    Variable,
    build_where_used,
    diff_variables,
    evaluate_expression,
    mate_edges,
    parse_instances,
    parse_mates,
    parse_variables,
    where_used_edges,
)

RECORDED = ROOT / "tests" / "fixtures" / "onshape"
SYNTHETIC = ROOT / "tests" / "fixtures" / "synthetic"


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


class TestEmptyPayloads(unittest.TestCase):
    """Parsers must return empty results cleanly rather than raising or
    fabricating structure. Inline payloads, not fixtures, so these keep testing
    the empty case after the real document has been populated."""

    def test_empty_variables(self):
        self.assertEqual(parse_variables([{"variables": []}]), [])

    def test_empty_assembly(self):
        empty = {"rootAssembly": {"instances": [], "features": []}}
        self.assertEqual(parse_instances(empty), [])
        self.assertEqual(parse_mates(empty), [])
        self.assertEqual(mate_edges(parse_mates(empty)), [])
        self.assertEqual(where_used_edges(parse_instances(empty)), [])


class TestRecordedRealDocument(unittest.TestCase):
    """The recorded 10-bar truss: 10 members, 9 Fastened mates.

    Nine mates rather than fourteen is deliberate -- a Fastened mate removes
    all six DOF, so ten parts with one fixed need exactly nine to be fully
    constrained. More would be over-constrained and Onshape rejects them.
    """

    def setUp(self):
        self.assembly = load(RECORDED / "assemblies_d_w_e.json")
        self.features = load(RECORDED / "assemblies_d_w_e_features.json")
        self.instances = parse_instances(self.assembly)

    def test_ten_members_named_m1_to_m10(self):
        names = sorted(i.name.split(" <")[0] for i in self.instances)
        self.assertEqual(names, sorted(f"m{n}" for n in range(1, 11)))

    def test_every_member_is_a_distinct_part(self):
        """Distinct partIds are what make members individually addressable."""
        part_ids = {i.part_id for i in self.instances}
        self.assertEqual(len(part_ids), 10)

    def test_nine_fastened_mates(self):
        mates = parse_mates(self.assembly)
        self.assertEqual(len(mates), 9)
        self.assertTrue(all(m.mate_type == "FASTENED" for m in mates))

    def test_mates_produce_eighteen_directed_edges(self):
        self.assertEqual(len(mate_edges(parse_mates(self.assembly))), 18)

    def test_all_members_reachable_through_mates(self):
        """The nine mates must span all ten members -- an unreachable member
        would never be found by the downstream traversal."""
        edges = mate_edges(parse_mates(self.assembly))
        adjacency: dict[str, list[str]] = {}
        for e in edges:
            adjacency.setdefault(e.source_id, []).append(e.target_id)

        start = self.instances[0].id
        seen, stack = {start}, [start]
        while stack:
            for nxt in adjacency.get(stack.pop(), []):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        self.assertEqual(len(seen), 10)

    def test_both_endpoints_agree_on_mated_pairs(self):
        """The resolved and BTM authoring formats must yield the same graph.
        Parsing the authoring form with the resolved parser used to give mates
        with no occurrences -- correct-looking objects producing zero edges."""
        resolved = {frozenset(m.occurrences) for m in parse_mates(self.assembly)}
        authoring = {frozenset(m.occurrences) for m in parse_mates(self.features)}
        self.assertEqual(resolved, authoring)
        self.assertTrue(all(len(p) == 2 for p in authoring))

    def test_mate_connector_origins_are_node_positions(self):
        """Connector origins recover the truss node coordinates: 360 in and
        720 in bays are 9.144 m and 18.288 m."""
        origins = {
            tuple(round(v, 3) for v in o)
            for m in parse_mates(self.assembly)
            for o in m.origins
        }
        self.assertIn((0.0, 9.144, 0.0), origins)        # n5, a support
        self.assertIn((9.144, 9.144, 0.0), origins)      # n3, the busy node
        self.assertIn((18.288, 9.144, 0.0), origins)     # n1

    def test_featurescript_connector_ids_survive(self):
        """Our generated connector ids carry through the authoring form, so a
        mate can be traced back to the node it was made at."""
        ids = [cid for m in parse_mates(self.features) for cid in m.connector_ids]
        self.assertTrue(any(cid.endswith(".m1_n5") for cid in ids))
        self.assertEqual(len(ids), 18)

    def test_distinct_parts_are_not_coupled_by_where_used(self):
        """All ten members are distinct parts, so where-used couples nothing.
        Their real connectivity comes from mates, not shared parts."""
        self.assertEqual(where_used_edges(self.instances), [])


class TestVariableParsing(unittest.TestCase):
    def setUp(self):
        self.variables = parse_variables(load(SYNTHETIC / "variables_populated.json"))

    def test_parses_every_row(self):
        self.assertEqual(len(self.variables), 4)
        self.assertEqual(
            [v.name for v in self.variables],
            ["bayWidth", "bayHeight", "barArea", "safetyFactor"],
        )

    def test_keeps_expression_and_evaluated_value(self):
        """The authored expression is what a human recognises in an ECN; the
        evaluated SI value is what the solver needs. Losing either is a bug."""
        area = next(v for v in self.variables if v.name == "barArea")
        self.assertEqual(area.expression, "1 in^2")
        self.assertAlmostEqual(area.value, 0.00064516)
        self.assertEqual(area.type, "ANY")

    def test_tolerates_missing_fields(self):
        parsed = parse_variables([{"variables": [{"name": "bare"}]}])
        self.assertEqual(parsed[0].name, "bare")
        self.assertIsNone(parsed[0].value)

    def test_tolerates_null_payload(self):
        self.assertEqual(parse_variables(None), [])


class TestRecordedVariables(unittest.TestCase):
    """The real variable table. Onshape returns `value: null` here -- the
    endpoint hands back only the authored expression, never an evaluated
    number, so the SI conversion is ours to do."""

    def setUp(self):
        self.variables = parse_variables(
            load(RECORDED / "variables_d_w_e_variables.json")
        )

    def test_three_variables_present(self):
        self.assertEqual(
            [v.name for v in self.variables], ["bayWidth", "bayHeight", "barArea"]
        )

    def test_area_uses_any_type(self):
        """Onshape has no Area type, so barArea must be ANY -- Number is
        unitless and would reject in^2."""
        area = next(v for v in self.variables if v.name == "barArea")
        self.assertEqual(area.type, "ANY")
        self.assertEqual(area.expression, "1 in^2")

    def test_onshape_supplies_no_evaluated_value(self):
        self.assertTrue(all(v.value is None for v in self.variables))

    def test_si_values_are_derived_from_expressions(self):
        by_name = {v.name: v for v in self.variables}
        self.assertAlmostEqual(by_name["bayWidth"].si_value, 9.144)
        self.assertAlmostEqual(by_name["barArea"].si_value, 0.00064516)


class TestExpressionEvaluation(unittest.TestCase):
    def test_lengths(self):
        self.assertAlmostEqual(evaluate_expression("360 in"), 9.144)
        self.assertAlmostEqual(evaluate_expression("25.4 mm"), 0.0254)
        self.assertAlmostEqual(evaluate_expression("1 ft"), 0.3048)
        self.assertAlmostEqual(evaluate_expression("2 m"), 2.0)

    def test_areas_square_the_factor(self):
        self.assertAlmostEqual(evaluate_expression("1 in^2"), 0.00064516)

    def test_unitless_number(self):
        self.assertEqual(evaluate_expression("2.5"), 2.5)

    def test_unevaluatable_returns_none_not_a_guess(self):
        """None means unknown. A silently wrong conversion is worse than no
        value, because everything downstream would treat it as real."""
        for expr in ("#bayWidth * 2", "360 furlongs", "", "abc"):
            self.assertIsNone(evaluate_expression(expr), expr)


class TestVariableDiff(unittest.TestCase):
    """Variable-table diffing is the primary change-detection signal."""

    def test_detects_changed_value(self):
        before = [Variable("barArea", "AREA", "1 in^2", 0.00064516)]
        after = [Variable("barArea", "AREA", "0.4 in^2", 0.000258064)]
        self.assertEqual(
            diff_variables(before, after), [("barArea", "1 in^2", "0.4 in^2")]
        )

    def test_reports_added_and_removed_as_changes(self):
        before = [Variable("gone", "LENGTH", "1 in")]
        after = [Variable("added", "LENGTH", "2 in")]
        self.assertEqual(
            diff_variables(before, after),
            [("added", None, "2 in"), ("gone", "1 in", None)],
        )

    def test_unchanged_table_reports_nothing(self):
        same = [Variable("bayWidth", "LENGTH", "360 in", 9.144)]
        self.assertEqual(diff_variables(same, list(same)), [])


class TestAssemblyParsing(unittest.TestCase):
    def setUp(self):
        self.assembly = load(SYNTHETIC / "assembly_populated.json")
        self.instances = parse_instances(self.assembly)
        self.mates = parse_mates(self.assembly)

    def test_parses_instances(self):
        self.assertEqual(len(self.instances), 4)
        self.assertEqual(self.instances[0].id, "MInst1")
        self.assertEqual(self.instances[0].part_id, "JHD")

    def test_preserves_suppressed_flag(self):
        spare = next(i for i in self.instances if i.id == "MInst4")
        self.assertTrue(spare.suppressed)

    def test_parses_mates(self):
        self.assertEqual(len(self.mates), 3)
        fastened = next(m for m in self.mates if m.id == "FMate1")
        self.assertEqual(fastened.mate_type, "FASTENED")
        self.assertEqual(fastened.occurrences, ("MInst1", "MInst3"))

    def test_occurrence_path_resolves_to_mated_instance(self):
        """An occurrence path is [subassembly..., instance]; the mated thing is
        the last entry, not the first."""
        nested = {
            "rootAssembly": {
                "features": [{
                    "featureId": "F1",
                    "featureData": {
                        "name": "Nested",
                        "mateType": "FASTENED",
                        "matedEntities": [
                            {"matedOccurrence": ["SubA", "Inner1"]},
                            {"matedOccurrence": ["SubA", "Inner2"]},
                        ],
                    },
                }]
            }
        }
        self.assertEqual(parse_mates(nested)[0].occurrences, ("Inner1", "Inner2"))

    def test_accepts_features_endpoint_envelope(self):
        """The /features endpoint wraps each feature in {type, message};
        the assembly definition does not. Both must parse."""
        wrapped = {
            "features": [{
                "type": 1,
                "message": {
                    "featureId": "FMate9",
                    "featureData": {
                        "name": "Wrapped",
                        "mateType": "SLIDER",
                        "matedEntities": [
                            {"matedOccurrence": ["A"]},
                            {"matedOccurrence": ["B"]},
                        ],
                    },
                },
            }]
        }
        mate = parse_mates(wrapped)[0]
        self.assertEqual(mate.id, "FMate9")
        self.assertEqual(mate.mate_type, "SLIDER")
        self.assertEqual(mate.occurrences, ("A", "B"))


class TestGroupMates(unittest.TestCase):
    """Group mates use a flat `occurrences` list rather than `matedEntities`,
    and must not be silently dropped."""

    GROUP = {"features": [{
        "featureId": "FGroup1",
        "featureType": "mateGroup",
        "featureData": {
            "name": "Group 1",
            "occurrences": [
                {"occurrence": ["MInst1"]},
                {"occurrence": ["MInst2"]},
                {"occurrence": ["MInst3"]},
            ],
        },
    }]}

    def test_group_occurrences_are_parsed(self):
        mate = parse_mates(self.GROUP)[0]
        self.assertEqual(mate.mate_type, "GROUP")
        self.assertEqual(mate.occurrences, ("MInst1", "MInst2", "MInst3"))

    def test_group_couples_every_member(self):
        """Documents the downside: a group over N parts fully connects them,
        so the traversal can no longer tell what a change actually reaches."""
        pairs = {(e.source_id, e.target_id) for e in mate_edges(parse_mates(self.GROUP))}
        self.assertEqual(len(pairs), 6)   # 3 parts -> 3*2 directed pairs
        self.assertIn(("MInst1", "MInst3"), pairs)


class TestMateEdges(unittest.TestCase):
    def setUp(self):
        self.mates = parse_mates(load(SYNTHETIC / "assembly_populated.json"))

    def test_mates_produce_edges_in_both_directions(self):
        """A mate is undirected in CAD: changing either side affects the other.
        Emitting one direction only would drop dominoes."""
        edges = mate_edges(self.mates)
        pairs = {(e.source_id, e.target_id) for e in edges}
        self.assertIn(("MInst1", "MInst3"), pairs)
        self.assertIn(("MInst3", "MInst1"), pairs)

    def test_suppressed_mates_excluded_by_default(self):
        pairs = {(e.source_id, e.target_id) for e in mate_edges(self.mates)}
        self.assertNotIn(("MInst4", "MInst1"), pairs)

    def test_suppressed_mates_included_on_request(self):
        pairs = {
            (e.source_id, e.target_id)
            for e in mate_edges(self.mates, include_suppressed=True)
        }
        self.assertIn(("MInst4", "MInst1"), pairs)

    def test_edges_are_tagged_as_mates(self):
        self.assertTrue(all(e.kind is EdgeKind.MATE for e in mate_edges(self.mates)))

    def test_no_duplicate_edges(self):
        edges = mate_edges(self.mates)
        pairs = [(e.source_id, e.target_id) for e in edges]
        self.assertEqual(len(pairs), len(set(pairs)))

    def test_self_mate_produces_no_edge(self):
        degenerate = {"features": [{
            "featureId": "F1",
            "featureData": {
                "name": "Self",
                "mateType": "FASTENED",
                "matedEntities": [
                    {"matedOccurrence": ["A"]},
                    {"matedOccurrence": ["A"]},
                ],
            },
        }]}
        self.assertEqual(mate_edges(parse_mates(degenerate)), [])


class TestWhereUsed(unittest.TestCase):
    def setUp(self):
        self.instances = parse_instances(load(SYNTHETIC / "assembly_populated.json"))

    def test_indexes_instances_by_part(self):
        wu = build_where_used(self.instances)
        self.assertEqual(wu.instances_of_part("JHD"), ["MInst1", "MInst2"])
        self.assertEqual(wu.instances_of_part("KLM"), ["MInst3"])

    def test_indexes_instances_by_element(self):
        wu = build_where_used(self.instances)
        self.assertEqual(len(wu.instances_of_element("1a595ebc191cbaa887523bc7")), 4)

    def test_unknown_part_returns_empty_not_error(self):
        self.assertEqual(build_where_used(self.instances).instances_of_part("nope"), [])

    def test_instances_sharing_a_part_are_coupled(self):
        """Two instances of the same part both change when the part changes."""
        pairs = {(e.source_id, e.target_id) for e in where_used_edges(self.instances)}
        self.assertIn(("MInst1", "MInst2"), pairs)
        self.assertIn(("MInst2", "MInst1"), pairs)

    def test_unique_parts_are_not_coupled(self):
        pairs = {(e.source_id, e.target_id) for e in where_used_edges(self.instances)}
        self.assertNotIn(("MInst3", "MInst1"), pairs)

    def test_edges_are_tagged_where_used(self):
        edges = where_used_edges(self.instances)
        self.assertTrue(all(e.kind is EdgeKind.WHERE_USED for e in edges))


if __name__ == "__main__":
    unittest.main(verbosity=2)
