"""Independent, test-only direct-stiffness reproduction of the published model.

Implements Martins & Ning, Engineering Design Optimization, Appendix D.2.2,
equations D.38-D.41. Uses the canonical graph's geometry and NO PyNite calls.
This is an independent numerical reference, not a new production solver.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from earl.analysis.benchmark import benchmark_graph
from earl.units import IN, PSI


def reference():
    graph = benchmark_graph()
    indices = {n.id: i for i, n in enumerate(graph.nodes)}
    stiffness = np.zeros((12, 12))
    stress_recovery = np.zeros((10, 12))
    for row, member in enumerate(graph.members):
        a, b = graph.node(member.start_node), graph.node(member.end_node)
        length = np.hypot(b.x - a.x, b.y - a.y)
        c, s = (b.x - a.x) / length, (b.y - a.y) / length
        direction = np.array([-c, -s, c, s])
        area = graph.section(member.section_id).area
        modulus = graph.material(member.material_id).elastic_modulus
        slots = [2 * indices[a.id], 2 * indices[a.id] + 1,
                 2 * indices[b.id], 2 * indices[b.id] + 1]
        stiffness[np.ix_(slots, slots)] += modulus * area / length * np.outer(direction, direction)
        stress_recovery[row, slots] = modulus / length * direction
    forces = np.zeros(12)
    for load in graph.load_cases[0].point_loads:
        offset = 2 * indices[load.node_id]
        forces[offset:offset + 2] += [load.fx, load.fy]
    free = [2 * indices[n.id] + axis for n in graph.nodes if n.id not in ("n5", "n6")
            for axis in (0, 1)]
    displacement = np.zeros(12)
    displacement[free] = np.linalg.solve(stiffness[np.ix_(free, free)], forces[free])
    stress = stress_recovery @ displacement
    return {"stress_psi": {m.id: float(stress[i] / PSI) for i, m in enumerate(graph.members)},
            "displacements_in": {n.id: [float(x / IN) for x in displacement[2*i:2*i+2]]
                                 for i, n in enumerate(graph.nodes)}}


if __name__ == "__main__":
    print(json.dumps(reference(), indent=2))
