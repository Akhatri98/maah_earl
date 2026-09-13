"""Optional independent evidence after the unchanged offline pipeline.

This trusted code invokes only the existing adapter and deterministic gate.
The original pipeline trace is retained; the final agent comparison receives
its own durable, validated audit supplement before the notification policy.
"""

from pathlib import Path

from earl.analysis.gate import attach_cross_check
from earl.artifacts.skyciv import create_report
from earl.contracts import Outcome
from earl.delivery.notify import deliver
from earl.pipeline import PipelineResult
from .state import atomic_json, utcnow


def finalize(result: PipelineResult, out_root: Path, *, allow_live: bool) -> PipelineResult:
    result.decision.validate()
    if result.after_graph is None or result.decision.outcome is Outcome.ERROR:
        return result
    run_dir = out_root / "runs" / result.run_id
    original = result.decision.outcome.value
    report, cross = create_report(result.after_graph, result.decision, run_dir, allow_live=allow_live)
    result.decision = attach_cross_check(result.decision, report, cross)
    result.decision.validate()
    atomic_json(run_dir / "agent-cross-check.json", {
        "at": utcnow(), "pipeline_outcome": original, "agent_outcome": result.decision.outcome.value,
        "provenance": report.provenance, "cross_check": cross.to_dict(), "decision": result.decision.to_dict()})
    # Refresh ECN from the final Decision, never send before ledger dedup.
    receipt = deliver(result.decision, run_dir, allow_live=False)
    result.artifacts["delivery"] = receipt.to_dict()
    return result
