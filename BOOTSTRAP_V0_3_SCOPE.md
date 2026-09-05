# Falguna Bootstrap v0.3 reliability — frozen scope

Date: 2026-09-05 (Asia/Kolkata)

## Objective

Reduce harness intervention in the proven v0.2 self-building loop. A fresh bounded self-improvement task must finish as a reviewed `DONE_CANDIDATE` with protected-main merge approval still `PENDING`.

## Slice 1 — deterministic browser provisioning

- Derive Playwright package and browser requirements from the selected project manifest and lockfile.
- Prefer the declared local Playwright CLI and a validated existing Playwright cache; permit an explicit install step only when policy allows it.
- Record versions, cache selection, executable validation, commands, and whether any network/install action was needed.
- Never depend on a repository-specific absolute executable path or a manual path correction.

Acceptance: repeated preflight selects the same valid local dependency/cache plan; missing or mismatched dependencies fail closed with an actionable deterministic result; localhost browser verification passes.

## Slice 2 — semantic-review calibration

- Run the replaceable independent reviewer against labeled clearly-correct, clearly-incomplete, and clearly-unsafe synthetic cases.
- Record per-case verdict, expected verdict, the four structured dimensions, blocking findings, unresolved uncertainty, and model/cost metadata.
- Calculate false accepts and false rejects. Hidden reasoning or chain of thought must not be requested or persisted.

Acceptance: all calibration cases match their labels, with zero false accepts and zero false rejects in the frozen calibration set.

## Slice 3 — stronger free browser network boundary

- Keep URL/policy validation, scrubbed environment, process-group timeout, and resource limits.
- Add a browser-runtime request allowlist that permits loopback HTTP(S) only and aborts every other browser request.
- Actively test one harmless external request and record that it was blocked while localhost verification still passes.
- State separately which controls are application/runtime enforced and which are OS/kernel enforced.

Acceptance: the real browser records a blocked external request and a successful localhost check. No VM-grade or kernel-level browser isolation claim is made.

## Slice 4 — intervention reduction and preserved gates

- Preserve the replaceable worker and reviewer adapters, checkpoint/resume, audit chain, `ModelCall`, `CostEvent`, Definition of Done, hashed evidence, bounded repair, and containment/quarantine behavior.
- No human candidate-solution coding and no protected-main merge.
- Target H0; permit at most one infrastructure-only H1 action with explicit justification.
- After two materially similar failures, change strategy rather than repeat.

## Out of scope

Sales, Marketing, Finance, Trading, TTT HQ, Company OS departments, deployment, production data, automatic merge, hostile workloads, Linux purchase/rental, paid infrastructure, model upgrade without evidence, and provider/harness coupling.

## Final v0.3 gate

One fresh bounded self-improvement run must prove deterministic browser provisioning, calibrated independent review, an actively blocked harmless external browser request with working localhost verification, complete audit/cost/checkpoint evidence, no candidate-solution coding by Aryan, and `DONE_CANDIDATE` with merge approval `PENDING`. Passing ends v0.3; no broader stage begins without approval.
