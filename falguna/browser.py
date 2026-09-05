import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse

from .models import CommandSpec, RunPolicy
from .policy import PolicyViolation


@dataclass(frozen=True)
class BrowserPlan:
    project_root: str
    command: CommandSpec
    base_url: str
    source: str
    config_path: Optional[str] = None
    executable_source: Optional[str] = None
    browsers_path: Optional[str] = None

    def evidence(self) -> dict:
        value = asdict(self)
        value["command"] = asdict(self.command)
        return value


class BrowserDiscovery:
    """Discovers a portable Playwright plan from policy and repository manifests."""

    CONFIG_NAMES = ("playwright.config.ts", "playwright.config.js", "playwright.config.mjs", "playwright.config.cjs")
    SCRIPT_NAMES = ("test:e2e", "test:browser", "playwright", "e2e")

    def __init__(self, worktree: Path, policy: RunPolicy):
        self.worktree = Path(worktree).resolve()
        self.policy = policy

    @staticmethod
    def require_localhost(url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise PolicyViolation("browser verification is localhost-only")
        if parsed.username or parsed.password:
            raise PolicyViolation("browser URL credentials are prohibited")

    def discover(self) -> Optional[BrowserPlan]:
        if not self.policy.browser_base_url:
            return None
        self.require_localhost(self.policy.browser_base_url)
        candidates: List[BrowserPlan] = []
        for relative_root in self.policy.browser_project_roots:
            root = (self.worktree / relative_root).resolve()
            if root != self.worktree and self.worktree not in root.parents:
                raise PolicyViolation("browser project root escapes worktree")
            if not root.is_dir():
                continue
            package = root / "package.json"
            config = next((root / name for name in self.CONFIG_NAMES if (root / name).is_file()), None)
            scripts = {}
            if package.is_file():
                try:
                    scripts = json.loads(package.read_text(encoding="utf-8")).get("scripts", {})
                except (json.JSONDecodeError, OSError, TypeError):
                    raise PolicyViolation(f"invalid browser manifest: {package.relative_to(self.worktree)}")
            script = next((name for name in self.SCRIPT_NAMES if name in scripts), None)
            root_rel = root.relative_to(self.worktree).as_posix() or "."
            if script:
                command = CommandSpec(["npm", "--prefix", root_rel, "run", script, "--", "--reporter=line"], 600, "playwright")
                source = f"package.json#{script}"
            elif config:
                command = CommandSpec(["npx", "--prefix", root_rel, "playwright", "test", "--reporter=line"], 600, "playwright")
                source = "playwright-config"
            else:
                continue
            executable_source = None
            browsers_path = None
            if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
                executable_source = "PLAYWRIGHT_BROWSERS_PATH"
                browsers_path = str(Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"]).expanduser().resolve())
            elif (root / "node_modules" / ".bin" / "playwright").exists():
                executable_source = "project-node-modules"
            else:
                executable_source = "npx-resolution"
            if browsers_path is None:
                cache_candidates = (Path.home() / "Library" / "Caches" / "ms-playwright", Path.home() / ".cache" / "ms-playwright")
                cache = next((path.resolve() for path in cache_candidates if path.is_dir()), None)
                if cache:
                    browsers_path = str(cache)
                    executable_source = f"standard-user-cache:{cache.parent.name}"
            candidates.append(BrowserPlan(root_rel, command, self.policy.browser_base_url, source, str(config.relative_to(self.worktree)) if config else None, executable_source, browsers_path))
        if len(candidates) > 1:
            raise PolicyViolation("ambiguous browser verification layouts; narrow browser_project_roots")
        return candidates[0] if candidates else None
