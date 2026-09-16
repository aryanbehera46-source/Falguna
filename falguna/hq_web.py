"""TTT HQ -- a distinct local web application, not a tab inside Falguna.

Hierarchy: Aryan -> Twenty Two Technologies Pvt. Ltd. -> Falguna + other TTT
products. This module is the "Twenty Two Technologies" surface: it owns
Boardroom, the Master Vision Backlog, and the Needs Aryan queue today, with
Opportunities / Sales Pipeline / Clients / Active Jobs / Revenue / Ventures
listed as future scope (not built here -- this pass is separation only).

Integration boundary with Falguna Engineering (falguna/web.py):
  TTT HQ manages/decides -> Falguna executes -> Falguna returns evidence ->
  TTT HQ records the business outcome.
Concretely: this server reads Falguna's own `supervisor_states`/`runs` tables
(read-only, via the same shared StateStore -- see NeedsAryanQueue in
ttt_hq.py) to surface missions awaiting a decision, and any decision on an
actionable one is written back through Falguna's own, unmodified
`ControlPlane.decide_merge` -- never a parallel write path. This process
never starts, pauses, cancels, or resumes a mission; only Falguna Engineering
does that.

Shared backend, separate surface: this server opens the exact same
`<root>/.falguna/state.db` that `falguna/web.py` opens (same
`open_control_plane`), but serves none of Falguna Engineering's routes or
HTML, and vice versa.
"""

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

from .runtime import open_control_plane
from .ttt_hq import BacklogStore, BoardroomStore, NeedsAryanQueue, hq_overview

PRODUCT_NAME = "Twenty Two Technologies HQ"


class TTTHQHandler(BaseHTTPRequestHandler):
    server_version = "TTTHQLocal/1.0"

    def log_message(self, format, *args):
        return

    @property
    def app_root(self):
        return self.server.app_root

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            return self._html(HQ_INDEX_HTML)
        if path == "/api/config":
            return self._json({"product": PRODUCT_NAME, "stage": "internal alpha", "falguna_url": self.server.falguna_url})
        if path == "/api/hq/overview":
            return self._json(hq_overview())
        control, store = open_control_plane(self.app_root)
        try:
            if path == "/api/boardroom":
                return self._json({"topics": BoardroomStore(store, control.audit).list_topics()})
            if path.startswith("/api/boardroom/"):
                topic_id = path.rsplit("/", 1)[-1]
                topic = BoardroomStore(store, control.audit).get_topic(topic_id)
                return self._json(topic or {"error": "topic not found"}, HTTPStatus.OK if topic else HTTPStatus.NOT_FOUND)
            if path == "/api/backlog":
                return self._json({"items": BacklogStore(store, control.audit).list_items()})
            if path == "/api/needs-aryan":
                return self._json({"items": NeedsAryanQueue(store, control.audit, control).list_pending()})
        finally:
            store.close()
        return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self._body()
            control, store = open_control_plane(self.app_root)
            try:
                if path == "/api/boardroom":
                    boardroom = BoardroomStore(store, control.audit)
                    topic_id = boardroom.create_topic(
                        body.get("title", ""), body.get("summary", ""), body.get("actor", "Aryan"),
                        proposed_category=body.get("proposed_category"), proposed_phase=body.get("proposed_phase"),
                        proposed_priority=body.get("proposed_priority"), proposed_revenue_impact=body.get("proposed_revenue_impact"),
                    )
                    return self._json({"topic_id": topic_id}, HTTPStatus.CREATED)
                if path.startswith("/api/boardroom/") and path.endswith("/contribution"):
                    topic_id = path.split("/")[3]
                    contribution_id = BoardroomStore(store, control.audit).add_contribution(topic_id, body.get("perspective", ""), body.get("content", ""))
                    return self._json({"contribution_id": contribution_id}, HTTPStatus.CREATED)
                if path.startswith("/api/boardroom/") and path.endswith("/decision"):
                    topic_id = path.split("/")[3]
                    backlog = BacklogStore(store, control.audit)
                    result = BoardroomStore(store, control.audit).decide(topic_id, body.get("action", ""), body.get("actor", "Aryan"), note=body.get("note"), backlog_store=backlog)
                    return self._json(result)
                if path == "/api/backlog":
                    item_id = BacklogStore(store, control.audit).create_item(
                        body.get("title", ""), body.get("actor", "Aryan"), category=body.get("category"),
                        phase=body.get("phase"), priority=body.get("priority"), dependency=body.get("dependency"),
                        revenue_impact=body.get("revenue_impact"), status=body.get("status", "Future"),
                    )
                    return self._json({"item_id": item_id}, HTTPStatus.CREATED)
                if path.startswith("/api/backlog/"):
                    item_id = path.rsplit("/", 1)[-1]
                    actor = body.pop("actor", "Aryan")
                    reason = body.pop("reason", None)
                    item = BacklogStore(store, control.audit).update_item(item_id, actor, reason=reason, **body)
                    return self._json(item)
                if path == "/api/needs-aryan":
                    item_id = NeedsAryanQueue(store, control.audit, control).create_item(
                        body.get("kind", ""), body.get("title", ""), body.get("what_is_needed", ""),
                        actor=body.get("actor", "Aryan"), recommendation=body.get("recommendation"),
                        rationale=body.get("rationale"), risk=body.get("risk"), expected_value=body.get("expected_value"),
                        ref_type=body.get("ref_type"), ref_id=body.get("ref_id"),
                    )
                    return self._json({"item_id": item_id}, HTTPStatus.CREATED)
                if path.startswith("/api/needs-aryan/") and path.endswith("/decision"):
                    item_id = unquote(path.split("/")[3])
                    result = NeedsAryanQueue(store, control.audit, control).decide(item_id, body.get("action", ""), body.get("actor", "Aryan"), note=body.get("note"))
                    return self._json(result)
                return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            finally:
                store.close()
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            return self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

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


def serve_hq(root, host: str = "127.0.0.1", port: int = 8766, falguna_url: str = "http://127.0.0.1:8765") -> None:
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("TTT HQ is local-only")
    from pathlib import Path
    server = ThreadingHTTPServer((host, port), TTTHQHandler)
    server.app_root = Path(root).resolve()
    server.falguna_url = falguna_url
    print(f"Twenty Two Technologies HQ internal alpha: http://{host}:{server.server_port}")
    server.serve_forever()


HQ_INDEX_HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Twenty Two Technologies</title><style>
:root{color-scheme:dark;--bg:#0b0908;--side:#100c0a;--panel:#181310;--soft:#201a16;--line:#332a23;--text:#f7f3ef;--muted:#a89c8f;--accent:#e2a15c;--warn:#ffc66d;--bad:#ff8c96}*{box-sizing:border-box}html,body{height:100%;overflow:hidden}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 Inter,ui-sans-serif,system-ui,-apple-system,sans-serif}button,input,select,textarea{font:inherit}.app{height:100dvh;display:grid;grid-template-columns:250px minmax(0,1fr);overflow:hidden}aside{background:var(--side);border-right:1px solid var(--line);padding:18px 12px;display:flex;flex-direction:column;overflow:hidden}.brand{display:flex;align-items:center;gap:10px;padding:4px 8px 20px;font-weight:750;font-size:16px}.mark{display:grid;place-items:center;width:29px;height:29px;border-radius:9px;background:var(--accent);color:#221202;font-weight:900}.navsec{color:var(--muted);font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;padding:16px 8px 6px}.navitem{display:block;width:100%;text-align:left;border:0;background:transparent;color:var(--text);padding:8px 8px;border-radius:8px;cursor:pointer;font-size:13px}.navitem:hover,.navitem.active{background:var(--soft)}.navitem.disabled{color:#5b5148;cursor:default}.navitem.disabled:hover{background:transparent}.boundary{margin-top:auto;color:var(--muted);font-size:11px;padding:10px 8px 2px;border-top:1px solid var(--line)}main{min-width:0;overflow-y:auto;padding:28px max(24px,calc((100vw - 250px - 860px)/2))}.col{max-width:860px;margin:0 auto;display:grid;gap:20px}h1{font-size:22px;margin:0 0 2px}.pageintro{color:var(--muted);font-size:13px;margin-bottom:6px}.view{display:none}.view.active{display:block}.section{border:1px solid var(--line);background:var(--panel);border-radius:14px;padding:18px;margin-bottom:18px}.section h2{margin:0 0 4px;font-size:17px}.sub{color:var(--muted);font-size:12px;margin-bottom:14px}.list{display:grid;gap:10px}.item{border:1px solid var(--line);background:var(--soft);border-radius:11px;padding:13px}.item h3{margin:0 0 4px;font-size:14px}.meta{color:var(--muted);font-size:11px;display:flex;gap:8px;flex-wrap:wrap;margin-bottom:6px}.meta span{border:1px solid var(--line);border-radius:999px;padding:2px 8px}.empty{color:var(--muted);font-size:12px;padding:6px 0}.form{display:grid;gap:8px;margin-top:12px;border-top:1px solid var(--line);padding-top:12px}.form input,.form select,.form textarea{background:var(--panel);border:1px solid var(--line);color:var(--text);border-radius:8px;padding:8px 10px;width:100%}.form textarea{min-height:50px;resize:vertical}.row{display:flex;gap:8px}.row>*{flex:1}.actions{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}.actions button{border:0;border-radius:8px;padding:6px 11px;font-size:12px;font-weight:700;cursor:pointer;background:var(--accent);color:#221202}.actions button.secondary{background:#2c241d;color:var(--text)}.actions button.danger{background:#542c34;color:#ffe0e4}.contrib{border-left:2px solid var(--line);padding:6px 0 6px 10px;margin-top:6px;font-size:12px}.contrib b{color:var(--accent)}.badge-actionable{color:var(--accent)}.badge-inspect{color:var(--warn)}
@media(max-width:820px){.app{grid-template-columns:1fr;height:auto;min-height:100dvh}aside{flex-direction:row;flex-wrap:wrap;align-items:center;gap:4px;border-right:0;border-bottom:1px solid var(--line);padding:10px 12px}aside .brand{width:100%;padding:2px 4px 10px}aside .navsec,aside .boundary{display:none}aside .navitem{padding:6px 10px;font-size:12px}main{padding:20px 16px}.row{flex-direction:column}}
</style></head><body><div class="app"><aside>
<div class="brand"><span class="mark">TT</span>Twenty Two Technologies</div>
<div class="navsec">Company</div>
<button class="navitem active" data-view="boardroom">Boardroom</button>
<button class="navitem" data-view="backlog">Master Vision Backlog</button>
<button class="navitem" data-view="needsAryan">Needs Aryan</button>
<div class="navsec">Coming soon</div>
<button class="navitem disabled" disabled>Opportunities</button>
<button class="navitem disabled" disabled>Sales Pipeline</button>
<button class="navitem disabled" disabled>Clients</button>
<button class="navitem disabled" disabled>Active Jobs</button>
<button class="navitem disabled" disabled>Revenue</button>
<button class="navitem disabled" disabled>Ventures / Company Ops</button>
<div class="boundary">TTT HQ decides · Falguna executes<br>Local-only, no automatic merge or deploy</div>
</aside>
<main>
<div class="col">
<div class="view active" id="view-boardroom">
<h1>Boardroom</h1>
<div class="pageintro">Strategy / Technology / Revenue / Finance-Risk / Operations -- discuss, then decide. Decisions persist and are never in-memory only.</div>
<div class="list" id="boardroomList"></div>
<div class="form">
<input id="brTitle" placeholder="Topic title">
<textarea id="brSummary" placeholder="What are we deciding on?"></textarea>
<div class="row">
<select id="brCategory"><option value="">Category (optional)</option><option>Revenue</option><option>Engineering</option><option>Operations</option><option>Strategy</option></select>
<select id="brPriority"><option value="">Priority (optional)</option><option>Low</option><option>Medium</option><option>High</option></select>
</div>
<div class="actions"><button id="brCreate" type="button">Open topic</button></div>
</div>
</div>
<div class="view" id="view-backlog">
<h1>Master Vision Backlog</h1>
<div class="pageintro">Persistent roadmap. Approved Boardroom decisions can create items here automatically.</div>
<div class="list" id="backlogList"></div>
<div class="form">
<input id="blTitle" placeholder="Item title">
<div class="row">
<select id="blCategory"><option value="">Category</option><option>Revenue</option><option>Engineering</option><option>Operations</option><option>Strategy</option></select>
<select id="blPhase"><option value="">Phase</option><option>Now</option><option>Next</option><option>Later</option></select>
<select id="blPriority"><option value="">Priority</option><option>Low</option><option>Medium</option><option>High</option></select>
</div>
<input id="blRevenueImpact" placeholder="Revenue impact (optional)">
<div class="actions"><button id="blCreate" type="button">Add backlog item</button></div>
</div>
</div>
<div class="view" id="view-needsAryan">
<h1>Needs Aryan</h1>
<div class="pageintro">Every pending decision in one place -- business approvals and Falguna Engineering missions awaiting a merge decision.</div>
<div class="list" id="needsAryanList"></div>
</div>
</div>
</main>
</div>
<script>
const $=id=>document.getElementById(id);
async function api(url,options){const r=await fetch(url,options);const j=await r.json();if(!r.ok)throw Object.assign(new Error(j.error||'Request failed'),{data:j});return j}
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let FALGUNA_URL='http://127.0.0.1:8765';
document.querySelectorAll('.navitem[data-view]').forEach(b=>b.onclick=()=>{document.querySelectorAll('.navitem[data-view]').forEach(x=>x.classList.remove('active'));document.querySelectorAll('.view').forEach(x=>x.classList.remove('active'));b.classList.add('active');$('view-'+b.dataset.view).classList.add('active')});
async function loadAll(){const c=await api('/api/config');FALGUNA_URL=c.falguna_url||FALGUNA_URL;await Promise.all([loadBoardroom(),loadBacklog(),loadNeedsAryan()])}
async function loadBoardroom(){const d=await api('/api/boardroom');renderBoardroom(d.topics||[])}
function renderBoardroom(topics){$('boardroomList').innerHTML=topics.length?topics.map(t=>`<div class="item" data-topic="${esc(t.id)}">
<h3>${esc(t.title)}</h3>
<div class="meta"><span>${esc(t.status)}</span>${t.proposed_category?`<span>${esc(t.proposed_category)}</span>`:''}${t.proposed_priority?`<span>${esc(t.proposed_priority)}</span>`:''}</div>
<div>${esc(t.summary)}</div>
<div class="hqcontribs" id="contribs-${esc(t.id)}"></div>
${t.status==='OPEN'?`<div class="form">
<select class="pv"><option value="">Add a perspective…</option><option>Strategy</option><option>Technology</option><option>Revenue</option><option>Finance-Risk</option><option>Operations</option></select>
<textarea class="ct" placeholder="Contribution"></textarea>
<div class="actions">
<button class="secondary addc" data-topic="${esc(t.id)}">Add contribution</button>
<button class="dec" data-topic="${esc(t.id)}" data-action="approve">Approve</button>
<button class="secondary dec" data-topic="${esc(t.id)}" data-action="request-changes">Request Changes</button>
<button class="secondary dec" data-topic="${esc(t.id)}" data-action="defer">Defer</button>
<button class="danger dec" data-topic="${esc(t.id)}" data-action="reject">Reject</button>
</div>
</div>`:''}
</div>`).join(''):'<div class="empty">No Boardroom topics yet.</div>';
topics.forEach(t=>loadTopicHistory(t.id));
document.querySelectorAll('.addc').forEach(b=>b.onclick=async()=>{const item=b.closest('.item');const perspective=item.querySelector('.pv').value;const content=item.querySelector('.ct').value.trim();if(!perspective||!content)return alert('Pick a perspective and write something first.');await api(`/api/boardroom/${b.dataset.topic}/contribution`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({perspective,content})});await loadBoardroom()});
document.querySelectorAll('.dec[data-topic]').forEach(b=>b.onclick=async()=>{const note=prompt('Note for this decision (optional):')||'';await api(`/api/boardroom/${b.dataset.topic}/decision`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:b.dataset.action,note,actor:'Aryan'})});await loadBoardroom();await loadBacklog()})}
async function loadTopicHistory(id){try{const t=await api('/api/boardroom/'+id);const el=$('contribs-'+id);if(!el)return;el.innerHTML=(t.contributions||[]).map(c=>`<div class="contrib"><b>${esc(c.perspective)}:</b> ${esc(c.content)}</div>`).join('')+(t.decisions||[]).map(d=>`<div class="contrib"><b>${esc(d.action)}</b> by ${esc(d.decided_by)}${d.note?': '+esc(d.note):''}</div>`).join('')}catch(e){}}
$('brCreate').onclick=async()=>{const title=$('brTitle').value.trim();const summary=$('brSummary').value.trim();if(!title||!summary)return alert('Title and summary are required.');await api('/api/boardroom',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title,summary,actor:'Aryan',proposed_category:$('brCategory').value||null,proposed_priority:$('brPriority').value||null})});$('brTitle').value='';$('brSummary').value='';await loadBoardroom()};
async function loadBacklog(){const d=await api('/api/backlog');renderBacklog(d.items||[])}
function renderBacklog(items){$('backlogList').innerHTML=items.length?items.map(i=>`<div class="item">
<h3>${esc(i.title)}</h3>
<div class="meta"><span>${esc(i.status)}</span>${i.category?`<span>${esc(i.category)}</span>`:''}${i.phase?`<span>${esc(i.phase)}</span>`:''}${i.priority?`<span>${esc(i.priority)}</span>`:''}${i.revenue_impact?`<span>${esc(i.revenue_impact)}</span>`:''}</div>
<div class="actions">
${['Future','Planned','Active','Done','Deferred'].map(s=>`<button class="secondary setstatus" data-id="${esc(i.id)}" data-status="${s}">${s}</button>`).join('')}
</div>
</div>`).join(''):'<div class="empty">No backlog items yet.</div>';
document.querySelectorAll('.setstatus').forEach(b=>b.onclick=async()=>{await api(`/api/backlog/${b.dataset.id}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({status:b.dataset.status,actor:'Aryan'})});await loadBacklog()})}
$('blCreate').onclick=async()=>{const title=$('blTitle').value.trim();if(!title)return alert('Title is required.');await api('/api/backlog',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title,actor:'Aryan',category:$('blCategory').value||null,phase:$('blPhase').value||null,priority:$('blPriority').value||null,revenue_impact:$('blRevenueImpact').value||null})});$('blTitle').value='';$('blRevenueImpact').value='';await loadBacklog()};
async function loadNeedsAryan(){const d=await api('/api/needs-aryan');renderNeedsAryan(d.items||[])}
function renderNeedsAryan(items){$('needsAryanList').innerHTML=items.length?items.map(i=>`<div class="item">
<h3>${esc(i.title)}</h3>
<div class="meta"><span>${esc(i.kind)}</span>${i.risk?`<span>${esc(i.risk)}</span>`:''}${i.expected_value?`<span>value: ${esc(i.expected_value)}</span>`:''}<span class="${i.actionable?'badge-actionable':'badge-inspect'}">${i.actionable?'actionable here':'inspect in Falguna Engineering'}</span></div>
<div>${esc(i.what_is_needed)}</div>
${i.recommendation?`<div class="contrib"><b>Recommendation:</b> ${esc(i.recommendation)}</div>`:''}
${i.rationale?`<div class="contrib"><b>Rationale:</b> ${esc(i.rationale)}</div>`:''}
<div class="actions">
${i.actionable?`<button class="na" data-id="${esc(i.id)}" data-action="approve">Approve</button><button class="secondary na" data-id="${esc(i.id)}" data-action="request-changes">Request Changes</button><button class="danger na" data-id="${esc(i.id)}" data-action="reject">Reject</button>`:''}
<button class="secondary na" data-id="${esc(i.id)}" data-action="defer">Defer</button>
${i.source==='falguna_engineering'?`<a class="secondary" style="border:0;border-radius:8px;padding:6px 11px;font-size:12px;font-weight:700;text-decoration:none;background:#2c241d;color:var(--text)" href="${FALGUNA_URL}/?run=${esc(i.ref_id)}" target="_blank" rel="noopener">Open in Falguna Engineering</a>`:''}
</div>
</div>`).join(''):'<div class="empty">Nothing needs Aryan right now.</div>';
document.querySelectorAll('.na').forEach(b=>b.onclick=async()=>{const note=prompt('Note (optional):')||'';try{await api(`/api/needs-aryan/${encodeURIComponent(b.dataset.id)}/decision`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:b.dataset.action,note,actor:'Aryan'})});await loadNeedsAryan()}catch(e){alert(e.message)}})}
loadAll().catch(e=>{$('boardroomList').innerHTML=`<div class="empty">Unable to load: ${esc(e.message)}</div>`});
</script></body></html>'''
