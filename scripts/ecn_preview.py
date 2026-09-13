"""Render every mocked ECN, so the template can be eyeballed. [Track A, Sprint 3A]

    .venv/Scripts/python.exe scripts/ecn_preview.py                # all, text
    .venv/Scripts/python.exe scripts/ecn_preview.py escalated      # one
    .venv/Scripts/python.exe scripts/ecn_preview.py --markdown
    .venv/Scripts/python.exe scripts/ecn_preview.py --json
    .venv/Scripts/python.exe scripts/ecn_preview.py --bare         # no graph/walk

`--bare` is the interesting one: it builds the ECN from the decision ALONE, with
no dependency graph and no traversal, which is what Track A would have if the
Sprint 0 contract is left as it stands. Compare it against the default to see
exactly what the contract gap costs the document -- the Source section empties
out and every provenance line degrades to "downstream".
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from earl.delivery import build_ecn, mock_decisions as mk  # noqa: E402
from earl.delivery.render import render_markdown, render_text  # noqa: E402
from earl.ingestion import walk_and_apply  # noqa: E402


CHANGE_FOR = {
    "approved": mk.approved_change,
    "escalated": mk.escalated_change,
    "threshold_edge": mk.edge_change,
    "cross_check_disagreement": mk.escalated_change,
    "solver_error": mk.escalated_change,
}


def main(argv: list[str]) -> int:
    # Windows consoles default to a codepage that mangles anything non-ASCII.
    # The ECN is ASCII by design, but the stream is reconfigured anyway so a
    # stray character can never turn a demo into a traceback.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    flags = {a for a in argv if a.startswith("--")}
    names = [a for a in argv if not a.startswith("--")] or list(mk.ALL)

    unknown = [n for n in names if n not in mk.ALL]
    if unknown:
        print(f"unknown scenario(s): {unknown}", file=sys.stderr)
        print(f"available: {', '.join(mk.ALL)}", file=sys.stderr)
        return 2

    bare = "--bare" in flags

    for i, name in enumerate(names):
        decision = mk.ALL[name]()

        graph = walk = None
        if not bare:
            graph = mk.mock_graph(change=CHANGE_FOR[name]())
            walk = walk_and_apply(graph)

        ecn = build_ecn(decision, graph=graph, walk=walk)

        if i:
            print("\n\n")
        print(f"###### scenario: {name}{'  (decision only, no graph)' if bare else ''}")
        print()

        if "--json" in flags:
            print(json.dumps(ecn.to_dict(), indent=2))
        elif "--markdown" in flags:
            print(render_markdown(ecn))
        else:
            print(render_text(ecn))

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
