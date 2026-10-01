"""Phase 6 Partner Network, anti-diversion risk, and verification services."""

import json
from typing import Any, Dict, Iterable, Optional

from .audit import AuditLog
from .partner_management import CommissionStore, PartnerStore
from .phase6_commercial import AccessContext, CommercialIdentityStore, CommercialSecurityError
from .store import StateStore, utcnow

PARTNER_ROLE_TYPES = {
    "REFERRAL_PARTNER", "LOCAL_BUSINESS_SCOUT", "LEAD_RESEARCHER", "APPOINTMENT_SETTER",
    "INDUSTRY_SPECIALIST", "LANGUAGE_PARTNER", "QA_CONTRIBUTOR", "DELIVERY_SPECIALIST",
    "REGIONAL_PARTNER", "ACCOUNT_PARTNER",
}
MATURITY_TIERS = {"APPLICANT", "VERIFIED", "CERTIFIED", "PROVEN", "SENIOR"}
KYC_STATUSES = {"NOT_STARTED", "PENDING", "VERIFIED", "FAILED", "EXPIRED"}
RISK_EVENT_TYPES = {
    "ALTERNATE_BENEFICIARY_REQUEST", "OFF_PLATFORM_PAYMENT_REQUEST", "CUSTOMER_PAID_REPRESENTATIVE",
    "REPEATED_LEAD_CANCELLATION", "DUPLICATE_CUSTOMER_ACROSS_PARTNERS", "ABNORMAL_REFUND_RATE",
    "UNUSUAL_COMMISSION_SPIKE", "OFFICIAL_FLOW_REFUSAL", "UNAUTHORIZED_PRICE_SCOPE_CHANGE",
    "BRAND_MISUSE", "UNDISCLOSED_RELATED_PARTY", "OFF_PLATFORM_DIVERSION", "UNAUTHORIZED_SUBCONTRACTING",
    "SETTLEMENT_MISMATCH",
}
RISK_SEVERITIES = {"LOW", "MEDIUM", "HIGH", "CRITICAL"}
RISK_ACTIONS = {"HOLD", "REVIEW", "SUSPEND_ACCESS", "CLEAR", "ESCALATE"}
PUBLIC_PAYMENT_WARNING = (
    "Partners/representatives are not authorized to collect customer money unless the official TTT "
    "invoice/payment page explicitly confirms the beneficiary and payment method."
)


class PartnerNetworkService:
    def __init__(self, store: StateStore, audit: AuditLog, identities: CommercialIdentityStore):
        self.store, self.audit, self.identities = store, audit, identities

    def configure_partner(self, context: AccessContext, partner_id: str, role_type: str,
                          maturity_tier: str = "APPLICANT", kyc_status: str = "NOT_STARTED",
                          public_verification_enabled: bool = False,
                          related_party_disclosed: bool = False) -> Dict[str, Any]:
        self.identities.authorize(context, "identities:manage", context.organization_id)
        partner = self.store.get("pm_partners", partner_id)
        if not partner:
            raise CommercialSecurityError("partner not found")
        if role_type not in PARTNER_ROLE_TYPES or maturity_tier not in MATURITY_TIERS or kyc_status not in KYC_STATUSES:
            raise CommercialSecurityError("invalid partner role, maturity tier, or KYC status")
        self.store.update("pm_partners", partner_id, role_type=role_type, maturity_tier=maturity_tier,
                          kyc_status=kyc_status, public_verification_enabled=int(public_verification_enabled),
                          related_party_disclosed=int(related_party_disclosed), no_side_deal_accepted=1,
                          no_unauthorized_subcontracting_accepted=1)
        self.audit.append("P6_PARTNER_PROFILE_CONFIGURED", {"partner_id": partner_id, "role_type": role_type,
                          "maturity_tier": maturity_tier, "actor": context.identity_id})
        return self.store.get("pm_partners", partner_id)

    def create_commission_plan(self, context: AccessContext, name: str, rate: float,
                               recurring_enabled: bool = False, lifetime_originator_enabled: bool = False,
                               excluded_pass_through_types: Optional[Iterable[str]] = None) -> Dict[str, Any]:
        self.identities.authorize(context, "finance:read", context.organization_id)
        if not name.strip() or rate < 0 or rate > 1:
            raise CommercialSecurityError("commission plan name and rate between 0 and 1 are required")
        now = utcnow()
        plan_id = self.store.create("p6_commission_plans", {
            "organization_id": context.organization_id, "name": name.strip(), "rate": rate,
            "recurring_enabled": int(recurring_enabled), "lifetime_originator_enabled": int(lifetime_originator_enabled),
            "excluded_pass_through_types_json": json.dumps(sorted(set(excluded_pass_through_types or []))),
            "status": "ACTIVE", "actor": context.identity_id, "created_at": now, "updated_at": now,
        })
        return self.store.get("p6_commission_plans", plan_id)

    def assign_plan(self, context: AccessContext, referral_id: str, plan_id: str) -> Dict[str, Any]:
        self.identities.authorize(context, "finance:read", context.organization_id)
        plan = self.store.get("p6_commission_plans", plan_id)
        referral = self.store.get("pm_referrals", referral_id)
        if not plan or plan["organization_id"] != context.organization_id or not referral:
            raise CommercialSecurityError("valid organization-scoped plan and referral required")
        commission = CommissionStore(self.store, self.audit).ensure_for_referral(referral_id, context.identity_id, float(plan["rate"]))
        self.store.update("pm_commissions", commission["id"], commission_plan_id=plan_id, rate=plan["rate"], excluded_amount=0.0)
        return self.store.get("pm_commissions", commission["id"])

    def record_contribution(self, context: AccessContext, partner_id: str, contribution_type: str,
                            evidence: Any, referral_id: Optional[str] = None,
                            opportunity_id: Optional[str] = None) -> Dict[str, Any]:
        self.identities.authorize(context, "opportunities:manage", context.organization_id)
        if not self.store.get("pm_partners", partner_id) or not evidence:
            raise CommercialSecurityError("valid partner and contribution evidence are required")
        now = utcnow()
        record_id = self.store.create("p6_partner_contributions", {
            "organization_id": context.organization_id, "partner_id": partner_id, "referral_id": referral_id,
            "opportunity_id": opportunity_id, "contribution_type": contribution_type,
            "evidence_json": json.dumps(evidence, sort_keys=True), "status": "RECORDED",
            "actor": context.identity_id, "created_at": now, "updated_at": now,
        })
        return self.store.get("p6_partner_contributions", record_id)


class CommercialRiskService:
    def __init__(self, store: StateStore, audit: AuditLog, identities: CommercialIdentityStore):
        self.store, self.audit, self.identities = store, audit, identities

    def flag(self, organization_id: str, event_type: str, severity: str, evidence: Any, source: str,
             partner_id: Optional[str] = None, customer_ref: Optional[str] = None,
             project_id: Optional[str] = None, payment_intent_id: Optional[str] = None) -> Dict[str, Any]:
        if event_type not in RISK_EVENT_TYPES or severity not in RISK_SEVERITIES or not evidence:
            raise CommercialSecurityError("valid risk type, severity, and evidence are required")
        now = utcnow()
        event_id = self.store.create("p6_risk_events", {
            "organization_id": organization_id, "event_type": event_type, "severity": severity,
            "partner_id": partner_id, "customer_ref": customer_ref, "project_id": project_id,
            "payment_intent_id": payment_intent_id, "evidence_json": json.dumps(evidence, sort_keys=True),
            "status": "OPEN", "source": source, "reviewer_identity_id": None, "current_action": None,
            "created_at": now, "updated_at": now,
        })
        self.audit.append("P6_COMMERCIAL_RISK_FLAGGED", {"risk_event_id": event_id, "event_type": event_type,
                          "severity": severity, "source": source})
        return self.store.get("p6_risk_events", event_id)

    def act(self, context: AccessContext, risk_event_id: str, action: str, reason: str) -> Dict[str, Any]:
        self.identities.authorize(context, "approvals:verify", context.organization_id)
        event = self.store.get("p6_risk_events", risk_event_id)
        if not event or event["organization_id"] != context.organization_id:
            raise CommercialSecurityError("risk event not found in organization")
        if action not in RISK_ACTIONS or not reason.strip():
            raise CommercialSecurityError("valid risk action and reason are required")
        if event["status"] in {"CLEARED", "CLOSED"}:
            raise CommercialSecurityError("risk event is already terminal")
        status_after = {"HOLD": "HELD", "REVIEW": "UNDER_REVIEW", "SUSPEND_ACCESS": "ESCALATED",
                        "CLEAR": "CLEARED", "ESCALATE": "ESCALATED"}[action]
        if action == "SUSPEND_ACCESS":
            if not event.get("partner_id"):
                raise CommercialSecurityError("partner-linked event required to suspend access")
            partner = self.store.get("pm_partners", event["partner_id"])
            if partner and partner["status"] == "APPROVED":
                PartnerStore(self.store, self.audit).suspend(partner["id"], context.identity_id, reason)
        self.store.update("p6_risk_events", risk_event_id, status=status_after,
                          reviewer_identity_id=context.identity_id, current_action=action)
        self.store.create("p6_risk_event_actions", {
            "risk_event_id": risk_event_id, "organization_id": context.organization_id, "action": action,
            "status_before": event["status"], "status_after": status_after,
            "actor_identity_id": context.identity_id, "reason": reason, "created_at": utcnow(),
        })
        return self.store.get("p6_risk_events", risk_event_id)

    def queue(self, context: AccessContext) -> Dict[str, Any]:
        self.identities.authorize(context, "finance:read", context.organization_id)
        items = list(reversed(self.store.list("p6_risk_events", "organization_id=?", (context.organization_id,))))
        for item in items:
            item["actions"] = self.store.list("p6_risk_event_actions", "risk_event_id=?", (item["id"],))
        return {"organization_id": context.organization_id, "items": items}


class CustomerVerificationService:
    def __init__(self, store: StateStore):
        self.store = store

    def verify_partner(self, partner_id: str) -> Dict[str, Any]:
        partner = self.store.get("pm_partners", partner_id)
        if not partner or not partner.get("public_verification_enabled"):
            return {"valid": False, "partner_id": partner_id, "status": "INVALID_OR_NOT_PUBLIC"}
        authorized = partner["status"] == "APPROVED" and partner.get("verification_status") == "VERIFIED"
        return {"valid": authorized, "partner_id": partner_id, "name": partner["full_name"],
                "role_type": partner.get("role_type"), "maturity_tier": partner.get("maturity_tier"),
                "status": "ACTIVE" if authorized else partner["status"],
                "may_collect_customer_money": False, "warning": PUBLIC_PAYMENT_WARNING}

    def verify_payment_instruction(self, payment_id: str, customer_ref: str,
                                   beneficiary_ref: str) -> Dict[str, Any]:
        payment = self.store.get("p6_payment_intents", payment_id)
        if not payment or payment["customer_ref"] != customer_ref:
            return {"valid": False, "payment_id": payment_id, "status": "NOT_FOUND", "warning": PUBLIC_PAYMENT_WARNING}
        valid = payment["beneficiary_ref"] == beneficiary_ref
        return {"valid": valid, "payment_id": payment_id, "status": payment["status"],
                "amount": payment["amount"], "currency": payment["currency"],
                "legal_owner_name": payment["legal_owner_name"],
                "approved_beneficiary_ref": payment["beneficiary_ref"] if valid else None,
                "provider": payment["provider"], "warning": PUBLIC_PAYMENT_WARNING}
