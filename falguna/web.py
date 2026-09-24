import json
import os
import re
import secrets
import shutil
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .attachments import AttachmentError, AttachmentStore
from .browser_planner import BrowserSettingsStore, plan_steps_from_objective
from .browser_runtime import (
    ACTIVELY_RUNNING_STATUSES as BROWSER_ACTIVELY_RUNNING_STATUSES,
    ALL_ACTION_TYPES as BROWSER_ALL_ACTION_TYPES,
    BrowserRuntimeError,
    BrowserSessionStatus,
    BrowserSessionStore,
    PlaywrightBrowserRuntime,
    TERMINAL_STATUSES as BROWSER_TERMINAL_STATUSES,
    classify_sensitive_action,
)
from .chat import ChatError, ChatResponder, ConversationStore, search_missions
from .computer_use import build_computer_channel, computer_use_available
from .codex_transport import CodexCliJSONTransport, DEFAULT_CODEX_MODEL, ResilientCodexTransport, SUPPORTED_CODEX_MODELS
from .continuity import ProjectUnderstandingCache, browser_e2e_applicable, resolve_continuation
from .discovery import ProjectDiscovery
from .gateway import OpenAICompatibleGateway
from .memory import (
    KnowledgeError, KnowledgeStore, MemoryConflict, MemoryError, MemorySettingsStore, MemoryStore,
    MemorySuggestionStore, assemble_chat_context, build_embedding_adapter, embedding_status,
)
from .model_router import (
    DEFAULT_REGISTRY_SETTINGS, ModelRegistry, ModelRouter, PRIVACY_MODES, build_providers, provider_status_snapshot,
)
from .models import CommandSpec, RunPolicy
from .notifications import NotificationStore
from .providers import ALL_ERROR_CATEGORIES, ErrorCategory, FalgunaModelError
from .research import ResearchError, ResearchResponder, ResearchStore, rank_sources
from .review import ModelSemanticReviewer
from .runtime import open_control_plane
from .search_providers import DuckDuckGoHTMLSearchProvider
from .store import utcnow
from .usability import MILESTONES, evidence_summary, mission_view
from .workers import StructuredEditWorker


MODEL = DEFAULT_CODEX_MODEL
# Falguna Search's default provider. Swappable in one line -- research.py's
# abstraction (SearchProvider/ProviderResult/SourceResult) never imports or
# knows about this concrete class, so replacing DuckDuckGoHTMLSearchProvider
# with a paid web-search API, browser-based retrieval, a provider-native
# search tool, or a self-hosted index touches only this one assignment.
SEARCH_PROVIDER = DuckDuckGoHTMLSearchProvider()
_operations = {}
_operations_lock = threading.Lock()

# Product Experience V2 -- Work Mode (Section 6). Falguna does not control
# any model's internal reasoning depth and never claims to; these three
# modes instead adjust the real knobs the system already has: how long a
# call is allowed to run, whether a failed/unsupported model attempt is
# allowed to fall back to another configured model (ResilientCodexTransport),
# and -- for Work missions only -- how many bounded repair attempts a run
# gets (RunPolicy.max_attempts, unchanged from today's default under
# BALANCED). Fast trades resilience for a quick, single-attempt answer;
# Deep trades time for more budget and more retries.
DEFAULT_WORK_MODE = "BALANCED"
WORK_MODE_SETTINGS = {
    "FAST": {"chat_timeout": 30, "research_timeout": 30, "mission_timeout": 150, "max_attempts": 1, "fallback": False},
    "BALANCED": {"chat_timeout": 60, "research_timeout": 60, "mission_timeout": 300, "max_attempts": 2, "fallback": True},
    "DEEP": {"chat_timeout": 120, "research_timeout": 120, "mission_timeout": 600, "max_attempts": 3, "fallback": True},
}


def _work_mode(value) -> str:
    value = str(value or "").upper()
    return value if value in WORK_MODE_SETTINGS else DEFAULT_WORK_MODE


def _model_choice(value) -> str:
    return value if value in SUPPORTED_CODEX_MODELS else MODEL


def _build_transport(codex_path, codex_home, timeout_seconds: int, use_fallback: bool):
    base = CodexCliJSONTransport(Path(codex_path), codex_home, timeout_seconds=timeout_seconds)
    return ResilientCodexTransport(base) if use_fallback else base


def _build_router(store, use_fallback: bool = True) -> ModelRouter:
    """Falguna V2.1: the provider-neutral seam Chat/Research/Work now all
    construct their transport through, instead of hard-coding a Codex-only
    OpenAICompatibleGateway+CodexCliJSONTransport pair. `_build_transport`
    above is kept only because a couple of narrow call sites (mission
    resume) still use it directly against Codex specifically; every other
    call site below goes through this router so a local (Ollama) or
    explicitly-enabled external provider is tried using the same
    Explicit -> Preferred-local -> Other-local -> External -> None policy,
    honoring whatever Privacy Mode is currently configured."""
    return ModelRouter.from_registry(ModelRegistry(store), codex_use_fallback=use_fallback)


class DiscoveryUncertain(ValueError):
    """Discovery could not confidently bound the change; carries evidence for the 409 response."""

    def __init__(self, message, evidence):
        super().__init__(message)
        self.evidence = evidence


def load_profiles(app_root: Path) -> list:
    raw = json.loads(Path(__file__).with_name("project_profiles.json").read_text())
    profiles = []
    for item in raw:
        profile = dict(item)
        repository = Path(profile["repository"])
        profile["repository"] = str((app_root / repository).resolve() if not repository.is_absolute() else repository.resolve())
        if not Path(profile["repository"]).is_dir() or not (Path(profile["repository"]) / ".git").exists():
            continue
        profiles.append(profile)
    return profiles


def validate_editable(values: list) -> list:
    cleaned = []
    for value in values:
        value = value.strip()
        path = Path(value)
        if not value or path.is_absolute() or ".." in path.parts:
            raise ValueError("Editable paths must be non-empty paths inside the selected project")
        cleaned.append(value)
    if not cleaned:
        raise ValueError("At least one editable path is required")
    return cleaned


def _browser_session_bucket(status: str, archived: bool) -> str:
    """Falguna Browser + Computer Use V1 (Section 25): the exact same board
    bucket vocabulary Engineering Worker runs already occupy
    (running/needs_you/completed/failed/cancelled/archived) -- a browser
    session is never a second, parallel task system, only a second `kind`
    of card feeding the same buckets."""
    if archived and status not in BROWSER_ACTIVELY_RUNNING_STATUSES:
        return "archived"
    if status in BROWSER_ACTIVELY_RUNNING_STATUSES:  # CREATED, RUNNING, WAITING
        return "running"
    if status in (BrowserSessionStatus.NEEDS_ARYAN, BrowserSessionStatus.PAUSED):
        return "needs_you"
    if status == BrowserSessionStatus.FAILED:
        return "failed"
    if status == BrowserSessionStatus.CANCELLED:
        return "cancelled"
    return "completed"


def _resolve_project_name(project_id, profiles_by_id: dict):
    if not project_id:
        return None
    profile = profiles_by_id.get(project_id)
    return profile["name"] if profile else None


def _browser_session_card(row: dict, chat_store, research_store, evidence_count: int, profiles_by_id: dict) -> dict:
    origin = "Direct"
    if row.get("conversation_id") and chat_store.get_conversation(row["conversation_id"]):
        origin = "Chat"
    elif row.get("research_id"):
        origin = "Search"
    title = (row.get("objective") or "Browser task").strip()
    # Computer-use sessions live in this exact same browser_sessions table
    # (Section: "never a parallel task system" applies just as much to
    # computer-use as it did to browser) -- task_type is the only thing
    # that distinguishes them, so Mission Control's `kind` field derives
    # from it rather than a new column.
    kind = "computer" if row.get("task_type") == "computer_use" else "browser"
    project_id = row.get("project_id")
    project_name = _resolve_project_name(project_id, profiles_by_id)
    return {
        "id": row["id"], "run_id": row["id"], "kind": kind, "title": title[:120],
        "status": row["status"], "project": project_name or project_id or "—", "project_id": project_id,
        "worker": kind, "model": None, "origin": origin, "started_at": row["created_at"],
        "last_activity": row["updated_at"], "cost_usd": 0.0, "evidence_count": evidence_count,
        "merge_approval": "NOT_REQUIRED",
        "current_step": row.get("current_url") or row.get("needs_aryan_reason") or row.get("task_type") or row["status"],
        "archived": bool(row.get("mc_archived")), "needs_aryan_reason": row.get("needs_aryan_reason"),
        "error": row.get("error"), "headless": bool(row.get("headless")),
    }


class FalgunaHandler(BaseHTTPRequestHandler):
    server_version = "FalgunaLocal/1.2"

    def log_message(self, format, *args):
        return

    @property
    def app_root(self):
        return self.server.app_root

    # ------------------------------------------------------------------ GET

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            return self._html(INDEX_HTML)
        if path == "/api/config":
            profiles = load_profiles(self.app_root)
            control, store = open_control_plane(self.app_root)
            try:
                privacy_mode = ModelRegistry(store).load()["privacy_mode"]
            finally:
                store.close()
            return self._json({
                "product": "Falguna Engineering", "stage": "internal alpha", "profiles": profiles, "model": MODEL,
                "available_models": sorted(SUPPORTED_CODEX_MODELS), "codex_runtime_found": bool(shutil.which("codex")),
                "work_modes": list(WORK_MODE_SETTINGS), "default_work_mode": DEFAULT_WORK_MODE,
                # Falguna V2.1: Codex remains listed above for full backward
                # compatibility (every conversation's model_override is still
                # a bare Codex model id unless the person explicitly picks a
                # provider/model pair), but it is no longer the only runtime
                # -- see /api/models for the full, live, multi-provider view.
                "privacy_mode": privacy_mode,
            })
        if path == "/api/settings":
            return self._settings()
        if path == "/api/models":
            return self._models_status()
        if path == "/api/search":
            query = parse_qs(parsed.query).get("q", [""])[0]
            return self._search(query)
        if path == "/api/missions/board":
            return self._missions_board()
        if path == "/api/browser/sessions":
            return self._list_browser_sessions()
        if path == "/api/browser/settings":
            return self._browser_settings_get()
        if path.startswith("/api/browser/sessions/"):
            session_id = path.rsplit("/", 1)[-1]
            return self._get_browser_session(session_id)
        if path == "/api/computer/status":
            return self._computer_status()
        if path == "/api/live-summary":
            return self._live_summary()
        if path == "/api/usage":
            return self._usage()
        if path == "/api/files":
            return self._files()
        if path == "/api/notifications":
            return self._list_notifications()
        if path.startswith("/api/attachments/"):
            attachment_id = path.rsplit("/", 1)[-1]
            return self._download_attachment(attachment_id)
        if path == "/api/research":
            return self._list_research()
        if path.startswith("/api/research/"):
            research_id = path.rsplit("/", 1)[-1]
            return self._get_research(research_id)
        if path == "/api/conversations":
            return self._list_conversations()
        if path.startswith("/api/conversations/"):
            conversation_id = path.rsplit("/", 1)[-1]
            return self._get_conversation(conversation_id)
        if path == "/api/memory":
            return self._list_memory(parse_qs(parsed.query))
        if path == "/api/memory/settings":
            return self._memory_settings_get()
        if path == "/api/memory/suggestions":
            return self._list_memory_suggestions(parse_qs(parsed.query))
        if path.startswith("/api/memory/") and path.endswith("/history"):
            return self._memory_history(path.split("/")[3])
        if path.startswith("/api/memory/"):
            return self._get_memory(path.rsplit("/", 1)[-1])
        if path == "/api/knowledge/documents":
            return self._list_knowledge_documents(parse_qs(parsed.query))
        if path.startswith("/api/knowledge/documents/"):
            return self._get_knowledge_document(path.rsplit("/", 1)[-1])
        if path == "/api/runs":
            control, store = open_control_plane(self.app_root)
            try:
                recent = []
                for run in reversed(store.list("runs")):
                    task = store.get("tasks", run["task_id"])
                    requirement = store.get("requirements", task["requirement_id"])
                    mission = store.get("missions", requirement["mission_id"])
                    recent.append({"run_id": run["id"], "title": mission["title"], "status": run["status"], "updated_at": run["updated_at"], "repository": task["repository"]})
                    if len(recent) == 30:
                        break
                return self._json({"runs": recent})
            finally:
                store.close()
        if path.startswith("/api/operations/"):
            token = path.rsplit("/", 1)[-1]
            with _operations_lock:
                operation = dict(_operations.get(token, {}))
            return self._json(operation or {"error": "operation not found"}, HTTPStatus.OK if operation else HTTPStatus.NOT_FOUND)
        if path.startswith("/api/runs/"):
            run_id = path.rsplit("/", 1)[-1]
            control, store = open_control_plane(self.app_root)
            try:
                run = store.get("runs", run_id)
                if run:
                    task = store.get("tasks", run["task_id"])
                    raw = json.loads(task["policy_json"])
                    raw["verification_commands"] = [CommandSpec(**item) for item in raw.get("verification_commands", [])]
                    control.reconcile_recovery_state(run_id, RunPolicy(**raw))
                view = mission_view(store, self.app_root / ".falguna", run_id)
                if view["status"] in {"DONE_CANDIDATE", "FAILED", "QUARANTINED", "CANCELLED"}:
                    view = evidence_summary(store, self.app_root / ".falguna", control.audit, run_id)
                view["conversation_handoffs"] = ConversationStore(store).handoffs_for_run(run_id)
                view["research_handoffs"] = ResearchStore(store).handoffs_for_run(run_id)
                return self._json(view)
            except ValueError as exc:
                return self._json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
            finally:
                store.close()
        return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def _settings(self):
        profiles = load_profiles(self.app_root)
        return self._json({
            "product": "Falguna Engineering",
            "model": MODEL,
            "profiles": [
                {"id": p["id"], "name": p["name"], "repository": p["repository"], "risk": p.get("risk", ""), "default_budget_usd": p.get("default_budget_usd", 0)}
                for p in profiles
            ],
            "boundaries": [
                "Isolated Git worktree per mission; repository-root filesystem boundary enforced",
                "Deny-by-default write and command policy; protected paths cannot be edited",
                "Native verification, plus optional localhost-only browser verification",
                "Independent semantic review before a mission can reach DONE_CANDIDATE",
                "Durable checkpoint/resume; a forced interruption recovers safely",
                "Hash-chained append-only audit log",
                "Human-only merge decision -- Falguna never merges or deploys",
                "Chat has no tools and cannot edit a repository; only a human handoff can start a mission",
                "Search treats retrieved web content as untrusted data, never as instructions; only http(s) sources are ever kept",
            ],
        })

    def _models_status(self):
        """Settings -> Models (Falguna V2.1): live health + model list for
        every currently-enabled provider, plus the persisted registry
        (privacy mode, preferred local model, provider config) -- safe to
        return verbatim, since ModelRegistry.public_view() and
        provider_status_snapshot() never include a secret value, only the
        name of the environment variable a credential would come from."""
        control, store = open_control_plane(self.app_root)
        try:
            registry = ModelRegistry(store)
            settings = registry.load()
            providers = build_providers(settings)
            return self._json({
                "privacy_modes": list(PRIVACY_MODES),
                "registry": registry.public_view(),
                "providers": provider_status_snapshot(providers),
            })
        finally:
            store.close()

    def _update_model_settings(self, body):
        """Explicit-only provider/privacy configuration (Section: no silent
        external default). Every field the person did not send is left
        untouched -- this is a merge onto the persisted registry, never a
        blind overwrite, so toggling Privacy Mode from Settings can never
        accidentally wipe out a Provider Setup the person configured
        earlier in the same session."""
        control, store = open_control_plane(self.app_root)
        try:
            registry = ModelRegistry(store)
            settings = registry.load()
            if "privacy_mode" in body:
                mode = str(body.get("privacy_mode") or "").upper()
                if mode not in PRIVACY_MODES:
                    raise ValueError(f"privacy_mode must be one of {', '.join(PRIVACY_MODES)}")
                settings["privacy_mode"] = mode
            if "preferred_local_model" in body:
                value = body.get("preferred_local_model")
                settings["preferred_local_model"] = (str(value).strip() or None) if value is not None else None
            incoming_providers = body.get("providers")
            if isinstance(incoming_providers, dict):
                for provider_id, patch in incoming_providers.items():
                    if not isinstance(patch, dict):
                        continue
                    current = settings["providers"].setdefault(provider_id, {})
                    for key in ("enabled", "base_url", "is_local", "api_key_env", "model_allowlist", "display_name"):
                        if key in patch:
                            current[key] = patch[key]
            registry.save(settings)
            return self._json({"registry": registry.public_view()})
        finally:
            store.close()

    def _search(self, query):
        control, store = open_control_plane(self.app_root)
        try:
            chat = ConversationStore(store)
            results = chat.search_conversations(query) + search_missions(store, query)
            return self._json({"query": query, "results": results})
        finally:
            store.close()

    def _list_research(self):
        control, store = open_control_plane(self.app_root)
        try:
            return self._json({"research": ResearchStore(store).list_queries()})
        finally:
            store.close()

    def _get_research(self, research_id):
        control, store = open_control_plane(self.app_root)
        try:
            rs = ResearchStore(store)
            query = rs.get_query(research_id)
            if not query:
                return self._json({"error": "research query not found"}, HTTPStatus.NOT_FOUND)
            return self._json({
                "research": query,
                "sources": rs.get_sources(research_id),
                "citations": rs.get_citations(research_id),
                "handoffs": rs.list_handoffs(research_id),
            })
        finally:
            store.close()

    def _list_conversations(self):
        control, store = open_control_plane(self.app_root)
        try:
            return self._json({"conversations": ConversationStore(store).list_conversations()})
        finally:
            store.close()

    def _get_conversation(self, conversation_id):
        control, store = open_control_plane(self.app_root)
        try:
            chat = ConversationStore(store)
            conversation = chat.get_conversation(conversation_id)
            if not conversation:
                return self._json({"error": "conversation not found"}, HTTPStatus.NOT_FOUND)
            return self._json({
                "conversation": conversation,
                "messages": chat.list_messages(conversation_id),
                "handoffs": chat.list_handoffs(conversation_id),
                "attachments": AttachmentStore(store, self.app_root).list_for_conversation(conversation_id),
            })
        finally:
            store.close()

    # -------------------------------------------- Memory & Knowledge V2

    def _memory_scope_registry(self, store):
        """Every scope Aryan (the sole operator) is authorized to see:
        personal + company (both unscoped) plus every currently-APPROVED
        project (project_profiles.json, via the existing load_profiles --
        never a second registry) plus every venture Venture Studio already
        knows about (vs_ventures). Returns (all_scopes, known_project_ids)."""
        project_ids = {p["id"] for p in load_profiles(self.app_root)}
        venture_ids = {v["id"] for v in store.list("vs_ventures")}
        scopes = [("personal", None), ("company", None)]
        scopes += [("project", pid) for pid in sorted(project_ids)]
        scopes += [("venture", vid) for vid in sorted(venture_ids)]
        return scopes, project_ids

    def _resolve_requested_scopes(self, store, params: dict):
        """Query-param scope filter for the Memory/Knowledge UI:
        ?scope_type=project&scope_id=falguna-engineering restricts to
        exactly that one scope; no filter means every scope this operator
        is authorized to see. A requested scope that is not in the
        authorized registry is simply dropped -- never silently widened."""
        all_scopes, project_ids = self._memory_scope_registry(store)
        scope_type = (params.get("scope_type") or [None])[0]
        scope_id = (params.get("scope_id") or [None])[0]
        if not scope_type:
            return all_scopes, project_ids
        requested = (scope_type, scope_id or None)
        return ([requested] if requested in all_scopes else []), project_ids

    def _list_memory(self, params: dict):
        control, store = open_control_plane(self.app_root)
        try:
            scopes, _ = self._resolve_requested_scopes(store, params)
            mem = MemoryStore(store, control.audit)
            query = (params.get("q") or [None])[0]
            kind = (params.get("kind") or [None])[0]
            state = (params.get("state") or ["active"])[0]
            pinned_only = (params.get("pinned") or [""])[0] == "1"
            if query:
                results = mem.search(scopes, query, limit=int((params.get("limit") or [20])[0]))
            else:
                results = mem.list(scopes, kind=kind, state=state, pinned_only=pinned_only, limit=int((params.get("limit") or [200])[0]))
            return self._json({"records": results, "scopes": [{"scope_type": t, "scope_id": s} for t, s in scopes]})
        finally:
            store.close()

    def _get_memory(self, record_id):
        control, store = open_control_plane(self.app_root)
        try:
            row = MemoryStore(store, control.audit).get(record_id)
            if not row:
                return self._json({"error": "memory record not found"}, HTTPStatus.NOT_FOUND)
            return self._json(row)
        finally:
            store.close()

    def _memory_history(self, record_id):
        control, store = open_control_plane(self.app_root)
        try:
            return self._json({"history": MemoryStore(store, control.audit).history(record_id)})
        finally:
            store.close()

    def _create_memory(self, body):
        control, store = open_control_plane(self.app_root)
        try:
            _, project_ids = self._memory_scope_registry(store)
            mem = MemoryStore(store, control.audit)
            record = mem.save(
                scope_type=body.get("scope_type", "personal"), scope_id=body.get("scope_id"),
                kind=body.get("kind", "fact"), content=body.get("content", ""),
                source_type=body.get("source_type", "user_stated"), source_ref=body.get("source_ref"),
                confidence=body.get("confidence", "user_provided"), actor=body.get("actor") or "Aryan",
                sensitivity=body.get("sensitivity", "normal"), supersedes_id=body.get("supersedes_id"),
                allow_conflict=bool(body.get("allow_conflict", False)), known_project_ids=project_ids,
            )
            return self._json(record, HTTPStatus.CREATED)
        finally:
            store.close()

    def _supersede_memory(self, record_id, body):
        control, store = open_control_plane(self.app_root)
        try:
            _, project_ids = self._memory_scope_registry(store)
            mem = MemoryStore(store, control.audit)
            existing = mem.get(record_id)
            if not existing:
                return self._json({"error": "memory record not found"}, HTTPStatus.NOT_FOUND)
            record = mem.save(
                scope_type=existing["scope_type"], scope_id=existing["scope_id"], kind=body.get("kind", existing["kind"]),
                content=body.get("content", ""), source_type=body.get("source_type", existing["source_type"]),
                source_ref=body.get("source_ref"), confidence=body.get("confidence", existing["confidence"]),
                actor=body.get("actor") or "Aryan", sensitivity=body.get("sensitivity", existing["sensitivity"]),
                supersedes_id=record_id, known_project_ids=project_ids,
            )
            return self._json(record, HTTPStatus.CREATED)
        finally:
            store.close()

    def _pin_memory(self, record_id, body):
        control, store = open_control_plane(self.app_root)
        try:
            record = MemoryStore(store, control.audit).pin(record_id, bool(body.get("pinned", True)), body.get("actor") or "Aryan")
            return self._json(record)
        finally:
            store.close()

    def _edit_memory(self, record_id, body):
        control, store = open_control_plane(self.app_root)
        try:
            record = MemoryStore(store, control.audit).edit(record_id, body.get("content", ""), body.get("actor") or "Aryan")
            return self._json(record)
        finally:
            store.close()

    def _forget_memory(self, record_id, body):
        control, store = open_control_plane(self.app_root)
        try:
            record = MemoryStore(store, control.audit).forget(record_id, body.get("actor") or "Aryan", body.get("reason", ""))
            return self._json(record)
        finally:
            store.close()

    def _purge_memory(self, record_id):
        control, store = open_control_plane(self.app_root)
        try:
            record = MemoryStore(store, control.audit).purge(record_id, "Aryan")
            return self._json(record)
        finally:
            store.close()

    def _memory_settings_get(self):
        control, store = open_control_plane(self.app_root)
        try:
            return self._json(embedding_status(store))
        finally:
            store.close()

    def _memory_settings_update(self, body):
        control, store = open_control_plane(self.app_root)
        try:
            settings_store = MemorySettingsStore(store)
            settings_store.save({k: v for k, v in body.items() if k in ("embedding_provider", "ollama_base_url", "ollama_embedding_model")})
            return self._json(embedding_status(store))
        finally:
            store.close()

    def _list_memory_suggestions(self, params: dict):
        control, store = open_control_plane(self.app_root)
        try:
            scopes, _ = self._resolve_requested_scopes(store, params)
            mem = MemoryStore(store, control.audit)
            suggestions = MemorySuggestionStore(store, control.audit, mem)
            return self._json({"suggestions": suggestions.list_pending(scopes)})
        finally:
            store.close()

    def _accept_memory_suggestion(self, suggestion_id, body):
        control, store = open_control_plane(self.app_root)
        try:
            mem = MemoryStore(store, control.audit)
            suggestions = MemorySuggestionStore(store, control.audit, mem)
            record = suggestions.accept(suggestion_id, body.get("actor") or "Aryan", confidence=body.get("confidence", "user_provided"))
            return self._json(record, HTTPStatus.CREATED)
        finally:
            store.close()

    def _dismiss_memory_suggestion(self, suggestion_id):
        control, store = open_control_plane(self.app_root)
        try:
            mem = MemoryStore(store, control.audit)
            suggestions = MemorySuggestionStore(store, control.audit, mem)
            return self._json(suggestions.dismiss(suggestion_id, "Aryan"))
        finally:
            store.close()

    def _list_knowledge_documents(self, params: dict):
        control, store = open_control_plane(self.app_root)
        try:
            scopes, _ = self._resolve_requested_scopes(store, params)
            kb = KnowledgeStore(store, control.audit)
            status = (params.get("status") or [None])[0]
            return self._json({"documents": kb.list_documents(scopes, status=status)})
        finally:
            store.close()

    def _get_knowledge_document(self, document_id):
        control, store = open_control_plane(self.app_root)
        try:
            kb = KnowledgeStore(store, control.audit)
            doc = kb.get_document(document_id)
            if not doc:
                return self._json({"error": "document not found"}, HTTPStatus.NOT_FOUND)
            return self._json({"document": doc, "chunks": kb.get_chunks(document_id)})
        finally:
            store.close()

    def _ingest_knowledge_document(self, body):
        import base64
        control, store = open_control_plane(self.app_root)
        try:
            _, project_ids = self._memory_scope_registry(store)
            data_b64 = body.get("data_base64", "")
            try:
                data = base64.b64decode(data_b64, validate=True) if data_b64 else (body.get("content", "") or "").encode("utf-8")
            except Exception as exc:
                raise KnowledgeError("data_base64 must be valid base64") from exc
            embedding_adapter = build_embedding_adapter(MemorySettingsStore(store).load())
            kb = KnowledgeStore(store, control.audit, embedding_adapter=embedding_adapter)
            doc = kb.ingest_bytes(
                filename=body.get("filename", "untitled.txt"), mime_type=body.get("mime_type", "text/plain"), data=data,
                scope_type=body.get("scope_type", "personal"), source_type=body.get("source_type", "upload"),
                actor=body.get("actor") or "Aryan", scope_id=body.get("scope_id"), source_ref=body.get("source_ref"),
                known_project_ids=project_ids, title=body.get("title"),
            )
            return self._json(doc, HTTPStatus.CREATED)
        finally:
            store.close()

    def _forget_knowledge_document(self, document_id, body):
        control, store = open_control_plane(self.app_root)
        try:
            kb = KnowledgeStore(store, control.audit)
            doc = kb.forget_document(document_id, body.get("actor") or "Aryan", body.get("reason", ""))
            return self._json(doc)
        finally:
            store.close()

    # ------------------------------------------------------- Product V2: GET

    def _missions_board(self):
        """Mission Control (Section 7): every run bucketed by real status --
        never a UI-only classification. `needs_you` is PAUSED or a
        DONE_CANDIDATE run whose merge decision is still PENDING (Falguna's
        equivalent of a Needs-Aryan item, Section 32); nothing here is
        merged, deployed, or otherwise decided by this endpoint."""
        control, store = open_control_plane(self.app_root)
        try:
            profiles_by_repo = {p["repository"]: p["name"] for p in load_profiles(self.app_root)}
            chat_store = ConversationStore(store)
            research_store = ResearchStore(store)
            # Falguna V2.1: an "archived" bucket for a run the person has
            # explicitly dismissed from the active board (Settings has no
            # equivalent -- this is a per-run action from the Mission
            # Control card itself). Archiving never deletes anything and
            # never counts as a merge decision; it only moves a resolved
            # run's card out of the headline active-count buckets, which is
            # what the "0 running / 27 needs you / 55 failed" confusion was
            # actually about -- every run this repo has ever produced was
            # counted forever, with no way to say "seen, done with this".
            board = {"running": [], "needs_you": [], "completed": [], "failed": [], "cancelled": [], "archived": []}
            for run in reversed(store.list("runs")[-200:]):
                task = store.get("tasks", run["task_id"])
                requirement = store.get("requirements", task["requirement_id"])
                mission = store.get("missions", requirement["mission_id"])
                costs = store.list("cost_events", "run_id=?", (run["id"],))
                artifacts = store.list("artifacts", "run_id=?", (run["id"],))
                approvals = store.list("approvals", "run_id=? AND kind=?", (run["id"], "PROTECTED_BRANCH_MERGE"))
                merge_status = approvals[-1]["status"] if approvals else "NOT_REQUESTED"
                origin = "Direct"
                if chat_store.handoffs_for_run(run["id"]):
                    origin = "Chat"
                elif research_store.handoffs_for_run(run["id"]):
                    origin = "Search"
                checkpoints = store.list("checkpoints", "run_id=?", (run["id"],))
                current_step = MILESTONES.get(checkpoints[-1]["stage"], run["status"]) if checkpoints else run["status"]
                card = {
                    "id": run["id"], "kind": "engineering",
                    "run_id": run["id"], "title": mission["title"], "status": run["status"],
                    "project": profiles_by_repo.get(task["repository"], task["repository"]),
                    "worker": run["worker"], "model": run["model"], "origin": origin,
                    "started_at": run["created_at"], "last_activity": run["updated_at"],
                    "cost_usd": round(sum(float(c["amount_usd"]) for c in costs), 8),
                    "evidence_count": len(artifacts), "merge_approval": merge_status, "current_step": current_step,
                    "archived": bool(run.get("mc_archived")),
                }
                is_actively_running = run["status"] in {"PLANNING", "WORKING", "VERIFYING", "REVIEWING"}
                if card["archived"] and not is_actively_running:
                    # An actively-running mission is never archived even if
                    # the flag was somehow set (defensive: it must stay
                    # visible and monitorable while real work is happening).
                    board["archived"].append(card)
                elif is_actively_running:
                    board["running"].append(card)
                elif run["status"] == "PAUSED" or (run["status"] == "DONE_CANDIDATE" and merge_status == "PENDING"):
                    board["needs_you"].append(card)
                elif run["status"] in {"FAILED", "QUARANTINED"}:
                    board["failed"].append(card)
                elif run["status"] == "CANCELLED":
                    board["cancelled"].append(card)
                else:
                    board["completed"].append(card)

            # Falguna Browser + Computer Use V1 (Section 25): browser
            # sessions are a second `kind` of card feeding the SAME buckets
            # above, never a second board. A brand-new database (or one on
            # a Falguna build predating this table) has no browser_sessions
            # table entries -- store.list() over an existing, migrated
            # table just returns [] in that case, so this never needs a
            # feature flag to stay safe.
            browser_sessions = BrowserSessionStore(store)
            profiles_by_id = {p["id"]: p for p in load_profiles(self.app_root)}
            for row in browser_sessions.list()[:200]:
                actions = browser_sessions.list_actions(row["id"])
                downloads = browser_sessions.list_downloads(row["id"])
                evidence_count = len([a for a in actions if a.get("screenshot_attachment_id")]) + len(downloads)
                card = _browser_session_card(row, chat_store, research_store, evidence_count, profiles_by_id)
                board[_browser_session_bucket(row["status"], bool(row.get("mc_archived")))].append(card)
            return self._json(board)
        finally:
            store.close()

    def _archive_run_from_board(self, run_id, archived):
        """Mission Control's dismiss/restore action (Falguna V2.1). Purely a
        board-visibility flag -- never a merge/reject/approve decision, and
        the run row, checkpoints, approvals, and audit log are completely
        untouched either way, so an archived run is still fully reachable
        (the Archived bucket, or by run id directly) and nothing about its
        history is lost."""
        control, store = open_control_plane(self.app_root)
        try:
            run = store.get("runs", run_id)
            if not run:
                return self._json({"error": "run not found"}, HTTPStatus.NOT_FOUND)
            if archived and run["status"] in {"PLANNING", "WORKING", "VERIFYING", "REVIEWING"}:
                return self._json({"error": "An actively running mission can't be archived -- pause or wait for it to finish first"}, HTTPStatus.BAD_REQUEST)
            store.update("runs", run_id, mc_archived=1 if archived else 0)
            return self._json({"run_id": run_id, "archived": bool(archived)})
        finally:
            store.close()

    def _live_summary(self):
        control, store = open_control_plane(self.app_root)
        try:
            running = needs_you = failed = 0
            for run in store.list("runs")[-200:]:
                # Falguna V2.1: an archived run (dismissed from the Mission
                # Control board, never deleted -- see _archive_run_from_board)
                # is excluded from this header indicator too, for the same
                # reason: this count is supposed to mean "needs your
                # attention right now", not "every run of this kind that has
                # ever existed". An actively-running mission is exempted from
                # archiving itself, so it is never wrongly excluded here.
                if run.get("mc_archived") and run["status"] not in {"PLANNING", "WORKING", "VERIFYING", "REVIEWING"}:
                    continue
                approvals = store.list("approvals", "run_id=? AND kind=?", (run["id"], "PROTECTED_BRANCH_MERGE"))
                merge_status = approvals[-1]["status"] if approvals else "NOT_REQUESTED"
                if run["status"] in {"PLANNING", "WORKING", "VERIFYING", "REVIEWING"}:
                    running += 1
                elif run["status"] == "PAUSED" or (run["status"] == "DONE_CANDIDATE" and merge_status == "PENDING"):
                    needs_you += 1
                elif run["status"] in {"FAILED", "QUARANTINED"}:
                    failed += 1
            for row in BrowserSessionStore(store).list()[:200]:
                if row.get("mc_archived") and row["status"] not in BROWSER_ACTIVELY_RUNNING_STATUSES:
                    continue
                bucket = _browser_session_bucket(row["status"], False)
                if bucket == "running":
                    running += 1
                elif bucket == "needs_you":
                    needs_you += 1
                elif bucket == "failed":
                    failed += 1
            return self._json({
                "running": running, "needs_you": needs_you, "failed": failed,
                "unread_notifications": NotificationStore(store).unread_count(),
            })
        finally:
            store.close()

    # -- Falguna Browser + Computer Use V1 (Sections 3, 22, 25) --

    def _browser_session_summary(self, row: dict, profiles_by_id: dict = None) -> dict:
        if profiles_by_id is None:
            profiles_by_id = {p["id"]: p for p in load_profiles(self.app_root)}
        project_id = row.get("project_id")
        return {
            "session_id": row["id"], "objective": row["objective"], "task_type": row["task_type"],
            "kind": "computer" if row.get("task_type") == "computer_use" else "browser",
            "project_id": project_id, "project_name": _resolve_project_name(project_id, profiles_by_id),
            "status": row["status"], "current_url": row.get("current_url"),
            "headless": bool(row.get("headless")), "privacy_mode": row.get("privacy_mode"),
            "needs_aryan_reason": row.get("needs_aryan_reason"), "error": row.get("error"),
            "error_category": row.get("error_category"), "next_step_index": row.get("next_step_index", 0),
            "conversation_id": row.get("conversation_id"), "research_id": row.get("research_id"),
            "created_at": row["created_at"], "started_at": row.get("started_at"),
            "completed_at": row.get("completed_at"), "updated_at": row.get("updated_at"),
            "archived": bool(row.get("mc_archived")),
        }

    def _list_browser_sessions(self):
        control, store = open_control_plane(self.app_root)
        try:
            sessions = BrowserSessionStore(store)
            profiles_by_id = {p["id"]: p for p in load_profiles(self.app_root)}
            return self._json({"sessions": [self._browser_session_summary(row, profiles_by_id) for row in sessions.list()]})
        finally:
            store.close()

    def _get_browser_session(self, session_id):
        control, store = open_control_plane(self.app_root)
        try:
            sessions = BrowserSessionStore(store)
            row = sessions.get(session_id)
            if not row:
                return self._json({"error": "browser session not found"}, HTTPStatus.NOT_FOUND)
            view = self._browser_session_summary(row)
            view["plan"] = json.loads(row["plan_json"]) if row.get("plan_json") else []
            view["tabs"] = sessions.list_tabs(session_id)
            view["actions"] = sessions.list_actions(session_id)
            view["downloads"] = sessions.list_downloads(session_id)
            latest_shot = sessions.latest_action_with_screenshot(session_id)
            view["latest_screenshot_attachment_id"] = latest_shot["screenshot_attachment_id"] if latest_shot else None
            return self._json(view)
        finally:
            store.close()

    def _browser_settings_get(self):
        control, store = open_control_plane(self.app_root)
        try:
            return self._json(BrowserSettingsStore(store).load())
        finally:
            store.close()

    def _browser_settings_update(self, body):
        control, store = open_control_plane(self.app_root)
        try:
            settings_store = BrowserSettingsStore(store)
            settings_store.save(body)
            return self._json(settings_store.load())
        finally:
            store.close()

    def _start_browser_session(self, body):
        """Section 2/33: objective in, a real background browser session
        out. API-first (Section 2's execution hierarchy) is honored simply
        by this being the ONLY way a browser task starts -- there is no
        code path here or in browser_runtime.py that would prefer clicking
        through a web UI over a direct API call Falguna could otherwise
        make; this module has no such API integrations of its own to
        prefer yet, so browser is correctly the first tier actually wired."""
        objective = str(body.get("objective", "")).strip()
        if not objective:
            raise ValueError("Provide a browser task objective")
        task_type = str(body.get("task_type") or "browser_research")
        project_id = body.get("project_id")
        if project_id:
            known = {item["id"] for item in load_profiles(self.app_root)}
            if project_id not in known:
                raise ValueError("Select an approved project")
        conversation_id = body.get("conversation_id")
        research_id = body.get("research_id")
        explicit_steps = body.get("steps")
        control, store = open_control_plane(self.app_root)
        try:
            browser_settings = BrowserSettingsStore(store).load()
            if not browser_settings.get("enabled", True):
                raise ValueError("Browser execution is turned off in Settings")
            headless = bool(body.get("headless", browser_settings.get("default_headless", True)))
            if explicit_steps:
                steps = [s for s in explicit_steps if isinstance(s, dict) and s.get("action") in BROWSER_ALL_ACTION_TYPES][:20]
                if not steps:
                    raise ValueError("No valid browser steps provided")
            else:
                # Section 29: the ONLY point in this whole subsystem that
                # ever touches a model -- an explicit-steps session above
                # never reaches this branch at all, so it runs with zero
                # provider calls regardless of what's configured.
                try:
                    steps = plan_steps_from_objective(store, objective, task_type, model_override=body.get("model"))
                except FalgunaModelError as exc:
                    raise ValueError(exc.message) from exc
            privacy_mode = ModelRegistry(store).load()["privacy_mode"]
            sessions = BrowserSessionStore(store)
            session_id = sessions.create(
                objective, task_type, project_id, "Aryan (local UI)", headless, privacy_mode,
                plan=steps, conversation_id=conversation_id, research_id=research_id,
            )
            control.audit.append("browser_session_created", {"session_id": session_id, "objective": objective[:200], "steps": len(steps)})
        finally:
            store.close()
        threading.Thread(target=_run_browser_session_background, args=(self.app_root, session_id), daemon=True).start()
        return self._json({"session_id": session_id, "status": BrowserSessionStatus.CREATED}, HTTPStatus.ACCEPTED)

    def _computer_status(self):
        """Read-only, side-effect-free (a screenshot attempt is the only
        real action here, and Section 17 already treats screenshot as
        always-safe) -- lets the UI, or a person checking from the API,
        know whether computer-use can actually run right now without first
        creating and running a whole session. Mirrors GET /api/browser/...
        health-style reporting rather than inventing a new pattern."""
        control, store = open_control_plane(self.app_root)
        try:
            settings = BrowserSettingsStore(store).load()
        finally:
            store.close()
        availability = computer_use_available()
        return self._json({"enabled_in_settings": bool(settings.get("computer_use_enabled", False)), **availability})

    def _start_computer_session(self, body):
        """Real screen/mouse/keyboard control, explicit-steps only in V1 --
        deliberately no auto-planning via a model here (unlike browser's
        optional plan_steps_from_objective): the computer-use module's own
        docstring is explicit that V1 stays "a minimal safe subset" and does
        NOT build "unrestricted desktop control", and letting a model
        improvise arbitrary clicks/keystrokes on the real desktop is exactly
        that. A person (or a caller acting on their exact instruction)
        supplies the steps; Falguna still gates every click/type through
        classify_sensitive_action and still requires an explicit approval
        to cross it, same as browser."""
        objective = str(body.get("objective", "")).strip()
        if not objective:
            raise ValueError("Provide a computer-use task objective")
        project_id = body.get("project_id")
        if project_id:
            known = {item["id"] for item in load_profiles(self.app_root)}
            if project_id not in known:
                raise ValueError("Select an approved project")
        explicit_steps = body.get("steps")
        if not explicit_steps:
            raise ValueError("Computer-use tasks require an explicit list of steps in V1")
        valid_actions = {"screenshot", "click", "type", "key"}
        steps = []
        for s in explicit_steps:
            if not isinstance(s, dict) or s.get("action") not in valid_actions:
                continue
            if s["action"] == "click" and not (isinstance(s.get("x"), (int, float)) and isinstance(s.get("y"), (int, float))):
                continue
            if s["action"] == "type" and not isinstance(s.get("text"), str):
                continue
            if s["action"] == "key" and not isinstance(s.get("combo"), str):
                continue
            steps.append(s)
        steps = steps[:20]
        if not steps:
            raise ValueError("No valid computer-use steps provided")
        control, store = open_control_plane(self.app_root)
        try:
            browser_settings = BrowserSettingsStore(store).load()
            if not browser_settings.get("computer_use_enabled", False):
                raise ValueError("Computer use is turned off in Settings")
            privacy_mode = ModelRegistry(store).load()["privacy_mode"]
            sessions = BrowserSessionStore(store)
            session_id = sessions.create(
                objective, "computer_use", project_id, "Aryan (local UI)", False, privacy_mode, plan=steps,
            )
            control.audit.append("computer_session_created", {"session_id": session_id, "objective": objective[:200], "steps": len(steps)})
        finally:
            store.close()
        threading.Thread(target=_run_computer_session_background, args=(self.app_root, session_id), daemon=True).start()
        return self._json({"session_id": session_id, "status": BrowserSessionStatus.CREATED}, HTTPStatus.ACCEPTED)

    def _approve_browser_gate(self, session_id):
        """Section 22's "Approve once" -- single-use, never a blanket
        authorization (see PlaywrightBrowserRuntime.run's
        approve_gate_for_index docstring). Shared by both browser and
        computer-use sessions (same table, same NEEDS_ARYAN pause shape) --
        the only branch is which background runner resumes execution,
        picked by task_type, never by a second endpoint or a second
        approval model."""
        control, store = open_control_plane(self.app_root)
        try:
            sessions = BrowserSessionStore(store)
            session = sessions.get(session_id)
            if not session:
                return self._json({"error": "browser session not found"}, HTTPStatus.NOT_FOUND)
            if session["status"] != BrowserSessionStatus.NEEDS_ARYAN:
                return self._json({"error": "This browser task is not waiting for your approval"}, HTTPStatus.BAD_REQUEST)
            resume_index = session.get("next_step_index", 0)
            is_computer = session.get("task_type") == "computer_use"
            control.audit.append(
                "computer_session_approved" if is_computer else "browser_session_approved",
                {"session_id": session_id, "step_index": resume_index},
            )
        finally:
            store.close()
        runner = _run_computer_session_background if is_computer else _run_browser_session_background
        threading.Thread(
            target=runner, args=(self.app_root, session_id),
            kwargs={"resume_from_index": resume_index, "approve_gate_for_index": resume_index}, daemon=True,
        ).start()
        return self._json({"session_id": session_id, "status": "RESUMING"}, HTTPStatus.ACCEPTED)

    def _reject_browser_gate(self, session_id):
        control, store = open_control_plane(self.app_root)
        try:
            sessions = BrowserSessionStore(store)
            session = sessions.get(session_id)
            if not session:
                return self._json({"error": "browser session not found"}, HTTPStatus.NOT_FOUND)
            if session["status"] != BrowserSessionStatus.NEEDS_ARYAN:
                return self._json({"error": "This browser task is not waiting for your approval"}, HTTPStatus.BAD_REQUEST)
            sessions.set_status(session_id, BrowserSessionStatus.CANCELLED, error="Rejected by Aryan", needs_aryan_reason=None)
            control.audit.append("browser_session_rejected", {"session_id": session_id})
            return self._json({"session_id": session_id, "status": BrowserSessionStatus.CANCELLED})
        finally:
            store.close()

    def _cancel_browser_session(self, session_id):
        control, store = open_control_plane(self.app_root)
        try:
            sessions = BrowserSessionStore(store)
            session = sessions.get(session_id)
            if not session:
                return self._json({"error": "browser session not found"}, HTTPStatus.NOT_FOUND)
            if session["status"] in BROWSER_TERMINAL_STATUSES:
                return self._json({"error": "This browser task has already finished"}, HTTPStatus.BAD_REQUEST)
            # Cooperative, bounded cancel (Section 39): flips the row now;
            # a still-running background thread notices at its next step
            # boundary (see PlaywrightBrowserRuntime.run's cancellation
            # checks) rather than being killed mid-action.
            sessions.set_status(session_id, BrowserSessionStatus.CANCELLED)
            control.audit.append("browser_session_cancel_requested", {"session_id": session_id})
            return self._json({"session_id": session_id, "status": BrowserSessionStatus.CANCELLED})
        finally:
            store.close()

    def _archive_browser_session(self, session_id, archived):
        control, store = open_control_plane(self.app_root)
        try:
            sessions = BrowserSessionStore(store)
            row = sessions.get(session_id)
            if not row:
                return self._json({"error": "browser session not found"}, HTTPStatus.NOT_FOUND)
            if archived and row["status"] in BROWSER_ACTIVELY_RUNNING_STATUSES:
                return self._json({"error": "An actively running browser task can't be archived -- cancel or wait for it to finish first"}, HTTPStatus.BAD_REQUEST)
            sessions.archive(session_id, archived)
            return self._json({"session_id": session_id, "archived": bool(archived)})
        finally:
            store.close()

    def _usage(self):
        """Usage/Cost (Section 26): real, recorded token counts and costs
        only -- never invented billing data. Falguna's only wired transport
        today is the subscription-based Codex CLI, which always reports
        cost_usd=0.0 (see codex_transport.py); the note below says so
        explicitly rather than implying a real dollar figure is being
        hidden or estimated."""
        control, store = open_control_plane(self.app_root)
        try:
            totals = {
                "chat": {"cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0, "calls": 0},
                "research": {"cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0, "calls": 0},
                "work": {"cost_usd": 0.0, "calls": 0},
            }
            for message in store.list("chat_messages"):
                if message.get("model_call_json"):
                    call = json.loads(message["model_call_json"])
                    totals["chat"]["cost_usd"] += float(call.get("cost_usd", 0) or 0)
                    totals["chat"]["input_tokens"] += int(call.get("input_tokens", 0) or 0)
                    totals["chat"]["output_tokens"] += int(call.get("output_tokens", 0) or 0)
                    totals["chat"]["calls"] += 1
            for query in store.list("research_queries"):
                if query.get("model_call_json"):
                    call = json.loads(query["model_call_json"])
                    totals["research"]["cost_usd"] += float(call.get("cost_usd", 0) or 0)
                    totals["research"]["input_tokens"] += int(call.get("input_tokens", 0) or 0)
                    totals["research"]["output_tokens"] += int(call.get("output_tokens", 0) or 0)
                    totals["research"]["calls"] += 1
            for call in store.list("model_calls"):
                totals["work"]["cost_usd"] += float(call["cost_usd"])
                totals["work"]["calls"] += 1
            for key in totals:
                totals[key]["cost_usd"] = round(totals[key]["cost_usd"], 8)
            return self._json({
                "totals": totals,
                "note": ("Falguna's configured runtime is the subscription-based Codex CLI, which has no "
                         "additional per-call cash cost, so cost_usd is 0 for every real call today. Token "
                         "counts are real recorded usage, not an estimate."),
            })
        finally:
            store.close()

    def _files(self):
        control, store = open_control_plane(self.app_root)
        try:
            all_attachments = AttachmentStore(store, self.app_root).list_all()
            uploads = [
                {"type": "upload", "id": a["id"], "filename": a["filename"], "content_type": a["content_type"],
                 "size_bytes": a["size_bytes"], "conversation_id": a["conversation_id"], "created_at": a["created_at"]}
                for a in all_attachments if not a.get("browser_session_id")
            ]
            generated = []
            for run in reversed(store.list("runs")[-100:]):
                task = store.get("tasks", run["task_id"])
                requirement = store.get("requirements", task["requirement_id"])
                mission = store.get("missions", requirement["mission_id"])
                for artifact in store.list("artifacts", "run_id=?", (run["id"],)):
                    generated.append({
                        "type": "artifact", "id": artifact["id"], "filename": Path(artifact["path"]).name,
                        "kind": artifact["kind"], "run_id": run["id"], "mission_title": mission["title"],
                        "created_at": artifact["created_at"],
                    })
            # Falguna Browser + Computer Use V1 (Section 26): a browser
            # session's screenshots and downloads are Falguna-GENERATED
            # evidence, not something the person uploaded -- they belong
            # here, linked back to the session that produced them, never
            # mislabeled as an "upload".
            browser_sessions = BrowserSessionStore(store)
            session_cache = {}
            profiles_by_id = {p["id"]: p for p in load_profiles(self.app_root)}
            for a in all_attachments:
                session_id = a.get("browser_session_id")
                if not session_id:
                    continue
                if session_id not in session_cache:
                    session_cache[session_id] = browser_sessions.get(session_id)
                session = session_cache[session_id] or {}
                # Files/artifacts inherit project association from the
                # session that produced them -- resolved from the same
                # project registry a session's project_id was validated
                # against at creation, never a second copy of it.
                project_id = session.get("project_id")
                generated.append({
                    "type": "browser_evidence", "id": a["id"], "filename": a["filename"],
                    "kind": "screenshot" if (a["content_type"] or "").startswith("image/") else "download",
                    "task_kind": "computer" if session.get("task_type") == "computer_use" else "browser",
                    "browser_session_id": session_id, "mission_title": session.get("objective", "Browser task"),
                    "project_id": project_id, "project_name": _resolve_project_name(project_id, profiles_by_id),
                    "created_at": a["created_at"],
                })
            generated.sort(key=lambda x: x["created_at"], reverse=True)
            return self._json({"uploads": uploads, "generated": generated[:200]})
        finally:
            store.close()

    def _download_attachment(self, attachment_id):
        control, store = open_control_plane(self.app_root)
        try:
            attachments = AttachmentStore(store, self.app_root)
            row = attachments.get(attachment_id)
            data = attachments.read_bytes(attachment_id) if row else None
            if data is None or not row:
                return self._json({"error": "attachment not found"}, HTTPStatus.NOT_FOUND)
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", row.get("content_type") or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            safe_name = re.sub(r'[\r\n"]', "", row.get("filename") or "file")
            self.send_header("Content-Disposition", f'attachment; filename="{safe_name}"')
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        finally:
            store.close()

    def _list_notifications(self):
        control, store = open_control_plane(self.app_root)
        try:
            notifications = NotificationStore(store)
            return self._json({"notifications": notifications.list_notifications(), "unread": notifications.unread_count()})
        finally:
            store.close()

    # ----------------------------------------------------------------- POST

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self._body()
            if path == "/api/runs":
                return self._start(body)
            if path.startswith("/api/runs/") and path.endswith("/decision"):
                run_id = path.split("/")[3]
                control, store = open_control_plane(self.app_root)
                try:
                    control.decide_merge(run_id, body.get("action", ""), "Aryan (local UI)", body.get("reason", "Operator decision from local UI"))
                    return self._json({"run_id": run_id, "decision": body["action"], "merge_performed": False})
                finally:
                    store.close()
            if path.startswith("/api/runs/") and path.endswith("/resume"):
                run_id = path.split("/")[3]
                token = secrets.token_urlsafe(16)
                with _operations_lock:
                    _operations[token] = {"state": "STARTING", "run_id": run_id}
                threading.Thread(target=_resume_mission, args=(self.app_root, token, run_id), daemon=True).start()
                return self._json({"operation": token, "state": "STARTING"}, HTTPStatus.ACCEPTED)
            if path.startswith("/api/runs/") and path.endswith(("/board-archive", "/board-unarchive")):
                run_id = path.split("/")[3]
                return self._archive_run_from_board(run_id, path.endswith("/board-archive"))
            if path.startswith("/api/runs/") and path.endswith(("/pause", "/cancel")):
                run_id = path.split("/")[3]
                action = path.rsplit("/", 1)[-1].upper()
                control, store = open_control_plane(self.app_root)
                try:
                    control.request_control(run_id, action)
                    return self._json({"run_id": run_id, "action": action, "mode": "safe-boundary"}, HTTPStatus.ACCEPTED)
                finally:
                    store.close()
            if path == "/api/browser/sessions":
                return self._start_browser_session(body)
            if path == "/api/browser/settings":
                return self._browser_settings_update(body)
            if path.startswith("/api/browser/sessions/") and path.endswith("/approve"):
                return self._approve_browser_gate(path.split("/")[4])
            if path.startswith("/api/browser/sessions/") and path.endswith("/reject"):
                return self._reject_browser_gate(path.split("/")[4])
            if path.startswith("/api/browser/sessions/") and path.endswith("/cancel"):
                return self._cancel_browser_session(path.split("/")[4])
            if path.startswith("/api/browser/sessions/") and path.endswith(("/board-archive", "/board-unarchive")):
                return self._archive_browser_session(path.split("/")[4], path.endswith("/board-archive"))
            if path == "/api/computer/sessions":
                return self._start_computer_session(body)
            if path == "/api/continue":
                profile = {item["id"]: item for item in load_profiles(self.app_root)}.get(body.get("project"))
                if not profile:
                    raise ValueError("Select an approved project")
                control, store = open_control_plane(self.app_root)
                try:
                    run_id = resolve_continuation(store, Path(profile["repository"]))
                    if not run_id:
                        raise ValueError("Continuation target is ambiguous or unavailable; choose a recent mission")
                    return self._json({"run_id": run_id})
                finally:
                    store.close()
            if path == "/api/conversations":
                return self._create_conversation(body)
            segments = path.split("/")
            if path.startswith("/api/conversations/") and path.endswith("/messages"):
                return self._post_message(segments[3], body)
            if path.startswith("/api/conversations/") and len(segments) >= 7 and segments[4] == "messages" and segments[6] == "stop":
                return self._stop_message(segments[3], segments[5])
            if path.startswith("/api/conversations/") and len(segments) >= 7 and segments[4] == "messages" and segments[6] == "regenerate":
                return self._regenerate_message(segments[3], segments[5])
            if path.startswith("/api/conversations/") and len(segments) >= 7 and segments[4] == "messages" and segments[6] == "edit":
                return self._edit_message(segments[3], segments[5], body)
            if path.startswith("/api/conversations/") and path.endswith("/rename"):
                return self._rename_conversation(segments[3], body)
            if path.startswith("/api/conversations/") and path.endswith("/archive"):
                return self._archive_conversation(segments[3])
            if path.startswith("/api/conversations/") and path.endswith("/delete"):
                return self._delete_conversation(segments[3])
            if path.startswith("/api/conversations/") and path.endswith("/model"):
                return self._set_conversation_model(segments[3], body)
            if path.startswith("/api/conversations/") and path.endswith("/work-mode"):
                return self._set_conversation_work_mode(segments[3], body)
            if path.startswith("/api/conversations/") and path.endswith("/attachments"):
                return self._upload_attachment(segments[3], body)
            if path.startswith("/api/conversations/") and path.endswith("/handoff"):
                return self._handoff(segments[3], body)
            if path.startswith("/api/notifications/") and path.endswith("/read"):
                return self._mark_notification_read(segments[3])
            if path == "/api/notifications/read-all":
                return self._mark_all_notifications_read()
            if path == "/api/models/settings":
                return self._update_model_settings(body)
            if path == "/api/research":
                return self._run_research(body)
            if path.startswith("/api/research/") and path.endswith("/continue-chat"):
                return self._research_to_chat(segments[3], body)
            if path.startswith("/api/research/") and path.endswith("/handoff"):
                return self._research_to_work(segments[3], body)
            if path == "/api/memory":
                return self._create_memory(body)
            if path == "/api/memory/settings":
                return self._memory_settings_update(body)
            if path.startswith("/api/memory/") and path.endswith("/supersede"):
                return self._supersede_memory(segments[3], body)
            if path.startswith("/api/memory/") and path.endswith("/pin"):
                return self._pin_memory(segments[3], body)
            if path.startswith("/api/memory/") and path.endswith("/edit"):
                return self._edit_memory(segments[3], body)
            if path.startswith("/api/memory/") and path.endswith("/forget"):
                return self._forget_memory(segments[3], body)
            if path.startswith("/api/memory/") and path.endswith("/purge"):
                return self._purge_memory(segments[3])
            if path.startswith("/api/memory/suggestions/") and path.endswith("/accept"):
                return self._accept_memory_suggestion(segments[4], body)
            if path.startswith("/api/memory/suggestions/") and path.endswith("/dismiss"):
                return self._dismiss_memory_suggestion(segments[4])
            if path == "/api/knowledge/documents":
                return self._ingest_knowledge_document(body)
            if path.startswith("/api/knowledge/documents/") and path.endswith("/forget"):
                return self._forget_knowledge_document(segments[4], body)
            return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except MemoryConflict as exc:
            return self._json({"error": str(exc), "candidates": exc.candidates}, HTTPStatus.CONFLICT)
        except DiscoveryUncertain as exc:
            return self._json({"error": str(exc), "discovery": exc.evidence}, HTTPStatus.CONFLICT)
        except (ValueError, KeyError, json.JSONDecodeError, ChatError, MemoryError, KnowledgeError) as exc:
            return self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def _start(self, body):
        token = self._launch(body.get("project"), body.get("objective", ""), body.get("max_cost_usd"),
                              work_mode=body.get("work_mode"), model=body.get("model"))
        return self._json({"operation": token, "state": "STARTING"}, HTTPStatus.ACCEPTED)

    def _launch(self, project_id, objective, max_cost_usd, conversation_id=None, research_id=None,
                work_mode=None, model=None):
        """Shared by /api/runs, Chat -> Work handoff, and Search -> Work handoff. All
        three paths go through the identical discovery/policy/worktree pipeline --
        a handoff cannot skip discovery, widen scope, or start a run without it.
        conversation_id/research_id only ever get attached to a run that this same
        call produced through the real control plane."""
        profiles = {item["id"]: item for item in load_profiles(self.app_root)}
        profile = profiles.get(project_id)
        if not profile:
            raise ValueError("Select an approved project")
        objective = str(objective or "").strip()
        if len(objective) < 12:
            raise ValueError("Provide a bounded engineering objective")
        discovery_started = time.monotonic()
        control, store = open_control_plane(self.app_root)
        try:
            plan, cache = ProjectUnderstandingCache(store).discover(Path(profile["repository"]), profile, objective)
        finally:
            store.close()
        discovery_ms = round((time.monotonic() - discovery_started) * 1000)
        if not plan.verification_commands:
            raise ValueError("VERIFY_COMMAND_INVALID: no runnable native verification command was discovered")
        if plan.requires_approval:
            error = plan.diagnostic or "DISCOVERY_SCOPE_UNCERTAIN"
            raise DiscoveryUncertain(f"{error}: Discovery is uncertain; approve or narrow the proposed scope before modification", plan.evidence())
        editable = validate_editable(plan.editable_files)
        commands = plan.verification_commands
        cap = float(max_cost_usd if max_cost_usd is not None else profile["default_budget_usd"])
        if cap < 0 or cap > float(profile["default_budget_usd"]):
            raise ValueError("Cost cap exceeds the approved project-profile maximum")
        token = secrets.token_urlsafe(16)
        with _operations_lock:
            _operations[token] = {"state": "STARTING", "run_id": None}
        discovery = {**plan.evidence(), "cache": cache, "duration_ms": discovery_ms}
        with _operations_lock:
            _operations[token]["discovery"] = discovery
        thread = threading.Thread(
            target=_run_mission,
            args=(self.app_root, token, profile, objective, editable, commands, cap, discovery, conversation_id, research_id,
                  _model_choice(model), _work_mode(work_mode)),
            daemon=True,
        )
        thread.start()
        return token

    def _create_conversation(self, body):
        profiles = {item["id"] for item in load_profiles(self.app_root)}
        project_id = body.get("project_id")
        if project_id and project_id not in profiles:
            raise ValueError("Select an approved project")
        control, store = open_control_plane(self.app_root)
        try:
            chat = ConversationStore(store)
            conversation_id = chat.create_conversation(body.get("title", ""), project_id)
            return self._json(chat.get_conversation(conversation_id), HTTPStatus.CREATED)
        finally:
            store.close()

    def _rename_conversation(self, conversation_id, body):
        control, store = open_control_plane(self.app_root)
        try:
            chat = ConversationStore(store)
            chat.rename_conversation(conversation_id, body.get("title", ""))
            return self._json(chat.get_conversation(conversation_id))
        finally:
            store.close()

    def _post_message(self, conversation_id, body):
        """Product Experience V2 (Sections 3-4): posting a message is now
        asynchronous, exactly like starting a Work mission -- the HTTP
        response returns as soon as the user's message (and a PENDING
        assistant placeholder) are persisted, and the reply is generated on
        a background thread. The client polls the returned operation token
        (the same GET /api/operations/<token> endpoint missions already
        use) and shows the real QUEUED -> THINKING -> COMPLETED/FAILED/
        CANCELLED state -- never a fabricated progress indicator."""
        content = str(body.get("content", "")).strip()
        if not content:
            raise ValueError("Message cannot be empty")
        attachment_ids = body.get("attachment_ids") or []
        if not isinstance(attachment_ids, list):
            raise ValueError("attachment_ids must be a list")
        control, store = open_control_plane(self.app_root)
        try:
            chat = ConversationStore(store)
            conversation = chat.get_conversation(conversation_id)
            if not conversation:
                return self._json({"error": "conversation not found"}, HTTPStatus.NOT_FOUND)
            user_message = chat.add_message(conversation_id, "user", content)
            if attachment_ids:
                AttachmentStore(store, self.app_root).attach_to_message(attachment_ids, conversation_id, user_message["id"])
            # Memory & Knowledge V2 (Pass E): a deterministic, literal-phrase
            # check for a save-worthy statement -- never auto-saved, only
            # ever queued as a suggestion the person must explicitly accept
            # (see MemorySuggestionStore.accept / the Memory UI). Scoped to
            # this conversation's own project (or personal, if none) --
            # never company-wide from a single chat message.
            scope_type = "project" if conversation.get("project_id") else "personal"
            scope_id = conversation.get("project_id")
            MemorySuggestionStore(store, control.audit, MemoryStore(store, control.audit)).create_from_message(
                scope_type, scope_id, conversation_id, user_message["id"], content,
            )
            pending = chat.add_pending_message(conversation_id)
            history = self._chat_history(chat, conversation_id, exclude_message_id=pending["id"])
        finally:
            store.close()
        token = self._spawn_chat_reply(conversation_id, conversation, pending, history)
        return self._json({"message": user_message, "assistant": pending, "operation": token}, HTTPStatus.ACCEPTED)

    @staticmethod
    def _chat_history(chat, conversation_id, exclude_message_id=None):
        return [
            {"role": m["role"], "content": m["content"]}
            for m in chat.list_messages(conversation_id)
            if m["role"] in {"user", "assistant"} and m["id"] != exclude_message_id and m.get("status", "COMPLETED") == "COMPLETED"
        ]

    def _spawn_chat_reply(self, conversation_id, conversation, pending, history):
        work_mode = _work_mode(conversation.get("work_mode"))
        model = _model_choice(conversation.get("model_override"))
        token = secrets.token_urlsafe(16)
        with _operations_lock:
            _operations[token] = {"state": "QUEUED", "conversation_id": conversation_id, "message_id": pending["id"], "started_at": utcnow()}
        threading.Thread(
            target=_run_chat_reply,
            args=(self.app_root, token, conversation_id, pending["id"], history, model, work_mode, conversation.get("project_id")),
            daemon=True,
        ).start()
        return token

    def _stop_message(self, conversation_id, message_id):
        control, store = open_control_plane(self.app_root)
        try:
            row = ConversationStore(store).cancel_message(message_id)
            return self._json(row)
        finally:
            store.close()

    def _regenerate_message(self, conversation_id, message_id):
        control, store = open_control_plane(self.app_root)
        try:
            chat = ConversationStore(store)
            conversation = chat.get_conversation(conversation_id)
            if not conversation:
                return self._json({"error": "conversation not found"}, HTTPStatus.NOT_FOUND)
            chat.prepare_regenerate(conversation_id, message_id)
            pending = chat.add_pending_message(conversation_id)
            history = self._chat_history(chat, conversation_id, exclude_message_id=pending["id"])
        finally:
            store.close()
        token = self._spawn_chat_reply(conversation_id, conversation, pending, history)
        return self._json({"assistant": pending, "operation": token}, HTTPStatus.ACCEPTED)

    def _edit_message(self, conversation_id, message_id, body):
        control, store = open_control_plane(self.app_root)
        try:
            chat = ConversationStore(store)
            conversation = chat.get_conversation(conversation_id)
            if not conversation:
                return self._json({"error": "conversation not found"}, HTTPStatus.NOT_FOUND)
            chat.edit_user_message(conversation_id, message_id, body.get("content", ""))
            pending = chat.add_pending_message(conversation_id)
            history = self._chat_history(chat, conversation_id, exclude_message_id=pending["id"])
        finally:
            store.close()
        token = self._spawn_chat_reply(conversation_id, conversation, pending, history)
        return self._json({"assistant": pending, "operation": token}, HTTPStatus.ACCEPTED)

    def _archive_conversation(self, conversation_id):
        control, store = open_control_plane(self.app_root)
        try:
            chat = ConversationStore(store)
            chat.archive_conversation(conversation_id)
            return self._json(chat.get_conversation(conversation_id))
        finally:
            store.close()

    def _delete_conversation(self, conversation_id):
        control, store = open_control_plane(self.app_root)
        try:
            ConversationStore(store).delete_conversation(conversation_id)
            return self._json({"id": conversation_id, "status": "DELETED"})
        finally:
            store.close()

    def _set_conversation_model(self, conversation_id, body):
        model = body.get("model")
        if model not in (None, "", "auto") and model not in SUPPORTED_CODEX_MODELS:
            raise ValueError("Unsupported model")
        model = model if model in SUPPORTED_CODEX_MODELS else None
        control, store = open_control_plane(self.app_root)
        try:
            chat = ConversationStore(store)
            if not chat.get_conversation(conversation_id):
                return self._json({"error": "conversation not found"}, HTTPStatus.NOT_FOUND)
            store.update("conversations", conversation_id, model_override=model)
            return self._json(chat.get_conversation(conversation_id))
        finally:
            store.close()

    def _set_conversation_work_mode(self, conversation_id, body):
        mode = _work_mode(body.get("work_mode"))
        control, store = open_control_plane(self.app_root)
        try:
            chat = ConversationStore(store)
            if not chat.get_conversation(conversation_id):
                return self._json({"error": "conversation not found"}, HTTPStatus.NOT_FOUND)
            store.update("conversations", conversation_id, work_mode=mode)
            return self._json(chat.get_conversation(conversation_id))
        finally:
            store.close()

    def _upload_attachment(self, conversation_id, body):
        control, store = open_control_plane(self.app_root)
        try:
            chat = ConversationStore(store)
            if not chat.get_conversation(conversation_id):
                return self._json({"error": "conversation not found"}, HTTPStatus.NOT_FOUND)
            record = AttachmentStore(store, self.app_root).save_base64(
                body.get("filename", "file"), body.get("content_type"), body.get("data_base64", ""),
                conversation_id=conversation_id,
            )
            return self._json(record, HTTPStatus.CREATED)
        finally:
            store.close()

    def _mark_notification_read(self, notification_id):
        control, store = open_control_plane(self.app_root)
        try:
            NotificationStore(store).mark_read(notification_id)
            return self._json({"id": notification_id, "read": True})
        finally:
            store.close()

    def _mark_all_notifications_read(self):
        control, store = open_control_plane(self.app_root)
        try:
            count = NotificationStore(store).mark_all_read()
            return self._json({"marked": count})
        finally:
            store.close()

    def _handoff(self, conversation_id, body):
        control, store = open_control_plane(self.app_root)
        try:
            conversation = ConversationStore(store).get_conversation(conversation_id)
        finally:
            store.close()
        if not conversation:
            return self._json({"error": "conversation not found"}, HTTPStatus.NOT_FOUND)
        project_id = body.get("project_id") or conversation.get("project_id")
        token = self._launch(project_id, body.get("objective", ""), body.get("max_cost_usd"), conversation_id=conversation_id,
                              work_mode=body.get("work_mode"), model=body.get("model"))
        return self._json({"operation": token, "state": "STARTING", "conversation_id": conversation_id}, HTTPStatus.ACCEPTED)

    def _run_research(self, body):
        """Runs one research query synchronously (mirrors _post_message's shape:
        no worktree, no isolation needed -- this never touches a repository).
        Retrieval and synthesis are two separate steps: SEARCH_PROVIDER only
        ever returns source metadata (never executed), and ResearchResponder
        only ever sees that metadata as clearly-labeled, untrusted data."""
        query_text = str(body.get("query", "")).strip()
        if len(query_text) < 3:
            raise ValueError("Provide a research query")
        project_id = body.get("project_id")
        conversation_id = body.get("conversation_id")
        model = _model_choice(body.get("model"))
        mode = _work_mode(body.get("work_mode"))
        settings = WORK_MODE_SETTINGS[mode]
        if project_id:
            profiles = {item["id"] for item in load_profiles(self.app_root)}
            if project_id not in profiles:
                raise ValueError("Select an approved project")
        control, store = open_control_plane(self.app_root)
        try:
            if conversation_id and not ConversationStore(store).get_conversation(conversation_id):
                raise ValueError("conversation not found")
            rs = ResearchStore(store)
            research_id = rs.create_query(query_text, SEARCH_PROVIDER.name, project_id, conversation_id, model_override=model, work_mode=mode)
            try:
                provider_result = SEARCH_PROVIDER.search(query_text, max_results=6)
                sources = rank_sources(provider_result.sources)
                # ResearchResponder.reply() itself never touches gateway/transport
                # when `sources` is empty (see research.py) -- always building
                # them here is cheap (no I/O happens at construction time) and
                # removes the need for this handler to special-case "is Codex
                # installed" the way it used to.
                gateway = OpenAICompatibleGateway(model, "http://127.0.0.1:1/v1", "")
                transport = _build_router(store, use_fallback=settings["fallback"])
                outcome = ResearchResponder(gateway, transport, model, timeout_seconds=settings["research_timeout"]).reply(query_text, sources)
                rs.save_result(research_id, outcome["answer"], sources, outcome["citations"], outcome["suggested_objective"], model_call=outcome["model_call"])
                NotificationStore(store).notify_once("RESEARCH_COMPLETE", f"Research ready: {query_text[:70]}", "", "research", research_id)
            except ResearchError as exc:
                rs.save_failure(research_id, str(exc))
                NotificationStore(store).notify_once("RESEARCH_FAILED", f"Research failed: {query_text[:70]}", str(exc), "research", research_id)
            record = rs.get_query(research_id)
            return self._json({
                "research": record,
                "sources": rs.get_sources(research_id),
                "citations": rs.get_citations(research_id),
            }, HTTPStatus.CREATED)
        finally:
            store.close()

    def _research_to_chat(self, research_id, body):
        control, store = open_control_plane(self.app_root)
        try:
            rs = ResearchStore(store)
            research = rs.get_query(research_id)
            if not research:
                return self._json({"error": "research query not found"}, HTTPStatus.NOT_FOUND)
            chat = ConversationStore(store)
            conversation_id = body.get("conversation_id") or research.get("conversation_id")
            if conversation_id and not chat.get_conversation(conversation_id):
                conversation_id = None
            if not conversation_id:
                conversation_id = chat.create_conversation(f"Research: {research['query'][:80]}", research.get("project_id"))
            chat.add_message(conversation_id, "user", f"Continue from Search: {research['query']}")
            sources = rs.get_sources(research_id)
            citation_note = ""
            if sources:
                citation_note = "\n\nSources:\n" + "\n".join(
                    f"[{i + 1}] {s['title'] or s['url']} — {s['url']}" for i, s in enumerate(sources)
                )
            chat.add_message(conversation_id, "assistant", (research.get("answer") or "") + citation_note)
            if research.get("conversation_id") != conversation_id:
                store.update("research_queries", research_id, conversation_id=conversation_id)
            return self._json({"conversation_id": conversation_id}, HTTPStatus.CREATED)
        finally:
            store.close()

    def _research_to_work(self, research_id, body):
        control, store = open_control_plane(self.app_root)
        try:
            research = ResearchStore(store).get_query(research_id)
        finally:
            store.close()
        if not research:
            return self._json({"error": "research query not found"}, HTTPStatus.NOT_FOUND)
        project_id = body.get("project_id") or research.get("project_id")
        token = self._launch(project_id, body.get("objective", ""), body.get("max_cost_usd"), research_id=research_id,
                              work_mode=body.get("work_mode"), model=body.get("model"))
        return self._json({"operation": token, "state": "STARTING", "research_id": research_id}, HTTPStatus.ACCEPTED)

    def _body(self):
        length = int(self.headers.get("Content-Length", "0"))
        # 16MB covers the largest legitimate request this server accepts --
        # a base64-encoded attachment (Section 14) up to AttachmentStore's
        # own MAX_ATTACHMENT_BYTES=10MB decoded cap, inflated ~1.34x by
        # base64 -- while still bounding every other, much smaller request.
        if length > 16 * 1024 * 1024:
            raise ValueError("request too large")
        return json.loads(self.rfile.read(length) or b"{}")

    def _json(self, value, status=HTTPStatus.OK):
        payload = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _html(self, value):
        payload = value.encode()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _notify_run_terminal(store, run_id, objective):
    """Fires a Notification Center entry (Section 13) only for a real
    terminal state a run actually reached -- never a fabricated event.
    DONE_CANDIDATE with a pending merge decision is Falguna's Needs-You /
    approval-required state (Section 32); FAILED/QUARANTINED is a task
    failure. notify_once dedupes so a resumed run that lands on the same
    terminal status twice does not spam the center."""
    run = store.get("runs", run_id)
    if not run:
        return
    notifications = NotificationStore(store)
    if run["status"] == "DONE_CANDIDATE":
        approvals = store.list("approvals", "run_id=? AND kind=?", (run_id, "PROTECTED_BRANCH_MERGE"))
        if not approvals or approvals[-1]["status"] == "PENDING":
            notifications.notify_once("APPROVAL_REQUIRED", f"Ready for your review: {objective[:70]}",
                                       "Falguna finished this mission and needs your merge decision. Nothing is merged until you decide.",
                                       "run", run_id)
    elif run["status"] in {"FAILED", "QUARANTINED"}:
        notifications.notify_once("TASK_FAILED", f"Mission failed: {objective[:70]}", run.get("error") or "", "run", run_id)


def _run_mission(app_root, token, profile, objective, editable, commands, cap, discovery, conversation_id=None,
                  research_id=None, model=DEFAULT_CODEX_MODEL, work_mode=DEFAULT_WORK_MODE):
    control, store = open_control_plane(app_root)
    settings = WORK_MODE_SETTINGS.get(work_mode, WORK_MODE_SETTINGS[DEFAULT_WORK_MODE])
    try:
        dependency_path = Path(profile["repository"]) / profile.get("package_root", ".") / "node_modules"
        browser_applicable = browser_e2e_applicable(objective, editable, profile)
        policy = RunPolicy(allowed_write_globs=editable, verification_commands=commands, max_cost_usd=cap, dependency_node_path=str(dependency_path) if dependency_path.is_dir() else None, verification_write_regexes=profile.get("verification_write_regexes", []), browser_applicable=browser_applicable, browser_base_url=profile.get("browser_base_url") if browser_applicable else None, browser_project_roots=profile.get("browser_project_roots", ["."]), browser_cached_install_allowed=bool(profile.get("browser_cached_install_allowed", False)), browser_external_probe_required=bool(profile.get("browser_external_probe_required", False)), require_implementation_change=discovery.get("objective_kind") == "FEATURE_CHANGE", implementation_files=discovery.get("implementation_files", []), max_attempts=settings["max_attempts"])
        gateway = OpenAICompatibleGateway(model, "http://127.0.0.1:1/v1", "")
        transport = _build_router(store, use_fallback=settings["fallback"])
        try:
            transport.resolve(model)  # fail fast, before a mission/run is ever created -- same contract as the old "no codex, no mission" check, now honestly covering every configured provider instead of only Codex
        except FalgunaModelError as exc:
            raise RuntimeError(exc.message) from exc
        worker = StructuredEditWorker(gateway, editable, timeout_seconds=settings["mission_timeout"], transport=transport)
        control.reviewer = ModelSemanticReviewer(gateway, timeout_seconds=settings["mission_timeout"], transport=transport)
        ids = control.create_mission(objective[:80], objective, Path(profile["repository"]), policy)
        def created(run_id):
            evidence_dir = app_root / ".falguna" / "evidence" / run_id
            evidence_dir.mkdir(parents=True, exist_ok=True)
            discovery_path = evidence_dir / "discovery.json"
            discovery_path.write_text(json.dumps(discovery, indent=2, sort_keys=True))
            control._record_artifact(run_id, "DISCOVERY", discovery_path)
            store.create("mission_timings", {"run_id": run_id, "stage": "discovery", "duration_ms": int(discovery.get("duration_ms", 0)), "metadata_json": json.dumps({"cache": discovery.get("cache", {})}, sort_keys=True), "created_at": utcnow()})
            control.audit.append("DISCOVERY_APPROVED", {"run_id": run_id, "confidence": discovery["confidence"], "editable_files": discovery["editable_files"]})
            if conversation_id:
                ConversationStore(store).record_handoff(conversation_id, run_id, objective)
            if research_id:
                ResearchStore(store).record_handoff(research_id, run_id, objective)
            with _operations_lock:
                _operations[token] = {"state": "RUNNING", "run_id": run_id, "discovery": discovery}
        run_id = control.start(ids["task_id"], worker, "structured-codex", model, policy, on_run_created=created)
        _notify_run_terminal(store, run_id, objective)
        with _operations_lock:
            _operations[token] = {"state": "COMPLETE", "run_id": run_id, "discovery": discovery}
    except Exception as exc:
        with _operations_lock:
            run_id = _operations.get(token, {}).get("run_id")
            _operations[token] = {"state": "FAILED", "run_id": run_id, "error": str(exc)}
        if run_id:
            try:
                _notify_run_terminal(store, run_id, objective)
            except Exception:
                pass
    finally:
        store.close()


def _resume_mission(app_root, token, run_id):
    control, store = open_control_plane(app_root)
    try:
        run = store.get("runs", run_id)
        if not run:
            raise ValueError("run not found")
        task = store.get("tasks", run["task_id"])
        raw = json.loads(task["policy_json"])
        raw["verification_commands"] = [CommandSpec(**item) for item in raw.get("verification_commands", [])]
        policy = RunPolicy(**raw)
        gateway = OpenAICompatibleGateway(run["model"], "http://127.0.0.1:1/v1", "")
        transport = _build_router(store, use_fallback=True)
        try:
            transport.resolve(run["model"])
        except FalgunaModelError as exc:
            raise RuntimeError(exc.message) from exc
        worker = StructuredEditWorker(gateway, policy.allowed_write_globs, timeout_seconds=300, transport=transport)
        control.reviewer = ModelSemanticReviewer(gateway, timeout_seconds=300, transport=transport)
        with _operations_lock:
            _operations[token] = {"state": "RUNNING", "run_id": run_id, "resumed": True}
        control.resume(run_id, worker, policy)
        requirement_body = store.get("requirements", task["requirement_id"])
        _notify_run_terminal(store, run_id, requirement_body["body"] if requirement_body else "")
        with _operations_lock:
            _operations[token] = {"state": "COMPLETE", "run_id": run_id, "resumed": True}
    except Exception as exc:
        with _operations_lock:
            _operations[token] = {"state": "FAILED", "run_id": run_id, "error": str(exc), "resumed": True}
    finally:
        store.close()


def _run_browser_session_background(app_root, session_id, resume_from_index=0, approve_gate_for_index=None):
    """Runs (or resumes) a browser session's plan in its own background
    thread -- exactly the same "kick off a thread, poll the row for state"
    shape _resume_mission/_run_mission already use for Work missions, so a
    browser task is a real background task Mission Control can watch, not
    a blocking HTTP call (Section 25: "appear like any other Falguna
    task... do not create a parallel task system"). Each thread opens its
    own StateStore connection, matching every other background runner in
    this module -- sqlite connections are not shared across threads."""
    control, store = open_control_plane(app_root)
    sessions = BrowserSessionStore(store)
    try:
        session = sessions.get(session_id)
        if not session:
            return
        steps = json.loads(session["plan_json"]) if session.get("plan_json") else []
        attachments = AttachmentStore(store, app_root)
        runtime = PlaywrightBrowserRuntime(app_root, store, sessions, attachments, audit=control.audit)
        runtime.run(session_id, steps, resume_from_index=resume_from_index, approve_gate_for_index=approve_gate_for_index)
    except Exception as exc:
        # PlaywrightBrowserRuntime.run() already converts every real failure
        # into a clean FAILED status internally -- this is only a last-resort
        # net so a genuinely unexpected error in the thread itself (not
        # inside run()) can never leave a session's status stuck at RUNNING
        # forever with no explanation.
        try:
            sessions.set_status(
                session_id, BrowserSessionStatus.FAILED,
                error="Falguna hit an unexpected internal error running this browser task.",
                error_category=BrowserRuntimeError.UNEXPECTED_FAILURE, error_detail=str(exc)[:2000],
            )
        except Exception:
            pass
    finally:
        store.close()


def _run_computer_session_background(app_root, session_id, resume_from_index=0, approve_gate_for_index=None):
    """Runs (or resumes) a computer-use session's plan. Deliberately NOT part
    of falguna/browser_runtime.py or falguna/computer_use.py -- it is pure
    orchestration glue, reusing the exact same BrowserSessionStore rows,
    AttachmentStore evidence mechanism, and "kick off a thread, poll the row"
    shape _run_browser_session_background already uses, so a computer-use
    task is a real background task on the SAME Mission Control board, never
    a parallel system. classify_sensitive_action is the same one function
    browser_runtime.py already applies to browser actions -- one classifier,
    not a second one invented for this module (mirrors
    PyAutoGUIComputerChannel's own internal gate, which stays in place as a
    defense-in-depth default for any other caller of that class).

    Gating happens HERE, at the orchestrator level, exactly like
    PlaywrightBrowserRuntime.run() gates browser actions before executing
    them -- never inside the channel -- so a session can pause the whole
    task as NEEDS_ARYAN and later resume past exactly one approved step
    (skip_gate=True passed only for that single index, recomputed fresh
    every call, never cached or reused for a later step)."""
    control, store = open_control_plane(app_root)
    sessions = BrowserSessionStore(store)
    attachments = AttachmentStore(store, app_root)
    try:
        session = sessions.get(session_id)
        if not session:
            return
        if session["status"] == BrowserSessionStatus.CANCELLED:
            return
        steps = json.loads(session["plan_json"]) if session.get("plan_json") else []
        browser_settings = BrowserSettingsStore(store).load()
        channel = build_computer_channel(browser_settings.get("computer_use_enabled", False))
        sessions.set_status(session_id, BrowserSessionStatus.RUNNING)

        def snapshot(reason: str, seq: int):
            # Screenshot is always safe/read-only (Section 17) -- capturing
            # one for evidence never itself needs a gate, even mid-pause.
            result = channel.screenshot()
            if result.status != "OK" or "image_base64" not in result.evidence:
                return None
            saved = attachments.save_base64(
                f"computer-{session_id}-{seq:03d}-{reason}.png", "image/png",
                result.evidence["image_base64"], browser_session_id=session_id,
            )
            return saved["id"]

        for idx, step in enumerate(steps[:20]):
            if idx < resume_from_index:
                continue
            live = sessions.get(session_id)
            if live and live["status"] == BrowserSessionStatus.CANCELLED:
                return
            seq = idx + 1
            action = step.get("action")
            description = step.get("description") or ""

            if action not in ("screenshot", "click", "type", "key"):
                continue  # unknown/unsupported action type -- filtered, not fatal (matches browser intake)

            skip_gate = False
            if action in ("click", "type"):
                gate_value = step.get("text") if action == "type" else description
                reason = classify_sensitive_action("click" if action == "click" else "type", description, gate_value)
                if reason and idx != approve_gate_for_index:
                    shot = snapshot("pause", seq)
                    sessions.record_action(session_id, seq, f"computer_{action}", None, gate_value, "PAUSED",
                                            detail=reason, screenshot_attachment_id=shot)
                    sessions.set_status(session_id, BrowserSessionStatus.NEEDS_ARYAN,
                                         needs_aryan_reason=f"sensitive_action:{reason}", next_step_index=idx)
                    control.audit.append("computer_session_paused", {"session_id": session_id, "reason": reason, "step_index": idx})
                    return
                skip_gate = bool(reason and idx == approve_gate_for_index)

            if action == "screenshot":
                result = channel.screenshot()
            elif action == "click":
                result = channel.click(step.get("x"), step.get("y"), description=description, skip_gate=skip_gate)
            elif action == "type":
                result = channel.type_text(step.get("text") or "", description=description, skip_gate=skip_gate)
            else:  # key
                result = channel.key(step.get("combo") or "", description=description, skip_gate=skip_gate)

            if action == "screenshot" and result.status == "OK" and "image_base64" in result.evidence:
                saved = attachments.save_base64(
                    f"computer-{session_id}-{seq:03d}-screenshot.png", "image/png",
                    result.evidence["image_base64"], browser_session_id=session_id,
                )
                evidence_attachment = saved["id"]
            else:
                evidence_attachment = snapshot(action, seq)

            target_value = f"{step.get('x')},{step.get('y')}" if action == "click" else (step.get("combo") if action == "key" else None)
            value_value = step.get("text") if action == "type" else None

            if result.status == "BLOCKED":
                sessions.record_action(session_id, seq, f"computer_{action}", target_value, value_value, "BLOCKED",
                                        detail=result.blocked_reason or "", screenshot_attachment_id=evidence_attachment)
                sessions.set_status(
                    session_id, BrowserSessionStatus.FAILED,
                    error=f"This computer-use action was blocked: {result.blocked_reason}.",
                    error_category="COMPUTER_USE_BLOCKED", next_step_index=idx,
                )
                control.audit.append("computer_session_blocked", {"session_id": session_id, "reason": result.blocked_reason, "step_index": idx})
                return
            if result.status == "FAILED":
                sessions.record_action(session_id, seq, f"computer_{action}", target_value, value_value, "FAILED",
                                        detail=str(result.evidence.get("detail", ""))[:500], screenshot_attachment_id=evidence_attachment)
                sessions.set_status(
                    session_id, BrowserSessionStatus.FAILED,
                    error="A computer-use action failed to execute.", error_category="COMPUTER_USE_FAILED",
                    error_detail=str(result.evidence.get("detail", ""))[:2000], next_step_index=idx,
                )
                return

            sessions.record_action(session_id, seq, f"computer_{action}", target_value, value_value, "OK",
                                    screenshot_attachment_id=evidence_attachment)
            live = sessions.get(session_id)
            if live and live["status"] == BrowserSessionStatus.CANCELLED:
                return
            sessions.set_status(session_id, BrowserSessionStatus.RUNNING, next_step_index=idx + 1)

        final_shot = snapshot("completed", len(steps) + 1)
        sessions.record_action(session_id, len(steps) + 1, "complete", None, None, "OK", screenshot_attachment_id=final_shot)
        sessions.set_status(session_id, BrowserSessionStatus.COMPLETED, next_step_index=len(steps))
        control.audit.append("computer_session_completed", {"session_id": session_id})
    except Exception as exc:
        try:
            sessions.set_status(
                session_id, BrowserSessionStatus.FAILED,
                error="Falguna hit an unexpected internal error running this computer-use task.",
                error_category="UNEXPECTED_FAILURE", error_detail=str(exc)[:2000],
            )
        except Exception:
            pass
    finally:
        store.close()


def _run_chat_reply(app_root, token, conversation_id, message_id, history, model, work_mode, project_id=None):
    """Background worker for the async Chat pipeline (Sections 3-4) --
    structurally identical to _run_mission: the HTTP handler has already
    returned, and this thread is the only thing that ever writes the
    reply. Every operation-state write below reflects a state this call
    actually reached; there is no fabricated "typing" percentage.

    Memory & Knowledge V2 (Pass F): retrieval below is 100% local SQL
    (FTS5) -- it never makes a network call, so it runs unconditionally
    regardless of Privacy Mode. The retrieved text only leaves this
    machine if/when the model call below does, and that call is already
    gated by ModelRouter's own Privacy Mode enforcement -- memory never
    gets a separate, wider network permission than the reply itself has."""
    control, store = open_control_plane(app_root)
    settings = WORK_MODE_SETTINGS.get(work_mode, WORK_MODE_SETTINGS[DEFAULT_WORK_MODE])
    chat = ConversationStore(store)
    try:
        with _operations_lock:
            _operations[token]["state"] = "THINKING"
        chat.mark_generating(message_id)
        memory_context = None
        try:
            scopes = [("personal", None), ("company", None)]
            if project_id:
                scopes.append(("project", project_id))
            last_user_text = next((m["content"] for m in reversed(history) if m["role"] == "user"), "")
            memory_context = assemble_chat_context(
                MemoryStore(store, control.audit), KnowledgeStore(store, control.audit), scopes, last_user_text,
            )
        except Exception:
            memory_context = None  # retrieval is best-effort -- a memory-store problem must never block a reply
        gateway = OpenAICompatibleGateway(model, "http://127.0.0.1:1/v1", "")
        transport = _build_router(store, use_fallback=settings["fallback"])
        outcome = ChatResponder(gateway, transport, model, timeout_seconds=settings["chat_timeout"]).reply(
            history, memory_context=memory_context["text"] if memory_context else None,
        )
        applied = chat.complete_message(
            message_id, outcome["reply"], outcome["model_call"], outcome["suggested_objective"],
            memory_context={"citations": memory_context["citations"]} if memory_context else None,
        )
        with _operations_lock:
            _operations[token] = {**_operations.get(token, {}), "state": "COMPLETE" if applied else "CANCELLED", "message_id": message_id}
    except ChatError as exc:
        # exc's own str() is already the sanitized, user-safe message (see
        # chat.ChatError.__init__) -- exc.category/exc.technical_detail carry
        # the machine-readable category and any raw provider text, persisted
        # separately so the UI can offer contextual recovery actions and an
        # expandable technical-details panel without ever showing raw
        # provider stdout in the primary bubble.
        applied = chat.fail_message(message_id, str(exc), category=exc.category, detail=exc.technical_detail)
        with _operations_lock:
            _operations[token] = {**_operations.get(token, {}), "state": "FAILED" if applied else "CANCELLED", "error": str(exc), "message_id": message_id}
    except Exception as exc:
        safe_message = "Falguna hit an unexpected internal error generating this reply."
        chat.fail_message(message_id, safe_message, category=ErrorCategory.UNEXPECTED_FAILURE, detail=str(exc))
        with _operations_lock:
            _operations[token] = {**_operations.get(token, {}), "state": "FAILED", "error": safe_message, "message_id": message_id}
    finally:
        store.close()


def reconcile_browser_sessions_at_startup(app_root: Path):
    """Call once, before a Falguna process starts accepting requests.

    A browser session's execution lives only in this process's memory. If a
    prior Falguna process exited -- crash, kill, or a plain restart to pick up
    new code -- while a session was still CREATED/RUNNING/WAITING, that
    background thread is gone and nothing will ever move the row again;
    Mission Control would otherwise show it as running forever. This closes
    those rows out honestly via BrowserSessionStore.reconcile_after_restart()
    and never raises, so a database/reconciliation problem can't block the
    server from starting. Returns the ids it reconciled (empty if none, or if
    reconciliation itself failed)."""
    try:
        control, store = open_control_plane(app_root)
        try:
            return BrowserSessionStore(store).reconcile_after_restart()
        finally:
            store.close()
    except Exception as exc:
        print(f"Falguna: browser session restart-reconciliation skipped ({exc})")
        return []


def serve(root: Path, host="127.0.0.1", port=8765):
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("Falguna v1.1 is local-only")
    server = ThreadingHTTPServer((host, port), FalgunaHandler)
    server.app_root = Path(root).resolve()
    reconciled = reconcile_browser_sessions_at_startup(server.app_root)
    if reconciled:
        print(f"Falguna: {len(reconciled)} browser session(s) were interrupted by restart "
              f"and marked FAILED: {', '.join(reconciled)}")
    print(f"Falguna Engineering internal alpha: http://{host}:{server.server_port}")
    server.serve_forever()


INDEX_HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<script>(function(){try{var t=localStorage.getItem('falguna-theme');if(t==='light'||t==='dark')document.documentElement.dataset.theme=t}catch(e){}})();</script>
<title>Falguna</title><style>
/* ---------- theme (Sections 19-20): System follows the OS/browser
   preference; an explicit choice is persisted in localStorage and applied
   via [data-theme] on <html> before first paint by the inline script
   above, so there is no flash of the wrong theme. Both palettes keep the
   same warm-amber accent identity -- light is a real, separately designed
   palette, not an inverted dark theme. */
:root{
  color-scheme:dark;
  --bg:#161310;--bg-glow:#241d12;--side:#1b1712;--panel:#211c15;--soft:#2a231a;--soft2:#332b1e;
  --line:#3c3325;--text:#f7f0e3;--muted:#ab9c86;--muted-dim:#7c7060;
  --accent:#e8a33d;--accent-hi:#f4bd63;--accent-ink:#2a1707;--accent-dim:#4d3a1e;--accent-soft:#332818;
  --warn:#f0c752;--bad:#ff8f78;--bad-dim:#4a2c25;--bad-ink:#ffd4c9;--good:#7fd99a;
  --user-bg:#2c2519;--user-ink:#e9dcc6;--shadow:#000c;--shadow-lite:#000a;--scrim:#0009;
}
@media (prefers-color-scheme:light){
  :root:not([data-theme="dark"]){
    color-scheme:light;
    --bg:#faf6ee;--bg-glow:#fff9ec;--side:#f4eedb;--panel:#ffffff;--soft:#f1e7d3;--soft2:#e9dabf;
    --line:#ddceac;--text:#241c10;--muted:#6e5f45;--muted-dim:#8c7c60;
    --accent:#d98a2c;--accent-hi:#a8620f;--accent-ink:#2a1707;--accent-dim:#e3c896;--accent-soft:#f3e3c3;
    --warn:#8a5a00;--bad:#b23a24;--bad-dim:#f8ddd5;--bad-ink:#7a2415;--good:#1e7a43;
    --user-bg:#efe0c2;--user-ink:#241c10;--shadow:#0002;--shadow-lite:#0001;--scrim:#0004;
  }
}
:root[data-theme="light"]{
  color-scheme:light;
  --bg:#faf6ee;--bg-glow:#fff9ec;--side:#f4eedb;--panel:#ffffff;--soft:#f1e7d3;--soft2:#e9dabf;
  --line:#ddceac;--text:#241c10;--muted:#6e5f45;--muted-dim:#8c7c60;
  --accent:#d98a2c;--accent-hi:#a8620f;--accent-ink:#2a1707;--accent-dim:#e3c896;--accent-soft:#f3e3c3;
  --warn:#8a5a00;--bad:#b23a24;--bad-dim:#f8ddd5;--bad-ink:#7a2415;--good:#1e7a43;
  --user-bg:#efe0c2;--user-ink:#241c10;--shadow:#0002;--shadow-lite:#0001;--scrim:#0004;
}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:radial-gradient(120% 140% at 18% -10%,var(--bg-glow) 0%,var(--bg) 46%);color:var(--text);font:14.5px/1.6 "Inter var",Inter,ui-sans-serif,system-ui,-apple-system,sans-serif;-webkit-font-smoothing:antialiased;transition:background .15s,color .15s}
button,input,select,textarea{font:inherit;color:inherit}
a{color:var(--accent-hi)}
svg{display:block}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
@media (prefers-reduced-motion:reduce){*,*::before,*::after{animation-duration:.001ms!important;animation-iteration-count:1!important;transition-duration:.001ms!important}}
.app{height:100dvh;display:grid;grid-template-columns:264px minmax(0,1fr);overflow:hidden}

/* ---------- sidebar ---------- */
aside{background:var(--side);border-right:1px solid var(--line);padding:14px 10px;display:flex;flex-direction:column;min-width:0;min-height:0;overflow:hidden}
.brand{display:flex;align-items:center;gap:9px;padding:6px 8px 16px;font-weight:700;font-size:15.5px;letter-spacing:.01em;flex:none}
.mark{display:grid;place-items:center;width:26px;height:26px;border-radius:8px;background:linear-gradient(155deg,var(--accent-hi),var(--accent));color:var(--accent-ink);font-weight:800;font-size:13px}
.new-chat{width:100%;flex:none;border:1px solid var(--line);background:var(--soft);color:var(--text);border-radius:11px;padding:9px 12px;text-align:left;cursor:pointer;font-weight:600;font-size:13px;display:flex;align-items:center;gap:9px;transition:background .12s,border-color .12s}
.new-chat:hover{background:var(--soft2);border-color:#4a3d28}
.new-chat svg{color:var(--accent)}
.nav{display:grid;gap:1px;flex:none;margin-top:16px}
.nav-item{display:flex;align-items:center;gap:10px;width:100%;border:0;background:transparent;padding:7px 9px;border-radius:8px;color:var(--muted);text-align:left;cursor:pointer;font-size:13px;transition:background .12s,color .12s}
.nav-item:hover{background:var(--soft);color:var(--text)}
.nav-item.active{background:var(--accent-soft);color:var(--accent-hi);font-weight:650}
.nav-icon{width:16px;height:16px;display:grid;place-items:center;color:var(--muted-dim);flex:none}
.nav-item:hover .nav-icon{color:var(--muted)}
.nav-item.active .nav-icon{color:var(--accent)}
.side-title{flex:none;padding:20px 9px 6px;color:var(--muted-dim);font-size:10.5px;font-weight:700;text-transform:uppercase;letter-spacing:.09em}
.side-list{flex:1;min-height:60px;overflow-x:hidden;overflow-y:auto;display:flex;flex-direction:column;gap:1px;padding-right:2px;scrollbar-width:thin;scrollbar-color:#4a3d28 transparent}
.side-group-label{padding:10px 9px 3px;color:var(--muted-dim);font-size:10.5px;font-weight:650;text-transform:uppercase;letter-spacing:.07em}
.side-list button{display:block;flex:0 0 auto;width:100%;min-width:0;border:0;background:transparent;color:var(--text);padding:7px 9px;border-radius:8px;text-align:left;cursor:pointer;overflow:hidden}
.side-list button:hover,.side-list button.active{background:var(--soft)}
.side-list button.active{color:var(--accent-hi)}
.side-list .row-title{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:12.5px;font-weight:550}
.side-list .row-sub{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--muted-dim);font-size:11px;margin-top:1px}
.side-empty{color:var(--muted-dim);padding:9px;font-size:12px}
.boundary{flex:none;border-top:1px solid var(--line);padding:12px 9px 2px;color:var(--muted-dim);font-size:11px;background:var(--side);line-height:1.5}

/* ---------- shell ---------- */
.workspace{min-width:0;min-height:0;display:grid;grid-template-rows:54px minmax(0,1fr);overflow:hidden}
.topbar{border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;padding:0 22px;background:linear-gradient(180deg,#1a1610,transparent)}
.topbar-title{display:flex;align-items:center;gap:10px;min-width:0}
.topbar-title strong{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:650;font-size:14px;color:var(--text)}
.menu-button{display:none;border:0;background:transparent;color:var(--text);padding:6px;border-radius:7px;cursor:pointer}
.model-pill{display:flex;align-items:center;gap:6px;border:1px solid var(--line);background:var(--panel);color:var(--muted);border-radius:999px;padding:5px 11px 5px 9px;font-size:11.5px;white-space:nowrap}
.model-pill .dot{width:6px;height:6px;border-radius:50%;background:var(--accent)}
.viewport{min-height:0;overflow:auto;display:flex;flex-direction:column}
.scrim{display:none}
.hidden{display:none!important}
::selection{background:var(--accent-dim);color:var(--text)}

/* ---------- generic page chrome (Search / Projects / History / Settings) ---------- */
.page{max-width:880px;margin:0 auto;padding:34px 24px 60px;width:100%}
.page h1{font-size:21px;margin:0 0 4px;font-weight:650;letter-spacing:-.01em}
.page .lede{color:var(--muted);margin:0 0 24px;font-size:13.5px}
.searchbar{display:flex;gap:8px;margin-bottom:22px}
.searchbar input{flex:1;background:var(--panel);border:1px solid var(--line);color:var(--text);border-radius:11px;padding:11px 14px}
.searchbar input:focus{outline:none;border-color:var(--accent-dim)}
.searchbar button{border:0;background:var(--accent);color:var(--accent-ink);border-radius:11px;padding:11px 18px;font-weight:700;cursor:pointer}
.result-list,.card-list{display:grid;gap:8px}
.result-row,.list-card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:13px 15px;cursor:pointer;text-align:left;transition:border-color .12s,background .12s}
.result-row:hover,.list-card:hover{border-color:#4a3d28;background:var(--soft)}
.result-row .kind{color:var(--muted-dim);font-size:10.5px;text-transform:uppercase;letter-spacing:.06em;margin-bottom:3px;font-weight:650}
.result-row .title,.list-card .title{font-weight:600;font-size:13.5px}
.result-row .meta,.list-card .meta{color:var(--muted);font-size:12px;margin-top:4px}
.empty-state{color:var(--muted);padding:36px 0;text-align:center;font-size:13px}
.project-card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:18px}
.project-card .title{font-size:15px;font-weight:650;display:flex;align-items:center;gap:9px}
.project-card .path{color:var(--muted-dim);font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11.5px;margin-top:5px;overflow-wrap:anywhere}
.project-card .risk{margin-top:11px;font-size:12.5px;color:var(--muted)}
.project-card .risk b{color:var(--text);font-weight:600}
.project-actions{display:flex;gap:8px;margin-top:14px;flex-wrap:wrap}
.pill-btn{border:1px solid var(--line);background:var(--soft);color:var(--text);border-radius:999px;padding:7px 13px;font-size:12px;cursor:pointer}
.pill-btn:hover{background:var(--soft2);border-color:#4a3d28}
.settings-list{display:grid;gap:7px;margin:14px 0 28px}
.settings-list div{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:11px 14px;font-size:12.5px;color:var(--text)}
.settings-note{background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--accent);border-radius:8px;padding:12px 15px;color:var(--muted);font-size:12.5px;margin-bottom:22px}
.section-label{font-size:12.5px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted-dim);font-weight:700;margin:26px 0 4px}
.status-pill{display:inline-block;border-radius:999px;padding:2px 9px;font-size:10.5px;text-transform:capitalize;border:1px solid var(--line)}
.status-pill.done{color:var(--accent-hi);border-color:var(--accent-dim)}
.status-pill.failed,.status-pill.quarantined{color:var(--bad);border-color:#5c332c}
.status-pill.pending,.status-pill.working,.status-pill.reviewing,.status-pill.verifying,.status-pill.planning{color:var(--warn);border-color:#5c4c2a}

/* ---------- Settings -> Models (Falguna V2.1: provider independence) ---------- */
.privacy-mode-options{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:9px;margin:10px 0 24px}
.privacy-mode-card{text-align:left;border:1px solid var(--line);background:var(--panel);border-radius:12px;padding:12px 14px;cursor:pointer;display:flex;flex-direction:column;gap:4px;transition:border-color .12s,background .12s}
.privacy-mode-card b{font-size:13px;color:var(--text)}
.privacy-mode-card span{font-size:11.5px;color:var(--muted);line-height:1.4}
.privacy-mode-card.active{border-color:var(--accent);background:var(--accent-soft)}
.privacy-mode-card.active b{color:var(--accent-hi)}
.provider-cards{display:grid;gap:10px;margin:10px 0 24px}
.provider-card{border:1px solid var(--line);background:var(--panel);border-radius:12px;padding:13px 15px}
.provider-card-head{display:flex;align-items:center;justify-content:space-between;gap:10px}
.provider-name{font-weight:650;font-size:13px;color:var(--text)}
.provider-meta{color:var(--muted-dim);font-size:11px;margin-top:2px}
.provider-detail{color:var(--muted);font-size:12px;margin-top:7px}
.provider-models{color:var(--muted);font-size:11.5px;margin-top:8px;padding-top:8px;border-top:1px solid var(--line)}
.provider-models.muted{color:var(--muted-dim);font-style:italic}
.health-pill{display:inline-flex;align-items:center;gap:5px;border-radius:999px;padding:3px 10px;font-size:10.5px;font-weight:650;text-transform:uppercase;letter-spacing:.04em;border:1px solid var(--line);white-space:nowrap}
.health-pill::before{content:"";width:6px;height:6px;border-radius:50%;background:currentColor}
.health-healthy{color:var(--good);border-color:var(--good)}
.health-degraded{color:var(--warn);border-color:var(--warn)}
.health-rate_limited{color:var(--warn);border-color:var(--warn)}
.health-auth_required{color:var(--bad);border-color:var(--bad)}
.health-offline{color:var(--muted-dim);border-color:var(--line)}
.health-unsupported{color:var(--muted-dim);border-color:var(--line)}
.provider-setup{border:1px solid var(--line);background:var(--panel);border-radius:12px;padding:13px 15px;margin-bottom:24px}
.provider-setup summary{cursor:pointer;font-weight:650;font-size:12.5px;color:var(--text)}
.settings-form{display:grid;gap:11px;margin-top:14px}
.settings-form label{display:flex;flex-direction:column;gap:5px;font-size:12px;color:var(--muted)}
.settings-form label input[type=text],.settings-form label input:not([type]){background:var(--soft);border:1px solid var(--line);color:var(--text);border-radius:9px;padding:9px 11px;font-size:12.5px}
.settings-form label.checkbox-row{flex-direction:row;align-items:center;gap:8px;font-size:12.5px;color:var(--text)}

/* ---------- Chat view ---------- */
.chat-view{display:grid;grid-template-rows:minmax(0,1fr) auto;min-height:0;height:100%}
.chat-scroll{min-height:0;overflow:auto;padding:0 max(22px,calc((100vw - 264px - 760px)/2))}
.chat-welcome{max-width:600px;margin:9vh auto 0;text-align:center}
.chat-welcome .glow{width:54px;height:54px;margin:0 auto 20px;border-radius:16px;background:linear-gradient(155deg,var(--accent-hi),var(--accent));display:grid;place-items:center;box-shadow:0 18px 44px -14px #e8a33d55}
.chat-welcome .glow svg{color:var(--accent-ink);width:26px;height:26px}
.chat-welcome h1{font-size:26px;margin:0 0 8px;font-weight:650;letter-spacing:-.015em;color:var(--text)}
.chat-welcome p{color:var(--muted);font-size:14px;margin:0 0 26px}
.chip-row{display:flex;gap:8px;flex-wrap:wrap;justify-content:center}
.chip{background:var(--panel);border:1px solid var(--line);border-radius:999px;padding:8px 15px;cursor:pointer;color:var(--muted);font-size:12.5px;display:inline-flex;align-items:center;gap:7px;transition:border-color .12s,background .12s}
.chip:hover{border-color:#4a3d28;background:var(--soft)}
.chip svg{width:13px;height:13px;color:var(--muted-dim)}
.thread{max-width:760px;margin:0 auto;padding:26px 0 8px;display:grid;gap:18px}
.msg{display:flex;gap:11px}
.msg .avatar{width:25px;height:25px;border-radius:8px;display:grid;place-items:center;font-size:11px;font-weight:750;flex:none;margin-top:3px}
.msg.user .avatar{background:var(--user-bg);color:var(--user-ink)}
.msg.assistant .avatar{background:linear-gradient(155deg,var(--accent-hi),var(--accent));color:var(--accent-ink)}
.msg .bubble{border:1px solid var(--line);background:var(--panel);border-radius:14px;padding:12px 15px;overflow-wrap:anywhere;min-width:0;flex:1;font-size:13.5px}
.msg.user .bubble{background:var(--user-bg);border-color:#463a27}
.msg .bubble.error{border-color:#5c332c;background:var(--bad-dim);color:var(--bad-ink)}
.msg .bubble.error .error-cat{font-size:10.5px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;opacity:.75;margin-bottom:4px}
.msg .bubble.error .error-detail{margin-top:8px;padding-top:8px;border-top:1px solid var(--bad-ink);opacity:.82;font-size:11.5px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap;overflow-wrap:anywhere}
.msg .bubble.error .error-actions{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:9px}
.msg .bubble.error .error-actions button{border:1px solid var(--bad-ink);background:transparent;color:var(--bad-ink);border-radius:8px;padding:4px 10px;font-size:11.5px;cursor:pointer}
.msg .bubble.error .error-actions a{color:var(--bad-ink);font-size:11.5px;text-decoration:underline}
.msg .bubble.error .error-actions details{width:100%}
.msg .bubble.error .error-actions summary{cursor:pointer;color:var(--bad-ink);font-size:11.5px;list-style:none}
.msg .bubble.error .error-actions summary::-webkit-details-marker{display:none}
.msg .who{color:var(--muted-dim);font-size:10.5px;text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px;font-weight:650}
.thinking{color:var(--muted);font-style:italic;display:flex;align-items:center;gap:6px}
.thinking .tdot{width:5px;height:5px;border-radius:50%;background:var(--accent);animation:tpulse 1.1s ease-in-out infinite}
.thinking .tdot:nth-child(2){animation-delay:.15s}.thinking .tdot:nth-child(3){animation-delay:.3s}
@keyframes tpulse{0%,60%,100%{opacity:.25}30%{opacity:1}}

/* ---------- composer ---------- */
.composer-wrap{padding:10px max(18px,calc((100vw - 264px - 760px)/2)) 20px;flex:none}
.composer{max-width:760px;margin:auto;background:var(--panel);border:1px solid var(--line);border-radius:20px;padding:6px 8px 8px;box-shadow:0 20px 50px -20px var(--shadow)}
.composer:focus-within{border-color:#4a3d28}
.composer textarea{display:block;width:100%;min-height:46px;max-height:180px;resize:none;border:0;outline:0;background:transparent;color:var(--text);padding:9px 8px 4px;font-size:14px}
.composer textarea::placeholder{color:var(--muted-dim)}
.compose-row{display:flex;align-items:center;gap:6px;padding:0 2px}
.icon-btn{border:1px solid transparent;background:transparent;color:var(--muted);width:30px;height:30px;border-radius:9px;display:grid;place-items:center;cursor:pointer}
.icon-btn:hover{background:var(--soft);color:var(--text)}
.mode-chip{display:flex;align-items:center;gap:6px;border:1px solid var(--line);background:var(--soft);color:var(--muted);border-radius:999px;padding:5px 10px 5px 8px;font-size:11.5px;cursor:default}
.mode-chip svg{width:12px;height:12px;color:var(--accent)}
.compose-spacer{flex:1}
.send-btn{border:0;background:var(--accent);color:var(--accent-ink);border-radius:10px;width:32px;height:32px;display:grid;place-items:center;cursor:pointer;transition:background .12s,transform .1s}
.send-btn:hover{background:var(--accent-hi)}
.send-btn:disabled{opacity:.4;cursor:not-allowed}
.compose-foot{max-width:760px;margin:7px auto 0;text-align:center;color:var(--muted-dim);font-size:10.5px}

/* ---------- handoff panel ---------- */
.handoff-panel{max-width:760px;margin:0 auto 16px;background:var(--panel);border:1px solid var(--accent-dim);border-radius:14px;padding:16px}
.handoff-panel h3{margin:0 0 3px;font-size:13.5px;display:flex;align-items:center;gap:7px}
.handoff-panel h3 svg{width:14px;height:14px;color:var(--accent)}
.handoff-panel .sub{color:var(--muted-dim);font-size:11.5px;margin:0 0 12px}
.handoff-panel label{display:block;color:var(--muted);font-size:11.5px;margin:10px 0 5px;font-weight:600}
.handoff-panel input,.handoff-panel select,.handoff-panel textarea{width:100%;color:var(--text);background:var(--soft);border:1px solid var(--line);border-radius:9px;padding:9px 10px}
.handoff-panel textarea{min-height:60px;resize:vertical}
/* Falguna V2.1: compact Chat -> Work trigger replacing the always-expanded
   panel above -- a single pill the person clicks when they actually want
   to hand off, never auto-opened (in particular, never opened just because
   a reply FAILED -- see renderMessageRow's error bubble, which only ever
   offers Retry / Change model, not this). */
.handoff-trigger-row{max-width:760px;margin:0 auto 10px;padding:0 4px;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.handoff-trigger{display:inline-flex;align-items:center;gap:7px;color:var(--accent-hi)}
.handoff-trigger svg{color:var(--accent)}
.handoff-prior{color:var(--muted-dim);font-size:11.5px}
.handoff-prior a{color:var(--muted)}
.modal.handoff-modal{max-width:480px;text-align:left}
.modal.handoff-modal label{display:block;color:var(--muted);font-size:11.5px;margin:10px 0 5px;font-weight:600}
.modal.handoff-modal select,.modal.handoff-modal textarea{width:100%;color:var(--text);background:var(--soft);border:1px solid var(--line);border-radius:9px;padding:9px 10px}
.modal.handoff-modal textarea{min-height:80px;resize:vertical}
.handoff-actions{display:flex;gap:9px;margin-top:13px;flex-wrap:wrap}
button.action{border:0;border-radius:9px;padding:9px 14px;font-weight:650;cursor:pointer;background:var(--accent);color:var(--accent-ink);font-size:13px}
button.action.secondary{background:var(--soft2);color:var(--text)}
button.action.danger{background:var(--bad-dim);color:var(--bad-ink)}
button.action:disabled{opacity:.5;cursor:not-allowed}

/* ---------- Work view ---------- */
.work-view{padding:32px max(22px,calc((100vw - 264px - 820px)/2)) 60px}
.work-empty{max-width:680px;margin:7vh auto 0;text-align:center}
.work-empty h1{font-size:24px;margin:0 0 9px;font-weight:650}
.work-empty p{color:var(--muted);font-size:14px;margin:0 0 24px}
.mission-form{max-width:600px;margin:0 auto;background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:20px;text-align:left}
.mission-form label{display:block;color:var(--muted);font-size:11.5px;margin:12px 0 5px;font-weight:600}
.mission-form input,.mission-form select,.mission-form textarea{width:100%;color:var(--text);background:var(--soft);border:1px solid var(--line);border-radius:9px;padding:10px}
.mission-form textarea{min-height:92px;resize:vertical}
.mission-form .risk-note{margin-top:8px;color:var(--muted-dim);font-size:11.5px}
.work-thread{max-width:820px;margin:auto}
.origin-banner{border:1px solid var(--accent-dim);background:var(--accent-soft);border-radius:11px;padding:10px 14px;margin-bottom:16px;font-size:12px;color:var(--text);display:flex;justify-content:space-between;gap:10px;align-items:center;flex-wrap:wrap}
.work-header{border:1px solid var(--line);background:var(--panel);border-radius:16px;padding:19px;margin-bottom:16px}
.work-header .objective{color:var(--muted);font-size:12.5px;margin-bottom:10px}
.status-line{font-size:19px;font-weight:700;margin:2px 0 8px;text-transform:capitalize}
.error{color:var(--bad);overflow-wrap:anywhere;font-size:13px}
.timeline{display:flex;gap:7px;flex-wrap:wrap;margin-top:6px}
.step{border:1px solid var(--line);border-radius:999px;padding:5px 10px;color:var(--muted);font-size:11.5px}
.step:last-child{border-color:var(--accent-dim);color:var(--accent-hi)}
.cards{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin-top:14px}
.card{background:var(--soft);border:1px solid var(--line);border-radius:11px;padding:12px;min-width:0}
.card span{display:block;color:var(--muted-dim);font-size:10.5px;margin-bottom:3px}
.card b{overflow-wrap:anywhere;font-size:13px}
.details{margin-top:13px;display:grid;gap:9px}
.details div{padding:11px 13px;border:1px solid var(--line);border-radius:10px;overflow-wrap:anywhere;background:var(--panel);font-size:12.5px}
.details span{color:var(--muted-dim);display:block;font-size:10.5px}
.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:15px}

/* ---------- Search / Research view ---------- */
.research-detail-view{max-width:760px;margin:0 auto;padding:34px 0 60px}
.answer-card{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:20px}
.answer-card .q{color:var(--muted-dim);font-size:10.5px;text-transform:uppercase;letter-spacing:.06em;margin-bottom:9px;font-weight:650}
.answer-card .a{font-size:14px;line-height:1.7;white-space:pre-wrap;overflow-wrap:anywhere}
.answer-card .a.error{color:var(--bad)}
.source-list{display:grid;gap:8px;margin-top:12px}
.source-card{border:1px solid var(--line);background:var(--soft);border-radius:11px;padding:11px 13px;display:block;text-decoration:none;color:inherit;transition:border-color .12s,background .12s}
.source-card:hover{border-color:#4a3d28;background:var(--soft2)}
.source-card .idx{display:inline-grid;place-items:center;width:17px;height:17px;border-radius:6px;background:var(--accent-soft);color:var(--accent-hi);font-size:10px;font-weight:700;margin-right:7px;vertical-align:middle}
.source-card .s-title{font-weight:600;font-size:13px}
.source-card .s-meta{color:var(--muted-dim);font-size:11px;margin-top:4px;overflow-wrap:anywhere}
.disclosure{margin-top:14px;border-top:1px solid var(--line);padding-top:14px}
.disclosure summary{cursor:pointer;color:var(--muted);font-size:12.5px;font-weight:600;list-style:none}
.disclosure summary::-webkit-details-marker{display:none}
.research-actions{display:flex;gap:9px;margin-top:16px;flex-wrap:wrap}

/* ---------- topbar: live indicator + notification bell (Sections 12-13) */
.topbar-right{display:flex;align-items:center;gap:8px}
.indicator-pill{display:flex;align-items:center;gap:6px;border:1px solid var(--line);background:var(--panel);color:var(--muted);border-radius:999px;padding:5px 11px;font-size:11.5px;cursor:pointer;white-space:nowrap}
.indicator-pill:hover{border-color:var(--accent-dim)}
.indicator-pill b{color:var(--text);font-weight:650}
.indicator-pill .warn-count{color:var(--warn)}
.indicator-pill .bad-count{color:var(--bad)}
.bell-btn{position:relative;border:1px solid var(--line);background:var(--panel);color:var(--muted);width:32px;height:32px;border-radius:9px;display:grid;place-items:center;cursor:pointer}
.bell-btn:hover{color:var(--text);border-color:var(--accent-dim)}
.bell-dot{position:absolute;top:5px;right:5px;width:7px;height:7px;border-radius:50%;background:var(--bad)}
.notif-panel{position:absolute;top:52px;right:22px;width:340px;max-width:calc(100vw - 30px);max-height:70vh;overflow:auto;background:var(--panel);border:1px solid var(--line);border-radius:14px;box-shadow:0 20px 50px -20px var(--shadow);z-index:30;padding:6px}
.notif-panel-head{display:flex;align-items:center;justify-content:space-between;padding:8px 8px 6px;font-weight:650;font-size:12.5px}
.notif-panel-head button{border:0;background:none;color:var(--accent-hi);font-size:11.5px;cursor:pointer}
.notif-row{padding:9px 8px;border-radius:9px;font-size:12.5px;cursor:default}
.notif-row.unread{background:var(--accent-soft)}
.notif-row .n-title{font-weight:600}
.notif-row .n-body{color:var(--muted);margin-top:2px;font-size:11.5px}
.notif-row .n-time{color:var(--muted-dim);margin-top:3px;font-size:10.5px}

/* ---------- toast (Section 27: error experience) */
.toast-stack{position:fixed;bottom:18px;right:18px;z-index:50;display:grid;gap:8px;max-width:min(92vw,380px)}
.toast{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:11px 13px;box-shadow:0 14px 34px -16px var(--shadow);font-size:12.5px;display:grid;gap:5px}
.toast.error{border-color:#7a3f33}
.toast .t-msg{display:flex;justify-content:space-between;gap:10px;align-items:flex-start}
.toast .t-msg button{border:0;background:none;color:var(--muted-dim);cursor:pointer;font-size:14px;line-height:1;padding:0}
.toast details{margin-top:2px}
.toast summary{cursor:pointer;color:var(--muted-dim);font-size:11px}
.toast pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:10.5px;color:var(--muted);margin:5px 0 0;max-height:140px;overflow:auto}

/* ---------- modal (delete/archive confirmation etc.) */
.modal-scrim{position:fixed;inset:0;background:var(--scrim);z-index:60;display:grid;place-items:center;padding:20px}
.modal{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:20px;max-width:420px;width:100%;box-shadow:0 24px 60px -20px var(--shadow)}
.modal h3{margin:0 0 8px;font-size:15px}
.modal p{margin:0 0 16px;color:var(--muted);font-size:13px}
.modal-actions{display:flex;justify-content:flex-end;gap:8px}

/* ---------- Mission Control (Sections 7-12) */
.mc-view{padding:30px max(22px,calc((100vw - 264px - 1080px)/2)) 60px}
.mc-head{display:flex;justify-content:space-between;align-items:flex-end;gap:14px;flex-wrap:wrap;margin-bottom:18px}
.mc-buckets{display:grid;gap:22px}
.mc-bucket-title{display:flex;align-items:center;gap:8px;font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:var(--muted-dim);margin-bottom:9px}
.mc-bucket-title .count{background:var(--soft);border-radius:999px;padding:1px 8px;color:var(--text);font-weight:650}
.mc-bucket-title.needs-you{color:var(--warn)}
.mc-bucket-title.needs-you .count{background:var(--accent-soft);color:var(--accent-hi)}
.mc-bucket-title.failed{color:var(--bad)}
.mc-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:10px}
.mc-card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:14px;cursor:pointer;text-align:left;display:grid;gap:7px;min-width:0}
.mc-card:hover{border-color:var(--accent-dim)}
.mc-card.needs-you{border-color:var(--accent-dim);background:var(--accent-soft)}
.mc-card .mc-title{font-weight:650;font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mc-card .mc-meta{color:var(--muted-dim);font-size:11px;display:flex;flex-wrap:wrap;gap:6px 10px}
.mc-card .mc-step{color:var(--muted);font-size:12px}
.mc-card .mc-controls{display:flex;gap:6px;flex-wrap:wrap;margin-top:2px}
.mc-card .mc-controls button{font-size:11px;padding:5px 9px}
.mc-empty-bucket{color:var(--muted-dim);font-size:12.5px;padding:14px 0}
.mc-card.archived{opacity:.68}
.mc-archived-bucket{margin-top:8px;border-top:1px solid var(--line);padding-top:14px}
.mc-archived-bucket summary{cursor:pointer;display:flex;align-items:center;gap:8px;font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:var(--muted-dim);list-style:none}
.mc-archived-bucket summary::-webkit-details-marker{display:none}
.mc-archived-bucket summary .count{background:var(--soft);border-radius:999px;padding:1px 8px;color:var(--text);font-weight:650}
.mc-archived-bucket[open] summary{margin-bottom:9px}
.mc-archived-bucket .mc-grid{margin-top:9px}

/* ---------- Files (Section 14) */
.files-view{max-width:900px;margin:0 auto;padding:34px 24px 60px}
.files-tabs{display:flex;gap:6px;margin-bottom:16px}
.files-tabs button{border:1px solid var(--line);background:var(--soft);color:var(--muted);border-radius:999px;padding:6px 14px;font-size:12px;cursor:pointer}
.files-tabs button.active{background:var(--accent-soft);color:var(--accent-hi);border-color:var(--accent-dim)}
.file-row{display:flex;justify-content:space-between;align-items:center;gap:10px;background:var(--panel);border:1px solid var(--line);border-radius:11px;padding:11px 14px}
.file-row .f-name{font-weight:600;font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.file-row .f-meta{color:var(--muted-dim);font-size:11px;margin-top:2px}

/* ---------- Chat V2: message state, actions, markdown, attachments */
.msg .status-note{color:var(--muted-dim);font-size:10.5px;margin-top:6px;display:flex;align-items:center;gap:6px}
.msg-actions{display:flex;gap:4px;margin-top:7px;opacity:0;transition:opacity .1s}
.msg:hover .msg-actions,.msg:focus-within .msg-actions{opacity:1}
.msg-action-btn{border:1px solid transparent;background:none;color:var(--muted-dim);padding:4px 7px;border-radius:7px;font-size:11px;cursor:pointer;display:inline-flex;align-items:center;gap:4px}
.msg-action-btn:hover{background:var(--soft);color:var(--text)}
.bubble pre{background:#00000030;border:1px solid var(--line);border-radius:9px;padding:10px 12px;overflow:auto;font-size:12px;margin:8px 0}
:root[data-theme="light"] .bubble pre,@media (prefers-color-scheme:light){:root:not([data-theme="dark"]) .bubble pre{background:#0000000d}}
.bubble code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.92em}
.bubble :not(pre)>code{background:var(--soft2);border-radius:4px;padding:1px 5px}
.bubble p{margin:0 0 8px}
.bubble p:last-child{margin-bottom:0}
.bubble strong{font-weight:700}
.bubble em{font-style:italic}
.msg.editing textarea{width:100%;min-height:60px;background:var(--panel);border:1px solid var(--accent-dim);border-radius:10px;padding:9px 10px;resize:vertical}
.msg.editing .edit-actions{display:flex;gap:8px;margin-top:8px}
.memory-sources{display:block;margin:8px 0 0;font-size:11.5px;color:var(--muted)}
.memory-sources summary{cursor:pointer;display:flex;align-items:center;gap:6px;list-style:none}
.memory-sources summary::-webkit-details-marker{display:none}
.memory-source-list{margin-top:6px;display:flex;flex-direction:column;gap:5px}
.memory-source-row{border:1px solid var(--line);background:var(--soft);border-radius:8px;padding:6px 9px;display:flex;gap:8px;align-items:baseline}
.attach-chip-row{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
.attach-chip{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--line);background:var(--soft);border-radius:999px;padding:4px 10px 4px 8px;font-size:11px;text-decoration:none;color:inherit}
.attach-chip:hover{border-color:var(--accent-dim)}
.attach-chip .x{cursor:pointer;color:var(--muted-dim)}
.composer-attach-row{display:flex;gap:6px;flex-wrap:wrap;padding:0 8px 6px}
.selector-row{display:flex;align-items:center;gap:6px;flex-wrap:wrap;padding:0 2px 6px}
.tiny-select{border:1px solid var(--line);background:var(--soft);color:var(--muted);border-radius:999px;padding:5px 9px;font-size:11px}

/* ---------- Settings V2 ---------- */
.settings-nav{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:22px;position:sticky;top:0;background:var(--bg);padding:4px 0;z-index:2}
.settings-nav button{border:1px solid var(--line);background:var(--soft);color:var(--muted);border-radius:999px;padding:6px 13px;font-size:12px;cursor:pointer}
.settings-nav button.active{background:var(--accent-soft);color:var(--accent-hi);border-color:var(--accent-dim)}
.settings-section{margin-bottom:30px}
.theme-options{display:flex;gap:10px;flex-wrap:wrap}
.theme-card{border:1px solid var(--line);background:var(--panel);border-radius:12px;padding:12px 16px;cursor:pointer;font-size:12.5px;display:flex;align-items:center;gap:8px}
.theme-card.active{border-color:var(--accent);background:var(--accent-soft);color:var(--accent-hi)}
.usage-cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;margin:14px 0}
.usage-card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px}
.usage-card span{display:block;color:var(--muted-dim);font-size:10.5px;text-transform:uppercase;letter-spacing:.05em;margin-bottom:5px}
.usage-card b{font-size:18px}
.usage-card small{display:block;color:var(--muted-dim);font-size:10.5px;margin-top:3px}

@media(max-width:850px){
  .app{grid-template-columns:1fr}
  .workspace{grid-column:1}
  .menu-button{display:inline-grid;place-items:center}
  aside{display:flex;position:fixed;inset:0 auto 0 0;width:min(86vw,290px);z-index:20;transform:translateX(-102%);transition:transform .18s ease;box-shadow:20px 0 50px var(--shadow-lite)}
  aside.open{transform:translateX(0)}
  .scrim{position:fixed;inset:0;background:var(--scrim);z-index:15}
  .scrim.open{display:block}
  .chat-scroll,.composer-wrap,.work-view{padding-left:15px;padding-right:15px}
  .cards{grid-template-columns:1fr 1fr}
  .topbar{padding:0 15px}
}
@media(max-width:520px){
  .cards{grid-template-columns:1fr}
  .msg.user{margin-left:0}
  .compose-foot{display:none}
  .work-header,.mission-form,.handoff-panel{padding:14px}
  .model-pill span.label{display:none}
}
</style></head><body>
<div class="app">
  <aside id="sidebar" aria-label="Falguna navigation">
    <div class="brand"><span class="mark">F</span>Falguna</div>
    <button class="new-chat" id="newChatBtn"></button>
    <nav class="nav" id="nav"></nav>
    <div class="side-title" id="sideListTitle">Recent chats</div>
    <div class="side-list" id="sideList"><div class="side-empty">Loading&hellip;</div></div>
    <div class="boundary">Localhost-only internal alpha<br>No automatic merge or deploy</div>
  </aside>
  <div class="scrim" id="scrim"></div>
  <main class="workspace">
    <header class="topbar">
      <div class="topbar-title"><button class="menu-button" id="menuButton" aria-label="Open navigation" aria-expanded="false"></button><strong id="viewTitle">Chat</strong></div>
      <div class="topbar-right">
        <button class="indicator-pill hidden" id="indicatorPill" type="button"></button>
        <div style="position:relative">
          <button class="bell-btn" id="bellBtn" type="button" aria-label="Notifications"></button>
          <div class="notif-panel hidden" id="notifPanel"></div>
        </div>
        <div class="model-pill" id="modelPill"><span class="dot"></span><span class="label">Falguna</span></div>
      </div>
    </header>
    <section class="viewport" id="viewport"></section>
  </main>
</div>
<div class="toast-stack" id="toastStack"></div>
<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const nl2br=s=>esc(s).replace(/\n/g,'<br>');
async function api(url,options){const r=await fetch(url,options);const j=await r.json();if(!r.ok)throw Object.assign(new Error(j.error||'Request failed'),{data:j});return j}
let profiles={};
let profileList=[];
let settingsModel='';

/* ---------------------------------------------------------- misc helpers */
function formatBytes(n){
  n=Number(n)||0;
  if(n<1024)return n+' B';
  if(n<1024*1024)return (n/1024).toFixed(1)+' KB';
  return (n/1024/1024).toFixed(2)+' MB';
}
function formatDuration(ms){
  const s=Math.max(0,Math.floor(ms/1000));
  if(s<60)return s+'s';
  const m=Math.floor(s/60),rs=s%60;
  if(m<60)return m+'m '+String(rs).padStart(2,'0')+'s';
  const h=Math.floor(m/60),rm=m%60;
  return h+'h '+String(rm).padStart(2,'0')+'m';
}
setInterval(()=>{
  document.querySelectorAll('[data-since]').forEach(el=>{
    const t=new Date(el.dataset.since).getTime();
    if(!isNaN(t))el.textContent=formatDuration(Date.now()-t);
  });
},1000);

/* -------------------------------------------------------------- toasts */
function showToast(message,opts){
  opts=opts||{};
  const stack=$('toastStack');
  if(!stack)return;
  const el=document.createElement('div');
  el.className='toast'+(opts.error?' error':'');
  const detail=opts.detail?`<details><summary>Details</summary><pre>${esc(opts.detail)}</pre></details>`:'';
  el.innerHTML=`<div class="t-msg"><span>${esc(message)}</span><button type="button" aria-label="Dismiss">&times;</button></div>${detail}`;
  el.querySelector('button').onclick=()=>el.remove();
  stack.appendChild(el);
  setTimeout(()=>{el.remove()},opts.error?9000:5000);
}

/* --------------------------------------------------------- confirm modal */
function confirmModal(opts){
  return new Promise(resolve=>{
    const scrim=document.createElement('div');
    scrim.className='modal-scrim';
    scrim.innerHTML=`<div class="modal"><h3>${esc(opts.title||'Are you sure?')}</h3><p>${esc(opts.body||'')}</p>
      <div class="modal-actions"><button type="button" class="pill-btn" id="mCancel">Cancel</button><button type="button" class="action${opts.danger?' danger':''}" id="mOk">${esc(opts.confirmLabel||'Confirm')}</button></div></div>`;
    document.body.appendChild(scrim);
    const finish=result=>{scrim.remove();document.removeEventListener('keydown',onKey);resolve(result)};
    function onKey(e){if(e.key==='Escape')finish(false)}
    document.addEventListener('keydown',onKey);
    scrim.addEventListener('click',e=>{if(e.target===scrim)finish(false)});
    scrim.querySelector('#mCancel').onclick=()=>finish(false);
    scrim.querySelector('#mOk').onclick=()=>finish(true);
    scrim.querySelector('#mOk').focus();
  });
}

/* ---------------------------------------------------- lightweight markdown */
function renderMarkdown(raw){
  const text=esc(raw==null?'':String(raw));
  const blocks=[];
  let out=text.replace(/```([a-zA-Z0-9_+-]*)\n?([\s\S]*?)```/g,(m,lang,code)=>{
    const idx=blocks.length;
    blocks.push(`<pre><code${lang?` class="lang-${esc(lang)}"`:''}>${code}</code></pre>`);
    return `\u0000B${idx}\u0000`;
  });
  out=out.replace(/`([^`\n]+)`/g,'<code>$1</code>');
  out=out.replace(/\*\*([^*\n]+)\*\*/g,'<strong>$1</strong>');
  out=out.replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,!?]|$)/g,'$1<em>$2</em>');
  const paras=out.split(/\n{2,}/);
  out=paras.map(p=>{
    const t=p.trim();
    const bm=t.match(/^\u0000B(\d+)\u0000$/);
    if(bm)return blocks[Number(bm[1])];
    return t?`<p>${t.replace(/\n/g,'<br>')}</p>`:'';
  }).join('');
  return out||'';
}

/* ------------------------------------------------------------------ icons */
const ICON={
  chat:'<path d="M3 5.5A2.5 2.5 0 0 1 5.5 3h9A2.5 2.5 0 0 1 17 5.5v6A2.5 2.5 0 0 1 14.5 14H9l-4 3v-3H5.5A2.5 2.5 0 0 1 3 11.5v-6Z" stroke="currentColor" stroke-width="1.4" fill="none" stroke-linejoin="round"/>',
  work:'<path d="M10 3.5v2M10 14.5v2M4.6 5.6l1.4 1.4M14 13l1.4 1.4M3.5 10h2M14.5 10h2M4.6 14.4 6 13M14 7l1.4-1.4" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/><circle cx="10" cy="10" r="3.2" stroke="currentColor" stroke-width="1.4" fill="none"/>',
  search:'<circle cx="8.7" cy="8.7" r="5" stroke="currentColor" stroke-width="1.4" fill="none"/><path d="m16 16-3.8-3.8" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>',
  projects:'<path d="M3 6.2A1.2 1.2 0 0 1 4.2 5h3.6l1.4 1.6h6.6A1.2 1.2 0 0 1 17 7.8v6.8A1.4 1.4 0 0 1 15.6 16H4.4A1.4 1.4 0 0 1 3 14.6V6.2Z" stroke="currentColor" stroke-width="1.4" fill="none" stroke-linejoin="round"/>',
  history:'<circle cx="10" cy="10.5" r="6.3" stroke="currentColor" stroke-width="1.4" fill="none"/><path d="M10 7v3.6l2.4 1.4" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/><path d="M7.3 3.6 5.6 5" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>',
  settings:'<circle cx="10" cy="10" r="2.6" stroke="currentColor" stroke-width="1.4" fill="none"/><path d="M10 3.6v1.7M10 14.7v1.7M16.4 10h-1.7M5.3 10H3.6M14.6 5.4l-1.2 1.2M6.6 13.4l-1.2 1.2M14.6 14.6l-1.2-1.2M6.6 6.6 5.4 5.4" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>',
  plus:'<path d="M9 3.5v11M3.5 9h11" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>',
  send:'<path d="M3 10 16 4l-4.6 12.5-2.6-5.6L3 10Z" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round" fill="currentColor" fill-opacity=".08"/>',
  clip:'<path d="M13.2 6.4 7.8 11.8a2 2 0 0 0 2.8 2.8l5.1-5.1a3.4 3.4 0 0 0-4.8-4.8L5.8 9.8a4.7 4.7 0 0 0 6.6 6.6l5-5" stroke="currentColor" stroke-width="1.3" fill="none" stroke-linecap="round"/>',
  spark:'<path d="M10 2.5c.6 3 1.8 4.4 4.8 5-3 .6-4.2 2-4.8 5-.6-3-1.8-4.4-4.8-5 3-.6 4.2-2 4.8-5Z" stroke="currentColor" stroke-width="1.1" fill="currentColor" fill-opacity=".18" stroke-linejoin="round"/>',
  menu:'<path d="M3 5.5h14M3 10h14M3 14.5h14" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>',
  handoff:'<path d="M4 10h9M9 5.5 13.5 10 9 14.5" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linecap="round" stroke-linejoin="round"/>',
  mission:'<circle cx="10" cy="10" r="6.4" stroke="currentColor" stroke-width="1.3" fill="none"/><circle cx="10" cy="10" r="2.6" stroke="currentColor" stroke-width="1.3" fill="none"/><path d="M10 2v2.6M10 15.4V18M2 10h2.6M15.4 10H18" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/>',
  files:'<path d="M4.6 3.6h6.4l3.2 3.2v9.2a.6.6 0 0 1-.6.6H4.6a.6.6 0 0 1-.6-.6V4.2a.6.6 0 0 1 .6-.6Z" stroke="currentColor" stroke-width="1.3" fill="none" stroke-linejoin="round"/><path d="M11 3.6v3.2h3.2" stroke="currentColor" stroke-width="1.3" fill="none" stroke-linejoin="round"/>',
  bell:'<path d="M6 8.4a4 4 0 0 1 8 0v3l1.3 2.4H4.7L6 11.4v-3Z" stroke="currentColor" stroke-width="1.3" fill="none" stroke-linejoin="round"/><path d="M8.3 15.8a1.7 1.7 0 0 0 3.4 0" stroke="currentColor" stroke-width="1.3" fill="none" stroke-linecap="round"/>',
  copy:'<rect x="7.3" y="7.3" width="8.2" height="9.6" rx="1.6" stroke="currentColor" stroke-width="1.3" fill="none"/><path d="M4.5 12.5V5.1a1.6 1.6 0 0 1 1.6-1.6h7.1" stroke="currentColor" stroke-width="1.3" fill="none" stroke-linecap="round"/>',
  edit:'<path d="M12.3 4.3 15.7 7.7 6.6 16.8 3 17.5l.7-3.6 8.6-9.6Z" stroke="currentColor" stroke-width="1.3" fill="none" stroke-linejoin="round"/>',
  retry:'<path d="M15.8 8.4A6 6 0 1 0 16.4 11" stroke="currentColor" stroke-width="1.3" fill="none" stroke-linecap="round"/><path d="M16.4 4.6v4.4h-4.4" stroke="currentColor" stroke-width="1.3" fill="none" stroke-linecap="round" stroke-linejoin="round"/>',
  stop:'<rect x="6" y="6" width="8" height="8" rx="1.4" fill="currentColor"/>',
  trash:'<path d="M4.5 6.2h11M8.2 6.2V4.6a1 1 0 0 1 1-1h1.6a1 1 0 0 1 1 1v1.6M6.3 6.2 6.9 16a1 1 0 0 0 1 .9h4.2a1 1 0 0 0 1-.9l.6-9.8" stroke="currentColor" stroke-width="1.3" fill="none" stroke-linecap="round" stroke-linejoin="round"/>',
  archive2:'<rect x="3.4" y="4" width="13.2" height="3.4" rx="1" stroke="currentColor" stroke-width="1.3" fill="none"/><path d="M4.6 7.4v7.4a1.4 1.4 0 0 0 1.4 1.4h8a1.4 1.4 0 0 0 1.4-1.4V7.4M8.2 10.6h3.6" stroke="currentColor" stroke-width="1.3" fill="none" stroke-linecap="round"/>',
  close:'<path d="M5 5l10 10M15 5 5 15" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>',
  sun:'<circle cx="10" cy="10" r="3.4" stroke="currentColor" stroke-width="1.3" fill="none"/><path d="M10 2.6v2M10 15.4v2M2.6 10h2M15.4 10h2M4.6 4.6l1.4 1.4M14 14l1.4 1.4M4.6 15.4 6 14M14 6l1.4-1.4" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/>',
  moon:'<path d="M15.5 12.3A6.2 6.2 0 1 1 7.7 4.5a5 5 0 0 0 7.8 7.8Z" stroke="currentColor" stroke-width="1.3" fill="currentColor" fill-opacity=".12" stroke-linejoin="round"/>',
  monitor:'<rect x="3" y="4.5" width="14" height="9" rx="1.3" stroke="currentColor" stroke-width="1.3" fill="none"/><path d="M7.5 16.5h5M10 13.5v3" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/>',
  download:'<path d="M10 3.5v9M6.2 9.2 10 13l3.8-3.8" stroke="currentColor" stroke-width="1.3" fill="none" stroke-linecap="round" stroke-linejoin="round"/><path d="M4 15.5h12" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/>',
  memory:'<path d="M10 3.4c-2.3 0-3.7 1.5-3.7 3.4 0 .9.3 1.6.9 2.2-1 .5-1.5 1.3-1.5 2.4 0 1.9 1.6 3.2 3.5 3.2h1.6c1.9 0 3.5-1.3 3.5-3.2 0-1.1-.5-1.9-1.5-2.4.6-.6.9-1.3.9-2.2 0-1.9-1.4-3.4-3.7-3.4Z" stroke="currentColor" stroke-width="1.3" fill="none" stroke-linejoin="round"/><path d="M8.2 8.2h3.6M7.9 10.9h4.2" stroke="currentColor" stroke-width="1.1" stroke-linecap="round"/>',
};
const icon=(name,size=16)=>`<svg width="${size}" height="${size}" viewBox="0 0 20 20" fill="none">${ICON[name]||''}</svg>`;

$('newChatBtn').innerHTML=icon('plus',15)+'New chat';
$('menuButton').innerHTML=icon('menu',17);
$('bellBtn').innerHTML=icon('bell',16)+'<span class="bell-dot hidden" id="bellDot"></span>';
$('nav').innerHTML=[
  ['chat','Chat'],['search','Search'],['work','Work'],['mission','Mission Control'],['memory','Memory'],['projects','Projects'],['files','Files'],['history','History'],['settings','Settings'],
].map(([id,label])=>`<button class="nav-item" data-view="${id}"><span class="nav-icon">${icon(id,15)}</span>${label}</button>`).join('');

function closeSidebar(){$('sidebar').classList.remove('open');$('scrim').classList.remove('open');$('menuButton').setAttribute('aria-expanded','false')}
function toggleSidebar(){const open=!$('sidebar').classList.contains('open');$('sidebar').classList.toggle('open',open);$('scrim').classList.toggle('open',open);$('menuButton').setAttribute('aria-expanded',String(open))}
$('menuButton').onclick=toggleSidebar;$('scrim').onclick=closeSidebar;
window.addEventListener('keydown',e=>{if(e.key==='Escape'){closeSidebar();stopBellPanel()}});
$('newChatBtn').onclick=()=>{location.hash='#/chat';closeSidebar()};

/* --------------------------------------------------------------- theme */
function currentThemeMode(){
  try{const t=localStorage.getItem('falguna-theme');if(t==='light'||t==='dark')return t}catch(e){}
  return 'system';
}
function applyTheme(mode){
  try{
    if(mode==='system'){localStorage.removeItem('falguna-theme');delete document.documentElement.dataset.theme}
    else{localStorage.setItem('falguna-theme',mode);document.documentElement.dataset.theme=mode}
  }catch(e){
    if(mode==='system')delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme=mode;
  }
  document.querySelectorAll('.theme-card').forEach(b=>b.classList.toggle('active',b.dataset.theme===mode));
}

/* -------------------------------------------------- notifications / live */
let notifPanelOpen=false;
function stopBellPanel(){notifPanelOpen=false;const p=$('notifPanel');if(p)p.classList.add('hidden')}
async function toggleNotifPanel(){
  notifPanelOpen=!notifPanelOpen;
  $('notifPanel').classList.toggle('hidden',!notifPanelOpen);
  if(notifPanelOpen)await loadNotifications();
}
function notifTarget(n){
  if(n.ref_type==='run')return '#/work/'+n.ref_id;
  if(n.ref_type==='research')return '#/search/'+n.ref_id;
  return '';
}
async function loadNotifications(){
  const panel=$('notifPanel');
  panel.innerHTML='<div class="notif-panel-head"><span>Notifications</span></div><div class="empty-state" style="padding:14px 0">Loading&hellip;</div>';
  let data;
  try{data=await api('/api/notifications')}catch(err){panel.innerHTML=`<div class="notif-panel-head"><span>Notifications</span></div><div class="empty-state error">${esc(err.message)}</div>`;return}
  renderNotifPanel(data.notifications||[]);
}
function renderNotifPanel(list){
  const panel=$('notifPanel');
  panel.innerHTML=`<div class="notif-panel-head"><span>Notifications</span><button type="button" id="notifMarkAll">Mark all read</button></div>`+
    (list.length?list.map(n=>`<button type="button" class="notif-row ${n.read?'':'unread'}" data-notif-open="${esc(notifTarget(n))}" data-notif-id="${esc(n.id)}">
        <div class="n-title">${esc(n.title)}</div>${n.body?`<div class="n-body">${esc(n.body)}</div>`:''}<div class="n-time">${esc(timeAgo(n.created_at))}</div>
      </button>`).join(''):'<div class="empty-state" style="padding:14px 0">Nothing yet.</div>');
  $('notifMarkAll').onclick=async e=>{
    e.stopPropagation();
    try{await api('/api/notifications/read-all',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});await loadNotifications();pollLive()}
    catch(err){showToast(err.message,{error:true})}
  };
  document.querySelectorAll('[data-notif-open]').forEach(b=>b.onclick=async()=>{
    const id=b.dataset.notifId,target=b.dataset.notifOpen;
    try{await api(`/api/notifications/${id}/read`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})}catch(err){}
    stopBellPanel();pollLive();
    if(target)go(target);
  });
}
$('indicatorPill').onclick=()=>go('#/mission');
$('bellBtn').onclick=e=>{e.stopPropagation();toggleNotifPanel()};
document.addEventListener('click',e=>{
  const panel=$('notifPanel');
  if(notifPanelOpen&&panel&&!panel.contains(e.target)&&e.target!==$('bellBtn')&&!$('bellBtn').contains(e.target))stopBellPanel();
});
async function pollLive(){
  let s;
  try{s=await api('/api/live-summary')}catch(err){return}
  const pill=$('indicatorPill');
  const running=s.running||0,needsYou=s.needs_you||0,failed=s.failed||0;
  if(running+needsYou>0){
    pill.classList.remove('hidden');
    pill.innerHTML=`<b>${running}</b> running`+(needsYou?` &middot; <span class="warn-count">${needsYou} needs you</span>`:'')+(failed?` &middot; <span class="bad-count">${failed} failed</span>`:'');
  }else if(failed>0){
    pill.classList.remove('hidden');
    pill.innerHTML=`<span class="bad-count">${failed} failed</span>`;
  }else{
    pill.classList.add('hidden');
  }
  const dot=$('bellDot');
  if(dot)dot.classList.toggle('hidden',!(s.unread_notifications>0));
}
let liveTimer=null;
function startLivePolling(){
  pollLive();
  clearInterval(liveTimer);
  liveTimer=setInterval(()=>{if(document.visibilityState==='visible')pollLive()},6000);
  document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible')pollLive()});
}

/* ---------------------------------------------------------------- router */

function currentRoute(){
  const raw=(location.hash||'#/chat').replace(/^#\/?/,'');
  const parts=raw.split('/');
  return {view:parts[0]||'chat', id:parts[1]?decodeURIComponent(parts[1]):null};
}
function go(hash){location.hash=hash}
const VIEW_TITLES={chat:'Chat',search:'Search',work:'Work',mission:'Mission Control',memory:'Memory',projects:'Projects',files:'Files',history:'History',settings:'Settings'};
function setActiveNav(view){
  document.querySelectorAll('.nav-item').forEach(b=>b.classList.toggle('active',b.dataset.view===view));
  $('viewTitle').textContent=VIEW_TITLES[view]||'Falguna';
}
document.querySelectorAll('.nav-item').forEach(b=>b.onclick=()=>{go('#/'+b.dataset.view);closeSidebar()});

let mcTimer=null;
async function router(){
  const {view,id}=currentRoute();
  setActiveNav(view);
  clearInterval(mcTimer);
  stopBellPanel();
  const vp=$('viewport');
  try{
    if(view==='chat'){await renderSideChats();return renderChatView(id)}
    if(view==='work'){await renderSideMissions();return renderWorkView(id)}
    if(view==='search'){await renderSideResearch();return renderSearchView(id)}
    if(view==='mission'){hideSideList();return renderMissionControlView()}
    if(view==='memory'){hideSideList();return renderMemoryView(id)}
    if(view==='browser'){hideSideList();return renderBrowserSessionView(id)}
    if(view==='projects'){hideSideList();return renderProjectsView()}
    if(view==='files'){hideSideList();return renderFilesView(id)}
    if(view==='history'){hideSideList();return renderHistoryView()}
    if(view==='settings'){hideSideList();return renderSettingsView(id)}
    go('#/chat');
  }catch(err){
    vp.innerHTML=`<div class="page"><div class="empty-state error">${esc(err.message)}</div></div>`;
  }
}
window.addEventListener('hashchange',router);

function hideSideList(){$('sideListTitle').textContent='';$('sideList').innerHTML=''}

async function loadProfiles(){
  if(profileList.length)return;
  const c=await api('/api/config');
  profileList=c.profiles;
  settingsModel=c.model;
  c.profiles.forEach(p=>profiles[p.id]=p);
  $('modelPill').innerHTML=`<span class="dot"></span><span class="label">${esc(settingsModel||'Falguna')}</span>`;
}
loadProfiles().catch(()=>{});

/* ------------------------------------------------------------ sidebar lists */

function groupByRecency(items,dateKey){
  const now=Date.now(),groups={Today:[],Yesterday:[],'Previous 7 days':[],Older:[]};
  for(const item of items){
    const days=(now-new Date(item[dateKey]).getTime())/86400000;
    if(days<1)groups.Today.push(item);
    else if(days<2)groups.Yesterday.push(item);
    else if(days<7)groups['Previous 7 days'].push(item);
    else groups.Older.push(item);
  }
  return Object.entries(groups).filter(([,v])=>v.length);
}

async function renderSideChats(){
  $('sideListTitle').textContent='Recent chats';
  const {conversations}=await api('/api/conversations');
  const {id:activeId}=currentRoute();
  const list=conversations||[];
  if(!list.length){$('sideList').innerHTML='<div class="side-empty">No chats yet.</div>';return}
  $('sideList').innerHTML=groupByRecency(list,'updated_at').map(([label,rows])=>
    `<div class="side-group-label">${esc(label)}</div>`+rows.map(c=>`<button data-open="#/chat/${esc(c.id)}" class="${c.id===activeId?'active':''}" title="${esc(c.title)}"><span class="row-title">${esc(c.title)}</span>${c.last_message_preview?`<span class="row-sub">${esc(c.last_message_preview)}</span>`:''}</button>`).join('')
  ).join('');
  wireSideList();
}
async function renderSideMissions(){
  $('sideListTitle').textContent='Recent missions';
  const {runs}=await api('/api/runs');
  const {id:activeId}=currentRoute();
  const list=(runs||[]).slice(0,20);
  $('sideList').innerHTML=list.length?list.map(r=>`<button data-open="#/work/${esc(r.run_id)}" class="${r.run_id===activeId?'active':''}" title="${esc(r.title)}"><span class="row-title">${esc(r.title)}</span><span class="row-sub">${esc(String(r.status||'unknown').replaceAll('_',' '))}</span></button>`).join(''):'<div class="side-empty">No missions yet.</div>';
  wireSideList();
}
async function renderSideResearch(){
  $('sideListTitle').textContent='Recent research';
  const {research}=await api('/api/research');
  const {id:activeId}=currentRoute();
  const list=research||[];
  if(!list.length){$('sideList').innerHTML='<div class="side-empty">No research yet.</div>';return}
  const statusLabel={DONE:'Answered',FAILED:'Failed',PENDING:'Pending'};
  $('sideList').innerHTML=groupByRecency(list,'updated_at').map(([label,rows])=>
    `<div class="side-group-label">${esc(label)}</div>`+rows.map(r=>`<button data-open="#/search/${esc(r.id)}" class="${r.id===activeId?'active':''}" title="${esc(r.query)}"><span class="row-title">${esc(r.query)}</span><span class="row-sub">${esc(statusLabel[r.status]||r.status)}</span></button>`).join('')
  ).join('');
  wireSideList();
}
function wireSideList(){
  document.querySelectorAll('[data-open]').forEach(b=>b.onclick=()=>{go(b.dataset.open);closeSidebar()});
}
function timeAgo(iso){
  if(!iso)return'';
  const diff=(Date.now()-new Date(iso).getTime())/1000;
  if(diff<60)return'just now';
  if(diff<3600)return Math.floor(diff/60)+'m ago';
  if(diff<86400)return Math.floor(diff/3600)+'h ago';
  return Math.floor(diff/86400)+'d ago';
}

/* --------------------------------------------------------------- Chat view */

const STARTERS=[
  ['spark','Help me think something through'],
  ['chat','Summarize my recent Work missions'],
  ['handoff','Turn an idea into a Work objective'],
];

let pendingAttachments=[];
let currentConversationData=null,currentConversationId=null,editingMessageId=null;
let chatPollGen=0;

async function renderChatView(id){
  await loadProfiles();
  const vp=$('viewport');
  pendingAttachments=[];editingMessageId=null;currentConversationData=null;currentConversationId=id;
  if(!id){
    vp.innerHTML=`
      <div class="chat-view">
        <div class="chat-scroll"><div class="chat-welcome">
          <div class="glow">${icon('spark',24)}</div>
          <h1>How can I help?</h1>
          <p>Ask a question, think something through, or describe what you're working on. Need current information from the web? Try <a href="#/search">Search</a>.</p>
          <div class="chip-row">${STARTERS.map(([ic,label])=>`<button class="chip" data-starter="${esc(label)}">${icon(ic,13)}${esc(label)}</button>`).join('')}</div>
        </div></div>
        ${composerHtml('Start chat',false)}
      </div>`;
    wireComposerChrome(false);
    document.querySelectorAll('[data-starter]').forEach(b=>b.onclick=()=>{$('composerInput').value=b.dataset.starter;$('composerInput').focus()});
    $('composer').addEventListener('submit',async e=>{
      e.preventDefault();
      const content=$('composerInput').value.trim();
      if(!content)return;
      $('sendBtn').disabled=true;
      try{
        const conv=await api('/api/conversations',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:content.slice(0,60)})});
        await api(`/api/conversations/${conv.id}/messages`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({content})});
        go('#/chat/'+conv.id);
      }catch(err){$('sendBtn').disabled=false;showToast(err.message,{error:true})}
    });
    return;
  }
  vp.innerHTML=`<div class="chat-view"><div class="chat-scroll"><div class="selector-row" id="chatToolbar"></div><div class="thread" id="thread"><div class="empty-state">Loading conversation&hellip;</div></div></div><div id="handoffMount"></div>${composerHtml('Send',true)}</div>`;
  wireComposerChrome(true);
  wireAttachUpload(id);
  renderPendingAttachRow();
  let data;
  try{
    data=await api('/api/conversations/'+id);
  }catch(err){
    $('thread').innerHTML=`<div class="empty-state error">${esc(err.message)}</div>`;
    return;
  }
  renderChatToolbar(id,data.conversation);
  renderThread(id,data);
  const last=data.messages[data.messages.length-1];
  if(last&&last.role==='assistant'&&['PENDING','GENERATING'].includes(last.status)){
    pollConversationUntilSettled(id);
  }
  $('composer').addEventListener('submit',async e=>{
    e.preventDefault();
    const content=$('composerInput').value.trim();
    if(!content)return;
    const attachIds=pendingAttachments.map(a=>a.id);
    const attachSnapshot=pendingAttachments.slice();
    $('composerInput').value='';$('composerInput').style.height='auto';
    pendingAttachments=[];renderPendingAttachRow();
    $('sendBtn').disabled=true;
    appendOptimisticUserBubble(content,attachSnapshot);
    try{
      await api(`/api/conversations/${id}/messages`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({content,attachment_ids:attachIds})});
      pollConversationUntilSettled(id);
    }catch(err){
      document.querySelectorAll('[data-optimistic]').forEach(n=>n.remove());
      const row=$('thinkingRow');if(row)row.remove();
      $('composerInput').value=content;
      showToast(err.message,{error:true});
    }finally{
      $('sendBtn').disabled=false;
    }
  });
}

async function renderChatToolbar(id,conversation){
  const bar=$('chatToolbar');
  if(!bar)return;
  let cfg;
  try{cfg=await api('/api/config')}catch(err){cfg={available_models:[],work_modes:['FAST','BALANCED','DEEP'],default_work_mode:'BALANCED'}}
  bar.innerHTML=`
    <select class="tiny-select" id="convModel" title="Model for this chat">
      <option value="">Auto (fallback-aware)</option>
      ${cfg.available_models.map(m=>`<option value="${esc(m)}" ${conversation.model_override===m?'selected':''}>${esc(m)}</option>`).join('')}
    </select>
    <select class="tiny-select" id="convWorkMode" title="Work mode for this chat">
      ${cfg.work_modes.map(w=>`<option value="${esc(w)}" ${(conversation.work_mode||cfg.default_work_mode)===w?'selected':''}>${esc(w.charAt(0)+w.slice(1).toLowerCase())}</option>`).join('')}
    </select>
    <div class="compose-spacer" style="flex:1"></div>
    <button type="button" class="msg-action-btn" id="archiveConvBtn">${icon('archive2',12)}Archive</button>
    <button type="button" class="msg-action-btn" id="deleteConvBtn">${icon('trash',12)}Delete</button>
  `;
  $('convModel').onchange=async e=>{
    try{await api(`/api/conversations/${id}/model`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({model:e.target.value||'auto'})})}
    catch(err){showToast(err.message,{error:true})}
  };
  $('convWorkMode').onchange=async e=>{
    try{await api(`/api/conversations/${id}/work-mode`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({work_mode:e.target.value})})}
    catch(err){showToast(err.message,{error:true})}
  };
  $('archiveConvBtn').onclick=async()=>{
    if(!await confirmModal({title:'Archive this chat?',body:'You can still find it again through search afterward.',confirmLabel:'Archive'}))return;
    try{await api(`/api/conversations/${id}/archive`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});renderSideChats();go('#/chat')}
    catch(err){showToast(err.message,{error:true})}
  };
  $('deleteConvBtn').onclick=async()=>{
    if(!await confirmModal({title:'Delete this chat?',body:'This removes it from your chat list.',confirmLabel:'Delete',danger:true}))return;
    try{await api(`/api/conversations/${id}/delete`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});renderSideChats();go('#/chat')}
    catch(err){showToast(err.message,{error:true})}
  };
}

function composerHtml(sendLabel,withExtras){
  return `<div class="composer-wrap">
    ${withExtras?'<div class="composer-attach-row" id="pendingAttachRow"></div>':''}
    <form class="composer" id="composer">
    <textarea id="composerInput" required placeholder="Message Falguna&hellip;" rows="1"></textarea>
    <div class="compose-row">
      <button type="button" class="icon-btn" id="attachBtn" title="${withExtras?'Attach a file':'Attachments are available once a chat exists'}">${icon('clip',16)}</button>
      <span class="mode-chip">${icon('chat',12)}Chat</span>
      <div class="compose-spacer"></div>
      <button class="send-btn" id="sendBtn" type="submit" aria-label="${esc(sendLabel)}">${icon('send',14)}</button>
    </div>
  </form>${withExtras?'<input type="file" id="fileInput" multiple hidden>':''}<div class="compose-foot">Falguna can be wrong. Hand off to Work for changes that need to be verified.</div></div>`;
}
function wireComposerChrome(withExtras){
  const ta=$('composerInput');
  ta.addEventListener('input',()=>{ta.style.height='auto';ta.style.height=Math.min(ta.scrollHeight,180)+'px'});
  ta.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();$('composer').requestSubmit()}});
  if(!withExtras){
    const attach=$('attachBtn');
    if(attach)attach.onclick=()=>showToast('Start the chat first, then attach a file to it.');
  }
}
function renderPendingAttachRow(){
  const row=$('pendingAttachRow');
  if(!row)return;
  row.innerHTML=pendingAttachments.map(a=>`<span class="attach-chip">${icon('clip',11)}${esc(a.filename)}<span class="x" data-remove-pending="${esc(a.id)}">${icon('close',10)}</span></span>`).join('');
  document.querySelectorAll('[data-remove-pending]').forEach(b=>b.onclick=()=>{pendingAttachments=pendingAttachments.filter(a=>a.id!==b.dataset.removePending);renderPendingAttachRow()});
}
function wireAttachUpload(conversationId){
  const input=$('fileInput'),btn=$('attachBtn');
  if(!input||!btn)return;
  btn.onclick=()=>input.click();
  input.onchange=async()=>{
    const files=Array.from(input.files||[]);
    input.value='';
    for(const file of files){
      if(file.size>10*1024*1024){showToast(`${file.name} is larger than the 10MB attachment limit.`,{error:true});continue}
      try{
        const dataUrl=await new Promise((resolve,reject)=>{const r=new FileReader();r.onload=()=>resolve(r.result);r.onerror=reject;r.readAsDataURL(file)});
        const base64=String(dataUrl).split(',')[1]||'';
        const rec=await api(`/api/conversations/${conversationId}/attachments`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({filename:file.name,content_type:file.type||'application/octet-stream',data_base64:base64})});
        pendingAttachments.push(rec);
        renderPendingAttachRow();
      }catch(err){showToast(err.message,{error:true})}
    }
  };
}
function appendOptimisticUserBubble(content,attachments){
  const t=$('thread');
  if(t.querySelector('.empty-state'))t.innerHTML='';
  const chips=(attachments||[]).length?`<div class="attach-chip-row">${attachments.map(a=>`<span class="attach-chip">${icon('clip',11)}${esc(a.filename)}</span>`).join('')}</div>`:'';
  t.insertAdjacentHTML('beforeend',`<div class="msg user" data-optimistic="1"><div class="avatar">Y</div><div class="bubble">${nl2br(content)}${chips}</div></div><div class="msg assistant" id="thinkingRow"><div class="avatar">F</div><div class="bubble thinking"><span class="tdot"></span><span class="tdot"></span><span class="tdot"></span></div></div>`);
  t.closest('.chat-scroll').scrollTop=9e6;
}

/* Chat state is polled from the conversation itself (never from a
   remembered operation token) -- this is what makes it safe to navigate
   away mid-generation and come back: the thread just shows whatever the
   real chat_messages.status is, truthfully, until it reaches a terminal
   state. See ConversationStore.add_pending_message's docstring. */
function pollConversationUntilSettled(id){
  const gen=++chatPollGen;
  const tick=async()=>{
    if(gen!==chatPollGen)return;
    let data;
    try{data=await api('/api/conversations/'+id)}catch(err){return}
    if(gen!==chatPollGen)return;
    if(currentRoute().view==='chat'&&currentRoute().id===id)renderThread(id,data);
    const last=data.messages[data.messages.length-1];
    if(last&&last.role==='assistant'&&['PENDING','GENERATING'].includes(last.status)){
      setTimeout(tick,900);
    }else if(currentRoute().view==='chat'&&currentRoute().id===id){
      renderSideChats();
    }
  };
  tick();
}

function renderMessageRow(m,attachments,isLast){
  if(editingMessageId===m.id){
    return `<div class="msg ${esc(m.role)} editing"><div class="avatar">${m.role==='user'?'Y':'F'}</div><div class="bubble">
      <textarea id="editArea">${esc(m.content)}</textarea>
      <div class="edit-actions"><button type="button" class="pill-btn" data-cancel-edit="1">Cancel</button><button type="button" class="action" data-save-edit="${esc(m.id)}">Save &amp; resubmit</button></div>
    </div></div>`;
  }
  const chips=(attachments||[]).length?`<div class="attach-chip-row">${attachments.map(a=>`<a class="attach-chip" href="/api/attachments/${esc(a.id)}" download>${icon('clip',11)}${esc(a.filename)}</a>`).join('')}</div>`:'';
  if(m.role==='user'){
    return `<div class="msg user"><div class="avatar">Y</div><div class="bubble">${nl2br(m.content)}${chips}</div>
      <div class="msg-actions">
        <button type="button" class="msg-action-btn" data-copy-text="${esc(m.content)}">${icon('copy',12)}Copy</button>
        <button type="button" class="msg-action-btn" data-edit-id="${esc(m.id)}">${icon('edit',12)}Edit</button>
      </div></div>`;
  }
  const status=m.status||'COMPLETED';
  if(status==='PENDING'||status==='GENERATING'){
    return `<div class="msg assistant"><div class="avatar">F</div><div class="bubble thinking"><span class="tdot"></span><span class="tdot"></span><span class="tdot"></span></div>
      <div class="status-note">${status==='PENDING'?'Queued':'Thinking'}&hellip; <span data-since="${esc(m.created_at)}">0s</span>
        <button type="button" class="msg-action-btn" data-stop-id="${esc(m.id)}">${icon('stop',11)}Stop</button>
      </div></div>`;
  }
  if(status==='CANCELLED'){
    return `<div class="msg assistant"><div class="avatar">F</div><div class="bubble error">Generation stopped.</div>
      ${isLast?`<div class="msg-actions"><button type="button" class="msg-action-btn" data-regen-id="${esc(m.id)}">${icon('retry',12)}Retry</button></div>`:''}</div>`;
  }
  if(status==='FAILED'||m.error){
    // Falguna V2.1 sanitized error UX: m.error is already the safe,
    // user-facing sentence a FalgunaModelError/ChatError composed (see
    // falguna/providers.py + falguna/chat.py) -- never raw provider
    // stdout. m.error_category picks which recovery actions make sense;
    // m.error_detail (if any) is raw diagnostic text, shown only behind
    // an explicit "Technical details" toggle, never by default.
    const hint=ERROR_CATEGORY_HINTS[m.error_category]||ERROR_CATEGORY_HINTS.TRANSPORT_FAILURE;
    return `<div class="msg assistant"><div class="avatar">F</div><div class="bubble error">
        ${m.error_category?`<div class="error-cat">${esc(hint.label)}</div>`:''}
        <div>${esc(m.error||'This reply failed.')}</div>
        <div class="error-actions">
          ${isLast?`<button type="button" data-regen-id="${esc(m.id)}">${icon('retry',11)}Retry</button>`:''}
          ${hint.showSettingsLink?'<a href="#/settings/models">Change model &rarr;</a>':''}
          ${m.error_detail?`<details><summary>Technical details</summary><div class="error-detail">${esc(m.error_detail)}</div></details>`:''}
        </div>
      </div></div>`;
  }
  // Memory & Knowledge V2 (Pass F/G): when retrieval actually assembled
  // something into this reply's context, memory_context_json carries
  // exactly which memory/knowledge items -- shown as real citations, never
  // implied when nothing was used (memory_context_json is null/absent for
  // every message before this feature existed and for every reply where
  // retrieval found nothing relevant).
  let memoryNote='';
  if(m.memory_context_json){
    try{
      const mc=JSON.parse(m.memory_context_json);
      const cites=(mc.citations||[]);
      if(cites.length)memoryNote=`<details class="memory-sources"><summary>${icon('memory',11)}Used ${cites.length} saved ${cites.length===1?'item':'items'} from Memory</summary>
        <div class="memory-source-list">${cites.map(c=>`<div class="memory-source-row"><span class="status-pill">${esc(c.type)}${c.kind?' &middot; '+esc(c.kind):''}</span> ${esc(c.preview||c.title||'')}</div>`).join('')}</div>
      </details>`;
    }catch(e){}
  }
  // memoryNote is rendered INSIDE the bubble (not as a flex sibling of it)
  // so its citation card's own width can never steal flex space from the
  // reply text -- a real, verified bug (the bubble is `flex:1;min-width:0`
  // in a non-wrapping `.msg{display:flex}` row, so an unconstrained
  // sibling here squeezed the reply text down to a near-zero-width,
  // one-character-per-line column). Keeping it inside the bubble's normal
  // block flow avoids the flex row entirely.
  return `<div class="msg assistant"><div class="avatar">F</div><div class="bubble">${renderMarkdown(m.content)}${memoryNote}</div>
    <div class="msg-actions">
      <button type="button" class="msg-action-btn" data-copy-text="${esc(m.content)}">${icon('copy',12)}Copy</button>
      ${isLast?`<button type="button" class="msg-action-btn" data-regen-id="${esc(m.id)}">${icon('retry',12)}Regenerate</button>`:''}
    </div></div>`;
}

function wireThreadActions(id){
  const t=$('thread');
  if(!t||t.dataset.wired)return;
  t.dataset.wired='1';
  t.addEventListener('click',async e=>{
    const copyBtn=e.target.closest('[data-copy-text]');
    if(copyBtn){
      try{await navigator.clipboard.writeText(copyBtn.dataset.copyText);showToast('Copied to clipboard')}
      catch(err){showToast('Could not copy automatically -- select the text and copy it manually.',{error:true})}
      return;
    }
    const editBtn=e.target.closest('[data-edit-id]');
    if(editBtn){
      editingMessageId=editBtn.dataset.editId;
      renderThread(id,currentConversationData);
      const area=$('editArea');
      if(area){area.focus();area.setSelectionRange(area.value.length,area.value.length)}
      return;
    }
    const cancelEdit=e.target.closest('[data-cancel-edit]');
    if(cancelEdit){editingMessageId=null;renderThread(id,currentConversationData);return}
    const saveEdit=e.target.closest('[data-save-edit]');
    if(saveEdit){
      const mid=saveEdit.dataset.saveEdit;
      const content=$('editArea').value.trim();
      if(!content){showToast('Message cannot be empty',{error:true});return}
      editingMessageId=null;
      try{
        await api(`/api/conversations/${id}/messages/${mid}/edit`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({content})});
        pollConversationUntilSettled(id);
      }catch(err){showToast(err.message,{error:true});renderThread(id,currentConversationData)}
      return;
    }
    const stopBtn=e.target.closest('[data-stop-id]');
    if(stopBtn){
      stopBtn.disabled=true;
      try{await api(`/api/conversations/${id}/messages/${stopBtn.dataset.stopId}/stop`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})}
      catch(err){/* 400 just means it already reached a terminal state first -- not a real error */}
      pollConversationUntilSettled(id);
      return;
    }
    const regenBtn=e.target.closest('[data-regen-id]');
    if(regenBtn){
      regenBtn.disabled=true;
      try{
        await api(`/api/conversations/${id}/messages/${regenBtn.dataset.regenId}/regenerate`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
        pollConversationUntilSettled(id);
      }catch(err){showToast(err.message,{error:true})}
      return;
    }
  });
}

function renderThread(id,data){
  currentConversationData=data;currentConversationId=id;
  const {conversation,messages,handoffs,attachments}=data;
  document.title='Falguna · '+conversation.title;
  const attachByMsg={};
  (attachments||[]).forEach(a=>{if(a.message_id)(attachByMsg[a.message_id]=attachByMsg[a.message_id]||[]).push(a)});
  const t=$('thread');
  if(!messages.length){
    t.innerHTML='<div class="empty-state">Say something to get started.</div>';
  }else{
    const lastId=messages[messages.length-1].id;
    t.innerHTML=messages.map(m=>renderMessageRow(m,attachByMsg[m.id]||[],m.id===lastId)).join('');
  }
  wireThreadActions(id);
  const last=messages[messages.length-1];
  const suggestion=last&&last.role==='assistant'&&last.status==='COMPLETED'&&last.suggested_objective;
  renderHandoffPanel(conversation,suggestion||'',handoffs);
  $('thread').closest('.chat-scroll').scrollTop=9e6;
}
function renderHandoffPanel(conversation,suggested,handoffs){
  // Falguna V2.1: a compact trigger, not an always-expanded form -- see the
  // .handoff-trigger-row CSS comment. Clicking it is the only way this
  // modal ever opens; nothing here is wired to a chat error state.
  const mount=$('handoffMount');
  const priorRuns=(handoffs||[]).map(h=>`<a href="#/work/${esc(h.run_id)}">${esc(new Date(h.created_at).toLocaleString())}</a>`).join(' &middot; ');
  mount.innerHTML=`<div class="handoff-trigger-row">
    <button type="button" class="pill-btn handoff-trigger" id="handoffTriggerBtn">${icon('handoff',13)}Continue in Work</button>
    ${priorRuns?`<span class="handoff-prior">Already started: ${priorRuns}</span>`:''}
  </div>`;
  $('handoffTriggerBtn').onclick=()=>openHandoffModal(conversation,suggested);
}

function openHandoffModal(conversation,suggested){
  const openOptions=profileList.map(p=>`<option value="${esc(p.id)}" ${p.id===conversation.project_id?'selected':''}>${esc(p.name)}</option>`).join('');
  const scrim=document.createElement('div');
  scrim.className='modal-scrim';
  scrim.innerHTML=`<div class="modal handoff-modal">
    <h3>${icon('handoff',15)}Continue in Work</h3>
    <p>Chat can't touch a repository itself. Work runs in an isolated worktree with verification, review, and a human approval gate -- nothing merges without your decision.</p>
    <label>Approved project</label>
    <select id="handoffProject">${openOptions}</select>
    <label>Bounded objective for Work</label>
    <textarea id="handoffObjective" placeholder="Describe one small, testable outcome.">${esc(suggested||'')}</textarea>
    <div class="modal-actions"><button type="button" class="pill-btn" id="handoffCancel">Cancel</button><button type="button" class="action" id="handoffStart">Start Work mission</button></div>
  </div>`;
  document.body.appendChild(scrim);
  const close=()=>{scrim.remove();document.removeEventListener('keydown',onKey)};
  function onKey(e){if(e.key==='Escape')close()}
  document.addEventListener('keydown',onKey);
  scrim.addEventListener('click',e=>{if(e.target===scrim)close()});
  scrim.querySelector('#handoffCancel').onclick=close;
  scrim.querySelector('#handoffStart').onclick=async()=>{
    const project_id=$('handoffProject').value;
    const objective=$('handoffObjective').value.trim();
    if(!project_id||objective.length<12){showToast('Choose a project and describe a bounded objective (12+ characters).',{error:true});return}
    const btn=scrim.querySelector('#handoffStart');
    btn.disabled=true;btn.textContent='Starting…';
    try{
      const {id}=currentRoute();
      const out=await api(`/api/conversations/${id}/handoff`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project_id,objective})});
      close();
      watchOperation(out.operation,runId=>go('#/work/'+runId));
    }catch(err){
      btn.disabled=false;btn.textContent='Start Work mission';
      showToast(err.message+(err.data&&err.data.discovery?' -- discovery needs a narrower objective.':''),{error:true});
    }
  };
}

function openNewBrowserTaskModal(){
  // Falguna Browser + Computer Use V1: project scoping for browser tasks
  // mirrors openHandoffModal's project picker exactly (same profileList,
  // same approved-project registry -- falguna/project_profiles.json via
  // load_profiles, never a second project list). Unlike Work's handoff
  // modal, an unscoped task is allowed here (existing policy: web.py's
  // _start_browser_session only validates project_id when one is given),
  // so the picker always offers "No project" as a real, selectable option.
  loadProfiles().catch(()=>{}).then(()=>{
    const openOptions=profileList.map(p=>`<option value="${esc(p.id)}">${esc(p.name)}</option>`).join('');
    const scrim=document.createElement('div');
    scrim.className='modal-scrim';
    scrim.innerHTML=`<div class="modal handoff-modal">
      <h3>${icon('handoff',15)}New browser task</h3>
      <p>Falguna drives a real browser to complete this. Files and evidence it produces inherit whichever project you pick here.</p>
      <label>Project</label>
      <select id="nbtProject"><option value="">No project (unscoped)</option>${openOptions}</select>
      <label>What should Falguna do in the browser?</label>
      <textarea id="nbtObjective" placeholder="Describe the browser task."></textarea>
      <div class="modal-actions"><button type="button" class="pill-btn" id="nbtCancel">Cancel</button><button type="button" class="action" id="nbtStart">Start browser task</button></div>
    </div>`;
    document.body.appendChild(scrim);
    const close=()=>{scrim.remove();document.removeEventListener('keydown',onKey)};
    function onKey(e){if(e.key==='Escape')close()}
    document.addEventListener('keydown',onKey);
    scrim.addEventListener('click',e=>{if(e.target===scrim)close()});
    scrim.querySelector('#nbtCancel').onclick=close;
    scrim.querySelector('#nbtStart').onclick=async()=>{
      const project_id=$('nbtProject').value||undefined;
      const objective=$('nbtObjective').value.trim();
      if(!objective){showToast('Describe what Falguna should do in the browser.',{error:true});return}
      const btn=scrim.querySelector('#nbtStart');
      btn.disabled=true;btn.textContent='Starting…';
      try{
        const out=await api('/api/browser/sessions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({objective,project_id})});
        close();
        go('#/browser/'+out.session_id);
      }catch(err){
        btn.disabled=false;btn.textContent='Start browser task';
        showToast(err.message,{error:true});
      }
    };
  });
}

/* --------------------------------------------------------------- Work view */

async function renderWorkView(runId){
  await loadProfiles();
  const vp=$('viewport');
  if(!runId){
    vp.innerHTML=`<div class="work-view"><div class="work-empty">
      <h1>Start a new mission</h1>
      <p>Falguna discovers the safe file scope and native verification, then works in an isolated worktree.</p>
      <form class="mission-form" id="mission">
        <label>Approved project</label><select id="project" required></select>
        <label>Bounded engineering objective</label><textarea id="objective" required placeholder="Describe one small, testable problem."></textarea>
        <label>Optional hard cost cap (USD)</label><input id="cost" type="number" min="0" step="0.01">
        <div class="risk-note" id="risk"></div>
        <div class="handoff-actions"><button class="action" id="run" type="submit">Run mission</button></div>
      </form>
      <p class="compose-foot" style="margin-top:16px">Falguna pauses before editing when discovery is uncertain or scope must expand. Prefer scoping the idea in Chat first, then hand it off here. Pause and cancel take effect at a safe boundary. Human approval stays required; no merge or deploy.</p>
    </div></div>`;
    $('project').innerHTML=profileList.map(p=>`<option value="${esc(p.id)}">${esc(p.name)}</option>`).join('');
    const selectProject=()=>{const p=profiles[$('project').value];if(!p)return;$('cost').value=p.default_budget_usd;$('cost').max=p.default_budget_usd;$('risk').textContent='Risk: '+p.risk};
    $('project').addEventListener('change',selectProject);selectProject();
    $('mission').addEventListener('submit',async e=>{
      e.preventDefault();$('run').disabled=true;
      try{
        const out=await api('/api/runs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project:$('project').value,objective:$('objective').value,max_cost_usd:Number($('cost').value)})});
        watchOperation(out.operation,id=>go('#/work/'+id));
      }catch(err){$('run').disabled=false;showToast(err.message,{error:true})}
    });
    return;
  }
  vp.innerHTML=`<div class="work-view"><div class="work-thread" id="workThread"><div class="empty-state">Loading mission&hellip;</div></div></div>`;
  await refreshWork(runId);
}
async function refreshWork(runId){
  let d;
  try{d=await api('/api/runs/'+runId)}catch(err){$('workThread').innerHTML=`<div class="empty-state error">${esc(err.message)}</div>`;return}
  renderWorkThread(runId,d);
}
function renderWorkThread(runId,d){
  const wt=$('workThread');
  const handoffs=d.conversation_handoffs||[];
  const researchHandoffs=d.research_handoffs||[];
  const origin=handoffs.length
    ?`<div class="origin-banner"><span>Started from a Chat handoff</span><a href="#/chat/${esc(handoffs[0].conversation_id)}">Open the conversation</a></div>`
    :researchHandoffs.length?`<div class="origin-banner"><span>Started from a Search handoff</span><a href="#/search/${esc(researchHandoffs[0].research_id)}">Open the research</a></div>`:'';
  const terminal=['DONE_CANDIDATE','FAILED','QUARANTINED','PAUSED','CANCELLED'].includes(d.status);
  wt.innerHTML=`${origin}<div class="work-header">
      <div class="objective">${esc(d.objective||d.mission||'')}</div>
      <div class="status-line">${esc((d.supervisor&&d.supervisor.phase||d.current_milestone||d.status||'').toString().replaceAll('_',' '))}</div>
      <div class="error">${d.failure?esc(`${d.failure.category}: ${d.failure.message}. ${d.failure.action}${d.action_disabled_reason?' Action unavailable: '+d.action_disabled_reason:''}`):''}</div>
      <div class="timeline">${(d.progress_events||[]).map(x=>`<span class="step">${esc(x.label)}</span>`).join('')}</div>
    </div>
    <div class="cards hidden" id="cards"></div>
    <div class="details hidden" id="details"></div>
    <div class="actions hidden" id="actions">
      <button class="action secondary" id="pause">Pause safely</button>
      <button class="action danger" id="cancel">Cancel</button>
      <button class="action secondary" id="resume">Resume / Retry</button>
      <button class="action" data-action="approve">Approve</button>
      <button class="action danger" data-action="reject">Reject</button>
      <button class="action secondary" data-action="request-changes">Request Changes</button>
    </div>`;
  if(terminal){renderEvidence(runId,d)}else{$('actions').classList.remove('hidden');renderControls(runId,d)}
  wireWorkActions(runId);
}
function renderControls(runId,d){
  const controls=d.available_controls||[];
  $('pause').classList.toggle('hidden',!controls.includes('Pause'));
  $('cancel').classList.toggle('hidden',!controls.includes('Cancel'));
  $('resume').classList.toggle('hidden',!controls.some(x=>['Resume','Retry'].includes(x)));
  document.querySelectorAll('[data-action]').forEach(b=>b.classList.toggle('hidden',d.status!=='DONE_CANDIDATE'||d.merge_approval!=='PENDING'));
}
function renderEvidence(runId,d){
  const values=[['Status',d.status],['Needs approval',d.merge_approval||'No'],['Tests',(d.native_tests||[]).length?d.native_tests.filter(x=>x.passed).length+'/'+d.native_tests.length:'Not run'],['Review',d.independent_review||'Not run'],['Cost','$'+Number(d.cost_usd||0).toFixed(4)],['Cache',d.discovery_cache||'Not recorded'],['Total time',((d.timings_ms||{}).total||0)+' ms'],['Evidence',d.evidence_hashes_valid?'Valid':'Not proven']];
  $('cards').innerHTML=values.map(x=>`<div class="card"><span>${esc(x[0])}</span><b>${esc(x[1])}</b></div>`).join('');
  $('cards').classList.remove('hidden');
  $('details').innerHTML=`<div><span>Files changed</span>${esc((d.files_changed||[]).join(', ')||'None')}</div><div><span>Native verification</span>${esc((d.native_tests||[]).map(x=>x.label+': '+(x.passed?'passed':'failed')).join(' · ')||'Not run')}</div><div><span>Stage timings</span>${esc(Object.entries(d.timings_ms||{}).map(x=>x[0]+': '+x[1]+' ms').join(' · ')||'Not recorded')}</div><div><span>Independent review / audit</span>${esc(d.independent_review||'Not run')} · audit ${d.audit_chain_valid?'valid':'not proven'} · protected-main merges 0</div><div><span>Unresolved issues</span>${esc((d.unresolved_issues||[]).join('; ')||'None')}</div>`;
  $('details').classList.remove('hidden');
  $('actions').classList.remove('hidden');
  renderControls(runId,d);
}
function wireWorkActions(runId){
  document.querySelectorAll('[data-action]').forEach(b=>b.onclick=async()=>{
    const reason=prompt('Reason for this decision:');if(!reason)return;
    try{
      await api(`/api/runs/${runId}/decision`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:b.dataset.action,reason})});
      await refreshWork(runId);pollLive();
    }catch(err){showToast(err.message,{error:true})}
  });
  const resumeBtn=$('resume');
  if(resumeBtn)resumeBtn.onclick=async()=>{
    try{const out=await api(`/api/runs/${runId}/resume`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});resumeBtn.disabled=true;watchOperation(out.operation,()=>{refreshWork(runId);pollLive()})}
    catch(err){showToast(err.message,{error:true})}
  };
  const pauseBtn=$('pause');
  if(pauseBtn)pauseBtn.onclick=async()=>{
    try{await api(`/api/runs/${runId}/pause`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});await refreshWork(runId);pollLive()}
    catch(err){showToast(err.message,{error:true})}
  };
  const cancelBtn=$('cancel');
  if(cancelBtn)cancelBtn.onclick=async()=>{
    try{await api(`/api/runs/${runId}/cancel`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});await refreshWork(runId);pollLive()}
    catch(err){showToast(err.message,{error:true})}
  };
}
let operationTimer=null;
async function watchOperation(token,onRunId){
  clearTimeout(operationTimer);
  try{
    const op=await api('/api/operations/'+token);
    if(op.run_id&&onRunId){onRunId(op.run_id);onRunId=null}
    if(op.state==='FAILED'){showToast(op.error||'Operation failed',{error:true});return}
    if(op.state!=='COMPLETE')operationTimer=setTimeout(()=>watchOperation(token,onRunId),800);
    else{renderSideMissions();pollLive()}
  }catch(err){showToast(err.message,{error:true})}
}

/* ------------------------------------------------------------- Search view */

async function renderSearchView(id){
  await loadProfiles();
  const vp=$('viewport');
  if(!id){
    vp.innerHTML=`<div class="page" style="max-width:680px">
      <div class="chat-welcome" style="margin-top:6vh">
        <div class="glow">${icon('search',22)}</div>
        <h1>Research anything</h1>
        <p>Falguna searches the web, keeps citations, and lets you continue the findings into Chat or hand them to Work.</p>
      </div>
      <form class="mission-form" id="researchForm" style="max-width:560px;margin:24px auto 0;text-align:left">
        <label>What do you want to research?</label>
        <textarea id="researchQuery" required placeholder="e.g. research this company, compare these two libraries, find current documentation for..."></textarea>
        <label>Attach to a project (optional)</label>
        <select id="researchProject"><option value="">No project</option>${profileList.map(p=>`<option value="${esc(p.id)}">${esc(p.name)}</option>`).join('')}</select>
        <div class="handoff-actions"><button class="action" id="researchGo" type="submit">${icon('search',13)}Research</button></div>
      </form>
      <details class="disclosure" style="max-width:560px;margin:24px auto 0">
        <summary>Find an existing chat or mission instead</summary>
        <div class="searchbar" style="margin-top:12px"><input id="quickQ" placeholder="Search chats and missions&hellip;"><button id="quickGo" type="button">Search</button></div>
        <div class="result-list" id="quickResults"></div>
      </details>
    </div>`;
    $('researchForm').addEventListener('submit',async e=>{
      e.preventDefault();
      const query=$('researchQuery').value.trim();
      if(query.length<3)return;
      $('researchGo').disabled=true;$('researchGo').textContent='Researching…';
      try{
        const out=await api('/api/research',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({query,project_id:$('researchProject').value||undefined})});
        go('#/search/'+out.research.id);
      }catch(err){
        $('researchGo').disabled=false;$('researchGo').innerHTML=icon('search',13)+'Research';
        showToast(err.message,{error:true});
      }
    });
    const runQuick=async()=>{
      const q=$('quickQ').value.trim();
      if(!q){$('quickResults').innerHTML='';return}
      const {results}=await api('/api/search?q='+encodeURIComponent(q));
      $('quickResults').innerHTML=results.length?results.map(r=>r.type==='conversation'
        ?`<button class="result-row" data-open="#/chat/${esc(r.id)}"><div class="kind">Chat</div><div class="title">${esc(r.title)}</div><div class="meta">${esc(timeAgo(r.updated_at))}</div></button>`
        :`<button class="result-row" data-open="#/work/${esc(r.run_id)}"><div class="kind">Work &middot; ${esc(String(r.status||'').replaceAll('_',' '))}</div><div class="title">${esc(r.title)}</div><div class="meta">${esc(r.repository||'')}</div></button>`
      ).join(''):'<div class="empty-state">No matches.</div>';
      document.querySelectorAll('#quickResults [data-open]').forEach(b=>b.onclick=()=>go(b.dataset.open));
    };
    $('quickGo').onclick=runQuick;
    $('quickQ').addEventListener('keydown',e=>{if(e.key==='Enter')runQuick()});
    return;
  }
  vp.innerHTML=`<div class="research-detail-view" id="researchDetail"><div class="empty-state">Loading research&hellip;</div></div>`;
  let data;
  try{
    data=await api('/api/research/'+id);
  }catch(err){
    $('researchDetail').innerHTML=`<div class="empty-state error">${esc(err.message)}</div>`;
    return;
  }
  renderResearchDetail(id,data);
}

function renderResearchDetail(id,data){
  const {research,sources,citations,handoffs}=data;
  document.title='Falguna · '+research.query;
  const mount=$('researchDetail');
  const failed=research.status==='FAILED';
  const citeBySource={};
  (citations||[]).forEach(c=>{(citeBySource[c.source_id]=citeBySource[c.source_id]||[]).push(c.claim)});
  const sourceCards=(sources||[]).map((s,i)=>`
    <a class="source-card" href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">
      <div><span class="idx">${i+1}</span><span class="s-title">${esc(s.title||s.url)}</span></div>
      <div class="s-meta">${esc(s.domain||'')}${s.published_at?' &middot; published '+esc(s.published_at):''} &middot; retrieved ${esc(timeAgo(s.retrieved_at))}</div>
      ${(citeBySource[s.id]||[]).length?`<div class="s-meta">Cited for: ${esc(citeBySource[s.id].join('; '))}</div>`:''}
    </a>`).join('');
  const priorRuns=(handoffs||[]).map(h=>`<a href="#/work/${esc(h.run_id)}">${esc(new Date(h.created_at).toLocaleString())}</a>`).join(' &middot; ');
  mount.innerHTML=`
    <div class="answer-card">
      <div class="q">${esc(research.query)}</div>
      <div class="a${failed?' error':''}">${failed?esc(research.error||'This research failed.'):esc(research.answer||'')}</div>
      ${sources&&sources.length?`<details class="disclosure" open><summary>Sources (${sources.length})</summary><div class="source-list">${sourceCards}</div></details>`:''}
    </div>
    ${failed?'':`<div class="research-actions"><button class="action secondary" id="continueChatBtn">${icon('chat',13)}Continue in Chat</button></div>
    <div class="handoff-panel" style="margin-top:16px">
      <h3>${icon('handoff',15)}Turn into Work</h3>
      <div class="sub">Search can't touch a repository itself. Work runs in an isolated worktree with verification, review, and a human approval gate.${priorRuns?` Already started: ${priorRuns}`:''}</div>
      <label>Approved project</label>
      <select id="researchHandoffProject">${profileList.map(p=>`<option value="${esc(p.id)}" ${p.id===research.project_id?'selected':''}>${esc(p.name)}</option>`).join('')}</select>
      <label>Bounded objective for Work</label>
      <textarea id="researchHandoffObjective" placeholder="Describe one small, testable outcome.">${esc(research.suggested_objective||'')}</textarea>
      <div class="handoff-actions"><button class="action" id="researchHandoffBtn" type="button">Start Work mission</button></div>
    </div>`}`;
  if(failed)return;
  $('continueChatBtn').onclick=async()=>{
    $('continueChatBtn').disabled=true;
    try{
      const out=await api(`/api/research/${id}/continue-chat`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
      go('#/chat/'+out.conversation_id);
    }catch(err){$('continueChatBtn').disabled=false;showToast(err.message,{error:true})}
  };
  $('researchHandoffBtn').onclick=async()=>{
    const project_id=$('researchHandoffProject').value;
    const objective=$('researchHandoffObjective').value.trim();
    if(!project_id||objective.length<12){showToast('Choose a project and describe a bounded objective (12+ characters).',{error:true});return}
    $('researchHandoffBtn').disabled=true;$('researchHandoffBtn').textContent='Starting…';
    try{
      const out=await api(`/api/research/${id}/handoff`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project_id,objective})});
      watchOperation(out.operation,runId=>go('#/work/'+runId));
    }catch(err){
      $('researchHandoffBtn').disabled=false;$('researchHandoffBtn').textContent='Start Work mission';
      showToast(err.message+(err.data&&err.data.discovery?' -- discovery needs a narrower objective.':''),{error:true});
    }
  };
}

/* ----------------------------------------------------------- Projects view */

async function renderProjectsView(){
  await loadProfiles();
  const vp=$('viewport');
  vp.innerHTML=`<div class="page"><h1>Projects</h1><p class="lede">Approved projects only. The browser cannot supply an arbitrary repository.</p>
    <div class="card-list" id="projectCards"></div></div>`;
  $('projectCards').innerHTML=profileList.map(p=>`
    <div class="project-card">
      <div class="title"><span class="mark" style="width:22px;height:22px;font-size:11px;border-radius:7px">${esc(p.name[0]||'P')}</span>${esc(p.name)}</div>
      <div class="path">${esc(p.repository)}</div>
      <div class="risk">${esc(p.risk||'—')} &middot; budget cap <b>$${esc(p.default_budget_usd)}</b></div>
      <div class="project-actions">
        <button class="pill-btn" data-newchat="${esc(p.id)}">New chat for this project</button>
        <button class="pill-btn" data-newmission="${esc(p.id)}">New mission in Work</button>
      </div>
    </div>`).join('') || '<div class="empty-state">No approved projects found.</div>';
  document.querySelectorAll('[data-newchat]').forEach(b=>b.onclick=async()=>{
    const conv=await api('/api/conversations',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:'New chat',project_id:b.dataset.newchat})});
    go('#/chat/'+conv.id);
  });
  document.querySelectorAll('[data-newmission]').forEach(b=>b.onclick=()=>go('#/work'));
}

/* ------------------------------------------------------ Mission Control view */

function mcBrowserControlsHtml(c){
  // Falguna Browser + Computer Use V1 (Section 22, 25): same Archive/Restore
  // board-visibility toggle Engineering Worker cards already have, plus
  // Approve once/Reject/Cancel task for a NEEDS_ARYAN pause -- never a
  // blanket future authorization, only ever this one paused step. `kind` is
  // 'browser' or 'computer' (both stored in browser_sessions, see web.py's
  // _browser_session_card) -- both use the exact same controls/routes, so
  // data-mc-kind carries whichever kind this card actually is rather than a
  // hardcoded 'browser', or a computer-use card would be dispatched wrong.
  const kind=c.kind==='computer'?'computer':'browser';
  const archiveBtn=c.archived
    ?`<button type="button" class="pill-btn" data-mc-action="unarchive" data-mc-kind="${kind}" data-run-id="${esc(c.id)}">Restore</button>`
    :(['CREATED','RUNNING','WAITING'].includes(c.status)?'':`<button type="button" class="pill-btn" data-mc-action="archive" data-mc-kind="${kind}" data-run-id="${esc(c.id)}">Archive</button>`);
  if(c.status==='NEEDS_ARYAN')return `<button type="button" class="pill-btn" data-mc-action="approve" data-mc-kind="${kind}" data-run-id="${esc(c.id)}">Approve once</button><button type="button" class="pill-btn" data-mc-action="reject" data-mc-kind="${kind}" data-run-id="${esc(c.id)}">Reject</button><button type="button" class="pill-btn" data-mc-action="cancel" data-mc-kind="${kind}" data-run-id="${esc(c.id)}">Cancel task</button>`;
  if(['CREATED','RUNNING','WAITING'].includes(c.status))return `<button type="button" class="pill-btn" data-mc-action="cancel" data-mc-kind="${kind}" data-run-id="${esc(c.id)}">Cancel</button>`;
  return archiveBtn;
}
function mcControlsHtml(c){
  if(c.kind==='browser'||c.kind==='computer')return mcBrowserControlsHtml(c);
  // Falguna V2.1: Archive/Restore is a board-visibility toggle only (see
  // _archive_run_from_board) -- available on any resolved card, alongside
  // whatever decision/retry controls that status already offers. It is
  // never available on an actively-running mission.
  const archiveBtn=c.archived
    ?`<button type="button" class="pill-btn" data-mc-action="unarchive" data-run-id="${esc(c.run_id)}">Restore</button>`
    :(['PLANNING','WORKING','VERIFYING','REVIEWING'].includes(c.status)?'':`<button type="button" class="pill-btn" data-mc-action="archive" data-run-id="${esc(c.run_id)}">Archive</button>`);
  if(c.status==='PAUSED')return `<button type="button" class="pill-btn" data-mc-action="resume" data-run-id="${esc(c.run_id)}">Resume</button><button type="button" class="pill-btn" data-mc-action="cancel" data-run-id="${esc(c.run_id)}">Cancel</button>${archiveBtn}`;
  if(c.status==='DONE_CANDIDATE'&&c.merge_approval==='PENDING')return `<button type="button" class="pill-btn" data-mc-action="approve" data-run-id="${esc(c.run_id)}">Approve</button><button type="button" class="pill-btn" data-mc-action="reject" data-run-id="${esc(c.run_id)}">Reject</button><button type="button" class="pill-btn" data-mc-action="request-changes" data-run-id="${esc(c.run_id)}">Changes</button>${archiveBtn}`;
  if(['PLANNING','WORKING','VERIFYING','REVIEWING'].includes(c.status))return `<button type="button" class="pill-btn" data-mc-action="pause" data-run-id="${esc(c.run_id)}">Pause</button><button type="button" class="pill-btn" data-mc-action="cancel" data-run-id="${esc(c.run_id)}">Cancel</button>`;
  if(['FAILED','QUARANTINED'].includes(c.status))return `<button type="button" class="pill-btn" data-mc-action="resume" data-run-id="${esc(c.run_id)}">Retry</button>${archiveBtn}`;
  return archiveBtn;
}
function mcCard(c){
  const needsYou=(c.status==='DONE_CANDIDATE'&&c.merge_approval==='PENDING')||c.status==='PAUSED'||c.status==='NEEDS_ARYAN';
  const kindLabel=c.kind==='browser'?'Browser':c.kind==='computer'?'Computer use':'Work';
  return `<div class="mc-card ${needsYou?'needs-you':''}${c.archived?' archived':''}" data-open-run="${esc(c.id||c.run_id)}" data-mc-kind="${esc(c.kind||'engineering')}">
    <div class="mc-title">${esc(c.title)}</div>
    <div class="mc-meta"><span class="status-pill ${esc((c.status||'').toLowerCase())}">${esc(String(c.status||'').replaceAll('_',' '))}</span><span>${esc(kindLabel)}</span><span>${esc(c.project||'')}</span><span>${esc(c.origin||'')}</span><span>${esc(timeAgo(c.last_activity))}</span>${c.cost_usd?`<span>$${esc(c.cost_usd)}</span>`:''}</div>
    <div class="mc-step">${esc(String(c.current_step||'').replaceAll('_',' '))}</div>
    <div class="mc-controls">${mcControlsHtml(c)}</div>
  </div>`;
}
function wireMcControls(){
  document.querySelectorAll('.mc-card').forEach(card=>{
    card.onclick=e=>{
      if(e.target.closest('[data-mc-action]'))return;
      const id=card.dataset.openRun;
      const isBrowserLike=card.dataset.mcKind==='browser'||card.dataset.mcKind==='computer';
      go(isBrowserLike?'#/browser/'+id:'#/work/'+id);
    };
  });
  document.querySelectorAll('[data-mc-action]').forEach(b=>b.onclick=async e=>{
    e.stopPropagation();
    const runId=b.dataset.runId,action=b.dataset.mcAction;
    try{
      if(b.dataset.mcKind==='browser'||b.dataset.mcKind==='computer'){
        const path=(action==='archive'||action==='unarchive')?`/api/browser/sessions/${runId}/board-${action}`:`/api/browser/sessions/${runId}/${action}`;
        await api(path,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
        await refreshMissionControl();
        pollLive();
        return;
      }
      if(action==='archive'||action==='unarchive'){
        await api(`/api/runs/${runId}/board-${action}`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
        await refreshMissionControl();
      }else if(action==='pause'||action==='cancel'){
        await api(`/api/runs/${runId}/${action}`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
        await refreshMissionControl();
      }else if(action==='resume'){
        const out=await api(`/api/runs/${runId}/resume`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
        watchOperation(out.operation,()=>refreshMissionControl());
      }else{
        const reason=prompt('Reason for this decision:');if(!reason)return;
        await api(`/api/runs/${runId}/decision`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,reason})});
        await refreshMissionControl();
      }
      pollLive();
    }catch(err){showToast(err.message,{error:true})}
  });
}
const MC_BUCKETS=[['needs_you','Needs you','needs-you'],['running','Running',''],['failed','Failed','failed'],['completed','Completed',''],['cancelled','Cancelled','']];
async function refreshMissionControl(){
  const host=$('mcBuckets');
  if(!host)return;
  let board;
  try{board=await api('/api/missions/board')}catch(err){host.innerHTML=`<div class="empty-state error">${esc(err.message)}</div>`;return}
  const activeHtml=MC_BUCKETS.map(([key,label,cls])=>{
    const items=board[key]||[];
    return `<div><div class="mc-bucket-title ${cls}">${esc(label)}<span class="count">${items.length}</span></div>${items.length?`<div class="mc-grid">${items.map(mcCard).join('')}</div>`:'<div class="mc-empty-bucket">Nothing here.</div>'}</div>`;
  }).join('');
  // Falguna V2.1: the Archived bucket is collapsed by default and kept
  // visually de-emphasized -- it exists so a dismissed run is never truly
  // gone, not to compete with the buckets that actually need attention.
  const archivedItems=board.archived||[];
  const archivedHtml=`<details class="mc-archived-bucket">
    <summary>Archived<span class="count">${archivedItems.length}</span></summary>
    ${archivedItems.length?`<div class="mc-grid">${archivedItems.map(mcCard).join('')}</div>`:'<div class="mc-empty-bucket">Nothing archived yet.</div>'}
  </details>`;
  host.innerHTML=activeHtml+archivedHtml;
  wireMcControls();
}
async function renderMissionControlView(){
  const vp=$('viewport');
  vp.innerHTML=`<div class="mc-view">
    <div class="mc-head"><div><h1 style="margin:0;font-size:21px;font-weight:650">Mission Control</h1><p class="lede" style="margin:4px 0 0">Every Work mission and browser task, grouped by what it actually needs from you next.</p></div><button type="button" class="pill-btn" id="newBrowserTaskBtn">New browser task</button></div>
    <div class="mc-buckets" id="mcBuckets"><div class="empty-state">Loading&hellip;</div></div>
  </div>`;
  $('newBrowserTaskBtn').onclick=()=>openNewBrowserTaskModal();
  await refreshMissionControl();
  clearInterval(mcTimer);
  mcTimer=setInterval(()=>{
    if(currentRoute().view==='mission')refreshMissionControl();else clearInterval(mcTimer);
  },5000);
}

/* ------------------------------------------------------------ Browser session view */

async function renderBrowserSessionView(id){
  const vp=$('viewport');
  if(!id){
    vp.innerHTML=`<div class="page"><div class="empty-state">Open a browser task from Mission Control, or start a new one there.</div></div>`;
    return;
  }
  vp.innerHTML=`<div class="page" id="browserSessionPage"><div class="empty-state">Loading&hellip;</div></div>`;
  await refreshBrowserSession(id);
  clearInterval(mcTimer);
  mcTimer=setInterval(()=>{
    const r=currentRoute();
    if(r.view==='browser'&&r.id===id)refreshBrowserSession(id);else clearInterval(mcTimer);
  },3000);
}
async function refreshBrowserSession(id){
  const host=$('browserSessionPage');
  if(!host)return;
  let row;
  try{row=await api('/api/browser/sessions/'+id)}catch(err){host.innerHTML=`<div class="empty-state error">${esc(err.message)}</div>`;return}
  // Section 23: latest screenshot, recent actions, current URL, status --
  // observable without blocking, never full video streaming in V1.
  const shot=row.latest_screenshot_attachment_id
    ?`<img src="/api/attachments/${esc(row.latest_screenshot_attachment_id)}" alt="latest screenshot" style="max-width:100%;border:1px solid var(--border,#333);border-radius:8px" />`
    :'<div class="empty-state">No screenshot yet.</div>';
  const tabsHtml=(row.tabs||[]).map(t=>`<div class="mc-step">Tab ${esc(t.tab_index)}: ${esc(t.url||t.title||'(blank)')}${t.status==='CLOSED'?' (closed)':''}</div>`).join('')||'<div class="mc-step">No tabs yet.</div>';
  const actionsHtml=(row.actions||[]).slice().reverse().slice(0,30).map(a=>`<div class="mc-step">#${esc(a.seq)} ${esc(a.action_type)}${a.target?' '+esc(a.target):''} &mdash; <span class="status-pill ${esc((a.result||'').toLowerCase())}">${esc(a.result)}</span>${a.detail?' &middot; '+esc(a.detail):''}</div>`).join('')||'<div class="mc-step">No actions yet.</div>';
  const downloadsHtml=(row.downloads||[]).map(d=>`<div class="mc-step"><a href="/api/attachments/${esc(d.attachment_id)}">${esc(d.filename)}</a> from ${esc(d.source_url||'')}</div>`).join('');
  let controls='';
  if(row.status==='NEEDS_ARYAN'){
    controls=`<button type="button" class="pill-btn" data-bs-action="approve">Approve once</button><button type="button" class="pill-btn" data-bs-action="reject">Reject</button><button type="button" class="pill-btn" data-bs-action="cancel">Cancel task</button>`;
  }else if(['CREATED','RUNNING','WAITING'].includes(row.status)){
    controls=`<button type="button" class="pill-btn" data-bs-action="cancel">Cancel</button>`;
  }else{
    controls=row.archived
      ?`<button type="button" class="pill-btn" data-bs-action="unarchive">Restore</button>`
      :`<button type="button" class="pill-btn" data-bs-action="archive">Archive</button>`;
  }
  host.innerHTML=`
    <h1 style="font-size:21px;margin:0 0 4px;font-weight:650">${esc(row.objective)}</h1>
    <div class="mc-meta"><span class="status-pill ${esc((row.status||'').toLowerCase())}">${esc(String(row.status||'').replaceAll('_',' '))}</span><span>${esc(row.task_type||'')}</span><span>${esc(row.project_name||'No project')}</span><span>${esc(row.current_url||'')}</span><span>${esc(timeAgo(row.updated_at))}</span></div>
    ${row.needs_aryan_reason?`<p class="lede">Needs you: ${esc(row.needs_aryan_reason)}</p>`:''}
    ${row.error?`<p class="lede error">${esc(row.error)}</p>`:''}
    <div class="mc-controls" style="margin:10px 0">${controls}</div>
    <h2 style="font-size:16px;margin:16px 0 6px">Latest screenshot</h2>
    ${shot}
    <h2 style="font-size:16px;margin:16px 0 6px">Tabs</h2>
    ${tabsHtml}
    ${downloadsHtml?`<h2 style="font-size:16px;margin:16px 0 6px">Downloads</h2>${downloadsHtml}`:''}
    <h2 style="font-size:16px;margin:16px 0 6px">Actions</h2>
    ${actionsHtml}
  `;
  document.querySelectorAll('[data-bs-action]').forEach(b=>b.onclick=async()=>{
    const action=b.dataset.bsAction;
    try{
      const path=(action==='archive'||action==='unarchive')?`/api/browser/sessions/${id}/board-${action}`:`/api/browser/sessions/${id}/${action}`;
      await api(path,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
      await refreshBrowserSession(id);
      pollLive();
    }catch(err){showToast(err.message,{error:true})}
  });
}

/* ------------------------------------------------------------------ Files view */

let filesTab='uploads';
async function renderFilesView(tab){
  filesTab=tab||filesTab||'uploads';
  const vp=$('viewport');
  vp.innerHTML=`<div class="files-view">
    <h1 style="font-size:21px;margin:0 0 4px;font-weight:650">Files</h1>
    <p class="lede">Chat attachments and mission-generated artifacts Falguna already has on disk.</p>
    <div class="files-tabs">
      <button type="button" data-ftab="uploads" class="${filesTab==='uploads'?'active':''}">Uploads</button>
      <button type="button" data-ftab="generated" class="${filesTab==='generated'?'active':''}">Generated</button>
    </div>
    <div class="result-list" id="filesList"><div class="empty-state">Loading&hellip;</div></div>
  </div>`;
  document.querySelectorAll('[data-ftab]').forEach(b=>b.onclick=()=>go('#/files/'+b.dataset.ftab));
  let data;
  try{data=await api('/api/files')}catch(err){$('filesList').innerHTML=`<div class="empty-state error">${esc(err.message)}</div>`;return}
  const rows=filesTab==='generated'?(data.generated||[]):(data.uploads||[]);
  $('filesList').innerHTML=rows.length?rows.map(f=>{
    if(f.type==='upload')return `<div class="file-row"><div><div class="f-name">${esc(f.filename)}</div><div class="f-meta">${esc(f.content_type||'')} &middot; ${esc(formatBytes(f.size_bytes))} &middot; ${esc(timeAgo(f.created_at))}</div></div><div style="display:flex;gap:8px;flex:none">${f.conversation_id?`<a class="pill-btn" href="#/chat/${esc(f.conversation_id)}">Open chat</a>`:''}<a class="pill-btn" href="/api/attachments/${esc(f.id)}" download title="Download">${icon('download',12)}</a></div></div>`;
    if(f.type==='browser_evidence')return `<div class="file-row"><div><div class="f-name">${esc(f.filename)}</div><div class="f-meta">${esc(f.kind||'')} &middot; ${esc(f.mission_title||'')}${f.project_name?' &middot; '+esc(f.project_name):''} &middot; ${esc(timeAgo(f.created_at))}</div></div><a class="pill-btn" href="#/browser/${esc(f.browser_session_id)}">Open browser task</a></div>`;
    return `<div class="file-row"><div><div class="f-name">${esc(f.filename)}</div><div class="f-meta">${esc(f.kind||'')} &middot; ${esc(f.mission_title||'')} &middot; ${esc(timeAgo(f.created_at))}</div></div><a class="pill-btn" href="#/work/${esc(f.run_id)}">Open mission</a></div>`;
  }).join(''):`<div class="empty-state">No ${filesTab==='generated'?'generated files':'uploads'} yet.</div>`;
}

/* ------------------------------------------------------------ History view */

async function renderHistoryView(){
  const vp=$('viewport');
  vp.innerHTML=`<div class="page"><h1>History</h1><p class="lede">Every chat, search, and mission, most recent first.</p>
    <div class="searchbar"><input id="histFilter" placeholder="Filter by title&hellip;"></div>
    <div class="files-tabs" id="histTabs">
      <button type="button" data-htab="all" class="active">All</button>
      <button type="button" data-htab="conversation">Chats</button>
      <button type="button" data-htab="research">Search</button>
      <button type="button" data-htab="mission">Work</button>
    </div>
    <div class="result-list" id="historyList"></div></div>`;
  const [{conversations},{runs},{research}]=await Promise.all([api('/api/conversations'),api('/api/runs'),api('/api/research')]);
  const items=[
    ...(conversations||[]).map(c=>({type:'conversation',id:c.id,title:c.title,updated_at:c.updated_at})),
    ...(runs||[]).map(r=>({type:'mission',run_id:r.run_id,title:r.title,status:r.status,updated_at:r.updated_at,repository:r.repository})),
    ...(research||[]).map(r=>({type:'research',id:r.id,title:r.query,status:r.status,updated_at:r.updated_at})),
  ].sort((a,b)=>new Date(b.updated_at)-new Date(a.updated_at));
  let activeTab='all';
  function renderList(){
    const q=$('histFilter').value.trim().toLowerCase();
    const rows=items.filter(r=>(activeTab==='all'||r.type===activeTab)&&(!q||r.title.toLowerCase().includes(q)));
    $('historyList').innerHTML=rows.length?rows.map(r=>{
      if(r.type==='conversation')return `<button class="result-row" data-open="#/chat/${esc(r.id)}"><div class="kind">Chat</div><div class="title">${esc(r.title)}</div><div class="meta">${esc(timeAgo(r.updated_at))}</div></button>`;
      if(r.type==='research')return `<button class="result-row" data-open="#/search/${esc(r.id)}"><div class="kind">Search · <span class="status-pill ${esc((r.status||'').toLowerCase())}">${esc(String(r.status||'').replaceAll('_',' '))}</span></div><div class="title">${esc(r.title)}</div><div class="meta">${esc(timeAgo(r.updated_at))}</div></button>`;
      return `<button class="result-row" data-open="#/work/${esc(r.run_id)}"><div class="kind">Work · <span class="status-pill ${esc((r.status||'').toLowerCase())}">${esc(String(r.status||'').replaceAll('_',' '))}</span></div><div class="title">${esc(r.title)}</div><div class="meta">${esc(timeAgo(r.updated_at))}</div></button>`;
    }).join(''):'<div class="empty-state">Nothing matches.</div>';
    document.querySelectorAll('#historyList [data-open]').forEach(b=>b.onclick=()=>go(b.dataset.open));
  }
  $('histFilter').addEventListener('input',renderList);
  document.querySelectorAll('#histTabs button').forEach(b=>b.onclick=()=>{
    activeTab=b.dataset.htab;
    document.querySelectorAll('#histTabs button').forEach(x=>x.classList.toggle('active',x===b));
    renderList();
  });
  renderList();
}

/* ------------------------------------------------------------ Memory view */

let memoryTab='memories';
let memoryScopeFilter='';  // '' = all authorized scopes; else 'personal'|'company'|'project:<id>'
let editingMemoryId=null;    // id of the memory record currently shown as an inline edit form, or null
let openHistoryId=null;      // id of the memory record whose History panel is expanded, or null
let memoryHistoryCache={};   // id -> history array, populated lazily on first expand
function memoryScopeParams(){
  if(!memoryScopeFilter)return '';
  const [t,id]=memoryScopeFilter.split(':');
  return `scope_type=${encodeURIComponent(t)}${id?`&scope_id=${encodeURIComponent(id)}`:''}`;
}
function memoryScopeLabel(rec){
  if(rec.scope_type==='project')return 'Project: '+esc((profiles[rec.scope_id]||{}).name||rec.scope_id||'?');
  if(rec.scope_type==='venture')return 'Venture';
  return rec.scope_type==='company'?'Company':'Personal';
}
async function renderMemoryView(tab){
  memoryTab=tab||memoryTab||'memories';
  await loadProfiles().catch(()=>{});
  const vp=$('viewport');
  vp.innerHTML=`<div class="page">
    <h1>Memory</h1>
    <p class="lede">Falguna's own local, provenance-aware memory and knowledge -- scoped to you, a project, or (when explicitly saved that way) the whole company. Offline keyword search always works here; nothing is sent anywhere by browsing this page.</p>
    <div class="files-tabs">
      <button type="button" data-mtab="memories" class="${memoryTab==='memories'?'active':''}">Memories</button>
      <button type="button" data-mtab="knowledge" class="${memoryTab==='knowledge'?'active':''}">Knowledge</button>
      <button type="button" data-mtab="suggestions" class="${memoryTab==='suggestions'?'active':''}">Suggestions</button>
    </div>
    <div class="settings-form" style="max-width:360px;margin:12px 0">
      <label>Scope
        <select id="memScope">
          <option value="">All authorized scopes</option>
          <option value="personal">Personal</option>
          <option value="company">Company (TTT-wide)</option>
          ${profileList.map(p=>`<option value="project:${esc(p.id)}">Project: ${esc(p.name)}</option>`).join('')}
        </select>
      </label>
    </div>
    <div id="memoryBody"><div class="empty-state">Loading&hellip;</div></div>
  </div>`;
  $('memScope').value=memoryScopeFilter;
  document.querySelectorAll('[data-mtab]').forEach(b=>b.onclick=()=>go('#/memory/'+b.dataset.mtab));
  $('memScope').onchange=()=>{memoryScopeFilter=$('memScope').value;renderMemoryBody()};
  await renderMemoryBody();
}
async function renderMemoryBody(){
  const body=$('memoryBody');
  if(!body)return;
  if(memoryTab==='memories')return renderMemoryRecordsTab(body);
  if(memoryTab==='knowledge')return renderKnowledgeTab(body);
  return renderMemorySuggestionsTab(body);
}
async function renderMemoryRecordsTab(body){
  body.innerHTML=`<div class="settings-form" style="max-width:520px;margin-bottom:14px">
      <label>Save a new memory to the current scope
        <textarea id="memNewContent" rows="2" placeholder="e.g. Aryan prefers concise commit messages"></textarea>
      </label>
      <div class="selector-row">
        <select id="memNewKind" class="tiny-select">
          <option value="preference">preference</option>
          <option value="fact" selected>fact</option>
          <option value="instruction">instruction</option>
          <option value="observation">observation</option>
          <option value="summary">summary</option>
          <option value="inference">inference</option>
        </select>
        <select id="memNewConfidence" class="tiny-select">
          <option value="verified">verified</option>
          <option value="user_provided" selected>user_provided</option>
          <option value="inferred">inferred</option>
        </select>
        <button type="button" class="action" id="memNewSave">Save to Memory</button>
      </div>
    </div>
    <div class="searchbar"><input id="memQuery" placeholder="Search memory (offline keyword search)&hellip;"></div>
    <div class="result-list" id="memList"><div class="empty-state">Loading&hellip;</div></div>`;
  const list=$('memList');
  $('memNewSave').onclick=async()=>{
    const content=$('memNewContent').value.trim();
    if(!content){showToast('Write something to save first',{error:true});return}
    const [scopeType,scopeId]=(memoryScopeFilter||'personal').split(':');
    try{
      await api('/api/memory',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
        scope_type:scopeType||'personal', scope_id:scopeId||null, kind:$('memNewKind').value,
        content, source_type:'user_stated', confidence:$('memNewConfidence').value,
      })});
      $('memNewContent').value='';showToast('Saved to memory');load();
    }catch(err){
      if(err.data&&err.data.candidates){
        const ok=await confirmModal({title:'Similar memory already exists',body:`${err.message}\n\nSave anyway as a separate memory?`,confirmLabel:'Save anyway'});
        if(ok){
          try{
            await api('/api/memory',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
              scope_type:scopeType||'personal', scope_id:scopeId||null, kind:$('memNewKind').value,
              content, source_type:'user_stated', confidence:$('memNewConfidence').value, allow_conflict:true,
            })});
            $('memNewContent').value='';showToast('Saved to memory');load();
          }catch(err2){showToast(err2.message,{error:true})}
        }
      }else showToast(err.message,{error:true});
    }
  };
  async function load(){
    const q=$('memQuery').value.trim();
    const params=[memoryScopeParams(),q?`q=${encodeURIComponent(q)}`:''].filter(Boolean).join('&');
    let data;
    try{data=await api('/api/memory'+(params?`?${params}`:''))}catch(err){list.innerHTML=`<div class="empty-state error">${esc(err.message)}</div>`;return}
    const rows=data.records||[];
    list.innerHTML=rows.length?rows.map(r=>{
      if(r.id===editingMemoryId){
        return `<div class="result-row" style="cursor:default;align-items:flex-start;flex-direction:column;gap:8px">
          <div class="kind"><span class="status-pill">${esc(r.kind)}</span> <span class="status-pill">${esc(r.confidence)}</span> &middot; ${memoryScopeLabel(r)}</div>
          <textarea id="memEditArea-${esc(r.id)}" rows="3" style="width:100%">${esc(r.content)}</textarea>
          <div class="edit-actions"><button type="button" class="pill-btn" data-mem-cancel-edit="1">Cancel</button><button type="button" class="action" data-mem-save-edit="${esc(r.id)}">Save (in place)</button></div>
        </div>`;
      }
      const hist=openHistoryId===r.id?memoryHistoryCache[r.id]:null;
      const historyPanel=openHistoryId!==r.id?'':(hist===undefined?'<div class="empty-state" style="padding:8px 0">Loading history&hellip;</div>':`
        <div class="mem-history-panel" style="width:100%;border-top:1px solid var(--line);margin-top:8px;padding-top:8px">
          ${hist.map(h=>`<div class="mem-history-row" style="padding:4px 0"><span class="status-pill${h.state==='active'?' done':''}">${esc(h.state)}</span> <span style="white-space:normal">${esc(h.content)}</span> <span class="meta">&middot; ${esc(timeAgo(h.updated_at))}</span></div>`).join('')}
        </div>`);
      return `
      <div class="result-row" style="cursor:default;align-items:flex-start;flex-wrap:wrap">
        <div style="flex:1;min-width:0">
          <div class="kind"><span class="status-pill">${esc(r.kind)}</span> <span class="status-pill">${esc(r.confidence)}</span> ${r.pinned?'<span class="status-pill done">pinned</span>':''} &middot; ${memoryScopeLabel(r)}</div>
          <div class="title" style="white-space:normal">${esc(r.content)}</div>
          <div class="meta">${esc(r.source_type)} &middot; ${esc(timeAgo(r.updated_at))}</div>
        </div>
        <div style="display:flex;gap:6px;flex:none;flex-wrap:wrap">
          <button type="button" class="pill-btn" data-mem-pin="${esc(r.id)}" data-pinned="${r.pinned?1:0}">${r.pinned?'Unpin':'Pin'}</button>
          <button type="button" class="pill-btn" data-mem-edit="${esc(r.id)}">Edit</button>
          <button type="button" class="pill-btn" data-mem-history="${esc(r.id)}">${openHistoryId===r.id?'Hide history':'History'}</button>
          <button type="button" class="pill-btn" data-mem-forget="${esc(r.id)}">Forget</button>
        </div>
        ${historyPanel}
      </div>`;
    }).join(''):'<div class="empty-state">No memory saved in this scope yet.</div>';
    document.querySelectorAll('[data-mem-pin]').forEach(b=>b.onclick=async()=>{
      try{await api(`/api/memory/${b.dataset.memPin}/pin`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({pinned:b.dataset.pinned!=='1'})});load()}
      catch(err){showToast(err.message,{error:true})}
    });
    document.querySelectorAll('[data-mem-forget]').forEach(b=>b.onclick=async()=>{
      const ok=await confirmModal({title:'Forget this memory?',body:'It will no longer be used in search or Chat context. This can be undone by an operator via History, or made permanent later with Purge.',confirmLabel:'Forget',danger:true});
      if(!ok)return;
      try{await api(`/api/memory/${b.dataset.memForget}/forget`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({reason:'forgotten from Memory UI'})});load()}
      catch(err){showToast(err.message,{error:true})}
    });
    document.querySelectorAll('[data-mem-edit]').forEach(b=>b.onclick=()=>{editingMemoryId=b.dataset.memEdit;load()});
    document.querySelectorAll('[data-mem-cancel-edit]').forEach(b=>b.onclick=()=>{editingMemoryId=null;load()});
    document.querySelectorAll('[data-mem-save-edit]').forEach(b=>b.onclick=async()=>{
      const id=b.dataset.memSaveEdit;
      const next=$(`memEditArea-${id}`).value;
      const row=rows.find(x=>x.id===id);
      if(next.trim()===row.content){editingMemoryId=null;load();return}
      try{await api(`/api/memory/${id}/edit`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({content:next})});editingMemoryId=null;showToast('Memory updated');load()}
      catch(err){showToast(err.message,{error:true})}
    });
    document.querySelectorAll('[data-mem-history]').forEach(b=>b.onclick=async()=>{
      const id=b.dataset.memHistory;
      if(openHistoryId===id){openHistoryId=null;load();return}
      openHistoryId=id;
      load();
      try{
        const {history}=await api(`/api/memory/${id}/history`);
        memoryHistoryCache[id]=history;
        if(openHistoryId===id)load();
      }catch(err){openHistoryId=null;showToast(err.message,{error:true});load()}
    });
  }
  $('memQuery').addEventListener('input',()=>{clearTimeout(window._memDebounce);window._memDebounce=setTimeout(load,220)});
  await load();
}
async function renderKnowledgeTab(body){
  body.innerHTML=`<div class="settings-form" style="max-width:520px">
      <label>Paste text to ingest as knowledge<textarea id="kbContent" rows="4" placeholder="Paste plain text, markdown, CSV, or JSON here"></textarea></label>
      <label>Title<input id="kbTitle" placeholder="notes.txt"></label>
      <div class="handoff-actions"><button type="button" class="action" id="kbIngest">Ingest into current scope</button></div>
    </div>
    <div class="result-list" id="kbList" style="margin-top:14px"><div class="empty-state">Loading&hellip;</div></div>`;
  const list=$('kbList');
  async function load(){
    const params=memoryScopeParams();
    let data;
    try{data=await api('/api/knowledge/documents'+(params?`?${params}`:''))}catch(err){list.innerHTML=`<div class="empty-state error">${esc(err.message)}</div>`;return}
    const rows=data.documents||[];
    list.innerHTML=rows.length?rows.map(d=>`
      <div class="result-row" style="cursor:default">
        <div style="flex:1;min-width:0">
          <div class="kind"><span class="status-pill ${d.status==='ready'?'done':d.status==='deleted'?'failed':'pending'}">${esc(d.status)}</span> &middot; ${d.chunk_count} chunk(s)</div>
          <div class="title">${esc(d.title)}</div>
          <div class="meta">${esc(d.source_type)} &middot; ${esc(timeAgo(d.updated_at))}</div>
        </div>
        ${d.status!=='deleted'?`<button type="button" class="pill-btn" data-kb-forget="${esc(d.id)}">Forget</button>`:''}
      </div>`).join(''):'<div class="empty-state">No knowledge documents in this scope yet.</div>';
    document.querySelectorAll('[data-kb-forget]').forEach(b=>b.onclick=async()=>{
      const ok=await confirmModal({title:'Forget this document?',body:'Its chunks are permanently removed from search and Chat context.',confirmLabel:'Forget',danger:true});
      if(!ok)return;
      try{await api(`/api/knowledge/documents/${b.dataset.kbForget}/forget`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({reason:'forgotten from Memory UI'})});load()}
      catch(err){showToast(err.message,{error:true})}
    });
  }
  $('kbIngest').onclick=async()=>{
    const content=$('kbContent').value;
    if(!content.trim()){showToast('Paste some text first',{error:true});return}
    const [scopeType,scopeId]=(memoryScopeFilter||'personal').split(':');
    try{
      await api('/api/knowledge/documents',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
        filename:$('kbTitle').value||'notes.txt', mime_type:'text/plain', data_base64:btoa(unescape(encodeURIComponent(content))),
        scope_type:scopeType||'personal', scope_id:scopeId||null, source_type:'upload',
      })});
      $('kbContent').value='';$('kbTitle').value='';showToast('Ingested');load();
    }catch(err){showToast(err.message,{error:true})}
  };
  await load();
}
async function renderMemorySuggestionsTab(body){
  body.innerHTML=`<div class="result-list" id="sugList"><div class="empty-state">Loading&hellip;</div></div>`;
  const list=$('sugList');
  async function load(){
    const params=memoryScopeParams();
    let data;
    try{data=await api('/api/memory/suggestions'+(params?`?${params}`:''))}catch(err){list.innerHTML=`<div class="empty-state error">${esc(err.message)}</div>`;return}
    const rows=data.suggestions||[];
    list.innerHTML=rows.length?rows.map(s=>`
      <div class="result-row" style="cursor:default">
        <div style="flex:1;min-width:0">
          <div class="kind"><span class="status-pill">${esc(s.suggested_kind)}</span> &middot; matched "${esc(s.signal)}"</div>
          <div class="title" style="white-space:normal">${esc(s.suggested_content)}</div>
          <div class="meta">${esc(timeAgo(s.created_at))}</div>
        </div>
        <div style="display:flex;gap:6px;flex:none">
          <button type="button" class="pill-btn" data-sug-accept="${esc(s.id)}">Save to memory</button>
          <button type="button" class="pill-btn" data-sug-dismiss="${esc(s.id)}">Dismiss</button>
        </div>
      </div>`).join(''):'<div class="empty-state">No pending suggestions. Falguna only suggests -- it never saves a memory from a conversation automatically.</div>';
    document.querySelectorAll('[data-sug-accept]').forEach(b=>b.onclick=async()=>{
      try{await api(`/api/memory/suggestions/${b.dataset.sugAccept}/accept`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});showToast('Saved to memory');load()}
      catch(err){showToast(err.message,{error:true})}
    });
    document.querySelectorAll('[data-sug-dismiss]').forEach(b=>b.onclick=async()=>{
      try{await api(`/api/memory/suggestions/${b.dataset.sugDismiss}/dismiss`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});load()}
      catch(err){showToast(err.message,{error:true})}
    });
  }
  await load();
}

/* ----------------------------------------------------------- Settings view */

const SETTINGS_TABS=[['appearance','Appearance'],['models','Models'],['work','Work Mode'],['browser','Browser'],['memory','Memory'],['files','Files'],['notifications','Notifications'],['privacy','Privacy'],['usage','Usage & Cost'],['advanced','Advanced']];
// Falguna V2.1: Privacy Mode is a routing boundary the Model Router enforces
// structurally (see falguna/model_router.py) -- Local Only never even
// contacts an external provider, it is not merely hidden from the result.
const PRIVACY_MODE_INFO=[
  ['LOCAL_ONLY','Local Only','Only a local runtime on this machine may ever be used. An external provider is never contacted, even if one is configured.'],
  ['HYBRID','Hybrid (default)','Prefers a local model when one is available; falls back to an explicitly-enabled external provider only when no local model can answer.'],
  ['EXTERNAL_ALLOWED','External Allowed','Same preference order as Hybrid, offered as a separate mode so "external is allowed" is always a deliberate, visible choice rather than an implicit default.'],
];
function healthLabel(state){return {HEALTHY:'Healthy',DEGRADED:'Degraded',RATE_LIMITED:'Rate limited',AUTH_REQUIRED:'Needs auth',OFFLINE:'Offline',UNSUPPORTED:'Not set up'}[state]||state}
// One entry per falguna.providers.ErrorCategory -- a short label plus
// whether a "Change model" link to Settings -> Models is a sensible next
// step for that category (never shown for a plain rate-limit/timeout,
// where Retry alone is the honest recovery action).
const ERROR_CATEGORY_HINTS={
  RATE_LIMITED:{label:'Rate limited',showSettingsLink:false},
  AUTH_REQUIRED:{label:'Needs authentication',showSettingsLink:true},
  PROVIDER_OFFLINE:{label:'Provider offline',showSettingsLink:true},
  MODEL_UNSUPPORTED:{label:'Model unavailable',showSettingsLink:true},
  TIMEOUT:{label:'Timed out',showSettingsLink:false},
  TRANSPORT_FAILURE:{label:'Connection problem',showSettingsLink:false},
  NO_COMPATIBLE_MODEL:{label:'No model available',showSettingsLink:true},
  UNEXPECTED_FAILURE:{label:'Unexpected error',showSettingsLink:false},
};
async function renderSettingsView(tab){
  tab=(tab&&SETTINGS_TABS.some(([id])=>id===tab))?tab:'appearance';
  const vp=$('viewport');
  const [s,cfg]=await Promise.all([api('/api/settings'),api('/api/config')]);
  vp.innerHTML=`<div class="page">
    <h1>Settings</h1>
    <p class="lede">Informational only in this release &mdash; nothing here can change Falguna's safety policy from the browser.</p>
    <div class="settings-nav" id="settingsNav">${SETTINGS_TABS.map(([id,label])=>`<button type="button" data-tab="${id}" class="${id===tab?'active':''}">${esc(label)}</button>`).join('')}</div>
    <div id="settingsBody"><div class="empty-state">Loading&hellip;</div></div>
  </div>`;
  document.querySelectorAll('#settingsNav button').forEach(b=>b.onclick=()=>go('#/settings/'+b.dataset.tab));
  const body=$('settingsBody');
  if(tab==='appearance'){
    const mode=currentThemeMode();
    body.innerHTML=`<div class="settings-section">
      <div class="section-label" style="margin-top:0">Theme</div>
      <div class="theme-options">
        <button type="button" class="theme-card ${mode==='system'?'active':''}" data-theme="system">${icon('monitor',15)}System</button>
        <button type="button" class="theme-card ${mode==='light'?'active':''}" data-theme="light">${icon('sun',15)}Light</button>
        <button type="button" class="theme-card ${mode==='dark'?'active':''}" data-theme="dark">${icon('moon',15)}Dark</button>
      </div>
    </div>
    <div class="settings-note">Approval boundaries, verification, review, isolation, and audit are enforced in the control plane and are not editable from the UI.</div>`;
    document.querySelectorAll('.theme-card').forEach(b=>b.onclick=()=>applyTheme(b.dataset.theme));
  }else if(tab==='models'){
    const models=await api('/api/models');
    const reg=models.registry;
    const ext=reg.providers.openai_compatible||{};
    const providerCard=p=>`<div class="provider-card">
        <div class="provider-card-head">
          <span class="provider-name">${esc(p.display_name)}</span>
          <span class="health-pill health-${p.health.state.toLowerCase()}">${esc(healthLabel(p.health.state))}</span>
        </div>
        <div class="provider-meta">${p.is_local?'Local &middot; inference happens on this machine, nothing leaves it':'External &middot; requests leave this machine'}</div>
        <div class="provider-detail">${esc(p.health.detail)}</div>
        ${p.models.length?`<div class="provider-models">${p.models.slice(0,8).map(m=>esc(m.display_name||m.model_id)).join(', ')}${p.models.length>8?` +${p.models.length-8} more`:''}</div>`:'<div class="provider-models muted">No model available yet</div>'}
      </div>`;
    body.innerHTML=`<div class="settings-section">
      <div class="section-label" style="margin-top:0">Privacy Mode</div>
      <p class="lede" style="margin:0 0 4px">Controls whether Falguna may ever reach an external model provider. This is enforced by the router itself, not a filter applied afterward.</p>
      <div class="privacy-mode-options">${PRIVACY_MODE_INFO.map(([id,label,desc])=>`<button type="button" class="privacy-mode-card ${reg.privacy_mode===id?'active':''}" data-mode="${id}"><b>${esc(label)}</b><span>${esc(desc)}</span></button>`).join('')}</div>
      <div class="section-label">Providers</div>
      <div class="provider-cards">${models.providers.map(providerCard).join('')||'<div class="empty-state">No provider is enabled.</div>'}</div>
      <p class="lede" style="margin-top:-10px">Local models run through <b>Ollama</b> if it's running on this machine (127.0.0.1:11434) -- Falguna only ever lists and uses a model already pulled there; it never downloads one for you. <b>Codex</b> stays available as an optional, subscription-billed adapter -- Falguna does not require it to run.</p>
      <details class="provider-setup">
        <summary>Provider Setup: custom OpenAI-compatible endpoint</summary>
        <div class="settings-form">
          <label>Display name<input id="mpDisplayName" value="${esc(ext.display_name||'Custom OpenAI-compatible')}" placeholder="My provider"></label>
          <label>Base URL<input id="mpBaseUrl" value="${esc(ext.base_url||'')}" placeholder="https://your-server/v1 or http://127.0.0.1:8080/v1"></label>
          <label>Model id(s), comma-separated<input id="mpModels" value="${esc((ext.model_allowlist||[]).join(', '))}" placeholder="my-model-name"></label>
          <label>API key environment variable (optional)<input id="mpKeyEnv" value="${esc(ext.api_key_env||'')}" placeholder="MY_PROVIDER_API_KEY"></label>
          <label class="checkbox-row"><input type="checkbox" id="mpIsLocal" ${ext.is_local?'checked':''}> This endpoint is self-hosted on my own machine/network (counts as local for Privacy Mode)</label>
          <label class="checkbox-row"><input type="checkbox" id="mpEnabled" ${ext.enabled?'checked':''}> Enable this provider</label>
          <div class="handoff-actions"><button type="button" class="action" id="mpSave">Save provider settings</button></div>
          <p class="lede" style="margin:0">Falguna never enables an external provider, auto-selects a model, or downloads anything on your behalf -- every field above is off until you turn it on. An API key is never typed here: Falguna only reads it from the environment variable you name.</p>
        </div>
      </details>
    </div>`;
    document.querySelectorAll('.privacy-mode-card').forEach(b=>b.onclick=async()=>{
      try{await api('/api/models/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({privacy_mode:b.dataset.mode})});showToast('Privacy Mode updated');renderSettingsView('models')}
      catch(err){showToast(err.message,{error:true})}
    });
    const mpSave=$('mpSave');
    if(mpSave)mpSave.onclick=async()=>{
      const model_allowlist=$('mpModels').value.split(',').map(s=>s.trim()).filter(Boolean);
      try{
        await api('/api/models/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({providers:{openai_compatible:{
          display_name:$('mpDisplayName').value.trim()||'Custom OpenAI-compatible', base_url:$('mpBaseUrl').value.trim(),
          model_allowlist, api_key_env:$('mpKeyEnv').value.trim(), is_local:$('mpIsLocal').checked, enabled:$('mpEnabled').checked,
        }}})});
        showToast('Provider settings saved');renderSettingsView('models');
      }catch(err){showToast(err.message,{error:true})}
    };
  }else if(tab==='work'){
    body.innerHTML=`<div class="settings-section">
      <div class="section-label" style="margin-top:0">Work Mode</div>
      <p class="lede" style="margin-bottom:14px">Fast / Balanced / Deep change real, verifiable knobs -- request timeout, whether Falguna's resilient fallback-model routing is used, and how many attempts a call gets. This is never a claim about hidden reasoning depth.</p>
      <div class="settings-list">
        <div><b>Fast</b> &middot; 30s timeout &middot; no fallback routing &middot; 1 attempt</div>
        <div><b>Balanced</b> (default) &middot; 60s timeout &middot; fallback routing on &middot; 2 attempts</div>
        <div><b>Deep</b> &middot; 120s timeout &middot; fallback routing on &middot; 3 attempts</div>
      </div>
    </div>`;
  }else if(tab==='files'){
    body.innerHTML=`<div class="settings-section">
      <div class="section-label" style="margin-top:0">Attachments</div>
      <div class="settings-list"><div>Per-file limit: <b>10 MB</b></div><div>Stored on disk under this project's local <code>.falguna/attachments</code> folder, indexed in the <a href="#/files">Files</a> view.</div></div>
    </div>`;
  }else if(tab==='notifications'){
    const notifs=await api('/api/notifications');
    body.innerHTML=`<div class="settings-section">
      <div class="section-label" style="margin-top:0">Notifications</div>
      <div class="settings-list"><div>${esc(notifs.unread)} unread of ${esc(notifs.notifications.length)} shown.</div></div>
      <div class="handoff-actions"><button type="button" class="action secondary" id="markAllReadSettings">Mark all as read</button></div>
    </div>`;
    $('markAllReadSettings').onclick=async()=>{
      try{await api('/api/notifications/read-all',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});renderSettingsView('notifications');pollLive()}
      catch(err){showToast(err.message,{error:true})}
    };
  }else if(tab==='browser'){
    const [bset,cstat]=await Promise.all([api('/api/browser/settings'),api('/api/computer/status').catch(()=>null)]);
    const computerStatusLine=(()=>{
      if(!cstat)return 'Status: could not be checked just now.';
      if(!cstat.installed)return `Not usable yet: ${esc(cstat.detail)}`;
      if(!cstat.usable)return `Installed but not usable: ${esc(cstat.detail)}`;
      return 'Installed and usable on this machine right now.';
    })();
    body.innerHTML=`<div class="settings-section">
      <div class="section-label" style="margin-top:0">Browser + Computer Use</div>
      <p class="lede" style="margin:0 0 4px">Falguna's own local browser execution (Playwright) -- never routed through Codex or any external provider. Screenshots and page content never leave this machine in Local Only Privacy Mode.</p>
      <div class="settings-form">
        <label class="checkbox-row"><input type="checkbox" id="bsEnabled" ${bset.enabled?'checked':''}> Browser execution enabled</label>
        <label class="checkbox-row"><input type="checkbox" id="bsHeadless" ${bset.default_headless?'checked':''}> Run headless by default (no visible window) &mdash; a task that needs you to see or complete something in the browser is unaffected by this setting</label>
        <label class="checkbox-row"><input type="checkbox" id="bsComputerUse" ${bset.computer_use_enabled?'checked':''}> Enable computer-use (screen/mouse/keyboard) foundation &mdash; off by default; every click/type still passes through the same sensitive-action approval gate as browser actions</label>
        <div class="settings-list" style="margin:0 0 8px"><div>Computer-use status: ${computerStatusLine}</div></div>
        <label>Maximum concurrent browser sessions<input type="number" id="bsMaxConcurrent" min="1" max="10" value="${esc(bset.max_concurrent_sessions)}"></label>
        <div class="handoff-actions"><button type="button" class="action" id="bsSave">Save browser settings</button></div>
      </div>
      <div class="section-label">Fixed in this release</div>
      <div class="settings-list">
        <div>Downloads: saved into this session's own evidence storage and shown in Files, never to an arbitrary local path</div>
        <div>Approval policy: always visible on a Needs Aryan card -- site, exact action, and reason, never hidden</div>
        <div>Session persistence: a paused session's browser profile (cookies/login state) is kept until you resume, reject, or cancel it</div>
      </div>
    </div>`;
    $('bsSave').onclick=async()=>{
      try{
        await api('/api/browser/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
          enabled:$('bsEnabled').checked, default_headless:$('bsHeadless').checked,
          computer_use_enabled:$('bsComputerUse').checked,
          max_concurrent_sessions:Math.max(1,Math.min(10,parseInt($('bsMaxConcurrent').value,10)||1)),
        })});
        showToast('Browser settings saved');renderSettingsView('browser');
      }catch(err){showToast(err.message,{error:true})}
    };
  }else if(tab==='memory'){
    const mset=await api('/api/memory/settings');
    body.innerHTML=`<div class="settings-section">
      <div class="section-label" style="margin-top:0">Local-first memory &amp; knowledge</div>
      <p class="lede" style="margin:0 0 4px">Keyword search over your saved memory and ingested documents always works fully offline, with zero embedding model. Semantic (meaning-based) retrieval is an optional add-on Falguna never installs on its own.</p>
      <div class="settings-list" style="margin:0 0 8px">
        <div>Offline keyword search: always available (SQLite FTS5, no network, no model)</div>
        <div>Semantic retrieval: <b>${mset.semantic_retrieval_active?'active':'not active'}</b>${mset.configured&&!mset.available?' &mdash; configured, but the runtime is not reachable right now':''}</div>
      </div>
      <div class="settings-form">
        <label>Embedding provider
          <select id="memEmbProvider">
            <option value="none" ${mset.provider==='none'?'selected':''}>None (keyword search only)</option>
            <option value="ollama" ${mset.provider==='ollama'?'selected':''}>Ollama (local, if installed)</option>
          </select>
        </label>
        <label>Ollama base URL<input id="memOllamaUrl" value="${esc(mset.settings.ollama_base_url)}"></label>
        <label>Ollama embedding model<input id="memOllamaModel" value="${esc(mset.settings.ollama_embedding_model)}"></label>
        <div class="handoff-actions"><button type="button" class="action" id="memSettingsSave">Save memory settings</button></div>
      </div>
      <div class="section-label">Setting up local embeddings on this Mac</div>
      <div class="settings-list">
        <div>Falguna never downloads or installs a model runtime automatically -- see MEMORY_LOCAL_EMBEDDINGS.md in the repository root for the exact, manual install steps for a lightweight local embedding model on this machine.</div>
        <div>Until that runtime is installed and reachable, retrieval is keyword-only -- this is expected, not an error.</div>
      </div>
    </div>`;
    $('memSettingsSave').onclick=async()=>{
      try{
        await api('/api/memory/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
          embedding_provider:$('memEmbProvider').value, ollama_base_url:$('memOllamaUrl').value, ollama_embedding_model:$('memOllamaModel').value,
        })});
        showToast('Memory settings saved');renderSettingsView('memory');
      }catch(err){showToast(err.message,{error:true})}
    };
  }else if(tab==='privacy'){
    body.innerHTML=`<div class="settings-section">
      <div class="section-label" style="margin-top:0">Safety boundaries preserved in this release</div>
      <div class="settings-list">${s.boundaries.map(b=>`<div>${esc(b)}</div>`).join('')}</div>
    </div>`;
  }else if(tab==='usage'){
    const usage=await api('/api/usage');
    body.innerHTML=`<div class="settings-section">
      <div class="section-label" style="margin-top:0">Usage &amp; cost</div>
      <div class="usage-cards">
        <div class="usage-card"><span>Chat</span><b>${esc(usage.totals.chat.calls)}</b><small>${esc(usage.totals.chat.input_tokens)}+${esc(usage.totals.chat.output_tokens)} tok &middot; $${usage.totals.chat.cost_usd.toFixed(4)}</small></div>
        <div class="usage-card"><span>Search</span><b>${esc(usage.totals.research.calls)}</b><small>${esc(usage.totals.research.input_tokens)}+${esc(usage.totals.research.output_tokens)} tok &middot; $${usage.totals.research.cost_usd.toFixed(4)}</small></div>
        <div class="usage-card"><span>Work</span><b>${esc(usage.totals.work.calls)}</b><small>$${usage.totals.work.cost_usd.toFixed(4)}</small></div>
      </div>
      <p class="lede">${esc(usage.note)}</p>
    </div>`;
  }else{
    body.innerHTML=`<div class="settings-section">
      <div class="section-label" style="margin-top:0">Product</div>
      <div class="settings-list"><div>${esc(s.product)} &middot; default model ${esc(s.model)}</div></div>
      <div class="section-label">Approved projects</div>
      <div class="settings-list">${s.profiles.map(p=>`<div><b>${esc(p.name)}</b> &middot; ${esc(p.repository)} &middot; cap $${esc(p.default_budget_usd)}</div>`).join('')}</div>
    </div>`;
  }
}

router().catch(e=>{$('viewport').innerHTML=`<div class="page"><div class="empty-state error">${esc(e.message)}</div></div>`});
startLivePolling();
</script></body></html>
'''
