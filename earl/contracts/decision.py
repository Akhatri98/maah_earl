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

from .common import CONTRACT_VERSION, SI_UNITS, Serializable, Units
from .graph import EdgeKind, OnshapeRef


class Outcome(str, Enum):
    """What the pipeline decided.

    ERROR is deliberately distinct from ESCALATED: a solver that failed to run
    is not the same as a structure that failed a check, and collapsing the two
    would let a crash read as a safety finding (or vice versa).
    """

    APPROVED = "approved"      # within threshold; branch may merge
    ESCALATED = "escalated"    # below threshold; held for a human decision
    ERROR = "error"            # analysis could not be completed


class MemberStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    NOT_EVALUATED = "not_evaluated"


@dataclass
class MemberResult(Serializable):
    """Per-member verdict. `safety_factor` is capacity / demand -- below 1.0
    means demand exceeds capacity.

    Conventions (binding, agreed in Sprint 4):

    * `axial_force_after` and the stresses are TENSION POSITIVE.
    * `capacity` is a STRESS in the payload's units, not a force: the design
      stress (`Material.design_stress`) when yield/allowable governs, or the
      Euler buckling stress Pcr / A when buckling governs, so that
      `capacity / |stress_after| == safety_factor`.
    * `hops_from_change` / `reached_via` are copied from the dependency walk
      (`walker.Reach.distance` / `.via`); 0 hops with `reached_via None` is
      the change target itself. Both None means the walk did not reach the
      member (or no walk was run) -- `is_affected` alone cannot say which.
    """

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

    # How the change reached this member, so the ECN can say "1 hop via
    # topology" without re-joining against the graph (contracts README rule 2).
    hops_from_change: int | None = None
    reached_via: EdgeKind | None = None


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


@dataclass
class SkyCivReport(Serializable):
    """Reference to the trusted artifact. The ECN cites this rather than
    restating its numbers, mirroring real ECN practice (plan.md, stage 4)."""

    report_id: str
    url: str | None = None
    local_path: str | None = None
    generated_at: str | None = None    # ISO-8601
    design_code: str | None = None     # e.g. "AISC 360-16"


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

    def evaluate(self) -> None:
        """Fill in `relative_difference` and `agrees` from the two values."""
        if self.pynite_value is None or self.skyciv_value is None:
            self.performed = False
            return
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
    # Where the change was evaluated (document, workspace, branch). Also
    # denormalized: an ECN that cannot name the sandbox branch is missing the
    # branch-not-main claim plan.md makes.
    source: OnshapeRef | None = None

    safety_factor_threshold: float = 1.0
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

    # -- derived views ------------------------------------------------------

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
        failing = [
            r for r in self.member_results
            if r.safety_factor is not None
            and r.safety_factor < self.safety_factor_threshold
        ]

        if failing and self.outcome is Outcome.APPROVED:
            worst = min(failing, key=lambda r: r.safety_factor)
            raise ValueError(
                f"decision {self.id!r} is APPROVED but member {worst.member_id!r} "
                f"has safety factor {worst.safety_factor} < threshold "
                f"{self.safety_factor_threshold}; this can never be auto-approved"
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
                and r.safety_factor < self.safety_factor_threshold
            )
            if below and r.status is MemberStatus.PASS:
                raise ValueError(
                    f"member {r.member_id!r} marked PASS with safety factor "
                    f"{r.safety_factor} below threshold {self.safety_factor_threshold}"
                )

        if self.outcome is Outcome.ERROR and not self.error_message:
            raise ValueError("outcome is ERROR but no error_message was given")

        if self.outcome is Outcome.ESCALATED and not failing:
            raise ValueError(
                "outcome is ESCALATED but no member is below threshold; "
                "escalation must be justified by a computed number"
            )
