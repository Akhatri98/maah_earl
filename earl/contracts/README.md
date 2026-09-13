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

## Version policy

`CONTRACT_VERSION` is stamped onto every payload as `schema_version`.

- **Minor** bump — additive, backward-compatible (a new optional field).
- **Major** bump — breaking (renamed/removed field, changed meaning). Tell the
  other track before you push it.

## Known open question

`EdgeKind` (`MATE`, `WHERE_USED`, `VARIABLE_REF`, `TOPOLOGY`, `LOAD_PATH`) is a
first guess made **before** seeing real Onshape assembly data. Expect it to be
amended in Sprint 1A/2A once the actual mate and where-used payloads are known.
It is additive-only if the existing members keep their meaning.
