"""Axial yield and Euler column capacity under explicitly simplified assumptions."""

from dataclasses import dataclass
from math import hypot, isfinite, pi, sqrt

from earl.contracts import DependencyGraph, Serializable


@dataclass(frozen=True)
class CapacityResult(Serializable):
    member_id: str
    force_capacity: float | None
    stress_capacity: float | None
    yield_capacity: float
    euler_capacity: float | None
    safety_factor: float | None
    mode: str
    slenderness: float | None
    zero_demand: bool = False
    note: str = ""


def member_capacity(graph: DependencyGraph, member_id: str, axial_force: float,
                    *, effective_length_factor: float = 1.0) -> CapacityResult:
    """Use min(Fy*A, pi^2*E*Imin/(K*L)^2) in compression; Fy*A in tension.

    K=1 assumes ideal pin-ended, straight members with full-length unsupported
    buckling about either principal axis. Slenderness is K*L/sqrt(Imin/A).
    Solid circular sections are declared by the CAD map, not inferred here.
    Missing inertia is NOT_EVALUATED, not infinite strength or a fake failure.
    Euler buckling is elastic only; the minimum with yield is not an AISC
    column curve. There are no imperfections, joint/local buckling checks,
    residual stress, lateral bracing, or code load factors. This is not AISC
    or ASCE compliant structural design.
    """
    graph.validate()
    if not isfinite(axial_force) or not isfinite(effective_length_factor) or effective_length_factor <= 0:
        raise ValueError("invalid force or effective length factor")
    member = graph.member(member_id)
    section = graph.section(member.section_id)
    material = graph.material(member.material_id)
    start, end = graph.node(member.start_node), graph.node(member.end_node)
    length = hypot(end.x - start.x, end.y - start.y)
    if length <= 0:
        raise ValueError("member length must be positive")
    yield_capacity = material.yield_strength * section.area
    inertia = min(section.iy, section.iz)
    if inertia <= 0:
        return CapacityResult(member_id, None, None, yield_capacity, None, None,
                              "not_evaluated", None,
                              note="Missing section inertia; buckling capacity is unknown.")
    slenderness = effective_length_factor * length / sqrt(inertia / section.area)
    euler = pi**2 * material.elastic_modulus * inertia / (effective_length_factor * length)**2
    capacity = min(yield_capacity, euler) if axial_force < 0 else yield_capacity
    mode = "euler_buckling" if axial_force < 0 and euler < yield_capacity else "yield"
    zero = axial_force == 0
    return CapacityResult(
        member_id, capacity, capacity / section.area, yield_capacity, euler,
        None if zero else capacity / abs(axial_force), mode, slenderness, zero,
        "Zero demand: safety factor is unbounded." if zero else "",
    )


def capacities(graph: DependencyGraph, forces: dict[str, float]) -> dict[str, CapacityResult]:
    graph.validate()
    return {mid: member_capacity(graph, mid, force) for mid, force in forces.items()}
