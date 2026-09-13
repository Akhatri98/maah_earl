"""The baseline agent: a tool-using LLM with no traversal and no threshold. [Track A, Sprint 5A]

plan.md, Demo flow step 2: "Show the baseline agent (LLM + PyNite tool access,
no fixed graph traversal or enforced thresholds) answering the same question,
missing something or inconsistent across runs."

This is that agent. It is written to be GOOD, not to be a straw man -- see the
prompt below, which tells it plainly that the change can affect members it did
not touch and that it should check every member. If it still drops dominoes,
that is the finding; if it does not, the project has learned something real
and the honest thing is to report that instead.

What it has and what it lacks is defined in `tools.py`, not here. This module
only runs the loop:

    system + tools -> model -> tool_use blocks -> ToolBox -> tool_result -> ...
                                    |
                                    +-- report_verdict -> done

## The ways a run can end

  * IT REPORTS. `report_verdict` gives the member list and the outcome word,
    both taken at face value and scored as-is. A verdict that contradicts the
    numbers the agent itself fetched is not corrected -- there is no Biject on
    this path, which is the entire point.
  * IT ANSWERS IN PROSE. If the model finishes its analysis in text without
    calling the tool, it gets ONE nudge; if it still does not call, the
    verdict is EXTRACTED from its own prose by a separate transcription-only
    pass (`_extract`, and the reasoning is written out there). Every such
    verdict is flagged in the transcript, in `AgentAnswer.detail` and in the
    scenario record's note.
  * IT SAYS NOTHING USABLE. No prose and no tool call, or an extraction pass
    that yields no verdict either, becomes an ERROR answer -- which the
    scoreboard counts as silence about every unsafe member.
  * IT RUNS OUT OF TURNS. `MAX_TURNS` caps the loop so one confused scenario
    cannot spend the whole budget. The cap also tries extraction, so a
    half-finished run is read for whatever conclusion it did reach.

Nothing here retries a failed scenario. A crash or a timeout is a real result
for an agent under evaluation, and hiding it would flatter the baseline.
Equally, nothing here discards a conclusion the agent actually reached just
because it arrived in the wrong format -- that would flatter the system.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..analysis.scoreboard import AGENT_BASELINE
from ..contracts import Outcome
from .agents import AgentAnswer
from .llm import (
    ChatTurn,
    LLMUnavailable,
    RecordingClient,
    ToolChatClient,
    ToolResult,
)
from .scenarios import Scenario, ScenarioGraphs
from .tools import REPORT_TOOL, ToolBox, tool_specs

MAX_TURNS = 24

SYSTEM_PROMPT = """\
You are a structural engineer reviewing a proposed change to a steel/aluminium \
truss before it is allowed to merge. Your job is to decide which members, if \
any, are UNSAFE after the change.

You have tools that give you the full structure, the material properties, and \
a finite-element solver you can run as often as you like on either the state \
before the change or the state after it.

How safety is defined here:

  safety factor = allowable stress / |axial stress|

A member is UNSAFE when its safety factor is below 1.0 -- that is, when the \
magnitude of its axial stress exceeds the material's allowable stress. Both \
tension and compression count: use the absolute value of the stress. The \
allowable stress is on the material, from get_material.

Two things about this structure that matter:

  * It is statically indeterminate. Changing ONE member redistributes force \
through the whole truss, so a change can overstress a member that was not \
touched -- including members some distance from the edit.
  * Making a member BIGGER is not automatically safe. A stiffer member \
attracts more load, which can overstress a neighbour.

Check every member, not just the one named in the change. When you are done, \
call report_verdict with every member you judge unsafe. Report 'escalated' if \
any member is unsafe and the change needs a human decision, 'approved' if the \
change is safe to merge.
"""

USER_PROMPT = """\
A change has been made to the truss and is waiting for review. Use the tools \
to investigate it, then call {report} with your answer.
"""

# The extraction pass. Deliberately gives the model NOTHING to reason with --
# no structure, no solver, no threshold rule, no tools but the report itself --
# so it can only transcribe a conclusion, never reach one. See
# `BaselineAgent._extract` for why this exists.
EXTRACT_PROMPT = """\
You are given a structural engineer's completed review of a change to a truss. \
Your only job is to record their conclusion in structured form by calling \
report_verdict.

Transcribe, do not analyse. List exactly the members the review concludes are \
unsafe — no more, no fewer. Do not add a member the review did not call \
unsafe, and do not drop one it did. If the review concludes nothing is unsafe, \
report an empty list and outcome 'approved'; otherwise report 'escalated'.
"""


@dataclass
class BaselineRun:
    """The transcript of one scenario, enough to replay or to show on stage."""

    scenario_id: str
    model: str
    turns: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    verdict: dict[str, Any] | None = None
    extracted: bool = False
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "model": self.model,
            "turns": self.turns,
            "tool_calls": self.tool_calls,
            "verdict": self.verdict,
            "extracted": self.extracted,
            "note": self.note,
        }

    def write(self, directory: str | Path) -> Path:
        out = Path(directory)
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"{self.scenario_id}.json"
        path.write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")
        return path


@dataclass
class BaselineAgent:
    """LLM + PyNite, no graph walk, no enforced threshold.

    `client_for` exists for replay: a recording is per scenario, so the
    transport has to be rebuilt for each one. A live run leaves it None and
    every scenario shares the one client.
    """

    client: ToolChatClient | None = None
    client_for: Callable[[str], ToolChatClient] | None = None
    name: str = AGENT_BASELINE
    max_turns: int = MAX_TURNS
    transcript_dir: str | Path | None = None
    # The transcript of the most recent scenario, for the harness / demo.
    last_run: BaselineRun | None = None

    def __post_init__(self) -> None:
        if self.client is None and self.client_for is None:
            raise ValueError("BaselineAgent needs a client or a client_for factory")

    def _client(self, scenario_id: str) -> ToolChatClient:
        if self.client_for is not None:
            return self.client_for(scenario_id)
        assert self.client is not None
        return self.client

    # -- the loop -----------------------------------------------------------

    def answer(self, scenario: Scenario, graphs: ScenarioGraphs) -> AgentAnswer:
        toolbox = ToolBox(graphs)
        client = self._client(scenario.id)
        recorder = RecordingClient(client)
        run = BaselineRun(scenario_id=scenario.id, model=client.model)
        self.last_run = run

        messages: list[dict[str, Any]] = [
            {"role": "user", "content": USER_PROMPT.format(report=REPORT_TOOL)}
        ]
        specs = tool_specs()
        nudged = False
        analysis = ""              # the most recent non-empty prose answer

        for _ in range(self.max_turns):
            try:
                turn = recorder.chat(
                    system=SYSTEM_PROMPT, messages=messages, tools=specs
                )
            except LLMUnavailable:
                raise
            except Exception as e:
                run.note = f"transport failed: {type(e).__name__}: {e}"
                run.turns = recorder.turns
                return self._finish(run, None, run.note)

            run.turns = recorder.turns
            # The client owns message shaping: chat completions wants the
            # assistant turn echoed back with its tool_calls intact, and one
            # `role: "tool"` message per result. See llm.ChatCompletionsShaping.
            messages.append(recorder.assistant_message(turn))
            if turn.text.strip():
                analysis = turn.text.strip()

            verdict = self._verdict_in(turn)
            if verdict is not None:
                run.verdict = verdict
                return self._finish(run, verdict, None)

            if not turn.tool_uses:
                if nudged:
                    return self._extract(run, recorder, analysis)
                nudged = True
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"Please finish the review by calling {REPORT_TOOL} "
                            "with the members you judge unsafe."
                        ),
                    }
                )
                continue

            results = []
            for call in turn.tool_uses:
                payload = toolbox.call_json(call.name, call.input)
                run.tool_calls.append(
                    {"name": call.name, "input": call.input, "result": payload}
                )
                results.append(
                    ToolResult(call_id=call.id, name=call.name, payload=payload)
                )
            # EVERY call the model made gets a result, including the ones that
            # errored. A missing result for a call it made is a protocol error
            # that the next request will reject, and silently dropping one
            # would also teach it to stop calling tools in parallel.
            messages.extend(recorder.tool_result_messages(results))

        return self._extract(
            run, recorder, analysis,
            reason=f"hit the {self.max_turns}-turn cap without reporting a verdict",
        )

    # -- extraction ---------------------------------------------------------

    def _extract(
        self,
        run: BaselineRun,
        client: ToolChatClient,
        analysis: str,
        reason: str = "model finished in prose without calling " + REPORT_TOOL,
    ) -> AgentAnswer:
        """Last resort: ask the model to record the verdict it already wrote.

        WHY THIS EXISTS. On the first live run the model did the engineering
        correctly -- it named m5 at SF 0.73 and explained the redistribution --
        and then simply ended its turn in prose without calling the tool.
        Scoring that as silence would have cost it a domino it had actually
        found, and the eval would have been measuring protocol compliance
        rather than engineering judgement. Muse only accepts
        `tool_choice: "auto"`, so the verdict cannot be forced out of the
        transport; this pass asks for it instead.

        WHY IT IS NOT A THUMB ON THE SCALE. The extractor is a transcription
        step, not an analysis step: it sees ONLY the baseline's own prose, has
        no tools, no structure, no solver and no threshold rule, and is told
        not to add or remove members. It cannot find a domino the baseline
        missed, only record one the baseline found. Every verdict obtained
        this way is marked in the transcript and in the scenario record's
        note, so the scoreboard never hides how the answer was read.
        """
        if not analysis:
            run.note = reason + "; no prose to extract a verdict from"
            return self._finish(run, None, run.note)

        report_spec = [s for s in tool_specs() if s["name"] == REPORT_TOOL]
        try:
            turn = client.chat(
                system=EXTRACT_PROMPT,
                messages=[{"role": "user", "content": analysis}],
                tools=report_spec,
            )
        except LLMUnavailable:
            raise
        except Exception as e:
            run.note = f"{reason}; extraction failed: {type(e).__name__}: {e}"
            run.turns = getattr(client, "turns", run.turns)
            return self._finish(run, None, run.note)

        run.turns = getattr(client, "turns", run.turns)
        verdict = self._verdict_in(turn)
        if verdict is None:
            run.note = reason + "; extraction produced no verdict either"
            return self._finish(run, None, run.note)

        run.verdict = verdict
        run.extracted = True
        run.note = reason + "; verdict read back from the model's own prose"
        return self._finish(run, verdict, None, extracted=True)

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _verdict_in(turn: ChatTurn) -> dict[str, Any] | None:
        for call in turn.tool_uses:
            if call.name == REPORT_TOOL:
                return dict(call.input)
        return None

    def _finish(
        self,
        run: BaselineRun,
        verdict: dict[str, Any] | None,
        note: str | None,
        *,
        extracted: bool = False,
    ) -> AgentAnswer:
        if self.transcript_dir is not None:
            run.write(self.transcript_dir)

        if verdict is None:
            return AgentAnswer(
                unsafe_member_ids=[],
                outcome=Outcome.ERROR,
                note=note or "no verdict",
                detail={"tool_calls": len(run.tool_calls)},
            )

        # Taken at face value. A verdict that contradicts the agent's own
        # numbers is exactly what this agent is here to be able to produce.
        raw_ids = verdict.get("unsafe_member_ids") or []
        member_ids = [str(m) for m in raw_ids if isinstance(m, (str, int))]
        raw_outcome = str(verdict.get("outcome", "")).lower()
        outcome = (
            Outcome.ESCALATED if raw_outcome == "escalated" else Outcome.APPROVED
        )
        rationale = verdict.get("rationale")
        return AgentAnswer(
            unsafe_member_ids=member_ids,
            outcome=outcome,
            # The note reaches the scenario record, so a verdict read back from
            # prose is never silently indistinguishable from a reported one.
            note=(
                f"[verdict extracted from the model's prose] {rationale or ''}".strip()
                if extracted
                else rationale
            ),
            detail={
                "tool_calls": len(run.tool_calls),
                "tools_used": sorted({c["name"] for c in run.tool_calls}),
                "extracted": extracted,
            },
        )


__all__ = ["MAX_TURNS", "SYSTEM_PROMPT", "BaselineAgent", "BaselineRun"]
