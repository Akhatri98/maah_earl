"""Serve the demo site, and optionally put it on the internet. [Track A, Sprint 7]

    python3 scripts/serve.py                  # http://127.0.0.1:8000
    python3 scripts/serve.py --ngrok          # + a public URL for the judges
    python3 scripts/serve.py --port 9000
    python3 scripts/serve.py --open           # launch a browser too

The site is `earl.web`: a change bench where anyone can edit the truss and
watch the whole pipeline run on it, the recorded Sprint 5A scoreboard, and
the enforcement demo (Act 4) as a button.

## About `--ngrok`

It shells out to the `ngrok` binary and then asks the local agent API
(127.0.0.1:4040) what URL it got. No new Python dependency, and nothing about
the site changes when the tunnel is up -- the tunnel is a pipe, not a
deployment mode.

**A tunnel makes this public.** Anyone with the URL can drive the bench, so
the server is built for that: it writes nothing, sends nothing, calls no
model, serves a fixed list of files, caps bodies, and rate-limits runs (see
`earl/web/app.py`). Run it while you are demoing and stop it afterwards --
Ctrl-C closes the tunnel with the server.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from earl.config import load_env  # noqa: E402
from earl.web.app import serve  # noqa: E402

NGROK_API = "http://127.0.0.1:4040/api/tunnels"
NGROK_WAIT_S = 20.0
RULE = "=" * 78

# Where a Windows package manager puts ngrok. `winget install` edits the PATH
# of shells started AFTER it, which is never the shell you just installed
# from -- so "ngrok is not on PATH" is the normal first-run state, not a
# broken machine. Look in the obvious places before believing it.
NGROK_FALLBACKS = (
    Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links" / "ngrok.exe",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
    / "Ngrok.Ngrok_Microsoft.Winget.Source_8wekyb3d8bbwe" / "ngrok.exe",
    Path(os.environ.get("ProgramFiles", "")) / "ngrok" / "ngrok.exe",
    Path("/usr/local/bin/ngrok"),
    Path("/opt/homebrew/bin/ngrok"),
)


def ngrok_binary() -> str | None:
    """The ngrok executable, from PATH or from where an installer left it."""
    found = shutil.which("ngrok")
    if found:
        return found
    for candidate in NGROK_FALLBACKS:
        try:
            if candidate.is_file():
                return str(candidate)
        except OSError:
            continue
    return None


# --------------------------------------------------------------------------
# ngrok
# --------------------------------------------------------------------------

def ngrok_url(timeout: float = NGROK_WAIT_S) -> str | None:
    """The public URL, from the local agent API, once it has one.

    The agent takes a second or two to come up and register the tunnel, so
    this polls rather than asking once. A tunnel we cannot name is a tunnel
    the user cannot share, so failing to find the URL is reported rather than
    shrugged off.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(NGROK_API, timeout=2.0) as response:
                tunnels = json.load(response).get("tunnels", [])
            for tunnel in tunnels:
                url = tunnel.get("public_url", "")
                if url.startswith("https://"):
                    return url
            if tunnels:
                return tunnels[0].get("public_url")
        except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
            pass
        time.sleep(0.4)
    return None


def start_ngrok(port: int) -> subprocess.Popen | None:
    """Launch the agent against our port, authenticating from the env.

    The token is read from `NGROK_AUTHTOKEN` (`.env`, gitignored) and handed
    over via `ngrok config add-authtoken`, which is where ngrok wants it. It
    is never printed, and it never appears in an argument we log.
    """
    binary = ngrok_binary()
    if binary is None:
        print("  ngrok: no `ngrok` binary found.")
        print("         Install it -- `winget install Ngrok.Ngrok` on Windows,")
        print("         `brew install ngrok` on a Mac, or https://ngrok.com/download.")
        return None

    token = os.environ.get("NGROK_AUTHTOKEN", "").strip()
    if token:
        try:
            subprocess.run(
                [binary, "config", "add-authtoken", token],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as e:
            first = (e.stderr or e.stdout or "").strip().splitlines()
            print(f"  ngrok: could not store the auth token ({e.returncode}).")
            if first:
                print(f"         {first[0]}")
            return None
    else:
        print("  ngrok: NGROK_AUTHTOKEN is not set; trying an anonymous tunnel.")

    # The agent's own log is kept, not discarded. The one failure that
    # actually happens -- ERR_NGROK_121, an agent too old for the account --
    # is invisible without it, and "the tunnel just did not come up" is a
    # miserable thing to debug in front of an audience.
    log = subprocess.PIPE
    process = subprocess.Popen(
        [binary, "http", str(port), "--log", "stdout", "--log-format", "logfmt"],
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
    )
    threading.Thread(target=_drain_ngrok, args=(process,), daemon=True).start()
    return process


def _drain_ngrok(process: subprocess.Popen) -> None:
    """Read the agent's log, and surface the lines a human needs.

    Everything else is dropped: the agent is chatty and the demo console
    belongs to the request log.
    """
    if process.stdout is None:
        return
    for line in process.stdout:
        lowered = line.lower()
        if "lvl=eror" in lowered or "lvl=crit" in lowered:
            message = line.split("err=", 1)[-1].strip().strip('"')
            print(f"  ngrok: {message[:300]}")
            if "ERR_NGROK_121" in line:
                print("         Fix: `ngrok update` (the agent is older than your "
                      "account allows).")


# --------------------------------------------------------------------------

def banner(local: str, public: str | None) -> None:
    print()
    print(RULE)
    print("  EARL -- the demo site")
    print(RULE)
    print(f"  local    {local}")
    if public:
        print(f"  public   {public}")
        print()
        print("  That URL is open to anyone who has it. The server writes nothing,")
        print("  sends nothing and calls no model -- but stop it when you are done.")
    print()
    print("  Bench    compose a CAD change; watch the walk, the solve, the notice")
    print("  Act 4    'Approve it anyway' -> the contract raises, live")
    print("  Eval     the recorded Sprint 5A scoreboard, reported as measured")
    print()
    print("  Ctrl-C to stop.")
    print(RULE)
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--host", default="127.0.0.1", help="bind address")
    parser.add_argument("--port", type=int, default=8000, help="bind port")
    parser.add_argument("--ngrok", action="store_true", help="open a public tunnel")
    parser.add_argument("--open", action="store_true", help="open a browser")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    load_env()

    try:
        server = serve(args.host, args.port)
    except OSError as e:
        print(f"could not bind {args.host}:{args.port} -- {e}")
        return 2

    local = f"http://{args.host}:{args.port}"
    tunnel = start_ngrok(args.port) if args.ngrok else None
    public = ngrok_url() if tunnel is not None else None
    if tunnel is not None and public is None:
        print("  ngrok: the agent started but never reported a URL.")
        print(f"         Check http://127.0.0.1:4040 -- serving {local} regardless.")

    banner(local, public)
    if args.open:
        threading.Timer(0.5, lambda: webbrowser.open(public or local)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopping…")
    finally:
        server.shutdown()
        server.server_close()
        if tunnel is not None:
            tunnel.terminate()
            try:
                tunnel.wait(timeout=5)
            except subprocess.TimeoutExpired:
                tunnel.kill()
            print("  tunnel closed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
