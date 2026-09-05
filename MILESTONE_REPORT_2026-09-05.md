# Falguna Phase 1 v0.1 milestone report

Date: 2026-09-05 (Asia/Kolkata)

## Outcome

Milestones A-C and E are implemented as a working bootstrap slice. Milestone D has the localhost-only Playwright capability boundary but no browser self-build acceptance run yet. Milestone F did not reach the authoritative three-consecutive-pass threshold. **Do not claim self-building and do not merge any candidate automatically.**

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
- Paid API cost: $0.11189940 cumulative for all Phase-1 worker experiments in this session, below the $0.50 cap. Auto-reload remained off.
- Human intervention: H0-H1.
- Limitations: no Phase-1 Playwright self-build run; Aider's text summary rounds per-run cost while the proxy ledger is authoritative.
- Safe to proceed: evidence/audit yes; browser milestone remains partial.

## E — independent review and DONE_CANDIDATE

- Built: separate deterministic review pass, secret/protected-policy checks, diff/evidence preservation, `DONE_CANDIDATE`, and a pending `PROTECTED_BRANCH_MERGE` approval record. The merge decision API records owner intent only; v0.1 intentionally contains no merge implementation.
- Evidence: five genuine Aider runs reached `DONE_CANDIDATE`; each kept the approval pending and main untouched.
- Cost: included in the $0.11189940 total.
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

Decision: **FALGUNA BOOTSTRAP THRESHOLD NOT YET REACHED.** The next iteration must change worker/editor strategy or use a stronger model only after current official pricing and a new explicit run cap are verified. Do not repeat the same Aider mini-model loop.

