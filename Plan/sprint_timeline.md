# EARL Build Checkpoints

Onshape Simulation already supplies automatically updated assembly FEA,
stress, displacement, factors of safety, and versioned simulations. SkyCiv
already supplies structural analysis and reports. EARL's added contribution is
an enforced structural gate, notification, and a decision record.

The one-shot brief supersedes the earlier multi-sprint plan. Work, commits,
pushes, and deployment stay on `astra`; `main` is not modified.

| Elapsed | Work | Working checkpoint |
|---|---|---|
| 0:00-0:20 | Install dependencies; units, mapping, typed delta, thresholds | Original 50 tests pass |
| 0:20-1:00 | PyNite solver, reference benchmark, capacity, sanity | Benchmark assertion passes |
| 1:00-1:30 | Gate, pipeline, CLI | Offline change produces a decision |
| 1:30-2:15 | FastAPI, SSE, one-file frontend | Six demo elements in the browser |
| 2:15-2:45 | Twenty-scenario eval, selecting baseline, committed cache | Scoreboard regenerates offline |
| 2:45-3:15 | ECN, .eml, SkyCiv adapter and labeled fallback | Offline escalation renders |
| 3:15-3:40 | Docker plus Render/Fly; tunnel fallback | Public URL responds |
| 3:40-4:00 | README, two-minute DEMO, RELIABILITY, final push | Submission lives on astra |

Run `python -m unittest discover -s tests -q` before every commit. The model
has no merge, CAD write, or send capability. Failed solves are errors. No
external call is required for a demo run.
