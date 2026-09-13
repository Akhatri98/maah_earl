"""Gmail delivery -- Stage 4, the last hop. [Track A, Sprint 4]

plan.md, stage 4:

    "Delivers the SkyCiv report and a lightweight Engineering Change Notice
     (ECN) to the responsible engineer."

This module turns an ECN (plus the Decision behind it and whatever evidence is
on disk) into one e-mail and sends it through the Gmail REST API. Three rules
shape it:

  * THE BODY IS THE RENDERED ECN, UNCHANGED. `render_text()` is the document;
    the e-mail does not paraphrase it, summarise it, or add a model-written
    cover note. What the engineer reads in the inbox is byte-identical to the
    change record that was logged.
  * EVIDENCE IS ATTACHED, NOT DESCRIBED. The ECN JSON (the ledger entry), the
    Decision JSON (the numbers), the SkyCiv report PDF when a local copy
    exists, and the truss SVG when one was rendered. A report that is only a
    URL stays a URL in the body -- the pipeline never fetches something at
    send time that escalation did not already fetch.
  * SENDING IS INJECTABLE AND OPTIONAL. `GmailSender` takes a `post` callable
    so tests never touch the network; `OutboxSender` writes the identical
    message as an .eml file for a demo without credentials. Both return the
    same `DeliveryReceipt`, so the pipeline result looks the same either way
    and the ECN says truthfully how it was delivered.

Send-only. The OAuth refresh token is expected to carry the `gmail.send`
scope and nothing else (see `earl.config.GmailConfig`).
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Any, Callable, Protocol

import requests

from ..config import GmailConfig
from ..contracts.common import Serializable
from ..contracts.decision import Decision
from .ecn import ECN
from .render import render_markdown, render_text

# (url, headers, json_body, form_body) -> parsed JSON response
PostFn = Callable[[str, dict[str, str], dict[str, Any] | None, dict[str, str] | None], dict[str, Any]]

SUBJECT_PREFIX = "[EARL]"
MAX_SUBJECT = 200


class GmailError(RuntimeError):
    """Gmail (or the token endpoint) refused or failed. Carries the HTTP status
    when there was one so the pipeline note can say 401 vs 5xx."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class Attachment:
    filename: str
    content: bytes
    mime_type: str = "application/octet-stream"

    @property
    def maintype(self) -> str:
        return self.mime_type.split("/", 1)[0]

    @property
    def subtype(self) -> str:
        return self.mime_type.split("/", 1)[1] if "/" in self.mime_type else "octet-stream"


@dataclass
class DeliveryReceipt(Serializable):
    """What happened to the notice. Part of the ledger: an ECN that was
    generated but never reached anyone is a dropped domino of a different
    kind, so the pipeline records the outcome of the send, not just the
    attempt."""

    delivered: bool
    channel: str                       # "gmail" | "outbox" | "none"
    to: str
    subject: str
    attachments: list[str] = field(default_factory=list)
    message_id: str | None = None      # Gmail message id, or the .eml path
    thread_id: str | None = None
    sent_at: str | None = None         # ISO-8601 UTC
    error: str | None = None


# --------------------------------------------------------------------------
# Building the message
# --------------------------------------------------------------------------

def subject_for(ecn: ECN) -> str:
    """`[EARL] HELD - ENGINEERING REVIEW REQUIRED: Held for review: ...`,
    clipped so a long change description cannot make the subject unreadable."""
    text = f"{SUBJECT_PREFIX} {ecn.status.headline}: {ecn.title}"
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= MAX_SUBJECT else text[: MAX_SUBJECT - 3] + "..."


def default_attachments(
    ecn: ECN,
    decision: Decision,
    *,
    svg: str | None = None,
    include_markdown: bool = True,
) -> list[Attachment]:
    """The evidence pack: ECN + Decision as JSON (always), the ECN as Markdown,
    the SkyCiv PDF when escalation saved one locally, the SVG when rendered."""
    attachments = [
        Attachment(f"{ecn.id}.json", ecn.to_json().encode("utf-8"), "application/json"),
        Attachment(f"{decision.id}.json", decision.to_json().encode("utf-8"), "application/json"),
    ]
    if include_markdown:
        attachments.append(
            Attachment(f"{ecn.id}.md", render_markdown(ecn).encode("utf-8"), "text/markdown")
        )
    report = decision.skyciv_report
    if report is not None and report.local_path:
        path = Path(report.local_path)
        if path.is_file():
            attachments.append(
                Attachment(path.name or f"{report.report_id}.pdf", path.read_bytes(), "application/pdf")
            )
    if svg:
        attachments.append(Attachment(f"{ecn.id}.svg", svg.encode("utf-8"), "image/svg+xml"))
    return attachments


def build_message(
    ecn: ECN,
    decision: Decision,
    *,
    sender: str,
    recipient: str,
    attachments: list[Attachment] | None = None,
    svg: str | None = None,
) -> EmailMessage:
    """One RFC 5322 message: the rendered ECN as the plain-text body plus the
    evidence pack. Deterministic apart from Date and Message-ID."""
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = subject_for(ecn)
    msg["Date"] = formatdate(localtime=False, usegmt=True)
    msg["Message-ID"] = make_msgid(domain="earl.local")
    msg["X-EARL-ECN"] = ecn.id
    msg["X-EARL-Decision"] = decision.id
    msg["X-EARL-Status"] = ecn.status.value
    msg.set_content(render_text(ecn))

    for att in attachments if attachments is not None else default_attachments(ecn, decision, svg=svg):
        msg.add_attachment(
            att.content,
            maintype=att.maintype,
            subtype=att.subtype,
            filename=att.filename,
        )
    return msg


def attachment_names(msg: EmailMessage) -> list[str]:
    return [
        part.get_filename() for part in msg.iter_attachments() if part.get_filename()
    ]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# --------------------------------------------------------------------------
# Senders
# --------------------------------------------------------------------------

class Sender(Protocol):
    """Anything the pipeline can hand a message to."""

    def send(self, msg: EmailMessage) -> DeliveryReceipt: ...


def _default_post(
    url: str,
    headers: dict[str, str],
    json_body: dict[str, Any] | None,
    form_body: dict[str, str] | None,
) -> dict[str, Any]:
    resp = requests.post(url, headers=headers, json=json_body, data=form_body, timeout=30)
    if resp.status_code >= 400:
        raise GmailError(
            f"HTTP {resp.status_code} from {url}: {resp.text[:300]}", status=resp.status_code
        )
    try:
        data = resp.json()
    except ValueError as e:
        raise GmailError(f"non-JSON body from {url}: {resp.text[:200]!r}", status=resp.status_code) from e
    if not isinstance(data, dict):
        raise GmailError(f"unexpected JSON body type {type(data).__name__} from {url}")
    return data


@dataclass
class GmailSender:
    """Sends through `users.messages.send` with a short-lived access token
    exchanged from the refresh token on first use."""

    config: GmailConfig
    post: PostFn | None = None
    _access_token: str | None = field(default=None, repr=False)
    log: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> GmailSender:
        return cls(GmailConfig.from_env())

    def _post(
        self,
        url: str,
        headers: dict[str, str],
        json_body: dict[str, Any] | None = None,
        form_body: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        try:
            return (self.post or _default_post)(url, headers, json_body, form_body)
        except GmailError:
            raise
        except requests.RequestException as e:
            raise GmailError(f"transport failure for {url}: {e}") from e

    def access_token(self, *, refresh: bool = False) -> str:
        if self._access_token and not refresh:
            return self._access_token
        data = self._post(
            self.config.token_url,
            {"Content-Type": "application/x-www-form-urlencoded"},
            None,
            {
                "client_id": self.config.client_id,
                "client_secret": self.config.client_secret,
                "refresh_token": self.config.refresh_token,
                "grant_type": "refresh_token",
            },
        )
        token = data.get("access_token")
        if not token:
            raise GmailError(f"token endpoint returned no access_token: {json.dumps(data)[:200]}")
        self._access_token = str(token)
        self.log.append("token: refreshed")
        return self._access_token

    def send(self, msg: EmailMessage) -> DeliveryReceipt:
        to = str(msg["To"])
        subject = str(msg["Subject"])
        names = attachment_names(msg)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
        try:
            token = self.access_token()
            data = self._post(
                self.config.send_url,
                {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                {"raw": raw},
                None,
            )
        except GmailError as e:
            self.log.append(f"send: FAILED {e}")
            return DeliveryReceipt(
                delivered=False, channel="gmail", to=to, subject=subject,
                attachments=names, error=f"{type(e).__name__}: {e}",
            )
        self.log.append(f"send: ok id={data.get('id')}")
        return DeliveryReceipt(
            delivered=True,
            channel="gmail",
            to=to,
            subject=subject,
            attachments=names,
            message_id=str(data.get("id")) if data.get("id") is not None else None,
            thread_id=str(data.get("threadId")) if data.get("threadId") is not None else None,
            sent_at=_utc_now(),
        )


@dataclass
class OutboxSender:
    """Writes the exact message Gmail would have received as an .eml file.

    For the demo without credentials, and for the eval harness, which wants
    the artefact without spending a real send per scenario. The receipt says
    `channel="outbox"` so nothing downstream can mistake it for a delivery.
    """

    directory: Path

    def send(self, msg: EmailMessage) -> DeliveryReceipt:
        self.directory = Path(self.directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        stem = re.sub(r"[^A-Za-z0-9._-]+", "_", str(msg["X-EARL-ECN"] or "ecn")) or "ecn"
        path = self.directory / f"{stem}.eml"
        path.write_bytes(msg.as_bytes())
        return DeliveryReceipt(
            delivered=False,
            channel="outbox",
            to=str(msg["To"]),
            subject=str(msg["Subject"]),
            attachments=attachment_names(msg),
            message_id=str(path),
            sent_at=_utc_now(),
        )


def deliver(
    ecn: ECN,
    decision: Decision,
    sender: Sender,
    *,
    sender_address: str,
    recipient: str,
    svg: str | None = None,
    attachments: list[Attachment] | None = None,
) -> DeliveryReceipt:
    """Build the message for `ecn` and hand it to `sender`. Never raises for a
    failed send -- the receipt carries the error, because a delivery failure
    must reach the ledger rather than abort the run after the verdict."""
    msg = build_message(
        ecn, decision, sender=sender_address, recipient=recipient,
        attachments=attachments, svg=svg,
    )
    try:
        return sender.send(msg)
    except Exception as e:  # a sender that raises anyway is still recorded
        return DeliveryReceipt(
            delivered=False,
            channel=getattr(sender, "channel", type(sender).__name__.lower()),
            to=recipient,
            subject=str(msg["Subject"]),
            attachments=attachment_names(msg),
            error=f"{type(e).__name__}: {e}",
        )


__all__ = [
    "Attachment",
    "DeliveryReceipt",
    "GmailError",
    "GmailSender",
    "OutboxSender",
    "Sender",
    "build_message",
    "default_attachments",
    "deliver",
    "subject_for",
]
