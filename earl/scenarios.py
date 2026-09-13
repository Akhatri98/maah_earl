"""Frozen scenario catalogue and validated judge inputs, shared by CLI and web."""

from __future__ import annotations

import json
from copy import deepcopy
from math import isfinite

from earl.config import PROJECT_ROOT
from earl.contracts import ChangeEvent, ChangeKind, DependencyGraph, TargetKind
from earl.units import IN

CATALOGUE = PROJECT_ROOT / "earl" / "eval" / "scenarios.json"
AREA_RANGE_IN2 = (0.5, 40.0)
LOAD_RANGE_KN = (0.0, 150.0)


def catalogue() -> list[dict]:
    return json.loads(CATALOGUE.read_text(encoding="utf-8"))["scenarios"]


def clamp(value: object, bounds: tuple[float, float], name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    try:
        number = float(value)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not isfinite(number):
        raise ValueError(f"{name} must be a finite number")
    return max(bounds[0], min(bounds[1], number))


def make_change(graph: DependencyGraph, scenario: str = "thin-compression", *,
                param: dict | None = None) -> ChangeEvent:
    graph.validate()
    param = param or {}
    if not isinstance(param, dict) or len(param) > 8:
        raise ValueError("param must be a small object")
    if scenario in ("custom-area", "custom-remove", "custom-load"):
        allowed = {"member", "area_in2", "load_kn", "node"}
        if set(param) - allowed:
            raise ValueError("unknown judge parameter")
        spec = {"id": scenario, "kind": scenario.removeprefix("custom-")}
        spec.update(param)
    else:
        spec = next((deepcopy(s) for s in catalogue() if s["id"] == scenario), None)
        if spec is None:
            raise ValueError("unknown scenario")
        if param:
            if set(param) - {"value"}:
                raise ValueError("preset override only accepts value")
            spec["area_in2" if spec["kind"] == "area" else "load_kn"] = param["value"]
    kind = spec["kind"]
    if kind in ("area", "remove"):
        mid = spec.get("member", "m8")
        member = graph.member(mid)
        current = graph.section(member.section_id).area
        if kind == "area":
            area = clamp(spec.get("area_in2", 8.0), AREA_RANGE_IN2, "area")
            return ChangeEvent(
                spec["id"], ChangeKind.FEATURE_EDIT,
                f"Resize {mid} from {current / IN**2:g} to {area:g} in^2.", mid,
                target_kind=TargetKind.MEMBER, field_name="area",
                numeric_before=current, numeric_after=area * IN**2,
            )
        return ChangeEvent(spec["id"], ChangeKind.MEMBER_REMOVED, f"Remove member {mid}.", mid,
                           target_kind=TargetKind.MEMBER, field_name="present",
                           numeric_before=1.0, numeric_after=0.0)
    if kind in ("add_load", "load", "move_load"):
        node_id = spec.get("node", "n2")
        if node_id not in {"n1", "n2", "n3", "n4"}:
            raise ValueError("judge loads must land on free nodes n1 through n4")
        graph.node(node_id)
        case = graph.load_cases[0]
        if kind == "add_load":
            force = clamp(spec.get("load_kn", 20), LOAD_RANGE_KN, "load")
            return ChangeEvent(spec["id"], ChangeKind.LOAD_ADDED,
                               f"Add {force:g} kN downward at {node_id}.", "p_added",
                               target_kind=TargetKind.LOAD, field_name="fy",
                               numeric_before=0.0, numeric_after=-force * 1000,
                               node_after=node_id, load_case_id=case.id)
        load = next((p for p in case.point_loads if p.id == spec.get("load", "p1")), None)
        if load is None:
            raise ValueError("unknown load ID")
        if kind == "move_load":
            return ChangeEvent(spec["id"], ChangeKind.LOAD_MOVED,
                               f"Move {load.id} ({abs(load.fy)/1000:g} kN) from {load.node_id} to {node_id}.",
                               load.id, target_kind=TargetKind.LOAD, field_name="node_id",
                               node_before=load.node_id, node_after=node_id, load_case_id=case.id)
        force = clamp(spec.get("load_kn", 60), LOAD_RANGE_KN, "load")
        return ChangeEvent(spec["id"], ChangeKind.FEATURE_EDIT,
                           f"Set {load.id} to {force:g} kN downward at {node_id}.", load.id,
                           target_kind=TargetKind.LOAD, field_name="fy",
                           numeric_before=load.fy, numeric_after=-force * 1000,
                           node_before=load.node_id, node_after=node_id, load_case_id=case.id)
    raise ValueError("unsupported scenario kind")
