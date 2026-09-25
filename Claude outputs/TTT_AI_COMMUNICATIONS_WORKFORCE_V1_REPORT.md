# TTT AI Communications Workforce v1

**Date:** 2026-09-25 · **Repo:** `falguna-bootstrap`, branch `claude-ui-chat-v1`, base commit `9f381a9` (unchanged)
**Status:** Built and verified. **Nothing committed, pushed, deployed, or sent externally.** The frozen static website export is untouched.

## What this is

Four AI roles now operate on the existing Unified Communications Center (`falguna/comms.py`), all wired through infrastructure that already existed rather than new architecture:

- **AI Receptionist** — classifies each open conversation, drafts a truthful acknowledgement, and asks for whatever structured info is actually missing (budget/timeline for a sales enquiry, a resume for a careers application) — never a guess.
- **AI Sales Representative** — qualifies the linked opportunity through the real `QualificationEngine`, drafts a proposal via the existing `ProposalStore` when it scores PURSUE, tracks follow-ups via `FollowupStore`, and escalates (via `NegotiationGuardrails`) instead of answering anything that reads as a pricing or contract commitment.
- **AI Customer Support** — handles routine enquiries with a small set of safe templated replies, treats each conversation as its own case (no new "ticket" table — `comm_conversations` already is one), surfaces the contact's own prior-conversation history as permitted context, and escalates sensitive content or anything with no safe template.
- **AI Account Manager** — drafts status updates from real Falguna Engineering delivery evidence (`AccountManagerService`, extended to also read the new Communications Center) and escalates real delivery blockers.

Every draft is created with status `DRAFT`. Nothing anywhere in the new code can create a `SENT` message, call an email provider's `send()`, or execute a commercial commitment — that's covered explicitly in `tests/test_comms_workforce.py`'s security tests.

## Approval controls (requirement 5)

All escalation goes through the one existing `NeedsAryanQueue` — no second approval system. A human sends a drafted message from TTT HQ's Communications view via a new "Approve & mark sent" action, which calls a new `CommsStore.mark_message_sent()` (mirrors `EmailStore.mark_sent`/`FollowupStore`'s existing pattern: only an explicit, owner-performed action moves a DRAFT to SENT).

## Email integration readiness (requirement 7)

Added `EmailProvider` / `NullEmailProvider` to `falguna/email_admin.py` — a small, provider-independent send boundary. The only provider wired in is `NullEmailProvider`, which is structurally incapable of sending (`is_configured()` is always `False`, `send()` always raises). `DEPARTMENT_MAILBOXES` lists planned `@twentytwotechnologies.com` addresses as addresses only, never asserted as live. Wiring a real provider later (SMTP/SES/Postmark/Gmail API) means writing one small adapter class — nothing else changes. No credentials of any kind are stored in code.

## TTT HQ Communications (requirement 6)

The Communications view is no longer read-only:
- **Run AI Workforce now** — runs all four roles across every open conversation in one pass.
- **Run AI agent** — runs the workforce for a single conversation.
- **View messages** — expands a conversation's real message history inline, with **Approve & mark sent** on any AI-drafted message.
- **Mark resolved** — closes out a conversation.
- New **AI Workforce** panel shows recent agent drafts, pending communications escalations, and the email provider's real (not-configured) status.

New endpoints (all in `falguna/hq_web.py`, following the codebase's existing `open_control_plane` / `NeedsAryanQueue` pattern): `GET /api/comms/workforce/activity`, `POST /api/comms/workforce/run`, `POST /api/comms/conversations/<id>/run-agent`, `POST /api/comms/conversations/<id>/messages`, `POST /api/comms/conversations/<id>/status`, `POST /api/comms/messages/<id>/mark-sent`. A conversation that was escalated is automatically handed back to `open` once its Needs Aryan item is decided, so nothing gets stuck.

## What was reused, not rebuilt

Per the "avoid unnecessary new architecture" instruction, the new code (`falguna/comms_workforce.py`, ~330 lines) is a thin orchestration layer. It reuses, unmodified: `CommsStore`, `NeedsAryanQueue`, `QualificationStore`, `ProposalStore`, `FollowupStore`, `NegotiationGuardrails`, `AccountManagerService`, `classify_intent`. Three small, additive extensions were made (no existing method changed): `CommsStore.mark_message_sent()`, `AccountManagerService.comms_signals()` (reads the new Comms tables the way `scope_signals()` already reads the old ones), and `EmailStore`'s provider boundary.

## End-to-end trial (requirement 8)

`tests/test_comms_workforce.py::EndToEndTrialTests` runs the real flow with local test data (`jordan@test-trial-client.invalid`): a project enquiry submitted through `EnquiryStore` → routed to the `sales` department → AI Receptionist acknowledges + AI Sales Rep drafts a proposal and escalates it → `NeedsAryanQueue.decide("approve")` → proposal marked `APPROVED` → `CommsStore.overview()` reflects it. The test then **closes and reopens the SQLite database file** and confirms the opportunity, conversation, and approved proposal all persisted correctly — real persistence and recovery, not an in-memory assumption.

## Test results (all run for real)

- **29/29 new tests pass** (`tests/test_comms_workforce.py`): all four roles, the dispatcher's idempotency (a second pass over an unchanged conversation takes no new action), the security/permission invariants (no agent path ever creates a `SENT` message, commitment language always escalates even with no budget on file, the null email provider can never send, a resolved conversation is never touched), and the end-to-end trial with persistence/recovery.
- **223/223 pass** on every existing test file touched or adjacent (`test_comms`, `test_account_management`, `test_revenue_hunter`, `test_sales_ops`, `test_hq_web`, `test_email_admin`) — no regression from the additive changes.
- **Full suite: 1477 passed, 16 failed, 3 skipped.** All 16 failures are pre-existing and unrelated: `test_video_pipeline.py` / `test_media_agents.py` / `test_media_providers.py` need the `ffmpeg`/`flite` binaries, which aren't installed in this environment, and one `test_trading_lab_data.py` test needs a live network provider. None touch Communications, Revenue Hunter, Sales Ops, Account Management, or TTT HQ.
- **Live smoke test**: booted the real `TTTHQHandler` against a scratch repo — `/api/comms/overview`, `/api/comms/workforce/activity`, `POST /api/comms/workforce/run`, and the full HQ index page all responded correctly.

## Preserved, unchanged

`git status` confirms only 4 files modified (`falguna/comms.py`, `falguna/account_management.py`, `falguna/email_admin.py`, `falguna/hq_web.py`) and 2 new files (`falguna/comms_workforce.py`, `tests/test_comms_workforce.py`). `static_export/`, `scripts/export_static_site.py`, `scripts/smoke_test_static_export.py`, Royal Table, every database, and every pre-existing untracked report/file are untouched. HEAD is still `9f381a9`. Nothing was committed, pushed, deployed, or sent externally.

## Exact staging commands (not run — for your review)

```bash
cd /Users/aryanbehera/.codex/.chatgpt-projects/g-p-6a88a9fa4fac81919cab1fd165faa445/falguna-bootstrap
git add falguna/comms.py falguna/account_management.py falguna/email_admin.py falguna/hq_web.py falguna/comms_workforce.py tests/test_comms_workforce.py
git status --short   # confirm only these six paths are staged
git commit -m "Build TTT AI Communications Workforce v1: Receptionist, Sales Rep, Support Rep, Account Manager"
```
No push, deploy, DNS, or commit included by these commands — that's your call whenever you're ready.

## Scope boundary

Not done, on purpose: no DNS/deploy/commit/push, no real email or message ever sent, no live email provider wired in, no change to the static export or Royal Table, no new approval system, no fabricated client facts or commitments anywhere in the drafting logic.
