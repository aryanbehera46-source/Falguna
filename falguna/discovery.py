import json
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from .models import CommandSpec


SKIP_PARTS = {".git", "node_modules", "dist", "build", "coverage", ".falguna"}
TEXT_SUFFIXES = {".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".py", ".html", ".css", ".json", ".md"}
TEST_MARKERS = ("test", "spec")


@dataclass(frozen=True)
class DiscoveryPlan:
    editable_files: list[str]
    verification_commands: list[CommandSpec]
    confidence: str
    rationale: list[str]
    requires_approval: bool = False

    def evidence(self) -> dict:
        value = asdict(self)
        value["verification_commands"] = [asdict(command) for command in self.verification_commands]
        return value


class ProjectDiscovery:
    """Read-only, bounded discovery over tracked repository text and native metadata."""

    def __init__(self, repository: Path, profile: dict):
        self.repository = Path(repository).resolve()
        self.profile = profile

    def discover(self, objective: str) -> DiscoveryPlan:
        if not (self.repository / ".git").exists():
            raise ValueError("approved project is not a Git repository")
        status = subprocess.check_output(
            ["git", "-C", str(self.repository), "status", "--porcelain"], text=True
        )
        if status.strip():
            raise ValueError("project has uncommitted changes; discovery stopped before creating a worktree")
        files = self._tracked_text_files()
        terms = self._terms(objective)
        entrypoint = self._package_entrypoint()
        scored = []
        for relative in files:
            path = self.repository / relative
            try:
                content = path.read_text(encoding="utf-8")[:120_000].lower()
            except (OSError, UnicodeError):
                continue
            lower_path = relative.lower()
            path_score = sum(5 for term in terms if term in lower_path)
            content_score = sum(min(content.count(term), 5) for term in terms)
            score = path_score + content_score + (8 if relative == entrypoint else 0)
            if score:
                scored.append((score, relative))
        scored.sort(key=lambda item: (-item[0], len(item[1]), item[1]))
        # Start with one implementation file. If the worker discovers that a second
        # file is necessary, the mission must pause for explicit scope expansion.
        implementation = [path for _, path in scored if not self._is_test(path)][:1]
        tests = [path for _, path in scored if self._is_test(path)][:1]
        editable = implementation + tests
        commands, sources = self._verification()
        high_confidence = bool(implementation and tests and commands and scored[0][0] >= 3)
        rationale = [
            f"ranked {len(scored)} tracked text files using objective terms",
            f"verification derived from {', '.join(sources)}" if sources else "no supported native verification metadata found",
        ]
        return DiscoveryPlan(
            editable,
            commands,
            "HIGH" if high_confidence else "UNCERTAIN",
            rationale,
            requires_approval=not high_confidence,
        )

    def _tracked_text_files(self) -> list[str]:
        output = subprocess.check_output(
            ["git", "-C", str(self.repository), "ls-files", "-z"]
        ).decode("utf-8")
        roots = tuple(self.profile.get("discovery_roots", ["."]))
        result = []
        for relative in filter(None, output.split("\0")):
            path = Path(relative)
            if any(part in SKIP_PARTS for part in path.parts):
                continue
            if path.suffix.lower() not in TEXT_SUFFIXES:
                continue
            if roots != (".",) and not any(relative == root or relative.startswith(root.rstrip("/") + "/") for root in roots):
                continue
            result.append(relative)
        return result[:500]

    @staticmethod
    def _terms(objective: str) -> list[str]:
        stop = {"the", "and", "for", "with", "from", "this", "that", "make", "add", "fix", "serviceflow", "regression", "coverage"}
        return sorted({term for term in re.findall(r"[a-z0-9_]+", objective.lower()) if len(term) >= 3 and term not in stop})

    @staticmethod
    def _is_test(relative: str) -> bool:
        lower = relative.lower()
        return any(marker in lower for marker in TEST_MARKERS)

    def _verification(self):
        commands = []
        sources = []
        package = self.repository / "package.json"
        if package.is_file():
            scripts = json.loads(package.read_text(encoding="utf-8")).get("scripts", {})
            for name in ("test", "typecheck", "lint", "build"):
                if name in scripts:
                    commands.append(CommandSpec(["npm", "run", name], 600, f"npm-{name}", "loopback" if name == "test" else "deny"))
            if commands:
                sources.append("package.json scripts")
        pyproject = self.repository / "pyproject.toml"
        if not commands and pyproject.is_file() and (self.repository / "tests").is_dir():
            commands.append(CommandSpec(["python3", "-m", "unittest", "discover", "-s", "tests", "-v"], 600, "python-unittest"))
            sources.append("pyproject.toml and tests/")
        return commands, sources

    def _package_entrypoint(self):
        package = self.repository / "package.json"
        if not package.is_file():
            return None
        try:
            main = json.loads(package.read_text(encoding="utf-8")).get("main")
            return str(main) if main else None
        except (OSError, ValueError):
            return None
