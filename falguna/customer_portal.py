"""Phase 5 Final Client Experience, Section 17 -- Customer Portal Data/API
Foundation.

This is deliberately a DATA FOUNDATION only, not a working customer
portal: per the standing instruction's explicit permission, real customer
authentication (login, sessions, tokens, password/OTP flows, "forgot
password") is deferred to Phase 6. Building a believable-looking sign-in
screen with no real identity verification behind it would be worse than
building nothing -- it would let anyone assert they are a given customer
and read another customer's invoices and conversations. So this module
adds exactly one access-controlled read path (`get_portal_bundle`) and
nothing a caller could use to self-authenticate, issue a session, or
prove who they are.

Composes, never duplicates, three already-existing read paths:
  - `CustomerContextService.customer_facing_context()` (falguna/
    customer_context.py) for organization/contacts/conversations/
    opportunities/projects-in-progress, with every internal-only note
    already stripped, and its `assert_scope()` isolation already in
    place -- the exact primitive a real Phase 6 session layer would call
    with the signed-in customer's own organization id.
  - `BillingStore` (falguna/billing.py) for the real per-invoice figures
    a customer can act on (amount, amount_received, status, due_date) --
    the `clients.total_won_value` aggregate `customer_facing_context()`
    already returns is a true summary, but a portal needs the actual
    invoice list.
  - `DisputeStore` (falguna/commercial.py), reduced to only the fields a
    customer should see about their own dispute (status, resolution,
    refund_amount) -- a dispute's raw `evidence_json` and
    `commission_impact_json` are TTT-internal and are never included
    here.

Every figure this module returns is read directly off its own store's
row; this module computes nothing, infers nothing, and never estimates a
status or a percentage that isn't already on file.
"""

from typing import Any, Dict, List, Optional

from .audit import AuditLog
from .billing import BillingStore
from .commercial import DisputeStore
from .comms import CommsStore
from .customer_context import CustomerContextService
from .store import StateStore


class CustomerPortalService:
    def __init__(
        self, store: StateStore, audit: AuditLog,
        comms: Optional[CommsStore] = None,
        context: Optional[CustomerContextService] = None,
        billing: Optional[BillingStore] = None,
        disputes: Optional[DisputeStore] = None,
    ):
        self.store = store
        self.audit = audit
        self.comms = comms or CommsStore(store, audit)
        self.context = context or CustomerContextService(store, self.comms)
        self.billing = billing or BillingStore(store, audit)
        self.disputes = disputes or DisputeStore(store, audit)

    @staticmethod
    def _customer_safe_invoice(invoice: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": invoice["id"], "amount": invoice["amount"], "currency": invoice["currency"],
            "amount_received": invoice["amount_received"], "status": invoice["status"],
            "due_date": invoice.get("due_date"), "milestone": invoice.get("milestone"),
        }

    @staticmethod
    def _customer_safe_dispute(dispute: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": dispute["id"], "invoice_id": dispute["invoice_id"], "reason": dispute["reason"],
            "amount_disputed": dispute["amount_disputed"], "status": dispute["status"],
            "resolution": dispute.get("resolution"), "refund_amount": dispute.get("refund_amount"),
            "created_at": dispute["created_at"], "resolved_at": dispute.get("resolved_at"),
        }

    @staticmethod
    def _customer_safe_project(project: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": project["id"], "status": project["status"], "delivery_route": project["delivery_route"],
            "created_at": project["created_at"], "updated_at": project["updated_at"],
        }


    def get_portal_bundle(
        self, organization_id: str, actor: str = "system",
        requested_by_organization_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """The one read this module exposes: everything a signed-in
        customer should be able to see about their own account, scoped by
        the same `assert_scope()` isolation `CustomerContextService`
        already enforces -- `requested_by_organization_id` is exactly
        where a real Phase 6 session layer would plug in the verified
        session's own organization id. Passing it as `None` (the default)
        means "an internal/trusted caller", not "any customer" -- the
        same convention every existing `CustomerContextService` caller
        already follows; this module adds no new way to bypass that
        check."""
        ctx = self.context.customer_facing_context(
            organization_id, actor=actor, requested_by_organization_id=requested_by_organization_id,
        )
        client_ids = [c["id"] for c in ctx["clients"]]

        invoices: List[Dict[str, Any]] = []
        for client_id in client_ids:
            invoices += [self._customer_safe_invoice(i) for i in self.billing.list_for_client(client_id)]

        disputes: List[Dict[str, Any]] = []
        for invoice in invoices:
            disputes += [self._customer_safe_dispute(d) for d in self.disputes.list(invoice_id=invoice["id"])]

        projects: List[Dict[str, Any]] = []
        for client_id in client_ids:
            projects += [self._customer_safe_project(p) for p in self.store.list("cs_projects", "client_id=?", (client_id,))]

        return {
            "organization": ctx["organization"], "contacts": ctx["contacts"], "clients": ctx["clients"],
            "projects": projects, "conversations": ctx["conversations"],
            "open_support_issues": ctx["open_support_issues"],
            "invoices": invoices, "disputes": disputes,
        }
