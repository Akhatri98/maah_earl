"""Budgeted live triggers. Snapshot fetches are pinned to immutable microversions."""

from __future__ import annotations

import os
import time

from earl.contracts import DependencyGraph
from earl.ingestion.onshape_client import OnshapeClient
from earl.ingestion.truss_map import map_assembly
from .changes import equal_physics, physical_model, snapshot_change
from .runner import ChangeSource
from .state import Ledger

DEFAULT_POLL_INTERVAL = 21600.0  # Four checks/day, 1460/year before any geometry reads.
MIN_POLL_INTERVAL = 3600.0


class PollTrigger:
    mode = "poll"

    def __init__(self, client: OnshapeClient, ledger: Ledger, *, interval=DEFAULT_POLL_INTERVAL):
        self.client, self.ledger, self.interval = client, ledger, max(MIN_POLL_INTERVAL, interval)
        self.client.ledger = ledger
        self.source_id = f"onshape:{client.config.document_id}:{client.config.workspace_id}"
        self.pending_snapshot = None

    def next_change(self):
        # Persist next-check before spending a request so a restart cannot defeat pacing.
        with self.ledger.edit() as state:
            key = self.source_id + ":next_poll"
            if state["cursors"].get(key, 0) > time.time():
                return None, None
            state["cursors"][key] = time.time() + self.interval
        return self.read_changed_snapshot()

    def read_changed_snapshot(self):
        microversion = self.client.current_microversion()
        stored = self.ledger.read()["snapshots"].get(self.source_id)
        if stored and stored["microversion"] == microversion:
            return None, None
        assembly_id = os.environ.get("ONSHAPE_ASSEMBLY_ID")
        variable_id = os.environ.get("ONSHAPE_VARIABLE_ELEMENT_ID")
        if not assembly_id or not variable_id:
            raise ValueError("Set ONSHAPE_ASSEMBLY_ID and ONSHAPE_VARIABLE_ELEMENT_ID; no repeated discovery calls")
        assembly = self.client.get_assembly_definition(assembly_id, microversion=microversion)
        variables = self.client.get_variables(variable_id, microversion=microversion)
        after = map_assembly(assembly, variables, provenance="live Onshape immutable snapshot")
        after.source.workspace_id = self.client.config.workspace_id
        after.source.version_id = microversion
        if after.mapping_warnings:
            raise ValueError("Unmapped live instances: review declared CAD mapping before acceptance")
        before = DependencyGraph.from_dict(stored["graph"]) if stored else after
        change = snapshot_change(before, after, microversion)
        self.pending_snapshot = {"graph": after.to_dict(), "microversion": microversion,
                                 "assembly": assembly, "variables": variables}
        if stored and equal_physics(physical_model(before), physical_model(after)):
            self.acknowledge(microversion)
            return None, None
        provenance = "live Onshape snapshot diff" if stored else "live Onshape initial baseline verification (no prior history)"
        from .delivery import resolve_recipient
        recipient = resolve_recipient(self.client, self.ledger)
        return ChangeSource(self.source_id, self.mode, microversion, graph=before, change=change,
                            provenance=provenance, recipient=recipient), microversion

    def acknowledge(self, cursor):
        if self.pending_snapshot is not None:
            with self.ledger.edit() as state:
                state["snapshots"][self.source_id] = self.pending_snapshot
                state["last_microversion"] = cursor
            self.pending_snapshot = None


class WebhookTrigger(PollTrigger):
    mode = "webhook"

    def __init__(self, client, ledger):
        super().__init__(client, ledger)
        self.message_ids = []

    def next_change(self):
        from .webhooks import maintain
        maintain(self.client, self.ledger)
        state = self.ledger.read()
        messages = [m for m in state["queue"] if m["documentId"] == self.client.config.document_id
                    and m["workspaceId"] == self.client.config.workspace_id]
        if not messages:
            return None, None
        interval = max(10, float(os.environ.get("ONSHAPE_MIN_SNAPSHOT_INTERVAL", "60")))
        with self.ledger.edit() as state:
            key = self.source_id + ":next_snapshot"
            if state["cursors"].get(key, 0) > time.time():
                return None, None
            state["cursors"][key] = time.time() + interval
        self.message_ids = [m["messageId"] for m in messages]
        source, cursor = self.read_changed_snapshot()
        if source is None:
            self.acknowledge(None)
        return source, cursor

    def acknowledge(self, cursor):
        super().acknowledge(cursor)
        with self.ledger.edit() as state:
            state["queue"] = [m for m in state["queue"] if m["messageId"] not in self.message_ids]
        self.message_ids = []
