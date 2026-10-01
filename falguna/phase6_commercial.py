"""TTT-owned Phase 6 commercial security, sandbox payments, and finance.

FALGUNA may consume this state and prepare recommendations. It cannot approve
or execute money movement, beneficiary changes, refunds, or commissions.
"""

import hashlib
import hmac
import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional

from .audit import AuditLog
from .billing import BillingStore
from .store import StateStore, utcnow

ROLES = {"CUSTOMER", "PARTNER", "FINANCE_OPERATOR", "SALES_OPERATOR", "ADMIN", "OWNER"}
ROLE_PERMISSIONS = {
    "CUSTOMER": {"payments:read_own"},
    "PARTNER": {"partners:read_own", "leads:create"},
    "FINANCE_OPERATOR": {"payments:read", "payments:create", "finance:read", "approvals:make", "approvals:verify"},
    "SALES_OPERATOR": {"payments:read", "payments:create", "opportunities:manage"},
    "ADMIN": {"payments:read", "payments:create", "finance:read", "approvals:make", "approvals:verify", "identities:manage"},
    "OWNER": {"*"},
}
PAYMENT_KINDS = {"ONE_TIME", "MILESTONE", "SUBSCRIPTION", "RETAINER", "BANK_TRANSFER"}
PAYMENT_METHODS = {"UPI", "CARD", "NET_BANKING", "WALLET", "LOCAL_METHOD", "INTERNATIONAL_CARD", "BANK_TRANSFER", "WIRE", "EMI_METADATA"}
TRANSITIONS = {
    "CREATED": {"AUTHORIZED", "CAPTURED", "FAILED"}, "AUTHORIZED": {"CAPTURED", "FAILED"},
    "CAPTURED": {"SETTLED", "REFUND_REQUESTED", "DISPUTED"},
    "SETTLED": {"REFUND_REQUESTED", "DISPUTED"}, "REFUND_REQUESTED": {"REFUNDED", "DISPUTED"},
    "DISPUTED": {"REFUNDED", "SETTLED"}, "FAILED": set(), "REFUNDED": set(),
}
OUTGOING_ACTIONS = {"REFUND", "COMMISSION_RELEASE", "VENDOR_PAYMENT", "BENEFICIARY_CHANGE"}
AI_ACTORS = {"FALGUNA", "AI", "MODEL", "AGENT"}


class CommercialSecurityError(ValueError):
    pass


@dataclass(frozen=True)
class AccessContext:
    identity_id: str
    organization_id: str


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class CommercialIdentityStore:
    def __init__(self, store: StateStore, audit: AuditLog):
        self.store, self.audit = store, audit

    def create(self, organization_id: str, subject_type: str, subject_ref: str, display_name: str,
               role: str, actor: str, permissions: Optional[Iterable[str]] = None) -> str:
        if role not in ROLES or not all((organization_id, subject_type, subject_ref, display_name, actor)):
            raise CommercialSecurityError("valid identity fields and commercial role are required")
        granted = sorted(set(ROLE_PERMISSIONS[role]) | set(permissions or []))
        now = utcnow()
        identity_id = self.store.create("p6_commercial_identities", {
            "organization_id": organization_id, "subject_type": subject_type, "subject_ref": subject_ref,
            "display_name": display_name, "role": role, "permissions_json": _json(granted),
            "status": "ACTIVE", "actor": actor, "created_at": now, "updated_at": now,
        })
        self.audit.append("P6_COMMERCIAL_IDENTITY_CREATED", {"identity_id": identity_id, "organization_id": organization_id, "role": role, "actor": actor})
        return identity_id

    def authorize(self, context: AccessContext, permission: str, organization_id: str) -> Dict[str, Any]:
        identity = self.store.get("p6_commercial_identities", context.identity_id)
        if not identity or identity["status"] != "ACTIVE":
            raise CommercialSecurityError("active commercial identity required")
        if identity["organization_id"] != context.organization_id or organization_id != context.organization_id:
            raise CommercialSecurityError("cross-organization access denied")
        permissions = set(json.loads(identity["permissions_json"]))
        if "*" not in permissions and permission not in permissions:
            raise CommercialSecurityError(f"permission denied: {permission}")
        return identity


class PaymentOrchestrator:
    """Provider-neutral payment state machine; records verified facts only."""

    def __init__(self, store: StateStore, audit: AuditLog, identities: CommercialIdentityStore):
        self.store, self.audit, self.identities = store, audit, identities

    def create_intent(self, context: AccessContext, customer_ref: str, amount: float, currency: str,
                      kind: str, idempotency_key: str, legal_owner_name: str, beneficiary_ref: str,
                      allowed_methods: Iterable[str], invoice_id: Optional[str] = None,
                      capture_mode: str = "MANUAL", metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self.identities.authorize(context, "payments:create", context.organization_id)
        if kind not in PAYMENT_KINDS or amount is None or amount <= 0:
            raise CommercialSecurityError("valid payment kind and positive amount required")
        methods = sorted(set(allowed_methods))
        if not methods or set(methods) - PAYMENT_METHODS:
            raise CommercialSecurityError("unsupported payment method capability")
        serialized = _json(metadata or {})
        if any(term in serialized.lower() for term in ('"pan"', 'cvv', 'card_number', 'cardnumber')):
            raise CommercialSecurityError("raw card data is forbidden; store provider token references only")
        existing = self.store.list("p6_payment_intents", "organization_id=? AND idempotency_key=?", (context.organization_id, idempotency_key))
        if existing:
            row = existing[-1]
            if (float(row["amount"]), row["currency"], row["customer_ref"]) != (float(amount), currency.upper(), customer_ref):
                raise CommercialSecurityError("idempotency key reused with different payment data")
            return row
        now = utcnow()
        payment_id = self.store.create("p6_payment_intents", {
            "organization_id": context.organization_id, "customer_ref": customer_ref, "invoice_id": invoice_id,
            "kind": kind, "amount": round(float(amount), 2), "currency": currency.upper(), "status": "CREATED",
            "capture_mode": capture_mode, "allowed_methods_json": _json(methods), "provider": "SANDBOX_ADAPTER",
            "provider_session_ref": None, "provider_transaction_ref": None, "payment_method_token_ref": None,
            "mandate_token_ref": None, "milestone_ref": None, "legal_owner_name": legal_owner_name,
            "beneficiary_ref": beneficiary_ref, "idempotency_key": idempotency_key, "metadata_json": serialized,
            "actor": context.identity_id, "created_at": now, "updated_at": now,
        })
        self._event(payment_id, context.organization_id, "INTENT_CREATED", None, "CREATED", amount,
                    currency.upper(), {"source": "TTT_HQ", "sandbox": True}, f"intent:{idempotency_key}", context.identity_id)
        return self.store.get("p6_payment_intents", payment_id)

    def _event(self, payment_id: str, organization_id: str, event_type: str, before: Optional[str], after: str,
               amount: Optional[float], currency: Optional[str], evidence: Any, key: str, actor: str,
               provider_event_ref: Optional[str] = None) -> Dict[str, Any]:
        prior = self.store.list("p6_payment_events", "payment_intent_id=? AND idempotency_key=?", (payment_id, key))
        if prior:
            return prior[-1]
        # Race-matrix fix: see FinanceAccounts.record_event -- same
        # check-then-act shape, same UNIQUE(payment_intent_id,
        # idempotency_key) backstop, same graceful fallback to the row the
        # winning connection actually persisted.
        try:
            event_id = self.store.create("p6_payment_events", {
                "payment_intent_id": payment_id, "organization_id": organization_id, "event_type": event_type,
                "status_before": before, "status_after": after, "amount": amount, "currency": currency,
                "evidence_json": _json(evidence), "provider_event_ref": provider_event_ref,
                "idempotency_key": key, "actor": actor, "created_at": utcnow(),
            })
        except sqlite3.IntegrityError:
            prior = self.store.list("p6_payment_events", "payment_intent_id=? AND idempotency_key=?", (payment_id, key))
            if not prior:
                raise
            return prior[-1]
        return self.store.get("p6_payment_events", event_id)

    def apply_verified_event(self, context: AccessContext, payment_id: str, new_status: str, amount: float,
                             currency: str, evidence: Any, idempotency_key: str,
                             provider_event_ref: Optional[str] = None) -> Dict[str, Any]:
        payment = self.store.get("p6_payment_intents", payment_id)
        if not payment:
            raise CommercialSecurityError("payment intent not found")
        self.identities.authorize(context, "payments:read", payment["organization_id"])
        prior = self.store.list("p6_payment_events", "payment_intent_id=? AND idempotency_key=?", (payment_id, idempotency_key))
        if prior:
            return self.store.get("p6_payment_intents", payment_id)
        if not evidence or new_status not in TRANSITIONS.get(payment["status"], set()):
            raise CommercialSecurityError("verified evidence and a valid payment transition are required")
        if round(float(amount), 2) != round(float(payment["amount"]), 2) or currency.upper() != payment["currency"]:
            raise CommercialSecurityError("provider amount/currency mismatch")
        self._event(payment_id, payment["organization_id"], f"PAYMENT_{new_status}", payment["status"], new_status,
                    amount, currency.upper(), evidence, idempotency_key, context.identity_id, provider_event_ref)
        self.store.update("p6_payment_intents", payment_id, status=new_status, provider_transaction_ref=provider_event_ref)
        self.audit.append("P6_PAYMENT_STATE_CHANGED", {"payment_id": payment_id, "from": payment["status"], "to": new_status, "actor": context.identity_id})
        return self.store.get("p6_payment_intents", payment_id)

    def ingest_webhook(self, provider: str, provider_event_ref: str, organization_id: str, raw_body: bytes,
                       signature: str, secret: str, payment_id: str) -> Dict[str, Any]:
        expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature or ""):
            raise CommercialSecurityError("webhook signature verification failed")
        digest = hashlib.sha256(raw_body).hexdigest()
        replay = self.store.list("p6_webhook_events", "provider=? AND provider_event_ref=?", (provider, provider_event_ref))
        if replay:
            if replay[-1]["payload_hash"] != digest:
                raise CommercialSecurityError("webhook replay reference has a different payload")
            return replay[-1]
        payment = self.store.get("p6_payment_intents", payment_id)
        if not payment or payment["organization_id"] != organization_id:
            raise CommercialSecurityError("webhook payment organization mismatch")
        now = utcnow()
        try:
            event_id = self.store.create("p6_webhook_events", {
                "provider": provider, "provider_event_ref": provider_event_ref, "payment_intent_id": payment_id,
                "organization_id": organization_id, "signature_verified": 1, "payload_hash": digest,
                "processing_status": "VERIFIED_PENDING_APPLICATION", "failure_reason": None,
                "received_at": now, "created_at": now,
            })
        except sqlite3.IntegrityError:
            replay = self.store.list("p6_webhook_events", "provider=? AND provider_event_ref=?", (provider, provider_event_ref))
            if not replay or replay[-1]["payload_hash"] != digest:
                raise CommercialSecurityError("webhook replay conflict")
            return replay[-1]
        return self.store.get("p6_webhook_events", event_id)


class ApprovalWorkflow:
    """Maker -> verifier -> Aryan approval. Deliberately has no execute method."""

    def __init__(self, store: StateStore, audit: AuditLog, identities: CommercialIdentityStore):
        self.store, self.audit, self.identities = store, audit, identities

    def _event(self, row: Dict[str, Any], event_type: str, before: Optional[str], after: str,
               actor_identity_id: str, details: Optional[Dict[str, Any]] = None) -> None:
        protected = {key: row.get(key) for key in ("action_type", "target_type", "target_id", "amount",
                     "currency", "beneficiary_ref", "payload_json", "risk_flags_json")}
        self.store.create("p6_approval_events", {
            "approval_request_id": row["id"], "organization_id": row["organization_id"],
            "event_type": event_type, "status_before": before, "status_after": after,
            "actor_identity_id": actor_identity_id,
            "snapshot_hash": hashlib.sha256(_json(protected).encode()).hexdigest(),
            "details_json": _json(details or {}), "created_at": utcnow(),
        })

    def request(self, context: AccessContext, action_type: str, target_type: str, target_id: str,
                payload: Dict[str, Any], amount: Optional[float] = None, currency: Optional[str] = None,
                beneficiary_ref: Optional[str] = None, risk_flags: Optional[Iterable[str]] = None) -> Dict[str, Any]:
        maker = self.identities.authorize(context, "approvals:make", context.organization_id)
        if maker["display_name"].strip().upper() in AI_ACTORS or action_type not in OUTGOING_ACTIONS:
            raise CommercialSecurityError("AI actors and unsupported outgoing actions are forbidden")
        flags = set(risk_flags or [])
        if action_type == "BENEFICIARY_CHANGE": flags.add("BENEFICIARY_CHANGE")
        if amount is not None and amount >= 100000: flags.add("HIGH_VALUE")
        now = utcnow()
        request_id = self.store.create("p6_approval_requests", {
            "organization_id": context.organization_id, "action_type": action_type, "target_type": target_type,
            "target_id": target_id, "amount": amount, "currency": currency, "beneficiary_ref": beneficiary_ref,
            "risk_flags_json": _json(sorted(flags)), "payload_json": _json(payload), "status": "PENDING_VERIFICATION",
            "maker_identity_id": maker["id"], "verifier_identity_id": None, "final_approver_identity_id": None,
            "maker_at": now, "verified_at": None, "approved_at": None, "rejection_reason": None,
            "execution_reference": None, "created_at": now, "updated_at": now,
        })
        row = self.store.get("p6_approval_requests", request_id)
        self._event(row, "REQUESTED", None, "PENDING_VERIFICATION", maker["id"])
        return row

    def verify(self, context: AccessContext, request_id: str) -> Dict[str, Any]:
        row = self._scoped(context, request_id, "approvals:verify")
        if row["status"] != "PENDING_VERIFICATION" or row["maker_identity_id"] == context.identity_id:
            raise CommercialSecurityError("independent verifier required")
        # Race-matrix fix: a plain update() here would let a concurrent
        # reject() (or another verify()) on the same stale read silently
        # overwrite this one. compare_and_set re-checks status=PENDING_
        # VERIFICATION atomically at write time, so only the first of two
        # racing decisions on this request can ever land.
        won = self.store.compare_and_set("p6_approval_requests", request_id,
            {"status": "PENDING_VERIFICATION"}, status="PENDING_ARYAN_APPROVAL",
            verifier_identity_id=context.identity_id, verified_at=utcnow())
        if not won:
            raise CommercialSecurityError("approval request changed concurrently; reload before verifying")
        updated = self.store.get("p6_approval_requests", request_id)
        self._event(updated, "VERIFIED", row["status"], updated["status"], context.identity_id)
        return updated

    def approve_by_aryan(self, context: AccessContext, request_id: str) -> Dict[str, Any]:
        # Final-approver fix: the previous check compared the identity's
        # subject_ref to the literal string "aryan". Real HTTP sessions
        # resolve subject_ref to the staff account's immutable, server-
        # assigned id (TTTHQHandler._commercial_context() always sets
        # subject_type="STAFF_USER", subject_ref=<site_staff_users.id>,
        # which StaffAuthService.create_user() always generates fresh) --
        # that id can never equal the literal "aryan", so no real login,
        # including the real Aryan's, could ever pass the old check.
        # Authorization now rests entirely on the identity's own
        # persisted, immutable record via the existing permission
        # framework: _scoped()/authorize() requires an ACTIVE commercial
        # identity, organization-scoped to this request (cross-
        # organization denied), holding the explicit "approvals:final_
        # approve" permission. Only the OWNER role carries that
        # permission (via its "*" grant in ROLE_PERMISSIONS); ADMIN and
        # FINANCE_OPERATOR identities do not and are denied regardless
        # of display_name or any other mutable/claimed field. Nothing
        # but the stored role/permissions on the authenticated identity
        # row decides this.
        row = self._scoped(context, request_id, "approvals:final_approve")
        if row["status"] != "PENDING_ARYAN_APPROVAL" or row["verifier_identity_id"] == context.identity_id:
            raise CommercialSecurityError("verified request and independent final approver required")
        # Race-matrix fix: expected includes verifier_identity_id, not just
        # status, so a concurrent amend() (material beneficiary/amount
        # change) that resets verifier_identity_id to None between the read
        # above and this write makes the CAS fail -- the old verification
        # can never authorize the changed request, even under a race.
        won = self.store.compare_and_set("p6_approval_requests", request_id,
            {"status": "PENDING_ARYAN_APPROVAL", "verifier_identity_id": row["verifier_identity_id"]},
            status="APPROVED_PENDING_EXECUTION", final_approver_identity_id=context.identity_id, approved_at=utcnow())
        if not won:
            raise CommercialSecurityError("approval request changed concurrently; reload before approving")
        updated = self.store.get("p6_approval_requests", request_id)
        self._event(updated, "ARYAN_APPROVED", row["status"], updated["status"], context.identity_id)
        return updated

    def amend(self, context: AccessContext, request_id: str, payload: Dict[str, Any],
              amount: Optional[float] = None, currency: Optional[str] = None,
              beneficiary_ref: Optional[str] = None) -> Dict[str, Any]:
        row = self._scoped(context, request_id, "approvals:make")
        if row["maker_identity_id"] != context.identity_id:
            raise CommercialSecurityError("only the original maker may amend the request")
        if row["status"] not in {"PENDING_VERIFICATION", "PENDING_ARYAN_APPROVAL"}:
            raise CommercialSecurityError("only a pending request may be amended")
        before = row["status"]
        # Race-matrix fix: CAS on the exact status just read. If an
        # approve_by_aryan() or reject() committed first (beneficiary
        # change arriving mid-approval), this amend loses the race and
        # fails closed instead of silently re-opening an already-decided
        # request behind that decision's back.
        won = self.store.compare_and_set("p6_approval_requests", request_id, {"status": before},
            payload_json=_json(payload), amount=amount, currency=currency, beneficiary_ref=beneficiary_ref,
            status="PENDING_VERIFICATION", verifier_identity_id=None, verified_at=None,
            final_approver_identity_id=None, approved_at=None)
        if not won:
            raise CommercialSecurityError("approval request changed concurrently; reload before amending")
        updated = self.store.get("p6_approval_requests", request_id)
        self._event(updated, "MATERIAL_CHANGE_REQUIRES_REVERIFICATION", before, updated["status"],
                    context.identity_id, {"prior_verification_invalidated": before == "PENDING_ARYAN_APPROVAL"})
        return updated

    def reject(self, context: AccessContext, request_id: str, reason: str) -> Dict[str, Any]:
        row = self._scoped(context, request_id, "approvals:verify")
        if not (reason or "").strip():
            raise CommercialSecurityError("rejection reason is required")
        if row["status"] not in {"PENDING_VERIFICATION", "PENDING_ARYAN_APPROVAL"}:
            raise CommercialSecurityError("only a pending request may be rejected")
        # Race-matrix fix: CAS on the exact status just read, so a
        # concurrent approve_by_aryan()/verify()/amend() that already moved
        # this request off that status makes the reject lose cleanly rather
        # than overwrite a decision that already landed.
        won = self.store.compare_and_set("p6_approval_requests", request_id, {"status": row["status"]},
            status="REJECTED", rejection_reason=reason)
        if not won:
            raise CommercialSecurityError("approval request changed concurrently; reload before rejecting")
        updated = self.store.get("p6_approval_requests", request_id)
        self._event(updated, "REJECTED", row["status"], "REJECTED", context.identity_id, {"reason": reason})
        return updated

    def queue(self, context: AccessContext) -> Dict[str, Any]:
        self.identities.authorize(context, "finance:read", context.organization_id)
        items = list(reversed(self.store.list("p6_approval_requests", "organization_id=?", (context.organization_id,))))
        for item in items:
            item["events"] = self.store.list("p6_approval_events", "approval_request_id=?", (item["id"],))
        return {"organization_id": context.organization_id, "items": items}

    def _scoped(self, context: AccessContext, request_id: str, permission: str) -> Dict[str, Any]:
        row = self.store.get("p6_approval_requests", request_id)
        if not row:
            raise CommercialSecurityError("approval request not found")
        self.identities.authorize(context, permission, row["organization_id"])
        return row


class FinanceAccounts:
    """Append-only financial events and dynamic protected/free cash."""

    PROTECTED_BUCKETS = {"ESTIMATED_TAX", "CUSTOMER_HELD", "ACCRUED_PAYABLES", "COMMITTED_COSTS", "OPERATING_RESERVE", "DISPUTED_FUNDS"}

    def __init__(self, store: StateStore, audit: AuditLog, identities: CommercialIdentityStore):
        self.store, self.audit, self.identities = store, audit, identities

    def record_event(self, context: AccessContext, event_type: str, bucket: str, direction: str, amount: float,
                     currency: str, source_type: str, source_id: str, evidence: Any, idempotency_key: str,
                     reverses_event_id: Optional[str] = None) -> Dict[str, Any]:
        self.identities.authorize(context, "finance:read", context.organization_id)
        if direction not in {"CREDIT", "DEBIT"} or amount is None or amount <= 0 or not evidence:
            raise CommercialSecurityError("valid direction, positive amount, and evidence are required")
        existing = self.store.list("p6_financial_events", "organization_id=? AND idempotency_key=?", (context.organization_id, idempotency_key))
        if existing:
            return existing[-1]
        # Race-matrix fix: the list-then-create above is check-then-act --
        # two connections can both see "no existing event" and both reach
        # this insert (e.g. two concurrent settlement reconciliations, or a
        # commission release racing another release attempt on the same
        # idempotency key). The UNIQUE(organization_id, idempotency_key)
        # constraint in schema_sqlite.sql is what actually prevents a
        # second financial consequence from ever being persisted; this
        # catches the resulting IntegrityError from the loser and returns
        # the winner's row instead of raising, so cash is credited/debited
        # exactly once and the caller still gets a normal idempotent result
        # rather than an unhandled database exception.
        try:
            event_id = self.store.create("p6_financial_events", {
                "organization_id": context.organization_id, "event_type": event_type, "account_bucket": bucket,
                "direction": direction, "amount": round(float(amount), 2), "currency": currency.upper(),
                "source_type": source_type, "source_id": source_id, "evidence_json": _json(evidence),
                "idempotency_key": idempotency_key, "actor": context.identity_id,
                "reverses_event_id": reverses_event_id, "created_at": utcnow(),
            })
        except sqlite3.IntegrityError:
            existing = self.store.list("p6_financial_events", "organization_id=? AND idempotency_key=?",
                                       (context.organization_id, idempotency_key))
            if not existing:
                raise
            return existing[-1]
        return self.store.get("p6_financial_events", event_id)

    def set_reserve_policy(self, context: AccessContext, currency: str, essential_monthly_burn: float,
                           minimum_runway_months: float = 3, target_runway_months: float = 6,
                           operating_reserve_override: Optional[float] = None) -> Dict[str, Any]:
        identity = self.identities.authorize(context, "*", context.organization_id)
        if identity["role"] != "OWNER" or min(essential_monthly_burn, minimum_runway_months, target_runway_months) < 0:
            raise CommercialSecurityError("owner and non-negative reserve policy values required")
        rows = self.store.list("p6_reserve_policies", "organization_id=?", (context.organization_id,))
        values = {"currency": currency.upper(), "essential_monthly_burn": essential_monthly_burn,
                  "minimum_runway_months": minimum_runway_months, "target_runway_months": target_runway_months,
                  "operating_reserve_override": operating_reserve_override,
                  "policy_notes": "Policy default; not legal/accounting advice", "actor": context.identity_id}
        if rows:
            self.store.update("p6_reserve_policies", rows[-1]["id"], **values)
            return self.store.get("p6_reserve_policies", rows[-1]["id"])
        now = utcnow(); values.update({"organization_id": context.organization_id, "created_at": now, "updated_at": now})
        policy_id = self.store.create("p6_reserve_policies", values)
        return self.store.get("p6_reserve_policies", policy_id)

    def cash_position(self, context: AccessContext, currency: str) -> Dict[str, Any]:
        self.identities.authorize(context, "finance:read", context.organization_id)
        currency = currency.upper()
        events = self.store.list("p6_financial_events", "organization_id=? AND currency=?", (context.organization_id, currency))
        signed = lambda e: (1 if e["direction"] == "CREDIT" else -1) * float(e["amount"])
        # Reserve/provision rows classify portions of existing cash; they do
        # not manufacture cash. Only the CASH bucket changes cash balance.
        balance = sum(signed(e) for e in events if e["account_bucket"] == "CASH")
        components = {bucket: sum(signed(e) for e in events if e["account_bucket"] == bucket) for bucket in self.PROTECTED_BUCKETS}
        policies = self.store.list("p6_reserve_policies", "organization_id=?", (context.organization_id,))
        policy = policies[-1] if policies else {"essential_monthly_burn": 0, "minimum_runway_months": 3, "target_runway_months": 6, "operating_reserve_override": None}
        operating = policy.get("operating_reserve_override")
        if operating is None:
            operating = float(policy["essential_monthly_burn"]) * float(policy["minimum_runway_months"])
        components["OPERATING_RESERVE"] = max(components["OPERATING_RESERVE"], float(operating))
        protected = sum(max(0.0, value) for value in components.values())
        return {"organization_id": context.organization_id, "currency": currency,
                "cash_balance": round(balance, 2), "protected_components": {k: round(v, 2) for k, v in components.items()},
                "protected_cash": round(protected, 2), "free_cash": round(balance - protected, 2),
                "minimum_runway_months": float(policy["minimum_runway_months"]),
                "target_runway_months": float(policy["target_runway_months"])}


class CommercialOperations:
    """Invoice -> sandbox payment -> settlement -> reconciliation -> receipt.

    The invoice remains authoritative for receivables, the payment intent for
    provider state, and immutable finance events for cash. Reconciliation is
    the only bridge between those ledgers.
    """

    RECONCILIATION_STATUSES = {"MATCHED", "PARTIAL", "MISMATCH", "UNMATCHED", "REVIEW_REQUIRED"}

    def __init__(self, store: StateStore, audit: AuditLog, identities: CommercialIdentityStore):
        self.store, self.audit, self.identities = store, audit, identities
        self.payments = PaymentOrchestrator(store, audit, identities)
        self.accounts = FinanceAccounts(store, audit, identities)
        self.billing = BillingStore(store, audit)

    def _invoice_organization(self, invoice: Dict[str, Any]) -> Optional[str]:
        rows = self.store.list("comm_organizations", "linked_client_id=?", (invoice["client_id"],))
        return rows[-1]["id"] if rows else None

    def create_checkout_for_invoice(self, context: AccessContext, invoice_id: str, amount: float,
                                    idempotency_key: str, legal_owner_name: str,
                                    beneficiary_ref: str, allowed_methods: Iterable[str]) -> Dict[str, Any]:
        invoice = self.billing.get(invoice_id)
        if not invoice:
            raise CommercialSecurityError("invoice not found")
        organization_id = self._invoice_organization(invoice)
        if not organization_id:
            raise CommercialSecurityError("invoice is not linked to a commercial organization")
        self.identities.authorize(context, "payments:create", organization_id)
        if invoice["status"] in {"CANCELLED", "PAID"}:
            raise CommercialSecurityError(f"invoice is already {invoice['status']}")
        remaining = round(float(invoice["amount"]) - float(invoice.get("amount_received") or 0), 2)
        if amount <= 0 or amount > remaining:
            raise CommercialSecurityError("checkout amount must be positive and cannot exceed invoice amount due")
        kind = "MILESTONE" if amount < remaining or invoice.get("milestone") else "ONE_TIME"
        return self.payments.create_intent(
            context, organization_id, amount, invoice["currency"], kind, idempotency_key,
            legal_owner_name, beneficiary_ref, allowed_methods, invoice_id=invoice_id,
            metadata={"source": "INVOICE", "invoice_id": invoice_id, "sandbox": True},
        )

    def reconcile_settlement(self, context: AccessContext, payment_id: str, settlement_ref: str,
                             settled_amount: float, settled_currency: str, evidence: Any,
                             idempotency_key: str) -> Dict[str, Any]:
        if not evidence:
            raise CommercialSecurityError("settlement reconciliation requires verified provider evidence")
        payment = self.store.get("p6_payment_intents", payment_id)
        if not payment:
            raise CommercialSecurityError("payment intent not found")
        self.identities.authorize(context, "finance:read", payment["organization_id"])
        invoice = self.billing.get(payment.get("invoice_id")) if payment.get("invoice_id") else None
        # Race-matrix fix (settlement vs settlement): the idempotency-key
        # replay check and the settlement_ref duplicate-detection check
        # below were both plain check-then-act reads followed by a separate
        # create() call, with no lock held across the gap. Two genuinely
        # concurrent reconcile_settlement() calls for the very same
        # settlement could race this two different ways: (a) an identical
        # idempotency_key replay -- both see "no existing row" and both
        # reach create(), and the loser would previously crash on the
        # UNIQUE(organization_id, idempotency_key) constraint with a raw,
        # uncaught sqlite3.IntegrityError instead of getting back the
        # winner's row; (b) a genuine double-submission with *different*
        # idempotency_keys for the same settlement_ref -- both could see
        # "no duplicate settlement_ref" and both commit a MATCHED/PARTIAL
        # reconciliation (and, further down, both create a receipt), which
        # is exactly the duplicate-settlement finding this code is supposed
        # to catch. BEGIN IMMEDIATE forces this whole
        # read-both-checks -> decide -> insert sequence to serialize
        # against every other reconcile_settlement() call on this
        # organization, so no concurrent caller can ever act on a stale
        # "nothing exists yet" snapshot.
        with self.store.transaction_immediate() as db:
            existing_rows = db.execute(
                "SELECT id FROM p6_reconciliations WHERE organization_id=? AND idempotency_key=?",
                (payment["organization_id"], idempotency_key),
            ).fetchall()
            if existing_rows:
                return self.store.get("p6_reconciliations", existing_rows[-1]["id"])
            findings = []
            status = "MATCHED"
            if payment["status"] != "SETTLED":
                findings.append("PAYMENT_NOT_SETTLED")
                status = "REVIEW_REQUIRED"
            if not invoice:
                findings.append("PAYMENT_WITHOUT_VALID_INVOICE")
                status = "UNMATCHED"
            elif self._invoice_organization(invoice) != payment["organization_id"]:
                findings.append("INVOICE_ORGANIZATION_MISMATCH")
                status = "MISMATCH"
            if settled_currency.upper() != payment["currency"]:
                findings.append("CURRENCY_MISMATCH")
                status = "MISMATCH"
            if round(float(settled_amount), 2) != round(float(payment["amount"]), 2):
                findings.append("AMOUNT_MISMATCH")
                status = "MISMATCH"
            if not settlement_ref:
                findings.append("MISSING_SETTLEMENT_REFERENCE")
                status = "REVIEW_REQUIRED"
            duplicate_rows = db.execute(
                "SELECT id FROM p6_reconciliations WHERE organization_id=? AND settlement_ref=?",
                (payment["organization_id"], settlement_ref),
            ).fetchall() if settlement_ref else []
            if duplicate_rows:
                findings.append("DUPLICATE_SETTLEMENT")
                status = "REVIEW_REQUIRED"
            if invoice and status == "MATCHED":
                remaining = round(float(invoice["amount"]) - float(invoice.get("amount_received") or 0), 2)
                if settled_amount < remaining:
                    status = "PARTIAL"
                    findings.append("PARTIAL_COLLECTION")
            now = utcnow()
            reconciliation_id = str(uuid.uuid4())
            db.execute(
                "INSERT INTO p6_reconciliations (id,organization_id,invoice_id,payment_intent_id,"
                "provider_transaction_ref,settlement_ref,expected_amount,settled_amount,expected_currency,"
                "settled_currency,status,findings_json,evidence_json,reviewed_by_identity_id,idempotency_key,"
                "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (reconciliation_id, payment["organization_id"], payment.get("invoice_id"), payment_id,
                 payment.get("provider_transaction_ref"), settlement_ref, payment["amount"], settled_amount,
                 payment["currency"], settled_currency.upper(), status, _json(findings), _json(evidence),
                 context.identity_id, idempotency_key, now, now),
            )
        if status in {"MATCHED", "PARTIAL"}:
            payment_evidence = {"type": "VERIFIED_PROVIDER_SETTLEMENT", "settlement_ref": settlement_ref,
                                "payment_intent_id": payment_id, "reconciliation_id": reconciliation_id}
            self.billing.record_payment(invoice["id"], settled_amount, context.identity_id, payment_evidence)
            self.accounts.record_event(context, "SETTLED_COLLECTION", "CASH", "CREDIT", settled_amount,
                                       settled_currency, "RECONCILIATION", reconciliation_id,
                                       evidence, f"settlement:{settlement_ref}")
            receipt_id = self.store.create("p6_receipts", {
                "organization_id": payment["organization_id"], "invoice_id": invoice["id"],
                "payment_intent_id": payment_id, "reconciliation_id": reconciliation_id,
                "amount": settled_amount, "currency": settled_currency.upper(),
                "provider_transaction_ref": payment.get("provider_transaction_ref") or settlement_ref,
                "status": "ISSUED", "issued_at": now, "created_at": now,
            })
            self.audit.append("P6_SETTLEMENT_RECONCILED", {"reconciliation_id": reconciliation_id,
                              "receipt_id": receipt_id, "status": status, "actor": context.identity_id})
        else:
            self.audit.append("P6_SETTLEMENT_REVIEW_REQUIRED", {"reconciliation_id": reconciliation_id,
                              "status": status, "findings": findings, "actor": context.identity_id})
        return self.store.get("p6_reconciliations", reconciliation_id)

    def reconciliation_snapshot(self, context: AccessContext) -> Dict[str, Any]:
        self.identities.authorize(context, "finance:read", context.organization_id)
        rows = list(reversed(self.store.list("p6_reconciliations", "organization_id=?", (context.organization_id,))))
        receipts = list(reversed(self.store.list("p6_receipts", "organization_id=?", (context.organization_id,))))
        return {"organization_id": context.organization_id, "items": rows, "receipts": receipts,
                "counts": {status: sum(1 for row in rows if row["status"] == status)
                           for status in sorted(self.RECONCILIATION_STATUSES)}}
