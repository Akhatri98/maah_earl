# Onshape Setup for EARL

Onshape Simulation already provides automatically updated assembly FEA and
versioned results; SkyCiv already supplies analysis/reporting. EARL adds an
autonomous code gate, notifications and a per-edit record, not better physics.
**The shipped live recording is empty. No populated live CAD ingestion or
authenticated webhook was verified in this build.** Fixture mode is verified.

## Model Convention

Use one assembly with ten active, top-level instances named `Member_m1`
through `Member_m10`. Case, surrounding whitespace, `Member m8`, and Onshape's
occurrence suffix (`Member_m8 <1>`) are tolerated. Duplicate mappings are an
error. Suppressed/missing instances are removed members; reintroducing a
declared member is supported. Unmapped instances log a warning and hold live
verification as ERROR, rather than silently approving an incomplete model.

EARL uses the declared topology in `earl/ingestion/truss_map.json` and
`scripts/contract_selfcheck.py`, **not part transforms or inferred CAD joints**:

| Member | Nodes | Member | Nodes |
|---|---|---|---|
| m1 | n5-n3 | m6 | n1-n2 |
| m2 | n3-n1 | m7 | n5-n4 |
| m3 | n6-n4 | m8 | n6-n3 |
| m4 | n4-n2 | m9 | n3-n2 |
| m5 | n3-n4 | m10 | n4-n1 |

Nodes are n5=(0,h), n6=(0,0), n3=(w,h), n4=(w,0), n1=(2w,h), n2=(2w,0).
n5/n6 are pinned wall supports; two downward point loads act at n2/n4.
Cross-sections are declared solid circles. Your CAD must represent this
structure and link its dimensions to the same variables; renaming arbitrary
parts to these names does not make the analysis physically applicable.

In the configured Variable Studio or Part Studio, expose these evaluated
variables (Onshape responses supply SI values regardless of display units):

| Name | Type | Demo value | Meaning |
|---|---|---|---|
| bayWidth | LENGTH | 9.144 m | Each of two bays |
| bayHeight | LENGTH | 9.144 m | Truss height |
| barArea | AREA | 20 in^2 | Default solid-round section |
| safetyFactor | NUMBER | 1.5 | Design target, never the hard floor |
| pointLoad | FORCE | 20 kN | Each downward load; zero is allowed |
| area_m1 ... area_m10 | AREA | Optional | Per-member override of barArea |

The first three variables are required. A per-member resize should change
`area_m8` and the CAD section it drives together. Batch dimension/area/load
edits are supported; material, restraint or acceptance-policy changes require
explicit reconfiguration and otherwise produce ERROR. This is linear axial
analysis with ideal Euler buckling, not AISC/ASCE-compliant design.

## Trusted Operator Setup

Set the API key pair, document/workspace IDs, `ONSHAPE_ASSEMBLY_ID`, and
`ONSHAPE_VARIABLE_ELEMENT_ID` using `.env.example`. Copy IDs from the document
and tab URLs; EARL does not repeatedly discover tabs. Set
`ONSHAPE_CALLS_USED` to existing account usage and `ONSHAPE_CALL_BUDGET` to a
conservative allowance. The counter reserves attempts before network calls,
survives restart, and never automatically resets at New Year.

Prefer webhooks. Configure a random `ONSHAPE_WEBHOOK_SECRET` of at least 32
characters. The documented `data` field echoes this bearer token over HTTPS;
it is not a signature. Company accounts may additionally configure the
documented HMAC signing key in Onshape and `ONSHAPE_WEBHOOK_SIGNING_SECRET`.
No invented auth option is sent in webhook registration.

Start the callback server with `DEMO_MODE=false` and
`EARL_ALLOW_LIVE_WEBHOOKS=true` explicitly. Use a public HTTPS callback URL.
The browser's manual demo routes still cannot call live integrations.

```sh
DEMO_MODE=false EARL_ALLOW_LIVE_WEBHOOKS=true python -m uvicorn earl.web:app --host 127.0.0.1 --port 8000
DEMO_MODE=false python -m earl watch --mode webhook --allow-live --register-webhook https://YOUR-HOST/api/webhook/onshape
```

Registration/ping callbacks return 200 without solving. The watcher consumes
the durable queue and coalesces bursts; snapshot reads default to at most once
per 60 seconds. First use verifies the current snapshot, not invented prior
history. Later runs compare persisted and newly observed immutable snapshots.
The API does not include a microversion in this event, so each consumed batch
first reads `currentmicroversion`, then fetches assembly and variables only
when changed. This verifies observed states, not every rapid intermediate edit.

Use `--list-webhooks` or `--unregister-webhook ID` with the same live flags for
administration. Registrations are non-transient, checked once daily, and
recreated if remotely removed; explicit unregister disables that renewal.
Poll fallback defaults to six hours, with a one-hour minimum. Four checks/day
alone cost 1460 calls/year; each changed snapshot needs two additional reads.
Missing credentials, unavailable APIs, or exhausted budget explicitly select
offline fixture behavior. No fixture is labeled live or sent to a live owner.

## Verified Contract Sources

Checked September 13, 2026: [current OpenAPI](https://cad.onshape.com/api/openapi)
reports server `/api/v17`, revision `1.220.87929-d54ca734df42`.
[Webhook guide](https://onshape-public.github.io/docs/app-dev/webhook/),
[signature documentation](https://cad.onshape.com/help/Content/Plans/enterprise_settings_webhooks.htm),
and [annual limits](https://onshape-public.github.io/docs/auth/limits/) informed
the implementation. Documentation-shaped tests are not live integration evidence.
