"""Sprint 1B: prove the PyNite adapter against the 10-bar truss benchmark.

plan.md (Validation): "Before trusting any output, the PyNite pipeline is
validated against the 10-bar truss benchmark from the structural
optimization literature." This script runs that validation and prints the
report table: the published Case 1 optimum's weight, active stress and
displacement constraints, feasibility, and the project's own equilibrium and
linearity checks on the equal-area structure. See
earl/analysis/benchmark.py for what each check proves and its reference.

Fully offline; no credentials needed.

Run:  python3 scripts/validate_solver.py
Exit: 0 when every check passes, 1 otherwise.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from earl.analysis.benchmark import (  # noqa: E402
    EQUAL_AREA_MEMBER_FORCES_LB,
    LBF,
    LOAD_CASE_ID,
    build_ten_bar_graph,
    validate_solver,
)
from earl.analysis.solver import solve  # noqa: E402


def main() -> int:
    report = validate_solver()
    print(report.to_table())

    # The regression pin is our own output, not a published value; it is
    # shown so a drift is visible next to the published checks.
    result = solve(build_ten_bar_graph(), LOAD_CASE_ID)
    worst = 0.0
    print("\nequal-area regression pin (lbf, tension positive; our own PyNite output):")
    for member_id, pinned in EQUAL_AREA_MEMBER_FORCES_LB.items():
        actual = result.force(member_id).axial_force / LBF
        rel = abs(actual - pinned) / abs(pinned)
        worst = max(worst, rel)
        print(f"  {member_id:<4} pinned {pinned:>12.2f}  actual {actual:>12.2f}  rel {rel:.1e}")
    pin_ok = worst <= 1e-6
    print(f"  max relative deviation {worst:.1e} -> {'PASS' if pin_ok else 'FAIL'}")

    ok = report.passed and pin_ok
    print("\nBENCHMARK PASSED" if ok else "\nBENCHMARK FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
