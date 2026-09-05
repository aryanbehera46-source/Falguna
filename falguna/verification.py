import hashlib
import json
from pathlib import Path
from typing import List

from .browser import BrowserDiscovery
from .capabilities import BrowserCapability, TerminalCapability
from .gitops import GitWorktreeManager
from .models import RunPolicy
from .policy import PermissionEngine, PolicyViolation
from .isolation import ProcessIsolator


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
        isolation = []
        for command in self.policy.verification_commands:
            completed = terminal.run(command, {"CI": "1"})
            results.append({"label": command.label, "argv": command.argv, "exit_code": completed.returncode, "stdout": completed.stdout[-8000:], "stderr": completed.stderr[-8000:]})
            isolation.append(terminal.last_isolation_evidence.__dict__)
        browser_plan = BrowserDiscovery(worktree, self.policy).discover()
        browser = None
        if browser_plan:
            browser_terminal = TerminalCapability(permissions)
            browser_result = BrowserCapability(browser_terminal).verify(browser_plan.command, browser_plan.base_url, browser_plan.browsers_path)
            browser = {"discovery": browser_plan.evidence(), "exit_code": browser_result.returncode, "stdout": browser_result.stdout[-8000:], "stderr": browser_result.stderr[-8000:], "isolation": browser_terminal.last_isolation_evidence.__dict__}
        containment_probe = ProcessIsolator(worktree, self.policy).harmless_prohibited_write_probe()
        passed = bool(changed) and all(item["exit_code"] == 0 for item in results) and (browser is None or browser["exit_code"] == 0) and containment_probe["blocked"]
        return passed, changed, results, browser, isolation, containment_probe


def preserve_artifact(source: Path, artifact_dir: Path) -> dict:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    data = source.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    target = artifact_dir / source.name
    target.write_bytes(data)
    return {"path": str(target), "sha256": digest}
