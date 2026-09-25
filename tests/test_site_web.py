"""Real, live-server tests for the public website (falguna/site_web.py).

Follows the same _LiveServerCase pattern already used throughout this test
suite (see tests/test_hq_web.py) -- a real repo, a real HTTP server on a
scratch port, real HTTP requests. Covers routing, the two intake forms'
integration with the real sales pipeline (OpportunityStore), careers
application handling incl. resume validation, CSRF protection, rate
limiting, and the staff login/session lifecycle.
"""
import http.client
import io
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from urllib.parse import urlencode

from falguna.runtime import open_control_plane
from falguna.site_auth import StaffAuthService, MAX_FAILED_LOGINS
from falguna.site_content import ServiceStore, CaseStudyStore, JobStore
import falguna.site_web as site_web
from falguna.site_web import SiteHandler
from http.server import ThreadingHTTPServer


class _SiteLiveServerCase(unittest.TestCase):
    port = 8797

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(self.repo)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "t@test.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Test"], check=True)
        (self.repo / "README.md").write_text("seed\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-m", "seed"], check=True, capture_output=True)

        self.control, self.store = open_control_plane(self.repo)
        ServiceStore(self.store).upsert(dict(
            slug="custom-software-engineering", division="Custom Software Engineering",
            tagline="tagline", summary="summary", deliverables=["a"], process=[{"step": "s", "detail": "d"}],
        ))
        CaseStudyStore(self.store).upsert(dict(
            slug="royal-table", title="Royal Table", client_label="Twenty Two Technologies project",
            is_own_project=True, summary="s", problem="p", approach="a", outcome="o", stack=["Node"],
        ))
        self.job_id = JobStore(self.store).create(dict(
            slug="test-role", title="Test Role", department="Engineering", employment_type="full_time",
            location_policy="Remote", summary="summary", responsibilities=["r"], requirements=["q"],
        ))
        self.auth = StaffAuthService(self.store)
        self.user_id = self.auth.create_user("staff@test.invalid", "Test Staff", "CorrectHorseBattery9!")

        # unique port per test class instance to avoid cross-test collisions
        self.port = self.port + (hash(self._testMethodName) % 400)
        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), SiteHandler)
        self.server.app_root = self.repo
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        time.sleep(0.15)
        site_web._rate_buckets.clear()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.store.close()
        self.temp.cleanup()

    def _conn(self):
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)

    def _get(self, path, headers=None):
        conn = self._conn()
        conn.request("GET", path, headers=headers or {})
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", errors="replace")
        conn.close()
        return resp, body

    def _csrf_cookie(self, path="/"):
        resp, _ = self._get(path)
        set_cookie = resp.getheader("Set-Cookie") or ""
        # http.client folds multiple Set-Cookie into one header joined by ", " in some cases;
        # be defensive and just regex the csrf value.
        import re
        m = re.search(r"csrf=([^;,\s]+)", set_cookie)
        return m.group(1) if m else None

    def _post_form(self, path, fields, cookie_header):
        body = urlencode(fields)
        conn = self._conn()
        conn.request("POST", path, body=body, headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Cookie": cookie_header,
        })
        resp = conn.getresponse()
        text = resp.read().decode("utf-8", errors="replace")
        conn.close()
        return resp, text


    def _post_multipart(self, path, fields, file_field=None, file_bytes=b"", filename="resume.pdf",
                          content_type="application/pdf", cookie_header=""):
        boundary = uuid.uuid4().hex
        parts = []
        for key, value in fields.items():
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode())
        if file_field:
            parts.append(
                (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
                 f"filename=\"{filename}\"\r\nContent-Type: {content_type}\r\n\r\n").encode() + file_bytes + b"\r\n"
            )
        parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(parts)
        conn = self._conn()
        conn.request("POST", path, body=body, headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)), "Cookie": cookie_header,
        })
        resp = conn.getresponse()
        text = resp.read().decode("utf-8", errors="replace")
        conn.close()
        return resp, text


class RoutingTests(_SiteLiveServerCase):
    def test_core_pages_return_200(self):
        for path in ["/", "/about", "/services", "/services/custom-software-engineering",
                      "/work", "/work/royal-table", "/products", "/careers", "/careers/apply",
                      "/careers/test-role", "/insights", "/contact", "/contact/start-a-project",
                      "/contact/general", "/legal/privacy", "/legal/terms", "/legal/accessibility",
                      "/login", "/sitemap.xml", "/robots.txt"]:
            resp, _ = self._get(path)
            self.assertEqual(resp.status, 200, f"{path} returned {resp.status}")

    def test_homepage_is_a_focused_sales_path(self):
        resp, body = self._get("/")
        self.assertEqual(resp.status, 200)
        self.assertIn("We build software that is ready for the", body)
        self.assertIn("Discuss your project", body)
        # This scratch database seeds one service; the home renderer must not
        # add unrelated catalogue sections around whatever is available.
        self.assertEqual(body.count('<div class="service-rail">'), 1)
        self.assertNotIn("A typical first engagement", body)
        self.assertNotIn("What we&#x27;re building ourselves", body)

    def test_detail_page_marks_parent_navigation_current(self):
        resp, body = self._get("/services/custom-software-engineering")
        self.assertEqual(resp.status, 200)
        self.assertIn('<a href="/services" aria-current="page">Services</a>', body)

    def test_unknown_service_and_case_study_slugs_404(self):
        resp, _ = self._get("/services/does-not-exist")
        self.assertEqual(resp.status, 404)
        resp, _ = self._get("/work/does-not-exist")
        self.assertEqual(resp.status, 404)

    def test_unknown_path_returns_404_not_a_crash(self):
        resp, _ = self._get("/this/does/not/exist")
        self.assertEqual(resp.status, 404)

    def test_staff_area_redirects_to_login_when_unauthenticated(self):
        resp, _ = self._get("/staff")
        self.assertEqual(resp.status, 303)
        self.assertEqual(resp.getheader("Location"), "/login")

    def test_security_headers_present_on_every_response(self):
        resp, _ = self._get("/")
        self.assertEqual(resp.getheader("X-Frame-Options"), "DENY")
        self.assertEqual(resp.getheader("X-Content-Type-Options"), "nosniff")
        self.assertIn("default-src 'self'", resp.getheader("Content-Security-Policy") or "")

    def test_sitemap_includes_seeded_content(self):
        resp, body = self._get("/sitemap.xml")
        self.assertEqual(resp.status, 200)
        self.assertIn("/services/custom-software-engineering", body)
        self.assertIn("/work/royal-table", body)
        self.assertIn("/careers/test-role", body)



class ContactIntakeTests(_SiteLiveServerCase):
    def test_forged_csrf_is_rejected_not_processed(self):
        resp, body = self._post_form("/contact/general", {
            "csrf_token": "forged-token-not-matching-any-cookie",
            "name": "Attacker", "email": "a@evil.com", "message": "forged",
        }, cookie_header="")
        self.assertEqual(resp.status, 400)
        self.assertIn("Security check failed", body)
        self.assertEqual(self.store.list("site_enquiries"), [])

    def test_general_enquiry_is_logged_with_no_opportunity_created(self):
        csrf = self._csrf_cookie("/contact/general")
        resp, body = self._post_form("/contact/general", {
            "csrf_token": csrf, "name": "Jamie", "email": "jamie@example.com", "message": "hello",
        }, cookie_header=f"csrf={csrf}")
        self.assertEqual(resp.status, 200)
        rows = self.store.list("site_enquiries", "kind = 'general'")
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["opportunity_id"])

    def test_project_enquiry_creates_a_real_pipeline_opportunity(self):
        csrf = self._csrf_cookie("/contact/start-a-project")
        resp, body = self._post_form("/contact/start-a-project", {
            "csrf_token": csrf, "name": "Prospect", "email": "prospect@example.com",
            "project_title": "New CRM", "company": "Prospect Co", "message": "We need a CRM.",
        }, cookie_header=f"csrf={csrf}")
        self.assertEqual(resp.status, 200)
        self.assertIn("logged in our pipeline", body)
        opps = self.store.list("rh_opportunities", "source = 'website_project_intake'")
        self.assertEqual(len(opps), 1)
        self.assertEqual(opps[0]["stage"], "New")
        self.assertEqual(opps[0]["client_name"], "Prospect Co")

    def test_general_enquiry_opens_a_linked_comms_conversation(self):
        csrf = self._csrf_cookie("/contact/general")
        self._post_form("/contact/general", {
            "csrf_token": csrf, "name": "Jamie", "email": "jamie2@example.com", "message": "hello there",
        }, cookie_header=f"csrf={csrf}")
        enquiry = self.store.list("site_enquiries", "email = 'jamie2@example.com'")[0]
        convs = self.store.list(
            "comm_conversations", "source_ref_type = 'site_enquiry' AND source_ref_id = ?", (enquiry["id"],),
        )
        self.assertEqual(len(convs), 1)
        self.assertEqual(convs[0]["channel"], "WEBSITE")
        self.assertEqual(convs[0]["department"], "general")
        self.assertIsNone(convs[0]["linked_opportunity_id"])
        messages = self.store.list("comm_messages", "conversation_id = ?", (convs[0]["id"],))
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["direction"], "INBOUND")
        self.assertIn("hello there", messages[0]["body"])

    def test_project_enquiry_comms_conversation_links_the_real_opportunity(self):
        csrf = self._csrf_cookie("/contact/start-a-project")
        self._post_form("/contact/start-a-project", {
            "csrf_token": csrf, "name": "Prospect2", "email": "prospect2@example.com",
            "project_title": "New Portal", "company": "Prospect2 Co", "message": "We need a portal.",
        }, cookie_header=f"csrf={csrf}")
        opp = self.store.list("rh_opportunities", "client_name = 'Prospect2 Co'")[0]
        convs = self.store.list("comm_conversations", "linked_opportunity_id = ?", (opp["id"],))
        self.assertEqual(len(convs), 1)
        self.assertEqual(convs[0]["department"], "sales")
        self.assertEqual(convs[0]["priority"], "high")
        self.assertEqual(convs[0]["status"], "open")  # 'new' -> 'open' once the inbound message lands

    def test_honeypot_field_silently_drops_bot_submissions(self):
        csrf = self._csrf_cookie("/contact/general")
        resp, _ = self._post_form("/contact/general", {
            "csrf_token": csrf, "name": "Bot", "email": "bot@example.com", "message": "spam",
            "website": "http://spam.example.com",
        }, cookie_header=f"csrf={csrf}")
        self.assertEqual(resp.status, 303)  # redirected, nothing processed
        self.assertEqual(self.store.list("site_enquiries"), [])

    def test_rate_limiting_blocks_after_the_configured_max(self):
        for _ in range(site_web._RATE_LIMIT_MAX):
            csrf = self._csrf_cookie("/contact/general")
            self._post_form("/contact/general", {
                "csrf_token": csrf, "name": "R", "email": "r@example.com", "message": "m",
            }, cookie_header=f"csrf={csrf}")
        csrf = self._csrf_cookie("/contact/general")
        resp, body = self._post_form("/contact/general", {
            "csrf_token": csrf, "name": "R", "email": "r@example.com", "message": "one too many",
        }, cookie_header=f"csrf={csrf}")
        self.assertEqual(resp.status, 429)


class CareersApplicationTests(_SiteLiveServerCase):
    def test_valid_application_with_resume_is_stored(self):
        csrf = self._csrf_cookie("/careers/apply")
        resp, body = self._post_multipart("/careers/apply", {
            "csrf_token": csrf, "job_slug": "test-role", "name": "Applicant",
            "email": "applicant@example.com", "phone": "", "links": "", "cover_note": "note",
        }, file_field="resume", file_bytes=b"%PDF-1.4 fake pdf content", filename="resume.pdf",
           content_type="application/pdf", cookie_header=f"csrf={csrf}")
        self.assertEqual(resp.status, 200)
        self.assertIn("Application received", body)
        apps = self.store.list("site_applications")
        self.assertEqual(len(apps), 1)
        self.assertEqual(apps[0]["job_title_snapshot"], "Test Role")
        self.assertEqual(apps[0]["resume_size_bytes"], len(b"%PDF-1.4 fake pdf content"))
        stored_path = Path(self.repo) / ".falguna" / apps[0]["resume_storage_rel_path"]
        self.assertTrue(stored_path.exists())
        convs = self.store.list("comm_conversations", "linked_application_id = ?", (apps[0]["id"],))
        self.assertEqual(len(convs), 1)
        self.assertEqual(convs[0]["channel"], "CAREERS")
        self.assertEqual(convs[0]["department"], "careers")

    def test_disallowed_file_extension_is_rejected(self):
        csrf = self._csrf_cookie("/careers/apply")
        resp, body = self._post_multipart("/careers/apply", {
            "csrf_token": csrf, "name": "Applicant", "email": "a@example.com",
        }, file_field="resume", file_bytes=b"#!/bin/sh\necho hi", filename="resume.sh",
           content_type="application/x-sh", cookie_header=f"csrf={csrf}")
        self.assertEqual(resp.status, 400)
        self.assertEqual(self.store.list("site_applications"), [])

    def test_missing_resume_is_rejected(self):
        csrf = self._csrf_cookie("/careers/apply")
        resp, body = self._post_multipart("/careers/apply", {
            "csrf_token": csrf, "name": "Applicant", "email": "a@example.com",
        }, file_field=None, cookie_header=f"csrf={csrf}")
        self.assertEqual(resp.status, 400)
        self.assertIn("resume", body.lower())



class StaffLoginTests(_SiteLiveServerCase):
    def _login(self, email, password):
        csrf = self._csrf_cookie("/login")
        resp, body = self._post_form("/login", {
            "csrf_token": csrf, "email": email, "password": password,
        }, cookie_header=f"csrf={csrf}")
        return resp, body

    def test_correct_credentials_redirect_to_staff_with_a_session_cookie(self):
        resp, _ = self._login("staff@test.invalid", "CorrectHorseBattery9!")
        self.assertEqual(resp.status, 303)
        self.assertEqual(resp.getheader("Location"), "/staff")
        set_cookie = resp.getheader("Set-Cookie") or ""
        self.assertIn("ttt_staff_session=", set_cookie)
        self.assertIn("HttpOnly", set_cookie)

    def test_session_cookie_actually_authenticates_the_staff_page(self):
        resp, _ = self._login("staff@test.invalid", "CorrectHorseBattery9!")
        set_cookie = resp.getheader("Set-Cookie") or ""
        import re
        session_val = re.search(r"ttt_staff_session=([^;,\s]+)", set_cookie).group(1)
        resp2, body2 = self._get("/staff", headers={"Cookie": f"ttt_staff_session={session_val}"})
        self.assertEqual(resp2.status, 200)
        self.assertIn("Welcome, Test Staff", body2)

    def test_wrong_password_is_rejected_with_generic_message(self):
        resp, body = self._login("staff@test.invalid", "wrong-password")
        self.assertEqual(resp.status, 401)
        self.assertIn("invalid email or password", body)

    def test_unknown_email_gets_the_same_generic_message_no_enumeration(self):
        resp, body = self._login("nobody@test.invalid", "whatever12345")
        self.assertEqual(resp.status, 401)
        self.assertIn("invalid email or password", body)

    def test_account_locks_after_repeated_failed_logins(self):
        for _ in range(MAX_FAILED_LOGINS):
            self._login("staff@test.invalid", "wrong-password")
        resp, body = self._login("staff@test.invalid", "CorrectHorseBattery9!")
        self.assertEqual(resp.status, 401)
        self.assertIn("locked", body.lower())


if __name__ == "__main__":
    unittest.main()
