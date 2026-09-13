"""Dropped-domino scoreboard -- the headline metric, computed. [Track B, Sprint 5B]

plan.md: "Headline metric -- dropped dominoes: affected members whose unsafe
state the system fails to catch or report. Target: zero." and "The gap
between that baseline's dropped-domino count and this system's is the core
proof point -- not asserted, measured."

This module is the measuring instrument.  It knows nothing about how an agent
reached its answer: a `ScenarioRecord` is one scenario, one agent under test,
what was truly unsafe and what that agent reported.  `score()` folds records
into one `AgentScore` per agent:

  * dropped domino  = a truth-unsafe member the agent did NOT report
    (per scenario, listed by scenario id so a demo can point at it);
  * false alarm     = a member the agent reported that is NOT truth-unsafe;
  * caught          = truth-unsafe AND reported.

A scenario whose truth outcome is ESCALATED but was reported APPROVED (or
ERROR) with no members listed counts every truth-unsafe member as dropped --
"I could not run" and "everything is fine" are both silence about a member
that is unsafe, which is exactly the failure the metric exists to count.

Ground truth comes from the validated solver plus Biject over EVERY load case
(`ground_truth`), never from a model.  Records are plain JSON
(`save_records` / `load_records`) so Track A's eval harness (Sprint 5A) can
write them from any agent and this module scores them all the same way.

Serialisation (R12): `Scoreboard.scores` is a list of `AgentScore`, not a
dict, because the contract's Serializable decoder rebuilds dataclasses inside
lists but not inside dict values; `score_for()` and `agents` give the dict
ergonomics back.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..contracts import Decision, DependencyGraph, MemberStatus, Outcome, Serializable
from .biject import evaluate
from .solver import solve

AGENT_SYSTEM = "system"
AGENT_BASELINE = "baseline"


# --------------------------------------------------------------------------
# Records: the interchange format
# --------------------------------------------------------------------------

@dataclass
class ScenarioRecord(Serializable):
    """One scenario, one agent under test.

    `truth_unsafe_member_ids` is what the validated solver says is unsafe;
    `reported_unsafe_member_ids` is what the agent said.  Outcomes are the
    `Outcome` values as strings ("approved" | "escalated" | "error") so the
    file is readable without this package.
    """

    scenario_id: str
    agent: str                       # "system" | "baseline" | anything
    truth_unsafe_member_ids: list[str] = field(default_factory=list)
    reported_unsafe_member_ids: list[str] = field(default_factory=list)
    truth_outcome: str | None = None
    reported_outcome: str | None = None
    note: str | None = None


def record_from_decision(
    scenario_id: str,
    agent: str,
    truth_unsafe_member_ids: list[str],
    decision: Decision,
) -> ScenarioRecord:
    """A record for an agent whose answer is a contract Decision.

    Reported members are the FAIL results plus `violating_member_ids` (equal
    by `Decision.validate()`, unioned defensively).  An ERROR decision reports
    nothing and carries its message in the note -- it is scored as silence.
    """
    reported = set(decision.violating_member_ids)
    reported.update(
        r.member_id for r in decision.member_results if r.status is MemberStatus.FAIL
    )
    truth = sorted(set(truth_unsafe_member_ids))
    outcome = decision.outcome
    return ScenarioRecord(
        scenario_id=scenario_id,
        agent=agent,
        truth_unsafe_member_ids=truth,
        reported_unsafe_member_ids=sorted(reported),
        truth_outcome=(Outcome.ESCALATED if truth else Outcome.APPROVED).value,
        reported_outcome=outcome.value if isinstance(outcome, Outcome) else str(outcome),
        note=decision.error_message if outcome is Outcome.ERROR else None,
    )


def ground_truth(graph: DependencyGraph, *, threshold: float) -> list[str]:
    """Unsafe member ids of `graph`: the validated solver on EVERY load case
    (contract self-weight flags) and Biject at `threshold`, worst case wins.
    Raises SolverError / ValueError when the truth cannot be computed -- a
    scenario without ground truth must not be scored as "nothing unsafe"."""
    graph.units.assert_si()
    graph.validate()
    if not graph.load_cases:
        raise ValueError(f"graph {graph.id!r} has no load cases; no ground truth")
    results = [solve(graph, lc.id) for lc in graph.load_cases]
    return list(evaluate(graph, results, threshold).violating_member_ids)


def load_records(path: str | Path) -> list[ScenarioRecord]:
    """Read a JSON list of ScenarioRecord objects (or {"records": [...]})."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict) and "records" in data:
        data = data["records"]
    if not isinstance(data, list):
        raise ValueError(f"{path}: expected a JSON list of scenario records")
    return [ScenarioRecord.from_dict(item) for item in data]


def save_records(records: list[ScenarioRecord], path: str | Path) -> Path:
    """Write records as a JSON list; creates parent directories."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([r.to_dict() for r in records], indent=2) + "\n", encoding="utf-8"
    )
    return path


# --------------------------------------------------------------------------
# Scores
# --------------------------------------------------------------------------

def _ratio(numerator: int, denominator: int) -> float:
    """1.0 when there was nothing to get wrong."""
    return 1.0 if denominator == 0 else numerator / denominator


@dataclass
class AgentScore(Serializable):
    """One agent's tally over every scenario it has a record for."""

    agent: str
    scenarios: int = 0
    unsafe_members_total: int = 0
    caught: int = 0
    dropped_dominoes: int = 0
    false_alarms: int = 0
    # scenario id -> the truth-unsafe members that agent failed to report.
    dropped_by_scenario: dict[str, list[str]] = field(default_factory=dict)

    @property
    def recall(self) -> float:
        """caught / truth-unsafe; the plan's target is 1.0 (zero dropped)."""
        return _ratio(self.caught, self.unsafe_members_total)

    @property
    def precision(self) -> float:
        """caught / reported; 1.0 when nothing was reported."""
        return _ratio(self.caught, self.caught + self.false_alarms)

    def to_dict(self) -> dict[str, Any]:
        # The derived ratios are added for readers of the JSON; from_dict
        # ignores keys that are not fields, so the round trip is unaffected.
        data = super().to_dict()
        data["recall"] = self.recall
        data["precision"] = self.precision
        return data


@dataclass
class Scoreboard(Serializable):
    """Every agent's score, in first-seen record order."""

    scores: list[AgentScore] = field(default_factory=list)

    @property
    def agents(self) -> list[str]:
        return [s.agent for s in self.scores]

    def score_for(self, agent: str) -> AgentScore:
        for s in self.scores:
            if s.agent == agent:
                return s
        raise KeyError(f"no score for agent {agent!r}")

    def to_dict(self) -> dict[str, Any]:
        return {"scores": [s.to_dict() for s in self.scores]}

    def to_markdown(self) -> str:
        """A table for the demo slide, plus the dropped dominoes by scenario
        so the number can be traced to the member it stands for."""
        lines = [
            "| agent | scenarios | unsafe members | caught | dropped dominoes "
            "| false alarms | recall | precision |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for s in self.scores:
            lines.append(
                f"| {s.agent} | {s.scenarios} | {s.unsafe_members_total} | {s.caught} "
                f"| {s.dropped_dominoes} | {s.false_alarms} "
                f"| {s.recall:.2f} | {s.precision:.2f} |"
            )
        if not self.scores:
            lines.append("| (no records) | 0 | 0 | 0 | 0 | 0 | 1.00 | 1.00 |")

        dropped = [s for s in self.scores if s.dropped_by_scenario]
        if dropped:
            lines.append("")
            lines.append("Dropped dominoes by scenario:")
            for s in dropped:
                for scenario_id, members in s.dropped_by_scenario.items():
                    lines.append(f"- {s.agent} / {scenario_id}: {', '.join(members)}")
        return "\n".join(lines)


def score(records: list[ScenarioRecord]) -> Scoreboard:
    """Fold records into one AgentScore per agent (first-seen order).

    Per record: truth = set(truth ids), reported = set(reported ids);
    caught = truth & reported, dropped = truth - reported, false alarms =
    reported - truth.  An agent that reported no members (APPROVED, ERROR,
    or an empty list) on an ESCALATED-truth scenario drops every one of
    them -- that is not a special case, it is the definition.
    """
    scores: dict[str, AgentScore] = {}
    for rec in records:
        tally = scores.get(rec.agent)
        if tally is None:
            tally = scores[rec.agent] = AgentScore(agent=rec.agent)

        truth = set(rec.truth_unsafe_member_ids)
        reported = set(rec.reported_unsafe_member_ids)
        dropped = sorted(truth - reported)
        tally.scenarios += 1
        tally.unsafe_members_total += len(truth)
        tally.caught += len(truth & reported)
        tally.dropped_dominoes += len(dropped)
        tally.false_alarms += len(reported - truth)
        if dropped:
            tally.dropped_by_scenario[rec.scenario_id] = dropped

    return Scoreboard(scores=list(scores.values()))


__all__ = [
    "AGENT_BASELINE",
    "AGENT_SYSTEM",
    "AgentScore",
    "ScenarioRecord",
    "Scoreboard",
    "ground_truth",
    "load_records",
    "record_from_decision",
    "save_records",
    "score",
]
