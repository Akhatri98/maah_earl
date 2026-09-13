"""The demo web app: EARL as something a judge can drive. [Track A, Sprint 7]

`earl.web.api` is the domain layer -- it turns validated request parameters
into the same `earl.eval.scenarios.Scenario` objects the eval set is built
from, runs `earl.pipeline.run_pipeline`, and shapes the result as JSON.
`earl.web.app` is the HTTP layer on `http.server`, with no framework and no
new dependency.

The split matters because the app is served over a public ngrok URL during
the demo: everything that decides anything lives in `api`, is offline, is
deterministic, and writes nothing to disk.
"""

from .api import BadRequest, run_change, tamper

__all__ = ["BadRequest", "run_change", "tamper"]
