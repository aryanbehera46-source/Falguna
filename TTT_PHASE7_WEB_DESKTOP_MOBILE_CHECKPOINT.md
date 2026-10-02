# TTT / FALGUNA Phase 7 — Web, Desktop & Mobile Applications Checkpoint

**Date:** 2026-10-02  
**Status:** **PHASE 7 — FINAL ACCEPTANCE**
**Branch:** `phase7/web-desktop-mobile-v1`  
**Baseline:** accepted Phase 6 commit `0c49f9b` (`phase6/commercial-platform-foundation-v1`)  
**Environment:** isolated worktree and synthetic browser/test data only

## Decision

Phase 7 closure was completed on 2026-10-02. The authoritative acceptance evidence, final implementation scope, regression totals, browser QA, accessibility review, threat review and production-activation gates are recorded in `TTT_PHASE7_FINAL_ACCEPTANCE_REPORT.md`. This checkpoint is retained as the validated pre-closure baseline; its former remaining-work list is superseded by that report.

Phase 7 uses one TTT domain ecosystem and a responsive-web-first application architecture. The existing stdlib server-rendered TTT website remains the public front door and shares the established TTT state store. Customer, partner and payment experiences are separate external surfaces with their own authentication boundary; the staff/admin HQ is not linked from public navigation.

Native desktop/mobile wrappers are not justified in this checkpoint. The current requirements are covered by responsive web at substantially lower operational and security cost. The application boundary, routes and server-side services preserve a future wrapper/API path. PWA installation/offline behavior is deferred until an actual offline, push-notification or device-integration requirement exists.

## Implemented

### Public commercial gateway

- Added an eight-intent homepage router for software, AI/automation, Business Launch & Growth, specialist/provider routing, partner entry, customer portal, invoice/payment verification and FALGUNA.
- Added public `/solutions`, `/business-launch-growth`, and `/partners` pages.
- Added accountable routing language for direct delivery, advisory, coordinated specialists, providers/referrals, products and licensed-professional boundaries.
- Added the new public routes to the sitemap and excluded authenticated routes from robots.
- Removed public-page wording that represented Twenty Two Technologies Pvt. Ltd. as the currently operating legal entity. The site now presents the Twenty Two Technologies brand without changing the deferred corporate-status decision.

### External identity and session boundary

- Added `p7_external_accounts` and `p7_external_sessions` as additive tables.
- Accounts can only be provisioned by trusted code against an already ACTIVE immutable Phase 6 `CUSTOMER` or `PARTNER` identity. There is no public sign-up.
- Passwords use the existing PBKDF2-HMAC-SHA256 implementation; sessions are opaque, expiring, revocable, HttpOnly and SameSite=Lax.
- Login failures are constant-shape, rate-limited and lock accounts after repeated failures.
- Role, organization and subject are always resolved server-side from the linked Phase 6 identity. Browser parameters never choose a tenant.
- External logout is protected by the session CSRF token.

### Customer application

- Added `/app`, `/app/projects`, `/app/billing`, and `/app/support`.
- Uses the existing `CustomerPortalService` and its customer-safe projections rather than a parallel store.
- Displays project state, delivery route, invoice amount/received/outstanding truth, due dates, refund/dispute-backed bundle data, and a support/communications foundation.
- A customer identity's immutable `subject_ref` selects the one permitted communications organization; cross-customer selection is not exposed.
- Added customer-scoped project detail routes with the approved SOW, recorded milestones, and client-owned invoices. Unknown and cross-customer project IDs return the same 404.
- Added customer-safe communication history. Internal notes, internal sender identity, drafts, failures, and approved-but-not-actually-sent outbound messages are excluded; only customer inbound messages and genuinely `SENT` replies render.

### Partner application

- Added `/partners/app`, `/partners/app/leads`, and `/partners/app/commissions`.
- Displays only the linked partner profile, registered leads, recorded contributions and commission records.
- Surfaces verification/KYC/policy metadata seams and the explicit prohibition on collecting customer money.
- Does not expose private customer finance or any payout execution path.
- Added authenticated, session-CSRF-protected policy acknowledgement and lead registration.
- The immutable external identity selects the partner; request fields cannot choose or reassign ownership.
- Existing duplicate/conflict detection remains the attribution authority: new referrals remain `PENDING_REVIEW`, conflicts are flagged for review, and an existing valid attribution is not silently replaced.
- Added self-reported industry and relationship-disclosure fields without changing the accepted attribution lifecycle.

### Public self-service intake

- Added a structured, self-selected commercial-need category to both contact forms and persisted it as a routing hint.
- This is not represented as automated AI classification; staff still triage the submitted category and free text.

### Payment and verification surface

- Added `/pay` and `/pay/verify` using the accepted Phase 6 provider-neutral state.
- Signed-in customers see only payment intents matching both the Phase 6 organization and their immutable customer reference.
- Displays status, amount/currency, sandbox provider/session metadata, allowed method capabilities, token/mandate references and subscription/autopay state without raw card data.
- Public verification requires exact payment, customer and beneficiary references and retains Phase 6 non-disclosure behavior for mismatches.
- The UI states that it is sandbox-only, that browser output cannot establish payment success, and that unofficial representative instructions are invalid.

### Operability

- Added the supported `python -m falguna ... site` command, closing the Phase 6 pre-production launcher/CLI gap for the website/login surface.

## Security invariants preserved

- Phase 6 immutable commercial identities remain the authorization root.
- Customer/partner role isolation and customer organization isolation are server-enforced.
- No staff/admin HQ link or public backdoor was added.
- No raw PAN/CVV storage or input was added.
- No AI approval or money-execution path was added.
- Payment state remains server/provider evidence controlled; the browser is display-only.
- No real customer, partner, payment or corporate data was used.

## Verification

- Python compilation: all six touched Python modules passed (`site_web`, `customer_portal`, `partner_management`, `phase7_portals`, `site_content`, and `store`).
- Customer portal, partner management, Phase 7 portal, and complete website HTTP suites: **121 passed** in 100.709s.
- Shared billing, commercial, commercial-web, payment-comms, Phase 6 commercial, finance, partner, final-approval-security, and Phase 6 HQ regressions: **178 passed** in 88.417s.
- Focused post-fix safety/UI-state checks: **9 passed** in 4.332s.
- Total recorded assertions across the final focused and shared regression runs: **299 passed**. The 9 focused checks are a subset and are not added to this unique total.
- CLI help exposes the new `site` command.

### Browser QA (local synthetic preview, port 8877; 2026-10-02 continuation)

- Public routes checked: homepage/all eight intents, Business Launch & Growth, partner program, project intake/category selector, and sandbox payment/verification surface.
- A synthetic customer completed the real login flow and opened overview, project list, project detail, SOW, milestones, linked invoice, billing, and customer-safe support history.
- A synthetic partner completed the real login, policy acknowledgement, lead form, lead registration, lead list, overview, and commission-status routes. The created browser-QA lead remained `PENDING_REVIEW` under the authenticated partner.
- Desktop at 1440×960 and mobile at 390×844: every checked public/customer/partner route reported document width equal to viewport width, with no horizontal overflow.
- Direct route navigation confirmed no project-detail/list shadowing or partner lead-route shadowing.
- Browser console collection reported no warnings or errors on the checked flows.
- No external provider, payment, email, WhatsApp, deployment, or live-data action was used.

Two concrete defects found during validation were fixed before this checkpoint:

1. The partner-safe bundle omitted `policy_acknowledged_at`, so the UI could continue presenting the gate after acknowledgement. The safe timestamp projection and an explicit rendered-form transition assertion were added.
2. Customer communication filtering treated `APPROVED` outbound content as customer-visible even though the communications state machine only establishes a real send at `SENT`. The projection now exposes outbound messages only at `SENT`, with a regression test for approved-but-unsent content.

The existing website test harness reports Python `ResourceWarning` messages for test-server sockets that are not explicitly closed after shutdown. All assertions pass; this is test-harness cleanup debt, not an application-route failure.

## Not completed / deferred

Phase 7 is not complete. Remaining work includes:

1. Complete the remaining customer financial presentation: authenticated invoice/receipt detail plus refund, dispute, and subscription views. Current billing still renders the invoice table only.
2. Add the partner contribution-detail view and partner-facing public-verification UI where the accepted Phase 6 record supports them; keep all provider actions sandbox-only.
3. Add external account provisioning/admin workflow, recovery, MFA/WebAuthn decision, session management, and security-event/audit views before production use.
4. Add dedicated repeatable browser automation for authenticated customer, partner, and payment journeys; this checkpoint completed manual real-browser QA only.
5. Perform accessibility keyboard/screen-reader review and production threat review.
6. Split host-aware canonical URLs/navigation only when real `app.`, `clients.`, `partners.`, and `pay.` subdomains are configured; no DNS work was performed.
7. Decide on PWA installation/push/offline only from real requirements; native wrappers remain unjustified today.

These are substantive acceptance items, not merely report cleanup. Phase 7 therefore cannot move directly to final acceptance yet, but the Claude continuation slice itself is validated and ready for a clean checkpoint commit.

## Files in this validated slice

- `falguna/customer_portal.py`
- `falguna/partner_management.py`
- `falguna/phase7_portals.py`
- `falguna/site_content.py`
- `falguna/site_web.py`
- `falguna/store.py`
- `tests/test_customer_portal.py`
- `tests/test_partner_management.py`
- `tests/test_phase7_portals.py`
- `tests/test_site_web.py`
- `TTT_PHASE7_WEB_DESKTOP_MOBILE_CHECKPOINT.md`

## External-action ledger

No push, merge, deploy, DNS change, live provider activation, real-data write, spending, customer/partner contact, public publication, or payment execution occurred.

## Exact next action

Phase 7 is closed. Preserve this branch and its acceptance evidence. Start no further work here unless a concrete Phase 7 defect is found; any Phase 8 work requires a separate explicit roadmap decision and run.
