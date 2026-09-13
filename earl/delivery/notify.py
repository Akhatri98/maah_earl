"""Trusted delivery boundary. Always persist an ECN and RFC 5322 email first.

The model is never passed this module or a send callable. Public/demo runs
cannot send. Production delivery additionally requires explicit allow_live,
DEMO_MODE=false, Gmail refresh/client credentials, and a configured recipient.
An unavailable Gmail API leaves the .eml as the deliverable, never loses it.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import format_datetime
from pathlib import Path

import requests

from earl.config import demo_mode, load_env
from earl.contracts import Decision
from . import ecn

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeliveryReceipt:
    recipient: str
    status: str
    note: str
    subject: str
    ecn_path: str
    email_path: str
    decision_path: str

    def to_dict(self) -> dict:
        return asdict(self)


def _address(value: str, default: str) -> str:
    return value if re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", value) else default


def deliver(decision: Decision, run_dir: Path, *, allow_live: bool = False) -> DeliveryReceipt:
    decision.validate()
    load_env()
    run_dir.mkdir(parents=True, exist_ok=True)
    recipient = _address(os.environ.get("GMAIL_NOTIFY_RECIPIENT", os.environ.get("EARL_NOTIFY_TO", "")),
                         "responsible.engineer@example.com")
    sender = _address(os.environ.get("GMAIL_SENDER_ADDRESS", os.environ.get("GMAIL_SENDER", "")), "earl@example.com")
    subject = f"EARL {decision.outcome.value.upper()} | {' '.join(decision.change_description.split())[:120]}"
    rendered = ecn.render(decision)
    plain = ecn.text(decision)
    message = EmailMessage(policy=SMTP)
    message["From"], message["To"], message["Subject"] = sender, recipient, subject
    try:
        issued_at = datetime.fromisoformat(decision.evaluated_at) if decision.evaluated_at else datetime.now(timezone.utc)
        if issued_at.tzinfo is None:
            issued_at = issued_at.replace(tzinfo=timezone.utc)
    except ValueError:
        issued_at = datetime.now(timezone.utc)
    message["Date"] = format_datetime(issued_at)
    message["Message-ID"] = f"<{decision.id}@earl.local>"
    message.set_content(plain)
    message.add_alternative(rendered, subtype="html")
    message.set_boundary(f"earl-{decision.id}")
    data = message.as_bytes()
    decision_path, html_path, email_path = (run_dir / name for name in ("decision.json", "ecn.html", "notification.eml"))
    decision_path.write_text(decision.to_json(), encoding="utf-8")
    html_path.write_text(rendered, encoding="utf-8")
    email_path.write_bytes(data)
    (run_dir / "email.txt").write_text(f"To: {recipient}\nSubject: {subject}\n\n{plain}", encoding="utf-8")
    status, note = "would be sent to", "Offline deliverable saved; no email was sent."
    required = ("GMAIL_REFRESH_TOKEN", "GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET")
    if (allow_live and not demo_mode() and all(os.environ.get(k) for k in required)
            and not recipient.endswith("@example.com") and not sender.endswith("@example.com")):
        try:
            token_response = requests.post("https://oauth2.googleapis.com/token", data={
                "client_id": os.environ["GMAIL_CLIENT_ID"], "client_secret": os.environ["GMAIL_CLIENT_SECRET"],
                "refresh_token": os.environ["GMAIL_REFRESH_TOKEN"], "grant_type": "refresh_token"}, timeout=(2, 4))
            token_response.raise_for_status()
            response = requests.post("https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
                                     headers={"Authorization": f"Bearer {token_response.json()['access_token']}"},
                                     json={"raw": base64.urlsafe_b64encode(data).decode()}, timeout=(2, 4))
            response.raise_for_status()
            if not response.json().get("id"):
                raise ValueError("Gmail did not return a message ID")
            status, note = "sent to", "Gmail acknowledged the message; the .eml remains the local record."
        except Exception as exc:
            LOG.warning("Gmail unavailable (%s); local email retained", type(exc).__name__)
            note = "Gmail delivery could not be confirmed. Local .eml retained; check Sent before retrying."
    receipt = DeliveryReceipt(recipient, status, note, subject, str(html_path), str(email_path), str(decision_path))
    (run_dir / "delivery.json").write_text(json.dumps(receipt.to_dict(), indent=2), encoding="utf-8")
    decision.validate()
    return receipt
