"""Deterministic snapshot diff. No geometry or safety inference by a model."""

from copy import deepcopy
from math import isclose

from earl.contracts import ChangeEvent, ChangeKind, DependencyGraph, TargetKind


def physical_model(graph: DependencyGraph) -> dict:
    """Ignore provenance and section IDs, never ignore physical section properties."""
    graph.validate()
    members = {}
    for member in graph.members:
        section, material = graph.section(member.section_id), graph.material(member.material_id)
        members[member.id] = {"start": member.start_node, "end": member.end_node,
                              "section": {k: v for k, v in section.to_dict().items() if k not in ("id", "name")},
                              "material": {k: v for k, v in material.to_dict().items() if k not in ("id", "name")}}
    return {"nodes": {n.id: n.to_dict() for n in graph.nodes}, "members": members,
            "loads": {c.id: c.to_dict() for c in graph.load_cases},
            "hard_floor": graph.hard_floor, "design_target": graph.design_target, "units": graph.to_dict()["units"]}


def equal_physics(a, b) -> bool:
    if isinstance(a, (float, int)) and isinstance(b, (float, int)):
        return isclose(a, b, rel_tol=1e-9, abs_tol=1e-12)
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(equal_physics(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(equal_physics(x, y) for x, y in zip(a, b))
    return a == b


def snapshot_change(before: DependencyGraph, after: DependencyGraph, identity: str) -> ChangeEvent:
    """Build one flat batch, then prove it reconstructs the observed model.

Supports declared node coordinates, member areas/presence, point loads and
load locations. A material/support/policy change outside this mapping is an
explicit ERROR, not a partial edit with an approval attached.
"""
    before.validate()
    after.validate()
    deltas = []

    def add(kind, target_kind, target_id, field, old=None, new=None, **kwargs):
        deltas.append(ChangeEvent(f"{identity}-{len(deltas)}", kind,
                                 f"Observed {target_id} {field}: {old} -> {new}", target_id,
                                 target_kind=target_kind, field_name=field,
                                 numeric_before=old, numeric_after=new, **kwargs))

    for node in after.nodes:
        prior = before.node(node.id)
        for axis in ("x", "y"):
            if not equal_physics(getattr(prior, axis), getattr(node, axis)):
                add(ChangeKind.FEATURE_EDIT, TargetKind.NODE, node.id, axis, getattr(prior, axis), getattr(node, axis))
    old_ids, new_ids = {m.id for m in before.members}, {m.id for m in after.members}
    for mid in sorted(old_ids - new_ids):
        add(ChangeKind.MEMBER_REMOVED, TargetKind.MEMBER, mid, "present", 1, 0)
    for mid in sorted(new_ids - old_ids):
        member = deepcopy(after.member(mid))
        section = deepcopy(after.section(member.section_id))
        section.id = f"restored_{mid}_{identity}"
        member.section_id = section.id
        add(ChangeKind.FEATURE_EDIT, TargetKind.MEMBER, mid, "present", 0, 1,
            member_after=member, section_after=section)
    for mid in sorted(old_ids & new_ids):
        old_area = before.section(before.member(mid).section_id).area
        new_area = after.section(after.member(mid).section_id).area
        if not equal_physics(old_area, new_area):
            add(ChangeKind.FEATURE_EDIT, TargetKind.MEMBER, mid, "area", old_area, new_area)
    for case in after.load_cases:
        old_case = next(c for c in before.load_cases if c.id == case.id)
        old_loads = {p.id: p for p in old_case.point_loads}
        for load in case.point_loads:
            old = old_loads.get(load.id)
            if old is None:
                component = "fx" if load.fx else "fy"
                add(ChangeKind.LOAD_ADDED, TargetKind.LOAD, load.id, component, 0, getattr(load, component),
                    node_after=load.node_id, load_case_id=case.id)
                continue
            for component in ("fx", "fy"):
                if not equal_physics(getattr(old, component), getattr(load, component)):
                    add(ChangeKind.VARIABLE_EDIT, TargetKind.LOAD, load.id, component,
                        getattr(old, component), getattr(load, component), load_case_id=case.id)
            if load.node_id != old.node_id:
                add(ChangeKind.LOAD_MOVED, TargetKind.LOAD, load.id, "node_id",
                    node_before=old.node_id, node_after=load.node_id, load_case_id=case.id)
    if not deltas:
        member = before.members[0]
        area = before.section(member.section_id).area
        add(ChangeKind.FEATURE_EDIT, TargetKind.MEMBER, member.id, "area", area, area)
    description = f"Observed CAD snapshot {identity}: " + "; ".join(d.description for d in deltas)
    change = ChangeEvent(identity, ChangeKind.FEATURE_EDIT, description, "assembly", deltas=deltas)
    if not equal_physics(physical_model(change.apply(before)), physical_model(after)):
        raise ValueError("Observed CAD snapshot includes unsupported material, support, policy, or load changes")
    return change
