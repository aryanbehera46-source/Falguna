"""TTT Digital Workforce v1 -- concrete worker roles + the browser/computer
execution foundation (Sections 4/5, Pass B).

Browser execution foundation: same channel/adapter shape as
`falguna/application_executor.py`. `ManualBrowserChannel` is the only
channel wired in by default and it always reports BLOCKED -- no adapter in
this codebase can open a real page, click a real control, or fill a real
field yet, so guessing is never an option; the honest default is to
escalate. `SimulatedBrowserChannel` is test/QA-only, gated the same way
`SimulatedTestChannel` is gated in application_executor.py. Neither channel
ever attempts to bypass a CAPTCHA, an auth challenge, or an anti-bot system
-- there is no code path here that could.
"""

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .attachments import AttachmentStore
from .browser_planner import plan_steps_from_objective
from .browser_runtime import (
    ALL_ACTION_TYPES,
    BrowserRuntimeError,
    BrowserSessionStatus,
    BrowserSessionStore,
    PlaywrightBrowserRuntime,
)
from .documents import DocumentStore
from .email_admin import EmailStore
from .model_router import ModelRegistry
from .providers import FalgunaModelError
from .workforce import WorkerResult, WorkforceWorker

# --------------------------------------------------------------------------
# Browser/computer execution foundation
# --------------------------------------------------------------------------


@dataclass
class BrowserResult:
    status: str  # "COMPLETED" | "BLOCKED"
    evidence: Dict[str, Any] = field(default_factory=dict)
    blocked_reason: Optional[str] = None


class BrowserChannel:
    name = "browser_channel"

    def attempt(self, task: Dict[str, Any]) -> BrowserResult:
        raise NotImplementedError


class ManualBrowserChannel(BrowserChannel):
    """The honest default: no real browser-automation adapter exists in
    this codebase yet (opening pages, navigating, reading content, clicking
    permitted controls, filling permitted fields, legitimate up/download).
    Building one is out of scope for this pass -- this channel exists so
    that "no adapter yet" is a real, escalated BLOCKED result rather than a
    silently skipped step or a fabricated success."""

    name = "manual_browser"

    def attempt(self, task: Dict[str, Any]) -> BrowserResult:
        return BrowserResult(
            status="BLOCKED",
            evidence={"reason_detail": "no browser/computer automation adapter is enabled for this task; needs manual execution or a specific, approved adapter"},
            blocked_reason="no_browser_adapter_available",
        )


class SimulatedBrowserChannel(BrowserChannel):
    """Test/QA-only -- reports a real-shaped COMPLETED result without
    touching any live page. Never reachable from ordinary execution; only a
    test file or a controlled QA run passes this channel in explicitly."""

    name = "simulated_browser"

    def attempt(self, task: Dict[str, Any]) -> BrowserResult:
        return BrowserResult(
            status="COMPLETED",
            evidence={"simulated": True, "note": "test/QA adapter only -- no real page was opened", "task_id": task["id"]},
        )


class RealPlaywrightBrowserChannel(BrowserChannel):
    """Phase 3 Milestone 2: the real adapter. Reuses the exact same,
    already-tested `BrowserSessionStore` / `AttachmentStore` /
    `PlaywrightBrowserRuntime` engine the human/chat-initiated
    `POST /api/browser/sessions` endpoint uses (see
    `falguna/web.py::_start_browser_session` /
    `_run_browser_session_background`) -- no second browser engine, no
    second sensitive-action classifier, no new escalation machinery.

    The HTTP path kicks off a background thread and lets the caller poll
    the session row for state. `WorkforceWorker.execute()` is instead a
    synchronous, blocking-call contract, so `attempt()` runs
    `PlaywrightBrowserRuntime.run()` directly on the calling thread (it
    mutates the session row as it progresses and returns None -- exactly
    as `browser_runtime.py` documents) and then re-reads that row for the
    session's final status.

    A session that ends anywhere other than COMPLETED -- NEEDS_ARYAN (a
    sensitive action or an unplanned challenge/login wall), FAILED, or an
    unexpected error caught here -- maps to a BLOCKED `BrowserResult`.
    `BrowserWorker.execute()` (unmodified) turns that into a BLOCKED
    `WorkerResult`, which `WorkforceOrchestrator.execute()` (also
    unmodified) already escalates to NEEDS_ARYAN with a real
    `workforce_action_approval` needs-Aryan item. That is the existing
    gate `PlaywrightBrowserRuntime.run()`'s own sensitive-action check
    (`classify_sensitive_action`) already feeds into for the human/chat
    path -- this channel does not weaken, bypass, or duplicate it, which
    is what Aryan's Milestone 2 instruction to preserve every existing
    sensitive-action approval gate requires."""

    name = "real_playwright_browser"

    def __init__(self, app_root, store, audit=None, actor: str = "Workforce"):
        self.app_root = app_root
        self.store = store
        self.audit = audit
        self.actor = actor

    def attempt(self, task: Dict[str, Any]) -> BrowserResult:
        inputs = json.loads(task["inputs_json"]) if task.get("inputs_json") else {}
        objective = str(inputs.get("objective") or task.get("objective") or "").strip()
        if not objective:
            return BrowserResult(
                status="BLOCKED",
                evidence={"reason_detail": "no objective provided for this browser task"},
                blocked_reason="no_objective_provided",
            )
        task_type = str(inputs.get("task_type") or task.get("task_type") or "browser_research")
        project_id = inputs.get("project_id")
        headless = bool(inputs.get("headless", True))
        explicit_steps = inputs.get("steps")
        sessions = BrowserSessionStore(self.store)
        if explicit_steps:
            steps = [s for s in explicit_steps if isinstance(s, dict) and s.get("action") in ALL_ACTION_TYPES][:20]
            if not steps:
                return BrowserResult(
                    status="BLOCKED",
                    evidence={"reason_detail": "no valid browser steps provided"},
                    blocked_reason="no_valid_steps",
                )
        else:
            try:
                steps = plan_steps_from_objective(self.store, objective, task_type, model_override=inputs.get("model"))
            except FalgunaModelError as exc:
                return BrowserResult(
                    status="BLOCKED",
                    evidence={"reason_detail": exc.message},
                    blocked_reason="browser_planning_failed",
                )
        privacy_mode = ModelRegistry(self.store).load()["privacy_mode"]
        session_id = sessions.create(
            objective, task_type, project_id, self.actor, headless, privacy_mode,
            plan=steps, conversation_id=None, research_id=None,
        )
        if self.audit is not None:
            self.audit.append("browser_session_created", {
                "session_id": session_id, "objective": objective[:200], "steps": len(steps), "source": "workforce",
            })
        try:
            attachments = AttachmentStore(self.store, self.app_root)
            runtime = PlaywrightBrowserRuntime(self.app_root, self.store, sessions, attachments, audit=self.audit)
            runtime.run(session_id, steps)
        except Exception as exc:
            # Mirrors _run_browser_session_background's own last-resort net:
            # PlaywrightBrowserRuntime.run() already converts every real
            # failure into a clean session status internally, so this only
            # catches a genuinely unexpected error in the calling thread
            # itself, and it never leaves the row stuck showing RUNNING.
            try:
                sessions.set_status(
                    session_id, BrowserSessionStatus.FAILED,
                    error="Falguna hit an unexpected internal error running this browser task.",
                    error_category=BrowserRuntimeError.UNEXPECTED_FAILURE, error_detail=str(exc)[:2000],
                )
            except Exception:
                pass
        session = sessions.get(session_id)
        if not session:
            return BrowserResult(
                status="BLOCKED",
                evidence={"session_id": session_id, "reason_detail": "browser session row disappeared after run()"},
                blocked_reason="session_missing",
            )
        status = session.get("status")
        actions = sessions.list_actions(session_id)
        evidence = {
            "session_id": session_id,
            "status": status,
            "objective": objective,
            "task_type": task_type,
            "step_count": len(steps),
            "actions_recorded": len(actions),
            "last_action": actions[-1] if actions else None,
        }
        if status == BrowserSessionStatus.COMPLETED:
            return BrowserResult(status="COMPLETED", evidence=evidence)
        blocked_reason = "browser_session_not_completed"
        if status == BrowserSessionStatus.NEEDS_ARYAN:
            blocked_reason = session.get("needs_aryan_reason") or "sensitive_action"
            evidence["needs_aryan_reason"] = session.get("needs_aryan_reason")
        elif status == BrowserSessionStatus.FAILED:
            blocked_reason = session.get("error_category") or "browser_session_failed"
            evidence["error"] = session.get("error")
        evidence["reason_detail"] = f"browser session ended with status {status!r}"
        return BrowserResult(status="BLOCKED", evidence=evidence, blocked_reason=blocked_reason)


class BrowserWorker(WorkforceWorker):
    """Handles browser research, web navigation, data collection, and
    forms/admin task types -- the ones that need a real page. Prefers API
    when one is registered (`api_handler`); falls back to the browser
    channel otherwise, per Section 2's API -> browser -> computer
    preference order."""

    name = "browser_worker"
    _SUPPORTED = {"browser_research", "web_navigation", "data_collection", "forms_admin"}

    def __init__(self, channel: Optional[BrowserChannel] = None, api_handler: Optional[Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]] = None):
        self.channel = channel or ManualBrowserChannel()
        self.api_handler = api_handler

    def supports(self, task_type: str) -> bool:
        return task_type in self._SUPPORTED

    def execute(self, task: Dict[str, Any]) -> WorkerResult:
        if self.api_handler is not None:
            api_evidence = self.api_handler(task)
            if api_evidence is not None:
                return WorkerResult(status="COMPLETED", result=api_evidence, evidence={"via": "api", **api_evidence}, execution_method="API")
        result = self.channel.attempt(task)
        if result.status == "COMPLETED":
            return WorkerResult(status="COMPLETED", result=result.evidence, evidence=result.evidence, execution_method="BROWSER")
        return WorkerResult(
            status="BLOCKED", evidence=result.evidence, blockers=[result.blocked_reason or "blocked"],
            next_action="Provide a real, approved browser/API adapter, or complete this step manually.",
            execution_method="BROWSER",
        )


# --------------------------------------------------------------------------
# Research Worker
# --------------------------------------------------------------------------


class ResearchWorker(WorkforceWorker):
    """Reuses the existing research provider shape (`falguna/research.py`'s
    `SourceResult`) rather than inventing a second research pipeline. With
    no provider wired -- the free-first default -- this honestly reports
    BLOCKED instead of fabricating sources; wiring a real provider (a
    search API, a browser-based retrieval, ...) is what turns this on."""

    name = "research_worker"
    _SUPPORTED = {"research", "trend_discovery"}
    # Phase 3 Milestone 3: execute() below writes nothing of its own -- it
    # only calls self.search_provider (a read) and returns a WorkerResult;
    # WorkforceOrchestrator is the one that durably records COMPLETED
    # afterward. There is no half-done state a restart could leave behind
    # (no document, no draft, no browser session), so re-running this from
    # scratch after an interruption is safe and produces an equivalent
    # result -- verified auto-resumable per WorkforceWorker's own docstring.
    auto_resumable_after_restart = True

    def __init__(self, search_provider: Optional[Callable[[str, int], List[Any]]] = None):
        self.search_provider = search_provider

    def supports(self, task_type: str) -> bool:
        return task_type in self._SUPPORTED

    def execute(self, task: Dict[str, Any]) -> WorkerResult:
        if self.search_provider is None:
            return WorkerResult(
                status="BLOCKED", blockers=["no research provider configured"],
                next_action="Wire a real search/research provider (see falguna/research.py) before running research tasks.",
                execution_method="API",
            )
        inputs = json.loads(task["inputs_json"]) if task.get("inputs_json") else {}
        query = inputs.get("query") or task["objective"]
        sources = self.search_provider(query, inputs.get("count", 5))
        if not sources:
            return WorkerResult(status="FAILED", next_action=f"no sources found for query {query!r}", execution_method="API")
        source_list = [{"url": s.url, "title": s.title, "snippet": s.snippet} for s in sources]
        return WorkerResult(
            status="COMPLETED", result={"query": query, "sources": source_list},
            evidence={"query": query, "source_count": len(source_list)},
            confidence="Medium", execution_method="API",
        )


# --------------------------------------------------------------------------
# Data Worker
# --------------------------------------------------------------------------


class DataWorker(WorkforceWorker):
    """Pure local transforms on data already provided as task inputs --
    dedupe/normalize records, or plan a file-organization move. Never
    fetches anything itself (that's BrowserWorker's job); it operates only
    on what is already on file, so it can always verify its own result."""

    name = "data_worker"
    _SUPPORTED = {"data_processing", "file_organization", "crm_admin"}
    # Phase 3 Milestone 3: execute() below is a pure, in-memory transform
    # of task.inputs_json (dedupe against records already given at task
    # creation) -- it never calls a store, a provider, or anything external,
    # so it has literally zero durable side effect to half-complete. Re-
    # running it from scratch after a restart always reproduces the exact
    # same output for the exact same inputs -- verified auto-resumable per
    # WorkforceWorker's own docstring.
    auto_resumable_after_restart = True

    def supports(self, task_type: str) -> bool:
        return task_type in self._SUPPORTED

    def execute(self, task: Dict[str, Any]) -> WorkerResult:
        inputs = json.loads(task["inputs_json"]) if task.get("inputs_json") else {}
        records = inputs.get("records")
        if not records:
            return WorkerResult(status="FAILED", next_action="no records provided in task inputs", execution_method="API")
        key = inputs.get("dedupe_key")
        seen = set()
        deduped = []
        for row in records:
            marker = row.get(key) if key else json.dumps(row, sort_keys=True)
            if marker in seen:
                continue
            seen.add(marker)
            deduped.append(row)
        return WorkerResult(
            status="COMPLETED", result={"records": deduped},
            evidence={"input_count": len(records), "output_count": len(deduped), "removed": len(records) - len(deduped)},
            execution_method="API",
        )


# --------------------------------------------------------------------------
# Document / Spreadsheet Workers
# --------------------------------------------------------------------------


class DocumentWorker(WorkforceWorker):
    name = "document_worker"
    _SUPPORTED = {"document_creation", "document_editing", "presentation_creation"}

    def __init__(self, documents: DocumentStore):
        self.documents = documents

    def supports(self, task_type: str) -> bool:
        return task_type in self._SUPPORTED

    def execute(self, task: Dict[str, Any]) -> WorkerResult:
        inputs = json.loads(task["inputs_json"]) if task.get("inputs_json") else {}
        title = inputs.get("title") or task["objective"]
        content = inputs.get("content_text") or ""
        doc_type = "report" if task["task_type"] == "presentation_creation" else "document"
        doc_id = self.documents.create_document(title, content, doc_type=doc_type, department=task["department"], source_task_id=task["id"], actor="system")
        return WorkerResult(
            status="COMPLETED", result={"doc_id": doc_id}, evidence={"doc_id": doc_id, "title": title, "doc_type": doc_type},
            execution_method="API",
        )


class SpreadsheetWorker(WorkforceWorker):
    name = "spreadsheet_worker"
    _SUPPORTED = {"spreadsheet_creation", "spreadsheet_editing"}

    def __init__(self, documents: DocumentStore):
        self.documents = documents

    def supports(self, task_type: str) -> bool:
        return task_type in self._SUPPORTED

    def execute(self, task: Dict[str, Any]) -> WorkerResult:
        inputs = json.loads(task["inputs_json"]) if task.get("inputs_json") else {}
        columns = inputs.get("columns")
        rows = inputs.get("rows")
        if not columns or rows is None:
            return WorkerResult(status="FAILED", next_action="task inputs must include columns and rows", execution_method="API")
        title = inputs.get("title") or task["objective"]
        doc_id = self.documents.create_spreadsheet(title, columns, rows, department=task["department"], source_task_id=task["id"], actor="system")
        return WorkerResult(
            status="COMPLETED", result={"doc_id": doc_id}, evidence={"doc_id": doc_id, "row_count": len(rows)},
            execution_method="API",
        )


# --------------------------------------------------------------------------
# Email/Admin Worker
# --------------------------------------------------------------------------


class EmailAdminWorker(WorkforceWorker):
    """Drafting is real, safe, and local, so it always completes; sending
    is not this worker's job at all -- see falguna/email_admin.py."""

    name = "email_admin_worker"
    _SUPPORTED = {"email_preparation"}

    def __init__(self, emails: EmailStore):
        self.emails = emails

    def supports(self, task_type: str) -> bool:
        return task_type in self._SUPPORTED

    def execute(self, task: Dict[str, Any]) -> WorkerResult:
        inputs = json.loads(task["inputs_json"]) if task.get("inputs_json") else {}
        body = inputs.get("body")
        if not body:
            return WorkerResult(status="FAILED", next_action="task inputs must include body", execution_method="API")
        draft = self.emails.draft(
            inputs.get("to_address"), inputs.get("subject"), body, department=task["department"], source_task_id=task["id"], actor="system",
        )
        return WorkerResult(
            status="COMPLETED", result={"email_id": draft["id"]}, evidence={"email_id": draft["id"], "status": "PREPARED"},
            execution_method="API",
        )


# --------------------------------------------------------------------------
# Content Worker
# --------------------------------------------------------------------------


class ContentWorker(WorkforceWorker):
    """Deterministic, template-based content-brief drafting for the same
    reason the rest of this codebase's business logic is deterministic
    (falguna/conversations.py's ReplyAgent, revenue_hunter's proposal
    generator): reproducible, explainable, offline. A real model-backed
    generator can be wired in later via the same DocumentStore output shape
    without changing anything upstream of this worker."""

    name = "content_worker"
    _SUPPORTED = {"content_operations"}

    def __init__(self, documents: DocumentStore):
        self.documents = documents

    def supports(self, task_type: str) -> bool:
        return task_type in self._SUPPORTED

    def execute(self, task: Dict[str, Any]) -> WorkerResult:
        inputs = json.loads(task["inputs_json"]) if task.get("inputs_json") else {}
        title = inputs.get("title") or task["objective"]
        brief = f"# {title}\n\nObjective: {task['objective']}\n"
        if inputs.get("notes"):
            brief += f"\nNotes: {inputs['notes']}\n"
        doc_id = self.documents.create_document(title, brief, doc_type="notes", department=task["department"], source_task_id=task["id"], actor="system")
        return WorkerResult(
            status="COMPLETED", result={"doc_id": doc_id}, evidence={"doc_id": doc_id, "title": title},
            execution_method="API",
        )
