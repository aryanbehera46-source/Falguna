"""Phase 7 external account boundary and customer/partner read models.

No public sign-up exists. A trusted operator provisions an account against an
already-active Phase 6 identity. Every read resolves role, organization and
subject from that immutable server-side identity; request parameters never
select a tenant.
"""
from __future__ import annotations

import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from .audit import AuditLog
from .customer_portal import CustomerPortalService
from .partner_management import PartnerError, PartnerStore, ReferralError, ReferralStore
from .site_auth import AuthError, hash_password, verify_password
from .store import StateStore, utcnow

SESSION_TTL_HOURS = 12
MAX_FAILED_LOGINS = 5
LOCKOUT_MINUTES = 15


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ExternalPortalAuth:
    def __init__(self, store: StateStore):
        self.store = store

    def provision(self, identity_id: str, email: str, password: str) -> str:
        identity = self.store.get("p6_commercial_identities", identity_id)
        if not identity or identity.get("status") != "ACTIVE" or identity.get("role") not in {"CUSTOMER", "PARTNER"}:
            raise AuthError("an active customer or partner identity is required")
        email = email.strip().lower()
        if "@" not in email or len(password) < 12:
            raise AuthError("valid email and a password of at least 12 characters are required")
        if self.store.list("p7_external_accounts", "email=?", (email,)):
            raise AuthError("an account with this email already exists")
        now = utcnow()
        return self.store.create("p7_external_accounts", {
            "identity_id": identity_id, "email": email, "password_hash": hash_password(password),
            "status": "ACTIVE", "failed_login_count": 0, "locked_until": None,
            "last_login_at": None, "created_at": now, "updated_at": now,
        })

    def login(self, email: str, password: str, ip_hash: Optional[str] = None,
              user_agent: Optional[str] = None) -> Dict[str, Any]:
        rows = self.store.list("p7_external_accounts", "email=?", (email.strip().lower(),))
        account = rows[0] if rows else None
        if not account:
            hash_password(password)
            raise AuthError("invalid email or password")
        if account.get("locked_until") and datetime.fromisoformat(account["locked_until"]) > _now():
            raise AuthError("account temporarily locked after repeated failed logins")
        identity = self.store.get("p6_commercial_identities", account["identity_id"])
        if account.get("status") != "ACTIVE" or not identity or identity.get("status") != "ACTIVE":
            raise AuthError("account is disabled")
        if not verify_password(password, account["password_hash"]):
            failed = int(account.get("failed_login_count") or 0) + 1
            updates: Dict[str, Any] = {"failed_login_count": failed, "updated_at": utcnow()}
            if failed >= MAX_FAILED_LOGINS:
                updates.update(failed_login_count=0, locked_until=(_now() + timedelta(minutes=LOCKOUT_MINUTES)).isoformat())
            self.store.update("p7_external_accounts", account["id"], **updates)
            raise AuthError("invalid email or password")
        self.store.update("p7_external_accounts", account["id"], failed_login_count=0,
                          locked_until=None, last_login_at=utcnow(), updated_at=utcnow())
        session_id, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        self.store.create("p7_external_sessions", {
            "account_id": account["id"], "csrf_token": csrf, "ip_hash": ip_hash,
            "user_agent": (user_agent or "")[:300],
            "expires_at": (_now() + timedelta(hours=SESSION_TTL_HOURS)).isoformat(),
            "revoked_at": None, "created_at": utcnow(), "updated_at": utcnow(),
        }, record_id=session_id)
        return {"session_id": session_id, "csrf_token": csrf, "identity": identity}

    def identity(self, session_id: str) -> Optional[Dict[str, Any]]:
        session = self.store.get("p7_external_sessions", session_id) if session_id else None
        if not session or session.get("revoked_at") or datetime.fromisoformat(session["expires_at"]) <= _now():
            return None
        account = self.store.get("p7_external_accounts", session["account_id"])
        if not account or account.get("status") != "ACTIVE":
            return None
        identity = self.store.get("p6_commercial_identities", account["identity_id"])
        return identity if identity and identity.get("status") == "ACTIVE" else None

    def check_csrf(self, session_id: str, csrf: str) -> bool:
        session = self.store.get("p7_external_sessions", session_id) if session_id else None
        if not session or session.get("revoked_at") or datetime.fromisoformat(session["expires_at"]) <= _now():
            return False
        account = self.store.get("p7_external_accounts", session["account_id"])
        return bool(account and account.get("status") == "ACTIVE" and
                    hmac.compare_digest(session.get("csrf_token", ""), csrf or ""))

    def logout(self, session_id: str) -> None:
        session = self.store.get("p7_external_sessions", session_id) if session_id else None
        if session and not session.get("revoked_at"):
            self.store.update("p7_external_sessions", session_id, revoked_at=utcnow())

    def account_security(self, session_id: str) -> Dict[str, Any]:
        session = self.store.get("p7_external_sessions", session_id) if session_id else None
        identity = self.identity(session_id)
        if not session or not identity:
            raise AuthError("active session required")
        account = self.store.get("p7_external_accounts", session["account_id"])
        sessions = self.store.list("p7_external_sessions", "account_id=?", (account["id"],))
        return {
            "email": account["email"], "display_name": identity["display_name"],
            "role": identity["role"], "last_login_at": account.get("last_login_at"),
            "sessions": [{
                "id": row["id"], "created_at": row["created_at"], "expires_at": row["expires_at"],
                "user_agent": row.get("user_agent") or "Unknown browser", "current": row["id"] == session_id,
                "active": not bool(row.get("revoked_at")) and datetime.fromisoformat(row["expires_at"]) > _now(),
            } for row in sessions],
            "mfa_status": "REQUIRED_FOR_PRODUCTION_NOT_CONFIGURED",
            "recovery_status": "OPERATOR_ASSISTED_NO_EMAIL_CHANNEL",
        }

    def change_password(self, session_id: str, csrf: str, current_password: str, new_password: str) -> None:
        if not self.check_csrf(session_id, csrf):
            raise AuthError("security check failed")
        session = self.store.get("p7_external_sessions", session_id)
        account = self.store.get("p7_external_accounts", session["account_id"]) if session else None
        if not account or not verify_password(current_password, account["password_hash"]):
            raise AuthError("current password is incorrect")
        if len(new_password) < 12:
            raise AuthError("new password must be at least 12 characters")
        self.store.update("p7_external_accounts", account["id"], password_hash=hash_password(new_password))
        for row in self.store.list("p7_external_sessions", "account_id=?", (account["id"],)):
            if row["id"] != session_id and not row.get("revoked_at"):
                self.store.update("p7_external_sessions", row["id"], revoked_at=utcnow())


class ExternalPortalService:
    def __init__(self, store: StateStore, audit: AuditLog):
        self.store, self.audit = store, audit

    def customer_bundle(self, identity: Dict[str, Any]) -> Dict[str, Any]:
        if identity.get("role") != "CUSTOMER":
            raise PermissionError("customer identity required")
        customer_org_id = identity["subject_ref"]
        return CustomerPortalService(self.store, self.audit).get_portal_bundle(
            customer_org_id, actor=identity["id"], requested_by_organization_id=customer_org_id,
        )

    def partner_bundle(self, identity: Dict[str, Any]) -> Dict[str, Any]:
        if identity.get("role") != "PARTNER":
            raise PermissionError("partner identity required")
        partner_id = identity["subject_ref"]
        partner = self.store.get("pm_partners", partner_id)
        if not partner:
            raise PermissionError("linked partner not found")
        safe_partner = {k: partner.get(k) for k in (
            "id", "full_name", "organization_name", "status", "verification_status", "role_type",
            "maturity_tier", "kyc_status", "agreement_accepted", "agreement_reference",
            "no_side_deal_accepted", "policy_acknowledged_at",
        )}
        referrals = self.store.list("pm_referrals", "partner_id=?", (partner_id,))
        referral_ids = {r["id"] for r in referrals}
        commissions = [c for c in self.store.list("pm_commissions") if c.get("referral_id") in referral_ids]
        contributions = self.store.list("p6_partner_contributions", "partner_id=?", (partner_id,))
        return {"partner": safe_partner, "referrals": referrals, "commissions": commissions,
                "contributions": contributions}

    def customer_payments(self, identity: Dict[str, Any]) -> Dict[str, Any]:
        if identity.get("role") != "CUSTOMER":
            raise PermissionError("customer identity required")
        customer_ref = identity["subject_ref"]
        rows = self.store.list("p6_payment_intents", "organization_id=? AND customer_ref=?",
                               (identity["organization_id"], customer_ref))
        safe = []
        for row in rows:
            safe.append({k: row.get(k) for k in (
                "id", "invoice_id", "kind", "amount", "currency", "status", "capture_mode",
                "provider", "provider_session_ref", "payment_method_token_ref", "mandate_token_ref",
                "legal_owner_name", "beneficiary_ref", "created_at", "updated_at",
            )} | {"allowed_methods": json.loads(row.get("allowed_methods_json") or "[]")})
        invoice_ids = {i["id"] for i in self.customer_bundle(identity)["invoices"]}
        receipts = [{k: r.get(k) for k in ("id", "invoice_id", "payment_intent_id", "amount", "currency", "provider_transaction_ref", "status", "issued_at")}
                    for r in self.store.list("p6_receipts") if r.get("invoice_id") in invoice_ids]
        client_ids = [c["id"] for c in self.customer_bundle(identity)["clients"]]
        subscriptions = [
            {k: s.get(k) for k in ("id", "plan_name", "amount", "currency", "cadence", "next_billing_date", "provider_token_ref", "mandate_ref", "autopay_status", "status", "last_invoice_id")}
            for client_id in client_ids for s in self.store.list("p6_subscriptions", "organization_id=? AND client_id=?",
                                     (identity["organization_id"], client_id))
        ]
        return {"payments": safe, "receipts": receipts, "subscriptions": subscriptions}

    def customer_invoice_detail(self, identity: Dict[str, Any], invoice_id: str) -> Optional[Dict[str, Any]]:
        bundle = self.customer_bundle(identity)
        invoice = next((i for i in bundle["invoices"] if i["id"] == invoice_id), None)
        if not invoice:
            return None
        payments = self.customer_payments(identity)
        return {"invoice": invoice,
                "receipts": [r for r in payments["receipts"] if r["invoice_id"] == invoice_id],
                "refunds": [r for r in bundle["refunds"] if r["invoice_id"] == invoice_id],
                "disputes": [d for d in bundle["disputes"] if d["invoice_id"] == invoice_id],
                "subscriptions": [s for s in payments["subscriptions"] if s.get("last_invoice_id") == invoice_id]}

    def verify_partner(self, partner_id: str) -> Dict[str, Any]:
        partner = self.store.get("pm_partners", (partner_id or "").strip())
        if not partner:
            return {"state": "UNKNOWN", "valid": False}
        active = partner.get("status") == "APPROVED" and partner.get("verification_status") == "VERIFIED"
        return {"state": "ACTIVE_VALID" if active else "SUSPENDED_INVALID", "valid": active,
                "partner_id": partner["id"], "name": partner.get("full_name") if active else None}

    def customer_project_detail(self, identity: Dict[str, Any], project_id: str) -> Optional[Dict[str, Any]]:
        """Phase 7, Sections 1-2: the authenticated customer project-detail
        read. Delegates entirely to CustomerPortalService.get_project_detail,
        which scopes the lookup through the same customer-owned bundle
        get_portal_bundle() already proves isolated -- a project id outside
        this customer's own organization returns None exactly like an
        unknown id, never a different error that would confirm the id
        belongs to someone else."""
        if identity.get("role") != "CUSTOMER":
            raise PermissionError("customer identity required")
        customer_org_id = identity["subject_ref"]
        return CustomerPortalService(self.store, self.audit).get_project_detail(
            customer_org_id, project_id, actor=identity["id"], requested_by_organization_id=customer_org_id,
        )

    def partner_acknowledge_policy(self, identity: Dict[str, Any]) -> Dict[str, Any]:
        """Phase 7, Section 8: the partner's own, dated acknowledgement of
        the no-money-collection/anti-diversion policy -- required before
        partner_register_lead() below will accept a new lead."""
        if identity.get("role") != "PARTNER":
            raise PermissionError("partner identity required")
        return PartnerStore(self.store, self.audit).acknowledge_policy(identity["subject_ref"], actor=identity["id"])

    def partner_register_lead(self, identity: Dict[str, Any], fields: Dict[str, Any]) -> str:
        """Phase 7, Section 5: authenticated, policy-gated lead registration.
        Delegates to the already-proven ReferralStore.register() (duplicate
        detection, normalization, PENDING_REVIEW attribution state) rather
        than building a parallel intake path -- this method's only added
        value is resolving partner_id from the immutable authenticated
        identity (never a request field) and enforcing the Section 8 policy
        acknowledgement gate before a lead can be registered at all."""
        if identity.get("role") != "PARTNER":
            raise PermissionError("partner identity required")
        partner_id = identity["subject_ref"]
        partner = self.store.get("pm_partners", partner_id)
        if not partner:
            raise PermissionError("linked partner not found")
        if not partner.get("policy_acknowledged_at"):
            raise ReferralError(
                "you must acknowledge the TTT partner payment policy before registering a lead"
            )
        return ReferralStore(self.store, self.audit).register(partner_id, dict(fields), actor=identity["id"])
