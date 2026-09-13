"""Prove the Onshape branch evaluate cycle works on this account. [Track A, Sprint 2A]

plan.md evaluates every change in a branch -- "a free, resettable sandbox, so
nothing is committed until it's verified". That is unverified until the whole
cycle actually succeeds against the live API, because branch availability
varies by Onshape plan and the variables-write payload shape is documented
rather than observed.

This spends ~7 API calls and does:

    create version -> create branch -> read variables (before)
                   -> WRITE a variable in the branch
                   -> read variables (after) -> diff into a ChangeEvent
                   -> rebuild the dependency graph from the branch
                   -> walk it -> confirm Main is untouched -> delete branch

It does NOT merge. Merging touches Main, and plan.md puts merge behind the
code-enforced threshold check, so it is not something a smoke test should do
casually. Pass --merge to include it.

## What this leaves behind, permanently

Onshape versions CANNOT be deleted. Every run adds one line to the document's
version history forever. The branch workspace is deleted and leaves no trace;
the version does not. That is the standing cost of the sandbox, and it matters
for Sprint 5's ~20 eval scenarios -- hence `BranchSession(reuse_existing=...)`,
which branches once and re-edits variables between scenarios.

Run:
    .venv/Scripts/python.exe scripts/onshape_branch_smoketest.py --confirm
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from earl.contracts.graph import OnshapeRef  # noqa: E402
from earl.ingestion import (  # noqa: E402
    BranchSession,
    OnshapeClient,
    OnshapeError,
    build_graph,
    detect_changes,
    parse_variables,
    walk_and_apply,
)

ASSEMBLY = "72f445c1fb5399de58418375"
PARTSTUDIO = "1a595ebc191cbaa887523bc7"

VARIABLE = "barArea"
NEW_VALUE = "0.4 in^2"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="required: this WRITES to the Onshape document and the version "
             "it creates can never be deleted",
    )
    parser.add_argument(
        "--merge",
        action="store_true",
        help="also merge the branch back into Main (writes to Main)",
    )
    parser.add_argument(
        "--keep", action="store_true", help="do not delete the branch afterwards"
    )
    args = parser.parse_args()

    if not args.confirm:
        print(__doc__)
        print("Refusing to run without --confirm.")
        return 2

    label = datetime.now(timezone.utc).strftime("smoketest-%Y%m%d-%H%M%S")
    client = OnshapeClient.from_env()
    main_workspace = client.config.workspace_id
    failures = 0

    try:
        with BranchSession.open(client, label=label, keep=args.keep) as branch:
            for note in branch.notes:
                print(f"     {note}")
            print(f"1/6  branch {branch.info.branch_name!r} "
                  f"({branch.info.workspace_id})")

            print("2/6  reading variables from the branch ...")
            before = branch.variables(PARTSTUDIO)
            print(f"     {len(before)} variables: "
                  f"{', '.join(f'{v.name}={v.expression}' for v in before)}")

            print(f"3/6  writing {VARIABLE} = {NEW_VALUE} IN THE BRANCH ...")
            after = branch.set_variable(PARTSTUDIO, VARIABLE, NEW_VALUE)
            applied = {v.name: v.expression for v in after}
            if applied.get(VARIABLE) != NEW_VALUE:
                failures += 1
                print(f"     !! write did not take: {VARIABLE} is "
                      f"{applied.get(VARIABLE)!r}, expected {NEW_VALUE!r}")
            else:
                print(f"     write OK: {VARIABLE} = {applied[VARIABLE]}")

            print("4/6  diffing before/after into a ChangeEvent ...")
            events, problems = detect_changes(before, after)
            for problem in problems:
                failures += 1
                print(f"     !! {problem}")
            if not events:
                failures += 1
                print("     !! no change detected from a write that took")
            else:
                change = events[0]
                print(f"     {change.id}: {change.description}")

            print("5/6  rebuilding the graph from the branch and walking it ...")
            if events:
                assembly = branch.client.get_assembly_definition(ASSEMBLY)
                result = build_graph(
                    assembly,
                    graph_id=f"graph-{label}",
                    change=events[0],
                    source=OnshapeRef(
                        document_id=client.config.document_id,
                        workspace_id=branch.info.workspace_id,
                        element_id=ASSEMBLY,
                        version_id=branch.info.version_id or None,
                        branch_name=branch.info.branch_name,
                    ),
                )
                walk = walk_and_apply(result.graph)
                print(f"     graph: {len(result.graph.nodes)} nodes, "
                      f"{len(result.graph.members)} members, "
                      f"{len(result.graph.edges)} edges")
                print(f"     walk : {len(result.graph.affected_member_ids)} "
                      f"members affected, max {walk.max_distance} hop(s)")
                if result.problems:
                    failures += 1
                    for problem in result.problems:
                        print(f"     !! {problem}")

            if args.merge:
                print("5b/  merging the branch back into Main ...")
                client.merge_into_workspace(
                    main_workspace, source_workspace_id=branch.info.workspace_id
                )
                print("     merge OK")

        print("6/6  confirming Main was NOT touched ...")
        main_now = {
            v.name: v.expression
            for v in parse_variables(client.get_variables(PARTSTUDIO))
        }
        if main_now.get(VARIABLE) == NEW_VALUE and not args.merge:
            failures += 1
            print(f"     !! Main's {VARIABLE} is {NEW_VALUE!r} -- the branch "
                  "write leaked into Main, which is the one thing the sandbox "
                  "exists to prevent")
        else:
            print(f"     Main still has {VARIABLE} = "
                  f"{main_now.get(VARIABLE)!r}  (branch was isolated)")

    except OnshapeError as e:
        failures += 1
        print(f"\nFAILED: HTTP {e.status}")
        print(f"  {e}")
        if e.status == 403:
            print(
                "\n  403 usually means the plan does not permit this operation.\n"
                "  If branch or variable-write is unavailable, plan.md's\n"
                "  'resettable sandbox' needs a different mechanism -- raise it\n"
                "  before Sprint 4 wires the tracks together."
            )

    print(f"\nAPI calls spent: {client.request_count}")
    for record in client.log:
        print(f"  {record.method:6} {record.status}  {record.path}")

    print("\nBRANCH EVALUATE CYCLE OK" if not failures else f"\n{failures} FAILURE(S)")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
