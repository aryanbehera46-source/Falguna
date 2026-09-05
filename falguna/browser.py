import json
import os
import platform
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse

from .models import CommandSpec, RunPolicy
from .policy import PolicyViolation
from .isolation import ProcessIsolator


@dataclass(frozen=True)
class BrowserPlan:
    project_root: str
    command: CommandSpec
    base_url: str
    source: str
    config_path: Optional[str] = None
    executable_source: Optional[str] = None
    browsers_path: Optional[str] = None
    provisioning: Optional[dict] = None

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
            provisioning = self._preflight(root, package, browsers_path)
            browsers_path = provisioning.get("browsers_path") or browsers_path
            executable_source = provisioning.get("executable_source") or executable_source
            candidates.append(BrowserPlan(root_rel, command, self.policy.browser_base_url, source, str(config.relative_to(self.worktree)) if config else None, executable_source, browsers_path, provisioning))
        if len(candidates) > 1:
            raise PolicyViolation("ambiguous browser verification layouts; narrow browser_project_roots")
        return candidates[0] if candidates else None

    def _preflight(self, root: Path, package: Path, browsers_path: Optional[str]) -> dict:
        lockfile = root / "package-lock.json"
        cli = root / "node_modules" / ".bin" / "playwright"
        browser_metadata = root / "node_modules" / "playwright-core" / "browsers.json"
        if self.policy.browser_require_lockfile and not lockfile.is_file():
            raise PolicyViolation("deterministic browser provisioning requires package-lock.json")
        install_evidence = {"performed": False, "command": None, "exit_code": None}
        if not cli.is_file() or not os.access(cli, os.X_OK):
            if not self.policy.browser_cached_install_allowed:
                raise PolicyViolation("local Playwright CLI missing; cached deterministic install is not allowed by policy")
            if "npm" not in self.policy.allowed_commands:
                raise PolicyViolation("npm is not allowlisted for deterministic browser provisioning")
            root_rel = root.relative_to(self.worktree).as_posix() or "."
            npm_cache = Path.home() / ".npm"
            spec = CommandSpec(["npm", "--prefix", root_rel, "ci", "--offline", "--ignore-scripts", "--no-audit", "--no-fund"], 300, "browser-dependencies-cached")
            completed, isolation = ProcessIsolator(self.worktree, self.policy).run(spec, {"NPM_CONFIG_CACHE": str(npm_cache)}, "deny")
            install_evidence = {"performed": True, "command": spec.argv, "exit_code": completed.returncode, "offline": True, "isolation": isolation.__dict__, "stderr": completed.stderr[-2000:]}
            if completed.returncode != 0 or not cli.is_file() or not os.access(cli, os.X_OK):
                raise PolicyViolation("offline lockfile installation could not provision the local Playwright CLI")
        try:
            manifest = json.loads(package.read_text(encoding="utf-8"))
            locked = json.loads(lockfile.read_text(encoding="utf-8")) if lockfile.is_file() else {}
            metadata = json.loads(browser_metadata.read_text(encoding="utf-8"))
            declared = (manifest.get("devDependencies") or {}).get("@playwright/test")
            installed = ((locked.get("packages") or {}).get("node_modules/@playwright/test") or {}).get("version")
            chromium = next(item for item in metadata["browsers"] if item["name"] == "chromium-headless-shell")
        except (OSError, ValueError, KeyError, StopIteration, TypeError, json.JSONDecodeError) as exc:
            raise PolicyViolation(f"invalid Playwright provisioning metadata: {exc}") from exc
        if not declared or declared != installed:
            raise PolicyViolation("Playwright manifest and lockfile versions do not match exactly")
        if "npm" not in self.policy.allowed_commands:
            raise PolicyViolation("npm is not allowlisted for Playwright validation")
        root_rel = root.relative_to(self.worktree).as_posix() or "."
        version_spec = CommandSpec(["npm", "--prefix", root_rel, "exec", "--offline", "--", "playwright", "--version"], 30, "browser-cli-version")
        version, version_isolation = ProcessIsolator(self.worktree, self.policy).run(version_spec, {"NPM_CONFIG_CACHE": str(Path.home() / ".npm")}, "deny")
        if version.returncode != 0 or installed not in version.stdout:
            raise PolicyViolation("installed Playwright CLI does not match the lockfile")
        cache = Path(browsers_path).resolve() if browsers_path else self._standard_cache()
        revision_dir = cache / f"chromium_headless_shell-{chromium['revision']}"
        executables = sorted(path for path in revision_dir.rglob("chrome-headless-shell") if path.is_file() and os.access(path, os.X_OK)) if revision_dir.is_dir() else []
        if not executables:
            raise PolicyViolation(f"required Chromium revision {chromium['revision']} is not provisioned in the validated Playwright cache")
        return {
            "status": "READY_CACHED",
            "package": "@playwright/test",
            "version": installed,
            "lockfile": str(lockfile.relative_to(self.worktree)),
            "cli": str(cli.relative_to(self.worktree)),
            "cli_version": version.stdout.strip(),
            "cli_validation": {"command": version_spec.argv, "exit_code": version.returncode, "offline": True, "isolation": version_isolation.__dict__},
            "browser": "chromium-headless-shell",
            "browser_revision": str(chromium["revision"]),
            "browser_executable": str(executables[0]),
            "browsers_path": str(cache),
            "executable_source": "validated-lockfile-and-standard-cache",
            "network_install_required": False,
            "dependency_install": install_evidence,
            "host": f"{platform.system()}-{platform.machine()}",
        }

    @staticmethod
    def _standard_cache() -> Path:
        candidates = (Path.home() / "Library" / "Caches" / "ms-playwright", Path.home() / ".cache" / "ms-playwright")
        cache = next((path.resolve() for path in candidates if path.is_dir()), None)
        if not cache:
            raise PolicyViolation("standard Playwright browser cache is missing")
        return cache
