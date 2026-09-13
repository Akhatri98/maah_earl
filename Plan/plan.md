# EARL
### Engineering Analysis & Rating Ledger

Your own pocket structural engineer — catches what a design change breaks before it ships, and keeps the paper trail to prove it.

## Abstract

**What will you build?**
An agent that catches structural consequences an LLM alone would miss. When an engineer changes a CAD design — resizing a member, removing a part, adding a load — the agent traces every downstream part that depends on it, runs a fast internal structural check to decide whether the change is safe, and, when something needs a human decision, generates a trusted, professional-grade analysis report and change record for review. Non-critical changes are approved automatically; anything that violates a safety threshold is escalated with real computed numbers attached, never silently approved or silently missed. The core claim is measurable: replaying a set of test changes against a validated structural model, the agent should catch every unsafe change (zero "dropped dominoes"), while a bare LLM given the same CAD access measurably does not.

**Which 3+ external apps will you connect?**
1. **Onshape** — the CAD platform. Source of the design geometry, the dependency structure (mates, "where used" references), and the target for reading parametric changes.
2. **SkyCiv** — a cloud structural analysis and design platform used by practicing structural engineers. Generates the trusted, professional-grade report for anything escalated, and independently cross-checks our own fast internal analysis.
3. **Gmail** — delivers the generated report and change record to the responsible engineer when a decision is needed.

---

## The problem

In an assembly, changing one part can silently break another: a bracket no longer fits, a fastener clashes, a load path exceeds a member's capacity. Today, an engineer has to manually re-check every downstream part by hand, and it's easy to miss one that isn't obviously connected. The failure mode that matters isn't "the agent didn't know what to do" — it's the **silent** one: a change ships, and nobody finds out it broke something until later.

## What we're building

A change-propagation and verification agent scoped to one platform (Onshape) and one class of structure (trusses), so the pipeline can be built deep instead of broad, with a four-stage architecture where each stage has one clear job.

### Pipeline

**1. Ingestion — Onshape**
A parametric change lands in Onshape — a variable in the variable table changes, or a feature is edited (member length, cross-section, an added load). The agent reads the assembly's mate structure and "where used" references via the Onshape REST API to build a dependency graph of every member and node downstream of the change. Evaluation happens in an Onshape **branch**, not the main workspace — a free, resettable sandbox, so nothing is committed until it's verified.

**2. Fast internal analysis — LLM + PyNite**
This is the cheap, fast, always-on gate that runs on every change. The LLM orchestrates — deciding what changed, what load case applies, how to set up the model — while **PyNite**, an open-source Python FEA library, computes the actual member stress, deflection, and safety factor: deterministic, reproducible, matrix-stiffness-method linear algebra, not a language model's estimate. This stage produces the immediate act-vs-escalate decision, without hitting any external paid service on every minor edit.

**3. Trusted artifact creation — SkyCiv**
Runs only when a change is escalated (or optionally on every approved change, for a full paper trail). SkyCiv is real commercial structural-analysis software used by practicing engineers, so its output does two jobs at once:
- It generates the **actual report** engineers would recognize — a real analysis with code-style design checks (e.g. AISC), not a homemade template.
- Because SkyCiv runs its own independent analysis, it **cross-checks** the fast internal PyNite gate. Agreement between the two is evidence the fast gate isn't quietly wrong; disagreement is itself a useful signal, and worth surfacing rather than hiding.

**4. Sharing — Gmail**
Delivers the SkyCiv report and a lightweight Engineering Change Notice (ECN) to the responsible engineer. The ECN is generated directly by the agent from the pipeline's own decision (what changed, what's affected, approved or escalated) and references the SkyCiv report as its supporting evidence — mirroring how a real ECN in practice typically attaches a calc report rather than containing the numbers itself.

**Decision logic (applies at stage 2, enforced regardless of stage 3):**
Each affected member is checked against a safety threshold by a verification layer ("Biject," our own module, not a third-party product), which enforces non-negotiable rules — e.g., a safety factor below 1.0 can never be auto-approved — regardless of what any model in the pipeline decides. Members within threshold: auto-approved, branch merges, ECN logged. Members below threshold: nothing merges; the change is held pending a human decision, and stages 3–4 run to produce the report that decision is based on.

## Why real physics, not just a smarter prompt

An LLM given the same Onshape and PyNite tool access could, in principle, do this correctly — but nothing guarantees it does so every time, the same way. The gap isn't arithmetic, it's discretion at the safety-critical junctions:

- **Graph traversal is a fixed algorithm**, not a model decision — it walks every dependency by construction, so completeness doesn't depend on the model remembering to check something several hops away.
- **The pass/fail comparison happens in code**, not by the model reading a number and judging it. The LLM never gets to interpret "is this safe" — it only sees the outcome the verification layer already computed.
- **Merge-to-main permission doesn't exist for the model at all.** It can only propose changes in a branch; a fixed threshold check in code, not the model's judgment, decides what merges.
- **The report and ECN are templated**, not generated freeform, so they're complete and consistent every run.

## Validation and evaluation

- **Solver validation first.** Before trusting any output, the PyNite pipeline is validated against the **10-bar truss benchmark** from the structural optimization literature (Haftka & Gürdal / Rajan formulation) — a widely published problem with independently verified stress and displacement results.
- **Cross-solver validation, ongoing.** Because linear-elastic truss analysis is exact (not approximate or fitted), a solver validated once generalizes to new scenarios of the same type. SkyCiv provides an independent, second-implementation check on top of that for every escalated case. Equilibrium (ΣF = 0 at each joint) and linearity checks (doubling a load should exactly double stress) run on every scenario as free, built-in correctness tests.
- **Demo structure.** The same 10-bar truss (6 nodes, 10 members) is the live demo structure — small enough to visualize every member changing state, real enough to be recognized by anyone in the field.
- **Eval harness.** ~20 scenarios are generated by parametrically perturbing the validated truss (resize a member, remove one, add/shift a load). Each scenario's correct outcome is computed offline with the validated solver — that's ground truth.
- **Headline metric — dropped dominoes:** affected members whose unsafe state the system fails to catch or report. Target: zero.
- **Baseline comparison.** The identical 20 scenarios are run through a tool-using LLM agent with the same Onshape and PyNite access but none of the fixed graph traversal, code-enforced thresholds, or branch-only write permission. The gap between that baseline's dropped-domino count and this system's is the core proof point — not asserted, measured.

## Scope and honesty notes

- The dependency graph itself is a known pattern (used widely in incident-response and multi-agent tooling) — the contribution is the domain (structural CAD), the physics-backed verification, and the escalation policy, not the graph structure.
- Load checks are simplified, illustrative structural checks (dead load + one or two applied loads), not full code-compliant (AASHTO/ASCE) bridge analysis, even where SkyCiv's design-check modules are used.
- "Biject" is our own verification module, not an existing third-party product.
- The FHWA National Bridge Inventory (NBI) is used only for narrative/demo color (e.g., referencing a real bridge's stats) — it does not contain the member-level geometry needed for FEA and is not used as simulation input.
- The escalation threshold is a defined rule (safety factor cutoff), not a learned or general-purpose judgment model — stated plainly rather than implied to be more autonomous than it is.

## Fit with the judging panel

- **Arga Labs** builds resettable, real-world sandboxes to test and train agents against enterprise software. Using an Onshape branch as a resettable sandbox for evaluating a change before committing it is the same idea, applied to CAD.
- **Lemma** builds production monitoring to catch "silent failures" in AI agents — the exact failure mode our headline metric (dropped dominoes) is built to catch and report, applied to engineering changes instead of production traces.

## Demo flow (~4–5 minutes)

1. **Open with the change**, not the pitch — show the truss and the edit that just happened.
2. **Show the baseline agent** (LLM + PyNite tool access, no fixed graph traversal or enforced thresholds) answering the same question, missing something or inconsistent across runs. Keep it short; let the gap speak for itself.
3. **Run the system** on the identical change — dependency graph lights up, the fast internal PyNite gate computes before/after stress and safety factor per member.
4. **Show an escalation** — a case where the safety factor drops below threshold. SkyCiv generates the trusted report; the ECN is created referencing it; both land in the engineer's inbox.
5. **Pull up the eval scoreboard** — dropped dominoes, baseline vs. system, across the 20 scenarios. This is the single most important slide for the rubric's reliability/eval weighting.
6. **One-line architecture recap** — ingestion, fast gate, trusted artifact, sharing.
7. **Close on the number**, not a tagline.

## Judging rubric alignment

| Criterion | Weight | How this project addresses it |
|---|---|---|
| Technical execution | 30% | Real Onshape and SkyCiv API integration, validated FEA pipeline, enforced verification layer |
| Reliability & evaluation | 25% | Solver validated against a known benchmark, cross-checked by an independent commercial platform; 20-scenario replay with quantified dropped-domino metric vs. baseline |
| Usefulness | 20% | Outputs are documents (ECN, professional-grade SkyCiv report) engineering teams already use, not a novel format |
| Originality | 15% | Engineering-change propagation with physics-backed, dual-solver verification is uncommon relative to typical dependency-graph agent demos |
| Demo clarity | 10% | Structured around one visual gap: baseline fails, system catches it, numbers prove it |