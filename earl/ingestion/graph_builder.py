"""Assemble a contract-valid DependencyGraph from Onshape reads. [Track A, Sprint 2A]

This is the join that was missing: `parsers.py` recovers CAD structure, and
`benchmark.py` supplies the supports and loads CAD does not carry, but nothing
turned the two into the `DependencyGraph` the contract requires.

## The id-space trap this exists to close

Onshape speaks in *occurrence ids* (`M5SODCTacczpSVpUi`). The contract speaks
in *truss ids* (`m1`, `n3`). `mate_edges()` emits occurrence ids, because that
is what mates reference -- but `DependencyGraph.validate()` checks every edge
endpoint against the member and node ids, so handing it raw occurrence ids
fails validation.

Translation therefore happens exactly once, here, in `translate_edges()`.
An occurrence with no known member is reported rather than dropped: a silently
discarded edge is a dependency the traversal will never walk, which is the
dropped domino this project exists to catch.

Traversal itself is deliberately NOT here -- the walker is the other half of
Sprint 2A and builds on this.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..contracts.graph import (
    ChangeEvent,
    ChangeKind,
    DependencyGraph,
    Edge,
    EdgeKind,
    Member,
    Node,
    OnshapeRef,
)
from .benchmark import BenchmarkSpec, benchmark_spec, variable_drives
from .parsers import (
    Instance,
    Mate,
    MateConnector,
    TrussTopology,
    derive_topology,
    mate_edges,
    parse_instances,
    parse_mate_connectors,
    parse_mates,
)


@dataclass
class IdMap:
    """Translation between Onshape occurrence ids and truss member ids.

    Built from part ids, which both sides carry: an instance knows the part it
    is an occurrence of, and a mate connector knows the part it sits on.
    """

    occurrence_to_member: dict[str, str] = field(default_factory=dict)
    member_to_occurrence: dict[str, str] = field(default_factory=dict)
    unmapped_occurrences: list[str] = field(default_factory=list)

    def member_for(self, occurrence_id: str) -> str | None:
        return self.occurrence_to_member.get(occurrence_id)


def build_id_map(instances: list[Instance], topology: TrussTopology) -> IdMap:
    """Map occurrence ids to member ids via the part id they share."""
    part_to_member = {pid: mid for mid, pid in topology.member_part_ids.items()}
    id_map = IdMap()
    for inst in instances:
        member_id = part_to_member.get(inst.part_id)
        if member_id is None:
            id_map.unmapped_occurrences.append(inst.id)
            continue
        id_map.occurrence_to_member[inst.id] = member_id
        id_map.member_to_occurrence[member_id] = inst.id
    return id_map


def translate_edges(edges: list[Edge], id_map: IdMap) -> tuple[list[Edge], list[str]]:
    """Rewrite occurrence-id edges into member-id edges.

    Returns the translated edges and a list of complaints about endpoints that
    could not be translated. Untranslatable edges are dropped from the result
    but never silently: the caller decides whether to fail.
    """
    out: list[Edge] = []
    problems: list[str] = []
    seen: set[tuple[str, str, str]] = set()

    for edge in edges:
        source = id_map.member_for(edge.source_id)
        target = id_map.member_for(edge.target_id)
        if source is None or target is None:
            unknown = [
                raw for raw, mapped in
                ((edge.source_id, source), (edge.target_id, target))
                if mapped is None
            ]
            problems.append(
                f"{edge.kind.value} edge {edge.source_id} -> {edge.target_id} "
                f"references occurrence(s) with no member: {unknown}"
            )
            continue
        if source == target:
            continue
        key = (source, target, edge.kind.value)
        if key not in seen:
            seen.add(key)
            out.append(Edge(source, target, edge.kind))
    return out, problems


def variable_ref_edges(
    change: ChangeEvent,
    member_ids: list[str],
    node_ids: list[str],
) -> tuple[list[Edge], list[str]]:
    """VARIABLE_REF edges from a changed variable to what it drives.

    A variable is not a member or a node, so it is not part of the structure
    -- but `DependencyGraph.validate()` admits `change.target_id` as an edge
    endpoint precisely so a change can hang off something that is not an
    entity. That makes the changed variable a legal seed for the walk.

    Only the variable that ACTUALLY changed gets edges. Emitting them for
    every variable in the table would reference ids the contract does not
    know, and validation would reject the graph.
    """
    if change.kind is not ChangeKind.VARIABLE_EDIT:
        return [], []

    # A variable edit whose target is already a member or node needs no
    # bridging edges -- the walk seeds straight from the entity. This happens
    # when the caller has already resolved the variable to what it drives.
    if change.target_id in set(member_ids) | set(node_ids):
        return [], []

    drives = variable_drives(change.target_id)
    if not drives:
        return [], [
            f"variable {change.target_id!r} changed, but nothing declares what "
            "it drives (see VARIABLE_DRIVES in benchmark.py) and it is not a "
            "member or node either; the walk would start from it and reach "
            "nothing"
        ]

    edges: list[Edge] = []
    if "members" in drives:
        edges += [
            Edge(change.target_id, m, EdgeKind.VARIABLE_REF) for m in member_ids
        ]
    if "nodes" in drives:
        edges += [
            Edge(change.target_id, n, EdgeKind.VARIABLE_REF) for n in node_ids
        ]
    return edges, []


def topology_edges(topology: TrussTopology) -> list[Edge]:
    """TOPOLOGY edges between members that share a node.

    Mates alone do not carry this. Nine Fastened mates fully constrain ten
    parts, so they form a spanning tree -- fewer edges than the truss really
    has. Two members meeting at a node genuinely depend on each other whether
    or not a mate happens to say so, and without these the traversal would
    miss real load paths.
    """
    members_at: dict[str, list[str]] = {}
    for member_id, ends in topology.member_ends.items():
        for node_id in ends:
            members_at.setdefault(node_id, []).append(member_id)

    edges: list[Edge] = []
    seen: set[tuple[str, str]] = set()
    for sharers in members_at.values():
        for i, a in enumerate(sorted(sharers)):
            for b in sorted(sharers)[i + 1:]:
                for pair in ((a, b), (b, a)):
                    if pair not in seen:
                        seen.add(pair)
                        edges.append(Edge(pair[0], pair[1], EdgeKind.TOPOLOGY))
    return edges


@dataclass
class GraphBuildResult:
    """The graph, plus everything that did not fit cleanly.

    `problems` is not decoration. A graph that built "fine" while quietly
    discarding an edge or a member is the exact failure mode the headline
    metric counts, so the caller is handed the complaints explicitly.
    """

    graph: DependencyGraph
    id_map: IdMap
    topology: TrussTopology
    problems: list[str] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return not self.problems


def build_graph(
    assembly: dict,
    *,
    graph_id: str,
    change: ChangeEvent,
    source: OnshapeRef,
    spec: BenchmarkSpec | None = None,
    strict: bool = True,
) -> GraphBuildResult:
    """Assemble a DependencyGraph from one assembly-definition response.

    The response must have been fetched with `includeMateConnectors=true`;
    without it the connector list is empty and no topology can be recovered.

    With `strict` (the default) an incomplete or lossy build raises rather
    than returning a graph that is quietly missing structure.
    """
    spec = spec or benchmark_spec()
    problems: list[str] = []

    instances = parse_instances(assembly)
    connectors: list[MateConnector] = parse_mate_connectors(assembly)
    mates: list[Mate] = parse_mates(assembly)

    topology = derive_topology(connectors)
    problems.extend(topology.unresolved)

    id_map = build_id_map(instances, topology)
    if id_map.unmapped_occurrences:
        problems.append(
            "occurrences with no matching member (their part carries no mate "
            f"connectors): {sorted(id_map.unmapped_occurrences)}"
        )

    mate_only, mate_problems = translate_edges(mate_edges(mates), id_map)
    problems.extend(mate_problems)

    # Mate edges and topology edges overlap by design: a mate at a node is
    # also a shared node. Keep the MATE label where one exists, since it is
    # the stronger provenance claim for an escalation to cite.
    mate_pairs = {(e.source_id, e.target_id) for e in mate_only}
    edges = mate_only + [
        e for e in topology_edges(topology)
        if (e.source_id, e.target_id) not in mate_pairs
    ]

    var_edges, var_problems = variable_ref_edges(
        change,
        sorted(topology.member_ends, key=_member_sort_key),
        sorted(topology.node_positions),
    )
    edges += var_edges
    problems.extend(var_problems)

    nodes = [
        Node(
            id=node_id,
            x=pos[0],
            y=pos[1],
            z=pos[2],
            support=spec.support_for(node_id),
        )
        for node_id, pos in sorted(topology.node_positions.items())
    ]

    members = [
        Member(
            id=member_id,
            start_node=ends[0],
            end_node=ends[1],
            section_id=spec.section.id,
            material_id=spec.material.id,
            onshape_id=id_map.member_to_occurrence.get(member_id),
        )
        for member_id, ends in sorted(
            topology.member_ends.items(),
            key=lambda kv: _member_sort_key(kv[0]),
        )
    ]

    graph = DependencyGraph(
        id=graph_id,
        change=change,
        source=source,
        nodes=nodes,
        members=members,
        materials=[spec.material],
        sections=[spec.section],
        load_cases=[spec.load_case],
        edges=edges,
    )

    # Loads reference nodes by name; if the CAD recovered different node ids
    # than the spec expects, the load lands nowhere. Catch it here rather than
    # letting Track B solve an unloaded structure and report it safe.
    node_ids = {n.id for n in nodes}
    for load in spec.load_case.point_loads:
        if load.node_id not in node_ids:
            problems.append(
                f"load {load.id!r} targets node {load.node_id!r}, which the CAD "
                f"read did not produce (got {sorted(node_ids)})"
            )
    for node_id in spec.supports:
        if node_id not in node_ids:
            problems.append(
                f"support declared at node {node_id!r}, which the CAD read did "
                "not produce"
            )

    result = GraphBuildResult(
        graph=graph, id_map=id_map, topology=topology, problems=problems
    )

    if strict and problems:
        raise ValueError(
            "dependency graph build was lossy:\n  - " + "\n  - ".join(problems)
        )

    graph.validate()
    return result


def _member_sort_key(member_id: str) -> tuple[int, str]:
    """Sort m2 before m10 rather than lexicographically."""
    digits = "".join(c for c in member_id if c.isdigit())
    return (int(digits) if digits else 0, member_id)
