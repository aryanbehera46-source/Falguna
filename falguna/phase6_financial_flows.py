"""Final Phase 6 sandbox financial flows: commissions, refunds, subscriptions, payables."""

import json
from datetime import date, timedelta
from typing import Any, Dict, Optional

from .audit import AuditLog
from .billing import BillingStore
from .partner_management import CommissionStore
from .phase6_commercial import (
    AccessContext, ApprovalWorkflow, CommercialIdentityStore, CommercialSecurityError, FinanceAccounts,
)
from .store import StateStore, utcnow


def _approval(store: StateStore, approval_id: str, organization_id: str, action_type: str, target_id: str) -> Dict[str, Any]:
    row = store.get("p6_approval_requests", approval_id)
    if not row or row["organization_id"] != organization_id or row["action_type"] != action_type or row["target_id"] != target_id:
        raise CommercialSecurityError("matching organization-scoped approval required")
    if row["status"] != "APPROVED_PENDING_EXECUTION":
        raise CommercialSecurityError("Aryan-approved request required")
    return row


class CommissionReleaseService:
    def __init__(self, store: StateStore, audit: AuditLog, identities: CommercialIdentityStore):
        self.store, self.audit, self.identities = store, audit, identities
        self.approvals = ApprovalWorkflow(store, audit, identities)
        self.accounts = FinanceAccounts(store, audit, identities)

    def prepare(self, context: AccessContext, commission_id: str, currency: str, excluded_amount: float,
                idempotency_key: str) -> Dict[str, Any]:
        self.identities.authorize(context, "approvals:make", context.organization_id)
        commission = self.store.get("pm_commissions", commission_id)
        if not commission or commission["status"] != "ELIGIBLE":
            raise CommercialSecurityError("eligible commission required")
        referral = self.store.get("pm_referrals", commission["referral_id"])
        invoice = self.store.get("rh_invoices", commission.get("invoice_id"))
        if not referral or not invoice:
            raise CommercialSecurityError("commission must link to referral and cleared invoice")
        open_risks = self.store.list("p6_risk_events", "organization_id=? AND partner_id=? AND status NOT IN ('CLEARED','CLOSED')",
                                     (context.organization_id, commission["partner_id"]))
        if open_risks:
            raise CommercialSecurityError("unresolved partner risk prevents commission release")
        refunded = float(commission.get("refunded_amount") or 0)
        cleared = float(invoice.get("amount_received") or 0)
        excluded = max(0.0, float(excluded_amount or 0))
        base = max(0.0, cleared - excluded - (refunded / float(commission["rate"]) if commission["rate"] else 0))
        amount = round(base * float(commission["rate"]), 2)
        if amount <= 0 or amount > float(commission.get("eligible_amount") or 0) + 1e-9:
            raise CommercialSecurityError("commission exceeds allowed eligible base")
        existing = self.store.list("p6_commission_releases", "organization_id=? AND idempotency_key=?",
                                   (context.organization_id, idempotency_key))
        if existing:
            return existing[-1]
        approval = self.approvals.request(context, "COMMISSION_RELEASE", "COMMISSION", commission_id,
            {"referral_id": referral["id"], "partner_id": commission["partner_id"], "eligible_base": base,
             "rate": commission["rate"], "excluded_amount": excluded}, amount, currency)
        now = utcnow()
        release_id = self.store.create("p6_commission_releases", {
            "organization_id": context.organization_id, "commission_id": commission_id,
            "approval_request_id": approval["id"], "cleared_collections": cleared, "excluded_amount": excluded,
            "refund_chargeback_amount": refunded, "eligible_base": base, "rate": commission["rate"],
            "commission_amount": amount, "currency": currency.upper(), "status": "PENDING_APPROVAL",
            "idempotency_key": idempotency_key, "created_at": now, "updated_at": now,
        })
        return self.store.get("p6_commission_releases", release_id)

    def mark_releasable(self, context: AccessContext, release_id: str) -> Dict[str, Any]:
        self.identities.authorize(context, "finance:read", context.organization_id)
        release = self.store.get("p6_commission_releases", release_id)
        if not release or release["organization_id"] != context.organization_id:
            raise CommercialSecurityError("commission release not found")
        if release["status"] == "RELEASABLE":
            return release
        _approval(self.store, release["approval_request_id"], context.organization_id, "COMMISSION_RELEASE", release["commission_id"])
        commission = self.store.get("pm_commissions", release["commission_id"])
        if not commission or commission["status"] != "ELIGIBLE":
            raise CommercialSecurityError("commission is no longer eligible")
        open_risks = self.store.list("p6_risk_events", "organization_id=? AND partner_id=? AND status NOT IN ('CLEARED','CLOSED')",
                                     (context.organization_id, commission["partner_id"]))
        if open_risks:
            raise CommercialSecurityError("new unresolved risk invalidates release readiness")
        self.store.update("p6_commission_releases", release_id, status="RELEASABLE")
        self.accounts.record_event(context, "COMMISSION_PAYABLE", "ACCRUED_PAYABLES", "CREDIT",
            release["commission_amount"], release["currency"], "COMMISSION_RELEASE", release_id,
            {"approval_request_id": release["approval_request_id"]}, f"commission-payable:{release_id}")
        return self.store.get("p6_commission_releases", release_id)


class RefundService:
    def __init__(self, store: StateStore, audit: AuditLog, identities: CommercialIdentityStore):
        self.store, self.audit, self.identities = store, audit, identities
        self.approvals = ApprovalWorkflow(store, audit, identities)
        self.accounts = FinanceAccounts(store, audit, identities)

    def request(self, context: AccessContext, payment_id: str, amount: float, currency: str,
                reason: str, idempotency_key: str) -> Dict[str, Any]:
        self.identities.authorize(context, "approvals:make", context.organization_id)
        payment = self.store.get("p6_payment_intents", payment_id)
        if not payment or payment["organization_id"] != context.organization_id or payment["status"] not in {"SETTLED", "DISPUTED"}:
            raise CommercialSecurityError("organization-scoped settled payment required")
        if currency.upper() != payment["currency"]:
            raise CommercialSecurityError("refund currency mismatch")
        confirmed = self.store.list("p6_refunds", "payment_intent_id=? AND status='CONFIRMED'", (payment_id,))
        already = sum(float(row["amount"]) for row in confirmed)
        eligible = round(float(payment["amount"]) - already, 2)
        if amount <= 0 or amount > eligible:
            raise CommercialSecurityError("refund exceeds eligible settled amount")
        existing = self.store.list("p6_refunds", "organization_id=? AND idempotency_key=?", (context.organization_id, idempotency_key))
        if existing:
            return existing[-1]
        approval = self.approvals.request(context, "REFUND", "PAYMENT", payment_id,
            {"invoice_id": payment["invoice_id"], "eligibility": "settled amount less confirmed refunds", "reason": reason},
            amount, currency, payment["beneficiary_ref"])
        now = utcnow()
        refund_id = self.store.create("p6_refunds", {
            "organization_id": context.organization_id, "payment_intent_id": payment_id,
            "invoice_id": payment["invoice_id"], "approval_request_id": approval["id"], "amount": amount,
            "currency": currency.upper(), "eligibility_basis_json": json.dumps({"settled": payment["amount"], "already_refunded": already, "eligible": eligible}),
            "reason": reason, "status": "PENDING_APPROVAL", "provider_ref": None, "evidence_json": None,
            "idempotency_key": idempotency_key, "created_at": now, "updated_at": now,
        })
        return self.store.get("p6_refunds", refund_id)

    def confirm_sandbox(self, context: AccessContext, refund_id: str, provider_ref: str, evidence: Any) -> Dict[str, Any]:
        self.identities.authorize(context, "finance:read", context.organization_id)
        refund = self.store.get("p6_refunds", refund_id)
        if not refund or refund["organization_id"] != context.organization_id:
            raise CommercialSecurityError("refund not found")
        if refund["status"] == "CONFIRMED":
            return refund
        if not provider_ref or not evidence:
            raise CommercialSecurityError("verified sandbox provider evidence required")
        _approval(self.store, refund["approval_request_id"], context.organization_id, "REFUND", refund["payment_intent_id"])
        duplicate = self.store.list("p6_refunds", "provider_ref=? AND status='CONFIRMED'", (provider_ref,))
        if duplicate:
            raise CommercialSecurityError("duplicate refund provider reference")
        payment = self.store.get("p6_payment_intents", refund["payment_intent_id"])
        if not payment or payment["organization_id"] != context.organization_id or payment["currency"] != refund["currency"]:
            raise CommercialSecurityError("refund/payment mismatch")
        self.store.update("p6_refunds", refund_id, status="CONFIRMED", provider_ref=provider_ref,
                          evidence_json=json.dumps(evidence, sort_keys=True))
        self.accounts.record_event(context, "REFUND_CONFIRMED", "CASH", "DEBIT", refund["amount"], refund["currency"],
            "REFUND", refund_id, evidence, f"refund:{provider_ref}")
        referrals = self.store.list("pm_referrals", "opportunity_id=?", (self.store.get("rh_invoices", refund["invoice_id"]).get("opportunity_id"),))
        if referrals:
            commission = CommissionStore(self.store, self.audit).get_for_referral(referrals[-1]["id"])
            if commission and commission["status"] in {"ELIGIBLE", "PROVISIONAL", "HELD"}:
                clawback = round(float(refund["amount"]) * float(commission["rate"]), 2)
                CommissionStore(self.store, self.audit).record_refund(referrals[-1]["id"], context.identity_id,
                    clawback, evidence, "confirmed customer refund", event_ref=refund_id)
        return self.store.get("p6_refunds", refund_id)


class SubscriptionService:
    CADENCES = {"MONTHLY": 30, "ANNUAL": 365}

    def __init__(self, store: StateStore, audit: AuditLog, identities: CommercialIdentityStore):
        self.store, self.audit, self.identities = store, audit, identities

    def create(self, context: AccessContext, client_id: str, plan_name: str, amount: float, currency: str,
               cadence: str, next_billing_date: str, provider_token_ref: Optional[str], mandate_ref: Optional[str],
               opportunity_id: Optional[str] = None) -> Dict[str, Any]:
        self.identities.authorize(context, "payments:create", context.organization_id)
        if cadence not in self.CADENCES or amount <= 0 or not self.store.get("clients", client_id):
            raise CommercialSecurityError("valid client, cadence, and amount required")
        if provider_token_ref and any(term in provider_token_ref.lower() for term in ("cvv", "pan=", "card_number")):
            raise CommercialSecurityError("token references only; raw card data forbidden")
        now = utcnow()
        sid = self.store.create("p6_subscriptions", {"organization_id": context.organization_id, "client_id": client_id,
            "opportunity_id": opportunity_id, "plan_name": plan_name, "amount": amount, "currency": currency.upper(),
            "cadence": cadence, "next_billing_date": next_billing_date, "provider_token_ref": provider_token_ref,
            "mandate_ref": mandate_ref, "autopay_status": "READY" if mandate_ref else "MANUAL",
            "status": "ACTIVE", "last_invoice_id": None, "retry_count": 0, "actor": context.identity_id,
            "created_at": now, "updated_at": now})
        return self.store.get("p6_subscriptions", sid)

    def create_due_invoice(self, context: AccessContext, subscription_id: str, as_of: Optional[str] = None) -> Dict[str, Any]:
        self.identities.authorize(context, "payments:create", context.organization_id)
        sub = self.store.get("p6_subscriptions", subscription_id)
        if not sub or sub["organization_id"] != context.organization_id or sub["status"] != "ACTIVE":
            raise CommercialSecurityError("active organization-scoped subscription required")
        as_of = as_of or date.today().isoformat()
        if sub["next_billing_date"] > as_of:
            raise CommercialSecurityError("subscription is not due")
        if sub.get("last_invoice_id"):
            prior = self.store.get("rh_invoices", sub["last_invoice_id"])
            if prior and prior["created_at"][:10] == sub["next_billing_date"]:
                return prior
        invoice_id = BillingStore(self.store, self.audit).create_invoice(sub["client_id"], context.identity_id,
            sub["amount"], sub["currency"], opportunity_id=sub.get("opportunity_id"), milestone=sub["plan_name"])
        next_date = (date.fromisoformat(sub["next_billing_date"]) + timedelta(days=self.CADENCES[sub["cadence"]])).isoformat()
        self.store.update("p6_subscriptions", subscription_id, last_invoice_id=invoice_id, next_billing_date=next_date,
                          autopay_status="INVOICE_CREATED")
        return self.store.get("rh_invoices", invoice_id)


class PayableService:
    def __init__(self, store: StateStore, audit: AuditLog, identities: CommercialIdentityStore):
        self.store, self.audit, self.identities = store, audit, identities
        self.approvals = ApprovalWorkflow(store, audit, identities)

    def create(self, context: AccessContext, payable_type: str, beneficiary_ref: str, amount: float,
               currency: str, metadata: Dict[str, Any], idempotency_key: str,
               linked_project_id: Optional[str] = None, linked_budget_id: Optional[str] = None) -> Dict[str, Any]:
        self.identities.authorize(context, "approvals:make", context.organization_id)
        existing = self.store.list("p6_payables", "organization_id=? AND idempotency_key=?", (context.organization_id, idempotency_key))
        if existing:
            return existing[-1]
        if amount <= 0 or not beneficiary_ref:
            raise CommercialSecurityError("positive payable and beneficiary required")
        flags = ["NEW_BENEFICIARY"] if not self.store.list("p6_payables", "organization_id=? AND beneficiary_ref=?", (context.organization_id, beneficiary_ref)) else []
        approval = self.approvals.request(context, "VENDOR_PAYMENT", "PAYABLE", idempotency_key, metadata, amount,
                                          currency.upper(), beneficiary_ref, flags)
        now = utcnow()
        pid = self.store.create("p6_payables", {"organization_id": context.organization_id, "payable_type": payable_type,
            "beneficiary_ref": beneficiary_ref, "amount": amount, "currency": currency.upper(),
            "linked_project_id": linked_project_id, "linked_budget_id": linked_budget_id,
            "expense_metadata_json": json.dumps(metadata, sort_keys=True), "approval_request_id": approval["id"],
            "status": "PENDING_APPROVAL", "idempotency_key": idempotency_key, "created_at": now, "updated_at": now})
        return self.store.get("p6_payables", pid)

    def mark_execution_ready(self, context: AccessContext, payable_id: str) -> Dict[str, Any]:
        self.identities.authorize(context, "finance:read", context.organization_id)
        payable = self.store.get("p6_payables", payable_id)
        if not payable or payable["organization_id"] != context.organization_id:
            raise CommercialSecurityError("payable not found")
        _approval(self.store, payable["approval_request_id"], context.organization_id, "VENDOR_PAYMENT", payable["idempotency_key"])
        self.store.update("p6_payables", payable_id, status="SANDBOX_EXECUTION_READY")
        return self.store.get("p6_payables", payable_id)
