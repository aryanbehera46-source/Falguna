# Twenty Two Technologies (TTT) — Service Catalog & Client Intake (v1)

*Established as part of Revenue Execution Sprint V1, Milestone 2. Concise, working document — not a marketing brochure.*

## Service Catalog

| Service line | What we deliver | Typical engagement | Proof of work |
|---|---|---|---|
| Full-stack web app builds | Booking/CRM/e-commerce/SaaS platforms: React/Node/Express or Django/Laravel stacks, SQL/Postgres, payments, admin dashboards | 2–8 week fixed-scope or milestone-based build | ServiceFlow (booking+CRM), Nivara Commerce (D2C storefront+order mgmt), Royal Table (restaurant reservation site) |
| Legacy modernization / rebuilds | Phased "small-bang" migration off legacy stacks (e.g. Drupal, jQuery) to a modern framework, tenant-by-tenant cutover, no big-bang risk | 4–12 week phased engagement, often with a maintenance retainer after go-live | Royal Table security/reliability hardening; ServiceFlow architecture |
| AI-assisted workflow tooling | LLM-backed SaaS features: structured extraction from unstructured input, review/approval workflows, agentic automation | 2–6 week build, scoped around one workflow at a time | BriefPilot AI (briefs/notes → structured reviewable workflows); Falguna itself (in-house AI workforce platform) |
| E-commerce (Shopify & custom) | Theme/app development, storefront customization, checkout/fulfillment integration | 2–6 week contract or ongoing part-time contract | Nivara Commerce |
| Ongoing maintenance & support | Bug fixes, small feature additions, monitoring, on-call for a shipped platform | Monthly retainer (hours-based) | Royal Table (post-launch support) |

**What we do not take on without a named specialist partner:** regulated/compliance-heavy domains (healthcare data, payments licensing), large-scale (50+ person) staffing asks, and anything requiring an on-site/local presence outside India.

## Client Intake Process (reusable)

1. **Source & log** — opportunity captured with source URL, date observed, and raw requirements (no paraphrasing that changes scope) into the Revenue Hunter pipeline.
2. **Qualify** — deterministic scoring against `DEFAULT_CAPABILITY_SKILLS` / exclusion signals (existing `QualificationEngine`); anything scoring as an exclusion (e.g. requires an on-site presence, a specialist license) is dropped with a reason, not silently ignored.
3. **Scope call / written scope exchange** — before any estimate is finalized, confirm: must-have features vs. nice-to-have, hard deadline (if any), existing codebase/assets client can share, decision-maker and approval process.
4. **Proposal** — tailored to the actual posting: problem restated in our own words (proof we read it), relevant proof-of-work project named, phased milestones, price range (or hourly if scope is open-ended), risks/assumptions stated explicitly, and an explicit "what we need from you to start" list.
5. **Contract & kickoff** — scope + milestones + payment schedule in writing before work starts; first milestone kept small (1–2 weeks) so both sides can confirm fit before committing further.
6. **Delivery & QA** — isolated branch/worktree per engineering task, verification command(s) agreed with the client where possible, independent QA pass before handover.
7. **Handover & retainer offer** — walkthrough + written handover doc + a maintenance-retainer option presented at delivery, not as an afterthought later.

This intake process is new for this sprint (no prior document existed under this name in the repo) — it formalizes what the existing Revenue Hunter / Sales pipeline code already does mechanically (steps 1–2) and adds the human steps around it (3–7) that aren't automatable and shouldn't be.
