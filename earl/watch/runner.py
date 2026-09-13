"""Fixed autonomous policy: evaluate, persist, deduplicate, and report.

No model is given this runner or any action tool. The existing pipeline is
unchanged and always runs with external sends disabled; notification policy
is applied only after its validated final Decision exists.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from earl.contracts import ChangeEvent, DependencyGraph, Outcome
from earl.pipeline import run_pipeline
from .state import Ledger, utcnow


@dataclass
class ChangeSource:
    source_id: str = "fixture-document"
    mode: str = "fixture"
    microversion: str | None = None
    scenario: str = "thin-compression"
    param: dict | None = None
    graph: DependencyGraph | None = None
    change: ChangeEvent | None = None
    provenance: str = "synthetic fixture trigger"
    recipient: str | None = None

    def fingerprint(self) -> str:
        graph = self.graph.to_dict() if self.graph else None
        if graph:
            graph = {key: graph[key] for key in ("nodes", "members", "sections", "materials", "load_cases",
                                                 "hard_floor", "design_target")}
        change = self.change.to_dict() if self.change else None
        if change:
            change = {key: value for key, value in change.items()
                      if key not in {"id", "timestamp", "description", "value_before", "value_after"}}
        value = {"source_id": self.source_id, "graph": graph, "change": change,
                 "scenario": self.scenario if change is None else None, "param": self.param}
        return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


@dataclass
class RunRecord:
    run_id: str
    timestamp: str
    fingerprint: str
    outcome: str
    governing_member: str | None
    safety_factor: float | None
    violating_member_ids: list[str]
    source: str
    source_id: str
    microversion: str | None
    description: str
    provenance: str
    notice: str | None = None
    notification_status: str = "NONE"
    notification_attempts: int = 0
    error: str | None = None


class Runner:
    def __init__(self, ledger: Ledger | None = None, *, out_root: Path | None = None,
                 allow_live: bool = False):
        self.ledger = ledger or Ledger()
        self.out_root = Path(out_root) if out_root is not None else self.ledger.path.parent.parent
        self.allow_live = allow_live

    def handle_change(self, source: ChangeSource) -> RunRecord:
        fingerprint, run_id = source.fingerprint(), uuid.uuid4().hex
        result = None
        try:
            stream = run_pipeline(scenario=source.scenario, param=source.param, graph=source.graph,
                                  change=source.change, allow_live=False, out_root=self.out_root, run_id=run_id)
            while True:
                try:
                    next(stream)
                except StopIteration as done:
                    result = done.value
                    break
            decision = result.decision
            decision.validate()
            governing = decision.governing_member
            record = RunRecord(run_id, utcnow(), fingerprint, decision.outcome.value,
                               governing.member_id if governing else None,
                               governing.safety_factor if governing else None,
                               sorted(decision.violating_member_ids), source.mode, source.source_id,
                               source.microversion, decision.change_description, source.provenance,
                               error=decision.error_message)
        except Exception as exc:
            record = RunRecord(run_id, utcnow(), fingerprint, Outcome.ERROR.value, None, None, [],
                               source.mode, source.source_id, source.microversion, source.scenario,
                               source.provenance, error=f"Pipeline unavailable: {type(exc).__name__}: {str(exc)[:240]}")

        key = hashlib.sha256(json.dumps([fingerprint, record.violating_member_ids]).encode()).hexdigest()
        with self.ledger.edit() as state:
            if record.outcome == Outcome.ESCALATED.value:
                if key in state["dedup"]:
                    record.notification_status = "SUPPRESSED"
                else:
                    record.notice, record.notification_status = "ESCALATION", "LOCAL"
                    state["dedup"][key] = {"run_id": run_id, "source_id": source.source_id}
                state["open_escalations"][source.source_id] = run_id
            elif record.outcome == Outcome.APPROVED.value and source.source_id in state["open_escalations"]:
                record.notice, record.notification_status = "CLEARED", "LOCAL"
                del state["open_escalations"][source.source_id]
                state["dedup"] = {k: v for k, v in state["dedup"].items() if v["source_id"] != source.source_id}
            if record.notice:
                state["notifications"][run_id] = {"kind": record.notice, "status": "LOCAL", "attempts": 0,
                                                   "source_id": source.source_id, "recipient": source.recipient,
                                                   "next_retry": None}
            state["runs"].append(asdict(record))
            if source.mode != "fixture" and source.microversion:
                state["last_microversion"] = source.microversion

        if record.notice and result is not None:
            # Phase 1: a distinct local notice, never a send. The structural ECN
            # remains the unmodified pipeline's complete evidence artifact.
            from email import policy
            from email.parser import BytesParser
            run_dir = self.out_root / "runs" / run_id
            try:
                message = BytesParser(policy=policy.SMTP).parsebytes((run_dir / "notification.eml").read_bytes())
                message.replace_header("Subject", f"EARL {record.notice} | {record.description[:120]}")
                (run_dir / "agent-notice.eml").write_bytes(message.as_bytes())
            except OSError as exc:
                with self.ledger.edit() as state:
                    state["notifications"][run_id]["status"] = "UNDELIVERED"
                    state["notifications"][run_id]["last_error"] = type(exc).__name__
                    state["runs"][-1]["notification_status"] = "UNDELIVERED"
                record.notification_status = "UNDELIVERED"
        return record


def handle_change(source: ChangeSource) -> RunRecord:
    return Runner().handle_change(source)
