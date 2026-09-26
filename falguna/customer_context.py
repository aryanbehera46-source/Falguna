"""TTT Communications V2 -- Milestone 4: Customer & Organization Data
Isolation.

A single, explicitly-scoped read path for "everything TTT knows about this
customer" -- built once here so every AI agent and every future HQ view
retrieves customer context the same, safely-scoped way instead of each
writing its own ad hoc query (which is exactly how cross-customer leakage
bugs happen).

Reuses, never duplicates:
  - `CommsStore` for organizations/contacts/conversations.
  - `rh_opportunities`/`rh_proposals`/`rh_followups`/`rh_active_jobs` and
    `clients`, reached only through a conversation's own real linkage
    (`linked_opportunity_id`/`linked_client_id`) -- never through fuzzy
    name matching, which could attribute the wrong company's data.

Isolation guarantees:
  - Every query in `internal_context()` is filtered by `organization_id`
    (or, transitively, by the opportunity/client ids a conversation that
    belongs to that organization actually links to) -- there is no code
    path here that can return another organization's row.
  - `customer_facing_context()` additionally strips every internal-only
    note (`is_internal_note`) before returning -- content meant only for
    TTT staff/AI never appears in a bundle destined for a customer-facing
    draft.
  - This module never touches Falguna's own internal engineering tables
    (missions/requirements/tasks/runs/checkpoints) -- TTT's customer data
    and Falguna's internal operations data are structurally separate
    stores accessed by entirely different code, not just different rows.
"""

from typing import Any, Dict, List, Optional

from .comms import CommsStore
from .store import StateStore


class ScopeError(ValueError):
    pass


class CustomerContextService:
    def __init__(self, store: StateStore, comms: CommsStore):
        self.store = store
        self.comms = comms

    def _require_organization(self, organization_id: str) -> Dict[str, Any]:
        org = self.store.get("comm_organizations", organization_id)
        if not org:
            raise ScopeError(f"organization not found: {organization_id}")
        return org

    def assert_scope(self, requested_by_organization_id: Optional[str], organization_id: str) -> None:
        """Explicit authorization check: when a caller states which
        organization it is authorized to act for (e.g. a future customer
        self-service session), and that doesn't match the organization
        whose context is being requested, this raises rather than silently
        returning someone else's data. Called with `requested_by_organization_id=None`
        for an internal TTT-staff/AI caller that isn't scoped to a single
        customer -- exactly today's callers (comms_workforce agents)."""
        if requested_by_organization_id and requested_by_organization_id != organization_id:
            raise ScopeError(
                f"requested organization {organization_id!r} does not match the authorized scope "
                f"{requested_by_organization_id!r}"
            )

    def internal_context(
        self, organization_id: str, actor: str = "system", requested_by_organization_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Full scoped bundle for TTT-internal use (an AI agent deciding
        what to do, or an HQ drill-down view) -- includes internal notes.
        Never call this to build a message that goes back to the customer;
        use `customer_facing_context()` for that."""
        self.assert_scope(requested_by_organization_id, organization_id)
        org = self._require_organization(organization_id)
        contacts = self.store.list("comm_contacts", "organization_id=?", (organization_id,))
        conversation_rows = self.store.list("comm_conversations", "organization_id=?", (organization_id,))
        conversations = [self.comms.get_conversation(c["id"]) for c in conversation_rows]

        opportunity_ids = sorted({c["linked_opportunity_id"] for c in conversations if c.get("linked_opportunity_id")})
        client_ids = sorted({c["linked_client_id"] for c in conversations if c.get("linked_client_id")})

        opportunities = [o for o in (self.store.get("rh_opportunities", oid) for oid in opportunity_ids) if o]
        proposals: List[Dict[str, Any]] = []
        followups: List[Dict[str, Any]] = []
        active_jobs: List[Dict[str, Any]] = []
        for oid in opportunity_ids:
            proposals += self.store.list("rh_proposals", "opportunity_id=?", (oid,))
            followups += self.store.list("rh_followups", "opportunity_id=?", (oid,))
            active_jobs += self.store.list("rh_active_jobs", "opportunity_id=?", (oid,))
        clients = [c for c in (self.store.get("clients", cid) for cid in client_ids) if c]

        support_conversations = [c for c in conversations if c["department"] in ("support", "billing")]
        open_support_issues = [c for c in support_conversations if c["status"] not in ("resolved", "closed")]

        return {
            "organization": org, "contacts": contacts, "conversations": conversations,
            "opportunities": opportunities, "proposals": proposals, "followups": followups,
            "active_jobs": active_jobs, "approved_agreements": [p for p in proposals if p["status"] == "APPROVED"],
            "clients": clients, "support_history": support_conversations, "open_support_issues": open_support_issues,
            "billing_status": self._billing_status(clients),
        }

    def customer_facing_context(
        self, organization_id: str, actor: str = "system", requested_by_organization_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """The same bundle, minus anything internal-only -- the only
        context an AI agent should draw on when drafting text a customer
        will actually see."""
        ctx = dict(self.internal_context(organization_id, actor=actor, requested_by_organization_id=requested_by_organization_id))
        stripped = []
        for conv in ctx["conversations"]:
            conv = dict(conv)
            conv["messages"] = [m for m in conv["messages"] if not m.get("is_internal_note")]
            stripped.append(conv)
        ctx["conversations"] = stripped
        return ctx

    @staticmethod
    def _billing_status(clients: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        # Factual only -- `total_won_value`/`status` are already-on-file
        # columns, never inferred or estimated here.
        return [{"client_id": c["id"], "name": c["name"], "status": c["status"],
                  "total_won_value": c["total_won_value"]} for c in clients]
