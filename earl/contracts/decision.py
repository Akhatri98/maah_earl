"""Decision object -- the payload Track B hands back to Track A.

Produced by: earl.analysis (PyNite + Biject)  [Track B]
             earl.artifacts (SkyCiv)          [Track B, escalations only]
Consumed by: earl.delivery (ECN + Gmail)      [Track A]

Design rule: this object must be SUFFICIENT TO WRITE THE ECN on its own.
Track A should not have to re-join against the dependency graph to find out
what changed or which members are in trouble, so the human-readable summary of
the change is denormalized onto it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from math import isfinite

from .common import CONTRACT_VERSION, SI_UNITS, Serializable, Units


class Outcome(str, Enum):
    """What the pipeline decided.

    ERROR is deliberately distinct from ESCALATED: a solver that failed to run
    is not the same as a structure that failed a check, and collapsing the two
    would let a crash read as a safety finding (or vice versa).
    """

    APPROVED = "approved"      # policy passed; no CAD write capability exists
    ESCALATED = "escalated"    # below threshold; held for a human decision
    ERROR = "error"            # analysis could not be completed


class MemberStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    NOT_EVALUATED = "not_evaluated"


class EscalationReason(str, Enum):
    BELOW_HARD_FLOOR = "below_hard_floor"
    BELOW_DESIGN_TARGET = "below_design_target"
    CROSS_CHECK_DISAGREEMENT = "cross_check_disagreement"
    NOT_EVALUATED = "not_evaluated"


@dataclass
class MemberResult(Serializable):
    """Per-member verdict. `safety_factor` is capacity / demand -- below 1.0
    means demand exceeds capacity."""

    member_id: str
    status: MemberStatus

    stress_before: float | None = None
    stress_after: float | None = None
    axial_force_after: float | None = None
    capacity: float | None = None
    safety_factor: float | None = None
    utilization: float | None = None    # 1 / safety_factor, for reporting

    is_affected: bool = False           # was it downstream of the change?
    onshape_id: str | None = None       # provenance, carried through for the ECN
    note: str | None = None
    safety_factor_before: float | None = None
    capacity_before: float | None = None
    area_before: float | None = None
    area_after: float | None = None
    capacity_mode: str | None = None
    slenderness: float | None = None
    zero_demand: bool = False


@dataclass
class SanityChecks(Serializable):
    """Free correctness tests that run on every scenario (plan.md, Validation).

    These are not a substitute for the SkyCiv cross-check; they are the cheap
    always-on guard that the solver did not produce nonsense.
    """

    equilibrium_ok: bool | None = None      # sum(F) = 0 at every joint
    max_residual_force: float | None = None
    linearity_ok: bool | None = None        # 2x load => exactly 2x stress
    note: str | None = None
    max_linearity_relative_error: float | None = None


@dataclass
class SkyCivReport(Serializable):
    """Reference to the trusted artifact. The ECN cites this rather than
    restating its numbers, mirroring real ECN practice (plan.md, stage 4)."""

    report_id: str
    url: str | None = None
    local_path: str | None = None
    generated_at: str | None = None    # ISO-8601
    design_code: str | None = None     # e.g. "AISC 360-16"
    provenance: str = "unspecified"
    model_fingerprint: str | None = None
    note: str | None = None


@dataclass
class CrossCheck(Serializable):
    """PyNite vs. SkyCiv agreement on the governing member.

    Disagreement is a signal worth surfacing, not hiding -- so `agrees=False`
    must still be delivered, never swallowed.
    """

    performed: bool = False
    member_id: str | None = None
    pynite_value: float | None = None
    skyciv_value: float | None = None
    relative_difference: float | None = None
    tolerance: float = 0.05
    agrees: bool | None = None
    provenance: str = "unspecified"
    note: str | None = None

    def evaluate(self) -> None:
        """Fill in `relative_difference` and `agrees` from the two values."""
        if self.pynite_value is None or self.skyciv_value is None:
            self.performed = False
            self.agrees = None
            self.relative_difference = None
            return
        if not all(isfinite(v) for v in (self.pynite_value, self.skyciv_value)):
            raise ValueError("cross-check values must be finite")
        denom = max(abs(self.pynite_value), abs(self.skyciv_value))
        self.relative_difference = (
            0.0 if denom == 0 else abs(self.pynite_value - self.skyciv_value) / denom
        )
        self.agrees = self.relative_difference <= self.tolerance
        self.performed = True


@dataclass
class Decision(Serializable):
    """Track B's output: the verdict on one change, with the numbers behind it."""

    id: str
    graph_id: str                  # the DependencyGraph this answers
    change_id: str
    outcome: Outcome

    # Denormalized from the graph so Track A can write the ECN without a join.
    change_description: str = ""

    safety_factor_threshold: float = 1.0
    hard_floor: float = 1.0
    design_target: float = 1.5
    escalation_reason: EscalationReason | None = None
    member_results: list[MemberResult] = field(default_factory=list)
    violating_member_ids: list[str] = field(default_factory=list)

    sanity_checks: SanityChecks = field(default_factory=SanityChecks)
    cross_check: CrossCheck = field(default_factory=CrossCheck)
    skyciv_report: SkyCivReport | None = None

    load_case_id: str | None = None
    solver: str = "PyNite"
    solver_version: str | None = None
    evaluated_at: str | None = None       # ISO-8601
    error_message: str | None = None      # populated iff outcome is ERROR

    units: Units = field(default_factory=lambda: SI_UNITS)
    schema_version: str = CONTRACT_VERSION
    expected_member_ids: list[str] = field(default_factory=list)
    source_provenance: str = "unspecified"
    narrative: str = ""
    narrative_provenance: str = "deterministic template"

    # -- derived views ------------------------------------------------------

    @property
    def threshold(self) -> float:
        """Legacy threshold can tighten policy, but can never weaken the floor."""
        return max(self.hard_floor, self.design_target, self.safety_factor_threshold)

    @property
    def governing_member(self) -> MemberResult | None:
        """The member with the lowest safety factor -- the one the ECN leads on."""
        scored = [r for r in self.member_results if r.safety_factor is not None]
        return min(scored, key=lambda r: r.safety_factor) if scored else None

    def result(self, member_id: str) -> MemberResult:
        for r in self.member_results:
            if r.member_id == member_id:
                return r
        raise KeyError(f"no result for member {member_id!r}")

    # -- the non-negotiable rule -------------------------------------------

    def validate(self) -> None:
        """Enforce the invariants plan.md says must hold REGARDLESS of what any
        model in the pipeline decided.

        Called at the track boundary, so a malformed approval fails loudly here
        rather than travelling on to become an auto-merge and an ECN that says
        everything is fine.
        """
        self.units.assert_si()
        if not isinstance(self.outcome, Outcome):
            raise ValueError("outcome must be an Outcome enum")
        if (not isfinite(self.hard_floor) or self.hard_floor < 1.0 or
                not isfinite(self.design_target) or self.design_target < self.hard_floor or
                not isfinite(self.safety_factor_threshold) or self.safety_factor_threshold <= 0):
            raise ValueError("invalid hard_floor or design_target")
        ids = [r.member_id for r in self.member_results]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate member results")
        if self.expected_member_ids and set(ids) != set(self.expected_member_ids):
            raise ValueError("member results do not cover the expected structure")
        for r in self.member_results:
            if not isinstance(r.status, MemberStatus):
                raise ValueError("status must be a MemberStatus enum")
            for number in (r.safety_factor, r.utilization, r.capacity,
                           r.stress_before, r.stress_after, r.axial_force_after):
                if number is not None and not isfinite(number):
                    raise ValueError("member results must be finite")
            if r.safety_factor is not None and r.safety_factor < 0:
                raise ValueError("safety factor cannot be negative")
            if r.capacity is not None and r.capacity < 0:
                raise ValueError("capacity cannot be negative")
            if r.safety_factor is None and r.status is MemberStatus.PASS:
                if not (r.zero_demand and r.axial_force_after == 0 and
                        r.capacity is not None and r.capacity > 0):
                    raise ValueError("PASS requires a score or verified zero demand")
        failing = [
            r for r in self.member_results
            if r.safety_factor is not None
            and r.safety_factor < self.threshold
        ]

        if failing and self.outcome is Outcome.APPROVED:
            worst = min(failing, key=lambda r: r.safety_factor)
            raise ValueError(
                f"decision {self.id!r} is APPROVED but member {worst.member_id!r} "
                f"has safety factor {worst.safety_factor} < threshold "
                f"{self.threshold}; this can never be auto-approved"
            )

        declared = set(self.violating_member_ids)
        actual = {r.member_id for r in failing}
        if declared != actual:
            raise ValueError(
                f"violating_member_ids {sorted(declared)} disagrees with the "
                f"member results {sorted(actual)}"
            )

        for r in self.member_results:
            below = (
                r.safety_factor is not None
                and r.safety_factor < self.threshold
            )
            if below and r.status is MemberStatus.PASS:
                raise ValueError(
                    f"member {r.member_id!r} marked PASS with safety factor "
                    f"{r.safety_factor} below threshold {self.threshold}"
                )
            if r.status is MemberStatus.FAIL and not below:
                raise ValueError("FAIL must be justified by a score below threshold")

        if self.outcome is Outcome.ERROR and not self.error_message:
            raise ValueError("outcome is ERROR but no error_message was given")

        incomplete = any(r.status is MemberStatus.NOT_EVALUATED for r in self.member_results)
        disagrees = self.cross_check.agrees is False
        bad_sanity = (self.sanity_checks.equilibrium_ok is False or
                      self.sanity_checks.linearity_ok is False)
        if bad_sanity and self.outcome is not Outcome.ERROR:
            raise ValueError("a failed sanity check requires Outcome.ERROR")
        if self.outcome is Outcome.APPROVED and (
            not self.member_results or incomplete or disagrees or self.error_message or
            self.escalation_reason is not None
        ):
            raise ValueError("incomplete, disputed or errored analysis cannot be approved")
        if self.outcome is Outcome.ESCALATED and not (failing or disagrees or incomplete):
            raise ValueError(
                "escalation requires a member below threshold, disagreement, "
                "or a member NOT_EVALUATED"
            )
        reasons = {
            EscalationReason.BELOW_HARD_FLOOR: any(
                r.safety_factor < self.hard_floor for r in failing),
            EscalationReason.BELOW_DESIGN_TARGET: bool(failing),
            EscalationReason.CROSS_CHECK_DISAGREEMENT: disagrees,
            EscalationReason.NOT_EVALUATED: incomplete,
        }
        if self.escalation_reason is not None and not reasons.get(self.escalation_reason, False):
            raise ValueError("escalation_reason is not supported by the results")
