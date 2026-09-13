"""Sprint 4 tests: Gmail delivery (Stage 4, the last hop).

Everything runs offline: `GmailSender.post` is injected, so the token exchange
and the send are asserted on the request the module builds, never on the
network.
"""

from __future__ import annotations

import base64
import json
import sys
import tempfile
import unittest
from email import policy
from email.parser import BytesParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from earl.config import GmailConfig  # noqa: E402
from earl.contracts import Outcome  # noqa: E402
from earl.delivery import mock_decisions as mk  # noqa: E402
from earl.delivery.ecn import build_ecn  # noqa: E402
from earl.delivery.gmail import (  # noqa: E402
    Attachment,
    DeliveryReceipt,
    GmailError,
    GmailSender,
    OutboxSender,
    build_message,
    default_attachments,
    deliver,
    subject_for,
)
from earl.delivery.render import render_text  # noqa: E402

CONFIG = GmailConfig(
    client_id="cid",
    client_secret="secret",
    refresh_token="rt",
    sender="earl@example.com",
    recipient="engineer@example.com",
)


class RecordingPost:
    """Stands in for the HTTP layer: records every call, replays answers."""

    def __init__(self, answers: list) -> None:
        self.answers = list(answers)
        self.calls: list[dict] = []

    def __call__(self, url, headers, json_body, form_body):
        self.calls.append({"url": url, "headers": headers, "json": json_body, "form": form_body})
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def parse(raw_b64: str):
    return BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(raw_b64))


class TestMessage(unittest.TestCase):
    def setUp(self):
        self.decision = mk.escalated()
        self.ecn = build_ecn(self.decision)

    def test_subject_carries_the_headline(self):
        subject = subject_for(self.ecn)
        self.assertTrue(subject.startswith("[EARL] HELD - ENGINEERING REVIEW REQUIRED:"))
        self.assertIn("m7", subject)
        self.assertLessEqual(len(subject), 200)

    def test_body_is_the_rendered_ecn_verbatim(self):
        msg = build_message(self.ecn, self.decision, sender="a@x", recipient="b@x")
        body = msg.get_body(preferencelist=("plain",)).get_content()
        self.assertEqual(body.strip(), render_text(self.ecn).strip())

    def test_default_attachments_are_the_evidence_pack(self):
        names = [a.filename for a in default_attachments(self.ecn, self.decision, svg="<svg/>")]
        self.assertEqual(
            names,
            [f"{self.ecn.id}.json", f"{self.decision.id}.json", f"{self.ecn.id}.md", f"{self.ecn.id}.svg"],
        )

    def test_attached_ecn_json_round_trips(self):
        msg = build_message(self.ecn, self.decision, sender="a@x", recipient="b@x")
        parts = {p.get_filename(): p for p in msg.iter_attachments()}
        payload = json.loads(parts[f"{self.ecn.id}.json"].get_content())
        self.assertEqual(payload["id"], self.ecn.id)
        self.assertEqual(payload["status"], "held")

    def test_local_skyciv_report_is_attached_when_it_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "report.pdf"
            pdf.write_bytes(b"%PDF-1.4 fake")
            self.decision.skyciv_report.local_path = str(pdf)
            names = [a.filename for a in default_attachments(self.ecn, self.decision)]
            self.assertIn("report.pdf", names)

    def test_missing_report_file_is_not_attached(self):
        self.decision.skyciv_report.local_path = "/nowhere/report.pdf"
        names = [a.filename for a in default_attachments(self.ecn, self.decision)]
        self.assertNotIn("report.pdf", names)

    def test_headers_identify_the_record(self):
        msg = build_message(self.ecn, self.decision, sender="a@x", recipient="b@x")
        self.assertEqual(msg["X-EARL-ECN"], self.ecn.id)
        self.assertEqual(msg["X-EARL-Decision"], self.decision.id)
        self.assertEqual(msg["X-EARL-Status"], "held")
        self.assertEqual(msg["From"], "a@x")
        self.assertEqual(msg["To"], "b@x")


class TestGmailSender(unittest.TestCase):
    def setUp(self):
        self.decision = mk.escalated()
        self.ecn = build_ecn(self.decision)

    def test_token_then_send(self):
        post = RecordingPost([
            {"access_token": "tok-1", "expires_in": 3600},
            {"id": "msg-123", "threadId": "thr-9"},
        ])
        sender = GmailSender(CONFIG, post=post)
        receipt = deliver(
            self.ecn, self.decision, sender,
            sender_address=CONFIG.sender, recipient=CONFIG.recipient, svg="<svg/>",
        )

        self.assertTrue(receipt.delivered)
        self.assertEqual(receipt.channel, "gmail")
        self.assertEqual(receipt.message_id, "msg-123")
        self.assertEqual(receipt.thread_id, "thr-9")
        self.assertEqual(receipt.to, CONFIG.recipient)
        self.assertIsNotNone(receipt.sent_at)
        self.assertIsNone(receipt.error)

        token_call, send_call = post.calls
        self.assertEqual(token_call["url"], CONFIG.token_url)
        self.assertEqual(token_call["form"]["grant_type"], "refresh_token")
        self.assertEqual(token_call["form"]["refresh_token"], "rt")
        self.assertEqual(send_call["url"], CONFIG.send_url)
        self.assertEqual(send_call["headers"]["Authorization"], "Bearer tok-1")

        msg = parse(send_call["json"]["raw"])
        self.assertEqual(msg["To"], CONFIG.recipient)
        self.assertEqual(msg["Subject"], subject_for(self.ecn))
        names = [p.get_filename() for p in msg.iter_attachments()]
        self.assertEqual(names, receipt.attachments)
        self.assertIn(f"{self.ecn.id}.svg", names)

    def test_token_is_reused_across_sends(self):
        post = RecordingPost([{"access_token": "tok"}, {"id": "1"}, {"id": "2"}])
        sender = GmailSender(CONFIG, post=post)
        msg = build_message(self.ecn, self.decision, sender="a@x", recipient="b@x")
        sender.send(msg)
        sender.send(msg)
        self.assertEqual([c["url"] for c in post.calls], [CONFIG.token_url, CONFIG.send_url, CONFIG.send_url])

    def test_http_failure_is_a_receipt_not_an_exception(self):
        post = RecordingPost([{"access_token": "tok"}, GmailError("HTTP 403 forbidden", status=403)])
        sender = GmailSender(CONFIG, post=post)
        receipt = deliver(
            self.ecn, self.decision, sender, sender_address="a@x", recipient="b@x",
        )
        self.assertFalse(receipt.delivered)
        self.assertEqual(receipt.channel, "gmail")
        self.assertIn("403", receipt.error)
        self.assertEqual(receipt.subject, subject_for(self.ecn))

    def test_token_endpoint_without_token_is_an_error(self):
        post = RecordingPost([{"error": "invalid_grant"}])
        sender = GmailSender(CONFIG, post=post)
        with self.assertRaises(GmailError):
            sender.access_token()

    def test_receipt_round_trips_through_json(self):
        receipt = DeliveryReceipt(True, "gmail", "b@x", "s", ["a.json"], "id", "thr", "2026-09-13T00:00:00Z")
        again = DeliveryReceipt.from_json(receipt.to_json())
        self.assertEqual(again.to_dict(), receipt.to_dict())


class TestOutboxSender(unittest.TestCase):
    def test_writes_an_eml_and_says_it_did_not_deliver(self):
        decision = mk.approved()
        ecn = build_ecn(decision)
        with tempfile.TemporaryDirectory() as tmp:
            receipt = deliver(
                ecn, decision, OutboxSender(Path(tmp) / "outbox"),
                sender_address="a@x", recipient="b@x",
            )
            self.assertFalse(receipt.delivered)
            self.assertEqual(receipt.channel, "outbox")
            path = Path(receipt.message_id)
            self.assertTrue(path.is_file())
            self.assertEqual(path.name, f"{ecn.id}.eml")
            msg = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
            self.assertIn("APPROVED - CHANGE MAY MERGE", msg["Subject"])
            self.assertEqual([p.get_filename() for p in msg.iter_attachments()], receipt.attachments)


class TestConfig(unittest.TestCase):
    def test_from_env_requires_every_variable(self):
        import os

        saved = {k: os.environ.pop(k, None) for k in (
            "GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN",
            "GMAIL_SENDER_ADDRESS", "GMAIL_NOTIFY_RECIPIENT",
        )}
        try:
            os.environ["GMAIL_CLIENT_ID"] = "x"
            self.assertFalse(GmailConfig.configured())
            with self.assertRaises(RuntimeError) as ctx:
                GmailConfig.from_env()
            self.assertIn("GMAIL_", str(ctx.exception))
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
