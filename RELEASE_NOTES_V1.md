# FALGUNA + TTT INTERNAL v1 — Release Candidate Notes

Branch: `claude-ttt-hq-v1`. This document covers the TTT HQ separation pass
(commit `d08226a` on the Mac repo) plus the release-quality pass that
produced this file.

## What this release is

Two distinct local-only applications sharing one backend:

- **Falguna Engineering** (`falguna web`, port 8765) — the AI engineering
  work platform: mission creation, isolated worktrees, native + browser
  verification, independent semantic review, supervisor/recovery,
  human-only merge gate.
- **Twenty Two Technologies HQ** (`falguna hq`, port 8766) — the company
  operating surface: Boardroom, Master Vision Backlog, and a unified Needs
  Aryan queue. Not a tab inside Falguna — a separate process, separate
  port, separate HTML, separate macOS launcher.

They share one SQLite file (`<repo>/.falguna/state.db`) and one audit log.
Neither app talks to the other over HTTP; TTT HQ reads Falguna's mission
state directly from the shared store, and writes decisions back through
Falguna's own existing `ControlPlane.decide_merge` — never a parallel path.

## Issues found and fixed in this pass

1. **Needs Aryan showed already-decided missions as still pending.**
   `decide_merge` (Falguna's own, unmodified) only updates the `approvals`
   table, not `supervisor_states` — that row only changes when the run
   itself resumes. The queue's pending-list filter didn't account for
   that, so an approved/rejected mission kept appearing as `PENDING`
   indefinitely. **Fixed**: a run is now excluded once its merge decision
   is recorded, regardless of the underlying `supervisor_states` row.
   Caught via a live two-server HTTP integration test, not a code review
   guess. New regression test:
   `test_needs_aryan_stops_listing_a_run_once_its_merge_decision_is_recorded`.

2. **Falguna's own launcher could crash on start** on any machine without
   `Codex.app` or a `ChatGPT*.app` installed in `/Applications` — zsh's
   default glob behavior aborts the whole script on an unmatched pattern
   containing a wildcard, with no error dialog (it never reaches
   `show_error`). Pre-existing, unrelated to the TTT HQ work, found while
   testing both launchers side by side. **Fixed**: one-token null-glob
   qualifier (`(N)`) on that specific pattern. This did not surface on
   your Mac because you have one of those apps installed, so the glob
   already matched there.

3. **TTT HQ's page had zero responsive breakpoints** (Falguna's has two).
   Fixed-width 250px sidebar would crowd out content on narrow viewports.
   **Fixed**: one `@media(max-width:820px)` rule, sidebar collapses to a
   horizontal bar, content stacks to one column. New regression test:
   `test_hq_html_has_a_responsive_breakpoint`.

4. **TTT HQ's CSP was more permissive than necessary** —
   `connect-src` allowed Falguna's origin, but the page only ever
   `fetch()`s same-origin; the Falguna link is a plain page navigation
   (`<a href>`), not a fetch. **Fixed**: tightened to `connect-src 'self'`,
   now identical to Falguna's own CSP.

No other issues found. Full detail on what was checked and how is in the
handoff message this file accompanies.

## Tests

112 tests total (110 going into this pass + 2 new). Same 15 pre-existing,
Mac-verified-clean failures as every prior pass (sandbox-environment noise
only — Seatbelt and the Codex transport don't exist here). Zero new
failures from this pass's changes.

## Capabilities (what actually works today)

- Boardroom: perspectives (Strategy/Technology/Revenue/Finance-Risk/
  Operations), discussion, Approve/Reject/Defer/Request Changes, decisions
  persist through a full process restart (proven, not claimed).
- Master Vision Backlog: Future/Planned/Active/Done/Deferred, full
  field-level history, auto-created from an approved Boardroom decision.
- Needs Aryan: unified queue of business items you create directly, plus
  live Falguna missions in `NEEDS_APPROVAL` — read from the shared store,
  never copied. Deciding an actionable engineering item routes through
  Falguna's real `decide_merge`. Non-actionable engineering items (stuck
  mid-run, not at the merge gate) show as inspect-only with a real deep
  link into Falguna Engineering — proven end-to-end in this pass, not just
  described.
- Independent launch/stop for both apps, proven safe in both directions
  (stopping one never touches the other — tested with both genuinely
  running simultaneously), idempotent (running a launcher twice while
  already running does not spawn a duplicate process), and safe against a
  stale/wrong PID file (refuses to kill an unrelated process).
- Full route separation: Falguna Engineering no longer serves any TTT HQ
  route (`404`, tested); TTT HQ never serves any Falguna mission route
  (`404`, tested).

## Limitations (current, honest)

- Opportunities / Sales Pipeline / Clients / Active Jobs / Revenue /
  Ventures are listed in TTT HQ's nav as disabled "Coming soon" items —
  intentionally not built. No data model, no backend, no fake UI behind
  them.
- Revenue Hunter is not merged. `falguna/handoff.py` (the acceptor) exists
  and is tested, but nothing calls it yet.
- `GET /api/hq/overview` is implemented and tested but not yet consumed by
  the UI — available for future use (e.g. a header/about panel).
- No auth, no multi-user — matches the existing single-owner design of
  both apps.
- Needs Aryan's engineering-item view is read-only aside from the actual
  merge decision; it cannot resume a mid-run mission (scope expansion,
  etc.) — that still requires opening Falguna Engineering directly, by
  design (no parallel write path into mission state).

## Mac-only verification (exact commands)

```bash
cd /Users/aryanbehera/.codex/.chatgpt-projects/g-p-6a88a9fa4fac81919cab1fd165faa445/falguna-bootstrap

# 1. Apply this pass on top of d08226a, then the tests
git apply /path/to/ttt-hq-release-pass.patch
python3 -m unittest discover -s tests -v   # expect 112 tests, OK

# 2. Both launchers, independently
open "launcher/Falguna.app"                       # :8765
open "launcher/Twenty Two Technologies.app"        # :8766
curl -s http://127.0.0.1:8765/api/config
curl -s http://127.0.0.1:8766/api/config

# 3. Stop one, confirm the other is untouched
open "launcher/Stop Twenty Two Technologies.app"
curl -s http://127.0.0.1:8765/api/config           # Falguna must still answer
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8766/   # must fail

open "launcher/Stop Falguna.app"

# 4. Confirm the Falguna launcher fix specifically (this is the one bug
#    that could NOT reproduce on your machine, since you already have
#    Codex.app or a ChatGPT app installed -- verify the code path is sane
#    by inspecting it, or temporarily rename /Applications/Codex.app to
#    force the glob-miss and confirm the launcher still starts cleanly):
zsh -n "launcher/Falguna.app/Contents/MacOS/Falguna" && echo "syntax OK"

# 5. Deep-link: from TTT HQ's Needs Aryan, click "Open in Falguna
#    Engineering" on any engineering-sourced item and confirm it opens
#    Falguna Engineering directly to that mission (?run=<id> in the URL).

# 6. Restart persistence, for real
#    (create a Boardroom topic, approve it, then:)
open "launcher/Stop Twenty Two Technologies.app"
open "launcher/Twenty Two Technologies.app"
#    -> topic and its auto-created backlog item must still be there.
```

## Merge readiness

**Ready to merge into `main`**, pending only the Mac verification commands
above. No safety gates were touched or weakened. No new systems, no
Revenue Hunter merge, no speculative features. Everything in this pass is
either a bug fix (all four described above) or a test.
