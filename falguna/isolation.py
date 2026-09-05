import os
import platform
import resource
import shutil
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
    resource_limits: str = "cpu, address-space, processes"


class ProcessIsolator:
    """Best-effort host process isolation; Seatbelt is used when available on macOS."""

    def __init__(self, worktree: Path, policy: RunPolicy):
        self.worktree = Path(worktree).resolve()
        self.policy = policy

    def _limit_resources(self):
        requested = [(resource.RLIMIT_CPU, 900)]
        if hasattr(resource, "RLIMIT_AS"):
            requested.append((resource.RLIMIT_AS, self.policy.max_memory_mb * 1024 * 1024))
        if hasattr(resource, "RLIMIT_NPROC"):
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
            if platform.system() == "Darwin" and shutil.which("sandbox-exec"):
                profile = self._seatbelt_profile(home, network_mode)
                argv = ["sandbox-exec", "-p", profile, *argv]
                backend = "macos-seatbelt"
            completed = subprocess.run(argv, cwd=self.worktree, env=safe_env, text=True, capture_output=True, timeout=spec.timeout_seconds, preexec_fn=self._limit_resources)
            evidence = IsolationEvidence(platform.system(), backend, network_mode, f"{self.worktree} and private temp home")
            return completed, evidence
