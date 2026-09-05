import os
import re
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path

from .gateway import ModelGateway
from .models import WorkerResult


class WorkerAdapter(ABC):
    @abstractmethod
    def execute(self, worktree: Path, requirement: str, run_id: str) -> WorkerResult:
        raise NotImplementedError


class AiderWorker(WorkerAdapter):
    """Wrapped, replaceable Aider process. It never receives host secrets by default."""

    def __init__(self, executable: Path, gateway: ModelGateway, timeout_seconds: int = 600, editable_files=None, architect: bool = False):
        self.executable = Path(executable)
        self.gateway = gateway
        self.timeout_seconds = timeout_seconds
        self.editable_files = list(editable_files or [])
        self.architect = architect

    def execute(self, worktree: Path, requirement: str, run_id: str) -> WorkerResult:
        config = self.gateway.configuration()
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(worktree / ".falguna-home"), "LANG": "C.UTF-8"}
        api_key_file = config.get("api_key_file")
        localhost_proxy = config["base_url"].startswith("http://127.0.0.1:") or config["base_url"].startswith("http://localhost:")
        if localhost_proxy:
            env["OPENAI_API_KEY"] = "falguna-local-proxy-token"
        elif api_key_file:
            key_path = Path(api_key_file).resolve()
            if not key_path.is_file() or key_path.stat().st_mode & 0o077:
                return WorkerResult(False, "API key file missing or permissions are not 0600", 78)
            env["OPENAI_API_KEY"] = key_path.read_text().strip()
        argv = [str(self.executable), "--yes-always", "--no-auto-commits", "--no-gitignore", "--no-stream", "--no-check-update", "--no-analytics", "--edit-format", "diff", "--model", config["model"], "--openai-api-base", config["base_url"]]
        for path in self.editable_files:
            argv.extend(["--file", path])
        if self.architect:
            argv.append("--architect")
        argv.extend(["--message", requirement])
        try:
            result = subprocess.run(argv, cwd=worktree, env=env, text=True, capture_output=True, timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired:
            return WorkerResult(False, "Aider timed out", 124)
        summary = (result.stdout + "\n" + result.stderr)[-12000:]
        token_match = re.search(r"Tokens:\s*([\d.]+)k? sent,\s*([\d.]+)k? received", summary)
        cost_match = re.search(r"Cost:\s*\$([\d.]+) message", summary)
        def tokens(value):
            return int(float(value) * (1000 if "." in value else 1))
        call = {"provider": "openai-compatible", "model": config["model"], "purpose": "implementation", "input_tokens": tokens(token_match.group(1)) if token_match else 0, "output_tokens": tokens(token_match.group(2)) if token_match else 0, "cost_usd": float(cost_match.group(1)) if cost_match else 0.0, "metadata": {"adapter": "aider"}}
        return WorkerResult(result.returncode == 0, summary, result.returncode, [call], call["cost_usd"])


class ScriptedWorker(WorkerAdapter):
    """Offline acceptance-test worker; never represents model autonomy."""

    def __init__(self, callback):
        self.callback = callback

    def execute(self, worktree: Path, requirement: str, run_id: str) -> WorkerResult:
        return self.callback(worktree, requirement, run_id)
