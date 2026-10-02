# TTT / FALGUNA Phase 7 — Web, Desktop & Mobile Applications Checkpoint

**Date:** 2026-10-02  
**Status:** PHASE 7 CHECKPOINT — NOT FINAL ACCEPTANCE  
**Branch:** `phase7/web-desktop-mobile-v1`  
**Baseline:** accepted Phase 6 commit `0c49f9b` (`phase6/commercial-platform-foundation-v1`)  
**Environment:** isolated worktree and synthetic browser/test data only

## Decision

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

### Partner application

- Added `/partners/app`, `/partners/app/leads`, and `/partners/app/commissions`.
- Displays only the linked partner profile, registered leads, recorded contributions and commission records.
- Surfaces verification/KYC/policy metadata seams and the explicit prohibition on collecting customer money.
- Does not expose private customer finance or any payout execution path.

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

- Python compilation: `falguna/site_web.py` and `falguna/phase7_portals.py` passed.
- New Phase 7 portal tests: **5 passed** (provisioning boundary, login/session revocation, customer isolation, payment isolation, partner isolation).
- Existing website HTTP suite plus new public/guard checks: **24 passed**.
- Phase 6 affected regression plus customer portal and Phase 7 suites: **84 passed** in 50.971s.
- CLI help exposes the new `site` command.

### Browser QA (local synthetic preview, port 8877)

- Homepage loaded with all eight intent routes and no browser console warnings/errors.
- Desktop at 1440×960: 4-column intent grid, full navigation, document width exactly 1440 (no horizontal overflow).
- Mobile at 390×844: 1-column intent grid, mobile navigation toggle, document width exactly 390 (no horizontal overflow).
- Customer/partner login rendered at mobile width; unauthenticated `/app` redirected to `/portal/login`.
- Payment surface visibly showed sandbox status and the no-representative-money warning.
- A synthetic customer completed the real login form and reached `/app`; the account resolved to the correct customer application with project/metric state and no console warnings/errors.

The existing website test harness reports Python `ResourceWarning` messages for test-server sockets that are not explicitly closed after shutdown. All assertions pass; this is test-harness cleanup debt, not an application-route failure.

## Not completed / deferred

Phase 7 is not complete. Remaining work includes:

1. Complete customer milestone/timeline/action-required/SOW presentation and detailed receipt/refund/subscription screens.
2. Add partner lead registration and policy acknowledgement mutations with session CSRF, duplicate protection and the accepted human gates.
3. Add authenticated invoice lookup/receipt detail and partner public-verification UI; keep all provider actions sandbox-only.
4. Add external account provisioning/admin workflow, recovery, MFA/WebAuthn decision, session management and security event/audit views before production use.
5. Split host-aware canonical URLs/navigation when real `app.`, `clients.`, `partners.` and `pay.` subdomains are configured; no DNS work was performed.
6. Add structured campaign/service/industry landing-page content and richer intake classification while preserving verified-capability claims.
7. Run wider repository regression in a controlled media/provider environment and add dedicated browser automation for authenticated customer, partner and payment journeys.
8. Perform accessibility keyboard/screen-reader review and production threat review.
9. Decide on PWA installation/push/offline only from real requirements; native wrappers remain unjustified today.

## External-action ledger

No push, merge, deploy, DNS change, live provider activation, real-data write, spending, customer/partner contact, public publication, or payment execution occurred.

## Exact next action

Build the next authenticated vertical slice on this branch: customer project detail with milestone/timeline/action-required/SOW data, invoice/receipt/refund detail, and automated HTTP/browser isolation tests. Then add the partner lead-registration mutation with CSRF, duplicate-safe attribution and policy acknowledgement—still synthetic and sandbox-only.
