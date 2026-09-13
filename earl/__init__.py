"""EARL — Engineering Analysis & Rating Ledger.

Pipeline stages (see Plan/plan.md):
    1. ingestion  — Onshape: read the change, build the dependency graph   [Track A]
    2. analysis   — PyNite fast gate + Biject threshold enforcement        [Track B]
    3. artifacts  — SkyCiv trusted report + PyNite cross-check             [Track B]
    4. delivery   — ECN generation + Gmail delivery                        [Track A]

`earl.contracts` is the shared boundary between the two tracks and is owned
by neither: change it only by agreement (see earl/contracts/README.md).

`earl.pipeline` wires the four stages together (Sprint 4):
`run_pipeline(graph) -> PipelineResult` (walk, decision, ECN, delivery).
"""

__version__ = "0.1.0"
