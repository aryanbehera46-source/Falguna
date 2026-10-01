"""End-to-end proof through the actual legitimate login page.

Every other Phase 6 HTTP test (including test_phase6_final_approval_
security.py) inserts a site_staff_sessions row directly rather than
going through the real /login form. This test is the one exception: it
drives falguna.site_web's real SiteHandler.do_GET("/login") /
do_POST("/login") -- the exact HTML form, double-submit CSRF cookie, and
StaffAuthService.login() password check a real browser would use --
and then uses the resulting session cookie against the real
TTTHQHandler Finance API on the same shared database, exactly as
documented in serve_site()'s own docstring ("Shares the same
.falguna/state.db as the other two services via open_control_plane").

This is the closest this test environment can get to a real browser
session; see TTT_PHASE6_COMMERCIAL_PLATFORM_CHECKPOINT.md for why an
interactive browser click-through of the login form itself remains a
recorded pre-production item rather than a Phase 6 blocker.
"""

import http.client
import re
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.cookies import SimpleCookie
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode

from falguna.hq_web import TTTHQHandler
from falguna.phase6_commercial import CommercialIdentityStore
from falguna.runtime import open_control_plane
from falguna.site_auth import StaffAuthService
from falguna.site_web import SiteHandler


class LiveLoginFinalApprovalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"; self.repo.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(self.repo)], check=True, capture_output=True)
        control, store = open_control_plane(self.repo)
        auth = StaffAuthService(store)
        self.password = "synthetic-login-password-123456"
        self.owner_user_id = auth.create_user("aryan.live.login@synthetic.invalid", "Aryan Behera",
                                              self.password, "staff")
        identities = CommercialIdentityStore(store, control.audit)
        identities.create("ttt", "STAFF_USER", self.owner_user_id, "Aryan Behera", "OWNER", "test")
        # Maker/verifier provisioned directly (this test's subject is the
        # real login -> final-approval bridge, not maker/verifier HTTP
        # plumbing, which test_phase6_final_approval_security.py already
        # covers end to end).
        maker_user_id = auth.create_user("maker.live@synthetic.invalid", "Maker", self.password, "staff")
        verifier_user_id = auth.create_user("verifier.live@synthetic.invalid", "Verifier", self.password, "staff")
        identities.create("ttt", "STAFF_USER", maker_user_id, "Maker", "FINANCE_OPERATOR", "test")
        identities.create("ttt", "STAFF_USER", verifier_user_id, "Verifier", "FINANCE_OPERATOR", "test")
        self.maker_user_id, self.verifier_user_id = maker_user_id, verifier_user_id
        store.close()

        self.site_server = ThreadingHTTPServer(("127.0.0.1", 0), SiteHandler)
        self.site_server.app_root = self.repo
        self.site_port = self.site_server.server_address[1]
        self.site_thread = threading.Thread(target=self.site_server.serve_forever, daemon=True)
        self.site_thread.start()

        self.hq_server = ThreadingHTTPServer(("127.0.0.1", 0), TTTHQHandler)
        self.hq_server.app_root = self.repo; self.hq_server.falguna_url = "http://127.0.0.1:1"
        self.hq_port = self.hq_server.server_address[1]
        self.hq_thread = threading.Thread(target=self.hq_server.serve_forever, daemon=True)
        self.hq_thread.start()

    def tearDown(self):
        self.site_server.shutdown(); self.site_server.server_close(); self.site_thread.join(timeout=2)
        self.hq_server.shutdown(); self.hq_server.server_close(); self.hq_thread.join(timeout=2)
        self.tmp.cleanup()

    def _real_login(self, email: str) -> str:
        """Drives the actual /login page and form exactly as a browser
        would: GET for the anti-CSRF cookie + hidden token, then POST
        the credentials form with that token and the password. Returns
        the real ttt_staff_session cookie value issued by the real
        StaffAuthService.login() password check."""
        get_req = urllib.request.Request(f"http://127.0.0.1:{self.site_port}/login")
        with urllib.request.urlopen(get_req, timeout=3) as resp:
            html = resp.read().decode()
            csrf_cookie = SimpleCookie()
            for header, value in resp.getheaders():
                if header.lower() == "set-cookie":
                    csrf_cookie.load(value)
        self.assertIn("csrf", csrf_cookie, "login page did not issue the expected anti-CSRF cookie")
        csrf_value = csrf_cookie["csrf"].value
        match = re.search(r'name="csrf_token" value="([^"]*)"', html)
        self.assertIsNotNone(match, "login form did not render a csrf_token field")
        self.assertEqual(match.group(1), csrf_value, "rendered csrf_token did not match the issued cookie")

        status, headers, _ = self._post_login_form(csrf_value, email, self.password)
        # The real _handle_login() redirects (does not auto-follow) on
        # success -- http.client, unlike urllib.request, does not follow
        # redirects transparently, so this status is the login handler's
        # own real response, not whatever page the redirect points at.
        self.assertIn(status, (301, 302, 303, 307, 308), f"a successful real login should redirect to /staff, got {status}")
        session_cookie = SimpleCookie()
        for header, value in headers:
            if header.lower() == "set-cookie":
                session_cookie.load(value)
        self.assertIn("ttt_staff_session", session_cookie, "real login did not issue a staff session cookie")
        return session_cookie["ttt_staff_session"].value

    def _post_login_form(self, csrf_value: str, email: str, password: str):
        """POSTs the real /login form over a raw HTTPConnection so the
        real handler's own status/headers are observed directly, with no
        redirect-following or 4xx/5xx exception-raising in the way."""
        form = urlencode({"csrf_token": csrf_value, "email": email, "password": password})
        conn = http.client.HTTPConnection("127.0.0.1", self.site_port, timeout=3)
        try:
            conn.request("POST", "/login", body=form, headers={
                "Content-Type": "application/x-www-form-urlencoded", "Cookie": f"csrf={csrf_value}"})
            resp = conn.getresponse()
            body = resp.read()
            return resp.status, resp.getheaders(), body
        finally:
            conn.close()

    def _hq_request(self, session, path, body=None, csrf="", organization="ttt"):
        import json
        headers = {"Content-Type": "application/json", "Cookie": f"ttt_staff_session={session}",
                  "X-TTT-Organization": organization}
        method = "GET"
        if body is not None:
            headers["X-CSRF-Token"] = csrf
            method = "POST"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{self.hq_port}{path}", data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=3) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_real_login_cookie_resolves_commercial_identity_on_hq(self):
        owner_session = self._real_login("aryan.live.login@synthetic.invalid")
        status, body = self._hq_request(owner_session, "/api/p6/session")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["organization_id"], "ttt")
        self.assertEqual(body["role"], "OWNER")
        self.assertEqual(body["display_name"], "Aryan Behera")

    def test_real_login_as_aryan_completes_final_approval_over_http(self):
        owner_session = self._real_login("aryan.live.login@synthetic.invalid")
        maker_session = self._real_login("maker.live@synthetic.invalid")
        verifier_session = self._real_login("verifier.live@synthetic.invalid")
        _, owner_body = self._hq_request(owner_session, "/api/p6/session")
        _, maker_body = self._hq_request(maker_session, "/api/p6/session")
        _, verifier_body = self._hq_request(verifier_session, "/api/p6/session")

        status, body = self._hq_request(maker_session, "/api/p6/approvals", {
            "action_type": "REFUND", "target_type": "PAYABLE", "target_id": "live-login-target",
            "payload": {}, "amount": 500, "currency": "INR", "beneficiary_ref": "vendor:synthetic"},
            csrf=maker_body["csrf_token"])
        self.assertEqual(status, 201, body)
        approval_id = body["id"]

        status, body = self._hq_request(verifier_session, f"/api/p6/approvals/{approval_id}/verify", {},
            csrf=verifier_body["csrf_token"])
        self.assertEqual(status, 200, body)
        self.assertEqual(body["status"], "PENDING_ARYAN_APPROVAL")

        # The actual proof: a session issued by the real /login form and
        # the real password check completes final Aryan approval.
        status, body = self._hq_request(owner_session, f"/api/p6/approvals/{approval_id}/approve", {},
            csrf=owner_body["csrf_token"])
        self.assertEqual(status, 200, body)
        self.assertEqual(body["status"], "APPROVED_PENDING_EXECUTION")

    def test_unauthenticated_hq_access_fails_closed(self):
        status, body = self._hq_request("not-a-real-session", "/api/p6/approvals")
        self.assertEqual(status, 401, body)
        self.assertIn("authenticated", body["error"])

    def test_wrong_password_is_rejected_by_real_login(self):
        get_req = urllib.request.Request(f"http://127.0.0.1:{self.site_port}/login")
        with urllib.request.urlopen(get_req, timeout=3) as resp:
            csrf_cookie = SimpleCookie()
            for header, value in resp.getheaders():
                if header.lower() == "set-cookie":
                    csrf_cookie.load(value)
        csrf_value = csrf_cookie["csrf"].value
        status, headers, _ = self._post_login_form(csrf_value, "aryan.live.login@synthetic.invalid",
                                                    "definitely-the-wrong-password")
        self.assertEqual(status, 401)
        session_cookie = SimpleCookie()
        for header, value in headers:
            if header.lower() == "set-cookie":
                session_cookie.load(value)
        self.assertNotIn("ttt_staff_session", session_cookie, "a failed login must not issue a staff session")

    def test_hq_shell_page_renders_without_a_session(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.hq_port}/")
        with urllib.request.urlopen(req, timeout=3) as resp:
            html = resp.read().decode()
        self.assertEqual(resp.status, 200)
        self.assertNotIn("Traceback (most recent call last)", html)
        self.assertIn("<html", html.lower())


if __name__ == "__main__":
    unittest.main()
