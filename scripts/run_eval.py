"""Regenerate all committed evaluation records without network access."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from earl.eval.harness import evaluate

if __name__ == "__main__":
    print(json.dumps(evaluate(write=True)["summary"], indent=2))
