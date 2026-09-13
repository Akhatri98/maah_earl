# SkyCiv fixtures

The test suite runs **entirely offline**. SkyCiv API calls are metered, so no
test ever talks to `api.skyciv.com`; `SkyCivClient` takes an injectable
`transport` (and `downloader`) and the tests feed it these files instead.

## `synthetic/` — hand-built, not real

Written to the SkyCiv v3 response shape *as remembered from the docs* — the
docs site was not reachable when this was written, so the request payload and
these responses are **unverified against the live API**. Every file carries a
`_comment` field saying so, so it can never be mistaken for a recording.

| file | what it exercises |
|---|---|
| `solve_response.json` | happy-path call 1: session.start, model.set, model.solve, getReport, design.getInput; the 10-bar truss forces in kN, with `axial` deliberately given in every shape `parse_member_results` tolerates (scalar, flat list, `[position, value]` pairs, dict) and both str/int member keys |
| `solve_response_list_combos.json` | solve data as a **list** of combination objects, forces only |
| `design_check_response.json` | call 2: `S3D.design.member.check` per-member ratios |
| `solve_failed_response.json` | `S3D.model.solve` with `status != 0` (a fatal function) |

## `recorded/` — real, once the smoke test has run

`scripts/skyciv_smoke.py` analyses the 10-bar graph once against the live API
with `SKYCIV_API_USERNAME` / `SKYCIV_API_KEY` from `.env`, records both raw
responses here as `skyciv_analyze_1.json` and `skyciv_analyze_2.json` (via
`SkyCivClient(record_dir=...)`), and prints SkyCiv's axial forces next to
PyNite's plus the raw result shape it saw. It costs 1–2 API calls and is
never run by the tests.

Once recordings exist, prefer them over the synthetic files: a real response
is the only thing that proves the documented shape (function names, restraint
codes, the sign of `axial`, where the report link lives) matches what the API
actually sends. Anything that turns out different is corrected in the
constants block at the top of `earl/artifacts/skyciv_client.py`.
