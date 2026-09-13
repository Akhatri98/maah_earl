[Submission branch: astra](https://github.com/Akhatri98/maah_earl/tree/astra)

`astra` is the working branch for this submission. Public deployment is being verified.

# EARL

EARL is CI for parametric CAD: structural regression tests with an enforced
gate and a paper trail. An engineer changes a truss; EARL re-solves it,
compares safety factors in code, holds failed changes for review, and writes
an Engineering Change Notice for the responsible human. Its contribution is
enforcement, notification, and record, not better physics.

## What Already Exists

[Onshape Simulation](https://www.onshape.com/en/features/simulation) already
provides cloud-native linear static FEA inside assemblies: stress,
displacement, factors of safety, automatically refreshed results, and versioned
simulations. [SkyCiv](https://skyciv.com/api/) already provides structural
analysis and reporting. EARL adds its numerical acceptance gate,
notification policy, and per-change decision record on top; no Onshape merge
or write capability is implemented.

## Development

```sh
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m unittest discover -s tests -q
```

The offline demo, validation details, and live URL will be documented here as
the working checkpoints complete. See [RELIABILITY.md](RELIABILITY.md).
