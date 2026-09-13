"""Continuous operation with an offline file-mutation trigger and durable cursor."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import signal
import threading
import time
from pathlib import Path

import fcntl

from .runner import ChangeSource, Runner
from .state import Ledger, atomic_json, utcnow
from earl.config import demo_mode, load_env

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
            self.runner.retry_notifications()
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


class FallbackTrigger:
    """A failed live trigger gets a durable ERROR; subsequent offline edits still run."""

    def __init__(self, primary, fixture):
        self.primary, self.fixture = primary, fixture
        self.mode = primary.mode
        self.retry_at = 0
        self.last_live = False

    def next_change(self):
        if time.time() >= self.retry_at:
            try:
                source, cursor = self.primary.next_change()
                self.last_live = True
                return source, cursor
            except Exception as exc:
                self.retry_at = time.time() + 3600
                self.last_live = False
                return ChangeSource(source_id=self.primary.source_id, mode=self.primary.mode,
                                    scenario="live trigger unavailable", provenance="live trigger failed; fixture fallback enabled",
                                    error=f"{type(exc).__name__}: {str(exc)[:300]}"), None
        self.last_live = False
        source, cursor = self.fixture.next_change()
        if source:
            source.provenance = "synthetic fixture fallback; live trigger unavailable"
        return source, cursor

    def acknowledge(self, cursor):
        if self.last_live:
            self.primary.acknowledge(cursor)
        elif cursor is not None:
            self.fixture.acknowledge(cursor)


def run_watch(*, mode: str, interval: float | None = None, fixture_file: Path | None = None,
              allow_live: bool = False, ticks: int | None = None, register_url: str | None = None,
              unregister_id: str | None = None, list_hooks: bool = False) -> None:
    ledger = Ledger()
    load_env()
    trigger = FixtureTrigger(fixture_file or ledger.path.parent / "change.json", ledger)
    required = ("ONSHAPE_ACCESS_KEY", "ONSHAPE_SECRET_KEY", "ONSHAPE_DOCUMENT_ID", "ONSHAPE_WORKSPACE_ID")
    if mode != "fixture" and allow_live and not demo_mode() and all(os.environ.get(k) for k in required):
        from earl.ingestion.onshape_client import OnshapeClient
        from .onshape import PollTrigger, WebhookTrigger, DEFAULT_POLL_INTERVAL, MIN_POLL_INTERVAL
        from .webhooks import register
        client = OnshapeClient.from_env()
        client.ledger, client.timeout = ledger, 4
        if register_url:
            try:
                print(json.dumps(register(client, ledger, register_url)), flush=True)
            except Exception as exc:
                LOG.warning("Webhook registration unconfirmed (%s); running fixture fallback", type(exc).__name__)
                Watcher(trigger, Runner(ledger, allow_live=False), interval=2.0).run(ticks=ticks)
                return
        if list_hooks:
            try:
                info = client.list_webhooks()
                print(json.dumps({"webhooks": [{"id": h.get("id"), "name": h.get("name"), "isTransient": h.get("isTransient")}
                                                for h in info.get("items", [])]}), flush=True)
            except Exception as exc:
                print(f"Webhook list unavailable ({type(exc).__name__}); local ledger retained.", flush=True)
            return
        if unregister_id:
            with ledger.edit() as state:
                state["webhooks"].setdefault(unregister_id, {})["desired"] = False
            try:
                client.unregister_webhook(unregister_id)
                print("Webhook unregistered.", flush=True)
            except Exception as exc:
                print(f"Remote unregister unconfirmed ({type(exc).__name__}); automatic renewal disabled locally.", flush=True)
            return
        if mode == "poll":
            interval = max(MIN_POLL_INTERVAL, interval if interval is not None else DEFAULT_POLL_INTERVAL)
            primary = PollTrigger(client, ledger, interval=interval)
        else:
            primary = WebhookTrigger(client, ledger)
        trigger = FallbackTrigger(primary, trigger)
    elif mode != "fixture":
        LOG.warning("Live %s disabled or credentials absent; using the offline fixture trigger", mode)
        if list_hooks or unregister_id:
            print("No webhook administration performed; live access is disabled.", flush=True)
            return
    # Budgeted poll pacing lives in PollTrigger. Wake sooner for queued email retries.
    heartbeat = min(interval, 30.0) if mode == "poll" and interval else interval or 2.0
    Watcher(trigger, Runner(ledger, allow_live=allow_live), interval=heartbeat).run(ticks=ticks)
