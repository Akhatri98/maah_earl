"""Explicit CAD instance -> member mapping and the only graph ingestion boundary."""

from __future__ import annotations

import json
import logging
from math import isfinite
from pathlib import Path
from typing import Any

from earl.config import hard_floor
from earl.contracts import DependencyGraph, Edge, EdgeKind
from earl.units import round_inertia
from scripts.contract_selfcheck import build_graph

from .parsers import (
    InstanceEdge, mate_edges, parse_instances, parse_mates, parse_variables,
    where_used_edges,
)

LOG = logging.getLogger(__name__)
MAP_PATH = Path(__file__).with_name("truss_map.json")


def resolve_edges(edges: list[InstanceEdge], ids: dict[str, str]) -> list[Edge]:
    """No raw instance identifier is allowed to become a contract endpoint."""
    result = []
    seen = set()
    for edge in edges:
        if edge.source_id not in ids or edge.target_id not in ids:
            LOG.warning("Unmapped dependency endpoint: %s -> %s", edge.source_id, edge.target_id)
            continue
        a, b = ids[edge.source_id], ids[edge.target_id]
        key = (a, b, edge.kind)
        if a != b and key not in seen:
            result.append(Edge(a, b, edge.kind))
            seen.add(key)
    return result


def map_assembly(
    assembly: dict[str, Any], variables: list[dict[str, Any]], *,
    provenance: str = "synthetic fixture",
) -> DependencyGraph:
    mapping = json.loads(MAP_PATH.read_text(encoding="utf-8"))
    graph = build_graph()
    graph.provenance = provenance
    graph.hard_floor = hard_floor()
    rows = {v.name: v for v in parse_variables(variables)}
    for name in ("bayWidth", "bayHeight", "barArea"):
        if name not in rows or rows[name].value is None:
            raise ValueError(f"required Onshape variable {name!r} is missing")
    for name, row in rows.items():
        if name not in mapping["variables"]:
            continue
        spec = mapping["variables"][name]
        if row.type != spec["type"] or row.value is None or not isfinite(row.value):
            raise ValueError(f"invalid type or SI value for {name}")
        if row.value <= 0:
            raise ValueError(f"{name} must be positive")
        if name in ("bayWidth", "bayHeight"):
            for node in graph.nodes:
                axis = spec["field"]
                setattr(node, axis, getattr(node, axis) * row.value / spec["canonical_value_m"])
        elif name == "barArea":
            for section in graph.sections:
                section.area = row.value
                section.iy = section.iz = round_inertia(row.value)
                section.j = 2 * section.iy
                section.shape = mapping["section_shape"]
                section.name = "Solid circular bar"
        elif name == "safetyFactor":
            graph.design_target = row.value
        elif name == "pointLoad":
            for case in graph.load_cases:
                for load in case.point_loads:
                    load.fy = -row.value

    instances = parse_instances(assembly)
    ids = {}
    parts = {}
    for instance in instances:
        mid = mapping["instance_members"].get(instance.name)
        if mid is None:
            warning = f"Unmapped instance {instance.name!r}; outside the declared truss map"
            LOG.warning(warning)
            graph.mapping_warnings.append(warning)
            continue
        if instance.suppressed:
            continue
        if mid in ids.values():
            raise ValueError(f"multiple active instances map to {mid}")
        ids[instance.id] = mid
        # Part provenance is permitted; occurrence IDs stay ingestion-local.
        parts[mid] = "/".join((instance.document_id, instance.element_id, instance.part_id))

    graph.members = [m for m in graph.members if m.id in parts]
    if not graph.members:
        raise ValueError("assembly has no mapped truss members")
    for member in graph.members:
        member.onshape_id = parts[member.id]
    raw_edges = mate_edges(parse_mates(assembly)) + where_used_edges(instances)
    graph.edges = resolve_edges(raw_edges, ids)
    for a in graph.members:
        for b in graph.members:
            if a.id != b.id and {a.start_node, a.end_node} & {b.start_node, b.end_node}:
                graph.edges.append(Edge(a.id, b.id, EdgeKind.TOPOLOGY))
    graph.affected_member_ids = [m.id for m in graph.members]
    graph.affected_node_ids = [n.id for n in graph.nodes]
    root = assembly.get("rootAssembly") or {}
    graph.source.document_id = root.get("documentId") or graph.source.document_id
    graph.source.element_id = root.get("elementId") or graph.source.element_id
    graph.change.numeric_before = rows["barArea"].value
    graph.change.numeric_after = rows["barArea"].value * 0.4
    graph.validate()
    return graph
