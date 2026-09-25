"""Real, server-enforced staff authentication for the public website's
gated staff area (falguna/site_web.py).

Not cosmetic: passwords are PBKDF2-HMAC-SHA256 salted+hashed (never stored
or logged in plaintext), sessions are opaque random tokens stored server
side with an expiry, every session carries its own CSRF token that state-
changing requests must echo back, and repeated failed logins lock the
account for a cooldown window. MFA (TOTP/WebAuthn) is intentionally not
implemented here -- see docs/website/WEBSITE_V1_SPEC.md for why -- so this
must not be treated as sufficient for a highly privileged production
identity without that follow-up.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from .store import StateStore

PBKDF2_ITERATIONS = 390_000
SESSION_TTL_HOURS = 12
MAX_FAILED_LOGINS = 5
LOCKOUT_MINUTES = 15


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


class AuthError(Exception):
    pass


def hash_password(password: str, salt: Optional[bytes] = None) -> str:
    if salt is None:
        salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, iterations, salt_hex, digest_hex = encoded.split("$")
        if algo != "pbkdf2_sha256":
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


class StaffAuthService:
    """CRUD + login/session lifecycle for the site_staff_users /
    site_staff_sessions tables. Deliberately independent of any other
    auth in the codebase (Falguna's browser-session store is unrelated)."""

    def __init__(self, store: StateStore):
        self.store = store

    # -- user management (admin-only in practice; no public signup route) --

    def create_user(self, email: str, display_name: str, password: str, role: str = "staff") -> str:
        email = email.strip().lower()
        if not email or "@" not in email:
            raise AuthError("a valid email is required")
        if len(password) < 12:
            raise AuthError("password must be at least 12 characters")
        existing = self.store.list("site_staff_users", "email = ?", (email,))
        if existing:
            raise AuthError("a user with this email already exists")
        now = utcnow()
        return self.store.create("site_staff_users", {
            "email": email,
            "display_name": display_name.strip() or email,
            "password_hash": hash_password(password),
            "role": role,
            "is_active": 1,
            "mfa_enabled": 0,
            "failed_login_count": 0,
            "locked_until": None,
            "last_login_at": None,
            "created_at": now,
            "updated_at": now,
        })

    def _get_user_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        rows = self.store.list("site_staff_users", "email = ?", (email.strip().lower(),))
        return rows[0] if rows else None

    # -- login / session --

    def login(self, email: str, password: str, ip_hash: Optional[str] = None,
               user_agent: Optional[str] = None) -> Dict[str, Any]:
        user = self._get_user_by_email(email)
        # Constant-shape failure: don't reveal whether the email exists.
        if user is None:
            hash_password(password)  # burn comparable time
            raise AuthError("invalid email or password")

        if user.get("locked_until"):
            locked_until = datetime.fromisoformat(user["locked_until"])
            if locked_until > _now_dt():
                raise AuthError("account temporarily locked after repeated failed logins")

        if not user.get("is_active"):
            raise AuthError("account is disabled")

        if not verify_password(password, user["password_hash"]):
            failed = int(user.get("failed_login_count") or 0) + 1
            updates: Dict[str, Any] = {"failed_login_count": failed, "updated_at": utcnow()}
            if failed >= MAX_FAILED_LOGINS:
                updates["locked_until"] = (_now_dt() + timedelta(minutes=LOCKOUT_MINUTES)).isoformat()
                updates["failed_login_count"] = 0
            self.store.update("site_staff_users", user["id"], **updates)
            raise AuthError("invalid email or password")

        self.store.update("site_staff_users", user["id"], failed_login_count=0,
                            locked_until=None, last_login_at=utcnow(), updated_at=utcnow())

        session_id = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(24)
        expires_at = (_now_dt() + timedelta(hours=SESSION_TTL_HOURS)).isoformat()
        self.store.create("site_staff_sessions", {
            "user_id": user["id"],
            "csrf_token": csrf_token,
            "ip_hash": ip_hash,
            "user_agent": (user_agent or "")[:300],
            "expires_at": expires_at,
            "revoked_at": None,
            "created_at": utcnow(),
        }, record_id=session_id)
        return {"session_id": session_id, "csrf_token": csrf_token, "user": user}

    def session(self, session_id: str) -> Optional[Dict[str, Any]]:
        if not session_id:
            return None
        row = self.store.get("site_staff_sessions", session_id)
        if not row or row.get("revoked_at"):
            return None
        if datetime.fromisoformat(row["expires_at"]) <= _now_dt():
            return None
        return row

    def current_user(self, session_id: str) -> Optional[Dict[str, Any]]:
        session = self.session(session_id)
        if not session:
            return None
        return self.store.get("site_staff_users", session["user_id"])

    def check_csrf(self, session_id: str, csrf_token: str) -> bool:
        session = self.session(session_id)
        if not session:
            return False
        return hmac.compare_digest(session.get("csrf_token", ""), csrf_token or "")

    def logout(self, session_id: str) -> None:
        session = self.store.get("site_staff_sessions", session_id)
        if session and not session.get("revoked_at"):
            self.store.update("site_staff_sessions", session_id, revoked_at=utcnow())
