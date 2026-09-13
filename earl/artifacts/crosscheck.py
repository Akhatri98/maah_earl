"""PyNite vs. SkyCiv cross-check. [Track B, Sprint 3B]

Two independent linear-elastic truss solvers given the same model must agree
on member axial forces to well within a percent; linear truss analysis is
exact, not fitted. Agreement is evidence the fast gate is not quietly wrong,
and disagreement is a signal worth delivering rather than hiding (plan.md,
stage 3) -- so `agrees=False` is a first-class result here, never an error.

The comparison is on |axial force| magnitudes, deliberately: SkyCiv's sign
convention is unverified (see skyciv_client), and a sign disagreement must
not masquerade as a 200 % magnitude disagreement or, worse, as agreement.
The arithmetic itself lives in `earl.contracts.CrossCheck.evaluate()`, so the
number Track A reads in the Decision is computed by the contract, not
re-derived here.
"""

from __future__ import annotations

from ..contracts import CrossCheck, Decision
from .skyciv_client import SkyCivRun


def _pynite_magnitude(decision: Decision, member_id: str) -> float | None:
    try:
        result = decision.result(member_id)
    except KeyError:
        return None
    if result.axial_force_after is None:
        return None
    return abs(result.axial_force_after)


def _skyciv_magnitude(run: SkyCivRun, member_id: str) -> float | None:
    result = run.member_results.get(member_id)
    return None if result is None else abs(result.axial_force)


def cross_check(
    decision: Decision,
    run: SkyCivRun,
    *,
    member_id: str | None = None,
    tolerance: float = 0.05,
) -> CrossCheck:
    """Compare one member's |axial force| between the decision (PyNite) and
    the SkyCiv run. Defaults to the decision's governing member.

    If either side is missing, the returned CrossCheck has `performed=False`
    with `member_id` filled in, so the ECN can say *which* member could not
    be checked.
    """
    if member_id is None:
        governing = decision.governing_member
        member_id = governing.member_id if governing is not None else None

    check = CrossCheck(member_id=member_id, tolerance=tolerance)
    if member_id is None:
        return check

    check.pynite_value = _pynite_magnitude(decision, member_id)
    check.skyciv_value = _skyciv_magnitude(run, member_id)
    check.evaluate()
    return check


def cross_check_table(
    decision: Decision, run: SkyCivRun, tolerance: float = 0.05
) -> list[CrossCheck]:
    """One evaluated CrossCheck per member present on BOTH sides, in the
    decision's member order."""
    table: list[CrossCheck] = []
    for result in decision.member_results:
        if result.axial_force_after is None:
            continue
        if result.member_id not in run.member_results:
            continue
        table.append(
            cross_check(decision, run, member_id=result.member_id, tolerance=tolerance)
        )
    return table
