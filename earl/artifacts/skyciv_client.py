"""SkyCiv S3D API client. [Track B, Sprint 3B]

SkyCiv is the *trusted* solver in the pipeline (plan.md, stage 3): a commercial
structural-analysis platform practicing engineers recognise. It does two jobs
at once for an escalated change -- it produces the report the ECN cites, and,
because it runs its own independent analysis, it cross-checks the fast PyNite
gate. Neither job is worth anything if the model we send it is subtly wrong,
which is why this module is mostly about unit conversion and restraint codes.

PAYLOAD SHAPE IS UNVERIFIED AGAINST THE LIVE API. The SkyCiv documentation
site was not reachable from the sandbox this was written in, so the request
format below follows the v3 "functions" request format from memory of the
docs. Every function name and code string therefore lives in the constants
block at the top of this file so a single smoke run (`scripts/skyciv_smoke.py`,
which records the raw responses to `tests/fixtures/skyciv/recorded/`) can fix
any of them in one place. Response parsing is deliberately shape-tolerant for
the same reason, and every raw response is recorded when `record_dir` is set.

Conventions (binding, see the Track B spec):

  * The graph is SI (m, N, Pa). SkyCiv is sent metric with SkyCiv's own unit
    block (`SKYCIV_UNITS`): lengths in m, section dimensions in mm (so areas
    are mm^2 and inertias mm^4), material strengths and moduli in MPa,
    forces in kN.
  * Node/member/section/material ids are 1-based integers in the SkyCiv
    model; `S3DModel.node_index` / `member_index` map contract ids to them.
  * Axial force sign in everything Track B emits is tension positive. SkyCiv's
    sign convention has NOT been verified; it is ASSUMED tension-positive
    (`parse_member_results(..., sign_hint=...)` exists to flip it once the
    smoke test settles the question). The cross-check compares magnitudes, so
    a wrong assumption cannot produce a false "agrees".
  * A truss is modelled by releasing bending at both member ends (fixity
    "FFFFRR") -- so every node also needs rotational restraint, and a planar
    truss needs the out-of-plane translation restrained at every node,
    exactly as the PyNite adapter does. ROLLER_X / ROLLER_Y semantics assume
    gravity along -Y.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import requests

from ..config import SkyCivConfig
from ..contracts import DependencyGraph, SupportType

# ---------------------------------------------------------------------------
# Constants block. EVERYTHING that names a SkyCiv function or encodes a SkyCiv
# string lives here so the smoke test can correct the lot in one place.
# ---------------------------------------------------------------------------

FN_SESSION_START = "S3D.session.start"
FN_MODEL_SET = "S3D.model.set"
FN_MODEL_SOLVE = "S3D.model.solve"
FN_REPORT = "S3D.results.getReport"
# Tried by scripts/skyciv_smoke.py if FN_REPORT comes back with status != 0.
FN_REPORT_ALT = "S3D.results.getAnalysisReport"
FN_DESIGN_INPUT = "S3D.design.member.getInput"
FN_DESIGN_CHECK = "S3D.design.member.check"

# The docs' sample input uses this underscore form.
DEFAULT_DESIGN_CODE = "AISC_360-16_LRFD"

SKYCIV_UNITS = {
    "length": "m",
    "section_length": "mm",
    "material_strength": "mpa",
    "density": "kg/m3",
    "force": "kn",
    "moment": "kn-m",
    "pressure": "kpa",
    "mass": "kg",
    "translation": "mm",
    "stress": "mpa",
}

VERTICAL_AXIS = "Y"
MEMBER_TYPE = "normal_continuous"
TRUSS_FIXITY = "FFFFRR"          # translations fixed, bending released (truss)
POINT_LOAD_TYPE = "N"            # nodal point load
LOAD_GROUP = "LG1"
SELF_WEIGHT_GROUP = "SW1"
MATERIAL_CLASS = "steel"
POISSON_RATIO = 0.3
ULTIMATE_OVER_YIELD = 1.3
ANALYSIS_TYPE = "linear"
REPORT_FILE_TYPE = "pdf"

# Restraint codes: letters are Tx Ty Tz Rx Ry Rz, F = fixed, R = released.
# Rotations are always fixed: with every member end released, nodal rotations
# have no stiffness and must not be left free. The planar rule (R5) then
# forces the constant-coordinate translation to F on top of these.
RESTRAINT_CODES: dict[SupportType, str] = {
    SupportType.FREE: "RRRFFF",
    SupportType.PIN: "FFFFFF",
    SupportType.ROLLER_X: "RFFFFF",     # free along X
    SupportType.ROLLER_Y: "FRFFFF",     # free along Y
    SupportType.FIXED: "FFFFFF",
}

RECORD_SLUG_CALL_1 = "skyciv_analyze_1"
RECORD_SLUG_CALL_2 = "skyciv_analyze_2"

# SI -> SkyCiv metric factors.
M2_TO_MM2 = 1e6
M4_TO_MM4 = 1e12
PA_TO_MPA = 1e-6
N_TO_KN = 1e-3
KN_TO_N = 1e3
MPA_TO_PA = 1e6


class SkyCivError(RuntimeError):
    """A SkyCiv call failed. `status` is the HTTP or function status when
    known; `function` names the API function that failed, if any; `body` is
    the raw HTTP response text for an HTTP-level failure (the message only
    carries a prefix of it), so the record file keeps the whole thing."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        function: str | None = None,
        body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.function = function
        self.body = body


# ---------------------------------------------------------------------------
# Model construction
# ---------------------------------------------------------------------------

@dataclass
class S3DModel:
    """The SkyCiv `s3d_model` dict plus the id maps needed to read results
    back into contract ids (SkyCiv ids are 1-based integers)."""

    model: dict[str, Any]
    node_index: dict[str, int]
    member_index: dict[str, int]


def _constant_axis(graph: DependencyGraph) -> int | None:
    """Index (0=x, 1=y, 2=z) of a coordinate shared by every node, or None.

    A planar truss has no out-of-plane stiffness once bending is released, so
    that translation must be restrained at every node (R5). Checked z first
    because that is the conventional plane; the first constant axis wins.
    """
    if not graph.nodes:
        return None
    for axis in (2, 1, 0):
        values = [(n.x, n.y, n.z)[axis] for n in graph.nodes]
        if all(math.isclose(v, values[0], abs_tol=1e-12) for v in values):
            return axis
    return None


def restraint_code(support: SupportType, constant_axis: int | None) -> str:
    """SkyCiv restraint code for a contract support, with the planar rule."""
    code = list(RESTRAINT_CODES[support])
    if constant_axis is not None:
        code[constant_axis] = "F"
    return "".join(code)


def build_s3d_model(graph: DependencyGraph, load_case_id: str) -> S3DModel:
    """Translate a contract graph + one load case into a SkyCiv s3d_model.

    Pure construction, no network. Raises ValueError for a non-SI graph, an
    invalid graph, or an unknown load case id.
    """
    graph.units.assert_si()
    graph.validate()
    load_case = next((lc for lc in graph.load_cases if lc.id == load_case_id), None)
    if load_case is None:
        raise ValueError(
            f"unknown load case {load_case_id!r}; graph has "
            f"{[lc.id for lc in graph.load_cases]}"
        )

    node_index = {n.id: i for i, n in enumerate(graph.nodes, start=1)}
    member_index = {m.id: i for i, m in enumerate(graph.members, start=1)}
    material_index = {m.id: i for i, m in enumerate(graph.materials, start=1)}
    constant_axis = _constant_axis(graph)

    nodes = {
        str(node_index[n.id]): {"x": float(n.x), "y": float(n.y), "z": float(n.z)}
        for n in graph.nodes
    }

    materials = {}
    for mat in graph.materials:
        materials[str(material_index[mat.id])] = {
            "name": mat.name,
            "density": float(mat.density),
            "elasticity_modulus": mat.elastic_modulus * PA_TO_MPA,
            "poissons_ratio": POISSON_RATIO,
            "yield_strength": mat.yield_strength * PA_TO_MPA,
            "ultimate_strength": mat.yield_strength * ULTIMATE_OVER_YIELD * PA_TO_MPA,
            "class": MATERIAL_CLASS,
        }

    # A SkyCiv section carries its material; a contract section does not (the
    # member does), so one SkyCiv section is emitted per distinct
    # (section, material) pair actually used by a member.
    sections: dict[str, Any] = {}
    section_index: dict[tuple[str, str], int] = {}
    for m in graph.members:
        key = (m.section_id, m.material_id)
        if key in section_index:
            continue
        sec = graph.section(m.section_id)
        sid = len(section_index) + 1
        section_index[key] = sid
        sections[str(sid)] = {
            "name": sec.name,
            "material_id": material_index[m.material_id],
            "area": sec.area * M2_TO_MM2,
            "Iz": sec.iz * M4_TO_MM4,
            "Iy": sec.iy * M4_TO_MM4,
            "J": sec.j * M4_TO_MM4,
        }

    members = {
        str(member_index[m.id]): {
            "type": MEMBER_TYPE,
            "node_A": node_index[m.start_node],
            "node_B": node_index[m.end_node],
            "section_id": section_index[(m.section_id, m.material_id)],
            "rotation_angle": 0,
            "fixity_A": TRUSS_FIXITY,
            "fixity_B": TRUSS_FIXITY,
        }
        for m in graph.members
    }

    # Every node gets a support entry: even a FREE truss node needs its
    # rotations (and, when planar, the out-of-plane translation) restrained.
    supports = {
        str(node_index[n.id]): {
            "node": node_index[n.id],
            "restraint_code": restraint_code(n.support, constant_axis),
        }
        for n in graph.nodes
    }

    point_loads = {
        str(i): {
            "type": POINT_LOAD_TYPE,
            "node": node_index[pl.node_id],
            "x_mag": pl.fx * N_TO_KN,
            "y_mag": pl.fy * N_TO_KN,
            "z_mag": pl.fz * N_TO_KN,
            "load_group": LOAD_GROUP,
        }
        for i, pl in enumerate(load_case.point_loads, start=1)
    }

    combination: dict[str, Any] = {"name": load_case.id, LOAD_GROUP: 1}
    self_weight: dict[str, Any] = {}
    if load_case.include_self_weight:
        self_weight["1"] = {"x": 0, "y": -1, "z": 0, "LG": SELF_WEIGHT_GROUP}
        combination[SELF_WEIGHT_GROUP] = 1

    model = {
        "settings": {
            "units": dict(SKYCIV_UNITS),
            "vertical_axis": VERTICAL_AXIS,
            "auto_stabilize_model": True,
        },
        "nodes": nodes,
        "members": members,
        "sections": sections,
        "materials": materials,
        "supports": supports,
        "point_loads": point_loads,
        "self_weight": self_weight,
        "load_combinations": {"1": combination},
    }
    return S3DModel(model=model, node_index=node_index, member_index=member_index)


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

@dataclass
class SkyCivMemberResult:
    member_id: str
    axial_force: float            # N, tension positive (assumed; see module doc)
    stress: float | None = None   # Pa, same sign


@dataclass
class SkyCivDesignResult:
    member_id: str
    ratio: float
    passed: bool


@dataclass
class SkyCivRun:
    session_id: str | None
    member_results: dict[str, SkyCivMemberResult]
    report_url: str | None
    design_results: dict[str, SkyCivDesignResult]
    design_code: str | None
    raw: dict[str, Any]
    api_calls: int
    warnings: list[str] = field(default_factory=list)


@dataclass
class RequestRecord:
    label: str
    functions: list[str]
    status: int
    bytes: int


def _reduce_axial(value: Any) -> float | None:
    """Collapse SkyCiv's several `axial` shapes to the value with max |x|.

    Accepts a number, a flat list of numbers, a list of [position, value]
    pairs (position along the member; the value is [1]), or a dict
    position -> value. Returns None for anything unrecognised.
    """
    candidates: list[float] = []
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        candidates.append(float(value))
    elif isinstance(value, str):
        try:
            candidates.append(float(value))
        except ValueError:
            return None
    elif isinstance(value, dict):
        for v in value.values():
            r = _reduce_axial(v)
            if r is not None:
                candidates.append(r)
    elif isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                r = _reduce_axial(item[1])
            else:
                r = _reduce_axial(item)
            if r is not None:
                candidates.append(r)
    if not candidates:
        return None
    return max(candidates, key=abs)


_AXIAL_KEYS = ("axial", "axial_force", "axial_stress", "Fx", "N")
_COMBO_KEYS = ("member_forces", "member_stresses")


def _combination_objects(data: Any) -> list[dict[str, Any]]:
    """Normalise results into a list of per-combination objects (R15).

    `data` may be a single combination object (has member_forces /
    member_stresses), a dict keyed by combination id, or a list of
    combination objects. Anything else yields [].
    """
    if isinstance(data, dict):
        if any(k in data for k in _COMBO_KEYS):
            return [data]
        return [v for v in data.values() if isinstance(v, dict)]
    if isinstance(data, (list, tuple)):
        return [c for c in data if isinstance(c, dict)]
    return []


def _per_member(table: Any, member_index: dict[str, int]) -> dict[str, float]:
    """member key (str or int SkyCiv id) -> reduced axial, in contract ids."""
    by_skyciv_id = {str(v): k for k, v in member_index.items()}
    out: dict[str, float] = {}
    if not isinstance(table, dict):
        return out
    for key, entry in table.items():
        member_id = by_skyciv_id.get(str(key))
        if member_id is None:
            continue
        raw = None
        if isinstance(entry, dict):
            for ak in _AXIAL_KEYS:
                if ak in entry:
                    raw = entry[ak]
                    break
        else:
            raw = entry
        value = _reduce_axial(raw)
        if value is None:
            continue
        if member_id in out:
            out[member_id] = max(out[member_id], value, key=abs)
        else:
            out[member_id] = value
    return out


def parse_member_results(
    solve_data: Any,
    member_index: dict[str, int],
    *,
    sign_hint: float | None = None,
) -> dict[str, SkyCivMemberResult]:
    """Read member axial forces (kN -> N) and stresses (MPa -> Pa) out of a
    `S3D.model.solve` payload, tolerating the several shapes SkyCiv may use.

    SkyCiv's axial sign is ASSUMED tension-positive; pass `sign_hint=-1.0`
    to flip it once the smoke test shows otherwise.
    """
    sign = 1.0 if sign_hint is None else float(sign_hint)
    forces: dict[str, float] = {}
    stresses: dict[str, float] = {}
    for combo in _combination_objects(solve_data):
        for member_id, value in _per_member(combo.get("member_forces"), member_index).items():
            forces[member_id] = max(forces.get(member_id, 0.0), value, key=abs)
        for member_id, value in _per_member(combo.get("member_stresses"), member_index).items():
            stresses[member_id] = max(stresses.get(member_id, 0.0), value, key=abs)

    results: dict[str, SkyCivMemberResult] = {}
    for member_id in member_index:
        if member_id not in forces:
            continue
        stress = stresses.get(member_id)
        results[member_id] = SkyCivMemberResult(
            member_id=member_id,
            axial_force=sign * forces[member_id] * KN_TO_N,
            stress=None if stress is None else sign * stress * MPA_TO_PA,
        )
    return results


_LINK_HINTS = ("download", "link", "url")


def _is_http_url(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(("http://", "https://"))


def parse_report_link(data: Any) -> str | None:
    """First http(s) string whose key contains download / link / url, searched
    depth-first; a bare http(s) string is accepted as-is.

    Within one dict the hints are tried in priority order (download, then
    link, then url) across *all* keys before recursing, and a matching key
    whose value is not an http(s) URL is skipped rather than returned. Both
    rules exist because SkyCiv's response shape is unverified: a decoy key
    such as `url_expiry: "24h"` or `link_type: "pdf"` sitting before the real
    `download_link` must not shadow it.
    """
    if isinstance(data, str):
        return data if _is_http_url(data) else None
    if isinstance(data, dict):
        for hint in _LINK_HINTS:
            for key, value in data.items():
                if hint in str(key).lower() and _is_http_url(value):
                    return value
        for value in data.values():
            found = parse_report_link(value) if isinstance(value, (dict, list)) else None
            if found:
                return found
    if isinstance(data, (list, tuple)):
        for item in data:
            found = parse_report_link(item)
            if found:
                return found
    return None


_RATIO_KEYS = ("ratio", "utilization", "utilisation", "utility_ratio", "max_ratio", "unity")
_PASS_KEYS = ("pass", "passed", "status", "result")
_MEMBER_KEYS = ("member", "member_id", "id")


def _to_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _passed(entry: dict[str, Any], ratio: float) -> bool:
    for key in _PASS_KEYS:
        if key in entry:
            value = entry[key]
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.strip().lower() in ("pass", "passed", "ok", "true", "yes")
    return ratio <= 1.0


def parse_design_results(
    data: Any, member_index: dict[str, int]
) -> dict[str, SkyCivDesignResult]:
    """Read per-member design ratios out of a `S3D.design.member.check` payload.

    Tolerates a dict keyed by member id, a list of entries carrying a member
    id, and either of those nested under a wrapper key (results / members /
    data / member_results).
    """
    by_skyciv_id = {str(v): k for k, v in member_index.items()}
    results: dict[str, SkyCivDesignResult] = {}

    def visit(member_key: Any, entry: Any) -> None:
        if not isinstance(entry, dict):
            return
        member_id = by_skyciv_id.get(str(member_key))
        if member_id is None:
            for mk in _MEMBER_KEYS:
                if mk in entry:
                    member_id = by_skyciv_id.get(str(entry[mk]))
                    if member_id:
                        break
        if member_id is None:
            return
        ratio = None
        for rk in _RATIO_KEYS:
            if rk in entry:
                ratio = _to_float(entry[rk])
                if ratio is not None:
                    break
        if ratio is None:
            return
        results[member_id] = SkyCivDesignResult(
            member_id=member_id, ratio=ratio, passed=_passed(entry, ratio)
        )

    container = data
    if isinstance(container, dict):
        for wrapper in ("results", "members", "data", "member_results"):
            if wrapper in container and isinstance(container[wrapper], (dict, list)):
                container = container[wrapper]
                break
    if isinstance(container, dict):
        for key, entry in container.items():
            visit(key, entry)
    elif isinstance(container, (list, tuple)):
        for entry in container:
            visit(None, entry)
    return results


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

def _function_entries(raw: Any) -> list[dict[str, Any]]:
    """Per-function result entries of a v3 response.

    The documented shape is `{"response": {...}, "functions": [{"function",
    "status", "msg", "data"}, ...]}`; when only a single `response` object is
    present it is treated as the one and only entry.
    """
    if not isinstance(raw, dict):
        return []
    functions = raw.get("functions")
    if isinstance(functions, list):
        return [f for f in functions if isinstance(f, dict)]
    if isinstance(functions, dict):
        return [f for f in functions.values() if isinstance(f, dict)]
    response = raw.get("response")
    if isinstance(response, dict) and "status" in response:
        return [response]
    return []


def _find_function(
    entries: list[dict[str, Any]], name: str, position: int
) -> dict[str, Any] | None:
    """Match an entry by function name, falling back to its position in the
    request when the response does not echo names."""
    for entry in entries:
        if entry.get("function") == name:
            return entry
    if entries and all("function" not in e for e in entries) and position < len(entries):
        return entries[position]
    return None


def _status(entry: dict[str, Any]) -> int | None:
    value = entry.get("status")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _session_id(raw: dict[str, Any], entries: list[dict[str, Any]]) -> str | None:
    for key in ("session_id", "last_session_id"):
        value = raw.get(key)
        if isinstance(value, str) and value:
            return value
    start = _find_function(entries, FN_SESSION_START, 0)
    if start is not None:
        data = start.get("data")
        if isinstance(data, str) and data:
            return data
        if isinstance(data, dict):
            for key in ("session_id", "id"):
                value = data.get(key)
                if isinstance(value, str) and value:
                    return value
    return None


@dataclass
class SkyCivClient:
    """Thin client over the v3 single-endpoint API.

    `transport` (payload -> parsed JSON dict) and `downloader` (url, dest ->
    Path) are injectable so the whole analyze/escalate path is testable
    offline with recorded or synthetic responses. Every call is logged and,
    with `record_dir`, saved raw -- SkyCiv calls are metered.
    """

    # repr=False: the config carries the API key, and a dataclass repr lands
    # in tracebacks, logs and test failure output.
    config: SkyCivConfig = field(repr=False)
    record_dir: Path | None = None
    timeout: int = 180
    transport: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    downloader: Callable[[str, Path], Path] | None = None
    log: list[RequestRecord] = field(default_factory=list)

    @classmethod
    def from_env(cls, record_dir: Path | str | None = None) -> SkyCivClient:
        return cls(
            config=SkyCivConfig.from_env(),
            record_dir=Path(record_dir) if record_dir else None,
        )

    @property
    def request_count(self) -> int:
        return len(self.log)

    # -- transport ---------------------------------------------------------

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = self.config.base_url
        resp = requests.post(url, json=payload, timeout=self.timeout)
        if resp.status_code != 200:
            raise SkyCivError(
                f"HTTP {resp.status_code} for {url}: {resp.text[:500]}",
                status=resp.status_code,
                body=resp.text,
            )
        try:
            data = resp.json()
        except ValueError as e:
            raise SkyCivError(
                f"non-JSON body from {url}: {resp.text[:200]!r}",
                status=resp.status_code,
                body=resp.text,
            ) from e
        if not isinstance(data, dict):
            raise SkyCivError(
                f"unexpected JSON body type {type(data).__name__} from {url}",
                status=resp.status_code,
            )
        return data

    def call(
        self,
        functions: list[dict[str, Any]],
        *,
        label: str,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """One POST carrying a list of functions; returns the parsed body.

        Raises SkyCivError for HTTP != 200, a non-JSON body, or a transport
        failure. Per-function `status` is NOT checked here (R14) -- callers
        decide which functions are fatal.
        """
        auth: dict[str, Any] = {"username": self.config.username, "key": self.config.key}
        if session_id:
            auth["session_id"] = session_id
        payload = {
            "auth": auth,
            "options": {"validate_input": True, "response_data_only": False},
            "functions": functions,
        }
        names = [str(f.get("function")) for f in functions]
        try:
            data = (self.transport or self._post)(payload)
        except SkyCivError as e:
            self.log.append(RequestRecord(label, names, -1, 0))
            self._record_error(label, names, e)
            raise
        except requests.RequestException as e:
            self.log.append(RequestRecord(label, names, -1, 0))
            self._record_error(label, names, e)
            raise SkyCivError(f"transport failure for {label}: {e}") from e
        if not isinstance(data, dict):
            raise SkyCivError(f"transport returned {type(data).__name__}, expected dict")
        self.log.append(
            RequestRecord(label, names, 200, len(json.dumps(data, default=str)))
        )
        if self.record_dir:
            self._record(label, data)
        return data

    def _record(self, label: str, data: Any) -> None:
        self.record_dir.mkdir(parents=True, exist_ok=True)
        (self.record_dir / f"{label}.json").write_text(
            json.dumps(data, indent=2, default=str), encoding="utf-8"
        )

    def _record_error(self, label: str, names: list[str], exc: BaseException) -> None:
        """Record a failed call as `<label>_error.json` when recording.

        A metered call that failed is still a call worth keeping: the smoke
        test's whole purpose is to see what the live API says, and an HTTP
        error body (the first 500 chars are in the SkyCivError message) is
        often the only clue as to which function name or unit string is
        wrong. Recording must never mask the original failure, so an
        unwritable record_dir is ignored here.
        """
        if not self.record_dir:
            return
        record = {
            "label": label,
            "functions": names,
            "error": type(exc).__name__,
            "message": str(exc),
            "status": getattr(exc, "status", None),
            "function": getattr(exc, "function", None),
            "body": getattr(exc, "body", None),
        }
        try:
            self._record(f"{label}_error", record)
        except OSError:
            pass

    # -- the analysis flow -------------------------------------------------

    def analyze(
        self,
        graph: DependencyGraph,
        load_case_id: str,
        *,
        design_code: str | None = DEFAULT_DESIGN_CODE,
        want_report: bool = True,
    ) -> SkyCivRun:
        """Set, solve and (optionally) report/design-check one load case.

        Call 1: session.start, model.set, model.solve [, getReport]
        [, design.member.getInput]. Only the first three are fatal; report
        and design failures become `SkyCivRun.warnings`. Call 2 (design
        check) needs the design input from call 1 plus the session id, so
        it is only sent when both exist.
        """
        s3d = build_s3d_model(graph, load_case_id)
        keep_open = bool(design_code)
        functions: list[dict[str, Any]] = [
            {"function": FN_SESSION_START, "arguments": {"keep_open": keep_open}},
            {"function": FN_MODEL_SET, "arguments": {"s3d_model": s3d.model}},
            {
                "function": FN_MODEL_SOLVE,
                "arguments": {"analysis_type": ANALYSIS_TYPE, "repair_model": True},
            },
        ]
        if want_report:
            functions.append(
                {"function": FN_REPORT, "arguments": {"file_type": REPORT_FILE_TYPE}}
            )
        if design_code:
            functions.append(
                {"function": FN_DESIGN_INPUT, "arguments": {"design_code": design_code}}
            )

        raw1 = self.call(functions, label=RECORD_SLUG_CALL_1)
        entries = _function_entries(raw1)
        positions = {f["function"]: i for i, f in enumerate(functions)}
        warnings: list[str] = []

        def lookup(name: str) -> dict[str, Any] | None:
            return _find_function(entries, name, positions[name])

        for name in (FN_SESSION_START, FN_MODEL_SET, FN_MODEL_SOLVE):
            entry = lookup(name)
            if entry is None:
                raise SkyCivError(
                    f"{name}: no result entry in the SkyCiv response", function=name
                )
            status = _status(entry)
            if status != 0:
                raise SkyCivError(
                    f"{name} failed with status {status}: {entry.get('msg', 'no message')}",
                    status=status,
                    function=name,
                )

        solve_entry = lookup(FN_MODEL_SOLVE)
        assert solve_entry is not None
        member_results = parse_member_results(solve_entry.get("data"), s3d.member_index)
        if not member_results:
            warnings.append(
                f"{FN_MODEL_SOLVE}: no member axial forces recognised in the response "
                "(shape unverified; see raw)"
            )

        report_url: str | None = None
        if want_report:
            entry = lookup(FN_REPORT)
            if entry is None or _status(entry) != 0:
                msg = "no result entry" if entry is None else entry.get("msg", "no message")
                warnings.append(f"{FN_REPORT} failed: {msg}")
            else:
                report_url = parse_report_link(entry.get("data"))
                if report_url is None:
                    warnings.append(f"{FN_REPORT}: no download link found in the response")

        design_input: Any = None
        if design_code:
            entry = lookup(FN_DESIGN_INPUT)
            if entry is None or _status(entry) != 0:
                msg = "no result entry" if entry is None else entry.get("msg", "no message")
                warnings.append(f"{FN_DESIGN_INPUT} failed: {msg}")
            else:
                design_input = entry.get("data")
                if design_input is None:
                    warnings.append(f"{FN_DESIGN_INPUT}: empty design input; check skipped")

        session_id = _session_id(raw1, entries)
        raw: dict[str, Any] = {"call_1": raw1}
        api_calls = 1
        design_results: dict[str, SkyCivDesignResult] = {}

        if design_input is not None:
            if session_id is None:
                warnings.append(
                    f"{FN_DESIGN_CHECK} skipped: no session id in the call-1 response"
                )
            else:
                check_fn = [
                    {
                        "function": FN_DESIGN_CHECK,
                        "arguments": {
                            "design_code": design_code,
                            "design_input": design_input,
                        },
                    }
                ]
                raw2 = self.call(check_fn, label=RECORD_SLUG_CALL_2, session_id=session_id)
                raw["call_2"] = raw2
                api_calls = 2
                entry = _find_function(_function_entries(raw2), FN_DESIGN_CHECK, 0)
                if entry is None or _status(entry) != 0:
                    msg = "no result entry" if entry is None else entry.get("msg", "no message")
                    warnings.append(f"{FN_DESIGN_CHECK} failed: {msg}")
                else:
                    design_results = parse_design_results(entry.get("data"), s3d.member_index)
                    if not design_results:
                        warnings.append(
                            f"{FN_DESIGN_CHECK}: no per-member ratios recognised (see raw)"
                        )

        return SkyCivRun(
            session_id=session_id,
            member_results=member_results,
            report_url=report_url,
            design_results=design_results,
            design_code=design_code if design_code else None,
            raw=raw,
            api_calls=api_calls,
            warnings=warnings,
        )

    # -- report download ---------------------------------------------------

    def download_report(self, url: str, dest: Path) -> Path:
        """Stream the report at `url` to `dest` (parents created)."""
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if self.downloader is not None:
            return Path(self.downloader(url, dest))
        with requests.get(url, stream=True, timeout=self.timeout) as resp:
            if resp.status_code != 200:
                raise SkyCivError(
                    f"HTTP {resp.status_code} downloading report {url}",
                    status=resp.status_code,
                )
            with dest.open("wb") as fh:
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        fh.write(chunk)
        return dest
