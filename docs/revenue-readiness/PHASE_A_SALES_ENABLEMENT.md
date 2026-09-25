# Phase A — Sales Enablement: Templates, Feasibility & Sales-Readiness Checklists

Part of TTT + Falguna PHASE A (Commercial UX + Executive HQ + Revenue Readiness). This document is the PASS 4 deliverable: reusable templates, a $30k+ feasibility checklist, a sales-readiness checklist, and a verified Royal Table demonstration brief.

It does not introduce a new pipeline or CRM. Every stage referenced below is the real `LIFECYCLE_STATES` sequence already implemented in `falguna/lifecycle.py` (`DISCOVERED → RESEARCHING → QUALIFIED → PITCH_READY → AWAITING_APPROVAL → APPROVED → APPLYING → CONTACTED → REPLIED → DISCOVERY_CONVERSATION → NEGOTIATING → WON → ONBOARDING → DELIVERY → CLIENT_REVIEW → COMPLETED → INVOICED → PAID → RETAIN`, with `LOST` reachable from any pre-Won state). The templates below are the human/agent content that fills each stage — not a replacement for it.

---

## 1. How this maps onto the real pipeline

| Lifecycle state(s) | What happens | Template to use |
|---|---|---|
| DISCOVERED → QUALIFIED | Lead identified, fit assessed | Discovery Template |
| PITCH_READY → AWAITING_APPROVAL → APPROVED | Proposal drafted and approved by Aryan before it goes out | Proposal Template |
| APPLYING → CONTACTED → REPLIED → DISCOVERY_CONVERSATION → NEGOTIATING | Outreach, first conversation, scoping | Scope Template (drafted here, finalized at WON) |
| WON → ONBOARDING | Contract/agreement signed, kickoff | Scope Template (final) + Milestone Template |
| DELIVERY | Engineering execution | Milestone Template + QA Template (continuous) |
| CLIENT_REVIEW | Client sign-off per milestone or at completion | QA Template (client-facing summary) |
| COMPLETED → INVOICED → PAID | Handover, billing | Handover Template |
| RETAIN | Ongoing relationship | Maintenance Template |

---

## 2. Discovery Template

Use during QUALIFIED, before a proposal is drafted. Keep it to one page — this is a qualification record, not a design document.

- **Client / organization:**
- **Source:** (Revenue Hunter opportunity ID, inbound referral, direct outreach — link the record)
- **Stated need (client's own words):**
- **Real underlying problem (our read):**
- **Budget signal:** (stated figure, inferred range, or "unknown — ask")
- **Timeline pressure:** (hard deadline vs. flexible)
- **Decision maker(s):**
- **Technical constraints already known:** (existing stack, hosting, compliance, integrations)
- **Why TTT/Falguna is a credible fit:** (closest matching delivered project — Royal Table, ServiceFlow, Nivara Living, BriefPilot AI)
- **Fit verdict:** Qualified / Needs more discovery / Not a fit (reason)
- **Next action:** who does what by when

---

## 3. Proposal Template

Use during PITCH_READY. Must reach AWAITING_APPROVAL (Aryan's actual approval) before APPROVED — no proposal leaves the building without this gate.

1. **Cover summary** — one paragraph: the problem, the outcome, why us.
2. **Scope** — bullet list of what is included, written as deliverables the client can verify, not internal tasks.
3. **Explicitly out of scope** — prevents scope creep disputes later; borrow language from Royal Table's own "Deliberate future scope" pattern (name what is deliberately excluded and why).
4. **Approach & timeline** — phased if the project spans more than ~3 weeks; each phase ends in something demonstrable.
5. **Milestones & payment schedule** — tie every payment to a specific, client-verifiable milestone (see Milestone Template). Never bill for elapsed time alone.
6. **Team** — who is actually doing the work (see feasibility checklist §5 for when this requires more than solo capacity).
7. **What "done" looks like** — the acceptance criteria the client will use to sign off at CLIENT_REVIEW.
8. **Price and payment terms.**
9. **Assumptions and risks** — anything that could change scope or timeline if wrong.

Internal gate: this template's completed draft is what goes into TTT HQ's AWAITING_APPROVAL state for Aryan's review before APPROVED/send.

---

## 4. Scope Template

Use once NEGOTIATING resolves and again (finalized) at WON → ONBOARDING. This is the contract-adjacent document, not the sales pitch — precise, not persuasive.

- **In-scope deliverables** (numbered, each independently verifiable)
- **Out-of-scope** (explicit — reuse the proposal's exclusions, refined by negotiation)
- **Acceptance criteria per deliverable**
- **Change-order process** — how a scope change gets priced and approved mid-project (prevents unpaid scope creep)
- **Access & credentials needed from the client** (hosting, DNS, third-party accounts) — flag anything that requires the client to act before work can start
- **Assumptions that, if wrong, change price or timeline**
- **Security/compliance requirements** (see feasibility checklist if this is a larger/regulated engagement)

---

## 5. Milestone Template

Use throughout DELIVERY. One of these per milestone, not one giant plan — matches how Royal Table's own delivery was structured (day-numbered case study, incremental hardening passes).

- **Milestone name & number**
- **Maps to scope items:** (reference the Scope Template's numbered deliverables)
- **Definition of done:** (what the client can see/click/test to confirm it's real)
- **Target date**
- **Payment tied to this milestone:** (amount or % — leave blank if milestone-based billing isn't used for this engagement)
- **Evidence to capture at completion:** screenshots, test output, a short recorded walkthrough — whatever proves it without requiring the client to take our word for it
- **Status:** Not started / In progress / Blocked (reason) / Client review / Accepted

Note: there is currently no dedicated milestone-tracking data store in TTT HQ or Falguna — this is a real, acknowledged gap (see §9). Until a store exists, milestones are tracked as structured entries in the opportunity/project's free-text fields or as a project doc, using this template as the fixed format so entries stay comparable across projects.

---

## 6. QA Template

Use continuously during DELIVERY and as the CLIENT_REVIEW artifact. Modeled directly on what already exists and works for Royal Table (`docs/final-ux-qa-hardening.md`, `docs/day18-case-study.md` style) — this generalizes that pattern rather than replacing it.

- **What was tested:** (feature/flow)
- **How it was tested:** (manual click-through, automated test suite, both — name the actual method, never assert untested)
- **Environments checked:** (dev/staging/prod; desktop/mobile; light/dark if applicable)
- **Result:** Pass / Fail / Partial, with specifics
- **Evidence:** screenshot, console log, test run output — attach or link, don't just assert
- **Known issues at handover:** (explicit — never silently omit a known gap)
- **Regression check:** confirm nothing previously working broke (compare against the last clean baseline, exactly as this phase's own PASS 6 verification did)

---

## 7. Handover Template

Use at COMPLETED, before INVOICED.

- **What was delivered** (plain-language summary, links to the live product)
- **What was verified working** (from the QA Template — the client-facing rollup)
- **Known limitations at handover** (never hidden — e.g. Royal Table's Render free-tier suspension behavior, deliberately-excluded future scope)
- **Access handed over:** credentials, admin accounts, repo access, hosting/DNS ownership — confirm client actually has what they need to run this without us
- **Support window:** what's covered post-handover and for how long, before Maintenance terms apply
- **Invoice reference**

---

## 8. Maintenance Template

Use at RETAIN.

- **Support tier / retainer terms** (response time, scope of "support" vs. new work)
- **Monitoring in place:** (what's actually being watched — e.g. is anyone checking if a free-tier backend like Render has gone to sleep/suspended?)
- **Renewal/upsell opportunities noted** (factual — what the client has asked about or would plausibly need next)
- **Escalation contact**

---

## 9. Known pipeline gap — milestone tracking

There is no dedicated milestone data store today. `rh_opportunities`/the lifecycle events table tracks pipeline *state* transitions with evidence, but not sub-project milestones inside DELIVERY. Documenting this honestly rather than building a new tracker in this pass (per the explicit "avoid duplicating existing modules or building a new CRM from scratch" instruction): for now, use the Milestone Template above as a fixed-format entry inside the project's existing free-text/notes fields or a per-project doc. A dedicated milestone table (keyed to `opportunity_id`, mirroring the shape of this template) is the natural next build once delivery volume makes the manual version painful — not before.

---

## 10. $30,000+ Project Feasibility Checklist

Use before APPROVED on any opportunity above roughly $10–15k, and mandatory above $30k. All boxes should be genuinely answerable — a checklist item nobody can answer honestly is a red flag, not a formality to skip.

**Staffing**
- [ ] Can this be delivered by current capacity (Aryan + Falguna-assisted engineering) within the proposed timeline, or does it require bringing in a specialist/contractor?
- [ ] If a specialist is needed, has one actually been identified and their availability confirmed — not just "we could find someone"?
- [ ] Is there a single accountable owner for delivery, distinct from whoever sold it?

**Contractual scope**
- [ ] Is the Scope Template complete, with explicit exclusions, before any contract is signed?
- [ ] Does the contract specify a change-order process for scope changes mid-project?
- [ ] Are payment milestones tied to verifiable deliverables, not just elapsed time?
- [ ] Is there a kill clause / partial-delivery clause if the engagement needs to end early?

**Security requirements**
- [ ] Does this project handle PII, payment data, health data, or other regulated data? If yes, name the applicable regime (e.g. PCI-DSS scope for payments, GDPR/local equivalent for PII) and confirm it's actually achievable at the quoted price and timeline.
- [ ] Are credentials/secrets handling requirements clear (who holds production secrets, how are they rotated)?
- [ ] Does the client require a security review, pen test, or compliance attestation we don't currently produce? If yes, is that priced in or explicitly excluded?

**Delivery risk**
- [ ] What is the single biggest technical unknown, and has it been de-risked with a spike/prototype before committing to a fixed price?
- [ ] What happens if a third-party dependency (payment gateway, hosting platform, external API) is unavailable or changes — is there a fallback, and is the client aware of the dependency?
- [ ] Is the infrastructure this will run on production-grade for the stated scale (e.g. not a free tier that can suspend, if this is genuinely a $30k+ client-facing system)?
- [ ] Has a realistic timeline been stress-tested against current committed work, not assumed on top of it?
- [ ] Is there a clear "what does success look like at CLIENT_REVIEW" the client has already agreed to, in writing, before work starts?

**Verdict:** Proceed / Proceed with named conditions / Do not proceed (reason) — record this explicitly per opportunity; do not let a large opportunity slide into APPROVED without an explicit answer here.

---

## 11. Sales-Readiness Checklist

The point of this checklist: outreach can start as soon as these are true — it does not wait for the full future AI workforce, Trading, Media, or Agent Builder.

- [x] At least one credible, verifiable client demonstration exists (Royal Table — see §12).
- [x] A real acquisition→delivery pipeline exists end-to-end in the product (`falguna/lifecycle.py`, `revenue_hunter.py`, `opportunity_agent.py`, `handoff.py`) — not a mock.
- [x] TTT HQ presents this pipeline through a coherent executive interface (Ventures → Client Services: Today / Sales Manager / Opportunities / Outbound Leads / Sales Pipeline).
- [x] Reusable templates exist for discovery, proposal, scope, milestones, QA, handover, and maintenance (this document).
- [x] A feasibility checklist exists for larger/riskier engagements (§10).
- [ ] Royal Table's backend is out of "Service Suspended" state before it is used live in a client call (currently blocking — see §12; the customer-facing GitHub Pages site is live and usable for a static walkthrough in the meantime).
- [ ] External communication channels (email send, e-signature, invoicing/payment collection) are confirmed working end-to-end at least once with a real (even if internal/test) transaction, not assumed — mark each as verified individually rather than as one bundled item.
- [ ] A pricing floor and typical range has been decided for the kinds of projects we're pitching first (small business web apps in the Royal Table/ServiceFlow/Nivara Living class) so proposals aren't priced ad hoc.
- [ ] Aryan has reviewed and approved this checklist and the templates above as the standard to use, at least once, before the first real outreach message goes out.

**Bottom line:** the product-side blocker list for starting outreach is short and named (Royal Table's live backend, and confirming external send/payment connectors actually fire) — not "finish the AI workforce first."

---

## 12. Royal Table — Verified Features & Remaining Limitations (client-facing demonstration brief)

This complements, rather than replaces, the existing `docs/client-demo-script.md` (already a complete, polished 5-minute demo script) and `README.md` in the Royal Table repo. This section is the internal "what can we honestly claim" brief behind that script.

**Verified working (confirmed this pass, live):**
- Customer-facing site (GitHub Pages): **live, HTTP 200**, confirmed by direct request during this verification pass.
- Full documented product loop per the README: reservation → order(s) → KOT(s) → Chef status queue → itemized bill → partial/full payment → receipt → analytics — implemented and covered by the project's own test suite (`npm test`, `test:day18`, `test:final-qa`, `test:week-demo`, per README).
- Security posture as documented: server-authoritative pricing, parameterized SQL, role-separated JWT sessions (Admin vs Staff/Chef), sequential KOT state enforcement, overpayment protection, safe additive migrations.
- Explicit, deliberate scope boundaries already documented (no POS hardware, no payment gateway integration, no multi-branch) — this is a strength for a demo: it shows engineering discipline about what was intentionally left out, not gaps discovered by a client.

**Confirmed limitation (live, currently blocking a full end-to-end demo):**
- Backend API (`royal-table-api.onrender.com`) returned **HTTP 503 / "Service Suspended"** when checked live during this verification pass — this is Render's free-tier service suspension, not an ordinary cold-start sleep, and requires manual intervention in the Render dashboard before Admin, Chef, live booking, or billing flows can be demonstrated live. This must be resolved (resume the Render service, or migrate to a paid/always-on tier before a real client-facing use) before promising a live walkthrough beyond the static customer site.

**Recommended demo posture until the backend is resumed:** lead with the static customer site (live) and the existing screenshot/recorded-walkthrough assets referenced in `docs/screenshot-strategy.md`, and be explicit with prospects that Admin/Chef/live-booking is available on request with advance notice (time to resume the service) rather than instantly on-demand — an honest limitation, not a hidden one.

**Not yet independently re-verified this pass** (carried over from the project's own documentation, not re-tested by this phase): the exact current content of `docs/day18-case-study.md`, `docs/screenshot-strategy.md`, and `docs/premium-design-system-overhaul.md` — these exist and are readable, but their claims weren't line-by-line re-validated in this pass; treat them as the project's own record, not as re-verified by TTT HQ in Phase A.

---

*This document is part of the untracked Phase A working set. It has not been committed. See the Phase A final report for full verification evidence and the overall READY/NOT READY verdict.
