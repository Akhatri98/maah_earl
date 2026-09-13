# EARL shared contracts (Sprint 0)

The agreed boundary between the two tracks. **Owned by neither track** — change
it only by agreement, and bump `CONTRACT_VERSION` in `common.py` when you do.

```
Track A  ──[ DependencyGraph ]──>  Track B
Track A  <──[ Decision ]─────────  Track B
```

| Contract | File | Producer | Consumer |
|---|---|---|---|
| `DependencyGraph` | `graph.py` | `earl.ingestion` (Onshape) — Track A | `earl.analysis` (PyNite/Biject) — Track B |
| `Decision` | `decision.py` | `earl.analysis` + `earl.artifacts` — Track B | `earl.delivery` (ECN/Gmail) — Track A |

Verify both at any time:

```
.venv/Scripts/python.exe scripts/contract_selfcheck.py
```

## The two design rules

**1. `DependencyGraph` must be sufficient to build the FEA model.**
Track B should never have to call back into Onshape for a coordinate, an area
or a load. If Track B needs a value in order to solve, it belongs on the graph.

**2. `Decision` must be sufficient to write the ECN.**
Track A should not have to re-join against the graph to find out what changed,
which is why `change_description` is denormalized onto the decision.

## Units are not optional

Every payload carries a `Units` block, defaulting to **SI (m, N, Pa, m²)**.

Two independently built halves that disagree about Pa vs. MPa produce a
plausible-looking number that is wrong by 10⁶ — exactly the silent failure this
project exists to catch. Consumers must check `units`, not assume them.

Note the friction this creates with validation: the 10-bar truss benchmark is
published in **imperial** (360 in bays, E = 1×10⁷ psi, P = 100 kips). Convert
once, at a single agreed place, rather than half-converting in two.

## The guardrails are in the contract

`Decision.validate()` enforces what `plan.md` says must hold *regardless of
what any model in the pipeline decided*. It raises on:

- `APPROVED` while any member is below `safety_factor_threshold`
- `violating_member_ids` disagreeing with the actual member results
- a member marked `PASS` while below threshold
- `ESCALATED` with no member below threshold (escalation needs a real number)
- `ERROR` with no `error_message`

Call it at the boundary — on produce *and* on consume. It is deliberately cheap
so there is no reason to skip it. This does not replace Biject; it is the
backstop that stops a malformed verdict from travelling onward to become an
auto-merge and an ECN saying everything is fine.

`Outcome.ERROR` is distinct from `ESCALATED` on purpose: a solver that failed
to run is not the same as a structure that failed a check, and collapsing them
would let a crash read as a safety finding.

## Changelog

**0.2.0 — Sprint 4 (additive; every 0.1.0 payload still decodes).**

| Field | Producer | Why |
|---|---|---|
| `Material.allowable_stress: float \| None` (+ `Material.design_stress` property) | both graph builders | The two tracks disagreed by exactly 2x about what belonged in `yield_strength` (25 ksi allowable vs 50 ksi yield). Now `yield_strength` means yield (SkyCiv, buckling) and Biject computes capacity from `design_stress` = the allowable when set, else yield. `DependencyGraph.validate()` rejects a non-positive allowable or one above yield. |
| `Decision.source: OnshapeRef \| None` | `earl.pipeline.enrich_decision` | The ECN must name the branch the change was evaluated in; it lived only on the graph. |
| `MemberResult.hops_from_change: int \| None`, `MemberResult.reached_via: EdgeKind \| None` | `earl.pipeline.enrich_decision` (from `walker.Reach`) | `is_affected` is a bare bool and on an indeterminate truss every member is affected; the ECN needs distance and edge kind. |

Also documented on `MemberResult` (no field change): axial force and stress are
tension-positive, and `capacity` is a **stress** (design stress, or Pcr/A when
buckling governs) so `capacity / |stress_after| == safety_factor`.

With these, rule 2 above holds literally: `build_ecn(decision)` with no graph
and no walk produces a notice with the Onshape source and per-member
provenance (`tests/test_pipeline.py::TestEnrichDecision`).

## Version policy

`CONTRACT_VERSION` is stamped onto every payload as `schema_version`.

- **Minor** bump — additive, backward-compatible (a new optional field).
- **Major** bump — breaking (renamed/removed field, changed meaning). Tell the
  other track before you push it.

## Formerly open question — resolved

`EdgeKind` (`MATE`, `WHERE_USED`, `VARIABLE_REF`, `TOPOLOGY`, `LOAD_PATH`) was a
first guess made before seeing real Onshape assembly data. Sprints 1A/2A kept
all five: `graph_builder` emits `MATE` (from Onshape mates), `TOPOLOGY`
(members sharing a node, derived from mate connectors) and `VARIABLE_REF`
(from the changed variable to what it drives); Track B's demo graph emits
`TOPOLOGY` and `LOAD_PATH`. `WHERE_USED` is emitted by `where_used_edges()` for
documents with shared parts. No amendment was needed.
