"""The tools the baseline agent gets -- and, precisely, the ones it does not. [Track A, Sprint 5A]

plan.md, Baseline comparison: "The identical 20 scenarios are run through a
tool-using LLM agent with the same Onshape and PyNite access but none of the
fixed graph traversal, code-enforced thresholds, or branch-only write
permission."

That sentence is the specification for this module, and getting it wrong in
either direction ruins the experiment: starve the baseline and the comparison
is rigged, hand it Biject and there is nothing left to compare. So the split
is written down explicitly.

## What the baseline HAS (identical to the system's own inputs)

  * `get_change`      -- the ChangeEvent, exactly as the pipeline receives it.
  * `get_structure`   -- every node, coordinate, support and load case.
  * `list_members`    -- every member, its end nodes and its area.
  * `get_material`    -- E, density, yield AND the 25 ksi design allowable.
    Withholding the allowable would leave it unable to judge safety at all,
    which would make the comparison meaningless rather than fair.
  * `solve_load_case` -- THE SAME PyNite SOLVER the system uses, on either the
    before or the after state, returning every member's axial force and
    stress. Not a summary, not a subset: the full result, every member, every
    time it asks.

It may call `solve_load_case` as often as it likes, on any load case, in
either state.

## What the baseline LACKS (the three things the plan removes)

  * NO FIXED GRAPH TRAVERSAL. There is no `walk` tool and the graphs it is
    shown have an empty `affected_member_ids`. Which members to reason about
    is its decision, every time -- where the system's BFS crosses every edge
    by construction.
  * NO CODE-ENFORCED THRESHOLD. There is no Biject tool, no safety-factor
    field and no `Decision.validate()`. It is told the threshold in the
    prompt and must do the division itself; nothing stops it approving a
    member it has just computed to be over its allowable.
  * NO BRANCH-ONLY WRITE PERMISSION. It has no write tool at all, so the
    question never arises -- the harness scores what it SAYS. (The system's
    merge hook is likewise off during the eval; neither agent touches Onshape.)

## Why the tools are pure functions

Every tool here reads a graph and returns JSON. None of them mutates
anything, so the baseline cannot disturb the state the system is measured on,
and the whole layer is testable offline without a model
(`tests/test_eval_tools.py`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ..analysis.solver import SolverError, member_length, solve
from ..contracts import DependencyGraph
from .scenarios import ScenarioGraphs

KSI = 6.894757e6
IN2 = 0.0254 ** 2

# The word the agent must use to end its turn. Kept here so the prompt, the
# tool schema and the parser cannot drift apart.
REPORT_TOOL = "report_verdict"


# --------------------------------------------------------------------------
# Tool schemas -- provider-neutral
# --------------------------------------------------------------------------

def tool_specs() -> list[dict[str, Any]]:
    """The tool list, as plain dicts. A provider adapter maps these onto its
    own schema; nothing here is Anthropic- or OpenAI-shaped."""
    return [
        {
            "name": "get_change",
            "description": (
                "The engineering change under review: what kind of edit it is, "
                "which entity it names, and its before/after values."
            ),
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "get_structure",
            "description": (
                "The truss: every node with its coordinates (metres) and support "
                "condition, and every load case with its point loads (newtons). "
                "Returns the state AFTER the change unless state='before'."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "state": {
                        "type": "string",
                        "enum": ["before", "after"],
                        "description": "Which state to describe. Default 'after'.",
                    }
                },
                "required": [],
            },
        },
        {
            "name": "list_members",
            "description": (
                "Every member in the truss: its id, the two nodes it spans, its "
                "length (m) and its cross-sectional area (m^2 and in^2). "
                "Returns the state AFTER the change unless state='before'."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "state": {"type": "string", "enum": ["before", "after"]}
                },
                "required": [],
            },
        },
        {
            "name": "get_material",
            "description": (
                "Material properties in SI: elastic modulus, density, yield "
                "strength, and the design allowable stress that members are "
                "checked against."
            ),
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "solve_load_case",
            "description": (
                "Run the PyNite finite-element solver on a load case and return "
                "every member's axial force (N, tension positive) and axial "
                "stress (Pa, tension positive), plus node displacements (m). "
                "Call it on the 'after' state to see the effect of the change, "
                "and on 'before' to compare. You may call this as many times as "
                "you need."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "load_case_id": {
                        "type": "string",
                        "description": "Load case id, from get_structure.",
                    },
                    "state": {"type": "string", "enum": ["before", "after"]},
                },
                "required": ["load_case_id"],
            },
        },
        {
            "name": REPORT_TOOL,
            "description": (
                "Report your final answer and end the review. List every member "
                "you judge unsafe after the change."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "unsafe_member_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Member ids that are unsafe after the change. Empty "
                            "if none are."
                        ),
                    },
                    "outcome": {
                        "type": "string",
                        "enum": ["approved", "escalated"],
                        "description": (
                            "'escalated' if the change needs a human decision, "
                            "'approved' if it may merge."
                        ),
                    },
                    "rationale": {
                        "type": "string",
                        "description": "Brief reasoning, for the transcript.",
                    },
                },
                "required": ["unsafe_member_ids", "outcome"],
            },
        },
    ]


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------

class ToolError(RuntimeError):
    """A tool call the agent got wrong. Returned to the model as an error
    result rather than raised, so a bad call costs it a turn instead of
    crashing the scenario -- but it is recorded either way."""


@dataclass
class ToolBox:
    """The tools bound to one scenario's graphs.

    `calls` counts every invocation, so the transcript can show how much of
    the structure the agent actually looked at -- the interesting number when
    it drops a domino.
    """

    graphs: ScenarioGraphs

    def __post_init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    # -- helpers ------------------------------------------------------------

    def _graph(self, state: str | None) -> DependencyGraph:
        chosen = (state or "after").lower()
        if chosen == "after":
            return self.graphs.after
        if chosen == "before":
            return self.graphs.before
        raise ToolError(f"state must be 'before' or 'after', got {state!r}")

    # -- the tools ----------------------------------------------------------

    def get_change(self) -> dict[str, Any]:
        change = self.graphs.after.change
        return {
            "kind": change.kind.value,
            "description": change.description,
            "target_id": change.target_id,
            "value_before": change.value_before,
            "value_after": change.value_after,
        }

    def get_structure(self, state: str | None = None) -> dict[str, Any]:
        graph = self._graph(state)
        return {
            "state": (state or "after"),
            "nodes": [
                {
                    "id": n.id,
                    "x": n.x,
                    "y": n.y,
                    "z": n.z,
                    "support": n.support.value,
                }
                for n in graph.nodes
            ],
            "load_cases": [
                {
                    "id": lc.id,
                    "name": lc.name,
                    "include_self_weight": lc.include_self_weight,
                    "point_loads": [
                        {
                            "id": p.id,
                            "node_id": p.node_id,
                            "fx": p.fx,
                            "fy": p.fy,
                            "fz": p.fz,
                        }
                        for p in lc.point_loads
                    ],
                }
                for lc in graph.load_cases
            ],
            "units": {"length": "m", "force": "N", "stress": "Pa", "area": "m^2"},
        }

    def list_members(self, state: str | None = None) -> dict[str, Any]:
        graph = self._graph(state)
        members = []
        for m in graph.members:
            area = graph.section(m.section_id).area
            members.append(
                {
                    "id": m.id,
                    "start_node": m.start_node,
                    "end_node": m.end_node,
                    "length_m": member_length(graph, m),
                    "area_m2": area,
                    "area_in2": area / IN2,
                    "material_id": m.material_id,
                }
            )
        return {"state": (state or "after"), "members": members}

    def get_material(self) -> dict[str, Any]:
        materials = []
        for mat in self.graphs.after.materials:
            materials.append(
                {
                    "id": mat.id,
                    "name": mat.name,
                    "elastic_modulus_pa": mat.elastic_modulus,
                    "yield_strength_pa": mat.yield_strength,
                    "allowable_stress_pa": mat.allowable_stress,
                    "allowable_stress_ksi": (
                        None
                        if mat.allowable_stress is None
                        else mat.allowable_stress / KSI
                    ),
                    "density_kg_m3": mat.density,
                }
            )
        return {"materials": materials}

    def solve_load_case(
        self, load_case_id: str, state: str | None = None
    ) -> dict[str, Any]:
        graph = self._graph(state)
        try:
            result = solve(graph, load_case_id)
        except (SolverError, ValueError) as e:
            raise ToolError(f"solve failed: {type(e).__name__}: {e}") from e
        # Member order follows the graph, not sorted ids, so the agent sees
        # m2 after m1 rather than m10 after m1.
        return {
            "state": (state or "after"),
            "load_case_id": load_case_id,
            "members": [
                {
                    "id": m.id,
                    "axial_force_n": result.force(m.id).axial_force,
                    "stress_pa": result.force(m.id).stress,
                    "stress_ksi": result.force(m.id).stress / KSI,
                }
                for m in graph.members
            ],
            "displacements": {
                n.id: {
                    "dx": result.displacements[n.id].dx,
                    "dy": result.displacements[n.id].dy,
                    "dz": result.displacements[n.id].dz,
                }
                for n in graph.nodes
                if n.id in result.displacements
            },
        }

    # -- dispatch -----------------------------------------------------------

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run one tool call. Unknown names and bad arguments become ToolError,
        which the agent loop turns into an error result for the model."""
        arguments = dict(arguments or {})
        self.calls.append({"name": name, "arguments": arguments})

        if name == "get_change":
            return self.get_change()
        if name == "get_structure":
            return self.get_structure(arguments.get("state"))
        if name == "list_members":
            return self.list_members(arguments.get("state"))
        if name == "get_material":
            return self.get_material()
        if name == "solve_load_case":
            load_case_id = arguments.get("load_case_id")
            if not isinstance(load_case_id, str) or not load_case_id:
                raise ToolError("solve_load_case needs a load_case_id string")
            return self.solve_load_case(load_case_id, arguments.get("state"))
        raise ToolError(
            f"unknown tool {name!r}; available: "
            + ", ".join(spec["name"] for spec in tool_specs())
        )

    def call_json(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        """`call`, serialised, with errors rendered as JSON rather than raised
        -- the shape a tool_result block wants."""
        try:
            return json.dumps(self.call(name, arguments))
        except ToolError as e:
            return json.dumps({"error": str(e)})


__all__ = ["REPORT_TOOL", "ToolBox", "ToolError", "tool_specs"]
