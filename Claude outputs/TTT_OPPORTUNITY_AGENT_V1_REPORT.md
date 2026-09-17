# TTT Opportunity Agent v1 — Report

Repo: `falguna-bootstrap` on your Mac, branch `claude-ui-chat-v1`. Built inside TTT HQ → Revenue Hunter, exactly as scoped. Nothing was committed — all changes sit as uncommitted working-tree edits, same as every prior pass in this engagement.

## 1. Architecture

A new module, `falguna/opportunity_agent.py`, adds the acquisition layer on top of the existing (already-QA'd) Revenue Hunter data model. It owns discovery, dedup, and the AUTO-FIND → AUTO-ANALYZE → AUTO-DRAFT pipeline; Revenue Hunter's existing `OpportunityStore`, `QualificationStore`, and `ProposalStore` still own the opportunity/qualification/proposal records themselves, so nothing was duplicated or moved out of TTT HQ. Falguna Engineering was not touched.

The provider abstraction is a `Protocol` (`OpportunitySource`) shaped like `research.py`'s existing `SearchProvider`: `name`, `available`, `unavailable_reason`, `discover(profile, limit) -> List[NormalizedOpportunity]`. Every source — real or future — returns the same `NormalizedOpportunity` dataclass (source, external_id, url, title, description, client_name, budget_text, currency, location, remote, posted_at, skills, raw_metadata), so nothing downstream ever branches on which site a listing came from. A test in the new suite (`test_a_third_party_style_fake_provider_plugs_in_without_any_special_casing`) plugs in a provider that isn't one of the shipped classes at all, to prove the abstraction actually holds.

`DiscoveryEngine.run_now()` is the orchestrator: load the persisted acquisition profile → for each source, skip if unavailable or disabled, else call `discover()` inside its own try/except → filter against the profile → dedup → create the opportunity → auto-qualify → if PURSUE, research + auto-draft a proposal + one Needs Aryan item. It's a plain method, callable from a script or a future scheduler with zero changes — no daemon was built for v1, per your instruction.

## 2. Files changed

- `falguna/opportunity_agent.py` — new, ~450 lines. Providers, dedup, acquisition profile, research enrichment, discovery engine.
- `falguna/store.py` — added a real additive-column migration mechanism (`_apply_additive_column_migrations`, driven by `PRAGMA table_info`), because `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` is not valid SQLite and a naive schema-file edit would never reach your real, already-migrated database. Used to add 3 new qualification columns without touching your existing data.
- `falguna/schema_sqlite.sql` — 4 new tables (`rh_discovery_runs`, `rh_discovered_sources`, `rh_opportunity_research`, `rh_settings`) and their indexes.
- `falguna/revenue_hunter.py` — `QualificationEngine`/`QualificationStore` extended with `capability_gaps`, `estimated_project_value`, `why` (purely additive, no existing test broke); `OpportunityStore.list()` now attaches qualification to every row (needed for the inbox); `DashboardService.today()` gained `aging_opportunities`, `last_discovery_run`, `opportunities_discovered_today`.
- `falguna/hq_web.py` — new routes (`/api/rh/discover`, `/api/rh/acquisition-profile` GET+POST, `/api/rh/discovery-runs[/<id>]`), opportunity list/detail routes extended with recommendation/source filters and attached research; the Opportunities and Today views rebuilt in the UI (see §7).
- `tests/test_opportunity_agent.py` — new, 52 tests.

## 3. Discovery sources — what's actually verified vs. not

**Two free, keyless sources are wired and code-verified, but their live reachability is unconfirmed** — I want to be exact about this rather than overstate it, per your instruction not to claim a source works without a verified real result:

- **Remotive** (`https://remotive.com/api/remote-jobs`, public JSON API) and **WeWorkRemotely** (`https://weworkremotely.com/categories/remote-programming-jobs.rss`, public RSS) — both free, keyless, explicitly published by their owners for programmatic use. Parsing is unit-tested against realistic fixtures matching each site's real response shape (JSON job objects / RSS `<item>`s), and both pass.
- **I could not get a real result from either, from any environment available to me.** I tried from this cloud session and from your Mac (via the device-bridge shell this session uses) — both times the request failed with `403 Forbidden` from an egress proxy, and on the Mac the error was explicit: `X-Proxy-Error: blocked-by-allowlist`. I confirmed this is a curated allowlist, not a general outage — `github.com`, `pypi.org`, and `npmjs.org` all worked fine from the same shell; `remotive.com` and `weworkremotely.com` specifically are not on it. This is an org/Cowork egress policy on the bridged shell, not a defect in the discovery code, and it doesn't necessarily apply when TTT HQ runs directly in your own Mac Terminal (see §11, Next Step) — I just have no way to test that path from here.
- The full discovery pipeline (run orchestration, per-provider error capture, dedup, qualification, proposal draft, Needs Aryan, restart persistence) **was** verified for real against your live Mac's actual server process (§9) — the only unverified link is the two providers' outbound network call itself.

**Three sources are intentionally unsupported, and say so honestly in every run:**
- **Upwork** — requires a paid/OAuth API application. Not connected.
- **Freelancer.com** — requires OAuth API credentials. Not connected.
- **LinkedIn** — its terms prohibit automated scraping; job data needs an authenticated session. Not supported.

All three are listed in the provider registry (so wiring real credentials later is a one-line change) but `discover()` is never called on them — verified by a test that asserts zero calls — and every run reports their exact reason.

## 4. Deduplication

Three signals, checked in order, before an opportunity is created (not cleaned up after): `(source, external_id)`, then a canonicalized URL (strips `www.`, scheme case, trailing slash), then a SHA-256 fingerprint of normalized title+client+description. All three paths are covered by dedicated tests, including one that reopens the database (simulating a restart) and confirms a second discovery run against the same fixture still reports it as a duplicate, not a new row.

## 5. Qualification behavior

Every discovered opportunity is qualified immediately and automatically — this reuses the existing, already-QA'd deterministic `QualificationEngine` (no network, no model call), extended with three fields your spec asked for: `capability_gaps` (skills required but not in our capability list), `estimated_project_value`, and a plain-English `why` explaining the recommendation. Nothing discovered is silently discarded: IGNORE and MAYBE opportunities are qualified, stored, and stay visible in filtered views — they're just excluded from proposal drafting and Needs Aryan.

## 6. Proposal & Needs Aryan behavior

A proposal is auto-drafted only for opportunities qualified **PURSUE** — one per opportunity, status `DRAFT`, using the existing deterministic template engine (unchanged fallback path if a model isn't available). Drafting a proposal always creates exactly one Needs Aryan approval item; MAYBE and IGNORE never create a proposal or a Needs Aryan item at all. A test with a mixed batch of 4 opportunities (2 PURSUE, 1 MAYBE, 1 IGNORE) confirms exactly 2 Needs Aryan items are created, not 4 — the flooding requirement holds. Nothing in `opportunity_agent.py` ever calls a "send" or "mark approved" path; I grepped for it to confirm.

One behavior worth flagging explicitly: proposal auto-drafting only fires when a `NeedsAryanQueue` is passed into the engine. In production this is always true (`hq_web.py`'s `/api/rh/discover` route always wires a real queue), but a bare/manual call to `DiscoveryEngine.run_now()` without one will qualify opportunities but skip drafting. This is intentional — drafting without anywhere to surface the approval seemed worse than not drafting — but flag it if you'd rather it always draft regardless.

## 7. UI changes

**Opportunities** is now an acquisition inbox: a "Find Opportunities Now" button (with live per-provider status), 6 quick filters (All / New discoveries / Pursue / Maybe / Ignored / Already reviewed), and filter controls for stage, source, minimum score, and max age. Each card shows source, age, fit score, recommendation, suggested price, portfolio match, and a computed next action. Opening a card now also shows discovery provenance (source link), the 3 new qualification fields, and any research findings with their citations.

**Today** gained two tiles (opportunities found today, aging opportunities 14+ days) and a "Last discovery run" summary showing exactly what each provider reported.

**Acquisition Settings** is a new view: services, skills, minimum budget, max listing age, remote preference, target countries, required/excluded keywords, and a per-source enable/disable toggle — all persisted, not hardcoded, and take effect on the next discovery run.

## 8. Safety behavior

Confirmed by code inspection and by test: no auto-send, no auto-apply, no login/credential/CAPTCHA bypass anywhere in the new code. Every provider failure is isolated (one provider's exception never kills the run) and recorded with `{provider, started_at, completed_at, found, new, duplicates, error}` for every provider on every run, including the ones that were skipped or unavailable. Proposals are always `DRAFT`; nothing transitions a proposal to sent.

## 9. Live test evidence (real Mac, real HTTP server, real SQLite)

Beyond the 52 new unit/integration tests (below), I ran a genuine live QA pass against your actual Mac, through the real `falguna hq` HTTP server (not mocks), using a disposable scratch copy so your real production database was never touched (verified before and after — your real `.falguna/audit.jsonl` timestamp is unchanged):

1. Started the real server, called `POST /api/rh/discover` for real — confirmed the honest per-provider failure/unavailable reporting shown in §3 came back correctly through the actual HTTP layer, not just in a test.
2. Created a realistic opportunity through the real API, restarted the server, then: qualified it (real HTTP call → `PURSUE`, fit 100/100, budget HIGH, matched the ServiceFlow portfolio project), drafted a proposal (real HTTP call → `DRAFT`, personalized content referencing the actual opportunity and portfolio proof), confirmed exactly one Needs Aryan item was created (`status: PENDING`).
3. **Restarted the server a third time** and re-verified everything through fresh HTTP calls: the opportunity, its qualification, its proposal (still `DRAFT`), its 3-entry stage history, the Needs Aryan item, the acquisition profile, and the discovery run history all survived the restart intact. Dashboard aggregation (`pipeline_value: $3200`, 1 proposal needing approval) was correct after the restart too.
4. Confirmed there is no `codex` CLI on your Mac, so AI research enrichment currently always takes its documented graceful no-op path — this was already covered by tests, but it's worth knowing proposals and qualification work fully without it today.

This live pass also caught and fixed **2 real bugs** that only showed up against a live server (not the mocked unit tests): `rh_settings` was missing an `updated_at` value on first save (NOT NULL violation — every acquisition-profile save would have crashed on a fresh database), and the new `rh_discovery_runs` table was missing an `updated_at` column entirely, which would have crashed *every single discovery run* the first time it tried to complete. Both are fixed and re-verified.

## 10. Full Mac test result

Ran the complete suite on your real Mac (authoritative, not the cloud sandbox):

- `tests.test_revenue_hunter` + `tests.test_ttt_hq` + `tests.test_hq_web` + `tests.test_opportunity_agent`: **121/121 passing**, zero regressions.
- Full repo suite (`tests discover`): 254 tests, 13 failures + 2 errors + 1 skip — **all confined to `test_bootstrap.py`** (Falguna Engineering's mission runner, unrelated to Revenue Hunter or this work). I checked: the same failures occur identically in the cloud sandbox, and they trace to a `write not allowed: falguna/__pycache__` scope-check tripping over stray bytecode cache directories in this working copy — a pre-existing environment artifact, not something introduced by this pass. Nothing in Revenue Hunter, TTT HQ, or Opportunity Agent territory is affected.

## 11. Limitations & exact next step

- **Real internet reachability for Remotive/WeWorkRemotely is unverified** — both environments available to me (this cloud session and the Mac device-bridge shell) sit behind an egress allowlist that blocks those two specific hosts while allowing others. **Your exact next step:** run `python3 -m falguna --root <path to falguna-bootstrap> hq` directly in your own Mac Terminal (outside this session's bridge) and click "Find Opportunities Now" once. That path isn't behind this sandboxing proxy, so it should tell us for real whether those two feeds are reachable from your machine. If they are, you're live; if not, I'd want to see the actual error to diagnose it.
- No `codex` CLI is present on your Mac, so research enrichment is currently a documented no-op. Not a blocker — proposals and qualification are fully deterministic and working — but worth knowing before you rely on the "research the client" feature.
- Per your list, I deliberately did not: auto-send anything, build a CRM, touch Falguna Engineering, redesign TTT HQ beyond Opportunities/Today/Settings, or add Media Engine/Trading/unrelated features.
- Nothing was committed. `git status` on your Mac shows exactly: modified `falguna/{store,schema_sqlite,revenue_hunter,hq_web}` and new `falguna/opportunity_agent.py` + `tests/test_opportunity_agent.py`, ready for you to review and commit when you're satisfied.
