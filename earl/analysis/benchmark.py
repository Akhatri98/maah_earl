"""Fixed reference assertions for the published ten-bar truss formulation.

Reference entries are frozen independently of the adapter. The provenance and
the distinction between published formulation and transcribed tables are in
tests/fixtures/benchmark/ten_bar.json and RELIABILITY.md.
"""

import json
from functools import lru_cache
from math import isclose
from pathlib import Path

from earl.units import IN, PSI, LBF, round_inertia
from scripts.contract_selfcheck import build_graph


def benchmark_graph():
    graph = build_graph()
    for material in graph.materials:
        material.elastic_modulus = 1e7 * PSI
    for section in graph.sections:
        section.area = 10 * IN**2
        section.iy = section.iz = round_inertia(section.area)
        section.j = 2 * section.iy
    for case in graph.load_cases:
        for load in case.point_loads:
            load.fy = -100_000 * LBF
    graph.validate()
    return graph


@lru_cache(maxsize=1)
def validate_benchmark() -> dict:
    from .solver import _solve_local
    from .sanity import check
    references = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "benchmark"
    expected = json.loads((references / "ten_bar.json").read_text())
    published = json.loads((references / "published_reference.json").read_text())
    graph = benchmark_graph()
    solved = _solve_local(graph, "lc_1")
    rtol = expected["relative_tolerance"]
    for reference in (expected, published):
        for mid, psi in reference["stress_psi"].items():
            actual = solved.stresses[mid] / PSI
            if not isclose(actual, psi, rel_tol=rtol, abs_tol=1e-5):
                raise AssertionError(f"benchmark stress {mid}: {actual} != {psi} psi")
        for nid, values in reference["displacements_in"].items():
            for actual, wanted in zip(solved.displacements[nid][:2], values):
                if not isclose(actual / IN, wanted, rel_tol=rtol, abs_tol=1e-8):
                    raise AssertionError(f"benchmark displacement {nid}: {actual / IN} != {wanted} in")
    checks = check(graph, solved)
    if not (checks.equilibrium_ok and checks.linearity_ok):
        raise AssertionError("benchmark sanity checks failed")
    return {"passed": True, "solver_version": solved.solver_version,
            "stress_psi": {m: s / PSI for m, s in solved.stresses.items()},
            "displacements_in": {n: [x / IN for x in d[:2]]
                                 for n, d in solved.displacements.items()},
            "sanity_checks": checks.to_dict(), "relative_tolerance": rtol,
            "published_reference_sha256": published["source_sha256"]}
