"""Eval harness -- the 20-scenario replay and the baseline agent. [Track A, Sprint 5A]

sprint_timeline.md, Sprint 5 Track A: "Eval harness -- ~20 scenarios from
parametrically perturbing the validated truss; baseline LLM+PyNite-only agent
(no graph traversal, no enforced thresholds) for comparison."

The scoring half is Track B's (`earl.analysis.scoreboard`), already built.
This package produces the `ScenarioRecord`s it folds.
"""

from .scenarios import (
    SCENARIOS,
    Scenario,
    ScenarioGraphs,
    add_load,
    base_area_in2,
    edit_area_variable,
    edit_load_variable,
    members_at,
    move_load,
    remove,
    resize,
    scale,
    scenario,
    scenario_ids,
)

__all__ = [
    "SCENARIOS",
    "Scenario",
    "ScenarioGraphs",
    "add_load",
    "base_area_in2",
    "edit_area_variable",
    "edit_load_variable",
    "members_at",
    "move_load",
    "remove",
    "resize",
    "scale",
    "scenario",
    "scenario_ids",
]
