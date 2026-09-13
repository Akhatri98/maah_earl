"""The single conversion boundary for benchmark and authored input units.

Legacy SI benchmark constants are retained so the original contract fixture is
unchanged. E_STEEL is a compatibility alias: 1e7 psi is the aluminium benchmark
modulus, NOT a steel specification. See RELIABILITY.md.
"""

from math import isfinite, pi

IN = 0.0254
LBF = 4.4482216152605
PSI = LBF / IN**2
KIP = 1000 * LBF
E_BENCHMARK = 6.895e10
E_STEEL = E_BENCHMARK
YIELD = 2.48e8
AREA = IN**2
P = 444_822.0
HARD_FLOOR = 1.0
DESIGN_TARGET = 1.5


def round_inertia(area: float) -> float:
    """Solid circular bar: I_y = I_z = pi*d^4/64 = A^2/(4*pi)."""
    if not isfinite(area) or area <= 0:
        raise ValueError("section area must be finite and positive")
    return area**2 / (4 * pi)


def to_si(value: float, unit: str) -> float:
    factors = {
        "m": 1.0, "mm": 0.001, "in": IN,
        "m2": 1.0, "mm2": 1e-6, "in2": IN**2,
        "n": 1.0, "kn": 1000.0, "lbf": LBF, "kip": KIP,
        "pa": 1.0, "mpa": 1e6, "psi": PSI,
    }
    normalized = unit.lower().replace("^", "").strip()
    if normalized not in factors or not isfinite(value):
        raise ValueError(f"unsupported unit or nonfinite value: {unit!r}")
    return value * factors[normalized]
