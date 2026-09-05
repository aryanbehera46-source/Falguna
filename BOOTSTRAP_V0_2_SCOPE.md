# Bootstrap v0.2 hardening — frozen scope

Date: 2026-09-05 (Asia/Kolkata)

## Objective

Harden the proven v0.1 self-building loop without expanding its product scope. A bounded self-repository change must still end as a reviewed `DONE_CANDIDATE` with a pending human merge decision.

## Slice 1 — portable browser discovery

- Discover Playwright verification from explicit policy plus project manifests/configuration in supported repository roots (`.`, `browser-tests`, `frontend`, `web`, `app`, `client`, `ui`).
- Resolve commands and browser executables without repository-specific absolute paths.
- Keep browser targets localhost-only and preserve discovery, command, output, and result evidence.
- Reject ambiguous, external, or policy-incompatible browser plans.

Acceptance: fixtures covering multiple layouts select a valid portable plan; a localhost browser check succeeds; a non-localhost target is blocked.

## Slice 2 — semantic independent review

- Keep review behind a replaceable adapter independent of the implementation worker.
- Review the original requirement, changed files, diff, and verification evidence.
- Produce structured verdicts for requirement satisfaction, scope compliance, regression evidence, and unresolved uncertainty.
- Persist conclusions and concise evidence only; never persist hidden reasoning or chain of thought.
- Fail closed when required dimensions are absent, negative, or uncertain.

Acceptance: an insufficient candidate that passes syntax/tests is rejected; a satisfying candidate produces all four structured dimensions and review evidence.

## Slice 3 — process and network isolation

- Retain worktree/path containment and command allow/deny policy.
- Run child commands with a scrubbed environment, private temporary home, time limits, and resource limits where supported.
- On macOS, use the strongest available native process profile to deny filesystem writes outside the worktree/private temporary area and deny network by default, with loopback-only network for browser verification.
- Block unsafe interpreter/evaluator forms and external resource targets before launch.
- Treat security/policy violations as quarantine events, not retryable failures.
- Document platform enforcement and degraded-mode limits honestly.

Acceptance: harmless prohibited filesystem/resource access and external-network access are blocked, while repository tests and localhost browser work remain allowed; enforcement evidence is recorded.

## Preserved gates

Checkpoint/resume, audit chain, `ModelCall`, `CostEvent`, hashed artifacts, native verification, bounded repair, Definition of Done, independent review, `DONE_CANDIDATE`, and pending human-only protected-main merge approval remain mandatory.

## Out of scope

Sales, Marketing, Finance, Trading, TTT HQ, Company OS departments, deployment, production data, automatic merge, unrestricted self-modification, Linux purchase/rental, and provider/model lock-in.

## Final v0.2 gate

One bounded self-improvement task must complete the full loop and prove all three slices in its artifacts, with no human candidate-solution coding, no protected-main merge, no security violation, and spend within a separately declared cap. Passing this gate ends the stage; no broader work begins without Aryan's approval.
