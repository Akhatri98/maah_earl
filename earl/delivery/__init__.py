"""Stage 4 — Sharing. ECN generation and Gmail delivery. [Track A]

Sprint 3A covers the ECN only, built against mocked Track B output
(`mock_decisions`). Gmail delivery is deliberately not here yet.
"""

from .ecn import ECN, ECNLine, ECNStatus, build_ecn
from .render import render_markdown, render_text
from . import mock_decisions, units

__all__ = [
    "ECN",
    "ECNLine",
    "ECNStatus",
    "build_ecn",
    "render_text",
    "render_markdown",
    "mock_decisions",
    "units",
]
