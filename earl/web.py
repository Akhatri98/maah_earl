"""FastAPI transport for the same synchronous pipeline as the CLI.

Public routes never enable live integrations. Parameters are bounded before a
solve, each solve is a killable process, and only two public runs may compute
at once. The eval route reads committed JSON; no background run registry.
"""

from __future__ import annotations

import asyncio
import html
import json
import os
import re
from functools import lru_cache
from pathlib import Path
from threading import BoundedSemaphore

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from earl.analysis.llm import interpret_change
from earl.config import PROJECT_ROOT
from earl.contracts import Decision
from earl.eval.harness import cached_results
from earl.ingestion.source import fixture_inputs
from earl.ingestion.truss_map import map_assembly
from earl.pipeline import STAGES, StageEvent, run_pipeline
from earl.scenarios import AREA_RANGE_IN2, LOAD_RANGE_KN, catalogue, make_change
from earl.units import IN
from earl.watch.state import Ledger
from earl.watch import webhooks

app = FastAPI(title="EARL Structural CI", docs_url=None, redoc_url=None)
OUTPUT_ROOT = PROJECT_ROOT / "out"
COMPUTE_SLOTS = BoundedSemaphore(2)
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data:; connect-src 'self'; frame-src 'self'; object-src 'none'; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'self'")


class BodyLimit:
    """Bound JSON buffering before FastAPI parses an untrusted POST body."""

    def __init__(self, app, max_bytes: int = 16384, read_timeout: float = 5.0):
        self.app, self.max_bytes, self.read_timeout = app, max_bytes, read_timeout

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "POST":
            return await self.app(scope, receive, send)
        body = bytearray()
        try:
            async with asyncio.timeout(self.read_timeout):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    chunk = message.get("body", b"")
                    if len(body) + len(chunk) > self.max_bytes:
                        return await JSONResponse({"detail": "Request body exceeds 16 KiB."}, status_code=413)(scope, receive, send)
                    body.extend(chunk)
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            return await JSONResponse({"detail": "Request body timed out."}, status_code=408)(scope, receive, send)
        consumed = False

        async def replay():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        return await self.app(scope, replay, send)


app.add_middleware(BodyLimit)


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = CSP
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return response


@lru_cache(maxsize=1)
def demo_graph():
    source = fixture_inputs()
    return map_assembly(source.assembly, source.variables, provenance=source.provenance)


@app.get("/", response_class=HTMLResponse)
def index():
    return FileResponse(PROJECT_ROOT / "earl" / "static" / "index.html", media_type="text/html")


@app.get("/healthz")
def health():
    return {"status": "ok", "project": "EARL", "branch": "astra", "public_mode": "offline",
            "build": os.environ.get("RENDER_GIT_COMMIT", os.environ.get("EARL_BUILD_SHA", "local-astra"))}


@app.get("/api/scenarios")
def scenarios():
    graph = demo_graph()
    graph.validate()
    return {"scenarios": catalogue(), "graph": graph.to_dict(), "stages": list(STAGES),
            "limits": {"area_in2": AREA_RANGE_IN2, "load_kn": LOAD_RANGE_KN,
                       "member_ids": [m.id for m in graph.members], "node_ids": ["n1", "n2", "n3", "n4"]},
            "presentation_units": {"square_inch_m2": IN**2},
            "demo_mode": True, "interpretation": "rule-based parser", "branch": "astra"}


class ParseRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


@app.post("/api/parse")
def parse_edit(body: ParseRequest):
    try:
        result = interpret_change(body.text, demo_graph(), allow_live=False)
        return {"change": result.change.to_dict(), "provenance": result.provenance, "note": result.note}
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/stream")
def stream(scenario: str = Query("thin-compression", max_length=80),
           param: str | None = Query(None, max_length=2000),
           description: str | None = Query(None, max_length=2000)):
    try:
        params = json.loads(param) if param is not None else None
        if params is not None and not isinstance(params, dict):
            raise ValueError("param must be a JSON object")
        # Validate/clamp using the shared domain function, before opening SSE.
        if description is not None:
            if param is not None:
                raise ValueError("use one description or one parameter set")
            interpret_change(description, demo_graph(), allow_live=False)
        else:
            make_change(demo_graph(), scenario, param=params).apply(demo_graph())
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    def generate():
        if not COMPUTE_SLOTS.acquire(blocking=False):
            yield 'event: busy\ndata: {"message":"Two runs are active. Try again in a moment."}\n\n'
            return
        try:
            for event in run_pipeline(scenario=scenario, param=params, description=description,
                                      allow_live=False, out_root=OUTPUT_ROOT):
                # The only producer of stages/verdicts is the shared pipeline.
                yield "event: stage\ndata: " + json.dumps(event.to_dict(), allow_nan=False) + "\n\n"
        finally:
            COMPUTE_SLOTS.release()

    return StreamingResponse(generate(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache, no-store", "X-Accel-Buffering": "no",
    })


@app.get("/api/eval")
def evaluation():
    try:
        return JSONResponse(cached_results(), headers={"Cache-Control": "public, max-age=300"})
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="Committed eval cache unavailable.") from exc


@app.get("/api/agent/status")
def agent_status():
    try:
        return JSONResponse(Ledger(OUTPUT_ROOT / "agent" / "state.json").status(),
                            headers={"Cache-Control": "no-store"})
    except (RuntimeError, OSError) as exc:
        raise HTTPException(status_code=503, detail="Agent ledger unavailable; no state reset performed.") from exc


@app.get("/api/agent/runs")
def agent_runs():
    try:
        return JSONResponse({"runs": Ledger(OUTPUT_ROOT / "agent" / "state.json").public_runs()},
                            headers={"Cache-Control": "no-store"})
    except (RuntimeError, OSError) as exc:
        raise HTTPException(status_code=503, detail="Agent ledger unavailable; no state reset performed.") from exc


@app.post("/api/webhook/onshape")
async def onshape_webhook(request: Request):
    if not webhooks.enabled():
        raise HTTPException(status_code=403, detail="Live webhook reception is disabled.")
    raw = await request.body()
    try:
        # Only bounded authentication and a durable inbox append. No API call,
        # pipeline invocation, watcher start, or BackgroundTask in this route.
        result = await asyncio.to_thread(webhooks.receive, raw, request.headers,
                                         Ledger(OUTPUT_ROOT / "agent" / "state.json"))
        return JSONResponse(result, status_code=200, headers={"Cache-Control": "no-store"})
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail="Webhook authentication or scope rejected.") from exc
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="Malformed webhook event.") from exc
    except (OSError, RuntimeError, OverflowError) as exc:
        raise HTTPException(status_code=503, detail="Webhook not persisted; retry required.") from exc


def _run_file(run_id: str, name: str, *, trace: bool = False) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", run_id):
        raise HTTPException(status_code=404, detail="Unknown run")
    path = OUTPUT_ROOT / "traces" / f"{run_id}.jsonl" if trace else OUTPUT_ROOT / "runs" / run_id / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Artifact not found")
    return path


@app.get("/api/run/{run_id}/ecn")
def ecn(run_id: str):
    # Revalidate the stored decision on consume, not just its producer.
    Decision.from_json(_run_file(run_id, "decision.json").read_text()).validate()
    return FileResponse(_run_file(run_id, "ecn.html"), media_type="text/html")


@app.get("/api/run/{run_id}/decision")
def decision(run_id: str):
    value = Decision.from_json(_run_file(run_id, "decision.json").read_text())
    value.validate()
    return JSONResponse(value.to_dict())


@app.get("/api/run/{run_id}/agent-cross-check")
def agent_cross_check(run_id: str):
    path = _run_file(run_id, "agent-cross-check.json")
    value = json.loads(path.read_text(encoding="utf-8"))
    Decision.from_dict(value["decision"]).validate()
    return JSONResponse(value)


@app.get("/api/run/{run_id}/trace")
def trace(run_id: str):
    path = _run_file(run_id, "", trace=True)
    for line in path.read_text(encoding="utf-8").splitlines():
        StageEvent.from_json(line).validate()
    return FileResponse(path, media_type="application/x-ndjson",
                        filename=f"{run_id}.jsonl")


@app.get("/api/run/{run_id}/email")
def email(run_id: str):
    Decision.from_json(_run_file(run_id, "decision.json").read_text()).validate()
    return FileResponse(_run_file(run_id, "notification.eml"), media_type="message/rfc822", filename=f"EARL-{run_id}.eml")


@app.get("/api/run/{run_id}/email-preview", response_class=HTMLResponse)
def email_preview(run_id: str):
    Decision.from_json(_run_file(run_id, "decision.json").read_text()).validate()
    text = _run_file(run_id, "email.txt").read_text()
    return '<!doctype html><html lang="en"><meta charset="utf-8"><title>Email preview</title><style>body{margin:18px;color:#293033;font:12px/1.6 system-ui}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}</style><pre>' + html.escape(text) + "</pre></html>"


@app.get("/api/run/{run_id}/report")
def report(run_id: str):
    return FileResponse(_run_file(run_id, "report.html"), media_type="text/html")


@app.get("/api/run/{run_id}/skyciv-pdf")
def report_pdf(run_id: str):
    return FileResponse(_run_file(run_id, "skyciv.pdf"), media_type="application/pdf")


@app.get("/api/fixture/skyciv")
def sample_report():
    return FileResponse(PROJECT_ROOT / "tests" / "fixtures" / "skyciv" / "published-example.pdf", media_type="application/pdf")


@app.get("/api/source/capabilities", response_class=PlainTextResponse)
def capability_test():
    return (PROJECT_ROOT / "tests" / "test_capabilities.py").read_text()
