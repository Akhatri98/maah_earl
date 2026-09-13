# EARL Reliability Record

EARL is CI for parametric CAD. Onshape Simulation already provides
cloud-native, automatically updated assembly FEA, stress, displacement,
factors of safety, and versioned simulation data. SkyCiv already provides
structural analysis and reports. EARL adds an enforced comparison policy,
responsible-human notification, and a per-change decision record. It does not
claim better physics or invent version history.

## Build Provenance

Work began on `astra`, containing the same commit as `dev` (`2cd8fdf`). The
original 50 tests passed before contract changes. Only `astra` is a submission
or deployment branch; `main` and the default branch are not changed.

## Baseline Protocol (Specified Before Evaluation)

1. Freeze twenty scenario definitions before collecting either system's
   results. Both systems receive the same canonical model, typed edit, load
   cases, material/section properties, and numerical verification tool.
2. The baseline must choose which surviving members to verify. It may select
   any subset, including all members, with no artificial budget or cap. It
   receives geometry and the edit, not ground-truth failures or EARL's solved
   results. A language model only selects members, never judges safety.
3. With a configured provider, ask the selector to return member IDs as
   structured data and cache its selection. Without a provider or on any
   failure, use a declared deterministic local-selection policy: for a resize
   or removal, select the changed member and members incident to its two
   endpoints; for a load edit, select members incident to the changed load's
   old/new nodes. A whole-structure variable change selects all members.
   Label these runs **rule-based baseline**, never measured LLM behavior.
4. A statically indeterminate truss cannot be physically solved one bar at a
   time. The tool must assemble/solve the full equilibrium system even for a
   subset request; only selected member stress recovery, capacity checks, and
   reports are exposed to the baseline. Selection never deletes unselected
   bars from the structural model. Both systems use the same solver/capacity
   implementation. This comparison measures omissions in verification scope,
   not speed or superior numerical accuracy.
5. Code applies the same design target and hard floor to returned numbers.
   A baseline that selects all members can score zero. There is no prompt
   asking a model whether a structure is safe, and no model-written verdict.
6. Ground truth is an offline full-structure solve with the validated solver,
   frozen to committed JSON. A **dropped domino** is a surviving member that
   fails the design target after the edit and is absent from the system's
   failure report. Report newly failing members (passed before, fail after)
   separately; initial demonstrator cases are expected to pass. Solver errors
   are counted separately and never relabeled as structural failures.
7. Cache scenario inputs, selected IDs, reported failures, per-member ground
   truth, outcomes, and counts for every run. The served scoreboard uses the
   committed cache; regeneration requires no network. This is one fixed
   benchmark family, not a claim of general LLM reliability.

The ten-bar benchmark is statically indeterminate to degree two (10 bars + 4
restrained translations - 2*6 joints = 2). An edit can redistribute forces
through all members. A dependency traversal produces a conservative superset;
the solver decides and the gate enforces. Traversal completeness is not our
experimental claim.

## Explicit Assumptions and Choices

- Geometry/connectivity/restraints are reused from
  `scripts.contract_selfcheck.build_graph()`, not independently reconstructed.
  The benchmark has two pinned wall supports. The viewer must depict actual
  restraints; generic roller drawing support does not change physics.
- `earl/units.py` owns conversions. Original rounded SI constants are retained
  for existing tests: `E=6.895e10 Pa`, `P=444822 N`, one inch `0.0254 m`.
  `E_STEEL` is a legacy alias for the aluminium-like benchmark modulus, not a
  steel certification. The illustrative demo yield limit is `248 MPa`.
- A section declared `solid_round` uses `Iy=Iz=A^2/(4*pi)` and `J=2*Iy`.
  Resizing is geometrically similar, so second moments scale with area squared.
  Area alone cannot identify a real CAD section: the shape is declared
  in `truss_map.json`, not inferred by an LLM.
- Euler compression buckling assumes ideal straight, pin-ended members,
  effective length factor `K=1`, full unbraced member length in both directions,
  and the weaker principal second moment. Compression capacity is the smaller
  of `Fy*A` and `pi^2*E*Imin/(K*L)^2`; tension capacity is `Fy*A` because Euler
  buckling does not apply to tension. Slenderness is `K*L/sqrt(Imin/A)`.
- This is linear elastic, axial-only, small-displacement 2D truss analysis,
  with simplified point loads. No plasticity, imperfections, joint strength,
  local buckling, fatigue, dynamics, code load combinations, or AISC/ASCE
  compliance is claimed. Euler/yield minima do not implement an inelastic
  column curve or a building-code design check.
- The original synthetic parser fixture has only two named bars, a gusset,
  and a spare. A separate full ten-member fixture is explicitly synthetic.
  The demo starts with 20 in^2 solid round sections and two 20 kN downward
  loads, so ordinary controls can exercise both approval and escalation.
  These are demonstrator parameters, distinct from benchmark validation.
- The evaluated SI Onshape variable table supplies dimensions and section
  area; `safetyFactor` is a design target. `hard_floor` is at least 1.0 and
  cannot be weakened by a payload or environment setting. The legacy
  `safety_factor_threshold` can tighten policy, never weaken the floor.
- Raw Onshape instance IDs remain in ingestion-local `InstanceEdge` objects.
  Only mapped node/member IDs enter `DependencyGraph`. Unknown instances are
  logged and included in graph warnings. An empty live document does not
  manufacture geometry; demo ingestion explicitly chooses synthetic data.
- `.eml` is a real saved deliverable, not proof of email delivery. Public
  demo mode cannot send Gmail or make live CAD/LLM/SkyCiv requests. No CAD
  branch creation, merge, or write implementation is part of this project.

## Verification Status

The contract/mapping checkpoint passes 62 offline tests, including the original
50. Solver reference numbers, sanity measurements, eval counts, integration
provenance, and deployment verification will be filled in after measurement.

## External-Service Reality

The checked-in `tests/fixtures/onshape/` responses are actual recordings of an
empty document. `tests/fixtures/synthetic/` is hand-authored test/demo data.
No SkyCiv, Gmail, or LLM credential is assumed. A fixture or locally generated
artifact must never be presented as a real SkyCiv solve or a sent message.

Reference for the overlap: [Onshape Simulation](https://www.onshape.com/en/features/simulation).
