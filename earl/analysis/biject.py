"""Biject -- the verification layer that turns forces into a verdict. [Track B, Sprint 2B]

plan.md: "Biject" is our own threshold-enforcement module, not a product. It
is PURE CODE with no model input: a member's verdict is a function of the
graph (material, section, geometry), the solver's forces and one number, the
safety-factor threshold. Nothing that any planner, LLM or caller says can move
a member from FAIL to PASS, and `evaluate()` has no parameter that can force
APPROVED. The plan's rule "SF < 1.0 can never be auto-approved" lives here as
MIN_ALLOWED_THRESHOLD: a threshold below it is a ValueError, not a setting.

Conventions (binding for everything Track B emits):

  * SIGN.  Axial force and stress are TENSION POSITIVE (solver.py negates
    PyNite once). A positive force is checked against the tension capacity,
    a negative one against the compression capacity.
  * CAPACITY IS INTERNAL IN FORCE, EXTERNAL IN STRESS (R6).  `Capacity` (this
    module only) holds forces: tension = Fy*A, compression = min(Fy*A, Euler
    Pcr = pi^2 E I_min / (K L)^2) over the inertias that ARE provided
    (positive). No inertia -> Fy*A with the note "inertia not provided;
    buckling not checked"; exactly one -> Pcr about that axis with a note
    that it is an upper bound (the missing axis could be weaker). The
    contract's `MemberResult.capacity` is a STRESS in Pa (capacity_force /
    area): Fy when yield governs, Pcr/A when buckling governs, so that
    capacity / |stress_after| == safety_factor exactly as in the Sprint 0 mock
    (scripts/contract_selfcheck.py).
  * NUMBERS MUST BE NUMBERS.  A non-finite or non-positive area, yield
    strength or elastic modulus, a non-finite force, or a non-finite safety
    factor is a ValueError. NaN compares False against everything, so
    "NaN < threshold" would read as PASS -- a NaN must never reach a verdict.
  * SAFETY FACTOR = capacity_force / |F|, capped at MAX_SAFETY_FACTOR (JSON
    has no Infinity). A ZERO-FORCE member (R7: |F| <= max(1e-9 N, 1e-9 x the
    largest |F| in the same SolveResult)) gets the cap, utilization 0.0 and
    the note "zero-force member; safety factor capped".
  * EVERY member in graph.members is evaluated, whatever any plan says
    (R8: focus_member_ids is advisory). With several SolveResults (several
    load cases, or the contract and self-weight variants of one case) a
    member's verdict is its WORST over all of them and the governing VARIANT
    KEY is written into its note.
  * VARIANT KEYS (shared with gate.py).  `variant_key(graph, result)` is the
    load case id when the result's `include_self_weight` equals the contract
    flag of that load case, else "<lc_id>+self_weight" -- the variant a plan
    added on top of the contract (R8, additive only). Both
    `Verdict.governing_load_case_ids` and the `before` dict are keyed this
    way, so a plan-added self-weight variant never borrows the contract
    variant's before-state, and the note says which one governed.
  * A FAIL on a member that is NOT in graph.affected_member_ids is surfaced
    with a note, never hidden: it means the graph walker missed a dependency,
    which is exactly the "dropped domino" the project exists to catch.
  * `before` (R11) is a dict of before-state SolveResults keyed by variant
    key; a member's stress_before comes from the before result of ITS
    governing variant, else None with the note "no before-state for
    governing case <key>".
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..contracts import DependencyGraph, LoadCase, MemberResult, MemberStatus, Outcome
from .solver import MemberForce, SolveResult, member_length

# A zero-force member would have an infinite safety factor; JSON cannot carry
# Infinity, so the factor is capped here. Anything at the cap is "unloaded".
MAX_SAFETY_FACTOR = 1.0e6
# plan.md: SF < 1.0 can never be auto-approved. The floor is code, not config.
MIN_ALLOWED_THRESHOLD = 1.0
# Euler effective-length factor K for a pin-ended truss member.
EFFECTIVE_LENGTH_FACTOR = 1.0
# R7 zero-force predicate: |F| <= max(ZERO_FORCE_ABS_TOL, ZERO_FORCE_REL_TOL * max|F|).
ZERO_FORCE_ABS_TOL = 1e-9          # N
ZERO_FORCE_REL_TOL = 1e-9
# Suffix of the variant key for the self-weight variant a plan adds on top of
# the contract load case (R8). gate.py builds its before-dict with the same key.
SELF_WEIGHT_VARIANT_SUFFIX = "+self_weight"

INERTIA_NOT_PROVIDED_NOTE = "inertia not provided; buckling not checked"
ONE_INERTIA_NOTE = (
    "only one inertia provided; buckling checked about that axis only "
    "(upper bound on Pcr)"
)
ZERO_FORCE_NOTE = "zero-force member; safety factor capped"
UNAFFECTED_FAIL_NOTE = (
    "unsafe but not in graph.affected_member_ids -- the graph walker may have "
    "missed a dependency"
)
SELF_WEIGHT_VARIANT_NOTE = "plan-added self-weight variant"


# --------------------------------------------------------------------------
# Capacity (force units; internal to this module)
# --------------------------------------------------------------------------

@dataclass
class Capacity:
    """Axial capacities of one member in NEWTONS (positive). `governing`
    names what limits the COMPRESSION capacity: "yield" or "buckling".
    Tension is always yield-governed."""

    member_id: str
    tension: float
    compression: float
    governing: str
    note: str | None


def _require_positive_finite(value: float, what: str) -> float:
    """B2: a NaN/inf/<= 0 property must stop the evaluation, not flow into a
    safety factor. `bool(nan <= 0)` is False, so a plain sign check would let
    NaN through -- hence math.isfinite first."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{what} must be a number, got {value!r}") from None
    if not math.isfinite(number) or number <= 0.0:
        raise ValueError(f"{what} must be finite and positive, got {value!r}")
    return number


def member_capacity(graph: DependencyGraph, member_id: str) -> Capacity:
    """Yield capacity Fy*A in tension; in compression the smaller of Fy*A and
    the Euler load pi^2 E I_min / (K L)^2 over the inertias the section
    PROVIDES (positive). Sections with iy = iz = 0 (the benchmark's bars) are
    yield-only and say so in the note -- the honest, illustrative check
    plan.md's Scope note allows. A section with only one positive inertia
    (B1) is still checked about that axis: skipping the check because the
    other axis is unknown would hide a real buckling failure, so the note
    says the Pcr is an upper bound instead."""
    member = graph.member(member_id)
    section = graph.section(member.section_id)
    material = graph.material(member.material_id)
    area = _require_positive_finite(section.area, f"member {member_id!r} area")
    fy = _require_positive_finite(
        material.yield_strength, f"material {material.id!r} yield strength"
    )
    modulus = _require_positive_finite(
        material.elastic_modulus, f"material {material.id!r} elastic modulus"
    )

    yield_force = fy * area
    provided = [i for i in (section.iy, section.iz) if i > 0.0]
    if not provided:
        return Capacity(member_id, yield_force, yield_force, "yield", INERTIA_NOT_PROVIDED_NOTE)
    i_min = min(provided)
    if not math.isfinite(i_min):
        raise ValueError(f"member {member_id!r} has a non-finite inertia {i_min!r}")
    notes: list[str] = []
    if len(provided) == 1:
        notes.append(ONE_INERTIA_NOTE)

    length = member_length(graph, member)
    if not math.isfinite(length) or length <= 0.0:
        raise ValueError(f"member {member_id!r} has a non-positive or non-finite length {length!r}")
    effective = EFFECTIVE_LENGTH_FACTOR * length
    euler = math.pi ** 2 * modulus * i_min / effective ** 2
    if euler < yield_force:
        notes.append(
            f"Euler buckling governs compression (Pcr {euler:.4g} N < Fy*A {yield_force:.4g} N)"
        )
        return Capacity(member_id, yield_force, euler, "buckling", "; ".join(notes))
    return Capacity(member_id, yield_force, yield_force, "yield", "; ".join(notes) or None)


# --------------------------------------------------------------------------
# Per-member evaluation
# --------------------------------------------------------------------------

def _check_threshold(threshold: float) -> float:
    """The floor from plan.md. Raises ValueError -- a misconfiguration is not
    a finding and must never become a verdict."""
    try:
        value = float(threshold)
    except (TypeError, ValueError):
        raise ValueError(f"safety-factor threshold must be a number, got {threshold!r}") from None
    if not math.isfinite(value):
        raise ValueError(f"safety-factor threshold must be finite, got {threshold!r}")
    if value < MIN_ALLOWED_THRESHOLD:
        raise ValueError(
            f"safety-factor threshold {value} is below the floor {MIN_ALLOWED_THRESHOLD}; "
            "SF < 1.0 can never be auto-approved (plan.md), so this floor lives in code"
        )
    return value


def zero_force_floor(result: SolveResult) -> float:
    """R7: the |F| at or below which a member of `result` is zero-force."""
    largest = max((abs(f.axial_force) for f in result.member_forces.values()), default=0.0)
    return max(ZERO_FORCE_ABS_TOL, ZERO_FORCE_REL_TOL * largest)


def is_zero_force(force: MemberForce, floor: float = ZERO_FORCE_ABS_TOL) -> bool:
    return abs(force.axial_force) <= floor


def evaluate_member(
    graph: DependencyGraph,
    member_id: str,
    force: MemberForce,
    threshold: float,
    *,
    is_affected: bool,
    stress_before: float | None = None,
    force_floor: float = ZERO_FORCE_ABS_TOL,
) -> MemberResult:
    """One member, one load case, one contract MemberResult.

    capacity (a STRESS, Pa) = the governing capacity force / area;
    safety_factor = capacity force / |F| (capped); utilization = 1 / SF;
    status = FAIL iff SF < threshold. `force_floor` is the R7 zero-force
    level -- `evaluate()` derives it from the whole SolveResult; the default is
    the absolute floor alone. A FAIL on a member that is not `is_affected`
    carries UNAFFECTED_FAIL_NOTE. A non-finite force or safety factor is a
    ValueError (B2): NaN would compare False against the threshold and read
    as PASS.
    """
    threshold = _check_threshold(threshold)
    member = graph.member(member_id)
    cap = member_capacity(graph, member_id)      # validates area, Fy, E
    area = graph.section(member.section_id).area
    notes: list[str] = []
    if cap.note:
        notes.append(cap.note)

    magnitude = abs(force.axial_force)
    if not math.isfinite(magnitude):
        raise ValueError(f"member {member_id!r} has a non-finite axial force {force.axial_force!r}")

    if force.axial_force >= 0.0:
        capacity_force = cap.tension
    else:
        capacity_force = cap.compression
    capacity_stress = capacity_force / area

    if magnitude <= force_floor:
        safety_factor = MAX_SAFETY_FACTOR
        utilization = 0.0
        notes.append(ZERO_FORCE_NOTE)
    else:
        safety_factor = capacity_force / magnitude
        if not math.isfinite(safety_factor):
            raise ValueError(
                f"member {member_id!r} has a non-finite safety factor {safety_factor!r} "
                f"(capacity {capacity_force!r} N, |F| {magnitude!r} N)"
            )
        if safety_factor >= MAX_SAFETY_FACTOR:
            safety_factor = MAX_SAFETY_FACTOR
            notes.append(f"safety factor capped at {MAX_SAFETY_FACTOR:g}")
        utilization = 1.0 / safety_factor

    # Belt and braces: whatever path produced it, a verdict is only taken
    # from a finite number. NaN < threshold is False and would mean PASS.
    if not math.isfinite(safety_factor):
        raise ValueError(f"member {member_id!r} has a non-finite safety factor {safety_factor!r}")

    status = MemberStatus.FAIL if safety_factor < threshold else MemberStatus.PASS
    if status is MemberStatus.FAIL and not is_affected:
        notes.append(UNAFFECTED_FAIL_NOTE)

    return MemberResult(
        member_id=member_id,
        status=status,
        stress_before=stress_before,
        stress_after=force.stress,
        axial_force_after=force.axial_force,
        capacity=capacity_stress,
        safety_factor=safety_factor,
        utilization=utilization,
        is_affected=is_affected,
        onshape_id=member.onshape_id,
        note="; ".join(notes) if notes else None,
    )


# --------------------------------------------------------------------------
# Variant keys (shared convention with gate.py)
# --------------------------------------------------------------------------

def _load_case(graph: DependencyGraph, load_case_id: str) -> LoadCase:
    for lc in graph.load_cases:
        if lc.id == load_case_id:
            return lc
    raise ValueError(
        f"SolveResult is for load case {load_case_id!r}, which graph {graph.id!r} "
        "does not define"
    )


def variant_key(graph: DependencyGraph, result: SolveResult) -> str:
    """The key under which `result` is tracked in `Verdict.governing_load_case_ids`
    and looked up in `before`.

    The contract variant (the result's `include_self_weight` equals the load
    case's own flag) is keyed by the bare load case id; the variant a plan
    added on top (self-weight switched on for a case whose flag is off, R8) is
    keyed "<lc_id>+self_weight". Keeping them apart is what lets a before-state
    solved WITHOUT self-weight never be paired with an after-state solved WITH
    it: the gate builds its before dict with exactly these keys.
    """
    lc = _load_case(graph, result.load_case_id)
    # "Plan-added" means self-weight is ON in the result while the contract
    # flag is OFF. The other mismatch (flag on, result off) happens when no
    # material has a density -- solver.effective_self_weight applies nothing
    # -- and that is still the contract run, not a plan-added variant.
    if bool(result.include_self_weight) and not bool(lc.include_self_weight):
        return f"{result.load_case_id}{SELF_WEIGHT_VARIANT_SUFFIX}"
    return result.load_case_id


def is_self_weight_variant(key: str) -> bool:
    return key.endswith(SELF_WEIGHT_VARIANT_SUFFIX)


def governing_note(key: str) -> str:
    """The member note for a governing variant key: "governing load case 'lc_x'"
    for the contract variant, "... (plan-added self-weight variant)" for the
    other, so a reader can tell which of the two runs of one load case won."""
    if is_self_weight_variant(key):
        lc_id = key[: -len(SELF_WEIGHT_VARIANT_SUFFIX)]
        return f"governing load case {lc_id!r} ({SELF_WEIGHT_VARIANT_NOTE})"
    return f"governing load case {key!r}"


# --------------------------------------------------------------------------
# Verdict over one graph and one or many solved load cases
# --------------------------------------------------------------------------

@dataclass
class Verdict:
    """What Biject decided: ESCALATED iff any member is FAIL, else APPROVED.
    `member_results` is in graph.members order and covers EVERY member;
    `violating_member_ids` is sorted and matches the FAIL results exactly (the
    invariant Decision.validate() enforces)."""

    outcome: Outcome
    member_results: list[MemberResult]
    violating_member_ids: list[str]
    threshold: float
    # Which solved variant each member's verdict came from (worst SF), as a
    # variant key (see `variant_key`): "lc_x" or "lc_x+self_weight".
    governing_load_case_ids: dict[str, str] = field(default_factory=dict)

    def result(self, member_id: str) -> MemberResult:
        for r in self.member_results:
            if r.member_id == member_id:
                return r
        raise KeyError(f"no result for member {member_id!r}")


def _append_note(result: MemberResult, text: str) -> None:
    result.note = f"{result.note}; {text}" if result.note else text


def evaluate(
    graph: DependencyGraph,
    results: list[SolveResult] | SolveResult,
    threshold: float,
    *,
    before: dict[str, SolveResult] | None = None,
) -> Verdict:
    """Verdict on `graph` from one or many SolveResults of it.

    threshold < MIN_ALLOWED_THRESHOLD (or non-finite) -> ValueError. Every
    member in graph.members is evaluated in every result; its verdict is the
    WORST (lowest SF) and the governing variant key goes into its note --
    "governing load case 'lc_x'" for the contract variant, with
    "(plan-added self-weight variant)" appended for the variant a plan added.
    `before` is keyed by variant key (R11, see `variant_key`): stress_before
    is read from the before result of the member's governing variant, else
    None with a note. There is deliberately NO parameter that can force
    APPROVED.
    """
    graph.units.assert_si()
    threshold = _check_threshold(threshold)

    if isinstance(results, SolveResult):
        results = [results]
    results = list(results)
    if not results:
        raise ValueError("Biject needs at least one SolveResult to evaluate")
    for r in results:
        if r.graph_id != graph.id:
            raise ValueError(
                f"SolveResult is for graph {r.graph_id!r}, not {graph.id!r}; "
                "pass the after-state results here and the before-state via `before`"
            )
    keys = [variant_key(graph, r) for r in results]
    floors = [zero_force_floor(r) for r in results]
    affected = set(graph.affected_member_ids)

    member_results: list[MemberResult] = []
    governing: dict[str, str] = {}
    for member in graph.members:
        worst: MemberResult | None = None
        worst_key: str | None = None
        for r, key, floor in zip(results, keys, floors):
            if member.id not in r.member_forces:
                raise ValueError(
                    f"SolveResult for load case {r.load_case_id!r} carries no force "
                    f"for member {member.id!r}; every member must be evaluated"
                )
            candidate = evaluate_member(
                graph,
                member.id,
                r.force(member.id),
                threshold,
                is_affected=member.id in affected,
                force_floor=floor,
            )
            if worst is None or candidate.safety_factor < worst.safety_factor:
                worst, worst_key = candidate, key
        assert worst is not None and worst_key is not None
        _append_note(worst, governing_note(worst_key))

        if before is not None:
            source = before.get(worst_key)
            if source is not None and member.id in source.member_forces:
                worst.stress_before = source.force(member.id).stress
            else:
                worst.stress_before = None
                _append_note(worst, f"no before-state for governing case {worst_key!r}")

        governing[member.id] = worst_key
        member_results.append(worst)

    violating = sorted(r.member_id for r in member_results if r.status is MemberStatus.FAIL)
    outcome = Outcome.ESCALATED if violating else Outcome.APPROVED
    return Verdict(
        outcome=outcome,
        member_results=member_results,
        violating_member_ids=violating,
        threshold=threshold,
        governing_load_case_ids=governing,
    )
