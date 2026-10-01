"""Final Phase 6 sandbox financial flows: commissions, refunds, subscriptions, payables."""

import json
import sqlite3
import uuid
from datetime import date, timedelta
from typing import Any, Dict, Optional

from .audit import AuditLog
from .billing import BillingStore
from .partner_management import CommissionStore
from .phase6_commercial import (
    AccessContext, ApprovalWorkflow, CommercialIdentityStore, CommercialSecurityError, FinanceAccounts,
    PaymentOrchestrator,
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
        # Tenant-isolation fix: pm_commissions/pm_referrals/rh_invoices
        # predate Phase 6's multi-organization commercial-identity layer and
        # carry no organization_id column of their own (confirmed against
        # schema_sqlite.sql). Without this check, a commercial identity
        # authorized under ANY organization -- not just the one that
        # actually owns this commission -- could prepare (and, on full
        # Aryan approval, release) a payable commission event under its own
        # organization_id using someone else's partner commission record: a
        # genuine cross-tenant commission release, demonstrated directly
        # (CommissionReleaseService.prepare() called by an "acme" identity
        # against a commission that only ever belonged to "ttt" succeeded
        # before this fix). Resolved the same way
        # CommercialOperations._invoice_organization() resolves ownership
        # elsewhere in this codebase: via the invoice's linked client in
        # comm_organizations.
        owning_rows = self.store.list("comm_organizations", "linked_client_id=?", (invoice["client_id"],))
        owning_organization_id = owning_rows[-1]["id"] if owning_rows else None
        if owning_organization_id != context.organization_id:
            raise CommercialSecurityError("commission does not belong to this organization")
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
        # Race/invariant fix (commission release vs commission release, and
        # commission release vs clawback): the commission's current
        # refunded_amount, its eligibility, the open-risk check, and this
        # release's own status transition must be read and applied as one
        # atomic unit. A plain read-then-write split across two tables
        # (p6_commission_releases and pm_commissions) leaves a window where
        # a concurrent refund clawback could land between the staleness
        # check and the status write, letting a release reach RELEASABLE
        # (and emit a commission-payable finance event) using a
        # commission_amount that was already stale by the time it
        # committed -- an overpayment relative to actually-settled,
        # non-refunded revenue. compare_and_set alone only guards one row's
        # own column, not an invariant spanning two tables, so this uses
        # BEGIN IMMEDIATE the same way RefundService.confirm_sandbox does
        # for its own cross-row sum check.
        with self.store.transaction_immediate() as db:
            current = db.execute("SELECT status FROM p6_commission_releases WHERE id=?", (release_id,)).fetchone()
            if not current:
                raise CommercialSecurityError("commission release not found")
            if current["status"] == "RELEASABLE":
                return self.store.get("p6_commission_releases", release_id)
            commission = db.execute(
                "SELECT status, partner_id, refunded_amount FROM pm_commissions WHERE id=?", (release["commission_id"],)
            ).fetchone()
            if not commission or commission["status"] != "ELIGIBLE":
                raise CommercialSecurityError("commission is no longer eligible")
            # `prepare()` snapshotted refund_chargeback_amount into this
            # release row at prepare-time. If that no longer matches the
            # commission's current refunded_amount -- a clawback happened
            # since, possibly this very instant via BEGIN IMMEDIATE's
            # serialization against the refund path -- the stored
            # commission_amount is stale. Fail closed and require a fresh
            # prepare() rather than release a number that may now be
            # too high.
            if round(float(commission["refunded_amount"] or 0), 2) != round(float(release["refund_chargeback_amount"]), 2):
                raise CommercialSecurityError(
                    "commission refund/clawback state changed since this release was prepared; re-prepare the release")
            open_risk = db.execute(
                "SELECT 1 FROM p6_risk_events WHERE organization_id=? AND partner_id=? "
                "AND status NOT IN ('CLEARED','CLOSED') LIMIT 1",
                (context.organization_id, commission["partner_id"]),
            ).fetchone()
            if open_risk:
                raise CommercialSecurityError("new unresolved risk invalidates release readiness")
            cursor = db.execute(
                "UPDATE p6_commission_releases SET status=?, updated_at=? WHERE id=? AND status=?",
                ("RELEASABLE", utcnow(), release_id, current["status"]),
            )
            if cursor.rowcount != 1:
                raise CommercialSecurityError("commission release changed concurrently; reload before releasing")
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
        payment = self.store.get("p6_payment_intents", refund["payment_intent_id"])
        if not payment or payment["organization_id"] != context.organization_id or payment["currency"] != refund["currency"]:
            raise CommercialSecurityError("refund/payment mismatch")
        # Race/invariant fix (refund vs refund): request()'s eligibility
        # check only looks at refunds already CONFIRMED at request time, so
        # two *different*, independently approved refund requests against
        # the same payment (different idempotency keys, different
        # provider_ref -- a plain CAS on one row can't see the other) could
        # otherwise both confirm and together exceed the settled amount.
        # This re-validates the combined total under BEGIN IMMEDIATE so the
        # read-the-sibling-sum -> decide -> flip-this-row sequence is
        # atomic against every other connection confirming a refund for
        # this same payment, not just against another attempt on this same
        # refund row.
        now = utcnow()
        with self.store.transaction_immediate() as db:
            current = db.execute("SELECT status FROM p6_refunds WHERE id=?", (refund_id,)).fetchone()
            if not current:
                raise CommercialSecurityError("refund not found")
            if current["status"] == "CONFIRMED":
                return self.store.get("p6_refunds", refund_id)
            duplicate = db.execute(
                "SELECT 1 FROM p6_refunds WHERE provider_ref=? AND status='CONFIRMED'", (provider_ref,)
            ).fetchone()
            if duplicate:
                raise CommercialSecurityError("duplicate refund provider reference")
            sibling_rows = db.execute(
                "SELECT amount FROM p6_refunds WHERE payment_intent_id=? AND status='CONFIRMED' AND id<>?",
                (refund["payment_intent_id"], refund_id),
            ).fetchall()
            already_confirmed = sum(float(row["amount"]) for row in sibling_rows)
            if round(already_confirmed + float(refund["amount"]), 2) > round(float(payment["amount"]), 2) + 1e-9:
                raise CommercialSecurityError(
                    "combined confirmed refunds would exceed the settled payment amount")
            cursor = db.execute(
                "UPDATE p6_refunds SET status=?, provider_ref=?, evidence_json=?, updated_at=? WHERE id=? AND status=?",
                ("CONFIRMED", provider_ref, json.dumps(evidence, sort_keys=True), now, refund_id, current["status"]),
            )
            if cursor.rowcount != 1:
                raise CommercialSecurityError("refund changed concurrently; reload before confirming")
        self.accounts.record_event(context, "REFUND_CONFIRMED", "CASH", "DEBIT", refund["amount"], refund["currency"],
            "REFUND", refund_id, evidence, f"refund:{provider_ref}")
        referrals = self.store.list("pm_referrals", "opportunity_id=?", (self.store.get("rh_invoices", refund["invoice_id"]).get("opportunity_id"),))
        if referrals:
            commission = CommissionStore(self.store, self.audit).get_for_referral(referrals[-1]["id"])
            if commission and commission["status"] in {"ELIGIBLE", "PROVISIONAL", "HELD"}:
                clawback = round(float(refund["amount"]) * float(commission["rate"]), 2)
                CommissionStore(self.store, self.audit).record_refund(referrals[-1]["id"], context.identity_id,
                    clawback, evidence, "confirmed customer refund", event_ref=refund_id)
        reconciliation_id = self.store.create("p6_refund_reconciliations", {
            "organization_id": context.organization_id, "refund_id": refund_id,
            "payment_intent_id": refund["payment_intent_id"], "invoice_id": refund["invoice_id"],
            "provider_ref": provider_ref, "amount": refund["amount"], "currency": refund["currency"],
            "status": "MATCHED", "evidence_json": json.dumps(evidence, sort_keys=True),
            "created_at": utcnow(), "updated_at": utcnow(),
        })
        self.audit.append("P6_REFUND_RECONCILED", {"refund_id": refund_id, "reconciliation_id": reconciliation_id,
                          "provider_ref": provider_ref, "actor": context.identity_id})
        return self.store.get("p6_refunds", refund_id)


class SubscriptionService:
    CADENCES = {"MONTHLY": 30, "ANNUAL": 365}

    def __init__(self, store: StateStore, audit: AuditLog, identities: CommercialIdentityStore):
        self.store, self.audit, self.identities = store, audit, identities
        self.payments = PaymentOrchestrator(store, audit, identities)

    def create(self, context: AccessContext, client_id: str, plan_name: str, amount: float, currency: str,
               cadence: str, next_billing_date: str, provider_token_ref: Optional[str], mandate_ref: Optional[str],
               opportunity_id: Optional[str] = None) -> Dict[str, Any]:
        self.identities.authorize(context, "payments:create", context.organization_id)
        if cadence not in self.CADENCES or amount <= 0 or not self.store.get("clients", client_id):
            raise CommercialSecurityError("valid client, cadence, and amount required")
        # Tenant-isolation fix: `clients` predates Phase 6's organization
        # layer and carries no organization_id of its own (same root cause
        # as the CommissionReleaseService.prepare() cross-tenant gap fixed
        # above). Without this, a commercial identity from ANY organization
        # could create a recurring subscription -- and later autopay
        # attempts and invoices -- against a client that actually belongs to
        # a completely different organization (demonstrated directly: an
        # "acme" identity successfully created a subscription against a
        # client only ever linked to "ttt" before this fix). Same
        # resolution pattern as CommercialOperations._invoice_organization()
        # and the commission-release fix: ownership is resolved via the
        # client's link in comm_organizations and must match the caller.
        owning_rows = self.store.list("comm_organizations", "linked_client_id=?", (client_id,))
        owning_organization_id = owning_rows[-1]["id"] if owning_rows else None
        if owning_organization_id != context.organization_id:
            raise CommercialSecurityError("client does not belong to this organization")
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
        cycle_date = sub["next_billing_date"]
        now = utcnow()
        with self.store.transaction() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO p6_subscription_cycles "
                "(id,subscription_id,cycle_date,invoice_id,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), subscription_id, cycle_date, None, "GENERATING", now, now),
            )
        cycles = self.store.list("p6_subscription_cycles", "subscription_id=? AND cycle_date=?", (subscription_id, cycle_date))
        cycle = cycles[-1]
        if cursor.rowcount == 0:
            if cycle.get("invoice_id"):
                return self.store.get("rh_invoices", cycle["invoice_id"])
            raise CommercialSecurityError("recurring invoice generation is already in progress")
        invoice_id = BillingStore(self.store, self.audit).create_invoice(sub["client_id"], context.identity_id,
            sub["amount"], sub["currency"], opportunity_id=sub.get("opportunity_id"), milestone=sub["plan_name"])
        self.store.update("p6_subscription_cycles", cycle["id"], invoice_id=invoice_id, status="INVOICED")
        next_date = (date.fromisoformat(cycle_date) + timedelta(days=self.CADENCES[sub["cadence"]])).isoformat()
        self.store.update("p6_subscriptions", subscription_id, last_invoice_id=invoice_id, next_billing_date=next_date,
                          autopay_status="INVOICE_CREATED")
        return self.store.get("rh_invoices", invoice_id)

    def create_autopay_attempt(self, context: AccessContext, subscription_id: str, idempotency_key: str) -> Dict[str, Any]:
        self.identities.authorize(context, "payments:create", context.organization_id)
        sub = self.store.get("p6_subscriptions", subscription_id)
        if not sub or sub["organization_id"] != context.organization_id or sub["status"] != "ACTIVE":
            raise CommercialSecurityError("active organization-scoped subscription required")
        if not sub.get("last_invoice_id") or not sub.get("mandate_ref") or not sub.get("provider_token_ref"):
            raise CommercialSecurityError("due invoice and provider token/mandate references required")
        existing = self.store.list("p6_subscription_attempts", "organization_id=? AND idempotency_key=?",
                                   (context.organization_id, idempotency_key))
        if existing:
            return existing[-1]
        invoice = self.store.get("rh_invoices", sub["last_invoice_id"])
        if not invoice or invoice["status"] == "PAID":
            raise CommercialSecurityError("unpaid recurring invoice required")
        cycle_date = invoice["created_at"][:10]
        intent = self.payments.create_intent(context, context.organization_id, sub["amount"], sub["currency"],
            "SUBSCRIPTION", f"subscription-payment:{idempotency_key}", "TTT", "beneficiary:official", ["CARD"],
            invoice_id=invoice["id"], metadata={"subscription_id": subscription_id})
        self.store.update("p6_payment_intents", intent["id"], payment_method_token_ref=sub["provider_token_ref"],
                          mandate_token_ref=sub["mandate_ref"])
        # Race-matrix fix (recurring retry vs delayed original event):
        # attempt_number is derived from how many attempt rows already
        # exist for this subscription+cycle. Two independently triggered
        # attempts for the same cycle -- an automatic retry racing a
        # delayed original provider event, each carrying its own
        # idempotency_key -- could otherwise both read "0 prior attempts"
        # and both try to claim attempt_number=1. UNIQUE(subscription_id,
        # cycle_date, attempt_number) guarantees only one of them can ever
        # persist that number; this loop catches the loser's
        # IntegrityError, re-reads the now-current count under the fresh
        # committed state, and retries with the next free number. Each
        # retry re-reads rather than reusing a stale count, so the final
        # numbering is always correct and gap-free regardless of how many
        # callers raced here.
        for _ in range(5):
            prior = self.store.list("p6_subscription_attempts", "subscription_id=? AND cycle_date=?", (subscription_id, cycle_date))
            attempt_number = len(prior) + 1
            try:
                attempt_id = self.store.create("p6_subscription_attempts", {
                    "organization_id": context.organization_id, "subscription_id": subscription_id, "invoice_id": invoice["id"],
                    "payment_intent_id": intent["id"], "cycle_date": cycle_date,
                    "attempt_number": attempt_number, "status": "PENDING_PROVIDER", "failure_reason": None,
                    "idempotency_key": idempotency_key, "created_at": utcnow(), "updated_at": utcnow(),
                })
            except sqlite3.IntegrityError:
                existing = self.store.list("p6_subscription_attempts", "organization_id=? AND idempotency_key=?",
                                           (context.organization_id, idempotency_key))
                if existing:
                    return existing[-1]
                continue
            self.store.update("p6_subscriptions", subscription_id, autopay_status="PENDING_PROVIDER")
            return self.store.get("p6_subscription_attempts", attempt_id)
        raise CommercialSecurityError("could not allocate a unique autopay attempt number; retry")

    def mark_attempt_failed(self, context: AccessContext, attempt_id: str, reason: str) -> Dict[str, Any]:
        self.identities.authorize(context, "payments:create", context.organization_id)
        attempt = self.store.get("p6_subscription_attempts", attempt_id)
        if not attempt or attempt["organization_id"] != context.organization_id or not reason.strip():
            raise CommercialSecurityError("organization-scoped attempt and failure reason required")
        if attempt["status"] == "FAILED":
            return attempt
        if attempt["status"] != "PENDING_PROVIDER":
            raise CommercialSecurityError("only pending autopay attempt may fail")
        self.store.update("p6_subscription_attempts", attempt_id, status="FAILED", failure_reason=reason)
        sub = self.store.get("p6_subscriptions", attempt["subscription_id"])
        self.store.update("p6_subscriptions", sub["id"], autopay_status="RETRY_REQUIRED", retry_count=int(sub["retry_count"])+1)
        return self.store.get("p6_subscription_attempts", attempt_id)

    def sync_collected(self, context: AccessContext, attempt_id: str) -> Dict[str, Any]:
        self.identities.authorize(context, "finance:read", context.organization_id)
        attempt = self.store.get("p6_subscription_attempts", attempt_id)
        if not attempt or attempt["organization_id"] != context.organization_id:
            raise CommercialSecurityError("autopay attempt not found")
        if attempt["status"] == "COLLECTED":
            return attempt
        payment = self.store.get("p6_payment_intents", attempt["payment_intent_id"])
        reconciliations = self.store.list("p6_reconciliations", "payment_intent_id=? AND status IN ('MATCHED','PARTIAL')", (payment["id"],))
        if payment["status"] != "SETTLED" or not reconciliations:
            raise CommercialSecurityError("verified settled and reconciled recurring payment required")
        self.store.update("p6_subscription_attempts", attempt_id, status="COLLECTED")
        self.store.update("p6_subscriptions", attempt["subscription_id"], autopay_status="COLLECTED", retry_count=0)
        return self.store.get("p6_subscription_attempts", attempt_id)

    def set_status(self, context: AccessContext, subscription_id: str, status: str) -> Dict[str, Any]:
        self.identities.authorize(context, "payments:create", context.organization_id)
        sub = self.store.get("p6_subscriptions", subscription_id)
        if not sub or sub["organization_id"] != context.organization_id or status not in {"ACTIVE", "PAUSED", "CANCELLED", "EXPIRED"}:
            raise CommercialSecurityError("valid organization-scoped subscription status required")
        if sub["status"] in {"CANCELLED", "EXPIRED"} and status == "ACTIVE":
            raise CommercialSecurityError("terminal subscription cannot resume")
        self.store.update("p6_subscriptions", subscription_id, status=status,
                          autopay_status="READY" if status == "ACTIVE" else status)
        return self.store.get("p6_subscriptions", subscription_id)


class CommercialEconomicsService:
    CHANNELS = {"WEBSITE_INBOUND", "REFERRAL_PARTNER", "MARKETPLACE", "AGENCY_WHITE_LABEL",
                "CUSTOMER_EXPANSION", "RFP_TENDER", "PERMITTED_OUTBOUND", "PRODUCTIZED_PAGE", "SELF_SERVICE"}

    def __init__(self, store: StateStore, audit: AuditLog, identities: CommercialIdentityStore):
        self.store, self.audit, self.identities = store, audit, identities

    def record(self, context: AccessContext, opportunity_id: str, source_channel: str, source_metadata: Dict[str, Any],
               origin_partner_id: Optional[str] = None, quoted_value: Optional[float] = None,
               contracted_value: Optional[float] = None, estimated_delivery_cost: Optional[float] = None,
               known_delivery_cost: Optional[float] = None, cost_evidence: Optional[Any] = None,
               gateway_fee: float = 0, service_tier: Optional[str] = None) -> Dict[str, Any]:
        self.identities.authorize(context, "finance:read", context.organization_id)
        if source_channel not in self.CHANNELS or not self.store.get("rh_opportunities", opportunity_id):
            raise CommercialSecurityError("valid opportunity and source channel required")
        if known_delivery_cost is not None and not cost_evidence:
            raise CommercialSecurityError("known delivery cost requires evidence")
        values = {"source_channel": source_channel, "source_metadata_json": json.dumps(source_metadata, sort_keys=True),
                  "origin_partner_id": origin_partner_id, "quoted_value": quoted_value, "contracted_value": contracted_value,
                  "estimated_delivery_cost": estimated_delivery_cost, "known_delivery_cost": known_delivery_cost,
                  "cost_evidence_json": json.dumps(cost_evidence, sort_keys=True) if cost_evidence else None,
                  "gateway_fee": gateway_fee, "service_tier": service_tier, "actor": context.identity_id}
        rows = self.store.list("p6_opportunity_economics", "organization_id=? AND opportunity_id=?", (context.organization_id, opportunity_id))
        if rows:
            self.store.update("p6_opportunity_economics", rows[-1]["id"], **values)
            return self.store.get("p6_opportunity_economics", rows[-1]["id"])
        values.update({"organization_id": context.organization_id, "opportunity_id": opportunity_id,
                       "created_at": utcnow(), "updated_at": utcnow()})
        eid = self.store.create("p6_opportunity_economics", values)
        return self.store.get("p6_opportunity_economics", eid)

    def analytics(self, context: AccessContext) -> Dict[str, Any]:
        self.identities.authorize(context, "finance:read", context.organization_id)
        rows = self.store.list("p6_opportunity_economics", "organization_id=?", (context.organization_id,))
        result: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            channel = result.setdefault(row["source_channel"], {"opportunities": 0, "won": 0, "settled_revenue": 0.0,
                "refunds": 0.0, "known_contribution": None})
            channel["opportunities"] += 1
            opportunity = self.store.get("rh_opportunities", row["opportunity_id"])
            if opportunity and str(opportunity.get("stage", "")).lower() == "won": channel["won"] += 1
            invoices = self.store.list("rh_invoices", "opportunity_id=?", (row["opportunity_id"],))
            invoice_ids = [i["id"] for i in invoices]
            collected = sum(float(i.get("amount_received") or 0) for i in invoices)
            refunds = sum(float(r["amount"]) for iid in invoice_ids for r in self.store.list("p6_refunds", "invoice_id=? AND status='CONFIRMED'", (iid,)))
            channel["settled_revenue"] += collected
            channel["refunds"] += refunds
            cost = row.get("known_delivery_cost")
            if cost is not None:
                contribution = collected - refunds - float(cost) - float(row.get("gateway_fee") or 0)
                channel["known_contribution"] = round((channel["known_contribution"] or 0) + contribution, 2)
        return {"organization_id": context.organization_id, "channels": result,
                "note": "Contribution is null unless known delivery cost evidence exists; CAC and margin are not inferred."}


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
        if payable["status"] == "SANDBOX_EXECUTION_READY":
            return payable
        _approval(self.store, payable["approval_request_id"], context.organization_id, "VENDOR_PAYMENT", payable["idempotency_key"])
        # Race-matrix fix (payable release duplication): CAS the transition
        # so two concurrent mark_execution_ready() calls -- both passing
        # the approval check above against the same already-approved
        # request -- produce exactly one execution-ready transition; the
        # second call observes the first's committed state and returns it
        # rather than re-applying an unconditional update.
        self.store.compare_and_set("p6_payables", payable_id, {"status": payable["status"]},
                                   status="SANDBOX_EXECUTION_READY")
        return self.store.get("p6_payables", payable_id)
