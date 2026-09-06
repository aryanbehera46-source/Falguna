import os
import subprocess
from pathlib import Path
from typing import Dict, Optional

from .models import CommandSpec
from .policy import PermissionEngine, PolicyViolation
from .isolation import ProcessIsolator


class FileCapability:
    def __init__(self, permissions: PermissionEngine):
        self.permissions = permissions

    def read_text(self, path: str) -> str:
        return self.permissions.resolve(Path(path)).read_text()

    def write_text(self, path: str, content: str) -> None:
        target = self.permissions.require_write(Path(path))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)


class TerminalCapability:
    def __init__(self, permissions: PermissionEngine):
        self.permissions = permissions
        self.last_isolation_evidence = None

    def run(self, spec: CommandSpec, env: Optional[Dict[str, str]] = None, network_mode: str = "deny", kernel_sandbox: bool = True) -> subprocess.CompletedProcess:
        self.permissions.require_command(spec)
        safe = {}
        if env:
            safe.update({key: value for key, value in env.items() if key in {"CI", "NODE_ENV", "NODE_PATH", "PORT", "PLAYWRIGHT_BROWSERS_PATH"}})
        completed, evidence = ProcessIsolator(self.permissions.repo_root, self.permissions.policy).run(spec, safe, network_mode, kernel_sandbox)
        self.last_isolation_evidence = evidence
        return completed


class BrowserCapability:
    """Localhost-only Playwright command wrapper."""

    def __init__(self, terminal: TerminalCapability):
        self.terminal = terminal

    def verify(self, command: CommandSpec, base_url: str, browsers_path: Optional[str] = None) -> subprocess.CompletedProcess:
        if not (base_url.startswith("http://127.0.0.1:") or base_url.startswith("http://localhost:")):
            raise PolicyViolation("browser verification is localhost-only")
        env = {"CI": "1"}
        if browsers_path:
            env["PLAYWRIGHT_BROWSERS_PATH"] = browsers_path
        return self.terminal.run(command, env, network_mode="loopback", kernel_sandbox=False)
