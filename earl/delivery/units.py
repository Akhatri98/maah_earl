"""Dual-unit rendering for the ECN. [Track A, Sprint 3A]

The contract is SI (`earl/contracts/README.md`: "Units are not optional"), and
the pipeline computes in SI. But the 10-bar truss benchmark is published in
imperial, the CAD variables are authored in inches, and a structural engineer
reads member stress in **ksi**, not pascals. An ECN that says `2.294e8 Pa` is
technically correct and practically unreadable.

So every physical quantity is rendered twice: the payload's declared system
first, the other in parentheses. That is not decoration -- it is the cheapest
possible defence against the exact failure this project exists to catch. A
stress wrong by 10^6 is invisible as `2.294e8 Pa` and obvious the moment it is
printed next to `33.3 ksi`.

Output is deliberately ASCII (`mm^2`, not `mm²`), matching the contract's own
`Units.area = "m^2"`. The demo runs on a Windows console whose default codepage
turns `²` into a replacement character, and mojibake in a live demo reads as a
bug in the engineering rather than in the terminal.

`Units` is READ, never assumed: if a payload declares IMPERIAL, imperial leads
and SI goes in the parentheses. The contract says consumers must check, so
this checks.
"""

from __future__ import annotations

from ..contracts.common import Units, UnitSystem

# -- conversion constants, matching earl/ingestion/benchmark.py --------------

M_PER_IN = 0.0254
PA_PER_PSI = 6894.757
PA_PER_KSI = PA_PER_PSI * 1000.0
N_PER_KIP = 4448.222
M2_PER_IN2 = M_PER_IN ** 2

DASH = "-"          # what a missing number renders as; never "None", never "0"


def _si_first(units: Units) -> bool:
    return units.system is not UnitSystem.IMPERIAL


# -- scalar formatting ------------------------------------------------------

def _sig(value: float, digits: int = 4) -> str:
    """Format without exponent noise for the magnitudes an ECN actually shows."""
    if value == 0:
        return "0"
    magnitude = abs(value)
    if magnitude >= 1e6 or magnitude < 1e-3:
        return f"{value:.{digits - 1}e}"
    decimals = max(0, digits - len(f"{int(magnitude)}")) if magnitude >= 1 else digits
    return f"{value:.{decimals}f}".rstrip("0").rstrip(".")


def _pair(primary: str, secondary: str) -> str:
    return f"{primary} ({secondary})"


# -- quantities -------------------------------------------------------------

def stress(pa: float | None, units: Units) -> str:
    """Stress, rendered in MPa and ksi.

    MPa rather than Pa because a member stress in Pa is an eight-digit number
    that nobody reads; ksi because that is what the benchmark and AISC use.
    """
    if pa is None:
        return DASH
    mpa = pa / 1e6
    ksi = pa / PA_PER_KSI
    si, imp = f"{_sig(mpa)} MPa", f"{_sig(ksi)} ksi"
    return _pair(si, imp) if _si_first(units) else _pair(imp, si)


def force(newtons: float | None, units: Units) -> str:
    """Force in kN and kip -- but small magnitudes stay in N and lbf.

    Member forces are kN-scale, so kN/kip is the right default. Equilibrium
    residuals are not: rendering a residual as `2.6e-13 kN` makes a number
    whose whole point is "indistinguishable from zero" look like a unit error.
    """
    if newtons is None:
        return DASH
    if abs(newtons) < 1000.0:
        si, imp = f"{_sig(newtons)} N", f"{_sig(newtons / (N_PER_KIP / 1000)) } lbf"
        return _pair(si, imp) if _si_first(units) else _pair(imp, si)
    kn = newtons / 1000.0
    kip = newtons / N_PER_KIP
    si, imp = f"{_sig(kn)} kN", f"{_sig(kip)} kip"
    return _pair(si, imp) if _si_first(units) else _pair(imp, si)


def area(m2: float | None, units: Units) -> str:
    if m2 is None:
        return DASH
    mm2 = m2 * 1e6
    in2 = m2 / M2_PER_IN2
    si, imp = f"{_sig(mm2)} mm^2", f"{_sig(in2)} in^2"
    return _pair(si, imp) if _si_first(units) else _pair(imp, si)


def length(m: float | None, units: Units) -> str:
    if m is None:
        return DASH
    inches = m / M_PER_IN
    si, imp = f"{_sig(m)} m", f"{_sig(inches)} in"
    return _pair(si, imp) if _si_first(units) else _pair(imp, si)


# -- dimensionless ----------------------------------------------------------

def ratio(value: float | None, *, decimals: int = 3) -> str:
    """Safety factors and the like -- no units, so no pair."""
    return DASH if value is None else f"{value:.{decimals}f}"


def percent(value: float | None, *, decimals: int = 1) -> str:
    return DASH if value is None else f"{value * 100:.{decimals}f}%"
