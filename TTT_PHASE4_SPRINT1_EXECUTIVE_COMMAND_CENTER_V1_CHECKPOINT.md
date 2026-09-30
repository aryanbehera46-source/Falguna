# TTT HQ Phase 4 Sprint 1 — Company State + Executive Command Center V1

Date: 2026-09-30

Branch/worktree: `work/phase4-command-center-v1` / `falguna-phase4-command-center-v1`

Baseline: `ece6f8d` (`work/reliability-partner-pilot-v1`)

## Completed scope

- Added `CompanyStateService`, a read-only aggregation layer over the existing persisted CRM, proposals, invoices/payment evidence, Active Jobs, completion/QA, workforce, Needs Aryan, partners, referrals, and commissions.
- Added `GET /api/company-state`. It performs no writes and introduces no schema or parallel source of truth.
- Wired an Executive company state panel into the existing premium Command Center.
- Financial cards explicitly separate quoted (approved proposal + stored opportunity price), invoiced (non-cancelled invoice face value), collected (evidence-backed `amount_received`), and outstanding.
- Delivery and attention cards surface active projects, workforce attention, pending decisions, evidence-backed handovers/QA, attributed referrals, and eligible commissions.
- Every card drills into an existing operational screen (pipeline, revenue, delivery, workforce, Needs Aryan, referrals, commissions).
- Added explicit unavailable state for the new company snapshot and truthful empty states on a clean synthetic database.
- Fixed a mobile min-width interaction between the horizontally scrolling shortcut bar and the active view that clipped content at 390px.

## Files changed

- `falguna/company_state.py`
- `falguna/hq_web.py`
- `tests/test_company_state.py`
- `tests/test_hq_web.py`
- `TTT_PHASE4_SPRINT1_EXECUTIVE_COMMAND_CENTER_V1_CHECKPOINT.md`

## Verification

- Focused regression: `142 tests`, `OK`.
- Coverage included company-state aggregation, financial separation/non-duplication, existing Command Center, partner/commission, revenue delivery, billing, workforce, and HQ HTTP integration.
- Browser QA used a fresh synthetic root and local HQ server only.
- Desktop: 1440×960, no document overflow, no console warnings/errors, executive grid rendered correctly.
- Mobile: 390×844, no document overflow, active view width 362px inside the 362px content column, no console warnings/errors.
- Drilldown: clicking Quoted opened the real Sales Pipeline view.

## Boundaries and limitations

- No schema changes, database copies, live customer data, external actions, payout execution, push, merge, deploy, DNS change, or spend.
- The snapshot retains stored currencies and does not invent FX conversion; totals are accompanied by this warning.
- A quoted amount exists only where an approved proposal has an opportunity `final_price`; proposal text is not parsed for money.
- Independent QA is represented by the persisted completion checklist because no separate QA table exists in this baseline.
- The local Falguna execution server was intentionally not started, so the existing Active Execution card correctly showed Falguna unavailable; this does not affect the Company State API.
- Broad-suite status remains the Step 0 baseline: pre-existing hardcoded-port collisions, missing ffmpeg/flite, and provider-network failures were not reclassified or claimed fixed. This sprint ran the focused 142-test set only.
- The optional cross-department transition was not attempted: primary scope and verification consumed the bounded allowance, and orchestration remains the next task for Claude.

## Next task

Implement the smallest persisted, idempotent, human-gated transition for Opportunity Won → accepted project/intake readiness, reusing existing closing, Needs Aryan, lifecycle, and audit infrastructure. Do not broaden into all remaining Phase 4 sections.
