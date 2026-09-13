"""Optional structured interpretation and prose. No safety or action capability.

The provider speaks a chat-completion JSON envelope configured with an explicit
URL and model. It receives no executable tools. Responses are untrusted data:
known edits and load-case IDs are validated in code, and narrative cannot
modify or replace a Decision. Missing credentials, malformed responses, and
network errors all fall back to recorded templates and deterministic parsing.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

import requests

from earl.config import PROJECT_ROOT, demo_mode, load_env
from earl.contracts import ChangeEvent, Decision, DependencyGraph
from earl.scenarios import AREA_RANGE_IN2, LOAD_RANGE_KN, clamp, make_change
from earl.units import IN, to_si

LOG = logging.getLogger(__name__)
TEMPLATES = PROJECT_ROOT / "tests" / "fixtures" / "llm" / "templates.json"
# This is the complete orchestration capability surface. No executable tools
# or application clients are ever passed to a language model.
ORCHESTRATION_CAPABILITIES = frozenset({"interpret_change", "select_load_case", "draft_narrative"})
PROVIDER_TOOLS: tuple = ()
SAFETY_REQUEST_PATTERN = r"\b(safe|unsafe|safety|approv\w*|adequate|compliant|compliance|passes?|fails?|stable|stability|merge|send|email)\b"
NEUTRAL_PROSE_WORDS = frozenset({
    "a", "an", "the", "this", "requested", "request", "engineering", "edit", "change", "changes",
    "member", "section", "cross", "sectional", "area", "load", "point", "magnitude", "node",
    "resize", "resizes", "resized", "remove", "removes", "removed", "add", "adds", "added",
    "move", "moves", "moved", "set", "sets", "from", "to", "at", "of", "in", "is", "was",
    "by", "and", "with", "square", "inches", "millimeters", "meters", "downward",
})


def _neutral_prose(text: str, description: str) -> bool:
    """Closed vocabulary: prose may rearrange edit facts, not add findings."""
    words = set(re.findall(r"[a-z0-9]+", text.lower()))
    source_words = set(re.findall(r"[a-z0-9]+", description.lower()))
    forbidden = r"\b(safe|unsafe|safety|approv\w*|escalat\w*|adequate|compliant|passes?|fails?|merge|send)\b"
    return bool(text.strip()) and len(text) <= 500 and words <= (source_words | NEUTRAL_PROSE_WORDS) and not re.search(forbidden, text, re.I)


@dataclass(frozen=True)
class Interpretation:
    change: ChangeEvent
    provenance: str
    note: str


def _provider(job: str, payload: dict, instruction: str, *, allow_live: bool) -> dict | None:
    load_env()
    if not allow_live or demo_mode():
        return None
    key, url, model = (os.environ.get(k, "") for k in ("META_MUSE_KEY", "META_MUSE_URL", "META_MUSE_MODEL"))
    if not key or not url or not model or not url.startswith("https://"):
        return None
    cache_key = hashlib.sha256(json.dumps([job, model, payload], sort_keys=True).encode()).hexdigest()
    cache = PROJECT_ROOT / "out" / "recordings" / "llm" / f"{cache_key}.json"
    try:
        response = requests.post(url, headers={"Authorization": f"Bearer {key}"},
                                 json={"model": model, "temperature": 0,
                                       "messages": [{"role": "system", "content": instruction},
                                                    {"role": "user", "content": json.dumps(payload)}],
                                       "response_format": {"type": "json_object"},
                                       "max_tokens": 400}, timeout=(2, 4), allow_redirects=False)
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        data = json.loads(content)
        if not isinstance(data, dict):
            raise ValueError("provider response must be an object")
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return data
    except Exception as exc:
        LOG.warning("Optional language provider unavailable (%s); using deterministic fallback", type(exc).__name__)
        if cache.exists():
            try:
                recorded = json.loads(cache.read_text())
                if isinstance(recorded, dict):
                    return recorded
            except (ValueError, OSError):
                pass
        return None


def _rule_spec(description: str, graph: DependencyGraph) -> tuple[str, dict]:
    text = description.lower().replace("\u00b2", "2")
    if re.search(r"\b(upward|upwards|horizontal|leftward|rightward)\b", text):
        raise ValueError("The demo load control supports downward loads only.")
    members = re.findall(r"\bm(?:10|[1-9])\b", text)
    nodes = re.findall(r"\bn[1-6]\b", text)
    if len(set(members)) > 1 or (members and nodes):
        raise ValueError("Submit one member edit or one load edit at a time.")
    if any(word in text for word in ("remove", "delete")) and len(set(members)) == 1:
        return "custom-remove", {"member": members[0]}
    if "move" in text and nodes:
        loads = re.findall(r"\bp[12]\b", text)
        load = loads[0] if loads else "p1"
        node = nodes[-1]
        scenario = next((s for s in json.loads((PROJECT_ROOT / "earl" / "eval" /
                         "scenarios.json").read_text())["scenarios"]
                         if s["kind"] == "move_load" and s["load"] == load and s["node"] == node), None)
        if scenario:
            return scenario["id"], {}
    area = re.search(r"(-?\d+(?:\.\d+)?)\s*(in\^?2|mm\^?2|m\^?2)\b", text)
    if len(set(members)) == 1 and area:
        si = to_si(float(area[1]), area[2])
        return "custom-area", {"member": members[0], "area_in2": si / IN**2}
    percent = re.search(r"(-?\d+(?:\.\d+)?)\s*%", text)
    if len(set(members)) == 1 and percent:
        current = graph.section(graph.member(members[0]).section_id).area / IN**2
        ratio = float(percent[1]) / 100
        if "by" in text:
            ratio = 1 + ratio if any(w in text for w in ("increase", "grow")) else 1 - ratio
        return "custom-area", {"member": members[0], "area_in2": current * ratio}
    force = re.search(r"(-?\d+(?:\.\d+)?)\s*(kn|n|kip|lbf)\b", text)
    if force and nodes:
        kn = abs(to_si(float(force[1]), force[2])) / 1000
        if "add" in text:
            return "add", {"node": nodes[-1], "load_kn": kn}
        return "custom-load", {"node": nodes[-1], "load_kn": kn}
    raise ValueError("Enter one edit, such as 'resize m8 to 8 in2', 'remove m8', or 'add 80 kN at n1'.")


def _from_spec(graph: DependencyGraph, scenario: str, params: dict) -> ChangeEvent:
    if scenario != "add":
        return make_change(graph, scenario, param=params)
    from earl.contracts import ChangeKind, TargetKind
    node = params.get("node")
    if node not in ("n1", "n2", "n3", "n4"):
        raise ValueError("loads must land on a free node")
    force = clamp(params.get("load_kn"), LOAD_RANGE_KN, "load")
    return ChangeEvent("text-add-load", ChangeKind.LOAD_ADDED,
                       f"Add {force:g} kN downward at {node}.", "p_added",
                       target_kind=TargetKind.LOAD, field_name="fy", numeric_before=0,
                       numeric_after=-force * 1000, node_after=node,
                       load_case_id=graph.load_cases[0].id)


def interpret_change(description: str, graph: DependencyGraph, *, allow_live: bool = False) -> Interpretation:
    graph.validate()
    if not isinstance(description, str) or not description.strip() or len(description) > 2000:
        raise ValueError("change text must contain 1 to 2000 characters")
    # Safety questions are never sent to a provider. They are not typed edits.
    if re.search(SAFETY_REQUEST_PATTERN, description, re.I):
        scenario, params = _rule_spec(description, graph)
        change = _from_spec(graph, scenario, params)
        change.apply(graph)
        return Interpretation(change, "rule-based parser", "Only the edit was parsed; the code gate determines the verdict.")
    payload = {"description": description, "member_ids": [m.id for m in graph.members],
               "node_ids": [n.id for n in graph.nodes]}
    answer = _provider("interpret_change", payload,
                       "Translate one geometric/load edit to JSON only: {scenario, params}. "
                       "scenario is custom-area (member, area_in2), custom-remove (member), "
                       "custom-load (node, load_kn) or add (node, load_kn). No other fields.",
                       allow_live=allow_live)
    if answer is not None:
        try:
            if set(answer) != {"scenario", "params"}:
                raise ValueError("unexpected provider fields")
            change = _from_spec(graph, answer["scenario"], answer["params"])
            change.apply(graph)
            return Interpretation(change, "provider or exact recorded response", "Typed edit validated in code.")
        except (ValueError, KeyError, TypeError, AttributeError):
            LOG.warning("Invalid provider edit; using rule parser")
    scenario, params = _rule_spec(description, graph)
    change = _from_spec(graph, scenario, params)
    change.apply(graph)
    return Interpretation(change, "rule-based parser", "No live LLM required; deterministic offline interpretation.")


def select_load_case(graph: DependencyGraph, description: str, *, allow_live: bool = False) -> tuple[str, str]:
    graph.validate()
    if not graph.load_cases:
        raise ValueError("the model has no load case")
    # The typed edit's case is binding for load changes.
    preferred = graph.change.load_case_id
    choices = {case.id for case in graph.load_cases}
    if graph.change.target_kind and graph.change.target_kind.value == "load" and preferred in choices:
        return preferred, "typed load delta"
    delta = graph.change.to_dict()
    context = {key: delta[key] for key in ("target_id", "target_kind", "field_name", "numeric_before", "numeric_after")}
    answer = _provider("select_load_case", {"change": context, "cases": [c.to_dict() for c in graph.load_cases]},
                       "Select one available load case. Return JSON {load_case_id}. No other fields.", allow_live=allow_live)
    if (answer and set(answer) == {"load_case_id"} and
            isinstance(answer["load_case_id"], str) and answer["load_case_id"] in choices):
        return answer["load_case_id"], "provider or recorded selection"
    return preferred if preferred in choices else graph.load_cases[0].id, "deterministic case selection"


def draft_narrative(decision: Decision, *, allow_live: bool = False) -> tuple[str, str]:
    decision.validate()
    # The model may restate only the edit. Verdict/evidence paragraphs are
    # templated from Decision by delivery and cannot be replaced with this text.
    answer = None
    if not re.search(SAFETY_REQUEST_PATTERN, decision.change_description, re.I):
        answer = _provider("draft_narrative", {"change_description": decision.change_description},
                       "Write one neutral sentence describing this requested engineering edit. "
                       "Return JSON {narrative}. Do not make recommendations or evaluate the structure.",
                       allow_live=allow_live)
    if answer and set(answer) == {"narrative"} and isinstance(answer["narrative"], str):
        text = answer["narrative"].strip()
        if _neutral_prose(text, decision.change_description):
            return text, "provider or recorded prose; non-authoritative"
    template = json.loads(TEMPLATES.read_text())["narrative"]
    return template.format(change=decision.change_description), "recorded deterministic template"
