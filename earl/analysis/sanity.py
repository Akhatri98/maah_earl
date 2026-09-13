"""Independent free-joint equilibrium and doubled-load checks on every solve."""

from __future__ import annotations

from math import hypot, isfinite

from earl.contracts import DependencyGraph, SanityChecks
from .solver import SolveResult, model_fingerprint, restrained_axes


def check(graph: DependencyGraph, result: SolveResult) -> SanityChecks:
    """Tolerances: residual <= max(1e-6 N, 1e-8*sum|P|); linearity 1e-9.

    Joint forces are reconstructed from signed member axial forces and unit
    vectors, not copied from the matrix solver's residual. Only free directions
    are tested; restrained directions carry support reactions. PyNite solves a
    second load combination with every applied load doubled, with no changed
    geometry, material, or section. A failure is an analysis ERROR.
    """
    graph.validate()
    result.validate()
    if result.model_fingerprint != model_fingerprint(graph, result.load_case_id):
        raise ValueError("sanity check model/result mismatch")
    case = next(c for c in graph.load_cases if c.id == result.load_case_id)
    residual = {n.id: [0.0, 0.0] for n in graph.nodes}
    load_scale = max(sum(abs(p.fx) + abs(p.fy) for p in case.point_loads), 1.0)
    for load in case.point_loads:
        residual[load.node_id][0] += load.fx
        residual[load.node_id][1] += load.fy
    for member in graph.members:
        start, end = graph.node(member.start_node), graph.node(member.end_node)
        length = hypot(end.x - start.x, end.y - start.y)
        force = result.axial_forces[member.id]
        vector = [force * (end.x - start.x) / length,
                  force * (end.y - start.y) / length]
        for axis in range(2):
            residual[start.id][axis] += vector[axis]
            residual[end.id][axis] -= vector[axis]
    maximum = max((abs(residual[n.id][axis]) for n in graph.nodes
                   for axis, fixed in enumerate(restrained_axes(n.support)) if not fixed),
                  default=0.0)
    stress_scale = max((abs(s) for s in result.stresses.values()), default=1.0)
    linearity_error = max((abs(result.doubled_stresses[mid] - 2 * stress) /
                           max(abs(2 * stress), 2 * stress_scale * 1e-12, 1.0)
                           for mid, stress in result.stresses.items()), default=0.0)
    return SanityChecks(
        equilibrium_ok=isfinite(maximum) and maximum <= max(1e-6, 1e-8 * load_scale),
        max_residual_force=maximum,
        linearity_ok=isfinite(linearity_error) and linearity_error <= 1e-9,
        max_linearity_relative_error=linearity_error,
        note="Free-joint force balance and independently recovered 2x-load stresses.",
    )


def combine(before: SanityChecks, after: SanityChecks) -> SanityChecks:
    return SanityChecks(
        equilibrium_ok=before.equilibrium_ok is True and after.equilibrium_ok is True,
        linearity_ok=before.linearity_ok is True and after.linearity_ok is True,
        max_residual_force=max(before.max_residual_force or 0, after.max_residual_force or 0),
        max_linearity_relative_error=max(before.max_linearity_relative_error or 0,
                                         after.max_linearity_relative_error or 0),
        note="Both before and after models checked; maxima cover both solves.",
    )
