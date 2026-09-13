"""Prove the Onshape branch cycle works on this account. [Track A, Sprint 2A]

plan.md evaluates every change in a branch -- "a free, resettable sandbox, so
nothing is committed until it's verified". That claim is unverified until
create / read / delete actually succeed against the live API, because branch
and merge availability varies by Onshape plan.

This spends ~5 API calls and does:

    create version  ->  create workspace  ->  read the assembly from it
                    ->  delete the workspace

It does NOT merge. Merging touches Main, and plan.md puts merge behind the
code-enforced threshold check, so it is not something a smoke test should do
casually. Pass --merge to include it (it merges the untouched branch back,
which is a no-op change, but it does write to Main).

## What this leaves behind, permanently

Onshape versions CANNOT be deleted. Every run adds one line to the document's
version history forever. The workspace is deleted and leaves no trace, but the
version does not. That is the real standing cost of the branch sandbox, and it
matters for Sprint 5's ~20 eval scenarios: 20 runs is 20 permanent versions.

Run:
    .venv/Scripts/python.exe scripts/onshape_branch_smoketest.py --confirm
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from earl.ingestion import OnshapeClient, OnshapeError  # noqa: E402
from earl.ingestion.parsers import (  # noqa: E402
    derive_topology,
    parse_mate_connectors,
)

ASSEMBLY = "72f445c1fb5399de58418375"


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
        "--keep",
        action="store_true",
        help="do not delete the branch workspace afterwards",
    )
    args = parser.parse_args()

    if not args.confirm:
        print(__doc__)
        print("Refusing to run without --confirm.")
        return 2

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    version_name = f"EARL smoketest {stamp}"
    branch_name = f"earl-smoketest-{stamp}"

    client = OnshapeClient.from_env()
    main_workspace = client.config.workspace_id
    workspace_id: str | None = None
    failures = 0

    try:
        print(f"1/4  creating version {version_name!r} from Main ...")
        version = client.create_version(version_name)
        version_id = version["id"]
        print(f"     version id {version_id}  (PERMANENT -- cannot be deleted)")

        print(f"2/4  creating branch workspace {branch_name!r} ...")
        workspace = client.create_workspace(branch_name, version_id=version_id)
        workspace_id = workspace["id"]
        print(f"     workspace id {workspace_id}")

        print("3/4  reading the assembly from the branch ...")
        # Point the client at the branch, then read exactly as the pipeline
        # would -- this is what "evaluate in a branch" has to mean.
        branch_client = OnshapeClient(
            config=type(client.config)(
                access_key=client.config.access_key,
                secret_key=client.config.secret_key,
                base_url=client.config.base_url,
                document_id=client.config.document_id,
                workspace_id=workspace_id,
            )
        )
        assembly = branch_client.get_assembly_definition(ASSEMBLY)
        # Fold the branch client's calls into the main log, or the reported
        # cost understates what the run actually spent -- and the annual
        # budget is the whole reason this client counts at all.
        client.log.extend(branch_client.log)
        topology = derive_topology(parse_mate_connectors(assembly))
        print(
            f"     branch read OK: {len(topology.node_positions)} nodes, "
            f"{len(topology.member_ends)} members, "
            f"complete={topology.is_complete}"
        )
        if not topology.is_complete:
            failures += 1
            for problem in topology.unresolved:
                print(f"     !! {problem}")

        if args.merge:
            print("3b/  merging the branch back into Main ...")
            client.merge_into_workspace(
                main_workspace, source_workspace_id=workspace_id
            )
            print("     merge OK")

    except OnshapeError as e:
        failures += 1
        print(f"\nFAILED: HTTP {e.status}")
        print(f"  {e}")
        if e.status == 403:
            print(
                "\n  403 usually means the plan does not permit this operation.\n"
                "  If branch/merge is unavailable, plan.md's 'resettable\n"
                "  sandbox' needs a different mechanism -- raise it before\n"
                "  Sprint 4 wires the tracks together."
            )
    finally:
        if workspace_id and not args.keep:
            print(f"4/4  deleting branch workspace {workspace_id} ...")
            try:
                client.delete_workspace(workspace_id)
                print("     deleted (reset works)")
            except OnshapeError as e:
                failures += 1
                print(f"     !! could not delete: HTTP {e.status}")
        elif workspace_id:
            print(f"4/4  keeping workspace {workspace_id} as asked")

    print(f"\nAPI calls spent: {client.request_count}")
    for record in client.log:
        print(f"  {record.method:6} {record.status}  {record.path}")

    print("\nBRANCH CYCLE OK" if not failures else f"\n{failures} FAILURE(S)")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
