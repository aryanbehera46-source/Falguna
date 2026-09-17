"""TTT HQ -- a distinct local web application, not a tab inside Falguna.

Hierarchy: Aryan -> Twenty Two Technologies Pvt. Ltd. -> Falguna + other TTT
products. This module is the "Twenty Two Technologies" surface: it owns
Boardroom, the Master Vision Backlog, the Needs Aryan queue, and (PASS 2)
Revenue Hunter -- Opportunities, the pipeline, proposals, follow-ups, Active
Jobs, and revenue analytics. Ventures / Company Ops remains future scope.

Revenue Hunter never sends anything to a client on its own: proposal and
follow-up generation always produce a DRAFT row; a proposal only becomes
APPROVED through the existing Needs Aryan queue (an owner decision), and a
follow-up only becomes SENT when the owner explicitly calls mark-sent after
actually sending it themselves.

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
from urllib.parse import parse_qs, unquote, urlparse

from .revenue_hunter import (
    ActiveJobError, ActiveJobStore, AnalyticsService, DashboardService, FollowupError,
    FollowupStore, OpportunityError, OpportunityStore, ProposalError, ProposalStore,
    QualificationStore, apply_decision_side_effect, extract_fields_from_text, extract_from_csv_rows,
)
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
            if path == "/api/rh/opportunities":
                query = parse_qs(urlparse(self.path).query)
                stage = (query.get("stage") or [None])[0]
                return self._json({"items": OpportunityStore(store, control.audit).list(stage)})
            if path.startswith("/api/rh/opportunities/"):
                opportunity_id = path.rsplit("/", 1)[-1]
                opportunity = OpportunityStore(store, control.audit).get(opportunity_id)
                return self._json(opportunity or {"error": "opportunity not found"}, HTTPStatus.OK if opportunity else HTTPStatus.NOT_FOUND)
            if path == "/api/rh/active-jobs":
                return self._json({"items": ActiveJobStore(store, control.audit).list()})
            if path == "/api/rh/dashboard":
                pending = NeedsAryanQueue(store, control.audit, control).list_pending()
                return self._json(DashboardService(store).today(pending))
            if path == "/api/rh/analytics":
                return self._json(AnalyticsService(store).summary())
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
                    queue = NeedsAryanQueue(store, control.audit, control)
                    before = store.get("needs_aryan_items", item_id)  # captured pre-decision for the side-effect hook below
                    result = queue.decide(item_id, body.get("action", ""), body.get("actor", "Aryan"), note=body.get("note"))
                    if before is not None and result.get("action"):
                        apply_decision_side_effect(store, control.audit, dict(before), result["action"], body.get("actor", "Aryan"))
                    return self._json(result)

                if path == "/api/rh/opportunities":
                    opportunities = OpportunityStore(store, control.audit)
                    import_mode = body.get("import_mode", "manual")
                    if import_mode == "paste_jd":
                        fields = extract_fields_from_text(body.get("text", ""))
                        fields.update({k: v for k, v in body.items() if k in {"title", "client_name", "source_url"} and v})
                        if not fields.get("title"):
                            raise ValueError("title is required (the JD text alone doesn't reliably contain one)")
                        opportunity_id = opportunities.create(fields, actor=body.get("actor", "Aryan"), source="paste_jd")
                        return self._json({"opportunity_id": opportunity_id}, HTTPStatus.CREATED)
                    if import_mode == "url":
                        url = body.get("url", "")
                        if not url.startswith(("http://", "https://")):
                            raise ValueError("url must start with http:// or https://")
                        fields = {"title": body.get("title", ""), "description": body.get("description"), "source_url": url}
                        opportunity_id = opportunities.create(fields, actor=body.get("actor", "Aryan"), source="url")
                        return self._json({"opportunity_id": opportunity_id}, HTTPStatus.CREATED)
                    if import_mode == "csv_json":
                        rows = extract_from_csv_rows(body.get("rows", []))
                        created = [opportunities.create(row, actor=body.get("actor", "Aryan"), source="csv_json") for row in rows]
                        return self._json({"opportunity_ids": created}, HTTPStatus.CREATED)
                    opportunity_id = opportunities.create(body, actor=body.get("actor", "Aryan"), source="manual")
                    return self._json({"opportunity_id": opportunity_id}, HTTPStatus.CREATED)

                if path.startswith("/api/rh/opportunities/") and path.endswith("/qualify"):
                    opportunity_id = path.split("/")[4]
                    result = QualificationStore(store, control.audit).qualify(opportunity_id, actor=body.get("actor", "Aryan"))
                    return self._json(result, HTTPStatus.CREATED)

                if path.startswith("/api/rh/opportunities/") and path.endswith("/stage"):
                    opportunity_id = path.split("/")[4]
                    opportunity = OpportunityStore(store, control.audit).move_stage(opportunity_id, body.get("to_stage", ""), body.get("actor", "Aryan"), note=body.get("note"))
                    return self._json(opportunity)

                if path.startswith("/api/rh/opportunities/") and path.endswith("/proposals"):
                    opportunity_id = path.split("/")[4]
                    needs_aryan = NeedsAryanQueue(store, control.audit, control)
                    result = ProposalStore(store, control.audit, needs_aryan).generate(opportunity_id, body.get("kind", ""), actor=body.get("actor", "Aryan"))
                    return self._json(result, HTTPStatus.CREATED)

                if path.startswith("/api/rh/opportunities/") and path.endswith("/followups"):
                    opportunity_id = path.split("/")[4]
                    followup_id = FollowupStore(store, control.audit).generate(opportunity_id, body.get("kind", ""), due_at=body.get("due_at"))
                    return self._json({"followup_id": followup_id}, HTTPStatus.CREATED)

                if path.startswith("/api/rh/followups/") and path.endswith("/sent"):
                    followup_id = path.split("/")[4]
                    FollowupStore(store, control.audit).mark_sent(followup_id, body.get("actor", "Aryan"))
                    return self._json({"followup_id": followup_id, "status": "SENT"})

                if path.startswith("/api/rh/opportunities/") and path.endswith("/won"):
                    opportunity_id = path.split("/")[4]
                    opportunity = OpportunityStore(store, control.audit).mark_won(opportunity_id, body.get("actor", "Aryan"), final_price=body.get("final_price"))
                    return self._json(opportunity)

                if path.startswith("/api/rh/opportunities/") and path.endswith("/lost"):
                    opportunity_id = path.split("/")[4]
                    opportunity = OpportunityStore(store, control.audit).mark_lost(opportunity_id, body.get("actor", "Aryan"), reason=body.get("reason"))
                    return self._json(opportunity)

                if path.startswith("/api/rh/opportunities/") and path.endswith("/active-job"):
                    opportunity_id = path.split("/")[4]
                    job_id = ActiveJobStore(store, control.audit).create_from_won_opportunity(opportunity_id, actor=body.get("actor", "Aryan"))
                    return self._json({"active_job_id": job_id}, HTTPStatus.CREATED)

                if path.startswith("/api/rh/active-jobs/") and path.endswith("/handoff"):
                    active_job_id = path.split("/")[4]
                    result = ActiveJobStore(store, control.audit).trigger_handoff(
                        active_job_id, body.get("repository", ""), control,
                        actor=body.get("actor", "Aryan"), policy_overrides=body.get("policy_overrides"),
                    )
                    return self._json(result, HTTPStatus.CREATED)

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
<div class="navsec">Revenue Hunter</div>
<button class="navitem" data-view="rhToday">Today</button>
<button class="navitem" data-view="rhOpportunities">Opportunities</button>
<button class="navitem" data-view="rhPipeline">Sales Pipeline</button>
<button class="navitem" data-view="rhClients">Clients</button>
<button class="navitem" data-view="rhActiveJobs">Active Jobs</button>
<button class="navitem" data-view="rhRevenue">Revenue</button>
<div class="navsec">Coming soon</div>
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
<div class="view" id="view-rhToday">
<h1>Today</h1>
<div class="pageintro">What should I do today? Highest-priority Revenue Hunter actions, in one place.</div>
<div class="row">
<div class="section" style="flex:1"><h2 id="rhPipelineValue">$0</h2><div class="sub">Pipeline value</div></div>
<div class="section" style="flex:1"><h2 id="rhWonRevenue">$0</h2><div class="sub">Won revenue</div></div>
</div>
<div class="section"><h2>Next actions</h2><div class="list" id="rhNextActions"></div></div>
</div>
<div class="view" id="view-rhOpportunities">
<h1>Opportunities</h1>
<div class="pageintro">Add an opportunity from a pasted job description, a URL, a manual form, or CSV/JSON import. Nothing here is ever sent automatically.</div>
<div class="form">
<select id="rhMode"><option value="manual">Manual form</option><option value="paste_jd">Paste job description</option><option value="url">Paste URL</option><option value="csv_json">CSV/JSON import</option></select>
<div id="rhModeManual">
<input id="rhTitle" placeholder="Title">
<input id="rhClient" placeholder="Client / company (optional)">
<textarea id="rhDescription" placeholder="Description"></textarea>
<div class="row"><input id="rhBudget" placeholder="Budget/rate"><input id="rhDeadline" placeholder="Deadline"></div>
<div class="row"><input id="rhSkills" placeholder="Required skills (comma-separated)"><input id="rhContractType" placeholder="Contract type"></div>
<div class="row"><input id="rhLocation" placeholder="Location/timezone"><input id="rhUrgency" placeholder="Urgency"></div>
</div>
<div id="rhModePasteJd" style="display:none">
<input id="rhJdTitle" placeholder="Title (the JD text alone often doesn't have a clean one)">
<input id="rhJdClient" placeholder="Client / company (optional)">
<textarea id="rhJdText" placeholder="Paste the full job description here" style="min-height:110px"></textarea>
</div>
<div id="rhModeUrl" style="display:none">
<input id="rhUrlTitle" placeholder="Title">
<input id="rhUrlValue" placeholder="https://...">
<textarea id="rhUrlDescription" placeholder="Description (Revenue Hunter never fetches the page itself -- paste what you see)"></textarea>
</div>
<div id="rhModeCsv" style="display:none">
<textarea id="rhCsvJson" placeholder='JSON array, e.g. [{"title":"Landing page","client_name":"Acme","budget_rate":"$500"}]' style="min-height:90px"></textarea>
</div>
<div class="actions"><button id="rhCreate" type="button">Add opportunity</button></div>
</div>
<div class="row" style="margin:6px 0">
<select id="rhStageFilter"><option value="">All stages</option></select>
</div>
<div class="list" id="rhOpportunityList"></div>
</div>
<div class="view" id="view-rhPipeline">
<h1>Sales Pipeline</h1>
<div class="pageintro">opportunity &middot; value &middot; client &middot; next action &middot; last activity &middot; deadline, grouped by stage.</div>
<div id="rhPipelineBoard"></div>
</div>
<div class="view" id="view-rhClients">
<h1>Clients</h1>
<div class="pageintro">Opportunities grouped by client.</div>
<div class="list" id="rhClientsList"></div>
</div>
<div class="view" id="view-rhActiveJobs">
<h1>Active Jobs</h1>
<div class="pageintro">Won opportunities that became Active Jobs. Handoff to Falguna Engineering is always owner-triggered, with a real target repository.</div>
<div class="list" id="rhActiveJobsList"></div>
</div>
<div class="view" id="view-rhRevenue">
<h1>Revenue</h1>
<div class="pageintro">Lean analytics on the acquisition funnel.</div>
<div class="list" id="rhAnalytics"></div>
</div>
</div>
</main>
</div>
<script>
const $=id=>document.getElementById(id);
async function api(url,options){const r=await fetch(url,options);const j=await r.json();if(!r.ok)throw Object.assign(new Error(j.error||'Request failed'),{data:j});return j}
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let FALGUNA_URL='http://127.0.0.1:8765';
const rhLoaders={rhToday:loadRhToday,rhOpportunities:loadRhOpportunities,rhPipeline:loadRhPipeline,rhClients:loadRhClients,rhActiveJobs:loadRhActiveJobs,rhRevenue:loadRhRevenue};
document.querySelectorAll('.navitem[data-view]').forEach(b=>b.onclick=()=>{document.querySelectorAll('.navitem[data-view]').forEach(x=>x.classList.remove('active'));document.querySelectorAll('.view').forEach(x=>x.classList.remove('active'));b.classList.add('active');$('view-'+b.dataset.view).classList.add('active');if(rhLoaders[b.dataset.view])rhLoaders[b.dataset.view]().catch(e=>{})});
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

// ---------- Revenue Hunter ----------
const RH_STAGES=["New","Qualified","Proposal Ready","Applied/Sent","Replied","Meeting","Negotiating","Won","Lost"];
const PROPOSAL_KINDS=["short","detailed","upwork","email_pitch","follow_up"];
const FOLLOWUP_KINDS=["proposal_followup","response_followup","negotiation_followup","payment_followup","repeat_business_followup"];
let rhOpenId=null;
if($('rhStageFilter').children.length<2)RH_STAGES.forEach(s=>{const o=document.createElement('option');o.value=s;o.textContent=s;$('rhStageFilter').appendChild(o)});
$('rhMode').onchange=()=>{const m=$('rhMode').value;$('rhModeManual').style.display=m==='manual'?'':'none';$('rhModePasteJd').style.display=m==='paste_jd'?'':'none';$('rhModeUrl').style.display=m==='url'?'':'none';$('rhModeCsv').style.display=m==='csv_json'?'':'none'};
$('rhStageFilter').onchange=()=>loadRhOpportunities();
$('rhCreate').onclick=async()=>{
const m=$('rhMode').value;let body;
if(m==='paste_jd'){if(!$('rhJdTitle').value.trim())return alert('Title is required.');if(!$('rhJdText').value.trim())return alert('Paste the job description text first.');body={import_mode:'paste_jd',title:$('rhJdTitle').value.trim(),client_name:$('rhJdClient').value.trim()||null,text:$('rhJdText').value}}
else if(m==='url'){if(!$('rhUrlTitle').value.trim()||!$('rhUrlValue').value.trim())return alert('Title and URL are required.');body={import_mode:'url',title:$('rhUrlTitle').value.trim(),url:$('rhUrlValue').value.trim(),description:$('rhUrlDescription').value||null}}
else if(m==='csv_json'){let rows;try{rows=JSON.parse($('rhCsvJson').value)}catch(e){return alert('That is not valid JSON.')}body={import_mode:'csv_json',rows}}
else{if(!$('rhTitle').value.trim())return alert('Title is required.');body={import_mode:'manual',title:$('rhTitle').value.trim(),client_name:$('rhClient').value.trim()||null,description:$('rhDescription').value||null,budget_rate:$('rhBudget').value||null,required_skills:$('rhSkills').value||null,deadline:$('rhDeadline').value||null,contract_type:$('rhContractType').value||null,location_timezone:$('rhLocation').value||null,urgency:$('rhUrgency').value||null}}
try{await api('/api/rh/opportunities',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});['rhTitle','rhClient','rhDescription','rhBudget','rhSkills','rhDeadline','rhContractType','rhLocation','rhUrgency','rhJdTitle','rhJdClient','rhJdText','rhUrlTitle','rhUrlValue','rhUrlDescription','rhCsvJson'].forEach(id=>{if($(id))$(id).value=''});await loadRhOpportunities()}catch(e){alert(e.message)}
};
async function loadRhToday(){const d=await api('/api/rh/dashboard');$('rhPipelineValue').textContent='$'+d.pipeline_value;$('rhWonRevenue').textContent='$'+d.won_revenue;$('rhNextActions').innerHTML=d.next_actions.length?d.next_actions.map(a=>`<div class="item"><h3>${esc(a.title||'')}</h3><div class="meta"><span>${esc(a.type)}</span></div><div>${esc(a.why||'')}</div></div>`).join(''):'<div class="empty">Nothing urgent right now.</div>'}
async function loadRhOpportunities(){const stage=$('rhStageFilter').value;const d=await api('/api/rh/opportunities'+(stage?`?stage=${encodeURIComponent(stage)}`:''));renderRhOpportunities(d.items||[])}
function rhCard(o){return `<div class="item"><h3>${esc(o.title)}</h3><div class="meta"><span>${esc(o.stage)}</span>${o.client_name?`<span>${esc(o.client_name)}</span>`:''}${o.budget_rate?`<span>${esc(o.budget_rate)}</span>`:''}${o.deadline?`<span>due ${esc(o.deadline)}</span>`:''}</div><div class="actions"><button class="secondary rhOpen" data-id="${esc(o.id)}">Open</button></div></div>`}
function renderRhOpportunities(items){$('rhOpportunityList').innerHTML=items.length?items.map(o=>rhOpenId===o.id?rhDetailCard(o):rhCard(o)).join(''):'<div class="empty">No opportunities yet.</div>';wireRhList()}
function wireRhList(){
document.querySelectorAll('.rhOpen').forEach(b=>b.onclick=async()=>{rhOpenId=b.dataset.id;await loadRhOpportunities()});
document.querySelectorAll('.rhClose').forEach(b=>b.onclick=async()=>{rhOpenId=null;await loadRhOpportunities()});
document.querySelectorAll('.rhQualify').forEach(b=>b.onclick=async()=>{try{await api(`/api/rh/opportunities/${b.dataset.id}/qualify`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});await loadRhOpportunities()}catch(e){alert(e.message)}});
document.querySelectorAll('.rhStageBtn').forEach(b=>b.onclick=async()=>{try{await api(`/api/rh/opportunities/${b.dataset.id}/stage`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({to_stage:b.dataset.stage})});await loadRhOpportunities()}catch(e){alert(e.message)}});
document.querySelectorAll('.rhWon').forEach(b=>b.onclick=async()=>{const price=prompt('Final price (optional):');try{await api(`/api/rh/opportunities/${b.dataset.id}/won`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({final_price:price?Number(price):null})});await loadRhOpportunities()}catch(e){alert(e.message)}});
document.querySelectorAll('.rhLost').forEach(b=>b.onclick=async()=>{const reason=prompt('Reason (optional):')||null;try{await api(`/api/rh/opportunities/${b.dataset.id}/lost`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({reason})});await loadRhOpportunities()}catch(e){alert(e.message)}});
document.querySelectorAll('.rhProposal').forEach(b=>b.onclick=async()=>{try{await api(`/api/rh/opportunities/${b.dataset.id}/proposals`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({kind:b.dataset.kind})});await loadRhOpportunities()}catch(e){alert(e.message)}});
document.querySelectorAll('.rhFollowup').forEach(b=>b.onclick=async()=>{try{await api(`/api/rh/opportunities/${b.dataset.id}/followups`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({kind:b.dataset.kind})});await loadRhOpportunities()}catch(e){alert(e.message)}});
document.querySelectorAll('.rhFollowupSent').forEach(b=>b.onclick=async()=>{try{await api(`/api/rh/followups/${b.dataset.id}/sent`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});await loadRhOpportunities()}catch(e){alert(e.message)}});
document.querySelectorAll('.rhActiveJob').forEach(b=>b.onclick=async()=>{try{await api(`/api/rh/opportunities/${b.dataset.id}/active-job`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});alert('Active Job created. Trigger the Falguna handoff from Active Jobs when ready.')}catch(e){alert(e.message)}});
}
function rhDetailCard(o){
const q=o.qualification;
return `<div class="item">
<h3>${esc(o.title)}</h3>
<div class="meta"><span>${esc(o.stage)}</span>${o.client_name?`<span>${esc(o.client_name)}</span>`:''}${o.budget_rate?`<span>${esc(o.budget_rate)}</span>`:''}${o.deadline?`<span>due ${esc(o.deadline)}</span>`:''}</div>
${o.description?`<div>${esc(o.description)}</div>`:''}
<div class="contrib"><b>Qualification:</b> ${q?`fit ${q.fit_score}/100, budget ${esc(q.budget_quality)}, <b>${esc(q.recommendation)}</b>, price ${esc(q.suggested_price)}, timeline ${esc(q.suggested_timeline)}${q.risk_flags?`, risks: ${esc(q.risk_flags)}`:''}`:'not qualified yet'}</div>
<div class="actions">
${!q?`<button class="rhQualify" data-id="${esc(o.id)}">Qualify</button>`:''}
${RH_STAGES.map(s=>`<button class="secondary rhStageBtn" data-id="${esc(o.id)}" data-stage="${s}">${s}</button>`).join('')}
${o.stage!=='Won'&&o.stage!=='Lost'?`<button class="rhWon" data-id="${esc(o.id)}">Won</button><button class="danger rhLost" data-id="${esc(o.id)}">Lost</button>`:''}
${o.stage==='Won'?`<button class="secondary rhActiveJob" data-id="${esc(o.id)}">Create Active Job</button>`:''}
<button class="secondary rhClose" data-id="${esc(o.id)}">Close</button>
</div>
<div class="contrib"><b>Proposals</b></div>
<div class="actions">${PROPOSAL_KINDS.map(k=>`<button class="secondary rhProposal" data-id="${esc(o.id)}" data-kind="${k}">Draft ${k}</button>`).join('')}</div>
${(o.proposals||[]).map(p=>`<div class="contrib"><b>${esc(p.kind)} (${esc(p.status)}):</b><br>${esc(p.content)}</div>`).join('')}
<div class="contrib"><b>Follow-ups</b></div>
<div class="actions">${FOLLOWUP_KINDS.map(k=>`<button class="secondary rhFollowup" data-id="${esc(o.id)}" data-kind="${k}">Draft ${k.replace('_followup','')}</button>`).join('')}</div>
${(o.followups||[]).map(f=>`<div class="contrib"><b>${esc(f.kind)} (${esc(f.status)}):</b> ${esc(f.draft_content)} ${f.status==='DRAFT'?`<button class="secondary rhFollowupSent" data-id="${esc(f.id)}">Mark sent</button>`:''}</div>`).join('')}
<div class="contrib"><b>Stage history</b></div>
${(o.stage_history||[]).map(h=>`<div class="contrib">${esc(h.from_stage||'-')} &rarr; <b>${esc(h.to_stage)}</b> by ${esc(h.actor)}${h.note?': '+esc(h.note):''}</div>`).join('')}
</div>`;
}
async function loadRhPipeline(){const d=await api('/api/rh/opportunities');const items=d.items||[];const byStage={};RH_STAGES.forEach(s=>byStage[s]=[]);items.forEach(o=>{(byStage[o.stage]||(byStage[o.stage]=[])).push(o)});
$('rhPipelineBoard').innerHTML=RH_STAGES.map(s=>`<div class="section"><h2>${s} (${(byStage[s]||[]).length})</h2><div class="list">${(byStage[s]||[]).length?(byStage[s]||[]).map(o=>`<div class="item"><h3>${esc(o.title)}</h3><div class="meta">${o.client_name?`<span>${esc(o.client_name)}</span>`:''}${o.budget_rate?`<span>${esc(o.budget_rate)}</span>`:''}${o.deadline?`<span>due ${esc(o.deadline)}</span>`:''}</div></div>`).join(''):'<div class="empty">Empty</div>'}</div></div>`).join('')}
async function loadRhClients(){const d=await api('/api/rh/opportunities');const items=d.items||[];const byClient={};items.forEach(o=>{const key=o.client_name||'(no client name)';(byClient[key]=byClient[key]||[]).push(o)});
const rows=Object.entries(byClient);
$('rhClientsList').innerHTML=rows.length?rows.map(([client,opps])=>{const won=opps.filter(o=>o.stage==='Won');const revenue=won.reduce((sum,o)=>sum+(o.final_price||0),0);return `<div class="item"><h3>${esc(client)}</h3><div class="meta"><span>${opps.length} opportunit${opps.length===1?'y':'ies'}</span><span>${won.length} won</span><span>$${revenue} revenue</span></div></div>`}).join(''):'<div class="empty">No clients yet.</div>'}
async function loadRhActiveJobs(){const d=await api('/api/rh/active-jobs');renderRhActiveJobs(d.items||[])}
function renderRhActiveJobs(items){$('rhActiveJobsList').innerHTML=items.length?items.map(j=>`<div class="item"><h3>Active Job ${esc(j.id)}</h3><div class="meta"><span>${esc(j.handoff_status)}</span>${j.mission_id?`<span>mission ${esc(j.mission_id)}</span>`:''}</div>${j.handoff_status!=='HANDED_OFF'?`<div class="form"><input class="rhRepoInput" data-id="${esc(j.id)}" placeholder="Local git repository path for the target job"><div class="actions"><button class="secondary rhHandoff" data-id="${esc(j.id)}">Trigger Falguna handoff</button></div></div>`:''}</div>`).join(''):'<div class="empty">No Active Jobs yet -- create one from a Won opportunity.</div>';
document.querySelectorAll('.rhHandoff').forEach(b=>b.onclick=async()=>{const repo=document.querySelector(`.rhRepoInput[data-id="${b.dataset.id}"]`).value.trim();if(!repo)return alert('Enter the target repository path first.');try{await api(`/api/rh/active-jobs/${b.dataset.id}/handoff`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({repository:repo})});await loadRhActiveJobs()}catch(e){alert(e.message)}})}
async function loadRhRevenue(){const a=await api('/api/rh/analytics');$('rhAnalytics').innerHTML=`<div class="item"><div class="meta">
<span>Added: ${a.opportunities_added}</span><span>Qualified: ${a.qualified}</span><span>Proposals: ${a.proposals_created}</span><span>Sent: ${a.proposals_sent}</span>
<span>Replies: ${a.replies}</span><span>Meetings: ${a.meetings}</span><span>Wins: ${a.wins}</span><span>Losses: ${a.losses}</span>
<span>Conversion: ${Math.round(a.conversion_rate*100)}%</span><span>Pipeline value: $${a.pipeline_value}</span><span>Won revenue: $${a.won_revenue}</span>
</div></div>`+Object.entries(a.source_performance||{}).map(([src,s])=>`<div class="item"><h3>${esc(src)}</h3><div class="meta"><span>added ${s.added}</span><span>won ${s.won}</span><span>lost ${s.lost}</span></div></div>`).join('')}

loadAll().catch(e=>{$('boardroomList').innerHTML=`<div class="empty">Unable to load: ${esc(e.message)}</div>`});
</script></body></html>'''
