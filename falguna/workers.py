import os
import json
import re
import subprocess
import urllib.error
import urllib.request
import hashlib
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from .gateway import ModelGateway
from .models import WorkerResult


class PatchTargetError(ValueError):
    pass


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

    def __init__(self, gateway: ModelGateway, editable_files, timeout_seconds: int = 180, transport=None, max_replans: int = 2, max_diff_chars: int = 120000, checkpoint=None, always_excerpt_large_files: bool = False, excerpt_max_chars: int = 12000, max_tokens: Optional[int] = None):
        self.gateway = gateway
        self.editable_files = list(editable_files)
        self.timeout_seconds = timeout_seconds
        self.transport = transport or self._request
        self.max_replans = max_replans
        self.max_diff_chars = max_diff_chars
        self.checkpoint = checkpoint
        # Opt-in output token cap (default None preserves every existing caller's
        # behavior unchanged -- no cap was ever sent before this option existed).
        # Real-world evidence (Sprint V1, Milestone 1, Royal Table retrial): a
        # CPU-only 3B local model, given a strict JSON-schema-constrained bounded-
        # edit request with no max_tokens, was observed generating past 1850
        # tokens without terminating its own response, consuming its entire
        # 300s per-call budget and being cancelled by the server (500 after
        # 5m0s) with no output at all -- a slow, silent, budget-exhausting
        # failure mode distinct from PATCH_NOOP/PATCH_STALE/SCOPE_EXPANSION,
        # which at least produce a decodable response the replan loop can act
        # on. A small, well-reasoned cap sized to the expected output (a short
        # summary plus 1-3 minimal old/new text patches) forces the request to
        # either finish promptly or fail fast as a normal parse/URL error the
        # existing except clause and (for replan-eligible errors) the
        # PatchTargetError feedback loop already handle -- it never changes
        # what is written to disk, only how long a single model call is
        # allowed to keep generating before giving up.
        self.max_tokens = max_tokens
        # Opt-in only (default False preserves every existing caller's behavior
        # unchanged). A plain chat-completions transport (no `uses_workspace_context`
        # -- i.e. no real filesystem access for the model) currently embeds a
        # file's FULL raw content in the request no matter how large it is.
        # For a slow CPU-only local model this is a real reliability problem,
        # not just a cost one: a real ~60KB production file was observed to
        # push a single structured-edit call's prompt processing past a 280s+
        # timeout with zero output, and past a 300s budget with an empty
        # PATCH_NOOP response. Setting this excerpts each oversized file around
        # the requirement's own terms (the same `_context_excerpt` helper this
        # class already uses for workspace-context replans), so the model sees
        # a real, contiguous, relevant slice instead of the whole file -- the
        # excerpt only shrinks what the model *reads*; `_apply_patches` still
        # matches and writes against the full on-disk file, so this cannot by
        # itself cause a wrong-location edit.
        self.always_excerpt_large_files = always_excerpt_large_files
        self.excerpt_max_chars = excerpt_max_chars

    def execute(self, worktree: Path, requirement: str, run_id: str) -> WorkerResult:
        config = dict(self.gateway.configuration())
        config["_falguna_worktree"] = str(worktree.resolve())
        config["_falguna_editable_files"] = list(self.editable_files)
        calls = []
        total_cost = 0.0
        try:
            files = {}
            root = worktree.resolve()
            for relative in self.editable_files:
                target = (root / relative).resolve()
                if target != root and root not in target.parents:
                    raise ValueError(f"editable path escapes worktree: {relative}")
                files[relative] = target.read_text(encoding="utf-8")
            workspace_context = getattr(self.transport, "uses_workspace_context", False)
            if workspace_context:
                model_files = list(files)
                config["_falguna_context_files"] = {
                    path: self._context_excerpt(content, requirement) for path, content in files.items()
                }
            elif self.always_excerpt_large_files:
                model_files = {
                    path: (self._context_excerpt(content, requirement, max_chars=self.excerpt_max_chars)
                           if len(content) > self.excerpt_max_chars else content)
                    for path, content in files.items()
                }
            else:
                model_files = files
            payload = {
                "model": config["model"],
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You are a bounded repository editor. Return minimal exact old-to-new text patches. Each old "
                            "snippet must occur exactly once in its file. Preserve all unrelated text byte-for-byte. "
                            "The 'old' field of every patch MUST be copied character-for-character from the file content "
                            "shown to you -- same characters, same line breaks, same indentation, same quote characters. "
                            "Do not retype, reformat, or reindent it from memory: locate the exact snippet in the given "
                            "file content and copy that literal span verbatim. If you are not fully certain of the exact "
                            "surrounding characters, choose a shorter contiguous snippet you can copy with total certainty "
                            "instead of a longer one you would have to reconstruct. "
                            "For a milestone, label each patch with task 1, 2, or 3 and order related tasks sequentially. "
                            "The working directory contains read-only copies of only the approved editable files. You may "
                            "inspect them with read-only shell commands, but do not attempt to edit them with tools. Large "
                            "files may be contiguous relevance excerpts with omitted prefixes and suffixes; do not treat "
                            "excerpt boundaries as syntax defects or assume the full file is malformed. Static code evidence "
                            "is sufficient to prove a defect when the incorrect behavior follows deterministically. When an "
                            "approved test file is present, add a focused regression assertion that fails before the fix and "
                            "passes after it. For client-side behavior, execute the relevant logic or an equivalent isolated "
                            "function with representative data; a source-text includes or regex assertion alone is insufficient. "
                            "Do not rely only on an unrelated broad smoke test. If an approved existing test harness is blocked "
                            "only by stale hardcoded calendar dates, make those fixture dates relative to the runtime date without "
                            "skipping, deleting, or weakening its assertions. "
                            "When the objective includes control-plane verification evidence, repair the reported verification "
                            "failure before revising an already-related product fix. Keep linked fixture, query, and expectation "
                            "dates consistent when converting a past-date scenario to a runtime-relative date. "
                            "Do not invent files, weaken tests, or include markdown."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps({"objective": requirement, "editable_files": model_files}, ensure_ascii=False),
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
                                            "before": {"type": "string"},
                                            "after": {"type": "string"},
                                            "task": {"type": "integer", "minimum": 1, "maximum": 3},
                                        },
                                        "required": ["path", "old", "new", "before", "after", "task"],
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
            if self.max_tokens:
                payload["max_tokens"] = self.max_tokens
            responses = []
            last_error = None
            for replan in range(self.max_replans + 1):
                if replan:
                    current = {path: (root / path).read_text(encoding="utf-8") for path in self.editable_files}
                    config["_falguna_context_files"] = {path: self._context_excerpt(value, requirement) for path, value in current.items()}
                    payload["messages"].append({"role": "user", "content": json.dumps({
                        "replan_reason": str(last_error), "current_files": list(current),
                        "instruction": "Re-read the current approved files and return replacement patches with unique anchors. Do not repeat the failed target."
                    })})
                decoded = self.transport(config, payload, self.timeout_seconds)
                call, call_cost = self._model_call(decoded, config)
                calls.append(call)
                total_cost += call_cost
                message = decoded["choices"][0]["message"]
                if message.get("refusal"):
                    raise ValueError("model refused bounded edit")
                response = json.loads(message["content"])
                try:
                    tasks = response.get("tasks")
                    if not tasks:
                        grouped = {}
                        for patch in response.get("patches", []):
                            grouped.setdefault(int(patch.get("task", 1)), []).append(patch)
                        tasks = [{"summary": f"milestone task {key}", "patches": grouped[key]} for key in sorted(grouped)]
                    if not tasks:
                        raise PatchTargetError("PATCH_NOOP: model returned no tasks or patches")
                    if not 1 <= len(tasks) <= 3:
                        raise ValueError("SCOPE_EXPANSION_REQUIRED: milestone must contain one to three related tasks")
                    for ordinal, task in enumerate(tasks, 1):
                        self._apply_patches(root, task.get("patches", []))
                        if self.checkpoint:
                            self.checkpoint(ordinal, task.get("summary", f"task {ordinal}"))
                    responses.append(response.get("summary") or "; ".join(task.get("summary", "") for task in tasks))
                    break
                except PatchTargetError as exc:
                    last_error = exc
                    if replan >= self.max_replans:
                        raise
            changed = sum((root / path).read_text(encoding="utf-8") != original for path, original in files.items())
            if not changed:
                raise ValueError("PATCH_NOOP: model edits made no changes")
            return WorkerResult(True, responses[-1], 0, calls, total_cost)
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError, OSError, urllib.error.URLError) as exc:
            return WorkerResult(False, f"Structured edit failed: {exc}", 65, calls, total_cost)

    def _apply_patches(self, root: Path, patches: list) -> None:
        if not patches:
            raise PatchTargetError("PATCH_NOOP: model returned no patches")
        allowed = set(self.editable_files)
        diff_chars = 0
        for patch in patches:
            relative = patch["path"]
            if relative not in allowed:
                # A wrong-but-plausible path (the model inventing a path instead of using one
                # of the literal approved editable_files) is a recoverable mistake, not a
                # security boundary violation -- route it through the same replan/feedback
                # loop as PATCH_STALE/PATCH_AMBIGUOUS so the model sees exactly which path it
                # got wrong and the real approved list on its very next attempt, instead of
                # failing the whole run closed and restarting from scratch with no memory of
                # the mistake (this was observed hallucinating a different wrong path on every
                # independent run attempt).
                raise PatchTargetError(
                    f"SCOPE_EXPANSION_REQUIRED: unapproved edit path: {relative}; "
                    f"the only approved editable file path(s) are: {sorted(allowed)}"
                )
            target = (root / relative).resolve()
            if target == root or root not in target.parents:
                raise ValueError(f"path escapes worktree: {relative}")
            current = target.read_text(encoding="utf-8")  # re-read before every sequential edit
            old, new = patch["old"], patch["new"]
            if not old or old == new:
                raise PatchTargetError(f"PATCH_NOOP: {relative}")
            matches = [match.start() for match in re.finditer(re.escape(old), current)]
            before, after = patch.get("before", ""), patch.get("after", "")
            anchored = [index for index in matches if (not before or current[:index].endswith(before)) and (not after or current[index + len(old):].startswith(after))]
            if not matches:
                raise PatchTargetError(f"PATCH_STALE: old text is absent in current {relative}; sha256={hashlib.sha256(current.encode()).hexdigest()}")
            # A unique exact old-text match is already deterministic. Optional model-supplied
            # anchors are only needed to disambiguate repeated text and may be stale themselves.
            if len(matches) == 1:
                anchored = matches
            if len(anchored) != 1:
                raise PatchTargetError(f"PATCH_AMBIGUOUS: target has {len(anchored)} anchored matches in {relative}; add unique before/after context")
            diff_chars += len(old) + len(new)
            if diff_chars > self.max_diff_chars or len(new) > max(50000, len(current) * 2):
                raise ValueError(f"CONTEXT_TOO_LARGE: unexpectedly huge diff for {relative}")
            index = anchored[0]
            target.write_text(current[:index] + new + current[index + len(old):], encoding="utf-8")

    @staticmethod
    def _model_call(decoded: dict, config: dict):
        usage = decoded.get("usage", {})
        prompt_tokens = int(usage.get("prompt_tokens", 0))
        completion_tokens = int(usage.get("completion_tokens", 0))
        cached_tokens = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0))
        calculated_cost = max(0, prompt_tokens - cached_tokens) * 0.75e-6 + cached_tokens * 0.075e-6 + completion_tokens * 4.50e-6
        cost = float(decoded.get("_falguna_cost_usd", calculated_cost))
        metadata = decoded.get("_falguna_metadata", {})
        call = {"provider": decoded.get("_falguna_provider", "openai-compatible"), "model": metadata.get("routed_model", config["model"]), "purpose": "implementation", "input_tokens": prompt_tokens, "output_tokens": completion_tokens, "cost_usd": cost, "metadata": {"adapter": "structured-edit", "cached_input_tokens": cached_tokens, **metadata}}
        return call, cost

    @staticmethod
    def _context_excerpt(content: str, requirement: str, max_chars: int = 50000) -> str:
        if len(content) <= max_chars:
            return content
        terms = {
            term for term in re.findall(r"[a-z0-9_]+", requirement.lower())
            if len(term) >= 4 and term not in {
                "only", "with", "this", "that", "from", "then", "appropriate", "inspect", "royal", "table",
                "admin", "find", "genuine", "reproducible", "involving", "workflow", "correct", "exists",
            }
        }
        lines = content.splitlines(keepends=True)
        ranked = sorted(
            ((sum(term in line.lower() for term in terms), index) for index, line in enumerate(lines)),
            key=lambda item: (-item[0], len(lines[item[1]]), item[1]),
        )
        best_score, best_index = ranked[0]
        if best_score == 0:
            return content[:max_chars]
        center = sum(len(line) for line in lines[:best_index]) + len(lines[best_index]) // 2
        start = max(0, center - max_chars // 2)
        end = min(len(content), start + max_chars)
        start = content.find("\n", start) + 1 if start else 0
        end_boundary = content.rfind("\n", start, end)
        return content[start:end_boundary if end_boundary > start else end]

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
