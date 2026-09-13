"""Autonomous notification policy under offline, failed, and mocked live sends."""

import json
import os
import tempfile
import unittest
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import Mock, patch

from earl.config import OnshapeConfig
from earl.ingestion.onshape_client import OnshapeClient
from earl.watch.delivery import resolve_recipient
from earl.watch.runner import ChangeSource, Runner
from earl.watch.state import Ledger

KEYS = {"DEMO_MODE": "false", "GMAIL_REFRESH_TOKEN": "test-only", "GMAIL_CLIENT_ID": "test-only",
        "GMAIL_CLIENT_SECRET": "test-only", "GMAIL_NOTIFY_RECIPIENT": "engineer@company.test",
        "GMAIL_SENDER_ADDRESS": "earl@company.test"}


class AutonomousDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ledger = Ledger(self.root / "agent" / "state.json")
        self.runner = Runner(self.ledger, out_root=self.root, allow_live=True)
        self.source = ChangeSource(mode="webhook", source_id="onshape:mocked", provenance="live Onshape mocked test source")
        self.env = patch.dict(os.environ, KEYS)
        self.env.start()
        self.addCleanup(self.env.stop)

    def post_success(self, *args, **kwargs):
        response = Mock()
        response.json.return_value = {"access_token": "test-only", "id": "gmail-mocked-message"}
        return response

    def test_mock_live_send_deduplicates_and_recovery_is_cleared(self):
        with patch("requests.post", side_effect=self.post_success) as post:
            first = self.runner.handle_change(self.source)
            repeat = self.runner.handle_change(self.source)
            self.source.scenario = "reinforce-chord"
            cleared = self.runner.handle_change(self.source)
        self.assertEqual(first.notification_status, "DELIVERED")
        self.assertEqual(repeat.notification_status, "SUPPRESSED")
        self.assertEqual(cleared.notice, "CLEARED")
        self.assertEqual(cleared.notification_status, "DELIVERED")
        self.assertEqual(post.call_count, 4)  # Two token exchanges and two sends, never the duplicate run.
        message = BytesParser(policy=policy.default).parsebytes((self.root / "runs" / cleared.run_id / "agent-notice.eml").read_bytes())
        self.assertIn("CLEARED", message["Subject"])

    def test_failed_send_persists_undelivered_and_retries_after_restart_with_backoff(self):
        with patch("requests.post", side_effect=ConnectionError("offline")) as post:
            result = self.runner.handle_change(self.source)
            job = self.ledger.read()["notifications"][result.run_id]
            self.assertEqual(job["status"], "UNDELIVERED")
            self.assertEqual(job["attempts"], 1)
            self.assertTrue((self.root / "runs" / result.run_id / "notification.eml").is_file())
            self.runner.retry_notifications(now=job["next_retry"] - 1)
            self.assertEqual(post.call_count, 1)
        restarted = Runner(Ledger(self.ledger.path), out_root=self.root, allow_live=True)
        with patch("requests.post", side_effect=ConnectionError("offline")):
            restarted.retry_notifications(now=job["next_retry"])
        second = self.ledger.read()["notifications"][result.run_id]
        self.assertEqual(second["next_retry"] - job["next_retry"], 60)
        with patch("requests.post", side_effect=self.post_success):
            restarted.retry_notifications(now=second["next_retry"])
        done = self.ledger.read()["notifications"][result.run_id]
        self.assertEqual(done["status"], "DELIVERED")
        self.assertEqual(done["attempts"], 3)

    def test_fixture_cannot_send_even_with_live_flag_and_credentials(self):
        with patch("requests.post") as post:
            result = self.runner.handle_change(ChangeSource())
        self.assertEqual(result.notification_status, "LOCAL")
        post.assert_not_called()

    def test_missing_credentials_retains_live_job_without_claiming_delivery(self):
        with patch.dict(os.environ, {"GMAIL_REFRESH_TOKEN": ""}), patch("requests.post") as post:
            result = self.runner.handle_change(self.source)
        self.assertEqual(result.notification_status, "UNDELIVERED")
        post.assert_not_called()

    def test_offline_restart_cannot_activate_live_job(self):
        with patch("requests.post", side_effect=ConnectionError("offline")):
            result = self.runner.handle_change(self.source)
        job = self.ledger.read()["notifications"][result.run_id]
        with patch("requests.post") as post:
            Runner(self.ledger, out_root=self.root).retry_notifications(now=job["next_retry"] + 1)
        post.assert_not_called()
        self.assertEqual(self.ledger.read()["notifications"][result.run_id]["attempts"], 1)

    def test_cleared_notice_waits_for_earlier_failed_incident_to_preserve_order(self):
        with patch("requests.post", side_effect=ConnectionError("offline")):
            failed = self.runner.handle_change(self.source)
            self.source.scenario = "reinforce-chord"
            cleared = self.runner.handle_change(self.source)
        self.assertEqual(cleared.notification_status, "PENDING")
        job = self.ledger.read()["notifications"][failed.run_id]
        with patch("requests.post", side_effect=self.post_success) as post:
            self.runner.retry_notifications(now=job["next_retry"] + 1)
        self.assertEqual(post.call_count, 4)
        self.assertEqual(self.ledger.read()["notifications"][cleared.run_id]["status"], "DELIVERED")

    def test_owner_email_used_only_when_explicit_and_failure_uses_environment(self):
        client = OnshapeClient(OnshapeConfig("test", "test", "https://cad.onshape.com", "a"*24, "b"*24), ledger=self.ledger)
        with patch.object(client, "get_document", return_value={"owner": {"email": "owner@company.test"}}) as get:
            self.assertEqual(resolve_recipient(client, self.ledger), "owner@company.test")
            self.assertEqual(resolve_recipient(client, self.ledger), "owner@company.test")
            self.assertEqual(get.call_count, 1)
        other = Ledger(self.root / "other" / "state.json")
        with patch.object(client, "get_document", side_effect=ConnectionError("offline")):
            self.assertEqual(resolve_recipient(client, other), "engineer@company.test")
        other = Ledger(self.root / "third" / "state.json")
        with patch.object(client, "get_document", return_value={"owner": {"id": "owner-id"}, "createdBy": {"email": "not-the-owner@company.test"}}):
            self.assertEqual(resolve_recipient(client, other), "engineer@company.test")
