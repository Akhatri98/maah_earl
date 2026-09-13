"""One synchronous, streaming pipeline shared by CLI and FastAPI.

This trusted application runner owns local audit persistence and calls fixed
delivery code. It is NOT a model tool loop: no clients, file handles, callbacks,
or merge/write/send functions are passed to the language model. Its complete
model capability surface is asserted in tests/test_capabilities.py.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator

from earl.analysis import capacity, gate, llm, sanity, solver
from earl.artifacts import skyciv
from earl.config import PROJECT_ROOT
from earl.contracts import ChangeEvent, Decision, DependencyGraph, Outcome
from earl.contracts.common import Serializable
from earl.delivery import notify
from earl.ingestion.source import ingest
from earl.ingestion.truss_map import map_assembly
from earl.scenarios import make_change

LOG = logging.getLogger(__name__)
STAGES = ("ingest", "map", "build", "solve", "sanity", "capacity", "gate", "artifact", "notify")


@dataclass
class StageEvent(Serializable):
    run_id: str
    stage: str
    status: str
    duration_ms: float
    line: str
    data: dict = field(default_factory=dict)


@dataclass
class PipelineResult:
    run_id: str
    decision: Decision
    artifacts: dict
    before_graph: DependencyGraph | None
    after_graph: DependencyGraph | None

    def to_dict(self) -> dict:
        self.decision.validate()
        return {"run_id": self.run_id, "decision": self.decision.to_dict(), "artifacts": self.artifacts,
                "before_graph": self.before_graph.to_dict() if self.before_graph else None,
                "after_graph": self.after_graph.to_dict() if self.after_graph else None}


def run_pipeline(*, scenario: str = "thin-compression", param: dict | None = None,
                 description: str | None = None, change: ChangeEvent | None = None,
                 graph: DependencyGraph | None = None, allow_live: bool = False,
                 out_root: Path | None = None, run_id: str | None = None,
                 solve_timeout: float = 12.0) -> Generator[StageEvent, None, PipelineResult]:
    """Yield exactly one durable event per stage, as each stage completes.

    No background task or in-memory run registry is required. The final notify
    event includes the validated Decision and artifact URLs; exhausting the
    generator returns the same PipelineResult for command-line callers.
    """
    run_id = run_id or uuid.uuid4().hex
    if not re.fullmatch(r"[a-f0-9]{32}", run_id):
        raise ValueError("run ID must be 32 lowercase hexadecimal characters")
    root = Path(out_root) if out_root is not None else PROJECT_ROOT / "out"
    run_dir = root / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    trace = root / "traces" / f"{run_id}.jsonl"
    trace.parent.mkdir(parents=True, exist_ok=True)
    before_graph = after_graph = None
    before = after = None
    before_caps = after_caps = None
    source = None
    decision = None
    artifacts: dict = {}
    failure = None
    selected_case = None
    parsed_provenance = "preset typed delta"

    def failed(exc: Exception, stage: str) -> str:
        nonlocal failure
        failure = f"{stage}: {type(exc).__name__}: {str(exc)[:400]}"
        LOG.warning("Run %s failed: %s", run_id, failure)
        return failure

    def error_result(message: str) -> Decision:
        if decision is not None:
            result = deepcopy(decision)
            result.outcome, result.error_message = Outcome.ERROR, message
            result.escalation_reason = None
            result.validate()
            return result
        current = after_graph or before_graph
        return gate.error_decision(decision_id=run_id, graph_id=current.id if current else "unbuilt",
                                   change_id=current.change.id if current else scenario,
                                   description=current.change.description if current else description or scenario,
                                   message=message)

    for stage in STAGES:
        started = time.perf_counter()
        status, data = "passed", {}
        line = ""
        try:
            if stage == "ingest":
                if graph is None:
                    source = ingest(allow_live=allow_live)
                    line = source.note
                    data = {"provenance": source.provenance, "request_count": source.request_count}
                else:
                    graph.validate()
                    line = "Validated supplied CAD contract; no external request."
                    data = {"provenance": graph.provenance, "request_count": 0}
            elif stage == "map" and not failure:
                before_graph = deepcopy(graph) if graph is not None else map_assembly(
                    source.assembly, source.variables, provenance=source.provenance)
                before_graph.validate()
                line = f"{len(before_graph.nodes)} nodes, {len(before_graph.members)} declared members; SI contract validated."
                data = {"warnings": before_graph.mapping_warnings, "provenance": before_graph.provenance}
            elif stage == "build" and not failure:
                if change is not None and description is not None:
                    raise ValueError("provide one typed change or one description, not both")
                if description is not None:
                    parsed = llm.interpret_change(description, before_graph, allow_live=allow_live)
                    selected_change, parsed_provenance = parsed.change, parsed.provenance
                else:
                    selected_change = deepcopy(change) if change is not None else make_change(before_graph, scenario, param=param)
                before_graph.change = selected_change
                before_graph.validate()
                after_graph = selected_change.apply(before_graph)
                after_graph.validate()
                selected_case, case_provenance = llm.select_load_case(after_graph, selected_change.description, allow_live=allow_live)
                line = selected_change.description
                data = {"change": selected_change.to_dict(), "interpretation": parsed_provenance,
                        "load_case_id": selected_case, "load_case_selection": case_provenance,
                        "before_graph": before_graph.to_dict(), "after_graph": after_graph.to_dict()}
            elif stage == "solve" and not failure:
                before = solver.solve(before_graph, selected_case, timeout=solve_timeout)
                after = solver.solve(after_graph, selected_case, timeout=solve_timeout)
                before.validate()
                after.validate()
                line = f"Benchmark-validated PyNite: before and after, {len(after.stresses)} members."
                data = {"before": before.to_dict(), "after": after.to_dict()}
            elif stage == "sanity" and not failure:
                checks = sanity.combine(before.sanity_checks, after.sanity_checks)
                data = {"sanity_checks": checks.to_dict()}
                if not checks.equilibrium_ok or not checks.linearity_ok:
                    raise ValueError("joint equilibrium or doubled-load linearity failed")
                line = f"Equilibrium residual {checks.max_residual_force:.2e} N; doubled-load linearity passed."
            elif stage == "capacity" and not failure:
                before_caps = capacity.capacities(before_graph, before.axial_forces)
                after_caps = capacity.capacities(after_graph, after.axial_forces)
                incomplete = sum(c.force_capacity is None for c in after_caps.values())
                line = f"Yield and Euler buckling checked; K=1. {incomplete} members not evaluated."
                data = {"before": {k: v.to_dict() for k, v in before_caps.items()},
                        "after": {k: v.to_dict() for k, v in after_caps.items()}}
            elif stage == "gate":
                if failure:
                    decision = error_result(failure)
                    status = "failed"
                else:
                    decision = gate.evaluate(before_graph, after_graph, before, after, decision_id=run_id,
                                             before_capacities=before_caps, after_capacities=after_caps)
                decision.evaluated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                decision.validate()
                governing = decision.governing_member
                line = (f"{decision.outcome.value.upper()}: {governing.member_id} SF {governing.safety_factor:.3f}."
                        if governing else f"{decision.outcome.value.upper()}: {decision.error_message or 'incomplete evaluation'}")
                data = {"decision": decision.to_dict(), "final": False}
            elif stage == "artifact":
                decision.validate()
                if after_graph is not None:
                    report, cross = skyciv.create_report(after_graph, decision, run_dir, allow_live=allow_live)
                    decision = gate.attach_cross_check(decision, report, cross)
                decision.narrative, decision.narrative_provenance = llm.draft_narrative(decision, allow_live=allow_live)
                decision.validate()
                line = (decision.skyciv_report.provenance if decision.skyciv_report else "No external report; analysis error is recorded.")
                if decision.cross_check.agrees is False:
                    line = "CROSS-CHECK DISAGREEMENT. Acceptance held and evidence preserved."
                data = {"decision": decision.to_dict(), "final": True}
            elif stage == "notify":
                decision.validate()
                receipt = notify.deliver(decision, run_dir, allow_live=allow_live)
                artifacts = {"ecn_url": f"/api/run/{run_id}/ecn", "email_url": f"/api/run/{run_id}/email",
                             "email_preview_url": f"/api/run/{run_id}/email-preview",
                             "trace_url": f"/api/run/{run_id}/trace", "report_url": f"/api/run/{run_id}/report",
                             "decision_url": f"/api/run/{run_id}/decision", "delivery": receipt.to_dict()}
                line = f"ECN and .eml saved; {receipt.status} {receipt.recipient}."
                result = PipelineResult(run_id, decision, artifacts, before_graph, after_graph)
                data = result.to_dict()
            else:
                status, line = "skipped", "Not run because an earlier analysis stage failed."
        except Exception as exc:
            status, line = "failed", failed(exc, stage)
            if stage in ("gate", "artifact", "notify"):
                decision = error_result(failure)
                data = {"decision": decision.to_dict(), "final": stage != "gate"}
            if stage == "notify":
                # Disk failures are not disguised as a delivered notice. The
                # final stream still carries the held ERROR and failure line.
                result = PipelineResult(run_id, decision, {"delivery_error": failure}, before_graph, after_graph)
                data = result.to_dict()
        event = StageEvent(run_id, stage, status, round((time.perf_counter() - started) * 1000, 3), line, data)
        with trace.open("a", encoding="utf-8") as output:
            output.write(json.dumps(event.to_dict(), allow_nan=False, separators=(",", ":")) + "\n")
        yield event
    decision.validate()
    return result


def run(**kwargs) -> PipelineResult:
    """Drain the same streaming implementation for non-streaming callers."""
    stream = run_pipeline(**kwargs)
    while True:
        try:
            next(stream)
        except StopIteration as completed:
            return completed.value
