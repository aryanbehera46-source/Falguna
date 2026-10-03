# TTT / FALGUNA Phase 9 — Final Acceptance Report

**Date:** 2026-10-03  
**Status:** **PHASE 9 — FINAL ACCEPTANCE**  
**Branch:** `phase9/falguna-frontier-independence-v1`  
**Accepted baseline:** Phase 8 `e4bc28c`  
**Phase 9 commits:** `00fc866`, `7679662`, `e156942`, `dc32397`

## Acceptance decision

**Classification: `PRIMARY_INTERNAL_BUILDER_READY_WITH_EXTERNAL_AUDIT`.**

FALGUNA is accepted as the primary environment for governed real internal digital work in the defined Work, Code, Research, Browser and safe TTT-internal task classes. Three materially different Independence Gate trials, a safe TTT operational trial and an operating-system process-death recovery test passed. This does not claim general intelligence, unrestricted autonomy, a trained proprietary frontier base model, public-cloud 24/7 activation, or production-signed desktop installers. External models and human/external review remain valid benchmark, audit and fallback systems.

## Durable production control plane

`falguna.frontier_supervisor.GraphSupervisor` is the supported long-running scheduler. It continuously reconciles expired leases, selects eligible READY nodes, re-checks objective state and per-action policy, dispatches only registered adapters, renews heartbeats during worker execution, records checkpoints/evidence, applies bounded retries, detects process loss through lease expiry and stops safely on SIGINT/SIGTERM. Paused, failed, completed and emergency-stopped objectives are not dispatched.

Supported local launch and status pattern:

```text
PYTHONPATH=. python3 -m falguna.frontier_supervisor \
  --state-dir <falguna-state-directory> \
  --organization <organization-id> \
  --handler-factory <approved-module:factory> \
  --approved-root <approved-repository-root>

PYTHONPATH=. python3 -m falguna.frontier_supervisor \
  --state-dir <falguna-state-directory> \
  --organization <organization-id> \
  --handler-factory <approved-module:factory> \
  --status
```

Handler factories explicitly construct the already-existing governed Code, Research and Browser adapters. No arbitrary worker type is dynamically trusted. The closure campaign includes the reproducible synthetic factory used for process-death evidence; production deployments must supply an approved factory for their configured model/search/browser runtimes and repository roots.

## Process-level restart evidence

The closure campaign launched the dispatcher as a separate operating-system process against a durable SQLite state directory. The worker recorded meaningful pre-side-effect progress, then the dispatcher PID was forcibly killed. After its lease was expired, a new dispatcher process opened the same state and resumed the graph.

- Prior process killed after meaningful work: yes.
- Durable checkpoint survived: yes.
- Attempts/leases: 2.
- Completed nodes repeated: 0.
- Side effects: exactly 1.
- Stale dead-worker completion rejected: yes.
- Audit events: `NODE_CLAIMED`, `NODE_CHECKPOINT`, `NODE_RECOVERED`, replacement `NODE_CLAIMED`, `NODE_COMPLETED`.
- Evidence: `docs/phase9_closure_campaign.json`.

## Independence Gate trials

### Trial #1 — synthetic code repair and browser verification

- Commit: `e156942`.
- Result: passed; `MEASURED_NOT_PROVEN` at the time of the single trial.
- 7/7 nodes completed.
- Code attempts: 2; code side effects: 1.
- Completed nodes repeated: 0.
- Interruption recovery, focused tests, real localhost Playwright QA and independent security/diff review passed.
- Human interventions and unauthorized/consequential actions: 0.
- Focused validation at checkpoint: 53 passed.

### Trial #2 — Research + Browser heavy

- Result: passed.
- Research recorded two source records with provider/date/source provenance; uncited synthesis remained explicitly non-evidentiary.
- Browser opened a synthetic localhost report, clicked its analysis control, extracted the changed user-visible result and captured screenshot evidence: 4 meaningful browser actions.
- Browser and security branches were separate READY dependency paths assigned to distinct worker identities.
- Final evidence-backed recommendation artifact produced.
- Prompt-like source content remained inert; external/consequential actions: 0.

### Trial #3 — mixed multi-agent software task

- Result: passed.
- Six graph nodes covered planning, sourced research, Code implementation, focused tests, independent security/scope review and final aggregation/candidate commit.
- Code worker was interrupted before mutation, recovered on a replacement lease and produced one side effect across two attempts.
- QA and security were independent dependency branches with distinct worker identities.
- Focused seeded test passed; candidate disposable repository was clean.
- Unauthorized actions: 0.

### Trial #4 — safe TTT-internal operation

- Result: passed.
- Read-only governed TTT HQ context was used to create an internal review checklist.
- TTT HQ remained the authoritative system.
- Commercial mutations: 0; approval bypass: none.
- Payment remained denied even at `COMPANY_AUTONOMOUS`.

Across the campaign, graph roles, assigned nodes, dependencies, starts/completions, attempts, durable outputs/evidence and final aggregation are persisted. The branches represent real graph coordination, not cosmetic agent labels.

## Work, Code, Research and Browser product flow

The chat-first default remains unchanged. Persistent Objectives expose Work, Code, Research, Browser, Data, TTT Internal and Markets Research in normal product language. A user can create an objective, create/inspect its dependency graph, see node status/role/policy, inspect recent evidence/approval/event summaries, save a continuity bundle, pause, resume and emergency-stop. State survives reload/session closure. Hidden model reasoning is never exposed.

Code, Research and Browser are real durable node types connected to the existing `WorkerAdapter`, Research provider/store and Playwright runtime contracts. Code is restricted to approved Git roots; Research preserves source provenance and treats retrieved instructions as data; Browser is restricted by action policy and the accepted closure trials use localhost only.

## Memory and continuity

- Objective, graph, events, evidence, checkpoints and approvals survive process/chat/session loss.
- Continuity bundles are versioned and content-addressed by SHA-256.
- Research and model/dataset records preserve provenance.
- Trial #1 and the process-restart campaign demonstrate recovery from persisted state.
- Organization/objective dependency checks and existing project-scoped Memory tests prevent cross-project/organization leakage.

## Model router and AI Lab foundation

- Provider/model abstraction and privacy/capability-aware routing are implemented.
- Local and `FALGUNA_OWNED` assets are first-class registry entries.
- Existing router fallback tests cover safe configured provider fallback; no single external provider is structurally mandatory.
- Candidate → evaluated → approved → retired promotion requires benchmark evidence and a passing evaluation.
- Model assets store capability, constraints, version, ownership and dataset provenance.
- Benchmark runs store suite version, capability dimensions, evidence and external-audit metadata; Independence Gate uses this registry.
- Competitor values are not hardcoded; date/source/provenance fields support later refresh from authoritative research.

Not claimed: a trained frontier-scale proprietary base model. Training, distillation, licensed dataset acquisition, GPU infrastructure and production inference remain the later Frontier Program.

## Autonomy and consequence controls

ASK, ASSIST, EXECUTE_WITH_APPROVAL, AUTONOMOUS_WITHIN_POLICY and COMPANY_AUTONOMOUS were exercised by policy tests. Payments, refunds, payouts, beneficiary changes, production deploy/push/merge, major spending, legal commitments, live outreach, DNS changes and live trading fail closed. Unknown classes fail closed. Recording an approval does not create an executor or bypass action-time checks.

TTT HQ remains the financial/commercial state owner; FALGUNA's Phase 9 seam is read-only. Phase 6 owner/finance controls were not weakened.

## Security/adversarial closure

Focused tests and trial evidence cover forged worker types, privilege/action escalation, stale lease output, duplicate completion/resume, expired-lease races, path traversal/out-of-root repositories, shell/command boundaries through structured argv, prompt injection in research inputs, organization/project isolation, malicious plugin scopes/schemes, unauthorized TTT context, consequential browser actions, secret-shaped output redaction, autonomy bypass and dispatcher restart edges. No unresolved critical Phase 9 defect remains.

## Resilience, cloud and desktop foundations

The durable local architecture now has queue-like graph nodes, resumable workers, leases/heartbeats, retries, process recovery, emergency stop, health/status output, continuity bundles and backup/restore-identifiable state. It is compatible with a future always-on cloud supervisor while the laptop is off; no public-cloud deployment is claimed.

The existing `launcher/Falguna.app` remains the practical macOS local/dev desktop artifact and its plist and launch script validate successfully. The accepted desktop architecture is a thin cross-platform shell over the same local/cloud APIs, with seams for OS file/repository grants, terminal integration, secure credential-store references, cloud task sync/status, crash/session restore and signed update metadata. Windows/Linux build targets and macOS/Windows/Linux production signing/notarization remain activation work; no signed installers are claimed.

## Existing foundation verification

Studio artifacts, digital twin and simulation have persisted Phase 9 schemas linked to organization/objective state and audit boundaries. Existing Markets modules provide market-data adapters, strategy registry, backtests, paper accounts/orders, risk limits and calibration/performance records; live capital remains disabled and segregated from TTT operating cash. Plugin manifests remain scope-bound, sandbox-described, non-secret-forwarding and approval-gated for live external actions.

## Validation record

- Python compilation for changed control-plane/supervisor/trial modules: passed.
- Final focused Phase 9 suite: **18 passed**.
- Closure campaign: process restart + Trials #2, #3 and #4: **4/4 passed** in 4.658 seconds.
- Prior Trial #1 acceptance: **7/7 nodes**, **53 tests passed**.
- Targeted Phase 9 plus touched model, memory, browser, Phase 8, Phase 7, Phase 6 finance/security/concurrency and Trading Lab regression invocation: **381 passed, 3 skipped, 1 environment-sensitive failure** in 345.68 seconds. The failure expected the real Stooq provider to be unreachable; it was reachable and returned `OK`. It is not an application assertion regression and no test was weakened.
- Final Playwright Work/Objectives acceptance: **4 passed** at **1440×960** and **390×844**. Create/plan/checkpoint/reload/pause/resume, visible safety boundaries, no horizontal overflow and no captured console/page errors passed.
- macOS dev artifact: `Info.plist` lint passed; launcher script syntax passed.
- `git diff --check`: passed before commits.

## Known limitations and deferred work

- External audit/model fallback remains advisable for novel or high-risk work.
- General independence outside the defined tested task classes is not claimed.
- Production cloud activation, hosted device identity, offline/cloud conflict resolution and always-on operations are deferred activation work.
- Production desktop signing/notarization, Windows/Linux packaging and update-signing CI are deferred activation work.
- Frontier base-model training and advanced owned-model research remain the Frontier Program.
- Live money movement, customer/provider data, outreach, deploy/push/merge, DNS, trading and legal/company actions remain disabled or human-gated.

No Phase 10 work was started. No push, merge, deploy, DNS change, payment/provider activation, real customer/provider data, outreach, spending, live trade or legal/company change occurred.
