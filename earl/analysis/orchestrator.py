"""Analysis planner -- the LLM proposes, the code disposes. [Track B, Sprint 2B]

plan.md draws a hard line: the model may help decide WHAT to look at, never
WHETHER it is safe. This module is where that line is enforced in code.

  * `AnalysisPlan` is advisory. It names a load case, whether to add
    self-weight and which members deserve emphasis. It deliberately has NO
    threshold, capacity, status or outcome field -- there is nothing an LLM
    can put in a plan that changes a verdict, because the verdict fields do
    not exist on the object it gets to fill in.
  * `RuleBasedPlanner` is the deterministic reference. It is what runs when
    there is no LLM, and what the LLM plan is checked against.
  * `LLMPlanner` asks a model for a plan, then VALIDATES IN CODE: the load
    case must exist, unknown member ids are dropped, the self-weight flag
    must be a real boolean. Anything else (transport failure, junk output,
    bad JSON, unknown load case) falls back to the rule plan with the reason
    written into the rationale, so a rejected plan is visible, never silent.
  * The prompt shows the model only the graph's structural OUTLINE (change,
    load case ids, member ids, affected ids) -- never stresses, capacities or
    thresholds -- so there is nothing numeric for it to reason its way
    around.

The gate (gate.py) treats `include_self_weight` as additive only (the
contract flag always wins) and `focus_member_ids` as ordering/visual
emphasis only: Biject evaluates every member regardless of the plan.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

import requests

from ..config import load_env, require
from ..contracts import ChangeKind, DependencyGraph

DEFAULT_LLM_BASE_URL = "https://api.meta.ai/v1"
DEFAULT_LLM_MODEL = "muse-spark-1.1"
LLM_TIMEOUT_SECONDS = 30

PLAN_SOURCE_LLM = "llm"
PLAN_SOURCE_RULES = "rules"


# --------------------------------------------------------------------------
# The plan
# --------------------------------------------------------------------------

@dataclass
class AnalysisPlan:
    """What to analyse. Advisory only -- see the module docstring for why
    there is no threshold / capacity / status / outcome here."""

    load_case_id: str
    include_self_weight: bool
    focus_member_ids: list[str] = field(default_factory=list)
    rationale: str = ""
    source: str = PLAN_SOURCE_RULES     # "llm" | "rules"


class Planner(Protocol):
    def plan(self, graph: DependencyGraph) -> AnalysisPlan: ...


# --------------------------------------------------------------------------
# Deterministic reference planner
# --------------------------------------------------------------------------

_LOAD_CHANGES = {ChangeKind.LOAD_ADDED, ChangeKind.LOAD_MOVED}


def _load_case_for_change(graph: DependencyGraph):
    """The load case that contains the changed load, when the change is a
    load change; otherwise None."""
    if graph.change.kind not in _LOAD_CHANGES:
        return None
    target = graph.change.target_id
    for lc in graph.load_cases:
        if any(pl.id == target for pl in lc.point_loads):
            return lc
    return None


def _member_ids(graph: DependencyGraph) -> list[str]:
    return [m.id for m in graph.members]


class RuleBasedPlanner:
    """Deterministic plan: the load case that carries the changed load (for
    LOAD_ADDED / LOAD_MOVED) else the first load case; self-weight as that
    case declares it; focus = affected members plus the change target when
    it is a member."""

    def plan(self, graph: DependencyGraph) -> AnalysisPlan:
        if not graph.load_cases:
            raise ValueError(
                f"graph {graph.id!r} has no load cases; nothing to analyse"
            )

        chosen = _load_case_for_change(graph)
        if chosen is not None:
            why = (
                f"load case {chosen.id!r} contains the changed load "
                f"{graph.change.target_id!r}"
            )
        else:
            chosen = graph.load_cases[0]
            why = f"first load case {chosen.id!r}"

        member_ids = _member_ids(graph)
        focus: list[str] = []
        for mid in graph.affected_member_ids:
            if mid in member_ids and mid not in focus:
                focus.append(mid)
        target = graph.change.target_id
        if target in member_ids and target not in focus:
            focus.append(target)

        return AnalysisPlan(
            load_case_id=chosen.id,
            include_self_weight=bool(chosen.include_self_weight),
            focus_member_ids=focus,
            rationale=f"rule-based: {why}; focus on {len(focus)} affected member(s)",
            source=PLAN_SOURCE_RULES,
        )


# --------------------------------------------------------------------------
# LLM transport
# --------------------------------------------------------------------------

class LLMError(RuntimeError):
    """The model could not be reached or answered with something unusable."""


class LLMClient(Protocol):
    def complete(self, system: str, user: str) -> str: ...


@dataclass
class MetaModelClient:
    """OpenAI-compatible chat-completions client (Meta Muse by default).

    `post` is injectable so tests exercise the exact URL / headers / body
    without touching the network. It must accept the same keyword arguments
    as `requests.post` and return an object with `.status_code`, `.text` and
    `.json()`.
    """

    api_key: str
    base_url: str = DEFAULT_LLM_BASE_URL
    model: str = DEFAULT_LLM_MODEL
    timeout: int = LLM_TIMEOUT_SECONDS
    post: Callable[..., Any] = requests.post

    @classmethod
    def from_env(cls) -> MetaModelClient:
        load_env()
        return cls(
            api_key=require("META_MUSE_KEY"),
            base_url=os.environ.get("LLM_BASE_URL", DEFAULT_LLM_BASE_URL).rstrip("/"),
            model=os.environ.get("LLM_MODEL", DEFAULT_LLM_MODEL),
        )

    @property
    def url(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"

    def complete(self, system: str, user: str) -> str:
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        try:
            resp = self.post(self.url, json=body, headers=headers, timeout=self.timeout)
        except Exception as e:  # requests.RequestException and friends
            raise LLMError(f"LLM request failed: {type(e).__name__}: {e}") from e

        status = getattr(resp, "status_code", None)
        if status != 200:
            text = str(getattr(resp, "text", ""))[:300]
            raise LLMError(f"LLM HTTP {status}: {text}")

        try:
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
        except Exception as e:
            raise LLMError(f"LLM response not in chat-completions shape: {e}") from e
        if not isinstance(content, str):
            raise LLMError("LLM response content is not a string")
        return content


# --------------------------------------------------------------------------
# LLM planner with code-side validation
# --------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You are the analysis planner for a structural change-review pipeline. "
    "You choose WHICH load case a truss should be checked under and WHICH "
    "members deserve attention. You do not judge safety; the solver and a "
    "fixed threshold rule do that. Reply with strict JSON only, no prose, "
    "no code fences, with exactly these keys: "
    '{"load_case_id": string, "include_self_weight": boolean, '
    '"focus_member_ids": [string, ...], "rationale": string}. '
    "load_case_id must be one of the listed load case ids; focus_member_ids "
    "must be a subset of the listed member ids."
)

_FENCE_RE = re.compile(r"^\s*```[a-zA-Z0-9_-]*\s*\n?(.*?)\n?\s*```\s*$", re.DOTALL)


def build_prompt(graph: DependencyGraph) -> str:
    """The user message. Shows ONLY the structural outline: the change, the
    load cases (id, name, load count, self-weight flag), the affected member
    ids and the member ids. No coordinates, forces, stresses, capacities or
    thresholds -- the model has nothing numeric to steer."""
    change = graph.change
    outline = {
        "change": {
            "kind": change.kind.value,
            "description": change.description,
            "target_id": change.target_id,
            "value_before": change.value_before,
            "value_after": change.value_after,
        },
        "load_cases": [
            {
                "id": lc.id,
                "name": lc.name,
                "point_loads": len(lc.point_loads),
                "include_self_weight": bool(lc.include_self_weight),
            }
            for lc in graph.load_cases
        ],
        "affected_member_ids": list(graph.affected_member_ids),
        "member_ids": _member_ids(graph),
    }
    return (
        "Structure and change (JSON):\n"
        + json.dumps(outline, indent=2)
        + "\n\nReturn the analysis plan as strict JSON."
    )


def _strip_fences(text: str) -> str:
    m = _FENCE_RE.match(text)
    return m.group(1) if m else text


def parse_plan_json(text: str) -> dict[str, Any]:
    """Strict JSON parse tolerant of ``` fences and leading/trailing prose
    around a single top-level object. Raises ValueError on anything else."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("empty response")
    candidate = _strip_fences(text).strip()
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        # Last resort: the outermost {...} in the text.
        start, end = candidate.find("{"), candidate.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("response is not JSON")
        try:
            data = json.loads(candidate[start:end + 1])
        except json.JSONDecodeError as e:
            raise ValueError(f"response is not valid JSON: {e.msg}") from e
    if not isinstance(data, dict):
        raise ValueError("response JSON is not an object")
    return data


def validate_plan_dict(graph: DependencyGraph, data: dict[str, Any]) -> AnalysisPlan:
    """Turn the model's dict into an AnalysisPlan, or raise ValueError with
    the reason. This is the code-side check that makes the LLM advisory."""
    lc_ids = [lc.id for lc in graph.load_cases]
    load_case_id = data.get("load_case_id")
    if not isinstance(load_case_id, str) or load_case_id not in lc_ids:
        raise ValueError(
            f"unknown load case {load_case_id!r}; known: {lc_ids}"
        )

    sw = data.get("include_self_weight", False)
    if not isinstance(sw, bool):
        raise ValueError(f"include_self_weight must be a boolean, got {sw!r}")

    raw_focus = data.get("focus_member_ids", [])
    if raw_focus is None:
        raw_focus = []
    if not isinstance(raw_focus, list):
        raise ValueError("focus_member_ids must be a list of member ids")
    member_ids = _member_ids(graph)
    focus: list[str] = []
    dropped: list[str] = []
    for mid in raw_focus:
        if isinstance(mid, str) and mid in member_ids:
            if mid not in focus:
                focus.append(mid)
        else:
            dropped.append(str(mid))

    rationale = data.get("rationale", "")
    if not isinstance(rationale, str):
        rationale = str(rationale)
    if dropped:
        rationale = (
            f"{rationale} [dropped unknown focus ids: {', '.join(dropped)}]"
        ).strip()

    return AnalysisPlan(
        load_case_id=load_case_id,
        include_self_weight=sw,
        focus_member_ids=focus,
        rationale=rationale,
        source=PLAN_SOURCE_LLM,
    )


@dataclass
class LLMPlanner:
    """Ask a model for a plan; accept it only if it survives validation,
    otherwise return the fallback plan and say why."""

    client: LLMClient
    fallback: Planner = field(default_factory=RuleBasedPlanner)

    def plan(self, graph: DependencyGraph) -> AnalysisPlan:
        # Compute the fallback first: if the graph is unplannable (no load
        # cases) that is a ValueError the gate must see, not an LLM matter.
        fallback_plan = self.fallback.plan(graph)

        try:
            raw = self.client.complete(SYSTEM_PROMPT, build_prompt(graph))
        except LLMError as e:
            return self._rejected(fallback_plan, f"transport: {e}")
        except Exception as e:  # a misbehaving client must not crash the gate
            return self._rejected(fallback_plan, f"{type(e).__name__}: {e}")

        try:
            data = parse_plan_json(raw)
        except ValueError as e:
            return self._rejected(fallback_plan, f"bad JSON: {e}")

        try:
            return validate_plan_dict(graph, data)
        except ValueError as e:
            return self._rejected(fallback_plan, f"invalid plan: {e}")

    @staticmethod
    def _rejected(fallback_plan: AnalysisPlan, reason: str) -> AnalysisPlan:
        reason = " ".join(str(reason).split())   # one line, no stray newlines
        return AnalysisPlan(
            load_case_id=fallback_plan.load_case_id,
            include_self_weight=fallback_plan.include_self_weight,
            focus_member_ids=list(fallback_plan.focus_member_ids),
            rationale=f"LLM plan rejected ({reason}); {fallback_plan.rationale}",
            source=fallback_plan.source,
        )


def default_planner() -> Planner:
    """LLMPlanner over Meta Muse when META_MUSE_KEY is configured, else the
    deterministic planner. Either way the verdict path is identical."""
    load_env()
    if os.environ.get("META_MUSE_KEY", ""):
        return LLMPlanner(MetaModelClient.from_env())
    return RuleBasedPlanner()


__all__ = [
    "AnalysisPlan",
    "Planner",
    "RuleBasedPlanner",
    "LLMClient",
    "LLMError",
    "MetaModelClient",
    "LLMPlanner",
    "SYSTEM_PROMPT",
    "build_prompt",
    "parse_plan_json",
    "validate_plan_dict",
    "default_planner",
    "DEFAULT_LLM_BASE_URL",
    "DEFAULT_LLM_MODEL",
]
