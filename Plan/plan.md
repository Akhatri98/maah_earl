# EARL: CI for Parametric CAD

EARL adds structural regression tests, an enforced gate, notification, and an
auditable change record to parametric CAD. An engineer proposes a truss change;
the solver computes before and after results and deterministic code applies
the policy. EARL's contribution is enforcement, notification, and record, not
better physics.

Onshape already provides cloud-native Simulation inside assemblies, including
linear static FEA, stress, displacement, factors of safety, automatic updates,
and versioned simulation data. SkyCiv already provides structural analysis and
reports. EARL adds an explicit threshold gate, responsible-human notification,
and a decision record to that existing workflow; it does not invent FEA or
version history.

## Controlling Scope

The user's one-shot build brief supersedes the original sprint proposal.
All work and deployment are on `astra`. Never commit to or merge into `main`,
and never change the default branch. The live Onshape document was empty when
the original fixtures were recorded. The demo uses labeled synthetic fixtures.

## Pipeline

`ingest -> map -> build before/after -> solve -> sanity -> capacity -> gate -> artifact -> notify`

- Read Onshape with a local fixture fallback. No Onshape write APIs exist.
- Map declared instance names to the canonical ten-bar truss from
  `scripts/contract_selfcheck.py`; instance IDs stay inside ingestion.
- Apply a typed delta to a copy of one before-model.
- Solve axial forces and displacements with PyNite; check equilibrium and
  load linearity. An analysis failure is `ERROR`, never a safety finding.
- Capacity includes yield and Euler compression buckling assumptions.
- Biject compares capacity/demand with hard floor 1.0 and design target 1.5.
  Neither a model nor prose can change the verdict.
- Reference a SkyCiv report when available; show fixture provenance otherwise.
- Produce an ECN and `.eml` locally. Gmail sends require configured credentials
  and non-demo operation. A public visitor cannot trigger live APIs.

## Evidence and Demo

The benchmark is statically indeterminate to degree two. A local edit can
redistribute forces across every member. Traversal gives a conservative
superset, not a completeness claim; the solver evaluates and the gate enforces.
Twenty committed scenarios compare EARL with a member-selecting baseline using
the same solver. The baseline protocol is recorded before evaluation in
`RELIABILITY.md`; missing API keys must not be disguised as an LLM run.

The demo is two minutes: change a section, run the pipeline, show the numerical
escalation and saved ECN/email, and close on the cached dropped-domino count.
The frontend is one static HTML file with inline SVG and vanilla JavaScript.
No bundler, external asset dependency, CAD branches, or CAD merge operations.

See `RELIABILITY.md` for validation numbers and limitations, and `DEMO.md` for
the exact rehearsal clicks.
