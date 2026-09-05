from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class RunStatus(str, Enum):
    CREATED = "CREATED"
    PLANNING = "PLANNING"
    WORKING = "WORKING"
    VERIFYING = "VERIFYING"
    REVIEWING = "REVIEWING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    DONE_CANDIDATE = "DONE_CANDIDATE"
    FAILED = "FAILED"
    QUARANTINED = "QUARANTINED"


@dataclass(frozen=True)
class CommandSpec:
    argv: List[str]
    timeout_seconds: int = 300
    label: str = "command"
    network_mode: str = "deny"


@dataclass(frozen=True)
class RunPolicy:
    allowed_write_globs: List[str] = field(default_factory=lambda: ["falguna/**", "tests/**", "README.md"])
    protected_globs: List[str] = field(default_factory=lambda: [".git/**", ".env", "**/.env", "falguna/policy.py", "schema/**"])
    allowed_commands: List[str] = field(default_factory=lambda: ["python3", "git", "npm", "npx"])
    verification_commands: List[CommandSpec] = field(default_factory=lambda: [CommandSpec(["python3", "-m", "unittest", "discover", "-s", "tests", "-v"], 300, "tests")])
    max_attempts: int = 2
    max_cost_usd: float = 0.50
    max_changed_files: int = 12
    allow_network: bool = False
    browser_project_roots: List[str] = field(default_factory=lambda: [".", "browser-tests", "frontend", "web", "app", "client", "ui"])
    browser_base_url: Optional[str] = None
    browser_require_lockfile: bool = True
    browser_cached_install_allowed: bool = False
    browser_external_probe_required: bool = False
    max_memory_mb: int = 1024
    max_processes: int = 64


@dataclass
class WorkerResult:
    success: bool
    summary: str
    exit_code: int
    model_calls: List[dict] = field(default_factory=list)
    cost_usd: float = 0.0


@dataclass
class ReviewResult:
    approved: bool
    summary: str
    findings: List[str] = field(default_factory=list)
    dimensions: dict = field(default_factory=dict)
    unresolved_uncertainty: List[str] = field(default_factory=list)
    model_calls: List[dict] = field(default_factory=list)
    cost_usd: float = 0.0
