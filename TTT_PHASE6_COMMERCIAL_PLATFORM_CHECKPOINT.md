# TTT / FALGUNA Phase 6 Commercial Platform Checkpoint

**Status:** CHECKPOINT — Phase 6 is not complete and this is not final acceptance.  
**Date:** 2026-10-01  
**Branch:** `phase6/commercial-platform-foundation-v1`  
**Accepted baseline:** `b42067f` — Phase 5 Final Client Experience  
**Implementation checkpoint:** `a49834d`  
**External actions:** None. No push, merge, deploy, customer contact, provider activation, account creation, spending, or money movement.

## Files changed

- `falguna/phase6_commercial.py` — organization-scoped commercial identity and authorization, provider-neutral sandbox payment state machine, webhook verification/replay protection, outgoing-action approval workflow, immutable finance events, and protected/free-cash policy calculation.
- `falguna/schema_sqlite.sql` — additive Phase 6 identity, payment, webhook, approval, finance-event, and reserve-policy tables/indexes.
- `falguna/store.py` — explicit allow-list entries for the new tables.
- `tests/test_phase6_commercial.py` — 22 synthetic security, idempotency, authorization, webhook, approval, and reserve-policy tests.
- This checkpoint report.

## Architecture decisions

1. TTT HQ remains the sole owner of commercial and financial records. The new module exposes deterministic services; it does not give FALGUNA or a model an execution surface.
2. Commercial authorization is fail-closed and explicitly organization-scoped. Roles are customer, partner, finance operator, sales operator, admin, and owner, with persisted permission seams.
3. Payments use a provider-neutral state machine and `SANDBOX_ADAPTER` only. Legal owner and beneficiary are separate fields. Provider session/transaction, payment-method, and mandate references are token/reference fields only.
4. Raw PAN, CVV, and card-number metadata are rejected. No provider secret is persisted.
5. Browser/client success is not payment truth. State changes require server-side evidence, exact amount/currency agreement, a valid state transition, and an idempotency key.
6. Webhook intake uses HMAC-SHA256 verification, persistent provider-event replay detection, payload-hash conflict detection, and organization/payment matching. Verified intake remains `VERIFIED_PENDING_APPLICATION`; verification alone does not mutate payment truth.
7. Outgoing actions follow maker -> independent verifier -> Aryan owner approval. Approval terminates at `APPROVED_PENDING_EXECUTION`; there is deliberately no execution method or provider call.
8. Financial events are append-only through the service and idempotent by organization/key. Corrections are modeled as new reversal events, not edits/deletes.
9. Protected cash is dynamic: tax, held customer funds, accrued payables, committed costs, disputed funds, and the greater of recorded operating reserve or minimum-runway policy. Default minimum runway is 3 months and target is 6 months. Reserve allocations do not manufacture cash; only the cash bucket changes cash balance.

## Completed requirements in this checkpoint

- Organization-scoped commercial identity foundation and role/permission seams.
- Cross-organization authorization checks for new service operations.
- Sandbox-first payment-intent architecture for one-time, milestone, subscription, retainer, and bank-transfer intent kinds.
- Capability metadata for UPI, cards, net banking, wallets, local methods, international cards, bank transfer/wire, and EMI metadata (TTT does not lend).
- Honest payment states for created, authorized, captured, settled, failed, refund-requested, refunded, and disputed.
- TTT intent IDs plus provider transaction/event reference seams.
- Idempotent intent creation and financial/payment events.
- Signed webhook verification and replay/conflicting-payload rejection.
- Legal owner / beneficiary separation.
- Maker, verifier, and final Aryan approval records for refund, commission release, vendor payment, and beneficiary change.
- Automatic high-risk flags for beneficiary changes and configured high-value actions.
- Append-only finance-event foundation and dynamic protected/free-cash calculation.
- Direct FALGUNA/AI maker attempts fail.

## Deferred requirements

The following remain Phase 6 work and must not be represented as complete:

- Authenticated HQ HTTP endpoints and UI. Existing local HQ authentication/identity integration must be designed before exposing these records; no insecure route was added merely to show progress.
- Concrete provider adapters, checkout/session creation, tokenized saved-method/mandate handling, subscription schedules, provider settlement ingestion, receipts, or real webhook application.
- Invoice-to-payment orchestration and reconciliation against the existing `rh_invoices`/billing evidence ledger.
- Full Accounts Receivable, Accounts Payable, expenses, tax/provision, gateway-fee, budget, project-economics, document-metadata, reconciliation, and approval-queue UI/API.
- Actual refund, commission, vendor-payment, or beneficiary execution. These require later explicit approval and provider integration.
- Extension of partner roles/lifecycle/KYC metadata, commission plans, recurring/lifetime-originator options, dashboard, and payout approval integration.
- Complete anti-diversion signal registry, brand misuse/customer-report intake, suspension review, and synthetic `verify partner` / `verify payment instructions` pages.
- Structured draft commercial/legal policy templates and configurable refund-policy evaluator.
- Acquisition/source analytics, small-to-enterprise service-tier economics, opportunity intelligence, and jurisdiction deny/unsupported policy seam.
- Race-condition/concurrency tests with multiple database connections and authenticated HTTP adversarial tests.
- Full repository regression and interactive desktop/mobile browser QA for a future HQ surface.

## Test results

- `tests/test_phase6_commercial.py`: **22 passed** in 13.66s.
- Related focused regression (`test_phase6_commercial`, `test_billing`, `test_finance_ledger`, `test_partner_management`, `test_commercial`, `test_customer_portal`): **164 passed** in 117.00s.
- Python compilation of `falguna/phase6_commercial.py`: passed.
- `git diff --check`: passed before the implementation commit.
- Full suite: not run in this checkpoint. The accepted Phase 5 report documents a roughly 26-minute full suite with known environment-specific ffmpeg/flite/network failures; this checkpoint does not claim those are resolved.

## Security findings and remaining risks

- The accepted Phase 5 checkout had customer-portal scope checks and multiple human gates but no unified Phase 6 commercial identity or provider-neutral payment truth model. This checkpoint introduces that seam without modifying existing Phase 5 data.
- Generic `StateStore` remains an internal persistence primitive and is not itself a security boundary. All future routes must use the Phase 6 services/context rather than direct table access.
- HMAC webhook verification is an adapter contract, not a claim that all future providers use the same signature scheme. A provider adapter must implement the provider's exact canonicalization, timestamp tolerance, key rotation, and signature algorithm.
- SQLite uniqueness protects ordinary replay/idempotency paths. Higher-contention production use still needs transaction/locking tests and likely a production database adapter before real-money activation.
- The raw-card rejection is defense-in-depth at this service boundary, not PCI certification. Real card collection must remain provider-hosted/tokenized and requires a separate compliance review.

## Money-movement guarantees at this checkpoint

- Provider is always `SANDBOX_ADAPTER` for newly created intents.
- No code calls a payment provider, bank, card network, wallet, email system, or customer-facing external service.
- No raw card secret is accepted for persistence.
- No payment state changes from client/browser success alone.
- Refunds, commissions, vendor payments, and beneficiary changes cannot execute from this module.
- Every modeled outgoing action requires an independent verifier and Aryan owner approval, then stops at `APPROVED_PENDING_EXECUTION`.
- FALGUNA/AI cannot act as maker or final approver and no AI execution method exists.

## Exact next steps

1. Add transaction-safe authenticated HQ service/API integration with explicit session-to-commercial-identity mapping and organization isolation tests.
2. Connect payment intents to existing invoices without replacing the existing evidence ledger; implement a sandbox adapter and verified-webhook application transaction.
3. Add reconciliation and approval-queue APIs/UI, including settlement mismatch, duplicate refund, and concurrency tests.
4. Extend the existing partner module with role/lifecycle/KYC seams, configurable commission plans, clawbacks/holds, anti-diversion risk events, and approval-gated payout preparation.
5. Implement draft-only policy records/templates and refund eligibility calculation, explicitly marked for qualified legal review.
6. Add acquisition/service-economics/opportunity-intelligence foundations and jurisdiction-policy records.
7. Run focused suites after each coherent slice, then full regression and desktop/mobile browser acceptance. Do not begin Phase 7.
