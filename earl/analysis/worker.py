"""One bounded numerical request on stdin, one validated JSON result on stdout."""

import contextlib
import io
import json
import sys

from earl.contracts import DependencyGraph
from .benchmark import validate_benchmark
from .sanity import check
from .solver import _solve_local


def main() -> int:
    try:
        request = json.loads(sys.stdin.read(2_000_000))
        graph = DependencyGraph.from_dict(request["graph"])
        with contextlib.redirect_stdout(io.StringIO()):
            validate_benchmark()
            result = _solve_local(graph, request["load_case_id"])
            result.sanity_checks = check(graph, result)
        result.validate()
        print(result.to_json(indent=None))
        return 0
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
