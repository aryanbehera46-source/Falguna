# TTT Opportunity Agent — Relevance Hardening Report

Nothing in this pass was committed to git, per instruction. All code changes are staged, uncommitted, on the Mac's `claude-ui-chat-v1` working tree, exactly as the prior phase's changes were left.

## 1. Root cause of "Freelance Writer" scoring 100/PURSUE

There were **two separate, additive root causes** — the second was only found by actually requalifying your real live data, not by unit tests alone.

**Cause 1 — coverage-percentage fit score with no absolute floor.** `QualificationEngine._fit_score` computed `matched skill tokens / total skill tokens`. Your "Freelance Writer" listing from Remotive had no real `required_skills`, so the only detected skill token was the word "ai" (picked up from the description). One match out of one token = 100% coverage = fit_score 100 — identical treatment to a listing with ten genuine concrete tech matches. "AI," "SaaS," "automation," etc. are topic words that show up in non-dev job posts constantly; a single match against one of them was never a safe signal.

**Cause 2 — found only during live requalification of your real 37 opportunities.** After fixing Cause 1 and re-running against your actual data, "Freelance Writer" *still* came back PURSUE, at score 100. Its real, scraped `required_skills` field was the single word **"REST"** (an artifact of the scraper, not an actual skill claim — the listing is genuinely about writing blog articles). The relevance gate's dev-skill override check used a bidirectional substring match (`"rest" in "rest api"`), so the bare fragment "REST" was wrongly treated as concrete evidence of REST API development, overriding the exclusion. This is the exact same class of bug as Cause 1 — a short, ambiguous token satisfying a match it shouldn't — just in a different place in the code (`_fit_score`'s capability match, and `_service_relevance`'s dev-override match both had it). Both were fixed with the same principle: a token may **equal** a capability/override phrase, or **contain** it as a longer descriptive string, but never be treated as a match merely because it's a substring *inside* a longer phrase.

## 2. Scoring changes (`falguna/revenue_hunter.py`)

- `_fit_score`: matches now require `skill_token == capability` or `capability in skill_token` — the reverse direction (`skill_token in capability`) is removed. A single generic/topic-word match (ai, saas, crm, automation, e-commerce, payments, booking system) is capped at 45, well under the PURSUE threshold, regardless of coverage percentage.
- `_service_relevance` (new): an independent hard gate, not a score. It checks title + description for exclusion-role phrases (writer, recruiter, HR, sales, support, accountant, legal/medical roles) against positive service-delivery phrases (full-stack, "build a/an", SaaS, dashboard, API integration, concrete languages/frameworks) and against skill tokens using the same equal-or-contains rule as above.
- `_recommendation`: now takes `relevance_passes_gate`. If the gate fails, the result is **always** IGNORE, checked before the score thresholds — a future scoring tweak can never let a gated opportunity back through.
- `_why`: explains gate-caused IGNORE distinctly ("not a software/AI development opportunity") from a plain low-score IGNORE.

## 3. Relevance-gate changes

Context-sensitivity was the explicit requirement, not a blacklist: an exclusion phrase only fails the gate when nothing overrides it. Verified against your own worked examples:
- "Build an AI writing SaaS for copywriters" → passes (has "build a[n]"/"saas")
- "Copywriter for AI company" → fails (only the bare topic word "ai" alongside "copywriter")

I also stress-tested this myself with a "Technical Writer for Developer Docs" case listing "html, css, git" as skills — markup/styling skills alone must not override a writer-role exclusion (they're common in non-dev content work too). That's why the override check uses a **stricter** token set (`_STRONG_DEV_OVERRIDE_TOKENS`: real languages/frameworks only — javascript, typescript, react, node.js, python, postgresql, rest api, etc.) than the general capability list, and excludes html/css/stripe/payments from being able to override on their own.

## 4. Acquisition-profile changes (`falguna/opportunity_agent.py`, `falguna/hq_web.py`)

`positive_service_signals` and `exclusion_role_signals` are now persisted, editable fields on the acquisition profile — not hardcoded. A new `build_qualification_engine(profile)` helper is used at every real qualify/discover call site (the qualify route, discovery, and requalification) so the engine always reflects whatever is currently saved. I added two new textareas to Acquisition Settings for these lists, wired through the existing save/load flow, plus a "Requalify all" button (see §6) and a relevance line on each opportunity's detail card showing pass/fail and which signals fired.

## 5. Live before/after distribution (your real 37 opportunities)

I requalified your actual `.falguna/state.db` on your Mac — not a disposable copy. **Before** (the state you actually saw): PURSUE 1, MAYBE 33, IGNORE 3. **After**: PURSUE 0, MAYBE 26, IGNORE 11.

"Freelance Writer" is confirmed no longer PURSUE — it now scores 0/100 and is IGNORE, correctly excluded on both "content writer" and "freelance writer" signals with no dev-work override.

## 6. Top PURSUE opportunities — honest result: there are none

I want to be direct about this rather than paper over it: **after the fix, zero of your 37 real opportunities qualify as PURSUE.** This is not a new bug — it's `_recommendation`'s pre-existing rule that PURSUE requires budget_quality MEDIUM or HIGH, and none of your 37 listings (12 Remotive + 25 WeWorkRemotely) have a stated numeric rate; job-board postings like these usually don't. So even the most clearly relevant leads cap out at MAYBE.

The top 8 by fit score, all of which pass the relevance gate (genuinely relevant, budget just isn't stated), for you to review manually and move to Pursue if the compensation checks out:

| Score | Source | Title |
|---|---|---|
| 100 | weworkremotely | Junior DevOps Engineer (CI/CD & Developer Tooling) |
| 100 | weworkremotely | Principal Software Engineer — Product team |
| 100 | weworkremotely | [Job-26953] Senior Full Stack Developer (React/.Net) |
| 100 | weworkremotely | Full-Stack Developer (Python, React, AI) |
| 100 | weworkremotely | Full-Stack Developer (Python, React, AI) *(duplicate listing)* |
| 100 | weworkremotely | Senior Shopify Full-stack Developer (IR-471) |
| 100 | weworkremotely | Data Engineer (Interior Design) |
| 100 | weworkremotely | Senior Product Manager, Cluster Linking |

Note "Senior Product Manager" and "Data Engineer" scoring 100 despite not being hands-on dev work is itself a residual calibration looseness — see Limitations.

## 7. Filtered / skipped accounting

Discovery-run accounting now tracks `filtered` (failed acquisition-profile match) and `invalid` (blank/malformed title) alongside found/new/duplicate, so `found = new + duplicate + filtered + invalid` always reconciles — this fixes the "13 found / 12 new / 0 duplicates" unexplained-gap you saw. Confirmed against your real data: 12 Remotive + 25 WeWorkRemotely = 37 opportunities currently in the database, matching your "37 imported" report. This accounting only applies to *future* discovery runs — the original run that produced these 37 predates this instrumentation, so I can't retroactively say whether that run's own gap was a duplicate, a filter, or an invalid listing.

## 8. Needs Aryan cleanup status

The one opportunity that flipped PURSUE→IGNORE ("Freelance Writer") had a real draft proposal and a pending Needs Aryan approval, both from before this fix. Handled per your instructions: the proposal is now `SUPERSEDED` (not deleted — new status value, first use), and its Needs Aryan item is `REJECTED` with a note explaining why. Zero PENDING Needs Aryan items reference it now, and there are zero duplicate Needs Aryan items anywhere in the database. Full qualification history is preserved — 74 rows (37 original + 37 requalified), nothing deleted, all 37 opportunities intact.

## 9. Tests

55 tests in `test_revenue_hunter.py` (up from 38), including all of your specified adversarial cases (5 should-IGNORE, 5 should-PURSUE/MAYBE, both context-sensitive examples) plus the two regression tests for the real REST-substring bug found in live data. 64 tests in `test_opportunity_agent.py` covering discovery-time gating, discovery accounting, and `requalify_all`'s downgrade/upgrade/idempotency/never-touch-Won-or-Lost behavior.

## 10. Full Mac test result

Ran on your actual Mac, against the real repo:
```
tests.test_revenue_hunter + tests.test_ttt_hq + tests.test_hq_web + tests.test_opportunity_agent
Ran 150 tests — OK (zero failures)
```
Full-repo `unittest discover`: 281 tests, 15 failures/errors, **all 15 confined to `test_bootstrap.py`** (an unrelated, pre-existing, environment-sensitive suite — confirmed identical on both the cloud sandbox and your Mac, unrelated to Revenue Hunter/Opportunity Agent).

## 11. Limitations

- **Zero PURSUE in your current batch**, as explained in §6 — driven by missing budget data on job-board listings, not a relevance problem. If you'd rather PURSUE not require a stated budget, that threshold in `_recommendation` is a one-line change, but I didn't make it unprompted since it changes risk tolerance, not relevance.
- **Score dilution on "shotgun" aggregator listings.** Several Lemon.io-style WeWorkRemotely postings ("Senior React Full-stack Developer," "Senior Golang Developer," "Senior DevOps Engineer," "Senior QA Engineer," "Senior Data Engineer") list 40+ generic skill tags (blockchain, Unity, WordPress, Symfony, etc. all in one listing) and scored only 19–24, landing in IGNORE via the score threshold — not the relevance gate, which correctly passed all of them as legitimate dev roles. Coverage-percentage scoring inherently penalizes listings with huge generic tag lists. This is a pre-existing behavior, not something introduced by this pass, and it's the opposite failure mode from what you reported (under- rather than over-scoring), so I flagged it rather than fixing it unprompted — happy to tune this if you want these surfaced as MAYBE instead.
- **"Senior Product Manager" and "Data Engineer" scoring 100** shows the relevance gate and fit score aren't fully unified — PM work isn't hands-on delivery, but the listing likely mentions enough dev-adjacent language to pass. Worth a follow-up pass if PM/leadership-only roles need their own exclusion signal.
- **Infrastructure note (not a code bug):** requalifying your real database hit a SQLite write-locking issue specific to how your Mac's connected-folder mount handles file locking — a leftover journal file from an earlier interrupted attempt caused several confusing failures, including one write that silently didn't take effect. I recovered safely at every step (verified via `PRAGMA integrity_check` and MD5 hashes before ever touching the live file, and kept two full backups: `.falguna/state.db.superseded-baseline-pre-hardening` and the same file under `.falguna/backups/`). Your real data was never at risk of being lost, but it took several extra verification passes to get right, and it's worth knowing this mount can be finicky for direct database writes in the future.
- UI additions (Settings textareas, Requalify-all button, relevance display on the detail card, filtered/invalid counts on discovery-run summaries) are implemented and pushed but not clicked-through in a live browser session — only verified by code review and the automated test suite.

Nothing was committed. The two backups above remain in place if you want to compare or roll back.
