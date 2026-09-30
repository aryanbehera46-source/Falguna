# FALGUNA Chat-First Commercial UX V1 Checkpoint

Date: 2026-09-30

## Scope completed

- Default route is the clean new-chat screen; the Home dashboard is no longer a user-facing destination.
- Chat landing removes operational count noise and internal-alpha/private-workspace labels.
- Existing Work, Mission Control, Activity, Projects, Memory, Files, History, Search, notifications, and settings remain available.
- New-chat composer exposes the actually available model choices and applies a selected model before the first message.
- Local profile menu provides working Settings, Help, and Connected tools routes.
- Login, team accounts, invites, and a public plugin marketplace remain explicitly deferred; no fake actions were added.
- Existing Falguna mark is reused for the product identity; source brand assets were not changed.

## Commercial experience V4 refinement

- Replaced exposed provider/routing jargon on the main product surface with one clear `Falguna` experience; technical provider and privacy controls remain in Settings.
- Rebuilt the light visual system with brighter neutral surfaces, higher-contrast typography, warm brand depth, elevated cards, and a larger editorial landing hierarchy.
- Rebuilt the composer around a real `+` capability menu for Files and folders, Web Research, Work, and Plugins.
- New-chat file attachment now creates a real draft conversation only when a file is selected, uploads into the synthetic conversation store, and associates the attachment with the first message.
- Added a real Plugins screen for the built-in capabilities actually available: Web Research, Work, Files, Memory, Projects, and Browser.
- Third-party OAuth installation, public marketplace, shared plugins, and team accounts remain explicitly deferred rather than shown as installed.

## Isolation and data boundary

- Branch: `work/chat-first-commercial-v1`
- Based on: `4049061` (`work/phase4-command-center-v1` Sprint 2 checkpoint)
- Worktree: `falguna-chat-first-commercial-v1`
- Preview: synthetic temporary root only; no real conversations, live database, launcher configuration, merge, push, or deployment changed.
- Protected uncommitted Sprint 3 edits remain in `falguna-phase4-command-center-v1`.

## Verification

- Python compile passed.
- Focused regression: 21 tests passed (`HTMLSeparationTests`, `ConversationStoreTests`, `ChatResponderTests`).
- Extracted browser JavaScript passed `node --check`.
- Desktop browser QA: 1440x960, chat landing, account menu, Help, Settings, no console warnings/errors.
- Desktop browser QA additionally covered the expanded capability menu and Plugins screen.
- Responsive browser QA: 780x1688 CSS viewport (mobile breakpoint active), no horizontal overflow, no new console errors.
- A broader combined web run was not counted as passing: it reproduced the known SQLite disk-I/O / server teardown instability and was interrupted rather than overstated.

## Deferred commercial phases

- Phase 6/7: hosted authentication, customer accounts, multi-tenancy, invites/referrals, public plugin marketplace and installation lifecycle, hosted support/account recovery.

## Next action

Review the isolated preview. Launcher switching, merging into the Phase 4 branch, deployment, and real-data use require separate approval.
