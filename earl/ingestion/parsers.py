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

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..contracts.graph import Edge, EdgeKind


# --------------------------------------------------------------------------
# Variable table
# --------------------------------------------------------------------------

# SI factors for the length units Onshape expressions are authored in.
_LENGTH_UNITS = {
    "m": 1.0, "meter": 1.0, "metre": 1.0,
    "cm": 0.01, "mm": 0.001,
    "in": 0.0254, "inch": 0.0254, "\"": 0.0254,
    "ft": 0.3048, "foot": 0.3048, "feet": 0.3048,
    "yd": 0.9144,
}

_EXPRESSION = re.compile(
    r"""^\s*
        (?P<number>[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)   # 360, 1.5, 1e3
        \s*\*?\s*
        (?P<unit>[A-Za-z"]+)?                         # in, mm, ft (optional)
        \s*(?:\^\s*(?P<power>\d+))?                   # ^2 for areas
        \s*$""",
    re.VERBOSE,
)


def evaluate_expression(expression: str) -> float | None:
    """Convert an Onshape variable expression to an SI float.

    Onshape's /variables endpoint returns `value: null` -- it hands back only
    the authored text ("360 in"), never an evaluated number. So the conversion
    has to happen here.

    Returns None for anything this cannot evaluate exactly -- an expression
    referencing another variable ("#bayWidth * 2"), arithmetic, or an unknown
    unit. None means "unknown", never a guessed number: a silently wrong
    conversion is worse than no value, because everything downstream would
    treat it as real.
    """
    if not expression:
        return None

    match = _EXPRESSION.match(expression.strip())
    if not match:
        return None

    number = float(match.group("number"))
    unit = (match.group("unit") or "").lower()
    power = int(match.group("power") or 1)

    if not unit:
        return number if power == 1 else None   # unitless; a power is nonsense

    factor = _LENGTH_UNITS.get(unit)
    if factor is None:
        return None

    return number * (factor ** power)


@dataclass(frozen=True)
class Variable:
    """One row of an Onshape variable table.

    `expression` is the authored text ("360 in") -- that is what a human
    recognises in an ECN, and what change detection diffs. `value` is whatever
    Onshape reported, which in practice is always None.

    Use `si_value` for a number to compute with.
    """

    name: str
    type: str
    expression: str
    value: float | None = None
    description: str = ""

    @property
    def si_value(self) -> float | None:
        """The value in SI units, evaluated from the expression when Onshape
        does not supply one. None when it cannot be evaluated exactly."""
        if self.value is not None:
            return self.value
        return evaluate_expression(self.expression)


def parse_variables(payload: Any) -> list[Variable]:
    """Parse GET /variables/d/{d}/w/{w}/e/{e}/variables.

    The response is a list of variable *studios*, each holding a list of
    variables; a part studio with no variable studio still returns one entry
    with an empty list.
    """
    out: list[Variable] = []
    for studio in payload or []:
        for v in studio.get("variables") or []:
            out.append(
                Variable(
                    name=v.get("name", ""),
                    type=v.get("type", ""),
                    expression=v.get("expression", ""),
                    value=_as_float(v.get("value")),
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
    # Mate connector origins in assembly coordinates, one per mated entity.
    # These are the truss NODE positions, which is what makes it possible to
    # tell which members meet where without re-deriving it from part geometry.
    origins: tuple[tuple[float, float, float], ...] = ()
    # Connector identifiers, where the source format carries them. Our
    # FeatureScript ids survive here as "<featureid>.m1_n5", so a mate can be
    # traced back to the exact node it was made at.
    connector_ids: tuple[str, ...] = ()


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
    """Parse mate features from either Onshape response that carries them.

    The two endpoints return genuinely different formats:

      * **assembly definition** (`GET /assemblies/.../e/{eid}` with
        includeMateFeatures) -- the resolved form, with `featureData`,
        `matedEntities` and the mate connector coordinate systems. Prefer this:
        it is the only one that carries node POSITIONS.
      * **`/features`** -- the authoring form, Onshape's BTM parameter tree,
        where the mate type and the mated occurrences are buried in a
        `parameters` list rather than named fields.

    Both are handled because reading the authoring form with the resolved
    parser silently yields mates with no occurrences -- correct-looking objects
    that produce zero edges. A quietly empty dependency graph is the precise
    failure this project exists to catch, so it must not be possible here.
    """
    payload = assembly_or_features or {}
    features = (payload.get("rootAssembly") or {}).get("features")
    if features is None:
        features = payload.get("features") or []

    mates: list[Mate] = []
    for f in features:
        body = f.get("message", f)

        if "featureData" in body:
            mates.append(_parse_resolved_mate(body))
        elif body.get("parameters") is not None:
            mates.append(_parse_btm_mate(body))
    return mates


def _parse_resolved_mate(body: dict[str, Any]) -> Mate:
    """Assembly-definition form: named fields, plus connector coordinates."""
    data = body.get("featureData") or {}
    occurrences: list[str] = []
    origins: list[tuple[float, float, float]] = []

    for entity in data.get("matedEntities") or []:
        occ = entity.get("matedOccurrence") or []
        if occ:
            # An occurrence path is a list of instance ids; the LAST entry is
            # the instance actually being mated, earlier entries are the
            # subassemblies containing it.
            occurrences.append(occ[-1])

        cs = entity.get("mateConnectorCS") or entity.get("matedCS") or {}
        origin = cs.get("origin")
        if origin and len(origin) == 3:
            origins.append(tuple(float(v) for v in origin))

    # Group mates hold a flat occurrence list instead of matedEntities.
    is_group = False
    for entry in data.get("occurrences") or []:
        is_group = True
        occ = entry.get("occurrence") or []
        if occ:
            occurrences.append(occ[-1])

    mate_type = (data.get("mateType") or "").upper()
    if not mate_type and is_group:
        mate_type = "GROUP"

    return Mate(
        id=body.get("featureId") or body.get("id") or "",
        name=data.get("name") or body.get("name") or "",
        mate_type=mate_type,
        occurrences=tuple(occurrences),
        suppressed=bool(body.get("suppressed", False)),
        origins=tuple(origins),
    )


def _parse_btm_mate(body: dict[str, Any]) -> Mate:
    """`/features` form: values live in a BTM `parameters` list, keyed by
    `parameterId`, not as named fields."""
    params = {}
    for p in body.get("parameters") or []:
        pm = p.get("message") or {}
        if pm.get("parameterId"):
            params[pm["parameterId"]] = pm

    mate_type = str((params.get("mateType") or {}).get("value") or "").upper()

    occurrences: list[str] = []
    connector_ids: list[str] = []
    query_param = params.get("mateConnectorsQuery") or params.get("occurrencesQuery")
    for q in (query_param or {}).get("queries") or []:
        qm = q.get("message") or {}
        path = qm.get("path") or []
        if path:
            occurrences.append(path[-1])
        if qm.get("featureId"):
            connector_ids.append(qm["featureId"])

    if not mate_type and body.get("featureType") == "mateGroup":
        mate_type = "GROUP"

    return Mate(
        id=body.get("featureId") or body.get("nodeId") or "",
        name=body.get("name") or "",
        mate_type=mate_type,
        occurrences=tuple(occurrences),
        suppressed=bool(body.get("suppressed", False)),
        connector_ids=tuple(connector_ids),
    )


# --------------------------------------------------------------------------
# Dependency edges / "where used"
# --------------------------------------------------------------------------

def mate_edges(mates: Iterable[Mate], *, include_suppressed: bool = False) -> list[Edge]:
    """Turn mates into dependency edges.

    A mate is undirected in CAD -- if A is fastened to B, a change to either
    affects the other -- so each mate yields edges in BOTH directions. Emitting
    only one direction would make the downstream traversal miss real
    dependencies, which is precisely a dropped domino.

    Caution on GROUP mates: a group couples every member of the group to every
    other, so one group over the whole assembly produces a fully-connected
    graph in which everything depends on everything. That is not wrong exactly,
    but it is useless -- the traversal can no longer distinguish what a change
    actually reaches. Model real per-node mates instead.
    """
    edges: list[Edge] = []
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
                        edges.append(Edge(pair[0], pair[1], EdgeKind.MATE))
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


def where_used_edges(instances: Iterable[Instance]) -> list[Edge]:
    """Edges between instances that share a source part.

    Two instances of the same part are coupled: changing the part changes both.
    """
    wu = build_where_used(instances)
    edges: list[Edge] = []
    for siblings in wu.by_part.values():
        for i, a in enumerate(siblings):
            for b in siblings[i + 1:]:
                edges.append(Edge(a, b, EdgeKind.WHERE_USED))
                edges.append(Edge(b, a, EdgeKind.WHERE_USED))
    return edges


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
