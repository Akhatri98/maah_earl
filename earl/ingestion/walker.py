"""Dependency graph walker. [Track A, Sprint 2A]

Given a change and the graph it lands in, find every entity downstream of it.

This is the module plan.md's completeness claim rests on:

    "Graph traversal is a fixed algorithm, not a model decision -- it walks
     every dependency by construction, so completeness doesn't depend on the
     model remembering to check something several hops away."

So two properties matter more than anything else here:

**Completeness.** Reachability is computed by breadth-first search over the
edges, not chosen. Nothing is filtered out on a judgement call about whether a
member "looks" relevant. If an edge exists, the walk crosses it.

**Determinism.** Same graph plus same change gives byte-identical output every
run: adjacency is sorted, the frontier is processed in sorted order, and ties
in distance resolve by sorted id. The baseline this project is measured
against is an LLM that may answer differently across runs, so "the same way
every time" has to be a property of the code, not a hope.

## On a truss, nearly everything is affected -- and that is correct

The 10-bar truss is statically indeterminate and fully connected, so changing
one member's area really does redistribute force into every other member.
A reachability walk marks all ten affected, and that is the physically honest
answer, not a bug. Being conservative here is also the safe direction for the
headline metric: a dropped domino is a member whose unsafe state was NOT
reported, so over-reporting never costs a domino.

What that means is the *discriminating* information is not set membership --
it is `distance` and `via`: how many hops the effect travelled and what kind
of dependency carried it. "m9 shares node n3 with the change" reads very
differently in an ECN to "m6 is four hops away via load path", and the demo's
graph lights up in rings rather than all at once. `WalkResult` therefore keeps
both: the flat set the contract wants, and the provenance that makes it
explainable.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from ..contracts.graph import (
    ChangeKind,
    DependencyGraph,
    EdgeKind,
)


# Change kinds whose target is legitimately not an entity in the structure:
# a variable never was one, and a removed member has just stopped being one.
_MAY_TARGET_NON_ENTITY = frozenset(
    {ChangeKind.VARIABLE_EDIT, ChangeKind.MEMBER_REMOVED}
)


@dataclass(frozen=True)
class Reach:
    """How the walk arrived at one entity.

    `distance` is hops from the change, so 0 is the change target itself.
    `via` is the edge kind that FIRST reached it -- the shortest path's last
    edge -- which is what an escalation cites when explaining the link.
    """

    entity_id: str
    distance: int
    via: EdgeKind | None          # None for a seed
    path: tuple[str, ...] = ()    # seed -> ... -> entity_id

    @property
    def is_seed(self) -> bool:
        return self.distance == 0


@dataclass
class WalkResult:
    """Everything the traversal found, with the provenance behind it."""

    seeds: tuple[str, ...] = ()
    reached: dict[str, Reach] = field(default_factory=dict)
    member_ids: list[str] = field(default_factory=list)
    node_ids: list[str] = field(default_factory=list)
    unreached_member_ids: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def reach(self, entity_id: str) -> Reach | None:
        return self.reached.get(entity_id)

    def distance(self, entity_id: str) -> int | None:
        found = self.reached.get(entity_id)
        return found.distance if found else None

    def at_distance(self, distance: int) -> list[str]:
        """Entities exactly this many hops out -- one ring of the demo viz."""
        return sorted(
            (r.entity_id for r in self.reached.values() if r.distance == distance),
            key=_entity_sort_key,
        )

    @property
    def max_distance(self) -> int:
        return max((r.distance for r in self.reached.values()), default=0)

    def explain(self, entity_id: str) -> str:
        """One human-readable line for the ECN."""
        found = self.reached.get(entity_id)
        if found is None:
            return f"{entity_id} is not downstream of the change"
        if found.is_seed:
            return f"{entity_id} is the change target"
        return (
            f"{entity_id} is {found.distance} hop(s) downstream via "
            f"{found.via.value} ({' -> '.join(found.path)})"
        )


# --------------------------------------------------------------------------
# Seeding
# --------------------------------------------------------------------------

def seed_ids(graph: DependencyGraph) -> tuple[list[str], list[str]]:
    """Where the walk starts, and any complaints about starting there.

    The change target is usually a member or node id, but two change kinds
    legitimately name something that is not in the structure:

    * a VARIABLE_EDIT names a variable, which was never an entity;
    * a MEMBER_REMOVED names the member it just deleted, which is no longer
      one -- the whole point of the change.

    The contract allows both (`validate()` admits `change.target_id` as an
    edge endpoint), and the edges that hang off the target are what the walk
    follows: VARIABLE_REF edges from the changed variable to what it drives,
    or the removed member's surviving TOPOLOGY edges to the neighbours that
    now carry its load. Any OTHER kind naming a non-entity is a real problem
    and is reported.
    """
    target = graph.change.target_id
    member_ids = {m.id for m in graph.members}
    node_ids = {n.id for n in graph.nodes}
    problems: list[str] = []

    if target in member_ids or target in node_ids:
        return [target], problems

    # Not an entity -- only valid if something actually hangs off it.
    outgoing = [e for e in graph.edges if e.source_id == target]
    if outgoing:
        if graph.change.kind not in _MAY_TARGET_NON_ENTITY:
            expected = " or ".join(sorted(k.value for k in _MAY_TARGET_NON_ENTITY))
            problems.append(
                f"change target {target!r} is not a member or node, but the "
                f"change kind is {graph.change.kind.value}, not {expected}"
            )
        return [target], problems

    problems.append(
        f"change target {target!r} matches no member, no node, and no edge "
        "source; the walk has nothing to start from and would report nothing "
        "affected"
    )
    return [], problems


# --------------------------------------------------------------------------
# The walk
# --------------------------------------------------------------------------

def walk(
    graph: DependencyGraph,
    *,
    max_hops: int | None = None,
    edge_kinds: frozenset[EdgeKind] | None = None,
) -> WalkResult:
    """Breadth-first traversal from the change to everything downstream.

    `max_hops` and `edge_kinds` exist for the demo visualisation and for
    experiments, NOT as a default filter: both default to unrestricted,
    because a walk that quietly stops early is how a domino gets dropped.
    Restricting either is recorded in `notes` so a narrowed walk can never be
    mistaken for a complete one.
    """
    result = WalkResult()

    seeds, problems = seed_ids(graph)
    result.notes.extend(problems)
    result.seeds = tuple(seeds)

    if max_hops is not None:
        result.notes.append(
            f"walk was limited to {max_hops} hop(s); this is NOT a complete "
            "reachability result"
        )
    if edge_kinds is not None:
        result.notes.append(
            "walk followed only "
            f"{sorted(k.value for k in edge_kinds)} edges; this is NOT a "
            "complete reachability result"
        )

    # Sorted adjacency keeps the traversal order -- and therefore the whole
    # result -- identical across runs.
    adjacency: dict[str, list[tuple[str, EdgeKind]]] = {}
    for edge in graph.edges:
        if edge_kinds is not None and edge.kind not in edge_kinds:
            continue
        adjacency.setdefault(edge.source_id, []).append((edge.target_id, edge.kind))
    for targets in adjacency.values():
        targets.sort(key=lambda t: (_entity_sort_key(t[0]), t[1].value))

    for seed in sorted(seeds, key=_entity_sort_key):
        result.reached[seed] = Reach(seed, 0, None, (seed,))

    frontier: deque[str] = deque(sorted(seeds, key=_entity_sort_key))
    while frontier:
        current = frontier.popleft()
        here = result.reached[current]
        if max_hops is not None and here.distance >= max_hops:
            continue
        for neighbour, kind in adjacency.get(current, []):
            if neighbour in result.reached:
                continue
            result.reached[neighbour] = Reach(
                entity_id=neighbour,
                distance=here.distance + 1,
                via=kind,
                path=here.path + (neighbour,),
            )
            frontier.append(neighbour)

    member_ids = {m.id for m in graph.members}
    node_ids = {n.id for n in graph.nodes}

    result.member_ids = sorted(
        (e for e in result.reached if e in member_ids), key=_entity_sort_key
    )
    result.node_ids = sorted(
        (e for e in result.reached if e in node_ids), key=_entity_sort_key
    )

    # Nodes are reached only if the graph carries node-level edges; today's
    # edges are member-to-member, so derive the touched nodes from the
    # affected members. Without this, `affected_node_ids` is empty and the
    # demo has no nodes to light up.
    if not result.node_ids and result.member_ids:
        touched: set[str] = set()
        for member_id in result.member_ids:
            member = graph.member(member_id)
            touched.update((member.start_node, member.end_node))
        result.node_ids = sorted(touched, key=_entity_sort_key)

    result.unreached_member_ids = sorted(
        member_ids - set(result.member_ids), key=_entity_sort_key
    )
    return result


def apply_walk(graph: DependencyGraph, result: WalkResult) -> None:
    """Write the traversal's findings onto the graph the contract hands over.

    Kept separate from `walk()` so the traversal itself has no side effects
    and can be run for inspection without mutating anything.
    """
    graph.affected_member_ids = list(result.member_ids)
    graph.affected_node_ids = list(result.node_ids)


def walk_and_apply(graph: DependencyGraph, **kwargs) -> WalkResult:
    """Walk the graph and populate its affected sets, then re-validate.

    Re-validating matters: `affected_member_ids` naming an unknown member is
    one of the things `DependencyGraph.validate()` catches, and this is the
    step that populates it.
    """
    result = walk(graph, **kwargs)
    apply_walk(graph, result)
    graph.validate()
    return result


def _entity_sort_key(entity_id: str) -> tuple[str, int, str]:
    """Order m2 before m10, and keep members, nodes and anything else in
    stable groups rather than interleaved lexicographically."""
    prefix = entity_id[0] if entity_id else ""
    digits = "".join(c for c in entity_id if c.isdigit())
    return (prefix, int(digits) if digits else 0, entity_id)
