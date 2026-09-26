# Twenty Two Technologies — Live Enquiry Activation V1

Report date: 2026-09-26. Branch `claude-ui-chat-v1`, starting HEAD `ac3127a` (confirmed before any work; unchanged — nothing was committed). This sprint is a focused integration pass on the existing TTT Communications V2 codebase (Milestones 1–12), not a rebuild.

## Step 1 — Audit of the real live enquiry path (honest findings)

All four forms were confirmed live and correctly branded by navigating to them directly in a browser this session: General Enquiries (`https://tally.so/r/VLgxN6`), Start a Project (`https://tally.so/r/vGkWW4`), Careers (`https://tally.so/r/XxXNNz`), Media Enquiries (`https://tally.so/r/obWxxN`). These match exactly what `scripts/export_static_site.py`'s `TALLY_FORMS` constant already declares and what the exported static site links to — the site has no backend of its own; Tally is the only submission path today.

What could **not** be verified, and must not be claimed as verified: no Tally dashboard/API credentials or session were available in this environment (navigating to `tally.so/dashboard` redirected to Tally's own login page). So this sprint cannot confirm, and does not claim: that a submission on any of the four forms actually lands in Tally's dashboard; which mailbox (if any) currently receives Tally's notification email for each form; whether that mailbox is one of TTT's own addresses or a personal one; or whether any Tally-side webhook/Zapier/notification rule is already configured. No real test submission was made through any of the four live forms this session, since doing so would inject synthetic data into Aryan's live Tally account without his real-time confirmation of what he wants recorded there.

**Manual verification Aryan needs to do himself** (this is the one real gap this sprint could not close):
1. Log into `tally.so`, open each of the four forms, and check Settings → Integrations/Notifications for each: is an email notification configured, and to which address? Is a webhook configured?
2. Submit one real, clearly-labelled test entry per form (e.g. name = "TTT Internal Test — please ignore") and confirm it appears under that form's **Responses** tab.
3. Confirm the notification (if any) actually arrives at an inbox Aryan checks. If nothing is configured, submissions today are invisible until someone opens the Tally dashboard.
4. Confirm the Careers form's résumé upload actually stores a retrievable file on Tally's side (open one from Responses).

## Step 2 — Integration path chosen, and why

The public site is a free Render Static Site with no backend, and Falguna/TTT HQ run locally on Aryan's Mac — not always on, not publicly reachable. Standing up an authenticated, persistent, publicly-reachable webhook receiver this sprint would mean either paying for hosting or exposing a local machine to the internet, both explicitly out of scope without separate approval.

**Chosen path (exactly the mission's own stated preference):** Tally submission → Aryan verifies/reads it in Tally's own dashboard or notification → Aryan exports that one submission as JSON from Tally → `scripts/import_tally_submission.py` → real `CommsStore`/`EnquiryStore`/`ApplicationStore` state → Falguna AI workforce triage in TTT HQ's Communications view. This needs no new hosting and no new public attack surface, and it is real today, not aspirational.

A **developer-only local webhook receiver** (`scripts/tally_webhook_dev_only.py`) was also built and tested end-to-end as groundwork for later: HMAC-SHA256 signature verification (fails closed — no secret configured, or a wrong/missing signature, is a 401 with no ingestion), binds to localhost only and refuses any other bind address, and calls the same `TallyIntakeService.ingest()`. This is **not deployed and not a live integration** — it is not wired into Tally's dashboard, not exposed publicly, and must not be treated as production until Aryan explicitly approves real hosting for it.

## Step 3 & 4 — `falguna/tally_intake.py`: normalization + routing

New module `falguna/tally_intake.py`. Reuses, never rebuilds:
- `EnquiryStore.submit()` for GENERAL and PROJECT (same conversation + real Revenue Hunter opportunity a native site submission gets).
- `ApplicationStore.create()` for CAREERS (same careers conversation, restricted applicant visibility unchanged).
- A direct `CommsStore` path for MEDIA (no pre-existing store for that type — mirrors `EnquiryStore`'s own general-enquiry pattern rather than inventing new architecture).
- `comms_workforce.run_agent_for_conversation` for AI triage, exactly like `email_ingestion.py` already does.

**Field mapping — the one honest limitation to flag clearly:** no Tally dashboard/API access was available, so the real per-form field keys/IDs could not be read. Fields are matched by **label** (case-insensitive, punctuation-tolerant) against an explicit, versioned candidate list in `FIELD_LABEL_CANDIDATES`. This is the minimum-guess alternative to inventing field IDs, but it is **unverified against a real Tally payload**. If a required field can't be confidently matched, the submission is **rejected and recorded** (never guessed, never fabricated) — visible in TTT HQ under the new "Website intake errors" panel. **Before relying on this for real submissions, feed one real export per form through `scripts/import_tally_submission.py` and confirm/tighten `FIELD_LABEL_CANDIDATES` against the real labels Tally sends** — this is the one remaining manual step.

Idempotency/audit: a new `tally_intake_events` table (additive schema change, registered in `StateStore`'s allowed-tables set) records every submission — ingested, duplicate, or rejected — keyed by Tally's own `submissionId`/`responseId`. A webhook retry or a re-run of the manual-import script on the same export is a safe no-op: verified in tests and in a live smoke test that a replayed PROJECT submission never creates a second opportunity, and a replayed CAREERS submission never creates a second application.

Résumés: only filename/MIME-type/size metadata is recorded (mirrors `email_ingestion.py`'s existing convention for attachments of unknown provenance) — résumé bytes are **never fetched** from Tally's hosted URL in this pass. Downloading and storing them (mirroring `site_web.py`'s `safe_upload_path()`) is a deliberate follow-up once a real payload exists to test against.

## Step 5 — AI triage

Every successful ingest dispatches `run_agent_for_conversation` — the existing Receptionist/Sales Rep/Account Manager pipeline drafts an acknowledgement, classifies risk (LOW/MEDIUM/HIGH), and escalates HIGH risk to the existing `NeedsAryanQueue`. Nothing sends automatically: `NullEmailProvider` remains the default, and outbound messages stay `DRAFT`/`APPROVED`, never `SENT`, until Aryan acts — verified explicitly in tests.

## Step 6 — TTT HQ additive change

One small, additive change to the existing Communications view (no new dashboard): `CommsStore.overview()` now returns `website_intake_errors` (recent rejected Tally submissions), and a new "Website intake errors (Tally)" panel was added to the existing Communications page in `hq_web.py`, right next to the existing "Failed delivery" panel it mirrors. This is the one gap the mission's Step 6 checklist otherwise left uncovered by the existing overview — new leads, department breakdown, drafts, and pending approvals were already visible there.

## Step 7 — Tests

New `tests/test_tally_intake.py`: **24 tests, all passing** — covering all four form types' real field normalization, unknown-form and malformed-payload rejection (never fabricated), idempotent duplicate submissions (including a bad payload retried unchanged, which is rejected again, not silently deduped as if it succeeded), real opportunity creation for PROJECT, correct department routing for all four types, AI draft/risk-classification dispatch, no-external-send under `NullEmailProvider`, persistence after a fresh store connection (simulating a restart), and that rejected submissions surface in the Communications overview.

Regression suites run: `test_comms.py`, `test_comms_workforce.py`, `test_email_ingestion.py`, `test_site_web.py`, `test_hq_web.py`, `test_ttt_hq.py`, `test_revenue_hunter.py` — **248 tests, all passing**, confirming the additive schema/store/overview changes did not break anything already relied upon.

Both scripts were also smoke-tested live against a scratch state directory (never the real `.falguna/state.db`): the manual-import CLI ingested a sample export end-to-end (real conversation, real AI draft, real risk classification) and correctly reported "duplicate" on re-run; the dev-only webhook correctly rejected an unsigned request (401), rejected a forged signature (401), accepted and ingested a correctly-signed one (200), and safely deduped a replay.

## Step 8 — Live Activation Checklist (honest status)

| # | Item | Status |
|---|---|---|
| A | Live forms tested and Tally submissions verified | **Not done.** Forms confirmed live/branded by browsing them; no real submission was made and no Tally dashboard access was available to confirm receipt. Aryan must do the 4-form manual check above. |
| B | Notification recipient verified | **Not verified.** No Tally credentials available. Aryan must check Settings → Notifications on each form. |
| C | Manual import operational | **Done.** `scripts/import_tally_submission.py` works end-to-end against a real local `StateStore`/`CommsStore`/AI workforce — smoke-tested live this session. |
| D | Local automated adapter tested | **Done, with a caveat.** `falguna/tally_intake.py` is fully tested (24 new tests + 248 regression tests, all passing) against realistic synthetic payloads shaped like Tally's documented webhook format. Field-label matching is **not yet confirmed against a real Tally export** — see Step 3. |
| E | Public automatic ingestion deployed and receiving production events | **Not done, and not attempted.** No public backend exists for the static site; deploying one needs separate, explicit approval per the mission's own constraints. The dev-only webhook receiver is real and tested, but only on localhost — never deployed. |

### What Aryan can do right now to start handling real customers today, even before E is ever built
1. Check the four forms' notification settings in Tally (the one manual step above) so nothing gets missed while relying on manual import.
2. When a real enquiry/application arrives (by email notification or by checking the Tally dashboard), export that one response as JSON from Tally and run: `python3 scripts/import_tally_submission.py path/to/export.json`.
3. Open TTT HQ → Communications: the new conversation will be there with an AI-drafted acknowledgement, correct department, and (for Start-a-Project) a real pipeline opportunity already linked. Review the draft, edit if needed, and send it through whatever channel is actually live today (this sprint does not change that — `NullEmailProvider` is still the default, by design).
4. If a submission doesn't show up after import, check the new "Website intake errors" panel in Communications — it will name the reason (usually a field-label mismatch), which is the signal to tighten `FIELD_LABEL_CANDIDATES` in `falguna/tally_intake.py`.

## Files changed/added (nothing committed — see commands below)

**New:**
- `falguna/tally_intake.py`
- `scripts/import_tally_submission.py`
- `scripts/tally_webhook_dev_only.py`
- `tests/test_tally_intake.py`

**Modified (all additive, no existing behavior changed):**
- `falguna/schema_sqlite.sql` — new `tally_intake_events` table.
- `falguna/store.py` — registers the new table in the allowed-tables set.
- `falguna/comms.py` — `overview()` gains `website_intake_errors`.
- `falguna/hq_web.py` — one new panel in the existing Communications view.

## Suggested selective staging (only if/when Aryan wants to commit — nothing was committed this sprint)

```
git add falguna/tally_intake.py falguna/schema_sqlite.sql falguna/store.py falguna/comms.py falguna/hq_web.py
git add scripts/import_tally_submission.py scripts/tally_webhook_dev_only.py
git add tests/test_tally_intake.py
```

(Deliberately not `git add .` or `git add -A` — the repo has many pre-existing untracked files from earlier work, e.g. `Claude outputs/*.md`, launcher `.app` bundles, that this sprint did not touch and should not sweep into a commit.)

One housekeeping note unrelated to this sprint's changes: a stray `.git/index.lock` file was observed during `git status` (harmless for read-only git commands, which all worked fine) — if a future `git add`/`commit` fails with a lock error, it's safe to remove `.git/index.lock` as long as no other git process is actually running.
