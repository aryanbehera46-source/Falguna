"""TTT Autonomous Revenue-to-Delivery Loop v1 -- Billing, Completion, and
Retention/Upsell (Sections 11-13, Pass D).

`BillingStore` manages `rh_invoices`: DRAFT -> READY -> SENT ->
PARTIALLY_PAID/PAID, or OVERDUE (only from a state that was actually SENT
and past its due date -- never fabricated), or CANCELLED from any
non-terminal state. The one hard rule from the continuation instruction:
`record_payment` must never mark an invoice PAID/PARTIALLY_PAID without
`evidence` -- it raises if none is given. Every payment recorded is
appended to a running evidence list, never overwritten, so the full
payment history stays inspectable.

`CompletionService` records real completion evidence -- same rule,
`evidence` is required. `RetentionStore` tracks retention/upsell
follow-ups (maintenance, hosting/support, automation, AI integration,
testimonial, referral, and a generic follow_up), each with an optional
follow-up date so a Sales Manager (Pass E) can list what's actually due.
"""

import json
from typing import Any, Dict, List, Optional

from .audit import AuditLog
from .store import StateStore, utcnow

INVOICE_STATUSES = {"DRAFT", "READY", "SENT", "PARTIALLY_PAID", "PAID", "OVERDUE", "CANCELLED"}
_TERMINAL_INVOICE_STATUSES = {"PAID", "CANCELLED"}

RETENTION_KINDS = {"maintenance", "support_hosting", "automation", "ai_integration", "testimonial", "referral", "follow_up"}
RETENTION_STATUSES = {"OPEN", "SCHEDULED", "COMPLETED", "DECLINED"}


class BillingError(ValueError):
    pass


class BillingStore:
    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit

    def create_invoice(
        self, client_id: str, actor: str, amount: float, currency: str = "USD",
        opportunity_id: Optional[str] = None, active_job_id: Optional[str] = None,
        milestone: Optional[str] = None, due_date: Optional[str] = None,
    ) -> str:
        if not self.store.get("clients", client_id):
            raise BillingError("client not found")
        if amount is None or amount <= 0:
            raise BillingError("amount must be a positive number")
        now = utcnow()
        invoice_id = self.store.create("rh_invoices", {
            "client_id": client_id, "opportunity_id": opportunity_id, "active_job_id": active_job_id,
            "amount": amount, "currency": currency, "milestone": milestone, "due_date": due_date,
            "amount_received": 0.0, "status": "DRAFT", "evidence_json": json.dumps([]),
            "created_at": now, "updated_at": now,
        })
        self.audit.append("RH_INVOICE_CREATED", {"invoice_id": invoice_id, "client_id": client_id, "amount": amount, "currency": currency, "actor": actor})
        return invoice_id

    def _require(self, invoice_id: str) -> Dict[str, Any]:
        invoice = self.store.get("rh_invoices", invoice_id)
        if not invoice:
            raise BillingError("invoice not found")
        return invoice

    def mark_ready(self, invoice_id: str, actor: str) -> Dict[str, Any]:
        invoice = self._require(invoice_id)
        if invoice["status"] != "DRAFT":
            raise BillingError(f"invoice must be DRAFT to mark READY (currently {invoice['status']})")
        self.store.update("rh_invoices", invoice_id, status="READY", updated_at=utcnow())
        self.audit.append("RH_INVOICE_READY", {"invoice_id": invoice_id, "actor": actor})
        return self.store.get("rh_invoices", invoice_id)

    def mark_sent(self, invoice_id: str, actor: str) -> Dict[str, Any]:
        """Owner-triggered only, same DRAFT/READY -> SENT shape as
        FollowupStore.mark_sent and ConversationStore.mark_sent -- this
        module never sends an invoice itself."""
        invoice = self._require(invoice_id)
        if invoice["status"] not in {"DRAFT", "READY"}:
            raise BillingError(f"invoice must be DRAFT or READY to mark SENT (currently {invoice['status']})")
        self.store.update("rh_invoices", invoice_id, status="SENT", updated_at=utcnow())
        self.audit.append("RH_INVOICE_SENT", {"invoice_id": invoice_id, "actor": actor})
        return self.store.get("rh_invoices", invoice_id)

    def record_payment(self, invoice_id: str, amount: float, actor: str, evidence: Any) -> Dict[str, Any]:
        if not evidence:
            raise BillingError("payment cannot be recorded without evidence (manual confirmation or integration reference)")
        invoice = self._require(invoice_id)
        if invoice["status"] in _TERMINAL_INVOICE_STATUSES:
            raise BillingError(f"invoice is already {invoice['status']}, no further payment can be recorded")
        if amount is None or amount <= 0:
            raise BillingError("payment amount must be a positive number")
        history = json.loads(invoice["evidence_json"]) if invoice.get("evidence_json") else []
        history.append({"amount": amount, "evidence": evidence, "actor": actor, "recorded_at": utcnow()})
        new_received = (invoice["amount_received"] or 0.0) + amount
        status = "PAID" if new_received >= invoice["amount"] else "PARTIALLY_PAID"
        self.store.update("rh_invoices", invoice_id, amount_received=new_received, status=status, evidence_json=json.dumps(history), updated_at=utcnow())
        self.audit.append("RH_INVOICE_PAYMENT_RECORDED", {
            "invoice_id": invoice_id, "amount": amount, "new_status": status, "actor": actor,
        })
        return self.store.get("rh_invoices", invoice_id)

    def check_overdue(self, invoice_id: str) -> Dict[str, Any]:
        """Explicit, evidence-based -- only moves SENT/PARTIALLY_PAID to
        OVERDUE when a real due_date has actually passed. Never invoked
        automatically on every read (no background clock in this
        codebase); callers (e.g. a dashboard refresh) call this when they
        want the status re-checked against today's real date."""
        invoice = self._require(invoice_id)
        if invoice["status"] not in {"SENT", "PARTIALLY_PAID"} or not invoice.get("due_date"):
            return invoice
        if invoice["due_date"] < utcnow()[:10]:
            self.store.update("rh_invoices", invoice_id, status="OVERDUE", updated_at=utcnow())
            self.audit.append("RH_INVOICE_OVERDUE", {"invoice_id": invoice_id, "due_date": invoice["due_date"]})
            return self.store.get("rh_invoices", invoice_id)
        return invoice

    def cancel(self, invoice_id: str, actor: str, reason: Optional[str] = None) -> Dict[str, Any]:
        invoice = self._require(invoice_id)
        if invoice["status"] in _TERMINAL_INVOICE_STATUSES:
            raise BillingError(f"invoice is already {invoice['status']}, cannot cancel")
        self.store.update("rh_invoices", invoice_id, status="CANCELLED", updated_at=utcnow())
        self.audit.append("RH_INVOICE_CANCELLED", {"invoice_id": invoice_id, "reason": reason, "actor": actor})
        return self.store.get("rh_invoices", invoice_id)

    def get(self, invoice_id: str) -> Optional[Dict[str, Any]]:
        return self.store.get("rh_invoices", invoice_id)

    def list_for_client(self, client_id: str) -> List[Dict[str, Any]]:
        return list(reversed(self.store.list("rh_invoices", "client_id=?", (client_id,))))

    def list(self) -> List[Dict[str, Any]]:
        return list(reversed(self.store.list("rh_invoices")))


class CompletionError(ValueError):
    pass


class CompletionService:
    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit

    def record_completion(
        self, opportunity_id: str, actor: str, evidence: Any,
        active_job_id: Optional[str] = None, checklist: Optional[Dict[str, bool]] = None,
    ) -> str:
        if not evidence:
            raise CompletionError("completion cannot be recorded without real evidence")
        if not self.store.get("rh_opportunities", opportunity_id):
            raise CompletionError("opportunity not found")
        existing = self.store.list("rh_completion_records", "opportunity_id=?", (opportunity_id,))
        if existing:
            return existing[-1]["id"]  # idempotent -- one completion record per opportunity
        now = utcnow()
        record_id = self.store.create("rh_completion_records", {
            "opportunity_id": opportunity_id, "active_job_id": active_job_id,
            "evidence_json": json.dumps(evidence if isinstance(evidence, (dict, list)) else {"note": str(evidence)}),
            "checklist_json": json.dumps(checklist) if checklist is not None else None,
            "actor": actor, "created_at": now,
        })
        self.audit.append("RH_COMPLETION_RECORDED", {"opportunity_id": opportunity_id, "record_id": record_id, "actor": actor})
        return record_id

    def get_for_opportunity(self, opportunity_id: str) -> Optional[Dict[str, Any]]:
        records = self.store.list("rh_completion_records", "opportunity_id=?", (opportunity_id,))
        return records[-1] if records else None


class RetentionError(ValueError):
    pass


class RetentionStore:
    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit

    def create_item(
        self, client_id: str, kind: str, actor: str, opportunity_id: Optional[str] = None,
        notes: Optional[str] = None, follow_up_date: Optional[str] = None,
    ) -> str:
        if kind not in RETENTION_KINDS:
            raise RetentionError(f"unknown retention kind: {kind!r} (expected one of {sorted(RETENTION_KINDS)})")
        if not self.store.get("clients", client_id):
            raise RetentionError("client not found")
        now = utcnow()
        item_id = self.store.create("rh_retention_items", {
            "client_id": client_id, "opportunity_id": opportunity_id, "kind": kind, "status": "OPEN",
            "follow_up_date": follow_up_date, "notes": notes, "actor": actor, "created_at": now, "updated_at": now,
        })
        self.audit.append("RH_RETENTION_ITEM_CREATED", {"item_id": item_id, "client_id": client_id, "kind": kind, "actor": actor})
        return item_id

    def update_item(
        self, item_id: str, actor: str, status: Optional[str] = None,
        notes: Optional[str] = None, follow_up_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        item = self.store.get("rh_retention_items", item_id)
        if not item:
            raise RetentionError("retention item not found")
        if status is not None and status not in RETENTION_STATUSES:
            raise RetentionError(f"unknown retention status: {status!r} (expected one of {sorted(RETENTION_STATUSES)})")
        changes: Dict[str, Any] = {"updated_at": utcnow()}
        if status is not None:
            changes["status"] = status
        if notes is not None:
            changes["notes"] = notes
        if follow_up_date is not None:
            changes["follow_up_date"] = follow_up_date
        self.store.update("rh_retention_items", item_id, **changes)
        self.audit.append("RH_RETENTION_ITEM_UPDATED", {"item_id": item_id, "status": status, "actor": actor})
        return self.store.get("rh_retention_items", item_id)

    def list_for_client(self, client_id: str) -> List[Dict[str, Any]]:
        return list(reversed(self.store.list("rh_retention_items", "client_id=?", (client_id,))))

    def list_due(self, as_of: Optional[str] = None) -> List[Dict[str, Any]]:
        as_of = as_of or utcnow()[:10]
        items = self.store.list("rh_retention_items", "status IN (?, ?)", ("OPEN", "SCHEDULED"))
        return [i for i in items if i.get("follow_up_date") and i["follow_up_date"] <= as_of]

    def list(self) -> List[Dict[str, Any]]:
        return list(reversed(self.store.list("rh_retention_items")))
