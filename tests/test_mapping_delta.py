"""Regression coverage for contract 0.2 ingestion and enforcement boundaries."""

import json
import unittest
from copy import deepcopy
from pathlib import Path

from earl.contracts import (
    ChangeEvent, ChangeKind, Decision, DependencyGraph, EscalationReason,
    MemberStatus, Outcome, TargetKind,
)
from earl.ingestion.truss_map import map_assembly
from earl.units import AREA
from scripts.contract_selfcheck import build_decision, build_graph

FIXTURES = Path(__file__).parent / "fixtures" / "synthetic"


class MappingTests(unittest.TestCase):
    def inputs(self):
        return tuple(json.loads((FIXTURES / name).read_text()) for name in
                     ("truss_assembly.json", "truss_variables.json"))

    def test_all_edges_use_member_ids(self):
        graph = map_assembly(*self.inputs())
        graph.validate()
        self.assertEqual(len(graph.members), 10)
        self.assertTrue(all(e.source_id.startswith("m") and e.target_id.startswith("m")
                            for e in graph.edges))
        self.assertNotIn("MInst", graph.to_json())

    def test_variables_define_geometry_section_and_target(self):
        assembly, rows = self.inputs()
        rows[0]["variables"][0]["value"] = 5.0
        graph = map_assembly(assembly, rows)
        self.assertEqual(graph.node("n1").x, 10.0)
        self.assertEqual(graph.design_target, 1.5)
        self.assertGreater(graph.sections[0].iy, 0)

    def test_unmapped_instance_is_a_warning(self):
        assembly, rows = self.inputs()
        assembly["rootAssembly"]["instances"].append({"id": "unknown", "name": "Mystery"})
        with self.assertLogs("earl.ingestion.truss_map", level="WARNING"):
            graph = map_assembly(assembly, rows)
        self.assertEqual(len(graph.mapping_warnings), 1)

    def test_graph_rejects_instance_even_when_it_is_change_target(self):
        graph = build_graph()
        graph.change.target_id = "MInst1"
        graph.edges[0].source_id = "MInst1"
        with self.assertRaises(ValueError):
            graph.validate()


class DeltaTests(unittest.TestCase):
    def test_area_delta_changes_only_target_and_preserves_input(self):
        before = build_graph()
        again = DependencyGraph.from_json(before.to_json())
        after = before.change.apply(before)
        self.assertEqual(before.to_dict(), again.to_dict())
        self.assertAlmostEqual(after.section(after.member("m5").section_id).area, AREA * 0.4)
        self.assertAlmostEqual(after.section(after.member("m4").section_id).area, AREA)
        self.assertAlmostEqual(after.sections[-1].iy / before.sections[0].iy, 0.16)

    def test_stale_before_value_is_rejected(self):
        graph = build_graph()
        graph.change.numeric_before *= 2
        with self.assertRaisesRegex(ValueError, "stale"):
            graph.change.apply(graph)

    def test_removal_and_load_move_are_typed(self):
        graph = build_graph()
        change = ChangeEvent("remove", ChangeKind.MEMBER_REMOVED, "Remove m5", "m5",
                             target_kind=TargetKind.MEMBER, field_name="present",
                             numeric_before=1, numeric_after=0)
        after = change.apply(graph)
        self.assertEqual(len(after.members), 9)
        self.assertNotIn("m5", after.affected_member_ids)
        move = ChangeEvent("move", ChangeKind.LOAD_MOVED, "Move p1", "p1",
                           target_kind=TargetKind.LOAD, field_name="node_id",
                           node_before="n2", node_after="n1")
        self.assertEqual(move.apply(graph).load_cases[0].point_loads[0].node_id, "n1")


class EnforcementBoundaryTests(unittest.TestCase):
    def clear(self):
        d = build_decision(build_graph())
        d.escalation_reason = None
        d.violating_member_ids = []
        for r in d.member_results:
            r.safety_factor, r.status = 3.0, MemberStatus.PASS
        return d

    def test_hard_floor_cannot_be_lowered_by_payload(self):
        d = self.clear()
        d.outcome = Outcome.APPROVED
        d.hard_floor = 0.1
        d.design_target = 0.2
        d.safety_factor_threshold = 0.2
        d.member_results[0].safety_factor = 0.5
        with self.assertRaises(ValueError):
            d.validate()

    def test_floor_and_design_target_are_separate(self):
        d = self.clear()
        d.member_results[0].safety_factor = 1.2
        d.member_results[0].status = MemberStatus.FAIL
        d.violating_member_ids = [d.member_results[0].member_id]
        d.escalation_reason = EscalationReason.BELOW_DESIGN_TARGET
        d.validate()
        d.outcome = Outcome.APPROVED
        with self.assertRaises(ValueError):
            d.validate()

    def test_disagreement_and_not_evaluated_can_escalate(self):
        d = self.clear()
        d.cross_check.agrees = False
        d.escalation_reason = EscalationReason.CROSS_CHECK_DISAGREEMENT
        d.validate()
        d = self.clear()
        d.member_results[0].status = MemberStatus.NOT_EVALUATED
        d.member_results[0].safety_factor = None
        d.escalation_reason = EscalationReason.NOT_EVALUATED
        d.validate()

    def test_serialization_validates_on_produce_and_consume(self):
        d = build_decision(build_graph())
        payload = d.to_dict()
        payload["outcome"] = "approved"
        with self.assertRaises(ValueError):
            Decision.from_dict(payload)
        d.outcome = Outcome.APPROVED
        with self.assertRaises(ValueError):
            d.to_dict()

    def test_nan_and_failed_sanity_cannot_become_safety_verdicts(self):
        d = self.clear()
        d.member_results[0].safety_factor = float("nan")
        with self.assertRaises(ValueError):
            d.validate()
        d = self.clear()
        d.outcome = Outcome.APPROVED
        d.sanity_checks.linearity_ok = False
        with self.assertRaises(ValueError):
            d.validate()
