import json
import os
import secrets
import shutil
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .chat import ChatError, ChatResponder, ConversationStore, search_missions
from .codex_transport import CodexCliJSONTransport, DEFAULT_CODEX_MODEL, ResilientCodexTransport
from .continuity import ProjectUnderstandingCache, browser_e2e_applicable, resolve_continuation
from .discovery import ProjectDiscovery
from .gateway import OpenAICompatibleGateway
from .models import CommandSpec, RunPolicy
from .review import ModelSemanticReviewer
from .runtime import open_control_plane
from .store import utcnow
from .usability import evidence_summary, mission_view
from .workers import StructuredEditWorker


MODEL = DEFAULT_CODEX_MODEL
_operations = {}
_operations_lock = threading.Lock()


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
            return self._json({"product": "Falguna Engineering", "stage": "internal alpha", "profiles": profiles, "model": MODEL})
        if path == "/api/settings":
            return self._settings()
        if path == "/api/search":
            query = parse_qs(parsed.query).get("q", [""])[0]
            return self._search(query)
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
            })
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
            if path.startswith("/api/conversations/") and path.endswith("/messages"):
                return self._post_message(path.split("/")[3], body)
            if path.startswith("/api/conversations/") and path.endswith("/rename"):
                return self._rename_conversation(path.split("/")[3], body)
            if path.startswith("/api/conversations/") and path.endswith("/handoff"):
                return self._handoff(path.split("/")[3], body)
            return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except DiscoveryUncertain as exc:
            return self._json({"error": str(exc), "discovery": exc.evidence}, HTTPStatus.CONFLICT)
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            return self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def _start(self, body):
        token = self._launch(body.get("project"), body.get("objective", ""), body.get("max_cost_usd"))
        return self._json({"operation": token, "state": "STARTING"}, HTTPStatus.ACCEPTED)

    def _launch(self, project_id, objective, max_cost_usd, conversation_id=None):
        """Shared by /api/runs and Chat -> Work handoff. Both paths go through the
        identical discovery/policy/worktree pipeline -- handoff cannot skip discovery,
        widen scope, or start a run without it. conversation_id only ever gets attached
        to a run that this same call produced through the real control plane."""
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
            args=(self.app_root, token, profile, objective, editable, commands, cap, discovery, conversation_id),
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
        content = str(body.get("content", "")).strip()
        if not content:
            raise ValueError("Message cannot be empty")
        control, store = open_control_plane(self.app_root)
        try:
            chat = ConversationStore(store)
            conversation = chat.get_conversation(conversation_id)
            if not conversation:
                return self._json({"error": "conversation not found"}, HTTPStatus.NOT_FOUND)
            user_message = chat.add_message(conversation_id, "user", content)
            history = [{"role": m["role"], "content": m["content"]} for m in chat.list_messages(conversation_id) if m["role"] in {"user", "assistant"}]
            try:
                codex = shutil.which("codex")
                if not codex:
                    raise ChatError("MODEL_UNAVAILABLE: authenticated Codex executable not found")
                gateway = OpenAICompatibleGateway(MODEL, "http://127.0.0.1:1/v1", "")
                codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
                transport = ResilientCodexTransport(CodexCliJSONTransport(Path(codex), codex_home, timeout_seconds=60))
                outcome = ChatResponder(gateway, transport, MODEL, timeout_seconds=60).reply(history)
                assistant_message = chat.add_message(conversation_id, "assistant", outcome["reply"], model_call=outcome["model_call"])
                assistant_message["suggested_objective"] = outcome["suggested_objective"]
            except ChatError as exc:
                assistant_message = chat.add_message(conversation_id, "assistant", "", error=str(exc))
                assistant_message["suggested_objective"] = None
            return self._json({"message": user_message, "assistant": assistant_message})
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
        token = self._launch(project_id, body.get("objective", ""), body.get("max_cost_usd"), conversation_id=conversation_id)
        return self._json({"operation": token, "state": "STARTING", "conversation_id": conversation_id}, HTTPStatus.ACCEPTED)

    def _body(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > 65536:
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


def _run_mission(app_root, token, profile, objective, editable, commands, cap, discovery, conversation_id=None):
    control, store = open_control_plane(app_root)
    try:
        dependency_path = Path(profile["repository"]) / profile.get("package_root", ".") / "node_modules"
        browser_applicable = browser_e2e_applicable(objective, editable, profile)
        policy = RunPolicy(allowed_write_globs=editable, verification_commands=commands, max_cost_usd=cap, dependency_node_path=str(dependency_path) if dependency_path.is_dir() else None, verification_write_regexes=profile.get("verification_write_regexes", []), browser_applicable=browser_applicable, browser_base_url=profile.get("browser_base_url") if browser_applicable else None, browser_project_roots=profile.get("browser_project_roots", ["."]), browser_cached_install_allowed=bool(profile.get("browser_cached_install_allowed", False)), browser_external_probe_required=bool(profile.get("browser_external_probe_required", False)), require_implementation_change=discovery.get("objective_kind") == "FEATURE_CHANGE", implementation_files=discovery.get("implementation_files", []))
        codex = shutil.which("codex")
        if not codex:
            raise RuntimeError("authenticated Codex executable not found")
        gateway = OpenAICompatibleGateway(MODEL, "http://127.0.0.1:1/v1", "")
        codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        transport = ResilientCodexTransport(CodexCliJSONTransport(Path(codex), codex_home, timeout_seconds=300))
        worker = StructuredEditWorker(gateway, editable, timeout_seconds=300, transport=transport)
        control.reviewer = ModelSemanticReviewer(gateway, timeout_seconds=300, transport=transport)
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
            with _operations_lock:
                _operations[token] = {"state": "RUNNING", "run_id": run_id, "discovery": discovery}
        run_id = control.start(ids["task_id"], worker, "structured-codex", MODEL, policy, on_run_created=created)
        with _operations_lock:
            _operations[token] = {"state": "COMPLETE", "run_id": run_id, "discovery": discovery}
    except Exception as exc:
        with _operations_lock:
            _operations[token] = {"state": "FAILED", "run_id": _operations[token].get("run_id"), "error": str(exc)}
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
        with _operations_lock:
            _operations[token] = {"state": "COMPLETE", "run_id": run_id, "resumed": True}
    except Exception as exc:
        with _operations_lock:
            _operations[token] = {"state": "FAILED", "run_id": run_id, "error": str(exc), "resumed": True}
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
<title>Falguna</title><style>
:root{
  color-scheme:dark;
  --bg:#161310;--side:#1b1712;--panel:#211c15;--soft:#2a231a;--soft2:#332b1e;
  --line:#3c3325;--text:#f7f0e3;--muted:#ab9c86;--muted-dim:#7c7060;
  --accent:#e8a33d;--accent-hi:#f4bd63;--accent-ink:#2a1707;--accent-dim:#4d3a1e;--accent-soft:#332818;
  --warn:#f0c752;--bad:#ff8f78;--bad-dim:#4a2c25;
  --user-bg:#2c2519;--user-ink:#e9dcc6;
}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:radial-gradient(120% 140% at 18% -10%,#241d12 0%,var(--bg) 46%);color:var(--text);font:14.5px/1.6 "Inter var",Inter,ui-sans-serif,system-ui,-apple-system,sans-serif;-webkit-font-smoothing:antialiased}
button,input,select,textarea{font:inherit;color:inherit}
a{color:var(--accent-hi)}
svg{display:block}
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
.composer{max-width:760px;margin:auto;background:var(--panel);border:1px solid var(--line);border-radius:20px;padding:6px 8px 8px;box-shadow:0 20px 50px -20px #000c}
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

@media(max-width:850px){
  .app{grid-template-columns:1fr}
  .workspace{grid-column:1}
  .menu-button{display:inline-grid;place-items:center}
  aside{display:flex;position:fixed;inset:0 auto 0 0;width:min(86vw,290px);z-index:20;transform:translateX(-102%);transition:transform .18s ease;box-shadow:20px 0 50px #000a}
  aside.open{transform:translateX(0)}
  .scrim{position:fixed;inset:0;background:#0009;z-index:15}
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
      <div class="model-pill" id="modelPill"><span class="dot"></span><span class="label">Falguna</span></div>
    </header>
    <section class="viewport" id="viewport"></section>
  </main>
</div>
<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(url,options){const r=await fetch(url,options);const j=await r.json();if(!r.ok)throw Object.assign(new Error(j.error||'Request failed'),{data:j});return j}
let profiles={};
let profileList=[];
let settingsModel='';

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
};
const icon=(name,size=16)=>`<svg width="${size}" height="${size}" viewBox="0 0 20 20" fill="none">${ICON[name]||''}</svg>`;

$('newChatBtn').innerHTML=icon('plus',15)+'New chat';
$('menuButton').innerHTML=icon('menu',17);
$('nav').innerHTML=[
  ['chat','Chat'],['work','Work'],['search','Search'],['projects','Projects'],['history','History'],['settings','Settings'],
].map(([id,label])=>`<button class="nav-item" data-view="${id}"><span class="nav-icon">${icon(id,15)}</span>${label}</button>`).join('');

function closeSidebar(){$('sidebar').classList.remove('open');$('scrim').classList.remove('open');$('menuButton').setAttribute('aria-expanded','false')}
function toggleSidebar(){const open=!$('sidebar').classList.contains('open');$('sidebar').classList.toggle('open',open);$('scrim').classList.toggle('open',open);$('menuButton').setAttribute('aria-expanded',String(open))}
$('menuButton').onclick=toggleSidebar;$('scrim').onclick=closeSidebar;
window.addEventListener('keydown',e=>{if(e.key==='Escape')closeSidebar()});
$('newChatBtn').onclick=()=>{location.hash='#/chat';closeSidebar()};

/* ---------------------------------------------------------------- router */

function currentRoute(){
  const raw=(location.hash||'#/chat').replace(/^#\/?/,'');
  const parts=raw.split('/');
  return {view:parts[0]||'chat', id:parts[1]?decodeURIComponent(parts[1]):null};
}
function go(hash){location.hash=hash}
function setActiveNav(view){
  document.querySelectorAll('.nav-item').forEach(b=>b.classList.toggle('active',b.dataset.view===view));
  const titles={chat:'Chat',work:'Work',search:'Search',projects:'Projects',history:'History',settings:'Settings'};
  $('viewTitle').textContent=titles[view]||'Falguna';
}
document.querySelectorAll('.nav-item').forEach(b=>b.onclick=()=>{go('#/'+b.dataset.view);closeSidebar()});

async function router(){
  const {view,id}=currentRoute();
  setActiveNav(view);
  const vp=$('viewport');
  try{
    if(view==='chat'){await renderSideChats();return renderChatView(id)}
    if(view==='work'){await renderSideMissions();return renderWorkView(id)}
    if(view==='search'){hideSideList();return renderSearchView(id)}
    if(view==='projects'){hideSideList();return renderProjectsView()}
    if(view==='history'){hideSideList();return renderHistoryView()}
    if(view==='settings'){hideSideList();return renderSettingsView()}
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

async function renderChatView(id){
  await loadProfiles();
  const vp=$('viewport');
  if(!id){
    vp.innerHTML=`
      <div class="chat-view">
        <div class="chat-scroll"><div class="chat-welcome">
          <div class="glow">${icon('spark',24)}</div>
          <h1>How can I help?</h1>
          <p>Ask a question, think something through, or describe what you're working on.</p>
          <div class="chip-row">${STARTERS.map(([ic,label])=>`<button class="chip" data-starter="${esc(label)}">${icon(ic,13)}${esc(label)}</button>`).join('')}</div>
        </div></div>
        ${composerHtml('Start chat')}
      </div>`;
    wireComposerChrome();
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
      }catch(err){$('sendBtn').disabled=false;alert(err.message)}
    });
    return;
  }
  vp.innerHTML=`<div class="chat-view"><div class="chat-scroll"><div class="thread" id="thread"><div class="empty-state">Loading conversation&hellip;</div></div></div><div id="handoffMount"></div>${composerHtml('Send')}</div>`;
  wireComposerChrome();
  let data;
  try{
    data=await api('/api/conversations/'+id);
  }catch(err){
    $('thread').innerHTML=`<div class="empty-state error">${esc(err.message)}</div>`;
    return;
  }
  renderThread(data);
  $('composer').addEventListener('submit',async e=>{
    e.preventDefault();
    const content=$('composerInput').value.trim();
    if(!content)return;
    $('composerInput').value='';
    $('sendBtn').disabled=true;
    appendOptimisticUserBubble(content);
    try{
      await api(`/api/conversations/${id}/messages`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({content})});
      data=await api('/api/conversations/'+id);
      renderThread(data);
      renderSideChats();
    }catch(err){
      appendErrorBubble(err.message);
    }finally{
      $('sendBtn').disabled=false;
    }
  });
}
function composerHtml(sendLabel){
  return `<div class="composer-wrap"><form class="composer" id="composer">
    <textarea id="composerInput" required placeholder="Message Falguna&hellip;" rows="1"></textarea>
    <div class="compose-row">
      <button type="button" class="icon-btn" id="attachBtn" title="Attachments (coming soon)">${icon('clip',16)}</button>
      <span class="mode-chip">${icon('chat',12)}Chat</span>
      <div class="compose-spacer"></div>
      <button class="send-btn" id="sendBtn" type="submit" aria-label="${esc(sendLabel)}">${icon('send',14)}</button>
    </div>
  </form><div class="compose-foot">Falguna can be wrong. Hand off to Work for changes that need to be verified.</div></div>`;
}
function wireComposerChrome(){
  const ta=$('composerInput');
  ta.addEventListener('input',()=>{ta.style.height='auto';ta.style.height=Math.min(ta.scrollHeight,180)+'px'});
  ta.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();$('composer').requestSubmit()}});
  const attach=$('attachBtn');
  if(attach)attach.onclick=()=>alert('Attachments are not part of this release yet.');
}
function appendOptimisticUserBubble(content){
  const t=$('thread');
  t.insertAdjacentHTML('beforeend',`<div class="msg user"><div class="avatar">Y</div><div class="bubble">${esc(content)}</div></div><div class="msg assistant" id="thinkingRow"><div class="avatar">F</div><div class="bubble thinking"><span class="tdot"></span><span class="tdot"></span><span class="tdot"></span></div></div>`);
  $('thread').closest('.chat-scroll').scrollTop=9e6;
}
function appendErrorBubble(message){
  const row=$('thinkingRow');
  if(row)row.remove();
  $('thread').insertAdjacentHTML('beforeend',`<div class="msg assistant"><div class="avatar">F</div><div class="bubble error">${esc(message)}</div></div>`);
}
function renderThread(data){
  const {conversation,messages,handoffs}=data;
  document.title='Falguna · '+conversation.title;
  const t=$('thread');
  if(!messages.length){
    t.innerHTML='<div class="empty-state">Say something to get started.</div>';
  }else{
    t.innerHTML=messages.map(m=>{
      if(m.role==='user')return `<div class="msg user"><div class="avatar">Y</div><div class="bubble">${esc(m.content)}</div></div>`;
      if(m.error)return `<div class="msg assistant"><div class="avatar">F</div><div class="bubble error">${esc(m.error)}</div></div>`;
      return `<div class="msg assistant"><div class="avatar">F</div><div class="bubble">${esc(m.content)}</div></div>`;
    }).join('');
  }
  const last=messages[messages.length-1];
  const suggestion=last&&last.role==='assistant'&&last.suggested_objective;
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
    if(!project_id||objective.length<12){alert('Choose a project and describe a bounded objective (12+ characters).');return}
    $('handoffBtn').disabled=true;$('handoffBtn').textContent='Starting…';
    try{
      const {id}=currentRoute();
      const out=await api(`/api/conversations/${id}/handoff`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project_id,objective})});
      watchOperation(out.operation,runId=>go('#/work/'+runId));
    }catch(err){
      $('handoffBtn').disabled=false;$('handoffBtn').textContent='Start Work mission';
      alert(err.message+(err.data&&err.data.discovery?' -- discovery needs a narrower objective.':''));
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
      }catch(err){$('run').disabled=false;alert(err.message)}
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
  const origin=handoffs.length?`<div class="origin-banner"><span>Started from a Chat handoff</span><a href="#/chat/${esc(handoffs[0].conversation_id)}">Open the conversation</a></div>`:'';
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
    await api(`/api/runs/${runId}/decision`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:b.dataset.action,reason})});
    await refreshWork(runId);
  });
  const resumeBtn=$('resume');
  if(resumeBtn)resumeBtn.onclick=async()=>{const out=await api(`/api/runs/${runId}/resume`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});resumeBtn.disabled=true;watchOperation(out.operation,()=>refreshWork(runId))};
  const pauseBtn=$('pause');
  if(pauseBtn)pauseBtn.onclick=async()=>{await api(`/api/runs/${runId}/pause`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});await refreshWork(runId)};
  const cancelBtn=$('cancel');
  if(cancelBtn)cancelBtn.onclick=async()=>{await api(`/api/runs/${runId}/cancel`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});await refreshWork(runId)};
}
let operationTimer=null;
async function watchOperation(token,onRunId){
  clearTimeout(operationTimer);
  try{
    const op=await api('/api/operations/'+token);
    if(op.run_id&&onRunId){onRunId(op.run_id);onRunId=null}
    if(op.state==='FAILED'){alert(op.error);return}
    if(op.state!=='COMPLETE')operationTimer=setTimeout(()=>watchOperation(token,onRunId),800);
    else{renderSideMissions()}
  }catch(err){alert(err.message)}
}

/* ------------------------------------------------------------- Search view */

async function renderSearchView(query){
  const vp=$('viewport');
  vp.innerHTML=`<div class="page"><h1>Search</h1><p class="lede">Search across chats and Work missions.</p>
    <div class="searchbar"><input id="q" placeholder="Search chats and missions&hellip;" value="${esc(query||'')}"><button id="go">Search</button></div>
    <div class="result-list" id="results"></div></div>`;
  const run=async()=>{
    const q=$('q').value.trim();
    history.replaceState(null,'','#/search/'+encodeURIComponent(q));
    if(!q){$('results').innerHTML='<div class="empty-state">Type to search.</div>';return}
    const {results}=await api('/api/search?q='+encodeURIComponent(q));
    $('results').innerHTML=results.length?results.map(r=>r.type==='conversation'
      ?`<button class="result-row" data-open="#/chat/${esc(r.id)}"><div class="kind">Chat</div><div class="title">${esc(r.title)}</div><div class="meta">${esc(timeAgo(r.updated_at))}</div></button>`
      :`<button class="result-row" data-open="#/work/${esc(r.run_id)}"><div class="kind">Work &middot; ${esc(String(r.status||'').replaceAll('_',' '))}</div><div class="title">${esc(r.title)}</div><div class="meta">${esc(r.repository||'')}</div></button>`
    ).join(''):'<div class="empty-state">No matches.</div>';
    document.querySelectorAll('[data-open]').forEach(b=>b.onclick=()=>go(b.dataset.open));
  };
  $('go').onclick=run;
  $('q').addEventListener('keydown',e=>{if(e.key==='Enter')run()});
  if(query)run();
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

/* ------------------------------------------------------------ History view */

async function renderHistoryView(){
  const vp=$('viewport');
  vp.innerHTML=`<div class="page"><h1>History</h1><p class="lede">Every chat and mission, most recent first.</p><div class="result-list" id="historyList"></div></div>`;
  const [{conversations},{runs}]=await Promise.all([api('/api/conversations'),api('/api/runs')]);
  const items=[
    ...(conversations||[]).map(c=>({type:'conversation',id:c.id,title:c.title,updated_at:c.updated_at})),
    ...(runs||[]).map(r=>({type:'mission',run_id:r.run_id,title:r.title,status:r.status,updated_at:r.updated_at,repository:r.repository})),
  ].sort((a,b)=>new Date(b.updated_at)-new Date(a.updated_at));
  $('historyList').innerHTML=items.length?items.map(r=>r.type==='conversation'
    ?`<button class="result-row" data-open="#/chat/${esc(r.id)}"><div class="kind">Chat</div><div class="title">${esc(r.title)}</div><div class="meta">${esc(timeAgo(r.updated_at))}</div></button>`
    :`<button class="result-row" data-open="#/work/${esc(r.run_id)}"><div class="kind">Work · <span class="status-pill ${esc((r.status||'').toLowerCase())}">${esc(String(r.status||'').replaceAll('_',' '))}</span></div><div class="title">${esc(r.title)}</div><div class="meta">${esc(timeAgo(r.updated_at))}</div></button>`
  ).join(''):'<div class="empty-state">Nothing yet. Start a chat or a mission.</div>';
  document.querySelectorAll('[data-open]').forEach(b=>b.onclick=()=>go(b.dataset.open));
}

/* ----------------------------------------------------------- Settings view */

async function renderSettingsView(){
  const vp=$('viewport');
  const s=await api('/api/settings');
  vp.innerHTML=`<div class="page"><h1>Settings</h1><p class="lede">Informational only in this release &mdash; nothing here can change Falguna's safety policy from the browser.</p>
    <div class="settings-note">Approval boundaries, verification, review, isolation, and audit are enforced in the control plane and are not editable from the UI.</div>
    <div class="settings-list">
      <div><span style="color:var(--muted-dim)">Model:</span> ${esc(s.model)}</div>
      <div><span style="color:var(--muted-dim)">Approved projects:</span> ${esc(s.profiles.length)}</div>
    </div>
    <div class="section-label">Safety boundaries preserved in this release</div>
    <div class="settings-list">${s.boundaries.map(b=>`<div>${esc(b)}</div>`).join('')}</div>
    <div class="section-label">Approved projects</div>
    <div class="settings-list">${s.profiles.map(p=>`<div><b>${esc(p.name)}</b> &middot; ${esc(p.repository)} &middot; cap $${esc(p.default_budget_usd)}</div>`).join('')}</div>
  </div>`;
}

router().catch(e=>{$('viewport').innerHTML=`<div class="page"><div class="empty-state error">${esc(e.message)}</div></div>`});
</script></body></html>
'''
