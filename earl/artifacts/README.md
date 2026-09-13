# `earl.artifacts` — Stage 3, the trusted artifact (Track B)

Runs only when the fast gate **escalated** (or on demand for a full paper
trail). SkyCiv, a commercial structural-analysis platform practising
engineers recognise, does two jobs at once: it produces the report the ECN
cites, and — because it runs its own independent analysis — it cross-checks
the PyNite gate. It never *changes* the verdict: the outcome was computed by
Biject from PyNite numbers, and SkyCiv is evidence attached to it.

```
Decision (ESCALATED) ──> escalation.escalate ──> Decision + skyciv_report + cross_check
                             │
                             ├─ skyciv_client.SkyCivClient.analyze   build_s3d_model, 1-2 POSTs
                             └─ crosscheck.cross_check              |F| PyNite vs |F| SkyCiv
```

Imports: `skyciv_client → crosscheck → escalation`, contracts only;
**`earl.artifacts` never imports `earl.analysis`** and vice versa. Scripts may
import both.

## PAYLOAD SHAPE IS UNVERIFIED — read this first

The SkyCiv docs site was not reachable from the sandbox this was written in.
The request format follows the v3 single-endpoint "functions" API *from
memory*; response parsing is deliberately shape-tolerant and every raw
response is recorded when `record_dir` is set. **Nothing below is confirmed
until `scripts/skyciv_smoke.py` has run once** against the live API. Every
SkyCiv string lives in the constants block at the top of `skyciv_client.py`
so a single smoke run can fix the lot in one place.

Specifically unverified:

| item | current assumption | where |
|---|---|---|
| function names | `S3D.session.start`, `S3D.model.set`, `S3D.model.solve`, `S3D.results.getReport` (fallback `S3D.results.getAnalysisReport`), `S3D.design.member.getInput`, `S3D.design.member.check` | `FN_*` |
| design-check argument name | `design_input` (the getInput data passed back; there is no `$previous` placeholder) | `analyze()` |
| where the session id lives | top-level `session_id` or `last_session_id`, else the session.start entry's data | `_session_id()` |
| where the report link lives | first **http(s)** string whose key contains `download`, then `link`, then `url` — priority order across all keys of a dict before recursing; a hinted key whose value is not a URL (`url_expiry: "24h"`, a relative `file_url`) is skipped | `parse_report_link()`, `_is_http_url()` |
| whether the response echoes function names | assumed `functions: [{function, status, msg, data}]`; falls back to positional matching | `_find_function()` |
| axial force sign | **assumed tension-positive**; `parse_member_results(..., sign_hint=-1.0)` flips it | `parse_member_results()` |
| solve data shape | dict keyed by combination → `member_forces`/`member_stresses` → member → `axial` (number, list, `[position, value]` pairs or dict); a list of combination objects and a single object are also accepted | `_combination_objects()`, `_reduce_axial()` |
| supports for every node | one entry per node, FREE included, to restrain rotations and the out-of-plane translation of a released-bending truss; `auto_stabilize_model: true` might make this unnecessary | `build_s3d_model()` |
| design code string | `AISC_360-16_LRFD` (underscore form) | `DEFAULT_DESIGN_CODE` |
| point-load type | `"N"` (nodal) | `POINT_LOAD_TYPE` |

The cross-check compares **magnitudes**, so a wrong sign assumption cannot
produce a false "agrees".

## Request flow (`SkyCivClient.analyze(graph, load_case_id, *, design_code, want_report)`)

One POST to `https://api.skyciv.com:8085/v3` (port-qualified;
`earl.config.SkyCivConfig`) carries
`{"auth": {username, key [, session_id]}, "options": {"validate_input": true,
"response_data_only": false}, "functions": [...]}`.

**Call 1** (`skyciv_analyze_1.json`):
`session.start {keep_open: bool(design_code)}` → `model.set {s3d_model}` →
`model.solve {analysis_type: "linear", repair_model: true}` →
`results.getReport {file_type: "pdf"}` (if `want_report`) →
`design.member.getInput {design_code}` (if `design_code`).
Only the first three are **fatal**: `status != 0` raises
`SkyCivError(function=<name>, status=<n>)` naming the function. Report and
design failures go to `SkyCivRun.warnings` and leave `report_url None` /
`design_results {}`.

**Call 2** (`skyciv_analyze_2.json`), only when getInput returned data *and* a
session id was found: `design.member.check {design_code, design_input}` with
`auth.session_id`. Without a session id the check is skipped with a warning.

`call()` raises `SkyCivError` for HTTP ≠ 200, a non-JSON body or a transport
failure; per-function status is checked by `analyze()`. `SkyCivError` carries
`status`, `function` and `body` (the full response text for HTTP/non-JSON
failures; the message itself quotes only a prefix). Every call is logged
(`client.log`, `client.request_count`) — SkyCiv calls are metered. With
`record_dir` set, a successful call is recorded as `<label>.json` and a failed
one as `<label>_error.json` (label, functions, error type, message, status,
function, body); a failure to write the record never masks the original
error. `SkyCivClient.config` is `repr=False`, so a logged client never prints
the username or key. `transport` and `downloader` are injectable, so tests
never touch the network.

`SkyCivRun`: `session_id, member_results {id: SkyCivMemberResult(axial_force N,
stress Pa)}, report_url, design_results {id: SkyCivDesignResult(ratio, passed)},
design_code, raw {"call_1", "call_2"}, api_calls, warnings`.

## Unit mapping (`build_s3d_model`)

The graph is SI; SkyCiv is sent metric with its own unit block
(`SKYCIV_UNITS`). Result values are converted back the same way.

| quantity | contract (SI) | SkyCiv | factor |
|---|---|---|---|
| node coordinates | m | m | 1 |
| section area | m² | mm² | 1e6 |
| Iy, Iz, J | m⁴ | mm⁴ | 1e12 |
| E, yield, ultimate (= 1.3 × yield) | Pa | MPa | 1e-6 |
| density | kg/m³ | kg/m³ | 1 |
| point loads | N | kN | 1e-3 |
| axial force (result) | N | kN | ×1e3 |
| stress (result) | Pa | MPa | ×1e6 |

Model mapping: ids are 1-based integers (`S3DModel.node_index` /
`member_index` map back to contract ids); members `normal_continuous` with
fixity `FFFFRR` at both ends (truss); one SkyCiv section per distinct
(section, material) pair used by a member; `vertical_axis: "Y"`,
`auto_stabilize_model: true`; restraint codes (Tx Ty Tz Rx Ry Rz, F fixed /
R released) FREE `RRRFFF`, PIN/FIXED `FFFFFF`, ROLLER_X `RFFFFF`, ROLLER_Y
`FRFFFF`, with the planar rule (R5) forcing the constant-coordinate
translation to F (a planar-z truss's free node is `RRFFFF`); point loads in
group `LG1`, distributed `self_weight` in `SW1` only when the load case's
`include_self_weight` is set; one load combination named after the load case.
(PyNite lumps self-weight to the nodes; the 5 % cross-check tolerance absorbs
the ~0.02 % difference.)

## What escalation attaches (`escalation.escalate`)

`escalate(graph, decision, client, *, report_dir=None, always=False,
design_code=DEFAULT_DESIGN_CODE) -> Decision`

* Raises `ValueError("decision '<id>' is for graph '<g1>', not '<g2>'")` when
  `decision.graph_id != graph.id` — checked right after `decision.validate()`,
  **before** the early returns below and before any metered call, because a
  mispaired graph is a caller bug whatever the outcome.
* Returns the (validated) input **untouched** for `Outcome.ERROR` regardless
  of `always`, and for `APPROVED` unless `always=True`. Note that since the
  gate's sanity-failure change an `ERROR` decision can carry Biject numbers;
  escalation still does not analyse it.
* Otherwise works on a copy (`Decision.from_dict(decision.to_dict())`), runs
  `client.analyze(graph, decision.load_case_id)` and attaches
  `SkyCivReport(report_id=f"skyciv-{decision.id}", url, local_path
  (downloaded to `report_dir/<report_id>.pdf` when `report_dir` is given),
  generated_at, design_code)` and `cross_check` on the governing member.
* **Failure is surfaced, not swallowed.** On `SkyCivError`, a network error,
  or a `ValueError`/`OSError` inside the SkyCiv path: outcome **unchanged**
  (never downgraded to APPROVED, never promoted to ERROR),
  `cross_check = CrossCheck(performed=False, member_id=<governing>)`,
  `skyciv_report = None`, and `; SkyCiv unavailable: <Type>: <msg>` is
  **appended** to the governing member's existing note. A report *download*
  failure keeps the URL and appends `SkyCiv report download failed: ...`;
  non-fatal `SkyCivRun.warnings` are appended as `SkyCiv warnings: a | b`.
* The result is `validate()`d before it is returned.

## Cross-check semantics (`crosscheck`)

`cross_check(decision, run, *, member_id=None, tolerance=0.05) -> CrossCheck`
compares `|MemberResult.axial_force_after|` (PyNite, N) with
`|SkyCivMemberResult.axial_force|` (N) for the given member (default: the
decision's governing member) through the contract's own
`CrossCheck.evaluate()`, so the number Track A reads was computed by the
contract. Linear truss analysis is exact, so two independent solvers should
agree to well under 5 %; `agrees=False` is a first-class result to deliver,
never an error. If either side is missing, `performed=False` with
`member_id` filled in so the ECN can say *which* member could not be checked.
`cross_check_table(decision, run)` does the same for every member present on
both sides.

## Running the smoke test

```
python3 scripts/skyciv_smoke.py                 # equal-area 10-bar truss, report + design check
python3 scripts/skyciv_smoke.py --demo          # the m7 demo change instead
python3 scripts/skyciv_smoke.py --no-design     # skip call 2
```

Needs `SKYCIV_API_USERNAME` / `SKYCIV_API_KEY` in the environment or `.env`;
without them it exits 2 **without any network call**. `load_env()` runs
**before** the argument parser is built, and `--report-dir` defaults to
`None` and is resolved to `SKYCIV_REPORT_DIR` after parsing, so a value set
only in `.env` is honoured while an explicit flag still wins. It records the
raw responses (and any `<label>_error.json`) to
`tests/fixtures/skyciv/recorded/`, retries the report with `FN_REPORT_ALT` if
`FN_REPORT` fails (one extra call: `retry_report_alt(..., session_open=)`
reuses call 1's session only when call 1 asked for `keep_open` — i.e. a
design code was requested — *and* a session id came back; otherwise it sends
a fresh `session.start` + `model.set` + `model.solve` + report in one call),
prints the raw shape of the solve data, SkyCiv's axial forces next to
PyNite's (with a verdict on the sign convention), warnings, report link and
design ratios. Costs 1–3 metered calls; never run by the tests. Optional
`SKYCIV_REPORT_DIR` (`.env.example`) is where escalation and the smoke script
save report PDFs.

Fixtures: `tests/fixtures/skyciv/synthetic/` are hand-built to the assumed
shape (each carries a `_comment` saying so); once recordings exist, prefer
them and correct the constants block for anything that differs. JSON member
keys are always strings (`"1"`); `parse_member_results()` also accepts
integer keys from a live dict, which `tests/test_skyciv.py` pins in code
rather than in a fixture.
