"""Continuous operation with an offline file-mutation trigger and durable cursor."""

from __future__ import annotations

import hashlib
import json
import logging
import signal
import threading
import time
from pathlib import Path

import fcntl

from .runner import ChangeSource, Runner
from .state import Ledger, atomic_json, utcnow

LOG = logging.getLogger(__name__)


class FixtureTrigger:
    mode = "fixture"

    def __init__(self, path: Path, ledger: Ledger):
        self.path, self.ledger = Path(path), ledger
        if not self.path.exists():
            atomic_json(self.path, {"scenario": "reinforce-chord"})

    def next_change(self) -> tuple[ChangeSource | None, str | None]:
        raw = self.path.read_bytes()
        if len(raw) > 16384:
            raise ValueError("fixture edit exceeds 16 KiB")
        stat = self.path.stat()
        cursor = f"{stat.st_mtime_ns}:{hashlib.sha256(raw).hexdigest()}"
        if self.ledger.read()["cursors"].get(str(self.path)) == cursor:
            return None, None
        payload = json.loads(raw)
        if not isinstance(payload, dict) or set(payload) - {"scenario", "param"}:
            raise ValueError("fixture edit accepts scenario and optional param only")
        return ChangeSource(scenario=payload.get("scenario", "reinforce-chord"), param=payload.get("param"),
                            microversion=f"fixture-{hashlib.sha256(raw).hexdigest()[:12]}"), cursor

    def acknowledge(self, cursor: str) -> None:
        with self.ledger.edit() as state:
            state["cursors"][str(self.path)] = cursor


class Watcher:
    def __init__(self, trigger, runner: Runner, *, interval: float = 2.0):
        if not 0.2 <= interval <= 86400:
            raise ValueError("interval must be between 0.2 and 86400 seconds")
        self.trigger, self.runner, self.interval = trigger, runner, interval
        self.stop = threading.Event()

    def tick(self):
        ledger, result = self.runner.ledger, None
        with ledger.edit() as state:
            state.update(running=True, mode=self.trigger.mode, last_tick=utcnow(),
                         lease_until=time.time() + self.interval + 120)
        try:
            source, cursor = self.trigger.next_change()
            if source is not None:
                result = self.runner.handle_change(source)
                self.trigger.acknowledge(cursor)
                line = f"{result.outcome.upper()} {result.run_id[:8]} {result.notice or result.notification_status}"
            else:
                line = "IDLE no document change"
        except Exception as exc:
            # A bad edit, solver ERROR, or unavailable service must not kill the loop.
            line = f"ERROR {type(exc).__name__}: {str(exc)[:200]}"
            LOG.warning("Watcher tick: %s", line)
        with ledger.edit() as state:
            state["last_message"] = line
        print(f"{utcnow()} [{self.trigger.mode}] {line}", flush=True)
        return result

    def run(self, *, ticks: int | None = None) -> None:
        path = self.runner.ledger.path.parent / "watcher.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("A watcher already owns this ledger") from exc
            old_handlers = {}
            if threading.current_thread() is threading.main_thread():
                for signum in (signal.SIGINT, signal.SIGTERM):
                    old_handlers[signum] = signal.signal(signum, lambda *_: self.stop.set())
            try:
                count = 0
                while not self.stop.is_set():
                    self.tick()
                    count += 1
                    if ticks is not None and count >= ticks:
                        break
                    self.stop.wait(self.interval)
            finally:
                with self.runner.ledger.edit() as state:
                    state.update(running=False, lease_until=0)
                for signum, handler in old_handlers.items():
                    signal.signal(signum, handler)
                fcntl.flock(lock, fcntl.LOCK_UN)


def run_watch(*, mode: str, interval: float | None = None, fixture_file: Path | None = None,
              allow_live: bool = False, ticks: int | None = None) -> None:
    ledger = Ledger()
    # Until a live trigger is explicitly enabled, every mode uses the same
    # safe fixture path. Web visitors have no route to call this function.
    if mode != "fixture":
        LOG.warning("%s trigger unavailable; using the offline fixture trigger", mode)
    trigger = FixtureTrigger(fixture_file or ledger.path.parent / "change.json", ledger)
    Watcher(trigger, Runner(ledger, allow_live=allow_live), interval=interval or 2.0).run(ticks=ticks)
