import os
import platform
import resource
import shutil
import signal
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from .models import CommandSpec, RunPolicy


@dataclass(frozen=True)
class IsolationEvidence:
    platform: str
    backend: str
    network_mode: str
    filesystem_write_scope: str
    environment_mode: str = "allowlisted"
    resource_limits: str = "cpu and address-space"


class ProcessIsolator:
    """Best-effort host process isolation; Seatbelt is used when available on macOS."""

    def __init__(self, worktree: Path, policy: RunPolicy):
        self.worktree = Path(worktree).resolve()
        self.policy = policy

    def _limit_resources(self):
        requested = [(resource.RLIMIT_CPU, 900)]
        if hasattr(resource, "RLIMIT_AS"):
            requested.append((resource.RLIMIT_AS, self.policy.max_memory_mb * 1024 * 1024))
        # Darwin counts RLIMIT_NPROC across the login user, so a low child limit can
        # prevent every subprocess from starting instead of bounding this process tree.
        if platform.system() != "Darwin" and hasattr(resource, "RLIMIT_NPROC"):
            requested.append((resource.RLIMIT_NPROC, self.policy.max_processes))
        for kind, value in requested:
            try:
                _, hard = resource.getrlimit(kind)
                limit = value if hard == resource.RLIM_INFINITY else min(value, hard)
                resource.setrlimit(kind, (limit, hard))
            except (OSError, ValueError):
                # Seatbelt and timeout remain active when a host rejects a specific rlimit.
                pass

    def _seatbelt_profile(self, command_home: Path, network_mode: str) -> str:
        roots = [self.worktree, command_home]
        write_rules = "\n".join(f'(allow file-write* (subpath "{path}"))' for path in roots)
        network = ""
        if network_mode == "loopback":
            network = '(allow network-bind (local ip "localhost:*"))\n(allow network-inbound (local ip "localhost:*"))\n(allow network-outbound (remote ip "localhost:*"))'
        return f'''(version 1)
(deny default)
(allow process*)
(allow sysctl-read)
(allow mach-lookup)
(allow file-read*)
(allow file-write* (literal "/dev/null"))
{write_rules}
{network}
'''

    def run(self, spec: CommandSpec, env: Dict[str, str], network_mode: str = "deny") -> tuple:
        with tempfile.TemporaryDirectory(prefix="falguna-command-home-") as temp_home:
            home = Path(temp_home).resolve()
            safe_env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "TMPDIR": str(home), "LANG": "C.UTF-8"}
            safe_env.update(env)
            argv = list(spec.argv)
            backend = "resource-limits-only"
            if platform.system() == "Darwin" and shutil.which("sandbox-exec") and network_mode == "deny":
                profile = self._seatbelt_profile(home, network_mode)
                argv = ["sandbox-exec", "-p", profile, *argv]
                backend = "macos-seatbelt"
            elif platform.system() == "Darwin" and network_mode == "loopback":
                backend = "application-loopback-policy+resource-limits"
            process = subprocess.Popen(argv, cwd=self.worktree, env=safe_env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, preexec_fn=self._limit_resources, start_new_session=True)
            try:
                stdout, stderr = process.communicate(timeout=spec.timeout_seconds)
                completed = subprocess.CompletedProcess(spec.argv, process.returncode, stdout, stderr)
            except subprocess.TimeoutExpired as exc:
                os.killpg(process.pid, signal.SIGKILL)
                stdout, stderr = process.communicate()
                completed = subprocess.CompletedProcess(spec.argv, 124, (exc.stdout or "") + (stdout or ""), (exc.stderr or "") + (stderr or "") + "\ncommand timed out and process group was contained")
            write_scope = f"{self.worktree} and private temp home" if backend == "macos-seatbelt" else "application policy only for browser subprocesses"
            limits = "cpu and address-space; process count is not safely per-tree on Darwin" if platform.system() == "Darwin" else "cpu, address-space, and process count"
            evidence = IsolationEvidence(platform.system(), backend, network_mode, write_scope, resource_limits=limits)
            return completed, evidence

    def harmless_prohibited_write_probe(self) -> dict:
        marker = Path("/tmp") / f"falguna-isolation-probe-{os.getpid()}"
        if marker.exists():
            marker.unlink()
        completed, evidence = self.run(CommandSpec(["/usr/bin/touch", str(marker)], 10, "prohibited-write-probe"), {}, "deny")
        escaped = marker.exists()
        if escaped:
            marker.unlink()
        return {
            "action": "write outside worktree",
            "resource": str(marker),
            "blocked": completed.returncode != 0 and not escaped,
            "exit_code": completed.returncode,
            "backend": evidence.backend,
            "stderr": completed.stderr[-2000:],
        }
