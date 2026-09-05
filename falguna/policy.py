import fnmatch
from pathlib import Path
from typing import Iterable

from .models import CommandSpec, RunPolicy


class PolicyViolation(RuntimeError):
    pass


class PermissionEngine:
    def __init__(self, repo_root: Path, policy: RunPolicy):
        self.repo_root = Path(repo_root).resolve()
        self.policy = policy

    def resolve(self, candidate: Path) -> Path:
        path = (self.repo_root / candidate).resolve() if not Path(candidate).is_absolute() else Path(candidate).resolve()
        if path != self.repo_root and self.repo_root not in path.parents:
            raise PolicyViolation("path escapes worktree")
        return path

    def require_write(self, candidate: Path) -> Path:
        path = self.resolve(candidate)
        rel = path.relative_to(self.repo_root).as_posix()
        if any(fnmatch.fnmatch(rel, pattern) for pattern in self.policy.protected_globs):
            raise PolicyViolation(f"protected path: {rel}")
        if not any(fnmatch.fnmatch(rel, pattern) for pattern in self.policy.allowed_write_globs):
            raise PolicyViolation(f"write not allowed: {rel}")
        return path

    def require_command(self, spec: CommandSpec) -> None:
        if not spec.argv or spec.argv[0] not in self.policy.allowed_commands:
            raise PolicyViolation("command is not allowlisted")
        forbidden = {"push", "merge", "rebase", "reset", "clean", "checkout"}
        if spec.argv[0] == "git" and any(arg in forbidden for arg in spec.argv[1:]):
            raise PolicyViolation("mutating/publishing git operation requires control-plane handling or approval")
        if spec.timeout_seconds <= 0 or spec.timeout_seconds > 900:
            raise PolicyViolation("invalid command timeout")

    def validate_changed_files(self, paths: Iterable[str]) -> None:
        paths = list(paths)
        if len(paths) > self.policy.max_changed_files:
            raise PolicyViolation("changed-file limit exceeded")
        for path in paths:
            self.require_write(Path(path))

