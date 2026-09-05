import os
import subprocess
from pathlib import Path
from typing import Dict, Optional

from .models import CommandSpec
from .policy import PermissionEngine, PolicyViolation


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

    def run(self, spec: CommandSpec, env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
        self.permissions.require_command(spec)
        safe_env = {"PATH": os.environ.get("PATH", ""), "HOME": str(self.permissions.repo_root / ".falguna-home"), "LANG": "C.UTF-8"}
        if env:
            safe_env.update({key: value for key, value in env.items() if key in {"CI", "NODE_ENV", "PORT"}})
        return subprocess.run(spec.argv, cwd=self.permissions.repo_root, env=safe_env, text=True, capture_output=True, timeout=spec.timeout_seconds)


class BrowserCapability:
    """Localhost-only Playwright command wrapper."""

    def __init__(self, terminal: TerminalCapability):
        self.terminal = terminal

    def verify(self, command: CommandSpec, base_url: str) -> subprocess.CompletedProcess:
        if not (base_url.startswith("http://127.0.0.1:") or base_url.startswith("http://localhost:")):
            raise PolicyViolation("browser verification is localhost-only")
        return self.terminal.run(command, {"CI": "1"})

