"""LIVE SkyCiv smoke test: one real analysis of the 10-bar truss. [Track B, 3B]

The SkyCiv request/response shape in earl/artifacts/skyciv_client.py was
written WITHOUT access to the docs site and is unverified until this script
has run once. It:

  1. refuses to run without SKYCIV_API_USERNAME / SKYCIV_API_KEY (from the
     environment or .env) -- it never touches the network otherwise;
  2. analyses the 10-bar graph once through SkyCivClient.analyze(), which
     records both raw responses to tests/fixtures/skyciv/recorded/
     (skyciv_analyze_1.json, skyciv_analyze_2.json);
  3. if the report function FN_REPORT came back with status != 0, tries the
     fallback FN_REPORT_ALT (one extra call);
  4. prints the raw result shape it saw, SkyCiv's axial forces next to
     PyNite's (so the sign convention and the unit mapping can be settled),
     the warnings, the report link and the design ratios.

Costs 1-3 metered API calls. NEVER run by the tests.

Run:  python3 scripts/skyciv_smoke.py [--demo] [--no-design] [--no-report]
      python3 scripts/skyciv_smoke.py --graph graph.json --load-case lc_1
Exit: 0 when the analysis ran, 2 when credentials are missing, 1 on failure.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from earl.analysis.benchmark import (  # noqa: E402
    LOAD_CASE_ID,
    build_ten_bar_graph,
    demo_change_graph,
)
from earl.analysis.solver import SolverError, solve  # noqa: E402
from earl.artifacts.skyciv_client import (  # noqa: E402
    DEFAULT_DESIGN_CODE,
    FN_MODEL_SET,
    FN_MODEL_SOLVE,
    FN_REPORT,
    FN_REPORT_ALT,
    FN_SESSION_START,
    REPORT_FILE_TYPE,
    SkyCivClient,
    SkyCivError,
    SkyCivRun,
    build_s3d_model,
    parse_report_link,
)
from earl.config import PROJECT_ROOT, load_env  # noqa: E402
from earl.contracts import DependencyGraph  # noqa: E402

DEFAULT_RECORD_DIR = PROJECT_ROOT / "tests" / "fixtures" / "skyciv" / "recorded"
RECORD_SLUG_REPORT_ALT = "skyciv_report_alt"

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_NO_CREDENTIALS = 2


def describe(value: Any, depth: int = 0, max_depth: int = 4, indent: str = "  ") -> list[str]:
    """A compact picture of a JSON value: types, keys, list lengths, a sample
    of scalars -- enough to see where the numbers live without dumping MBs."""
    pad = indent * depth
    if isinstance(value, dict):
        lines = [f"{pad}dict[{len(value)}]"]
        if depth >= max_depth:
            return lines + [f"{pad}{indent}keys: {list(value)[:12]}"]
        for i, (k, v) in enumerate(value.items()):
            if i >= 8:
                lines.append(f"{pad}{indent}... {len(value) - 8} more keys")
                break
            lines.append(f"{pad}{indent}{k!r}:")
            lines.extend(describe(v, depth + 2, max_depth, indent))
        return lines
    if isinstance(value, list):
        lines = [f"{pad}list[{len(value)}]"]
        if value and depth < max_depth:
            lines.append(f"{pad}{indent}[0]:")
            lines.extend(describe(value[0], depth + 2, max_depth, indent))
        return lines
    text = repr(value)
    return [f"{pad}{type(value).__name__}: {text[:80]}{'...' if len(text) > 80 else ''}"]


def _function_summary(raw: dict[str, Any]) -> list[str]:
    lines = [f"top-level keys: {list(raw)}"]
    functions = raw.get("functions")
    if isinstance(functions, list):
        for i, entry in enumerate(functions):
            if not isinstance(entry, dict):
                continue
            data = entry.get("data")
            shape = type(data).__name__
            if isinstance(data, dict):
                shape += f" keys={list(data)[:10]}"
            elif isinstance(data, list):
                shape += f" len={len(data)}"
            lines.append(
                f"  [{i}] {entry.get('function', '?')}: status={entry.get('status')} "
                f"msg={str(entry.get('msg', ''))[:100]!r} data={shape}"
            )
    else:
        lines.append("  (no 'functions' list; see the raw shape below)")
    return lines


def _report_failed(run: SkyCivRun) -> bool:
    return run.report_url is None and any(FN_REPORT in w for w in run.warnings)


def retry_report_alt(client: SkyCivClient, graph: DependencyGraph, load_case_id: str,
                     run: SkyCivRun, *, session_open: bool) -> str | None:
    """FN_REPORT failed: try FN_REPORT_ALT.

    `session_open` says whether call 1 asked SkyCiv to keep its session open
    (SkyCivClient.analyze does so only when a design check was requested).
    A session started with keep_open=False is gone once call 1 returns, even
    though its id is still in `run.session_id`, so the report can only be
    re-used from a session that is BOTH open and known; otherwise the model
    is set and solved again in the same call, which is one extra metered
    call rather than a guaranteed "session not found"."""
    functions: list[dict[str, Any]] = []
    reuse = session_open and run.session_id is not None
    if not reuse:
        s3d = build_s3d_model(graph, load_case_id)
        functions += [
            {"function": FN_SESSION_START, "arguments": {"keep_open": False}},
            {"function": FN_MODEL_SET, "arguments": {"s3d_model": s3d.model}},
            {"function": FN_MODEL_SOLVE, "arguments": {"analysis_type": "linear", "repair_model": True}},
        ]
    functions.append({"function": FN_REPORT_ALT, "arguments": {"file_type": REPORT_FILE_TYPE}})
    raw = client.call(functions, label=RECORD_SLUG_REPORT_ALT,
                      session_id=run.session_id if reuse else None)
    run.raw["report_alt"] = raw
    print(f"\n-- {FN_REPORT_ALT} response --")
    for line in _function_summary(raw):
        print(line)
    for entry in raw.get("functions", []) if isinstance(raw.get("functions"), list) else []:
        if isinstance(entry, dict) and entry.get("function") == FN_REPORT_ALT:
            if entry.get("status") == 0:
                return parse_report_link(entry.get("data"))
            print(f"  {FN_REPORT_ALT} also failed: {entry.get('msg')}")
    return None


def print_force_table(graph: DependencyGraph, load_case_id: str, run: SkyCivRun) -> None:
    try:
        pynite = solve(graph, load_case_id)
    except SolverError as e:
        print(f"PyNite could not solve the graph: {e}")
        return
    print(f"\n{'member':<7} {'PyNite N':>14} {'SkyCiv N':>14} {'|rel diff|':>11}  sign")
    same = opposite = 0
    for m in graph.members:
        p = pynite.force(m.id).axial_force
        s = run.member_results.get(m.id)
        if s is None:
            print(f"{m.id:<7} {p:>14.1f} {'missing':>14}")
            continue
        rel = abs(abs(s.axial_force) - abs(p)) / abs(p) if p else float("nan")
        sign = "same" if (p >= 0) == (s.axial_force >= 0) else "OPPOSITE"
        same += sign == "same"
        opposite += sign != "same"
        print(f"{m.id:<7} {p:>14.1f} {s.axial_force:>14.1f} {rel:>10.2%}  {sign}")
    if same and not opposite:
        print("sign       : SkyCiv agrees with PyNite -> tension-positive assumption HOLDS")
    elif opposite and not same:
        print("sign       : every sign flipped -> SkyCiv is COMPRESSION-positive; "
              "use parse_member_results(..., sign_hint=-1.0)")
    elif same or opposite:
        print("sign       : mixed signs -> check the unit/geometry mapping before trusting either")


def main(argv: list[str] | None = None) -> int:
    # .env first: the --report-dir default below reads SKYCIV_REPORT_DIR, and
    # a value that only lives in .env would otherwise be invisible to argparse.
    load_env()
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--graph", help="DependencyGraph JSON (default: the equal-area 10-bar truss)")
    parser.add_argument("--load-case", default=None, help="load case id (default: the graph's first)")
    parser.add_argument("--demo", action="store_true", help="use the m7 demo change instead of equal areas")
    parser.add_argument("--record-dir", default=str(DEFAULT_RECORD_DIR),
                        help="where raw responses are recorded")
    parser.add_argument("--design-code", default=DEFAULT_DESIGN_CODE)
    parser.add_argument("--no-design", action="store_true", help="skip the design check (saves call 2)")
    parser.add_argument("--no-report", action="store_true", help="do not request the PDF report")
    parser.add_argument("--no-report-alt", action="store_true",
                        help=f"do not retry with {FN_REPORT_ALT} when {FN_REPORT} fails")
    parser.add_argument("--report-dir", default=None,
                        help="download the report PDF here (default: SKYCIV_REPORT_DIR, else no download)")
    args = parser.parse_args(argv)
    if args.report_dir is None:
        args.report_dir = os.environ.get("SKYCIV_REPORT_DIR") or None

    missing = [k for k in ("SKYCIV_API_USERNAME", "SKYCIV_API_KEY") if not os.environ.get(k)]
    if missing:
        print(f"refusing to run: {', '.join(missing)} not set (see .env.example). "
              "No network call was made.", file=sys.stderr)
        return EXIT_NO_CREDENTIALS

    if args.graph:
        graph = DependencyGraph.from_json(Path(args.graph).read_text(encoding="utf-8"))
    elif args.demo:
        graph = demo_change_graph()
    else:
        graph = build_ten_bar_graph()
    graph.validate()
    load_case_id = args.load_case or (graph.load_cases[0].id if graph.load_cases else LOAD_CASE_ID)

    record_dir = Path(args.record_dir)
    client = SkyCivClient.from_env(record_dir=record_dir)
    print(f"graph      : {graph.id} ({len(graph.nodes)} nodes, {len(graph.members)} members), "
          f"load case {load_case_id}")
    print(f"endpoint   : {client.config.base_url}")
    print(f"recording  : {record_dir}")

    design_code = None if args.no_design else args.design_code
    # SkyCivClient.analyze keeps the session open only for a design check;
    # retry_report_alt must know that to decide whether call 1's session id
    # is still usable.
    session_open = bool(design_code)
    try:
        run = client.analyze(
            graph,
            load_case_id,
            design_code=design_code,
            want_report=not args.no_report,
        )
    except SkyCivError as e:
        print(f"\nSkyCiv FAILED: {e} (function={e.function}, status={e.status})", file=sys.stderr)
        print(f"raw responses (if any) were recorded under {record_dir}", file=sys.stderr)
        return EXIT_FAILED

    print(f"\nsession id : {run.session_id}")
    print(f"api calls  : {run.api_calls}")
    for label, raw in run.raw.items():
        print(f"\n-- {label} function summary --")
        for line in _function_summary(raw):
            print(line)

    if _report_failed(run) and not args.no_report and not args.no_report_alt:
        try:
            run.report_url = retry_report_alt(
                client, graph, load_case_id, run, session_open=session_open
            )
        except SkyCivError as e:
            print(f"{FN_REPORT_ALT} call failed: {e}")

    print("\n-- raw shape of the solve data (call_1) --")
    solve_data: Any = None
    for entry in run.raw.get("call_1", {}).get("functions", []) or []:
        if isinstance(entry, dict) and entry.get("function") == FN_MODEL_SOLVE:
            solve_data = entry.get("data")
    for line in describe(solve_data):
        print(line)

    print_force_table(graph, load_case_id, run)

    print(f"\nreport url : {run.report_url}")
    print(f"design code: {run.design_code}")
    for member_id, d in sorted(run.design_results.items()):
        print(f"  {member_id:<5} ratio={d.ratio:.3f} {'pass' if d.passed else 'FAIL'}")
    if not run.design_results:
        print("  (no design ratios recognised)")
    print(f"warnings   : {run.warnings or 'none'}")
    print(f"requests   : {client.request_count} POST(s) -> "
          f"{', '.join(f'{r.label}[{r.status}]' for r in client.log)}")

    if run.report_url and args.report_dir:
        dest = Path(args.report_dir) / f"skyciv-smoke-{graph.id}.pdf"
        try:
            print(f"downloaded : {client.download_report(run.report_url, dest)}")
        except SkyCivError as e:
            print(f"download failed: {e}")

    print(f"\nrecorded responses: {sorted(p.name for p in record_dir.glob('*.json'))}")
    print("Next: compare these recordings with the constants block at the top of "
          "earl/artifacts/skyciv_client.py and the synthetic fixtures, and fix any mismatch there.")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
