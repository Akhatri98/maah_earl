# `earl.eval` — the 20-scenario replay [Track A, Sprint 5A]

The measurement half of the project. `plan.md` stakes the whole claim on one
number:

> **Headline metric — dropped dominoes:** affected members whose unsafe state
> the system fails to catch or report. Target: zero.
>
> **Baseline comparison.** The identical 20 scenarios are run through a
> tool-using LLM agent with the same Onshape and PyNite access but none of the
> fixed graph traversal, code-enforced thresholds, or branch-only write
> permission. The gap between that baseline's dropped-domino count and this
> system's is the core proof point — not asserted, measured.

This package produces that measurement. The **scoring** is Track B's
(`earl.analysis.scoreboard`, Sprint 5B, already shipped); this package feeds it
`ScenarioRecord` JSON in the format `earl/analysis/README.md` documents.

| module | what |
|---|---|
| `scenarios.py` | the 20 perturbations, and the properties the set is chosen for |
| `agents.py` | the `Agent` interface and `SystemAgent` (the real pipeline) |
| `tools.py` | what the baseline gets, and precisely what it does not |
| `llm.py` | tool-calling transport: Muse, recording, replay, scripted |
| `baseline.py` | the baseline agent's loop |
| `harness.py` | ground truth, the replay, the records |

## Run it

```bash
python3 scripts/run_eval.py                          # system only, offline, no credentials
python3 scripts/run_eval.py --list                   # the scenario set
python3 scripts/run_eval.py --agents system,baseline # LIVE baseline (META_MUSE_KEY)
python3 scripts/run_eval.py --replay artifacts/eval/transcripts
python3 scripts/run_eval.py --scenario s01 --verbose
```

Exit 0 when the system dropped no dominoes, 1 if it dropped any, 2 on a
misconfiguration.

## The scenario set

Twenty scenarios covering every change kind `plan.md` names — resize (thinner
and thicker), remove, add a load, move a load, edit a variable. **Eleven are
unsafe and nine are safe** under the validated solver at threshold 1.0.

Three properties are deliberate, and `tests/test_scenarios.py` pins all three
against the real solver rather than trusting the annotations:

1. **"Escalate everything" must lose.** If no change were ever genuinely safe,
   an agent that flags all ten members every time would score zero dropped
   dominoes — a perfect headline number
   (`Plan/sprint3a_handover.md` §3). The nine safe scenarios cost that policy
   ninety false alarms.
2. **Every change kind appears both safe and unsafe**, so neither agent can do
   well by learning "removals are bad" or "thickening is fine".
3. **In ten of the eleven unsafe scenarios the failing member is not the one
   the change names.** An agent that reasons only about the entity it touched
   drops those dominoes. `s03` is the deliberate exception, kept so the set
   cannot be accused of only ever rewarding a downstream answer.

The base design is Track B's demo structure (the published Case 1 optimum with
a uniform 10 % margin): m5 governs at SF 1.100, m7 next at 1.489, everything
else ≥ 3.2. That thin margin on m5 is why a change three hops away can still
break something.

### Two traps the set is built around

- **`build_ten_bar_graph()` is not walkable.** It emits **zero edges** and
  targets `"benchmark"`, which matches no entity, so a walk over it reaches
  nothing. Every scenario here attaches the full `topology_edges()` set plus
  `LOAD_PATH` edges from a load change's node and `VARIABLE_REF` edges from a
  changed variable, and targets a real member, node or edge source.
- **`affected_member_ids` is left empty.** The fixed walk fills it inside
  `run_pipeline`. A scenario that shipped its own affected set would be marking
  its own homework — and would hand the baseline the traversal it is supposed
  to lack.

## What the baseline has, and what it lacks

Getting this split wrong in either direction ruins the experiment: starve the
baseline and the comparison is rigged, hand it Biject and there is nothing left
to compare.

**It has** the change, the full structure, every member and area, the material
**including the 25 ksi design allowable**, and the same PyNite solver on either
the before or the after state, callable as often as it likes, always returning
every member. It runs on **the same Meta Muse model as the rest of the
pipeline** — a weaker baseline would make the gap read as a model-quality
artefact rather than a structural one.

**It lacks** exactly three things, and `tests/test_eval_tools.py` asserts each
absence:

- **no fixed graph traversal** — no `walk` tool, and an empty `affected_member_ids`;
- **no code-enforced threshold** — no Biject, no safety-factor field, no
  `Decision.validate()`. It is told the rule in the prompt and must do the
  division itself; nothing stops it approving a member it just computed to be
  over its allowable;
- **no write permission** — no write tool at all. (The system's merge hook is
  likewise off during the eval; neither agent touches Onshape.)

The prompt tells it plainly that the truss is statically indeterminate, that a
change can overstress members it did not touch, that making a member bigger is
not automatically safe, and to check every member. It is written to be good,
not to be a straw man. If it still drops dominoes, that is the finding; if it
does not, the honest thing is to report that.

### The verdict-extraction pass

On the first live run the model did the engineering **correctly** — it named
m5 at SF 0.73 and explained the redistribution — and then ended its turn in
prose without ever calling `report_verdict`. Scored naively that is silence,
and it would have cost the baseline a dropped domino it had actually found.
That measures protocol compliance, not engineering judgement.

Muse accepts only `tool_choice: "auto"` (named function choice returns a 400),
so the verdict cannot be forced out of the transport. Instead, after one
nudge, a separate **transcription-only** pass reads the verdict back:

- the extractor sees **only the baseline's own prose** — no structure, no
  member list, no solver, no threshold rule, and no tool but `report_verdict`;
- it is instructed to transcribe, not analyse: exactly the members the review
  called unsafe, no more and no fewer.

It therefore cannot find a domino the baseline missed, only record one the
baseline found. Every verdict obtained this way is flagged in the transcript
(`extracted: true`), in `AgentAnswer.detail`, and in the scenario record's
note, so the scoreboard never hides how an answer was read.

## Three rules that keep the replay honest

1. **Ground truth is computed, never declared.** `scoreboard.ground_truth()` —
   validated solver over every load case, plus Biject — runs on a graph no
   agent has touched. A scenario whose truth cannot be computed is **excluded**
   and named in the summary, because scoring it as "nothing unsafe" would hand
   every agent a free scenario.
2. **Each agent gets its own build of the scenario.** The system's walk writes
   `affected_member_ids` onto the graph it is handed; share one build and the
   baseline inherits the traversal, and the comparison is void. This is the
   single most load-bearing line in `harness.py`.
3. **An agent that crashes is scored, not skipped.** An exception, a silence,
   or a turn-cap becomes an ERROR answer reporting no members, which the
   scoreboard counts as silence about every unsafe member. "I could not run"
   is a dropped domino.

## Recording and replay

A live baseline run writes every model turn to `<out>/transcripts/<scenario>.json`.
`--replay <dir>` re-runs the baseline from those files with no network at all.

Replay is **exact**, not approximate, and that is a property of the tools
rather than of the transport: every tool in `tools.py` is a pure function of
the scenario graphs, so feeding back the recorded turns reproduces the same
tool calls and the same verdict. A replay that has drifted raises rather than
inventing a turn. That is what lets the demo show real measured numbers without
a network call on stage.

## The result of the first full live run

Twenty scenarios, both agents, Muse `muse-spark-1.1`, threshold 1.0:

| agent | scenarios | unsafe members | caught | dropped dominoes | false alarms | recall | precision |
|---|---:|---:|---:|---:|---:|---:|---:|
| system | 20 | 22 | 22 | 0 | 0 | 1.00 | 1.00 |
| baseline | 20 | 22 | 22 | 0 | 0 | 1.00 | 1.00 |

**The baseline tied the system, scenario for scenario and member for member.**
This is a negative result for `plan.md`'s core proof point, and it is recorded
here rather than tuned away.

### Why, and it is not that the baseline got lucky

The transcripts show a consistent, competent strategy in all twenty runs:
`get_change`, `get_structure`, `list_members`, `get_material`, then
`solve_load_case` on the before state and again on the after state — about
seven tool calls, two solves, every time. It then compared all ten members
against the allowable.

It never needed a dependency walk, because **one `solve_load_case` call
returns every member's stress**. On a six-node, ten-member truss with a single
load case, "check everything downstream" and "check everything" are the same
action, and the second is one tool call. Enumeration is free, so fixed
traversal buys nothing measurable.

That is a property of the demo structure, not a flaw in the baseline's tools:
the system's own gate also solves every member, so the bulk solver is exactly
the "same PyNite access" `plan.md` specifies. Handing the baseline a
one-member-at-a-time solver instead would manufacture a gap by handicap.

### What this does and does not license the project to claim

Not supported by this experiment:

- that fixed graph traversal catches dominoes an LLM misses — **on this
  structure, at this scale, it does not**.

Still supported, and still visible in the same run:

- **the system is right every time, by construction.** 22/22 with zero false
  alarms, from a fixed algorithm rather than a model that happened to be
  careful.
- **the system is deterministic.** The walker has a test pinning byte-identical
  output across runs; the baseline has no such guarantee, and did in fact vary
  — on one run of `s01` it completed the analysis correctly and never called
  `report_verdict`, and `s09` needed the extraction pass.
- **the threshold is enforced in code.** An LLM can approve a member it has
  just computed to be over its allowable; `Decision.validate()` raises rather
  than let that through. Untested by this scenario set.

### Where a real gap might still be

Three candidate axes, none of them measured yet:

1. **Consistency across repeated runs.** `plan.md` itself says the baseline
   should be "missing something *or inconsistent across runs*". Running each
   scenario N times and scoring answer stability needs no scenario redesign,
   and there is already anecdotal variance.
2. **Scale.** Traversal earns its place when a structure is too large to dump
   and check exhaustively. The 10-bar truss is not.
3. **Enforcement under pressure.** A scenario engineered to tempt an approval
   just below threshold tests the one guarantee the baseline structurally
   cannot make.

## Output

```
artifacts/eval/
  records.json        the ScenarioRecord interchange (scoreboard input)
  scoreboard.json     the folded scores
  scoreboard.md       the demo slide: the table, the split, exclusions
  transcripts/        one per scenario, on a live baseline run
```
