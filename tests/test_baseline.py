"""Baseline agent tests: the loop, and the ways a run can end. [Track A, Sprint 5A]

Driven by `ScriptedClient`, so every path is exercised without a model, a key
or a network call -- the whole suite stays offline.

The point of most of these is that the baseline is scored on what it SAYS. A
verdict that contradicts the numbers it just fetched is not corrected here,
because there is no Biject on this path; that is the asymmetry the eval
measures. Silence and crashes are scored too, never skipped.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from earl.analysis.scoreboard import score  # noqa: E402
from earl.contracts import Outcome  # noqa: E402
from earl.eval.baseline import BaselineAgent  # noqa: E402
from earl.eval.llm import (  # noqa: E402
    ChatTurn,
    LLMUnavailable,
    MuseToolClient,
    RecordingClient,
    ReplayClient,
    ScriptedClient,
    ToolUse,
)
from earl.eval.scenarios import scenario  # noqa: E402


class _FakeResponse:
    """Just enough of a `requests` response for MuseToolClient."""

    status_code = 200

    def __init__(self, payload):
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def _use(name: str, call_id: str = "t", **arguments) -> ChatTurn:
    """One assistant turn calling one tool, in the chat-completions shape."""
    return ChatTurn(
        raw_content={
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
            ],
        },
        tool_uses=[ToolUse(id=call_id, name=name, input=arguments)],
        stop_reason="tool_calls",
    )


def _report(members, outcome="escalated", rationale="because") -> ChatTurn:
    return _use(
        "report_verdict",
        unsafe_member_ids=members,
        outcome=outcome,
        rationale=rationale,
    )


def _silence(text: str = "Looks fine to me.") -> ChatTurn:
    return ChatTurn(
        raw_content={"role": "assistant", "content": text},
        text=text,
        stop_reason="stop",
    )


def _run(turns, scenario_id="s01", **kwargs):
    s = scenario(scenario_id)
    agent = BaselineAgent(client=ScriptedClient(list(turns)), **kwargs)
    return agent, agent.answer(s, s.build())


class TestVerdictIsTakenAtFaceValue(unittest.TestCase):

    def test_a_reported_verdict_is_the_answer(self):
        _, answer = _run([_report(["m5"])])
        self.assertEqual(answer.unsafe_member_ids, ["m5"])
        self.assertIs(answer.outcome, Outcome.ESCALATED)

    def test_a_wrong_verdict_is_not_corrected(self):
        """The agent names a member that is fine and misses the one that is
        not. Nothing in this path second-guesses it."""
        _, answer = _run([_report(["m7"])])
        self.assertEqual(answer.unsafe_member_ids, ["m7"])

    def test_a_verdict_contradicting_its_own_numbers_still_stands(self):
        """It solves, sees m5 over the allowable, then approves anyway. The
        system could not do this -- Decision.validate() forbids it -- and
        that difference is exactly what the eval is measuring."""
        _, answer = _run(
            [
                _use("solve_load_case", load_case_id="lc_benchmark"),
                _report([], outcome="approved"),
            ]
        )
        self.assertIs(answer.outcome, Outcome.APPROVED)
        self.assertEqual(answer.unsafe_member_ids, [])

    def test_an_unknown_outcome_word_falls_back_to_approved(self):
        _, answer = _run([_report(["m5"], outcome="maybe?")])
        self.assertIs(answer.outcome, Outcome.APPROVED)

    def test_non_string_member_ids_are_dropped_not_crashed_on(self):
        _, answer = _run([_report(["m5", None, {"a": 1}])])
        self.assertEqual(answer.unsafe_member_ids, ["m5"])

    def test_tool_calls_are_counted_for_the_transcript(self):
        agent, answer = _run(
            [
                _use("get_change"),
                _use("list_members"),
                _use("solve_load_case", load_case_id="lc_benchmark"),
                _report(["m5"]),
            ]
        )
        self.assertEqual(answer.detail["tool_calls"], 3)
        self.assertEqual(
            answer.detail["tools_used"],
            ["get_change", "list_members", "solve_load_case"],
        )


class TestTheRunCanEndBadly(unittest.TestCase):
    """Silence and crashes are results, not things to retry away."""

    def test_one_silence_earns_a_nudge(self):
        _, answer = _run([_silence(), _report(["m5"])])
        self.assertIs(answer.outcome, Outcome.ESCALATED)

    def test_a_second_silence_falls_back_to_extracting_the_prose_verdict(self):
        """The model did the engineering and wrote it out, then never called
        the tool. Scoring that as silence would cost it a domino it found."""
        _, answer = _run(
            [
                _silence("m5 is at SF 0.73, below 1.0. Everything else passes."),
                _silence(""),
                _report(["m5"]),          # the extraction pass
            ]
        )
        self.assertEqual(answer.unsafe_member_ids, ["m5"])
        self.assertIs(answer.outcome, Outcome.ESCALATED)

    def test_an_extracted_verdict_is_marked_as_such_everywhere(self):
        """Nothing may hide that the answer was read back from prose."""
        agent, answer = _run(
            [_silence("m5 fails."), _silence(""), _report(["m5"])]
        )
        self.assertTrue(answer.detail["extracted"])
        self.assertIn("extracted from the model's prose", answer.note)
        self.assertTrue(agent.last_run.extracted)
        record = answer.record("s01", "baseline", ["m5"])
        self.assertIn("extracted", record.note)

    def test_a_reported_verdict_is_not_marked_as_extracted(self):
        _, answer = _run([_report(["m5"])])
        self.assertFalse(answer.detail["extracted"])
        self.assertEqual(answer.note, "because")

    def test_extraction_that_also_produces_nothing_is_an_error_answer(self):
        _, answer = _run([_silence("maybe"), _silence(""), _silence("")])
        self.assertIs(answer.outcome, Outcome.ERROR)
        self.assertIn("extraction produced no verdict", answer.note)
        self.assertEqual(answer.unsafe_member_ids, [])

    def test_silence_with_no_prose_at_all_is_an_error_answer(self):
        """Nothing was said, so there is nothing to transcribe -- and the
        extraction pass is skipped rather than asked to invent one."""
        _, answer = _run([_silence(""), _silence("")])
        self.assertIs(answer.outcome, Outcome.ERROR)
        self.assertIn("no prose", answer.note)

    def test_running_out_of_turns_is_an_error_answer(self):
        _, answer = _run([_use("get_change")] * 3, max_turns=3)
        self.assertIs(answer.outcome, Outcome.ERROR)
        self.assertIn("turn cap", answer.note)

    def test_a_transport_failure_is_an_error_answer_not_a_crash(self):
        class Boom:
            model = "boom"

            def chat(self, **kwargs):
                raise RuntimeError("connection reset")

        s = scenario("s01")
        answer = BaselineAgent(client=Boom()).answer(s, s.build())
        self.assertIs(answer.outcome, Outcome.ERROR)
        self.assertIn("connection reset", answer.note)

    def test_an_error_answer_is_scored_as_silence(self):
        """The scoreboard counts every truth-unsafe member as dropped."""
        _, answer = _run([_silence(""), _silence("")])
        record = answer.record("s01", "baseline", ["m5", "m6"])
        board = score([record])
        self.assertEqual(board.score_for("baseline").dropped_dominoes, 2)

    def test_a_missing_client_is_refused_at_construction(self):
        with self.assertRaises(ValueError):
            BaselineAgent()


class TestTheWireProtocol(unittest.TestCase):
    """The loop driven by a real MuseToolClient against a fake endpoint.

    This is the test that would have caught a chat-completions/blocks mix-up:
    it asserts the conversation the live run actually sends, turn by turn.
    """

    def _canned(self, replies):
        """A fake `post` that returns each reply in turn and records the
        request body it was given."""
        sent: list[dict] = []

        def fake_post(url, **kwargs):
            sent.append(kwargs["json"])
            payload = replies[len(sent) - 1]
            return _FakeResponse(payload)

        return fake_post, sent

    def test_the_second_request_carries_the_call_and_its_tool_result(self):
        solve_call = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_solve",
                                "type": "function",
                                "function": {
                                    "name": "solve_load_case",
                                    "arguments": '{"load_case_id": "lc_benchmark"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }
        verdict = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_report",
                                "type": "function",
                                "function": {
                                    "name": "report_verdict",
                                    "arguments": json.dumps(
                                        {
                                            "unsafe_member_ids": ["m5"],
                                            "outcome": "escalated",
                                        }
                                    ),
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }
        fake_post, sent = self._canned([solve_call, verdict])
        client = MuseToolClient(
            api_key="k", base_url="https://llm.example/v1", model="muse-test",
            post=fake_post,
        )
        s = scenario("s01")
        answer = BaselineAgent(client=client).answer(s, s.build())

        self.assertEqual(answer.unsafe_member_ids, ["m5"])
        self.assertIs(answer.outcome, Outcome.ESCALATED)
        self.assertEqual(len(sent), 2)

        # Turn 1: system + the opening user message, and the tool list.
        first = sent[0]["messages"]
        self.assertEqual(first[0]["role"], "system")
        self.assertEqual(first[1]["role"], "user")
        self.assertTrue(sent[0]["tools"])

        # Turn 2: the assistant turn echoed back with its tool_calls, then one
        # `role: "tool"` message answering it by id.
        second = sent[1]["messages"]
        assistant = second[-2]
        result = second[-1]
        self.assertEqual(assistant["role"], "assistant")
        self.assertEqual(assistant["tool_calls"][0]["id"], "call_solve")
        self.assertEqual(result["role"], "tool")
        self.assertEqual(result["tool_call_id"], "call_solve")

        # ...carrying the real solver output, m5 over its 25 ksi allowable.
        solved = json.loads(result["content"])
        m5 = next(m for m in solved["members"] if m["id"] == "m5")
        self.assertGreater(abs(m5["stress_ksi"]), 25.0)

    def test_every_call_in_a_parallel_turn_gets_its_own_result(self):
        """Chat completions rejects a follow-up that leaves a tool_call
        unanswered, so a dropped result would break the run, not just the
        model's habits."""
        parallel = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "a",
                                "function": {"name": "get_change", "arguments": "{}"},
                            },
                            {
                                "id": "b",
                                "function": {"name": "get_material", "arguments": "{}"},
                            },
                            {
                                "id": "c",
                                "function": {"name": "nope", "arguments": "{}"},
                            },
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }
        done = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "d",
                                "function": {
                                    "name": "report_verdict",
                                    "arguments": '{"unsafe_member_ids": [], "outcome": "approved"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }
        fake_post, sent = self._canned([parallel, done])
        client = MuseToolClient(api_key="k", post=fake_post)
        s = scenario("s01")
        BaselineAgent(client=client).answer(s, s.build())

        results = [m for m in sent[1]["messages"] if m.get("role") == "tool"]
        self.assertEqual([m["tool_call_id"] for m in results], ["a", "b", "c"])
        # Even the unknown tool gets a result -- an error one.
        self.assertIn("error", json.loads(results[2]["content"]))


class TestTranscripts(unittest.TestCase):
    """A live run must leave behind everything a replay needs."""

    def test_the_transcript_is_written_and_replayable(self):
        turns = [
            _use("solve_load_case", load_case_id="lc_benchmark"),
            _report(["m5"]),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            agent, answer = _run(turns, transcript_dir=tmp)
            path = Path(tmp) / "s01-thin-m7-demo.json"
            self.assertTrue(path.exists())

            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["scenario_id"], "s01-thin-m7-demo")
            self.assertEqual(len(data["turns"]), 2)
            self.assertEqual(data["verdict"]["unsafe_member_ids"], ["m5"])
            self.assertEqual(len(data["tool_calls"]), 1)

            # Replaying the recording reproduces the same answer.
            s = scenario("s01")
            replayed = BaselineAgent(
                client_for=lambda sid: ReplayClient.from_file(path)
            ).answer(s, s.build())
            self.assertEqual(replayed.unsafe_member_ids, answer.unsafe_member_ids)
            self.assertIs(replayed.outcome, answer.outcome)

    def test_replaying_past_the_end_raises_rather_than_inventing_a_turn(self):
        client = ReplayClient(turns=[])
        with self.assertRaises(LLMUnavailable):
            client.chat(system="", messages=[], tools=[])

    def test_the_recorder_captures_every_turn_in_order(self):
        recorder = RecordingClient(ScriptedClient([_use("get_change"), _report([])]))
        recorder.chat(system="", messages=[], tools=[])
        recorder.chat(system="", messages=[], tools=[])
        self.assertEqual(len(recorder.turns), 2)
        self.assertEqual(recorder.turns[0]["stop_reason"], "tool_calls")

    def test_client_for_is_consulted_per_scenario(self):
        seen: list[str] = []

        def client_for(scenario_id: str):
            seen.append(scenario_id)
            return ScriptedClient([_report(["m5"])])

        agent = BaselineAgent(client_for=client_for)
        for sid in ("s01", "s02"):
            s = scenario(sid)
            agent.answer(s, s.build())
        self.assertEqual(seen, ["s01-thin-m7-demo", "s02-thin-m3"])


if __name__ == "__main__":
    unittest.main()
