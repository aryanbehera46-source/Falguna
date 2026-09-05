# Phase 1 v0.1 frozen scope

## Objective

Reach the earliest safe self-building threshold: a bounded improvement to this repository is implemented in an isolated worktree, resumed after forced interruption, verified, independently reviewed, and returned as a `DONE_CANDIDATE` for Aryan's merge decision.

## In scope

- Durable Mission, Requirement, Task, TaskStep, Run, Approval, ModelCall, CostEvent, Artifact and Checkpoint state
- Replaceable ModelGateway and worker adapters; wrapped Aider first
- Git worktree isolation; repository-root filesystem boundary
- Command allowlist, no shell evaluation, localhost-only browser checks
- Bounded retry/recovery and durable checkpoint/resume
- Native tests/build/typecheck/lint plus optional Playwright command
- Definition-of-Done gates, independent reviewer path, evidence manifest
- Hash-chained append-only audit log
- `DONE_CANDIDATE` and human-only proposed merge approval

## Out of scope / prohibited

Company departments, marketing, sales, finance, trading, support, social automation, marketplace actions, production deployment or DB writes, raw production secrets, arbitrary network/spending/banking/DNS/client/legal actions, automatic protected-branch merge, unrestricted self-modification, and any worker change to security/approval/audit/verification policy.

## Acceptance gate

Three consecutive bounded self-repository tasks must pass all gates. One must be forcibly interrupted and resumed. Median intervention must be H0-H1, with no H3/H4, unauthorized action, policy weakening, automatic merge, or spend outside the approved envelope. Until then, self-building is **not claimed**.

