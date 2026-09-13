"""Everything the truss needs that CAD does not carry. [Track A, Sprint 2A]

Onshape models geometry. It does not model supports, loads, material strength
or member capacity -- `truss_modelling_spec.md` says so explicitly ("Not
modelled in CAD"). But `DependencyGraph.validate()` rejects a structure where
every node is FREE, because an unconstrained model is not solvable. So a graph
assembled purely from Onshape reads is, correctly, invalid.

This module is the single declared source for that missing half, so the answer
to "where did the supports come from?" is one file rather than a constant
buried in whichever module happened to need it first.

Values are the published 10-bar truss benchmark (Haftka & Gurdal / Rajan),
converted to SI exactly once, here -- the single conversion point
`earl/contracts/README.md` asks for.

## The material is aluminium, not steel

The benchmark's properties are E = 10^7 psi, rho = 0.1 lb/in^3, allowable
stress 25 ksi. Those are aluminium: A36 steel is E = 29x10^6 psi, rho =
0.284 lb/in^3, yield 36 ksi. `scripts/contract_selfcheck.py` currently labels
E = 6.895e10 Pa (= 10^7 psi, aluminium) as "A36 Steel" and pairs it with
steel's density and steel's yield -- a mix of two materials.

That matters unevenly:

  * E is already the benchmark value, so Track B's displacement validation is
    unaffected either way.
  * density only bites once self-weight is switched on (it is off by default).
  * the allowable stress sets member CAPACITY, and therefore the safety factor
    that decides approve-vs-escalate. 36 ksi instead of 25 ksi makes every
    member read about 44% safer than the benchmark says it is.

This module uses the published benchmark values. `contract_selfcheck.py` is a
Sprint 0 artifact owned by neither track, so it is left alone pending
agreement rather than changed unilaterally.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..contracts.graph import (
    LoadCase,
    Material,
    PointLoad,
    Section,
    SupportType,
)

# -- unit conversion, done once ---------------------------------------------

IN = 0.0254              # in -> m
PSI_TO_PA = 6894.757     # psi -> Pa
KIP_TO_N = 4448.222      # kip -> N
LB_PER_IN3_TO_KG_M3 = 0.45359237 / IN**3   # -> 27679.9

# -- the published benchmark, in SI -----------------------------------------

E_ALUMINIUM = 1.0e7 * PSI_TO_PA            # 6.895e10 Pa
ALLOWABLE_STRESS = 25_000 * PSI_TO_PA      # 1.724e8 Pa -- sets member capacity
DENSITY = 0.1 * LB_PER_IN3_TO_KG_M3        # 2768 kg/m^3
DEFAULT_AREA = 1.0 * IN**2                 # 6.4516e-4 m^2
BENCHMARK_LOAD = 100.0 * KIP_TO_N          # 444_822 N

MATERIAL_ID = "mat_al"
SECTION_ID = "sec_1in2"
LOAD_CASE_ID = "lc_1"

# Supports: pinned at the two left-hand nodes. Any node not named here is FREE.
SUPPORTS: dict[str, SupportType] = {
    "n5": SupportType.PIN,
    "n6": SupportType.PIN,
}

# Downward 100-kip point loads at the two lower free nodes.
LOADED_NODES: tuple[str, ...] = ("n2", "n4")


@dataclass(frozen=True)
class BenchmarkSpec:
    """The non-CAD half of the model, resolved for one graph build."""

    material: Material
    section: Section
    load_case: LoadCase
    supports: dict[str, SupportType]

    def support_for(self, node_id: str) -> SupportType:
        """Restraint for a node. Unlisted nodes are free, not an error --
        most of the truss is."""
        return self.supports.get(node_id, SupportType.FREE)


def benchmark_spec(
    *,
    area: float = DEFAULT_AREA,
    load: float = BENCHMARK_LOAD,
    include_self_weight: bool = False,
) -> BenchmarkSpec:
    """Build the supports/loads/material half of the 10-bar truss model.

    `area` is a parameter rather than a constant because resizing a member is
    one of the change kinds the pipeline ingests: the Onshape `barArea`
    variable drives it, and `Variable.si_value` is what reads it.
    """
    return BenchmarkSpec(
        material=Material(
            id=MATERIAL_ID,
            name="Benchmark aluminium (E = 10^7 psi)",
            elastic_modulus=E_ALUMINIUM,
            yield_strength=ALLOWABLE_STRESS,
            density=DENSITY,
        ),
        section=Section(
            id=SECTION_ID,
            name=f"{area / IN**2:.3g} in^2 bar",
            area=area,
        ),
        load_case=LoadCase(
            id=LOAD_CASE_ID,
            name="Benchmark load case (100 kip down at n2 and n4)",
            point_loads=[
                PointLoad(id=f"p_{node}", node_id=node, fy=-load)
                for node in LOADED_NODES
            ],
            include_self_weight=include_self_weight,
        ),
        supports=dict(SUPPORTS),
    )
