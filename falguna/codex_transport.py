import json
import os
import subprocess
import tempfile
from pathlib import Path

DEFAULT_CODEX_MODEL = "gpt-5.6-luna"
SUPPORTED_CODEX_MODELS = frozenset({"gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol", "gpt-6-astra", "gpt-5.5"})
FALLBACK_CODEX_MODELS = (DEFAULT_CODEX_MODEL, "gpt-5.6-terra", "gpt-5.6-sol")


class ModelUnsupportedError(OSError):
    pass


class CodexCliJSONTransport:
    """Strict-JSON transport using an authenticated, read-only local Codex runtime."""

    uses_workspace_context = True

    def __init__(self, executable: Path, codex_home: Path, timeout_seconds: int = 180, runner=None):
        self.executable = Path(executable).resolve()
        self.codex_home = Path(codex_home).resolve()
        self.timeout_seconds = timeout_seconds
        self.runner = runner or subprocess.run
        self._compatible_models = set()

    def preflight(self, model: str) -> None:
        if model in self._compatible_models:
            return
        if model not in SUPPORTED_CODEX_MODELS:
            raise ModelUnsupportedError(f"MODEL_UNSUPPORTED: authenticated Codex runtime does not support {model}")
        self._compatible_models.add(model)

    def __call__(self, config, payload, timeout_seconds):
        self.preflight(config["model"])
        schema = payload["response_format"]["json_schema"]["schema"]
        prompt = "\n\n".join(f"{item['role'].upper()}:\n{item['content']}" for item in payload["messages"])
        with tempfile.TemporaryDirectory(prefix="falguna-codex-transport-") as directory:
            root = Path(directory)
            context_root = root / "context"
            context_root.mkdir()
            if config.get("_falguna_worktree"):
                worktree = Path(config["_falguna_worktree"]).resolve()
                if not worktree.is_dir():
                    raise ValueError("Codex transport isolated worktree does not exist")
                for relative in config.get("_falguna_editable_files", []):
                    source = (worktree / relative).resolve()
                    if source == worktree or worktree not in source.parents or not source.is_file():
                        raise ValueError(f"Codex transport context path is not an approved file: {relative}")
                    target = context_root / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    context = config.get("_falguna_context_files", {}).get(relative)
                    target.write_text(context, encoding="utf-8") if context is not None else target.write_bytes(source.read_bytes())
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
                "--json", "-C", str(context_root), "-",
            ]
            try:
                completed = self.runner(argv, input=prompt, env=env, text=True, capture_output=True, timeout=min(timeout_seconds, self.timeout_seconds))
            except subprocess.TimeoutExpired as exc:
                stdout = (exc.stdout or "")[-4000:]
                stderr = (exc.stderr or "")[-4000:]
                raise OSError(f"TRANSPORT_FAILURE: Codex transport timed out after {exc.timeout}s: stdout={stdout!r} stderr={stderr!r}") from exc
            if completed.returncode != 0:
                stdout = (completed.stdout or "")[-4000:]
                stderr = (completed.stderr or "")[-4000:]
                detail = f"stdout={stdout!r} stderr={stderr!r}"
                if "model is not supported" in (stdout + stderr).lower():
                    raise ModelUnsupportedError(f"MODEL_UNSUPPORTED: {detail}")
                raise OSError(f"TRANSPORT_FAILURE: Codex transport exit {completed.returncode}: {detail}")
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


class ResilientCodexTransport:
    """Session-cached supported-model routing with bounded unsupported fallback."""

    uses_workspace_context = True

    def __init__(self, transport: CodexCliJSONTransport, candidates=FALLBACK_CODEX_MODELS):
        self.transport = transport
        self.candidates = tuple(model for model in candidates if model in SUPPORTED_CODEX_MODELS)
        self.compatibility = {}

    def __call__(self, config, payload, timeout_seconds):
        requested = config.get("model", DEFAULT_CODEX_MODEL)
        candidates = [requested] + [model for model in self.candidates if model != requested]
        errors = []
        for model in candidates:
            if self.compatibility.get(model) is False:
                continue
            cached = self.compatibility.get(model) is True
            routed = dict(config, model=model)
            try:
                result = self.transport(routed, payload, timeout_seconds)
                self.compatibility[model] = True
                result.setdefault("_falguna_metadata", {})["requested_model"] = requested
                result["_falguna_metadata"]["routed_model"] = model
                result["_falguna_metadata"]["preflight_cache"] = "HIT" if cached else "MISS"
                return result
            except ModelUnsupportedError as exc:
                self.compatibility[model] = False
                errors.append(str(exc))
        raise ModelUnsupportedError("MODEL_UNSUPPORTED: no configured authenticated model is compatible; " + " | ".join(errors))
