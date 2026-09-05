import hashlib
import json
from pathlib import Path
from typing import List

from .capabilities import TerminalCapability
from .gitops import GitWorktreeManager
from .models import ReviewResult, RunPolicy
from .policy import PermissionEngine, PolicyViolation


class DefinitionOfDone:
    def __init__(self, git: GitWorktreeManager, policy: RunPolicy):
        self.git = git
        self.policy = policy

    def verify(self, worktree: Path):
        permissions = PermissionEngine(worktree, self.policy)
        changed = self.git.changed_files(worktree)
        permissions.validate_changed_files(changed)
        terminal = TerminalCapability(permissions)
        results = []
        for command in self.policy.verification_commands:
            completed = terminal.run(command, {"CI": "1"})
            results.append({"label": command.label, "argv": command.argv, "exit_code": completed.returncode, "stdout": completed.stdout[-8000:], "stderr": completed.stderr[-8000:]})
        passed = bool(changed) and all(item["exit_code"] == 0 for item in results)
        return passed, changed, results


class IndependentReviewer:
    """Deterministic independent run path; a model reviewer can replace it via the same result contract."""

    BLOCKED_MARKERS = ("OPENAI_API_KEY", "AWS_SECRET", "BEGIN PRIVATE KEY", "--no-verify")

    def review(self, diff: str, changed_files: List[str]) -> ReviewResult:
        findings = []
        if not diff.strip():
            findings.append("empty diff")
        for marker in self.BLOCKED_MARKERS:
            if marker in diff:
                findings.append(f"blocked marker in diff: {marker}")
        if any(path.startswith("schema/") or path == "falguna/policy.py" for path in changed_files):
            findings.append("protected policy/schema change")
        return ReviewResult(not findings, "independent deterministic review", findings)


def preserve_artifact(source: Path, artifact_dir: Path) -> dict:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    data = source.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    target = artifact_dir / source.name
    target.write_bytes(data)
    return {"path": str(target), "sha256": digest}

