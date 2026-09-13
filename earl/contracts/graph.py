"""Dependency graph -- the payload Track A hands to Track B.

Produced by: earl.ingestion (Onshape)      [Track A]
Consumed by: earl.analysis (PyNite/Biject) [Track B]

Design rule: this object must be SUFFICIENT TO BUILD THE FEA MODEL on its own.
Track B should never have to call back into Onshape to find a coordinate, a
cross-sectional area or a load. If Track B needs a value in order to solve, it
belongs here.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, replace
from enum import Enum
from math import isclose, isfinite

from .common import CONTRACT_VERSION, SI_UNITS, Serializable, Units


# --------------------------------------------------------------------------
# Structure
# --------------------------------------------------------------------------

class SupportType(str, Enum):
    """Node restraint. Truss scope: pin and roller carry the demo cases."""

    FREE = "free"
    PIN = "pin"              # translation fixed, rotation free
    ROLLER_X = "roller_x"    # free to translate along X
    ROLLER_Y = "roller_y"
    FIXED = "fixed"


@dataclass
class Material(Serializable):
    id: str
    name: str
    elastic_modulus: float    # E
    yield_strength: float     # Track B derives member capacity from this
    density: float = 0.0      # 0 = ignore self-weight


@dataclass
class Section(Serializable):
    """Cross-section properties. Truss analysis needs `area`; the inertia terms
    are carried so this contract survives a later move to frame elements."""

    id: str
    name: str
    area: float
    iy: float = 0.0
    iz: float = 0.0
    j: float = 0.0
    shape: str = "unspecified"


@dataclass
class Node(Serializable):
    id: str
    x: float
    y: float
    z: float = 0.0
    support: SupportType = SupportType.FREE


@dataclass
class Member(Serializable):
    """One truss member. `onshape_id` is the provenance link back to the CAD
    entity, so an escalation can point at a real part rather than an index."""

    id: str
    start_node: str
    end_node: str
    section_id: str
    material_id: str
    onshape_id: str | None = None


@dataclass
class PointLoad(Serializable):
    id: str
    node_id: str
    fx: float = 0.0
    fy: float = 0.0
    fz: float = 0.0


@dataclass
class LoadCase(Serializable):
    id: str
    name: str
    point_loads: list[PointLoad] = field(default_factory=list)
    include_self_weight: bool = False


# --------------------------------------------------------------------------
# The change, and what it touches
# --------------------------------------------------------------------------

class ChangeKind(str, Enum):
    VARIABLE_EDIT = "variable_edit"     # variable table value changed
    FEATURE_EDIT = "feature_edit"       # member length / cross-section edited
    MEMBER_REMOVED = "member_removed"
    LOAD_ADDED = "load_added"
    LOAD_MOVED = "load_moved"


class EdgeKind(str, Enum):
    """Why one entity depends on another.

    Recorded per-edge so an escalation can explain *how* a member came to be
    affected. "Downstream via mate" reads very differently to an engineer than
    "downstream via shared variable".
    """

    MATE = "mate"                   # Onshape mate between parts
    WHERE_USED = "where_used"       # Onshape "where used" reference
    VARIABLE_REF = "variable_ref"   # both driven by the same variable
    TOPOLOGY = "topology"           # shares a node in the truss
    LOAD_PATH = "load_path"         # carries load redistributed from the change


@dataclass
class Edge(Serializable):
    source_id: str
    target_id: str
    kind: EdgeKind


@dataclass
class OnshapeRef(Serializable):
    """Where this came from, precisely enough to re-fetch or reset it."""

    document_id: str
    workspace_id: str
    element_id: str
    version_id: str | None = None
    branch_name: str | None = None   # read provenance only; no CAD writes exist


class TargetKind(str, Enum):
    MEMBER = "member"
    NODE = "node"
    LOAD = "load"
    VARIABLE = "variable"


@dataclass
class ChangeEvent(Serializable):
    id: str
    kind: ChangeKind
    description: str                 # human-readable; flows into the ECN
    target_id: str                   # member/node/variable the change lands on
    value_before: str | None = None
    value_after: str | None = None
    target_kind: TargetKind | None = None
    field_name: str | None = None
    numeric_before: float | None = None
    numeric_after: float | None = None
    node_before: str | None = None
    node_after: str | None = None
    load_case_id: str = "lc_1"

    def apply(self, graph: DependencyGraph) -> DependencyGraph:
        """Apply a typed SI delta to a copy, rejecting stale before values.

        Area edits preserve a geometrically similar section (I scales with
        area squared). Load moves carry explicit node IDs, not parsed prose.
        Legacy string-only events still deserialize, but cannot drive a solve.
        """
        graph.validate()
        if self.target_kind is None or self.field_name is None:
            raise ValueError("a typed change is required; strings are not solver input")
        for value in (self.numeric_before, self.numeric_after):
            if value is not None and (isinstance(value, bool) or not isfinite(value)):
                raise ValueError("change numbers must be finite")
        result = deepcopy(graph)
        result.change = deepcopy(self)

        def checked(current: float, *, positive: bool = False) -> float:
            if self.numeric_before is None or self.numeric_after is None:
                raise ValueError("numeric before and after values are required")
            if not isclose(current, self.numeric_before, rel_tol=1e-9, abs_tol=1e-12):
                raise ValueError(f"stale change: before={self.numeric_before}, actual={current}")
            if positive and self.numeric_after <= 0:
                raise ValueError("new value must be positive")
            return self.numeric_after

        if self.target_kind is TargetKind.MEMBER:
            member = result.member(self.target_id)
            if self.kind is ChangeKind.MEMBER_REMOVED and self.field_name == "present":
                if checked(1.0) != 0:
                    raise ValueError("member removal requires present: 1 -> 0")
                result.members.remove(member)
                result.edges = [e for e in result.edges
                                if self.target_id not in (e.source_id, e.target_id)]
            elif self.field_name == "area" and self.kind in (
                ChangeKind.FEATURE_EDIT, ChangeKind.VARIABLE_EDIT
            ):
                section = result.section(member.section_id)
                area = checked(section.area, positive=True)
                ratio = area / section.area
                new_id = f"section_{member.id}_{self.id}"
                result.sections.append(replace(
                    section, id=new_id, area=area, iy=section.iy * ratio**2,
                    iz=section.iz * ratio**2, j=section.j * ratio**2,
                ))
                member.section_id = new_id
            else:
                raise ValueError("unsupported member delta")
        elif self.target_kind is TargetKind.NODE and self.field_name in ("x", "y"):
            node = result.node(self.target_id)
            setattr(node, self.field_name, checked(getattr(node, self.field_name)))
        elif self.target_kind is TargetKind.LOAD:
            case = next((c for c in result.load_cases if c.id == self.load_case_id), None)
            if case is None:
                raise ValueError("unknown load case")
            load = next((p for p in case.point_loads if p.id == self.target_id), None)
            if self.kind is ChangeKind.LOAD_ADDED:
                if load is not None or self.field_name not in ("fx", "fy"):
                    raise ValueError("new load must have a unique ID and force component")
                result.node(self.node_after)
                value = checked(0.0)
                case.point_loads.append(PointLoad(
                    self.target_id, self.node_after, **{self.field_name: value}
                ))
            elif load is None:
                raise ValueError("unknown load")
            elif self.kind is ChangeKind.LOAD_MOVED and self.field_name == "node_id":
                if load.node_id != self.node_before:
                    raise ValueError("stale load location")
                result.node(self.node_after)
                load.node_id = self.node_after
            elif self.field_name in ("fx", "fy"):
                setattr(load, self.field_name, checked(getattr(load, self.field_name)))
            else:
                raise ValueError("unsupported load delta")
        elif self.target_kind is TargetKind.VARIABLE:
            if self.field_name in ("bayWidth", "bayHeight"):
                axis = "x" if self.field_name == "bayWidth" else "y"
                divisor = 2 if axis == "x" else 1
                current = max(getattr(n, axis) for n in result.nodes) / divisor
                ratio = checked(current, positive=True) / current
                for node in result.nodes:
                    setattr(node, axis, getattr(node, axis) * ratio)
            elif self.field_name == "barArea":
                sections = [result.section(m.section_id) for m in result.members]
                for section in {s.id: s for s in sections}.values():
                    area = checked(section.area, positive=True)
                    ratio = area / section.area
                    section.area, section.iy, section.iz, section.j = (
                        area, section.iy * ratio**2, section.iz * ratio**2,
                        section.j * ratio**2,
                    )
            else:
                raise ValueError("unsupported variable delta")
        else:
            raise ValueError("unsupported typed change")

        result.affected_member_ids = [m.id for m in result.members]
        result.affected_node_ids = [n.id for n in result.nodes]
        result.validate()
        return result


# --------------------------------------------------------------------------
# Top-level payload
# --------------------------------------------------------------------------

@dataclass
class DependencyGraph(Serializable):
    """Track A's output: one change, the structure it lands in, and every
    entity downstream of it."""

    id: str
    change: ChangeEvent
    source: OnshapeRef

    nodes: list[Node] = field(default_factory=list)
    members: list[Member] = field(default_factory=list)
    materials: list[Material] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)
    load_cases: list[LoadCase] = field(default_factory=list)

    edges: list[Edge] = field(default_factory=list)
    # A conservative superset. In this degree-two indeterminate benchmark,
    # the full structure is re-solved; traversal is not a completeness result.
    affected_member_ids: list[str] = field(default_factory=list)
    affected_node_ids: list[str] = field(default_factory=list)

    units: Units = field(default_factory=lambda: SI_UNITS)
    schema_version: str = CONTRACT_VERSION
    hard_floor: float = 1.0
    design_target: float = 1.5
    provenance: str = "unspecified"
    mapping_warnings: list[str] = field(default_factory=list)

    # -- lookups Track B will want -----------------------------------------

    def member(self, member_id: str) -> Member:
        for m in self.members:
            if m.id == member_id:
                return m
        raise KeyError(f"no member {member_id!r}")

    def node(self, node_id: str) -> Node:
        for n in self.nodes:
            if n.id == node_id:
                return n
        raise KeyError(f"no node {node_id!r}")

    def section(self, section_id: str) -> Section:
        for s in self.sections:
            if s.id == section_id:
                return s
        raise KeyError(f"no section {section_id!r}")

    def material(self, material_id: str) -> Material:
        for m in self.materials:
            if m.id == material_id:
                return m
        raise KeyError(f"no material {material_id!r}")

    def validate(self) -> None:
        """Fail loudly on a malformed graph rather than let Track B build a
        subtly wrong model out of it."""
        self.units.assert_si()
        if (not isfinite(self.hard_floor) or self.hard_floor < 1.0 or
                not isfinite(self.design_target) or self.design_target < self.hard_floor):
            raise ValueError("invalid hard_floor or design_target")
        if not self.nodes or not self.members:
            raise ValueError("structure needs nodes and members")
        node_ids = {n.id for n in self.nodes}
        member_ids = {m.id for m in self.members}
        section_ids = {s.id for s in self.sections}
        material_ids = {m.id for m in self.materials}

        if len(node_ids) != len(self.nodes):
            raise ValueError("duplicate node ids")
        if len(member_ids) != len(self.members):
            raise ValueError("duplicate member ids")
        if len(section_ids) != len(self.sections) or len(material_ids) != len(self.materials):
            raise ValueError("duplicate section or material ids")
        if node_ids & member_ids:
            raise ValueError("node and member IDs must be disjoint")
        for n in self.nodes:
            if not all(isfinite(v) for v in (n.x, n.y, n.z)):
                raise ValueError("nonfinite node coordinate")
        for s in self.sections:
            if not isfinite(s.area) or s.area <= 0:
                raise ValueError("section area must be finite and positive")
            if not all(isfinite(v) and v >= 0 for v in (s.iy, s.iz, s.j)):
                raise ValueError("invalid section inertia")
        for material in self.materials:
            if not all(isfinite(v) and v > 0 for v in
                       (material.elastic_modulus, material.yield_strength)):
                raise ValueError("material properties must be finite and positive")

        for m in self.members:
            for ref, pool, label in (
                (m.start_node, node_ids, "node"),
                (m.end_node, node_ids, "node"),
                (m.section_id, section_ids, "section"),
                (m.material_id, material_ids, "material"),
            ):
                if ref not in pool:
                    raise ValueError(
                        f"member {m.id!r} references unknown {label} {ref!r}"
                    )
            if m.start_node == m.end_node:
                raise ValueError(f"member {m.id!r} is degenerate (start == end)")

        for lc in self.load_cases:
            if len({p.id for p in lc.point_loads}) != len(lc.point_loads):
                raise ValueError("duplicate load IDs")
            for pl in lc.point_loads:
                if not all(isfinite(v) for v in (pl.fx, pl.fy, pl.fz)):
                    raise ValueError("nonfinite load")
                if pl.node_id not in node_ids:
                    raise ValueError(
                        f"load {pl.id!r} references unknown node {pl.node_id!r}"
                    )

        known = node_ids | member_ids
        for e in self.edges:
            for ref in (e.source_id, e.target_id):
                if ref not in known:
                    raise ValueError(f"edge references unknown entity {ref!r}")

        unknown = set(self.affected_member_ids) - member_ids
        if unknown:
            raise ValueError(
                f"affected_member_ids names unknown members: {sorted(unknown)}"
            )

        if all(n.support is SupportType.FREE for n in self.nodes):
            raise ValueError(
                "structure has no supports; the FEA model would be unconstrained"
            )
