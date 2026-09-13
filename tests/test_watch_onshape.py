"""Documentation-shaped mocks, NOT evidence of a live Onshape integration."""

import base64
import hashlib
import hmac
import json
import os
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from fastapi.testclient import TestClient

from earl import web
from earl.config import OnshapeConfig
from earl.contracts import ChangeEvent, ChangeKind, TargetKind
from earl.ingestion.budget import CallBudgetExhausted, reserve_call
from earl.ingestion.onshape_client import OnshapeClient
from earl.ingestion.source import fixture_inputs
from earl.ingestion.truss_map import map_assembly
from earl.scenarios import make_change
from earl.watch.changes import equal_physics, physical_model, snapshot_change
from earl.watch.onshape import PollTrigger, WebhookTrigger, SnapshotMoved
from earl.watch.runner import ChangeSource, Runner
from earl.watch.state import Ledger
from earl.watch.watcher import FallbackTrigger, FixtureTrigger, Watcher
from earl.watch.webhooks import MODEL_CHANGED, authenticate, maintain, register

ENV = {"DEMO_MODE": "false", "EARL_ALLOW_LIVE_WEBHOOKS": "true", "ONSHAPE_WEBHOOK_SECRET": "unit-test-only-secret-32-characters-long",
       "ONSHAPE_DOCUMENT_ID": "a" * 24, "ONSHAPE_WORKSPACE_ID": "b" * 24,
       "ONSHAPE_ASSEMBLY_ID": "c" * 24, "ONSHAPE_VARIABLE_ELEMENT_ID": "d" * 24,
       "ONSHAPE_CALL_BUDGET": "2500", "ONSHAPE_CALLS_USED": "0"}


class LiveTriggerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ledger = Ledger(self.root / "agent" / "state.json")
        self.client = OnshapeClient(OnshapeConfig("test", "test", "https://cad.onshape.com", "a"*24, "b"*24), ledger=self.ledger)
        self.source = fixture_inputs()
        self.graph = map_assembly(self.source.assembly, self.source.variables)
        self.env = patch.dict(os.environ, ENV)
        self.env.start()
        self.addCleanup(self.env.stop)

    def response(self, payload, status=200):
        return Mock(status_code=status, content=json.dumps(payload).encode(), json=lambda: payload,
                    is_redirect=False, is_permanent_redirect=False, text=json.dumps(payload))

    def test_budget_reservation_survives_restart_and_refuses_before_network(self):
        with patch.dict(os.environ, {"ONSHAPE_CALL_BUDGET": "1"}), patch("requests.get", return_value=self.response({"microversion": "e" * 24})) as get:
            self.client.current_microversion()
            restarted = OnshapeClient(self.client.config, ledger=Ledger(self.ledger.path))
            with self.assertRaises(CallBudgetExhausted):
                restarted.current_microversion()
            self.assertEqual(get.call_count, 1)
            self.assertEqual(self.ledger.status()["remaining_api_calls"], 0)

    def test_failed_requests_consume_conservative_budget(self):
        with patch("requests.get", side_effect=requests.ConnectionError("offline")):
            with self.assertRaises(requests.ConnectionError):
                self.client.current_microversion()
        self.assertEqual(self.ledger.read()["api_budget"]["used"], 1)
        self.assertEqual(self.client.request_count, 1)

    def test_budget_is_atomic_across_clients(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: reserve_call(Ledger(self.ledger.path), "GET", "/test"), range(25)))
        self.assertEqual(self.ledger.read()["api_budget"]["used"], 25)

    def test_documented_webhook_registration_shape_and_post_restriction(self):
        with patch("requests.post", return_value=self.response({"id": "e" * 24})) as post:
            self.client.register_webhook("https://example.org/api/webhook/onshape", ENV["ONSHAPE_WEBHOOK_SECRET"])
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["events"], [MODEL_CHANGED])
        self.assertFalse(payload["isTransient"])
        self.assertEqual(payload["options"], {"collapseEvents": True})
        self.assertEqual(payload["workspaceId"], "b"*24)
        self.assertFalse(post.call_args.kwargs["allow_redirects"])
        with self.assertRaises(ValueError):
            self.client._post("/api/documents/anything", {})

    def test_poll_only_fetches_geometry_when_microversion_moves_and_is_pinned(self):
        trigger = PollTrigger(self.client, self.ledger)
        with patch.object(self.client, "current_microversion", return_value="e"*24) as micro, patch.object(self.client, "get_assembly_definition", return_value=self.source.assembly) as assembly, patch.object(self.client, "get_variables", return_value=self.source.variables) as variables, patch.object(self.client, "get_document", return_value={"owner": {}}):
            change, cursor = trigger.next_change()
            trigger.acknowledge(cursor)
            self.assertIsNone(trigger.next_change()[0])
            self.assertEqual(micro.call_count, 2)
            self.assertIsNone(trigger.read_changed_snapshot()[0])
            self.assertEqual(assembly.call_count, 1)
            self.assertEqual(variables.call_count, 1)
            variables.assert_called_once_with("d"*24)
            self.assertEqual(assembly.call_args.kwargs["microversion"], "e"*24)
            self.assertEqual(change.provenance, "live Onshape initial baseline verification (no prior history)")
        self.assertIn("assembly", self.ledger.read()["snapshots"][trigger.source_id])

    def test_live_mapping_failure_is_recorded_then_fixture_still_operates(self):
        primary = Mock(mode="poll", source_id="onshape:test", ledger=self.ledger)
        primary.next_change.side_effect = ValueError("empty live document")
        fixture = FixtureTrigger(self.root / "edit.json", self.ledger)
        watcher = Watcher(FallbackTrigger(primary, fixture), Runner(self.ledger, out_root=self.root))
        self.assertEqual(watcher.tick().outcome, "error")
        fallback = watcher.tick()
        self.assertEqual(fallback.outcome, "approved")
        self.assertEqual(fallback.source, "fixture")
        self.assertIn("fallback", fallback.provenance)
        self.assertEqual(primary.next_change.call_count, 1)

    def event(self, kind=MODEL_CHANGED):
        return {"event": kind, "documentId": "a"*24, "workspaceId": "b"*24,
                "webhookId": "e"*24, "messageId": "f"*24, "data": ENV["ONSHAPE_WEBHOOK_SECRET"]}

    def test_http_handshake_queue_replay_and_scope_never_solve_inline(self):
        client = TestClient(web.app)
        with patch.object(web, "OUTPUT_ROOT", self.root), patch("earl.web.run_pipeline") as solve, patch("requests.get") as get:
            self.assertEqual(client.post("/api/webhook/onshape", json=self.event("webhook.register")).status_code, 200)
            self.assertEqual(self.ledger.read()["queue"], [])
            event = self.event()
            event["messageId"] = "1"*24
            self.assertEqual(client.post("/api/webhook/onshape", json=event).status_code, 200)
            self.assertTrue(client.post("/api/webhook/onshape", json=event).json()["duplicate"])
            self.assertEqual(len(self.ledger.read()["queue"]), 1)
            event["workspaceId"] = "9"*24
            self.assertEqual(client.post("/api/webhook/onshape", json=event).status_code, 401)
            event["data"] = "wrong"
            self.assertEqual(client.post("/api/webhook/onshape", json=event).status_code, 401)
            solve.assert_not_called()
            get.assert_not_called()
        self.assertNotIn(ENV["ONSHAPE_WEBHOOK_SECRET"], self.ledger.path.read_text())

    def test_public_demo_rejects_even_authenticated_webhook(self):
        with patch.dict(os.environ, {"DEMO_MODE": "true"}), patch.object(web, "OUTPUT_ROOT", self.root):
            self.assertEqual(TestClient(web.app).post("/api/webhook/onshape", json=self.event()).status_code, 403)
        self.assertFalse(self.ledger.path.exists())

    def test_documented_signature_checks_raw_body_and_timestamp(self):
        raw, timestamp = json.dumps(self.event()).encode(), str(time.time())
        signature = base64.b64encode(hmac.new(b"test-signing-key", timestamp.encode()+b"."+raw, hashlib.sha256).digest()).decode()
        headers = {"x-onshape-webhook-timestamp": timestamp, "x-onshape-webhook-signature-primary": signature}
        with patch.dict(os.environ, {"ONSHAPE_WEBHOOK_SIGNING_SECRET": "test-signing-key"}):
            self.assertTrue(authenticate(raw, headers, self.event()))
            self.assertFalse(authenticate(raw+b" ", headers, self.event()))
            headers["x-onshape-webhook-timestamp"] = "0"
            self.assertFalse(authenticate(raw, headers, self.event()))

    def test_webhook_worker_drains_inbox_and_unchanged_version_uses_only_cheap_read(self):
        trigger = WebhookTrigger(self.client, self.ledger)
        watcher = Watcher(trigger, Runner(self.ledger, out_root=self.root))
        with self.ledger.edit() as state:
            state["queue"].append({"documentId": "a"*24, "workspaceId": "b"*24, "messageId": "f"*24})
        with patch.object(self.client, "current_microversion", return_value="e"*24) as micro, patch.object(self.client, "get_assembly_definition", return_value=self.source.assembly) as assembly, patch.object(self.client, "get_variables", return_value=self.source.variables), patch.object(self.client, "get_document", return_value={"owner": {}}):
            self.assertEqual(watcher.tick().outcome, "approved")
            self.assertEqual(self.ledger.read()["queue"], [])
            with self.ledger.edit() as state:
                state["cursors"][trigger.source_id + ":next_snapshot"] = 0
                state["queue"].append({"documentId": "a"*24, "workspaceId": "b"*24, "messageId": "1"*24})
            self.assertIsNone(watcher.tick())
            self.assertEqual(micro.call_count, 3)
            self.assertEqual(assembly.call_count, 1)
            self.assertEqual(self.ledger.read()["queue"], [])
            self.assertEqual(self.ledger.status()["last_microversion"], "e"*24)

    def test_removed_managed_webhook_is_recreated_and_no_fictional_expiry_is_sent(self):
        from earl.ingestion.onshape_client import OnshapeError
        with self.ledger.edit() as state:
            state["webhooks"]["e"*24] = {"managed": True, "desired": True, "next_check": 0,
                                          "url": "https://example.org/api/webhook/onshape"}
        with patch.object(self.client, "get_webhook", side_effect=OnshapeError(404, "gone", "test")), patch.object(self.client, "list_webhooks", return_value={"items": []}), patch.object(self.client, "register_webhook", return_value={"id": "1"*24, "isTransient": False}) as create:
            maintain(self.client, self.ledger)
        self.assertEqual(create.call_count, 1)
        self.assertFalse(self.ledger.read()["webhooks"]["e"*24]["desired"])
        self.assertTrue(self.ledger.read()["webhooks"]["1"*24]["desired"])

    def test_maintenance_renews_transient_registration_without_every_tick_calls(self):
        with self.ledger.edit() as state:
            state["webhooks"]["e"*24] = {"managed": True, "desired": True, "next_check": 0}
        with patch.object(self.client, "get_webhook", return_value={"isTransient": True}) as get, patch.object(self.client, "renew_webhook") as renew:
            maintain(self.client, self.ledger)
            maintain(self.client, self.ledger)
            self.assertEqual(get.call_count, 1)
            renew.assert_called_once_with("e"*24)

    def test_snapshot_batch_roundtrip_matches_observed_multi_edit(self):
        after = make_change(self.graph, "thin-compression").apply(self.graph)
        after = make_change(after, "custom-load", param={"node": "n1", "load_kn": 50}).apply(after)
        after.node("n1").x += 1
        change = snapshot_change(self.graph, after, "observed")
        self.assertGreater(len(change.deltas), 1)
        replay = ChangeEvent.from_json(change.to_json()).apply(self.graph)
        self.assertTrue(equal_physics(physical_model(replay), physical_model(after)))
        self.assertEqual(ChangeSource(graph=self.graph, change=change).fingerprint(),
                         ChangeSource(graph=self.graph, change=snapshot_change(self.graph, after, "other-id")).fingerprint())

    def test_snapshot_remove_restore_and_unsupported_policy_are_explicit(self):
        removed = make_change(self.graph, "remove-m1").apply(self.graph)
        change = snapshot_change(removed, self.graph, "restore")
        self.assertTrue(equal_physics(physical_model(change.apply(removed)), physical_model(self.graph)))
        after = deepcopy(self.graph)
        after.design_target = 2
        with self.assertRaisesRegex(ValueError, "unsupported"):
            snapshot_change(self.graph, after, "policy")

    def test_name_suffix_and_member_area_mapping(self):
        assembly, variables = deepcopy(self.source.assembly), deepcopy(self.source.variables)
        assembly["rootAssembly"]["instances"][7]["name"] = "  member_m8 <2>  "
        variables[0]["variables"].append({"name": "area_m8", "type": "AREA", "value": .002})
        graph = map_assembly(assembly, variables)
        self.assertEqual(graph.section(graph.member("m8").section_id).area, .002)
        self.assertEqual(len(graph.members), 10)

    def test_variables_request_evaluated_values_at_workspace_not_microversion(self):
        with patch("requests.get", return_value=self.response(self.source.variables)) as get:
            self.client.get_variables("d"*24)
        self.assertIn("/w/" + "b"*24, get.call_args.args[0])
        self.assertEqual(get.call_args.kwargs["params"], {"includeValuesAndReferencedVariables": "true"})

    def test_evaluated_formatted_variables_map_units_without_expression_inference(self):
        variables = [{"variables": [
            {"name": "bayWidth", "type": "LENGTH", "value": "9144 mm", "expression": "do not parse me"},
            {"name": "bayHeight", "type": "LENGTH", "value": "9.144 meter"},
            {"name": "barArea", "type": "ANY", "value": "20 in^2"},
            {"name": "pointLoad", "type": "ANY", "value": "20 kN"},
            {"name": "area_m8", "type": "ANY", "value": "0.002 meter^2"},
        ]}]
        graph = map_assembly(self.source.assembly, variables)
        self.assertAlmostEqual(graph.node("n1").x, 18.288)
        self.assertEqual(graph.section(graph.member("m8").section_id).area, .002)
        self.assertEqual(graph.load_cases[0].point_loads[0].fy, -20000)
        variables[0]["variables"][2]["value"] = "20 kg"
        with self.assertRaises(ValueError):
            map_assembly(self.source.assembly, variables)
        variables[0]["variables"][2]["value"] = "0.01"
        with self.assertRaises(ValueError):
            map_assembly(self.source.assembly, variables)

    def test_workspace_race_discards_snapshot_and_persists_short_retry(self):
        primary = PollTrigger(self.client, self.ledger)
        fixture = FixtureTrigger(self.root / "edit.json", self.ledger)
        trigger = FallbackTrigger(primary, fixture)
        watcher = Watcher(trigger, Runner(self.ledger, out_root=self.root))
        with patch.object(self.client, "current_microversion", side_effect=["e"*24, "f"*24]), patch.object(self.client, "get_assembly_definition", return_value=self.source.assembly), patch.object(self.client, "get_variables", return_value=self.source.variables), patch.object(self.client, "get_document") as owner:
            result = watcher.tick()
        self.assertEqual(result.outcome, "error")
        self.assertEqual(self.ledger.read()["snapshots"], {})
        self.assertEqual(self.ledger.status()["last_microversion"], "f"*24)
        self.assertLessEqual(trigger.retry_at - time.time(), SnapshotMoved.retry_seconds)
        owner.assert_not_called()
        restarted = FallbackTrigger(primary, fixture)
        self.assertEqual(restarted.retry_at, trigger.retry_at)
        self.assertEqual(restarted.effective_mode, "fixture")
