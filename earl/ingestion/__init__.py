"""Stage 1 — Ingestion. Onshape change reading and dependency-graph construction. [Track A]"""

from .onshape_client import OnshapeClient, OnshapeError, RequestRecord
from .branch import BranchInfo, BranchSession, evaluate_in_branch
from .changes import detect_changes, detect_variable_changes, undriven_changes
from .walker import Reach, WalkResult, apply_walk, seed_ids, walk, walk_and_apply
from .graph_builder import GraphBuildResult, IdMap, build_graph, build_id_map
from .benchmark import BenchmarkSpec, benchmark_spec, variable_drives
from .parsers import (
    Instance,
    Mate,
    MateConnector,
    TrussTopology,
    Variable,
    WhereUsed,
    build_where_used,
    derive_topology,
    diff_variables,
    mate_edges,
    parse_instances,
    parse_mate_connectors,
    parse_mates,
    parse_variables,
    where_used_edges,
)

__all__ = [
    "OnshapeClient",
    "OnshapeError",
    "RequestRecord",
    "Variable",
    "Instance",
    "Mate",
    "MateConnector",
    "TrussTopology",
    "WhereUsed",
    "parse_variables",
    "diff_variables",
    "parse_instances",
    "parse_mates",
    "parse_mate_connectors",
    "derive_topology",
    "mate_edges",
    "build_where_used",
    "where_used_edges",
    "benchmark_spec",
    "BenchmarkSpec",
    "variable_drives",
    "build_graph",
    "build_id_map",
    "GraphBuildResult",
    "IdMap",
    "walk",
    "walk_and_apply",
    "apply_walk",
    "seed_ids",
    "Reach",
    "WalkResult",
    "detect_changes",
    "detect_variable_changes",
    "undriven_changes",
    "BranchSession",
    "BranchInfo",
    "evaluate_in_branch",
]
