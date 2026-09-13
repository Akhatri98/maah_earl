"""Start a persistent local server, offline watcher, and ngrok tunnel on astra.

This is a developer deployment command, not a public endpoint or model tool.
It never reads or prints a tunnel credential. It refuses dirty tracked source
so /healthz identifies the commit actually launched. Logs and process IDs are
local files under out/services; the tunnel requires this machine to stay awake.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from earl.watch.state import Ledger


def main():
    branch = subprocess.check_output(["git", "branch", "--show-current"], cwd=ROOT, text=True).strip()
    if branch != "astra":
        raise SystemExit("Refusing deployment: the working branch must be astra.")
    for args in (["git", "diff", "--quiet"], ["git", "diff", "--cached", "--quiet"]):
        if subprocess.run(args, cwd=ROOT).returncode:
            raise SystemExit("Commit tracked source changes on astra before starting the deployment.")
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    agent = Ledger().status()
    if agent["running"] and agent["mode"] != "fixture":
        raise SystemExit("A non-fixture watcher owns the ledger; do not relabel it as a public demo.")
    for port in (8000, 4040):
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                raise SystemExit(f"Port {port} is already in use; do not replace another service.")
    tunnel_binary = shutil.which("ngrok")
    if not tunnel_binary:
        raise SystemExit("The ngrok executable/configuration must already be installed.")
    output = ROOT / "out" / "services"
    output.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment.update(DEMO_MODE="true", EARL_BUILD_SHA=sha, OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1")
    with (output / "server.log").open("ab") as log:
        server = subprocess.Popen([sys.executable, "-m", "uvicorn", "earl.web:app", "--host", "127.0.0.1", "--port", "8000"],
                                  cwd=ROOT, env=environment, stdin=subprocess.DEVNULL, stdout=log,
                                  stderr=subprocess.STDOUT, start_new_session=True)
    with (output / "tunnel.log").open("ab") as log:
        tunnel = subprocess.Popen([tunnel_binary, "http", "http://127.0.0.1:8000", "--log", "stdout", "--log-format", "json"],
                                  cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log,
                                  stderr=subprocess.STDOUT, start_new_session=True)
    public_url = None
    ready = False
    watcher = None
    try:
        for _ in range(30):
            if server.poll() is not None or tunnel.poll() is not None:
                raise RuntimeError("A service stopped. Inspect out/services logs.")
            try:
                health = requests.get("http://127.0.0.1:8000/healthz", timeout=1).json()
                tunnels = requests.get("http://127.0.0.1:4040/api/tunnels", timeout=1).json()["tunnels"]
                public_url = next(t["public_url"] for t in tunnels if t["public_url"].startswith("https://"))
                if health["build"] == sha:
                    ready = True
                    break
            except (requests.RequestException, ValueError, KeyError, StopIteration):
                pass
            time.sleep(0.5)
        if not ready:
            raise RuntimeError("Matching server and tunnel were not available within startup budget.")
        public_health = requests.get(public_url + "/healthz", timeout=10,
                                     headers={"ngrok-skip-browser-warning": "EARL deployment verification"})
        public_health.raise_for_status()
        if public_health.json().get("build") != sha:
            raise RuntimeError("Public URL is not serving the astra commit just launched.")
        if not Ledger().status()["running"]:
            with (output / "watcher.log").open("ab") as log:
                watcher = subprocess.Popen([sys.executable, "-m", "earl", "watch", "--mode", "fixture", "--interval", "2"],
                                           cwd=ROOT, env=environment, stdin=subprocess.DEVNULL, stdout=log,
                                           stderr=subprocess.STDOUT, start_new_session=True)
            for _ in range(20):
                if watcher.poll() is not None:
                    raise RuntimeError("Fixture watcher stopped; inspect out/services/watcher.log")
                if Ledger().status()["running"]:
                    break
                time.sleep(.2)
            else:
                raise RuntimeError("No watcher heartbeat after startup")
    except Exception as exc:
        for process in (watcher, tunnel, server):
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        raise SystemExit(str(exc)) from exc
    record = {"branch": branch, "commit": sha, "url": public_url, "server_pid": server.pid, "tunnel_pid": tunnel.pid,
              "watcher_pid": watcher.pid if watcher else None,
              "note": "Temporary tunnel; this machine, server, watcher and tunnel must remain running. Watcher is fixture-only."}
    (output / "deployment.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(record, indent=2))


if __name__ == "__main__":
    main()
