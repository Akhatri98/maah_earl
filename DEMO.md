[Live Demo](https://unbridle-dining-crystal.ngrok-free.dev) | [Submission Branch: astra](https://github.com/Akhatri98/maah_earl/tree/astra)

`astra` is the working and deployment branch for this submission.

# EARL: Two-Minute Demo

Onshape Simulation already provides automatically updated assembly FEA,
stress, displacement, safety factors, and versioned simulations. SkyCiv
already supplies analysis and reports. EARL adds enforcement, notification,
and a per-change decision record, not better physics.

## Before the Clock

Open the live URL and click ngrok's **Visit Site** button if its first-visit
notice appears. Use a laptop viewport of at least 1366 x 768. The page loads
the recorded model and automatically runs **Reinforce m1: 22 in^2**; wait for
**APPROVED**. Keep the dev machine awake for the temporary tunnel.

Start `python -m earl watch --mode fixture --interval 2` in a terminal. The
Autonomous agent panel shows a heartbeat. Keep another terminal ready with
`python scripts/mutate_fixture.py thin-compression`. This edits a local
synthetic CAD event file; it does not invoke the pipeline. Do not call this a
live Onshape edit. The agent persists its ledger across restarts.

The offline backup is [localhost:8000](http://127.0.0.1:8000), running the same
pipeline with no external requests. Do not enable live integrations for this
script. Each run starts from the same before-model, not the previous edit.

## Exact Clicks

| Time | Action | Say |
|---|---|---|
| 0:00-0:15 | Run the prepared fixture-edit command. Do not click Run. Watch a new ESCALATED row arrive in **Autonomous agent**. | "Nobody asked EARL to check this edit. It watches, evaluates, and reports on its own. This trigger is an offline CAD fixture." |
| 0:15-0:35 | Point at the autonomous row, then open its ECN. | "Onshape Simulation and SkyCiv already do analysis. Our agent adds the binding code gate, notification policy, and durable record." |
| 0:35-0:55 | Follow the trace to **Gate** and point at **ESCALATED**. | "The computed safety factor is 0.478 against a hard floor of 1.0. Euler buckling governs. Code makes this decision. A model has no merge tool." |
| 0:55-1:15 | Click **Compare** above the truss; hover m8. Then click **After**. | "The before-model passes. This change does not. All members are checked because forces redistribute through this indeterminate structure." |
| 1:15-1:35 | In the bottom-right artifact panel, click **ECN** if needed; point at the report provenance below it. | "The ECN records the gate's decision. The SkyCiv attachment is a genuine published example of a different model. Independent numerical cross-check: not performed. We do not pretend this fixture is a live solve." |
| 1:35-1:50 | Click **Email**, then the download-email icon if a file is useful. | "This MIME email exists on disk and would go to the responsible engineer. No email is sent in public demo mode." |
| 1:50-2:00 | Point at **Dropped dominoes** in the lower-left panel. | "Twenty fixed cases, eighteen failing-member findings. EARL misses zero; the rule-based selection baseline misses ten. Same solver, different verification scope, reproducible offline." |

## Judge Follow-Through

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
