# EARL Reliability Record

EARL is CI for parametric CAD. Onshape Simulation already provides
cloud-native, automatically updated assembly FEA, stress, displacement,
factors of safety, and versioned simulation data. SkyCiv already provides
structural analysis and reports. EARL adds an enforced comparison policy,
responsible-human notification, and a per-change decision record. It does not
claim better physics or invent version history.

## Build Provenance

Work began on `astra`, containing the same commit as `dev` (`2cd8fdf`). The
original 50 tests passed before contract changes. Only `astra` is a submission
or deployment branch; `main` and the default branch are not changed.

## Autonomous Operation: Phase 1

`earl/watch/` wraps the unchanged pipeline and code-only Biject gate. It has
no model tool loop. Fixture mode observes a local file's modification time
and content; the synthetic edit script only saves that file, never requests
verification. The browser polls read-only agent state and cannot start a
watcher or enable live calls. The original 119 tests remain unchanged.

The ledger uses a separate process lock, fsync, and atomic rename. Runs,
source cursors, open escalations and notification dedup survive restart.
An identical change fingerprint plus sorted violating IDs suppresses a repeat
notice; a verified approval after escalation records a CLEARED notice.
ERROR never clears an escalation. Reappearance after a cleared notice is a
new incident. Corrupt state fails closed rather than resetting history.
A process lock permits one watcher per ledger; a heartbeat lease lets the
UI recognize a stopped/crashed watcher. Pipeline errors are recorded and the
loop continues. Interrupted work can be evaluated again after restart.

The independently committed Phase 1 disables pipeline live calls and saves an additional
`agent-notice.eml` for a new escalation or recovery. Local notice creation is
not Gmail delivery. Missing live trigger configuration falls back explicitly
to fixture mode. Webhooks, quota enforcement and autonomous live send are
not claimed as verified by that checkpoint.

## Live Trigger Contract: Phase 2

The webhook inbox now exists at `POST /api/webhook/onshape`; it authenticates,
checks document/workspace scope, persists bounded metadata, and responds 200
without solving. `DEMO_MODE=false` and `EARL_ALLOW_LIVE_WEBHOOKS=true` are both
required. Only the separately started trusted watcher spends live API calls.
Public demo mode rejects even a correctly authenticated callback.

The current [Onshape OpenAPI](https://cad.onshape.com/api/openapi) was downloaded
without credentials on 2026-09-13: server `/api/v17`, revision
`1.220.87929-d54ca734df42`, SHA256
`5c072358064bee2f5fc2ea9df131ef43e471e65633d56267a88ab9abb93ac7ee`.
It specifies `onshape.model.lifecycle.changed`, `documentId`, `workspaceId`,
`events`, `url`, `data`, `options.collapseEvents`, and `isTransient`.
The schema's callback entry incorrectly says GET while the current
[developer guide](https://onshape-public.github.io/docs/app-dev/webhook/)
explicitly specifies POST delivery; EARL follows the guide and records this
unverified-live discrepancy. Lifecycle registration/ping need 200, not a
challenge response. There is no documented expiry timestamp: non-transient
registration prevents inactivity cleanup; daily GET plus renewal/re-registration
handles a transient or removed managed hook. Explicit unregister disables renewal.

The echoed `data` token is a shared bearer secret over HTTPS, not cryptographic
payload integrity. Optional company HMAC uses the documented timestamp/raw-body
construction and primary/secondary signature headers, with a five-minute
freshness limit. Secrets and complete callbacks are not stored or exposed.
Message IDs are deduplicated persistently (last 10,000); inbox capacity is 256.
A full/unwritable inbox returns 503 so delivery is not falsely acknowledged.
[Signature source](https://cad.onshape.com/help/Content/Plans/enterprise_settings_webhooks.htm).

Every Onshape client request reserves the local counter before transport,
including failures/redirects, and cannot exceed `ONSHAPE_CALL_BUDGET`.
This is deliberately more conservative than Onshape's billable 2xx/3xx count.
Seed prior account usage with `ONSHAPE_CALLS_USED`; other applications' future
calls are not visible locally. No annual automatic reset is invented because
account allocation renewal dates differ. Exhaustion is shown in agent status.
Poll defaults to six hours (minimum one hour), with pacing persisted across
restart. Webhook snapshot batches are paced to 60 seconds by default. Both
read only `currentmicroversion` first, then cache immutable assembly/variable
snapshots; no full assembly fetch occurs on unchanged ticks.
[Annual quota source](https://onshape-public.github.io/docs/auth/limits/).

Contract 0.3 adds flat typed batches and explicit member restoration so a
collapsed multi-edit event is evaluated atomically, not at imagined intermediate
states. Reconstruction is checked against all observed physical properties.
Names tolerate case/space/occurrence suffixes and optional `area_mN` variables
are declared data. Unknown live instances, unsupported materials/restraints/
policy changes, or an empty live assembly produce ERROR, never partial approval.
See [Onshape setup](docs/ONSHAPE_SETUP.md) for exact CAD requirements.

**No Onshape credentials or populated live document were available.** HTTP
handshake, signature, budget, queue, pinned reads, batched diffs, and renewal
are tested with mocks shaped from current documentation, not real callbacks.
On unavailable live triggers, a separate fixture source remains usable and is
never reported as live CAD evidence.

## Baseline Protocol (Specified Before Evaluation)

1. Freeze twenty scenario definitions before collecting either system's
   results. Both systems receive the same canonical model, typed edit, load
   cases, material/section properties, and numerical verification tool.
2. The baseline must choose which surviving members to verify. It may select
   any subset, including all members, with no artificial budget or cap. It
   receives geometry and the edit, not ground-truth failures or EARL's solved
   results. A language model only selects members, never judges safety.
3. With a configured provider, ask the selector to return member IDs as
   structured data and cache its selection. Without a provider or on any
   failure, use a declared deterministic local-selection policy: for a resize
   or removal, select the changed member and members incident to its two
   endpoints; for a load edit, select members incident to the changed load's
   old/new nodes. A whole-structure variable change selects all members.
   Label these runs **rule-based baseline**, never measured LLM behavior.
4. A statically indeterminate truss cannot be physically solved one bar at a
   time. The tool must assemble/solve the full equilibrium system even for a
   subset request; only selected member stresses, capacity checks, and
   reports are exposed to the baseline. Global sanity checks recover all
   stresses internally in the worker. Selection never deletes unselected
   bars from the structural model. Both systems use the same solver/capacity
   implementation. This comparison measures omissions in verification scope,
   not speed or superior numerical accuracy.
5. Code applies the same design target and hard floor to returned numbers.
   A baseline that selects all members can score zero. There is no prompt
   asking a model whether a structure is safe, and no model-written verdict.
6. Ground truth is an offline full-structure solve with the validated solver,
   frozen to committed JSON. A **dropped domino** is a surviving member that
   fails the design target after the edit and is absent from the system's
   failure report. Report newly failing members (passed before, fail after)
   separately; initial demonstrator cases are expected to pass. Solver errors
   are counted separately and never relabeled as structural failures.
7. Cache scenario inputs, selected IDs, reported failures, per-member ground
   truth, outcomes, and counts for every run. The served scoreboard uses the
   committed cache; regeneration requires no network. This is one fixed
   benchmark family, not a claim of general LLM reliability.

Unselected members are `NOT_EVALUATED`; the baseline is held for incomplete
verification, not silently approved. Dropped dominoes measure missing specific
member findings, **not unsafe merges or released designs**. Incomplete-scope
escalation does not count as reporting every unnamed failing member.

The ten-bar benchmark is statically indeterminate to degree two (10 bars + 4
restrained translations - 2*6 joints = 2). An edit can redistribute forces
through all members. A dependency traversal produces a conservative superset;
the solver decides and the gate enforces. Traversal completeness is not our
experimental claim.

## Explicit Assumptions and Choices

- Geometry/connectivity/restraints are reused from
  `scripts.contract_selfcheck.build_graph()`, not independently reconstructed.
  The benchmark has two pinned wall supports. The viewer must depict actual
  restraints; generic roller drawing support does not change physics.
- `earl/units.py` owns conversions. Original rounded SI constants are retained
  for existing tests: `E=6.895e10 Pa`, `P=444822 N`, one inch `0.0254 m`.
  `E_STEEL` is a legacy alias for the aluminium-like benchmark modulus, not a
  steel certification. The illustrative demo yield limit is `248 MPa`.
- A section declared `solid_round` uses `Iy=Iz=A^2/(4*pi)` and `J=2*Iy`.
  Resizing is geometrically similar, so second moments scale with area squared.
  Area alone cannot identify a real CAD section: the shape is declared
  in `truss_map.json`, not inferred by an LLM.
- Euler compression buckling assumes ideal straight, pin-ended members,
  effective length factor `K=1`, full unbraced member length in both directions,
  and the weaker principal second moment. Compression capacity is the smaller
  of `Fy*A` and `pi^2*E*Imin/(K*L)^2`; tension capacity is `Fy*A` because Euler
  buckling does not apply to tension. Slenderness is `K*L/sqrt(Imin/A)`.
- This is linear elastic, axial-only, small-displacement 2D truss analysis,
  with simplified point loads and no self-weight. No plasticity, imperfections,
  joint strength,
  local buckling, fatigue, dynamics, code load combinations, or AISC/ASCE
  compliance is claimed. Euler/yield minima do not implement an inelastic
  column curve or a building-code design check.
  One selected available load case is checked, not a code-combination envelope.
- The original synthetic parser fixture has only two named bars, a gusset,
  and a spare. A separate full ten-member fixture is explicitly synthetic.
  The demo starts with 20 in^2 solid round sections and two 20 kN downward
  loads, so ordinary controls can exercise both approval and escalation.
  These are demonstrator parameters, distinct from benchmark validation.
- The supplied snapshot is the before-model. Each run applies one proposed
  typed delta to that snapshot; edits do not accumulate between demo runs.
  These manual demo runs remain independent. The autonomous watcher separately
  keeps observed before/after snapshots and has an authenticated webhook inbox;
  it does not reconstruct unobserved historical microversions.
- The evaluated SI Onshape variable table supplies dimensions and section
  area; `safetyFactor` is a design target. `hard_floor` is at least 1.0 and
  cannot be weakened by a payload or environment setting. The legacy
  `safety_factor_threshold` can tighten policy, never weaken the floor.
- Raw Onshape instance IDs remain in ingestion-local `InstanceEdge` objects.
  Only mapped node/member IDs enter `DependencyGraph`. Unknown instances are
  logged and included in graph warnings. An empty live document does not
  manufacture geometry; demo ingestion explicitly chooses synthetic data.
- `.eml` is a real saved deliverable, not proof of email delivery. Public
  demo mode cannot send Gmail or make live CAD/LLM/SkyCiv requests. No CAD
  branch creation, merge, or write implementation is part of this project.
  `responsible.engineer@example.com` is an explicit placeholder, not a resolved
  CAD owner. A trusted operator can configure the actual recipient.

## Solver Validation

The benchmark uses the canonical geometry with uniform `A=10 in^2`,
`E=10,000,000 psi`, two downward `100,000 lbf` loads at n2/n4, and pinned n5/n6.
Exact inch/lbf conversions are applied once in `earl/units.py`; these validation
values deliberately differ from the original rounded contract specimen.

The definition is the standard Haftka/Gurdal/Rajan ten-bar family, also
published with runnable reference code in [Martins and Ning, Appendix D.2.2](https://mdobook.github.io/html/appendix-functions/)
and its [reference implementation](https://raw.githubusercontent.com/mdobook/resources/df0c0f1bcc8baea1ede63442fa3f32fa4f275e71/exercises/tenbartruss/truss.py).
The uniform 10 in^2 initial design is also specified in [Chen et al. (2015)](https://onlinelibrary.wiley.com/doi/10.1155/2015/521482).
The frozen values below reproduce that published formulation with an
independent direct-stiffness calculation (`scripts/benchmark_reference.py`),
without PyNite. They are **not claimed to be a transcribed numerical table
from Haftka's book**. A textbook formulation and a same-family benchmark are
not evidence of code compliance or universal solver correctness.

The book authors' **unmodified published code was also executed directly** at
commit `df0c0f1bcc8baea1ede63442fa3f32fa4f275e71`. Its stresses and the
displacements returned by its linear solve were recorded in
`tests/fixtures/benchmark/published_reference.json`. Against PyNite, maximum
absolute differences were `3.45608e-11 psi` and `4.44089e-15 in`. The source
SHA-256 is `8e877b3b4436779d954759df77b2a690634f5c7978891734defa861898be7c39`.
`scripts/published_reference_selfcheck.py` accepts that checksum-pinned source
as a local input and reproduces the comparison without changing its matrices
or loads. The recorded numbers are from executable published code, not a
textbook results table. Normal runtime validation needs only committed JSON.

| Member | Reference Stress (psi, Tension Positive) | PyNite (psi) |
|---|---:|---:|
| m1 | 19536.498697 | 19536.498697 |
| m2 | 4012.463226 | 4012.463226 |
| m3 | -20463.501303 | -20463.501303 |
| m4 | -5987.536774 | -5987.536774 |
| m5 | 3548.961922 | 3548.961922 |
| m6 | 4012.463226 | 4012.463226 |
| m7 | 14797.625453 | 14797.625453 |
| m8 | -13486.645795 | -13486.645795 |
| m9 | 8467.655712 | 8467.655712 |
| m10 | -5674.479912 | -5674.479912 |

| Node | Reference dx (in) | Reference dy (in) | PyNite dx, dy (in) |
|---|---:|---:|---|
| n1 | 0.847762629 | -3.795126309 | 0.847762629, -3.795126309 |
| n2 | -0.952237371 | -3.939574985 | -0.952237371, -3.939574985 |
| n3 | 0.703313953 | -1.674352450 | 0.703313953, -1.674352450 |
| n4 | -0.736686047 | -1.802115080 | -0.736686047, -1.802115080 |
| n5, n6 | 0 | 0 | 0, 0 |

The runtime asserts all ten stresses and all twelve translational values
against both committed reference datasets on first worker use. Relative
tolerance is `1e-6`,
with absolute tolerances `1e-5 psi` and `1e-8 in`. The separate test compares
the independent calculation to PyNite to six decimal places in psi and nine
inches decimal places. The production engine is pinned `PyNiteFEA 2.4.1`.

## Sanity Checks

Every successful EARL solve includes both checks. Free-direction joint balance
reconstructs member force vectors plus external loads; allowed residual is
`max(1e-6 N, 1e-8 * sum(abs(applied forces)))`. Restrained directions may
carry reactions. Linearity uses a second load combination with every load
doubled and checks every recovered member stress at relative tolerance `1e-9`
with a near-zero numerical scale. Failure or missing checks force `ERROR`.

The initial demo measured `3.2742e-11 N` maximum joint residual and `0.0`
maximum load-linearity error. Its minimum safety factor is `1.998859` (m8,
Euler buckling). A killed, singular, malformed, or timed-out solver has no
safety verdict. A separate worker process is killed after at most 12 seconds;
no credential environment variables are passed to it.

PyNite axial signs are inverted once to report tension positive. Load-scaled
axial values below `1e-12 * sum(abs(loads))` are treated as roundoff zero.
Zero demand uses a null/unbounded safety factor with an explicit flag, never
nonstandard JSON Infinity. Missing inertia is `NOT_EVALUATED`; tiny inertia
used solely for PyNite end-release algebra never becomes a capacity.

## Evaluation Results

The catalogue was frozen in commit `51dd329` after this protocol was committed
and before the first evaluation. Five area edits, five member removals, five
added loads, and five moved loads comprise the twenty cases. Every initial
member passes. The changed models contain 18 failing-member findings in total.

| System | Reported Findings | Dropped Dominoes | Cases With Drops | Analysis Errors |
|---|---:|---:|---:|---:|
| EARL | 18 | 0 | 0 | 0 |
| Rule-based selection baseline | 8 | 10 | 7 | 0 |

The baseline is explicitly the deterministic local policy above, not a live
LLM run. Selecting all members is permitted and a test verifies that this
eliminates its omissions. An incomplete baseline report is escalated, even in
cases with no failing members; these numbers do not count unsafe approvals.
Ground truth and EARL share the validated solver/capacity implementation, so
agreement is expected when EARL checks every member. The useful contrast is
complete numerical verification versus a fallible selection heuristic, not a
new structural-analysis algorithm or graph traversal result.

`python -m earl eval` regenerates all 42 committed JSON files under
`earl/eval/cache/`: one benchmark, one scoreboard, twenty ground-truth runs,
and twenty baseline runs. Each case contains its input, typed change, selected
IDs, explicit findings, and validated decisions. The scoreboard endpoint only
reads this cache and checks the catalogue hash; it never invokes a model.

## Verification Status

The end-to-end pipeline checkpoint passes 119 offline tests, including the
original 50. With networking disabled, `thin-compression` resizes m8 from
20 to 8 in^2, produces SF `0.478`, escalates, and writes an ECN, MIME email,
Decision JSON, report reference, and nine-stage JSONL trace. `reinforce-chord`
approves. A mocked solver crash produces a held `ERROR` with a saved email.

Tests cover hard-floor rejection on produce and consume, solver benchmark,
both sanity checks, buckling, stale model and capacity results, unmapped instances,
typed edit application, incomplete and disputed escalation, capability denial,
network fallbacks, public-input clamps, and tampered SSE/trace rejection.
Headless Chromium application checks exercise all six panels, comparison,
hover, sorting, removal, parser preview, artifacts, and five viewports:
1440x900, 1366x768, 820x1180, 390x844, and 320x700. They found no page overflow,
overlapping controls, JavaScript exceptions, or third-party requests. The member
table intentionally scrolls horizontally on narrow phones.

The gate independently checks any supplied capacity cache against the current
graph and solved forces. A stale capacity cannot reuse an older passing safety
factor for a changed model. Missing capacity remains `NOT_EVALUATED`; a
contradictory supplied value is an analysis-integrity error, not an approval.

`scripts/browser_selfcheck.py` writes screenshots and measurements under
`out/qa/`. It uses an isolated browser profile, not a user's session. Native
Safari also rendered the public escalated ECN. SVG colors change only when a
gate event arrives; preserving the member elements makes their transition
real, and reduced-motion users get an immediate color update.

## External-Service Reality

The checked-in `tests/fixtures/onshape/` responses are actual recordings of an
empty document. `tests/fixtures/synthetic/` is hand-authored test/demo data.
No SkyCiv, Gmail, or LLM credential is assumed. A fixture or locally generated
artifact must never be presented as a real SkyCiv solve or a sent message.

The committed SkyCiv fixture is the actual [public example PDF](https://skyciv.com/media/docs/SkyCiv-Example-Report.pdf)
retrieved by HTTP 200 on 2026-09-13, with its SHA-256 in
`tests/fixtures/skyciv/provenance.json`. It is the 2016 engine-crane sample,
**not the EARL truss**. No authenticated SkyCiv analysis response could be
recorded without credentials. The offline ECN cites this sample's format and
explicitly says the independent cross-check was NOT PERFORMED. This is a
remaining live-integration limitation, not a fabricated successful check.
An optional live adapter submits the exact model, retrieves a report reference
and governing axial demand magnitude, records the raw response, and reuses it
only for an exact model/member fingerprint. Disagreement is returned to Biject
and forces escalation. A failed PDF/report function, rejected report URL, or
local report/recording write failure cannot discard a completed numerical
disagreement. The `S3D-*` reference is a local content-derived label, not a
claimed SkyCiv job identifier. Failure of the optional independent service
means the cross-check is unavailable; failure of the primary PyNite analysis
means `ERROR`. Neither is fabricated into a safety finding.

The optional language client requires an explicit `META_MUSE_URL` and
`META_MUSE_MODEL` in addition to `META_MUSE_KEY`; an unspecified provider is
not guessed. All three jobs fall back when unavailable or malformed. Prose
uses a closed neutral vocabulary from the typed edit, cannot add a safety
claim, and cannot replace the canonical change description in the ECN.
The baseline selector receives geometry, sections, material properties, loads,
units, and thresholds, but no solved results or EARL affected-member hints.

Public requests always disable live calls independently of environment flags.
Trusted CLI calls additionally need `--live`, `DEMO_MODE=false`, and the
corresponding credentials. Local output persistence is application code, not
a model capability. The model sees structured data and no executable tools,
file handles, Onshape client, or delivery callable. The capability test checks
the strict three-operation surface and absence of action tools. The gate
enforces the EARL acceptance state, not a real Onshape branch permission.

Offline semantic outputs (model, forces, comparisons) are deterministic. Run IDs,
audit timestamps, and measured durations intentionally vary between runs.

## Public Deployment

Verified URL: [EARL on astra](https://unbridle-dining-crystal.ngrok-free.dev).
The initial public verification on 2026-09-13 received all nine SSE stages,
an `ESCALATED` decision with m8 SF `0.47845501789713346`, and HTTP 200 for the
ECN, email, report reference, trace, and decision endpoints. Ingest arrived at
53.5 ms and solve at 865.8 ms; these observed arrival times show the proxy did
not buffer the run into a single final response. They are not latency promises.
`scripts/deployment_selfcheck.py` reproduces these checks against the URL.

The Dockerfile and Render blueprint are checked in, with `branch: astra`
explicitly set. No authenticated Render or Fly.io deployment credentials were
available. The Docker CLI was present but its engine was not running, so a
local image build was not verified. The current URL is **not a Render/Fly
deployment** and has no persistent-volume or uptime guarantee.

The brief's cloudflared fallback was changed to the already configured ngrok
agent because [Cloudflare Quick Tunnels explicitly do not support SSE](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/trycloudflare/).
No named Cloudflare tunnel configuration was available. A working incremental
SSE stream takes priority over choosing a proxy that buffers this demo.
[Ngrok's free tier](https://ngrok.com/docs/pricing-limits/free-plan-limits) adds
a first-visit HTML notice with **Visit Site**; it is visible to real visitors,
not hidden by the application. Automated public app tests use the documented
`ngrok-skip-browser-warning` header and record that fact. Local tests require
no such header. Free-tier traffic quotas still apply.

For the existing dev machine, `python scripts/start_demo.py` launches the
committed `astra` source and existing ngrok configuration, refusing occupied
ports or dirty tracked source. It records process IDs and logs under
`out/services/`, puts the launched commit in `/healthz`, and verifies the
public endpoint reports that commit. The dev machine must remain awake and
both processes must remain running. Credentials are never printed or committed.

Public mode is always offline, even if an operator sets `DEMO_MODE=false`.
Judge areas are clamped to 0.5-40 in^2 and loads to 0-150 kN. Member IDs and
free load nodes are allowlisted; unsupported parameters are rejected. Two
simultaneous runs are allowed, with a 12-second killable timeout per solve.
POST bodies are limited to 16 KiB and five seconds of receive time before JSON
parsing; change text itself is limited to 2,000 characters.
No public request can choose arbitrary geometry, files, URLs, or API tools.
Every completed stage is appended to the local JSONL trace. If a browser
disconnects mid-run, its stream may end before delivery; the UI reports ERROR
and does not claim a completed acceptance record. There is no background run
manager or durable queue, as required for this hackathon scope.

Reference for the overlap: [Onshape Simulation](https://www.onshape.com/en/features/simulation).
