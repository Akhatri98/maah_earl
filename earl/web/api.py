"""What the web app is allowed to do. [Track A, Sprint 7]

This module is the whole domain layer of the demo site. The HTTP layer
(`earl.web.app`) parses requests and writes bytes; every decision about what
a request *means* is made here, because this is the code that has to be safe
to point a public URL at.

## The rules it is built to

1. **A request can only compose a change, never a program.** Every parameter
   is checked against the base design's own vocabulary -- member ids, node
   ids, load ids -- and every number is clamped to a stated range. There is
   no path from a request body to a file name, a module name or a shell.
2. **The same perturbation code as the eval.** A change composed in the
   browser is built by `earl.eval.scenarios`, the module that builds the
   twenty eval scenarios. A judge is therefore driving the tested thing, not
   a demo-only imitation of it.
3. **Nothing here writes to disk, calls a model, or reaches the network.**
   `run_pipeline` is called with no SkyCiv client and no sender, so a run is
   a solve and nothing else. The eval numbers come from the recordings
   committed in `demo/recordings`, read once.
4. **The threshold floor is not a parameter.** A request may raise the bar
   (`threshold >= 1.0`); a request that tries to lower it reaches
   `gate.resolve_threshold` and is refused there, in the same code path a
   misconfigured `.env` hits. The site reports the refusal rather than
   hiding it -- see `REFUSED` below.
"""

from __future__ import annotations

import math
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any

from ..analysis.benchmark import (
    CONNECTIVITY,
    DEMO_AREA_AFTER_IN2,
    DEMO_MEMBER,
    KSI,
    LOADED_NODES,
    MEMBER_IDS,
    NODE_COORDS_IN,
    STRESS_LIMIT_KSI,
    SUPPORTED_NODES,
    YIELD_KSI,
)
from ..analysis.scoreboard import ground_truth, load_records, score
from ..contracts import Decision, Outcome
from ..eval import scenarios as sc
from ..pipeline import PipelineResult, run_pipeline

ROOT = Path(__file__).resolve().parent.parent.parent
RECORDINGS = ROOT / "demo" / "recordings"

# --------------------------------------------------------------------------
# Limits. Every one of these is a range a structural engineer would accept as
# a plausible edit; the point is that the set of reachable inputs is small,
# enumerable and finite, not that these particular bounds are sacred.
# --------------------------------------------------------------------------

AREA_MIN_IN2 = 0.05
AREA_MAX_IN2 = 60.0
LOAD_MAX_KIPS = 500.0
FACTOR_MIN = 0.05
FACTOR_MAX = 5.0
THRESHOLD_MAX = 5.0
MAX_STORED_RUNS = 250

CHANGE_KINDS = (
    "scenario",
    "resize",
    "remove",
    "add_load",
    "move_load",
    "variable_load",
    "variable_area",
)


class BadRequest(ValueError):
    """The request did not describe a change to this structure."""


@dataclass(frozen=True)
class Refused:
    """A run the code declined to perform, with the reason it gave.

    This is not an error path in the usual sense. `gate.resolve_threshold`
    refusing a threshold below 1.0 is the guarantee working, and the site
    renders it as such.
    """

    reason: str
    detail: str


# --------------------------------------------------------------------------
# The base design, as the browser needs to see it
# --------------------------------------------------------------------------

def _length_in(node_a: str, node_b: str) -> float:
    (ax, ay), (bx, by) = NODE_COORDS_IN[node_a], NODE_COORDS_IN[node_b]
    return math.hypot(bx - ax, by - ay)


def base_model() -> dict[str, Any]:
    """Everything the change bench needs to build its controls: the members
    and their design areas, the nodes and their coordinates, the loads that
    exist, and the limits every input is clamped to."""
    base_loads = {nid: kips for nid, kips in zip(LOADED_NODES, sc.BASE_LOADS_KIPS)}
    return {
        "members": [
            {
                "id": mid,
                "node_a": a,
                "node_b": b,
                "base_area_in2": sc.base_area_in2(mid),
                "length_in": round(_length_in(a, b), 2),
            }
            for mid, a, b in CONNECTIVITY
        ],
        "nodes": [
            {
                "id": nid,
                "x_in": x,
                "y_in": y,
                "support": "pin" if nid in SUPPORTED_NODES else "free",
                "load_kips": base_loads.get(nid, 0.0),
            }
            for nid, (x, y) in NODE_COORDS_IN.items()
        ],
        "loads": [
            {"id": f"p_{nid}", "node_id": nid, "down_kips": kips}
            for nid, kips in base_loads.items()
        ],
        "material": {
            "name": "Aluminium 2024-T3",
            "yield_ksi": YIELD_KSI,
            "allowable_ksi": STRESS_LIMIT_KSI,
            "note": (
                "capacity comes from the 25 ksi design allowable, not the "
                "50 ksi yield -- safety factor = allowable / stress"
            ),
        },
        "limits": {
            "area_min_in2": AREA_MIN_IN2,
            "area_max_in2": AREA_MAX_IN2,
            "load_max_kips": LOAD_MAX_KIPS,
            "factor_min": FACTOR_MIN,
            "factor_max": FACTOR_MAX,
            "threshold_max": THRESHOLD_MAX,
        },
        "demo": {"member": DEMO_MEMBER, "area_after_in2": DEMO_AREA_AFTER_IN2},
        "scenarios": [
            {
                "id": s.id,
                "kind": s.kind.value,
                "description": s.description,
                "target_id": s.target_id,
            }
            for s in sc.SCENARIOS
        ],
    }


# --------------------------------------------------------------------------
# Request -> Scenario. Nothing below trusts a single incoming value.
# --------------------------------------------------------------------------

def _number(payload: dict[str, Any], key: str, low: float, high: float) -> float:
    if key not in payload:
        raise BadRequest(f"{key} is required")
    try:
        value = float(payload[key])
    except (TypeError, ValueError):
        raise BadRequest(f"{key} must be a number, got {payload[key]!r}") from None
    if not math.isfinite(value):
        raise BadRequest(f"{key} must be finite, got {payload[key]!r}")
    if not low <= value <= high:
        raise BadRequest(f"{key} must be between {low:g} and {high:g}, got {value:g}")
    return value


def _member_id(payload: dict[str, Any], key: str = "member_id") -> str:
    value = payload.get(key)
    if value not in MEMBER_IDS:
        raise BadRequest(f"{key} must be one of {', '.join(MEMBER_IDS)}")
    return str(value)


def _node_id(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if value not in NODE_COORDS_IN:
        raise BadRequest(f"{key} must be one of {', '.join(NODE_COORDS_IN)}")
    return str(value)


def build_scenario(payload: dict[str, Any]) -> sc.Scenario:
    """A validated request becomes one of the eval set's own perturbations.

    The scenario id is a fixed prefix plus the parameters, never anything the
    caller supplies: it ends up in a branch name and a graph id, so it stays
    inside an alphabet we choose.
    """
    kind = payload.get("kind")
    if kind not in CHANGE_KINDS:
        raise BadRequest(f"kind must be one of {', '.join(CHANGE_KINDS)}")

    if kind == "scenario":
        # One of the twenty, by id -- a lookup in a frozen tuple, so the id
        # is checked by membership rather than parsed.
        wanted = payload.get("scenario_id")
        for candidate in sc.SCENARIOS:
            if candidate.id == wanted:
                return candidate
        raise BadRequest(f"no scenario with id {wanted!r}")

    if kind == "resize":
        member = _member_id(payload)
        area = _number(payload, "area_in2", AREA_MIN_IN2, AREA_MAX_IN2)
        return sc.resize(f"web-resize-{member}", member, round(area, 4))

    if kind == "remove":
        member = _member_id(payload)
        return sc.remove(f"web-remove-{member}", member)

    if kind == "add_load":
        node = _node_id(payload, "node_id")
        down = _number(payload, "down_kips", -LOAD_MAX_KIPS, LOAD_MAX_KIPS)
        east = _number(payload, "east_kips", -LOAD_MAX_KIPS, LOAD_MAX_KIPS)
        if down == 0.0 and east == 0.0:
            raise BadRequest("a load of zero in both directions is not a change")
        return sc.add_load(f"web-load-{node}", node, down_kips=down, east_kips=east)

    if kind == "move_load":
        from_node = _node_id(payload, "from_node")
        to_node = _node_id(payload, "to_node")
        if from_node not in LOADED_NODES:
            raise BadRequest(
                f"there is no load at {from_node}; loads start at "
                f"{' and '.join(LOADED_NODES)}"
            )
        if from_node == to_node:
            raise BadRequest("moving a load to the node it is already on is not a change")
        return sc.move_load(
            f"web-move-{from_node}-{to_node}", f"p_{from_node}", from_node, to_node
        )

    if kind == "variable_load":
        after = _number(payload, "after_kips", 0.0, LOAD_MAX_KIPS)
        before = sc.BASE_LOADS_KIPS[0]
        if after == before:
            raise BadRequest(f"the design load is already {before:g} kip")
        return sc.edit_load_variable(f"web-designload-{after:g}", before, after)

    factor = _number(payload, "factor", FACTOR_MIN, FACTOR_MAX)
    if factor == 1.0:
        raise BadRequest("scaling every bar by 1x is not a change")
    return sc.edit_area_variable(f"web-bararea-{factor:g}", factor)


def _threshold(payload: dict[str, Any]) -> float:
    """The requested threshold, range-checked but NOT floor-checked.

    The floor deliberately is not enforced here. A value below 1.0 is passed
    straight to the gate so it is refused by `gate.resolve_threshold` -- the
    one place that rule lives -- and the site can show a judge the real
    refusal rather than a form validation message we wrote for the occasion.
    """
    if "threshold" not in payload or payload["threshold"] in (None, ""):
        return 1.0
    return _number(payload, "threshold", 0.0, THRESHOLD_MAX)


# --------------------------------------------------------------------------
# Shaping a run for the browser
# --------------------------------------------------------------------------

def _ksi(pascals: float | None) -> float | None:
    return None if pascals is None else round(pascals / KSI, 3)


def _member_rows(result: PipelineResult) -> list[dict[str, Any]]:
    rows = []
    for r in result.decision.member_results:
        rows.append(
            {
                "member_id": r.member_id,
                "status": r.status.value,
                "stress_before_ksi": _ksi(r.stress_before),
                "stress_after_ksi": _ksi(r.stress_after),
                "capacity_ksi": _ksi(r.capacity),
                "safety_factor": None if r.safety_factor is None else round(r.safety_factor, 3),
                "utilization": None if r.utilization is None else round(r.utilization, 3),
                "is_affected": r.is_affected,
                "hops_from_change": r.hops_from_change,
                "reached_via": None if r.reached_via is None else r.reached_via.value,
                "note": r.note,
            }
        )
    rows.sort(key=lambda row: (row["safety_factor"] is None, row["safety_factor"] or 0.0))
    return rows


def _walk_rings(result: PipelineResult) -> list[dict[str, Any]]:
    if result.walk is None:
        return []
    rings = []
    for distance in range(0, result.walk.max_distance + 1):
        entities = result.walk.at_distance(distance)
        members = [e for e in entities if e in MEMBER_IDS]
        rings.append(
            {
                "distance": distance,
                "members": members,
                "entities": entities,
                "label": "the change itself" if distance == 0 else f"{distance} hop(s) out",
            }
        )
    return rings


def _edited_member_ids(scenario: sc.Scenario) -> list[str]:
    """Which members the change NAMES -- as opposed to which it reaches.

    The gap between these two sets is the entire thesis, so the browser is
    given both and draws the difference.
    """
    if scenario.removed_member is not None:
        return [scenario.removed_member]
    if scenario.areas_in2:
        return sorted(scenario.areas_in2, key=MEMBER_IDS.index)
    return []


def shape_result(
    result: PipelineResult,
    scenario: sc.Scenario,
    truth: list[str],
    run_id: str,
) -> dict[str, Any]:
    decision = result.decision
    governing = decision.governing_member
    edited = _edited_member_ids(scenario)
    violating = list(decision.violating_member_ids)
    return {
        "run_id": run_id,
        "scenario_id": scenario.id,
        "change": {
            "kind": scenario.kind.value,
            "description": scenario.description,
            "target_id": scenario.target_id,
            "value_before": scenario.value_before,
            "value_after": scenario.value_after,
            "document_id": result.graph.source.document_id if result.graph.source else None,
            "branch_name": result.graph.source.branch_name if result.graph.source else None,
            "edited_member_ids": edited,
        },
        "walk": {
            "rings": _walk_rings(result),
            "members_reached": sorted(
                (m for m in (result.walk.member_ids if result.walk else []) if m in MEMBER_IDS),
                key=MEMBER_IDS.index,
            ),
            "member_count": len(result.graph.members),
            "notes": [n for n in result.notes if n.startswith("walk:")],
        },
        "decision": {
            "id": decision.id,
            "outcome": decision.outcome.value,
            "threshold": decision.safety_factor_threshold,
            "violating_member_ids": violating,
            "governing_member_id": governing.member_id if governing else None,
            "governing_safety_factor": (
                None if governing is None or governing.safety_factor is None
                else round(governing.safety_factor, 3)
            ),
            "error_message": decision.error_message,
            "solver": decision.solver,
            "load_case_id": decision.load_case_id,
            "members": _member_rows(result),
        },
        # The point of the whole demo, computed rather than asserted: was the
        # member that failed one the change actually named?
        "dropped_domino": {
            "unsafe_members": truth,
            "edited_members": edited,
            "downstream_failures": [m for m in violating if m not in edited],
            "caught": sorted(set(truth) & set(violating), key=MEMBER_IDS.index),
            "missed": sorted(set(truth) - set(violating), key=MEMBER_IDS.index),
        },
        "ecn": {
            "id": result.ecn.id,
            "status": result.ecn.status.value,
            "headline": result.ecn.status.headline,
            "title": result.ecn.title,
            "text": result.ecn_text,
            "markdown": result.ecn_markdown,
        },
        "svg": result.svg,
        "notes": result.notes,
    }


# --------------------------------------------------------------------------
# The run store. Tamper acts on the Decision a run really produced, so the
# browser never gets to hand us a Decision of its own invention.
# --------------------------------------------------------------------------

class RunStore:
    """The last `MAX_STORED_RUNS` decisions, by run id, in memory only."""

    def __init__(self, limit: int = MAX_STORED_RUNS) -> None:
        self._limit = limit
        self._runs: OrderedDict[str, str] = OrderedDict()
        self._lock = Lock()

    def put(self, decision: Decision) -> str:
        run_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._runs[run_id] = decision.to_json()
            while len(self._runs) > self._limit:
                self._runs.popitem(last=False)
        return run_id

    def get(self, run_id: str) -> Decision:
        with self._lock:
            raw = self._runs.get(run_id)
        if raw is None:
            raise BadRequest(
                "that run is no longer in memory -- run the change again "
                "(the server keeps only the most recent runs)"
            )
        return Decision.from_json(raw)


STORE = RunStore()


# --------------------------------------------------------------------------
# The two things the site can do
# --------------------------------------------------------------------------

def run_change(payload: dict[str, Any]) -> dict[str, Any]:
    """Compose the change, run the real pipeline, shape the answer.

    `run_pipeline` is called with no SkyCiv client and no sender: Stage 3 and
    Stage 4 are not a browser's to trigger. The ECN is still built and
    rendered, which is what a judge wants to read.
    """
    scenario = build_scenario(payload)
    threshold = _threshold(payload)
    graphs = scenario.build()

    try:
        result = run_pipeline(
            graphs.after,
            before=graphs.before,
            threshold=threshold,
            skyciv=None,
            sender=None,
            merge_hook=None,
        )
    except ValueError as e:
        # The threshold floor, and anything else the contracts refuse. This
        # is the guarantee, not a crash: report it as the refusal it is.
        return {
            "refused": True,
            "reason": "the code refused this run",
            "detail": str(e),
            "exception": type(e).__name__,
        }

    truth = ground_truth(graphs.after, threshold=threshold)
    run_id = STORE.put(result.decision)
    return shape_result(result, scenario, truth, run_id)


def tamper(payload: dict[str, Any]) -> dict[str, Any]:
    """Flip a real Decision's outcome and call `validate()` on it.

    This is Act 4 of the terminal demo, made interactive. The Decision is the
    one the named run produced -- reloaded from its own JSON, so nothing the
    browser sends can weaken it -- and `validate()` is the contract's, not a
    re-implementation. If it ever fails to raise, the site says the guarantee
    is broken rather than printing a success.
    """
    run_id = payload.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise BadRequest("run_id is required")
    requested = payload.get("outcome", Outcome.APPROVED.value)
    try:
        outcome = Outcome(requested)
    except ValueError:
        raise BadRequest(
            f"outcome must be one of {', '.join(o.value for o in Outcome)}"
        ) from None

    decision = STORE.get(run_id)
    was = decision.outcome
    governing = decision.governing_member
    decision.outcome = outcome

    try:
        decision.validate()
    except ValueError as e:
        return {
            "enforced": True,
            "outcome_before": was.value,
            "outcome_requested": outcome.value,
            "governing_member_id": governing.member_id if governing else None,
            "governing_safety_factor": (
                None if governing is None or governing.safety_factor is None
                else round(governing.safety_factor, 3)
            ),
            "threshold": decision.safety_factor_threshold,
            "exception": type(e).__name__,
            "message": str(e),
            "reasons": [chunk.strip() for chunk in str(e).split(";") if chunk.strip()],
        }

    # No exception. Either the decision was legitimately approvable (asking
    # for the outcome it already had), or the guarantee has a hole.
    legitimate = was is outcome or outcome is not Outcome.APPROVED
    return {
        "enforced": False,
        "legitimate": legitimate,
        "outcome_before": was.value,
        "outcome_requested": outcome.value,
        "message": (
            f"validate() accepted {outcome.value}: this decision does not "
            "contradict its own numbers."
            if legitimate
            else "validate() did NOT raise on a contradicted approval -- "
                 "the guarantee is broken and this build must not be presented."
        ),
    }


# --------------------------------------------------------------------------
# The recorded eval, read once
# --------------------------------------------------------------------------

_SCOREBOARD_CACHE: dict[str, Any] | None = None
_SCOREBOARD_LOCK = Lock()


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def scoreboard() -> dict[str, Any]:
    """The Sprint 5A result, from the committed recordings.

    Cached: the file does not change while the server runs, and a judge
    clicking the tab must not be able to make us re-read it a thousand times.
    """
    global _SCOREBOARD_CACHE
    with _SCOREBOARD_LOCK:
        if _SCOREBOARD_CACHE is not None:
            return _SCOREBOARD_CACHE

        records_path = RECORDINGS / "records.json"
        payload: dict[str, Any] = {
            "available": records_path.exists(),
            "consistency_markdown": _read_text(RECORDINGS / "consistency.md"),
        }
        if records_path.exists():
            board = score(load_records(records_path))
            payload["markdown"] = board.to_markdown()
            payload["agents"] = [
                {
                    "agent": a.agent,
                    "scenarios": a.scenarios,
                    "unsafe_members": a.unsafe_members_total,
                    "caught": a.caught,
                    "dropped_dominoes": a.dropped_dominoes,
                    "false_alarms": a.false_alarms,
                    "recall": round(a.recall, 4),
                }
                for a in board.scores
            ]
        _SCOREBOARD_CACHE = payload
        return payload


__all__ = [
    "AREA_MAX_IN2",
    "AREA_MIN_IN2",
    "BadRequest",
    "CHANGE_KINDS",
    "FACTOR_MAX",
    "FACTOR_MIN",
    "LOAD_MAX_KIPS",
    "STORE",
    "THRESHOLD_MAX",
    "base_model",
    "build_scenario",
    "run_change",
    "scoreboard",
    "shape_result",
    "tamper",
]
