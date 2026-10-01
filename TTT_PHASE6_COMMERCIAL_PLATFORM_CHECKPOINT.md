# TTT / FALGUNA Phase 6 Commercial Platform Checkpoint

**Status:** **PHASE 6 CHECKPOINT — NOT FINAL ACCEPTANCE**
**Date:** 2026-10-01  
**Branch:** `phase6/commercial-platform-foundation-v1`  
**Accepted baseline:** `b42067f` — Phase 5 Final Client Experience  
**Implementation checkpoint:** `a49834d`  
**Continuation starting commit:** `a836b79`
**Authenticated integration/reconciliation commit:** `7f5dac0`
**Partner risk/verification commit:** `f98305d`
**Sandbox financial-flow finalization commit:** `bd8aeda`
**External actions:** None. No push, merge, deploy, customer contact, provider activation, account creation, spending, or money movement.

## Files changed

- `falguna/phase6_commercial.py` — organization-scoped commercial identity and authorization, provider-neutral sandbox payment state machine, webhook verification/replay protection, outgoing-action approval workflow, immutable finance events, and protected/free-cash policy calculation.
- `falguna/schema_sqlite.sql` — additive Phase 6 identity, payment, webhook, approval, finance-event, and reserve-policy tables/indexes.
- `falguna/store.py` — explicit allow-list entries for the new tables.
- `tests/test_phase6_commercial.py` — 22 synthetic security, idempotency, authorization, webhook, approval, and reserve-policy tests.
- This checkpoint report.

## 2026-10-01 continuation outcome

This continuation turned the original data-model foundation into a guarded operational backend slice:

- Every new `/api/p6/*` HQ route requires a valid server-side TTT staff session, an active commercial identity mapped to that staff user, an exact organization header match, the required commercial role permission, and (for mutations) the session CSRF token.
- Added authenticated APIs for invoice checkout creation, verified provider-event application, settlement reconciliation, reconciliation/receipt views, approval request/verify/amend/reject/final-approve, and finance cash position.
- Added invoice-linked checkout validation. A checkout cannot cross organizations, use a terminal invoice, or exceed the real amount due. Partial/milestone checkout remains explicit.
- Added separate capture and settlement truth. Only a `SETTLED` payment can reconcile into an invoice collection.
- Added reconciliation records with `MATCHED`, `PARTIAL`, `MISMATCH`, `UNMATCHED`, and `REVIEW_REQUIRED` states, plus amount/currency, missing settlement, duplicate settlement, invalid invoice, and organization mismatch findings.
- Only matched or partial verified settlements update the existing invoice ledger, create a receipt, append a finance cash event, and affect cash. Mismatch/review rows do none of those things.
- Added append-only approval events with a hash of amount/currency/beneficiary/action/payload/risk fields at every decision point.
- Material approval changes reset the request to `PENDING_VERIFICATION`, clear prior verifier/final-approver state, and append an invalidation event. Rejection requires a reason and is also append-only.
- Corrected a partial-write hazard found during testing: settlement evidence is now validated before a reconciliation row can be created.

The follow-on partner/risk slice adds:

- Partner role types, maturity tier, KYC-status metadata seam, public-verification opt-in, related-party disclosure, no-side-deal, and unauthorized-subcontracting policy metadata over the existing partner ledger.
- Organization-scoped commission plans with configurable rates, recurring/lifetime-originator capability flags, and excluded pass-through categories. No single global commission percentage is imposed.
- Partner contribution records linked to referral/opportunity evidence.
- Evidence-backed anti-diversion risk events covering alternate beneficiaries, off-platform payment, alleged representative payment, disappearing leads, duplicate customers, abnormal refunds/commissions, official-flow refusal, unauthorized price/scope changes, brand misuse, undisclosed related parties, diversion, and unauthorized subcontracting.
- Human-controlled `HOLD`, `REVIEW`, `SUSPEND_ACCESS`, `CLEAR`, and `ESCALATE` actions with an append-only action history. FALGUNA may flag evidence but has no action method or authority.
- Public, data-minimized local verification APIs for partners and payment instructions. Exact customer and beneficiary references are required for payment verification; failed verification reveals no amount or beneficiary.

The finalization pass adds:

- Settled-collection-based commission release preparation, configured exclusions, unresolved-risk blocking, maker -> independent verifier -> Aryan approval, `RELEASABLE` state, and an idempotent commission-payable finance event. It deliberately has no payout executor.
- Organization-scoped partial refund requests, exact currency and remaining-settled-amount enforcement, approval gating, verified sandbox confirmation, one idempotent cash debit, and proportional partner-commission clawback.
- Monthly/annual subscription records with provider token/mandate references only and idempotent due-cycle draft invoice creation.
- Vendor/contractor/operating payable records with new-beneficiary risk flags, the complete approval chain, and a sandbox execution-ready terminal state with no money movement.
- An authenticated Commercial Finance HQ view for cash/protected/free cash, reconciliations, approval actions, commissions, refunds, subscriptions/payables, and risk signals. It bootstraps organization and CSRF data only from the current authenticated staff session.

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
- Authenticated, CSRF-protected, organization-scoped HQ APIs for the Phase 6 operational slice.
- Invoice -> checkout -> capture -> settlement -> reconciliation -> receipt -> existing billing ledger -> finance cash event.
- Honest partial/milestone collection path and mismatch/review path.
- Append-only approval audit history, rejection, and re-verification after material change.
- Configurable partner profile/commission-plan foundation and evidence-backed contribution records.
- Anti-diversion risk-event queue and human-only hold/review/suspend/clear/escalate controls.
- Data-minimized partner/payment-instruction verification with explicit no-money-collection warning.
- Synthetic commission release through `RELEASABLE`, including the accrued-payable ledger consequence.
- Synthetic partial refund confirmation with settled-amount/currency bounds and commission clawback.
- Subscription due-invoice and outgoing-payable approval foundations.
- Authenticated Commercial Finance UI/API wiring without exposing unauthenticated financial data.

## Deferred requirements

The following remain Phase 6 work and must not be represented as complete:

- Concrete provider adapters, provider-hosted checkout/session creation, tokenized saved-method/mandate handling, subscription schedules, and real provider settlement ingestion/application.
- Full subscription autopay attempts, verified recurring collection, failure/retry/pause/resume/cancellation/expiration, and Flow F proof.
- Full Accounts Receivable, expenses, tax/provision, gateway-fee, budget, project-economics, document-metadata, and expanded finance dashboards. The new finance view does not fabricate missing values.
- Actual refund, commission, vendor-payment, or beneficiary execution. These require later explicit approval and provider integration.
- Extension of partner roles/lifecycle/KYC metadata, commission plans, recurring/lifetime-originator options, dashboard, and payout approval integration.
- Full partner maturity transition service, performance dashboard, configurable exclusion calculation against collected amounts, and commission-release approval integration.
- Branded customer verification HTML pages; the local JSON verification APIs and service controls are implemented.
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

Continuation verification:

- Phase 6 backend + authenticated HTTP suites: **36 passed** in 26.69s.
- Phase 6 commercial, partner/risk, and authenticated HTTP suites after the follow-on slice: **50 passed** in 38.40s.
- Related regression across Phase 6, partner management, commercial, billing, and customer portal: **179 passed** in 141.11s.
- Broader focused run across Phase 6, billing, finance ledger, partner management, commercial, customer portal, and HQ HTTP: **254 passed, 1 failed** in 222.78s.
- The one failure is the pre-existing `WorkforceMediaHQServerTests::test_executive_coordinator_full_loop_over_http` two-second client timeout at `/api/executive/sync`. It reproduced alone (1 failed in 3.49s). The route does not pass through any `/api/p6/*` code and no assertion failed; the HTTP response did not arrive inside the test's fixed two-second timeout. It was not weakened, skipped, or represented as fixed.
- Python compilation for `falguna/phase6_commercial.py` and `falguna/hq_web.py`: passed.
- `git diff --check`: passed.
- Finalization focused suites (`test_phase6_financial_flows`, `test_phase6_partner`, `test_phase6_commercial`, `test_phase6_hq`): **61 passed** in 49.63s.
- Finalization related regression across Phase 6, billing, finance ledger, partner management, commercial, and customer portal: **203 passed** in 163.33s.
- Python compilation for `falguna/phase6_financial_flows.py` and `falguna/hq_web.py`: passed.

## End-to-end flows proven in this continuation

- **Flow A — Commercial Payment: proven for the internal sandbox path.** Synthetic linked customer/invoice -> authenticated checkout -> verified capture -> separate verified settlement -> matched reconciliation -> receipt -> existing billing ledger marks paid -> finance cash event -> cash-position read. A partial settlement proves `PARTIALLY_PAID`; amount/currency mismatches prove no invoice/receipt/cash mutation.
- **Flow B — Partner Commission: proven through the sandbox releasable boundary.** Synthetic partner/referral/opportunity/invoice and cleared collection -> configured exclusion -> risk check -> maker -> independent verifier -> Aryan approval -> `RELEASABLE` -> idempotent commission-payable ledger event. No payout method exists.
- **Flow C — Fraud Hold: proven for the internal control path.** Evidence-backed suspicious action -> risk event -> human financial/control hold -> append-only review history -> no finance event or money movement. A human suspension action also suspends the linked approved partner; FALGUNA cannot do this.
- **Flow D — Customer Verification: proven for the local synthetic API/service path.** Public partner lookup is opt-in and data-minimized. Payment instruction lookup requires exact payment/customer/beneficiary matching, rejects forged beneficiaries, and withholds payment details when the customer reference is wrong.
- **Flow E — Refund: substantially proven, but not final-acceptance complete.** Synthetic settled payment -> bounded partial refund request -> maker -> independent verifier -> Aryan approval -> verified sandbox confirmation -> one cash-ledger debit -> proportional commission clawback. A separate refund reconciliation record/customer-facing refund status and dispute-versus-refund race proof remain deferred.
- **Flow F — Subscription: partial.** Token/mandate-reference subscription -> due-cycle draft invoice is proven. Synthetic autopay -> verified collection -> settlement -> receipt is not yet proven.
- **Flow G — Outgoing Payable: proven through the sandbox execution-ready boundary.** Synthetic payable -> new-beneficiary flag -> maker -> independent verifier -> Aryan approval -> `SANDBOX_EXECUTION_READY`, with no finance event or payment execution.

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
- Commissions stop at `RELEASABLE`; payables stop at `SANDBOX_EXECUTION_READY`; neither can execute or call an external provider. Refund confirmation is a local verified-sandbox record only and calls no provider.
- Every modeled outgoing action requires an independent verifier and Aryan owner approval, then stops at `APPROVED_PENDING_EXECUTION`.
- FALGUNA/AI cannot act as maker or final approver and no AI execution method exists.

## Exact next steps

1. Complete subscription autopay attempt/failure/retry/pause/resume/cancel/expiry and prove Flow F through the existing verified payment/reconciliation path.
2. Add refund reconciliation/customer-facing status and dispute-versus-refund double-reduction protection.
3. Add beneficiary allowlist/change re-verification and payable reconciliation records.
4. Finish acquisition/source economics, customer-lifetime/account-expansion, service tiers, and bounded jurisdiction/opportunity policy records without invented cost or profitability data.
5. Add the mandatory multi-connection concurrency/race suite and repair any exposed transaction-boundary defects.
6. Run full repository regression and authenticated desktop/mobile browser acceptance. Until these pass, this remains a checkpoint and not final acceptance. Do not begin Phase 7.
