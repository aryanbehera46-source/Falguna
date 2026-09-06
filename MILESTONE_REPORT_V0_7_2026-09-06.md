# FALGUNA v0.7 ONE-CLICK LAUNCHER COMPLETE

Date: 2026-09-06 (Asia/Kolkata)

## Decision

Bootstrap v0.7 passed. Aryan can start the existing local Falguna web app by double-clicking `launcher/Falguna.app`, without opening Terminal. The launcher is a minimal native macOS app bundle around the unchanged v0.6 server command and control plane.

## Open and stop

- Open: double-click `launcher/Falguna.app`. It confirms the Falguna identity endpoint at `http://127.0.0.1:8765/api/config`, waits for readiness when starting, then opens the default browser to `http://127.0.0.1:8765`.
- Reopen: double-clicking again detects the ready Falguna endpoint, opens it, and does not create a second listener.
- Stop: double-click `launcher/Stop Falguna.app`. It reads the launcher PID, verifies that the process command is the local Falguna web server, requests a normal `TERM` shutdown, and removes the PID file after exit. It does not force-kill.
- Fallback: from the repository, `python3 -m falguna --root . web` remains supported. A manually started fallback server must be stopped from the same terminal; the stop app intentionally controls only launcher-owned processes.

Keep both app bundles in the repository's `launcher` directory. Runtime state is outside Git under `~/Library/Application Support/Falguna` and contains only `server.pid` while running plus `server.log`; no credentials are stored by the launcher.

## Acceptance evidence

- Host: current Intel Mac (`x86_64`), macOS 15.7.9, system Python 3.9.6.
- Cold one-click launch: passed through macOS `open launcher/Falguna.app`; the readiness endpoint returned the expected Falguna product configuration.
- Local binding: exactly one listener, PID `23449`, on `127.0.0.1:8765`; no public interface binding.
- Browser readiness path: passed; the served page contained Falguna Engineering, Run Mission, and Approve Merge controls.
- Re-click: passed; PID remained `23449` and listener count remained one.
- Clean stop: passed through macOS `open launcher/Stop Falguna.app`; the process exited, PID file was removed, and port 8765 closed.
- Fallback command: passed; it served the unchanged v0.6 browser path on loopback and stopped normally.
- Native verification: 27/27 tests passed, including the new launcher boundary test.
- Bundle validation: both property lists passed macOS validation; both launcher scripts passed shell syntax validation.
- Repository implementation commit: `b5c9338`.
- Security violations / protected-main merges: `0 / 0`.
- No mission, candidate worktree, approval decision, merge, deployment, or external network path was created for this launcher acceptance.

## Intervention and cost

- Intervention: `H0`; no repair loop, candidate-solution intervention, infrastructure workaround, or approval action was required.
- Additional cash cost: `$0.00`; launcher implementation and acceptance used no model/API mission and the reset credit was untouched.
- New runtime dependencies: none.

## Preserved boundaries

The launcher explicitly invokes `--host 127.0.0.1 --port 8765`. If the port is occupied by anything other than the expected Falguna identity response, startup stops with a local alert. The launcher does not alter Seatbelt containment, Playwright request restrictions, secret scrubbing, audit/evidence flows, cost caps, human approval, protected branches, merge behavior, or deployment behavior.

## Known limitations

The app bundles are lightweight local launchers, not signed/distributed installers, and must remain in the repository's `launcher` directory to find the current checkout. macOS may show its normal first-open confirmation for a local unsigned app. The stop app intentionally cannot stop a server started manually or an unrelated process using port 8765. Logs remain local until Aryan removes them. Falguna remains restricted to bounded trusted local work and does not provide VM-grade isolation, hostile-workload safety, public hosting, production-secret handling, automatic merging, or deployment.

No later stage has been started.
