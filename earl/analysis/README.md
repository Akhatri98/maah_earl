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
| **`MemberResult.capacity` is a STRESS in Pa** (R6): the design stress `Fd = Material.design_stress` (the declared `allowable_stress` when set, else `yield_strength` — Sprint 4 agreement with Track A, contract 0.2.0) when that governs, `Pcr / A` when Euler buckling governs, so `capacity / |stress_after| == safety_factor`. The force-valued `Capacity` dataclass is internal to `biject.py`. | `biject.member_capacity`, `biject.evaluate_member` |
| **Safety factor** `= capacity_force / |F|`, capped at `MAX_SAFETY_FACTOR = 1e6` (JSON has no Infinity). A zero-force member (`|F| <= max(1e-9 N, 1e-9 · max|F|)`) gets the cap, `utilization = 0.0` and the note `zero-force member; safety factor capped`. | `biject` |
| **Threshold floor is code, not config.** `MIN_ALLOWED_THRESHOLD = 1.0`; Biject and the gate raise `ValueError` for anything below it or non-finite. `SAFETY_FACTOR_THRESHOLD` in `.env` can only raise the bar. | `biject._check_threshold`, `gate.resolve_threshold` |
| **Every member is evaluated, every load case is evaluated.** `focus_member_ids` in a plan is advisory; with `evaluate_all_load_cases=True` (default) Biject takes each member's **worst** SF over all load-case *variants* and writes the governing variant into its note: `governing load case 'lc_x'` for the contract run, `governing load case 'lc_x' (plan-added self-weight variant)` for the run a plan added (`biject.governing_note`). | `biject.evaluate`, `gate` |
| **Self-weight is additive only** (R8). `solve(..., include_self_weight=)` can switch it on for a case whose contract flag is off, never off. The gate always solves the planned case with the contract flag; a plan asking for self-weight adds a second variant and Biject takes the worst. Self-weight is lumped to the nodes (`ρ A L g / 2` per end). | `solver.effective_self_weight`, `gate._solve_after` |
| **Variant keys** are the shared currency between Biject and the gate. `biject.variant_key(graph, result)` is the bare load case id when the result's `include_self_weight` equals the load case's contract flag, else `"<lc_id>+self_weight"` (`SELF_WEIGHT_VARIANT_SUFFIX`; the flag-on / weight-off case of a density-less material is still the contract run and keeps the bare id). `Verdict.governing_load_case_ids`, the `before=` dict and the gate's before-state notes are all keyed this way, so a plan-added variant never borrows the contract variant's `stress_before`. `is_self_weight_variant(key)` and `governing_note(key)` are the helpers. | `biject.variant_key`, `gate._solve_before` |
| **Bad numerics are rejected, not solved.** `solver.check_finite(graph)` raises `SolverError` naming entity and field for a non-finite node coordinate, section `area` non-finite or ≤ 0, `iy/iz/j` non-finite or < 0, `elastic_modulus`/`yield_strength` non-finite or ≤ 0, `density` non-finite or < 0, or any non-finite point-load component in **any** load case (a NaN would otherwise solve to NaN forces that a capped safety factor reads as "safe"). Biject independently requires finite, positive `area`, `yield_strength` and `elastic_modulus` (`biject._require_positive_finite`) and raises `ValueError` if a safety factor is ever non-finite — `NaN < threshold` can never read as PASS. | `solver.check_finite`, `biject.member_capacity`, `biject.evaluate_member` |
| **Duplicate load-case ids are an error.** The contract does not check this. `gate.check_unique_load_case_ids(graph)` raises `ValueError("duplicate load case ids [...]")` right after `graph.validate()` (→ ERROR decision); `solver._load_case` and `sanity._load_case` raise on a duplicate too, so no entry point silently analyses the first case and drops the rest. On a `before=` graph the same check is only a note (R11). | `gate`, `solver`, `sanity` |
| **A FAIL outside `affected_member_ids` is surfaced**, never hidden: note `unsafe but not in graph.affected_member_ids -- the graph walker may have missed a dependency`. That is the dropped domino the project exists to catch. | `biject` |
| **Crashes are ERROR decisions, never approvals or findings** (R10). `SolverError`/`ValueError` from validation, planning or solving become `Outcome.ERROR`, `error_message = "<Type>: <msg>"` (never empty), every member `NOT_EVALUATED` with `safety_factor None`, `violating_member_ids []`. Returned, not raised. | `gate.error_decision` |
| **A failed sanity check is an ERROR decision too — with the numbers kept.** When `equilibrium_ok` or `linearity_ok` on the planned case comes back `False`, `gate.sanity_failure(checks)` builds `error_message = "sanity check failed: equilibrium residual 1.230e+02 N exceeds tolerance; linearity deviation ..."` and the outcome becomes `Outcome.ERROR`, but Biject's `member_results`, `violating_member_ids` and the `sanity_checks` stay on the decision so a reviewer can see what the flagged solve said. A check that could not run (`None`) is only a note. **Consumers: `ERROR` no longer implies every member is `NOT_EVALUATED`** — test for the `sanity check failed:` prefix. | `gate.sanity_failure`, `gate.run_fast_gate` |
| **The before-state is isolated** (R11). `before=` is solved once per **after variant** (same load case id, `include_self_weight=True` when the after variant had it, else `None`), each in its own try/except; failures set `stress_before None` and add `before-state not analysed for '<variant key>': <reason>` to `sanity_checks.note`. A before graph whose material has no density honestly yields a weightless result under the `+self_weight` key. | `gate._solve_before` |
| Every returned `Decision` has passed `decision.validate()`. Timestamps are ISO-8601 UTC. `solver_version = importlib.metadata.version("PyNiteFEA")`. | `gate` |

## Modules

### `solver.py` — PyNite adapter
`solve(graph, load_case_id, *, load_scale=1.0, include_self_weight=None) -> SolveResult`
(`member_forces`, `displacements`, `reactions`, all cast to `float`, tension
positive). Order inside `solve()`: `assert_si` → `graph.validate()` →
`check_finite` → empty-structure / finite `load_scale` → `check_stability` →
`check_planar_loads` → `build_model` → PyNite. `build_model()` is pure
construction and runs none of the checks. Truss modelling: bending
released at both ends of every member, rotations restrained at every node,
the constant coordinate axis (planar rule, R5 — one axis, z then y then x)
restrained at every node, then the contract support (`PIN` = DX DY DZ,
`ROLLER_X` = DY DZ, `ROLLER_Y` = DX DZ, `FIXED` = all). `ROLLER_*` semantics
assume gravity along −Y. Zero `iy/iz/j` are floored at
`SECTION_PROPERTY_FLOOR = 1e-12` (bending is released, so axial results are
unaffected). `load_scale` is the load-combination factor (R3), so the
linearity check deviates by exactly 0.0.

*Planar detection* (`planar_axis`) is relative: an axis is constant when its
spread is ≤ `PLANAR_TOL = 1e-9` × the largest spread over all three axes
(floor 1.0 m), so 1e-17 coordinate noise on a 10 m truss and a 1 km truss
both classify correctly; `restrained_dofs`, `check_stability`,
`check_planar_loads` and `build_model` all go through the same predicate.

*Out-of-plane loads are unstable, not absorbed.* The planar-axis restraint is
a modelling device; at a node whose **contract** support does not restrain
that axis, a load along it would be taken by a fictitious reaction, every
member would read zero and the structure would be approved. So
`check_planar_loads(graph, load_case_id, include_self_weight=None)` raises
`SolverError("structure is unstable: load '<id>' acts along <axis> (f<axis>=<v>), the out-of-plane axis of this planar truss, at unrestrained node '<node>'")`
for any point-load component along the planar axis at such a node, and,
when the planar axis is **y** and self-weight is effective, for the lumped
weight (> 0) of any member whose end node is not restrained in DY
(`... self-weight of member '<m>' ...`). Loads at `PIN`/`FIXED` (or a roller
that restrains that axis) solve as before and appear as reactions.

`check_stability(graph)` rank-tests the translational truss stiffness with
numpy **before** PyNite runs (R4) and raises
`SolverError("structure is unstable: ...")`; PyNite failures are wrapped the
same way, with "unstable" in the message when applicable. A duplicated
`load_case_id` is `SolverError("duplicate load case id 'lc_x' on graph 'g' (2 load cases carry it)")`
from both `solve()` and `build_model()`.

### `sanity.py` — free correctness tests
`equilibrium_check(graph, result)` re-derives ΣF = 0 at every node from the
graph geometry, member forces, applied loads, reactions and lumped self-weight
(tolerance `1e-6` × largest applied load, floor `1e-6` N); it raises
`ValueError` for an unknown **or duplicated** `result.load_case_id` rather
than balancing against the first case that matches.
`linearity_check(graph, lc)` solves at scale 1 and 2 and demands every stress
double (`1e-9` relative). `run_sanity_checks()` packages both as the
contract's `SanityChecks`; a check that cannot run is `None` with the reason in
`note`, never a pass.

### `biject.py` — the verification layer
Pure code, no model input. `member_capacity()` (tension `Fy·A`; compression
`min(Fy·A, π²EI_min/(KL)²)` over the inertias that are **provided** (> 0):
none → `Fy·A` with `INERTIA_NOT_PROVIDED_NOTE = "inertia not provided; buckling not checked"`;
exactly one → buckling checked about that axis only, with
`ONE_INERTIA_NOTE = "only one inertia provided; buckling checked about that axis only (upper bound on Pcr)"`
prepended to the capacity note; both → `I_min = min(iy, iz)`).
`area`, `yield_strength` and `elastic_modulus` must be finite and positive
(`ValueError` otherwise, on the yield-only path too). `evaluate_member()`,
`evaluate(graph, results, threshold, *, before=None) -> Verdict`
(`outcome`, `member_results` in graph order, sorted `violating_member_ids`,
`threshold`, `governing_load_case_ids`). `before` is `dict[variant_key, SolveResult]`
(see the variant-key convention above): a member's `stress_before` comes
from the before result of **its governing variant**, else `None` with the
note `no before-state for governing case '<key>'`. A result whose
`load_case_id` is not in `graph.load_cases` is a `ValueError`. Public
helpers: `variant_key`, `is_self_weight_variant`, `governing_note`,
`SELF_WEIGHT_VARIANT_SUFFIX = "+self_weight"`,
`SELF_WEIGHT_VARIANT_NOTE = "plan-added self-weight variant"`.
There is **no parameter that can force APPROVED**.

### `orchestrator.py` — the planner
`AnalysisPlan(load_case_id, include_self_weight, focus_member_ids, rationale,
source)` — deliberately **no** threshold, capacity, status or outcome field.
`RuleBasedPlanner` (deterministic reference), `LLMPlanner(client)` over any
`LLMClient` with code-side validation (unknown load case / bad JSON /
transport failure → fallback plan with `LLM plan rejected (<reason>); ...`
in the rationale), `MetaModelClient.from_env()` (OpenAI-compatible chat
completions: `META_MUSE_KEY`, `LLM_BASE_URL`, `LLM_MODEL`; the `api_key`
field is `repr=False` so a logged client never prints it), `default_planner()`.
The prompt shows only the structural outline — no forces, stresses,
capacities or thresholds. `parse_plan_json` rejects a reply longer than
`MAX_LLM_RESPONSE_BYTES = 65536` UTF-8 bytes **before** parsing
(`response too long (N bytes > 65536 max)` → fallback plan) and strips code
fences with linear string handling, so a hostile reply cannot stall the gate.

### `gate.py` — Stage 2 entry point
`run_fast_gate(graph, *, threshold=None, planner=None, before=None,
evaluate_all_load_cases=True, decision_id=None) -> Decision`. Order:
threshold (raises on misconfiguration) → SI + validate →
`check_unique_load_case_ids` → plan → solve planned case (+ self-weight
variant if the plan adds it, + every other load case) → before graph per
**variant key** (isolated) → sanity checks on the planned case → Biject →
`sanity_failure` (→ `Outcome.ERROR` with the numbers kept, see conventions)
→ Decision → `validate()`. `planner=None` means `RuleBasedPlanner`;
pass `default_planner()` explicitly for the LLM path. A planner that names an
unknown load case or crashes is replaced by the rule plan with a note. `run_gate()`
returns the `GateRun` (plan, raw `SolveResult`s, `before_results` keyed by
variant key, verdict) for scripts.
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
(`ESCALATED — 1 member below threshold 1`). Labels sit `LABEL_OFFSET = 10` px
off their member; members whose canvas midpoints coincide (within
`LABEL_COINCIDENT_PX = 1` px — the crossing diagonals m7/m8 and m9/m10) get
their labels at 30 % / 70 % along their own member instead of the shared
midpoint (`_label_positions`), so no label is drawn on top of another.
Stdlib only; text is escaped.

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
E = 6.894757e10 Pa, **yield 50 ksi = 3.447379e8 Pa** (nominal 2024-T3 yield;
what SkyCiv's design check uses) and **allowable 25 ksi = 1.723689e8 Pa** (the
benchmark's published allowable; what Biject computes capacity from — Sprint 4
agreement, contract 0.2.0), density 2767.99 kg/m³. Every member has its own
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

With capacity at the 25 ksi allowable the published optimum sits *exactly* on
the threshold (m5 at 25.003 ksi → SF 0.9999), which is correct for a
stress-constrained optimum and useless as a safe before-state. The **demo
design** is therefore the optimum with a uniform 10 % margin,
`DEMO_AREAS_IN2 = optimum × 1.10` (m5 SF 1.10, every other member higher).
Solver validation still uses the exact optimum.

`demo_change_graph()`: the demo design with **m7 (n5–n4) thinned from 8.203 to
6.000 in²** (`ChangeEvent chg-demo-m7`, `FEATURE_EDIT`, target `m7`). Edges
are the full node-sharing TOPOLOGY set (`topology_edges()`, the same set Track
A's `graph_builder.topology_edges()` derives from the Onshape mate connectors)
plus LOAD_PATH m7→{m5, m2}; `affected_member_ids` is every member, as the
fixed BFS from m7 reaches: 1 hop {m1, m3, m4, m5, m10} (+ m2 via load path),
2 hops {m6, m8, m9}. `demo_before_graph()` is the unchanged demo design with
the same ids. Verified numbers (threshold 1.0):

* **m5: stress 234.6 MPa (≈ 34.03 ksi), SF 0.735 → FAIL**; stress_before 156.7 MPa (22.73 ksi)
* m7 (the edited member): 155.7 MPa (22.58 ksi), SF 1.107 → PASS
* every other member SF ≥ 3.26 (m3); the demo design itself is APPROVED with m5 SF 1.10
* `violating_member_ids == ["m5"]`, `ground_truth(...) == ["m5"]`, outcome ESCALATED

The edited member passes and its **neighbour** fails — a real downstream
domino, which is the demo narrative. The same change through Track A's Sprint
3A mock (`mock_decisions.escalated`, optimum without the margin, m7 7.46 →
6.00) gives m5 SF 0.751 / m7 SF 1.101, and `run_fast_gate()` reproduces those
literals (`tests/test_pipeline.py`).

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
| `python3 scripts/run_fast_gate.py --no-llm --out decision.json --svg truss.svg` | runs the gate on the m7 demo change with the optimum as before-state; `--graph g.json [--before b.json]` for any graph (decoded only — a graph that fails `validate()` still yields an ERROR decision in `--out`; a malformed `--before` is a note), `--threshold`, `--planned-only`, `--decision-id`; without `--no-llm` the LLM planner is used when `META_MUSE_KEY` is set. If the graph is too malformed to draw, `svg not written: ...` goes to stderr and the JSON + exit code are unaffected | 0 APPROVED / 2 ESCALATED / 1 ERROR or bad threshold |
| `python3 scripts/skyciv_smoke.py` | LIVE Stage 3 smoke test (see `earl/artifacts/README.md`) | 0 / 2 no creds / 1 |

All offline; the gate script only reaches the network for the LLM plan, never
for the verdict. Tests: `python3 -m unittest discover -s tests -v` — every
Track B test passes `threshold=1.0` explicitly and never reads `.env`.

## Integration notes from Track A's graph builder

Verified by a probe that fed `earl.ingestion.benchmark.benchmark_spec()`'s
graph straight into `run_fast_gate` (no code change needed on the gate side):

* **Same material id, different yield — RESOLVED in Sprint 4.** Track A's
  `benchmark_spec()` used to set `mat_al.yield_strength` to the **25 ksi
  allowable** while Track B's `benchmark.py` used the **50 ksi nominal 2024-T3
  yield**, so safety factors differed by exactly 2× between builders. Contract
  0.2.0 carries both (`yield_strength` = 50 ksi, `allowable_stress` = 25 ksi)
  and Biject computes capacity from `Material.design_stress`; both builders
  now agree (`tests/test_pipeline.py::TestBuildersAgree`).
* **One area for every bar.** Track A drives all ten members from a single
  Onshape variable `barArea` (default 1 in²). At 1 in² and 25 ksi under the
  benchmark's 100-kip loads **every member fails** (m3 at ≈ 205 ksi); a uniform
  area of **≥ 8.19 in²** is needed for the whole truss to pass at 25 ksi.
  `scripts/run_pipeline.py --onshape-fixture` therefore runs that path at
  10-kip loads, where 1 in² is safe and a `barArea` cut to 0.75 in² escalates. Track B's optimum has per-member
  areas (`sec_<mid>`), which is why its demo change fails exactly one member.
* **Load case id is `lc_1`** in Track A (`LOAD_CASE_ID`), `lc_benchmark` in
  Track B's `benchmark.py`; the gate plans over whatever ids the graph carries,
  so neither needs renaming.
* **Track A's walker marks all 10 members affected** for the `barArea`
  edit that is Track A's primary change signal: `walker.walk` reaches every
  member at distance 1 over `VARIABLE_REF` edges (`walk_and_apply` writes
  them into `affected_member_ids`; `build_graph` alone leaves the list
  empty). So the `unsafe but not in graph.affected_member_ids` dropped-domino
  note can only fire on Track A graphs for a narrower change (the selfcheck's
  own change reaches 5 of 10).
* The gate accepted Track A's graph as-is: SI units, unique load-case ids,
  finite numerics and an XY-planar truss with in-plane loads all pass the
  checks above without adjustment.

## Contract amendments (R18)

Done in Sprint 4 (contract 0.2.0): `Material.allowable_stress` (capacity),
`Decision.source`, `MemberResult.hops_from_change` / `reached_via`, and item 2
below (documented on `MemberResult`). `earl.pipeline.enrich_decision()` fills
the new Decision fields from the graph and the walk. Still proposals:

1. **A before-state slot.** Either `DependencyGraph.before: DependencyGraph | None`
   or a documented two-graph hand-off, so `stress_before` does not depend on the
   caller remembering to pass `before=` to `run_fast_gate`. Today the gate
   accepts a second graph and matches load cases by id (R11).
2. ~~**Document on `decision.py`:** axial force sign is tension positive
   (`MemberResult.axial_force_after`), and `MemberResult.capacity` is a
   **stress in Pa** (R6), so `capacity / |stress_after| == safety_factor`.~~ Done.
3. **Dict-typed fields in `Serializable._decode`.** `dict[str, Dataclass]`
   does not survive the JSON round-trip (values stay dicts), which is why
   `Scoreboard.scores` is a list. Supporting dict values would let
   `AgentScore.dropped_by_scenario`-style fields hold dataclasses too.
4. *(optional)* **`Decision.analysis_plan`** — the plan (load case, self-weight,
   focus, rationale, source) is currently only reachable through
   `gate.run_gate()`; a small additive field would let the ECN say which case
   led and whether an LLM proposed it. Planner fallbacks currently land in
   `SanityChecks.note`, the only free-text slot.
