# Test fixtures

The test suite runs **entirely offline** against these files. No test makes a
live API call, so the suite is free to run and does not consume the Onshape
annual request budget (~2500 calls/year).

## `onshape/` — recorded, real

Actual responses from the live document, captured by
`scripts/onshape_discover.py`. They hold the **modelled 10-bar truss**: 10
parts `m1`…`m10`, 9 Fastened mates, 20 mate connectors, and the 3 variables
(`bayWidth`, `bayHeight`, `barArea`).

| File | Holds |
|---|---|
| `assemblies_d_w_e.json` | Assembly definition — instances, mates, **and all 20 mate connectors** |
| `assemblies_d_w_e_features.json` | Assembly `/features` — the BTM authoring form of the same mates |
| `partstudios_d_w_e_features.json` | Part studio features — the `tenBarTruss` custom feature |
| `parts_d_w_e.json` | Parts list, for provenance ids |
| `variables_d_w_e_variables.json` | Variable table |

### `assemblies_d_w_e.json` must keep its mate connectors

It was re-recorded with `includeMateConnectors=true`, and that is not
cosmetic. The nine mates reference only **13 of the 20** connectors — nine
Fastened mates fully constrain ten parts, so they form a spanning tree, not
the full truss. The other seven (`m3_n6`, `m4_n2`, `m5_n4`, `m6_n2`, `m8_n6`,
`m9_n2`, `m10_n1`) are real member endpoints that no mate names.

Without them, seven members have an endpoint with no coordinate and the
topology cannot be recovered — a graph quietly missing structure, which is the
dropped domino this project exists to catch. `derive_topology()` refuses to
return a partial result, and `build_graph(strict=True)` raises.

Re-record only from a document that still has the truss modelled:

```
.venv/Scripts/python.exe scripts/onshape_discover.py    # costs 5 API calls
```

## `synthetic/` — hand-built, not real

Written to Onshape's documented response shape to cover cases the real
document does not exhibit: parts shared between instances (so `where_used`
couples something), group mates, and variables with populated `value` fields.
Marked with a `_comment` field so they are never mistaken for recorded
responses.

Prefer a recorded fixture wherever the real document can produce the case — a
real response is the only thing that proves the documented shape matches what
the API actually sends.
