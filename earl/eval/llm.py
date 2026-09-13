"""Tool-using chat transport for the baseline agent. [Track A, Sprint 5A]

The whole project runs on Meta Muse. `earl.analysis.orchestrator` already
talks to it for the Stage 2 planner, but that client is single-turn
(`complete(system, user) -> str`), which is all a planner needs. The baseline
agent is a different shape: it holds a multi-turn conversation and calls
tools, so it needs its own transport over the same endpoint.

The base URL, model, credential and response cap all come from
`orchestrator` rather than being restated here, so there is exactly one place
that says where Muse lives and the two clients can never drift apart.

Four implementations, one protocol:

  * `MuseToolClient` -- the real thing, OpenAI-compatible chat completions
    with tool calling.
  * `RecordingClient` -- wraps any client and captures every turn.
  * `ReplayClient` -- replays a recorded transcript with no network at all.
  * `ScriptedClient` -- a fixed list of turns, for tests.

## Two message shapes, one loop

The agent loop in `baseline.py` is provider-neutral: it never builds a
message. Shaping the assistant turn and the tool results is the client's job
(`assistant_message` / `tool_result_messages`), because the chat-completions
shape differs from the block-based one in ways that are easy to get subtly
wrong -- tool arguments arrive as a JSON *string*, and each result is its own
`role: "tool"` message rather than several blocks in one user message.

## Replay is exact, not approximate

Every tool in `tools.py` is a pure function of the scenario graphs, so
feeding back the recorded model turns reproduces the same tool calls and the
same verdict. Transcripts record the NORMALISED turn (tool calls, text, stop
reason) alongside the raw payload, so a recording stays replayable even if the
wire format changes underneath it.

That is what lets the demo run the baseline from a recording without the
scoreboard becoming a fiction -- the numbers came from real model runs, and
`records.json` says which.

## Why the baseline runs on the same model as everything else

It would flatter the comparison to put a weaker model in the baseline seat.
It would also invalidate it: the claim in plan.md is that the gap comes from
structure -- fixed traversal, code-enforced thresholds -- not from the model
being bad at arithmetic. So the baseline runs on the same Muse model the rest
of the pipeline uses. Any dominoes it drops are dropped by the project's own
model, which simply was not made to check every member.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import requests

from ..analysis.orchestrator import (
    DEFAULT_LLM_BASE_URL,
    DEFAULT_LLM_MODEL,
    LLM_TIMEOUT_SECONDS,
    LLMError,
)
from ..config import load_env

API_KEY_ENV = "META_MUSE_KEY"
# A tool-calling turn carries solver output back to the model, so replies run
# larger than the planner's few hundred bytes of plan JSON -- but they are
# still bounded, and an unbounded reply is not something the eval should
# spend time parsing.
MAX_RESPONSE_BYTES = 1_000_000
# The agent is asked to reason over ten members; a turn that wants more than
# this is not making progress.
DEFAULT_MAX_TOKENS = 4096


class LLMUnavailable(RuntimeError):
    """No transport could be built -- missing credential, or a recording that
    does not exist. Raised at construction or at the start of a scenario,
    never silently converted into an answer, so a misconfigured eval fails
    before it half-populates a scoreboard."""


@dataclass
class ToolUse:
    """One tool call the model asked for."""

    id: str
    name: str
    input: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolResult:
    """One executed tool call, on its way back to the model."""

    call_id: str
    name: str
    payload: str            # JSON, already serialised by the toolbox


@dataclass
class ChatTurn:
    """One model response, normalised away from any provider's types."""

    raw_content: Any = None          # the provider's own assistant payload
    tool_uses: list[ToolUse] = field(default_factory=list)
    text: str = ""
    stop_reason: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)

    def to_record(self) -> dict[str, Any]:
        """The transcript entry: the normalised turn, plus the raw payload for
        forensics. Replay reads the normalised half, so a recording survives a
        change of wire format."""
        return {
            "tool_uses": [
                {"id": t.id, "name": t.name, "input": t.input} for t in self.tool_uses
            ],
            "text": self.text,
            "stop_reason": self.stop_reason,
            "usage": self.usage,
            "raw": self.raw_content,
        }

    @classmethod
    def from_record(cls, data: dict[str, Any]) -> ChatTurn:
        return cls(
            raw_content=data.get("raw"),
            tool_uses=[
                ToolUse(
                    id=t.get("id", ""),
                    name=t.get("name", ""),
                    input=dict(t.get("input") or {}),
                )
                for t in data.get("tool_uses", [])
            ],
            text=data.get("text", ""),
            stop_reason=data.get("stop_reason"),
            usage=data.get("usage", {}),
        )


class ToolChatClient(Protocol):
    """A multi-turn, tool-calling chat transport."""

    model: str

    def chat(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ChatTurn: ...

    def assistant_message(self, turn: ChatTurn) -> dict[str, Any]: ...

    def tool_result_messages(
        self, results: list[ToolResult]
    ) -> list[dict[str, Any]]: ...


class ChatCompletionsShaping:
    """Message shaping for the OpenAI-compatible chat-completions format.

    Shared by every client here, including the offline ones -- they never send
    a message, but the agent loop still builds the history through them, so
    keeping one shape means a replayed run walks the same code path as a live
    one.
    """

    def assistant_message(self, turn: ChatTurn) -> dict[str, Any]:
        """The assistant turn, echoed back verbatim when the provider gave us
        one. Reconstructed from the normalised turn otherwise, which is what a
        replayed recording needs."""
        if isinstance(turn.raw_content, dict):
            return turn.raw_content
        message: dict[str, Any] = {"role": "assistant", "content": turn.text or None}
        if turn.tool_uses:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.input),
                    },
                }
                for call in turn.tool_uses
            ]
        return message

    def tool_result_messages(
        self, results: list[ToolResult]
    ) -> list[dict[str, Any]]:
        """One `role: "tool"` message per call. Chat completions has no
        multi-result user message -- every result is addressed to the call it
        answers by `tool_call_id`, and omitting one for a call the model made
        is a protocol error, not a style choice."""
        return [
            {
                "role": "tool",
                "tool_call_id": result.call_id,
                "name": result.name,
                "content": result.payload,
            }
            for result in results
        ]


def to_openai_tools(specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert the neutral tool specs from `tools.py` into the function-calling
    shape chat completions expects."""
    return [
        {
            "type": "function",
            "function": {
                "name": spec["name"],
                "description": spec["description"],
                "parameters": spec["input_schema"],
            },
        }
        for spec in specs
    ]


# --------------------------------------------------------------------------
# Meta Muse
# --------------------------------------------------------------------------

@dataclass
class MuseToolClient(ChatCompletionsShaping):
    """Meta Muse over OpenAI-compatible chat completions, with tool calling.

    `post` is injectable so tests exercise the exact URL, headers and body
    without touching the network, exactly as `orchestrator.MetaModelClient`
    does.
    """

    api_key: str = field(default="", repr=False)   # never let a log line print it
    base_url: str = DEFAULT_LLM_BASE_URL
    model: str = DEFAULT_LLM_MODEL
    max_tokens: int = DEFAULT_MAX_TOKENS
    timeout: int = LLM_TIMEOUT_SECONDS
    post: Any = requests.post

    @classmethod
    def from_env(cls, *, model: str | None = None) -> MuseToolClient:
        load_env()
        key = os.environ.get(API_KEY_ENV, "")
        if not key:
            raise LLMUnavailable(
                f"{API_KEY_ENV} is not set. The baseline agent needs a model; "
                "add the key to .env, or run the eval with --agents system, or "
                "replay a recording with --replay."
            )
        return cls(
            api_key=key,
            base_url=os.environ.get("LLM_BASE_URL", DEFAULT_LLM_BASE_URL).rstrip("/"),
            model=model or os.environ.get("LLM_MODEL", DEFAULT_LLM_MODEL),
        )

    @property
    def url(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"

    def chat(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ChatTurn:
        # `tool_choice` is always "auto": Muse rejects every other value with
        #   400 only `"auto"` is supported for `tool_choice`. `"none"`,
        #   `"required"`, and named function choices are not currently
        #   supported
        # so a verdict cannot be forced out of the model by the transport.
        # `baseline.py` handles that with an extraction pass instead -- see
        # its EXTRACT_PROMPT and the comment above it.
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *messages],
            "tools": to_openai_tools(tools),
            "tool_choice": "auto",
            "max_tokens": self.max_tokens,
            "temperature": 0,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        try:
            response = self.post(
                self.url, json=body, headers=headers, timeout=self.timeout
            )
        except Exception as e:      # requests.RequestException and friends
            raise LLMError(f"LLM request failed: {type(e).__name__}: {e}") from e

        status = getattr(response, "status_code", None)
        if status != 200:
            text = str(getattr(response, "text", ""))[:300]
            raise LLMError(f"LLM HTTP {status}: {text}")

        raw = getattr(response, "text", "") or ""
        if len(raw.encode("utf-8", errors="ignore")) > MAX_RESPONSE_BYTES:
            raise LLMError(
                f"LLM response too long (> {MAX_RESPONSE_BYTES} bytes); refusing to parse"
            )

        try:
            message = response.json()["choices"][0]["message"]
        except Exception as e:
            raise LLMError(f"LLM response not in chat-completions shape: {e}") from e

        return self._turn(message, response)

    def _turn(self, message: dict[str, Any], response: Any) -> ChatTurn:
        tool_uses = []
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            tool_uses.append(
                ToolUse(
                    id=str(call.get("id", "")),
                    name=str(function.get("name", "")),
                    input=_parse_arguments(function.get("arguments")),
                )
            )
        try:
            payload = response.json()
        except Exception:           # pragma: no cover - already parsed above
            payload = {}
        content = message.get("content")
        return ChatTurn(
            raw_content=message,
            tool_uses=tool_uses,
            text=content if isinstance(content, str) else "",
            stop_reason=(payload.get("choices") or [{}])[0].get("finish_reason"),
            usage=payload.get("usage") or {},
        )


def _parse_arguments(arguments: Any) -> dict[str, Any]:
    """Tool arguments arrive as a JSON STRING in chat completions. A model that
    emits malformed JSON gets an empty argument dict, which the toolbox then
    rejects with an error result -- costing it a turn rather than the run."""
    if isinstance(arguments, dict):
        return arguments
    if not isinstance(arguments, str) or not arguments.strip():
        return {}
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


# --------------------------------------------------------------------------
# Recording and replay
# --------------------------------------------------------------------------

@dataclass
class RecordingClient(ChatCompletionsShaping):
    """Wraps a live client and appends every turn to `turns`.

    The agent writes `turns` out per scenario, so a live run leaves behind
    everything a replay needs.
    """

    inner: Any = None
    turns: list[dict[str, Any]] = field(default_factory=list)

    @property
    def model(self) -> str:
        return self.inner.model

    def chat(self, **kwargs) -> ChatTurn:
        turn = self.inner.chat(**kwargs)
        self.turns.append(turn.to_record())
        return turn

    def assistant_message(self, turn: ChatTurn) -> dict[str, Any]:
        return self.inner.assistant_message(turn)

    def tool_result_messages(self, results: list[ToolResult]) -> list[dict[str, Any]]:
        return self.inner.tool_result_messages(results)


@dataclass
class ReplayClient(ChatCompletionsShaping):
    """Replays recorded turns in order. Never touches the network.

    Running past the end of a recording raises rather than inventing a turn: a
    replay that has drifted from what was recorded must fail loudly, not
    quietly produce a different answer than the one the scoreboard reports.
    """

    turns: list[dict[str, Any]] = field(default_factory=list)
    model: str = DEFAULT_LLM_MODEL
    _index: int = 0

    @classmethod
    def from_file(cls, path: str | Path) -> ReplayClient:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(turns=data.get("turns", []), model=data.get("model", DEFAULT_LLM_MODEL))

    def chat(self, **kwargs) -> ChatTurn:
        if self._index >= len(self.turns):
            raise LLMUnavailable(
                f"replay exhausted after {len(self.turns)} turn(s); the recording "
                "does not match this run"
            )
        turn = self.turns[self._index]
        self._index += 1
        return ChatTurn.from_record(turn)


@dataclass
class ScriptedClient(ChatCompletionsShaping):
    """A fixed list of turns, for tests."""

    scripted: list[ChatTurn] = field(default_factory=list)
    model: str = "scripted"
    _index: int = 0

    def chat(self, **kwargs) -> ChatTurn:
        if self._index >= len(self.scripted):
            raise LLMUnavailable("scripted client exhausted")
        turn = self.scripted[self._index]
        self._index += 1
        return turn


__all__ = [
    "API_KEY_ENV",
    "DEFAULT_MAX_TOKENS",
    "MAX_RESPONSE_BYTES",
    "ChatCompletionsShaping",
    "ChatTurn",
    "LLMError",
    "LLMUnavailable",
    "MuseToolClient",
    "RecordingClient",
    "ReplayClient",
    "ScriptedClient",
    "ToolChatClient",
    "ToolResult",
    "ToolUse",
    "to_openai_tools",
]
