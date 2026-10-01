"""Real authenticated-session proof that final Aryan approval is secure.

These tests do not construct AccessContext by hand. Every identity acts
through a real ThreadingHTTPServer(TTTHQHandler), a real StaffAuthService
account/session, and the real _commercial_context() resolution path --
the exact path that exposed the original defect (subject_ref literal
"aryan" matching, which no real login could ever satisfy).
"""

import json
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

from falguna.hq_web import TTTHQHandler
from falguna.phase6_commercial import CommercialIdentityStore
from falguna.runtime import open_control_plane
from falguna.site_auth import StaffAuthService
from falguna.store import StateStore, utcnow


class RealSessionFinalApprovalTests(unittest.TestCase):
    """One real HTTP server; several real staff logins with distinct roles."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"; self.repo.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(self.repo)], check=True, capture_output=True)
        self.control, self.store = open_control_plane(self.repo)
        auth = StaffAuthService(self.store)
        identities = CommercialIdentityStore(self.store, self.control.audit)

        self.maker_session, self.maker_csrf = self._real_login(auth, identities, "maker@synthetic.invalid",
            "Maker", "ttt", "FINANCE_OPERATOR")
        self.verifier_session, self.verifier_csrf = self._real_login(auth, identities, "verifier@synthetic.invalid",
            "Verifier", "ttt", "FINANCE_OPERATOR")
        self.owner_session, self.owner_csrf = self._real_login(auth, identities, "aryan.real.login@synthetic.invalid",
            "Aryan Behera", "ttt", "OWNER")
        self.admin_session, self.admin_csrf = self._real_login(auth, identities, "admin@synthetic.invalid",
            "Admin User", "ttt", "ADMIN")
        self.finance_other_session, self.finance_other_csrf = self._real_login(auth, identities,
            "finance-other@synthetic.invalid", "Other Finance", "ttt", "FINANCE_OPERATOR")
        self.other_org_owner_session, self.other_org_owner_csrf = self._real_login(auth, identities,
            "acme.owner@synthetic.invalid", "Acme Owner", "acme", "OWNER")

        # A real staff login whose identity is provisioned OWNER, then
        # revoked (e.g. offboarded) -- must not be able to approve even
        # though the role on the row still reads OWNER.
        self.revoked_owner_session, self.revoked_owner_csrf, self.revoked_identity_id = self._real_login(
            auth, identities, "revoked.owner@synthetic.invalid", "Revoked Owner", "ttt", "OWNER", return_identity=True)
        self.store.update("p6_commercial_identities", self.revoked_identity_id, status="REVOKED")

        self.store.close()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), TTTHQHandler)
        self.server.app_root = self.repo; self.server.falguna_url = "http://127.0.0.1:1"
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2)
        self.tmp.cleanup()

    def _real_login(self, auth, identities, email, display_name, organization_id, role, return_identity=False):
        user_id = auth.create_user(email, display_name, "synthetic-password-123456", "staff")
        session_id = f"session-{email}"
        csrf = f"csrf-{email}"
        self.store.create("site_staff_sessions", {"user_id": user_id, "csrf_token": csrf, "ip_hash": None,
            "user_agent": "test", "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "revoked_at": None, "created_at": utcnow()}, record_id=session_id)
        identity_id = identities.create(organization_id, "STAFF_USER", user_id, display_name, role, "test")
        if return_identity:
            return session_id, csrf, identity_id
        return session_id, csrf

    def _request(self, session, csrf, path, body=None, organization="ttt"):
        headers = {"Content-Type": "application/json", "Cookie": f"ttt_staff_session={session}",
                  "X-TTT-Organization": organization}
        method = "GET"
        if body is not None:
            headers["X-CSRF-Token"] = csrf
            method = "POST"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=3) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def _maker_then_verifier(self, action_type, amount=500):
        status, body = self._request(self.maker_session, self.maker_csrf, "/api/p6/approvals", {
            "action_type": action_type, "target_type": "PAYABLE", "target_id": f"target-{action_type}",
            "payload": {}, "amount": amount, "currency": "INR", "beneficiary_ref": "vendor:synthetic"})
        self.assertEqual(status, 201, body)
        approval_id = body["id"]
        status, body = self._request(self.verifier_session, self.verifier_csrf,
            f"/api/p6/approvals/{approval_id}/verify", {})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["status"], "PENDING_ARYAN_APPROVAL")
        return approval_id

    def test_real_aryan_owner_can_final_approve_every_outgoing_action_type(self):
        for action_type in ("REFUND", "COMMISSION_RELEASE", "VENDOR_PAYMENT", "BENEFICIARY_CHANGE"):
            with self.subTest(action_type=action_type):
                approval_id = self._maker_then_verifier(action_type)
                status, body = self._request(self.owner_session, self.owner_csrf,
                    f"/api/p6/approvals/{approval_id}/approve", {})
                self.assertEqual(status, 200, body)
                self.assertEqual(body["status"], "APPROVED_PENDING_EXECUTION")

    def test_another_admin_cannot_final_approve(self):
        approval_id = self._maker_then_verifier("VENDOR_PAYMENT")
        status, body = self._request(self.admin_session, self.admin_csrf,
            f"/api/p6/approvals/{approval_id}/approve", {})
        self.assertEqual(status, 403, body)
        self.assertIn("approvals:final_approve", body["error"])

    def test_another_finance_user_cannot_final_approve(self):
        approval_id = self._maker_then_verifier("REFUND")
        status, body = self._request(self.finance_other_session, self.finance_other_csrf,
            f"/api/p6/approvals/{approval_id}/approve", {})
        self.assertEqual(status, 403, body)
        self.assertIn("approvals:final_approve", body["error"])

    def test_changing_display_name_to_aryan_does_nothing(self):
        approval_id = self._maker_then_verifier("COMMISSION_RELEASE")
        # Rename the admin's own identity display_name to "Aryan" --
        # authorization must not even look at this mutable field. Opens
        # its own connection, the same way the real server does per
        # request (self.store was already closed after setUp).
        probe = StateStore(Path(self.repo) / ".falguna" / "state.db")
        admin_identity = probe.list("p6_commercial_identities",
            "subject_type=? AND display_name=?", ("STAFF_USER", "Admin User"))[-1]
        probe.update("p6_commercial_identities", admin_identity["id"], display_name="Aryan")
        probe.close()
        status, body = self._request(self.admin_session, self.admin_csrf,
            f"/api/p6/approvals/{approval_id}/approve", {})
        self.assertEqual(status, 403, body)
        self.assertIn("approvals:final_approve", body["error"])

    def test_another_organizations_owner_cannot_approve(self):
        approval_id = self._maker_then_verifier("BENEFICIARY_CHANGE")
        status, body = self._request(self.other_org_owner_session, self.other_org_owner_csrf,
            f"/api/p6/approvals/{approval_id}/approve", {}, organization="acme")
        self.assertEqual(status, 403, body)
        self.assertIn("organization", body["error"])

    def test_revoked_owner_identity_cannot_approve(self):
        approval_id = self._maker_then_verifier("VENDOR_PAYMENT")
        status, body = self._request(self.revoked_owner_session, self.revoked_owner_csrf,
            f"/api/p6/approvals/{approval_id}/approve", {})
        self.assertEqual(status, 403, body)
        self.assertIn("active commercial identity", body["error"])

    def test_maker_verifier_final_approver_separation_still_enforced(self):
        # Maker cannot verify its own request.
        status, body = self._request(self.maker_session, self.maker_csrf, "/api/p6/approvals", {
            "action_type": "REFUND", "target_type": "PAYABLE", "target_id": "target-self-verify",
            "payload": {}, "amount": 500, "currency": "INR", "beneficiary_ref": "vendor:synthetic"})
        self.assertEqual(status, 201, body)
        approval_id = body["id"]
        status, body = self._request(self.maker_session, self.maker_csrf,
            f"/api/p6/approvals/{approval_id}/verify", {})
        self.assertEqual(status, 403, body)
        self.assertIn("independent verifier", body["error"])

        # An owner identity that happens to also be the verifier cannot
        # then final-approve its own verification.
        approval_id = self._maker_then_verifier_by_owner("COMMISSION_RELEASE")
        status, body = self._request(self.owner_session, self.owner_csrf,
            f"/api/p6/approvals/{approval_id}/approve", {})
        self.assertEqual(status, 403, body)
        self.assertIn("independent final approver", body["error"])

    def _maker_then_verifier_by_owner(self, action_type):
        status, body = self._request(self.maker_session, self.maker_csrf, "/api/p6/approvals", {
            "action_type": action_type, "target_type": "PAYABLE", "target_id": f"target-owner-verifies-{action_type}",
            "payload": {}, "amount": 500, "currency": "INR", "beneficiary_ref": "vendor:synthetic"})
        self.assertEqual(status, 201, body)
        approval_id = body["id"]
        status, body = self._request(self.owner_session, self.owner_csrf,
            f"/api/p6/approvals/{approval_id}/verify", {})
        self.assertEqual(status, 200, body)
        return approval_id


if __name__ == "__main__":
    unittest.main()
