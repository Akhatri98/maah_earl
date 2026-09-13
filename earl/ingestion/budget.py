"""Conservative persisted Onshape request budget, reserved before transport.

Counts failed/uncertain requests too, unlike Onshape's billable-success count.
There is no automatic calendar reset: account allocation renewal dates differ.
Set ONSHAPE_CALLS_USED to at least existing account usage when commissioning.
"""

import os

from earl.config import load_env
from earl.watch.state import Ledger, utcnow


class CallBudgetExhausted(RuntimeError):
    pass


def reserve_call(ledger: Ledger, method: str, path: str) -> None:
    load_env()
    limit = int(os.environ.get("ONSHAPE_CALL_BUDGET", "2500"))
    initial = int(os.environ.get("ONSHAPE_CALLS_USED", "0"))
    if limit < 0 or initial < 0:
        raise ValueError("Onshape budget and prior usage must be nonnegative")
    exhausted = False
    with ledger.edit() as state:
        budget = state["api_budget"]
        budget["limit"] = limit
        budget["used"] = max(budget["used"], initial)
        if budget["used"] >= limit:
            exhausted = True
            state["last_message"] = "Onshape API budget exhausted; live calls refused."
        else:
            budget["used"] += 1
            budget["last_call"] = {"at": utcnow(), "method": method, "path": path}
    if exhausted:
        raise CallBudgetExhausted("Onshape annual call budget exhausted; no request was made")
