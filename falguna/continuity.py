import hashlib
import json
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from .discovery import DiscoveryPlan, ProjectDiscovery
from .models import CommandSpec
from .store import utcnow


FRONTEND_SUFFIXES = {".html", ".css", ".jsx", ".tsx", ".vue", ".svelte"}
FRONTEND_PARTS = {"public", "client", "frontend", "web", "ui"}
FRONTEND_TERMS = {"browser", "frontend", "responsive", "mobile", "tablet", "layout", "button", "form", "page", "ui"}


def browser_e2e_applicable(objective: str, editable_files: list[str], profile: dict) -> bool:
    if not profile.get("browser_base_url"):
        return False
    words = set(objective.lower().replace("/", " ").replace("-", " ").split())
    file_signal = any(Path(item).suffix.lower() in FRONTEND_SUFFIXES or FRONTEND_PARTS.intersection(Path(item).parts) for item in editable_files)
    return bool(file_signal or words.intersection(FRONTEND_TERMS))


class ProjectUnderstandingCache:
    def __init__(self, store):
        self.store = store

    def discover(self, repository: Path, profile: dict, objective: str) -> tuple[DiscoveryPlan, dict]:
        repository = Path(repository).resolve()
        fingerprint = self.fingerprint(repository, profile)
        entries = self.store.list("project_cache", "repository=? AND profile_id=?", (str(repository), profile.get("id", "unknown")))
        latest = entries[-1] if entries else None
        if latest and latest["fingerprint"] == fingerprint:
            cached = json.loads(latest["payload_json"])
            # Re-score only the objective-sensitive files while reusing validated structure and commands.
            fresh = ProjectDiscovery(repository, profile).discover(objective)
            cached_commands = [CommandSpec(**item) for item in cached["verification_commands"]]
            plan = DiscoveryPlan(fresh.editable_files, cached_commands, fresh.confidence, fresh.rationale + ["validated project-understanding cache hit"], fresh.requires_approval, fresh.implementation_files, fresh.verification_files, fresh.objective_kind, fresh.diagnostic)
            return plan, {"status": "HIT", "fingerprint": fingerprint, "revalidated": ["git-head", "profile", "package-metadata"]}
        plan = ProjectDiscovery(repository, profile).discover(objective)
        payload = plan.evidence()
        self.store.create("project_cache", {"repository": str(repository), "profile_id": profile.get("id", "unknown"), "fingerprint": fingerprint, "payload_json": json.dumps(payload, sort_keys=True), "created_at": utcnow(), "updated_at": utcnow()})
        return plan, {"status": "MISS" if not latest else "INVALIDATED", "fingerprint": fingerprint, "reason": "new project" if not latest else "repo, package, or profile changed"}

    @staticmethod
    def fingerprint(repository: Path, profile: dict) -> str:
        head = subprocess.check_output(["git", "-C", str(repository), "rev-parse", "HEAD"], text=True).strip()
        package_root = Path(str(profile.get("package_root", ".")))
        metadata = []
        for name in ("package.json", "package-lock.json", "pyproject.toml"):
            path = repository / package_root / name if name.startswith("package") else repository / name
            if path.is_file():
                metadata.append((str(path.relative_to(repository)), hashlib.sha256(path.read_bytes()).hexdigest()))
        raw = json.dumps({"head": head, "profile": profile, "metadata": metadata}, sort_keys=True, default=str).encode()
        return hashlib.sha256(raw).hexdigest()


def resolve_continuation(store, repository: Path) -> Optional[str]:
    tasks = store.list("tasks", "repository=?", (str(Path(repository).resolve()),))
    candidates = []
    for task in tasks:
        runs = store.list("runs", "task_id=?", (task["id"],))
        candidates.extend(run for run in runs if run["status"] in {"PAUSED", "FAILED"})
    if len(candidates) != 1:
        return None
    return candidates[0]["id"]
