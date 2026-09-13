"""Durable at-least-once autonomous delivery; no model participates in policy.

A Gmail timeout after remote acceptance is ambiguous. Stable Message-ID and
persisted dedup prevent routine re-emailing, but cannot guarantee exactly-once
delivery. Retrying an unacknowledged send may duplicate it; this is recorded.
"""

from __future__ import annotations

import time
from pathlib import Path

from earl.contracts import Decision
from earl.delivery import notify
from .state import Ledger, utcnow


def resolve_recipient(client, ledger: Ledger) -> str:
    key = f"owner:{client.config.document_id}"
    cached = ledger.read().get("owners", {}).get(key)
    if cached and cached["retry_at"] > time.time():
        return notify.recipient_address(cached.get("email"))
    email, provenance = None, "configured recipient fallback"
    try:
        document = client.get_document()
        owner = document.get("owner") or {}
        # The current public BTOwnerInfo schema does not guarantee email. Do
        # not substitute createdBy/modifiedBy or guess an owner lookup API.
        value = owner.get("email")
        if isinstance(value, str) and notify._address(value, ""):
            email, provenance = value, "document owner email explicitly returned by Onshape"
    except Exception as exc:
        provenance = f"owner lookup unavailable ({type(exc).__name__}); configured recipient fallback"
    with ledger.edit() as state:
        state.setdefault("owners", {})[key] = {"email": email, "provenance": provenance,
                                               "retry_at": time.time() + 86400}
    return notify.recipient_address(email)


def dispatch(ledger: Ledger, out_root: Path, *, allow_live: bool, now: float | None = None) -> int:
    now = time.time() if now is None else now
    count = 0
    for run_id in list(ledger.read()["notifications"]):
        with ledger.edit() as state:
            job = state["notifications"][run_id]
            if job["status"] not in {"PENDING", "UNDELIVERED", "SENDING"}:
                continue
            if job.get("next_retry", 0) is not None and job.get("next_retry", 0) > now:
                continue
            if job.get("allow_live") and not allow_live:
                continue  # A later offline invocation cannot activate a live outbox.
            earlier = list(state["notifications"]).index(run_id)
            if any(other["source_id"] == job["source_id"] and other["status"] not in {"LOCAL", "DELIVERED"}
                   for other in list(state["notifications"].values())[:earlier]):
                continue  # Preserve incident -> cleared order, including across failed sends.
            job.update(status="SENDING", next_retry=now + 120, attempts=job["attempts"] + 1)
            job = dict(job)
        status, error, receipt = "UNDELIVERED", None, None
        try:
            decision = Decision.from_dict(job["decision"])
            decision.validate()
            receipt = notify.deliver(decision, out_root / "runs" / run_id,
                                     allow_live=bool(allow_live and job.get("allow_live")),
                                     recipient=job.get("recipient"), notice_kind=job["kind"])
            if receipt.delivered:
                status = "DELIVERED"
            elif not job.get("allow_live"):
                status = "LOCAL"
            else:
                error = receipt.note if receipt.attempted else "Live delivery configuration incomplete or DEMO_MODE enabled; .eml retained."
            # A failure making the extra copy cannot erase a Gmail acknowledgment.
            try:
                (out_root / "runs" / run_id / "agent-notice.eml").write_bytes(Path(receipt.email_path).read_bytes())
            except OSError:
                error = "Agent attachment copy failed; original notification.eml retained."
        except Exception as exc:
            # Never discard a job because a token, connection, or artifact write failed.
            error = f"{type(exc).__name__}: delivery unconfirmed; retained for retry"
        delay = min(3600, 30 * (2 ** min(job["attempts"] - 1, 7)))
        with ledger.edit() as state:
            current = state["notifications"][run_id]
            current.update(status=status, last_attempt=utcnow(), last_error=error,
                           next_retry=now + delay if status == "UNDELIVERED" else None)
            if receipt:
                current["receipt"] = receipt.to_dict()
                current["recipient"] = receipt.recipient
            for record in state["runs"]:
                if record["run_id"] == run_id:
                    record.update(notification_status=status, notification_attempts=job["attempts"])
                    break
        count += 1
    return count
