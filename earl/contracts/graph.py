"""Dependency graph -- the payload Track A hands to Track B.

Produced by: earl.ingestion (Onshape)      [Track A]
Consumed by: earl.analysis (PyNite/Biject) [Track B]

Design rule: this object must be SUFFICIENT TO BUILD THE FEA MODEL on its own.
Track B should never have to call back into Onshape to find a coordinate, a
cross-sectional area or a load. If Track B needs a value in order to solve, it
belongs here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

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
    """Material properties, all stresses in the payload's `units`.

    `yield_strength` is the material's yield (Fy) and means exactly that: it
    is what a design-code check (SkyCiv) and a buckling check use.

    `allowable_stress` is the DESIGN allowable the member is checked against.
    When it is set, Biject computes member capacity from it; when it is None,
    capacity falls back to `yield_strength`. It exists because the two tracks
    once disagreed by exactly 2x about which of the two numbers belonged in
    `yield_strength` (the 10-bar benchmark publishes a 25 ksi allowable for a
    50 ksi material) -- the Pa-vs-MPa silent failure one level up. Carrying
    both makes the convention explicit instead of a matter of who built the
    graph.
    """

    id: str
    name: str
    elastic_modulus: float    # E
    yield_strength: float     # Fy -- design-code checks and buckling use this
    density: float = 0.0      # 0 = ignore self-weight
    # Design allowable; Biject uses it for capacity when set (see docstring).
    allowable_stress: float | None = None

    @property
    def design_stress(self) -> float:
        """The stress member capacity is computed from: the allowable when
        declared, else yield. Consumers should read this, not pick a field."""
        return (
            self.allowable_stress
            if self.allowable_stress is not None
            else self.yield_strength
        )


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
    branch_name: str | None = None   # evaluation runs on a branch, never main


@dataclass
class ChangeEvent(Serializable):
    id: str
    kind: ChangeKind
    description: str                 # human-readable; flows into the ECN
    target_id: str                   # member/node/variable the change lands on
    value_before: str | None = None
    value_after: str | None = None


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
    # Computed by the traversal algorithm, NOT by a model. The completeness
    # claim in plan.md rests on these being produced by fixed graph walking.
    affected_member_ids: list[str] = field(default_factory=list)
    affected_node_ids: list[str] = field(default_factory=list)

    units: Units = field(default_factory=lambda: SI_UNITS)
    schema_version: str = CONTRACT_VERSION

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
        node_ids = {n.id for n in self.nodes}
        member_ids = {m.id for m in self.members}
        section_ids = {s.id for s in self.sections}
        material_ids = {m.id for m in self.materials}

        if len(node_ids) != len(self.nodes):
            raise ValueError("duplicate node ids")
        if len(member_ids) != len(self.members):
            raise ValueError("duplicate member ids")

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
            for pl in lc.point_loads:
                if pl.node_id not in node_ids:
                    raise ValueError(
                        f"load {pl.id!r} references unknown node {pl.node_id!r}"
                    )

        known = node_ids | member_ids | {self.change.target_id}
        for e in self.edges:
            for ref in (e.source_id, e.target_id):
                if ref not in known:
                    raise ValueError(f"edge references unknown entity {ref!r}")

        unknown = set(self.affected_member_ids) - member_ids
        if unknown:
            raise ValueError(
                f"affected_member_ids names unknown members: {sorted(unknown)}"
            )

        for mat in self.materials:
            if mat.allowable_stress is not None and mat.allowable_stress <= 0.0:
                raise ValueError(
                    f"material {mat.id!r} declares non-positive allowable "
                    f"stress {mat.allowable_stress}"
                )
            if (
                mat.allowable_stress is not None
                and mat.yield_strength > 0.0
                and mat.allowable_stress > mat.yield_strength
            ):
                raise ValueError(
                    f"material {mat.id!r} declares allowable stress "
                    f"{mat.allowable_stress} above its yield {mat.yield_strength}"
                )

        if all(n.support is SupportType.FREE for n in self.nodes):
            raise ValueError(
                "structure has no supports; the FEA model would be unconstrained"
            )
