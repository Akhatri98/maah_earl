# EARL — Engineering Analysis & Rating Ledger

Multi-App AI Agent Hackathon by Lemma. See `Plan/plan.md` for the pitch and
`Plan/sprint_timeline.md` for how the work was split.

| | |
|---|---|
| **Two-minute demo** | _<!-- paste the video link here -->_ |
| **Live site** | `python3 scripts/serve.py --ngrok` — see [The site](#the-site) |
| **Tests** | 805, all offline: `python3 -m unittest discover -s tests -t . -q` |

## Project overview

An engineer changes one dimension in a CAD assembly. The parts that break are
usually *not* the parts they touched — a 10-bar truss is statically
indeterminate, so thinning one member pushes its load into members nobody
edited. Today that gets caught in review, or it doesn't.

EARL watches a CAD change in Onshape, walks every member downstream of it,
runs a real structural solve (PyNite) behind a code-enforced safety threshold
(Biject), has SkyCiv produce the trusted report when something fails, and
mails the engineer an Engineering Change Notice that cites it. Nothing merges
to Main unless the code says so.

```
Onshape change ─▶ DependencyGraph ─▶ walker ─▶ PyNite + Biject ─▶ Decision
                  (earl.ingestion)             (earl.analysis)
                                                     │ escalated
                                                     ▼
                                  SkyCiv report + cross-check (earl.artifacts)
                                                     │
                                                     ▼
                              ECN ─▶ Gmail / outbox (earl.delivery)   (earl.pipeline wires it)
```

## External apps used

| App | What EARL does with it | Where |
|---|---|---|
| **Onshape** | Reads the CAD change — variable-table edits, feature edits, mate structure and "where used" — and builds the dependency graph. Evaluates on a branch, never on Main. | `earl/ingestion/` |
| **SkyCiv** | Produces the independent, trusted structural report when a change is escalated, and gets cross-checked against our own solver. | `earl/artifacts/` |
| **Gmail** | Delivers the Engineering Change Notice to the engineer, with the truss picture attached. | `earl/delivery/` |
| **Meta Muse** | The LLM behind stage-2 orchestration and behind the baseline agent in our eval. One provider for every model call, baseline included. | `earl/analysis/orchestrator.py`, `earl/eval/` |
| **PyNite** | The FEA solver behind the fast gate, validated against the published 10-bar truss benchmark. | `earl/analysis/` |
| **ngrok** | Tunnels the demo site so judges can drive it. | `scripts/serve.py` |

**Two honest notes.** SkyCiv's free tier caps a model at 5 members and our
truss has 10, so Stage 3 reaches the solver and stops at an account limit; the
ECN and the demo both say so rather than citing a report nobody can open. And
Gmail credentials are not in this repo, so `--send` needs your own — without
it the message is written to `artifacts/demo/outbox/*.eml`, byte for byte what
Gmail would receive.

## Setup

```bash
python3 -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                # fill in what you have; everything below runs without it

python3 -m unittest discover -s tests -t . -q        # 801 tests, all offline

python3 scripts/serve.py                             # THE SITE: drive it in a browser
python3 scripts/serve.py --ngrok                     # + a public URL for judges
python3 scripts/demo.py                              # THE TALK: six acts, offline, ~4 min
python3 scripts/demo.py --pause                      # stop between acts, for rehearsal
python3 scripts/run_pipeline.py                      # the demo change, every stage, offline
python3 scripts/run_pipeline.py --onshape-fixture    # Track A's graph builder on the recorded Onshape read
python3 scripts/run_pipeline.py --skyciv --send      # live SkyCiv + Gmail (needs credentials)
python3 scripts/run_eval.py                          # the 20-scenario replay: zero dropped dominoes
python3 scripts/run_eval.py --agents system,baseline # + the baseline LLM agent (needs META_MUSE_KEY)
python3 scripts/validate_solver.py                   # PyNite vs the published 10-bar truss optimum
python3 scripts/contract_selfcheck.py                # the shared contracts and their guardrails
```

Everything above runs with an empty `.env`. Credentials are only needed for
the lines that say so. `.env.example` lists every key with the values blank.

## The site

`scripts/serve.py` puts EARL behind a browser so someone else can drive it.
Pick a member, thin it, and watch the dependency walk, the solve, the verdict
and the change notice come back — then press **Approve it anyway** and watch
`Decision.validate()` raise, which is the one thing on the page no model can
promise. There is also a tab for the recorded eval, reported as measured.

Every change composed in the browser is built by `earl.eval.scenarios`, the
same module that builds the twenty eval scenarios, and run through
`earl.pipeline.run_pipeline`. There is no demo-only shortcut behind the form.

`--ngrok` tunnels it (needs the `ngrok` binary and `NGROK_AUTHTOKEN` in
`.env`). **That makes the URL public**, so the server is built for it: it
writes nothing, sends nothing, calls no model, serves a fixed list of five
files, caps request bodies, and rate-limits runs. `earl/web/README.md` has
the details.

## The demo change

The 10-bar truss (Haftka & Gürdal / Rajan) at its published optimum with a 10 %
design margin. An engineer thins diagonal **m7** from 8.203 to 6.000 in² to
save weight. m7 itself is fine (safety factor 1.11). Its neighbour **m5**, which
nobody touched, drops to **0.73** and the change is HELD. That is the dropped
domino the project exists to catch.

## Layout

| Package | Stage | Track |
|---|---|---|
| `earl/contracts` | the shared `DependencyGraph` / `Decision` boundary (v0.2.0) | both |
| `earl/ingestion` | Onshape client, parsers, graph builder, walker, branch sandbox | A |
| `earl/analysis` | PyNite solver, Biject thresholds, planner, benchmark, scoreboard, SVG | B |
| `earl/artifacts` | SkyCiv client, escalation, cross-check | B |
| `earl/delivery` | ECN template and renderers, Gmail delivery | A |
| `earl/eval` | the 20-scenario replay and the baseline agent (Sprint 5A) | A |
| `earl/web` | the browser demo: change bench, scoreboard, enforcement (Sprint 7) | A |
| `earl/pipeline.py` | the end-to-end wire (Sprint 4) | both |

## Conventions that bite

- **SI everywhere** inside the contracts (m, N, Pa). The benchmark's imperial
  numbers are converted once, in each track's `benchmark.py`.
- **Two stresses on a material.** `yield_strength` is yield (50 ksi here; what
  SkyCiv's design check uses). `allowable_stress` is the design allowable
  (25 ksi; what Biject computes capacity from). Safety factor = allowable /
  stress. See `earl/contracts/README.md`.
- **The threshold floor is code.** `SAFETY_FACTOR_THRESHOLD` in `.env` can only
  raise the bar above 1.0; a value below it raises before anything is solved.

## How we tested reliability

Four layers, because "it worked when I ran it" is not a reliability claim.

**1. The solver is validated against published numbers.**
`scripts/validate_solver.py` checks PyNite against the 10-bar truss benchmark
(Haftka & Gürdal / Rajan) — member stresses, displacements and the optimum
weight, at a stated tolerance. Every downstream number rests on that, so it is
checked first and independently.

**2. 805 offline tests.** `python3 -m unittest discover -s tests -t . -q`. No
network, no credentials, no model calls — recorded fixtures throughout, so the
suite means the same thing on a clean clone. It covers the contracts and their
guardrails, both graph builders, the walker, the solver, the threshold logic,
the ECN renderers, the SkyCiv payload, the eval harness and the web app.

**3. A 20-scenario eval with computed ground truth.**
`scripts/run_eval.py` parametrically perturbs the validated truss — resize,
remove, add a load, move a load, edit a variable. Ground truth is **computed**
by the validated solver on every run, never written down; eleven scenarios are
unsafe, nine are safe, and in ten of the eleven the member that fails is not
the one the change names. The metric is the *dropped domino*: a truth-unsafe
member an agent failed to report. Each agent gets its own build of each
scenario so the system's traversal cannot leak to the baseline, and an agent
that crashes is scored as silence rather than skipped.

**4. Repeat runs, to catch nondeterminism.** Three repeats of all twenty
scenarios for both agents — 60 runs each. Reported below, including the part
that went against us.

The result is in [The number](#the-number). We also ran the whole demo end to
end as a dry run, which caught two things 760 green tests had not: SkyCiv had
never actually worked live (every payload was rejected — a truss section
carries area only, so `Iy` was zero, and one of our own tests was asserting
that broken behaviour), and the Gmail credentials were empty. Both are fixed
or stated plainly; see `Plan/sprint_timeline.md`, Sprint 6.

## The number

`scripts/run_eval.py` replays twenty parametric perturbations of the validated
truss — resize, remove, add a load, move a load, edit a variable. Eleven are
unsafe and nine are safe by the validated solver, and in ten of the eleven the
member that fails is **not** the one the change names.

```
| agent    | scenarios | unsafe members | caught | dropped dominoes | false alarms | recall |
|----------|----------:|---------------:|-------:|-----------------:|-------------:|-------:|
| system   |        20 |             22 |     22 |                0 |            0 |   1.00 |
| baseline |        20 |             22 |     22 |                0 |            0 |   1.00 |
```

**The baseline ties the system on this set, and that is the measured result.**
A tool-using LLM on the same model, with no graph traversal and no enforced
threshold, found every unsafe member and raised no false alarms — because one
`solve_load_case` call returns all ten members' stresses, so on a structure
this small there is nothing for a dependency walk to find that brute force
does not. The traversal advantage `plan.md` claims is real in principle but is
**not demonstrated by this experiment**.

What the twenty scenarios do still show is that the system is right every
time, deterministically, with a paper trail. See
[`earl/eval/README.md`](earl/eval/README.md) for the full finding, what it
does and does not license the project to claim, and the axes on which the two
agents might still differ.
