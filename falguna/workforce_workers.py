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

from .documents import DocumentStore
from .email_admin import EmailStore
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
