"""Baseline toolbox tests: what the agent gets, and what it must not. [Track A, Sprint 5A]

The load-bearing tests here are the negative ones. plan.md's comparison is
only valid if the baseline has the same PyNite access and the same data as the
system, and lacks exactly three things: the fixed graph traversal, the
code-enforced threshold, and write permission. If a future change quietly adds
a `walk` tool or a safety-factor field, the eval stops measuring the gap it
claims to measure -- so the absence is asserted, not assumed.
Every test passes threshold=1.0 explicitly -- never the developer's .env.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from earl.eval.scenarios import scenario  # noqa: E402
from earl.eval.tools import REPORT_TOOL, ToolBox, ToolError, tool_specs  # noqa: E402

KSI = 6.894757e6


def _box(scenario_id: str = "s01") -> ToolBox:
    return ToolBox(scenario(scenario_id).build())


class TestWhatTheBaselineLacks(unittest.TestCase):
    """The three removals plan.md specifies."""

    def setUp(self):
        self.names = {spec["name"] for spec in tool_specs()}
        self.blob = json.dumps(tool_specs()).lower()

    def test_there_is_no_graph_traversal_tool(self):
        for forbidden in ("walk", "affected", "downstream", "dependenc", "travers"):
            self.assertNotIn(
                forbidden,
                " ".join(self.names).lower(),
                f"a tool named for {forbidden!r} would hand the baseline the walk",
            )

    def test_no_tool_returns_a_safety_factor_or_a_verdict(self):
        """Biject is the system's advantage; handing it over ends the
        experiment. The agent must divide for itself."""
        box = _box()
        payload = json.dumps(
            [
                box.get_change(),
                box.get_structure(),
                box.list_members(),
                box.get_material(),
                box.solve_load_case("lc_benchmark"),
            ]
        ).lower()
        for forbidden in ("safety_factor", "utilization", "capacity", "status", "verdict"):
            self.assertNotIn(forbidden, payload)

    def test_the_graph_the_baseline_sees_carries_no_affected_set(self):
        box = _box()
        self.assertEqual(box.graphs.after.affected_member_ids, [])
        self.assertEqual(box.graphs.after.affected_node_ids, [])

    def test_there_is_no_write_or_merge_tool(self):
        for forbidden in ("write", "merge", "commit", "push", "branch", "apply"):
            self.assertFalse(
                any(forbidden in name for name in self.names),
                f"{forbidden!r} appears in a tool name",
            )


class TestWhatTheBaselineHas(unittest.TestCase):
    """The parity half: everything it needs to get the right answer."""

    def test_the_allowable_stress_is_given(self):
        """Withholding it would make the comparison meaningless rather than
        fair -- the agent could not judge safety at all."""
        materials = _box().get_material()["materials"]
        self.assertEqual(len(materials), 1)
        self.assertAlmostEqual(materials[0]["allowable_stress_ksi"], 25.0, places=3)
        self.assertGreater(materials[0]["yield_strength_pa"], 0.0)

    def test_solve_returns_every_member_not_a_subset(self):
        after = _box().graphs.after
        solved = _box().solve_load_case("lc_benchmark")
        self.assertEqual(
            [m["id"] for m in solved["members"]], [m.id for m in after.members]
        )

    def test_solve_exposes_the_domino_the_system_catches(self):
        """m5 is over the 25 ksi allowable after the s01 change. The baseline
        can see this; whether it looks is its own affair."""
        solved = _box("s01").solve_load_case("lc_benchmark")
        m5 = next(m for m in solved["members"] if m["id"] == "m5")
        self.assertGreater(abs(m5["stress_ksi"]), 25.0)

    def test_before_and_after_states_are_both_reachable(self):
        box = _box("s01")
        after = box.solve_load_case("lc_benchmark", "after")
        before = box.solve_load_case("lc_benchmark", "before")
        stress = lambda r: next(  # noqa: E731
            m["stress_ksi"] for m in r["members"] if m["id"] == "m5"
        )
        self.assertGreater(abs(stress(after)), abs(stress(before)))

    def test_structure_and_members_describe_the_whole_truss(self):
        box = _box()
        structure = box.get_structure()
        self.assertEqual(len(structure["nodes"]), 6)
        self.assertEqual(len(structure["load_cases"]), 1)
        self.assertEqual(len(box.list_members()["members"]), 10)

    def test_members_carry_both_unit_systems(self):
        member = _box().list_members()["members"][0]
        self.assertGreater(member["area_in2"], member["area_m2"])
        self.assertGreater(member["length_m"], 0.0)

    def test_the_change_is_described_exactly_as_the_pipeline_sees_it(self):
        change = _box("s01").get_change()
        self.assertEqual(change["kind"], "feature_edit")
        self.assertEqual(change["target_id"], "m7")
        self.assertEqual(change["value_after"], "6 in^2")

    def test_the_report_tool_takes_a_member_list_and_an_outcome(self):
        spec = next(s for s in tool_specs() if s["name"] == REPORT_TOOL)
        props = spec["input_schema"]["properties"]
        self.assertIn("unsafe_member_ids", props)
        self.assertEqual(props["outcome"]["enum"], ["approved", "escalated"])


class TestDispatch(unittest.TestCase):
    """Bad calls cost the agent a turn; they never crash the scenario."""

    def test_unknown_tool_is_an_error_result_not_an_exception(self):
        payload = json.loads(_box().call_json("walk_graph", {}))
        self.assertIn("unknown tool", payload["error"])

    def test_bad_state_is_an_error_result(self):
        payload = json.loads(_box().call_json("list_members", {"state": "sideways"}))
        self.assertIn("before", payload["error"])

    def test_missing_load_case_id_is_an_error_result(self):
        payload = json.loads(_box().call_json("solve_load_case", {}))
        self.assertIn("load_case_id", payload["error"])

    def test_unknown_load_case_is_an_error_result(self):
        payload = json.loads(
            _box().call_json("solve_load_case", {"load_case_id": "nope"})
        )
        self.assertIn("solve failed", payload["error"])

    def test_call_raises_tool_error_while_call_json_serialises_it(self):
        box = _box()
        with self.assertRaises(ToolError):
            box.call("nope", {})
        self.assertIn("error", json.loads(box.call_json("nope", {})))

    def test_every_call_is_counted_for_the_transcript(self):
        box = _box()
        box.call("get_change")
        box.call_json("solve_load_case", {"load_case_id": "lc_benchmark"})
        box.call_json("nope", {})
        self.assertEqual([c["name"] for c in box.calls],
                         ["get_change", "solve_load_case", "nope"])

    def test_tools_do_not_mutate_the_graphs(self):
        box = _box()
        before = box.graphs.after.to_json()
        box.call("get_structure")
        box.call("solve_load_case", {"load_case_id": "lc_benchmark"})
        self.assertEqual(box.graphs.after.to_json(), before)


if __name__ == "__main__":
    unittest.main()
