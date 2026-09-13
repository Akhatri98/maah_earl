[Live Demo](https://unbridle-dining-crystal.ngrok-free.dev) | [Submission Branch: astra](https://github.com/Akhatri98/maah_earl/tree/astra)

`astra` is the working and deployment branch for this submission.

# EARL: Two-Minute Demo

Onshape Simulation already provides automatically updated assembly FEA,
stress, displacement, safety factors, and versioned simulations. SkyCiv
already supplies analysis and reports. EARL adds enforcement, notification,
and a per-change decision record, not better physics.

## Before the Clock

Open the live URL and click ngrok's **Visit Site** button if its first-visit
notice appears. Use a laptop viewport of at least 1366 x 768 (a short scroll
reaches the scoreboard) or 1440 x 900. Opening the page does not request a
solve. **Follow agent** is enabled; it displays the latest autonomous record.
Keep the dev machine awake for the temporary tunnel.

The deployment launcher already starts the fixture watcher. For a separate
local demo, start `python -m earl watch --mode fixture --interval 2` once. The
Autonomous agent panel shows a heartbeat. Initialize a passing state with
`python scripts/mutate_fixture.py reinforce-chord` and wait for APPROVED.
Keep another terminal ready with
`python scripts/mutate_fixture.py thin-compression`. This edits a local
synthetic CAD event file; it does not invoke the pipeline. Do not call this a
live Onshape edit. The agent persists its ledger across restarts.

The offline backup is [localhost:8000](http://127.0.0.1:8000), running the same
pipeline with no external requests. Do not enable live integrations for this
script. Each run starts from the same before-model, not the previous edit.

## Exact Clicks

| Time | Action | Say |
|---|---|---|
| 0:00-0:20 | Run `python scripts/mutate_fixture.py thin-compression`. Do not click Run. Watch a new row, red m8, and ESCALATED appear automatically. | "Nobody asked EARL to check this edit. The agent watches and reports on its own. This trigger is a local CAD fixture, not a live Onshape callback." |
| 0:20-0:40 | Point at SF 0.48 and the no-merge-capability link. Click **Compare**, hover m8, then **After**. | "Onshape Simulation and SkyCiv already do analysis. EARL adds the code gate, notification and record. The actual factor is 0.478; the hard floor is 1.0. No model decides safety." |
| 0:40-1:00 | Run the same thin-compression edit command again. Point at **SUPPRESSED** in the new ledger row. | "The same violation is still held, but the engineer is not emailed on every tick. That dedup decision survives restart." |
| 1:00-1:20 | Click **ECN**, then **Email** in the artifact panel. Point at fixture provenance. | "These artifacts exist on disk. Public demo mail is local-only. The SkyCiv PDF is a genuine example of a different model; an independent truss check was NOT PERFORMED." |
| 1:20-1:40 | Run `python scripts/mutate_fixture.py reinforce-chord`. Watch APPROVED and **CLEARED** arrive. | "Recovery is also reported without a prompt. A live sender has a durable retry outbox; it was tested with mocks, not sent through Gmail here." |
| 1:40-2:00 | Scroll slightly if needed and point at **Dropped dominoes**. | "Twenty fixed cases, eighteen failing-member findings. EARL misses zero; the rule-based selection baseline misses ten. Same solver, different verification scope, reproducible offline." |

## Judge Follow-Through

**Manual check** remains an optional investigation tool, not the agent's
trigger. Running it pauses **Follow agent** in this browser only; re-enable
that checkbox to resume viewing the ledger. The watcher continues either way.

- Resize with the **Area** slider and member dropdown; click **Run structural
  check**. The edited control is the active request, not a cumulative edit.
- Choose **m1** in **Remove** and run to see force redistribution. Removing a
  redundant member is not automatically unsafe; the solver and gate decide.
- Change **Load** and its node dropdown, then run. Larger downward loads make
  escalation easy. Parameters are clamped and support nodes cannot be selected.
- Type `resize m8 to 8 in2`, click **Parse edit**, inspect the typed SI delta,
  and then run. **RULE-BASED PARSER** is intentional: no LLM key is required.

## Do Not Imply

No CAD merge was performed or physically blocked in Onshape. EARL enforces its
own acceptance state; the capability test proves the model has no action
tools. This is linear-elastic axial truss analysis with ideal Euler buckling,
not AISC/ASCE-compliant design. The baseline is a declared local rule, not a
measurement of a live LLM. Dropped findings are not unsafe released designs.
See [RELIABILITY.md](RELIABILITY.md) for the complete evidence and limitations.
