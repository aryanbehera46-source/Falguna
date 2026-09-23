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
from .chat import ChatError, ChatResponder, ConversationStore, search_missions
from .codex_transport import CodexCliJSONTransport, DEFAULT_CODEX_MODEL, ResilientCodexTransport, SUPPORTED_CODEX_MODELS
from .continuity import ProjectUnderstandingCache, browser_e2e_applicable, resolve_continuation
from .discovery import ProjectDiscovery
from .gateway import OpenAICompatibleGateway
from .models import CommandSpec, RunPolicy
from .notifications import NotificationStore
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
            return self._json({
                "product": "Falguna Engineering", "stage": "internal alpha", "profiles": profiles, "model": MODEL,
                "available_models": sorted(SUPPORTED_CODEX_MODELS), "codex_runtime_found": bool(shutil.which("codex")),
                "work_modes": list(WORK_MODE_SETTINGS), "default_work_mode": DEFAULT_WORK_MODE,
            })
        if path == "/api/settings":
            return self._settings()
        if path == "/api/search":
            query = parse_qs(parsed.query).get("q", [""])[0]
            return self._search(query)
        if path == "/api/missions/board":
            return self._missions_board()
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
            board = {"running": [], "needs_you": [], "completed": [], "failed": [], "cancelled": []}
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
                    "run_id": run["id"], "title": mission["title"], "status": run["status"],
                    "project": profiles_by_repo.get(task["repository"], task["repository"]),
                    "worker": run["worker"], "model": run["model"], "origin": origin,
                    "started_at": run["created_at"], "last_activity": run["updated_at"],
                    "cost_usd": round(sum(float(c["amount_usd"]) for c in costs), 8),
                    "evidence_count": len(artifacts), "merge_approval": merge_status, "current_step": current_step,
                }
                if run["status"] in {"PLANNING", "WORKING", "VERIFYING", "REVIEWING"}:
                    board["running"].append(card)
                elif run["status"] == "PAUSED" or (run["status"] == "DONE_CANDIDATE" and merge_status == "PENDING"):
                    board["needs_you"].append(card)
                elif run["status"] in {"FAILED", "QUARANTINED"}:
                    board["failed"].append(card)
                elif run["status"] == "CANCELLED":
                    board["cancelled"].append(card)
                else:
                    board["completed"].append(card)
            return self._json(board)
        finally:
            store.close()

    def _live_summary(self):
        control, store = open_control_plane(self.app_root)
        try:
            running = needs_you = failed = 0
            for run in store.list("runs")[-200:]:
                approvals = store.list("approvals", "run_id=? AND kind=?", (run["id"], "PROTECTED_BRANCH_MERGE"))
                merge_status = approvals[-1]["status"] if approvals else "NOT_REQUESTED"
                if run["status"] in {"PLANNING", "WORKING", "VERIFYING", "REVIEWING"}:
                    running += 1
                elif run["status"] == "PAUSED" or (run["status"] == "DONE_CANDIDATE" and merge_status == "PENDING"):
                    needs_you += 1
                elif run["status"] in {"FAILED", "QUARANTINED"}:
                    failed += 1
            return self._json({
                "running": running, "needs_you": needs_you, "failed": failed,
                "unread_notifications": NotificationStore(store).unread_count(),
            })
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
            uploads = [
                {"type": "upload", "id": a["id"], "filename": a["filename"], "content_type": a["content_type"],
                 "size_bytes": a["size_bytes"], "conversation_id": a["conversation_id"], "created_at": a["created_at"]}
                for a in AttachmentStore(store, self.app_root).list_all()
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
            if path.startswith("/api/runs/") and path.endswith(("/pause", "/cancel")):
                run_id = path.split("/")[3]
                action = path.rsplit("/", 1)[-1].upper()
                control, store = open_control_plane(self.app_root)
                try:
                    control.request_control(run_id, action)
                    return self._json({"run_id": run_id, "action": action, "mode": "safe-boundary"}, HTTPStatus.ACCEPTED)
                finally:
                    store.close()
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
            if path == "/api/research":
                return self._run_research(body)
            if path.startswith("/api/research/") and path.endswith("/continue-chat"):
                return self._research_to_chat(segments[3], body)
            if path.startswith("/api/research/") and path.endswith("/handoff"):
                return self._research_to_work(segments[3], body)
            return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except DiscoveryUncertain as exc:
            return self._json({"error": str(exc), "discovery": exc.evidence}, HTTPStatus.CONFLICT)
        except (ValueError, KeyError, json.JSONDecodeError, ChatError) as exc:
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
            args=(self.app_root, token, conversation_id, pending["id"], history, model, work_mode),
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
                codex = shutil.which("codex")
                if sources and not codex:
                    raise ResearchError("MODEL_UNAVAILABLE: authenticated Codex executable not found")
                if codex:
                    gateway = OpenAICompatibleGateway(model, "http://127.0.0.1:1/v1", "")
                    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
                    transport = _build_transport(codex, codex_home, settings["research_timeout"], settings["fallback"])
                else:
                    gateway = transport = None  # only reachable when sources is empty; reply() never touches these in that case
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
        codex = shutil.which("codex")
        if not codex:
            raise RuntimeError("authenticated Codex executable not found")
        gateway = OpenAICompatibleGateway(model, "http://127.0.0.1:1/v1", "")
        codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        transport = _build_transport(codex, codex_home, settings["mission_timeout"], settings["fallback"])
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
        codex = shutil.which("codex")
        if not codex:
            raise RuntimeError("authenticated Codex executable not found")
        gateway = OpenAICompatibleGateway(run["model"], "http://127.0.0.1:1/v1", "")
        codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        transport = ResilientCodexTransport(CodexCliJSONTransport(Path(codex), codex_home, timeout_seconds=300))
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


def _run_chat_reply(app_root, token, conversation_id, message_id, history, model, work_mode):
    """Background worker for the async Chat pipeline (Sections 3-4) --
    structurally identical to _run_mission: the HTTP handler has already
    returned, and this thread is the only thing that ever writes the
    reply. Every operation-state write below reflects a state this call
    actually reached; there is no fabricated "typing" percentage."""
    control, store = open_control_plane(app_root)
    settings = WORK_MODE_SETTINGS.get(work_mode, WORK_MODE_SETTINGS[DEFAULT_WORK_MODE])
    chat = ConversationStore(store)
    try:
        with _operations_lock:
            _operations[token]["state"] = "THINKING"
        chat.mark_generating(message_id)
        codex = shutil.which("codex")
        if not codex:
            raise ChatError("MODEL_UNAVAILABLE: authenticated Codex executable not found")
        gateway = OpenAICompatibleGateway(model, "http://127.0.0.1:1/v1", "")
        codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        transport = _build_transport(codex, codex_home, settings["chat_timeout"], settings["fallback"])
        outcome = ChatResponder(gateway, transport, model, timeout_seconds=settings["chat_timeout"]).reply(history)
        applied = chat.complete_message(message_id, outcome["reply"], outcome["model_call"], outcome["suggested_objective"])
        with _operations_lock:
            _operations[token] = {**_operations.get(token, {}), "state": "COMPLETE" if applied else "CANCELLED", "message_id": message_id}
    except ChatError as exc:
        applied = chat.fail_message(message_id, str(exc))
        with _operations_lock:
            _operations[token] = {**_operations.get(token, {}), "state": "FAILED" if applied else "CANCELLED", "error": str(exc), "message_id": message_id}
    except Exception as exc:
        chat.fail_message(message_id, f"UNEXPECTED_FAILURE: {exc}")
        with _operations_lock:
            _operations[token] = {**_operations.get(token, {}), "state": "FAILED", "error": str(exc), "message_id": message_id}
    finally:
        store.close()


def serve(root: Path, host="127.0.0.1", port=8765):
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("Falguna v1.1 is local-only")
    server = ThreadingHTTPServer((host, port), FalgunaHandler)
    server.app_root = Path(root).resolve()
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
  --warn:#f0c752;--bad:#ff8f78;--bad-dim:#4a2c25;--good:#7fd99a;
  --user-bg:#2c2519;--user-ink:#e9dcc6;--shadow:#000c;--shadow-lite:#000a;--scrim:#0009;
}
@media (prefers-color-scheme:light){
  :root:not([data-theme="dark"]){
    color-scheme:light;
    --bg:#faf6ee;--bg-glow:#fff9ec;--side:#f4eedb;--panel:#ffffff;--soft:#f1e7d3;--soft2:#e9dabf;
    --line:#ddceac;--text:#241c10;--muted:#6e5f45;--muted-dim:#8c7c60;
    --accent:#d98a2c;--accent-hi:#a8620f;--accent-ink:#2a1707;--accent-dim:#e3c896;--accent-soft:#f3e3c3;
    --warn:#8a5a00;--bad:#b23a24;--bad-dim:#f8ddd5;--good:#1e7a43;
    --user-bg:#efe0c2;--user-ink:#241c10;--shadow:#0002;--shadow-lite:#0001;--scrim:#0004;
  }
}
:root[data-theme="light"]{
  color-scheme:light;
  --bg:#faf6ee;--bg-glow:#fff9ec;--side:#f4eedb;--panel:#ffffff;--soft:#f1e7d3;--soft2:#e9dabf;
  --line:#ddceac;--text:#241c10;--muted:#6e5f45;--muted-dim:#8c7c60;
  --accent:#d98a2c;--accent-hi:#a8620f;--accent-ink:#2a1707;--accent-dim:#e3c896;--accent-soft:#f3e3c3;
  --warn:#8a5a00;--bad:#b23a24;--bad-dim:#f8ddd5;--good:#1e7a43;
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
.nav-item{display:flex;align-items:center;gap:10px;width:100%;border:0;background:transparent;padding:7px 9px;border-radius:8px;color:#cdbfa9;text-align:left;cursor:pointer;font-size:13px;transition:background .12s,color .12s}
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
.topbar-title strong{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:650;font-size:14px;color:#e7dcc7}
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
.settings-list div{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:11px 14px;font-size:12.5px;color:#d8cbb4}
.settings-note{background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--accent);border-radius:8px;padding:12px 15px;color:var(--muted);font-size:12.5px;margin-bottom:22px}
.section-label{font-size:12.5px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted-dim);font-weight:700;margin:26px 0 4px}
.status-pill{display:inline-block;border-radius:999px;padding:2px 9px;font-size:10.5px;text-transform:capitalize;border:1px solid var(--line)}
.status-pill.done{color:var(--accent-hi);border-color:var(--accent-dim)}
.status-pill.failed,.status-pill.quarantined{color:var(--bad);border-color:#5c332c}
.status-pill.pending,.status-pill.working,.status-pill.reviewing,.status-pill.verifying,.status-pill.planning{color:var(--warn);border-color:#5c4c2a}

/* ---------- Chat view ---------- */
.chat-view{display:grid;grid-template-rows:minmax(0,1fr) auto;min-height:0;height:100%}
.chat-scroll{min-height:0;overflow:auto;padding:0 max(22px,calc((100vw - 264px - 760px)/2))}
.chat-welcome{max-width:600px;margin:9vh auto 0;text-align:center}
.chat-welcome .glow{width:54px;height:54px;margin:0 auto 20px;border-radius:16px;background:linear-gradient(155deg,var(--accent-hi),var(--accent));display:grid;place-items:center;box-shadow:0 18px 44px -14px #e8a33d55}
.chat-welcome .glow svg{color:var(--accent-ink);width:26px;height:26px}
.chat-welcome h1{font-size:26px;margin:0 0 8px;font-weight:650;letter-spacing:-.015em;color:#f7f0e3}
.chat-welcome p{color:var(--muted);font-size:14px;margin:0 0 26px}
.chip-row{display:flex;gap:8px;flex-wrap:wrap;justify-content:center}
.chip{background:var(--panel);border:1px solid var(--line);border-radius:999px;padding:8px 15px;cursor:pointer;color:#d8cbb4;font-size:12.5px;display:inline-flex;align-items:center;gap:7px;transition:border-color .12s,background .12s}
.chip:hover{border-color:#4a3d28;background:var(--soft)}
.chip svg{width:13px;height:13px;color:var(--muted-dim)}
.thread{max-width:760px;margin:0 auto;padding:26px 0 8px;display:grid;gap:18px}
.msg{display:flex;gap:11px}
.msg .avatar{width:25px;height:25px;border-radius:8px;display:grid;place-items:center;font-size:11px;font-weight:750;flex:none;margin-top:3px}
.msg.user .avatar{background:var(--user-bg);color:var(--user-ink)}
.msg.assistant .avatar{background:linear-gradient(155deg,var(--accent-hi),var(--accent));color:var(--accent-ink)}
.msg .bubble{border:1px solid var(--line);background:var(--panel);border-radius:14px;padding:12px 15px;overflow-wrap:anywhere;min-width:0;flex:1;font-size:13.5px}
.msg.user .bubble{background:var(--user-bg);border-color:#463a27}
.msg .bubble.error{border-color:#5c332c;background:var(--bad-dim);color:#ffd4c9}
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
.handoff-panel input,.handoff-panel select,.handoff-panel textarea{width:100%;color:var(--text);background:#120e08;border:1px solid var(--line);border-radius:9px;padding:9px 10px}
.handoff-panel textarea{min-height:60px;resize:vertical}
.handoff-actions{display:flex;gap:9px;margin-top:13px;flex-wrap:wrap}
button.action{border:0;border-radius:9px;padding:9px 14px;font-weight:650;cursor:pointer;background:var(--accent);color:var(--accent-ink);font-size:13px}
button.action.secondary{background:var(--soft2);color:var(--text)}
button.action.danger{background:var(--bad-dim);color:#ffd9cf}
button.action:disabled{opacity:.5;cursor:not-allowed}

/* ---------- Work view ---------- */
.work-view{padding:32px max(22px,calc((100vw - 264px - 820px)/2)) 60px}
.work-empty{max-width:680px;margin:7vh auto 0;text-align:center}
.work-empty h1{font-size:24px;margin:0 0 9px;font-weight:650}
.work-empty p{color:var(--muted);font-size:14px;margin:0 0 24px}
.mission-form{max-width:600px;margin:0 auto;background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:20px;text-align:left}
.mission-form label{display:block;color:var(--muted);font-size:11.5px;margin:12px 0 5px;font-weight:600}
.mission-form input,.mission-form select,.mission-form textarea{width:100%;color:var(--text);background:#120e08;border:1px solid var(--line);border-radius:9px;padding:10px}
.mission-form textarea{min-height:92px;resize:vertical}
.mission-form .risk-note{margin-top:8px;color:var(--muted-dim);font-size:11.5px}
.work-thread{max-width:820px;margin:auto}
.origin-banner{border:1px solid var(--accent-dim);background:var(--accent-soft);border-radius:11px;padding:10px 14px;margin-bottom:16px;font-size:12px;color:#e8dcc6;display:flex;justify-content:space-between;gap:10px;align-items:center;flex-wrap:wrap}
.work-header{border:1px solid var(--line);background:var(--panel);border-radius:16px;padding:19px;margin-bottom:16px}
.work-header .objective{color:var(--muted);font-size:12.5px;margin-bottom:10px}
.status-line{font-size:19px;font-weight:700;margin:2px 0 8px;text-transform:capitalize}
.error{color:var(--bad);overflow-wrap:anywhere;font-size:13px}
.timeline{display:flex;gap:7px;flex-wrap:wrap;margin-top:6px}
.step{border:1px solid var(--line);border-radius:999px;padding:5px 10px;color:#cbbea4;font-size:11.5px}
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
};
const icon=(name,size=16)=>`<svg width="${size}" height="${size}" viewBox="0 0 20 20" fill="none">${ICON[name]||''}</svg>`;

$('newChatBtn').innerHTML=icon('plus',15)+'New chat';
$('menuButton').innerHTML=icon('menu',17);
$('bellBtn').innerHTML=icon('bell',16)+'<span class="bell-dot hidden" id="bellDot"></span>';
$('nav').innerHTML=[
  ['chat','Chat'],['search','Search'],['work','Work'],['mission','Mission Control'],['projects','Projects'],['files','Files'],['history','History'],['settings','Settings'],
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
const VIEW_TITLES={chat:'Chat',search:'Search',work:'Work',mission:'Mission Control',projects:'Projects',files:'Files',history:'History',settings:'Settings'};
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
    return `<div class="msg assistant"><div class="avatar">F</div><div class="bubble error">${esc(m.error||'This reply failed.')}</div>
      ${isLast?`<div class="msg-actions"><button type="button" class="msg-action-btn" data-regen-id="${esc(m.id)}">${icon('retry',12)}Retry</button></div>`:''}</div>`;
  }
  return `<div class="msg assistant"><div class="avatar">F</div><div class="bubble">${renderMarkdown(m.content)}</div>
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
  const mount=$('handoffMount');
  const openOptions=profileList.map(p=>`<option value="${esc(p.id)}" ${p.id===conversation.project_id?'selected':''}>${esc(p.name)}</option>`).join('');
  const priorRuns=(handoffs||[]).map(h=>`<a href="#/work/${esc(h.run_id)}">${esc(new Date(h.created_at).toLocaleString())}</a>`).join(' · ');
  mount.innerHTML=`<div class="handoff-panel">
    <h3>${icon('handoff',15)}Hand off to Work</h3>
    <div class="sub">Chat can't touch a repository itself. Work runs in an isolated worktree with verification, review, and a human approval gate.${priorRuns?` Already started: ${priorRuns}`:''}</div>
    <label>Approved project</label>
    <select id="handoffProject">${openOptions}</select>
    <label>Bounded objective for Work</label>
    <textarea id="handoffObjective" placeholder="Describe one small, testable outcome.">${esc(suggested)}</textarea>
    <div class="handoff-actions"><button class="action" id="handoffBtn" type="button">Start Work mission</button></div>
  </div>`;
  $('handoffBtn').onclick=async()=>{
    const project_id=$('handoffProject').value;
    const objective=$('handoffObjective').value.trim();
    if(!project_id||objective.length<12){showToast('Choose a project and describe a bounded objective (12+ characters).',{error:true});return}
    $('handoffBtn').disabled=true;$('handoffBtn').textContent='Starting…';
    try{
      const {id}=currentRoute();
      const out=await api(`/api/conversations/${id}/handoff`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project_id,objective})});
      watchOperation(out.operation,runId=>go('#/work/'+runId));
    }catch(err){
      $('handoffBtn').disabled=false;$('handoffBtn').textContent='Start Work mission';
      showToast(err.message+(err.data&&err.data.discovery?' -- discovery needs a narrower objective.':''),{error:true});
    }
  };
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

function mcControlsHtml(c){
  if(c.status==='PAUSED')return `<button type="button" class="pill-btn" data-mc-action="resume" data-run-id="${esc(c.run_id)}">Resume</button><button type="button" class="pill-btn" data-mc-action="cancel" data-run-id="${esc(c.run_id)}">Cancel</button>`;
  if(c.status==='DONE_CANDIDATE'&&c.merge_approval==='PENDING')return `<button type="button" class="pill-btn" data-mc-action="approve" data-run-id="${esc(c.run_id)}">Approve</button><button type="button" class="pill-btn" data-mc-action="reject" data-run-id="${esc(c.run_id)}">Reject</button><button type="button" class="pill-btn" data-mc-action="request-changes" data-run-id="${esc(c.run_id)}">Changes</button>`;
  if(['PLANNING','WORKING','VERIFYING','REVIEWING'].includes(c.status))return `<button type="button" class="pill-btn" data-mc-action="pause" data-run-id="${esc(c.run_id)}">Pause</button><button type="button" class="pill-btn" data-mc-action="cancel" data-run-id="${esc(c.run_id)}">Cancel</button>`;
  if(['FAILED','QUARANTINED'].includes(c.status))return `<button type="button" class="pill-btn" data-mc-action="resume" data-run-id="${esc(c.run_id)}">Retry</button>`;
  return '';
}
function mcCard(c){
  const needsYou=(c.status==='DONE_CANDIDATE'&&c.merge_approval==='PENDING')||c.status==='PAUSED';
  return `<div class="mc-card ${needsYou?'needs-you':''}" data-open-run="${esc(c.run_id)}">
    <div class="mc-title">${esc(c.title)}</div>
    <div class="mc-meta"><span class="status-pill ${esc((c.status||'').toLowerCase())}">${esc(String(c.status||'').replaceAll('_',' '))}</span><span>${esc(c.project||'')}</span><span>${esc(c.origin||'')}</span><span>${esc(timeAgo(c.last_activity))}</span>${c.cost_usd?`<span>$${esc(c.cost_usd)}</span>`:''}</div>
    <div class="mc-step">${esc(String(c.current_step||'').replaceAll('_',' '))}</div>
    <div class="mc-controls">${mcControlsHtml(c)}</div>
  </div>`;
}
function wireMcControls(){
  document.querySelectorAll('.mc-card').forEach(card=>{
    card.onclick=e=>{if(e.target.closest('[data-mc-action]'))return;go('#/work/'+card.dataset.openRun)};
  });
  document.querySelectorAll('[data-mc-action]').forEach(b=>b.onclick=async e=>{
    e.stopPropagation();
    const runId=b.dataset.runId,action=b.dataset.mcAction;
    try{
      if(action==='pause'||action==='cancel'){
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
  host.innerHTML=MC_BUCKETS.map(([key,label,cls])=>{
    const items=board[key]||[];
    return `<div><div class="mc-bucket-title ${cls}">${esc(label)}<span class="count">${items.length}</span></div>${items.length?`<div class="mc-grid">${items.map(mcCard).join('')}</div>`:'<div class="mc-empty-bucket">Nothing here.</div>'}</div>`;
  }).join('');
  wireMcControls();
}
async function renderMissionControlView(){
  const vp=$('viewport');
  vp.innerHTML=`<div class="mc-view">
    <div class="mc-head"><div><h1 style="margin:0;font-size:21px;font-weight:650">Mission Control</h1><p class="lede" style="margin:4px 0 0">Every Work mission, grouped by what it actually needs from you next.</p></div></div>
    <div class="mc-buckets" id="mcBuckets"><div class="empty-state">Loading&hellip;</div></div>
  </div>`;
  await refreshMissionControl();
  clearInterval(mcTimer);
  mcTimer=setInterval(()=>{
    if(currentRoute().view==='mission')refreshMissionControl();else clearInterval(mcTimer);
  },5000);
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
  $('filesList').innerHTML=rows.length?rows.map(f=>f.type==='upload'
    ?`<div class="file-row"><div><div class="f-name">${esc(f.filename)}</div><div class="f-meta">${esc(f.content_type||'')} &middot; ${esc(formatBytes(f.size_bytes))} &middot; ${esc(timeAgo(f.created_at))}</div></div><div style="display:flex;gap:8px;flex:none">${f.conversation_id?`<a class="pill-btn" href="#/chat/${esc(f.conversation_id)}">Open chat</a>`:''}<a class="pill-btn" href="/api/attachments/${esc(f.id)}" download title="Download">${icon('download',12)}</a></div></div>`
    :`<div class="file-row"><div><div class="f-name">${esc(f.filename)}</div><div class="f-meta">${esc(f.kind||'')} &middot; ${esc(f.mission_title||'')} &middot; ${esc(timeAgo(f.created_at))}</div></div><a class="pill-btn" href="#/work/${esc(f.run_id)}">Open mission</a></div>`
  ).join(''):`<div class="empty-state">No ${filesTab==='generated'?'generated files':'uploads'} yet.</div>`;
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

/* ----------------------------------------------------------- Settings view */

const SETTINGS_TABS=[['appearance','Appearance'],['models','Models'],['work','Work Mode'],['files','Files'],['notifications','Notifications'],['privacy','Privacy'],['usage','Usage & Cost'],['advanced','Advanced']];
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
    body.innerHTML=`<div class="settings-section">
      <div class="section-label" style="margin-top:0">Available models</div>
      <div class="settings-list">${cfg.available_models.map(m=>`<div>${esc(m)}${m===cfg.model?' &middot; default':''}</div>`).join('')}</div>
      <div class="settings-list"><div>Authenticated Codex runtime detected on this machine: <b>${cfg.codex_runtime_found?'Yes':'No'}</b></div></div>
      <p class="lede">Each chat picks its own model (or Auto, which uses Falguna's resilient fallback-aware routing) from the selector above its composer -- there is no single global default to change here.</p>
    </div>`;
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
