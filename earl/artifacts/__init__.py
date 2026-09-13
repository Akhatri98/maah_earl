"""Stage 3 — Trusted artifact creation. SkyCiv report generation and cross-check. [Track B]

Re-exports the public surface of the Track B artifact modules, leaf-first
(R16): skyciv_client, then crosscheck (needs SkyCivRun), then escalation
(needs both). This package imports `earl.analysis` never and `earl.contracts`
only through its submodules; `earl.analysis` never imports this package, so
the two halves of Track B stay independently importable.
"""

from .skyciv_client import (
    DEFAULT_DESIGN_CODE,
    SkyCivClient,
    SkyCivError,
    SkyCivRun,
    build_s3d_model,
)
from .crosscheck import cross_check, cross_check_table
from .escalation import escalate

__all__ = [
    "SkyCivClient",
    "SkyCivError",
    "SkyCivRun",
    "build_s3d_model",
    "DEFAULT_DESIGN_CODE",
    "cross_check",
    "cross_check_table",
    "escalate",
]
