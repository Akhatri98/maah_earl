"""Onshape REST client. [Track A, Sprint 1A]

Auth is plain HTTP Basic with the API key pair -- verified against the live
API, so the HMAC request-signing scheme is not needed.

The Onshape free tier allows on the order of 2500 calls/year, so this client
treats requests as a scarce resource:

  * every call is counted and logged (`client.request_count`, `client.log`),
  * `record_dir` saves each raw response to disk, so a shape only has to be
    discovered once and tests can replay it offline for free,
  * redirects are NOT followed silently -- a redirect would spend a second
    call without saying so.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import requests

from ..config import OnshapeConfig

# Onshape asks for this Accept header; the qs weighting selects the JSON
# flavour of endpoints that can also return other media types.
ACCEPT = "application/json;charset=UTF-8;qs=0.09"


class OnshapeError(RuntimeError):
    """An Onshape API call failed. Carries the status code for triage:
    401 = auth, 403 = permission, 404 = bad document/workspace/element id."""

    def __init__(self, status: int, message: str, url: str) -> None:
        super().__init__(f"HTTP {status} for {url}: {message}")
        self.status = status
        self.url = url


@dataclass
class RequestRecord:
    method: str
    path: str
    status: int
    bytes: int


@dataclass
class OnshapeClient:
    config: OnshapeConfig
    record_dir: Path | None = None
    timeout: int = 30
    log: list[RequestRecord] = field(default_factory=list)

    @classmethod
    def from_env(cls, record_dir: Path | str | None = None) -> OnshapeClient:
        return cls(
            config=OnshapeConfig.from_env(),
            record_dir=Path(record_dir) if record_dir else None,
        )

    @property
    def request_count(self) -> int:
        return len(self.log)

    # -- transport ---------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        body: Any = None,
    ) -> Any:
        url = f"{self.config.base_url}{path}"
        resp = requests.request(
            method,
            url,
            params=params,
            json=body,
            auth=(self.config.access_key, self.config.secret_key),
            headers={"Accept": ACCEPT, "Content-Type": "application/json"},
            timeout=self.timeout,
            allow_redirects=False,   # never spend a second call silently
        )

        if resp.is_redirect or resp.is_permanent_redirect:
            raise OnshapeError(
                resp.status_code,
                f"unexpected redirect to {resp.headers.get('Location')!r} "
                "(not followed, to avoid spending another API call)",
                url,
            )

        self.log.append(
            RequestRecord(method, path, resp.status_code, len(resp.content))
        )

        # Onshape answers writes with 200 or 201; DELETE can answer 204.
        if resp.status_code not in (200, 201, 204):
            raise OnshapeError(resp.status_code, resp.text[:500], url)

        if resp.status_code == 204 or not resp.content:
            return None

        data = resp.json()
        if self.record_dir:
            self._record(path, data)
        return data

    def for_workspace(self, workspace_id: str) -> OnshapeClient:
        """A sibling client aimed at a different workspace (normally a branch).

        Copies rather than constructs, so a subclass -- a test fake, say --
        stays its own type and keeps its state. The log starts empty so the
        branch's calls can be counted separately and then folded back into the
        parent's total.
        """
        sibling = copy.copy(self)
        sibling.config = replace(self.config, workspace_id=workspace_id)
        sibling.log = []
        return sibling

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._request("GET", path, params=params)

    def _post(self, path: str, body: Any = None) -> Any:
        """A write. Onshape writes are NOT free to undo -- a version, once
        created, cannot be deleted -- so callers should make the intent
        explicit rather than treating this like a read."""
        return self._request("POST", path, body=body)

    def _delete(self, path: str) -> Any:
        return self._request("DELETE", path)

    def _record(self, path: str, data: Any) -> None:
        self.record_dir.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-zA-Z0-9]+", "_", path.replace("/api/", "")).strip("_")
        # Ids are long and noisy; keep the endpoint-shaped tail readable.
        slug = re.sub(r"_[0-9a-f]{24}", "", slug)[:90]
        (self.record_dir / f"{slug}.json").write_text(
            json.dumps(data, indent=2), encoding="utf-8"
        )

    # -- document structure ------------------------------------------------

    def _dw(self) -> str:
        return f"d/{self.config.document_id}/w/{self.config.workspace_id}"

    def list_elements(self) -> list[dict[str, Any]]:
        """Every tab in the workspace: part studios, assemblies, BOMs."""
        return self._get(f"/api/documents/{self._dw()}/elements")

    def find_element(self, element_type: str) -> dict[str, Any] | None:
        """First element of a type, e.g. 'ASSEMBLY' or 'PARTSTUDIO'.

        Costs a call; prefer passing a known element id around once found.
        """
        for el in self.list_elements():
            if el.get("elementType", "").upper() == element_type.upper():
                return el
        return None

    # -- stage 1 reads -----------------------------------------------------

    def get_variables(self, element_id: str) -> list[dict[str, Any]]:
        """Variable table for a part studio.

        This is the primary ingestion signal: a parametric change in EARL is
        usually a variable-table value changing.
        """
        return self._get(f"/api/variables/{self._dw()}/e/{element_id}/variables")

    # NOTE: there is deliberately no `set_variables()` posting to
    # /api/variables/.../variables. It 404s against this document, verified
    # live: the variables here are `assignVariable` FEATURES in the part
    # studio, not rows of a Variable Studio table, so there is no variable
    # table to write to. Edits go through `update_partstudio_feature` below.

    def update_partstudio_feature(
        self, element_id: str, feature_id: str, feature: dict[str, Any]
    ) -> Any:
        """Replace one part studio feature. This is the 'evaluate' in the
        branch cycle -- editing an `assignVariable` feature is how a variable
        actually changes.

        A WRITE, and it edits whatever workspace this client points at -- so
        point it at a branch, never at Main. `BranchSession` in `branch.py`
        exists so that is the default rather than something to remember.

        `feature` must be the COMPLETE BTMFeature wrapper as Onshape sent it,
        with only the intended field changed: the feature is replaced by what
        it is given, so a partial payload silently drops every parameter left
        out. `parsers.feature_with_expression()` builds that payload.
        """
        return self._post(
            f"/api/partstudios/{self._dw()}/e/{element_id}"
            f"/features/featureid/{feature_id}",
            {"feature": feature},
        )

    def get_assembly_definition(
        self,
        element_id: str,
        *,
        include_mate_features: bool = True,
        include_mate_connectors: bool = True,
    ) -> dict[str, Any]:
        """Assembly instances, occurrences and (optionally) mate features and
        mate connectors.

        `includeMateFeatures` folds the mate data into this single response,
        which is why it is preferred over a separate /features call.

        `includeMateConnectors` matters more than it looks. Mates only mention
        the connectors they actually consume: the truss is fully constrained by
        nine mates, which touch 13 of the 20 connectors the FeatureScript
        created. The other seven are real node positions that no mate names, so
        without this flag seven members have an endpoint with no coordinate and
        the topology cannot be recovered. Defaults to true for that reason --
        an incomplete graph is the dropped domino this project exists to catch.
        """
        params = {
            "includeMateFeatures": str(include_mate_features).lower(),
            "includeMateConnectors": str(include_mate_connectors).lower(),
            "includeNonSolids": "false",
        }
        return self._get(f"/api/assemblies/{self._dw()}/e/{element_id}", params)

    def get_assembly_features(self, element_id: str) -> dict[str, Any]:
        """Assembly feature list -- mates as features, with their queries."""
        return self._get(f"/api/assemblies/{self._dw()}/e/{element_id}/features")

    def get_partstudio_features(self, element_id: str) -> dict[str, Any]:
        """Part studio feature list. Feature edits (member length, section)
        land here, and features reference variables by name."""
        return self._get(f"/api/partstudios/{self._dw()}/e/{element_id}/features")

    def get_parts(self, element_id: str) -> list[dict[str, Any]]:
        """Parts in a part studio, for provenance ids on members."""
        return self._get(f"/api/parts/{self._dw()}/e/{element_id}")

    # -- branch management (Sprint 2A) -------------------------------------
    #
    # plan.md evaluates every change in a BRANCH, never the main workspace:
    # "a free, resettable sandbox, so nothing is committed until it's
    # verified". Onshape has no single "branch" object -- a branch is a
    # workspace parented to a version, so creating one is two steps:
    #
    #   1. POST a version   -- an immutable snapshot of the source workspace
    #   2. POST a workspace -- the editable branch, rooted at that version
    #
    # The asymmetry matters: a WORKSPACE can be deleted (so "reset" is cheap
    # and total), but a VERSION cannot. Every evaluation therefore leaves a
    # permanent version behind in the document's history. That is the real
    # cost of the sandbox, and it is worth knowing before running twenty eval
    # scenarios against a live document.

    def list_versions(self) -> list[dict[str, Any]]:
        return self._get(f"/api/documents/d/{self.config.document_id}/versions")

    def list_workspaces(self) -> list[dict[str, Any]]:
        """Every workspace (branch) in the document, including Main."""
        return self._get(f"/api/documents/d/{self.config.document_id}/workspaces")

    def find_workspace(self, name: str) -> dict[str, Any] | None:
        for ws in self.list_workspaces():
            if ws.get("name") == name:
                return ws
        return None

    def create_version(
        self, name: str, *, workspace_id: str | None = None
    ) -> dict[str, Any]:
        """Snapshot a workspace as an immutable version.

        PERMANENT: Onshape provides no way to delete a version. Each one is a
        line in the document's history forever.
        """
        return self._post(
            f"/api/documents/{self.config.document_id}/versions",
            {
                "documentId": self.config.document_id,
                "workspaceId": workspace_id or self.config.workspace_id,
                "name": name,
            },
        )

    def create_workspace(self, name: str, *, version_id: str) -> dict[str, Any]:
        """Create a branch workspace rooted at a version."""
        return self._post(
            f"/api/documents/{self.config.document_id}/workspaces",
            {
                "documentId": self.config.document_id,
                "versionId": version_id,
                "name": name,
                "isReadOnly": False,
            },
        )

    def delete_workspace(self, workspace_id: str) -> None:
        """Discard a branch. This is the 'reset' in create/evaluate/reset."""
        self._delete(
            f"/api/documents/{self.config.document_id}/workspaces/{workspace_id}"
        )

    def merge_into_workspace(
        self,
        target_workspace_id: str,
        *,
        source_workspace_id: str | None = None,
        source_version_id: str | None = None,
    ) -> dict[str, Any]:
        """Merge a branch back into a target workspace (normally Main).

        Exactly one of `source_workspace_id` / `source_version_id` is used.
        Merge is the only branch operation the pipeline must never invoke on
        the model's say-so -- plan.md puts merge permission behind the
        code-enforced threshold check, not behind the LLM.
        """
        if bool(source_workspace_id) == bool(source_version_id):
            raise ValueError(
                "pass exactly one of source_workspace_id or source_version_id"
            )
        body: dict[str, Any] = {"documentId": self.config.document_id}
        if source_workspace_id:
            body["workspaceId"] = source_workspace_id
        else:
            body["versionId"] = source_version_id
        return self._post(
            f"/api/documents/{self.config.document_id}"
            f"/workspaces/{target_workspace_id}/merge",
            body,
        )
