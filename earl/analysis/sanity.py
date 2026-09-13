"""Free correctness tests that run on every scenario. [Track B, Sprint 1B]

plan.md (Validation): "Equilibrium (sum F = 0 at each joint) and linearity
checks (doubling a load should exactly double stress) run on every scenario
as free, built-in correctness tests."

These are cheap and independent of PyNite's own bookkeeping on purpose:

  * `equilibrium_check` re-derives the joint balance from the GRAPH geometry
    plus the member forces, applied loads and reactions in the SolveResult --
    never from PyNite's statics table. If the adapter got a sign, a node
    mapping or a lumped self-weight wrong, the joints do not balance and the
    check says so.
  * `linearity_check` solves twice (combination factor 1 and 2) and demands
    every stress double. Because the factor is applied in the load combination
    (solver.py, R3), the deviation is exactly 0.0 for a healthy linear model;
    anything else means a non-linear or stateful solver path crept in.

The result is the contract's `SanityChecks` block on the Decision, so the ECN
can show the engineer that the numbers behind an approval passed both.
"""

from __future__ import annotations

import numpy as np

from ..contracts import DependencyGraph, SanityChecks
from .solver import (
    SolveResult,
    SolverError,
    lumped_self_weight,
    member_vector,
    solve,
)

# Residual tolerance: relative to the largest applied load, with an absolute
# floor so an unloaded structure is not held to a zero tolerance.
EQUILIBRIUM_REL_TOL = 1e-6
EQUILIBRIUM_ABS_FLOOR = 1e-6           # N
LINEARITY_REL_TOL = 1e-9
# Members whose |stress| is below this are zero-force members: their ratio
# would be 0/0, so they are skipped by the linearity check.
LINEARITY_ZERO_STRESS = 1e-9           # Pa


def _load_case(graph: DependencyGraph, load_case_id: str):
    """The one load case carrying `load_case_id`.  A duplicate id is rejected
    (mirroring solver._load_case) rather than silently resolved to the first
    match: `equilibrium_check` takes a ready-made SolveResult, so it is the
    only path here that solve() has not already guarded, and balancing a
    result against the WRONG case's loads would report a spurious failure."""
    matches = [lc for lc in graph.load_cases if lc.id == load_case_id]
    if len(matches) > 1:
        raise ValueError(
            f"duplicate load case id {load_case_id!r} on graph {graph.id!r} "
            f"({len(matches)} load cases carry it)"
        )
    if not matches:
        raise ValueError(f"unknown load case {load_case_id!r} on graph {graph.id!r}")
    return matches[0]


def equilibrium_check(graph: DependencyGraph, result: SolveResult) -> tuple[bool, float]:
    """At EVERY node: member axial forces resolved into x/y/z (tension pulls
    the node toward the member's other end) + applied point loads +
    reactions (+ lumped self-weight when the result includes it) == 0.

    Returns (ok, max_residual_force). Computed from the graph geometry and the
    result, not from PyNite. Applied loads are scaled by `result.load_scale`
    to match the combination the result was solved at.
    """
    graph.units.assert_si()
    if result.graph_id != graph.id:
        raise ValueError(
            f"result is for graph {result.graph_id!r}, not {graph.id!r}"
        )
    lc = _load_case(graph, result.load_case_id)
    scale = result.load_scale

    residual: dict[str, np.ndarray] = {n.id: np.zeros(3) for n in graph.nodes}
    largest_load = 0.0

    for pl in lc.point_loads:
        load = scale * np.array([pl.fx, pl.fy, pl.fz], dtype=float)
        residual[pl.node_id] += load
        largest_load = max(largest_load, float(np.linalg.norm(load)))

    for m in graph.members:
        f = result.force(m.id)
        v = member_vector(graph, m)
        L = float(np.linalg.norm(v))
        if L == 0.0:
            raise ValueError(f"member {m.id!r} has zero length")
        n = v / L
        # Tension (positive) pulls the start node toward the end node and the
        # end node toward the start node.
        residual[m.start_node] += f.axial_force * n
        residual[m.end_node] -= f.axial_force * n
        if result.include_self_weight:
            w = scale * lumped_self_weight(graph, m)
            residual[m.start_node] += np.array([0.0, -w / 2.0, 0.0])
            residual[m.end_node] += np.array([0.0, -w / 2.0, 0.0])
            largest_load = max(largest_load, w / 2.0)

    for node_id, rxn in result.reactions.items():
        if node_id in residual:
            residual[node_id] += np.array([rxn.fx, rxn.fy, rxn.fz], dtype=float)

    max_residual = max(
        (float(np.linalg.norm(r)) for r in residual.values()), default=0.0
    )
    tol = max(EQUILIBRIUM_ABS_FLOOR, EQUILIBRIUM_REL_TOL * largest_load)
    return max_residual <= tol, max_residual


def linearity_check(
    graph: DependencyGraph,
    load_case_id: str,
    *,
    solve_fn=solve,
    include_self_weight: bool | None = None,
) -> tuple[bool, float]:
    """Solve at scale 1 and scale 2; every member stress must double.

    Returns (ok, max_relative_deviation) where the deviation of a member is
    |stress_2 / (2 stress_1) - 1|. Members with |stress_1| below
    LINEARITY_ZERO_STRESS are skipped. `include_self_weight` is forwarded to
    `solve_fn` only when given, so a plain (graph, load_case_id, load_scale=)
    callable still works as an injected solver.
    """
    graph.units.assert_si()
    extra = {} if include_self_weight is None else {"include_self_weight": include_self_weight}
    one = solve_fn(graph, load_case_id, load_scale=1.0, **extra)
    two = solve_fn(graph, load_case_id, load_scale=2.0, **extra)

    worst = 0.0
    for member_id, f1 in one.member_forces.items():
        if abs(f1.stress) < LINEARITY_ZERO_STRESS:
            continue
        f2 = two.force(member_id)
        deviation = abs(f2.stress / (2.0 * f1.stress) - 1.0)
        worst = max(worst, deviation)
    return worst <= LINEARITY_REL_TOL, worst


def run_sanity_checks(
    graph: DependencyGraph, load_case_id: str, result: SolveResult
) -> SanityChecks:
    """Both checks on one solved load case, packaged for the Decision.

    The linearity check re-solves the same self-weight variant the result was
    produced with (R8: the override is additive, so passing the result's flag
    back in can only reproduce it, never drop it). A check that cannot run
    is reported as None with the reason in `note`, never as a pass.
    """
    if result.load_case_id != load_case_id:
        raise ValueError(
            f"result is for load case {result.load_case_id!r}, not {load_case_id!r}"
        )
    notes: list[str] = []

    eq_ok: bool | None
    residual: float | None
    try:
        eq_ok, residual = equilibrium_check(graph, result)
        if not eq_ok:
            notes.append(f"equilibrium residual {residual:.3e} N exceeds tolerance")
    except (ValueError, KeyError) as e:
        eq_ok, residual = None, None
        notes.append(f"equilibrium check not run: {type(e).__name__}: {e}")

    lin_ok: bool | None
    try:
        lin_ok, deviation = linearity_check(
            graph,
            load_case_id,
            include_self_weight=True if result.include_self_weight else None,
        )
        if not lin_ok:
            notes.append(f"linearity deviation {deviation:.3e} exceeds tolerance")
    except (SolverError, ValueError) as e:
        lin_ok = None
        notes.append(f"linearity check not run: {type(e).__name__}: {e}")

    return SanityChecks(
        equilibrium_ok=eq_ok,
        max_residual_force=residual,
        linearity_ok=lin_ok,
        note="; ".join(notes) if notes else None,
    )
