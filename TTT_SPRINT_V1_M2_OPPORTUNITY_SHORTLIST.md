# Milestone 2 — Real Opportunity Shortlist & Proposal Drafts (Sprint V1)

**Status: all proposal text below is UNSENT.** No outreach channel to these clients is currently authorized/connected in this session (no email/Upwork/LinkedIn credentials wired in) — these are ready-to-review drafts for Aryan to send manually or to approve for a connected channel later.

## Sourcing method (real, traceable)

Sourced via TTT HQ's existing `POST /api/rh/discover` production endpoint (`DiscoveryEngine.run_now`), which pulls from Remotive and WeWorkRemotely's public RSS/API feeds. Upwork, Freelancer, and LinkedIn are correctly reported as `available: false` by the existing `UnavailableSource` — no credentials configured, no scraping attempted. Discovery run observed: 2026-09-25, ~07:48 UTC, yielding 29 new deduplicated opportunities logged into `rh_opportunities`.

## Honest finding: sourcing/fit gap

Most listings from Remotive/WeWorkRemotely are **individual freelance-marketplace or full-time-employee postings** (Lemon.io, Toptal, A.Team match individuals to client work; direct-company FTE listings like Reddit's or KoboToolbox's require a named individual hire for 1+ year). These are not opportunities a company can credibly "bid on" as an agency — applying to them as Twenty Two Technologies would misrepresent what the listing is for. Of 29 new opportunities, **2** were found to be genuinely agency-pitchable (a named company, describing a concrete, scoped project, with language open to a contracted delivery team rather than strictly a solo FTE hire). Reporting 2, not the target 3, per the mission's own instruction to report the actual number rather than force a third.

**Recommended fix for next sourcing pass:** add a source better suited to agency-style contracts (Upwork Business/Agency listings, Clutch/GoodFirms RFPs, or direct outreach to companies posting "Lead Developer" style project roles) rather than relying solely on individual-hire job boards.

---

## Opportunity 1 — Senior Shopify Developer (contract-based), Sanctuary Computer / garden3d

- **Source URL:** https://remotive.com/remote-jobs/software-development/senior-shopify-developer-2091140
- **Observed:** 2026-09-25
- **Client:** Sanctuary Computer (garden3d), a worker-owned creative/dev collective (clients cited: Google, Stripe, Figma, Hinge, Etsy)
- **Scope (as published):** Contract-based Senior Shopify Developer for their Design & Development team; stack includes Shopify, Node.js, GraphQL, Next.js, TypeScript, Redux, Cypress/Jest testing, CMS/Prismic
- **Budget (as published):** $80k–$150k (listing's own stated range; likely an annualized FTE-equivalent figure, not necessarily what a contract engagement would price at)
- **Confidence:** Medium — explicitly "contract-based," but unclear from the posting alone whether they'd accept a studio/agency vs. requiring an individual under their direction
- **TTT capability match:** Direct — Nivara Commerce (D2C storefront, checkout, inventory, fulfillment) is a close proof-of-work match

### Draft proposal (UNSENT)

> Subject: Contract Shopify build support — Twenty Two Technologies
>
> Hi Sanctuary Computer team,
>
> I came across your contract-based Senior Shopify Developer listing (garden3d / Sanctuary Computer, posted on Remotive). We're Twenty Two Technologies, a small dev studio — I wanted to introduce us as an option for the contract engagement rather than a single hire, in case that flexibility is useful.
>
> Relevant background: we built Nivara Commerce, a D2C storefront and order-management platform (checkout, inventory, fulfillment, sales analytics) — the same problem space as the Shopify + Node/GraphQL + Next.js stack you listed.
>
> If it's useful, we can scope a first milestone (e.g., one specific theme/storefront feature or integration) at a fixed price so you can evaluate fit before any larger commitment — typically 1–2 weeks for a well-defined first slice. Happy to share more detail on Nivara Commerce or talk through your current Shopify setup if you'd like.
>
> — Aryan, Twenty Two Technologies

**Estimate (TTT-side, not client-confirmed):** first milestone 1–2 weeks / $800–$1,800 depending on scope; full contract scope would need a real scope call given the wide skill list published.

**Risks:** posting may only accept individual contractors, not studios; budget range published looks FTE-shaped, so a contract-rate conversation is needed before quoting further.

---

## Opportunity 2 — Lead Developer: Rebuild, Modernize & Scale (Social Good SaaS), Track it Forward

- **Source URL:** https://weworkremotely.com/remote-jobs/track-it-forward-lead-developer-rebuild-modernize-scale-social-good-saas-remote
- **Observed:** 2026-09-25
- **Client:** Track it Forward — Oakland, CA; bootstrapped, profitable, 15-year-old volunteer time-tracking SaaS for nonprofits/schools (30M+ hours tracked); team of 4
- **Scope (as published):** Greenfield rebuild of a legacy Drupal 6 app (hosted on Pantheon) + two Ionic/Capacitor mobile apps, migrating to a modern stack (Python/Django and/or Laravel mentioned), phased "small-bang" migration (parallel build, incremental tenant-by-tenant cutover — explicitly not a risky big-bang cutover)
- **Budget:** not published
- **Confidence:** Medium-High — direct company (not a marketplace intermediary), concrete described scope and migration strategy, explicitly wants "complete ownership of the technical architecture" handed to whoever takes this on, which is a natural fit for a small contracted team rather than only a solo FTE
- **TTT capability match:** Direct — this is exactly the "legacy modernization / rebuild" service line; ServiceFlow (booking+CRM platform) is comparable architectural complexity

### Draft proposal (UNSENT)

> Subject: An alternative to a solo hire for your Drupal 6 → modern-stack rebuild
>
> Hi Track it Forward team,
>
> Saw your Lead Developer listing for the Drupal 6 → modern-stack rebuild (WeWorkRemotely). The phased "small-bang" approach you described — parallel greenfield build, incremental tenant-by-tenant migration, no risky cutover — is a sound plan, and it's also naturally suited to being delivered by a small contracted team rather than a single new hire who has to ramp up on your 15-year-old codebase alone.
>
> We're Twenty Two Technologies. Comparable prior work: ServiceFlow, a full booking + CRM platform we built (availability/double-booking protection, billing, staff dashboards) — similar scope to what a nonprofit volunteer-tracking rebuild needs.
>
> If useful, we'd propose starting with a scoped discovery phase (1–2 weeks, fixed price): review the current Drupal 6 architecture and Ionic mobile apps, confirm the target stack, and produce a written migration plan with phase boundaries — before committing to the full rebuild. That gives you a low-risk way to evaluate working with us before any larger engagement.
>
> Happy to share more on ServiceFlow or talk through your current architecture.
>
> — Aryan, Twenty Two Technologies

**Estimate (TTT-side, not client-confirmed):** discovery phase 1–2 weeks / $1,000–$2,000 fixed; full rebuild would be milestone-based (likely 8–16 weeks total) and needs the discovery output before a real number is honest.

**Risks:** posting is titled "Lead Developer" (singular hire) — team may specifically want one person embedded long-term rather than a contracted studio; would need to be upfront about this being a team engagement, not an individual, in the first reply.

---

## Opportunities considered and excluded (for the report, not proposed)

| Title | Client | Why excluded |
|---|---|---|
| Automation Engineer (UIPath/AI) | Toptal (anonymized end client) | Individual-only marketplace (Toptal); end client not identifiable for direct outreach |
| Senior AI Engineer | Lemon.io | Individual freelance-marketplace matching, not an agency-contractable posting |
| DevOps Senior | Coderio | Coderio is itself a dev agency hiring an individual to join their own team, not a project to bid on |
| Frontend Web Application Developer | KoboToolbox | Explicitly "full-time... commitment of at least 1 year" — FTE only |
| Backend Software Engineer, PDP Experience | Reddit | Large-company internal FTE hire, not outsourceable at this scale |
