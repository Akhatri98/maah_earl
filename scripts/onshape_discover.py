"""Sprint 1A discovery: learn the real shape of the Onshape reads, once.

Spends a small fixed batch of live API calls and records every response to
tests/fixtures/onshape/. After this has run, the test suite replays those
fixtures offline and costs nothing, so this script should rarely need to run
again.

Each call is isolated: one failing endpoint does not abort the batch and waste
the calls that already succeeded.

Run:  .venv/Scripts/python.exe scripts/onshape_discover.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from earl.ingestion.onshape_client import OnshapeClient, OnshapeError  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "onshape"

# Known from the Sprint 0 smoke test -- hardcoded so discovery does not spend a
# call re-listing elements.
PARTSTUDIO = "1a595ebc191cbaa887523bc7"
ASSEMBLY = "72f445c1fb5399de58418375"


def summarize(label: str, data, depth: int = 0) -> None:
    """Print enough structure to design a parser against, without dumping
    thousands of lines of geometry."""
    pad = "  " * (depth + 1)
    if isinstance(data, dict):
        print(f"{pad}{label}: dict({len(data)} keys)")
        for k, v in list(data.items())[:14]:
            kind = type(v).__name__
            if isinstance(v, list):
                print(f"{pad}  .{k}: list[{len(v)}]")
            elif isinstance(v, dict):
                print(f"{pad}  .{k}: dict({len(v)} keys) {list(v)[:6]}")
            else:
                shown = repr(v)
                print(f"{pad}  .{k}: {kind} = {shown[:70]}")
    elif isinstance(data, list):
        print(f"{pad}{label}: list[{len(data)}]")
        if data:
            summarize("[0]", data[0], depth + 1)
    else:
        print(f"{pad}{label}: {data!r}")


def main() -> int:
    force = "--force" in sys.argv
    client = OnshapeClient.from_env(record_dir=FIXTURES)

    # Probe with ONE call before committing to the batch. Running the full
    # sweep against an unmodelled document spends five calls to learn five
    # times that it is empty -- the budget is ~2500/year, so don't.
    print(f"{'=' * 68}\nprobe: assembly definition\n{'=' * 68}")
    try:
        assembly = client.get_assembly_definition(ASSEMBLY)
    except OnshapeError as e:
        print(f"  FAILED: {e}")
        return 1

    summarize("assembly definition", assembly)
    root = assembly.get("rootAssembly") or {}
    populated = bool(root.get("instances") or root.get("features"))

    if not populated and not force:
        print(
            "\n  DOCUMENT IS EMPTY -- no instances and no mate features.\n"
            "  Stopping after 1 call instead of spending 4 more on empty responses.\n"
            "  Model the truss in Onshape, then re-run. Use --force to sweep anyway."
        )
        print(f"\nAPI calls spent: {client.request_count}")
        return 2

    calls = [
        ("variables (part studio)", lambda: client.get_variables(PARTSTUDIO)),
        ("assembly features", lambda: client.get_assembly_features(ASSEMBLY)),
        ("partstudio features", lambda: client.get_partstudio_features(PARTSTUDIO)),
        ("parts", lambda: client.get_parts(PARTSTUDIO)),
    ]

    failures = 0
    for label, call in calls:
        print(f"\n{'=' * 68}\n{label}\n{'=' * 68}")
        try:
            data = call()
        except OnshapeError as e:
            failures += 1
            print(f"  FAILED: {e}")
            continue
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"  FAILED ({type(e).__name__}): {e}")
            continue
        summarize(label, data)

    print(f"\n{'=' * 68}")
    print(f"API calls spent: {client.request_count}   failures: {failures}")
    for r in client.log:
        print(f"  {r.status}  {r.bytes:>8,}b  {r.path}")
    print(f"\nfixtures written to: {FIXTURES}")
    for f in sorted(FIXTURES.glob("*.json")):
        print(f"  {f.name}  ({f.stat().st_size:,}b)")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
