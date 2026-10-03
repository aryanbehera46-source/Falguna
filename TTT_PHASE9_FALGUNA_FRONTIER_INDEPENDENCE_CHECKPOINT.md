# TTT / FALGUNA Phase 9 — Frontier & Independence Checkpoint 1

**Date:** 2026-10-03

**Status:** **PHASE 9 IN PROGRESS — NOT FINAL ACCEPTANCE**

**Branch:** `phase9/falguna-frontier-independence-v1`

**Accepted Phase 8 baseline:** `e4bc28c3018d6e20aa9d4a69c31aaae7c826427a`

**Environment:** isolated local worktree; synthetic/local data only; no external consequential action

## What this checkpoint establishes

Phase 9 now has one durable, provider-neutral control plane above the already accepted FALGUNA systems. It does not replace the existing engineering mission runner, Research, browser/computer runtime, memory, model router, artifacts, or TTT HQ. It adds the shared objective/graph/policy/evidence layer those systems can progressively plug into.

- Durable objectives with Work, Code, Research, Browser, Data, TTT Internal and Markets Research modes.
- Explicit ASK, ASSIST, EXECUTE_WITH_APPROVAL, AUTONOMOUS_WITHIN_POLICY and COMPANY_AUTONOMOUS policy levels.
- Dependency-aware execution graphs with lead, code, research, QA, security, data, design, sales and finance-analysis roles.
- Atomic worker claiming, bounded leases, heartbeat state and expired-worker recovery.
- Durable evidence, events, approvals, stop conditions, limits, checkpoints and versioned continuity bundles.
- Emergency stop that cancels unfinished graph nodes while retaining completed evidence and audit history.
- FALGUNA model-asset registry with dataset provenance, capability/constraint metadata, benchmark evidence and candidate → evaluated → approved → retired promotion.
- Effort vocabulary for Auto, Fast, Balanced, Deep, Maximum and Autonomous, with policy/capability/privacy-aware owned/local/external selection.
- Permissioned MCP-style plugin manifest foundation with explicit scopes, allowed endpoint schemes, bounded timeouts, sandbox metadata, no secret forwarding and action-time approval for live external actions.
- Independence Gate records for real repo-task results. A result requires implementation, tests, diff review, checkpoint, security review, browser evidence where applicable, evidence records and an external audit. Three passing tasks are required before the gate can report repeatability; one success never establishes independence.
- Read-only TTT HQ context summary. TTT remains authoritative for commercial state; FALGUNA cannot mutate it through this control plane.
- Studio artifact, digital-twin, scenario-simulation and benchmark registry schemas are present as common foundations.
- A chat-first UI now exposes persistent Objectives under optional Workspace navigation, while existing Research, Work/Code, Projects, Memory, Studio/Files and Plugins remain available without provider jargon.

## Universal execution graph

The graph persists objective, task nodes, dependencies, assigned agents, governed tool name, approval class, structured inputs/outputs, attempts, leases, evidence, approvals, events, limits and next action. The standard graph is:

`inspect → research → bounded execution → QA + security → checkpoint/report`

The graph exposes state and evidence, not hidden model reasoning. External/tool content is stored as inert JSON/data. Cross-objective and cross-organization dependencies are rejected.

## Autonomy and safety state

The following action classes are hard-bound regardless of autonomy level: payments, refunds, payouts, beneficiary changes, legal commitments, production deployments, external outreach, major spending, security-critical actions, live trading, push, merge and DNS changes. Recording an approval does not turn this control plane into an executor; a future dedicated executor must re-check action-time authorization.

Unknown action classes fail closed. Live external actions and live trading report disabled. The existing FALGUNA engineering runner still requires a human merge decision and does not push, merge or deploy.

## Existing capabilities reused

- `falguna/model_router.py`: local/external provider-neutral routing and privacy enforcement.
- Existing engineering mission/runtime modules: isolated worktrees, bounded commands, tests, checkpoints, semantic review and merge approval.
- Existing Research: source collection, citations, evidence-only synthesis and prompt-injection treatment.
- Existing browser/computer runtime: isolated sessions, screenshots/evidence and sensitive-action gates.
- Existing Memory & Knowledge: scoped/provenance-aware memory and local-first retrieval.
- Existing Trading Lab: research/backtest/paper architecture with live capital disabled.
- Existing TTT HQ/Phase 8: authoritative commercial state and human decisions.

## Own-model and AI Laboratory roadmap

Implemented now: model asset/version registry, ownership label, capability and constraint metadata, dataset provenance, benchmark registry, evidence requirement, controlled promotion and policy-aware selection.

Not implemented or claimed now: frontier-scale base-model training, distillation jobs, fine-tuning workers, dataset acquisition/licensing, GPU cluster orchestration, distributed checkpoints, alignment training or production inference serving.

Realistic future ranges vary heavily by architecture and are planning inputs, not commitments:

- Small specialist models: curated/licensed task data, one or several modern GPUs, days to weeks for fine-tuning/evaluation, plus ongoing red-team and regression work.
- Medium proprietary models: billions of tokens, multi-node accelerator capacity, robust data governance, checkpoint/recovery and a sustained evaluation team.
- Frontier-scale base models: orders of magnitude more data/compute/capital and a dedicated research, infrastructure, safety and operations organization. This is not appropriate to pretend-complete in Phase 9 checkpoint work.

Competitor benchmark values are deliberately not hardcoded. The registry stores source/evidence and suite versions so current authoritative measurements can be refreshed at evaluation time.

## Desktop distribution architecture

The existing local Python application remains the working development build on macOS. The distribution direction is a thin cross-platform shell around the local/cloud control-plane APIs, preserving the current backend and avoiding a heavyweight rewrite. Required seams are defined by the existing localhost API and the new durable objective state: OS file/folder grants, repo/terminal grants, secure credential storage, browser/computer channel, local model runtime, cloud task status, notifications, device sessions, crash recovery and update metadata.

Remaining work: select and spike the shell against the actual UI/runtime; signed/notarized macOS packaging; signed Windows installer; Linux AppImage/deb/rpm; credential-store adapters; device registration; update signing; offline/cloud conflict handling; packaging CI. No package is represented as signed or production-ready in this checkpoint.

## Verification evidence

- Python compilation: passed for `frontier`, `web` and `store`.
- Focused Phase 9 service/API tests: **9 passed**.
- Shared model, memory, browser, orchestration and Phase 8 suite invocation: **212 passed, 2 skipped**, plus one invocation error caused by requesting the nonexistent module `tests.test_research`; this was a test-selection error, not an application assertion failure.
- Corrected research/security selection: **53 passed** and **1 pre-existing environment-sensitive timeout** in `test_research_with_sources_but_no_authenticated_codex_degrades_to_failed_not_a_crash` (HTTP call exceeded its 5-second test limit while model fallback waited). Phase 9 does not change that research execution path.
- Browser/UX acceptance: **4 passed** at **1440×960** and **390×844**. Covered objective creation, graph creation, continuity checkpoint, reload persistence, visible safety boundaries, no horizontal overflow and no captured browser console/page errors.
- Browser harness initialization exposed a concurrent first-migration race; the synthetic browser server now completes migration before accepting threaded requests. Production migration concurrency remains a broader infrastructure hardening item.

## Security review performed

- Organization-scoped reads and writes; cross-organization read returns controlled not-found.
- Same-objective dependency enforcement.
- Atomic claim prevents two workers owning one ready node.
- Lease-owner-only completion and expired lease recovery.
- Consequential and unknown action classes fail closed.
- Human actor/reason required for approval decisions and emergency stop.
- Plugin wildcard/admin/root scopes rejected; non-http(s)/MCP endpoint schemes rejected; secrets forwarding remains false.
- Model promotion cannot occur without benchmark evidence; approval requires a passing run.
- TTT context is read-only and does not duplicate commercial records.
- Prompt-like plugin text remains inert persisted data.

## Limitations and next implementation slice

This is the first integrated backbone, not final Phase 9 acceptance. The graph currently coordinates and records work but does not yet dispatch every node into the existing engineering, Research and browser workers. Studio/digital-twin/simulation tables need service APIs and UI. Data execution needs a sandboxed SQL/Python adapter. The voice seam, cloud queue/worker service, hosted identity/device binding, desktop shell spike, benchmark refresh collectors, local fine-tuning pipeline and three-task Independence Gate campaign remain open.

**Exact next step:** implement the graph dispatcher/adapters that bind CODE nodes to the existing isolated engineering runner, RESEARCH nodes to Research, and BROWSER nodes to the governed browser runtime; add checkpoint/resume reconciliation and execute the first synthetic Independence Gate repo task end-to-end. Do not start Phase 10.

## External-action ledger

No push, merge, deploy, DNS change, payment, refund, payout, provider activation, real customer/provider data, outreach, spending, live trade, company/legal action or Phase 10 work occurred.
