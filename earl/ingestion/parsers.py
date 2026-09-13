"""Parse raw Onshape responses into graph-ready structures. [Track A, Sprint 1A]

Scope note: this module deliberately stops short of mapping CAD geometry onto
truss nodes and members. That mapping depends on how the truss is actually
modelled (is a member a part? are nodes mate connectors?), which cannot be
known until the geometry exists in the document. What is here is the part whose
shape Onshape documents and guarantees:

  * the variable table (the primary change signal),
  * assembly instances (the things that can be affected),
  * mate features (the edges that make one instance depend on another),
  * a reverse "where used" index derived from those mates.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..contracts.graph import EdgeKind
from ..units import evaluated_variable


# --------------------------------------------------------------------------
# Variable table
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Variable:
    """One row of an Onshape variable table.

    `expression` is the authored text ("360 in"); `value` is Onshape's
    evaluated value, normalized from numeric SI or a supported formatted unit
    string. Authored expressions are never evaluated locally. Both are kept: the
    expression is what a human recognises in an ECN, the value is what the
    solver needs.
    """

    name: str
    type: str
    expression: str
    value: float | None = None
    description: str = ""


def parse_variables(payload: Any) -> list[Variable]:
    """Parse GET /variables/d/{d}/w/{w}/e/{e}/variables.

    The response is a list of variable *studios*, each holding a list of
    variables; a part studio with no variable studio still returns one entry
    with an empty list.
    """
    out: list[Variable] = []
    for studio in payload or []:
        for v in studio.get("variables") or []:
            kind, value = evaluated_variable(v.get("value"), v.get("type", ""))
            out.append(
                Variable(
                    name=v.get("name", ""),
                    type=kind,
                    expression=v.get("expression", ""),
                    value=value,
                    description=v.get("description") or "",
                )
            )
    return out


def diff_variables(
    before: Iterable[Variable], after: Iterable[Variable]
) -> list[tuple[str, str | None, str | None]]:
    """Compare two variable-table reads.

    Returns (name, before_expression, after_expression) for every variable
    added, removed or changed. A None on either side means the variable did not
    exist on that side -- which is itself a change worth reporting, not a
    missing value to be silently skipped.
    """
    b = {v.name: v for v in before}
    a = {v.name: v for v in after}
    changes: list[tuple[str, str | None, str | None]] = []
    for name in sorted(set(b) | set(a)):
        bv, av = b.get(name), a.get(name)
        if bv is None or av is None or bv.expression != av.expression:
            changes.append(
                (name, bv.expression if bv else None, av.expression if av else None)
            )
    return changes


# --------------------------------------------------------------------------
# Assembly structure
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Instance:
    """One occurrence of a part or subassembly in an assembly."""

    id: str
    name: str
    type: str                      # "Part" | "Assembly" | "Feature"
    document_id: str = ""
    element_id: str = ""
    part_id: str = ""
    suppressed: bool = False


@dataclass(frozen=True)
class Mate:
    """A mate feature: the relationship that makes instances interdependent."""

    id: str
    name: str
    mate_type: str                 # FASTENED | REVOLUTE | SLIDER | ...
    occurrences: tuple[str, ...] = ()
    suppressed: bool = False


def parse_instances(assembly: dict[str, Any]) -> list[Instance]:
    """Parse rootAssembly.instances from GET /assemblies/d/{d}/w/{w}/e/{e}."""
    root = (assembly or {}).get("rootAssembly") or {}
    return [
        Instance(
            id=i.get("id", ""),
            name=i.get("name", ""),
            type=i.get("type", ""),
            document_id=i.get("documentId", "") or "",
            element_id=i.get("elementId", "") or "",
            part_id=i.get("partId", "") or "",
            suppressed=bool(i.get("suppressed", False)),
        )
        for i in root.get("instances") or []
    ]


def parse_mates(assembly_or_features: dict[str, Any]) -> list[Mate]:
    """Parse mate features.

    Accepts either the assembly definition (which carries rootAssembly.features
    when requested with includeMateFeatures) or the dedicated /features
    response, since the two nest the same feature objects differently.
    """
    payload = assembly_or_features or {}
    features = (payload.get("rootAssembly") or {}).get("features")
    if features is None:
        features = payload.get("features") or []

    mates: list[Mate] = []
    for f in features:
        # /features wraps each feature in a {type, message} envelope;
        # the assembly definition does not.
        body = f.get("message", f)
        data = body.get("featureData") or {}
        occurrences: list[str] = []
        for entity in data.get("matedEntities") or []:
            occ = entity.get("matedOccurrence") or []
            if occ:
                # An occurrence path is a list of instance ids; the LAST entry
                # is the instance actually being mated, earlier entries are the
                # subassemblies containing it.
                occurrences.append(occ[-1])

        mates.append(
            Mate(
                id=body.get("featureId") or body.get("id") or "",
                name=data.get("name") or body.get("name") or "",
                mate_type=(data.get("mateType") or "").upper(),
                occurrences=tuple(occurrences),
                suppressed=bool(body.get("suppressed", False)),
            )
        )
    return mates


# --------------------------------------------------------------------------
# Dependency edges / "where used"
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class InstanceEdge:
    """Ingestion-local IDs. Resolve through truss_map before producing a contract."""

    source_id: str
    target_id: str
    kind: EdgeKind


def mate_edges(mates: Iterable[Mate], *, include_suppressed: bool = False) -> list[InstanceEdge]:
    """Turn mates into dependency edges.

    A mate is undirected in CAD -- if A is fastened to B, a change to either
    affects the other -- so each mate yields edges in BOTH directions. Emitting
    only one direction would make the downstream traversal miss real
    dependencies, which is precisely a dropped domino.
    """
    edges: list[InstanceEdge] = []
    seen: set[tuple[str, str]] = set()
    for mate in mates:
        if mate.suppressed and not include_suppressed:
            continue
        occ = [o for o in mate.occurrences if o]
        for i, a in enumerate(occ):
            for b in occ[i + 1:]:
                if a == b:
                    continue
                for pair in ((a, b), (b, a)):
                    if pair not in seen:
                        seen.add(pair)
                        edges.append(InstanceEdge(pair[0], pair[1], EdgeKind.MATE))
    return edges

@dataclass
class WhereUsed:
    """Reverse index: which instances reference a given part/element.

    Onshape has no single "where used" endpoint for this; it is derived from
    the assembly contents.
    """

    by_element: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    by_part: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))

    def instances_of_element(self, element_id: str) -> list[str]:
        return list(self.by_element.get(element_id, []))

    def instances_of_part(self, part_id: str) -> list[str]:
        return list(self.by_part.get(part_id, []))


def build_where_used(instances: Iterable[Instance]) -> WhereUsed:
    """Index instances by the element and part they come from, so an edit to a
    source part studio can be traced to every assembly instance using it."""
    wu = WhereUsed()
    for inst in instances:
        if inst.element_id:
            wu.by_element[inst.element_id].append(inst.id)
        if inst.part_id:
            wu.by_part[inst.part_id].append(inst.id)
    return wu


def where_used_edges(instances: Iterable[Instance]) -> list[InstanceEdge]:
    """Edges between instances that share a source part.

    Two instances of the same part are coupled: changing the part changes both.
    """
    wu = build_where_used(instances)
    edges: list[InstanceEdge] = []
    for siblings in wu.by_part.values():
        for i, a in enumerate(siblings):
            for b in siblings[i + 1:]:
                edges.append(InstanceEdge(a, b, EdgeKind.WHERE_USED))
                edges.append(InstanceEdge(b, a, EdgeKind.WHERE_USED))
    return edges
