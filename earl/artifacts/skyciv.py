"""Optional SkyCiv analysis/report adapter with honest, model-bound replay.

The committed public example is a genuine SkyCiv report of a DIFFERENT model.
It is never treated as evidence that this truss agrees with another solver.
Only a successful live response, or its exact model/member recording, can set
CrossCheck.performed. Custom A/I sections support analysis, not SkyCiv code
design; the check compares governing axial force magnitudes, in N.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import time
from math import isfinite
from pathlib import Path
from urllib.parse import urlsplit

import requests

from earl.analysis.solver import model_fingerprint, restrained_axes
from earl.config import PROJECT_ROOT, SkyCivConfig, demo_mode, load_env
from earl.contracts import CrossCheck, Decision, DependencyGraph, SkyCivReport
from earl.units import from_si, inertia_mm4

LOG = logging.getLogger(__name__)
FIXTURES = PROJECT_ROOT / "tests" / "fixtures" / "skyciv"


def to_skyciv(graph: DependencyGraph, load_case_id: str) -> dict:
    """Translate SI contracts once into SkyCiv's explicit metric unit system."""
    graph.validate()
    case = next(c for c in graph.load_cases if c.id == load_case_id)
    if case.include_self_weight or any(p.fz for p in case.point_loads):
        raise ValueError("only the validated planar, no-self-weight load case is supported")
    nodes = {node.id: index + 1 for index, node in enumerate(graph.nodes)}
    materials = {mat.id: index + 1 for index, mat in enumerate(graph.materials)}
    sections = {}
    members = {}
    for index, member in enumerate(graph.members, 1):
        section = graph.section(member.section_id)
        sections[str(index)] = {"name": section.name, "area": from_si(section.area, "mm2"),
                                "Iy": inertia_mm4(section.iy), "Iz": inertia_mm4(section.iz),
                                "J": inertia_mm4(section.j), "material_id": materials[member.material_id]}
        members[str(index)] = {"node_A": nodes[member.start_node], "node_B": nodes[member.end_node],
                                "section_id": index, "type": "normal", "rotation_angle": 0,
                                "fixity_A": "FFFFRR", "fixity_B": "FFFFRR"}
    supports = {}
    for index, node in enumerate(graph.nodes, 1):
        fixed_x, fixed_y = restrained_axes(node.support)
        supports[str(index)] = {"node": nodes[node.id],
                                "restraint_code": ("F" if fixed_x else "R") +
                                ("F" if fixed_y else "R") + "FFFF"}
    return {
        "settings": {"units": {"length": "m", "section_length": "mm", "material_strength": "mpa",
                                  "density": "kg/m3", "force": "kn", "moment": "kn-m",
                                  "pressure": "mpa", "mass": "kg", "translation": "mm", "stress": "mpa"},
                     "vertical_axis": "Y"},
        "nodes": {str(nodes[n.id]): {"x": n.x, "y": n.y, "z": n.z} for n in graph.nodes},
        "members": members, "sections": sections,
        "materials": {str(materials[m.id]): {"name": m.name, "elasticity_modulus": from_si(m.elastic_modulus, "MPa"),
                                             "density": m.density, "poissons_ratio": 0.3,
                                             "yield_strength": from_si(m.yield_strength, "MPa")}
                      for m in graph.materials},
        "supports": supports,
        "point_loads": {str(index): {"type": "n", "node": nodes[p.node_id],
                                      "x_mag": from_si(p.fx, "kN"), "y_mag": from_si(p.fy, "kN"),
                                      "z_mag": 0, "load_group": "applied"}
                        for index, p in enumerate(case.point_loads, 1)},
        "load_combinations": {"1": {"name": case.id, "applied": 1}},
        "self_weight": {"enabled": False, "x": 0, "y": 0, "z": 0},
    }


def _function(response: dict, name: str) -> dict:
    for item in response.get("functions", []):
        if item.get("function") == name:
            if item.get("status") != 0:
                raise ValueError(f"SkyCiv {name} did not succeed")
            return item["data"]
    raise ValueError(f"SkyCiv response is missing {name}")


def parse_response(response: dict, *, fingerprint: str, member_id: str,
                   pynite_force: float, provenance: str) -> tuple[SkyCivReport, CrossCheck]:
    """Parse documented fetchMemberResult array format; no guessed values."""
    if response.get("model_fingerprint") != fingerprint or response.get("member_id") != member_id:
        raise ValueError("recorded SkyCiv response does not match this exact model/member")
    raw = response["raw_response"]
    if any(function.get("status") != 0 and function.get("function") != "S3D.results.getAnalysisReport"
           for function in raw.get("functions", [])):
        raise ValueError("one or more SkyCiv analysis functions failed")
    values = _function(raw, "S3D.results.fetchMemberResult")
    if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], list) or not values[0]:
        raise ValueError("expected one load combination of member station results")
    # SkyCiv's explicitly selected force unit is kN. Compare demand magnitudes;
    # the distinct PyNite axial sign convention is not evidence of disagreement.
    stations = [float(value) for value in values[0]]
    if not all(isfinite(value) for value in stations):
        raise ValueError("nonfinite SkyCiv station result")
    force = max(abs(value) for value in stations) * 1000
    cross = CrossCheck(member_id=member_id, pynite_value=abs(pynite_force), skyciv_value=force,
                       provenance=provenance,
                       note="Governing axial demand magnitude (N), tolerance 5%; sign is not cross-checked.")
    cross.evaluate()
    link = None
    try:
        report_data = _function(raw, "S3D.results.getAnalysisReport")
        candidate = report_data.get("view_link") or report_data.get("download_link")
        if _trusted_report_url(candidate):
            link = candidate
    except (ValueError, KeyError, TypeError, AttributeError):
        pass
    note = "Linear static analysis, not an AISC/ASCE design check."
    if link is None:
        note += " Report/PDF unavailable or link rejected; numerical cross-check retained."
    report = SkyCivReport(report_id=f"S3D-{fingerprint[:12]}", url=link,
                         model_fingerprint=fingerprint, provenance=provenance, note=note)
    return report, cross


def _trusted_report_url(url: str) -> bool:
    if not isinstance(url, str):
        return False
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == "https" and not parsed.username and not parsed.password and
                parsed.hostname in {"skyciv.com", "solver.skyciv.com", "platform.skyciv.com"})
    except ValueError:
        return False


def _download_report(url: str, destination: Path) -> None:
    if not _trusted_report_url(url):
        raise ValueError("untrusted report URL")
    start = time.monotonic()
    chunks = []
    size = 0
    with requests.get(url, timeout=(2, 4), stream=True, allow_redirects=False) as response:
        response.raise_for_status()
        for chunk in response.iter_content(65536):
            size += len(chunk)
            if size > 8_000_000 or time.monotonic() - start > 8:
                raise ValueError("report exceeds download budget")
            chunks.append(chunk)
    data = b"".join(chunks)
    if not data.startswith(b"%PDF"):
        raise ValueError("report is not a PDF")
    destination.write_bytes(data)


def create_report(graph: DependencyGraph, decision: Decision, run_dir: Path, *,
                  allow_live: bool = False) -> tuple[SkyCivReport, CrossCheck]:
    graph.validate()
    decision.validate()
    run_dir.mkdir(parents=True, exist_ok=True)
    load_env()
    member = decision.governing_member
    if not decision.load_case_id or member is None:
        return _example(run_dir, "No completed solve to cross-check.")
    fingerprint = model_fingerprint(graph, decision.load_case_id)
    key = f"{fingerprint}-{member.member_id}"
    recording = PROJECT_ROOT / "out" / "recordings" / "skyciv" / f"{key}.json"
    if allow_live and not demo_mode() and os.environ.get("SKYCIV_API_KEY") and os.environ.get("SKYCIV_API_USERNAME"):
        try:
            config = SkyCivConfig.from_env()
            member_index = next(i for i, m in enumerate(graph.members, 1) if m.id == member.member_id)
            functions = [
                {"function": "S3D.session.start", "arguments": {"keep_open": False}},
                {"function": "S3D.model.set", "arguments": {"s3d_model": to_skyciv(graph, decision.load_case_id)}},
                {"function": "S3D.model.solve", "arguments": {"analysis_type": "linear", "repair_model": False,
                                                              "return_data": False}},
                {"function": "S3D.results.fetchMemberResult", "arguments": {"member_id": member_index,
                                                                            "LC": [1], "res_key": "axial", "type": "array"}},
                {"function": "S3D.results.getAnalysisReport", "arguments": {"job_name": f"EARL {decision.id}",
                                                                             "file_type": "pdf", "load_combinations": [1]}},
            ]
            response = requests.post(config.base_url, json={"auth": {"username": config.username, "key": config.key},
                                                               "options": {"validate_input": True, "response_data_only": False,
                                                                           "timeout": 6000}, "functions": functions},
                                     timeout=(2, 8), allow_redirects=False)
            response.raise_for_status()
            recorded = {"model_fingerprint": fingerprint, "member_id": member.member_id, "raw_response": response.json()}
            report, cross = parse_response(recorded, fingerprint=fingerprint, member_id=member.member_id,
                                           pynite_force=member.axial_force_after, provenance="live SkyCiv API")
            try:
                recording.parent.mkdir(parents=True, exist_ok=True)
                recording.write_text(json.dumps(recorded, indent=2, allow_nan=False), encoding="utf-8")
            except (OSError, ValueError, TypeError) as exc:
                LOG.warning("SkyCiv recording unavailable (%s); numerical cross-check retained", type(exc).__name__)
                report.note += " Exact API response could not be recorded locally."
            try:
                report_data = _function(recorded["raw_response"], "S3D.results.getAnalysisReport")
                pdf = run_dir / "skyciv.pdf"
                _download_report(report_data.get("download_link", report.url), pdf)
                report.local_path = str(pdf)
            except Exception as exc:
                LOG.warning("Report download unavailable (%s); numerical cross-check retained", type(exc).__name__)
            _preserve_reference(run_dir, report, cross)
            return report, cross
        except Exception as exc:
            LOG.warning("Optional SkyCiv call unavailable (%s); checking recorded fixtures", type(exc).__name__)
    for path in (FIXTURES / "responses" / f"{key}.json", recording):
        if not path.exists():
            continue
        try:
            report, cross = parse_response(json.loads(path.read_text()), fingerprint=fingerprint,
                                           member_id=member.member_id, pynite_force=member.axial_force_after,
                                           provenance="recorded SkyCiv API response (exact model)")
            _preserve_reference(run_dir, report, cross)
            return report, cross
        except (ValueError, KeyError, TypeError, OSError):
            LOG.warning("Ignoring invalid or mismatched SkyCiv recording")
    return _example(run_dir, "No live credentials or exact-model recording; independent check not performed.")


def _example(run_dir: Path, reason: str) -> tuple[SkyCivReport, CrossCheck]:
    metadata = json.loads((FIXTURES / "provenance.json").read_text())
    sample = FIXTURES / metadata["file"]
    if hashlib.sha256(sample.read_bytes()).hexdigest() != metadata["sha256"]:
        raise ValueError("published report fixture checksum mismatch")
    report = SkyCivReport(report_id="SKYCIV-PUBLISHED-EXAMPLE-2016", url=metadata["source_url"],
                         local_path=str(sample), provenance="recorded public example (different model)",
                         generated_at="2016-08-10", note=metadata["note"])
    cross = CrossCheck(performed=False, agrees=None, provenance=report.provenance, note=reason)
    _write_reference(run_dir, report, cross)
    return report, cross


def _write_reference(run_dir: Path, report: SkyCivReport, cross: CrossCheck) -> None:
    record = {"report": report.to_dict(), "cross_check": cross.to_dict()}
    (run_dir / "report.json").write_text(json.dumps(record, indent=2, allow_nan=False), encoding="utf-8")
    e = html.escape
    if report.model_fingerprint:
        link = "skyciv-pdf" if report.local_path else report.url
        link_label = "Open locally recorded PDF" if report.local_path else "Open SkyCiv report (network required)"
    else:
        link, link_label = "/api/fixture/skyciv", "Open locally recorded example PDF (different model)"
    report_anchor = f'<a href="{e(link, quote=True)}">{link_label}</a>' if link else "No report link available; the numerical comparison is retained in the Decision."
    body = f"""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>EARL report reference</title><style>body{{font:15px system-ui;max-width:760px;margin:40px auto;padding:0 24px;color:#242a2c;line-height:1.6}}a{{color:#176d55}}aside{{border-left:3px solid #b77914;padding:12px 18px;background:#fff8e9}}code{{overflow-wrap:anywhere}}</style>
<h1>Report reference</h1><p><code>{e(report.report_id)}</code></p><aside><strong>{e(report.provenance)}</strong><p>{e(report.note or '')}</p></aside>
<p>Independent numerical cross-check: <strong>{'performed' if cross.performed else 'NOT PERFORMED'}</strong>.</p>
<p>{e(cross.note or '')}</p><p>{report_anchor}</p>
<p>Onshape Simulation already provides assembly linear static analysis, stress, displacement, and safety factors. SkyCiv provides analysis and reports. EARL adds the acceptance gate, notification, and decision record, not better physics.</p></html>"""
    (run_dir / "report.html").write_text(body, encoding="utf-8")


def _preserve_reference(run_dir: Path, report: SkyCivReport, cross: CrossCheck) -> None:
    """Artifact persistence must not discard a completed numerical finding."""
    try:
        _write_reference(run_dir, report, cross)
    except OSError as exc:
        LOG.warning("Report reference could not be written (%s); cross-check retained", type(exc).__name__)
        report.note = (report.note or "") + " Local report-reference write failed; comparison retained in the Decision."
