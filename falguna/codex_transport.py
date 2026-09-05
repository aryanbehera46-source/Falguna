import json
import os
import subprocess
import tempfile
from pathlib import Path


class CodexCliJSONTransport:
    """Strict-JSON transport using an authenticated, read-only local Codex runtime."""

    def __init__(self, executable: Path, codex_home: Path, timeout_seconds: int = 180, runner=None):
        self.executable = Path(executable).resolve()
        self.codex_home = Path(codex_home).resolve()
        self.timeout_seconds = timeout_seconds
        self.runner = runner or subprocess.run

    def __call__(self, config, payload, timeout_seconds):
        schema = payload["response_format"]["json_schema"]["schema"]
        prompt = "\n\n".join(f"{item['role'].upper()}:\n{item['content']}" for item in payload["messages"])
        with tempfile.TemporaryDirectory(prefix="falguna-codex-transport-") as directory:
            root = Path(directory)
            schema_path = root / "schema.json"
            output_path = root / "result.json"
            schema_path.write_text(json.dumps(schema), encoding="utf-8")
            env = {
                "PATH": os.environ.get("PATH", ""),
                "HOME": str(root),
                "CODEX_HOME": str(self.codex_home),
                "LANG": "C.UTF-8",
            }
            argv = [
                str(self.executable), "exec", "--ephemeral", "--ignore-user-config", "--ignore-rules",
                "--skip-git-repo-check", "--sandbox", "read-only", "--model", config["model"],
                "--output-schema", str(schema_path), "--output-last-message", str(output_path),
                "--json", "-C", str(root), "-",
            ]
            completed = self.runner(argv, input=prompt, env=env, text=True, capture_output=True, timeout=min(timeout_seconds, self.timeout_seconds))
            if completed.returncode != 0:
                raise OSError(f"Codex transport failed with exit {completed.returncode}: {completed.stderr[-1000:]}")
            content = output_path.read_text(encoding="utf-8")
            json.loads(content)
            usage = self._usage(completed.stdout)
            return {
                "choices": [{"message": {"content": content}}],
                "usage": usage,
                "_falguna_provider": "codex-cli-subscription",
                "_falguna_cost_usd": 0.0,
                "_falguna_metadata": {"adapter": "codex-cli-json", "billing": "subscription-no-additional-cash-cost", "sandbox": "read-only", "ephemeral": True},
            }

    @staticmethod
    def _usage(stdout: str) -> dict:
        latest = {}
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            usage = event.get("usage") or (event.get("data") or {}).get("usage")
            if isinstance(usage, dict):
                latest = usage
        return {
            "prompt_tokens": int(latest.get("input_tokens", latest.get("prompt_tokens", 0)) or 0),
            "completion_tokens": int(latest.get("output_tokens", latest.get("completion_tokens", 0)) or 0),
            "prompt_tokens_details": {"cached_tokens": int(latest.get("cached_input_tokens", 0) or 0)},
        }
