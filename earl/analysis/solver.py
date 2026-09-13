"""PyNite adapter -- turns a DependencyGraph into member forces. [Track B, Sprint 1B]

This is the only module that talks to PyNite. Everything above it (sanity
checks, Biject, the gate, the benchmark) sees `SolveResult`, a plain, unit-
checked, sign-normalised record, so a future solver swap touches one file.

Why the adapter is more than a thin wrapper:

  * SIGN.  PyNite 3.2's `Member3D.axial()` is compression-positive (a bar
    pulled with +1000 N reports -1000).  Every force Track B emits is
    TENSION POSITIVE, so the adapter negates it once, here.
  * TRUSS MODELLING.  A truss is a frame with bending released at both ends
    of every member.  Once everything is released, nodal rotations have no
    stiffness, so RX/RY/RZ are restrained at every node.  A planar truss
    also has no stiffness out of its plane, so the constant coordinate axis
    (whichever of x/y/z is the same at every node) is restrained at every
    node too.  Only then is the contract support applied.  ROLLER_X /
    ROLLER_Y semantics assume gravity along -Y.
  * SECTION FLOOR.  Zero Iy/Iz/J makes the stiffness matrix singular even
    with releases, so they are floored at SECTION_PROPERTY_FLOOR.  Bending
    is released, so the floor cannot affect axial results.
  * SELF-WEIGHT IS LUMPED to the nodes (rho*A*L*g/2 per end), standard
    truss practice.  Axial force is then constant along every member,
    MemberForce stays single-valued, and the equilibrium check in sanity.py
    lumps exactly the same value.
  * LOAD SCALE IS THE COMBINATION FACTOR.  Point loads and lumped
    self-weight go into PyNite case `load_case_id` at 1x; the single load
    combination (also named `load_case_id`) carries `load_scale`, so scaling
    covers everything and the linearity check deviates by exactly 0.0.
  * INDEPENDENT STABILITY CHECK.  With the inertia floor, PyNite's own
    singularity check can return finite garbage for a mechanism.
    `check_stability()` assembles the truss stiffness on the translational
    DOFs with numpy and rank-tests it BEFORE PyNite is called.
"""

from __future__ import annotations

import contextlib
import importlib.metadata
import io
import math
from dataclasses import dataclass, field

import numpy as np

from ..contracts import DependencyGraph, Member, SupportType

try:
    from Pynite import FEModel3D
except ImportError as e:  # pragma: no cover - PyNite is a hard dependency
    raise ImportError("PyNiteFEA is required: pip install PyNiteFEA") from e


class SolverError(RuntimeError):
    """The structure could not be solved (unknown load case, empty model,
    instability, or a PyNite failure). Never an approval, never a finding."""


# Zero Iy/Iz/J make the stiffness matrix singular even with bending released;
# this floor keeps the matrix well-posed without affecting axial results.
SECTION_PROPERTY_FLOOR = 1e-12
POISSON_RATIO = 0.3
G_ACCEL = 9.80665          # m/s^2 -- contract density is kg/m^3, so g lives here

# Rank test tolerance for check_stability, relative to the largest diagonal.
STABILITY_RANK_TOL = 1e-10

SOLVER_NAME = "PyNite"


def _solver_version() -> str:
    try:
        return importlib.metadata.version("PyNiteFEA")
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover
        return "unknown"


SOLVER_VERSION = _solver_version()

_AXES = ("DX", "DY", "DZ")

# Translational restraints implied by each contract support type. Rotations
# are always restrained (truss), and the planar axis is added separately.
_SUPPORT_DOFS: dict[SupportType, frozenset[str]] = {
    SupportType.FREE: frozenset(),
    SupportType.PIN: frozenset({"DX", "DY", "DZ"}),
    SupportType.ROLLER_X: frozenset({"DY", "DZ"}),   # free to roll along X
    SupportType.ROLLER_Y: frozenset({"DX", "DZ"}),   # free to roll along Y
    SupportType.FIXED: frozenset({"DX", "DY", "DZ"}),
}


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------

@dataclass
class MemberForce:
    """Axial force in one member. TENSION POSITIVE, compression negative.
    `stress` = axial_force / area, same sign. SI throughout."""

    member_id: str
    axial_force: float
    stress: float
    area: float
    length: float


@dataclass
class NodeDisplacement:
    node_id: str
    dx: float
    dy: float
    dz: float


@dataclass
class Reaction:
    node_id: str
    fx: float
    fy: float
    fz: float


@dataclass
class SolveResult:
    """One solved load case of one graph, in SI, tension positive."""

    graph_id: str
    load_case_id: str
    member_forces: dict[str, MemberForce]
    displacements: dict[str, NodeDisplacement]
    reactions: dict[str, Reaction]
    solver: str = SOLVER_NAME
    solver_version: str = field(default=SOLVER_VERSION)
    include_self_weight: bool = False
    # The combination factor the loads were solved at (see build_model). The
    # equilibrium check needs it to scale the applied loads it re-derives
    # from the graph.
    load_scale: float = 1.0

    def force(self, member_id: str) -> MemberForce:
        try:
            return self.member_forces[member_id]
        except KeyError:
            raise KeyError(f"no force for member {member_id!r}") from None

    def max_abs_stress(self) -> tuple[str, float]:
        """(member_id, |stress|) of the most highly stressed member."""
        if not self.member_forces:
            raise ValueError("SolveResult carries no member forces")
        worst = max(self.member_forces.values(), key=lambda f: abs(f.stress))
        return worst.member_id, abs(worst.stress)


# --------------------------------------------------------------------------
# Geometry helpers (shared with sanity.py so both sides agree exactly)
# --------------------------------------------------------------------------

def member_vector(graph: DependencyGraph, member: Member) -> np.ndarray:
    """Vector from the member's start node to its end node (m)."""
    a = graph.node(member.start_node)
    b = graph.node(member.end_node)
    return np.array([b.x - a.x, b.y - a.y, b.z - a.z], dtype=float)


def member_length(graph: DependencyGraph, member: Member) -> float:
    return float(np.linalg.norm(member_vector(graph, member)))


def planar_axis(graph: DependencyGraph) -> str | None:
    """The translational DOF to restrain at every node for a planar truss.

    Whichever coordinate is constant across ALL nodes has no member stiffness
    in that direction, so it must be restrained or the model is a mechanism.
    Exactly one axis is chosen (a structure lying on a line would otherwise be
    over-restrained and never reported unstable); preference z, then y, then x
    -- z first because Node.z defaults to 0.0.
    """
    if not graph.nodes:
        return None
    for axis, attr in (("DZ", "z"), ("DY", "y"), ("DX", "x")):
        values = [getattr(n, attr) for n in graph.nodes]
        if max(values) - min(values) == 0.0:
            return axis
    return None


def restrained_dofs(graph: DependencyGraph) -> dict[str, set[str]]:
    """Translational DOFs the adapter restrains at each node: the contract
    support plus the planar axis. Rotations are always restrained and are not
    listed here."""
    planar = planar_axis(graph)
    out: dict[str, set[str]] = {}
    for n in graph.nodes:
        dofs = set(_SUPPORT_DOFS[n.support])
        if planar is not None:
            dofs.add(planar)
        out[n.id] = dofs
    return out


def lumped_self_weight(graph: DependencyGraph, member: Member) -> float:
    """Weight (N, positive) of one member; half goes to each end node."""
    rho = graph.material(member.material_id).density
    if rho <= 0.0:
        return 0.0
    area = graph.section(member.section_id).area
    return rho * area * member_length(graph, member) * G_ACCEL


def effective_self_weight(
    graph: DependencyGraph, load_case_id: str, include_self_weight: bool | None
) -> bool:
    """R8: the override is ADDITIVE only -- it can switch self-weight on for a
    case whose contract flag is off, never off. Also false when no material
    has a density, so a "self-weight" run never silently applies nothing."""
    lc = _load_case(graph, load_case_id)
    wanted = lc.include_self_weight or bool(include_self_weight)
    return wanted and any(m.density > 0.0 for m in graph.materials)


def _load_case(graph: DependencyGraph, load_case_id: str):
    for lc in graph.load_cases:
        if lc.id == load_case_id:
            return lc
    known = [lc.id for lc in graph.load_cases]
    raise SolverError(
        f"unknown load case {load_case_id!r} on graph {graph.id!r}; known: {known}"
    )


# --------------------------------------------------------------------------
# R4a: independent stability check
# --------------------------------------------------------------------------

def check_stability(graph: DependencyGraph) -> None:
    """Rank-test the truss stiffness on the free translational DOFs.

    K = sum over members of (E*A/L) n n^T on the 3 translational DOFs of each
    end node, with the DOFs the adapter restrains removed. A rank deficit
    means a zero-stiffness mode (a mechanism), and PyNite -- with the section
    floor keeping its matrix formally non-singular -- may not notice.
    Raises SolverError mentioning "unstable".
    """
    index = {n.id: i for i, n in enumerate(graph.nodes)}
    ndof = 3 * len(graph.nodes)
    K = np.zeros((ndof, ndof))

    for m in graph.members:
        L = member_length(graph, m)
        if L <= 0.0:
            raise SolverError(f"member {m.id!r} has zero length")
        E = graph.material(m.material_id).elastic_modulus
        A = graph.section(m.section_id).area
        if E <= 0.0 or A <= 0.0:
            raise SolverError(
                f"member {m.id!r} has non-positive stiffness (E={E}, A={A})"
            )
        n = member_vector(graph, m) / L
        k = (E * A / L) * np.outer(n, n)
        i, j = 3 * index[m.start_node], 3 * index[m.end_node]
        K[i:i + 3, i:i + 3] += k
        K[j:j + 3, j:j + 3] += k
        K[i:i + 3, j:j + 3] -= k
        K[j:j + 3, i:i + 3] -= k

    restrained = restrained_dofs(graph)
    free = [
        3 * index[n.id] + a
        for n in graph.nodes
        for a, axis in enumerate(_AXES)
        if axis not in restrained[n.id]
    ]
    if not free:
        return
    K_free = K[np.ix_(free, free)]
    scale = float(np.max(np.abs(np.diag(K_free))))
    if scale == 0.0:
        deficit = K_free.shape[0]
    else:
        rank = int(np.linalg.matrix_rank(K_free, tol=STABILITY_RANK_TOL * scale))
        deficit = K_free.shape[0] - rank
    if deficit > 0:
        raise SolverError(
            f"structure is unstable: {deficit} zero-stiffness mode(s) among "
            "free translational DOFs"
        )


# --------------------------------------------------------------------------
# Model construction and solve
# --------------------------------------------------------------------------

def build_model(
    graph: DependencyGraph,
    load_case_id: str,
    *,
    load_scale: float = 1.0,
    include_self_weight: bool | None = None,
) -> FEModel3D:
    """Pure construction -- no analysis. One PyNite load case and one load
    combination, both named `load_case_id`; the combination factor is
    `load_scale` (R3). Self-weight is lumped to the nodes (R2)."""
    lc = _load_case(graph, load_case_id)
    with_weight = effective_self_weight(graph, load_case_id, include_self_weight)
    restrained = restrained_dofs(graph)

    try:
        model = FEModel3D()

        for mat in graph.materials:
            E = mat.elastic_modulus
            G = E / (2.0 * (1.0 + POISSON_RATIO))
            model.add_material(mat.id, E, G, POISSON_RATIO, mat.density, fy=mat.yield_strength)

        for sec in graph.sections:
            model.add_section(
                sec.id,
                sec.area,
                max(sec.iy, SECTION_PROPERTY_FLOOR),
                max(sec.iz, SECTION_PROPERTY_FLOOR),
                max(sec.j, SECTION_PROPERTY_FLOOR),
            )

        for n in graph.nodes:
            model.add_node(n.id, n.x, n.y, n.z)
            dofs = restrained[n.id]
            model.def_support(
                n.id,
                support_DX="DX" in dofs,
                support_DY="DY" in dofs,
                support_DZ="DZ" in dofs,
                support_RX=True,
                support_RY=True,
                support_RZ=True,
            )

        for m in graph.members:
            model.add_member(m.id, m.start_node, m.end_node, m.material_id, m.section_id)
            # Truss: release bending at both ends.
            model.def_releases(m.id, Ryi=True, Rzi=True, Ryj=True, Rzj=True)

        for pl in lc.point_loads:
            for direction, value in (("FX", pl.fx), ("FY", pl.fy), ("FZ", pl.fz)):
                if value != 0.0:
                    model.add_node_load(pl.node_id, direction, value, case=load_case_id)

        if with_weight:
            for m in graph.members:
                w = lumped_self_weight(graph, m)
                if w > 0.0:
                    model.add_node_load(m.start_node, "FY", -w / 2.0, case=load_case_id)
                    model.add_node_load(m.end_node, "FY", -w / 2.0, case=load_case_id)

        model.add_load_combo(load_case_id, {load_case_id: load_scale})
    except SolverError:
        raise
    except Exception as e:  # any PyNite failure
        raise SolverError(f"PyNite failed: {type(e).__name__}: {e or 'no message'}") from e

    return model


def solve(
    graph: DependencyGraph,
    load_case_id: str,
    *,
    load_scale: float = 1.0,
    include_self_weight: bool | None = None,
) -> SolveResult:
    """Solve one load case. Raises SolverError for an unknown load case, an
    empty structure, an unstable structure (message contains "unstable") or
    any PyNite failure. Never mutates the graph."""
    graph.units.assert_si()
    graph.validate()
    if not graph.members or not graph.nodes:
        raise SolverError(f"graph {graph.id!r} has no members to analyse")
    if not math.isfinite(load_scale):
        raise SolverError(f"load_scale must be finite, got {load_scale!r}")

    with_weight = effective_self_weight(graph, load_case_id, include_self_weight)
    check_stability(graph)
    model = build_model(
        graph, load_case_id, load_scale=load_scale, include_self_weight=include_self_weight
    )

    captured = io.StringIO()
    try:
        with contextlib.redirect_stdout(captured):
            model.analyze_linear(log=False, check_stability=True, check_statics=False, sparse=True)
    except Exception as e:
        text = f"{type(e).__name__}: {e or 'no message'} {captured.getvalue().strip()}".strip()
        if "unstable" in text.lower() or "singular" in text.lower():
            raise SolverError("structure is unstable: " + text) from e
        raise SolverError("PyNite failed: " + text) from e

    combo = load_case_id
    try:
        member_forces: dict[str, MemberForce] = {}
        for m in graph.members:
            area = graph.section(m.section_id).area
            axial = -float(model.members[m.id].axial(0.0, combo))   # -> tension positive
            member_forces[m.id] = MemberForce(
                member_id=m.id,
                axial_force=axial,
                stress=axial / area,
                area=area,
                length=member_length(graph, m),
            )

        displacements: dict[str, NodeDisplacement] = {}
        reactions: dict[str, Reaction] = {}
        for n in graph.nodes:
            pn = model.nodes[n.id]
            displacements[n.id] = NodeDisplacement(
                n.id, float(pn.DX[combo]), float(pn.DY[combo]), float(pn.DZ[combo])
            )
            reactions[n.id] = Reaction(
                n.id,
                float(pn.RxnFX.get(combo, 0.0)),
                float(pn.RxnFY.get(combo, 0.0)),
                float(pn.RxnFZ.get(combo, 0.0)),
            )
    except Exception as e:
        raise SolverError(
            f"PyNite failed while reading results: {type(e).__name__}: {e or 'no message'}"
        ) from e

    for f in member_forces.values():
        if not math.isfinite(f.axial_force):
            raise SolverError(
                f"structure is unstable: non-finite axial force in member {f.member_id!r}"
            )

    return SolveResult(
        graph_id=graph.id,
        load_case_id=load_case_id,
        member_forces=member_forces,
        displacements=displacements,
        reactions=reactions,
        include_self_weight=with_weight,
        load_scale=load_scale,
    )
