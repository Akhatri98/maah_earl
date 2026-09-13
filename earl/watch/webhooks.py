"""Verified Onshape v17 webhook contract; authenticated inbox, never a solver.

Source: cad.onshape.com/api/openapi, 1.220.87929-d54ca734df42 (2026-09-13).
Lifecycle delivery is POST per the current developer guide; the generated
OpenAPI callback entries say GET, an upstream inconsistency documented in
RELIABILITY.md. Registration/ping require an HTTP 200, not a challenge echo.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
from datetime import datetime

from earl.config import demo_mode, load_env
from earl.ingestion.onshape_client import OnshapeError
from .state import Ledger, utcnow

MODEL_CHANGED = "onshape.model.lifecycle.changed"
LIFECYCLE = {"webhook.register", "webhook.ping", "webhook.unregister"}


def enabled() -> bool:
    load_env()
    return not demo_mode() and os.environ.get("EARL_ALLOW_LIVE_WEBHOOKS", "").lower() == "true"


def authenticate(raw: bytes, headers, payload: dict) -> bool:
    signing = os.environ.get("ONSHAPE_WEBHOOK_SIGNING_SECRET")
    if signing:
        timestamp = headers.get("x-onshape-webhook-timestamp", "")
        try:
            seconds = float(timestamp)
            if seconds > 1e12:
                seconds /= 1000
        except ValueError:
            try:
                seconds = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
            except ValueError:
                return False
        if abs(time.time() - seconds) > 300:
            return False
        expected = base64.b64encode(hmac.new(signing.encode(), timestamp.encode() + b"." + raw,
                                            hashlib.sha256).digest()).decode()
        return any(hmac.compare_digest(expected, headers.get(name, "")) for name in
                   ("x-onshape-webhook-signature-primary", "x-onshape-webhook-signature-secondary"))
    # For accounts without company signing settings, `data` is an opaque
    # shared bearer token carried over HTTPS, not an invented signature API.
    secret, received = os.environ.get("ONSHAPE_WEBHOOK_SECRET", ""), payload.get("data")
    return len(secret) >= 32 and isinstance(received, str) and hmac.compare_digest(secret, received)


def receive(raw: bytes, headers, ledger: Ledger) -> dict:
    payload = json.loads(raw)
    if not isinstance(payload, dict) or not authenticate(raw, headers, payload):
        raise PermissionError("Webhook authentication failed")
    event = payload.get("event")
    if event not in LIFECYCLE | {MODEL_CHANGED}:
        raise ValueError("Unsupported webhook event")
    for key in ("webhookId", "messageId"):
        if not isinstance(payload.get(key), str) or not re.fullmatch(r"[0-9a-f]{24}", payload[key]):
            raise ValueError(f"Invalid {key}")
    if payload.get("documentId") != os.environ.get("ONSHAPE_DOCUMENT_ID"):
        raise PermissionError("Unexpected document")
    expected_workspace = os.environ.get("ONSHAPE_WORKSPACE_ID")
    if event == MODEL_CHANGED and payload.get("workspaceId") != expected_workspace:
        raise PermissionError("Unexpected workspace")
    with ledger.edit() as state:
        seen = state.setdefault("webhook_messages", {})
        key = payload["messageId"]
        if key in seen:
            return {"accepted": True, "duplicate": True}
        if event == MODEL_CHANGED and len(state["queue"]) >= 256:
            raise OverflowError("Webhook inbox full; sender must retry")
        seen[key] = utcnow()
        # Bounded replay history; older retries still compare current microversion.
        if len(seen) > 10000:
            del seen[next(iter(seen))]
        if event in LIFECYCLE:
            state["webhooks"].setdefault(payload["webhookId"], {}).update(
                last_event=event, last_event_at=utcnow(), confirmed=event != "webhook.unregister")
        else:
            state["queue"].append({"messageId": key, "webhookId": payload["webhookId"],
                                   "documentId": payload["documentId"], "workspaceId": expected_workspace,
                                   "received_at": time.time()})
    return {"accepted": True, "event": event}


def register(client, ledger: Ledger, url: str) -> dict:
    secret = os.environ.get("ONSHAPE_WEBHOOK_SECRET", "")
    if len(secret) < 32:
        raise ValueError("Set ONSHAPE_WEBHOOK_SECRET to a random secret of at least 32 characters")
    # Onshape does not deduplicate registrations; discover an uncertain prior POST first.
    existing = client.list_webhooks().get("items", [])
    if len(existing) >= 20:
        raise ValueError("Webhook list needs pagination; inspect registrations explicitly before creating another")
    matches = [hook for hook in existing if hook.get("url") == url and hook.get("data") == secret]
    if len(matches) > 1:
        raise ValueError("Multiple matching registrations exist; unregister duplicates explicitly")
    info = matches[0] if matches else client.register_webhook(url, secret)
    hook_id = client._hook_id(info["id"])
    with ledger.edit() as state:
        state["webhooks"].setdefault(hook_id, {}).update(
            managed=True, desired=True, url=url, is_transient=info.get("isTransient", True),
            next_check=time.time() + 86400, registered_at=utcnow())
    if info.get("isTransient", True):
        client.renew_webhook(hook_id)
    return {"id": hook_id, "status": "registered; awaiting authenticated lifecycle callback"}


def maintain(client, ledger: Ledger) -> None:
    for hook_id, hook in ledger.read()["webhooks"].items():
        if not hook.get("managed") or not hook.get("desired") or hook.get("next_check", 0) > time.time():
            continue
        # Failed maintenance must not become a two-second retry loop.
        with ledger.edit() as state:
            state["webhooks"][hook_id]["next_check"] = time.time() + 86400
        try:
            info = client.get_webhook(hook_id)
            if info.get("isTransient", True):
                client.renew_webhook(hook_id)
        except OnshapeError as exc:
            if exc.status != 404:
                raise
            register(client, ledger, hook["url"])
            with ledger.edit() as state:
                state["webhooks"][hook_id]["desired"] = False
