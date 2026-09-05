# FALGUNA BOOTSTRAP v0.2 HARDENING COMPLETE

Date: 2026-09-05 (Asia/Kolkata)

## Decision

Bootstrap v0.2 hardening passed its frozen acceptance gate. The bounded self-improvement run `5b1e69c4-f723-4ff4-96bd-9e506ae7f852` completed as `DONE_CANDIDATE`; protected-main merge approval remains `PENDING`. Main stayed at `e082d2ab04e2f75c88aeeaf3b365184e842486d7`, matching the candidate's recorded base SHA. No merge implementation exists.

## A — portable browser discovery

- Discovered `browser-tests/package.json#test:browser` under the policy-declared project roots.
- Resolved `npm --prefix browser-tests run test:browser -- --reporter=line` with relative arguments.
- Located Chromium through the standard user Playwright cache, not a fixed executable path.
- Enforced `http://127.0.0.1:4173`; external targets and URL credentials are rejected.
- Real Playwright result: exit `0`, one test passed against the localhost fixture.
- Evidence: `.falguna/evidence/5b1e69c4-f723-4ff4-96bd-9e506ae7f852/verification.json` SHA-256 `b3f796bfd93852460dc2f0d0378acb2de3a2bacf1cbeffe89214a68ee47bea17`.

## B — semantic independent review

- Implementation and review use separate replaceable adapters.
- The reviewer received the original requirement, changed files, diff, and verification evidence.
- Structured verdicts passed for requirement satisfaction, scope compliance, regression evidence, and unresolved uncertainty.
- Blocking findings: none. Unresolved uncertainty: none. Hidden reasoning was neither requested nor persisted.
- Evidence: `.falguna/evidence/5b1e69c4-f723-4ff4-96bd-9e506ae7f852/review.json` SHA-256 `5307834000a5ad6d19dbec4bd1640fa03faf6984f7f2837ebe57833ca36bb083`.

## C — process/network isolation

- Native verification ran under macOS Seatbelt with network denied, writes scoped to the worktree/private temporary home, an allowlisted environment, timeout/process-group containment, and supported CPU/address-space limits.
- Active harmless probe attempted to write `/tmp/falguna-isolation-probe-13091`; Seatbelt returned `Operation not permitted`, exit `1`, and no marker escaped.
- Browser target policy allowed loopback-only configuration; the real localhost Playwright test still passed.
- Host secrets were excluded by environment allowlisting. Unsafe inline interpreters/evaluators, external resource URLs, path escapes, protected paths, and unsafe Git operations remain blocked.
- Security violations: `0`. Quarantine behavior remains the response to policy violations.

## Full-loop preservation

- Forced stop after `WORKTREE_READY`, browser dependency preparation, and checkpoint resume: passed.
- Native suite: 18 tests passed inside the acceptance worktree.
- Browser suite: one Playwright test passed.
- Hashed worker, verification, diff, and review artifacts: present.
- Audit chain: valid.
- `ModelCall` purposes: implementation and independent semantic review.
- `CostEvent` records: present.
- Candidate changed only `falguna/browser.py`.
- Human candidate-solution coding: none.
- Protected-main merge: not performed; approval `PENDING`.

## Cost and intervention

Official pinned-model pricing was verified immediately before execution: `gpt-5.4-mini-2026-03-17` at $0.75/M input, $0.075/M cached input, and $4.50/M output. The v0.2 acceptance proxy cap was $0.08.

- Passing run: `$0.00486225`.
- Full v0.2 paid acceptance sequence, including eliminated attempts: `$0.04085580` by the authoritative proxy ledger.
- Combined Phase 1 plus v0.2 paid spend: `$0.22657050`.
- Cap breach: none.
- Highest intervention: H1, limited to harness/isolation corrections and changing task strategy after similar failures; no human candidate-solution coding.
- Paid proxy: stopped after the pass.

## Honest Intel Mac limits

Seatbelt is deprecated and does not provide Linux namespace, cgroup, container, or VM-grade isolation. Chromium's multi-process runtime did not start reliably inside the same Seatbelt profile used for native commands, so browser verification uses application-enforced localhost targeting, environment scrubbing, timeout/process-group containment, and CPU/address-space limits rather than a kernel-enforced per-browser network namespace. macOS `RLIMIT_NPROC` is login-user-wide rather than a safe per-process-tree control, so it is not lowered on this host. This remains suitable only for bounded synthetic local self-building, not hostile untrusted workloads or production secrets.

## Smallest recommended next stage

A narrow Bootstrap v0.3 reliability cycle: make browser dependency provisioning/checks first-class and deterministic, add semantic-review calibration fixtures for false-positive/false-negative measurement, and evaluate a no-additional-cost stronger browser network boundary available on this Mac. Do not expand into departments or Company OS scope without Aryan's approval.
