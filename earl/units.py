"""The single conversion boundary for benchmark and authored input units.

Legacy SI benchmark constants are retained so the original contract fixture is
unchanged. E_STEEL is a compatibility alias: 1e7 psi is the aluminium benchmark
modulus, NOT a steel specification. See RELIABILITY.md.
"""

from math import isfinite, pi
import re

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


def from_si(value: float, unit: str) -> float:
    """Convert at an external API/presentation boundary, never inside a solve."""
    return value / to_si(1.0, unit)


def inertia_mm4(value: float) -> float:
    if not isfinite(value) or value < 0:
        raise ValueError("inertia must be nonnegative and finite")
    return value / 0.001**4


def evaluated_variable(value, declared_type: str) -> tuple[str, float | None]:
    """Normalize evaluated CAD values, never evaluate an authored expression.

    Onshape BTVariableInfo.value can be a formatted string. ANY needs an
    explicit recognized unit; bare numeric SI is retained for legacy fixtures.
    Unsupported dimensions/formats stay unevaluated and fail required mapping.
    """
    kind = declared_type.upper()
    if value is None or isinstance(value, bool):
        return kind, None
    match = re.fullmatch(r"\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*(.*?)\s*", str(value))
    if not match:
        return kind, None
    number, unit = float(match[1]), match[2].lower().replace("^", "")
    if not isfinite(number):
        return kind, None
    if not unit:
        return kind, number if kind != "ANY" else None
    aliases = {"meter": "m", "meters": "m", "millimeter": "mm", "millimeters": "mm",
               "inch": "in", "inches": "in", "newton": "n", "newtons": "n",
               "kilonewton": "kn", "kilonewtons": "kn", "meter2": "m2",
               "millimeter2": "mm2", "inch2": "in2"}
    unit = aliases.get(unit, unit)
    dimensions = {"m": "LENGTH", "mm": "LENGTH", "in": "LENGTH",
                  "m2": "AREA", "mm2": "AREA", "in2": "AREA",
                  "n": "FORCE", "kn": "FORCE", "lbf": "FORCE", "kip": "FORCE"}
    dimension = dimensions.get(unit)
    if dimension is None or kind not in (dimension, "ANY"):
        return kind, None
    return dimension, to_si(number, unit)
