"""Sprint 1A tests for the Onshape ingestion layer.

Runs entirely offline against recorded and synthetic fixtures -- no API calls,
so the suite is free to run and does not eat the annual request budget.

Two fixture sets, on purpose:
  * `fixtures/onshape/`   -- real responses recorded from the live document,
                             which is currently EMPTY. These pin the
                             empty-document behaviour so a parser can never
                             start inventing structure that isn't there.
  * `fixtures/synthetic/` -- hand-built to Onshape's documented shape, so the
                             parsers are proven against populated data before
                             the truss is modelled.
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


class TestRecordedEmptyDocument(unittest.TestCase):
    """The live document has no geometry yet. Parsers must return empty
    results cleanly rather than raising or fabricating structure."""

    def test_variables_of_empty_document(self):
        data = load(RECORDED / "variables_d_w_e_variables.json")
        self.assertEqual(parse_variables(data), [])

    def test_assembly_of_empty_document(self):
        data = load(RECORDED / "assemblies_d_w_e.json")
        self.assertEqual(parse_instances(data), [])
        self.assertEqual(parse_mates(data), [])

    def test_features_endpoint_of_empty_document(self):
        data = load(RECORDED / "assemblies_d_w_e_features.json")
        self.assertEqual(parse_mates(data), [])

    def test_empty_document_yields_no_edges(self):
        data = load(RECORDED / "assemblies_d_w_e.json")
        self.assertEqual(mate_edges(parse_mates(data)), [])
        self.assertEqual(where_used_edges(parse_instances(data)), [])


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
        self.assertEqual(area.type, "AREA")

    def test_tolerates_missing_fields(self):
        parsed = parse_variables([{"variables": [{"name": "bare"}]}])
        self.assertEqual(parsed[0].name, "bare")
        self.assertIsNone(parsed[0].value)

    def test_tolerates_null_payload(self):
        self.assertEqual(parse_variables(None), [])


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
