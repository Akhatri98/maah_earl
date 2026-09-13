"""Scoreboard tests: dropped dominoes are counted, listed and serialised. [Track B]

The tallies use hand-built records so each rule is pinned on its own;
`ground_truth` and `record_from_decision` run the real solver on the R1 demo
so the interchange with the gate is exercised end to end.
Every test passes threshold=1.0 explicitly -- never the developer's .env.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from earl.analysis.benchmark import demo_before_graph, demo_change_graph  # noqa: E402
from earl.analysis.gate import run_fast_gate  # noqa: E402
from earl.analysis.scoreboard import (  # noqa: E402
    AgentScore,
    ScenarioRecord,
    Scoreboard,
    ground_truth,
    load_records,
    record_from_decision,
    save_records,
    score,
)
from earl.contracts import Outcome  # noqa: E402


def _records() -> list[ScenarioRecord]:
    """Three scenarios, two agents. The system catches everything; the
    baseline misses m5 in s1 (says approved), misses m7 and invents m2 in
    s3, and is right on the safe scenario s2."""
    return [
        ScenarioRecord("s1", "system", ["m5"], ["m5"], "escalated", "escalated"),
        ScenarioRecord("s2", "system", [], [], "approved", "approved"),
        ScenarioRecord("s3", "system", ["m5", "m7"], ["m7", "m5"], "escalated", "escalated"),
        ScenarioRecord("s1", "baseline", ["m5"], [], "escalated", "approved"),
        ScenarioRecord("s2", "baseline", [], [], "approved", "approved"),
        ScenarioRecord("s3", "baseline", ["m5", "m7"], ["m5", "m2"], "escalated", "escalated"),
    ]


class TestScore(unittest.TestCase):
    def setUp(self):
        self.board = score(_records())

    def test_agents_in_first_seen_order(self):
        self.assertEqual(self.board.agents, ["system", "baseline"])
        self.assertIsInstance(self.board.score_for("system"), AgentScore)
        with self.assertRaises(KeyError):
            self.board.score_for("nobody")

    def test_system_catches_everything(self):
        s = self.board.score_for("system")
        self.assertEqual(s.scenarios, 3)
        self.assertEqual(s.unsafe_members_total, 3)
        self.assertEqual(s.caught, 3)
        self.assertEqual(s.dropped_dominoes, 0)
        self.assertEqual(s.false_alarms, 0)
        self.assertEqual(s.dropped_by_scenario, {})
        self.assertEqual(s.recall, 1.0)
        self.assertEqual(s.precision, 1.0)

    def test_baseline_drops_and_false_alarms(self):
        b = self.board.score_for("baseline")
        self.assertEqual(b.scenarios, 3)
        self.assertEqual(b.unsafe_members_total, 3)
        self.assertEqual(b.caught, 1)
        self.assertEqual(b.dropped_dominoes, 2)
        self.assertEqual(b.false_alarms, 1)
        self.assertEqual(b.dropped_by_scenario, {"s1": ["m5"], "s3": ["m7"]})
        self.assertAlmostEqual(b.recall, 1 / 3)
        self.assertAlmostEqual(b.precision, 0.5)

    def test_approved_with_nothing_listed_drops_every_truth_member(self):
        board = score([
            ScenarioRecord("s9", "lazy", ["m5", "m7", "m2"], [], "escalated", "approved"),
        ])
        s = board.score_for("lazy")
        self.assertEqual(s.dropped_dominoes, 3)
        self.assertEqual(s.dropped_by_scenario, {"s9": ["m2", "m5", "m7"]})
        self.assertEqual(s.recall, 0.0)

    def test_error_outcome_counts_as_silence(self):
        board = score([
            ScenarioRecord("s9", "crashy", ["m5"], [], "escalated", "error", note="boom"),
        ])
        self.assertEqual(board.score_for("crashy").dropped_dominoes, 1)

    def test_empty_records(self):
        board = score([])
        self.assertEqual(board.agents, [])
        self.assertIn("no records", board.to_markdown())

    def test_ratios_when_nothing_unsafe(self):
        s = score([ScenarioRecord("s2", "x", [], [], "approved", "approved")]).score_for("x")
        self.assertEqual(s.recall, 1.0)
        self.assertEqual(s.precision, 1.0)


class TestMarkdown(unittest.TestCase):
    def test_contains_agents_and_dropped_listing(self):
        md = score(_records()).to_markdown()
        self.assertIn("| system |", md)
        self.assertIn("| baseline |", md)
        self.assertIn("dropped dominoes", md)
        self.assertIn("baseline / s1: m5", md)
        self.assertIn("baseline / s3: m7", md)
        self.assertNotIn("system / s", md)


class TestSerialisation(unittest.TestCase):
    def test_scoreboard_json_round_trip(self):
        board = score(_records())
        again = Scoreboard.from_json(board.to_json())
        self.assertEqual(again.to_dict(), board.to_dict())
        self.assertIsInstance(again.scores[0], AgentScore)            # R12: list decodes
        self.assertEqual(again.score_for("baseline").dropped_by_scenario,
                         {"s1": ["m5"], "s3": ["m7"]})
        self.assertAlmostEqual(again.score_for("baseline").recall, 1 / 3)

    def test_to_dict_carries_ratios_for_readers(self):
        data = score(_records()).to_dict()
        self.assertIn("recall", data["scores"][0])
        self.assertIn("precision", data["scores"][0])
        json.dumps(data)   # plain JSON, no Infinity, no enums

    def test_records_save_and_load(self):
        records = _records()
        with tempfile.TemporaryDirectory() as tmp:
            path = save_records(records, Path(tmp) / "nested" / "records.json")
            self.assertTrue(path.exists())
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertIsInstance(raw, list)
            self.assertEqual(raw[0]["scenario_id"], "s1")
            back = load_records(path)
        self.assertEqual([r.to_dict() for r in back], [r.to_dict() for r in records])
        self.assertEqual(score(back).to_dict(), score(records).to_dict())

    def test_load_records_accepts_wrapped_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wrapped.json"
            path.write_text(json.dumps({"records": [_records()[0].to_dict()]}), encoding="utf-8")
            back = load_records(path)
        self.assertEqual(len(back), 1)
        self.assertEqual(back[0].agent, "system")

    def test_load_records_rejects_non_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text(json.dumps({"agent": "x"}), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_records(path)


class TestGroundTruthAndDecisions(unittest.TestCase):
    """Real solver, real Biject: the R1 demo has exactly one unsafe member."""

    def test_ground_truth_on_demo_change(self):
        self.assertEqual(ground_truth(demo_change_graph(), threshold=1.0), ["m5"])

    def test_ground_truth_on_optimum(self):
        self.assertEqual(ground_truth(demo_before_graph(), threshold=1.0), [])

    def test_ground_truth_threshold_floor(self):
        with self.assertRaises(ValueError):
            ground_truth(demo_change_graph(), threshold=0.5)

    def test_ground_truth_needs_load_cases(self):
        graph = demo_change_graph()
        graph.load_cases = []
        with self.assertRaises(ValueError):
            ground_truth(graph, threshold=1.0)

    def test_record_from_gate_decision(self):
        graph = demo_change_graph()
        decision = run_fast_gate(graph, threshold=1.0)
        rec = record_from_decision("demo-m7", "system", ground_truth(graph, threshold=1.0), decision)
        self.assertEqual(rec.scenario_id, "demo-m7")
        self.assertEqual(rec.agent, "system")
        self.assertEqual(rec.truth_unsafe_member_ids, ["m5"])
        self.assertEqual(rec.reported_unsafe_member_ids, ["m5"])
        self.assertEqual(rec.truth_outcome, Outcome.ESCALATED.value)
        self.assertEqual(rec.reported_outcome, Outcome.ESCALATED.value)
        self.assertIsNone(rec.note)
        self.assertEqual(score([rec]).score_for("system").dropped_dominoes, 0)

    def test_record_from_error_decision(self):
        graph = demo_change_graph()
        graph.load_cases = []
        decision = run_fast_gate(graph, threshold=1.0)
        rec = record_from_decision("demo-broken", "system", ["m5"], decision)
        self.assertEqual(rec.reported_unsafe_member_ids, [])
        self.assertEqual(rec.reported_outcome, Outcome.ERROR.value)
        self.assertIn("no load cases", rec.note)
        self.assertEqual(score([rec]).score_for("system").dropped_by_scenario, {"demo-broken": ["m5"]})


if __name__ == "__main__":
    unittest.main()
