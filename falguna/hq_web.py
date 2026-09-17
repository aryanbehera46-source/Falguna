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
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .account_management import AccountManagementError, AccountManagerService
from .analytics_growth import AnalyticsError, AnalyticsStore, GrowthAgent, GrowthExperimentStore
from .application_executor import ApplicationExecutor, ApplicationExecutorError
from .billing import BillingError, BillingStore, CompletionError, CompletionService, RetentionError, RetentionStore
from .command_center import CEOBriefStore, command_center_snapshot, kpi_snapshot
from .department_performance import ai_workforce_performance, department_performance
from .goals import GoalError, GoalStore
from .finance_ledger import LedgerError, LedgerStore, cash_and_runway, client_profitability
from .capital_and_risk import (
    BudgetStore, CapitalRiskError, ReservePolicyStore, RiskRegisterStore,
    allowed_experimental_capital, budget_status, recommend_allocation,
)
from .conversations import ConversationError, ConversationStore
from .documents import DocumentError, DocumentStore
from .email_admin import EmailError, EmailStore
from .lifecycle import LifecycleError, LifecycleOrchestrator
from .media import BrandStore, CampaignStore, ContentStore, MediaError, ScriptStore
from .media_agents import (
    AnalyticsIngestionAgent, ContentStrategistAgent, CreativeDirectorAgent, GrowthRecommendationAgent,
    PublishingAgent, ScriptWriterAgent, ThumbnailAgent, VideoEditAgent, VisualAssetAgent, VoiceAgent,
)
from .media_providers import MediaAssetStore, MediaProviderError
from .onboarding import DeliveryBriefService, OnboardingError, OnboardingStore
from .opportunity_agent import (
    AcquisitionProfileStore, DiscoveryEngine, DiscoveryRunStore, build_qualification_engine,
    get_research_for_opportunity, requalify_all,
)
from .outbound import OutboundLeadError, OutboundLeadStore, OutreachError, OutreachService
from .publishing import ManualPublishingChannel, PublicationStore, PublishingError
from .recurring_workflows import RecurringWorkflowError, RecurringWorkflowStore
from .revenue_hunter import (
    ActiveJobError, ActiveJobStore, AnalyticsService, DashboardService, FollowupError,
    FollowupStore, OpportunityError, OpportunityStore, ProposalError, ProposalStore,
    QualificationStore, apply_decision_side_effect, extract_fields_from_text, extract_from_csv_rows,
)
from .runtime import open_control_plane
from .sales_manager import SalesManagerService
from .sales_ops import ClientStore, ClosingError, ClosingService, NegotiationGuardrails, SalesPolicyStore
from .ttt_hq import BacklogStore, BoardroomStore, NeedsAryanQueue, hq_overview, workforce_media_today_signals
from .video_pipeline import VideoPipeline
from .workforce import WorkforceError, WorkforceOrchestrator, WorkforceTaskStore
from .workforce_workers import (
    BrowserWorker, ContentWorker, DataWorker, DocumentWorker, EmailAdminWorker, ResearchWorker,
    SpreadsheetWorker,
)

PRODUCT_NAME = "Twenty Two Technologies HQ"


def _build_workforce_orchestrator(app_root, store, audit, needs_aryan) -> WorkforceOrchestrator:
    """Wires one WorkforceOrchestrator with every registered worker --
    Digital Workforce (Pass A/B) and every Media agent (Pass C/D) -- all
    sharing the same task model, per Section 10's "do not make separate
    incompatible execution frameworks." Every honest default applies here
    exactly as it does in isolation: no browser/publish adapter, no
    research provider, so those task types BLOCK and escalate rather than
    fabricate a result -- this route wires nothing that pretends otherwise.
    """
    media_output_root = Path(app_root) / ".falguna" / "media_output"
    documents = DocumentStore(store, audit)
    emails = EmailStore(store, audit, needs_aryan=needs_aryan)
    content = ContentStore(store, audit)
    scripts = ScriptStore(store, audit)
    assets = MediaAssetStore(store, audit, output_root=media_output_root / "assets")
    pipeline = VideoPipeline(media_output_root / "video")
    publications = PublicationStore(store, audit, needs_aryan=needs_aryan)
    analytics = AnalyticsStore(store, audit)
    growth = GrowthAgent(analytics)

    orch = WorkforceOrchestrator(store, audit, needs_aryan=needs_aryan)
    for worker in [
        BrowserWorker(), ResearchWorker(), DataWorker(), DocumentWorker(documents), SpreadsheetWorker(documents),
        EmailAdminWorker(emails), ContentWorker(documents),
        ContentStrategistAgent(content), ScriptWriterAgent(content, scripts), CreativeDirectorAgent(content, scripts),
        VisualAssetAgent(content, assets), VoiceAgent(content, assets), VideoEditAgent(content, assets, pipeline),
        ThumbnailAgent(content, assets), PublishingAgent(content, publications),
        AnalyticsIngestionAgent(content, publications, analytics), GrowthRecommendationAgent(content, publications, growth),
    ]:
        orch.register_worker(worker)
    return orch


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
            if path == "/api/cc/snapshot":
                return self._json(command_center_snapshot(store))
            if path == "/api/cc/ceo-brief/latest":
                brief = CEOBriefStore(store, control.audit).latest()
                return self._json(brief or {"error": "no brief generated yet"}, HTTPStatus.OK if brief else HTTPStatus.NOT_FOUND)
            if path == "/api/cc/ceo-brief":
                return self._json({"items": CEOBriefStore(store, control.audit).list()})
            if path == "/api/cc/kpis":
                return self._json(kpi_snapshot(store))
            if path == "/api/cc/goals":
                query = parse_qs(urlparse(self.path).query)
                status = (query.get("status") or [None])[0]
                department = (query.get("department") or [None])[0]
                return self._json({"items": GoalStore(store, control.audit).list(status, department)})
            if path.startswith("/api/cc/goals/") and path.endswith("/recommended-actions"):
                goal_id = path.split("/")[4]
                return self._json({"actions": GoalStore(store, control.audit).recommended_actions(goal_id)})
            if path.startswith("/api/cc/goals/"):
                goal_id = path.rsplit("/", 1)[-1]
                goal = GoalStore(store, control.audit).get(goal_id)
                return self._json(goal or {"error": "goal not found"}, HTTPStatus.OK if goal else HTTPStatus.NOT_FOUND)
            if path == "/api/cc/ledger":
                query = parse_qs(urlparse(self.path).query)
                entry_type = (query.get("entry_type") or [None])[0]
                category = (query.get("category") or [None])[0]
                client_id = (query.get("client_id") or [None])[0]
                status_filter = (query.get("status") or ["RECORDED"])[0]
                return self._json({"items": LedgerStore(store, control.audit).list(entry_type, category, client_id=client_id, status=status_filter)})
            if path == "/api/cc/cash-runway":
                return self._json(cash_and_runway(store))
            if path.startswith("/api/cc/clients/") and path.endswith("/profitability"):
                client_id = path.split("/")[4]
                return self._json(client_profitability(store, client_id))
            if path == "/api/cc/budgets":
                query = parse_qs(urlparse(self.path).query)
                status_filter = (query.get("status") or ["ACTIVE"])[0]
                return self._json({"items": BudgetStore(store, control.audit).list(status_filter)})
            if path.startswith("/api/cc/budgets/") and path.endswith("/status"):
                budget_id = path.split("/")[4]
                budget = BudgetStore(store, control.audit).get(budget_id)
                if not budget:
                    return self._json({"error": "budget not found"}, HTTPStatus.NOT_FOUND)
                needs_aryan = NeedsAryanQueue(store, control.audit, control)
                return self._json(budget_status(store, budget, needs_aryan=needs_aryan))
            if path == "/api/cc/reserve-policy":
                return self._json(ReservePolicyStore(store).get())
            if path == "/api/cc/experimental-capital":
                return self._json(allowed_experimental_capital(store))
            if path == "/api/cc/risks":
                query = parse_qs(urlparse(self.path).query)
                status_filter = (query.get("status") or [None])[0]
                category_filter = (query.get("category") or [None])[0]
                return self._json({"items": RiskRegisterStore(store, control.audit).list(status_filter, category_filter)})
            if path == "/api/cc/department-performance":
                return self._json(department_performance(store))
            if path == "/api/cc/ai-workforce-performance":
                return self._json(ai_workforce_performance(store))
            if path == "/api/rh/opportunities":
                query = parse_qs(urlparse(self.path).query)
                stage = (query.get("stage") or [None])[0]
                items = OpportunityStore(store, control.audit).list(stage)
                recommendation = (query.get("recommendation") or [None])[0]
                if recommendation:
                    items = [o for o in items if (o.get("qualification") or {}).get("recommendation") == recommendation]
                source = (query.get("source") or [None])[0]
                if source:
                    items = [o for o in items if o.get("source") == source]
                return self._json({"items": items})
            if path.startswith("/api/rh/opportunities/") and path.endswith("/lifecycle"):
                # Checked ahead of the bare opportunity-fetch route below (same
                # ordering trick that route's own suffix checks already use),
                # since that route's naive rsplit would otherwise treat
                # "lifecycle" itself as the opportunity id.
                opportunity_id = path.split("/")[4]
                if not store.get("rh_opportunities", opportunity_id):
                    return self._json({"error": "opportunity not found"}, HTTPStatus.NOT_FOUND)
                orchestrator = LifecycleOrchestrator(store, control.audit)
                return self._json({
                    "opportunity_id": opportunity_id, "current_state": orchestrator.current_state(opportunity_id),
                    "history": orchestrator.history(opportunity_id),
                })
            if path.startswith("/api/rh/opportunities/") and path.endswith("/conversations"):
                opportunity_id = path.split("/")[4]
                return self._json({"items": ConversationStore(store, control.audit).list_for_opportunity(opportunity_id)})
            if path.startswith("/api/rh/opportunities/") and path.endswith("/negotiations"):
                opportunity_id = path.split("/")[4]
                return self._json({"items": NegotiationGuardrails(store, control.audit).history(opportunity_id)})
            if path.startswith("/api/rh/opportunities/") and path.endswith("/onboarding"):
                opportunity_id = path.split("/")[4]
                return self._json({"items": OnboardingStore(store, control.audit).list_for_opportunity(opportunity_id)})
            if path.startswith("/api/rh/opportunities/") and path.endswith("/delivery-brief"):
                opportunity_id = path.split("/")[4]
                return self._json(DeliveryBriefService(store, control.audit).build(opportunity_id))
            if path.startswith("/api/rh/opportunities/") and path.endswith("/completion"):
                opportunity_id = path.split("/")[4]
                record = CompletionService(store, control.audit).get_for_opportunity(opportunity_id)
                return self._json(record or {"error": "no completion record yet"}, HTTPStatus.OK if record else HTTPStatus.NOT_FOUND)
            if path.startswith("/api/rh/opportunities/") and path.endswith("/scope-signals"):
                opportunity_id = path.split("/")[4]
                return self._json({"items": AccountManagerService(store, control.audit).scope_signals(opportunity_id)})
            if path.startswith("/api/rh/opportunities/"):
                opportunity_id = path.rsplit("/", 1)[-1]
                opportunity = OpportunityStore(store, control.audit).get(opportunity_id)
                if opportunity:
                    opportunity["research"] = get_research_for_opportunity(store, opportunity_id)
                return self._json(opportunity or {"error": "opportunity not found"}, HTTPStatus.OK if opportunity else HTTPStatus.NOT_FOUND)
            if path == "/api/rh/active-jobs":
                return self._json({"items": ActiveJobStore(store, control.audit).list()})
            if path.startswith("/api/rh/active-jobs/") and path.endswith("/account-status"):
                active_job_id = path.split("/")[4]
                return self._json(AccountManagerService(store, control.audit).status_for_active_job(active_job_id))
            if path.startswith("/api/rh/active-jobs/") and path.endswith("/client-update-draft"):
                active_job_id = path.split("/")[4]
                return self._json({"draft": AccountManagerService(store, control.audit).client_update_draft(active_job_id)})
            if path == "/api/rh/portfolio-overview":
                return self._json({"items": AccountManagerService(store, control.audit).portfolio_overview()})
            if path == "/api/rh/sales-manager/overview":
                return self._json(SalesManagerService(store, control.audit, control).overview())
            if path == "/api/rh/outbound-leads":
                query = parse_qs(urlparse(self.path).query)
                status = (query.get("status") or [None])[0]
                return self._json({"items": OutboundLeadStore(store, control.audit).list(status)})
            if path.startswith("/api/rh/outbound-leads/") and path.endswith("/outreach"):
                lead_id = path.split("/")[4]
                return self._json({"items": OutreachService(store, control.audit).list_for_lead(lead_id)})
            if path.startswith("/api/rh/outbound-leads/"):
                lead_id = path.rsplit("/", 1)[-1]
                lead = OutboundLeadStore(store, control.audit).get(lead_id)
                return self._json(lead or {"error": "lead not found"}, HTTPStatus.OK if lead else HTTPStatus.NOT_FOUND)
            if path == "/api/rh/dashboard":
                pending = NeedsAryanQueue(store, control.audit, control).list_pending()
                discovery_runs = DiscoveryRunStore(store).list(limit=5)
                today = DashboardService(store).today(pending, discovery_runs)
                today.update(workforce_media_today_signals(store))
                return self._json(today)
            if path == "/api/rh/analytics":
                return self._json(AnalyticsService(store).summary())
            if path == "/api/rh/acquisition-profile":
                return self._json(AcquisitionProfileStore(store).get())
            if path == "/api/rh/discovery-runs":
                return self._json({"items": DiscoveryRunStore(store).list(limit=20)})
            if path.startswith("/api/rh/discovery-runs/"):
                run_id = path.rsplit("/", 1)[-1]
                run = DiscoveryRunStore(store).get(run_id)
                return self._json(run or {"error": "discovery run not found"}, HTTPStatus.OK if run else HTTPStatus.NOT_FOUND)
            if path == "/api/rh/sales-policy":
                return self._json(SalesPolicyStore(store).get())
            if path == "/api/rh/clients":
                return self._json({"items": ClientStore(store, control.audit).list()})
            if path == "/api/rh/invoices":
                return self._json({"items": BillingStore(store, control.audit).list()})
            if path.startswith("/api/rh/clients/") and path.endswith("/invoices"):
                client_id = path.split("/")[4]
                return self._json({"items": BillingStore(store, control.audit).list_for_client(client_id)})
            if path.startswith("/api/rh/clients/") and path.endswith("/retention"):
                client_id = path.split("/")[4]
                return self._json({"items": RetentionStore(store, control.audit).list_for_client(client_id)})
            if path == "/api/rh/retention/due":
                return self._json({"items": RetentionStore(store, control.audit).list_due()})
            if path.startswith("/api/rh/clients/"):
                client_id = path.rsplit("/", 1)[-1]
                client = ClientStore(store, control.audit).get(client_id)
                return self._json(client or {"error": "client not found"}, HTTPStatus.OK if client else HTTPStatus.NOT_FOUND)

            # -- Digital Workforce (Section 20) --
            if path == "/api/wf/tasks":
                query = parse_qs(urlparse(self.path).query)
                department = (query.get("department") or [None])[0]
                status = (query.get("status") or [None])[0]
                return self._json({"items": WorkforceTaskStore(store, control.audit).list(department, status)})
            if path.startswith("/api/wf/tasks/") and path.endswith("/history"):
                task_id = path.split("/")[4]
                return self._json({"items": WorkforceTaskStore(store, control.audit).history(task_id)})
            if path.startswith("/api/wf/tasks/"):
                task_id = path.rsplit("/", 1)[-1]
                task = WorkforceTaskStore(store, control.audit).get(task_id)
                return self._json(task or {"error": "task not found"}, HTTPStatus.OK if task else HTTPStatus.NOT_FOUND)
            if path == "/api/wf/recurring-workflows":
                return self._json({"items": RecurringWorkflowStore(store, control.audit).list()})
            if path.startswith("/api/wf/recurring-workflows/"):
                workflow_id = path.rsplit("/", 1)[-1]
                workflow = RecurringWorkflowStore(store, control.audit).get(workflow_id)
                return self._json(workflow or {"error": "workflow not found"}, HTTPStatus.OK if workflow else HTTPStatus.NOT_FOUND)
            if path == "/api/wf/documents":
                query = parse_qs(urlparse(self.path).query)
                department = (query.get("department") or [None])[0]
                doc_type = (query.get("doc_type") or [None])[0]
                return self._json({"items": DocumentStore(store, control.audit).list(department, doc_type)})
            if path.startswith("/api/wf/documents/"):
                doc_id = path.rsplit("/", 1)[-1]
                doc = DocumentStore(store, control.audit).get(doc_id)
                return self._json(doc or {"error": "document not found"}, HTTPStatus.OK if doc else HTTPStatus.NOT_FOUND)
            if path == "/api/wf/emails":
                query = parse_qs(urlparse(self.path).query)
                department = (query.get("department") or [None])[0]
                status = (query.get("status") or [None])[0]
                return self._json({"items": EmailStore(store, control.audit).list(department, status)})

            # -- Media/Growth Engine (Section 20) --
            if path == "/api/media/brands":
                return self._json({"items": BrandStore(store, control.audit).list()})
            if path.startswith("/api/media/brands/"):
                brand_id = path.rsplit("/", 1)[-1]
                brand = BrandStore(store, control.audit).get(brand_id)
                return self._json(brand or {"error": "brand not found"}, HTTPStatus.OK if brand else HTTPStatus.NOT_FOUND)
            if path == "/api/media/campaigns":
                query = parse_qs(urlparse(self.path).query)
                brand_id = (query.get("brand_id") or [None])[0]
                return self._json({"items": CampaignStore(store, control.audit).list(brand_id)})
            if path == "/api/media/content":
                query = parse_qs(urlparse(self.path).query)
                brand_id = (query.get("brand_id") or [None])[0]
                campaign_id = (query.get("campaign_id") or [None])[0]
                content_state = (query.get("content_state") or [None])[0]
                return self._json({"items": ContentStore(store, control.audit).list(brand_id, campaign_id, content_state)})
            if path.startswith("/api/media/content/") and path.endswith("/history"):
                content_id = path.split("/")[4]
                return self._json({"items": ContentStore(store, control.audit).history(content_id)})
            if path.startswith("/api/media/content/") and path.endswith("/scripts"):
                content_id = path.split("/")[4]
                return self._json({"items": ScriptStore(store, control.audit).list_versions(content_id)})
            if path.startswith("/api/media/content/") and path.endswith("/assets"):
                content_id = path.split("/")[4]
                return self._json({"items": MediaAssetStore(store, control.audit).list(content_id)})
            if path.startswith("/api/media/content/"):
                content_id = path.rsplit("/", 1)[-1]
                content_item = ContentStore(store, control.audit).get(content_id)
                return self._json(content_item or {"error": "content item not found"}, HTTPStatus.OK if content_item else HTTPStatus.NOT_FOUND)
            if path == "/api/media/publications":
                query = parse_qs(urlparse(self.path).query)
                content_id = (query.get("content_id") or [None])[0]
                status = (query.get("status") or [None])[0]
                return self._json({"items": PublicationStore(store, control.audit).list(content_id, status)})
            if path.startswith("/api/media/publications/") and path.endswith("/analytics"):
                publication_id = path.split("/")[4]
                return self._json({"items": AnalyticsStore(store, control.audit).list(publication_id)})
            if path.startswith("/api/media/publications/") and path.endswith("/growth-recommendation"):
                publication_id = path.split("/")[4]
                return self._json(GrowthAgent(AnalyticsStore(store, control.audit)).recommend(publication_id))
            if path == "/api/media/experiments":
                query = parse_qs(urlparse(self.path).query)
                content_id = (query.get("content_id") or [None])[0]
                status = (query.get("status") or [None])[0]
                return self._json({"items": GrowthExperimentStore(store, control.audit).list(content_id, status)})
        finally:
            store.close()
        return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self._body()
            control, store = open_control_plane(self.app_root)
            try:
                orchestrator = LifecycleOrchestrator(store, control.audit)
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
                if path == "/api/cc/ceo-brief/generate":
                    brief = CEOBriefStore(store, control.audit).generate(actor=body.get("actor", "Aryan"), period_start=body.get("period_start"))
                    return self._json(brief, HTTPStatus.CREATED)
                if path == "/api/cc/goals":
                    goal_id = GoalStore(store, control.audit).create(
                        body.get("title", ""), body.get("target"), body.get("unit", ""),
                        actor=body.get("actor", "Aryan"), start_date=body.get("start_date"), deadline=body.get("deadline"),
                        owner=body.get("owner"), department=body.get("department"),
                        linked_kpis=body.get("linked_kpis"), linked_actions=body.get("linked_actions"),
                    )
                    return self._json({"goal_id": goal_id}, HTTPStatus.CREATED)
                if path.startswith("/api/cc/goals/") and path.endswith("/progress"):
                    goal_id = path.split("/")[4]
                    goal = GoalStore(store, control.audit).update_progress(goal_id, body.get("value"), body.get("actor", "Aryan"), note=body.get("note"))
                    return self._json(goal)
                if path.startswith("/api/cc/goals/") and path.endswith("/status"):
                    goal_id = path.split("/")[4]
                    goal = GoalStore(store, control.audit).set_status(goal_id, body.get("status", ""), body.get("actor", "Aryan"), reason=body.get("reason"))
                    return self._json(goal)
                if path == "/api/cc/ledger":
                    entry_id = LedgerStore(store, control.audit).record(
                        body.get("entry_type", ""), body.get("category", ""), body.get("amount"),
                        body.get("evidence"), actor=body.get("actor", "Aryan"), currency=body.get("currency", "INR"),
                        business_unit=body.get("business_unit"), client_id=body.get("client_id"),
                        project_ref=body.get("project_ref"), occurred_on=body.get("occurred_on"), note=body.get("note"),
                    )
                    return self._json({"entry_id": entry_id}, HTTPStatus.CREATED)
                if path.startswith("/api/cc/ledger/") and path.endswith("/void"):
                    entry_id = path.split("/")[4]
                    entry = LedgerStore(store, control.audit).void(entry_id, body.get("actor", "Aryan"), body.get("reason", ""))
                    return self._json(entry)
                if path == "/api/cc/budgets":
                    budget_id = BudgetStore(store, control.audit).create(
                        body.get("department", ""), body.get("monthly_budget"), actor=body.get("actor", "Aryan"),
                        currency=body.get("currency", "INR"), limit_kind=body.get("limit_kind", "SOFT"),
                        warning_threshold_pct=body.get("warning_threshold_pct", 0.8),
                    )
                    return self._json({"budget_id": budget_id}, HTTPStatus.CREATED)
                if path == "/api/cc/capital-allocation/recommend":
                    result = recommend_allocation(store, body.get("available_amount"), actor=body.get("actor", "Aryan"))
                    return self._json(result, HTTPStatus.CREATED)
                if path == "/api/cc/reserve-policy":
                    policy = ReservePolicyStore(store).save(body)
                    return self._json(policy)
                if path == "/api/cc/risks":
                    needs_aryan = NeedsAryanQueue(store, control.audit, control)
                    risk_id = RiskRegisterStore(store, control.audit, needs_aryan=needs_aryan).create(
                        body.get("title", ""), body.get("category", ""), body.get("severity", ""),
                        actor=body.get("actor", "Aryan"), likelihood_band=body.get("likelihood_band", "unknown"),
                        owner=body.get("owner"), mitigation=body.get("mitigation"), evidence=body.get("evidence"),
                    )
                    return self._json({"risk_id": risk_id}, HTTPStatus.CREATED)
                if path.startswith("/api/cc/risks/") and path.endswith("/status"):
                    risk_id = path.split("/")[4]
                    risk = RiskRegisterStore(store, control.audit).update_status(risk_id, body.get("status", ""), body.get("actor", "Aryan"), note=body.get("note"))
                    return self._json(risk)
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
                        apply_decision_side_effect(store, control.audit, dict(before), result["action"], body.get("actor", "Aryan"), orchestrator=orchestrator)
                        if before.get("ref_type") == "rh_closing_package" and result["action"] == "APPROVED":
                            # Commercial safety default: an unconfigured Sales
                            # Policy prepared this as a package instead of
                            # closing immediately -- approval is the one
                            # moment the real close actually executes.
                            ClosingService(store, control.audit, orchestrator=orchestrator, needs_aryan=queue).finalize_pending_closing(item_id, body.get("actor", "Aryan"))
                    return self._json(result)

                if path == "/api/rh/opportunities":
                    opportunities = OpportunityStore(store, control.audit, orchestrator=orchestrator)
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
                    profile = AcquisitionProfileStore(store).get()
                    result = QualificationStore(store, control.audit, build_qualification_engine(profile), orchestrator=orchestrator).qualify(opportunity_id, actor=body.get("actor", "Aryan"))
                    return self._json(result, HTTPStatus.CREATED)

                if path == "/api/rh/requalify":
                    needs_aryan = NeedsAryanQueue(store, control.audit, control)
                    result = requalify_all(store, control.audit, needs_aryan, actor=body.get("actor", "Aryan"), orchestrator=orchestrator)
                    return self._json(result, HTTPStatus.CREATED)

                if path.startswith("/api/rh/opportunities/") and path.endswith("/stage"):
                    opportunity_id = path.split("/")[4]
                    opportunity = OpportunityStore(store, control.audit).move_stage(opportunity_id, body.get("to_stage", ""), body.get("actor", "Aryan"), note=body.get("note"))
                    return self._json(opportunity)

                if path.startswith("/api/rh/opportunities/") and path.endswith("/proposals"):
                    opportunity_id = path.split("/")[4]
                    needs_aryan = NeedsAryanQueue(store, control.audit, control)
                    result = ProposalStore(store, control.audit, needs_aryan, orchestrator=orchestrator).generate(opportunity_id, body.get("kind", ""), actor=body.get("actor", "Aryan"))
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
                    actor = body.get("actor", "Aryan")
                    opportunity = OpportunityStore(store, control.audit).mark_won(opportunity_id, actor, final_price=body.get("final_price"))
                    orchestrator.try_transition(opportunity_id, "WON", actor, reason="marked Won directly", sync_stage=False)
                    return self._json(opportunity)

                if path.startswith("/api/rh/opportunities/") and path.endswith("/lost"):
                    opportunity_id = path.split("/")[4]
                    actor = body.get("actor", "Aryan")
                    opportunity = OpportunityStore(store, control.audit).mark_lost(opportunity_id, actor, reason=body.get("reason"))
                    orchestrator.try_transition(opportunity_id, "LOST", actor, reason=body.get("reason") or "marked Lost directly", sync_stage=False)
                    return self._json(opportunity)

                if path.startswith("/api/rh/opportunities/") and path.endswith("/active-job"):
                    opportunity_id = path.split("/")[4]
                    job_id = ActiveJobStore(store, control.audit).create_from_won_opportunity(opportunity_id, actor=body.get("actor", "Aryan"))
                    return self._json({"active_job_id": job_id}, HTTPStatus.CREATED)

                if path.startswith("/api/rh/opportunities/") and len(path.split("/")) == 5:
                    # Bare edit route -- distinct from the action-suffixed routes above
                    # (.../qualify, .../stage, .../won, ...), which all have 6 path
                    # segments and are matched first, so they always take priority.
                    opportunity_id = path.split("/")[4]
                    actor = body.pop("actor", "Aryan")
                    opportunity = OpportunityStore(store, control.audit).update(opportunity_id, actor, **body)
                    return self._json(opportunity)

                if path == "/api/rh/discover":
                    # AUTO-FIND -> AUTO-ANALYZE -> AUTO-DRAFT, in one owner-triggered
                    # call. Never sends, applies, or emails anything -- see
                    # opportunity_agent.DiscoveryEngine's own docstring.
                    needs_aryan = NeedsAryanQueue(store, control.audit, control)
                    engine = DiscoveryEngine(store, control.audit, needs_aryan, orchestrator=orchestrator)
                    result = engine.run_now(
                        actor=body.get("actor", "Aryan"),
                        limit_per_source=int(body.get("limit_per_source", 25)),
                        research=bool(body.get("research", True)),
                    )
                    return self._json(result, HTTPStatus.CREATED)

                if path == "/api/rh/acquisition-profile":
                    profile = AcquisitionProfileStore(store).save(body)
                    return self._json(profile)

                if path.startswith("/api/rh/opportunities/") and path.endswith("/lifecycle/transition"):
                    opportunity_id = path.split("/")[4]
                    # Explicit route -> raises on an illegal transition
                    # (LifecycleError), unlike the best-effort hooks wired
                    # into qualify()/generate()/etc: a caller hitting this
                    # route directly needs to know whether it actually
                    # landed, per lifecycle.py's own try_transition docstring.
                    event = orchestrator.transition(
                        opportunity_id, body.get("to_state", ""), body.get("actor", "Aryan"),
                        reason=body.get("reason"), evidence=body.get("evidence"), next_action=body.get("next_action"),
                        approval_required=bool(body.get("approval_required", False)), approval_status=body.get("approval_status"),
                        money_impact=body.get("money_impact"),
                    )
                    return self._json(event, HTTPStatus.CREATED)

                if path.startswith("/api/rh/opportunities/") and path.endswith("/apply"):
                    opportunity_id = path.split("/")[4]
                    needs_aryan = NeedsAryanQueue(store, control.audit, control)
                    executor = ApplicationExecutor(store, control.audit, needs_aryan=needs_aryan, orchestrator=orchestrator)
                    result = executor.apply(
                        opportunity_id, body.get("proposal_id", ""), actor=body.get("actor", "Aryan"),
                        channel=body.get("channel", "manual_review"), allow_simulated=bool(body.get("allow_simulated", False)),
                    )
                    return self._json(result, HTTPStatus.CREATED)

                if path == "/api/rh/sales-policy":
                    policy = SalesPolicyStore(store).save(body)
                    return self._json(policy)

                if path.startswith("/api/rh/opportunities/") and path.endswith("/negotiation/evaluate"):
                    opportunity_id = path.split("/")[4]
                    needs_aryan = NeedsAryanQueue(store, control.audit, control)
                    terms = {k: body.get(k) for k in (
                        "price", "currency", "discount_pct", "upfront_payment_pct",
                        "payment_terms", "free_revisions", "timeline_days",
                    ) if k in body}
                    result = NegotiationGuardrails(store, control.audit, needs_aryan=needs_aryan).evaluate(
                        opportunity_id, body.get("actor", "Aryan"), **terms,
                    )
                    return self._json(result, HTTPStatus.CREATED)

                if path.startswith("/api/rh/opportunities/") and path.endswith("/close"):
                    opportunity_id = path.split("/")[4]
                    needs_aryan = NeedsAryanQueue(store, control.audit, control)
                    result = ClosingService(store, control.audit, orchestrator=orchestrator, needs_aryan=needs_aryan).close(
                        opportunity_id, body.get("actor", "Aryan"), client_name=body.get("client_name", ""),
                        final_scope=body.get("final_scope"), final_price=body.get("final_price"), currency=body.get("currency"),
                        payment_terms=body.get("payment_terms"), milestones=body.get("milestones"), deadline=body.get("deadline"),
                        deliverables=body.get("deliverables"), acceptance_criteria=body.get("acceptance_criteria"),
                        communication_channel=body.get("communication_channel"),
                    )
                    return self._json(result, HTTPStatus.CREATED)

                if path.startswith("/api/rh/opportunities/") and path.endswith("/conversations"):
                    opportunity_id = path.split("/")[4]
                    needs_aryan = NeedsAryanQueue(store, control.audit, control)
                    conversations = ConversationStore(store, control.audit, orchestrator=orchestrator, needs_aryan=needs_aryan)
                    result = conversations.record_inbound(
                        opportunity_id, body.get("channel", ""), body.get("body", ""), actor=body.get("actor", "system"),
                        sender=body.get("sender"), external_thread_id=body.get("external_thread_id"), evidence=body.get("evidence"),
                    )
                    return self._json(result, HTTPStatus.CREATED)

                if path.startswith("/api/rh/conversations/") and path.endswith("/draft-reply"):
                    message_id = path.split("/")[4]
                    result = ConversationStore(store, control.audit, orchestrator=orchestrator).draft_reply(message_id, actor=body.get("actor", "system"))
                    return self._json(result or {"drafted": False}, HTTPStatus.CREATED)

                if path.startswith("/api/rh/conversations/") and path.endswith("/sent"):
                    message_id = path.split("/")[4]
                    result = ConversationStore(store, control.audit, orchestrator=orchestrator).mark_sent(message_id, body.get("actor", "Aryan"))
                    return self._json(result)

                if path.startswith("/api/rh/opportunities/") and path.endswith("/onboarding/init"):
                    opportunity_id = path.split("/")[4]
                    items = OnboardingStore(store, control.audit).init_checklist(opportunity_id, actor=body.get("actor", "Aryan"))
                    return self._json({"items": items}, HTTPStatus.CREATED)

                if path.startswith("/api/rh/opportunities/") and path.endswith("/onboarding"):
                    opportunity_id = path.split("/")[4]
                    result = OnboardingStore(store, control.audit).set_item(
                        opportunity_id, body.get("item_type", ""), body.get("status", ""),
                        actor=body.get("actor", "Aryan"), value_text=body.get("value_text"), notes=body.get("notes"),
                    )
                    return self._json(result, HTTPStatus.CREATED)

                if path.startswith("/api/rh/opportunities/") and path.endswith("/completion"):
                    opportunity_id = path.split("/")[4]
                    record_id = CompletionService(store, control.audit).record_completion(
                        opportunity_id, body.get("actor", "Aryan"), body.get("evidence"),
                        active_job_id=body.get("active_job_id"), checklist=body.get("checklist"),
                    )
                    return self._json({"record_id": record_id}, HTTPStatus.CREATED)

                if path.startswith("/api/rh/clients/") and path.endswith("/invoices"):
                    client_id = path.split("/")[4]
                    invoice_id = BillingStore(store, control.audit).create_invoice(
                        client_id, body.get("actor", "Aryan"), body.get("amount"), currency=body.get("currency", "USD"),
                        opportunity_id=body.get("opportunity_id"), active_job_id=body.get("active_job_id"),
                        milestone=body.get("milestone"), due_date=body.get("due_date"),
                    )
                    return self._json({"invoice_id": invoice_id}, HTTPStatus.CREATED)

                if path.startswith("/api/rh/invoices/") and path.endswith("/ready"):
                    invoice_id = path.split("/")[4]
                    result = BillingStore(store, control.audit).mark_ready(invoice_id, body.get("actor", "Aryan"))
                    return self._json(result)

                if path.startswith("/api/rh/invoices/") and path.endswith("/sent"):
                    invoice_id = path.split("/")[4]
                    result = BillingStore(store, control.audit).mark_sent(invoice_id, body.get("actor", "Aryan"))
                    return self._json(result)

                if path.startswith("/api/rh/invoices/") and path.endswith("/payment"):
                    invoice_id = path.split("/")[4]
                    result = BillingStore(store, control.audit).record_payment(
                        invoice_id, body.get("amount"), body.get("actor", "Aryan"), body.get("evidence"),
                    )
                    return self._json(result, HTTPStatus.CREATED)

                if path.startswith("/api/rh/invoices/") and path.endswith("/overdue-check"):
                    invoice_id = path.split("/")[4]
                    result = BillingStore(store, control.audit).check_overdue(invoice_id)
                    return self._json(result)

                if path.startswith("/api/rh/invoices/") and path.endswith("/cancel"):
                    invoice_id = path.split("/")[4]
                    result = BillingStore(store, control.audit).cancel(invoice_id, body.get("actor", "Aryan"), reason=body.get("reason"))
                    return self._json(result)

                if path.startswith("/api/rh/clients/") and path.endswith("/retention"):
                    client_id = path.split("/")[4]
                    item_id = RetentionStore(store, control.audit).create_item(
                        client_id, body.get("kind", ""), body.get("actor", "Aryan"),
                        opportunity_id=body.get("opportunity_id"), notes=body.get("notes"), follow_up_date=body.get("follow_up_date"),
                    )
                    return self._json({"item_id": item_id}, HTTPStatus.CREATED)

                if path.startswith("/api/rh/retention/") and len(path.split("/")) == 5:
                    item_id = path.split("/")[4]
                    result = RetentionStore(store, control.audit).update_item(
                        item_id, body.get("actor", "Aryan"), status=body.get("status"),
                        notes=body.get("notes"), follow_up_date=body.get("follow_up_date"),
                    )
                    return self._json(result)

                if path == "/api/rh/outbound-leads":
                    lead_id = OutboundLeadStore(store, control.audit).create_lead(
                        body.get("company_name", ""), body.get("actor", "Aryan"), website=body.get("website"),
                        contact_name=body.get("contact_name"), contact_channel=body.get("contact_channel"),
                        likely_need=body.get("likely_need"), proposed_offer=body.get("proposed_offer"),
                        confidence=body.get("confidence"), relevance_notes=body.get("relevance_notes"), source=body.get("source"),
                    )
                    return self._json({"lead_id": lead_id}, HTTPStatus.CREATED)

                if path.startswith("/api/rh/outbound-leads/") and path.endswith("/status"):
                    lead_id = path.split("/")[4]
                    result = OutboundLeadStore(store, control.audit).update_status(lead_id, body.get("status", ""), body.get("actor", "Aryan"))
                    return self._json(result)

                if path.startswith("/api/rh/outbound-leads/") and path.endswith("/outreach"):
                    lead_id = path.split("/")[4]
                    needs_aryan = NeedsAryanQueue(store, control.audit, control)
                    result = OutreachService(store, control.audit, needs_aryan=needs_aryan).draft_message(
                        lead_id, body.get("channel", ""), body.get("message", ""), actor=body.get("actor", "Aryan"),
                    )
                    return self._json(result, HTTPStatus.CREATED)

                if path.startswith("/api/rh/outreach/") and path.endswith("/sent"):
                    draft_id = path.split("/")[4]
                    result = OutreachService(store, control.audit).mark_sent(draft_id, body.get("actor", "Aryan"))
                    return self._json(result)

                if path.startswith("/api/rh/active-jobs/") and path.endswith("/enrich"):
                    active_job_id = path.split("/")[4]
                    result = DeliveryBriefService(store, control.audit).enrich_active_job_payload(active_job_id, actor=body.get("actor", "system"))
                    return self._json(result, HTTPStatus.CREATED)

                if path.startswith("/api/rh/active-jobs/") and path.endswith("/handoff"):
                    active_job_id = path.split("/")[4]
                    # Best-effort: automatically fold in whatever onboarding
                    # context exists so far before the real handoff, exactly
                    # like every other observability hook in this codebase --
                    # onboarding data being incomplete or absent must never
                    # block a real, owner-triggered handoff.
                    try:
                        DeliveryBriefService(store, control.audit).enrich_active_job_payload(active_job_id, actor=body.get("actor", "Aryan"))
                    except OnboardingError:
                        pass
                    result = ActiveJobStore(store, control.audit).trigger_handoff(
                        active_job_id, body.get("repository", ""), control,
                        actor=body.get("actor", "Aryan"), policy_overrides=body.get("policy_overrides"),
                    )
                    return self._json(result, HTTPStatus.CREATED)

                # -- Digital Workforce (Section 20) --
                needs_aryan_q = NeedsAryanQueue(store, control.audit, control)
                if path == "/api/wf/tasks":
                    task_id = WorkforceTaskStore(store, control.audit).create(
                        body.get("department", ""), body.get("objective", ""), body.get("task_type", ""),
                        actor=body.get("actor", "Aryan"), source=body.get("source"), priority=body.get("priority"),
                        inputs=body.get("inputs"),
                    )
                    return self._json({"task_id": task_id}, HTTPStatus.CREATED)
                if path.startswith("/api/wf/tasks/") and path.endswith("/execute"):
                    task_id = path.split("/")[4]
                    orch = _build_workforce_orchestrator(self.app_root, store, control.audit, needs_aryan_q)
                    result = orch.execute(task_id, actor=body.get("actor", "system"))
                    return self._json(result)
                if path == "/api/wf/recurring-workflows":
                    workflow_id = RecurringWorkflowStore(store, control.audit).create(
                        body.get("name", ""), body.get("department", ""), body.get("objective", ""), body.get("task_type", ""),
                        body.get("schedule_kind", ""), schedule_config=body.get("schedule_config"),
                        task_template=body.get("task_template"), actor=body.get("actor", "Aryan"),
                    )
                    return self._json({"workflow_id": workflow_id}, HTTPStatus.CREATED)
                if path.startswith("/api/wf/recurring-workflows/") and path.endswith("/pause"):
                    workflow_id = path.split("/")[4]
                    return self._json(RecurringWorkflowStore(store, control.audit).pause(workflow_id, body.get("actor", "Aryan")))
                if path.startswith("/api/wf/recurring-workflows/") and path.endswith("/resume"):
                    workflow_id = path.split("/")[4]
                    return self._json(RecurringWorkflowStore(store, control.audit).resume(workflow_id, body.get("actor", "Aryan")))
                if path == "/api/wf/recurring-workflows/run-due":
                    task_ids = RecurringWorkflowStore(store, control.audit).run_due(actor=body.get("actor", "system"))
                    return self._json({"task_ids": task_ids}, HTTPStatus.CREATED)

                # -- Media/Growth Engine (Section 20) --
                if path == "/api/media/brands":
                    brand_id = BrandStore(store, control.audit).create(
                        body.get("name", ""), voice_tone=body.get("voice_tone"), audience=body.get("audience"),
                        platforms=body.get("platforms"), content_pillars=body.get("content_pillars"),
                        visual_guidelines=body.get("visual_guidelines"), publishing_rules=body.get("publishing_rules"),
                        approval_policy=body.get("approval_policy"), actor=body.get("actor", "Aryan"),
                    )
                    return self._json({"brand_id": brand_id}, HTTPStatus.CREATED)
                if path == "/api/media/campaigns":
                    campaign_id = CampaignStore(store, control.audit).create(
                        body.get("brand_id", ""), body.get("name", ""), objective=body.get("objective"),
                        start_date=body.get("start_date"), end_date=body.get("end_date"), actor=body.get("actor", "Aryan"),
                    )
                    return self._json({"campaign_id": campaign_id}, HTTPStatus.CREATED)
                if path == "/api/media/content":
                    content_id = ContentStore(store, control.audit).create(
                        body.get("brand_id", ""), body.get("title", ""), body.get("format", ""),
                        objective=body.get("objective"), campaign_id=body.get("campaign_id"), platform=body.get("platform"),
                        target_audience=body.get("target_audience"), cta=body.get("cta"),
                        planned_publish_date=body.get("planned_publish_date"), actor=body.get("actor", "Aryan"),
                    )
                    return self._json({"content_id": content_id}, HTTPStatus.CREATED)
                if path.startswith("/api/media/content/") and path.endswith("/transition"):
                    content_id = path.split("/")[4]
                    result = ContentStore(store, control.audit).transition(
                        content_id, body.get("to_state", ""), body.get("actor", "Aryan"),
                        reason=body.get("reason"), evidence=body.get("evidence"),
                    )
                    return self._json(result)
                if path.startswith("/api/media/content/") and path.endswith("/scripts"):
                    content_id = path.split("/")[4]
                    script_id = ScriptStore(store, control.audit).create_version(
                        content_id, hook=body.get("hook"), body=body.get("body"), scenes=body.get("scenes"),
                        voiceover=body.get("voiceover"), visual_cues=body.get("visual_cues"), cta=body.get("cta"),
                        caption=body.get("caption"), title_options=body.get("title_options"), actor=body.get("actor", "Aryan"),
                    )
                    return self._json({"script_id": script_id}, HTTPStatus.CREATED)
                if path == "/api/media/publications":
                    pub_id = PublicationStore(store, control.audit, needs_aryan=needs_aryan_q).create(
                        body.get("content_id", ""), body.get("platform", ""), actor=body.get("actor", "Aryan"),
                    )
                    return self._json({"publication_id": pub_id}, HTTPStatus.CREATED)
                if path.startswith("/api/media/publications/") and path.endswith("/submit-for-approval"):
                    pub_id = path.split("/")[4]
                    publications = PublicationStore(store, control.audit, needs_aryan=needs_aryan_q)
                    publications.mark_ready(pub_id, body.get("actor", "Aryan"))
                    return self._json(publications.submit_for_approval(pub_id, body.get("actor", "Aryan")), HTTPStatus.CREATED)
                if path.startswith("/api/media/publications/") and path.endswith("/approve"):
                    pub_id = path.split("/")[4]
                    return self._json(PublicationStore(store, control.audit, needs_aryan=needs_aryan_q).approve(pub_id, body.get("actor", "Aryan")))
                if path.startswith("/api/media/publications/") and path.endswith("/publish"):
                    # Always the honest, manual-by-default channel over HTTP --
                    # a simulated "publish" is QA-only and never exposed here.
                    pub_id = path.split("/")[4]
                    publications = PublicationStore(store, control.audit, needs_aryan=needs_aryan_q)
                    pub = publications.get(pub_id)
                    if not pub:
                        return self._json({"error": "publication not found"}, HTTPStatus.NOT_FOUND)
                    content_row = ContentStore(store, control.audit).get(pub["content_id"])
                    result = publications.publish(pub_id, content_row, ManualPublishingChannel(), actor=body.get("actor", "system"))
                    return self._json(result)
                if path.startswith("/api/media/publications/") and path.endswith("/mark-published-manually"):
                    pub_id = path.split("/")[4]
                    result = PublicationStore(store, control.audit, needs_aryan=needs_aryan_q).mark_published_manually(
                        pub_id, body.get("evidence") or {}, body.get("actor", "Aryan"),
                    )
                    return self._json(result)
                if path == "/api/media/analytics":
                    metric_id = AnalyticsStore(store, control.audit).record(
                        body.get("publication_id", ""), body.get("metric_kind", ""), body.get("value"),
                        body.get("source", ""), captured_at=body.get("captured_at"), actor=body.get("actor", "Aryan"),
                    )
                    return self._json({"metric_id": metric_id}, HTTPStatus.CREATED)
                if path == "/api/media/experiments":
                    experiment_id = GrowthExperimentStore(store, control.audit).create(
                        body.get("hypothesis", ""), content_id=body.get("content_id"),
                        variable_tested=body.get("variable_tested"), expected_signal=body.get("expected_signal"),
                        actor=body.get("actor", "Aryan"),
                    )
                    return self._json({"experiment_id": experiment_id}, HTTPStatus.CREATED)
                if path.startswith("/api/media/experiments/") and path.endswith("/result"):
                    experiment_id = path.split("/")[4]
                    result = GrowthExperimentStore(store, control.audit).record_result(
                        experiment_id, body.get("result", ""), body.get("decision", ""), body.get("actor", "Aryan"),
                    )
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
:root{color-scheme:dark;--bg:#0b0908;--side:#100c0a;--panel:#181310;--soft:#201a16;--line:#332a23;--text:#f7f3ef;--muted:#a89c8f;--accent:#e2a15c;--warn:#ffc66d;--bad:#ff8c96}*{box-sizing:border-box}html,body{height:100%;overflow:hidden}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 Inter,ui-sans-serif,system-ui,-apple-system,sans-serif}button,input,select,textarea{font:inherit}.app{height:100dvh;display:grid;grid-template-columns:250px minmax(0,1fr);overflow:hidden}aside{background:var(--side);border-right:1px solid var(--line);padding:18px 12px;display:flex;flex-direction:column;min-height:0;overflow-y:auto;overflow-x:hidden}.brand{display:flex;align-items:center;gap:10px;padding:4px 8px 20px;font-weight:750;font-size:16px}.mark{display:grid;place-items:center;width:29px;height:29px;border-radius:9px;background:var(--accent);color:#221202;font-weight:900}.navsec{color:var(--muted);font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;padding:16px 8px 6px}.navitem{display:block;width:100%;text-align:left;border:0;background:transparent;color:var(--text);padding:8px 8px;border-radius:8px;cursor:pointer;font-size:13px}.navitem:hover,.navitem.active{background:var(--soft)}.navitem.disabled{color:#5b5148;cursor:default}.navitem.disabled:hover{background:transparent}.boundary{margin-top:auto;color:var(--muted);font-size:11px;padding:10px 8px 2px;border-top:1px solid var(--line)}main{min-width:0;overflow-y:auto;padding:28px max(24px,calc((100vw - 250px - 860px)/2))}.col{max-width:860px;margin:0 auto;display:grid;gap:20px}h1{font-size:22px;margin:0 0 2px}.pageintro{color:var(--muted);font-size:13px;margin-bottom:6px}.view{display:none}.view.active{display:block}.section{border:1px solid var(--line);background:var(--panel);border-radius:14px;padding:18px;margin-bottom:18px}.section h2{margin:0 0 4px;font-size:17px}.sub{color:var(--muted);font-size:12px;margin-bottom:14px}.list{display:grid;gap:10px}.item{border:1px solid var(--line);background:var(--soft);border-radius:11px;padding:13px}.item h3{margin:0 0 4px;font-size:14px}.meta{color:var(--muted);font-size:11px;display:flex;gap:8px;flex-wrap:wrap;margin-bottom:6px}.meta span{border:1px solid var(--line);border-radius:999px;padding:2px 8px}.empty{color:var(--muted);font-size:12px;padding:6px 0}.form{display:grid;gap:8px;margin-top:12px;border-top:1px solid var(--line);padding-top:12px}.form input,.form select,.form textarea{background:var(--panel);border:1px solid var(--line);color:var(--text);border-radius:8px;padding:8px 10px;width:100%}.form textarea{min-height:50px;resize:vertical}.row{display:flex;gap:8px}.row>*{flex:1}.actions{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}.actions button{border:0;border-radius:8px;padding:6px 11px;font-size:12px;font-weight:700;cursor:pointer;background:var(--accent);color:#221202}.actions button.secondary{background:#2c241d;color:var(--text)}.actions button.danger{background:#542c34;color:#ffe0e4}.contrib{border-left:2px solid var(--line);padding:6px 0 6px 10px;margin-top:6px;font-size:12px}.contrib b{color:var(--accent)}.badge-actionable{color:var(--accent)}.badge-inspect{color:var(--warn)}.badge{color:var(--warn);font-weight:700;border-color:var(--warn)!important}
@media(max-width:820px){.app{grid-template-columns:1fr;height:auto;min-height:100dvh}aside{flex-direction:row;flex-wrap:wrap;align-items:center;gap:4px;border-right:0;border-bottom:1px solid var(--line);padding:10px 12px}aside .brand{width:100%;padding:2px 4px 10px}aside .navsec,aside .boundary{display:none}aside .navitem{padding:6px 10px;font-size:12px}main{padding:20px 16px}.row{flex-direction:column}}
</style></head><body><div class="app"><aside>
<div class="brand"><span class="mark">TT</span>Twenty Two Technologies</div>
<div class="navsec">Command Center</div>
<button class="navitem active" data-view="commandCenter">Command Center</button>
<div class="navsec">CEO Intelligence / Finance</div>
<button class="navitem" data-view="ccGoals">Goals</button>
<button class="navitem" data-view="ccKpis">KPIs</button>
<button class="navitem" data-view="ccLedger">Finance Ledger</button>
<button class="navitem" data-view="ccCash">Cash &amp; Runway</button>
<button class="navitem" data-view="ccBudgets">Budgets</button>
<button class="navitem" data-view="ccCapital">Capital Allocation</button>
<button class="navitem" data-view="ccDeptPerf">Department Performance</button>
<button class="navitem" data-view="ccRiskRegister">Risk Register</button>
<div class="navsec">Company</div>
<button class="navitem" data-view="boardroom">Boardroom</button>
<button class="navitem" data-view="backlog">Master Vision Backlog</button>
<button class="navitem" data-view="needsAryan">Needs Aryan</button>
<div class="navsec">Revenue Hunter</div>
<button class="navitem" data-view="rhToday">Today</button>
<button class="navitem" data-view="rhSalesManager">Sales Manager</button>
<button class="navitem" data-view="rhOpportunities">Opportunities</button>
<button class="navitem" data-view="rhOutboundLeads">Outbound Leads</button>
<button class="navitem" data-view="rhPipeline">Sales Pipeline</button>
<button class="navitem" data-view="rhClients">Clients</button>
<button class="navitem" data-view="rhActiveJobs">Active Jobs / Delivery</button>
<button class="navitem" data-view="rhRevenue">Revenue</button>
<button class="navitem" data-view="rhSettings">Acquisition Settings</button>
<div class="navsec">Digital Workforce</div>
<button class="navitem" data-view="wfTasks">Workforce Tasks</button>
<button class="navitem" data-view="wfWorkflows">Recurring Workflows</button>
<div class="navsec">Media / Growth</div>
<button class="navitem" data-view="mediaBrands">Brands</button>
<button class="navitem" data-view="mediaContent">Content Calendar</button>
<button class="navitem" data-view="mediaPublications">Publishing</button>
<button class="navitem" data-view="mediaExperiments">Growth Experiments</button>
<div class="navsec">Coming soon</div>
<button class="navitem disabled" disabled>Ventures / Company Ops</button>
<div class="boundary">TTT HQ decides · Falguna executes<br>Local-only, no automatic merge or deploy</div>
</aside>
<main>
<div class="col">
<div class="view active" id="view-commandCenter">
<h1>Command Center</h1>
<div class="pageintro">What the company is doing right now -- sourced live from Revenue Hunter, billing, Digital Workforce, and Media/Growth. No vanity metrics; every card below is a real, sourced read.</div>
<div class="row">
<div class="section" style="flex:1"><h2 id="ccWonRevenue">$0</h2><div class="sub">Lifetime won revenue</div></div>
<div class="section" style="flex:1"><h2 id="ccCashIn">$0</h2><div class="sub">Cash in to date (invoice payments, inflow-only)</div></div>
</div>
<div class="row">
<div class="section" style="flex:1"><h2 id="ccOutstanding">$0</h2><div class="sub">Receivables outstanding</div></div>
<div class="section" style="flex:1"><h2 id="ccOverdue">$0</h2><div class="sub">Receivables overdue</div></div>
</div>
<div class="row">
<div class="section" style="flex:1"><h2 id="ccPipeline">0</h2><div class="sub">Active pipeline (<span id="ccNegotiating">0</span> negotiating)</div></div>
<div class="section" style="flex:1"><h2 id="ccClients">0</h2><div class="sub">Clients</div></div>
</div>
<div class="row">
<div class="section" style="flex:1"><h2 id="ccActiveJobs">0</h2><div class="sub">Active delivery jobs</div></div>
<div class="section" style="flex:1"><h2 id="ccNeedsAryan">0</h2><div class="sub">Needs Aryan pending</div></div>
</div>
<div class="row">
<div class="section" style="flex:1"><h2 id="ccWfAttention">0</h2><div class="sub">Workforce tasks needing attention</div></div>
<div class="section" style="flex:1"><h2 id="ccMediaFailures">0</h2><div class="sub">Media publishing failures</div></div>
</div>
<div class="section"><h2>Risk signals</h2><div class="list" id="ccRisks"></div></div>
<div class="section"><h2>Upcoming obligations</h2><div class="list" id="ccUpcoming"></div></div>
<div class="section"><h2>Key opportunities</h2><div class="list" id="ccKeyOpps"></div></div>
<div class="section">
<h2>CEO Brief</h2>
<div class="sub" id="ccBriefMeta">No brief generated yet.</div>
<div class="actions"><button id="ccGenerateBrief" type="button">Generate CEO Brief</button></div>
<div class="list" id="ccBriefFacts"></div>
<div class="list" id="ccBriefRecs"></div>
</div>
</div>
<div class="view" id="view-ccGoals">
<h1>Goals</h1>
<div class="pageintro">Persistent company goals with a target, unit, owner, department, and linked KPIs/actions. Progress is only ever moved forward explicitly -- never inferred.</div>
<div class="list" id="ccGoalsList"></div>
<div class="form">
<input id="glTitle" placeholder="Goal title">
<div class="row"><input id="glTarget" type="number" step="any" placeholder="Target"><input id="glUnit" placeholder="Unit (e.g. USD, clients, %)"></div>
<div class="row"><input id="glOwner" placeholder="Owner (optional)"><input id="glDepartment" placeholder="Department (optional)"></div>
<div class="row"><input id="glStartDate" type="date"><input id="glDeadline" type="date"></div>
<div class="actions"><button id="glCreate" type="button">Create goal</button></div>
</div>
</div>
<div class="view" id="view-ccKpis">
<h1>KPIs</h1>
<div class="pageintro">Sales / Delivery / Workforce / Media / Finance -- every metric computed live from existing tables for a trailing 30-day window. A null value with a source means the codebase has no data model for that metric yet, never a guess.</div>
<div class="list" id="ccKpisList"></div>
</div>
<div class="view" id="view-ccLedger">
<h1>Finance Ledger</h1>
<div class="pageintro">Every inflow/outflow requires evidence. Nothing here is a full accounting system -- it is a sourced, auditable record. Voiding preserves history; nothing is ever hard-deleted.</div>
<div class="list" id="ccLedgerList"></div>
<div class="form">
<div class="row">
<select id="leType"><option value="OUTFLOW">OUTFLOW</option><option value="INFLOW">INFLOW</option></select>
<select id="leCategory"><option value="client_revenue">client_revenue</option><option value="subscription_api_cost">subscription_api_cost</option><option value="software_tooling">software_tooling</option><option value="hosting">hosting</option><option value="contractor">contractor</option><option value="marketing">marketing</option><option value="hardware">hardware</option><option value="tax_reserve">tax_reserve</option><option value="owner_contribution">owner_contribution</option><option value="other">other</option></select>
</div>
<div class="row"><input id="leAmount" type="number" step="any" placeholder="Amount"><input id="leCurrency" placeholder="Currency" value="INR"></div>
<input id="leBusinessUnit" placeholder="Business unit (e.g. Sales, Delivery, Digital Workforce, Media/Growth)">
<input id="leEvidence" placeholder="Evidence (required -- receipt/invoice reference, description)">
<div class="row"><input id="leOccurredOn" type="date"><input id="leProjectRef" placeholder="Project ref (optional)"></div>
<textarea id="leNote" placeholder="Note (optional)"></textarea>
<div class="actions"><button id="leCreate" type="button">Record entry</button></div>
</div>
</div>
<div class="view" id="view-ccCash">
<h1>Cash &amp; Runway</h1>
<div class="pageintro">Actual, expected, and projected are always kept separate -- never blended into one number.</div>
<div class="section"><h2>Actual</h2><div class="list" id="ccCashActual"></div></div>
<div class="section"><h2>Expected</h2><div class="list" id="ccCashExpected"></div></div>
<div class="section"><h2>Projected</h2><div class="list" id="ccCashProjected"></div></div>
<div class="section"><h2>Runway</h2><div class="list" id="ccCashRunway"></div></div>
</div>
<div class="view" id="view-ccBudgets">
<h1>Budgets</h1>
<div class="pageintro">Spend-to-date is always computed live from the Finance Ledger, never stored. A HARD limit breach escalates to Needs Aryan automatically; a SOFT limit only warns.</div>
<div class="list" id="ccBudgetsList"></div>
<div class="form">
<div class="row"><input id="bgDepartment" placeholder="Department"><input id="bgMonthlyBudget" type="number" step="any" placeholder="Monthly budget"></div>
<div class="row"><select id="bgLimitKind"><option value="SOFT">SOFT</option><option value="HARD">HARD</option></select><input id="bgCurrency" placeholder="Currency" value="INR"></div>
<input id="bgWarningPct" type="number" step="any" placeholder="Warning threshold (0-1, default 0.8)">
<div class="actions"><button id="bgCreate" type="button">Create budget</button></div>
</div>
</div>
<div class="view" id="view-ccCapital">
<h1>Capital Allocation</h1>
<div class="pageintro">Recommendations only -- this never moves money automatically. Reserve policy structurally separates experimental capital from client funds, tax reserve, operating runway, and the emergency reserve.</div>
<div class="section"><h2>Reserve policy</h2><div class="list" id="ccReservePolicy"></div>
<div class="form">
<div class="row"><input id="rpOperating" type="number" step="any" placeholder="Operating reserve %"><input id="rpTax" type="number" step="any" placeholder="Tax reserve %"></div>
<div class="row"><input id="rpEmergency" type="number" step="any" placeholder="Emergency reserve %"><input id="rpReinvestment" type="number" step="any" placeholder="Reinvestment pool %"></div>
<div class="row"><input id="rpOwnerDist" type="number" step="any" placeholder="Owner distribution %"><input id="rpExperimental" type="number" step="any" placeholder="Experimental capital %"></div>
<div class="actions"><button id="rpSave" type="button">Save reserve policy</button></div>
</div>
</div>
<div class="section"><h2 id="ccExpCapital">$0</h2><div class="sub">Available experimental capital (owner_contribution inflows minus spend -- never client funds)</div></div>
<div class="section"><h2>Recommend allocation</h2>
<div class="form">
<input id="caAvailable" type="number" step="any" placeholder="Available amount to allocate">
<div class="actions"><button id="caRecommend" type="button">Get recommendations</button></div>
</div>
<div class="list" id="ccCapitalRecs"></div>
</div>
</div>
<div class="view" id="view-ccDeptPerf">
<h1>Department Performance</h1>
<div class="pageintro">Output / failures / blocked work / cost / business impact, read straight from each department's own system. No composite score -- a plain, sourced breakdown to read and judge.</div>
<div class="list" id="ccDeptPerfList"></div>
<h1 style="margin-top:24px">AI Workforce Performance</h1>
<div class="pageintro">Per-worker success/failure/retry/intervention rate and approximate cost. Every rate is a real ratio of real counts -- null, not zero, when there's no data yet.</div>
<div class="list" id="ccWorkforcePerfList"></div>
</div>
<div class="view" id="view-ccRiskRegister">
<h1>Risk Register</h1>
<div class="pageintro">Severity and likelihood band are plain labels, never a fabricated numerical probability. High/critical severity escalates to Needs Aryan automatically.</div>
<div class="list" id="ccRiskRegisterList"></div>
<div class="form">
<input id="rkTitle" placeholder="Risk title">
<div class="row">
<select id="rkCategory"><option value="revenue_risk">revenue_risk</option><option value="client_risk">client_risk</option><option value="delivery_risk">delivery_risk</option><option value="finance_risk">finance_risk</option><option value="security_risk">security_risk</option><option value="infrastructure_risk">infrastructure_risk</option><option value="legal_compliance_risk">legal_compliance_risk</option><option value="concentration_risk">concentration_risk</option></select>
<select id="rkSeverity"><option value="low">low</option><option value="medium">medium</option><option value="high">high</option><option value="critical">critical</option></select>
<select id="rkLikelihood"><option value="unknown">unknown</option><option value="unlikely">unlikely</option><option value="possible">possible</option><option value="likely">likely</option><option value="near_certain">near_certain</option></select>
</div>
<input id="rkOwner" placeholder="Owner (optional)">
<textarea id="rkMitigation" placeholder="Mitigation plan (optional)"></textarea>
<textarea id="rkEvidence" placeholder="Evidence (optional)"></textarea>
<div class="actions"><button id="rkCreate" type="button">Log risk</button></div>
</div>
</div>
<div class="view" id="view-boardroom">
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
<div class="row">
<div class="section" style="flex:1"><h2 id="rhFoundToday">0</h2><div class="sub">Opportunities found today</div></div>
<div class="section" style="flex:1"><h2 id="rhAgingCount">0</h2><div class="sub">Aging opportunities (14+ days, still active)</div></div>
</div>
<div class="section"><h2>Last discovery run</h2><div class="list" id="rhLastRun"></div></div>
<div class="section"><h2>Next actions</h2><div class="list" id="rhNextActions"></div></div>
<div class="row">
<div class="section" style="flex:1"><h2>Blocked workforce tasks</h2><div class="list" id="todayWfBlocked"></div></div>
<div class="section" style="flex:1"><h2>Media needing approval</h2><div class="list" id="todayMediaApproval"></div></div>
</div>
<div class="row">
<div class="section" style="flex:1"><h2>Content due soon</h2><div class="list" id="todayContentDue"></div></div>
<div class="section" style="flex:1"><h2>Publishing failures</h2><div class="list" id="todayPublishFailures"></div></div>
</div>
<div class="section"><h2>Strong growth signals</h2><div class="list" id="todayGrowthSignals"></div></div>
</div>
<div class="view" id="view-rhSalesManager">
<h1>Sales Manager</h1>
<div class="pageintro">The full lifecycle in one place -- what needs attention, who replied, what can close soon, who's onboarding, what's blocked, what's overdue, and who to upsell. Read-only: every item links back to where you actually act on it.</div>
<div class="section"><h2>Needs attention now</h2><div class="list" id="smAttention"></div></div>
<div class="section"><h2>Best leads</h2><div class="sub">Priority is a plain High/Medium/Low label from real qualification + age -- never a manufactured probability.</div><div class="list" id="smBestLeads"></div></div>
<div class="row">
<div class="section" style="flex:1"><h2>Who replied</h2><div class="list" id="smWhoReplied"></div></div>
<div class="section" style="flex:1"><h2>Negotiations needing action</h2><div class="list" id="smNegotiations"></div></div>
</div>
<div class="row">
<div class="section" style="flex:1"><h2>Can close soon</h2><div class="list" id="smCloseSoon"></div></div>
<div class="section" style="flex:1"><h2>Onboarding clients</h2><div class="list" id="smOnboarding"></div></div>
</div>
<div class="row">
<div class="section" style="flex:1"><h2>Blocked deliveries</h2><div class="list" id="smBlockedDeliveries"></div></div>
<div class="section" style="flex:1"><h2>Overdue invoices</h2><div class="list" id="smOverdueInvoices"></div></div>
</div>
<div class="section"><h2>Upsell-ready clients</h2><div class="list" id="smUpsell"></div></div>
</div>
<div class="view" id="view-rhOpportunities">
<h1>Opportunities</h1>
<div class="pageintro">Auto-discovered from configured sources, or add one yourself. Nothing here is ever sent automatically -- discovery only finds, qualifies, and drafts; you approve.</div>
<div class="section">
<div class="actions"><button id="rhDiscoverNow" type="button">Find Opportunities Now</button></div>
<div class="list" id="rhDiscoverStatus"></div>
</div>
<div class="row" style="margin:6px 0;flex-wrap:wrap">
<button class="secondary rhQuickFilter" data-filter="all">All</button>
<button class="secondary rhQuickFilter" data-filter="new">New discoveries</button>
<button class="secondary rhQuickFilter" data-filter="pursue">Pursue</button>
<button class="secondary rhQuickFilter" data-filter="maybe">Maybe</button>
<button class="secondary rhQuickFilter" data-filter="ignored">Ignored</button>
<button class="secondary rhQuickFilter" data-filter="reviewed">Already reviewed</button>
</div>
<div class="row" style="margin:6px 0;flex-wrap:wrap">
<select id="rhStageFilter"><option value="">All stages</option></select>
<select id="rhSourceFilter"><option value="">All sources</option></select>
<input id="rhMinScore" type="number" min="0" max="100" placeholder="Min score">
<input id="rhMaxAgeDays" type="number" min="0" placeholder="Max age (days)">
</div>
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
<div class="list" id="rhOpportunityList"></div>
</div>
<div class="view" id="view-rhOutboundLeads">
<h1>Outbound Leads</h1>
<div class="pageintro">One researched prospect at a time -- no bulk lists, no scraping. Outreach is always drafted only: nothing here ever sends a message. A draft needs your approval in Needs Aryan before you send it yourself and mark it sent.</div>
<div class="form">
<input id="obCompany" placeholder="Company name">
<input id="obWebsite" placeholder="Website (optional)">
<input id="obContact" placeholder="Contact name / channel (optional)">
<textarea id="obLikelyNeed" placeholder="Likely need"></textarea>
<textarea id="obOffer" placeholder="Proposed offer"></textarea>
<div class="row">
<select id="obConfidence"><option value="">Confidence (optional)</option><option>Low</option><option>Medium</option><option>High</option></select>
<input id="obSource" placeholder="Source (how you found them)">
</div>
<textarea id="obNotes" placeholder="Relevance notes -- why this is a real, explainable lead"></textarea>
<div class="actions"><button id="obCreate" type="button">Add lead</button></div>
</div>
<div class="list" id="rhOutboundLeadsList"></div>
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
<div class="view" id="view-rhSettings">
<h1>Acquisition Settings</h1>
<div class="pageintro">What Opportunity Agent looks for and where. Changes take effect on the next discovery run -- persisted, not hard-coded.</div>
<div class="form">
<textarea id="rhSetServices" placeholder="Services we sell (one per line)"></textarea>
<textarea id="rhSetSkills" placeholder="Skills (comma-separated)"></textarea>
<div class="row"><input id="rhSetMinBudget" type="number" placeholder="Minimum budget (USD, optional)"><input id="rhSetMaxAge" type="number" placeholder="Maximum listing age (days)"></div>
<div class="row">
<select id="rhSetRemote"><option value="remote_ok">Remote OK (default)</option><option value="remote_only">Remote only</option><option value="any">Any</option></select>
<input id="rhSetCountries" placeholder="Target countries (comma-separated, blank = no restriction)">
</div>
<input id="rhSetKeywords" placeholder="Keywords to require (comma-separated, blank = no restriction)">
<input id="rhSetExcluded" placeholder="Excluded keywords (comma-separated)">
<div class="sub">Relevance gate (service-relevance hardening)</div>
<textarea id="rhSetPositiveSignals" placeholder="Positive service signals -- phrases that indicate genuine deliverable dev/AI work (one per line)"></textarea>
<textarea id="rhSetExclusionSignals" placeholder="Exclusion role signals -- phrases for roles TTT does not deliver, e.g. writer, recruiter, sales rep (one per line)"></textarea>
<div class="sub">Sources</div>
<div class="list" id="rhSetSources"></div>
<div class="actions"><button id="rhSetSave" type="button">Save acquisition profile</button></div>
</div>
</div>
<div class="section">
<h2>Re-qualify existing opportunities</h2>
<div class="pageintro">Re-runs qualification for every non-terminal opportunity using the current acquisition profile and scoring logic -- e.g. after a relevance hardening pass. Never deletes history; a PURSUE downgrade supersedes its draft proposal and rejects any pending Needs Aryan item for it, an upgrade drafts a proposal exactly as a fresh discovery would.</div>
<div class="actions"><button id="rhRequalifyAll" type="button">Requalify all</button></div>
<div class="list" id="rhRequalifyResult"></div>
</div>
<div class="view" id="view-wfTasks">
<h1>Workforce Tasks</h1>
<div class="pageintro">The Digital Workforce's durable task queue -- research, browser/API/computer work, documents, spreadsheets, email prep, content ops, and every Media agent step. Created by recurring workflows or by other TTT/Falguna processes; a BLOCKED or NEEDS_ARYAN task means a real capability gap or a real decision, surfaced generically in Needs Aryan.</div>
<div class="list" id="wfTasksList"></div>
</div>
<div class="view" id="view-wfWorkflows">
<h1>Recurring Workflows</h1>
<div class="pageintro">Lightweight schedule definitions -- not an OS-level daemon. Each one creates a real Workforce Task when it's due; nothing here executes a task by itself.</div>
<div class="list" id="wfWorkflowsList"></div>
</div>
<div class="view" id="view-mediaBrands">
<h1>Brands</h1>
<div class="pageintro">Persistent voice/tone, audience, platforms, content pillars, visual guidelines, and approval policy -- one definition per brand, read by every piece of content and every Media agent.</div>
<div class="list" id="mediaBrandsList"></div>
</div>
<div class="view" id="view-mediaContent">
<h1>Content Calendar</h1>
<div class="pageintro">Every content item's real pipeline state: Research -> Idea -> Content Plan -> Script -> Visual Plan -> Asset Creation -> Voice -> Video/Edit -> Captions -> Thumbnail -> Approval -> Publish -> Analytics -> Learn.</div>
<div class="list" id="mediaContentList"></div>
</div>
<div class="view" id="view-mediaPublications">
<h1>Publishing</h1>
<div class="pageintro">DRAFT -> READY -> AWAITING_APPROVAL -> APPROVED -> PUBLISHING -> PUBLISHED/FAILED. No real platform adapter exists yet in v1 -- a real publish always needs owner approval (Needs Aryan) and, today, a manual publish + real evidence outside this system; PUBLISHED here is never claimed without that real evidence.</div>
<div class="list" id="mediaPublicationsList"></div>
</div>
<div class="view" id="view-mediaExperiments">
<h1>Growth Experiments</h1>
<div class="pageintro">Hypothesis / variable tested / expected signal / result / decision -- a plain record, not an attribution model.</div>
<div class="list" id="mediaExperimentsList"></div>
</div>
</div>
</main>
</div>
<script>
const $=id=>document.getElementById(id);
async function api(url,options){const r=await fetch(url,options);const j=await r.json();if(!r.ok)throw Object.assign(new Error(j.error||'Request failed'),{data:j});return j}
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let FALGUNA_URL='http://127.0.0.1:8765';
const rhLoaders={commandCenter:loadCommandCenter,ccGoals:loadCcGoals,ccKpis:loadCcKpis,ccLedger:loadCcLedger,ccCash:loadCcCash,ccBudgets:loadCcBudgets,ccCapital:loadCcCapital,ccDeptPerf:loadCcDeptPerf,ccRiskRegister:loadCcRiskRegister,rhToday:loadRhToday,rhSalesManager:loadRhSalesManager,rhOpportunities:loadRhOpportunities,rhOutboundLeads:loadRhOutboundLeads,rhPipeline:loadRhPipeline,rhClients:loadRhClients,rhActiveJobs:loadRhActiveJobs,rhRevenue:loadRhRevenue,rhSettings:loadRhSettings,wfTasks:loadWfTasks,wfWorkflows:loadWfWorkflows,mediaBrands:loadMediaBrands,mediaContent:loadMediaContent,mediaPublications:loadMediaPublications,mediaExperiments:loadMediaExperiments};
document.querySelectorAll('.navitem[data-view]').forEach(b=>b.onclick=()=>{document.querySelectorAll('.navitem[data-view]').forEach(x=>x.classList.remove('active'));document.querySelectorAll('.view').forEach(x=>x.classList.remove('active'));b.classList.add('active');$('view-'+b.dataset.view).classList.add('active');if(rhLoaders[b.dataset.view])rhLoaders[b.dataset.view]().catch(e=>{})});
async function loadAll(){const c=await api('/api/config');FALGUNA_URL=c.falguna_url||FALGUNA_URL;await Promise.all([loadCommandCenter(),loadBoardroom(),loadBacklog(),loadNeedsAryan()])}
async function loadCommandCenter(){
const d=await api('/api/cc/snapshot');
$('ccWonRevenue').textContent='$'+d.revenue.won_revenue_lifetime;
$('ccCashIn').textContent='$'+d.cash.cash_in_to_date;
$('ccOutstanding').textContent='$'+d.receivables.outstanding_total;
$('ccOverdue').textContent='$'+d.receivables.overdue_total;
$('ccPipeline').textContent=d.pipeline.active_count;
$('ccNegotiating').textContent=d.pipeline.negotiating_count;
$('ccClients').textContent=d.clients.total;
$('ccActiveJobs').textContent=d.delivery.active_jobs_total;
$('ccNeedsAryan').textContent=d.needs_aryan.pending_count;
$('ccWfAttention').textContent=d.workforce.needs_attention_count;
$('ccMediaFailures').textContent=d.media.publishing_failures_count;
$('ccRisks').innerHTML=(d.risk_signals||[]).length?d.risk_signals.map(r=>`<div class="item"><h3>${esc(r.summary)}</h3><div class="meta"><span>${esc(r.category)}</span><span>${esc(r.severity)}</span></div></div>`).join(''):'<div class="empty">No active risk signals.</div>';
$('ccUpcoming').innerHTML=(d.upcoming_obligations||[]).length?d.upcoming_obligations.map(o=>`<div class="item"><h3>${esc(o.kind)}</h3><div class="meta"><span>due ${esc(o.due_date||'')}</span></div></div>`).join(''):'<div class="empty">Nothing due soon.</div>';
$('ccKeyOpps').innerHTML=(d.pipeline.key_opportunities||[]).length?d.pipeline.key_opportunities.map(o=>`<div class="item"><h3>${esc(o.title)}</h3><div class="meta"><span>${esc(o.stage)}</span><span>${esc(o.client_name||'')}</span></div></div>`).join(''):'<div class="empty">No active opportunities.</div>';
try{const b=await api('/api/cc/ceo-brief/latest');if(!b.error){renderCeoBrief(b)}}catch(e){}
}
function renderCeoBrief(b){
$('ccBriefMeta').textContent=`Covers ${b.period_start} to ${b.period_end}`;
const facts=JSON.parse(b.confirmed_facts_json);
$('ccBriefFacts').innerHTML=`<div class="item"><h3>Confirmed facts</h3><div class="meta"><span>deals won ${facts.deals_won}</span><span>new opportunities ${facts.new_opportunities}</span><span>client replies ${facts.client_replies}</span><span>payments received $${facts.payments_received_total}</span><span>workforce failures ${facts.workforce_failures}</span></div></div>`;
const recs=JSON.parse(b.recommendations_json);
const priorities=JSON.parse(b.top_priorities_json);
const estimates=JSON.parse(b.estimates_json);
$('ccBriefRecs').innerHTML=`<div class="item"><h3>Top priorities</h3><div class="meta">${priorities.map(p=>`<span>${esc(p)}</span>`).join('')||'<span>None</span>'}</div></div>`+recs.map(r=>`<div class="item">${esc(r)}</div>`).join('')+estimates.map(e=>`<div class="item">${esc(e)}</div>`).join('');
}
function renderMetric(label,m){if(!m)return'';return `<div class="item"><h3>${esc(label)}: ${m.value===null||m.value===undefined?'—':esc(m.value)}</h3><div class="meta"><span>${esc(m.source||'')}</span></div></div>`}
async function loadCcGoals(){const d=await api('/api/cc/goals');const items=d.items||[];$('ccGoalsList').innerHTML=items.length?items.map(g=>`<div class="item">
<h3>${esc(g.title)} <span style="color:var(--muted);font-weight:400">(${esc(g.status)})</span></h3>
<div class="meta"><span>${esc(g.current_value)} / ${esc(g.target)} ${esc(g.unit)}</span><span>${g.progress===null?'progress n/a':(g.progress*100).toFixed(1)+'%'}</span>${g.at_risk?'<span class="badge">at risk</span>':''}${g.owner?`<span>owner ${esc(g.owner)}</span>`:''}${g.department?`<span>${esc(g.department)}</span>`:''}${g.deadline?`<span>due ${esc(g.deadline)}</span>`:''}</div>
<div class="actions">
<button class="secondary progress" data-id="${esc(g.id)}">Update progress</button>
<button class="secondary achieve" data-id="${esc(g.id)}">Mark achieved</button>
<button class="secondary pause" data-id="${esc(g.id)}">Pause</button>
<button class="danger cancel" data-id="${esc(g.id)}">Cancel</button>
</div>
</div>`).join(''):'<div class="empty">No goals yet.</div>';
document.querySelectorAll('#ccGoalsList .progress').forEach(b=>b.onclick=async()=>{const v=prompt('New current value:');if(v===null||v==='')return;await api(`/api/cc/goals/${b.dataset.id}/progress`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({value:parseFloat(v),actor:'Aryan'})});await loadCcGoals()});
document.querySelectorAll('#ccGoalsList .achieve').forEach(b=>b.onclick=async()=>{await api(`/api/cc/goals/${b.dataset.id}/status`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({status:'ACHIEVED',actor:'Aryan'})});await loadCcGoals()});
document.querySelectorAll('#ccGoalsList .pause').forEach(b=>b.onclick=async()=>{await api(`/api/cc/goals/${b.dataset.id}/status`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({status:'PAUSED',actor:'Aryan'})});await loadCcGoals()});
document.querySelectorAll('#ccGoalsList .cancel').forEach(b=>b.onclick=async()=>{await api(`/api/cc/goals/${b.dataset.id}/status`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({status:'CANCELLED',actor:'Aryan'})});await loadCcGoals()});
}
$('glCreate').onclick=async()=>{const title=$('glTitle').value.trim();const target=parseFloat($('glTarget').value);const unit=$('glUnit').value.trim();if(!title||isNaN(target)||!unit)return alert('Title, target, and unit are required.');await api('/api/cc/goals',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title,target,unit,owner:$('glOwner').value||null,department:$('glDepartment').value||null,start_date:$('glStartDate').value||null,deadline:$('glDeadline').value||null,actor:'Aryan'})});$('glTitle').value='';$('glTarget').value='';$('glUnit').value='';$('glOwner').value='';$('glDepartment').value='';await loadCcGoals()};
async function loadCcKpis(){const d=await api('/api/cc/kpis');const groups=[['Sales',d.sales],['Delivery',d.delivery],['Workforce',d.workforce],['Media',d.media],['Finance',d.finance]];
$('ccKpisList').innerHTML=groups.map(([name,metrics])=>`<div class="section"><h2>${esc(name)}</h2><div class="list">${Object.entries(metrics).map(([k,m])=>renderMetric(k,m)).join('')}</div></div>`).join('');
}
async function loadCcLedger(){const d=await api('/api/cc/ledger');const items=d.items||[];$('ccLedgerList').innerHTML=items.length?items.map(e=>`<div class="item">
<h3>${esc(e.entry_type)} ${esc(e.currency)} ${esc(e.amount)} -- ${esc(e.category)}</h3>
<div class="meta"><span>${esc(e.business_unit||'unassigned')}</span><span>${esc(e.occurred_on)}</span><span>${esc(e.status)}</span></div>
<div>${esc(e.evidence)}</div>
${e.status==='RECORDED'?`<div class="actions"><button class="danger void" data-id="${esc(e.id)}">Void</button></div>`:''}
</div>`).join(''):'<div class="empty">No ledger entries yet.</div>';
document.querySelectorAll('#ccLedgerList .void').forEach(b=>b.onclick=async()=>{const reason=prompt('Reason for voiding this entry:');if(!reason)return;await api(`/api/cc/ledger/${b.dataset.id}/void`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({reason,actor:'Aryan'})});await loadCcLedger()});
}
$('leCreate').onclick=async()=>{const amount=parseFloat($('leAmount').value);const evidence=$('leEvidence').value.trim();if(isNaN(amount)||!evidence)return alert('Amount and evidence are required.');await api('/api/cc/ledger',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({entry_type:$('leType').value,category:$('leCategory').value,amount,evidence,currency:$('leCurrency').value||'INR',business_unit:$('leBusinessUnit').value||null,occurred_on:$('leOccurredOn').value||null,project_ref:$('leProjectRef').value||null,note:$('leNote').value||null,actor:'Aryan'})});$('leAmount').value='';$('leEvidence').value='';$('leBusinessUnit').value='';$('leNote').value='';await loadCcLedger()};
async function loadCcCash(){const d=await api('/api/cc/cash-runway');
$('ccCashActual').innerHTML=`<div class="item"><h3>Invoice cash in: $${esc(d.actual.invoice_cash_in_to_date)}</h3></div><div class="item"><h3>Ledger inflows: $${esc(d.actual.ledger_inflows_recorded)}</h3></div><div class="item"><h3>Ledger outflows: $${esc(d.actual.ledger_outflows_recorded)}</h3></div><div class="item"><h3>Combined cash estimate: $${esc(d.actual.combined_cash_estimate)}</h3><div class="meta"><span>${esc(d.actual.source)}</span></div></div>`;
$('ccCashExpected').innerHTML=`<div class="item"><h3>Receivables outstanding: $${esc(d.expected.receivables_outstanding)}</h3></div><div class="item"><h3>Receivables overdue: $${esc(d.expected.receivables_overdue)}</h3></div><div class="item"><h3>Due within ${esc(d.expected.receivables_due_within_days)} days: $${esc(d.expected.receivables_due_soon)}</h3></div>`;
$('ccCashProjected').innerHTML=`<div class="item"><h3>Near-term cash: $${esc(d.projected.near_term_cash)}</h3><div class="meta"><span>${esc(d.projected.note)}</span></div></div>`;
$('ccCashRunway').innerHTML=`<div class="item"><h3>Monthly burn rate: ${d.runway.monthly_burn_rate===null?'—':'$'+esc(d.runway.monthly_burn_rate)}</h3></div><div class="item"><h3>Runway: ${d.runway.runway_months===null?'—':esc(d.runway.runway_months)+' months'}</h3><div class="meta"><span>${esc(d.runway.note)}</span></div></div>`;
}
async function loadCcBudgets(){const d=await api('/api/cc/budgets');const items=d.items||[];$('ccBudgetsList').innerHTML=items.length?items.map(b=>`<div class="item">
<h3>${esc(b.department)} -- ${esc(b.currency)} ${esc(b.monthly_budget)}/mo (${esc(b.limit_kind)})</h3>
<div class="meta"><span>warning at ${(b.warning_threshold_pct*100).toFixed(0)}%</span></div>
<div class="actions"><button class="secondary status" data-id="${esc(b.id)}">Check status</button></div>
<div class="list" id="budget-status-${esc(b.id)}"></div>
</div>`).join(''):'<div class="empty">No budgets yet.</div>';
document.querySelectorAll('#ccBudgetsList .status').forEach(b=>b.onclick=async()=>{const s=await api(`/api/cc/budgets/${b.dataset.id}/status`);const el=$('budget-status-'+b.dataset.id);el.innerHTML=`<div class="item"><div class="meta"><span>spend to date ${esc(s.spend_to_date)}</span><span>remaining ${esc(s.remaining_budget)}</span>${s.warning?'<span class="badge">warning</span>':''}${s.over_limit?'<span class="badge">over limit</span>':''}${s.escalated_needs_aryan_id?'<span class="badge">escalated to Needs Aryan</span>':''}</div></div>`});
}
$('bgCreate').onclick=async()=>{const department=$('bgDepartment').value.trim();const monthly_budget=parseFloat($('bgMonthlyBudget').value);if(!department||isNaN(monthly_budget))return alert('Department and monthly budget are required.');const warn=$('bgWarningPct').value;await api('/api/cc/budgets',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({department,monthly_budget,limit_kind:$('bgLimitKind').value,currency:$('bgCurrency').value||'INR',warning_threshold_pct:warn?parseFloat(warn):0.8,actor:'Aryan'})});$('bgDepartment').value='';$('bgMonthlyBudget').value='';$('bgWarningPct').value='';await loadCcBudgets()};
async function loadCcCapital(){const p=await api('/api/cc/reserve-policy');
$('ccReservePolicy').innerHTML=p.configured?`<div class="item"><div class="meta"><span>operating ${(p.operating_reserve_pct*100).toFixed(0)}%</span><span>tax ${(p.tax_reserve_pct*100).toFixed(0)}%</span><span>emergency ${(p.emergency_reserve_pct*100).toFixed(0)}%</span><span>reinvestment ${(p.reinvestment_pool_pct*100).toFixed(0)}%</span><span>owner dist. ${(p.owner_distribution_pct*100).toFixed(0)}%</span><span>experimental ${(p.experimental_capital_pct*100).toFixed(0)}%</span></div></div>`:'<div class="empty">Reserve policy not configured yet -- all percentages default to 0.</div>';
const ec=await api('/api/cc/experimental-capital');$('ccExpCapital').textContent='$'+ec.available;
}
$('rpSave').onclick=async()=>{const pct=v=>v===''?0:parseFloat(v)/100;await api('/api/cc/reserve-policy',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({operating_reserve_pct:pct($('rpOperating').value),tax_reserve_pct:pct($('rpTax').value),emergency_reserve_pct:pct($('rpEmergency').value),reinvestment_pool_pct:pct($('rpReinvestment').value),owner_distribution_pct:pct($('rpOwnerDist').value),experimental_capital_pct:pct($('rpExperimental').value)})});await loadCcCapital()};
$('caRecommend').onclick=async()=>{const amount=parseFloat($('caAvailable').value);if(isNaN(amount))return alert('Enter an available amount first.');const r=await api('/api/cc/capital-allocation/recommend',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({available_amount:amount,actor:'Aryan'})});$('ccCapitalRecs').innerHTML=(r.recommendations||[]).length?r.recommendations.map(rec=>`<div class="item"><h3>${esc(rec.category)}: $${esc(rec.amount)}</h3><div class="meta"><span>confidence ${esc(rec.confidence)}</span><span>${esc(rec.risk)}</span></div><div>${esc(rec.reason)}</div><div class="meta"><span>${esc(rec.expected_benefit)}</span></div><div class="meta"><span>alternative: ${esc(rec.alternative)}</span></div></div>`).join(''):'<div class="empty">No recommendations for the current state.</div>'};
async function loadCcDeptPerf(){const dp=await api('/api/cc/department-performance');const depts=[['Sales',dp.sales],['Delivery',dp.delivery],['Digital Workforce',dp.digital_workforce],['Media/Growth',dp.media_growth],['Falguna Engineering',dp.falguna_engineering]];
$('ccDeptPerfList').innerHTML=depts.map(([name,m])=>`<div class="section"><h2>${esc(name)}</h2><div class="list">${Object.entries(m).map(([k,v])=>renderMetric(k,v)).join('')}</div></div>`).join('');
const wf=await api('/api/cc/ai-workforce-performance');const workers=Object.entries(wf.by_worker||{});
$('ccWorkforcePerfList').innerHTML=workers.length?workers.map(([name,w])=>`<div class="item"><h3>${esc(name)}</h3><div class="meta"><span>tasks ${esc(w.tasks_total)}</span><span>success ${w.success_rate===null?'—':(w.success_rate*100).toFixed(1)+'%'}</span><span>failure ${w.failure_rate===null?'—':(w.failure_rate*100).toFixed(1)+'%'}</span><span>retry ${w.retry_rate===null?'—':(w.retry_rate*100).toFixed(1)+'%'}</span><span>intervention ${w.intervention_rate===null?'—':(w.intervention_rate*100).toFixed(1)+'%'}</span><span>avg duration ${w.average_duration_seconds===null?'—':esc(w.average_duration_seconds)+'s'}</span><span>cost ${w.approximate_cost===null?'—':'$'+esc(w.approximate_cost)}</span></div></div>`).join(''):'<div class="empty">No workforce tasks yet.</div>';
}
async function loadCcRiskRegister(){const d=await api('/api/cc/risks');const items=d.items||[];$('ccRiskRegisterList').innerHTML=items.length?items.map(r=>`<div class="item">
<h3>${esc(r.title)} <span style="color:var(--muted);font-weight:400">(${esc(r.status)})</span></h3>
<div class="meta"><span>${esc(r.category)}</span><span>severity ${esc(r.severity)}</span><span>likelihood ${esc(r.likelihood_band)}</span>${r.owner?`<span>owner ${esc(r.owner)}</span>`:''}</div>
${r.mitigation?`<div>${esc(r.mitigation)}</div>`:''}
<div class="actions">
<button class="secondary mitigating" data-id="${esc(r.id)}">Mitigating</button>
<button class="secondary monitoring" data-id="${esc(r.id)}">Monitoring</button>
<button class="danger closed" data-id="${esc(r.id)}">Closed</button>
</div>
</div>`).join(''):'<div class="empty">No risks logged yet.</div>';
['mitigating','monitoring','closed'].forEach(cls=>{document.querySelectorAll('#ccRiskRegisterList .'+cls).forEach(b=>b.onclick=async()=>{await api(`/api/cc/risks/${b.dataset.id}/status`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({status:cls.toUpperCase(),actor:'Aryan'})});await loadCcRiskRegister()})});
}
$('rkCreate').onclick=async()=>{const title=$('rkTitle').value.trim();if(!title)return alert('Title is required.');await api('/api/cc/risks',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title,category:$('rkCategory').value,severity:$('rkSeverity').value,likelihood_band:$('rkLikelihood').value,owner:$('rkOwner').value||null,mitigation:$('rkMitigation').value||null,evidence:$('rkEvidence').value||null,actor:'Aryan'})});$('rkTitle').value='';$('rkOwner').value='';$('rkMitigation').value='';$('rkEvidence').value='';await loadCcRiskRegister()};
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
$('ccGenerateBrief').onclick=async()=>{const b=await api('/api/cc/ceo-brief/generate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({actor:'Aryan'})});renderCeoBrief(b)};
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
let rhEditId=null;
let rhQuickFilterVal='all';
const RH_REVIEWED_STAGES=new Set(['Applied/Sent','Replied','Meeting','Negotiating','Won','Lost']);
if($('rhStageFilter').children.length<2)RH_STAGES.forEach(s=>{const o=document.createElement('option');o.value=s;o.textContent=s;$('rhStageFilter').appendChild(o)});
$('rhMode').onchange=()=>{const m=$('rhMode').value;$('rhModeManual').style.display=m==='manual'?'':'none';$('rhModePasteJd').style.display=m==='paste_jd'?'':'none';$('rhModeUrl').style.display=m==='url'?'':'none';$('rhModeCsv').style.display=m==='csv_json'?'':'none'};
$('rhStageFilter').onchange=()=>loadRhOpportunities();
$('rhSourceFilter').onchange=()=>loadRhOpportunities();
$('rhMinScore').oninput=()=>loadRhOpportunities();
$('rhMaxAgeDays').oninput=()=>loadRhOpportunities();
document.querySelectorAll('.rhQuickFilter').forEach(b=>b.onclick=()=>{rhQuickFilterVal=b.dataset.filter;document.querySelectorAll('.rhQuickFilter').forEach(x=>x.classList.remove('active'));b.classList.add('active');loadRhOpportunities()});
$('rhDiscoverNow').onclick=async()=>{
$('rhDiscoverNow').disabled=true;
$('rhDiscoverStatus').innerHTML='<div class="empty">Running discovery -- Remotive and WeWorkRemotely only; unavailable sources are reported, never faked...</div>';
try{
const r=await api('/api/rh/discover',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
$('rhDiscoverStatus').innerHTML=`<div class="item"><div class="meta"><span>found ${r.opportunities_found}</span><span>new ${r.opportunities_new}</span><span>duplicates ${r.opportunities_duplicate}</span><span>filtered ${r.opportunities_filtered||0}</span><span>invalid ${r.opportunities_invalid||0}</span></div>${(r.providers||[]).map(p=>`<div class="contrib"><b>${esc(p.provider)}:</b> ${p.available===false?'unavailable -- '+esc(p.error||''):(p.error?'error -- '+esc(p.error):`found ${p.found}, new ${p.new}, duplicates ${p.duplicates}, filtered ${p.filtered||0}, invalid ${p.invalid||0}`)}</div>`).join('')}</div>`;
await loadRhOpportunities();
}catch(e){
$('rhDiscoverStatus').innerHTML=`<div class="empty">Discovery failed: ${esc(e.message)}</div>`;
}finally{
$('rhDiscoverNow').disabled=false;
}
};
$('rhCreate').onclick=async()=>{
const m=$('rhMode').value;let body;
if(m==='paste_jd'){if(!$('rhJdTitle').value.trim())return alert('Title is required.');if(!$('rhJdText').value.trim())return alert('Paste the job description text first.');body={import_mode:'paste_jd',title:$('rhJdTitle').value.trim(),client_name:$('rhJdClient').value.trim()||null,text:$('rhJdText').value}}
else if(m==='url'){if(!$('rhUrlTitle').value.trim()||!$('rhUrlValue').value.trim())return alert('Title and URL are required.');body={import_mode:'url',title:$('rhUrlTitle').value.trim(),url:$('rhUrlValue').value.trim(),description:$('rhUrlDescription').value||null}}
else if(m==='csv_json'){let rows;try{rows=JSON.parse($('rhCsvJson').value)}catch(e){return alert('That is not valid JSON.')}body={import_mode:'csv_json',rows}}
else{if(!$('rhTitle').value.trim())return alert('Title is required.');body={import_mode:'manual',title:$('rhTitle').value.trim(),client_name:$('rhClient').value.trim()||null,description:$('rhDescription').value||null,budget_rate:$('rhBudget').value||null,required_skills:$('rhSkills').value||null,deadline:$('rhDeadline').value||null,contract_type:$('rhContractType').value||null,location_timezone:$('rhLocation').value||null,urgency:$('rhUrgency').value||null}}
try{await api('/api/rh/opportunities',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});['rhTitle','rhClient','rhDescription','rhBudget','rhSkills','rhDeadline','rhContractType','rhLocation','rhUrgency','rhJdTitle','rhJdClient','rhJdText','rhUrlTitle','rhUrlValue','rhUrlDescription','rhCsvJson'].forEach(id=>{if($(id))$(id).value=''});await loadRhOpportunities()}catch(e){alert(e.message)}
};
async function loadRhToday(){const d=await api('/api/rh/dashboard');$('rhPipelineValue').textContent='$'+d.pipeline_value;$('rhWonRevenue').textContent='$'+d.won_revenue;$('rhFoundToday').textContent=d.opportunities_discovered_today||0;$('rhAgingCount').textContent=(d.aging_opportunities||[]).length;
const lr=d.last_discovery_run;$('rhLastRun').innerHTML=lr?`<div class="item"><div class="meta"><span>${esc(lr.started_at||'')}</span><span>found ${lr.opportunities_found}</span><span>new ${lr.opportunities_new}</span><span>duplicates ${lr.opportunities_duplicate}</span><span>filtered ${lr.opportunities_filtered||0}</span><span>invalid ${lr.opportunities_invalid||0}</span></div>${(lr.providers||[]).map(p=>`<div class="contrib"><b>${esc(p.provider)}:</b> ${p.available===false?'unavailable -- '+esc(p.error||''):(p.error?'error -- '+esc(p.error):`found ${p.found}, new ${p.new}, duplicates ${p.duplicates}, filtered ${p.filtered||0}, invalid ${p.invalid||0}`)}</div>`).join('')}</div>`:'<div class="empty">No discovery run yet -- try "Find Opportunities Now" on Opportunities.</div>';
$('rhNextActions').innerHTML=d.next_actions.length?d.next_actions.map(a=>`<div class="item"><h3>${esc(a.title||'')}</h3><div class="meta"><span>${esc(a.type)}</span></div><div>${esc(a.why||'')}</div></div>`).join(''):'<div class="empty">Nothing urgent right now.</div>'
$('todayWfBlocked').innerHTML=(d.workforce_blocked_tasks||[]).length?d.workforce_blocked_tasks.map(t=>`<div class="item"><h3>${esc(t.objective)}</h3><div class="meta"><span>${esc(t.status)}</span><span>${esc(t.task_type)}</span><span>${esc(t.department)}</span></div></div>`).join(''):'<div class="empty">No blocked workforce tasks.</div>'
$('todayMediaApproval').innerHTML=(d.media_pending_approval||[]).length?d.media_pending_approval.map(i=>`<div class="item"><h3>${esc(i.title)}</h3><div class="meta"><span>${esc(i.kind)}</span></div></div>`).join(''):'<div class="empty">Nothing pending.</div>'
$('todayContentDue').innerHTML=(d.content_due_soon||[]).length?d.content_due_soon.map(c=>`<div class="item"><h3>${esc(c.title)}</h3><div class="meta"><span>${esc(c.content_state)}</span><span>due ${esc(c.planned_publish_date)}</span></div></div>`).join(''):'<div class="empty">Nothing due soon.</div>'
$('todayPublishFailures').innerHTML=(d.publishing_failures||[]).length?d.publishing_failures.map(p=>`<div class="item"><h3>${esc(p.platform)}</h3><div class="meta"><span>${esc(p.status)}</span></div></div>`).join(''):'<div class="empty">No publishing failures.</div>'
$('todayGrowthSignals').innerHTML=(d.strong_growth_signals||[]).length?d.strong_growth_signals.map(s=>`<div class="item"><div class="meta"><span>content ${esc(s.content_id)}</span></div><div>${esc(s.reasoning||'')}</div></div>`).join(''):'<div class="empty">No strong growth signals yet.</div>'}
function rhAge(o){const t=Date.parse(o.created_at);if(isNaN(t))return null;return Math.max(0,Math.floor((Date.now()-t)/86400000))}
function rhPopulateSourceFilter(items){const cur=$('rhSourceFilter').value;const sources=[...new Set(items.map(o=>o.source).filter(Boolean))].sort();const opts='<option value="">All sources</option>'+sources.map(s=>`<option value="${esc(s)}">${esc(s)}</option>`).join('');if($('rhSourceFilter').innerHTML!==opts)$('rhSourceFilter').innerHTML=opts;$('rhSourceFilter').value=sources.includes(cur)?cur:''}
function rhNextAction(o){const q=o.qualification;if(!q)return'Qualify';if(o.stage==='Won'||o.stage==='Lost')return'-- done --';if(q.recommendation==='IGNORE')return'Review or leave ignored';if(q.recommendation==='PURSUE'&&(o.stage==='New'||o.stage==='Qualified'||o.stage==='Proposal Ready'))return'Review draft proposal, approve & send';if(q.recommendation==='MAYBE')return'Decide: pursue or ignore';if(o.stage==='Applied/Sent')return'Await reply / send follow-up';if(o.stage==='Replied')return'Schedule meeting';if(o.stage==='Meeting'||o.stage==='Negotiating')return'Move to Won or Lost';return'-'}
async function loadRhOpportunities(){
const d=await api('/api/rh/opportunities');
const items=d.items||[];
rhPopulateSourceFilter(items);
const stage=$('rhStageFilter').value;
const source=$('rhSourceFilter').value;
const minScore=$('rhMinScore').value?Number($('rhMinScore').value):null;
const maxAgeDays=$('rhMaxAgeDays').value?Number($('rhMaxAgeDays').value):null;
const filtered=items.filter(o=>{
const q=o.qualification;
if(stage&&o.stage!==stage)return false;
if(source&&o.source!==source)return false;
if(minScore!=null&&(!q||q.fit_score<minScore))return false;
const age=rhAge(o);
if(maxAgeDays!=null&&(age==null||age>maxAgeDays))return false;
if(rhQuickFilterVal==='new'&&o.stage!=='New')return false;
if(rhQuickFilterVal==='pursue'&&(!q||q.recommendation!=='PURSUE'))return false;
if(rhQuickFilterVal==='maybe'&&(!q||q.recommendation!=='MAYBE'))return false;
if(rhQuickFilterVal==='ignored'&&(!q||q.recommendation!=='IGNORE'))return false;
if(rhQuickFilterVal==='reviewed'&&!RH_REVIEWED_STAGES.has(o.stage))return false;
return true;
});
renderRhOpportunities(filtered);
}
function rhCard(o){const q=o.qualification;const age=rhAge(o);return `<div class="item"><h3>${esc(o.title)}</h3><div class="meta"><span>${esc(o.stage)}</span>${o.source?`<span>src: ${esc(o.source)}</span>`:''}${age!=null?`<span>${age}d old</span>`:''}${o.client_name?`<span>${esc(o.client_name)}</span>`:''}${o.budget_rate?`<span>${esc(o.budget_rate)}</span>`:''}${q?`<span>score ${q.fit_score}</span><span><b>${esc(q.recommendation)}</b></span>${q.recommendation_detail==='PURSUE_WITH_BUDGET_UNKNOWN'?'<span class="badge">Budget unknown</span>':''}${q.suggested_price?`<span>sugg. ${esc(q.suggested_price)}</span>`:''}${q.portfolio_match?`<span>match: ${esc(q.portfolio_match)}</span>`:''}`:'<span>not qualified</span>'}</div><div class="contrib"><b>Next:</b> ${esc(rhNextAction(o))}</div><div class="actions"><button class="secondary rhOpen" data-id="${esc(o.id)}">Open</button></div></div>`}
function renderRhOpportunities(items){$('rhOpportunityList').innerHTML=items.length?items.map(o=>rhOpenId===o.id?rhDetailCard(o):rhCard(o)).join(''):'<div class="empty">No opportunities yet.</div>';wireRhList()}
function wireRhList(){
document.querySelectorAll('.rhOpen').forEach(b=>b.onclick=async()=>{rhOpenId=b.dataset.id;rhEditId=null;await loadRhOpportunities()});
document.querySelectorAll('.rhClose').forEach(b=>b.onclick=async()=>{rhOpenId=null;rhEditId=null;await loadRhOpportunities()});
document.querySelectorAll('.rhEditToggle').forEach(b=>b.onclick=async()=>{rhEditId=rhEditId===b.dataset.id?null:b.dataset.id;await loadRhOpportunities()});
document.querySelectorAll('.rhEditCancel').forEach(b=>b.onclick=async()=>{rhEditId=null;await loadRhOpportunities()});
document.querySelectorAll('.rhEditSave').forEach(b=>b.onclick=async()=>{const id=b.dataset.id;const title=$(`rhEditTitle_${id}`).value.trim();if(!title)return alert('Title cannot be blank.');const body={title,client_name:$(`rhEditClient_${id}`).value||null,description:$(`rhEditDescription_${id}`).value||null,budget_rate:$(`rhEditBudget_${id}`).value||null,required_skills:$(`rhEditSkills_${id}`).value||null,deadline:$(`rhEditDeadline_${id}`).value||null,contract_type:$(`rhEditContractType_${id}`).value||null,location_timezone:$(`rhEditLocation_${id}`).value||null,urgency:$(`rhEditUrgency_${id}`).value||null};try{await api(`/api/rh/opportunities/${id}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});rhEditId=null;await loadRhOpportunities()}catch(e){alert(e.message)}});
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
const age=rhAge(o);
return `<div class="item">
<h3>${esc(o.title)}</h3>
<div class="meta"><span>${esc(o.stage)}</span>${o.source?`<span>source: ${esc(o.source)}</span>`:''}${age!=null?`<span>${age}d old</span>`:''}${o.client_name?`<span>${esc(o.client_name)}</span>`:''}${o.budget_rate?`<span>${esc(o.budget_rate)}</span>`:''}${o.deadline?`<span>due ${esc(o.deadline)}</span>`:''}</div>
${o.source_url?`<div class="contrib"><b>Source link:</b> <a href="${esc(o.source_url)}" target="_blank" rel="noopener">${esc(o.source_url)}</a></div>`:''}
${o.description&&rhEditId!==o.id?`<div>${esc(o.description)}</div>`:''}
${rhEditId===o.id?`<div class="form">
<div class="row"><input id="rhEditTitle_${esc(o.id)}" placeholder="Title" value="${esc(o.title||'')}"><input id="rhEditClient_${esc(o.id)}" placeholder="Client name" value="${esc(o.client_name||'')}"></div>
<textarea id="rhEditDescription_${esc(o.id)}" placeholder="Description">${esc(o.description||'')}</textarea>
<div class="row"><input id="rhEditBudget_${esc(o.id)}" placeholder="Budget/rate" value="${esc(o.budget_rate||'')}"><input id="rhEditSkills_${esc(o.id)}" placeholder="Required skills" value="${esc(o.required_skills||'')}"></div>
<div class="row"><input id="rhEditDeadline_${esc(o.id)}" placeholder="Deadline" value="${esc(o.deadline||'')}"><input id="rhEditContractType_${esc(o.id)}" placeholder="Contract type" value="${esc(o.contract_type||'')}"></div>
<div class="row"><input id="rhEditLocation_${esc(o.id)}" placeholder="Location/timezone" value="${esc(o.location_timezone||'')}"><input id="rhEditUrgency_${esc(o.id)}" placeholder="Urgency" value="${esc(o.urgency||'')}"></div>
<div class="actions"><button class="rhEditSave" data-id="${esc(o.id)}">Save changes</button><button class="secondary rhEditCancel" data-id="${esc(o.id)}">Cancel</button></div>
</div>`:''}
<div class="contrib"><b>Qualification:</b> ${q?`fit ${q.fit_score}/100, budget ${esc(q.budget_quality)}, <b>${esc(q.recommendation)}</b>${q.recommendation_detail==='PURSUE_WITH_BUDGET_UNKNOWN'?' <span class="badge">Budget unknown</span>':''}, price ${esc(q.suggested_price)}, timeline ${esc(q.suggested_timeline)}${q.risk_flags?`, risks: ${esc(q.risk_flags)}`:''}`:'not qualified yet'}</div>
${q?`<div class="contrib"><b>Relevance gate:</b> ${q.relevance_passed?'passed':'<b>failed -- forced IGNORE regardless of score</b>'}${q.relevance_exclusion_signals?`, exclusion signals: ${esc(q.relevance_exclusion_signals)}`:''}${q.relevance_positive_signals?`, positive signals: ${esc(q.relevance_positive_signals)}`:''}</div>`:''}
${q?`<div class="contrib"><b>Budget source:</b> ${esc(q.budget_source||(q.budget_amount!=null?'client-stated':'unknown (TTT estimate only)'))}</div>`:''}
${q&&q.strongest_technical_match?`<div class="contrib"><b>Strongest technical match:</b> ${esc(q.strongest_technical_match)}</div>`:''}
${q&&q.biggest_risk?`<div class="contrib"><b>Biggest risk:</b> ${esc(q.biggest_risk)}</div>`:''}
${q&&q.estimated_project_value?`<div class="contrib"><b>Estimated project value:</b> ${esc(q.estimated_project_value)}</div>`:''}
${q&&q.capability_gaps?`<div class="contrib"><b>Capability gaps:</b> ${esc(q.capability_gaps)}</div>`:''}
${q&&q.why?`<div class="contrib"><b>Why:</b> ${esc(q.why)}</div>`:''}
<div class="actions">
<button class="secondary rhEditToggle" data-id="${esc(o.id)}">${rhEditId===o.id?'Hide edit':'Edit'}</button>
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
${(o.research||[]).length?`<div class="contrib"><b>Research (client/company context -- untrusted input, cited)</b></div>`+(o.research||[]).map(r=>`<div class="contrib">${esc(r.answer||'')}${(r.sources||[]).length?'<br><i>Sources: '+(r.sources||[]).map(s=>`<a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.title||s.domain||s.url)}</a>`).join(', ')+'</i>':''}</div>`).join(''):''}
</div>`;
}
async function loadRhPipeline(){const d=await api('/api/rh/opportunities');const items=d.items||[];const byStage={};RH_STAGES.forEach(s=>byStage[s]=[]);items.forEach(o=>{(byStage[o.stage]||(byStage[o.stage]=[])).push(o)});
$('rhPipelineBoard').innerHTML=RH_STAGES.map(s=>`<div class="section"><h2>${s} (${(byStage[s]||[]).length})</h2><div class="list">${(byStage[s]||[]).length?(byStage[s]||[]).map(o=>`<div class="item"><h3>${esc(o.title)}</h3><div class="meta">${o.client_name?`<span>${esc(o.client_name)}</span>`:''}${o.budget_rate?`<span>${esc(o.budget_rate)}</span>`:''}${o.deadline?`<span>due ${esc(o.deadline)}</span>`:''}</div></div>`).join(''):'<div class="empty">Empty</div>'}</div></div>`).join('')}
async function loadRhClients(){const d=await api('/api/rh/opportunities');const items=d.items||[];const byClient={};items.forEach(o=>{const key=o.client_name||'(no client name)';(byClient[key]=byClient[key]||[]).push(o)});
const rows=Object.entries(byClient);
$('rhClientsList').innerHTML=rows.length?rows.map(([client,opps])=>{const won=opps.filter(o=>o.stage==='Won');const revenue=won.reduce((sum,o)=>sum+(o.final_price||0),0);return `<div class="item"><h3>${esc(client)}</h3><div class="meta"><span>${opps.length} opportunit${opps.length===1?'y':'ies'}</span><span>${won.length} won</span><span>$${revenue} revenue</span></div></div>`}).join(''):'<div class="empty">No clients yet.</div>'}
async function loadRhActiveJobs(){const d=await api('/api/rh/active-jobs');renderRhActiveJobs(d.items||[])}
function renderRhActiveJobs(items){$('rhActiveJobsList').innerHTML=items.length?items.map(j=>{let p={};try{p=JSON.parse(j.job_payload_json||'{}')}catch(e){}return `<div class="item"><h3>${esc(p.title||('Active Job '+j.id))}</h3><div class="meta"><span>${esc(j.handoff_status)}</span>${j.mission_id?`<span>mission ${esc(j.mission_id)}</span>`:''}${p.client_name?`<span>${esc(p.client_name)}</span>`:''}${p.price!=null?`<span>$${esc(String(p.price))}</span>`:''}${p.deadline?`<span>due ${esc(p.deadline)}</span>`:''}</div>${p.requirement?`<div class="contrib"><b>Scope:</b> ${esc(p.requirement)}</div>`:''}${p.deliverables?`<div class="contrib"><b>Deliverables:</b> ${esc(p.deliverables)}</div>`:''}${p.notes?`<div class="contrib"><b>Notes:</b> ${esc(p.notes)}</div>`:''}${j.handoff_status!=='HANDED_OFF'?`<div class="form"><input class="rhRepoInput" data-id="${esc(j.id)}" placeholder="Local git repository path for the target job"><div class="actions"><button class="secondary rhHandoff" data-id="${esc(j.id)}">Trigger Falguna handoff</button></div></div>`:''}</div>`}).join(''):'<div class="empty">No Active Jobs yet -- create one from a Won opportunity.</div>';
document.querySelectorAll('.rhHandoff').forEach(b=>b.onclick=async()=>{const repo=document.querySelector(`.rhRepoInput[data-id="${b.dataset.id}"]`).value.trim();if(!repo)return alert('Enter the target repository path first.');try{await api(`/api/rh/active-jobs/${b.dataset.id}/handoff`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({repository:repo})});await loadRhActiveJobs()}catch(e){alert(e.message)}})}
async function loadRhRevenue(){const a=await api('/api/rh/analytics');$('rhAnalytics').innerHTML=`<div class="item"><div class="meta">
<span>Added: ${a.opportunities_added}</span><span>Qualified: ${a.qualified}</span><span>Proposals: ${a.proposals_created}</span><span>Sent: ${a.proposals_sent}</span>
<span>Replies: ${a.replies}</span><span>Meetings: ${a.meetings}</span><span>Wins: ${a.wins}</span><span>Losses: ${a.losses}</span>
<span>Conversion: ${Math.round(a.conversion_rate*100)}%</span><span>Pipeline value: $${a.pipeline_value}</span><span>Won revenue: $${a.won_revenue}</span>
</div></div>`+Object.entries(a.source_performance||{}).map(([src,s])=>`<div class="item"><h3>${esc(src)}</h3><div class="meta"><span>added ${s.added}</span><span>won ${s.won}</span><span>lost ${s.lost}</span></div></div>`).join('')}
async function loadRhSettings(){
const p=await api('/api/rh/acquisition-profile');
$('rhSetServices').value=(p.services||[]).join('\n');
$('rhSetSkills').value=(p.skills||[]).join(', ');
$('rhSetMinBudget').value=p.min_budget_usd!=null?p.min_budget_usd:'';
$('rhSetMaxAge').value=p.max_age_days!=null?p.max_age_days:'';
$('rhSetRemote').value=p.remote_preference||'remote_ok';
$('rhSetCountries').value=(p.target_countries||[]).join(', ');
$('rhSetKeywords').value=(p.keywords||[]).join(', ');
$('rhSetExcluded').value=(p.excluded_keywords||[]).join(', ');
$('rhSetPositiveSignals').value=(p.positive_service_signals||[]).join('\n');
$('rhSetExclusionSignals').value=(p.exclusion_role_signals||[]).join('\n');
const sourceSettings=p.source_settings||{};
$('rhSetSources').innerHTML=Object.keys(sourceSettings).length?Object.entries(sourceSettings).map(([name,cfg])=>`<label style="display:flex;gap:8px;align-items:center;padding:4px 0"><input type="checkbox" class="rhSetSourceToggle" data-source="${esc(name)}" ${cfg&&cfg.enabled?'checked':''}> ${esc(name)}</label>`).join(''):'<div class="empty">No sources configured.</div>';
}
$('rhSetSave').onclick=async()=>{
const services=$('rhSetServices').value.split('\n').map(s=>s.trim()).filter(Boolean);
const skills=$('rhSetSkills').value.split(',').map(s=>s.trim()).filter(Boolean);
const targetCountries=$('rhSetCountries').value.split(',').map(s=>s.trim()).filter(Boolean);
const keywords=$('rhSetKeywords').value.split(',').map(s=>s.trim()).filter(Boolean);
const excludedKeywords=$('rhSetExcluded').value.split(',').map(s=>s.trim()).filter(Boolean);
const positiveServiceSignals=$('rhSetPositiveSignals').value.split('\n').map(s=>s.trim()).filter(Boolean);
const exclusionRoleSignals=$('rhSetExclusionSignals').value.split('\n').map(s=>s.trim()).filter(Boolean);
const sourceSettings={};
document.querySelectorAll('.rhSetSourceToggle').forEach(cb=>{sourceSettings[cb.dataset.source]={enabled:cb.checked}});
const body={
services, skills,
min_budget_usd:$('rhSetMinBudget').value?Number($('rhSetMinBudget').value):null,
max_age_days:$('rhSetMaxAge').value?Number($('rhSetMaxAge').value):null,
remote_preference:$('rhSetRemote').value,
target_countries:targetCountries,
keywords, excluded_keywords:excludedKeywords,
positive_service_signals:positiveServiceSignals,
exclusion_role_signals:exclusionRoleSignals,
source_settings:sourceSettings,
};
try{
await api('/api/rh/acquisition-profile',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
alert('Acquisition profile saved.');
await loadRhSettings();
}catch(e){alert(e.message)}
};
$('rhRequalifyAll').onclick=async()=>{
$('rhRequalifyAll').disabled=true;
$('rhRequalifyResult').innerHTML='<div class="empty">Requalifying every non-terminal opportunity against the current profile...</div>';
try{
const r=await api('/api/rh/requalify',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
const d=r.distribution||{};
$('rhRequalifyResult').innerHTML=`<div class="item"><div class="meta"><span>requalified ${r.requalified}</span><span>PURSUE ${d.PURSUE||0}</span><span>MAYBE ${d.MAYBE||0}</span><span>IGNORE ${d.IGNORE||0}</span></div>
<div class="contrib"><b>Downgraded from PURSUE:</b> ${(r.downgraded_from_pursue||[]).length}</div>
<div class="contrib"><b>Upgraded to PURSUE:</b> ${(r.upgraded_to_pursue||[]).length}</div>
<div class="contrib"><b>Superseded proposals:</b> ${(r.superseded_proposal_ids||[]).length}</div>
<div class="contrib"><b>Rejected stale Needs Aryan items:</b> ${(r.rejected_needs_aryan_ids||[]).length}</div>
</div>`;
await loadRhOpportunities();
}catch(e){
$('rhRequalifyResult').innerHTML=`<div class="empty">Requalification failed: ${esc(e.message)}</div>`;
}finally{
$('rhRequalifyAll').disabled=false;
}
};

function smItem(title,metaParts){return `<div class="item"><h3>${esc(title)}</h3><div class="meta">${(metaParts||[]).filter(Boolean).map(m=>`<span>${esc(m)}</span>`).join('')}</div></div>`}
async function loadRhSalesManager(){
const o=await api('/api/rh/sales-manager/overview');
const att=o.attention||{};
$('smAttention').innerHTML=`<div class="item"><div class="meta"><span>${(att.needs_aryan_pending||[]).length} pending decisions</span><span>${(att.blocked_deliveries||[]).length} blocked deliveries</span><span>${(att.overdue_invoices||[]).length} overdue invoices</span></div></div>`;
$('smBestLeads').innerHTML=(o.best_leads||[]).length?o.best_leads.map(l=>smItem(l.title,[l.priority+' priority',l.recommendation,l.suggested_price,l.stage])).join(''):'<div class="empty">No active leads yet.</div>';
$('smWhoReplied').innerHTML=(o.who_replied||[]).length?o.who_replied.map(m=>smItem(m.body?m.body.slice(0,80):'(message)',[m.intent,m.channel])).join(''):'<div class="empty">No open replies.</div>';
$('smNegotiations').innerHTML=(o.negotiations_needing_action||[]).length?o.negotiations_needing_action.map(i=>smItem(i.title,[i.kind])).join(''):'<div class="empty">None right now.</div>';
$('smCloseSoon').innerHTML=(o.can_close_soon||[]).length?o.can_close_soon.map(c=>smItem(c.title,['within policy'])).join(''):'<div class="empty">Nothing flagged yet.</div>';
$('smOnboarding').innerHTML=(o.onboarding_clients||[]).length?o.onboarding_clients.map(c=>smItem(c.title,[`${(c.onboarding_completeness||{}).received||0}/${(c.onboarding_completeness||{}).total||0} received`])).join(''):'<div class="empty">No one onboarding right now.</div>';
$('smBlockedDeliveries').innerHTML=(o.blocked_deliveries||[]).length?o.blocked_deliveries.map(d=>smItem(d.title||d.active_job_id,[d.blocker_reason])).join(''):'<div class="empty">Nothing blocked.</div>';
$('smOverdueInvoices').innerHTML=(o.overdue_invoices||[]).length?o.overdue_invoices.map(i=>smItem('Invoice '+i.id,['$'+i.amount+' '+(i.currency||''),'due '+(i.due_date||'')])).join(''):'<div class="empty">Nothing overdue.</div>';
$('smUpsell').innerHTML=(o.upsell_ready_clients||[]).length?o.upsell_ready_clients.map(c=>smItem(c.client_name,[`${(c.due_items||[]).length} due follow-up(s)`])).join(''):'<div class="empty">No upsell follow-ups due.</div>';
}
async function loadRhOutboundLeads(){const d=await api('/api/rh/outbound-leads');renderRhOutboundLeads(d.items||[])}
function renderRhOutboundLeads(items){
$('rhOutboundLeadsList').innerHTML=items.length?items.map(l=>`<div class="item"><h3>${esc(l.company_name)}</h3><div class="meta"><span>${esc(l.status)}</span>${l.confidence?`<span>${esc(l.confidence)} confidence</span>`:''}${l.website?`<span>${esc(l.website)}</span>`:''}</div>${l.likely_need?`<div class="contrib"><b>Likely need:</b> ${esc(l.likely_need)}</div>`:''}${l.relevance_notes?`<div class="contrib"><b>Why:</b> ${esc(l.relevance_notes)}</div>`:''}<div class="form"><textarea class="obDraftMsg" data-id="${esc(l.id)}" placeholder="Draft outreach message (nothing is ever sent automatically)"></textarea><input class="obDraftChannel" data-id="${esc(l.id)}" placeholder="Channel (e.g. business email)"><div class="actions"><button class="secondary obDraft" data-id="${esc(l.id)}">Prepare outreach draft</button></div></div></div>`).join(''):'<div class="empty">No outbound leads yet -- add one researched prospect at a time.</div>';
document.querySelectorAll('.obDraft').forEach(b=>b.onclick=async()=>{
const msg=document.querySelector(`.obDraftMsg[data-id="${b.dataset.id}"]`).value.trim();
const channel=document.querySelector(`.obDraftChannel[data-id="${b.dataset.id}"]`).value.trim()||'email';
if(!msg)return alert('Write the draft message first.');
try{await api(`/api/rh/outbound-leads/${b.dataset.id}/outreach`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({channel,message:msg})});alert('Draft prepared -- review it in Needs Aryan before sending it yourself.');await loadRhOutboundLeads()}catch(e){alert(e.message)}
});
}
$('obCreate').onclick=async()=>{
try{
await api('/api/rh/outbound-leads',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
company_name:$('obCompany').value.trim(),website:$('obWebsite').value.trim()||null,contact_name:$('obContact').value.trim()||null,
likely_need:$('obLikelyNeed').value.trim()||null,proposed_offer:$('obOffer').value.trim()||null,
confidence:$('obConfidence').value||null,source:$('obSource').value.trim()||null,relevance_notes:$('obNotes').value.trim()||null,
})});
$('obCompany').value='';$('obWebsite').value='';$('obContact').value='';$('obLikelyNeed').value='';$('obOffer').value='';$('obConfidence').value='';$('obSource').value='';$('obNotes').value='';
await loadRhOutboundLeads();
}catch(e){alert(e.message)}
};

// ---------- Digital Workforce ----------
async function loadWfTasks(){const d=await api('/api/wf/tasks');$('wfTasksList').innerHTML=(d.items||[]).length?d.items.map(t=>`<div class="item"><h3>${esc(t.objective)}</h3><div class="meta"><span>${esc(t.status)}</span><span>${esc(t.task_type)}</span><span>${esc(t.department)}</span>${t.assigned_worker?`<span>${esc(t.assigned_worker)}</span>`:''}${t.retries?`<span>${t.retries} retr${t.retries===1?'y':'ies'}</span>`:''}</div></div>`).join(''):'<div class="empty">No workforce tasks yet.</div>'}
async function loadWfWorkflows(){const d=await api('/api/wf/recurring-workflows');$('wfWorkflowsList').innerHTML=(d.items||[]).length?d.items.map(w=>`<div class="item"><h3>${esc(w.name)}</h3><div class="meta"><span>${esc(w.status)}</span><span>${esc(w.schedule_kind)}</span><span>${esc(w.department)}</span>${w.next_due_at?`<span>next due ${esc(w.next_due_at)}</span>`:''}</div></div>`).join(''):'<div class="empty">No recurring workflows yet.</div>'}
// ---------- Media / Growth ----------
async function loadMediaBrands(){const d=await api('/api/media/brands');$('mediaBrandsList').innerHTML=(d.items||[]).length?d.items.map(b=>`<div class="item"><h3>${esc(b.name)}</h3><div class="meta">${b.voice_tone?`<span>${esc(b.voice_tone)}</span>`:''}${b.audience?`<span>${esc(b.audience)}</span>`:''}</div></div>`).join(''):'<div class="empty">No brands yet.</div>'}
async function loadMediaContent(){const d=await api('/api/media/content');$('mediaContentList').innerHTML=(d.items||[]).length?d.items.map(c=>`<div class="item"><h3>${esc(c.title)}</h3><div class="meta"><span>${esc(c.content_state)}</span><span>${esc(c.format)}</span>${c.platform?`<span>${esc(c.platform)}</span>`:''}${c.planned_publish_date?`<span>due ${esc(c.planned_publish_date)}</span>`:''}</div></div>`).join(''):'<div class="empty">No content items yet.</div>'}
async function loadMediaPublications(){const d=await api('/api/media/publications');$('mediaPublicationsList').innerHTML=(d.items||[]).length?d.items.map(p=>`<div class="item"><h3>${esc(p.platform)}</h3><div class="meta"><span>${esc(p.status)}</span>${p.execution_mode?`<span>${esc(p.execution_mode)}</span>`:''}${p.published_at?`<span>published ${esc(p.published_at)}</span>`:''}</div></div>`).join(''):'<div class="empty">No publications yet.</div>'}
async function loadMediaExperiments(){const d=await api('/api/media/experiments');$('mediaExperimentsList').innerHTML=(d.items||[]).length?d.items.map(x=>`<div class="item"><h3>${esc(x.hypothesis)}</h3><div class="meta"><span>${esc(x.status)}</span>${x.variable_tested?`<span>${esc(x.variable_tested)}</span>`:''}</div>${x.decision?`<div class="contrib"><b>Decision:</b> ${esc(x.decision)}</div>`:''}</div>`).join(''):'<div class="empty">No growth experiments yet.</div>'}

loadAll().catch(e=>{$('boardroomList').innerHTML=`<div class="empty">Unable to load: ${esc(e.message)}</div>`});
</script></body></html>'''
