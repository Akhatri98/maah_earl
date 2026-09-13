"""Reproducible twenty-case evaluation; the web app only reads committed JSON."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

from earl.analysis import capacity, gate, solver
from earl.analysis.benchmark import validate_benchmark
from earl.config import PROJECT_ROOT
from earl.contracts import Decision, Outcome
from earl.ingestion.source import fixture_inputs
from earl.ingestion.truss_map import map_assembly
from earl.scenarios import CATALOGUE, catalogue, make_change
from .baseline import select_members, verify_selected

CACHE = PROJECT_ROOT / "earl" / "eval" / "cache"
PROTOCOL = "selection-required-v1"


def dropped_dominoes(failing: list[str], reported: list[str]) -> list[str]:
    return sorted(set(failing) - set(reported), key=lambda mid: int(mid[1:]))


def _summary(decision: Decision, failing: list[str]) -> dict:
    decision.validate()
    dropped = dropped_dominoes(failing, decision.violating_member_ids)
    return {"outcome": decision.outcome.value, "reported_member_ids": decision.violating_member_ids,
            "dropped_member_ids": dropped, "dropped_dominoes": len(dropped),
            "caught_dominoes": len(set(failing) & set(decision.violating_member_ids)),
            "error": decision.error_message}


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")


def evaluate(*, write: bool = False, cache_dir: Path | None = None, allow_live_baseline: bool = False) -> dict:
    """Freeze ground truth and both reports. No network by default."""
    cache = Path(cache_dir) if cache_dir is not None else CACHE
    benchmark = validate_benchmark()
    source = fixture_inputs()
    graph = map_assembly(source.assembly, source.variables, provenance=source.provenance)
    scenarios = catalogue()
    if len(scenarios) != 20 or len({s["id"] for s in scenarios}) != 20:
        raise ValueError("the evaluation catalogue must contain twenty distinct scenarios")
    base_solve = solver.solve(graph)
    base_caps = capacity.capacities(graph, base_solve.axial_forces)
    initial_failures = [mid for mid, cap in base_caps.items()
                        if cap.safety_factor is not None and cap.safety_factor < graph.design_target]
    cases = []
    for scenario in scenarios:
        before = deepcopy(graph)
        before.change = make_change(before, scenario["id"])
        after = before.change.apply(before)
        # Selection is frozen before either system's post-change results exist.
        selection = select_members(before, after, allow_live=allow_live_baseline)
        baseline = verify_selected(after, selection)
        solved = None
        capacities = None
        try:
            solved = solver.solve(after)
            capacities = capacity.capacities(after, solved.axial_forces)
            truth_failures = [mid for mid, cap in capacities.items()
                              if cap.safety_factor is not None and cap.safety_factor < after.design_target]
            earl = gate.evaluate(before, after, base_solve, solved, decision_id=f"eval-{scenario['id']}",
                                 before_capacities=base_caps, after_capacities=capacities)
        except solver.SolveError as exc:
            truth_failures = []
            earl = gate.error_decision(decision_id=f"eval-{scenario['id']}", graph_id=after.id,
                                       change_id=after.change.id, description=after.change.description, message=str(exc))
        earl.validate()
        baseline.validate()
        case = {"id": scenario["id"], "name": scenario["name"], "kind": scenario["kind"],
                "change": after.change.to_dict(), "ground_truth_failing_ids": truth_failures,
                "newly_failing_ids": dropped_dominoes(truth_failures, initial_failures),
                "earl": _summary(earl, truth_failures), "baseline": _summary(baseline, truth_failures)}
        case["baseline"].update(selected_member_ids=selection.member_ids, provenance=selection.provenance,
                                unverified_member_ids=[m.id for m in after.members if m.id not in selection.member_ids])
        cases.append(case)
        if write:
            _save(cache / "truth" / f"{scenario['id']}.json", {
                "scenario": scenario, "before_graph": before.to_dict(), "after_graph": after.to_dict(),
                "before_solve": base_solve.to_dict(), "after_solve": solved.to_dict() if solved else None,
                "capacities": {mid: cap.to_dict() for mid, cap in capacities.items()} if capacities else None,
                "ground_truth_failing_ids": truth_failures, "earl_decision": earl.to_dict(),
            })
            _save(cache / "baseline" / f"{scenario['id']}.json", {
                "scenario_id": scenario["id"], "protocol": PROTOCOL, "selection": selection.__dict__,
                "decision": baseline.to_dict(), "score": case["baseline"],
            })
    summary = {"scenario_count": len(cases), "ground_truth_failures": sum(len(c["ground_truth_failing_ids"]) for c in cases),
               "initial_failing_ids": initial_failures, "baseline_label": "rule-based baseline" if not allow_live_baseline else "see per-case provenance"}
    for system in ("earl", "baseline"):
        summary[system] = {
            "dropped_dominoes": sum(c[system]["dropped_dominoes"] for c in cases),
            "caught_dominoes": sum(c[system]["caught_dominoes"] for c in cases),
            "scenarios_with_drops": sum(c[system]["dropped_dominoes"] > 0 for c in cases),
            "analysis_errors": sum(c[system]["outcome"] == Outcome.ERROR.value for c in cases),
        }
    result = {"schema_version": "1.0", "protocol": PROTOCOL,
              "catalogue_sha256": hashlib.sha256(CATALOGUE.read_bytes()).hexdigest(),
              "solver_version": solver.SOLVER_VERSION, "benchmark_passed": benchmark["passed"],
              "baseline_selection_order": "before post-change solver results",
              "metric": "A surviving member below the design target after the edit and absent from the system's failure report.",
              "scope_note": "One ten-bar family; scope-selection omissions, not a claim about general LLM reliability or unsafe releases.",
              "summary": summary, "scenarios": cases}
    if write:
        _save(cache / "results.json", result)
        _save(cache / "benchmark.json", benchmark)
    return result


def cached_results() -> dict:
    result = json.loads((CACHE / "results.json").read_text(encoding="utf-8"))
    if result["catalogue_sha256"] != hashlib.sha256(CATALOGUE.read_bytes()).hexdigest():
        raise ValueError("eval cache is stale; run python -m earl eval")
    return result
