"""Harness tests: the replay is honest about what it measured. [Track A, Sprint 5A]

Three properties carry the whole eval, and each has a test that fails loudly
if it stops holding:

  * ground truth is computed, and a scenario whose truth cannot be computed is
    EXCLUDED rather than scored as "nothing unsafe";
  * each agent gets its own build of the scenario, so the system's walk cannot
    leak the traversal to the baseline;
  * an agent that crashes is scored as silence, not skipped.

The system agent runs the real pipeline; the baseline stand-ins are scripted,
so the whole file is offline.
Every test passes threshold=1.0 explicitly -- never the developer's .env.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from earl.analysis.scoreboard import AGENT_BASELINE, AGENT_SYSTEM, load_records  # noqa: E402
from earl.contracts import Outcome  # noqa: E402
from earl.eval.agents import AgentAnswer, SystemAgent  # noqa: E402
from earl.eval.harness import run_eval, run_scenario, write_run  # noqa: E402
from earl.eval.llm import LLMUnavailable  # noqa: E402
from earl.eval.scenarios import SCENARIOS, scenario  # noqa: E402

THRESHOLD = 1.0


class _Stub:
    """An agent that answers from a fixed table, for harness-level tests."""

    def __init__(self, name, table=None, raises=False, record_affected=None):
        self.name = name
        self.table = table or {}
        self.raises = raises
        self.record_affected = record_affected

    def answer(self, scenario_obj, graphs):
        if self.record_affected is not None:
            self.record_affected.append(list(graphs.after.affected_member_ids))
        if self.raises:
            raise RuntimeError("stub exploded")
        members = self.table.get(scenario_obj.id, [])
        return AgentAnswer(
            unsafe_member_ids=list(members),
            outcome=Outcome.ESCALATED if members else Outcome.APPROVED,
        )


class TestSystemOverTheWholeSet(unittest.TestCase):
    """The headline number, computed rather than asserted."""

    @classmethod
    def setUpClass(cls):
        cls.eval_run = run_eval([SystemAgent(threshold=THRESHOLD)], threshold=THRESHOLD)

    def test_every_scenario_was_scorable(self):
        self.assertEqual(self.eval_run.excluded, [])
        self.assertEqual(len(self.eval_run.outcomes), len(SCENARIOS))

    def test_the_system_drops_no_dominoes(self):
        board = self.eval_run.board
        system = board.score_for(AGENT_SYSTEM)
        self.assertEqual(system.scenarios, 20)
        self.assertEqual(system.dropped_dominoes, 0)
        self.assertEqual(system.recall, 1.0)

    def test_the_system_raises_no_false_alarms(self):
        self.assertEqual(self.eval_run.board.score_for(AGENT_SYSTEM).false_alarms, 0)

    def test_the_system_agrees_with_truth_scenario_by_scenario(self):
        for outcome in self.eval_run.outcomes:
            with self.subTest(outcome.scenario.id):
                answer = outcome.answers[AGENT_SYSTEM]
                self.assertEqual(
                    sorted(answer.unsafe_member_ids),
                    sorted(outcome.truth_unsafe_member_ids),
                )
                self.assertIs(answer.outcome, outcome.truth_outcome)

    def test_the_fixed_walk_ran_for_every_scenario(self):
        """The system's advantage is the traversal; confirm it actually
        happened rather than being assumed."""
        for outcome in self.eval_run.outcomes:
            with self.subTest(outcome.scenario.id):
                walked = outcome.answers[AGENT_SYSTEM].detail["walked_members"]
                self.assertGreaterEqual(len(walked), 9)


class TestIsolationBetweenAgents(unittest.TestCase):
    """The single most load-bearing line in the harness."""

    def test_the_baseline_never_sees_the_systems_walk(self):
        seen: list[list[str]] = []
        agents = [
            SystemAgent(threshold=THRESHOLD),
            _Stub(AGENT_BASELINE, record_affected=seen),
        ]
        outcome = run_scenario(scenario("s01"), agents, threshold=THRESHOLD)

        # The system walked and populated its own graph...
        self.assertGreaterEqual(
            len(outcome.answers[AGENT_SYSTEM].detail["walked_members"]), 9
        )
        # ...and the baseline's build was untouched by it.
        self.assertEqual(seen, [[]])

    def test_ground_truth_is_computed_before_any_agent_runs(self):
        outcome = run_scenario(
            scenario("s01"), [_Stub(AGENT_BASELINE, {"s01-thin-m7-demo": ["m1"]})],
            threshold=THRESHOLD,
        )
        self.assertEqual(outcome.truth_unsafe_member_ids, ["m5"])
        self.assertEqual(outcome.answers[AGENT_BASELINE].unsafe_member_ids, ["m1"])


class TestFailuresAreScoredNotSkipped(unittest.TestCase):

    def test_an_agent_that_raises_becomes_an_error_answer(self):
        outcome = run_scenario(
            scenario("s01"), [_Stub(AGENT_BASELINE, raises=True)], threshold=THRESHOLD
        )
        answer = outcome.answers[AGENT_BASELINE]
        self.assertIs(answer.outcome, Outcome.ERROR)
        self.assertIn("stub exploded", answer.note)

    def test_a_crash_costs_the_agent_every_domino_in_that_scenario(self):
        run = run_eval(
            [_Stub(AGENT_BASELINE, raises=True)], [scenario("s09")], threshold=THRESHOLD
        )
        score = run.board.score_for(AGENT_BASELINE)
        self.assertEqual(score.dropped_dominoes, 4)      # m2, m5, m6, m10
        self.assertEqual(score.recall, 0.0)

    def test_a_missing_model_stops_the_run_rather_than_scoring_a_drop(self):
        """A missing key or recording means the agent never answered. Scoring
        that as silence would invent the headline number out of a
        misconfiguration, so it propagates instead."""

        class NoModel:
            name = AGENT_BASELINE

            def answer(self, scenario_obj, graphs):
                raise LLMUnavailable("no recording for this scenario")

        with self.assertRaises(LLMUnavailable):
            run_eval([NoModel()], [scenario("s01")], threshold=THRESHOLD)

    def test_a_scenario_without_truth_is_excluded_from_every_agents_score(self):
        # `replace` rather than mutation: SCENARIOS is shared across the whole
        # suite, and a frozen dataclass poked in place would corrupt it.
        broken = replace(
            scenario("s01"), id="broken", loads_kips=(float("nan"), float("nan"))
        )
        run = run_eval([_Stub(AGENT_BASELINE)], [broken], threshold=THRESHOLD)
        self.assertEqual(run.records, [])
        self.assertEqual(len(run.excluded), 1)
        self.assertIn(broken.id, run.excluded[0])
        self.assertIn("EXCLUDED", run.summary())


class TestComparisonAndOutput(unittest.TestCase):

    def setUp(self):
        # A baseline that only ever names the edited member -- the "check what
        # you touched" policy the set is designed to punish.
        table = {
            s.id: ([s.target_id] if s.target_id.startswith("m") else [])
            for s in SCENARIOS
        }
        self.eval_run = run_eval(
            [SystemAgent(threshold=THRESHOLD), _Stub(AGENT_BASELINE, table)],
            threshold=THRESHOLD,
        )

    def test_the_gap_is_measured_not_asserted(self):
        board = self.eval_run.board
        self.assertEqual(board.score_for(AGENT_SYSTEM).dropped_dominoes, 0)
        self.assertGreater(board.score_for(AGENT_BASELINE).dropped_dominoes, 0)

    def test_the_naive_policy_also_raises_false_alarms(self):
        self.assertGreater(self.eval_run.board.score_for(AGENT_BASELINE).false_alarms, 0)

    def test_dropped_dominoes_are_traceable_to_the_scenario(self):
        dropped = self.eval_run.board.score_for(AGENT_BASELINE).dropped_by_scenario
        self.assertIn("s01-thin-m7-demo", dropped)
        self.assertEqual(dropped["s01-thin-m7-demo"], ["m5"])

    def test_the_summary_names_both_agents_and_the_split(self):
        summary = self.eval_run.summary()
        self.assertIn("11 unsafe, 9 safe", summary)
        self.assertIn(f"| {AGENT_SYSTEM} |", summary)
        self.assertIn(f"| {AGENT_BASELINE} |", summary)

    def test_the_run_is_written_as_the_documented_interchange(self):
        with tempfile.TemporaryDirectory() as tmp:
            written = write_run(self.eval_run, tmp)
            self.assertEqual(set(written), {"records", "scoreboard", "summary"})

            records = load_records(written["records"])
            self.assertEqual(len(records), 40)      # 20 scenarios x 2 agents
            self.assertEqual({r.agent for r in records},
                             {AGENT_SYSTEM, AGENT_BASELINE})

            board = json.loads(written["scoreboard"].read_text(encoding="utf-8"))
            self.assertEqual(len(board["scores"]), 2)
            self.assertEqual(board["scores"][0]["recall"], 1.0)


if __name__ == "__main__":
    unittest.main()
