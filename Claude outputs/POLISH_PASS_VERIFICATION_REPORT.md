# Falguna UI Polish Pass — Verification Report

Branch: `claude-ui-chat-v1` (same branch as the first pass — **still nothing
committed**). Repo:
`~/.codex/.chatgpt-projects/g-p-6a88a9fa4fac81919cab1fd165faa445/falguna-bootstrap`
on your Mac. All edits were made directly in that working tree and remain
**uncommitted**. The attached `falguna-ui-polish-v1.patch` is the full
cumulative diff from `main` (v1 UI+Chat + this polish pass together, since
v1 was never committed) — open it, or just look at the working tree; nothing
will be committed or merged unless you say so.

## What this pass changed (visual/product only — no architecture)

All ten of your numbered points, addressed directly in `falguna/web.py`'s
`INDEX_HTML` (and one small, additive backend field in `falguna/chat.py`):

1. **General-purpose Chat home.** "What should we build?" is gone. The Chat
   welcome screen now reads "How can I help?" / "Ask a question, think
   something through, or describe what you're working on." — no coding
   framing.
2. **Visual hierarchy / empty space.** Body background is now a soft warm
   radial gradient instead of flat black; the welcome screen has a centered
   icon badge, headline, subtext and a tight chip row instead of a wall of
   empty space above four boxes.
3. **Typography / spacing / sidebar / nav.** Sidebar nav items now use a
   proper inline SVG icon system (chat/work/search/projects/history/settings)
   instead of emoji, consistent 8–9px radii and spacing tokens throughout,
   and a real "New chat" button with an icon.
4. **Color identity.** Full palette replacement: the old green-heavy scheme
   is gone. New `:root` tokens are a warm yellow/orange/cream accent family
   (`--accent:#e8a33d`, `--accent-hi:#f4bd63`) over a warm dark
   cream-black background (`--bg:#161310`, `--text:#f7f0e3`), calmer and
   less "heavy black" than before.
5. **Composer.** Rebuilt: attachment icon button (clearly marked
   not-yet-wired via a tooltip + click alert rather than pretending to
   work), a static "Chat" mode chip, a circular accent send button, and the
   developer-style explanatory line moved out of the primary surface into a
   small one-line footer ("Falguna can be wrong. Hand off to Work for
   changes that need to be verified.") instead of the old blunt capability
   disclaimer sitting directly under the input.
6. **Suggested prompts.** Four large engineering-only boxes are gone. Now
   three small pill-shaped chips, general-purpose: "Help me think something
   through", "Summarize my recent Work missions", "Turn an idea into a Work
   objective".
7. **Recent chat presentation.** Sidebar list is now grouped by recency
   (Today / Yesterday / Previous 7 days / Older) instead of one flat list,
   and each row shows a one-line preview of the last message (new
   `last_message_preview` field — see below). Renamed the section label
   "Recent" → "Recent chats" for clarity.
8. **Top header / balance.** The static "v1 UI + CHAT" badge is replaced
   with a live model-identity pill (dot + model name, pulled from
   `/api/config`), and the header layout is tighter with a subtle gradient.
9. **Overall feel.** Warm palette + calmer copy + real icon system + the
   composer changes above are aimed squarely at "ChatGPT simplicity +
   Claude calmness + Falguna identity" rather than an internal console.
10. **Work stays engineering-focused.** The Work view's copy, controls,
    and card/detail layout are untouched in substance — only the shared
    color tokens and icon system carry over, so Work still reads as a
    mission-timeline tool, not a chat.

### The one backend touch: `last_message_preview`

`ConversationStore.list_conversations()` in `falguna/chat.py` now attaches a
read-only `last_message_preview` string to each conversation row (last
`chat_messages.content` for that conversation, whitespace-collapsed,
truncated to 140 chars with an ellipsis). This is purely additive
presentation data for the sidebar — it doesn't change conversation identity,
ordering, or any existing field, and nothing else reads it. Covered by a new
unit test (`test_list_conversations_carries_a_truncated_last_message_preview`)
and verified live in the smoke test below.

## What was explicitly preserved (not touched this pass)

- Chat/Work separation — still two distinct views (`renderChatView` /
  `renderWorkView`), wired only through the same `/handoff` endpoint as
  before.
- TTT HQ separation — `falguna/hq_web.py` / `falguna/ttt_hq.py` untouched;
  `test_falguna_html_no_longer_contains_ttt_hq_surface` still passes.
- All safety architecture — `orchestrator.py`, `verification.py`,
  `review.py`, `isolation.py`, `policy.py`, `audit.py`, `supervisor.py`,
  `discovery.py`, `continuity.py`, `workers.py`, `browser.py`,
  `capabilities.py`, `gitops.py` — zero changes.
- Existing persistence — same three additive tables from the first pass;
  no schema changes this time.
- Existing Work functionality — mission form, controls (Pause/Cancel/
  Resume/Retry/Approve/Reject/Request Changes), evidence cards, and the
  discovery→policy→worktree→verification→review→approval pipeline are
  byte-identical in behavior; only their container's colors/icons changed.
- No Search implementation change, no Media/Trading/Revenue Hunter, no new
  systems — Search is still the same simple title/message substring lookup
  from the first pass.

## Test results

Ran `python3 -m unittest discover -s tests -v` on the branch after this
pass, from a clean `__pycache__`:

| | Total | Passed | Failed | Errors | Skipped |
|---|---|---|---|---|---|
| Baseline (`main`) | 112 | 97 | 13 | 2 | 1 |
| After v1 pass | 131 | 116 | 13 | 2 | 1 |
| After this polish pass | 132 | 116 | 13 | 2 | 1 |

The 15 non-passing tests are **byte-for-byte the same test names** as both
prior baselines — same environment limitation (no macOS Seatbelt sandbox, no
real Codex CLI in this remote-VM bridge), already documented in
`RELEASE_NOTES_V1.md`. Nothing in this pass touched, grew, or shrank that
set.

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

The one new test (`test_list_conversations_carries_a_truncated_last_message_preview`)
passes, plus every previously-passing test — including all 19 from the
first pass, and the existing `test_local_web_shell_exposes_required_operator_controls`,
`test_v11_controls_are_available_in_responsive_ui`,
`test_chat_and_work_are_both_present_and_distinct_surfaces`, and
`test_falguna_html_is_otherwise_unchanged` — still pass, because the copy
they assert on either didn't change or was updated alongside the code
(see below).

### Test changes made (copy-only, to match intentional wording changes)

- `test_local_web_shell_exposes_required_operator_controls`: the literal
  string `"What should we build?"` was asserted; updated to
  `"How can I help?"` to match the new general-purpose headline. Every
  other asserted label (`Falguna`, `internal alpha`, `Approved projects`,
  `Recent missions`, `Recent chats`, `Run mission`, `Chat`, `Work`,
  `Search`, `Projects`, `History`, `Settings`, `Needs approval`, `Approve`,
  `Reject`, `Request Changes`, `Resume`, `Human approval stays required`,
  `side-empty`, `nav-icon`, `Open navigation`, `min-height:60px`,
  `overflow-y:auto`, `closeSidebar`, `flex:0 0 auto`) is unchanged and
  still present verbatim.
- `test_chat_and_work_are_both_present_and_distinct_surfaces`: asserted the
  literal disclaimer `"Chat has no tools and cannot edit a repository"`
  inside the handoff panel; that copy was reworded per your instruction
  #5 (move developer-style text off the primary surface) to `"Chat can't
  touch a repository itself"` — the assertion was updated to match. Note
  this exact sentence is unrelated to, and does not affect, the separate
  `/api/settings` boundaries list, which still says
  `"Chat has no tools and cannot edit a repository; only a human handoff
  can start a mission"` verbatim (verified live below) — that's
  machine-readable API text, not UI copy, and instruction #5 only asked to
  change the primary chat surface.
- `test_v11_controls_are_available_in_responsive_ui`: asserted the static
  `"v1 UI + CHAT"` badge text, which instruction #8 explicitly asked to
  replace with a real model indicator; updated the assertion to check for
  the `model-pill` CSS class (the structural marker of the new dynamic
  badge) instead of the removed static string. `Pause safely`, `Cancel`,
  `Resume / Retry`, `safe boundary`, and both responsive breakpoints
  (`@media(max-width:850px)`, `@media(max-width:520px)`) are unchanged and
  still asserted verbatim.
- `tests/test_hq_web.py`: no changes needed this pass — every label it
  checks (`Falguna`, `Chat`, `Work`, `Approve`, `Reject`,
  `Request Changes`, and the TTT-HQ-absence checks) is still present /
  still absent exactly as before.

No test had its assertion *weakened* — every change swaps one exact string
for the new exact string the intentional copy change produced, or swaps a
removed literal for the structural marker that replaced it.

## Live smoke test (real server, real HTTP)

Booted `python3 -m falguna --root <fresh throwaway repo> web` (same pattern
as the first pass — throwaway repo in the VM's native `/tmp`, never your
real `.falguna/state.db`, for the same SQLite-over-FUSE reason documented
in the first verification report) and hit it with real HTTP requests:

- `GET /` → 200, serves the new page (46,543 bytes — the previous v1 page
  was 39,509 bytes; the increase is the icon system, new CSS, and grouped
  sidebar logic).
- `GET /api/settings` → 200, boundaries list intact verbatim, including the
  API-level "Chat has no tools and cannot edit a repository" line noted
  above.
- `POST /api/conversations` → 201, creates a conversation.
- `POST /api/conversations/<id>/messages` → 200, persists the user message;
  assistant turn degrades gracefully to `MODEL_UNAVAILABLE: authenticated
  Codex executable not found` in this codex-less environment, same as the
  first pass (expected to succeed for real on your Mac where `codex` is
  authenticated).
- `GET /api/conversations` → 200, and the new field is live end-to-end:
  `"last_message_preview": ""` for this conversation (correct — its most
  recent message is the empty-content assistant error, not the user's
  text, which is exactly what "last message" should show).
- `GET /api/search?q=...` → 200, finds the new conversation.

## What's in scope vs. deliberately deferred (unchanged from your ask)

Built: all ten numbered points, plus the one additive backend field they
depend on. Nothing else touched.

Deferred, by your explicit instruction: no Search implementation change, no
Media/Trading/Revenue Hunter, no new systems. Also deferred, as genuinely
out of scope for "polish": a real attachment upload flow (the icon is
present and honest about not being wired yet — clicking it says so rather
than silently doing nothing or faking success) and a real
provider/model *switcher* (the header now shows the actual configured
model as a live pill, per point 5's "model/provider selector if
available" — but making it interactive would be a policy-relevant change
your instructions didn't ask for and I didn't want to quietly expand
scope on my own).

## Next step

Please review `falguna-ui-polish-v1.patch` (or just open the repo — same
branch, same uncommitted working tree, now with both the v1 UI/Chat pass
and this polish pass sitting together, still nothing committed). Boot it
the normal way (`python3 -m falguna --root . web` on your Mac, or
`launcher/Falguna.app`) to see it live. Nothing will be committed or merged
unless you say so.
