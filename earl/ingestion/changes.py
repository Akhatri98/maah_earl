"""Turn two reads of the CAD into ChangeEvents. [Track A, Sprint 2A]

`diff_variables()` answers "what is different between these two variable
tables". This module answers the question the pipeline actually asks: "what
CHANGED, stated in the form the dependency graph and the ECN both need".

The gap between those is real. A diff is a tuple of strings; a `ChangeEvent`
has to carry a stable id, a change kind, a human-readable description that
flows verbatim into the ECN, and a target the walker can seed from.

## One change per graph

`DependencyGraph` carries exactly one `ChangeEvent`, so an edit touching two
variables is two graphs, not one graph with two changes. `detect_changes()`
therefore returns a LIST and leaves the choice to the caller rather than
silently analysing the first and dropping the rest -- a change quietly not
analysed is a dropped domino before the walk even starts.
"""

from __future__ import annotations

import hashlib
from typing import Iterable

from ..contracts.graph import ChangeEvent, ChangeKind
from .benchmark import variable_drives
from .parsers import Variable, diff_variables


def change_id(*parts: str | None) -> str:
    """A short, stable id derived from the change itself.

    Deterministic on purpose: replaying the same scenario in the eval harness
    must produce the same id, or two runs of one scenario look like two
    different changes in the record.
    """
    digest = hashlib.sha256(
        "|".join(p or "" for p in parts).encode("utf-8")
    ).hexdigest()
    return f"chg-{digest[:12]}"


def describe_variable_change(
    name: str, before: str | None, after: str | None
) -> tuple[ChangeKind, str]:
    """The kind and the ECN sentence for one variable difference.

    Wording is templated rather than model-generated -- plan.md puts the ECN
    in the "templated, not freeform" column so it reads the same every run.
    """
    if before is None and after is not None:
        return ChangeKind.VARIABLE_EDIT, f"Variable {name} added with value {after}"
    if after is None and before is not None:
        return (
            ChangeKind.VARIABLE_EDIT,
            f"Variable {name} removed (was {before})",
        )
    return (
        ChangeKind.VARIABLE_EDIT,
        f"Variable {name} changed from {before} to {after}",
    )


def detect_variable_changes(
    before: Iterable[Variable],
    after: Iterable[Variable],
) -> list[ChangeEvent]:
    """Every variable-table difference, as ChangeEvents.

    A variable that was added or removed is a change too, not a missing value
    to skip: removing the variable a feature reads changes the geometry just
    as surely as editing it.
    """
    events: list[ChangeEvent] = []
    for name, old, new in diff_variables(before, after):
        kind, description = describe_variable_change(name, old, new)
        events.append(
            ChangeEvent(
                id=change_id("variable", name, old, new),
                kind=kind,
                description=description,
                target_id=name,
                value_before=old,
                value_after=new,
            )
        )
    return events


def undriven_changes(events: Iterable[ChangeEvent]) -> list[ChangeEvent]:
    """Changes whose target drives nothing the walker can reach.

    A variable nothing declares a dependency for produces a graph with no
    VARIABLE_REF edges, so the walk starts and immediately stops, and the run
    reports nothing affected -- indistinguishable from a genuinely safe
    change. Surfacing these is the point: it means `VARIABLE_DRIVES` in
    `benchmark.py` has fallen out of step with the FeatureScript.
    """
    return [
        e for e in events
        if e.kind is ChangeKind.VARIABLE_EDIT and not variable_drives(e.target_id)
    ]


def detect_changes(
    before_variables: Iterable[Variable],
    after_variables: Iterable[Variable],
) -> tuple[list[ChangeEvent], list[str]]:
    """All detected changes, plus complaints worth acting on.

    Today this covers the variable table only -- the pipeline's primary change
    signal, and the one Onshape reports in a form that diffs cleanly. Feature
    edits and member removal are detectable from the feature list, but that
    payload is Onshape's BTM tree, where a meaningful diff needs more than
    comparing the two documents; it is deliberately left for when a scenario
    needs it rather than half-built now.
    """
    events = detect_variable_changes(before_variables, after_variables)
    problems = [
        f"change {e.id} targets variable {e.target_id!r}, which drives nothing "
        "declared in VARIABLE_DRIVES; the walk would reach nothing and the run "
        "would report the change as affecting no members"
        for e in undriven_changes(events)
    ]
    return events, problems
