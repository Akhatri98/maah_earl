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

import json
import re
from dataclasses import dataclass, field
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

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.config.base_url}{path}"
        resp = requests.get(
            url,
            params=params,
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
            RequestRecord("GET", path, resp.status_code, len(resp.content))
        )

        if resp.status_code != 200:
            raise OnshapeError(resp.status_code, resp.text[:500], url)

        data = resp.json()
        if self.record_dir:
            self._record(path, data)
        return data

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

    def get_assembly_definition(
        self, element_id: str, *, include_mate_features: bool = True
    ) -> dict[str, Any]:
        """Assembly instances, occurrences and (optionally) mate features.

        `includeMateFeatures` folds the mate data into this single response,
        which is why it is preferred over a separate /features call.
        """
        params = {
            "includeMateFeatures": str(include_mate_features).lower(),
            "includeMateConnectors": "false",
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
