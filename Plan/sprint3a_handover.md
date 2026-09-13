# Sprint 3A handover — ECN template

**Track A → Track B.** Updated after Track B's Sprint 1B–3B merge (`0a801d4`,
`637e6a0`).

Sprint 3A is done: `earl/delivery` builds an ECN from a `Decision`. It has now
been run against **real `run_fast_gate()` output**, not just the mock, and works
end to end with no changes needed on either side. 503 tests pass across both
tracks.

Gmail send is deliberately **not** built yet.

```
.venv/Scripts/python.exe scripts/ecn_preview.py            # all 5 scenarios
.venv/Scripts/python.exe scripts/ecn_preview.py escalated  # the demo one
.venv/Scripts/python.exe scripts/ecn_preview.py escalated --bare
.venv/Scripts/python.exe -m unittest discover -s tests -t . -q
```

---

## 1. BLOCKER — the two tracks disagree about member capacity by 2x

This is the one that needs a decision before the demo.

| | Field | Value | Source |
|---|---|---|---|
| `earl/ingestion/benchmark.py:135` | `Material.yield_strength` | **25 ksi** | the benchmark's published *allowable* stress |
| `earl/analysis/benchmark.py:78` | `Material.yield_strength` | **50 ksi** | nominal 2024-T3 *yield* |

Both are deliberate, both are defensible in isolation, both are "a stress in Pa"
in the same contract field — and `biject.member_capacity()` multiplies whichever
it is given by the area to get capacity. So **every safety factor in the system
depends on which graph builder produced the graph, and the two differ by exactly
2x.** That is the Pa-vs-MPa silent failure the contracts README warns about, one
level up, and it is currently live inside the project.

### Why 25 ksi is the right number for the pipeline

At the published optimum areas, capacity = 25 ksi puts m5 at a safety factor of
**exactly 1.000** — the active stress constraint, precisely as the literature
reports. At 50 ksi the same design reads **2.001**, i.e. a stress-constrained
optimum with no active constraint, which is structurally meaningless: if nothing
binds, the optimizer would have shrunk the members further.

Engineering practice points the same way — an ASD-style check is against an
allowable that already embeds the margin, not against raw yield.

### What it costs the demo

Measured, not asserted — the identical change through `run_fast_gate()`:

| Scenario | at 25 ksi | at 50 ksi |
|---|---|---|
| Baseline, optimum areas | approved, m5 **SF 1.000** | approved, m5 SF 2.001 |
| **m7 7.46 → 6.00 in²** (the demo) | **escalated**, m5 **SF 0.751** | **approved**, m5 SF 1.503 |
| m9 21.53 → 12.00 in² | **escalated**, m5 SF 0.995 | **approved**, m5 SF 1.990 |

At 50 ksi **both escalation scenarios become auto-approvals** and the
dropped-domino demo disappears.

### Proposed fix

`Material` gains an optional allowable, and capacity prefers it (additive, minor
version bump — nothing breaks if it is absent):

```python
# contracts/graph.py
@dataclass
class Material:
    allowable_stress: float | None = None   # design allowable; capacity uses this when set
```

`member_capacity()` then uses `allowable_stress or yield_strength`. That keeps
`yield_strength` honestly meaning yield (your point) while making the design
constraint explicit (my point), and removes the ambiguity rather than picking a
winner.

**Zero-contract-change alternative:** both tracks put 25 ksi in `yield_strength`
and the field comment is amended to say it carries the design allowable. Less
clean, but it works today — and it is what `earl/ingestion/benchmark.py` already
does.

Either way, **the two builders must stop disagreeing.** Your call which.

---

## 2. Good news — the physics cross-validates

Independently of the above: I solved the truss with a direct-stiffness
implementation written from scratch (no PyNite), and `run_fast_gate()` agrees on
member stress to **five significant figures** — m5 after the change is
`2.2937e+08 Pa` from both.

That solve also reproduces the published Haftka & Gürdal optimum weight of
5060.9 lb to within 0.1 lb. So the solver and the whole stress path are sound;
the 2x is purely the capacity convention, nothing physical.

---

## 3. Partly resolved — per-member areas

My earlier note asked for per-member areas, since a uniform 1 in² makes every
member fail before any change (safety factors 0.12–0.70) and the approve path
never fires.

`build_ten_bar_graph(areas_in2=[...])` already does this — one `Section` per
member, exactly the shape needed. Two things remain:

- its **default is still uniform 1.0 in²**, which is the degenerate case;
- `graph_builder.build_graph()` (Track A, the Onshape path) still assigns one
  shared section to all ten members, so it cannot yet express the optimum. That
  is my fix and I will make it.

Worth restating the eval consequence: if no change is ever genuinely safe, a
baseline agent whose policy is "escalate everything" scores **zero dropped
dominoes** — a perfect score. The headline metric only separates the system from
the baseline when some changes are safe and some are not.

---

## 4. Integration path — which graph feeds Sprint 4

`build_ten_bar_graph()` emits **0 edges**, and its default `change.target_id` is
`"benchmark"`, which matches no member. The walker therefore reaches nothing:

```
change target 'benchmark' matches no member, no node, and no edge source;
the walk has nothing to start from and would report nothing affected
```

That is correct for what your helper is *for* — you need geometry and loads to
solve, not a dependency graph. But it means the Sprint 4 graph must come from
Track A (`graph_builder.build_graph()` from Onshape, or `mock_graph()`), not
from the benchmark helper. Flagging so we do not wire it the wrong way round.

Rendered against a Track B graph the ECN says `not reached by the dependency
walk` on every row — honest, and exactly the degradation it is designed for, but
not what you want on stage.

---

## 5. Still open from before — `Decision` cannot write a full ECN alone

`earl/contracts/decision.py` states the design rule:

> this object must be SUFFICIENT TO WRITE THE ECN on its own

Two things a real ECN wants are not on it:

1. **The Onshape source.** `OnshapeRef` lives only on `DependencyGraph`. An ECN
   that cannot name the branch the change was evaluated in is missing the
   sandbox-not-main claim the project makes.
2. **How each member came to be affected.** `walker.Reach` carries `distance`
   and `via`; `MemberResult.is_affected` is a bare bool, and on a statically
   indeterminate truss every member is affected, so that bool carries nothing.

`build_ecn()` takes graph and walk as **optional** arguments and degrades
honestly, so nothing is blocked. Compare `ecn_preview.py escalated` against
`--bare` to see the cost: Source empties, and provenance drops from
`1 hop via topology` to `downstream`.

Proposed amendment (additive):

```python
# contracts/decision.py
class Decision:
    source: OnshapeRef | None = None       # denormalized, as change_description already is

class MemberResult:
    hops_from_change: int | None = None    # walker.Reach.distance
    reached_via: EdgeKind | None = None    # walker.Reach.via
```

Track B populates `source` by copying it off the graph it was handed, and the
two `MemberResult` fields from `WalkResult.reach(member_id)` — no new
computation.

**If you'd rather not touch the contract,** say so and I'll amend the design-rule
comment instead. The current wording claims a sufficiency the object does not
have, and that is worse than either fix.

---

## 6. Smaller things

- **`earl/ingestion/benchmark.py`'s docstring is stale.** It says
  `contract_selfcheck.py` "currently labels E = 6.895e10 Pa as A36 Steel … left
  alone pending agreement". Fixed in `a347695`. Docs only.
- **The ECN shows both unit systems** — `229.4 MPa (33.27 ksi)`. Output is ASCII
  (`in^2`, not `in²`) because the Windows console codepage mangles it.
- **Non-SI payloads are refused, not converted** — `build_ecn()` calls
  `units.assert_si()`; conversion stays the producer's job.
- **What the ECN needs from SkyCiv:** `SkyCivReport.url` *or* `local_path`. Given
  only `report_id`, it renders a warning that it cites evidence a reviewer cannot
  open. An escalation with no report, or no cross-check, is called out rather
  than left blank.
- **Cross-check disagreement is surfaced, not buried** — see
  `ecn_preview.py cross_check_disagreement`.

---

## What Sprint 4 wires up

Replace `mock_decisions` with real `run_fast_gate()` output — already proven to
work — plus `artifacts.escalate()` to populate `skyciv_report` and `cross_check`.
`build_ecn()` needs no changes: it calls `Decision.validate()` on the consume
side and refuses to render an approval the decision did not make.
