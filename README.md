# EARL — Engineering Analysis & Rating Ledger

Multi-App AI Agent Hackathon by Lemma. See `Plan/plan.md` for the pitch and
`Plan/sprint_timeline.md` for how the work was split.

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

## Run it

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

Without `--send` the e-mail is written to `artifacts/demo/outbox/*.eml`, byte
for byte what Gmail would receive.

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
