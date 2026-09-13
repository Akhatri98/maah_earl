# Test fixtures

The test suite runs **entirely offline** against these files. No test makes a
live API call, so the suite is free to run and does not consume the Onshape
annual request budget (~2500 calls/year).

## `onshape/` — recorded, real

Actual responses from the live document, captured by
`scripts/onshape_discover.py`. **The document was empty when these were
recorded** (no parts, no variables, no mates — `defaultFeatures` is just the
stock Origin/Top/Front/Right planes).

They are kept precisely because they are empty: they pin the empty-document
behaviour, so a parser can never start inventing structure that isn't there.

Re-record only when the truss has actually been modelled:

```
.venv/Scripts/python.exe scripts/onshape_discover.py    # costs 5 API calls
```

## `synthetic/` — hand-built, not real

Written to Onshape's documented response shape so the parsers can be proven
against *populated* data before the CAD exists. Marked with a `_comment` field
so they are never mistaken for recorded responses.

Once the truss is modelled, prefer a recorded fixture over these — a real
response is the only thing that proves the documented shape matches what the
API actually sends.
