"""Simulate an engineer's saved CAD edit; does not call the pipeline or web app."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from earl.config import PROJECT_ROOT
from earl.watch.state import atomic_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("scenario", choices=("thin-compression", "reinforce-chord", "design-margin"))
    parser.add_argument("--file", type=Path, default=PROJECT_ROOT / "out" / "agent" / "change.json")
    args = parser.parse_args()
    atomic_json(args.file, {"scenario": args.scenario})
    print(f"Saved synthetic CAD edit: {args.scenario}. The watcher, not this script, evaluates it.")


if __name__ == "__main__":
    main()
