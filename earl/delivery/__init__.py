"""Stage 4 — Sharing. ECN generation and Gmail delivery. [Track A]

Sprint 3A built the ECN against mocked Track B output (`mock_decisions`);
Sprint 4 replaced the mock with real `run_fast_gate()` output (see
`earl.pipeline`) and added the Gmail hop (`gmail`). `mock_decisions` stays
as the ECN template's fixed test fixture -- its numbers are reproduced by the
real gate under the agreed capacity convention (tests/test_pipeline.py).
"""

from .ecn import ECN, ECNLine, ECNStatus, build_ecn
from .gmail import (
    Attachment,
    DeliveryReceipt,
    GmailError,
    GmailSender,
    OutboxSender,
    build_message,
    deliver,
)
from .render import render_markdown, render_text
from . import mock_decisions, units

__all__ = [
    "ECN",
    "ECNLine",
    "ECNStatus",
    "build_ecn",
    "render_text",
    "render_markdown",
    "Attachment",
    "DeliveryReceipt",
    "GmailError",
    "GmailSender",
    "OutboxSender",
    "build_message",
    "deliver",
    "mock_decisions",
    "units",
]
