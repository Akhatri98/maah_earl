"""PyNite linear, pin-jointed 2D truss adapter; all boundary values are SI.

Positive axial force/stress means tension. PyNite uses the opposite axial
sign, so it is inverted exactly once here. Rotational DOFs and out-of-plane
translation are restrained; bending moments are released at both member ends.
There is no stabilizing spring, substitute solver, or fabricated error result.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from importlib.metadata import version
from math import dist, isfinite
from pathlib import Path

from earl.contracts import DependencyGraph, SanityChecks, Serializable, SupportType

SOLVER_VERSION = version("PyNiteFEA")
MAX_SOLVE_SECONDS = 12.0


class SolveError(RuntimeError):
    """An analysis could not be completed; the caller must emit Outcome.ERROR."""


def restrained_axes(support: SupportType) -> tuple[bool, bool]:
    if support in (SupportType.PIN, SupportType.FIXED):
        return True, True
    if support is SupportType.ROLLER_X:
        return False, True
    if support is SupportType.ROLLER_Y:
        return True, False
    return False, False


def model_fingerprint(graph: DependencyGraph, load_case_id: str) -> str:
    graph.validate()
    member_sections = {m.section_id for m in graph.members}
    member_materials = {m.material_id for m in graph.members}
    body = {
        "units": asdict(graph.units),
        "nodes": [n.to_dict() for n in graph.nodes],
        "members": [{k: v for k, v in m.to_dict().items() if k != "onshape_id"}
                    for m in graph.members],
        "sections": [s.to_dict() for s in graph.sections if s.id in member_sections],
        "materials": [m.to_dict() for m in graph.materials if m.id in member_materials],
        "load_case": next(c.to_dict() for c in graph.load_cases if c.id == load_case_id),
    }
    return hashlib.sha256(json.dumps(body, sort_keys=True, allow_nan=False).encode()).hexdigest()


@dataclass
class SolveResult(Serializable):
    graph_id: str
    load_case_id: str
    model_fingerprint: str
    axial_forces: dict[str, float] = field(default_factory=dict)
    stresses: dict[str, float] = field(default_factory=dict)
    displacements: dict[str, list[float]] = field(default_factory=dict)
    reactions: dict[str, list[float]] = field(default_factory=dict)
    doubled_stresses: dict[str, float] = field(default_factory=dict)
    sanity_checks: SanityChecks = field(default_factory=SanityChecks)
    solver_version: str = SOLVER_VERSION
    condition_number: float = 0.0

    def validate(self) -> None:
        if not self.stresses or set(self.stresses) != set(self.axial_forces):
            raise ValueError("incomplete member solve results")
        if set(self.stresses) != set(self.doubled_stresses):
            raise ValueError("missing doubled-load results")
        values = list(self.stresses.values()) + list(self.axial_forces.values())
        values += list(self.doubled_stresses.values()) + [self.condition_number]
        for vector in list(self.displacements.values()) + list(self.reactions.values()):
            if len(vector) != 3:
                raise ValueError("a displacement/reaction must have three components")
            values.extend(vector)
        if not all(isfinite(v) for v in values):
            raise ValueError("nonfinite solver results")


def _solve_local(graph: DependencyGraph, load_case_id: str) -> SolveResult:
    """Worker implementation. The process boundary supplies the time limit."""
    graph.validate()
    if len(graph.nodes) > 50 or len(graph.members) > 100:
        raise SolveError("model exceeds demonstrator size limits")
    if any(n.z != 0 for n in graph.nodes):
        raise SolveError("only planar XY trusses are supported")
    case = next((c for c in graph.load_cases if c.id == load_case_id), None)
    if case is None:
        raise SolveError("unknown load case")
    if case.include_self_weight or any(p.fz != 0 for p in case.point_loads):
        raise SolveError("self-weight and out-of-plane loads are outside validated scope")

    cache = Path(tempfile.gettempdir()) / "earl-numerical-cache"
    os.environ.setdefault("MPLCONFIGDIR", str(cache / "matplotlib"))
    os.environ.setdefault("XDG_CACHE_HOME", str(cache))
    os.environ.setdefault("MPLBACKEND", "Agg")
    from Pynite import FEModel3D
    import numpy as np

    model = FEModel3D()
    for node in graph.nodes:
        model.add_node(node.id, node.x, node.y, node.z)
        dx, dy = restrained_axes(node.support)
        model.def_support(node.id, support_DX=dx, support_DY=dy,
                          support_DZ=True, support_RX=True, support_RY=True,
                          support_RZ=True)
    for material in graph.materials:
        model.add_material(material.id, material.elastic_modulus,
                           material.elastic_modulus / 2.6, 0.3, 0.0)
    for section in graph.sections:
        # Missing inertia is not invented as capacity. A tiny positive solver
        # section is needed for end-release condensation; capacity reports NE.
        model.add_section(section.id, section.area, max(section.iy, 1e-16),
                          max(section.iz, 1e-16), max(section.j, 1e-16))
    for member in graph.members:
        a, b = graph.node(member.start_node), graph.node(member.end_node)
        if dist((a.x, a.y), (b.x, b.y)) <= 1e-8:
            raise SolveError(f"member {member.id} has zero length")
        model.add_member(member.id, a.id, b.id, member.material_id, member.section_id)
        model.def_releases(member.id, Ryi=True, Rzi=True, Ryj=True, Rzj=True)
    for load in case.point_loads:
        model.add_node_load(load.node_id, "FX", load.fx, case="applied")
        model.add_node_load(load.node_id, "FY", load.fy, case="applied")
    model.add_load_combo("actual", {"applied": 1.0})
    model.add_load_combo("doubled", {"applied": 2.0})
    try:
        model.analyze_linear(log=False, check_stability=True, sparse=False)
        free = [6 * node.ID + axis for node, source in
                ((model.nodes[n.id], n) for n in graph.nodes)
                for axis, fixed in enumerate(restrained_axes(source.support)) if not fixed]
        k = model.K("actual", check_stability=False, sparse=False)
        condition = float(np.linalg.cond(k[np.ix_(free, free)])) if free else 1.0
        if not isfinite(condition) or condition > 1e12:
            raise SolveError("singular or ill-conditioned truss; a mechanism cannot be rated")
        result = SolveResult(graph.id, load_case_id, model_fingerprint(graph, load_case_id),
                             condition_number=condition)
        scale = max(sum(abs(p.fx) + abs(p.fy) for p in case.point_loads), 1.0)
        for member in graph.members:
            bar = model.members[member.id]
            force = -float(bar.axial(0.0, "actual"))
            doubled = -float(bar.axial(0.0, "doubled"))
            if abs(force) < scale * 1e-12:
                force = 0.0
            if abs(doubled) < 2 * scale * 1e-12:
                doubled = 0.0
            area = graph.section(member.section_id).area
            result.axial_forces[member.id] = force
            result.stresses[member.id] = force / area
            result.doubled_stresses[member.id] = doubled / area
        for node in graph.nodes:
            solved = model.nodes[node.id]
            result.displacements[node.id] = [float(solved.DX["actual"]),
                                            float(solved.DY["actual"]), 0.0]
            result.reactions[node.id] = [float(solved.RxnFX["actual"]),
                                        float(solved.RxnFY["actual"]), 0.0]
        result.validate()
        return result
    except SolveError:
        raise
    except Exception as exc:
        raise SolveError(f"PyNite analysis failed: {type(exc).__name__}: {exc}") from exc


def solve(graph: DependencyGraph, load_case_id: str = "lc_1", *,
          timeout: float = MAX_SOLVE_SECONDS) -> SolveResult:
    """Validate, solve and sanity-check in a killable, time-bounded process.

    No network or credential is supplied to the worker. Every new process also
    asserts the fixed benchmark before accepting the requested model.
    """
    graph.validate()
    if not isfinite(timeout) or timeout <= 0 or timeout > MAX_SOLVE_SECONDS:
        raise ValueError("invalid solve timeout")
    request = json.dumps({"graph": graph.to_dict(), "load_case_id": load_case_id},
                         allow_nan=False)
    env = {key: value for key, value in os.environ.items() if key in
           ("PATH", "SYSTEMROOT", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL")}
    env.update(OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1", MPLBACKEND="Agg")
    try:
        process = subprocess.run(
            [sys.executable, "-m", "earl.analysis.worker"], input=request,
            capture_output=True, text=True, timeout=timeout, env=env,
            cwd=Path(__file__).resolve().parents[2],
        )
    except subprocess.TimeoutExpired as exc:
        raise SolveError(f"solver exceeded {timeout:g} seconds and was terminated") from exc
    if process.returncode != 0:
        try:
            message = json.loads(process.stdout).get("error", "solver process failed")
        except (ValueError, AttributeError):
            message = "solver process failed before returning validated results"
        raise SolveError(message)
    try:
        result = SolveResult.from_json(process.stdout)
        result.validate()
        if result.model_fingerprint != model_fingerprint(graph, load_case_id):
            raise ValueError("solver returned results for a different model")
        return result
    except (ValueError, TypeError, KeyError) as exc:
        raise SolveError(f"invalid solver response: {exc}") from exc
