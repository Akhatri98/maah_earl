"""Read-only Onshape ingestion with a deterministic synthetic fallback."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from earl.config import PROJECT_ROOT, demo_mode, load_env
from .onshape_client import OnshapeClient
from .parsers import parse_instances

FIXTURES = PROJECT_ROOT / "tests" / "fixtures" / "synthetic"
LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class IngestionInput:
    assembly: dict[str, Any]
    variables: list[dict[str, Any]]
    provenance: str
    note: str
    request_count: int = 0


def fixture_inputs(note: str = "Demo mode: synthetic Onshape-format snapshot.") -> IngestionInput:
    return IngestionInput(
        json.loads((FIXTURES / "truss_assembly.json").read_text(encoding="utf-8")),
        json.loads((FIXTURES / "truss_variables.json").read_text(encoding="utf-8")),
        "synthetic fixture", note,
    )


def ingest(*, allow_live: bool = False) -> IngestionInput:
    load_env()
    if not allow_live or demo_mode():
        return fixture_inputs()
    required = ("ONSHAPE_ACCESS_KEY", "ONSHAPE_SECRET_KEY", "ONSHAPE_DOCUMENT_ID",
                "ONSHAPE_WORKSPACE_ID")
    if not all(os.environ.get(key) for key in required):
        return fixture_inputs("Onshape credentials absent; synthetic fixture selected automatically.")
    client = None
    try:
        client = OnshapeClient.from_env(PROJECT_ROOT / "out" / "recordings" / "onshape")
        client.timeout = 4
        assembly_id = os.environ.get("ONSHAPE_ASSEMBLY_ID", "")
        variable_id = os.environ.get("ONSHAPE_VARIABLE_ELEMENT_ID", "")
        if not assembly_id or not variable_id:
            elements = client.list_elements()
            assembly_id = assembly_id or next(e["id"] for e in elements
                                               if e.get("elementType") == "ASSEMBLY")
            variable_id = variable_id or next(e["id"] for e in elements
                                               if e.get("elementType") in ("VARIABLESTUDIO", "PARTSTUDIO"))
        assembly = client.get_assembly_definition(assembly_id)
        variables = client.get_variables(variable_id)
        if not parse_instances(assembly):
            return fixture_inputs("Live Onshape assembly is empty; explicit synthetic demo fallback.")
        return IngestionInput(assembly, variables, "live Onshape", "Read-only CAD snapshot.", client.request_count)
    except Exception as exc:
        LOG.warning("Onshape unavailable (%s); serving fixture", type(exc).__name__)
        return fixture_inputs(f"Onshape unavailable ({type(exc).__name__}); synthetic fixture selected.")
