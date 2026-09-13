"""The HTTP layer: bytes in, bytes out, no judgement. [Track A, Sprint 7]

`http.server` rather than a framework, deliberately. `requirements.txt` has
three entries and the demo has to survive a `pip install` on a strange
machine ten minutes before we present; a router this small is not worth a
fourth dependency and a new failure mode.

That choice has a real cost and it is worth stating: `ThreadingHTTPServer` is
a development server. It is fine for a handful of judges behind an ngrok
tunnel, which is exactly what it is for, and it is not what you would put in
front of the internet for a week.

## What a request can reach

The public URL is the threat model, so the surface is deliberately tiny:

  * **Static files come from a fixed list**, not from the path. `_STATIC` maps
    a handful of route names to the four files the page needs; anything else
    is a 404 before the filesystem is touched, so there is no traversal to
    get wrong.
  * **Bodies are capped** at `MAX_BODY_BYTES` and must be JSON objects.
  * **Every run is rate limited per client** (`RateLimiter`) and the number of
    concurrent solves is capped (`_SOLVE_SLOTS`), because a truss solve is
    cheap but not free and the URL is one anybody can share.
  * **Nothing is written and nothing is sent.** `api.run_change` calls the
    pipeline with no SkyCiv client and no Gmail sender; no handler here opens
    a file for writing.
"""

from __future__ import annotations

import json
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import BoundedSemaphore, Lock
from typing import Any, Callable
from urllib.parse import urlparse

from . import api

STATIC_DIR = Path(__file__).resolve().parent / "static"

# The entire set of files this server will ever read. A request names a key,
# never a path.
_STATIC: dict[str, tuple[str, str]] = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}

MAX_BODY_BYTES = 16 * 1024
RATE_LIMIT_REQUESTS = 90          # per client
RATE_LIMIT_WINDOW_S = 60.0
MAX_CONCURRENT_SOLVES = 4
SOLVE_WAIT_S = 20.0

_SOLVE_SLOTS = BoundedSemaphore(MAX_CONCURRENT_SOLVES)

# One line per request on the demo console -- watching the judges click is
# half the fun of running it. Tests turn it off so a suite stays readable.
LOG_REQUESTS = True

# Headers on every response. `default-src 'self'` with no 'unsafe-inline' is
# affordable because the CSS and JS are separate files -- which is also why
# the rendered SVG can be injected into the page without giving script a way
# in: a <script> inside injected markup is blocked by script-src.
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; "
        "script-src 'self'; base-uri 'none'; form-action 'none'; "
        "frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}


class RateLimiter:
    """A fixed-window request count per client, in memory.

    Crude on purpose: the job is to stop one shared link from turning into a
    solver benchmark, not to be a CDN. Clients that go quiet are forgotten on
    the next sweep so the table cannot grow without bound.
    """

    def __init__(self, limit: int = RATE_LIMIT_REQUESTS, window: float = RATE_LIMIT_WINDOW_S) -> None:
        self.limit = limit
        self.window = window
        self._hits: dict[str, deque[float]] = {}
        self._lock = Lock()

    def allow(self, client: str) -> bool:
        now = time.monotonic()
        with self._lock:
            if len(self._hits) > 2048:
                self._sweep(now)
            hits = self._hits.setdefault(client, deque())
            while hits and now - hits[0] > self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            return True

    def _sweep(self, now: float) -> None:
        for client in [c for c, h in self._hits.items() if not h or now - h[-1] > self.window]:
            del self._hits[client]


LIMITER = RateLimiter()


class Handler(BaseHTTPRequestHandler):
    server_version = "EARL"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # -- plumbing ----------------------------------------------------------

    @property
    def client_key(self) -> str:
        """Who to rate limit. Behind ngrok every connection arrives from the
        local agent, so the forwarded address is the only thing that
        distinguishes clients -- and it is client-controlled, which is
        acceptable here because the limit is a courtesy, not a security
        boundary. The connection address is the fallback and the floor."""
        forwarded = self.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()[:64]
        return self.client_address[0] if self.client_address else "unknown"

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, allow_nan=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message, "status": status})

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise api.BadRequest("Content-Length must be an integer") from None
        if length < 0 or length > MAX_BODY_BYTES:
            raise api.BadRequest(f"request body must be under {MAX_BODY_BYTES} bytes")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise api.BadRequest(f"body must be JSON: {e}") from None
        if not isinstance(payload, dict):
            raise api.BadRequest("body must be a JSON object")
        return payload

    def log_message(self, fmt: str, *args: Any) -> None:
        # The default writes a timestamp format nobody reads, and the demo
        # console needs to stay legible.
        if LOG_REQUESTS:
            print(f"  {self.client_key:>15}  {fmt % args}")

    # -- routes ------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in _STATIC:
            return self._static(path)
        if path == "/api/model":
            return self._json(200, api.base_model())
        if path == "/api/scoreboard":
            return self._json(200, api.scoreboard())
        if path == "/api/health":
            return self._json(200, {"ok": True, "service": "earl"})
        self._error(404, f"no route for {path}")

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        routes: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "/api/run": self._run,
            "/api/tamper": api.tamper,
        }
        handler = routes.get(path)
        if handler is None:
            return self._error(404, f"no route for {path}")
        if not LIMITER.allow(self.client_key):
            return self._error(
                429,
                f"more than {RATE_LIMIT_REQUESTS} requests in "
                f"{RATE_LIMIT_WINDOW_S:g}s -- slow down and try again",
            )
        try:
            payload = self._read_json()
            self._json(200, handler(payload))
        except api.BadRequest as e:
            self._error(400, str(e))
        except Exception as e:  # never leak a traceback to a public URL
            self.log_message("unhandled %s: %s", type(e).__name__, e)
            self._error(500, f"the run failed: {type(e).__name__}")

    def _run(self, payload: dict[str, Any]) -> dict[str, Any]:
        """A solve, under the concurrency cap.

        A run is ~100 ms, so the cap is about a shared link being hammered
        rather than about any one request being slow. Waiting past
        `SOLVE_WAIT_S` is reported as busy, not queued forever.
        """
        if not _SOLVE_SLOTS.acquire(timeout=SOLVE_WAIT_S):
            raise api.BadRequest("the solver is busy; try that again in a moment")
        try:
            return api.run_change(payload)
        finally:
            _SOLVE_SLOTS.release()

    def _static(self, route: str) -> None:
        name, content_type = _STATIC[route]
        try:
            body = (STATIC_DIR / name).read_bytes()
        except OSError:
            return self._error(404, f"{name} is not installed")
        self._send(200, body, content_type)


def serve(host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    """A bound, not-yet-serving server. The caller runs it, so a script can
    start the tunnel between binding and serving."""
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


__all__ = ["Handler", "LIMITER", "MAX_BODY_BYTES", "RateLimiter", "serve"]
