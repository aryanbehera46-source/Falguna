# Falguna Bootstrap v0.1

The earliest safe self-building control plane. It runs one bounded repository task at a time in an isolated Git worktree, with deny-by-default permissions, checkpoints, verification, independent review, evidence, cost records, and a human-only proposed-merge gate.

It is not the Company OS and never merges or deploys automatically.

## Local commands

For normal use, start the local app from this folder and open the printed address:

```sh
python3 -m falguna --root . web
```

The default address is `http://127.0.0.1:8765`. The server accepts only localhost binding. Project and verification choices come from the approved `falguna/project_profiles.json` file; the browser cannot supply an arbitrary repository or verification command.

The command surface remains available for diagnostics:

```sh
python3 -m unittest discover -s tests -v
python3 -m falguna init
python3 -m falguna create-mission --title "Example" --requirement "Bounded change"
python3 -m falguna run --objective "Bounded change" --editable falguna/feature.py --test "python3 -m unittest discover -s tests -v"
python3 -m falguna status
python3 -m falguna status --run RUN_ID
python3 -m falguna summary --run RUN_ID
python3 -m falguna decide --run RUN_ID --action request-changes --actor "Aryan" --reason "Add edge-case coverage"
```

The operational view hides benchmark internals and reports milestones, actionable failure classification, requirement/test/browser/review evidence, changed files, cost, risk, unresolved issues, and the pending human merge decision. `decide` accepts `approve`, `reject`, or `request-changes`; it records the human decision only. Falguna still contains no protected-main merge implementation.

SQLite is the durable local development store because PostgreSQL is not installed on the current Intel Mac. `schema/postgres.sql` is the canonical promotion schema and storage access stays behind `StateStore`.

## Bootstrap v0.2 hardening boundaries

Browser verification is discovered from an explicit localhost URL and a project Playwright manifest/configuration. The selected command, configuration source, standard browser-cache source, output, and isolation mode are recorded as evidence; repository-specific browser executable paths are not accepted.

Independent review is replaceable and fail-closed. A semantic reviewer must return concise structured verdicts for requirement satisfaction, scope, regression evidence, and unresolved uncertainty. Hidden reasoning is neither requested nor persisted.

On this Intel Mac, native child commands run under the built-in Seatbelt sandbox with writes limited to the worktree/private temporary home and network denied, plus a scrubbed environment, timeouts, and supported resource limits. Chromium cannot reliably start under that deprecated Seatbelt interface, so browser verification uses an application-enforced localhost target plus secret scrubbing, process-group timeout containment, and CPU/address-space limits. macOS does not provide a safe per-process-tree `RLIMIT_NPROC`, and this bootstrap does not claim VM/container-grade kernel isolation. Synthetic local work remains the safety boundary.

## Bootstrap v0.3 reliability boundaries

Browser preflight ties the local Playwright CLI to the committed manifest and lockfile, validates the exact Chromium revision in the standard cache, and records whether an offline cached install was needed. Browser verification carries an active Playwright request allowlist: loopback requests continue and a harmless external request is aborted and evidenced. This is application/runtime enforcement, not a macOS kernel network sandbox.

Independent reviewers can be calibrated through the same replaceable adapter against labeled correct, incomplete, and unsafe candidates. Calibration persists structured verdicts, evidence, blocking findings, uncertainty, confusion-matrix counts, usage, and cost—never hidden reasoning.

Where a metered API credential is unavailable, the optional `CodexCliJSONTransport` can use the already authenticated local Codex runtime in ephemeral read-only mode. It preserves the structured worker/reviewer contracts and records subscription-backed calls with zero additional cash cost; it does not copy account credentials into candidate worktrees.
