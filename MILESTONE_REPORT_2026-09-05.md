# Falguna Phase 1 v0.1 milestone report

Date: 2026-09-05 (Asia/Kolkata)

## Outcome

Milestones A-E are implemented as a working bootstrap slice. A corrective structured-edit worker reached the authoritative three-consecutive-pass threshold, including a forced interruption/resume and a real localhost Playwright self-build verification. **The safe bootstrap threshold is reached, but no candidate may be merged automatically.**

## A — control plane, durable state, ModelGateway

- Built: Mission, Requirement, Task, TaskStep, Run, Checkpoint, Approval, ModelCall, CostEvent and Artifact records; SQLite adapter; PostgreSQL promotion schema; OpenAI-compatible and local ModelGateway configurations.
- Evidence: native bootstrap suite passes; CLI initializes and creates durable mission state.
- Cost: $0 model cost for construction.
- Human intervention: H0 for runtime tests; initial code authored under Aryan's Phase-1 authorization.
- Limitation: SQLite is the documented Intel-Mac development substitute; PostgreSQL migration has not run against a live server.
- Safe to proceed: yes, for synthetic local bootstrap work only.

## B — wrapped worker, isolation, scoped capabilities

- Built: replaceable WorkerAdapter; wrapped Aider 0.86.0; Git worktree manager; path containment; protected-file and changed-file gates; command allowlist; no shell evaluation; no push/merge/reset/clean/checkout; real API key retained behind a localhost proxy.
- Evidence: worktrees were created from clean main; main stayed unchanged; policy escape/protected-file tests pass.
- Cost: included in acceptance spend below.
- Human intervention: H0-H1 task definition and bounded strategy changes; no human solution coding inside candidate worktrees.
- Limitation: macOS worktrees isolate checkout state, not processes/network/kernel; this is calibration-grade, synthetic-only isolation.
- Safe to proceed: yes, only under current synthetic/no-production restrictions.

## C — verification, retry/recovery, checkpoint/resume

- Built: native command verification, two-attempt bound, failure evidence fed into one repair, durable checkpoints, resume, quarantine on policy violation.
- Evidence: seven native control-plane tests pass, including forced interruption/resume, bounded repair, failed-verification rejection, and no approval on failure.
- Cost: $0 model cost for the offline tests.
- Human intervention: none inside the tested flows.
- Limitation: resume is stage-based; it does not resume a model stream mid-call.
- Safe to proceed: yes.

## D — browser, evidence, audit, cost

- Built: localhost-only browser command wrapper; hashed Artifact records; append-only SHA-256 chained audit log; ModelCall and CostEvent persistence; localhost metering proxy remained capped at $0.50.
- Evidence: audit tamper detection passes; worker, verification, diff and review artifacts preserved per completed candidate; proxy ledger is preserved under `.falguna/`.
- Paid API cost: $0.12512445 cumulative for all Phase-1 worker experiments in this session, below the $0.50 cap. Auto-reload remained off.
- Human intervention: H0-H1.
- Corrective evidence: run `f4de3ab6-52fb-4282-96fc-a75b68e1b76c` passed native verification and a real headless Chromium check against a localhost-only candidate status page after forced interruption/resume.
- Limitations: Playwright uses a host-pinned local Chromium executable for this Intel-Mac calibration environment; the proxy ledger remains authoritative for paid cost.
- Safe to proceed: evidence/audit yes; browser milestone remains partial.

## E — independent review and DONE_CANDIDATE

- Built: separate deterministic review pass, secret/protected-policy checks, diff/evidence preservation, `DONE_CANDIDATE`, and a pending `PROTECTED_BRANCH_MERGE` approval record. The merge decision API records owner intent only; v0.1 intentionally contains no merge implementation.
- Evidence: five genuine Aider runs reached `DONE_CANDIDATE`; each kept the approval pending and main untouched.
- Cost: included in the $0.12512445 total.
- Human intervention: no manual candidate code.
- Limitations: deterministic reviewer is independent from the worker run but is not yet a different-model semantic review.
- Safe to proceed: yes for candidate generation; no candidate promotion without Aryan's explicit approval.

## F — first self-build threshold

Authoritative gate: three consecutive bounded self-repository tasks pass all gates; at least one is interrupted/resumed; median intervention H0-H1; no H3/H4, unauthorized action, or cap breach.

Results:

- `1393e90c-afa9-412f-8258-56d3958bda23`: PASS, forced stop/resume, audit-verify candidate, hidden evaluation pass.
- `eeaf0205-fdf5-4805-8b0a-5c5725f4e4c6`: PASS, missing-run status candidate, hidden evaluation pass.
- `3c49651a-2e06-47e5-87da-d59dd50a2746`: FAIL, worker used unavailable `python` in its test.
- `ceb16b02-a71c-4ed5-9c46-7123c3c7f999`: PASS, forced stop/resume, terminal-status helper, hidden evaluation pass.
- `d4775ad4-40a2-4ceb-b823-b7d36b3509ec`: PASS, task-step uniqueness, hidden evaluation pass.
- `e5b69bb7-3fb0-4b49-bc01-e86317cfbf5e`: FAIL after bounded repair; worker omitted a required cost-event field. The host execution was also interrupted and the run resumed from checkpoint.
- `77afccd5-e94e-4606-b989-3cec730200eb`: PASS, forced stop/resume, terminal-status helper, hidden evaluation pass.
- `fdad38c6-d522-4680-ae86-e68e0069f44e`: FAIL after bounded repair; worker test assumed pre-existing task state.
- `035c6e5c-4956-46a0-9e3f-6415a11084e2`: FAIL; architect mode planned the correct patch but its editor did not apply it.

Longest consecutive passing streak: **2**. Threshold required: **3**. Highest intervention: H1. Unauthorized/security events: 0. Protected main merges: 0.

Original decision at the preserved checkpoint: **FALGUNA BOOTSTRAP THRESHOLD NOT YET REACHED.** The next iteration had to change worker/editor strategy or use a stronger model only after current official pricing and a new explicit run cap were verified.

## Corrective continuation — threshold result

The worker abstraction was preserved. Aider was replaced for the corrective sequence by `StructuredEditWorker`, which requests strict JSON old-to-new patches, accepts only explicitly declared files, requires each old snippet to match exactly once, preserves unrelated bytes, and records usage/cost through the existing contracts. A stronger model was not used because the first failure mode was editor application reliability rather than demonstrated model incapability.

Before paid execution, `gpt-5.4-mini-2026-03-17` pricing was freshly verified from official OpenAI documentation at $0.75/M input, $0.075/M cached input, and $4.50/M output. The declared limits were $0.10 per task and $0.30 for the corrective sequence.

Fresh authoritative streak from clean main `458e6b96fbd27a4841643bcf28502cdd34749ec3`:

- `f4de3ab6-52fb-4282-96fc-a75b68e1b76c`: PASS, forced stop/resume, native tests PASS, localhost Playwright PASS, independent review PASS, `DONE_CANDIDATE`, merge approval PENDING, cost $0.006807.
- `69f10063-8dbf-4f06-a5b3-44fb3b61d7dc`: PASS, native tests PASS, independent review PASS, `DONE_CANDIDATE`, merge approval PENDING, cost $0.005232.
- `5ce2cdcd-78f1-4791-a6b6-437173d2a855`: PASS, native tests PASS, independent review PASS, `DONE_CANDIDATE`, merge approval PENDING, cost $0.005622.

Passing-streak spend: **$0.017661**. Total corrective-continuation paid spend including two eliminated failed calibration attempts: **$0.06059025**, below the $0.30 hard cap. Total Phase-1 paid spend including the preserved checkpoint: **$0.18571470**. Security violations: 0. Protected-main merges: 0. Highest intervention: H1 for worker/harness correction; no human coded any candidate solution. The paid proxy was stopped after the third pass.

Decision: **FALGUNA BOOTSTRAP THRESHOLD REACHED.** The safe next stage is a narrow bootstrap v0.2 hardening cycle: make browser-runtime discovery portable, add a genuinely semantic independent reviewer, and validate process/network isolation before any broader self-building scope. Do not expand into Sales, Marketing, Finance, Trading, or full Company OS.
