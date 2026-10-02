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
        return bool(session and not session.get("revoked_at") and hmac.compare_digest(session.get("csrf_token", ""), csrf or ""))

    def logout(self, session_id: str) -> None:
        session = self.store.get("p7_external_sessions", session_id) if session_id else None
        if session and not session.get("revoked_at"):
            self.store.update("p7_external_sessions", session_id, revoked_at=utcnow())


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
            "no_side_deal_accepted",
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
        subscriptions = [
            {k: s.get(k) for k in ("id", "plan_name", "amount", "currency", "cadence", "next_billing_date", "autopay_status", "status")}
            for s in self.store.list("p6_subscriptions", "organization_id=? AND client_id=?",
                                     (identity["organization_id"], customer_ref))
        ]
        return {"payments": safe, "subscriptions": subscriptions}
