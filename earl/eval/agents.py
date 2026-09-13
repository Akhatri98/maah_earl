"""What an agent under evaluation looks like, and the system's own entry. [Track A, Sprint 5A]

plan.md's proof point is a comparison, so the harness has to treat the system
and the baseline as two instances of the same thing: something you hand a
change to, which answers "which members are unsafe?".

`Agent` is that interface, and it is deliberately narrow. An agent receives
the scenario and the graphs and returns an `AgentAnswer` -- a list of member
ids and an outcome word. It does NOT return a verdict the harness trusts: the
harness scores the answer against ground truth computed by the validated
solver, so an agent that lies, guesses or crashes is measured, not believed.

Two agents live in this package:

  * `SystemAgent` (here) -- the real pipeline: fixed BFS walk, PyNite, Biject,
    the code-enforced threshold. It answers with a contract `Decision`.
  * `BaselineAgent` (`baseline.py`) -- a tool-using LLM with the same PyNite
    access and the same structural data, but no graph traversal and no
    enforced threshold. It answers with whatever it says.

The asymmetry is the experiment; everything else is held constant, including
the scenario graphs, which are rebuilt per agent so nothing one agent computes
can reach another (see `harness.run_scenario`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..analysis.scoreboard import AGENT_SYSTEM, ScenarioRecord, record_from_decision
from ..contracts import Decision, MemberStatus, Outcome
from ..pipeline import PipelineResult, run_pipeline
from .scenarios import Scenario, ScenarioGraphs


@dataclass
class AgentAnswer:
    """One agent's answer to one scenario.

    `unsafe_member_ids` is what the agent claims is below the safety
    threshold. `outcome` is the word it puts on the change as a whole. Both
    are taken at face value and scored; neither is corrected.
    """

    unsafe_member_ids: list[str] = field(default_factory=list)
    outcome: Outcome = Outcome.APPROVED
    note: str | None = None
    # Set when the agent's answer is a full contract Decision (the system).
    decision: Decision | None = None
    # Whatever the agent wants kept for the transcript / demo.
    detail: dict = field(default_factory=dict)

    def record(self, scenario_id: str, agent: str, truth: list[str]) -> ScenarioRecord:
        """Score-ready record. A Decision-backed answer goes through Track B's
        `record_from_decision` so the system is scored by exactly the rule the
        scoreboard documents, not by a second implementation here."""
        if self.decision is not None:
            rec = record_from_decision(scenario_id, agent, truth, self.decision)
            if self.note and not rec.note:
                rec.note = self.note
            return rec
        return ScenarioRecord(
            scenario_id=scenario_id,
            agent=agent,
            truth_unsafe_member_ids=sorted(set(truth)),
            reported_unsafe_member_ids=sorted(set(self.unsafe_member_ids)),
            truth_outcome=(Outcome.ESCALATED if truth else Outcome.APPROVED).value,
            reported_outcome=self.outcome.value,
            note=self.note,
        )


class Agent(Protocol):
    """Anything the harness can put a scenario to."""

    name: str

    def answer(self, scenario: Scenario, graphs: ScenarioGraphs) -> AgentAnswer: ...


# --------------------------------------------------------------------------
# The system under test
# --------------------------------------------------------------------------

@dataclass
class SystemAgent:
    """EARL itself: `run_pipeline` on the scenario's after-graph.

    Everything the plan claims is structural runs here unchanged -- the fixed
    walk populates `affected_*`, PyNite solves, Biject applies the threshold
    in code, and `Decision.validate()` refuses an approval that contradicts a
    computed number. The harness adds nothing and suppresses nothing.

    SkyCiv and Gmail are off: Stage 3 is metered and Stage 4 would put twenty
    e-mails in someone's inbox, and neither can change a verdict that Stage 2
    already computed. The ECN is still built for every scenario, so the run
    leaves the paper trail plan.md asks for.
    """

    name: str = AGENT_SYSTEM
    threshold: float | None = None
    render_svg: bool = True
    # Kept so the harness can write the ECN / SVG per scenario.
    last_result: PipelineResult | None = None

    def answer(self, scenario: Scenario, graphs: ScenarioGraphs) -> AgentAnswer:
        result = run_pipeline(
            graphs.after,
            before=graphs.before,
            threshold=self.threshold,
            skyciv=None,
            sender=None,
            render_svg=self.render_svg,
            ecn_id=f"ECN-{scenario.id.upper()}",
            decision_id=f"dec-{scenario.id}",
        )
        self.last_result = result
        decision = result.decision
        return AgentAnswer(
            unsafe_member_ids=sorted(
                set(decision.violating_member_ids)
                | {
                    r.member_id
                    for r in decision.member_results
                    if r.status is MemberStatus.FAIL
                }
            ),
            outcome=decision.outcome,
            decision=decision,
            detail={
                "walked_members": list(graphs.after.affected_member_ids),
                "notes": list(result.notes),
            },
        )


__all__ = ["Agent", "AgentAnswer", "SystemAgent"]
