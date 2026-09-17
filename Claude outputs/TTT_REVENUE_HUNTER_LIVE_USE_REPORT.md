# TTT Revenue Hunter -- Live-Use Readiness Report

## 0. Important: what "inspect the existing app" actually found

Before writing any code I spent the first part of this pass searching for the existing Revenue Hunter app, per your instruction that this is "not a greenfield rebuild." I looked in:

- The entire `~/Freelancing` folder (all 10 projects: ServiceFlow, BriefPilot AI, Nivara Commerce, your portfolio site, freelance-operating-system, and the restaurant-website/royal-table projects). None of these is Revenue Hunter -- freelance-operating-system turned out to be markdown SOPs and Excel trackers, not an application.
- The connected Falguna repo, including all 50+ git branches and its own `RELEASE_NOTES_V1.md`.
- `~/Desktop` and `~/Documents` (top level).
- Your home folder's top-level directory names.

I found no built Revenue Hunter application anywhere. Falguna's own release notes from a prior pass say so explicitly:

> "Opportunities / Sales Pipeline / Clients / Active Jobs / Revenue / Ventures are listed in TTT HQ's nav as disabled 'Coming soon' items -- intentionally not built. No data model, no backend, no fake UI behind them."
> "Revenue Hunter is not merged. `falguna/handoff.py` (the acceptor) exists and is tested, but nothing calls it yet."

So "Revenue Hunter" existed only as a label in TTT HQ (`falguna/ttt_hq.py`'s `hq_overview()`) plus a one-way handoff *receiver* stub (`falguna/handoff.py`) -- no opportunity tracker, no scoring, no proposals, no pipeline, no UI with real data.

I flagged this to you mid-session and you told me to use my own judgment. Given TTT HQ is already the correct, separate-from-Falguna home for this (its own local server, its own nav, "Coming soon" placeholders already reserved for exactly this), I built Revenue Hunter as a real feature of TTT HQ rather than starting a new codebase. Architecturally this **is** "inspect first, extend what's there" -- I extended TTT HQ's existing structure (`ttt_hq.py`, `hq_web.py`, the shared `StateStore`, the existing `NeedsAryanQueue`) rather than replacing anything. But I want to be honest that the *business logic* itself (scoring, proposals, pipeline, follow-ups) is new, not a repair of something broken.

## 1. What was already working

- TTT HQ as a separate local app from Falguna Engineering (own server, port 8766, own HTML) -- Boardroom, Master Vision Backlog, and a unified Needs Aryan queue that also surfaces Falguna Engineering missions awaiting a merge decision.
- `falguna/handoff.py`: a real, tested acceptor that turns a valid job payload into a real Falguna Engineering mission via the existing `ControlPlane.create_mission`. This was fully functional but had nothing to call it.
- The shared `StateStore` (SQLite) pattern every other Falguna module uses, with the same durability guarantees (survives process restarts, every table has `created_at`/`updated_at`).

## 2. What I built (all new, all in TTT HQ, none of it in Falguna Engineering)

**`falguna/revenue_hunter.py`** (new, ~700 lines) -- the entire Revenue Hunter backend:

- **Opportunity intake & structuring** (`OpportunityStore`, `extract_fields_from_text`, `extract_from_csv_rows`): manual form, pasted job description (regex/keyword extraction of budget, skills, deadline, contract type, location, urgency -- never guesses a title), pasted URL (stored as a source reference only -- **Revenue Hunter never fetches a URL's content**, by design, per your "do not build scraping that violates platform rules" instruction read as literally as possible), and CSV/JSON import (header-alias normalization).
- **Qualification** (`QualificationEngine`): deterministic, documented scoring -- fit score (skill overlap against a capability profile, word-boundary matched so short keywords like "ai" can't false-positive inside words like "available"), budget quality, effort-vs-return, portfolio match (against your three real, verified self-built projects -- ServiceFlow, Nivara Commerce, BriefPilot AI -- used as actual proof, not placeholders), recurring potential, urgency, risk/red-flag detection (unpaid/exposure-only/no-budget/vague-description), and a final PURSUE/MAYBE/IGNORE recommendation with suggested price, timeline, and which portfolio piece to cite.
- **Proposals** (`ProposalStore`, `generate_proposal_text`): short, detailed, Upwork-style, email pitch, and follow-up message -- all built from the opportunity's own fields (title, description, skills, qualification results), never generic boilerplate. Every proposal is created as **DRAFT** and only becomes **APPROVED** through the existing Needs Aryan queue -- there is no code path that sends anything.
- **Follow-up engine** (`FollowupStore`): proposal / response / negotiation / payment / repeat-business follow-ups, drafted only, `mark_sent()` is the only way a followup's status changes, and it's an explicit owner action after they've actually sent it.
- **Pipeline** (`OpportunityStore.move_stage`): the exact 8+2 stages you specified (New -> Qualified -> Proposal Ready -> Applied/Sent -> Replied -> Meeting -> Negotiating -> Won/Lost), full stage history, terminal-stage protection (can't silently move out of Won/Lost).
- **Won -> Active Job -> Falguna handoff** (`ActiveJobStore`): marking an opportunity Won creates a structured job payload (title, requirement built from the approved proposal or description, client, price). The handoff to a real Falguna Engineering mission is **owner-triggered only** -- you supply the real target repository path, and it calls the existing, unmodified `accept_revenue_hunter_handoff`. If the repository isn't a real git checkout, it raises and creates nothing. This is the same contract `falguna/handoff.py` already promised; I didn't touch that file.
- **Dashboard** (`DashboardService.today()`): pipeline value, won revenue, opportunities needing qualification, proposals needing approval, follow-ups due, and a merged "next actions" list.
- **Analytics** (`AnalyticsService.summary()`): opportunities added/qualified, proposals created/sent, replies, meetings, wins, losses, conversion rate, pipeline value, won revenue, and per-source performance.
- **Needs Aryan integration**: I added exactly two new kinds to the *existing* `NEEDS_ARYAN_KINDS` set in `ttt_hq.py` (`outreach_approval`, `negotiation_response_approval`) rather than building a second approval system. Deciding a Revenue-Hunter-origin item (e.g. approving a proposal) applies its real side effect -- flipping the actual proposal row to APPROVED -- via a small `apply_decision_side_effect()` hook called from `hq_web.py` right after the existing `NeedsAryanQueue.decide()`. The queue itself is untouched and still fully generic.

**`falguna/hq_web.py`** (extended, not replaced): new GET/POST routes for opportunities, qualification, proposals, follow-ups, stage moves, Won/Lost, Active Jobs, handoff, dashboard, and analytics -- all following the exact same handler pattern (`_json`, `_body`, error handling) the file already used for Boardroom/Backlog/Needs Aryan. The HQ's nav now has a real "Revenue Hunter" section (Today, Opportunities, Sales Pipeline, Clients, Active Jobs, Revenue) replacing what used to be disabled "Coming soon" buttons -- "Ventures / Company Ops" is the only one still marked coming soon, since it's out of this pass's scope.

**`falguna/schema_sqlite.sql` / `falguna/store.py`**: six new tables (`rh_opportunities`, `rh_stage_history`, `rh_qualifications`, `rh_proposals`, `rh_followups`, `rh_active_jobs`), each with the required `created_at`/`updated_at` columns the shared `StateStore` needs, added to the allow-list.

## 3. Design choices worth flagging

- **Scoring and drafting are deterministic templates, not model-generated.** A tool you'll use every day for real client acquisition needs to behave the same way every time, work offline, and be fully unit-testable without mocking a live model. Nothing here depends on network access or the Codex transport Chat/Search use. This was a judgment call -- if you'd rather have model-assisted drafting later, it can be layered in behind the same optional-transport pattern `research.py` already uses, without touching the deterministic core.
- **URL import never fetches the page.** You wrote "do not build scraping that violates platform rules" -- the safest reading of that is not to scrape at all. Pasting a URL records it as a source reference; you still paste the actual text/fields alongside it.
- **Nothing sends anything.** Proposals need an explicit Needs Aryan approval; follow-ups need an explicit "mark sent" click after you've sent it yourself. There is no outbound network call anywhere in this module.

## 4. Live-use readiness -- verified, not just read

I ran the actual pipeline through a live server on your Mac (not just unit tests): started TTT HQ for real (`python3 -m falguna hq`), created an opportunity from a pasted job description over HTTP, qualified it (correctly recommended PURSUE, $2500, 3 weeks, cited ServiceFlow as proof), generated a proposal, confirmed the Needs Aryan queue picked it up with the right reference, and confirmed the dashboard reflected the new pipeline value. Full lifecycle tests (through real HTTP requests, not mocks) also cover: qualify -> propose -> approve-via-Needs-Aryan (proving the real proposal row flips to APPROVED) -> move through every pipeline stage -> mark Won -> create Active Job -> trigger a real Falguna handoff -> confirm a real mission row exists in `missions`.

**Proposal workflow status**: fully working, owner-approval mandatory, verified end-to-end.
**Pipeline status**: all 8 stages + Won/Lost working, with history, verified end-to-end.
**Follow-up status**: drafts generate correctly, `mark_sent` verified, double-send blocked.
**Active Job / Falguna handoff status**: real, verified against a real git repository producing a real mission row; a fake repository path is rejected with zero side effects.

## 5. Tests

**29 new tests** in `tests/test_revenue_hunter.py`: field extraction (including a test that extraction never fabricates a title), qualification scoring (strong fit -> PURSUE, unpaid work -> always IGNORE, poor skill fit -> IGNORE, a regression test for a short-keyword false-positive bug I found and fixed during this pass, missing-budget risk flagging, vague-description flagging, retainer -> high recurring potential), proposal generation (tailored per kind, rejects unknown kinds), the full opportunity lifecycle (create -> qualify -> propose -> approve -> pipeline moves -> Won -> Active Job -> real handoff), terminal-stage protection, double-handoff protection, fake-repository rejection, follow-up draft/mark-sent/no-double-send, a simulated-process-restart persistence test, dashboard/analytics correctness, and an HTTP-layer suite covering the full lifecycle through real requests, URL import never fetching, non-http URL rejection, CSV/JSON multi-import, and a prompt-injection-shaped pasted JD proving untrusted pasted text is only ever stored as data, never acted on.

**Full suite on your Mac**: 191 tests total (162 existing + 29 new). All 29 new tests pass. The pre-existing 15 failures (all in `tests/test_bootstrap.py`, unrelated to this work -- Definition-of-Done/repair-loop timing issues in Falguna Engineering itself) are unchanged before and after this change -- **zero regressions**.

## 6. Remaining blockers / honest limitations

- **Qualification and proposal quality are only as good as the capability/portfolio profile I hardcoded** (`DEFAULT_CAPABILITY_SKILLS`, `DEFAULT_PORTFOLIO_PROJECTS` in `revenue_hunter.py`) -- based on the three real projects I found in `~/Freelancing`. Update these lists as your actual skill set and portfolio evolve; they're small, plain Python lists at the top of the file.
- **No email/Upwork API integration** -- proposals and follow-ups are drafted text you copy and send yourself. This matches "no sending automatically" but means there's a manual copy/paste step in the daily loop.
- **"Clients" and "Revenue" views are lightweight roll-ups computed from opportunities**, not a separate CRM system -- deliberately, to avoid over-building per your DO NOT ADD list.
- **The Won -> Active Job -> Falguna handoff requires you to type in a real local repository path** at handoff time. This is intentional (can't fake or guess a target repo) but means it's a manual step, not automatic.

## 7. Exact launch instructions

```bash
cd <your falguna-bootstrap repo>
python3 -m falguna --root . hq --port 8766
# open http://127.0.0.1:8766 -- Revenue Hunter is under the "Revenue Hunter" nav section
```

Falguna Engineering itself is unaffected and launches exactly as before (`python3 -m falguna --root . web`).

## 8. Delivery

A reviewable patch (`falguna-revenue-hunter-v1.patch`, diffed against commit `3f54ec9`) is attached, plus this report. Nothing was committed or merged -- `git status` in the repo shows the same files as uncommitted working-tree changes.
