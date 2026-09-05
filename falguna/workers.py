import os
import json
import re
import subprocess
import urllib.error
import urllib.request
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


class StructuredEditWorker(WorkerAdapter):
    """Replaceable worker that applies model-produced full-file edits deterministically."""

    def __init__(self, gateway: ModelGateway, editable_files, timeout_seconds: int = 180, transport=None):
        self.gateway = gateway
        self.editable_files = list(editable_files)
        self.timeout_seconds = timeout_seconds
        self.transport = transport or self._request

    def execute(self, worktree: Path, requirement: str, run_id: str) -> WorkerResult:
        config = self.gateway.configuration()
        try:
            files = {}
            root = worktree.resolve()
            for relative in self.editable_files:
                target = (root / relative).resolve()
                if target != root and root not in target.parents:
                    raise ValueError(f"editable path escapes worktree: {relative}")
                files[relative] = target.read_text(encoding="utf-8")
            payload = {
                "model": config["model"],
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You are a bounded repository editor. Return minimal exact old-to-new text patches. Each old "
                            "snippet must occur exactly once in its file. Preserve all unrelated text byte-for-byte. "
                            "Do not invent files, weaken tests, or include markdown."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps({"objective": requirement, "editable_files": files}, ensure_ascii=False),
                    },
                ],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "bounded_file_edits",
                        "strict": True,
                        "schema": {
                            "type": "object",
                            "properties": {
                                "summary": {"type": "string"},
                                "patches": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "path": {"type": "string"},
                                            "old": {"type": "string"},
                                            "new": {"type": "string"},
                                        },
                                        "required": ["path", "old", "new"],
                                        "additionalProperties": False,
                                    },
                                },
                            },
                            "required": ["summary", "patches"],
                            "additionalProperties": False,
                        },
                    },
                },
            }
            decoded = self.transport(config, payload, self.timeout_seconds)
            message = decoded["choices"][0]["message"]
            if message.get("refusal"):
                raise ValueError("model refused bounded edit")
            response = json.loads(message["content"])
            allowed = set(self.editable_files)
            patches = response["patches"]
            if not patches:
                raise ValueError("model returned no patches")
            changed = 0
            contents = dict(files)
            for patch in patches:
                relative = patch["path"]
                if relative not in allowed:
                    raise ValueError(f"unapproved edit path: {relative}")
                target = (root / relative).resolve()
                if target != root and root not in target.parents:
                    raise ValueError(f"edit path escapes worktree: {relative}")
                old = patch["old"]
                new = patch["new"]
                if not old or old == new or contents[relative].count(old) != 1:
                    raise ValueError(f"patch old text must match exactly once and change content: {relative}")
                contents[relative] = contents[relative].replace(old, new, 1)
            for relative, content in contents.items():
                if content != files[relative]:
                    (root / relative).write_text(content, encoding="utf-8")
                    changed += 1
            if not changed:
                raise ValueError("model edits made no changes")
            usage = decoded.get("usage", {})
            prompt_tokens = int(usage.get("prompt_tokens", 0))
            completion_tokens = int(usage.get("completion_tokens", 0))
            cached_tokens = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0))
            cost = max(0, prompt_tokens - cached_tokens) * 0.75e-6 + cached_tokens * 0.075e-6 + completion_tokens * 4.50e-6
            call = {
                "provider": "openai-compatible",
                "model": config["model"],
                "purpose": "implementation",
                "input_tokens": prompt_tokens,
                "output_tokens": completion_tokens,
                "cost_usd": cost,
                "metadata": {"adapter": "structured-edit", "cached_input_tokens": cached_tokens},
            }
            return WorkerResult(True, response["summary"], 0, [call], cost)
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError, OSError, urllib.error.URLError) as exc:
            return WorkerResult(False, f"Structured edit failed: {exc}", 65)

    @staticmethod
    def _request(config, payload, timeout_seconds):
        api_key_file = config.get("api_key_file")
        localhost_proxy = config["base_url"].startswith("http://127.0.0.1:") or config["base_url"].startswith("http://localhost:")
        if localhost_proxy:
            key = "falguna-local-proxy-token"
        else:
            key_path = Path(api_key_file or "").resolve()
            if not key_path.is_file() or key_path.stat().st_mode & 0o077:
                raise ValueError("API key file missing or permissions are not 0600")
            key = key_path.read_text(encoding="utf-8").strip()
        request = urllib.request.Request(
            config["base_url"].rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return json.loads(response.read())


class ScriptedWorker(WorkerAdapter):
    """Offline acceptance-test worker; never represents model autonomy."""

    def __init__(self, callback):
        self.callback = callback

    def execute(self, worktree: Path, requirement: str, run_id: str) -> WorkerResult:
        return self.callback(worktree, requirement, run_id)
