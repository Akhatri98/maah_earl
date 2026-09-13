"""Consistency tests: same scenario twice, same answer? [Track A, Sprint 5A]

The first live run settled the "missing something" half of plan.md's baseline
claim -- on the 10-bar truss the baseline missed nothing. This module measures
the other half, "inconsistent across runs", so the tallies it produces need to
be right before any of it reaches a slide.

Agents here are stubs with scripted per-run answers, so the whole file is
offline; the system's real determinism is pinned in `tests/test_walker.py` and
exercised end to end by `TestTheSystemIsStable` below.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from earl.analysis.scoreboard import AGENT_BASELINE, AGENT_SYSTEM  # noqa: E402
from earl.contracts import Outcome  # noqa: E402
from earl.eval.agents import AgentAnswer, SystemAgent  # noqa: E402
from earl.eval.consistency import RepeatResult, run_consistency  # noqa: E402
from earl.eval.scenarios import scenario  # noqa: E402

THRESHOLD = 1.0


class _Wobbly:
    """An agent that answers differently on each successive call."""

    def __init__(self, name, answers):
        self.name = name
        self.answers = list(answers)
        self.calls = 0

    def answer(self, scenario_obj, graphs):
        members = self.answers[self.calls % len(self.answers)]
        self.calls += 1
        return AgentAnswer(
            unsafe_member_ids=list(members),
            outcome=Outcome.ESCALATED if members else Outcome.APPROVED,
        )


class TestRepeatResult(unittest.TestCase):
    """The per-scenario tallies, on hand-built data."""

    def test_one_distinct_answer_is_stable(self):
        result = RepeatResult("s1", "a", truth=["m5"], runs=[["m5"], ["m5"], ["m5"]])
        self.assertTrue(result.stable)
        self.assertEqual(result.distinct, [["m5"]])
        self.assertEqual(result.correct_runs, 3)
        self.assertEqual(result.dropped_total, 0)

    def test_a_changed_answer_is_unstable(self):
        result = RepeatResult("s1", "a", truth=["m5"], runs=[["m5"], [], ["m5"]])
        self.assertFalse(result.stable)
        self.assertEqual(result.distinct, [["m5"], []])
        self.assertEqual(result.correct_runs, 2)

    def test_dropped_dominoes_are_summed_over_repeats_not_scenarios(self):
        """An agent that reports m5 on Monday and nothing on Tuesday dropped a
        domino on Tuesday; instability is the same claim, measured per run."""
        result = RepeatResult(
            "s1", "a", truth=["m5", "m6"], runs=[["m5", "m6"], [], ["m5"]]
        )
        self.assertEqual(result.dropped_total, 3)      # 0 + 2 + 1

    def test_stable_and_wrong_is_still_reported_as_stable(self):
        """Reliably wrong is a different failure from flaky, and the report
        must not blur them."""
        result = RepeatResult("s1", "a", truth=["m5"], runs=[["m7"], ["m7"]])
        self.assertTrue(result.stable)
        self.assertEqual(result.correct_runs, 0)
        self.assertEqual(result.dropped_total, 2)


class TestTheSystemIsStable(unittest.TestCase):
    """The real pipeline, repeated, on real scenarios."""

    def test_the_system_gives_the_same_answer_every_time(self):
        report = run_consistency(
            [SystemAgent(threshold=THRESHOLD)],
            [scenario("s01"), scenario("s09"), scenario("s04")],
            repeats=3,
            threshold=THRESHOLD,
        )
        self.assertEqual(report.repeats, 3)
        self.assertEqual(len(report.results), 3)
        for result in report.results:
            with self.subTest(result.scenario_id):
                self.assertTrue(result.stable)
                self.assertEqual(result.correct_runs, 3)
                self.assertEqual(result.dropped_total, 0)


class TestCatchingAWobble(unittest.TestCase):

    def setUp(self):
        # Right, then silent, then right again -- the shape the first live run
        # hinted at when the model analysed correctly but never reported.
        self.agent = _Wobbly(AGENT_BASELINE, [["m5"], [], ["m5"]])
        self.report = run_consistency(
            [self.agent], [scenario("s01")], repeats=3, threshold=THRESHOLD
        )

    def test_the_wobble_is_detected(self):
        result = self.report.results[0]
        self.assertFalse(result.stable)
        self.assertEqual(result.correct_runs, 2)
        self.assertEqual(result.dropped_total, 1)

    def test_the_markdown_names_the_scenario_and_both_answers(self):
        md = self.report.to_markdown()
        self.assertIn("Answers that changed between runs:", md)
        self.assertIn("s01-thin-m7-demo", md)
        self.assertIn("(none)", md)

    def test_a_clean_report_says_so_rather_than_leaving_it_blank(self):
        report = run_consistency(
            [_Wobbly(AGENT_SYSTEM, [["m5"]])], [scenario("s01")],
            repeats=2, threshold=THRESHOLD,
        )
        self.assertIn("same answer on every repeat", report.to_markdown())

    def test_the_report_is_written_as_json_and_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.report.write(tmp)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["repeats"], 3)
            self.assertEqual(data["results"][0]["distinct_answers"], [["m5"], []])
            self.assertFalse(data["results"][0]["stable"])
            self.assertTrue(path.with_name("consistency.md").exists())


class TestGuards(unittest.TestCase):

    def test_repeats_must_be_at_least_one(self):
        with self.assertRaises(ValueError):
            run_consistency([SystemAgent()], [scenario("s01")], repeats=0)

    def test_each_repeat_recomputes_truth_and_rebuilds_the_graph(self):
        """Consistency mode reuses `run_scenario`, so it inherits the same
        isolation guarantee: a second repeat must not see the first's walk."""
        seen: list[list[str]] = []

        class Watcher:
            name = AGENT_BASELINE

            def answer(self, scenario_obj, graphs):
                seen.append(list(graphs.after.affected_member_ids))
                return AgentAnswer()

        run_consistency(
            [SystemAgent(threshold=THRESHOLD), Watcher()],
            [scenario("s01")], repeats=3, threshold=THRESHOLD,
        )
        self.assertEqual(seen, [[], [], []])


if __name__ == "__main__":
    unittest.main()
