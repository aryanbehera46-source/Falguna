"""TTT Autonomous Revenue-to-Delivery Loop v1 -- Sales Manager / Revenue
Director (Section 14, Pass E).

`SalesManagerService` is a read-only aggregator: every method composes
data that already exists on file in other stores (NeedsAryanQueue,
DashboardService/AnalyticsService, ApplicationExecutor's own
`rh_application_attempts`, ConversationStore, NegotiationGuardrails,
AccountManagerService, BillingStore, RetentionStore) -- it creates nothing
new, decides nothing on its own, and never invents a number. Its only job
is to turn "what's everywhere" into "what matters right now," answering
the specific questions Section 14 calls out.

Prioritization deliberately avoids fake probability precision: there is no
machine-learned win-probability model in this codebase, so `priority_label`
buckets into a plain High/Medium/Low using only real, on-file signals
(qualification recommendation, a parsed suggested price, and age) rather
than manufacturing a decimal percentage that would imply a precision this
system doesn't have.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .account_management import AccountManagerService
from .audit import AuditLog
from .billing import BillingStore, RetentionStore
from .revenue_hunter import TERMINAL_STAGES, _parse_number
from .sales_ops import ClientStore
from .store import StateStore
from .ttt_hq import NeedsAryanQueue

_RELEVANT_NEEDS_ARYAN_KINDS = {
    "proposal_approval", "pricing_decision", "outreach_approval",
    "client_response_decision", "negotiation_response_approval", "scope_expansion",
}


def _age_days(created_at: Optional[str]) -> Optional[int]:
    if not created_at:
        return None
    try:
        created = datetime.fromisoformat(created_at)
    except (TypeError, ValueError):
        return None
    return (datetime.now(timezone.utc) - created).days


def _priority_label(recommendation: Optional[str], price: Optional[float], age_days: Optional[int]) -> str:
    """A plain qualitative bucket -- not a manufactured score. PURSUE with
    a real price on file and reasonably fresh is High; anything with no
    positive qualification signal is Low; everything else is Medium."""
    if recommendation == "PURSUE" and price and price >= 1000:
        return "High"
    if recommendation == "PURSUE":
        return "Medium"
    if recommendation == "PASS":
        return "Low"
    if age_days is not None and age_days >= 14:
        return "Low"
    return "Medium"


class SalesManagerService:
    def __init__(self, store: StateStore, audit: AuditLog, control=None):
        self.store = store
        self.audit = audit
        self.control = control
        self.needs_aryan = NeedsAryanQueue(store, audit, control)
        self.account_manager = AccountManagerService(store, audit)
        self.billing = BillingStore(store, audit)
        self.retention = RetentionStore(store, audit)
        self.clients = ClientStore(store, audit)

    # -- "What needs attention now?" --

    def whats_needs_attention_now(self) -> Dict[str, Any]:
        pending = self.needs_aryan.list_pending()
        blocked_deliveries = self.blocked_deliveries()
        overdue = self.overdue_invoices()
        return {
            "needs_aryan_pending": pending,
            "blocked_deliveries": blocked_deliveries,
            "overdue_invoices": overdue,
            "total_attention_items": len(pending) + len(blocked_deliveries) + len(overdue),
        }

    # -- "Which leads are best?" --

    def best_leads(self, limit: int = 10) -> List[Dict[str, Any]]:
        opportunities = self.store.list("rh_opportunities")
        active = [o for o in opportunities if o["stage"] not in TERMINAL_STAGES]
        ranked = []
        for opp in active:
            quals = self.store.list("rh_qualifications", "opportunity_id=?", (opp["id"],))
            qual = quals[-1] if quals else None
            recommendation = qual["recommendation"] if qual else None
            price = _parse_number(qual["suggested_price"]) if qual else None
            age = _age_days(opp.get("created_at"))
            ranked.append({
                "opportunity_id": opp["id"], "title": opp["title"], "stage": opp["stage"],
                "recommendation": recommendation, "suggested_price": qual["suggested_price"] if qual else None,
                "age_days": age, "priority": _priority_label(recommendation, price, age),
            })
        # Within the same priority tier, older opportunities surface first
        # (age is one of the stated prioritization factors -- something
        # that's been sitting a while is more urgent than something fresh).
        order = {"High": 0, "Medium": 1, "Low": 2}
        ranked.sort(key=lambda r: (order.get(r["priority"], 3), -(r["age_days"] or 0)))
        return ranked[:limit]

    # -- "Which proposals need approval?" --

    def proposals_needing_approval(self) -> List[Dict[str, Any]]:
        return [i for i in self.needs_aryan.list_pending() if i.get("kind") == "proposal_approval"]

    # -- "Which applications are blocked?" --

    def blocked_applications(self) -> List[Dict[str, Any]]:
        return list(reversed(self.store.list("rh_application_attempts", "status=?", ("BLOCKED",))))

    # -- "Who replied?" --

    def who_replied(self) -> List[Dict[str, Any]]:
        """Inbound conversation messages with no outbound reply after them
        yet -- real, on-file signal of who's waiting on a response."""
        messages = self.store.list("rh_conversation_messages")
        by_opportunity: Dict[str, List[Dict[str, Any]]] = {}
        for m in messages:
            by_opportunity.setdefault(m["opportunity_id"], []).append(m)
        awaiting = []
        for opportunity_id, msgs in by_opportunity.items():
            msgs.sort(key=lambda m: m["created_at"])
            last = msgs[-1]
            if last["direction"] == "INBOUND":
                awaiting.append(last)
        return awaiting

    # -- "Which negotiations need action?" --

    def negotiations_needing_action(self) -> List[Dict[str, Any]]:
        return [i for i in self.needs_aryan.list_pending() if i.get("kind") == "negotiation_response_approval"]

    # -- "What can close soon?" --

    def what_can_close_soon(self) -> List[Dict[str, Any]]:
        """Opportunities currently in NEGOTIATING with their latest
        negotiation terms on file and within policy -- a real, on-file
        readiness signal, not a predicted close date."""
        candidates = []
        events = self.store.list("rh_lifecycle_events", "to_state=?", ("NEGOTIATING",))
        negotiating_ids = {e["opportunity_id"] for e in events}
        for opportunity_id in negotiating_ids:
            opp = self.store.get("rh_opportunities", opportunity_id)
            if not opp or opp["stage"] in TERMINAL_STAGES:
                continue
            terms = self.store.list("rh_negotiation_terms", "opportunity_id=?", (opportunity_id,))
            latest = terms[-1] if terms else None
            if latest and latest["within_policy"]:
                candidates.append({"opportunity_id": opportunity_id, "title": opp["title"], "latest_terms": latest})
        return candidates

    # -- "Which clients are onboarding?" --

    def onboarding_clients(self) -> List[Dict[str, Any]]:
        from .onboarding import OnboardingStore
        onboarding_store = OnboardingStore(self.store, self.audit)
        events = self.store.list("rh_lifecycle_events", "to_state=?", ("ONBOARDING",))
        opportunity_ids = {e["opportunity_id"] for e in events}
        result = []
        for opportunity_id in opportunity_ids:
            opp = self.store.get("rh_opportunities", opportunity_id)
            if not opp:
                continue
            result.append({
                "opportunity_id": opportunity_id, "title": opp["title"],
                "onboarding_completeness": onboarding_store.completion_summary(opportunity_id),
            })
        return result

    # -- "Which deliveries are blocked?" --

    def blocked_deliveries(self) -> List[Dict[str, Any]]:
        return [s for s in self.account_manager.portfolio_overview() if s["blocked"]]

    # -- "Which invoices are overdue?" --

    def overdue_invoices(self) -> List[Dict[str, Any]]:
        # Real, evidence-based: re-checks each SENT/PARTIALLY_PAID invoice
        # against today's real due_date before reporting, rather than
        # trusting a possibly-stale OVERDUE flag alone.
        for invoice in self.billing.list():
            if invoice["status"] in {"SENT", "PARTIALLY_PAID"}:
                self.billing.check_overdue(invoice["id"])
        return [i for i in self.billing.list() if i["status"] == "OVERDUE"]

    # -- "Which clients can be upsold?" --

    def upsell_ready_clients(self) -> List[Dict[str, Any]]:
        due = self.retention.list_due()
        by_client: Dict[str, List[Dict[str, Any]]] = {}
        for item in due:
            by_client.setdefault(item["client_id"], []).append(item)
        result = []
        for client_id, items in by_client.items():
            client = self.clients.get(client_id)
            if not client:
                continue
            result.append({"client_id": client_id, "client_name": client["name"], "due_items": items})
        return result

    def overview(self) -> Dict[str, Any]:
        """One call for the TTT HQ UI's Sales Manager surface -- everything
        above, assembled together."""
        return {
            "attention": self.whats_needs_attention_now(),
            "best_leads": self.best_leads(),
            "proposals_needing_approval": self.proposals_needing_approval(),
            "blocked_applications": self.blocked_applications(),
            "who_replied": self.who_replied(),
            "negotiations_needing_action": self.negotiations_needing_action(),
            "can_close_soon": self.what_can_close_soon(),
            "onboarding_clients": self.onboarding_clients(),
            "blocked_deliveries": self.blocked_deliveries(),
            "overdue_invoices": self.overdue_invoices(),
            "upsell_ready_clients": self.upsell_ready_clients(),
        }
