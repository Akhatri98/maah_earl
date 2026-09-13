"""The branch evaluation sandbox. [Track A, Sprint 2A]

plan.md:

    "Evaluation happens in an Onshape branch, not the main workspace -- a
     free, resettable sandbox, so nothing is committed until it's verified."

and, on why the model cannot be trusted with this:

    "Merge-to-main permission doesn't exist for the model at all. It can only
     propose changes in a branch."

`BranchSession` is how that is enforced in code rather than by convention. It
hands out a client already pointed at the branch, so a write lands in the
sandbox by default and reaching Main takes deliberate effort. It deliberately
exposes NO merge method: merging is gated on the threshold check, which lives
in the decision layer, so it does not belong on the object the analysis holds.

## The asymmetry worth knowing

A workspace can be deleted, so reset is cheap and total. A VERSION cannot --
Onshape has no delete for them. Creating a branch requires a version, so every
evaluation leaves one permanent line in the document's history. Twenty eval
scenarios is twenty permanent versions.

`reuse_existing` exists because of that: for a batch of scenarios, branch once
and reset the variables between runs rather than branching per scenario.

## Which way the change flows

Two readings of plan.md are both defensible, so this supports either:

  * **EARL applies a candidate change** -- `set_variable()`, or
    `evaluate_in_branch()` for several at once, writes it into the branch.
    Before and after both come from the pipeline, which is what the eval
    harness wants.
  * **The engineer already changed Main** -- branch from an older version with
    `from_version_id` and compare against it.

`detect_changes()` in `changes.py` turns either pair of reads into
ChangeEvents, so the rest of the pipeline does not care which was used.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator

from .onshape_client import OnshapeClient, OnshapeError
from .parsers import (
    Variable,
    VariableFeature,
    feature_with_expression,
    parse_variable_features,
    parse_variables,
)


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


@dataclass
class BranchInfo:
    """What a branch cost and where it lives."""

    version_id: str
    version_name: str
    workspace_id: str
    branch_name: str
    reused: bool = False

    @property
    def left_a_permanent_version(self) -> bool:
        return not self.reused


@dataclass
class BranchSession:
    """A resettable Onshape sandbox for evaluating one change.

    Use as a context manager so the branch is cleaned up even when the
    analysis raises -- an abandoned branch is a workspace nobody deletes, and
    they accumulate:

        with BranchSession.open(client, label="chg-001") as branch:
            branch.set_variable(PARTSTUDIO, "barArea", "0.4 in^2")
            assembly = branch.client.get_assembly_definition(ASSEMBLY)
    """

    parent: OnshapeClient
    client: OnshapeClient
    info: BranchInfo
    keep: bool = False
    closed: bool = False
    notes: list[str] = field(default_factory=list)

    # -- lifecycle ---------------------------------------------------------

    @classmethod
    def open(
        cls,
        client: OnshapeClient,
        *,
        label: str | None = None,
        from_version_id: str | None = None,
        reuse_existing: str | None = None,
        keep: bool = False,
    ) -> BranchSession:
        """Create (or reuse) a branch and return a session pointed at it.

        `reuse_existing` takes a workspace id and skips both writes entirely
        -- no new version, no new workspace. That is the cheap path for a
        batch of eval scenarios.
        """
        suffix = label or _stamp()
        notes: list[str] = []

        if reuse_existing:
            info = BranchInfo(
                version_id="",
                version_name="",
                workspace_id=reuse_existing,
                branch_name=f"(reused {reuse_existing})",
                reused=True,
            )
            notes.append(
                f"reusing existing workspace {reuse_existing}; no version created"
            )
        else:
            version_name = f"EARL eval {suffix}"
            if from_version_id:
                version_id = from_version_id
                notes.append(f"branching from existing version {from_version_id}")
            else:
                version = client.create_version(version_name)
                version_id = version["id"]
                notes.append(
                    f"created version {version_id} ({version_name!r}) -- "
                    "PERMANENT, Onshape cannot delete versions"
                )
            branch_name = f"earl-eval-{suffix}"
            workspace = client.create_workspace(branch_name, version_id=version_id)
            info = BranchInfo(
                version_id=version_id,
                version_name=version_name,
                workspace_id=workspace["id"],
                branch_name=branch_name,
            )

        branch_client = client.for_workspace(info.workspace_id)
        return cls(
            parent=client,
            client=branch_client,
            info=info,
            keep=keep,
            notes=notes,
        )

    def reset(self) -> None:
        """Delete the branch. Total, and free -- unlike the version behind it."""
        if self.closed:
            return
        self.closed = True
        # Fold the branch client's calls into the parent's log, or the
        # reported cost understates what the run actually spent.
        self.parent.log.extend(self.client.log)
        self.client.log.clear()

        if self.info.reused:
            self.notes.append("reused workspace left in place, as asked")
            return
        if self.keep:
            self.notes.append(f"branch {self.info.workspace_id} kept, as asked")
            return
        try:
            self.parent.delete_workspace(self.info.workspace_id)
        except OnshapeError as e:
            # Worth shouting about: an undeleted branch is a workspace that
            # stays until somebody notices it.
            self.notes.append(
                f"could not delete branch {self.info.workspace_id}: HTTP {e.status}"
            )
            raise

    def __enter__(self) -> BranchSession:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.reset()

    # -- evaluating a change in the sandbox --------------------------------

    def variables(self, element_id: str) -> list[Variable]:
        return parse_variables(self.client.get_variables(element_id))

    def variable_features(self, element_id: str) -> list[VariableFeature]:
        """The writable form of the variables -- `assignVariable` features."""
        return parse_variable_features(
            self.client.get_partstudio_features(element_id)
        )

    def set_variable(
        self, element_id: str, name: str, expression: str
    ) -> list[Variable]:
        """Change one variable in the branch and return the resulting table.

        Goes through the `assignVariable` FEATURE, not the variables endpoint:
        POSTing to /variables 404s against this document, because these
        variables are part studio features rather than a Variable Studio
        table. See the note in `onshape_client.py`.

        The variable's type decides which parameter carries the value, and it
        is read from the feature rather than assumed -- `barArea` is ANY
        (Onshape has no Area type), and writing "0.4 in^2" into a LENGTH slot
        would be ignored or silently taken as a length.
        """
        features = self.variable_features(element_id)
        by_name = {f.name: f for f in features}
        if name not in by_name:
            raise KeyError(
                f"variable {name!r} has no assignVariable feature "
                f"(have {sorted(by_name)}); refusing to create it, because a "
                "typo would otherwise read as a new variable rather than an "
                "edit to the intended one"
            )

        target = by_name[name]
        self.client.update_partstudio_feature(
            element_id,
            target.feature_id,
            feature_with_expression(target, expression),
        )
        return self.variables(element_id)

    def call_cost(self) -> int:
        """API calls spent inside the branch so far."""
        return self.client.request_count


@contextmanager
def evaluate_in_branch(
    client: OnshapeClient,
    element_id: str,
    changes: dict[str, str],
    *,
    label: str | None = None,
    keep: bool = False,
) -> Iterator[BranchSession]:
    """Convenience: open a branch, apply variable changes, hand back the session.

    Written as a generator so the caller still controls the lifetime via
    `with`, rather than the branch being reset before anything can read it.
    """
    session = BranchSession.open(client, label=label, keep=keep)
    try:
        for name, expression in changes.items():
            session.set_variable(element_id, name, expression)
        yield session
    finally:
        session.reset()
