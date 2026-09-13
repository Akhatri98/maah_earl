"""Muse tool-calling transport tests. [Track A, Sprint 5A]

`post` is injected, so the exact URL, headers and body are asserted without a
network call -- the same approach `tests/test_orchestrator.py` takes with the
planner's `MetaModelClient`, against the same endpoint.

The chat-completions shape has three things that are easy to get subtly wrong,
and each has a test here: tool arguments arrive as a JSON *string*, each result
is its own `role: "tool"` message keyed by `tool_call_id`, and a malformed
argument blob must cost the model a turn rather than the run.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from earl.analysis.orchestrator import (  # noqa: E402
    DEFAULT_LLM_BASE_URL,
    DEFAULT_LLM_MODEL,
    LLMError,
)
from earl.eval.llm import (  # noqa: E402
    ChatTurn,
    LLMUnavailable,
    MuseToolClient,
    RecordingClient,
    ReplayClient,
    ToolResult,
    ToolUse,
    to_openai_tools,
)
from earl.eval.tools import tool_specs  # noqa: E402


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or json.dumps(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def _reply(message, finish_reason="tool_calls", usage=None):
    return FakeResponse(
        200,
        {
            "choices": [{"message": message, "finish_reason": finish_reason}],
            "usage": usage or {"total_tokens": 12},
        },
    )


def _client(post, **kwargs):
    return MuseToolClient(
        api_key="test-key", base_url="https://llm.example/v1",
        model="muse-test", post=post, **kwargs,
    )


class TestRequestShape(unittest.TestCase):

    def test_it_talks_to_the_same_endpoint_as_the_planner(self):
        client = MuseToolClient(api_key="k")
        self.assertEqual(client.base_url, DEFAULT_LLM_BASE_URL)
        self.assertEqual(client.model, DEFAULT_LLM_MODEL)
        self.assertEqual(client.url, f"{DEFAULT_LLM_BASE_URL}/chat/completions")

    def test_it_builds_an_openai_compatible_tool_call_request(self):
        seen: dict = {}

        def fake_post(url, **kwargs):
            seen["url"] = url
            seen.update(kwargs)
            return _reply({"role": "assistant", "content": "hi"}, "stop")

        _client(fake_post).chat(
            system="SYS",
            messages=[{"role": "user", "content": "USER"}],
            tools=tool_specs(),
        )

        self.assertEqual(seen["url"], "https://llm.example/v1/chat/completions")
        self.assertEqual(seen["headers"]["Authorization"], "Bearer test-key")
        body = seen["json"]
        self.assertEqual(body["model"], "muse-test")
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(body["tool_choice"], "auto")
        self.assertEqual(body["messages"][0], {"role": "system", "content": "SYS"})
        self.assertEqual(body["messages"][1], {"role": "user", "content": "USER"})
        self.assertTrue(all(t["type"] == "function" for t in body["tools"]))

    def test_the_api_key_never_appears_in_a_repr(self):
        self.assertNotIn("test-key", repr(_client(lambda *a, **k: None)))

    def test_tool_specs_convert_to_the_function_shape(self):
        converted = to_openai_tools(tool_specs())
        self.assertEqual(
            [t["function"]["name"] for t in converted],
            [s["name"] for s in tool_specs()],
        )
        first = converted[0]["function"]
        self.assertIn("description", first)
        self.assertEqual(first["parameters"], tool_specs()[0]["input_schema"])


class TestResponseParsing(unittest.TestCase):

    def test_tool_arguments_arrive_as_a_json_string_and_are_parsed(self):
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "solve_load_case",
                        "arguments": '{"load_case_id": "lc_benchmark", "state": "after"}',
                    },
                }
            ],
        }
        turn = _client(lambda *a, **k: _reply(message)).chat(
            system="", messages=[], tools=tool_specs()
        )
        self.assertEqual(len(turn.tool_uses), 1)
        call = turn.tool_uses[0]
        self.assertEqual(call.id, "call_1")
        self.assertEqual(call.name, "solve_load_case")
        self.assertEqual(call.input, {"load_case_id": "lc_benchmark", "state": "after"})
        self.assertEqual(turn.stop_reason, "tool_calls")
        self.assertEqual(turn.usage, {"total_tokens": 12})

    def test_malformed_arguments_become_an_empty_dict_not_a_crash(self):
        """The toolbox then rejects the call with an error result, which costs
        the model a turn rather than the run."""
        message = {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "c",
                    "function": {"name": "solve_load_case", "arguments": "{not json"},
                }
            ],
        }
        turn = _client(lambda *a, **k: _reply(message)).chat(
            system="", messages=[], tools=[]
        )
        self.assertEqual(turn.tool_uses[0].input, {})

    def test_plain_text_replies_carry_no_tool_calls(self):
        turn = _client(
            lambda *a, **k: _reply({"role": "assistant", "content": "done"}, "stop")
        ).chat(system="", messages=[], tools=[])
        self.assertEqual(turn.tool_uses, [])
        self.assertEqual(turn.text, "done")
        self.assertEqual(turn.stop_reason, "stop")

    def test_a_non_200_is_an_llm_error_carrying_the_body(self):
        def fake_post(*a, **k):
            return FakeResponse(400, None, text="tools are not supported")

        with self.assertRaises(LLMError) as caught:
            _client(fake_post).chat(system="", messages=[], tools=tool_specs())
        self.assertIn("400", str(caught.exception))
        self.assertIn("tools are not supported", str(caught.exception))

    def test_a_transport_exception_is_an_llm_error(self):
        def fake_post(*a, **k):
            raise ConnectionError("no route to host")

        with self.assertRaises(LLMError):
            _client(fake_post).chat(system="", messages=[], tools=[])

    def test_a_reply_in_the_wrong_shape_is_an_llm_error(self):
        def fake_post(*a, **k):
            return FakeResponse(200, {"not_choices": []})

        with self.assertRaises(LLMError):
            _client(fake_post).chat(system="", messages=[], tools=[])

    def test_an_oversized_reply_is_refused_before_parsing(self):
        def fake_post(*a, **k):
            return FakeResponse(200, {"choices": []}, text="x" * 2_000_000)

        with self.assertRaises(LLMError) as caught:
            _client(fake_post).chat(system="", messages=[], tools=[])
        self.assertIn("too long", str(caught.exception))


class TestMessageShaping(unittest.TestCase):
    """The half the agent loop delegates, because it is easy to get wrong."""

    def setUp(self):
        self.client = _client(lambda *a, **k: None)

    def test_the_assistant_turn_is_echoed_back_verbatim(self):
        message = {"role": "assistant", "content": None, "tool_calls": [{"id": "c"}]}
        turn = ChatTurn(raw_content=message)
        self.assertIs(self.client.assistant_message(turn), message)

    def test_an_assistant_turn_is_rebuilt_when_there_is_no_raw_payload(self):
        """What a replayed recording needs: the normalised turn is enough."""
        turn = ChatTurn(
            tool_uses=[ToolUse("c1", "get_change", {"state": "after"})], text="looking"
        )
        message = self.client.assistant_message(turn)
        self.assertEqual(message["role"], "assistant")
        self.assertEqual(message["content"], "looking")
        call = message["tool_calls"][0]
        self.assertEqual(call["id"], "c1")
        self.assertEqual(call["function"]["name"], "get_change")
        self.assertEqual(json.loads(call["function"]["arguments"]), {"state": "after"})

    def test_every_result_is_its_own_tool_message_keyed_by_call_id(self):
        results = [
            ToolResult("c1", "get_change", '{"kind": "feature_edit"}'),
            ToolResult("c2", "get_material", '{"materials": []}'),
        ]
        messages = self.client.tool_result_messages(results)
        self.assertEqual(len(messages), 2)
        self.assertEqual([m["role"] for m in messages], ["tool", "tool"])
        self.assertEqual([m["tool_call_id"] for m in messages], ["c1", "c2"])
        self.assertEqual(messages[0]["content"], '{"kind": "feature_edit"}')


class TestRecordingAndReplay(unittest.TestCase):

    def test_a_recorded_turn_round_trips_through_the_normalised_form(self):
        turn = ChatTurn(
            raw_content={"role": "assistant"},
            tool_uses=[ToolUse("c1", "solve_load_case", {"load_case_id": "lc"})],
            text="",
            stop_reason="tool_calls",
        )
        restored = ChatTurn.from_record(turn.to_record())
        self.assertEqual(restored.tool_uses[0].name, "solve_load_case")
        self.assertEqual(restored.tool_uses[0].input, {"load_case_id": "lc"})
        self.assertEqual(restored.stop_reason, "tool_calls")

    def test_the_recorder_delegates_shaping_to_the_client_it_wraps(self):
        inner = _client(lambda *a, **k: None)
        recorder = RecordingClient(inner)
        self.assertEqual(recorder.model, "muse-test")
        messages = recorder.tool_result_messages([ToolResult("c", "n", "{}")])
        self.assertEqual(messages[0]["tool_call_id"], "c")

    def test_replaying_past_the_end_raises_rather_than_inventing_a_turn(self):
        with self.assertRaises(LLMUnavailable):
            ReplayClient(turns=[]).chat(system="", messages=[], tools=[])


if __name__ == "__main__":
    unittest.main()
