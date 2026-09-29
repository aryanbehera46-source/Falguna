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
        # Requirement 1 audit fix: when hidden_verification_files is set, the
        # worker's worktree now originates from a history-scrubbed clone
        # (GitWorktreeManager.create_confidential) that never had these paths
        # committed, so restoring the real file for this verification step
        # makes `git status` report it as a brand-new untracked addition
        # rather than an unmodified tracked file. Excluding it here restores
        # the same "restoring the hidden file is invisible to changed-file
        # accounting" contract the pre-existing non-scrubbed architecture had
        # implicitly (there, restoring put the file back byte-for-byte
        # identical to a tracked HEAD entry, so git status already showed
        # nothing). This never hides a worker's own write to that path: an
        # attempted write is rejected earlier, at write time, by
        # PermissionEngine.require_write against allowed_write_globs (see
        # HiddenTestBypassAttemptAdversarialTests) -- this filter only ever
        # sees the independent verifier's own restore, never the worker's.
        hidden_paths = set(getattr(self.policy, "hidden_verification_files", None) or [])
        if hidden_paths:
            changed = [path for path in changed if path not in hidden_paths]
        permissions.validate_changed_files(changed)
        terminal = TerminalCapability(permissions)
        results = []
        implementation_changed = not self.policy.require_implementation_change or bool(set(changed).intersection(self.policy.implementation_files))
        if not implementation_changed:
            results.append({"label": "implementation-scope", "argv": [], "exit_code": 1, "stdout": "", "stderr": "IMPLEMENTATION_SCOPE_UNRESOLVED: feature/change mission changed only verification files"})
        isolation = []
        command_env = {"CI": "1"}
        if self.policy.dependency_node_path:
            dependency_path = Path(self.policy.dependency_node_path).resolve()
            if not dependency_path.is_dir():
                raise PolicyViolation("approved local dependency path is unavailable")
            command_env["NODE_PATH"] = str(dependency_path)
        for command in self.policy.verification_commands:
            completed = terminal.run(command, command_env, network_mode=command.network_mode)
            results.append({"label": command.label, "argv": command.argv, "exit_code": completed.returncode, "stdout": completed.stdout[-8000:], "stderr": completed.stderr[-8000:]})
            isolation.append(terminal.last_isolation_evidence.__dict__)
        browser_plan = BrowserDiscovery(worktree, self.policy).discover()
        browser = None
        if self.policy.browser_applicable and browser_plan is None:
            browser = {"passed": False, "applicability": "REQUIRED", "error": "browser verification was applicable but no approved local Playwright plan was found"}
        if browser_plan:
            browser_terminal = TerminalCapability(permissions)
            browser_result = BrowserCapability(browser_terminal).verify(browser_plan.command, browser_plan.base_url, browser_plan.browsers_path)
            browser = {"discovery": browser_plan.evidence(), "exit_code": browser_result.returncode, "stdout": browser_result.stdout[-8000:], "stderr": browser_result.stderr[-8000:], "isolation": browser_terminal.last_isolation_evidence.__dict__}
            boundary_marker = "FALGUNA_NETWORK_BOUNDARY external_blocked=true localhost_ok=true"
            browser["network_boundary"] = {
                "enforcement": "Playwright browser-runtime request allowlist",
                "external_probe_blocked": boundary_marker in browser_result.stdout,
                "localhost_verified": browser_result.returncode == 0,
                "kernel_enforced": False,
            }
            browser["passed"] = browser_result.returncode == 0 and (not self.policy.browser_external_probe_required or browser["network_boundary"]["external_probe_blocked"])
        containment_probe = ProcessIsolator(worktree, self.policy).harmless_prohibited_write_probe()
        browser_passed = browser is None or (browser.get("exit_code") == 0 and (not self.policy.browser_external_probe_required or browser["network_boundary"]["external_probe_blocked"]))
        passed = bool(changed) and implementation_changed and all(item["exit_code"] == 0 for item in results) and browser_passed and containment_probe["blocked"]
        return passed, changed, results, browser, isolation, containment_probe


def preserve_artifact(source: Path, artifact_dir: Path) -> dict:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    data = source.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    target = artifact_dir / source.name
    target.write_bytes(data)
    return {"path": str(target), "sha256": digest}
