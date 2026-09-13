"""Offline autonomy, persistent notification policy, and read-only agent routes."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from earl import web
from earl.watch.runner import ChangeSource, Runner
from earl.watch.state import Ledger, atomic_json
from earl.watch.watcher import FixtureTrigger, Watcher


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.ledger = Ledger(self.root / "agent" / "state.json")
        self.runner = Runner(self.ledger, out_root=self.root)

    def test_persistence_and_second_identical_escalation_suppressed_after_restart(self):
        with patch("requests.get") as get, patch("requests.post") as post:
            first = self.runner.handle_change(ChangeSource())
            restarted = Runner(Ledger(self.ledger.path), out_root=self.root)
            second = restarted.handle_change(ChangeSource(microversion="another-trigger-same-edit"))
        self.assertEqual(first.outcome, "escalated")
        self.assertEqual(first.notice, "ESCALATION")
        self.assertEqual(second.notification_status, "SUPPRESSED")
        self.assertEqual(len(restarted.ledger.read()["runs"]), 2)
        self.assertEqual(len(restarted.ledger.read()["notifications"]), 1)
        self.assertTrue((self.root / "runs" / first.run_id / "agent-notice.eml").is_file())
        get.assert_not_called()
        post.assert_not_called()

    def test_recovery_creates_cleared_notice_and_a_new_violation_notifies_again(self):
        self.runner.handle_change(ChangeSource())
        recovered = self.runner.handle_change(ChangeSource(scenario="reinforce-chord"))
        self.assertEqual(recovered.notice, "CLEARED")
        self.assertEqual(self.ledger.status()["open_escalations"], 0)
        self.assertIn("EARL CLEARED", (self.root / "runs" / recovered.run_id / "agent-notice.eml").read_text())
        self.assertEqual(self.runner.handle_change(ChangeSource()).notice, "ESCALATION")

    def test_fixture_trigger_full_pipeline_and_cursor_survive_restart(self):
        fixture = self.root / "edit.json"
        atomic_json(fixture, {"scenario": "thin-compression"})
        watcher = Watcher(FixtureTrigger(fixture, self.ledger), self.runner)
        result = watcher.tick()
        self.assertEqual(result.outcome, "escalated")
        self.assertTrue((self.root / "traces" / f"{result.run_id}.jsonl").is_file())
        restarted = Watcher(FixtureTrigger(fixture, Ledger(self.ledger.path)), self.runner)
        self.assertIsNone(restarted.tick())
        atomic_json(fixture, {"scenario": "reinforce-chord"})
        self.assertEqual(restarted.tick().notice, "CLEARED")

    def test_pipeline_error_does_not_kill_loop_or_clear_open_escalation(self):
        fixture = self.root / "edit.json"
        self.runner.handle_change(ChangeSource())
        atomic_json(fixture, {"scenario": "unknown"})
        watcher = Watcher(FixtureTrigger(fixture, self.ledger), self.runner)
        self.assertEqual(watcher.tick().outcome, "error")
        self.assertEqual(self.ledger.status()["open_escalations"], 1)
        atomic_json(fixture, {"scenario": "reinforce-chord"})
        self.assertEqual(watcher.tick().notice, "CLEARED")

    def test_unexpected_pipeline_exception_is_a_durable_error(self):
        with patch("earl.watch.runner.run_pipeline", side_effect=RuntimeError("injected")):
            result = self.runner.handle_change(ChangeSource())
        self.assertEqual(result.outcome, "error")
        self.assertEqual(self.ledger.read()["runs"][0]["outcome"], "error")

    def test_shutdown_releases_running_status(self):
        watcher = Watcher(FixtureTrigger(self.root / "edit.json", self.ledger), self.runner)
        watcher.run(ticks=1)
        self.assertFalse(self.ledger.status()["running"])

    def test_corrupt_ledger_is_not_silently_reset(self):
        self.ledger.path.parent.mkdir(parents=True)
        self.ledger.path.write_text("broken")
        with self.assertRaises(RuntimeError):
            self.ledger.read()
        self.assertEqual(self.ledger.path.read_text(), "broken")

    def test_public_routes_cannot_start_watcher_or_call_external_service(self):
        self.runner.handle_change(ChangeSource(recipient="private@example.org"))
        client = TestClient(web.app)
        with patch.object(web, "OUTPUT_ROOT", self.root), patch("requests.get") as get, patch("requests.post") as post:
            response = client.get("/api/agent/runs")
            self.assertNotIn("private@example.org", response.text)
            self.assertEqual(client.get("/api/agent/status").json()["run_count"], 1)
            self.assertEqual(client.post("/api/agent/status").status_code, 405)
            self.assertEqual(client.post("/api/agent/start").status_code, 404)
        get.assert_not_called()
        post.assert_not_called()
