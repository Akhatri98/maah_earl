"""Compare PyNite with the unmodified, checksum-pinned book reference code.

The source file is an explicit local input; this script never downloads or
executes arbitrary URLs. Runtime demo validation uses committed numerical
fixtures and does not need this research source or a network connection.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from earl.analysis.benchmark import validate_benchmark

SOURCE_COMMIT = "df0c0f1bcc8baea1ede63442fa3f32fa4f275e71"
SOURCE_URL = f"https://raw.githubusercontent.com/mdobook/resources/{SOURCE_COMMIT}/exercises/tenbartruss/truss.py"
SOURCE_SHA256 = "8e877b3b4436779d954759df77b2a690634f5c7978891734defa861898be7c39"


def compare(source: Path) -> dict:
    if hashlib.sha256(source.read_bytes()).hexdigest() != SOURCE_SHA256:
        raise ValueError("Reference source checksum differs from the reviewed published implementation")
    spec = importlib.util.spec_from_file_location("published_tenbar_reference", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    captured = []
    linear_solve = np.linalg.solve

    def capture(matrix, rhs):
        displacement = linear_solve(matrix, rhs)
        captured.append(displacement.copy())
        return displacement

    # Observe the returned displacement, without changing the reference solver.
    with patch.object(np.linalg, "solve", side_effect=capture):
        _, stress = module.truss(np.full(10, 10.0))
    if len(captured) != 1 or captured[0].size != 8:
        raise ValueError("Unexpected reference solve shape")
    displacement = captured[0].reshape(4, 2)
    observed = validate_benchmark()
    stress_psi = {f"m{i + 1}": float(value) for i, value in enumerate(stress)}
    displacements_in = {f"n{i + 1}": values.tolist() for i, values in enumerate(displacement)}
    displacements_in.update(n5=[0.0, 0.0], n6=[0.0, 0.0])
    stress_error, displacement_error = 0.0, 0.0
    for mid, expected in stress_psi.items():
        np.testing.assert_allclose(observed["stress_psi"][mid], expected, rtol=1e-9, atol=1e-6)
        stress_error = max(stress_error, abs(observed["stress_psi"][mid] - expected))
    for nid, expected in displacements_in.items():
        np.testing.assert_allclose(observed["displacements_in"][nid], expected, rtol=1e-9, atol=1e-9)
        displacement_error = max(displacement_error, max(abs(a - b) for a, b in zip(observed["displacements_in"][nid], expected)))
    return {"passed": True, "source_url": SOURCE_URL, "source_commit": SOURCE_COMMIT, "source_sha256": SOURCE_SHA256,
            "source_modified": False, "area_in2": 10.0, "elastic_modulus_psi": 1e7,
            "downward_load_each_lbf": 100000.0, "solver_version": observed["solver_version"],
            "stress_psi": stress_psi, "displacements_in": displacements_in,
            "max_stress_difference_psi": stress_error, "max_displacement_difference_in": displacement_error,
            "displacement_capture": "Observed the published np.linalg.solve return value; matrices and loads unchanged.",
            "note": "Numbers from executing published runnable code, not a transcribed textbook results table."}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "out" / "qa" / "published-benchmark.json")
    args = parser.parse_args()
    result = compare(args.source)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
