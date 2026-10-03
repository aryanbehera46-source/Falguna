"""Governed adapters from the Phase 9 graph to existing FALGUNA workers."""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional
from urllib.parse import urlparse

from .browser_runtime import BrowserSessionStatus
from .frontier import AutonomyPolicy, FrontierControlPlane, FrontierError
from .research import ResearchStore, rank_sources


class WorkerDispatchError(RuntimeError):
    pass


def _safe_repo(path: str, roots: Iterable[Path]) -> Path:
    repo = Path(path or "").resolve()
    allowed = [Path(root).resolve() for root in roots]
    if not any(repo == root or root in repo.parents for root in allowed):
        raise WorkerDispatchError("Repository path is outside the approved local roots")
    if not (repo / ".git").exists():
        raise WorkerDispatchError("Approved path is not a Git repository")
    return repo


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: ("[REDACTED]" if any(word in key.lower() for word in ("secret", "token", "password", "api_key")) else _redact(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


class CodeGraphWorker:
    """Invokes the existing WorkerAdapter contract inside an approved repo."""

    def __init__(self, worker, approved_roots: Iterable[Path]):
        self.worker = worker
        self.approved_roots = list(approved_roots)

    def __call__(self, inputs: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        repo = _safe_repo(str(inputs.get("repository") or ""), self.approved_roots)
        requirement = str(inputs.get("requirement") or "").strip()
        if not requirement:
            raise WorkerDispatchError("Code worker requires a bounded requirement")
        before = subprocess.run(["git", "status", "--porcelain=v1"], cwd=repo, text=True, capture_output=True, check=True).stdout
        result = self.worker.execute(repo, requirement, context["node_id"])
        if not result.success:
            raise WorkerDispatchError(f"Code worker failed: {result.summary}")
        diff = subprocess.run(["git", "diff", "--"], cwd=repo, text=True, capture_output=True, check=True).stdout
        changed = subprocess.run(["git", "status", "--porcelain=v1"], cwd=repo, text=True, capture_output=True, check=True).stdout.splitlines()
        return {"worker": "CODE", "summary": result.summary, "exit_code": result.exit_code,
                "before_status": before.splitlines(), "changed_files": [line[3:] for line in changed],
                "diff_sha256": hashlib.sha256(diff.encode()).hexdigest(), "side_effect_key": inputs.get("side_effect_key")}


class ResearchGraphWorker:
    """Uses the existing provider/store seam and persists source provenance."""

    def __init__(self, provider, store, responder=None):
        self.provider, self.research, self.responder = provider, ResearchStore(store), responder

    def __call__(self, inputs: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        query = str(inputs.get("query") or "").strip()
        if not query:
            raise WorkerDispatchError("Research worker requires a query")
        result = self.provider.search(query, max_results=min(int(inputs.get("max_results", 4)), 8))
        sources = rank_sources(result.sources)
        research_id = self.research.create_query(query, result.provider_name, project_id=inputs.get("project_id"))
        synthesis = self.responder.reply(query, sources, result) if self.responder else {
            "answer": "Bounded source collection completed; factual conclusions require cited synthesis.", "citations": [],
            "suggested_objective": None, "model_call": None,
        }
        self.research.save_result(research_id, synthesis["answer"], sources, synthesis["citations"], synthesis.get("suggested_objective"), synthesis.get("model_call"))
        return {"worker": "RESEARCH", "research_id": research_id, "answer": synthesis["answer"],
                "sources": [{"url": source.url, "title": source.title, "published_at": source.published_at,
                             "provenance": result.provider_name, "confidence": "SOURCED"} for source in sources],
                "inference": "Any uncited synthesis is explicitly non-evidentiary."}


class BrowserGraphWorker:
    """Runs the existing browser runtime, restricted to local test origins."""

    def __init__(self, runtime, sessions):
        self.runtime, self.sessions = runtime, sessions

    def __call__(self, inputs: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        steps = list(inputs.get("steps") or [])
        for step in steps:
            if step.get("action") not in {"open", "inspect", "extract", "screenshot", "click"}:
                raise WorkerDispatchError("Browser action is outside the Trial #1 local QA allowlist")
            if step.get("action") == "open":
                parsed = urlparse(str(step.get("target") or ""))
                if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "localhost"}:
                    raise WorkerDispatchError("Browser worker is restricted to localhost in this run")
        session_id = self.sessions.create(inputs.get("objective", "Local browser QA"), "phase9_trial", None,
                                          "phase9-dispatcher", True, "LOCAL_TEST", plan=steps)
        self.runtime.run(session_id, steps)
        session = self.sessions.get(session_id)
        if session["status"] != BrowserSessionStatus.COMPLETED:
            detail = str(session.get("error_detail") or "")[:500]
            raise WorkerDispatchError(f"Browser QA did not complete: {session.get('error') or session['status']} {detail}".strip())
        actions = self.sessions.list_actions(session_id)
        return {"worker": "BROWSER_QA", "session_id": session_id, "status": session["status"],
                "current_url": session.get("current_url"), "actions": len(actions),
                "screenshot_attachment_id": (self.sessions.latest_action_with_screenshot(session_id) or {}).get("screenshot_attachment_id")}


class GraphWorkerDispatcher:
    """Claims one durable node, executes only an allowlisted adapter, records evidence."""

    def __init__(self, control: FrontierControlPlane, organization_id: str,
                 handlers: Dict[str, Callable[[Dict[str, Any], Dict[str, Any]], Dict[str, Any]]],
                 max_attempts: int = 2):
        self.control, self.organization_id = control, organization_id
        self.handlers = {str(key).upper(): value for key, value in handlers.items()}
        self.max_attempts = max(1, min(int(max_attempts), 5))

    def run_node(self, node_id: str, worker_id: str) -> Dict[str, Any]:
        node = self.control._org_row("p9_graph_nodes", node_id, self.organization_id)
        objective = self.control._org_row("p9_objectives", node["objective_id"], self.organization_id)
        policy = AutonomyPolicy.decision(objective["autonomy_level"], node["approval_class"])
        if not policy["allowed"]:
            raise FrontierError(policy["reason"])
        handler = self.handlers.get(node["node_type"])
        if handler is None:
            raise WorkerDispatchError("No governed adapter is registered for this worker type")
        claimed = self.control.claim_node(node_id, self.organization_id, worker_id)
        inputs = json.loads(claimed.get("input_json") or "{}")
        self.control.heartbeat_node(node_id, self.organization_id, worker_id, checkpoint={"stage": "DISPATCHED", "attempt": claimed["attempt"]})
        try:
            output = _redact(handler(inputs, {"node_id": node_id, "objective_id": node["objective_id"], "worker_id": worker_id}))
            evidence = [{"kind": f"{node['node_type']}_RESULT", "summary": f"{node['node_type']} worker completed with durable output",
                         "provenance": {"adapter": handler.__class__.__name__, "attempt": claimed["attempt"]}}]
            return self.control.complete_node(node_id, self.organization_id, worker_id, output, evidence)
        except Exception as exc:
            self.control.fail_node(node_id, self.organization_id, worker_id, str(exc), retryable=True, max_attempts=self.max_attempts)
            raise
