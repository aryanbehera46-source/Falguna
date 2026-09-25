# Revenue Trial 1 — Royal Table as Client-Delivery Simulation

**Status label for this whole document: SIMULATED SALES SCENARIO built on REAL, VERIFIED delivery capability.**
The prospective client, their brief, the proposal and pricing below are a realistic rehearsal, not an actual signed
deal. Every capability claim about Royal Table itself is grounded in this session's live verification (see
`TTT_FALGUNA_PHASE_A_REPORT.md` Section 4) or in `Freelancing/restaurant-website/README.md` and
`docs/day18-case-study.md`, which describe the shipped, tested system. Nothing below assumes an agent,
integration, or workflow that isn't actually built yet.

## 0. Why Royal Table is the right simulation vehicle

Royal Table is TTT's first real, working, deployed product: a full-stack restaurant reservation + operations
platform (reservations, multi-order KOT kitchen flow, itemized billing, payments, analytics, inventory, staff
RBAC, a read-only AI kitchen-briefing endpoint). It has a live customer-facing frontend, a documented API, a
persistent database, and an existing internal case study and demo script. That makes it the one asset TTT can
point to today and say "we built this, here is the live link" — everything else in TTT (Media, Trading Lab,
Venture Studio) is either paper-trading or not yet revenue-generating. Revenue Trial 1 therefore treats Royal
Table's proven capability as the *product TTT sells to a new prospective client*, and rehearses the full
Sales → Scope → Proposal → Delivery → Case Study pipeline TTT would run for a real deal of this shape.

---

## 1. Client Brief (simulated prospect)

**Client:** Anaya Kapoor, owner-operator, "Spice Route" — a 60-seat North Indian restaurant in Pune, currently
taking reservations by phone and WhatsApp, running kitchen tickets on paper, and reconciling bills by hand at
close.

**How the lead arrived (simulated):** Inbound referral from a hospitality-industry contact after seeing the Royal
Table live demo link. In a real pipeline this lead would be logged in TTT HQ under Client Services → Revenue
Hunter (`rhOpportunities` / `rhPipeline`), the same real, honest discovery pipeline this session verified is
already live (Section 3 below shows the exact view).

**Stated problems:**
- Phone/WhatsApp reservations get missed or double-booked during dinner rush.
- No single source of truth between the host stand and the kitchen — orders are re-shouted or re-written.
- End-of-night billing is manual addition on a notepad; discounts and split payments are a common source of
  disputes.
- No visibility into which dishes are actually selling versus what's on the menu.
- Basic pantry/inventory tracking is on a WhatsApp group with the head chef.

**Stated constraints:**
- Budget-conscious independent restaurant, not a chain — cannot afford enterprise POS hardware or a
  multi-month build.
- Wants something live before the restaurant's first anniversary event in 10 weeks.
- One admin user (the owner) and one Chef/kitchen login are enough for launch; no multi-branch need.
- Explicitly does not want online payment gateways or QR ordering yet — cash/UPI/card handled in person is fine.

**Explicitly out of scope per the client:** payroll, supplier ordering, multi-branch, POS hardware integration —
which conveniently matches Royal Table's own documented "Deliberate future scope" exclusions, so no
expectation-setting gap exists between what the client wants and what the platform already deliberately does not
do.

---

## 2. Scope (generated from the brief, mapped to verified Royal Table capability)

| Client need | Royal Table capability it maps to (verified) | New work required |
|---|---|---|
| Online + phone-backed reservations with capacity checks | Reservation capacity-check flow (verified live in this session's frontend test — see Section 6) | Brand the front end for Spice Route; none of the reservation logic is new |
| Kitchen order flow, no more re-shouted orders | Order → KOT → Chef status queue (`New → Accepted → Preparing → Ready → Served`) | Chef login provisioning only |
| End-of-night billing without manual math | Server-authoritative itemized billing, discounts, tax/service charge, partial/full payment recording | None — used as-is |
| Menu sales visibility | Actual-order analytics (orders, billed/paid revenue, AOV, top dishes, top category) | None — used as-is |
| Pantry tracking beyond a WhatsApp group | Inventory + stock adjustments + grocery requirements module | Initial stock data entry (client-side task) |
| Kitchen "what's coming" visibility for the head chef | Read-only AI kitchen briefing endpoint (rate-limited, reservation/order/inventory-aware) | None — used as-is |
| Deliberately excluded | Online payment gateway, QR ordering, multi-branch, payroll, supplier ordering, POS hardware | Not built for this client either, matching the client's own stated exclusions |

**Scope conclusion:** this is a *configuration and re-branding engagement on a proven platform*, not a
from-scratch build. That is the honest, sellable story — faster delivery, lower price, lower risk than a bespoke
build, because the hard engineering (server-authoritative pricing, sequential KOT state machine, role-separated
auth, safe additive migrations) is already shipped and already has a regression suite.

---

## 3. Where this would run inside TTT (real views, verified live this session)

| Delivery stage | TTT HQ location (verified to exist and render) | What's real today vs. what Agent Builder V1 would automate |
|---|---|---|
| Sales / lead capture | Client Services → Today (`rhToday`), Opportunities (`rhOpportunities`), Sales Pipeline (`rhPipeline`) | REAL: Revenue Hunter already runs live discovery against Remotive/WeWorkRemotely (38 opportunities found in this session's check, pipeline value $61,543). Manually logging an inbound referral lead like Anaya's into that same pipeline is a normal, human, low-effort step today — not automated, not pretended to be. |
| Scope / proposal drafting | No dedicated view yet — would currently be a Workforce Task (`wfTasks`) of type `proposal_drafting`, department `client_services` | MANUAL today: a human (or Falguna Chat/Work, assisted) drafts it, as this document itself was drafted. Agent Builder V1's Sales agent is the gap that would automate first-draft generation. |
| Engineering / delivery build | Active Jobs / Delivery (`rhActiveJobs`) | REAL as a tracking surface (view exists, verified reachable); the actual engineering work for a new client (branding, Chef login provisioning, stock data entry) is human work, honestly labeled MANUAL — no Engineering agent exists yet. |
| QA | Workforce Tasks (`wfTasks`) with `task_type=qa_verification`, exactly the pattern this session used to verify the workforce-visibility fix (see `TTT_FALGUNA_PHASE_A_REPORT.md`) | REAL mechanism (the task/transition/evidence data model is live and this session proved it renders status, blockers, and "waiting for Aryan" correctly); no autonomous QA agent exists yet to create these tasks itself — a human runs the golden-workflow regression suite Royal Table already has. |
| Delivery / handover | Active Jobs / Delivery (`rhActiveJobs`) + Department Performance (`ccDeptPerf`) | REAL tracking surfaces; handover itself (credential rotation, training the owner, deployment) is MANUAL. |
| Executive visibility | CEO Brief (`coCeoV2`) | REAL — verified this session to honestly aggregate objectives/clients/revenue/risks as a real, currently-empty report (no fabricated ventures), i.e. exactly "AI works, Aryan gets reports/decisions" once a real client and job exist in the data. |

**Honest gap statement:** every stage above already has a real place to live in TTT's data model and UI. The
missing piece for full automation is not the data model or the UI — both already exist and were verified live —
it is the absence of actual autonomous Sales/Engineering/QA/Delivery agents that would create and advance these
records without a human doing it by hand. That is precisely the Agent Builder V1 gap, and it is the same
conclusion this session already reached independently for Workforce visibility.

## 4. Proposal (client-facing draft, simulated)

> **To:** Anaya Kapoor, Spice Route
> **From:** Twenty Two Technologies (TTT)
> **Re:** Reservations, kitchen and billing platform — proposal

Spice Route needs one system that takes a booking, tells the kitchen exactly what to make, bills it correctly,
and shows you what's actually selling — without the cost or timeline of a custom build. We already operate
**Royal Table**, a live restaurant operations platform built and tested for exactly this workflow
(reservation → order → kitchen ticket → itemized bill → payment → analytics). Rather than building you something
new from zero, we configure and brand this proven platform for Spice Route, which is why we can commit to a
launch before your anniversary event.

**What you get:** a branded reservation site for Spice Route, an Admin console for you, a Chef console for your
kitchen, itemized billing with discounts/tax/service charge, cash/UPI/card payment recording, sales analytics by
dish and category, basic inventory and grocery-planning, and a read-only AI briefing your head chef can check
before service. What we deliberately do not include, matching what you told us: online payment gateways, QR
ordering, multi-branch, payroll, or POS hardware.

**Timeline:** 3 weeks to a working, branded, client-reviewable environment; 10 weeks total including a
supervised soft-launch window before your anniversary event (see Milestones).

**Investment:** see Section 7 (Pricing) — a fixed build/configuration fee plus a monthly platform + support fee,
not a large custom-development quote, because the platform already exists and is already tested.

---

## 5. Milestones

| # | Milestone | Target | Exit condition |
|---|---|---|---|
| M1 | Kickoff & data intake | Week 1 | Menu (items, prices, categories), branding assets, opening hours, and staff list received from client |
| M2 | Branded environment live (staging) | Week 3 | Spice Route–branded reservation site + Admin + Chef consoles reachable at a staging URL; sample reservation walks through the full golden workflow (reservation → order → KOT → bill → payment → receipt) |
| M3 | Client UAT | Week 5 | Owner and head chef run real-looking scenarios themselves; acceptance criteria in Section 6 checked off |
| M4 | Production cutover | Week 7 | Production domain, production database, real credentials issued and rotated, staff trained |
| M5 | Supervised soft-launch | Weeks 7–10 | Real service nights run on the platform with TTT on-call; anniversary event covered live |
| M6 | Handover & maintenance start | Week 10 | Handover package delivered (Section 8); maintenance plan begins |

---

## 6. Acceptance criteria

1. A customer can make a reservation on the branded site and receive a confirmation; a duplicate/over-capacity
   booking for the same date and time is rejected with a clear message (this exact capacity-check and
   fetch-failure error path was live-tested this session against the real Royal Table frontend).
2. Admin can attach one or more orders to a reservation; each submitted order becomes a KOT visible on the Chef
   console and advances only in the sequence New → Accepted → Preparing → Ready → Served (no state can be
   skipped).
3. Prices and bill totals are calculated by the server from the live menu, not editable from the browser; a
   manipulated client-side price is rejected (documented and covered by Royal Table's existing regression suite).
4. Admin can apply an itemized discount and configurable tax/service charge, record a cash, card, UPI, or partial
   payment, and print a receipt that matches the recorded totals.
5. Admin can view actual-order analytics (orders, billed revenue, paid revenue, average order value, top dishes,
   top category) for a selected date range.
6. Chef can view a read-only AI-generated kitchen briefing summarizing the day's reservations, actual orders,
   open KOTs, and any low-stock ingredients.
7. Admin and Chef logins are role-separated: a Chef credential cannot reach billing, payment, or staff-management
   endpoints.
8. All of the above pass Royal Table's existing automated suite (`npm test`, `npm run test:day18`,
   `npm run test:final-qa`, `npm run test:week-demo`) plus one manual walkthrough with the actual client.

---

## 7. Pricing recommendation (assumptions stated explicitly)

**This is a recommendation for discussion, not a quoted or binding price** — Claude is not a financial advisor
and this is not investment or legal advice; final pricing is Aryan's decision.

Assumptions:
- Local (India) independent-restaurant client, not an enterprise chain.
- Reuse of the existing Royal Table codebase (branding/config engagement), not a from-scratch build — this is
  the single biggest driver of a lower price than a bespoke quote.
- Render's paid tier (not the free tier that caused this session's "Service Suspended" finding) is required for
  a client-facing production deployment, to avoid the exact backend-availability gap this trial surfaced.
- One Admin + one Chef account at launch; no multi-branch, no payment gateway integration.
- TTT, not the client, hosts and operates the backend and database (client pays a recurring platform fee rather
  than owning infrastructure).

| Item | Recommendation | Basis |
|---|---|---|
| One-time setup/branding/configuration fee | ₹35,000–₹55,000 (~$420–$660) | Reflects configuration + data intake + UAT support, not new engineering; existing case study/demo materials shorten the sales and onboarding cycle |
| Monthly platform + support fee | ₹6,000–₹9,000/month (~$70–$110) | Covers a paid Render tier, database backups, and a bounded monthly support allowance (see Section 8) |
| Optional: dedicated inventory/grocery onboarding session | ₹5,000 flat (~$60) | One-time data entry help for initial stock counts |

A ~30–40% first-client discount off the low end of this range is a reasonable option if Spice Route agrees to be
a named reference/case-study client (consistent with how Royal Table itself was positioned).

---

## 8. Handover & maintenance offer

**Handover package:**
- Rotated production credentials for Admin and Chef, delivered privately (never by email in plaintext, per
  Royal Table's own documented security practice of never publishing Admin/Chef credentials).
- A short client-specific runbook: how to add/remove menu items, how to reset a Chef password, how to read the
  analytics view, how to export data.
- The existing `docs/day18-case-study.md`-style verification story, adapted so the client understands what was
  tested and how.

**Maintenance offer (recurring, tied to the monthly platform fee in Section 7):**
- Uptime monitoring on the paid Render tier (resolving this trial's finding that a suspended/free-tier backend
  fails silently on the reservation form — see Section 9).
- A bounded monthly support allowance (e.g., 2 hours) for menu/branding changes and questions.
- Quarterly review of actual-order analytics with the owner — an honest, low-effort version of the "AI works,
  owner gets a report" pattern TTT is building toward at the company level.
- Security patching and dependency updates, batched, not ad hoc.

---

## 9. Findings surfaced by this trial (honest, from real testing — not simulated)

1. **Render free-tier backend can go from "asleep" to fully "Suspended"**, which does not self-recover on the
   next request the way a normal free-tier cold start would. Confirmed live this session: `GET
   https://royal-table-api.onrender.com/` returns a static "Service Suspended" page, and no authorized Render
   dashboard session exists in the linked browser (confirmed against the plain sign-in screen) — resuming it
   requires Aryan to sign in to Render manually (GitHub is the most likely auth method given the repo's deploy
   flow) and resume/restart the `royal-table-api` service from the dashboard. This is exactly the kind of gap a
   paying client cannot tolerate, and is the direct justification for the paid-tier line item in Section 7's
   pricing.
2. **Reservation submission fails silently on the client-facing site when the backend is unreachable.** The menu
   section already shows a clear "Unable to load our menu." message on fetch failure, but submitting the
   reservation form itself only logs a console error (`TypeError: Failed to fetch`) with no visible message to
   the person filling out the form. This is a genuine, minor UX gap in the Royal Table codebase (a separate
   client repository from `falguna-bootstrap`), noted here for Aryan's awareness rather than fixed in this
   session, since it sits outside the Phase A scope and branch this trial was scoped to.
3. No live booking/admin flow could be completed end-to-end in this trial because the backend was unreachable
   for the entire session (confirmed suspended, not merely cold). This is reported honestly rather than
   simulated: Acceptance criteria in Section 6 are written from the documented/tested golden workflow in
   `docs/day18-case-study.md`, not from a live run performed in this session.

## 10. Client-ready case study draft (verified capabilities only)

*(This is written to hand to a prospective client, e.g. Spice Route or the next lead in the pipeline. It restates
only what this session verified live or what Royal Table's own tested documentation describes — nothing about a
specific client's results, since Royal Table has no real paying client yet.)*

> ### Royal Table — from paper tickets to a connected kitchen
>
> **The problem.** Independent restaurants usually run reservations by phone, kitchen orders on paper, and
> billing by hand — three disconnected systems that don't explain each other. A reservation total can't tell you
> what a table actually ordered; a kitchen ticket can't tell the front desk if a table's bill is settled.
>
> **What we built.** Royal Table connects the full flow: a customer reserves online with live capacity checks,
> staff attach one or more itemized orders to that reservation, each order becomes a kitchen ticket the Chef
> advances through a controlled sequence (New → Accepted → Preparing → Ready → Served), and the same order data
> produces the final itemized bill, payment record, and receipt. Nothing is priced or totaled in the browser —
> the server is the only source of truth for money.
>
> **What it already does, live:**
> - Customer-facing reservation site with real-time capacity checking (verified live).
> - Multi-order kitchen ticket workflow with enforced state sequencing.
> - Itemized billing with discounts, configurable tax/service charge, and cash/card/UPI/partial payment
>   recording.
> - Actual-order sales analytics: revenue, average order value, top dishes, top category.
> - Inventory and grocery-planning tied to the same kitchen data.
> - A rate-limited, read-only AI kitchen briefing that reads real reservation/order/inventory data — not a
>   generic chatbot bolted on top.
> - Role-separated Admin and Chef access, so kitchen staff never touch financial controls.
>
> **How we know it works.** Royal Table has its own automated test suite covering the full golden workflow —
> reservation, multiple orders, price-tampering rejection, invalid-quantity rejection, sequential KOT
> enforcement, billing adjustments, partial-then-full payment, and receipt/analytics correctness — plus an
> independent security and operations regression suite that runs on every change.
>
> **What we deliberately left out (for now).** Online payment gateways, POS hardware, QR ordering, payroll,
> multi-branch operations, and full recipe costing are intentionally outside this release — so we don't oversell
> what the platform does today.
>
> **Honesty note included by design:** Royal Table is TTT's own reference deployment. As of this case study's
> writing, its live production backend is temporarily suspended on its hosting provider's free tier pending a
> paid-tier upgrade — the platform's tested capability is real and unchanged, but a prospective client's
> production deployment would run on a paid tier from day one specifically to avoid this.

---

## 11. Sales readiness assessment

**Ready to show a prospect today:** the frontend (live, branded, professional), the documented feature set, the
existing internal case study and demo script, and this trial's real proposal/pricing/milestone template.

**Not ready to show a prospect today:** a live end-to-end demo of the reservation → payment flow, because the
backend is currently suspended. Before any real sales call that needs a live demo, Aryan must first sign in to
Render and resume the `royal-table-api` service (Section 9, finding 1) — this is the single blocking action
between "sales-ready on paper" and "sales-ready live."

**Recommended fix before next paying pitch:** upgrade Royal Table's Render service off the free tier (or at
minimum resume it and monitor it) so a live demo link never shows "Service Suspended" mid-pitch, and add a
visible user-facing error message to the reservation form's submit-failure path (Section 9, finding 2) so a
real client never sees a request silently vanish.
