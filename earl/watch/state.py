"""Restart-safe local agent ledger. Atomic replacement and process locks.

The lock is a separate inode: replacing state.json must not replace the lock.
Unreadable/corrupt state fails closed rather than losing dedup or quota history.
"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import fcntl

from earl.config import PROJECT_ROOT


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output, indent=2, allow_nan=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def empty_state() -> dict:
    return {"version": 1, "last_microversion": None, "last_tick": None,
            "running": False, "lease_until": 0, "mode": None, "last_message": "Watcher not started.",
            "runs": [], "dedup": {}, "open_escalations": {}, "cursors": {}, "snapshots": {},
            "notifications": {}, "queue": [], "webhooks": {},
            "api_budget": {"year": datetime.now(timezone.utc).year, "used": 0, "limit": 2500}}


class Ledger:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else PROJECT_ROOT / "out" / "agent" / "state.json"

    def _read(self) -> dict:
        if not self.path.exists():
            return empty_state()
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if value["version"] != 1 or not isinstance(value["runs"], list):
                raise ValueError("unsupported ledger schema")
            for key in ("dedup", "notifications", "cursors", "api_budget", "open_escalations"):
                if not isinstance(value[key], dict):
                    raise ValueError(f"invalid ledger field {key}")
            return value
        except (ValueError, KeyError, TypeError) as exc:
            raise RuntimeError(f"Agent ledger is invalid; preserve and repair {self.path}") from exc

    @contextmanager
    def edit(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix(".lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                state = self._read()
                yield state
                atomic_json(self.path, state)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def read(self) -> dict:
        # Atomic replacement means readers see either complete version, never a partial write.
        return deepcopy(self._read())

    def status(self) -> dict:
        import time
        state = self.read()
        budget = state["api_budget"]
        running = bool(state["running"] and state["lease_until"] > time.time())
        return {"state": "running" if running else "idle", "running": running,
                "mode": state["mode"], "last_tick": state["last_tick"],
                "last_microversion": state["last_microversion"], "message": state["last_message"],
                "remaining_api_calls": max(0, budget["limit"] - budget["used"]),
                "api_budget": budget, "run_count": len(state["runs"]),
                "open_escalations": len(state["open_escalations"]),
                "undelivered": sum(n["status"] == "UNDELIVERED" for n in state["notifications"].values())}

    def public_runs(self) -> list[dict]:
        # No owner address, credentials, raw webhook payload, local path, or cached CAD geometry.
        fields = {"run_id", "timestamp", "fingerprint", "outcome", "governing_member", "safety_factor",
                  "violating_member_ids", "source", "source_id", "microversion", "description",
                  "notice", "notification_status", "notification_attempts", "error", "provenance"}
        return [{key: value for key, value in run.items() if key in fields}
                for run in reversed(self.read()["runs"])]
