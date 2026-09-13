"""Does the agent give the same answer twice? [Track A, Sprint 5A]

plan.md, Demo flow step 2, says the baseline should be caught "missing
something **or inconsistent across runs**". The first full live run settled
the first half: on the 10-bar truss the baseline missed nothing, because one
`solve_load_case` call returns every member and enumeration is therefore free
(see `earl/eval/README.md`). This module measures the second half.

The asymmetry being tested is not about being clever, it is about being the
same twice:

  * the system's traversal is a fixed BFS with sorted adjacency, and
    `tests/test_walker.py` pins byte-identical output across runs; the gate
    and Biject are deterministic on top of it. Repeat a scenario and the
    answer cannot change.
  * the baseline is a language model. Nothing guarantees run N+1 agrees with
    run N, and the first live run already showed it wobble: on one run of s01
    it completed the analysis correctly and never called `report_verdict`,
    and s09 needed the extraction pass.

`plan.md`'s headline metric counts dropped dominoes. An agent that reports m5
on Monday and nothing on Tuesday has dropped a domino on Tuesday, so
instability is not a softer claim than the original one -- it is the same
claim, measured over repeats instead of over scenarios.

What is reported per (scenario, agent):

  * `answers`     -- every distinct set of members reported across the runs;
  * `stable`      -- exactly one distinct answer;
  * `correct_runs`-- how many runs matched ground truth exactly.

A scenario that is stable AND correct is the only fully good outcome. Stable
and wrong is worse than it looks (it is reliably wrong), and unstable is the
finding this module exists to catch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import json

from .agents import Agent
from .harness import DEFAULT_THRESHOLD, run_scenario
from .scenarios import SCENARIOS, Scenario


@dataclass
class RepeatResult:
    """One scenario, one agent, repeated N times."""

    scenario_id: str
    agent: str
    truth: list[str] = field(default_factory=list)
    runs: list[list[str]] = field(default_factory=list)     # reported ids, per run
    outcomes: list[str] = field(default_factory=list)
    notes: list[str | None] = field(default_factory=list)

    @property
    def distinct(self) -> list[list[str]]:
        seen: list[list[str]] = []
        for run in self.runs:
            if run not in seen:
                seen.append(run)
        return seen

    @property
    def stable(self) -> bool:
        return len(self.distinct) <= 1

    @property
    def correct_runs(self) -> int:
        return sum(1 for run in self.runs if run == sorted(set(self.truth)))

    @property
    def dropped_total(self) -> int:
        """Dominoes dropped across all runs -- the headline metric, summed
        over repeats rather than over scenarios."""
        truth = set(self.truth)
        return sum(len(truth - set(run)) for run in self.runs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "agent": self.agent,
            "truth": self.truth,
            "runs": self.runs,
            "outcomes": self.outcomes,
            "notes": self.notes,
            "stable": self.stable,
            "distinct_answers": self.distinct,
            "correct_runs": self.correct_runs,
            "dropped_total": self.dropped_total,
        }


@dataclass
class ConsistencyReport:
    repeats: int = 0
    results: list[RepeatResult] = field(default_factory=list)

    def for_agent(self, agent: str) -> list[RepeatResult]:
        return [r for r in self.results if r.agent == agent]

    @property
    def agents(self) -> list[str]:
        out: list[str] = []
        for r in self.results:
            if r.agent not in out:
                out.append(r.agent)
        return out

    def to_markdown(self) -> str:
        lines = [
            f"Each scenario run {self.repeats}x.",
            "",
            "| agent | scenarios | unstable | runs correct | dropped dominoes "
            "(all runs) |",
            "|---|---:|---:|---:|---:|",
        ]
        for agent in self.agents:
            rows = self.for_agent(agent)
            unstable = [r for r in rows if not r.stable]
            correct = sum(r.correct_runs for r in rows)
            total_runs = sum(len(r.runs) for r in rows)
            dropped = sum(r.dropped_total for r in rows)
            lines.append(
                f"| {agent} | {len(rows)} | {len(unstable)} | "
                f"{correct}/{total_runs} | {dropped} |"
            )

        wobbly = [r for r in self.results if not r.stable]
        if wobbly:
            lines += ["", "Answers that changed between runs:"]
            for r in wobbly:
                shown = " | ".join(
                    ("(none)" if not a else ",".join(a)) for a in r.distinct
                )
                lines.append(
                    f"- {r.agent} / {r.scenario_id}: truth "
                    f"{','.join(r.truth) or '(none)'} -> {shown}"
                )
        else:
            lines += ["", "Every agent gave the same answer on every repeat."]
        return "\n".join(lines)

    def write(self, out_dir: str | Path) -> Path:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        path = out / "consistency.json"
        path.write_text(
            json.dumps(
                {
                    "repeats": self.repeats,
                    "results": [r.to_dict() for r in self.results],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        (out / "consistency.md").write_text(
            self.to_markdown() + "\n", encoding="utf-8"
        )
        return path


def run_consistency(
    agents: list[Agent],
    scenarios: tuple[Scenario, ...] | list[Scenario] = SCENARIOS,
    *,
    repeats: int = 3,
    threshold: float = DEFAULT_THRESHOLD,
    on_scenario=None,
) -> ConsistencyReport:
    """Replay each scenario `repeats` times and record what changed.

    Each repeat goes through `run_scenario`, so every guard the single-run
    harness has still applies: ground truth is recomputed per repeat, each
    agent gets its own build of the graph, and an `LLMUnavailable` stops the
    run rather than being scored.
    """
    if repeats < 1:
        raise ValueError(f"repeats must be at least 1, got {repeats}")

    report = ConsistencyReport(repeats=repeats)
    index: dict[tuple[str, str], RepeatResult] = {}

    for scenario in scenarios:
        for _ in range(repeats):
            outcome = run_scenario(scenario, agents, threshold=threshold)
            if not outcome.scorable:
                continue
            for agent in agents:
                answer = outcome.answers[agent.name]
                key = (scenario.id, agent.name)
                result = index.get(key)
                if result is None:
                    result = index[key] = RepeatResult(
                        scenario_id=scenario.id,
                        agent=agent.name,
                        truth=sorted(set(outcome.truth_unsafe_member_ids)),
                    )
                    report.results.append(result)
                result.runs.append(sorted(set(answer.unsafe_member_ids)))
                result.outcomes.append(answer.outcome.value)
                result.notes.append(answer.note)
        if on_scenario is not None:
            on_scenario(scenario, [index[(scenario.id, a.name)] for a in agents
                                   if (scenario.id, a.name) in index])

    return report


__all__ = ["ConsistencyReport", "RepeatResult", "run_consistency"]
