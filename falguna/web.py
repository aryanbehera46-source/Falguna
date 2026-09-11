import json
import os
import secrets
import shutil
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

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
    server_version = "FalgunaLocal/1.1"

    def log_message(self, format, *args):
        return

    @property
    def app_root(self):
        return self.server.app_root

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            return self._html(INDEX_HTML)
        if path == "/api/config":
            profiles = load_profiles(self.app_root)
            return self._json({"product": "Falguna Engineering", "stage": "internal alpha", "profiles": profiles, "model": MODEL})
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
                view = mission_view(store, self.app_root / ".falguna", run_id)
                if view["status"] in {"DONE_CANDIDATE", "FAILED", "QUARANTINED", "CANCELLED"}:
                    view = evidence_summary(store, self.app_root / ".falguna", control.audit, run_id)
                return self._json(view)
            except ValueError as exc:
                return self._json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
            finally:
                store.close()
        return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

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
            return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            return self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def _start(self, body):
        profiles = {item["id"]: item for item in load_profiles(self.app_root)}
        profile = profiles.get(body.get("project"))
        if not profile:
            raise ValueError("Select an approved project")
        objective = str(body.get("objective", "")).strip()
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
            return self._json({"error": f"{error}: Discovery is uncertain; approve or narrow the proposed scope before modification", "discovery": plan.evidence()}, HTTPStatus.CONFLICT)
        editable = validate_editable(plan.editable_files)
        commands = plan.verification_commands
        cap = float(body.get("max_cost_usd", profile["default_budget_usd"]))
        if cap < 0 or cap > float(profile["default_budget_usd"]):
            raise ValueError("Cost cap exceeds the approved project-profile maximum")
        token = secrets.token_urlsafe(16)
        with _operations_lock:
            _operations[token] = {"state": "STARTING", "run_id": None}
        discovery = {**plan.evidence(), "cache": cache, "duration_ms": discovery_ms}
        with _operations_lock:
            _operations[token]["discovery"] = discovery
        thread = threading.Thread(target=_run_mission, args=(self.app_root, token, profile, objective, editable, commands, cap, discovery), daemon=True)
        thread.start()
        return self._json({"operation": token, "state": "STARTING"}, HTTPStatus.ACCEPTED)

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


def _run_mission(app_root, token, profile, objective, editable, commands, cap, discovery):
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
<title>Falguna Engineering</title><style>
:root{color-scheme:dark;--bg:#0a0d12;--panel:#121720;--line:#293243;--text:#edf2f7;--muted:#99a6b8;--accent:#7be0b8;--warn:#ffc66d;--bad:#ff8585}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,-apple-system,sans-serif}.shell{max-width:980px;margin:auto;padding:32px 20px 64px}header{display:flex;justify-content:space-between;gap:20px;align-items:start;margin-bottom:26px}h1{margin:0;font-size:30px}h2{font-size:18px;margin:0 0 18px}.tag{border:1px solid var(--line);border-radius:999px;padding:6px 10px;color:var(--accent)}.grid{display:grid;grid-template-columns:1fr 1fr;gap:18px}.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:20px}.wide{grid-column:1/-1}label{display:block;color:var(--muted);font-size:13px;margin:14px 0 6px}input,select,textarea,button{font:inherit}input,select,textarea{width:100%;color:var(--text);background:#0d1118;border:1px solid var(--line);border-radius:8px;padding:10px}textarea{min-height:110px;resize:vertical}button{border:0;border-radius:8px;padding:10px 15px;font-weight:650;cursor:pointer;background:var(--accent);color:#082018}button.secondary{background:#273142;color:var(--text)}button.danger{background:#542b33;color:#ffdfe3}button:disabled{opacity:.5;cursor:not-allowed}.actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:18px}.muted{color:var(--muted)}.status{font-size:22px;color:var(--accent)}.milestones{display:flex;gap:8px;flex-wrap:wrap;padding:0}.milestones li{list-style:none;border:1px solid var(--line);border-radius:999px;padding:5px 9px}.facts{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}.fact{background:#0d1118;border-radius:8px;padding:12px}.fact span{display:block;color:var(--muted);font-size:12px}.error{color:var(--bad)}.hidden{display:none}@media(max-width:700px){.grid{grid-template-columns:1fr}.facts{grid-template-columns:1fr 1fr}header{display:block}.tag{display:inline-block;margin-top:10px}}
</style></head><body><main class="shell"><header><div><h1>Falguna Engineering</h1><div class="muted">Local engineering control plane</div></div><span class="tag">internal alpha</span></header>
<div class="grid"><section class="card"><h2>New Project Mission</h2><form id="mission"><label>Approved project</label><select id="project" required></select><label>Bounded engineering objective</label><textarea id="objective" required placeholder="Describe one small, testable problem. Falguna will discover the files and native checks."></textarea><label>Optional hard cost cap (USD)</label><input id="cost" type="number" min="0" step="0.01"><div class="muted" id="risk"></div><button id="run" type="submit" style="margin-top:18px">Discover &amp; Run Mission</button></form><p class="muted">Falguna pauses before editing when discovery is uncertain or scope must expand.</p></section>
<section class="card"><h2>Mission Status</h2><div id="empty" class="muted">Submit a task to see live milestones.</div><div id="status" class="hidden"><div class="status" id="state"></div><div class="muted" id="missionTitle"></div><ul class="milestones" id="milestones"></ul><div id="failure" class="error"></div></div></section>
<section class="card wide hidden" id="evidence"><h2>Final Evidence</h2><div class="facts" id="facts"></div><label>Files changed</label><div id="files"></div><label>Unresolved issues</label><div id="issues"></div><div class="actions"><button class="secondary hidden" id="resume">Resume from Checkpoint</button><span id="decisions"><button data-action="approve">Approve Merge</button><button class="danger" data-action="reject">Reject</button><button class="secondary" data-action="request-changes">Request Changes</button></span></div><p class="muted">A decision records human intent only. This app cannot merge or deploy.</p></section></div></main>
<script>
const $=id=>document.getElementById(id);let runId=null,poll=null,profiles={};
async function api(url,options){const r=await fetch(url,options);const j=await r.json();if(!r.ok)throw new Error(j.error||'Request failed');return j}
async function boot(){const c=await api('/api/config');c.profiles.forEach(p=>profiles[p.id]=p);$('project').innerHTML=c.profiles.map(p=>`<option value="${p.id}">${p.name}</option>`).join('');selectProject();const saved=new URLSearchParams(location.search).get('run');if(saved){runId=saved;$('empty').classList.add('hidden');$('status').classList.remove('hidden');await refresh()}}
function selectProject(){const p=profiles[$('project').value];if(!p)return;$('cost').value=p.default_budget_usd;$('cost').max=p.default_budget_usd;$('risk').textContent='Risk: '+p.risk}
$('project').addEventListener('change',selectProject);$('mission').addEventListener('submit',async e=>{e.preventDefault();$('run').disabled=true;$('empty').classList.add('hidden');$('status').classList.remove('hidden');$('state').textContent='Discovering safe scope…';$('evidence').classList.add('hidden');try{const out=await api('/api/runs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project:$('project').value,objective:$('objective').value,max_cost_usd:Number($('cost').value)})});watchOperation(out.operation)}catch(err){showError(err.message);$('run').disabled=false}});
async function watchOperation(token){try{const op=await api('/api/operations/'+token);if(op.run_id){runId=op.run_id;await refresh()}if(op.state==='FAILED'){showError(op.error);$('run').disabled=false;return}if(op.state!=='COMPLETE')setTimeout(()=>watchOperation(token),700);else{$('run').disabled=false;await refresh()}}catch(err){showError(err.message);$('run').disabled=false}}
async function refresh(){if(!runId)return;const d=await api('/api/runs/'+runId);$('state').textContent=d.status;$('missionTitle').textContent=d.mission+' · Attempt '+d.attempt;$('milestones').innerHTML=(d.completed_milestones||[]).map(x=>`<li>${x}</li>`).join('');$('failure').textContent=d.failure?`${d.failure.category}: ${d.failure.message} — ${d.failure.action}`:'';if(['DONE_CANDIDATE','FAILED','QUARANTINED'].includes(d.status))showEvidence(d)}
function showEvidence(d){$('evidence').classList.remove('hidden');const items=[['Requirements',d.requirement_coverage||'—'],['Native tests',(d.native_tests||[]).length?d.native_tests.filter(x=>x.passed).length+'/'+d.native_tests.length:'—'],['Browser/E2E',d.browser_e2e||'—'],['Independent review',d.independent_review||'—'],['Cost','$'+Number(d.cost_usd||0).toFixed(4)],['Risk',d.risk||'—'],['Evidence hashes',d.evidence_hashes_valid?'VALID':'NOT PROVEN'],['Audit chain',d.audit_chain_valid?'VALID':'NOT PROVEN'],['Merge approval',d.merge_approval||'—']];$('facts').innerHTML=items.map(x=>`<div class="fact"><span>${x[0]}</span>${x[1]}</div>`).join('');$('files').textContent=(d.files_changed||[]).join(', ')||'None';$('issues').textContent=(d.unresolved_issues||[]).join('; ')||'None';$('resume').classList.toggle('hidden',!['FAILED','QUARANTINED'].includes(d.status));$('decisions').classList.toggle('hidden',d.status!=='DONE_CANDIDATE'||d.merge_approval!=='PENDING')}
function showError(message){$('state').textContent='Unable to continue';$('failure').textContent=message}
document.querySelectorAll('[data-action]').forEach(b=>b.addEventListener('click',async()=>{const action=b.dataset.action;const reason=prompt('Reason for this decision:');if(!reason)return;await api(`/api/runs/${runId}/decision`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,reason})});await refresh()}));boot().catch(e=>showError(e.message));
$('resume').addEventListener('click',async()=>{const out=await api(`/api/runs/${runId}/resume`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});$('resume').disabled=true;watchOperation(out.operation)});
</script></body></html>'''

# Work-ready daily-use shell. Kept self-contained so the localhost launcher has no asset build step.
INDEX_HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Falguna</title><style>
:root{color-scheme:dark;--bg:#080a0e;--side:#0d1016;--panel:#121720;--soft:#171d27;--line:#252d3a;--text:#f1f4f8;--muted:#929eae;--accent:#79e2b7;--warn:#ffc66d;--bad:#ff8c96}*{box-sizing:border-box}html,body{height:100%;overflow:hidden}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 Inter,ui-sans-serif,system-ui,-apple-system,sans-serif}button,input,select,textarea{font:inherit}.app{height:100dvh;display:grid;grid-template-columns:272px minmax(0,1fr);overflow:hidden}aside{background:var(--side);border-right:1px solid var(--line);padding:18px 12px;display:flex;flex-direction:column;min-width:0;min-height:0;overflow:hidden}.brand{display:flex;align-items:center;gap:10px;padding:4px 8px 18px;font-weight:750;font-size:17px;flex:none}.mark{display:grid;place-items:center;width:29px;height:29px;border-radius:9px;background:var(--accent);color:#062018;font-weight:900}.new{width:100%;flex:none;border:1px solid var(--line);background:var(--soft);color:var(--text);border-radius:10px;padding:10px 12px;text-align:left;cursor:pointer}.side-title{flex:none;padding:20px 8px 7px;color:var(--muted);font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.08em}.projects{display:grid;gap:2px;flex:none}.project{display:flex;align-items:center;gap:9px;width:100%;border:0;background:transparent;padding:7px 8px;border-radius:8px;color:#c9d1dc;text-align:left;cursor:pointer}.project:hover,.project.selected{background:var(--soft);color:var(--text)}.project-dot{width:8px;height:8px;border-radius:50%;background:var(--accent);box-shadow:0 0 0 3px #79e2b71a;flex:none}.history{flex:1;min-height:80px;overflow-x:hidden;overflow-y:auto;display:flex;flex-direction:column;gap:3px;padding-right:3px;scrollbar-width:thin;scrollbar-color:#303949 transparent}.history button{display:block;flex:0 0 auto;width:100%;min-width:0;border:0;background:transparent;color:var(--text);padding:8px;border-radius:8px;text-align:left;cursor:pointer;overflow:hidden}.history button:hover,.history button.active{background:var(--soft)}.history .mission-title,.history small{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.history small{color:var(--muted);font-size:11px;text-transform:capitalize}.history-empty{color:var(--muted);padding:8px;font-size:12px}.boundary{flex:none;border-top:1px solid var(--line);padding:13px 8px 2px;color:var(--muted);font-size:12px;background:var(--side)}.workspace{min-width:0;min-height:0;display:grid;grid-template-rows:58px minmax(0,1fr) auto;overflow:hidden}.topbar{border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;padding:0 22px}.topbar-title{display:flex;align-items:center;gap:10px}.menu-button{display:none;border:0;background:transparent;color:var(--text);padding:6px;border-radius:7px;cursor:pointer;font-size:18px}.badge{border:1px solid #285845;color:var(--accent);border-radius:999px;padding:4px 9px;font-size:11px}.conversation{min-height:0;overflow:auto;padding:34px max(22px,calc((100vw - 272px - 850px)/2))}.welcome{max-width:720px;margin:11vh auto 0;text-align:center}.welcome h1{font-size:30px;margin:0 0 9px}.welcome p{color:var(--muted);font-size:16px}.thread{max-width:850px;margin:auto;display:grid;gap:18px}.bubble{border:1px solid var(--line);background:var(--panel);border-radius:16px;padding:18px;overflow-wrap:anywhere}.bubble.user{margin-left:12%;background:#151b24}.eyebrow{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.08em;font-weight:700}.status-line{font-size:21px;font-weight:700;margin:4px 0}.error{color:var(--bad);overflow-wrap:anywhere}.timeline{display:flex;gap:7px;flex-wrap:wrap;margin-top:13px}.step{border:1px solid var(--line);border-radius:999px;padding:5px 9px;color:#cbd4df}.step:last-child{border-color:#326a55;color:var(--accent)}.cards{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin-top:14px}.card{background:var(--soft);border:1px solid var(--line);border-radius:11px;padding:12px;min-width:0}.card span{display:block;color:var(--muted);font-size:11px;margin-bottom:3px}.card b{overflow-wrap:anywhere}.details{margin-top:13px;display:grid;gap:10px}.details div{padding:11px 12px;border:1px solid var(--line);border-radius:10px;overflow-wrap:anywhere}.details span{color:var(--muted);display:block;font-size:11px}.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:15px}button.action{border:0;border-radius:9px;padding:9px 13px;font-weight:700;cursor:pointer;background:var(--accent);color:#062018}button.action.secondary{background:#293342;color:var(--text)}button.action.danger{background:#542c34;color:#ffe0e4}button:disabled{opacity:.5;cursor:not-allowed}.composer-wrap{padding:14px max(18px,calc((100vw - 272px - 850px)/2)) 18px;background:linear-gradient(transparent,var(--bg) 18%)}.composer{max-width:850px;margin:auto;background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:11px;box-shadow:0 15px 45px #0008}.composer:focus-within{border-color:#3b4a5e}.composer textarea{display:block;width:100%;min-height:54px;max-height:160px;resize:vertical;border:0;outline:0;background:transparent;color:var(--text);padding:7px}.compose-row{display:flex;align-items:center;gap:9px}.compose-row select,.compose-row input{background:var(--soft);border:1px solid var(--line);color:var(--text);border-radius:8px;padding:7px 9px;min-width:0}.compose-row select{flex:1}.compose-row input{width:88px}.send{border:0;background:var(--accent);color:#062018;border-radius:9px;padding:8px 13px;font-weight:750;cursor:pointer;white-space:nowrap}.hint{max-width:850px;margin:6px auto 0;text-align:center;color:var(--muted);font-size:11px}.scrim{display:none}.hidden{display:none!important}@media(max-width:850px){.app{grid-template-columns:1fr}.workspace{grid-column:1}.menu-button{display:inline-grid;place-items:center}aside{display:flex;position:fixed;inset:0 auto 0 0;width:min(86vw,300px);z-index:20;transform:translateX(-102%);transition:transform .18s ease;box-shadow:20px 0 50px #000a}aside.open{transform:translateX(0)}.scrim{position:fixed;inset:0;background:#0009;z-index:15}.scrim.open{display:block}.conversation,.composer-wrap{padding-left:15px;padding-right:15px}.cards{grid-template-columns:1fr 1fr}.topbar{padding:0 15px}}@media(max-width:520px){.badge{font-size:10px}.cards{grid-template-columns:1fr}.compose-row{flex-wrap:wrap}.compose-row select{flex-basis:calc(100% - 97px)}.compose-row input{width:88px}.send{width:100%}.bubble.user{margin-left:0}.conversation{padding-top:20px}.welcome{margin-top:5vh}.hint{display:none}}
</style></head><body><div class="app"><aside id="sidebar" aria-label="Mission navigation"><div class="brand"><span class="mark">F</span>Falguna</div><button class="new" id="newMission">＋ New mission</button><div class="side-title">Approved projects</div><div class="projects" id="projects"></div><div class="side-title">Recent missions</div><div class="history" id="history"><div class="history-empty">Loading missions…</div></div><div class="boundary">Localhost-only internal alpha<br>No automatic merge or deploy</div></aside><div class="scrim" id="scrim"></div><main class="workspace"><header class="topbar"><div class="topbar-title"><button class="menu-button" id="menuButton" aria-label="Open navigation" aria-expanded="false">☰</button><strong>Engineering work mode</strong></div><span class="badge">v1.1 RELIABILITY + SPEED</span></header><section class="conversation" id="conversation"><div class="welcome" id="welcome"><h1>What should we build?</h1><p>Choose an approved project and describe the outcome. Falguna discovers the safe file scope and native verification.</p></div><div class="thread hidden" id="thread"><article class="bubble user"><div class="eyebrow">You</div><div id="objectiveText"></div></article><article class="bubble"><div class="eyebrow">Falguna · Work mode</div><div class="status-line" id="state">Preparing mission…</div><div id="failure" class="error"></div><div class="timeline" id="timeline"></div><div class="cards hidden" id="cards"></div><div class="details hidden" id="details"></div><div class="actions hidden" id="actions"><button class="action secondary" id="pause">Pause safely</button><button class="action danger" id="cancel">Cancel</button><button class="action secondary" id="resume">Resume / Retry</button><button class="action" data-action="approve">Approve</button><button class="action danger" data-action="reject">Reject</button><button class="action secondary" data-action="request-changes">Request Changes</button></div></article></div></section><footer class="composer-wrap"><form class="composer" id="mission"><textarea id="objective" required placeholder="Describe a bounded client-work requirement…"></textarea><div class="compose-row"><select id="project" required aria-label="Approved project"></select><input id="cost" type="number" min="0" step="0.01" aria-label="Cost cap"><button class="send" id="run" type="submit">Run mission</button></div></form><div class="hint">Pause and cancel take effect at a safe boundary. Human approval stays required; no merge or deploy.</div></footer></main></div>
<script>
const $=id=>document.getElementById(id);let runId=null,profiles={},operationTimer=null;
async function api(url,options){const r=await fetch(url,options);const j=await r.json();if(!r.ok)throw Object.assign(new Error(j.error||'Request failed'),{data:j});return j}
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function boot(){const [c,recent]=await Promise.all([api('/api/config'),api('/api/runs')]);c.profiles.forEach(p=>profiles[p.id]=p);$('project').innerHTML=c.profiles.map(p=>`<option value="${esc(p.id)}">${esc(p.name)}</option>`).join('');$('projects').innerHTML=c.profiles.map(p=>`<button class="project" data-project="${esc(p.id)}"><span class="project-dot"></span><span>${esc(p.name)}</span></button>`).join('');document.querySelectorAll('[data-project]').forEach(b=>b.onclick=()=>{$('project').value=b.dataset.project;selectProject();closeSidebar();$('objective').focus()});renderHistory(recent.runs);selectProject();const saved=new URLSearchParams(location.search).get('run');if(saved)await openRun(saved)}
function renderHistory(runs){const list=(runs||[]).slice(0,20);$('history').innerHTML=list.length?list.map(r=>`<button data-run="${esc(r.run_id)}" title="${esc(r.title)}"><span class="mission-title">${esc(r.title)}</span><small>${esc(String(r.status||'unknown').replaceAll('_',' '))}</small></button>`).join(''):'<div class="history-empty">No missions yet.</div>';document.querySelectorAll('[data-run]').forEach(b=>b.onclick=()=>{openRun(b.dataset.run);closeSidebar()})}
function selectProject(){const p=profiles[$('project').value];if(!p)return;$('cost').value=p.default_budget_usd;$('cost').max=p.default_budget_usd;document.querySelectorAll('[data-project]').forEach(b=>b.classList.toggle('selected',b.dataset.project===p.id))}
function closeSidebar(){$('sidebar').classList.remove('open');$('scrim').classList.remove('open');$('menuButton').setAttribute('aria-expanded','false')}
function toggleSidebar(){const open=!$('sidebar').classList.contains('open');$('sidebar').classList.toggle('open',open);$('scrim').classList.toggle('open',open);$('menuButton').setAttribute('aria-expanded',String(open))}
function beginThread(objective){$('welcome').classList.add('hidden');$('thread').classList.remove('hidden');$('objectiveText').textContent=objective;$('state').textContent='Planning and discovering safe scope…';$('failure').textContent='';$('timeline').innerHTML='';$('cards').classList.add('hidden');$('details').classList.add('hidden');$('actions').classList.add('hidden')}
async function openRun(id){runId=id;const d=await api('/api/runs/'+id);history.replaceState(null,'','?run='+id);beginThread(d.objective||d.mission);renderRun(d)}
$('project').addEventListener('change',selectProject);$('menuButton').onclick=toggleSidebar;$('scrim').onclick=closeSidebar;window.addEventListener('keydown',e=>{if(e.key==='Escape')closeSidebar()});$('newMission').onclick=()=>{runId=null;history.replaceState(null,'','/');$('thread').classList.add('hidden');$('welcome').classList.remove('hidden');$('objective').value='';closeSidebar();$('objective').focus()};
$('mission').addEventListener('submit',async e=>{e.preventDefault();$('run').disabled=true;beginThread($('objective').value.trim());try{const out=await api('/api/runs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project:$('project').value,objective:$('objective').value,max_cost_usd:Number($('cost').value)})});watchOperation(out.operation)}catch(err){showError(err.message);$('run').disabled=false}});
async function watchOperation(token){clearTimeout(operationTimer);try{const op=await api('/api/operations/'+token);if(op.run_id){runId=op.run_id;history.replaceState(null,'','?run='+runId);await refresh()}if(op.state==='FAILED'){showError(op.error);$('run').disabled=false;return}if(op.state!=='COMPLETE')operationTimer=setTimeout(()=>watchOperation(token),800);else{$('run').disabled=false;await refresh();const recent=await api('/api/runs');renderHistory(recent.runs)}}catch(err){showError(err.message);$('run').disabled=false}}
async function refresh(){if(!runId)return;renderRun(await api('/api/runs/'+runId))}
function renderRun(d){$('state').textContent=(d.current_milestone||d.status).replaceAll('_',' ');$('failure').textContent=d.failure?`${d.failure.category}: ${d.failure.message}. ${d.failure.action}`:'';$('timeline').innerHTML=(d.progress_events||[]).map(x=>`<span class="step">${esc(x.label)}</span>`).join('');const terminal=['DONE_CANDIDATE','FAILED','QUARANTINED','PAUSED','CANCELLED'].includes(d.status);if(terminal)renderEvidence(d);else{$('cards').classList.add('hidden');$('details').classList.add('hidden');$('actions').classList.remove('hidden');renderControls(d)}}
function renderControls(d){const controls=d.available_controls||[];$('pause').classList.toggle('hidden',!controls.includes('Pause'));$('cancel').classList.toggle('hidden',!controls.includes('Cancel'));$('resume').classList.toggle('hidden',!controls.some(x=>['Resume','Retry'].includes(x)));document.querySelectorAll('[data-action]').forEach(b=>b.classList.toggle('hidden',d.status!=='DONE_CANDIDATE'||d.merge_approval!=='PENDING'))}
function renderEvidence(d){const values=[['Status',d.status],['Needs approval',d.merge_approval||'No'],['Tests',(d.native_tests||[]).length?d.native_tests.filter(x=>x.passed).length+'/'+d.native_tests.length:'Not run'],['Review',d.independent_review||'Not run'],['Cost','$'+Number(d.cost_usd||0).toFixed(4)],['Cache',d.discovery_cache||'Not recorded'],['Total time',((d.timings_ms||{}).total||0)+' ms'],['Evidence',d.evidence_hashes_valid?'Valid':'Not proven']];$('cards').innerHTML=values.map(x=>`<div class="card"><span>${esc(x[0])}</span><b>${esc(x[1])}</b></div>`).join('');$('cards').classList.remove('hidden');$('details').innerHTML=`<div><span>Files changed</span>${esc((d.files_changed||[]).join(', ')||'None')}</div><div><span>Native verification</span>${esc((d.native_tests||[]).map(x=>x.label+': '+(x.passed?'passed':'failed')).join(' · ')||'Not run')}</div><div><span>Stage timings</span>${esc(Object.entries(d.timings_ms||{}).map(x=>x[0]+': '+x[1]+' ms').join(' · ')||'Not recorded')}</div><div><span>Independent review / audit</span>${esc(d.independent_review||'Not run')} · audit ${d.audit_chain_valid?'valid':'not proven'} · protected-main merges 0</div><div><span>Unresolved issues</span>${esc((d.unresolved_issues||[]).join('; ')||'None')}</div>`;$('details').classList.remove('hidden');$('actions').classList.remove('hidden');renderControls(d)}
function showError(message){$('state').textContent='Blocked';$('failure').textContent=message}
document.querySelectorAll('[data-action]').forEach(b=>b.onclick=async()=>{const reason=prompt('Reason for this decision:');if(!reason)return;await api(`/api/runs/${runId}/decision`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:b.dataset.action,reason})});await refresh()});
$('resume').onclick=async()=>{const out=await api(`/api/runs/${runId}/resume`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});$('resume').disabled=true;watchOperation(out.operation)};
$('pause').onclick=async()=>{await api(`/api/runs/${runId}/pause`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});await refresh()};
$('cancel').onclick=async()=>{await api(`/api/runs/${runId}/cancel`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});await refresh()};
boot().catch(e=>showError(e.message));
</script></body></html>'''
