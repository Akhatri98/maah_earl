"""The replay: every scenario, every agent, one scoreboard. [Track A, Sprint 5A]

plan.md, Validation: "Each scenario's correct outcome is computed offline with
the validated solver -- that's ground truth" and "The identical 20 scenarios
are run through a tool-using LLM agent ... The gap between that baseline's
dropped-domino count and this system's is the core proof point -- not
asserted, measured."

This module runs that replay. Three rules hold it honest:

  * GROUND TRUTH IS COMPUTED, NEVER DECLARED.  `scoreboard.ground_truth()` --
    the validated solver over every load case plus Biject -- decides what was
    unsafe, and it runs on a graph no agent has touched. A scenario whose
    truth cannot be computed is recorded as an ERROR and EXCLUDED from the
    scoring, because scoring it as "nothing unsafe" would hand every agent a
    free scenario.
  * EACH AGENT GETS ITS OWN BUILD OF THE SCENARIO.  `Scenario.build()` is
    called once per agent, so the system's fixed walk (which writes
    `affected_member_ids` onto the graph it is given) cannot leak the
    traversal to the baseline agent that is supposed to lack it. This is the
    single most load-bearing line in the harness: share one graph and the
    comparison is void.
  * AN AGENT THAT CRASHES IS SCORED, NOT SKIPPED.  An exception becomes an
    ERROR answer reporting no members, which the scoreboard counts as silence
    about every unsafe member. "I could not run" is a dropped domino, and a
    harness that retried or skipped would be hiding the failure mode the
    project exists to measure.  The one exception is `LLMUnavailable` -- a
    missing key or a missing recording means the agent never answered at all,
    and turning that into a dropped domino would invent the headline number
    out of a misconfiguration, so it stops the run instead.

Records are the plain-JSON interchange Track B's scoreboard documents, so the
two halves stay decoupled: this module writes records, `scoreboard.score()`
folds them, and neither needs the other in memory.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from ..analysis.scoreboard import (
    ScenarioRecord,
    Scoreboard,
    ground_truth,
    save_records,
    score,
)
from ..analysis.solver import SolverError
from ..contracts import Outcome
from .agents import Agent, AgentAnswer, SystemAgent
from .llm import LLMUnavailable
from .scenarios import SCENARIOS, Scenario

DEFAULT_THRESHOLD = 1.0


@dataclass
class ScenarioOutcome:
    """One scenario's truth and every agent's answer to it."""

    scenario: Scenario
    truth_unsafe_member_ids: list[str] = field(default_factory=list)
    truth_error: str | None = None
    answers: dict[str, AgentAnswer] = field(default_factory=dict)
    seconds: dict[str, float] = field(default_factory=dict)

    @property
    def scorable(self) -> bool:
        return self.truth_error is None

    @property
    def truth_outcome(self) -> Outcome:
        return Outcome.ESCALATED if self.truth_unsafe_member_ids else Outcome.APPROVED


@dataclass
class EvalRun:
    """Everything the replay produced."""

    outcomes: list[ScenarioOutcome] = field(default_factory=list)
    records: list[ScenarioRecord] = field(default_factory=list)
    threshold: float = DEFAULT_THRESHOLD
    excluded: list[str] = field(default_factory=list)

    @property
    def board(self) -> Scoreboard:
        return score(self.records)

    def summary(self) -> str:
        """The demo slide: the scoreboard table, plus what the set contained
        and anything that had to be excluded from it."""
        unsafe = sum(1 for o in self.outcomes if o.scorable and o.truth_unsafe_member_ids)
        safe = sum(1 for o in self.outcomes if o.scorable and not o.truth_unsafe_member_ids)
        lines = [
            f"{len(self.outcomes)} scenarios at threshold {self.threshold:g}: "
            f"{unsafe} unsafe, {safe} safe by the validated solver.",
            "",
            self.board.to_markdown(),
        ]
        if self.excluded:
            lines += [
                "",
                "EXCLUDED (ground truth could not be computed; not scored for "
                "any agent):",
                *(f"- {line}" for line in self.excluded),
            ]
        return "\n".join(lines)


def run_scenario(
    scenario: Scenario,
    agents: list[Agent],
    *,
    threshold: float = DEFAULT_THRESHOLD,
) -> ScenarioOutcome:
    """Ground truth for one scenario, then every agent's answer to it.

    Truth is computed on its own build of the graph, before any agent runs.
    """
    outcome = ScenarioOutcome(scenario=scenario)

    try:
        outcome.truth_unsafe_member_ids = ground_truth(
            scenario.build().after, threshold=threshold
        )
    except (SolverError, ValueError) as e:
        outcome.truth_error = f"{type(e).__name__}: {e}"
        return outcome

    for agent in agents:
        graphs = scenario.build()      # a fresh build per agent -- see the module docstring
        started = time.monotonic()
        try:
            answer = agent.answer(scenario, graphs)
        except LLMUnavailable:
            # NOT an agent failure. A missing credential or a missing
            # recording means the agent never got to answer, and scoring that
            # as silence would invent a dropped domino out of a
            # misconfiguration -- a wrong number on the one slide that matters.
            # It propagates and stops the run instead.
            raise
        except Exception as e:         # scored as silence, never skipped
            answer = AgentAnswer(
                outcome=Outcome.ERROR,
                note=f"agent raised {type(e).__name__}: {e}",
            )
        outcome.seconds[agent.name] = time.monotonic() - started
        outcome.answers[agent.name] = answer

    return outcome


def run_eval(
    agents: list[Agent] | None = None,
    scenarios: tuple[Scenario, ...] | list[Scenario] = SCENARIOS,
    *,
    threshold: float = DEFAULT_THRESHOLD,
    on_scenario=None,
) -> EvalRun:
    """Replay every scenario through every agent and collect the records.

    `on_scenario(outcome)` is called after each scenario so a CLI can print
    progress without this module knowing about a terminal.
    """
    agents = agents if agents is not None else [SystemAgent(threshold=threshold)]
    run = EvalRun(threshold=threshold)

    for scenario in scenarios:
        outcome = run_scenario(scenario, agents, threshold=threshold)
        run.outcomes.append(outcome)

        if outcome.scorable:
            for agent in agents:
                answer = outcome.answers[agent.name]
                run.records.append(
                    answer.record(
                        scenario.id, agent.name, outcome.truth_unsafe_member_ids
                    )
                )
        else:
            run.excluded.append(f"{scenario.id}: {outcome.truth_error}")

        if on_scenario is not None:
            on_scenario(outcome)

    return run


def write_run(run: EvalRun, out_dir: str | Path) -> dict[str, Path]:
    """Persist the replay: the records, the scoreboard as JSON, and the
    Markdown table the demo puts on screen."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written = {"records": save_records(run.records, out / "records.json")}

    board = run.board
    (out / "scoreboard.json").write_text(board.to_json(), encoding="utf-8")
    written["scoreboard"] = out / "scoreboard.json"
    (out / "scoreboard.md").write_text(run.summary() + "\n", encoding="utf-8")
    written["summary"] = out / "scoreboard.md"
    return written


__all__ = [
    "DEFAULT_THRESHOLD",
    "EvalRun",
    "ScenarioOutcome",
    "run_eval",
    "run_scenario",
    "write_run",
]
