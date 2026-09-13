# `earl.analysis` — Stage 2, the fast gate (Track B)

`DependencyGraph` in, validated `Decision` out. PyNite computes the forces,
Biject turns them into a verdict, and nothing an LLM says can move a member
from FAIL to PASS. This package is the "LLM proposes, code disposes" half of
`Plan/plan.md` made structural.

```
DependencyGraph ──> gate.run_fast_gate ──> Decision
                       │
                       ├─ orchestrator   plan: which load case leads, add self-weight? (advisory)
                       ├─ solver         PyNite adapter: forces, displacements, reactions (SI, tension +)
                       ├─ sanity         equilibrium + linearity on the planned case
                       └─ biject         capacity, safety factor, PASS/FAIL, ESCALATED/APPROVED
benchmark   10-bar truss: solver validation + the demo structure
scoreboard  dropped-domino metric (Sprint 5B) and the JSON interchange for Track A's harness
visualize   standalone SVG of graph + decision
```

Import order is leaf-first and fixed (R16): `solver → sanity → biject →
orchestrator → gate → benchmark → scoreboard → visualize`. Submodules import
each other by module (`from .solver import solve`), never through the
package, and **`earl.analysis` never imports `earl.artifacts`** (Stage 3
imports Stage 2, not the other way round).

## Conventions (binding for everything Track B emits)

| Rule | Where it lives |
|---|---|
| **SI only.** m, N, Pa, m², kg/m³. Every entry point calls `graph.units.assert_si()` and `graph.validate()`. The benchmark's imperial numbers are converted **once**, in `benchmark.py`. | `solver.solve`, `biject.evaluate`, `gate`, `scoreboard.ground_truth` |
| **Axial force sign: tension positive, compression negative.** PyNite 3.2's `Member3D.axial()` is compression-positive; the adapter negates it once. `stress = F / A`, same sign. | `solver.solve` |
| **`MemberResult.capacity` is a STRESS in Pa** (R6): `Fy` when yield governs, `Pcr / A` when Euler buckling governs, so `capacity / |stress_after| == safety_factor`. The force-valued `Capacity` dataclass is internal to `biject.py`. | `biject.evaluate_member` |
| **Safety factor** `= capacity_force / |F|`, capped at `MAX_SAFETY_FACTOR = 1e6` (JSON has no Infinity). A zero-force member (`|F| <= max(1e-9 N, 1e-9 · max|F|)`) gets the cap, `utilization = 0.0` and the note `zero-force member; safety factor capped`. | `biject` |
| **Threshold floor is code, not config.** `MIN_ALLOWED_THRESHOLD = 1.0`; Biject and the gate raise `ValueError` for anything below it or non-finite. `SAFETY_FACTOR_THRESHOLD` in `.env` can only raise the bar. | `biject._check_threshold`, `gate.resolve_threshold` |
| **Every member is evaluated, every load case is evaluated.** `focus_member_ids` in a plan is advisory; with `evaluate_all_load_cases=True` (default) Biject takes each member's **worst** SF over all load cases and writes `governing load case '<lc>'` into its note. | `biject.evaluate`, `gate` |
| **Self-weight is additive only** (R8). `solve(..., include_self_weight=)` can switch it on for a case whose contract flag is off, never off. The gate always solves the planned case with the contract flag; a plan asking for self-weight adds a second variant and Biject takes the worst. Self-weight is lumped to the nodes (`ρ A L g / 2` per end). | `solver.effective_self_weight`, `gate._solve_after` |
| **A FAIL outside `affected_member_ids` is surfaced**, never hidden: note `unsafe but not in graph.affected_member_ids -- the graph walker may have missed a dependency`. That is the dropped domino the project exists to catch. | `biject` |
| **Crashes are ERROR decisions, never approvals or findings** (R10). `SolverError`/`ValueError` from validation, planning or solving become `Outcome.ERROR`, `error_message = "<Type>: <msg>"` (never empty), every member `NOT_EVALUATED` with `safety_factor None`, `violating_member_ids []`. Returned, not raised. | `gate.error_decision` |
| **The before-state is isolated** (R11). `before=` is solved per load case in its own try/except; failures set `stress_before None` and add `before-state not analysed for '<lc>': <reason>` to `sanity_checks.note`. | `gate._solve_before` |
| Every returned `Decision` has passed `decision.validate()`. Timestamps are ISO-8601 UTC. `solver_version = importlib.metadata.version("PyNiteFEA")`. | `gate` |

## Modules

### `solver.py` — PyNite adapter
`solve(graph, load_case_id, *, load_scale=1.0, include_self_weight=None) -> SolveResult`
(`member_forces`, `displacements`, `reactions`, all cast to `float`, tension
positive). `build_model()` is pure construction. Truss modelling: bending
released at both ends of every member, rotations restrained at every node,
the constant coordinate axis (planar rule, R5 — one axis, z then y then x)
restrained at every node, then the contract support (`PIN` = DX DY DZ,
`ROLLER_X` = DY DZ, `ROLLER_Y` = DX DZ, `FIXED` = all). `ROLLER_*` semantics
assume gravity along −Y. Zero `iy/iz/j` are floored at
`SECTION_PROPERTY_FLOOR = 1e-12` (bending is released, so axial results are
unaffected). `load_scale` is the load-combination factor (R3), so the
linearity check deviates by exactly 0.0. `check_stability(graph)` rank-tests
the translational truss stiffness with numpy **before** PyNite runs (R4) and
raises `SolverError("structure is unstable: ...")`; PyNite failures are
wrapped the same way, with "unstable" in the message when applicable.

### `sanity.py` — free correctness tests
`equilibrium_check(graph, result)` re-derives ΣF = 0 at every node from the
graph geometry, member forces, applied loads, reactions and lumped self-weight
(tolerance `1e-6` × largest applied load, floor `1e-6` N).
`linearity_check(graph, lc)` solves at scale 1 and 2 and demands every stress
double (`1e-9` relative). `run_sanity_checks()` packages both as the
contract's `SanityChecks`; a check that cannot run is `None` with the reason in
`note`, never a pass.

### `biject.py` — the verification layer
Pure code, no model input. `member_capacity()` (tension `Fy·A`; compression
`min(Fy·A, π²EI_min/(KL)²)` when an inertia is provided, else `Fy·A` with
the note `inertia not provided; buckling not checked`), `evaluate_member()`,
`evaluate(graph, results, threshold, *, before=None) -> Verdict`
(`outcome`, `member_results` in graph order, sorted `violating_member_ids`,
`threshold`, `governing_load_case_ids`). `before` is `dict[lc_id, SolveResult]`.
There is **no parameter that can force APPROVED**.

### `orchestrator.py` — the planner
`AnalysisPlan(load_case_id, include_self_weight, focus_member_ids, rationale,
source)` — deliberately **no** threshold, capacity, status or outcome field.
`RuleBasedPlanner` (deterministic reference), `LLMPlanner(client)` over any
`LLMClient` with code-side validation (unknown load case / bad JSON /
transport failure → fallback plan with `LLM plan rejected (<reason>); ...`
in the rationale), `MetaModelClient.from_env()` (OpenAI-compatible chat
completions: `META_MUSE_KEY`, `LLM_BASE_URL`, `LLM_MODEL`), `default_planner()`.
The prompt shows only the structural outline — no forces, stresses,
capacities or thresholds.

### `gate.py` — Stage 2 entry point
`run_fast_gate(graph, *, threshold=None, planner=None, before=None,
evaluate_all_load_cases=True, decision_id=None) -> Decision`. Order:
threshold (raises on misconfiguration) → SI + validate → plan → solve planned
case (+ self-weight variant if the plan adds it, + every other load case) →
before graph per load case (isolated) → sanity checks on the planned case →
Biject → Decision → `validate()`. `planner=None` means `RuleBasedPlanner`;
pass `default_planner()` explicitly for the LLM path. A planner that names an
unknown load case or crashes is replaced by the rule plan with a note. `run_gate()`
returns the `GateRun` (plan, raw `SolveResult`s, verdict) for scripts.
`Decision.load_case_id` is the planned case; `sanity_checks` are for that case.

### `benchmark.py` — 10-bar truss validation and demo
See below.

### `scoreboard.py` — dropped dominoes
See "Scoreboard interchange" below.

### `visualize.py` — SVG
`render_truss_svg(graph, decision=None, *, width=900, height=520, title=None)
-> str`, `save_truss_svg(graph, decision, path) -> Path`. Members coloured by
status (FAIL `#d33`, PASS `#2a9d4a`, not evaluated `#888`), affected members
thicker, the change target dashed, labels `m5 SF=0.81`, supports, load arrows
for the decision's load case, legend and an outcome banner
(`ESCALATED — 1 member below threshold 1`). Stdlib only; text is escaped.

## The benchmark (`benchmark.py`)

The 10-bar planar truss is the standard test problem of the structural
optimization literature:

* R. T. Haftka and Z. Gürdal, *Elements of Structural Optimization*, 3rd ed.,
  Kluwer, 1992 — the ten-bar truss example (E = 10⁴ ksi, ρ = 0.1 lb/in³,
  P = 100 kips at nodes 2 and 4, σ_allow = 25 ksi, δ_max = 2 in).
* S. D. Rajan, "Sizing, shape, and topology design optimization of trusses
  using genetic algorithm", *J. Struct. Eng.* 121(10), 1995 — the same problem
  and its Case 1 optimum (weight ≈ 5060.85 lb).

Geometry: bays 360 in; nodes n1 (720,360), n2 (720,0), n3 (360,360),
n4 (360,0), n5 (0,360) PIN, n6 (0,0) PIN; members m1 n5–n3, m2 n3–n1,
m3 n6–n4, m4 n4–n2, m5 n3–n4, m6 n1–n2, m7 n5–n4, m8 n6–n3, m9 n3–n2,
m10 n4–n1. Converted to SI once here (`IN = 0.0254`, `KIP = 4448.2216`,
`KSI = 6.894757e6`, `LB_PER_IN3 = 27679.9`). Material "Aluminium 2024-T3",
E = 6.894757e10 Pa, **yield 50 ksi = 3.447379e8 Pa** (nominal 2024-T3 yield,
used as capacity; the benchmark's 25 ksi is an *allowable* used only for the
published-optimum checks), density 2767.99 kg/m³. Every member has its own
`Section` (`sec_<mid>`, `iy = iz = j = 0`), so Biject notes
`inertia not provided; buckling not checked` on every member — the honest,
illustrative check `plan.md`'s scope note allows.

`validate_solver()` checks (`python3 scripts/validate_solver.py`, PyNite 3.2.0):

| check | expected | actual | tol |
|---|---:|---:|---:|
| optimum weight (lb) — geometry, numbering, density | 5060.85 | 5060.93 | 1e-3 |
| optimum \|stress\| m5 (ksi) — active stress constraint | 25.0 | 25.0027 | 2e-3 |
| optimum n1 drop (in) — active displacement constraint | 2.0 | 2.0000 | 2e-3 |
| optimum feasibility: all \|σ\| ≤ 25 ksi, all \|δ\| ≤ 2 in | — | pass | 2e-3 |
| equal-area equilibrium residual (N) | 0 | 0.0 | 1e-6 |
| equal-area linearity deviation | 0 | 0.0 | 1e-9 |

`EQUAL_AREA_MEMBER_FORCES_LB` is a **regression pin** of our own PyNite output
at all-1.0 in² (m1 +195364.99 … m10 −56744.80 lbf), not a published value.

### The demo change (R1)

`demo_change_graph()`: the optimum with **m7 (n5–n4) thinned from 7.457 to
3.500 in²** (`ChangeEvent chg-demo-m7`, `FEATURE_EDIT`, target `m7`),
edges TOPOLOGY m7→{m1, m8, m3, m4, m5, m10}, LOAD_PATH m7→{m5, m2},
`affected_member_ids = [m7, m1, m8, m3, m4, m5, m10, m2]`,
`affected_node_ids = [n4, n5]`. `demo_before_graph()` is the unchanged
optimum with the same ids. Verified numbers (threshold 1.0):

* **m5: stress 427.8 MPa (≈ 62.05 ksi), SF 0.806 → FAIL**; stress_before 172.4 MPa (25.003 ksi)
* m7 (the edited member): 258.4 MPa, SF 1.334 → PASS
* every other member SF ≥ 3.365 (m10); the optimum itself is APPROVED with m5 SF 2.0
* `violating_member_ids == ["m5"]`, `ground_truth(...) == ["m5"]`, outcome ESCALATED

The edited member passes and its **neighbour** fails — a real downstream
domino, which is the demo narrative.

## Scoreboard interchange (for Track A's eval harness, Sprint 5A)

`scoreboard.py` scores any agent from plain JSON. Truth for a scenario graph
is `ground_truth(graph, threshold=1.0)` (validated solver + Biject over every
load case). One record per (scenario, agent):

```json
[
  {
    "scenario_id": "s07-thin-m7",
    "agent": "system",
    "truth_unsafe_member_ids": ["m5"],
    "reported_unsafe_member_ids": ["m5"],
    "truth_outcome": "escalated",
    "reported_outcome": "escalated",
    "note": null
  },
  {
    "scenario_id": "s07-thin-m7",
    "agent": "baseline",
    "truth_unsafe_member_ids": ["m5"],
    "reported_unsafe_member_ids": ["m7"],
    "truth_outcome": "escalated",
    "reported_outcome": "escalated",
    "note": "LLM agent flagged the edited member only"
  }
]
```

```python
from earl.analysis import ScenarioRecord, Scoreboard, ground_truth, run_fast_gate, score
from earl.analysis.scoreboard import load_records, record_from_decision, save_records

truth = ground_truth(graph, threshold=1.0)
records = [record_from_decision("s07-thin-m7", "system", truth, run_fast_gate(graph, threshold=1.0))]
records.append(ScenarioRecord("s07-thin-m7", "baseline", truth, ["m7"], "escalated", "escalated"))
save_records(records, "eval/records.json")
board = score(load_records("eval/records.json"))
print(board.to_markdown()); board.score_for("baseline").dropped_dominoes  # -> 1
```

Per record: `caught = truth ∩ reported`, **dropped domino = truth − reported**,
false alarm = reported − truth. An APPROVED or ERROR answer with no members
listed drops every truth-unsafe member — silence counts. `AgentScore` carries
`scenarios, unsafe_members_total, caught, dropped_dominoes, false_alarms,
dropped_by_scenario` plus `recall`/`precision`; `Scoreboard.scores` is a
**list** (R12) with `score_for(agent)` and `agents`; `to_markdown()`,
`to_json()/from_json()`.

## Scripts

| script | what | exit |
|---|---|---|
| `python3 scripts/validate_solver.py` | prints the benchmark table and the regression pin | 0 pass / 1 fail |
| `python3 scripts/run_fast_gate.py --no-llm --out decision.json --svg truss.svg` | runs the gate on the m7 demo change with the optimum as before-state; `--graph g.json [--before b.json]` for any graph, `--threshold`, `--planned-only`, `--decision-id`; without `--no-llm` the LLM planner is used when `META_MUSE_KEY` is set | 0 APPROVED / 2 ESCALATED / 1 ERROR or bad threshold |
| `python3 scripts/skyciv_smoke.py` | LIVE Stage 3 smoke test (see `earl/artifacts/README.md`) | 0 / 2 no creds / 1 |

All offline; the gate script only reaches the network for the LLM plan, never
for the verdict. Tests: `python3 -m unittest discover -s tests -v` — every
Track B test passes `threshold=1.0` explicitly and never reads `.env`.

## Proposed contract amendments for Track A (R18 — proposals only, NOT implemented)

1. **A before-state slot.** Either `DependencyGraph.before: DependencyGraph | None`
   or a documented two-graph hand-off, so `stress_before` does not depend on the
   caller remembering to pass `before=` to `run_fast_gate`. Today the gate
   accepts a second graph and matches load cases by id (R11).
2. **Document on `decision.py`:** axial force sign is tension positive
   (`MemberResult.axial_force_after`), and `MemberResult.capacity` is a
   **stress in Pa** (R6), so `capacity / |stress_after| == safety_factor`.
3. **Dict-typed fields in `Serializable._decode`.** `dict[str, Dataclass]`
   does not survive the JSON round-trip (values stay dicts), which is why
   `Scoreboard.scores` is a list. Supporting dict values would let
   `AgentScore.dropped_by_scenario`-style fields hold dataclasses too.
4. *(optional)* **`Decision.analysis_plan`** — the plan (load case, self-weight,
   focus, rationale, source) is currently only reachable through
   `gate.run_gate()`; a small additive field would let the ECN say which case
   led and whether an LLM proposed it. Planner fallbacks currently land in
   `SanityChecks.note`, the only free-text slot.
