"""Stage 2 — Fast internal analysis. PyNite solver + Biject threshold enforcement. [Track B]

Re-exports the public surface of the Track B analysis modules. Imports are
leaf-first (R16) so nothing here can create a cycle: solver -> sanity ->
biject -> orchestrator -> gate, then benchmark, scoreboard and visualize,
which only depend on the earlier ones. Submodules import each other by
module (`from .solver import solve`), never through this package, and this
package never imports `earl.artifacts`.
"""

from .solver import MemberForce, SolveResult, SolverError, solve
from .sanity import run_sanity_checks
from .biject import MAX_SAFETY_FACTOR, Verdict, member_capacity
from .biject import evaluate as biject_evaluate
from .orchestrator import (
    AnalysisPlan,
    LLMPlanner,
    MetaModelClient,
    RuleBasedPlanner,
    default_planner,
)
from .gate import run_fast_gate
from .benchmark import build_ten_bar_graph, validate_solver
from .scoreboard import ScenarioRecord, Scoreboard, ground_truth, score
from .visualize import render_truss_svg, save_truss_svg

__all__ = [
    # solver
    "solve",
    "SolveResult",
    "SolverError",
    "MemberForce",
    # sanity
    "run_sanity_checks",
    # biject
    "member_capacity",
    "biject_evaluate",
    "Verdict",
    "MAX_SAFETY_FACTOR",
    # orchestrator
    "AnalysisPlan",
    "RuleBasedPlanner",
    "LLMPlanner",
    "MetaModelClient",
    "default_planner",
    # gate
    "run_fast_gate",
    # benchmark
    "build_ten_bar_graph",
    "validate_solver",
    # scoreboard
    "score",
    "ScenarioRecord",
    "Scoreboard",
    "ground_truth",
    # visualize
    "render_truss_svg",
    "save_truss_svg",
]
