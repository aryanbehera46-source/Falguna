# TTT / FALGUNA Phase 9 — Frontier & Independence Checkpoint 2

**Date:** 2026-10-03

**Status:** **PHASE 9 — FINAL ACCEPTANCE**

**Final acceptance:** See `TTT_PHASE9_FINAL_ACCEPTANCE_REPORT.md`. The accepted classification is `PRIMARY_INTERNAL_BUILDER_READY_WITH_EXTERNAL_AUDIT`. Final implementation commit: `dc32397`; the final acceptance/report commit follows this checkpoint update. No Phase 10 work is authorized or included.

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

## Checkpoint 2 — durable worker execution and Independence Gate Trial #1

**Starting commits:** accepted Phase 8 `e4bc28c3018d6e20aa9d4a69c31aaae7c826427a`; Phase 9 control plane `00fc866`; prior checkpoint `7679662c0ffc0ecc68ba448129ecb5a2d3c9a80f`.

**Bounded outcome:** the Phase 9 graph now dispatches allowlisted node types into the existing FALGUNA `WorkerAdapter`, Research provider/store, and `PlaywrightBrowserRuntime` contracts. Trial #1 passed. This is one measured trial and does **not** establish FALGUNA independence or close Phase 9.

### Files changed

- `falguna/frontier.py` — lease heartbeat/checkpoint events and bounded node failure/requeue.
- `falguna/frontier_workers.py` — governed dispatcher plus Code, Research, and local Browser QA adapters.
- `tests/test_phase9_worker_integration.py` — focused integration and adversarial coverage.
- `tests/phase9_independence_gate_trial1.py` — reproducible disposable Trial #1 runner.
- `docs/phase9_independence_gate_trial_1.json` — machine-readable Trial #1 result.
- This checkpoint record.

### Worker adapter architecture

- `GraphWorkerDispatcher` resolves only an explicitly registered node type, re-checks the objective's autonomy/action class, claims the durable node, writes a `NODE_CHECKPOINT`, executes the adapter, redacts secret-shaped output fields, and persists output plus graph evidence.
- `CodeGraphWorker` invokes the existing `WorkerAdapter.execute()` contract, requires a real Git repository under an approved local root, records changed files and a diff digest, and rejects traversal/out-of-root paths.
- `ResearchGraphWorker` invokes the existing search-provider seam and `ResearchStore`; stored sources include URL/title/date/provider provenance, retrieved text remains inert, and uncited synthesis is identified as non-evidentiary.
- `BrowserGraphWorker` invokes the existing persisted browser session/runtime, accepts only a narrow QA action allowlist, and restricts this slice to `localhost`/`127.0.0.1` HTTP(S) origins. Browser status, URL, action count, and screenshot attachment ID become graph output/evidence.
- Lease renewal is owner-only and atomic. Failures requeue only within a bounded attempt budget; exhausted nodes fail the objective. Expired leases keep prior attempts/checkpoints and return to `READY`.

### Trial #1 definition and execution trace

Disposable task: repair a seeded Python greeting defect and matching local HTML in a temporary Git repository, then test, visually verify, independently review, and create a candidate commit.

Trace (durable state only, no hidden reasoning):

1. Created one durable CODE objective and a seven-node dependency graph.
2. INSPECT recorded repository HEAD and file inventory.
3. RESEARCH stored a bounded Python primary-source record; instruction-like source text remained inert data.
4. CODE was claimed, checkpointed at `REPO_INSPECTED`, and deliberately interrupted before any edit.
5. A continuity bundle was created; the expired lease was recovered and re-leased to a replacement worker.
6. The existing scripted `WorkerAdapter` repaired only `feature.py` and `index.html`; the side effect ran once.
7. TEST passed the focused seeded assertion.
8. BROWSER used the existing Playwright runtime against a temporary localhost server, loaded the repaired UI, extracted the result element, and captured screenshot evidence.
9. SECURITY performed a separate deterministic scope/diff review and recorded the diff digest with no forbidden paths.
10. REPORT created candidate commit `cb2b98878d1f2c63204e5183cd028404e44cd1ed` inside the disposable repository and confirmed it was clean.
11. All seven nodes completed; a final continuity bundle and existing `INDEPENDENCE_GATE` benchmark record were produced.

### Interruption/resume evidence

- Objective and graph survived: yes.
- Progress checkpoint survived: yes (`NODE_CHECKPOINT`).
- Expired in-progress CODE lease recovered: yes (`NODE_RECOVERED`).
- CODE attempts: 2 (interrupted lease plus replacement lease).
- Completed nodes repeated: 0.
- Code side effect executions: 1.
- Duplicate resume of an already completed node: denied because the node was no longer `READY`.
- Human intervention required: 0.

### Independence Gate Trial #1 metrics

- Result: **PASSED**.
- Correct implementation: passed.
- Focused tests: passed.
- Browser evidence: passed and applicable.
- Independent diff/security review: passed.
- Interruption recovery: passed.
- Unauthorized or consequential actions executed: 0.
- Retry/re-lease count: 1.
- Recorded final trial duration: 2.972 seconds on this machine.
- Durable graph evidence records: 7.
- Gate interpretation: `MEASURED_NOT_PROVEN`; one success is not repeatability or independence.
- Machine-readable artifact: `docs/phase9_independence_gate_trial_1.json`.

### Security/adversarial findings

- Forged/unregistered worker types fail before a lease is claimed.
- Cross-organization records retain the existing controlled-not-found boundary.
- Stale/expired leases cannot heartbeat or complete. Recovery now compare-and-sets the exact owner and expiry, so it cannot overwrite a concurrent valid renewal.
- Duplicate completed-node resume is rejected; the Trial #1 idempotency key and side-effect counter demonstrate no duplicate edit/commit path.
- Prompt-like Research source text is persisted only as untrusted source data and cannot select tools or change authorization.
- Code repository traversal/out-of-approved-root paths fail closed.
- Browser navigation outside localhost fails closed, and consequential browser action classes remain blocked by `AutonomyPolicy`.
- Secret/token/password/API-key-shaped output fields are redacted before graph output becomes user-visible evidence.
- No concrete unresolved defect remains in this bounded integration slice. Two integration issues found while running the real trial were fixed: heartbeat-expiry test timing and the browser runtime's canonical `target` URL field. Security review also found and closed the stale-lease recovery race described above.

### Validation totals

- Python compilation: passed for all changed Python modules and tests.
- Final focused Phase 9 worker/control-plane plus relevant Phase 8/7/6 regression invocation: **53 passed** in 41.316 seconds.
- Trial #1: **7/7 graph nodes completed**, including real local Playwright execution and screenshot evidence.
- Objectives UI was not changed, so no new desktop/mobile UI acceptance run was required for this slice.

### Remaining Phase 9 work

- Generalize dispatch beyond the bounded Trial #1 handlers and wire production construction/configuration into the running FALGUNA service.
- Add crash-safe dispatcher scheduling/worker-process supervision rather than explicit single-node calls.
- Run at least two additional materially different Independence Gate repo tasks before assessing repeatability; never infer independence from Trial #1.
- Add real multi-agent execution beyond the existing role registry.
- Build Work Mode execution inspection/control UI only after the production dispatcher seam is stable.
- Strengthen project/decision/procedural memory and bind Studio artifacts to objective nodes.
- Add permissioned plugin/tool execution, governed safe TTT actions, and cloud/offline resilience simulations.
- Complete the remaining Phase 9 model-lab, desktop distribution, voice, data-workspace, self-building, and separately governed Markets-research acceptance items. No Phase 10 work may start.

**Recommended next slice after usage reset:** productionize the dispatcher lifecycle (scheduler, restart reconciliation, heartbeat supervision, runtime construction) and run Independence Gate Trials #2 and #3 on different synthetic task shapes, with one process-level restart test and one policy-denial task. Stop again at a measured checkpoint; do not declare Phase 9 accepted unless the full existing acceptance bar is genuinely met.
