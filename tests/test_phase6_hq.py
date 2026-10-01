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
from falguna.store import utcnow


class Phase6AuthenticatedHQTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"; self.repo.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(self.repo)], check=True, capture_output=True)
        self.control, self.store = open_control_plane(self.repo)
        auth = StaffAuthService(self.store)
        self.user_id = auth.create_user("finance@synthetic.invalid", "Finance Operator", "synthetic-password", "staff")
        self.session_id, self.csrf = "synthetic-session", "synthetic-csrf"
        self.store.create("site_staff_sessions", {"user_id": self.user_id, "csrf_token": self.csrf,
            "ip_hash": None, "user_agent": "test", "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "revoked_at": None, "created_at": utcnow()}, record_id=self.session_id)
        identities = CommercialIdentityStore(self.store, self.control.audit)
        identities.create("ttt-org", "STAFF_USER", self.user_id, "Finance Operator", "FINANCE_OPERATOR", "test")
        now = utcnow()
        client_id = self.store.create("clients", {"name": "Synthetic Client", "primary_contact": None,
            "contact_channel": None, "status": "ACTIVE", "total_won_value": 500, "created_at": now, "updated_at": now})
        self.store.create("comm_organizations", {"name": "Synthetic Org", "domain": "synthetic.invalid",
            "linked_client_id": client_id, "notes": None, "created_at": now, "updated_at": now}, record_id="ttt-org")
        self.invoice_id = self.store.create("rh_invoices", {"client_id": client_id, "opportunity_id": None,
            "active_job_id": None, "amount": 500, "currency": "INR", "milestone": None, "due_date": None,
            "amount_received": 0, "status": "SENT", "evidence_json": "[]", "created_at": now, "updated_at": now})
        self.store.close()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), TTTHQHandler)
        self.server.app_root = self.repo; self.server.falguna_url = "http://127.0.0.1:1"
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2)
        self.tmp.cleanup()

    def _request(self, path, body=None, authenticated=True, csrf=True, organization="ttt-org"):
        headers = {"Content-Type": "application/json"}
        if authenticated:
            headers.update({"Cookie": f"ttt_staff_session={self.session_id}", "X-TTT-Organization": organization})
        if body is not None and csrf:
            headers["X-CSRF-Token"] = self.csrf
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data,
                                     method="POST" if body is not None else "GET", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=3) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_commercial_api_rejects_missing_session(self):
        status, body = self._request("/api/p6/approvals", authenticated=False)
        self.assertEqual(status, 401)
        self.assertIn("authenticated", body["error"])

    def test_commercial_api_rejects_cross_organization_header(self):
        status, _ = self._request("/api/p6/approvals", organization="another-org")
        self.assertEqual(status, 403)

    def test_mutation_requires_csrf(self):
        status, body = self._request(f"/api/p6/invoices/{self.invoice_id}/checkout", {
            "amount": 500, "idempotency_key": "checkout", "legal_owner_name": "TTT",
            "beneficiary_ref": "beneficiary:official", "allowed_methods": ["UPI"]}, csrf=False)
        self.assertEqual(status, 403)
        self.assertIn("CSRF", body["error"])

    def test_authenticated_checkout_and_scoped_reconciliation_view(self):
        status, payment = self._request(f"/api/p6/invoices/{self.invoice_id}/checkout", {
            "amount": 500, "idempotency_key": "checkout", "legal_owner_name": "TTT",
            "beneficiary_ref": "beneficiary:official", "allowed_methods": ["UPI"]})
        self.assertEqual(status, 201)
        self.assertEqual(payment["organization_id"], "ttt-org")
        status, snapshot = self._request("/api/p6/reconciliation")
        self.assertEqual(status, 200)
        self.assertEqual(snapshot["organization_id"], "ttt-org")


if __name__ == "__main__":
    unittest.main()
