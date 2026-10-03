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

from falguna.ecosystem import EcosystemService
from falguna.hq_web import HQ_INDEX_HTML, TTTHQHandler
from falguna.phase6_commercial import CommercialIdentityStore
from falguna.runtime import open_control_plane
from falguna.site_auth import StaffAuthService
from falguna.store import utcnow


class Phase8OperatorHQTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"; self.repo.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(self.repo)], check=True, capture_output=True)
        control, store = open_control_plane(self.repo)
        auth = StaffAuthService(store)
        self.user_id = auth.create_user("owner@synthetic.invalid", "Synthetic Owner", "synthetic-password", "staff")
        self.session_id, self.csrf = "p8-session", "p8-csrf"
        store.create("site_staff_sessions", {"user_id": self.user_id, "csrf_token": self.csrf, "ip_hash": None,
            "user_agent": "test", "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "revoked_at": None, "created_at": utcnow()}, record_id=self.session_id)
        CommercialIdentityStore(store, control.audit).create("ttt-org", "STAFF_USER", self.user_id, "Synthetic Owner", "OWNER", "test")
        service = EcosystemService(store, control.audit)
        self.intake_id = service.create_intake(organization_id="ttt-org", customer_ref="customer-a",
            original_message="Need a workflow", normalized_meaning="Workflow delivery request", geography="India",
            preferred_language="English", category="automation", industry="retail")
        self.route_id = service.recommend_route(self.intake_id, "PARTNER_OR_SPECIALIST_COORDINATION",
            ["specialist capability required"], [{"source": "synthetic"}], ["timeline unknown"])
        self.profile_id = service.create_profile(organization_id="ttt-org", profile_type="DELIVERY_SPECIALIST",
            display_name="Synthetic Provider", capabilities=[{"tag": "automation", "evidence_status": "EVIDENCE_REVIEWED",
            "evidence": [{"source": "synthetic"}]}], geography=["India"], languages=["English"],
            verification_level="EVIDENCE_REVIEWED", verification_evidence=[{"source": "synthetic"}])
        self.foreign_profile_id = service.create_profile(organization_id="other-org", profile_type="DELIVERY_SPECIALIST",
            display_name="Foreign Provider", capabilities=[{"tag": "automation", "evidence_status": "SELF_REPORTED"}])
        store.close()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), TTTHQHandler)
        self.server.app_root = self.repo; self.server.falguna_url = "http://127.0.0.1:1"
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2); self.tmp.cleanup()

    def request(self, path, body=None, authenticated=True, csrf=True, organization="ttt-org"):
        headers = {"Content-Type": "application/json"}
        if authenticated:
            headers.update({"Cookie": f"ttt_staff_session={self.session_id}", "X-TTT-Organization": organization})
        if body is not None and csrf:
            headers["X-CSRF-Token"] = self.csrf
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode() if body is not None else None,
            method="POST" if body is not None else "GET", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_private_operator_snapshot_is_authenticated_and_organization_scoped(self):
        self.assertEqual(self.request("/api/p8/operator", authenticated=False)[0], 401)
        self.assertEqual(self.request("/api/p8/operator", organization="forged-org")[0], 403)
        status, snapshot = self.request("/api/p8/operator")
        self.assertEqual(status, 200)
        self.assertEqual(snapshot["organization_id"], "ttt-org")
        self.assertEqual(snapshot["intakes"][0]["id"], self.intake_id)

    def test_route_override_requires_csrf_and_records_human_reason(self):
        path = f"/api/p8/routes/{self.route_id}/decide"
        self.assertEqual(self.request(path, {"mode": "TTT_ADVISORY", "reason": "human review"}, csrf=False)[0], 403)
        status, route = self.request(path, {"mode": "TTT_ADVISORY", "reason": "Discovery must happen first"})
        self.assertEqual(status, 200)
        self.assertEqual(route["decided_mode"], "TTT_ADVISORY")
        self.assertEqual(route["decided_by_identity_id"], self.request("/api/p6/session")[1].get("identity_id", route["decided_by_identity_id"]))

    def test_profile_verification_uses_persisted_evidence_and_cross_org_fails_closed(self):
        path = f"/api/p8/profiles/{self.profile_id}/review"
        status, profile = self.request(path, {"action": "APPROVE", "reason": "persisted evidence reviewed"})
        self.assertEqual(status, 200); self.assertEqual(profile["verification_level"], "TTT_VERIFIED")
        status, body = self.request(f"/api/p8/profiles/{self.foreign_profile_id}/review",
            {"action": "APPROVE", "reason": "forged", "evidence": [{"kind": "forged"}]})
        self.assertEqual(status, 403); self.assertIn("outside", body["error"])

    def test_operator_ui_is_private_control_plane_not_public_site_navigation(self):
        self.assertIn('id="view-p8Ecosystem"', HQ_INDEX_HTML)
        self.assertIn("async function loadP8Ecosystem()", HQ_INDEX_HTML)
        self.assertIn("money collection", HQ_INDEX_HTML)


if __name__ == "__main__":
    unittest.main()
