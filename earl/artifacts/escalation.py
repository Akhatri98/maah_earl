"""Escalation: attach the trusted SkyCiv artifact to a Decision. [Track B, 3B]

Stage 3 runs only when the fast gate escalated (or on demand, `always=True`,
for a full paper trail). It never *changes* the verdict: the outcome was
computed by Biject from PyNite numbers and SkyCiv is evidence attached to it,
not a second opinion that can overrule it. Two rules follow from that:

  * A SkyCiv failure is surfaced, not swallowed. The outcome stays exactly as
    it was (never downgraded to APPROVED, never promoted to ERROR -- an
    unreachable report service is not a solver crash), `cross_check` says
    `performed=False`, and the reason lands in the governing member's note
    prefixed "SkyCiv unavailable:" so the ECN shows it.
  * The input Decision is never mutated. The result is a copy made through
    the contract's own JSON round-trip, validated before it is returned.

ERROR decisions are returned untouched regardless of `always`: there is no
model worth sending when the analysis itself did not complete.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import requests

from ..contracts import CrossCheck, Decision, DependencyGraph, Outcome, SkyCivReport
from .crosscheck import cross_check
from .skyciv_client import DEFAULT_DESIGN_CODE, SkyCivClient, SkyCivError

UNAVAILABLE_PREFIX = "SkyCiv unavailable: "
REPORT_SUFFIX = ".pdf"

# Failures that mean "SkyCiv could not be used", as opposed to a bug in our
# own code. ValueError covers a load case the SkyCiv model builder cannot
# find; OSError covers a report that could not be written to report_dir.
_UNAVAILABLE = (SkyCivError, requests.RequestException, OSError, ValueError)


def _append_note(existing: str | None, text: str) -> str:
    """Append, never replace: whatever Biject already said must survive."""
    return f"{existing}; {text}" if existing else text


def _reason(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {str(exc) or 'no message'}"


def _surface(decision: Decision, text: str) -> None:
    """Put `text` where the ECN will show it: the governing member's note, or
    the sanity-check note when no member has a safety factor."""
    governing = decision.governing_member
    if governing is not None:
        governing.note = _append_note(governing.note, text)
    else:
        decision.sanity_checks.note = _append_note(decision.sanity_checks.note, text)


def escalate(
    graph: DependencyGraph,
    decision: Decision,
    client: SkyCivClient,
    *,
    report_dir: Path | None = None,
    always: bool = False,
    design_code: str | None = DEFAULT_DESIGN_CODE,
) -> Decision:
    """Run SkyCiv for an escalated decision and attach report + cross-check.

    Returns a validated copy. The input is returned as-is (after validation)
    for ERROR outcomes and, unless `always`, for APPROVED ones.
    """
    decision.validate()
    if decision.outcome is Outcome.ERROR:
        return decision
    if decision.outcome is Outcome.APPROVED and not always:
        return decision

    out = Decision.from_dict(decision.to_dict())
    governing = out.governing_member
    governing_id = governing.member_id if governing is not None else None

    load_case_id = out.load_case_id
    if load_case_id is None and graph.load_cases:
        load_case_id = graph.load_cases[0].id

    try:
        if load_case_id is None:
            raise ValueError("decision names no load case and the graph has none")
        run = client.analyze(graph, load_case_id, design_code=design_code)
    except _UNAVAILABLE as e:
        out.skyciv_report = None
        out.cross_check = CrossCheck(performed=False, member_id=governing_id)
        _surface(out, UNAVAILABLE_PREFIX + _reason(e))
        out.validate()
        return out

    report = SkyCivReport(
        report_id=f"skyciv-{out.id}",
        url=run.report_url,
        local_path=None,
        generated_at=datetime.now(timezone.utc).isoformat(),
        design_code=run.design_code,
    )
    if report_dir is not None and run.report_url:
        dest = Path(report_dir) / f"{report.report_id}{REPORT_SUFFIX}"
        try:
            report.local_path = str(client.download_report(run.report_url, dest))
        except _UNAVAILABLE as e:
            # The analysis itself succeeded; keep the URL, say why the local
            # copy is missing.
            _surface(out, f"SkyCiv report download failed: {_reason(e)}")

    out.skyciv_report = report
    out.cross_check = cross_check(out, run, member_id=governing_id)
    if run.warnings:
        _surface(out, "SkyCiv warnings: " + " | ".join(run.warnings))

    out.validate()
    return out
