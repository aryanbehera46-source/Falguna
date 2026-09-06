import json
import os
import secrets
import shutil
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .codex_transport import CodexCliJSONTransport
from .discovery import ProjectDiscovery
from .gateway import OpenAICompatibleGateway
from .models import CommandSpec, RunPolicy
from .review import ModelSemanticReviewer
from .runtime import open_control_plane
from .usability import evidence_summary, mission_view
from .workers import StructuredEditWorker


MODEL = "gpt-5.4-mini"
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
    server_version = "FalgunaLocal/0.8"

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
                if view["status"] in {"DONE_CANDIDATE", "FAILED", "QUARANTINED"}:
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
        plan = ProjectDiscovery(Path(profile["repository"]), profile).discover(objective)
        if plan.requires_approval:
            return self._json({"error": "Discovery is uncertain; approve or narrow the proposed scope before modification", "discovery": plan.evidence()}, HTTPStatus.CONFLICT)
        editable = validate_editable(plan.editable_files)
        commands = plan.verification_commands
        cap = float(body.get("max_cost_usd", profile["default_budget_usd"]))
        if cap < 0 or cap > float(profile["default_budget_usd"]):
            raise ValueError("Cost cap exceeds the approved project-profile maximum")
        token = secrets.token_urlsafe(16)
        with _operations_lock:
            _operations[token] = {"state": "STARTING", "run_id": None}
        with _operations_lock:
            _operations[token]["discovery"] = plan.evidence()
        thread = threading.Thread(target=_run_mission, args=(self.app_root, token, profile, objective, editable, commands, cap, plan.evidence()), daemon=True)
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
        dependency_path = Path(profile["repository"]) / "node_modules"
        policy = RunPolicy(allowed_write_globs=editable, verification_commands=commands, max_cost_usd=cap, dependency_node_path=str(dependency_path) if dependency_path.is_dir() else None, verification_write_regexes=profile.get("verification_write_regexes", []))
        codex = shutil.which("codex")
        if not codex:
            raise RuntimeError("authenticated Codex executable not found")
        gateway = OpenAICompatibleGateway(MODEL, "http://127.0.0.1:1/v1", "")
        codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        transport = CodexCliJSONTransport(Path(codex), codex_home, timeout_seconds=300)
        worker = StructuredEditWorker(gateway, editable, transport=transport)
        control.reviewer = ModelSemanticReviewer(gateway, timeout_seconds=300, transport=transport)
        ids = control.create_mission(objective[:80], objective, Path(profile["repository"]), policy)
        def created(run_id):
            evidence_dir = app_root / ".falguna" / "evidence" / run_id
            evidence_dir.mkdir(parents=True, exist_ok=True)
            discovery_path = evidence_dir / "discovery.json"
            discovery_path.write_text(json.dumps(discovery, indent=2, sort_keys=True))
            control._record_artifact(run_id, "DISCOVERY", discovery_path)
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
        transport = CodexCliJSONTransport(Path(codex), codex_home, timeout_seconds=300)
        worker = StructuredEditWorker(gateway, policy.allowed_write_globs, transport=transport)
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
        raise ValueError("Falguna v0.8 is local-only")
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
