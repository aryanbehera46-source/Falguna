"""TTT-owned Phase 6 commercial security, sandbox payments, and finance.

FALGUNA may consume this state and prepare recommendations. It cannot approve
or execute money movement, beneficiary changes, refunds, or commissions.
"""

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional

from .audit import AuditLog
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
        event_id = self.store.create("p6_payment_events", {
            "payment_intent_id": payment_id, "organization_id": organization_id, "event_type": event_type,
            "status_before": before, "status_after": after, "amount": amount, "currency": currency,
            "evidence_json": _json(evidence), "provider_event_ref": provider_event_ref,
            "idempotency_key": key, "actor": actor, "created_at": utcnow(),
        })
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
        event_id = self.store.create("p6_webhook_events", {
            "provider": provider, "provider_event_ref": provider_event_ref, "payment_intent_id": payment_id,
            "organization_id": organization_id, "signature_verified": 1, "payload_hash": digest,
            "processing_status": "VERIFIED_PENDING_APPLICATION", "failure_reason": None,
            "received_at": now, "created_at": now,
        })
        return self.store.get("p6_webhook_events", event_id)


class ApprovalWorkflow:
    """Maker -> verifier -> Aryan approval. Deliberately has no execute method."""

    def __init__(self, store: StateStore, audit: AuditLog, identities: CommercialIdentityStore):
        self.store, self.audit, self.identities = store, audit, identities

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
        return self.store.get("p6_approval_requests", request_id)

    def verify(self, context: AccessContext, request_id: str) -> Dict[str, Any]:
        row = self._scoped(context, request_id, "approvals:verify")
        if row["status"] != "PENDING_VERIFICATION" or row["maker_identity_id"] == context.identity_id:
            raise CommercialSecurityError("independent verifier required")
        self.store.update("p6_approval_requests", request_id, status="PENDING_ARYAN_APPROVAL",
                          verifier_identity_id=context.identity_id, verified_at=utcnow())
        return self.store.get("p6_approval_requests", request_id)

    def approve_by_aryan(self, context: AccessContext, request_id: str) -> Dict[str, Any]:
        row = self._scoped(context, request_id, "finance:read")
        identity = self.store.get("p6_commercial_identities", context.identity_id)
        if identity["role"] != "OWNER" or identity["subject_ref"].strip().lower() != "aryan":
            raise CommercialSecurityError("Aryan owner identity is the required final approver")
        if row["status"] != "PENDING_ARYAN_APPROVAL" or row["verifier_identity_id"] == context.identity_id:
            raise CommercialSecurityError("verified request and independent final approver required")
        self.store.update("p6_approval_requests", request_id, status="APPROVED_PENDING_EXECUTION",
                          final_approver_identity_id=context.identity_id, approved_at=utcnow())
        return self.store.get("p6_approval_requests", request_id)

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
        event_id = self.store.create("p6_financial_events", {
            "organization_id": context.organization_id, "event_type": event_type, "account_bucket": bucket,
            "direction": direction, "amount": round(float(amount), 2), "currency": currency.upper(),
            "source_type": source_type, "source_id": source_id, "evidence_json": _json(evidence),
            "idempotency_key": idempotency_key, "actor": context.identity_id,
            "reverses_event_id": reverses_event_id, "created_at": utcnow(),
        })
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
