# Falguna Product UI + Chat Experience v1 — Verification Report

Branch: `claude-ui-chat-v1` (created off `main` @ `eee3912`, **nothing committed** —
the branch just exists so these changes sit apart from `main` while you review).
Repo: `~/.codex/.chatgpt-projects/g-p-6a88a9fa4fac81919cab1fd165faa445/falguna-bootstrap`
on your Mac. All edits were made directly in that working tree; they are currently
**uncommitted**. A copy of the exact diff is attached as `falguna-ui-chat-v1.patch`.

## What changed

- `falguna/schema_sqlite.sql` — additive only. Three new tables: `conversations`,
  `chat_messages`, `conversation_handoffs`, plus their indexes. Nothing existing
  touched.
- `falguna/store.py` — one-line change: the three new table names added to
  `StateStore.create()`'s allowlist. Nothing else touched.
- `falguna/chat.py` — **new file**. `ConversationStore` (persistence for
  conversations/messages/handoffs) and `ChatResponder` (generates a reply through
  the *same* replaceable `ModelGateway`/`CodexCliJSONTransport`/
  `ResilientCodexTransport` routing Work missions already use — same JSON-schema
  contract, same cost/usage accounting as `StructuredEditWorker`). Chat is given
  no worktree and no editable-file context, so it cannot read or propose edits to
  a real file — it can only converse and optionally suggest an objective for a
  human-initiated handoff.
- `falguna/web.py` — added routes for conversations/messages/rename/handoff/
  search/settings; refactored the old `_start` into a shared `_launch()` used by
  *both* `/api/runs` and the new handoff endpoint, so a Chat→Work handoff goes
  through the identical discovery → policy → worktree → verification pipeline as
  a directly-started mission (it cannot skip discovery or widen scope). The
  served page (`INDEX_HTML`) was rewritten as a Chat/Work/Search/Projects/
  History/Settings product with hash-based client-side routing, a persistent
  sidebar, a modern message-bubble chat UI, a cleaned-up Work timeline, and the
  same two responsive breakpoints as before (850px / 520px), extended to the new
  views.
- `tests/test_bootstrap.py`, `tests/test_hq_web.py` — updated the handful of
  assertions that check exact `INDEX_HTML` label text (the page's copy changed;
  the *capabilities* being asserted — operator controls, responsive breakpoints,
  TTT HQ separation — did not) and added one new test proving Chat and Work are
  distinct wired-together surfaces.
- `tests/test_chat_web.py` — **new file**, 19 new tests: persistence-layer tests
  for `ConversationStore` (including a test that a handoff row only ever
  references a `run_id` the real `ControlPlane.create_mission`/`start` produced),
  unit tests for `ChatResponder` against a fake transport (same pattern the
  existing suite already uses for `StructuredEditWorker`), and live-HTTP tests
  against a real `FalgunaHandler` server (conversation CRUD, graceful
  no-codex-installed degradation, search, settings, and a test proving handoff
  is rejected by the exact same "Select an approved project" guardrail as a
  direct `/api/runs` call).

## What was explicitly preserved (not touched)

`falguna/orchestrator.py`, `verification.py`, `review.py`, `isolation.py`,
`policy.py`, `audit.py`, `supervisor.py`, `discovery.py`, `continuity.py`,
`workers.py`, `browser.py`, `capabilities.py`, `gitops.py` — worktree isolation,
verification, independent review, recovery/supervisor, the hash-chained audit
log, model routing, checkpoint/resume, and the human-only approval gate are all
unmodified. `falguna/hq_web.py` and `falguna/ttt_hq.py` (TTT HQ) are untouched —
still a fully separate process/port/page.

## Decisions made without asking again

You answered "no preference" on the two scoping questions, so I went with the
recommended option in each case:
1. **Chat replies are wired to the real model gateway** (not a stub) — same
   `ResilientCodexTransport(CodexCliJSONTransport(...))` path Work already uses.
   On this machine there's no `codex` executable on `PATH` inside the remote
   dev VM I ran tests in, so live chat replies degrade gracefully to a visible
   error bubble rather than crashing or faking a reply — verified below. On your
   actual Mac, where `codex` is authenticated, real replies should work as-is.
2. **Patch delivered as uncommitted working-tree changes** on a new branch, plus
   an exported `.patch` file as a backup/review copy.

## Test results

Ran `python3 -m unittest discover -s tests -v` twice: once on `main` before any
change (baseline), once on `claude-ui-chat-v1` after all changes.

| | Total | Passed | Failed | Errors | Skipped |
|---|---|---|---|---|---|
| Baseline (`main`) | 112 | 97 | 13 | 2 | 1 |
| After this pass | 131 | 116 | 13 | 2 | 1 |

The 15 non-passing tests are **byte-for-byte the same test names** before and
after — this environment (a remote Linux VM bridged to your Mac) lacks macOS's
Seatbelt sandbox and a real Codex CLI, which is exactly the "same 15
pre-existing, Mac-verified-clean failures" your own `RELEASE_NOTES_V1.md`
already documents from the last pass. Nothing in this change touched that set,
grew it, or shrank it. Full list (all in `test_bootstrap.BootstrapTests`,
unrelated to this pass — supervisor/sandbox/browser-discovery tests):

```
ERROR: test_browser_discovery_supports_nested_manifest_without_fixed_executable
ERROR: test_milestone_task_verification_survives_bounded_repair
FAIL:  test_approved_project_profile_is_bounded_and_local
FAIL:  test_end_to_end_done_candidate_requires_pending_human_approval
FAIL:  test_failed_bounded_repair_resume_returns_to_worker_with_diagnostics
FAIL:  test_forced_interruption_resumes_from_checkpoint
FAIL:  test_missing_supervisor_state_is_rebuilt_from_immutable_verification
FAIL:  test_mission_records_stage_timing_metrics
FAIL:  test_operational_view_summary_and_three_human_decisions
FAIL:  test_pause_at_safe_boundary_then_resume
FAIL:  test_repair_is_available_after_two_worker_retries_and_stops_after_one
FAIL:  test_review_correction_keeps_verification_attempt_artifacts_immutable
FAIL:  test_review_failure_runs_one_bounded_correction_then_fresh_review
FAIL:  test_review_required_file_outside_scope_enters_needs_aryan
FAIL:  test_verification_failure_gets_one_bounded_repair
```

**19 new tests, all passing**, covering: conversation CRUD and archival, message
persistence, `ChatResponder`'s schema/transport contract (success, refusal,
malformed JSON, transport failure), search, and — the safety-relevant one — a
handoff row that only ever references a `run_id` produced by the real
`ControlPlane`.

## Live smoke test (real server, real HTTP, not just unit tests)

Booted `python3 -m falguna --root <fresh throwaway repo> web` and hit it with
real HTTP requests (against a throwaway repo, not your production
`.falguna/state.db` — see caveat below):

- `GET /` → 200, serves the new page (39,509 bytes)
- `GET /api/settings` → 200, correct model + preserved-boundaries list
- `POST /api/conversations` → 201, creates a conversation
- `POST /api/conversations/<id>/messages` → 200, persists the user message, and
  since no `codex` executable exists in this environment, the assistant turn
  comes back with a clear `MODEL_UNAVAILABLE: authenticated Codex executable
  not found` error field rather than a fake reply or a 500 — this is the
  graceful-degradation path by design, and it's exactly what should still work
  correctly once you run this on your Mac where `codex` is authenticated (the
  same-shaped success path is what `ChatResponderTests` verifies with a fake
  transport).
- `GET /api/conversations` and `GET /api/search?q=...` → 200, both find the new
  conversation

## A caveat you should know about, unrelated to the code itself

Running the real server against your actual repo's `.falguna/state.db`
(2.3&nbsp;MB) through this remote-VM bridge hit a `sqlite3.OperationalError:
disk I/O error` — SQLite doesn't get along with this particular mounted-folder
bridge. That's why the live smoke test above ran against a fresh throwaway repo
instead (created and destroyed entirely in `/tmp` on the VM, never touching your
real data). It's an environment limitation of this bridge, not a bug in the
patch — your existing `README.md` already documents that meaningful
verification here happens Mac-side via the launcher apps. I'd suggest verifying
the live server the normal way: double-click `launcher/Falguna.app` (or
`python3 -m falguna --root . web` in a real Terminal on your Mac) once you've
reviewed the diff.

## What's in scope vs. deliberately deferred

Built: sidebar (New Chat / Chat / Work / Search / Projects / History /
Settings), persistent conversations, modern message-bubble chat UI, Chat→Work
handoff that pre-fills the objective and links back to its originating
conversation from the Work timeline, a cleaned-up Work timeline, project and
history reopening, and the responsive desktop/mobile layout, all while leaving
TTT HQ untouched and not touching Revenue Hunter, Media Engine, or Trading.

Deferred (flagged honestly, not hidden): I did not write an HTTP-level test that
drives a full Chat→Work handoff through real `ProjectDiscovery` to a running
mission (that needs your live Codex-authenticated Mac environment to be
meaningful, and I didn't want to write a test that gambles on discovery
confidence heuristics I hadn't fully audited). Instead the handoff→real-run
safety property is proven directly against the control plane (see
`test_handoff_links_a_real_run_produced_by_the_real_control_plane`), and the
handoff endpoint's guardrail parity with `/api/runs` is proven over real HTTP.
Settings is read-only in this pass by design — nothing in the UI can weaken
policy, verification, or the approval gate.

## Next step

Please review `falguna-ui-chat-v1.patch` (or just open the repo — the branch
`claude-ui-chat-v1` has the same changes sitting uncommitted in your working
tree, nothing committed). Nothing will be committed or merged unless you say so.
