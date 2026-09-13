# EARL — Engineering Analysis & Rating Ledger

**Your own pocket structural engineer: it catches what a CAD change breaks
before it ships, and keeps the paper trail to prove it.**

Built for the Multi-App AI Agent Hackathon by Lemma.

| | |
|---|---|
| **▶ Two-minute demo** | _<!-- TODO: paste the video link here before submitting -->_ |
| **Live site** | `python3 scripts/serve.py --ngrok` — drive it yourself, see [The site](#the-site) |
| **Terminal demo** | `python3 scripts/demo.py` — six acts, ~4 min |
| **Tests** | 805, all offline: `python3 -m unittest discover -s tests -t . -q` |

Everything above runs **offline, with no credentials**.

---

## Overview

An engineer thins one diagonal of a truss in Onshape to save weight. The
member they touched is fine. Three hops away, a member **nobody edited** goes
36 % over its allowable stress — and in a normal workflow nobody finds out
until something is built.

EARL is the agent that catches that. On every CAD change it:

1. **reads the change out of Onshape** — variable-table and feature edits, the
   assembly's mates and "where used" references — and builds a dependency
   graph of everything downstream;
2. **walks that graph with a fixed BFS**, not a model's judgement, so
   completeness does not depend on an LLM remembering to look three hops out;
3. **solves the real structure with PyNite** (matrix stiffness FEA) and hands
   the forces to **Biject**, the verification SDK described below, which turns
   them into a per-member PASS/FAIL verdict in pure code;
4. **escalates to SkyCiv** — commercial structural-analysis software — for a
   trusted report and an independent second-solver cross-check when something
   fails;
5. **mails the engineer an Engineering Change Notice** citing it, via Gmail.

**Nothing merges to Main unless the code says so.** The LLM in the pipeline
plans which load case to lead with; it never gets to decide whether a change
is safe, and it holds no merge permission at all.

```
Onshape change ─▶ DependencyGraph ─▶ fixed BFS walk ─▶ PyNite + Biject ─▶ Decision
                  (earl.ingestion)                     (earl.analysis)
                                                             │ escalated
                                                             ▼
                                          SkyCiv report + cross-check (earl.artifacts)
                                                             │
                                                             ▼
                                      ECN ─▶ Gmail / outbox (earl.delivery)
                                      (earl.pipeline wires all four stages)
```

### The demo change, end to end

The 10-bar truss (Haftka & Gürdal / Rajan) at its published optimum with a
10 % design margin. Diagonal **m7** is thinned from 8.203 to 6.000 in² to save
weight. m7 itself passes at safety factor **1.107**. Its neighbour **m5**,
which nobody touched, drops to **0.735** — 136 % utilization — and the change
is **HELD**. That is the dropped domino the project exists to catch, and
`python3 scripts/run_pipeline.py` prints the whole thing, ECN included, with
no network access.

---

## External apps

| App | Stage | What EARL does with it | Status | Where |
|---|---|---|---|---|
| **Onshape** | 1 — ingestion | REST client (`earl/ingestion/onshape_client.py`): reads the variable table, part-studio and assembly features, mates and mate connectors, parts; derives topology and a "where used" index; creates/resets/deletes a **branch sandbox** so evaluation never touches Main | **Live.** The 10-bar truss is modelled in a real document; every fixture in `tests/fixtures/onshape/` is a recorded live response. `scripts/onshape_branch_smoketest.py` runs the full create-version → branch → write-variable → re-read → walk → delete cycle against the live API | `earl/ingestion/` |
| **SkyCiv** | 3 — trusted artifact | Builds an S3D model from the same graph, solves it, pulls the PDF report and an AISC-360 design check, and cross-checks member forces against PyNite (`earl/artifacts/`) | **Live, and honestly capped.** The dry run fixed a real payload bug (a truss section carries area only, so `Iy` was rejected 110 times) and the model now validates and reaches SkyCiv's solver — where the **free tier's 5-member limit** stops a 10-member truss. That is a purchasing decision, not a bug, and the ECN says so rather than citing a report nobody can open | `earl/artifacts/` |
| **Gmail** | 4 — delivery | Send-only REST client (`earl/delivery/gmail.py`). The e-mail body **is** the rendered ECN, byte for byte; evidence (ECN JSON, Decision JSON, truss SVG, SkyCiv PDF when one exists) is attached, never paraphrased | **Wired, credentials pending.** Without `--send`, the identical message is written to `artifacts/demo/outbox/*.eml` — byte for byte what Gmail would receive | `earl/delivery/` |
| **Meta Muse** (LLM) | 2 — orchestration, and the eval baseline | Plans which load case leads and whether to add self-weight (advisory only — see Biject below). The **same model** also drives the baseline agent the reliability numbers are measured against | **Live.** All twenty eval scenarios and the 3× consistency run were executed against it; every turn is recorded in `demo/recordings/transcripts/` | `earl/analysis/orchestrator.py`, `earl/eval/` |
| **PyNite** | 2 — fast gate | The open-source matrix-stiffness FEA library that computes every force EARL acts on. Not a hosted app, but it is the dependency the whole verdict rests on, so it is validated against published results before anything else is believed | **Live on every run**, offline | `earl/analysis/solver.py` |
| **ngrok** | demo | Public URL for the browser demo so a judge can drive it from their own machine | **Live**, and the server is built for a public URL: it writes nothing, sends nothing, calls no model, serves five fixed files, caps bodies and rate-limits runs | `scripts/serve.py` |

---

## Biject — the verification SDK

`earl/analysis/biject.py` is the part of EARL that decides whether a change is
safe, and it is the reason the rest of the pipeline can be trusted. It is a
**self-contained, dependency-free SDK**: import it, hand it a graph and solver
forces, get verdicts back. It imports nothing but the contracts and the solver
adapter, and it is covered by its own dedicated test module.

```python
from earl.analysis import biject

verdict = biject.evaluate(graph, solve_results, threshold=1.0, before=before)

verdict.outcome              # Outcome.ESCALATED | Outcome.APPROVED
verdict.violating_member_ids # ['m5']  — sorted, matches the FAIL results exactly
verdict.result('m5')         # MemberResult: SF 0.735, util 136.1%, capacity, notes
biject.member_capacity(graph, 'm5')   # tension / compression capacity + what governs
```

**What makes it a guarantee rather than a hope:**

- **No model input, by construction.** A verdict is a function of the graph,
  the solver's forces and one number. There is **no parameter anywhere in the
  API that can force APPROVED** — not for a caller, not for a planner, not for
  an LLM. The plan's rule "an LLM proposes, code disposes" is a shape of the
  function signature, not a convention.
- **The threshold floor is code, not config.** `MIN_ALLOWED_THRESHOLD = 1.0`.
  A `SAFETY_FACTOR_THRESHOLD` below it raises a `ValueError` *before anything
  is solved* — you can raise the bar, never lower it. The web layer does not
  re-implement this check; it reaches the same function (a rule enforced in
  two places is a rule that can disagree with itself).
- **A real capacity model.** Tension: design stress × area. Compression: the
  smaller of that and the Euler load `π²EI_min/(KL)²`, over the inertias the
  section actually provides — with a note when none are provided (no buckling
  check) or only one is (the `Pcr` is an upper bound). Capacity crosses the
  contract as a *stress*, so `capacity / |stress| == safety_factor` exactly.
- **NaN can never read as PASS.** `NaN < threshold` is `False`, which would
  silently mean "safe". Every area, modulus, design stress, force and safety
  factor is checked with `math.isfinite` first, and a non-finite value is a
  `ValueError` — never a verdict.
- **Every member is evaluated, whatever the plan said.** A planner's
  `focus_member_ids` is advisory and is never read to narrow anything. With
  several load cases (or self-weight variants) a member's verdict is its
  **worst** across all of them, and the governing variant is written into its
  note — so a planner picking the mild case cannot hide a member that fails
  under the harsh one.
- **It reports on the graph walker, too.** A member that FAILs while *not* in
  `affected_member_ids` is surfaced with a note saying the walk may have
  missed a dependency. The verification layer watches the traversal that feeds
  it — that is the dropped domino, caught from the other side.
- **A second, independent backstop.** `Decision.validate()`
  (`earl/contracts/decision.py`) re-checks the invariants at the track
  boundary: APPROVED with any member under threshold, `violating_member_ids`
  disagreeing with the results, a member marked PASS while below threshold,
  ESCALATED with no computed failure, ERROR with no message. Cheap enough that
  there is no reason to skip it, called on both produce and consume.

**You can try to break it on stage.** Every escalated run in the browser demo
has a button marked **Approve it anyway**. It takes the `Decision` that run
really produced, flips `outcome` to `APPROVED`, calls `validate()` — and
prints the `ValueError` naming m5 and its safety factor. That is the one thing
on the page no model can promise, on a judge's own change.

---

## Setup

```bash
python3 -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt                    # three deps: requests, python-dotenv, PyNiteFEA
cp .env.example .env                               # optional — everything below runs without it
```

`.env.example` documents every credential and which stage wants it. **No
credential is needed for the tests, the demo, the site, the eval replay or the
end-to-end pipeline** — those all run offline against recorded data.

```bash
python3 -m unittest discover -s tests -t . -q   # 805 tests, all offline, ~18s

python3 scripts/serve.py                        # THE SITE: drive it in a browser
python3 scripts/serve.py --ngrok                # + a public URL for judges
python3 scripts/demo.py                         # THE TALK: six acts, offline, ~4 min
python3 scripts/demo.py --pause                 # stop between acts, for rehearsal
python3 scripts/run_pipeline.py                 # the demo change, every stage, offline
python3 scripts/run_pipeline.py --onshape-fixture   # the graph builder on a recorded Onshape read
python3 scripts/run_pipeline.py --skyciv --send     # live SkyCiv + Gmail (needs credentials)
python3 scripts/run_eval.py                     # the 20-scenario replay: zero dropped dominoes
python3 scripts/run_eval.py --agents system,baseline   # + the live baseline LLM (META_MUSE_KEY)
python3 scripts/validate_solver.py              # PyNite vs the published 10-bar truss optimum
python3 scripts/contract_selfcheck.py           # the shared contracts and their guardrails
```

With live credentials, two scripts touch the real APIs and say what they cost
before they do: `scripts/onshape_branch_smoketest.py --confirm` (~7 Onshape
calls, and one permanent version in the document's history — Onshape versions
cannot be deleted) and `scripts/skyciv_smoke.py`.

---

## The site

`scripts/serve.py` puts EARL behind a browser so someone else can drive it.
Pick a member, thin it, and watch the dependency walk, the solve, the verdict
and the change notice come back — then press **Approve it anyway** and watch
`Decision.validate()` raise, which is the one thing on the page no model can
promise. A separate tab carries the recorded eval, reported as measured.

Every change composed in the browser is built by `earl.eval.scenarios` — the
same module that builds the twenty eval scenarios — and run through
`earl.pipeline.run_pipeline`. There is no demo-only shortcut behind the form,
and the threshold floor is not re-implemented in the web layer: a request that
tries to lower it reaches `gate.resolve_threshold` and is refused there.

`--ngrok` tunnels it (needs the `ngrok` binary and `NGROK_AUTHTOKEN` in
`.env`). **That makes the URL public**, so the server is built for it — the
threat model and what it therefore refuses to do are in
[`earl/web/README.md`](earl/web/README.md).

---

## How we tested reliability

Five layers, from "is the physics right" up to "is the agent right, repeatedly".

**1. The solver is validated against published results, not against itself.**
`scripts/validate_solver.py` replays the 10-bar truss benchmark at its
published Case 1 optimum:

```
optimum weight (lb)                        5060.8500  ->  5060.9254   PASS
optimum |stress| m5 (ksi)                    25.0000  ->    25.0027   PASS
optimum n1 drop (in)                          2.0000  ->     2.0000   PASS
equilibrium residual (N)                      0.0000  ->     0.0000   PASS
linearity deviation (2x load => 2x stress)    0.0000  ->     0.0000   PASS
```

Plus a regression pin on all ten member forces (max relative deviation
5.6 × 10⁻⁸). Linear-elastic truss analysis is exact, so a solver validated
once generalizes to new scenarios of the same type.

**2. Equilibrium and linearity re-run on every single scenario.** `ΣF = 0` at
every joint and "double the load, exactly double the stress" are free
correctness tests, so they are not reserved for the benchmark — they run on
every solve, and a failed check becomes `Outcome.ERROR`, never an approval and
never a safety finding. A crashed solve and an unsafe structure are different
things and the contract keeps them apart.

**3. 805 tests, every one offline.** No test makes a live API call, so the
suite is free to run and never spends the Onshape annual request budget.
That includes a dedicated module for Biject's verdict logic, the contract
guardrails, the walker's byte-identical determinism, the SkyCiv payload
against recorded responses, and nine spellings of `../.env` thrown at the web
server on a raw socket.

**4. A 20-scenario replay, scored against computed ground truth.**
`scripts/run_eval.py` perturbs the validated truss twenty ways — resize
thinner and thicker, remove, add a load, move a load, edit a variable.
**Eleven are unsafe, nine are safe**, and in ten of the eleven the member that
fails is *not* the one the change names. Three rules keep it honest: ground
truth is *computed* by the validated solver on a graph no agent touched, never
declared; each agent gets its own build of the scenario (share one and the
baseline inherits our traversal); and an agent that crashes or goes silent is
**scored, not skipped** — "I could not run" counts as a dropped domino. Nine
safe scenarios are in the set specifically so that "escalate everything"
cannot score a perfect headline number.

```
| agent    | scenarios | unsafe members | caught | dropped dominoes | false alarms | recall |
|----------|----------:|---------------:|-------:|-----------------:|-------------:|-------:|
| system   |        20 |             22 |     22 |                0 |            0 |   1.00 |
| baseline |        20 |             22 |     22 |                0 |            0 |   1.00 |
```

**5. Then every scenario, three times over.** 60 runs per agent: **zero
unstable answers, 60/60 correct, zero dropped dominoes.**

### The result we did not want, reported anyway

`plan.md` predicted the baseline — a tool-using LLM with the same PyNite
access but no fixed traversal and no enforced threshold — would drop dominoes
we caught. **It did not. It tied us, member for member, on all 60 runs.** The
transcripts show why, and it is not luck: one `solve_load_case` call returns
all ten members' stresses, so on a six-node truss "check everything
downstream" and "check everything" are the same action. Traversal earns its
keep at scale; this structure is not that scale.

We rebuilt the demo around the measurement instead of tuning the measurement
to fit the demo. What the same 60 runs still show:

- **the system is right every time by construction**, from a fixed algorithm,
  not from a model that happened to be careful that run;
- **the system is deterministic** — the walker has a test pinning
  byte-identical output — while the baseline did visibly vary: on one run it
  did the engineering correctly and then never called `report_verdict` at all,
  which scored naively is silence;
- **the threshold is enforced in code.** An LLM can approve a member it has
  just computed to be over its allowable. `Decision.validate()` raises instead.

The full write-up, including the three axes where a real gap may still exist,
is in [`earl/eval/README.md`](earl/eval/README.md).

### What the dry run caught that no test could

Running the whole thing end to end found two things 760 green tests had not:
SkyCiv had **never worked live** (every truss model rejected with 110
`Iy should be > 0` errors — a truss section carries area only, and one of our
own tests was asserting that broken payload), now fixed; and Gmail credentials
were empty, so `--send` raised. Both are reported in the
ECN and on stage rather than papered over. The pipeline is built for exactly
this: **every stage after the gate degrades rather than aborts.** SkyCiv down,
Gmail down, a renderer exception — each lands in `PipelineResult.notes` and,
where the contract has a slot, on the Decision itself. The verdict was
computed before any of them ran, and a delivery failure must never turn into a
missing verdict.

---

## Layout

| Package | Stage | Track |
|---|---|---|
| `earl/contracts` | the shared `DependencyGraph` / `Decision` boundary (v0.2.0) + `validate()` guardrails | both |
| `earl/ingestion` | Onshape client, parsers, graph builder, BFS walker, branch sandbox | A |
| `earl/analysis` | PyNite solver, **Biject**, LLM planner, sanity checks, benchmark, scoreboard, SVG | B |
| `earl/artifacts` | SkyCiv client, escalation, cross-check | B |
| `earl/delivery` | ECN template and renderers, Gmail / outbox delivery | A |
| `earl/eval` | the 20-scenario replay and the baseline agent | A |
| `earl/web` | the browser demo: change bench, scoreboard, enforcement button | A |
| `earl/pipeline.py` | the end-to-end wire, and the only path to a merge | both |

Each package has its own README with the conventions that bind it.
`Plan/plan.md` is the pitch; `Plan/sprint_timeline.md` is how the work was
split and what each sprint actually landed.

## Conventions that bite

- **SI everywhere** inside the contracts (m, N, Pa, m²). Every payload carries
  an explicit `Units` block and every entry point asserts it. Two halves that
  disagree about Pa vs. MPa produce a plausible number wrong by 10⁶ — exactly
  the silent failure this project exists to catch. The benchmark's imperial
  numbers are converted **once**, in each track's `benchmark.py`.
- **Tension positive.** PyNite 3.2 reports axial force compression-positive;
  the adapter negates it once, at the boundary.
- **Two stresses on a material.** `yield_strength` is yield (50 ksi here; what
  SkyCiv's design check uses). `allowable_stress` is the design allowable
  (25 ksi; what Biject computes capacity from). Safety factor = allowable /
  stress.
- **Evaluation happens in an Onshape branch**, never Main. `BranchSession`
  exposes no merge method at all; merging is the caller's callable, reachable
  only after `Decision.validate()` passes on an APPROVED outcome.

## Scope notes, stated plainly

- Load checks are simplified, illustrative structural checks (dead load plus
  one or two applied loads), not full code-compliant bridge analysis, even
  where SkyCiv's design-check modules are used.
- The escalation threshold is a defined rule (a safety-factor cutoff), not a
  learned or general-purpose judgement model.
- The SkyCiv free tier caps a solve at 5 members, so the 10-member demo truss
  returns no live report; the ECN says so instead of citing one.
- "Biject" is EARL's own verification module. It is not an existing commercial
  product, and nothing here should be read as claiming otherwise.
