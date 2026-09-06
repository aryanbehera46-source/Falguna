# FALGUNA v0.6 LOCAL WEB APP COMPLETE

Date: 2026-09-06 (Asia/Kolkata)

## Decision

Bootstrap v0.6 passed. Aryan can open Falguna Engineering at `http://127.0.0.1:8765` and complete the normal bounded task flow from the browser. The UI wraps the proven v0.5 control plane; it does not replace its worker, containment, verification, review, evidence, audit, cost, checkpoint, or approval boundaries.

## Start and use

From the Falguna repository, run `python3 -m falguna --root . web`, then open `http://127.0.0.1:8765`. The server binds to localhost only.

The internal-alpha home screen provides an approved project selector, bounded objective and editable-file inputs, an approved verification profile, a profile-bounded cost cap, Run Mission, live milestone status, classified failures, final evidence, and Approve Merge / Reject / Request Changes controls. Decision controls record intent only; there is no merge or deployment implementation.

## Browser-only acceptance

- Approved real project: Falguna Engineering.
- Run: `c2a11243-8de1-475d-bb23-975fcff1417f`.
- Stable base: `334194a960644b5f4e8b98407390e000caf50074`.
- Bounded task: add one exact acceptance sentence to `BOOTSTRAP_V0_6_SCOPE.md`; no other editable file permitted.
- Input, submission, status monitoring, and evidence inspection were completed through the browser UI.
- Result: `DONE_CANDIDATE` in one attempt.
- Requirements: passed.
- Native verification: 1/1 passed; the full Falguna suite contained 26/26 passing tests.
- Browser/E2E for the candidate documentation task: correctly `NOT_APPLICABLE`.
- UI responsive inspection: 1440x900, 1024x768, and 390x844; no horizontal overflow.
- Independent semantic review: passed with no unresolved issues.
- Changed files: exactly `BOOTSTRAP_V0_6_SCOPE.md`, retained only in the isolated candidate worktree.
- Evidence hashes and audit chain: valid.
- Risk: low.
- Merge approval: `PENDING`; no decision control was used.
- Security violations / protected-main merges: `0 / 0`.
- Original repository remained on the stable base; no candidate merge or deployment occurred.

Candidate diff SHA-256: `a9463b27eb1166cf683cded51dcb28d81b7ecf0850d542fe967be8fdc5fce43e`.

Verification SHA-256: `7e239e56305a922980f67ea4da21af5ee19179c77a4480cb106e676a854deceb`.

Review SHA-256: `04008fed64a8a011cc26434d0e0c826d8ca3a933424a84141fc601cf5c8ab0c9`.

## Intervention and cost

- Intervention: `H0`; no mission repair, infrastructure repair, candidate coding, or approval intervention.
- Model: `gpt-5.4-mini`; no stronger tier used.
- Recorded usage: 24,753 input tokens and 987 output tokens across implementation and independent review.
- Additional cash cost: `$0.00` through the authenticated subscription transport.
- Hard run cap: `$0.05`; no paid run occurred and the reset credit was untouched.

## Known limitations

This remains a local Intel Mac internal-alpha tool for bounded, trusted work. Normal use still requires starting one local command before opening the browser. Approved projects and verification profiles are file-configured, editable paths remain explicit, one process handles local missions, and decisions do not merge or deploy. Seatbelt and Playwright restrictions remain application/runtime controls rather than VM-grade isolation. Do not use it for hostile code, production secrets, public hosting, unattended merge, or deployment.

## Smallest next stage

The smallest next stage is a one-click local launcher for the existing web server. It has not been started and requires Aryan approval.
