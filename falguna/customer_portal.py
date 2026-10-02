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

import json
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
            "id": invoice["id"], "client_id": invoice.get("client_id"),
            "amount": invoice["amount"], "currency": invoice["currency"],
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

    @staticmethod
    def _customer_safe_refund(refund: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": refund["id"], "invoice_id": refund["invoice_id"], "payment_intent_id": refund["payment_intent_id"],
            "amount": refund["amount"], "currency": refund["currency"], "reason": refund["reason"],
            "status": refund["status"], "provider_ref": refund.get("provider_ref") if refund["status"] == "CONFIRMED" else None,
            "created_at": refund["created_at"],
        }

    @staticmethod
    def _customer_safe_message(message: Dict[str, Any]) -> Dict[str, Any]:
        # Deliberately excludes sender_agent (an internal AI role name, or
        # "Aryan" -- see comm_messages schema comment) and every risk/
        # moderation field: a customer should see who sent a reply by
        # direction ("us" vs "them"), never which internal agent drafted
        # it. is_internal_note messages are already stripped upstream by
        # CustomerContextService.customer_facing_context() before this is
        # ever called.
        return {
            "id": message["id"], "direction": message["direction"],
            "body": message["body"], "created_at": message.get("created_at"),
        }

    @classmethod
    def _customer_safe_conversation(cls, conversation: Dict[str, Any]) -> Dict[str, Any]:
        # A DRAFT/un-sent outbound message is a staff-side work-in-progress
        # awaiting human approval (see falguna/comms.py's status lifecycle)
        # -- showing it to the customer before it is actually SENT would
        # let them see content Aryan/staff may still edit or never send at
        # all. APPROVED is still an internal pre-send state, so it is also
        # excluded. The customer's own inbound messages are always real and
        # always shown regardless of status.
        safe_messages = [
            cls._customer_safe_message(m) for m in conversation.get("messages", [])
            if m.get("direction") == "INBOUND" or m.get("status") == "SENT"
        ]
        return {
            "id": conversation["id"], "channel": conversation["channel"],
            "department": conversation["department"], "subject": conversation.get("subject"),
            "status": conversation["status"], "priority": conversation.get("priority"),
            "created_at": conversation.get("created_at"), "updated_at": conversation.get("updated_at"),
            "messages": safe_messages,
        }

    @staticmethod
    def _customer_safe_sow(closing_record: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """The approved scope/SOW reference for a project, read verbatim off
        its real rh_closing_records row -- never invented, never estimated.
        Returns None when no closing record exists yet (an honest "nothing
        on file" rather than a fabricated placeholder)."""
        if not closing_record:
            return None
        return {
            "final_scope": closing_record.get("final_scope"),
            "final_price": closing_record.get("final_price"),
            "currency": closing_record.get("currency"),
            "payment_terms": closing_record.get("payment_terms"),
            "deadline": closing_record.get("deadline"),
            "deliverables": closing_record.get("deliverables"),
            "acceptance_criteria": closing_record.get("acceptance_criteria"),
        }

    @staticmethod
    def _customer_safe_milestones(
        closing_record: Optional[Dict[str, Any]], invoices: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Projects the loosely-structured rh_closing_records.milestones_json
        (just a list of {"name", "amount", ...} dicts -- see
        SalesOperationsService._execute_close(), there is no guaranteed
        date/status/id per milestone) into a customer-safe view, enriched
        ONLY where a real invoice's `milestone` label matches by name.
        Never invents a status, due date, or amount-received: a milestone
        with no matching invoice is reported as SCOPED, meaning only "this
        is part of the agreed scope," nothing about delivery progress."""
        if not closing_record or not closing_record.get("milestones_json"):
            return []
        try:
            raw = json.loads(closing_record["milestones_json"])
        except (TypeError, ValueError):
            return []
        if not isinstance(raw, list):
            return []
        invoices_by_label: Dict[str, Dict[str, Any]] = {}
        for invoice in invoices:
            label = (invoice.get("milestone") or "").strip().lower()
            if label and label not in invoices_by_label:
                invoices_by_label[label] = invoice
        out = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or entry.get("title") or "Milestone")
            matched = invoices_by_label.get(name.strip().lower())
            out.append({
                "name": name,
                "amount": entry.get("amount"),
                "status": matched["status"] if matched else "SCOPED",
                "invoice_id": matched["id"] if matched else None,
                "due_date": matched.get("due_date") if matched else None,
                "amount_received": matched.get("amount_received") if matched else None,
            })
        return out


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
        refunds: List[Dict[str, Any]] = []
        for invoice in invoices:
            disputes += [self._customer_safe_dispute(d) for d in self.disputes.list(invoice_id=invoice["id"])]
            refunds += [self._customer_safe_refund(r) for r in self.store.list("p6_refunds", "invoice_id=?", (invoice["id"],))]

        projects: List[Dict[str, Any]] = []
        for client_id in client_ids:
            projects += [self._customer_safe_project(p) for p in self.store.list("cs_projects", "client_id=?", (client_id,))]

        safe_conversations = [self._customer_safe_conversation(c) for c in ctx["conversations"]]
        open_support_ids = {c["id"] for c in ctx["open_support_issues"]}
        safe_open_support_issues = [c for c in safe_conversations if c["id"] in open_support_ids]

        return {
            "organization": ctx["organization"], "contacts": ctx["contacts"], "clients": ctx["clients"],
            "projects": projects, "conversations": safe_conversations,
            "open_support_issues": safe_open_support_issues,
            "invoices": invoices, "disputes": disputes, "refunds": refunds,
        }

    def get_project_detail(
        self, organization_id: str, project_id: str, actor: str = "system",
        requested_by_organization_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Phase 7, Sections 1-2: the authenticated customer project-detail
        view -- name/id, status, the real approved SOW reference (if any),
        milestones derived from that same real SOW, and the invoices tied
        to the project's own client. Reuses `get_portal_bundle()`'s own
        scoping rather than querying cs_projects directly by id, so a
        project id that does not belong to this organization's own bundle
        is indistinguishable from one that doesn't exist at all -- no
        existence leak across organizations. Returns None for either case;
        the HTTP layer renders both as the same 404."""
        bundle = self.get_portal_bundle(
            organization_id, actor=actor, requested_by_organization_id=requested_by_organization_id,
        )
        project = next((p for p in bundle["projects"] if p["id"] == project_id), None)
        if project is None:
            return None
        raw_project = self.store.get("cs_projects", project_id)
        closing_record = None
        if raw_project and raw_project.get("opportunity_id"):
            closings = self.store.list("rh_closing_records", "opportunity_id=?", (raw_project["opportunity_id"],))
            closing_record = closings[-1] if closings else None
        project_client_id = raw_project.get("client_id") if raw_project else None
        client_invoices = (
            [i for i in bundle["invoices"] if i.get("client_id") == project_client_id]
            if project_client_id else bundle["invoices"]
        )
        return {
            "project": project,
            "sow": self._customer_safe_sow(closing_record),
            "milestones": self._customer_safe_milestones(closing_record, client_invoices),
            "invoices": client_invoices,
        }
