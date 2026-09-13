[Live Demo](https://unbridle-dining-crystal.ngrok-free.dev) | [Submission Branch: astra](https://github.com/Akhatri98/maah_earl/tree/astra)

`astra` is the working branch for this submission and the source of the deployment.

# EARL

EARL is an autonomous CI agent for parametric CAD: structural regression tests with an enforced
gate and a paper trail. It watches for an engineer's truss edits; EARL re-solves them,
compares safety factors in code, holds failed changes in EARL, and writes
an Engineering Change Notice for the responsible human. Its contribution is
enforcement, notification, and record, not better physics.

## What Already Exists

[Onshape Simulation](https://www.onshape.com/en/features/simulation) already
provides cloud-native linear static FEA inside assemblies: stress,
displacement, factors of safety, automatically refreshed results, and versioned
simulations. [SkyCiv](https://skyciv.com/api/) already provides structural
analysis and reporting. EARL adds its numerical acceptance gate,
notification policy, and per-change decision record on top; no Onshape merge
or CAD write capability is implemented. Webhook administration belongs only
to the trusted watcher, never the model.

## Try It

Start the independently demoable offline agent with `python -m earl watch
--mode fixture --interval 2`. In another terminal, run `python
scripts/mutate_fixture.py thin-compression`. Do not press Run in the browser:
the **Autonomous agent** ledger updates by itself. Repeat the same edit to see
`SUPPRESSED`, then use `reinforce-chord` to produce a `CLEARED` notice.
The local file is a synthetic CAD-edit trigger, not a live Onshape integration.
Stop cleanly with Ctrl-C; `out/agent/state.json` preserves runs and dedup state.
Phase 1 is independently demoable from commit `f3d8fea`. The live trigger
adapter is documentation-verified and budget-guarded, but no authenticated
Onshape callback or populated live geometry was available to verify.
See [Onshape setup](docs/ONSHAPE_SETUP.md) for the exact model convention,
webhook configuration, and conservative polling limits.

For a trusted live watcher, escalation and cleared notices use the existing
Gmail sender automatically. They require `--allow-live`, `DEMO_MODE=false`,
credentials, and real sender/recipient addresses. Failed sends remain
`UNDELIVERED` in a persistent outbox and retry with backoff; `.eml` is kept.
No Gmail send was verified live in this build. Fixture events never send.

The dashboard follows autonomous runs by default. For an optional manual
comparison, choose **Thin m8: 8 in^2**, and click **Run structural
check**. The member becomes thinner, the pipeline streams its work, and Biject
escalates at **SF 0.478**, below the hard floor of 1.0. The ECN, email preview,
and raw trace are real generated artifacts. **Reinforce m1: 22 in^2** approves.

The public URL is a temporary ngrok tunnel. Its first-visit notice has a
**Visit Site** button; the dev machine must stay awake. Public requests cannot
call live APIs or send mail. The entire application also runs locally offline.

## Run Locally

```sh
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m uvicorn earl.web:app --host 127.0.0.1 --port 8000
```

Open [localhost:8000](http://127.0.0.1:8000). No keys, JavaScript dependencies,
frontend build, or CAD account are required.

## Architecture

```mermaid
flowchart LR
    Watch[Fixture file / authenticated webhook / budgeted poll] --> Ledger[Durable agent ledger + snapshot cache]
    Ledger --> CAD
    CAD[Read-only CAD / synthetic fixture] --> Map[Declared truss map + SI contract]
    Edit[Typed change / optional LLM interpretation] --> Models
    Map --> Models[Before + after models]
    Models --> Solve[PyNite + benchmark + sanity checks]
    Solve --> Capacity[Yield / Euler capacity]
    Capacity --> Gate[Biject: code-only gate]
    Gate --> Decision[Validated Decision]
    Decision --> Report[SkyCiv adapter / explicit fixture]
    Report --> Recheck[Disagreement recheck]
    Recheck --> Record[ECN + .eml + JSONL trace]
    Record --> UI[CLI / FastAPI SSE / single HTML]
    Record --> Dedup[Code-only incident / recovery policy]
    Dedup --> Outbox[Persistent local / Gmail outbox + retries]
    Outbox --> UI
```

The model can interpret an edit, select a load case, and draft neutral prose.
It has **no merge, write, or send tool**; [the capability test](tests/test_capabilities.py)
asserts that boundary. `Decision.validate()` rejects approval below the floor
on both serialization and consumption. A solver failure is **ERROR**, not a
safety finding. No actual Onshape branch permission or merge is implemented.

## Evidence

Twenty fixed perturbations produce 18 member-failure findings. **EARL drops 0;
the rule-based selection baseline drops 10.** Both use the same validated
solver; the baseline chooses its verification scope before seeing results and
may choose all members. This measures omitted findings, not unsafe releases
or general LLM performance. [Committed results](earl/eval/cache/results.json)
power the scoreboard without a live evaluation.

```sh
python -m unittest discover -s tests -q
python -m earl eval
python -m earl run --scenario thin-compression
```

The CLI exits 0 for approval, 2 for escalation, and 1 for error, and always
attempts to write deliverables under `out/`. The original 119 offline tests
remain, with additional autonomous-operation tests. The [two-minute script](DEMO.md) gives exact
demo clicks. [RELIABILITY.md](RELIABILITY.md) records benchmark numbers,
baseline protocol, assumptions, and deployment verification.

## What Is Not Live

CAD geometry is the declared synthetic ten-bar fixture; the recorded live
Onshape document was empty. The offline SkyCiv attachment is a genuine public
example report of **a different model**, not an authenticated truss solve;
the UI explicitly says the independent cross-check was **NOT PERFORMED**.
Gmail is off and the saved `.eml` is the deliverable. Analysis is linear,
axial-only, and not AISC/ASCE code-compliant design.

The trusted watcher can request the existing SkyCiv cross-check before it
classifies notifications. `scripts/skyciv_live_selfcheck.py --allow-live
--record-fixture` verifies and records an actual response only with credentials
and `DEMO_MODE=false`; otherwise it reports **NOT VERIFIED LIVE**. That is the
result here. No real truss response or numerical agreement has been invented.

`python scripts/agent_selfcheck.py --url http://127.0.0.1:8000` exercises saved
file edits, duplicate suppression and recovery in a real browser with zero
manual-run requests. The dashboard follows the agent by default; manual
checks remain available but are not required for autonomous operation.

[Dockerfile](Dockerfile) and [Render configuration](render.yaml) are provided,
with Render explicitly selecting `astra`. The verified public deployment is
the temporary tunnel, not Render; see the reliability record for restart
instructions and deployment limitations.
