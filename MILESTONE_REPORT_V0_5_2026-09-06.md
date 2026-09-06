# FALGUNA BOOTSTRAP v0.5 OPERATIONAL USABILITY COMPLETE

Date: 2026-09-06 (Asia/Kolkata)

## Decision

Bootstrap v0.5 passed. Aryan can now submit and inspect bounded internal engineering tasks through the Falguna command surface without interacting with benchmark machinery. Protected-main merge remains impossible without a recorded human decision, and this acceptance ended with approval `PENDING`.

## Implemented flow

- `run`: accepts a bounded objective, explicit editable files, verification commands, optional localhost browser target, model, and hard cost cap; it uses the existing structured worker, isolated worktree, verification, independent review, evidence, audit, cost, and approval loop.
- `status`: shows the mission, objective, current state, attempt, completed milestones, and an actionable failure category when blocked.
- `summary`: shows requirement coverage, native checks, browser applicability/result, independent review, changed files, cost, risk, unresolved issues, evidence-hash validity, audit validity, and merge state.
- `decide`: records `approve`, `reject`, or `request-changes` human intent. It performs no merge.

Failure categories are model failure, tool failure, verification failure, permission block, security containment, budget stop, and human input required.

## Representative end-to-end acceptance

- Run: `0fe99093-af75-4c10-a977-cda30d5015c2`.
- Genuine bounded defect: `clamp_nonnegative` returned the wrong value for both negative and positive inputs; the frozen baseline failed 2/2 focused native tests.
- Input path: the new `falguna run` command.
- Candidate: model-authored change limited to `src.py`; no human candidate-solution coding.
- Result: `DONE_CANDIDATE` in one attempt.
- Requirements: passed.
- Native verification: 2/2 passed, exit 0.
- Browser/E2E: not applicable to this non-UI task; no unnecessary browser run was performed.
- Independent semantic review: passed; zero blocking findings and zero unresolved uncertainty.
- Changed files: exactly `src.py`.
- Risk: low.
- Retained artifact hashes: valid.
- Audit chain: valid.
- Security violations / protected-main merges: `0 / 0`.
- Merge approval: `PENDING`.
- Original acceptance repository remained at `5c4593232335905c38d363bf8d3e2054034c5a40`; the candidate exists only in its isolated worktree.

Candidate diff SHA-256: `6ecb90f60c19359bd3fcca059499f4627918aecabf9aa5cef5c2ccaf603aa7b3`.

Verification SHA-256: `8094778430159c1bc6d135edefe0c3d278e5060786d60320f16ea7bb52d7e50a`.

Review SHA-256: `23d8c47d99e6d929890cc54a87c97b2bb84c8e4778d5301b7e08695ccfce6313`.

## Regression, intervention, and cost

- Falguna native suite: 23/23 passed.
- Intervention level: `H0`; no acceptance repair or infrastructure intervention.
- Model: `gpt-5.4-mini`; no stronger tier used.
- Recorded usage: 23,784 input tokens and 568 output tokens across implementation and review.
- Actual additional cash cost: `$0.00` through the authenticated local subscription transport.
- Hard run cap: `$0.05`; no paid run occurred and the reset credit was untouched.
- Notional API list-price equivalent at the already verified v0.4 rates: `$0.020394`.

## Known limitations

This remains a local Intel Mac bootstrap for bounded, trusted internal engineering. Native commands use deprecated macOS Seatbelt containment, not a namespace/container/microVM/VM. Chromium, when applicable, remains Playwright runtime-controlled rather than kernel-isolated. The command currently expects explicit editable files and verification commands, processes one mission at a time, and records approval intent without implementing merge or deployment. Do not use it for hostile code, production secrets, unattended merging, or deployment.

## Smallest next stage

Bootstrap v0.6 should be limited to safe operator ergonomics around reusable per-project mission profiles and resume/retry presentation. It has not been started.
