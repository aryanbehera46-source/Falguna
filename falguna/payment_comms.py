"""Phase 5 Final Client Experience, Section 16 -- Payment Communication
Bridge.

Bridges real billing/dispute state (falguna/billing.py's BillingStore,
falguna/commercial.py's DisputeStore) to customer-facing communication
(falguna/comms.py's CommsStore) -- grounded entirely in the evidence
those two modules already recorded, exactly like
account_management.py's client_update_draft(). This module never
fabricates a "payment received" or a resolution outcome: every draft it
writes quotes a real row (a payment entry inside an invoice's own
evidence_json, a dispute's own approved refund_amount, an actual
due_date) and nothing it cannot point to. If the underlying state isn't
there yet (no payment recorded, invoice not actually OVERDUE, dispute not
actually resolved), the corresponding draft_*() method returns None and
writes nothing -- it never invents a status to have something to say.

Like every other outbound message in this codebase, everything this
module produces is a DRAFT (falguna/comms.py's draft-then-approve
convention) -- it never sends anything itself, and the same HIGH-risk
approval gate inside CommsStore.send_message_via_provider /
send_message_via_whatsapp_provider still governs whether/when a payment-
related draft can actually go out.

Idempotent per real event: a `comm_payment_drafts` row is the permanent,
inspectable record of "this billing/dispute event already produced a
customer-facing draft" -- keyed on the invoice/dispute id, the event
type, and the evidence itself (the same amount+evidence match billing.py's
own record_payment() uses for its own idempotency), so replaying the same
trigger twice (e.g. a dashboard refresh calling check_overdue() again, or
re-processing the same webhook) never produces a duplicate customer
message.
"""

import json
from typing import Any, Dict, Optional

from .audit import AuditLog
from .billing import BillingStore
from .commercial import DisputeStore
from .comms import CHANNELS, CommsStore
from .store import StateStore, utcnow

_SOURCE_REF_TYPE = "payment_comms_bridge"
_DISPUTE_ACK_STATUSES = {"OPEN", "UNDER_REVIEW"}
_DISPUTE_RESOLVED_STATUSES = {"RESOLVED_NO_REFUND", "PARTIAL_REFUND_APPROVED", "FULL_REFUND_APPROVED"}


class PaymentCommsError(ValueError):
    pass


class PaymentCommsBridge:
    def __init__(
        self, store: StateStore, audit: AuditLog,
        comms: Optional[CommsStore] = None,
        billing: Optional[BillingStore] = None,
        disputes: Optional[DisputeStore] = None,
    ):
        self.store = store
        self.audit = audit
        self.comms = comms or CommsStore(store, audit)
        self.billing = billing or BillingStore(store, audit)
        self.disputes = disputes or DisputeStore(store, audit)

    # -- shared plumbing ----------------------------------------------

    def _existing_draft(
        self, event_type: str, evidence: Dict[str, Any],
        invoice_id: Optional[str] = None, dispute_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """The same evidence-equality idempotency check billing.py's own
        record_payment() uses for duplicate payment observations, applied
        here so the same real event never produces two customer drafts."""
        where = "event_type=?"
        params: list = [event_type]
        if invoice_id:
            where += " AND invoice_id=?"
            params.append(invoice_id)
        if dispute_id:
            where += " AND dispute_id=?"
            params.append(dispute_id)
        rows = self.store.list("comm_payment_drafts", where, tuple(params))
        evidence_blob = json.dumps(evidence, sort_keys=True)
        for row in rows:
            if json.dumps(json.loads(row["evidence_json"]), sort_keys=True) == evidence_blob:
                return self.store.get("comm_messages", row["comm_message_id"])
        return None

    def _conversation_for_client(self, client_id: str, actor: str) -> str:
        existing = self.store.list(
            "comm_conversations", "linked_client_id=? AND department=?", (client_id, "billing"),
        )
        if existing:
            return existing[-1]["id"]
        client = self.store.get("clients", client_id)
        channel = "EMAIL"
        if client and client.get("contact_channel") in CHANNELS:
            channel = client["contact_channel"]
        conv = self.comms.open_conversation(
            channel, "billing", subject="Billing & payments", actor=actor,
            linked_client_id=client_id,
            source_ref_type=_SOURCE_REF_TYPE, source_ref_id=client_id,
        )
        return conv["id"]


    def _record_draft(
        self, event_type: str, evidence: Dict[str, Any], body: str, client_id: str,
        invoice_id: Optional[str] = None, dispute_id: Optional[str] = None, actor: str = "system",
    ) -> Dict[str, Any]:
        conversation_id = self._conversation_for_client(client_id, actor)
        message = self.comms.add_message(
            conversation_id, "OUTBOUND", body, kind="message", actor=actor,
            source_ref_type=_SOURCE_REF_TYPE, source_ref_id=invoice_id or dispute_id,
        )
        draft_id = self.store.create("comm_payment_drafts", {
            "invoice_id": invoice_id, "dispute_id": dispute_id, "event_type": event_type,
            "comm_message_id": message["id"], "evidence_json": json.dumps(evidence),
            "actor": actor, "created_at": utcnow(),
        })
        self.audit.append("COMM_PAYMENT_DRAFT_CREATED", {
            "draft_id": draft_id, "event_type": event_type, "invoice_id": invoice_id,
            "dispute_id": dispute_id, "message_id": message["id"], "actor": actor,
        })
        return message


    # -- invoice-driven drafts ------------------------------------------

    def draft_invoice_ready_notice(self, invoice_id: str, actor: str = "system") -> Optional[Dict[str, Any]]:
        """Only ever drafted once the invoice is actually READY or SENT --
        never a preview of a DRAFT invoice no one has approved yet."""
        invoice = self.billing.get(invoice_id)
        if not invoice:
            raise PaymentCommsError("invoice not found")
        if invoice["status"] not in {"READY", "SENT"}:
            return None
        evidence = {
            "invoice_id": invoice_id, "status": invoice["status"],
            "amount": invoice["amount"], "currency": invoice["currency"],
            "due_date": invoice.get("due_date"),
        }
        existing = self._existing_draft("INVOICE_READY", evidence, invoice_id=invoice_id)
        if existing:
            return existing
        due = f" by {invoice['due_date']}" if invoice.get("due_date") else ""
        body = (
            f"Your invoice for {invoice['amount']:.2f} {invoice['currency']} is ready"
            f"{due}. Let us know if you have any questions about it."
        )
        return self._record_draft(
            "INVOICE_READY", evidence, body, invoice["client_id"], invoice_id=invoice_id, actor=actor,
        )


    def draft_payment_received_notice(self, invoice_id: str, actor: str = "system") -> Optional[Dict[str, Any]]:
        """Grounded in the invoice's own evidence_json payment history --
        never drafted unless a real payment entry is actually on file."""
        invoice = self.billing.get(invoice_id)
        if not invoice:
            raise PaymentCommsError("invoice not found")
        history = json.loads(invoice["evidence_json"]) if invoice.get("evidence_json") else []
        if not history:
            return None
        latest = history[-1]
        evidence = {
            "invoice_id": invoice_id, "amount": latest["amount"],
            "recorded_at": latest["recorded_at"], "evidence": latest["evidence"],
        }
        existing = self._existing_draft("PAYMENT_RECEIVED", evidence, invoice_id=invoice_id)
        if existing:
            return existing
        remaining = max(0.0, (invoice["amount"] or 0.0) - (invoice["amount_received"] or 0.0))
        tail = (
            f" {remaining:.2f} {invoice['currency']} remains outstanding."
            if remaining > 1e-9 else " This invoice is now fully paid -- thank you."
        )
        body = (
            f"We've received a payment of {latest['amount']:.2f} {invoice['currency']} "
            f"on invoice {invoice_id}.{tail}"
        )
        return self._record_draft(
            "PAYMENT_RECEIVED", evidence, body, invoice["client_id"], invoice_id=invoice_id, actor=actor,
        )


    def draft_overdue_reminder(self, invoice_id: str, actor: str = "system") -> Optional[Dict[str, Any]]:
        """Only ever reminds about a real, already-recorded OVERDUE status
        (set by BillingStore.check_overdue against an actual due_date that
        has passed) -- this never predicts or pre-empts an overdue state
        itself."""
        invoice = self.billing.get(invoice_id)
        if not invoice:
            raise PaymentCommsError("invoice not found")
        if invoice["status"] != "OVERDUE":
            return None
        evidence = {
            "invoice_id": invoice_id, "status": "OVERDUE",
            "due_date": invoice.get("due_date"), "amount_received": invoice.get("amount_received"),
        }
        existing = self._existing_draft("INVOICE_OVERDUE", evidence, invoice_id=invoice_id)
        if existing:
            return existing
        outstanding = max(0.0, (invoice["amount"] or 0.0) - (invoice["amount_received"] or 0.0))
        body = (
            f"Just a friendly reminder that invoice {invoice_id} "
            f"({outstanding:.2f} {invoice['currency']} outstanding) was due on {invoice['due_date']}. "
            "Please let us know if you'd like to discuss payment."
        )
        return self._record_draft(
            "INVOICE_OVERDUE", evidence, body, invoice["client_id"], invoice_id=invoice_id, actor=actor,
        )


    # -- dispute-driven drafts -------------------------------------------

    def draft_dispute_acknowledgement(self, dispute_id: str, actor: str = "system") -> Optional[Dict[str, Any]]:
        dispute = self.disputes.get(dispute_id)
        if not dispute:
            raise PaymentCommsError("dispute not found")
        if dispute["status"] not in _DISPUTE_ACK_STATUSES:
            return None
        evidence = {"dispute_id": dispute_id, "status": dispute["status"]}
        existing = self._existing_draft("DISPUTE_ACKNOWLEDGED", evidence, dispute_id=dispute_id)
        if existing:
            return existing
        body = (
            f"We've received your concern about invoice {dispute['invoice_id']} and it's now under review. "
            "We'll follow up as soon as we have an update -- no action is needed from you right now."
        )
        return self._record_draft(
            "DISPUTE_ACKNOWLEDGED", evidence, body, dispute["client_id"], dispute_id=dispute_id, actor=actor,
        )


    def draft_dispute_resolution_notice(self, dispute_id: str, actor: str = "system") -> Optional[Dict[str, Any]]:
        """Never states a refund figure beyond dispute['refund_amount'] --
        the one human-authorized number DisputeStore.resolve() itself
        wrote; this module adds no number of its own."""
        dispute = self.disputes.get(dispute_id)
        if not dispute:
            raise PaymentCommsError("dispute not found")
        if dispute["status"] not in _DISPUTE_RESOLVED_STATUSES:
            return None
        evidence = {
            "dispute_id": dispute_id, "status": dispute["status"],
            "refund_amount": dispute.get("refund_amount"), "resolution": dispute.get("resolution"),
        }
        existing = self._existing_draft("DISPUTE_RESOLVED", evidence, dispute_id=dispute_id)
        if existing:
            return existing
        if dispute["status"] == "RESOLVED_NO_REFUND":
            body = (
                f"We've reviewed your concern about invoice {dispute['invoice_id']}: {dispute['resolution']} "
                "No refund applies in this case."
            )
        else:
            invoice = self.billing.get(dispute["invoice_id"])
            currency = invoice["currency"] if invoice else ""
            body = (
                f"We've reviewed your concern about invoice {dispute['invoice_id']}: {dispute['resolution']} "
                f"A refund of {dispute['refund_amount']:.2f} {currency} has been approved."
            )
        return self._record_draft(
            "DISPUTE_RESOLVED", evidence, body, dispute["client_id"], dispute_id=dispute_id, actor=actor,
        )


    # -- read-only accessors ----------------------------------------------

    def get_draft(self, draft_id: str) -> Optional[Dict[str, Any]]:
        return self.store.get("comm_payment_drafts", draft_id)

    def list_for_invoice(self, invoice_id: str) -> list:
        return list(reversed(self.store.list("comm_payment_drafts", "invoice_id=?", (invoice_id,))))

    def list_for_dispute(self, dispute_id: str) -> list:
        return list(reversed(self.store.list("comm_payment_drafts", "dispute_id=?", (dispute_id,))))
