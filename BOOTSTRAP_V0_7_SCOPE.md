# Falguna Bootstrap v0.7 — one-click local launcher

## Frozen acceptance checklist

- Double-clicking the native macOS `Falguna.app` bundle starts the existing web app without Terminal interaction.
- The launcher binds explicitly to `127.0.0.1:8765`, waits for the Falguna identity endpoint, and opens the default browser only after readiness.
- If that Falguna endpoint is already ready, another click opens it without spawning a duplicate server.
- If port 8765 belongs to another process, the launcher fails safely instead of stopping or replacing it.
- `Stop Falguna.app` terminates only the launcher-recorded process after verifying that it is a local Falguna web command; it never force-kills a process.
- Launcher state contains only a PID and operational log under the user's Application Support directory. No credentials are copied or stored.
- The fallback `python3 -m falguna --root . web` command and the complete v0.6 browser flow continue to work.
- Native regression remains green with zero security violations and zero protected-main merges.

No public binding, backend refactor, automatic merge, deployment, credential storage, new framework, cosmetic branding project, broader Company OS function, or post-v0.7 stage is in scope.
