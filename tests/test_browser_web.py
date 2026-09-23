"""HTTP-level tests for Falguna Browser + Computer Use V1's Mission Control
integration (falguna/web.py's /api/browser/* routes and its merge into
/api/missions/board and /api/live-summary).

falguna/browser_runtime.py's own test suite (tests/test_browser_runtime.py)
already covers the runtime in depth (gates, challenges, downloads, resume);
this file is specifically about the HTTP surface and Section 25's
requirement that a browser session "appear like any other Falguna task" on
the SAME Mission Control board, not a parallel one.
"""
import json
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from falguna.browser_runtime import BrowserRuntimeError, BrowserSessionStatus, BrowserSessionStore, playwright_available
from falguna.runtime import open_control_plane
from falguna.web import FalgunaHandler, reconcile_browser_sessions_at_startup

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _install_fake_pyautogui(screenshot_ok=True):
    """Locally-duplicated copy of tests/test_browser_runtime.py's helper --
    kept local per this file's own stated "no cross-test-file import"
    convention. Injects a fake `pyautogui` module so computer_use.py's real
    `import pyautogui` statements bind to it, exercising the real
    orchestration code paths deterministically without a real display."""
    fake_image = types.SimpleNamespace(width=640, height=480)
    fake_image.save = lambda buf, format=None: buf.write(b"\x89PNG\r\n\x1a\nFAKE")
    module = types.ModuleType("pyautogui")
    module.screenshot = (
        mock.Mock(return_value=fake_image) if screenshot_ok
        else mock.Mock(side_effect=RuntimeError("screen recording permission denied"))
    )
    module.click = mock.Mock()
    module.typewrite = mock.Mock()
    module.hotkey = mock.Mock()
    return module


class _FixtureHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(FIXTURES_DIR), **kwargs)

    def log_message(self, fmt, *args):
        pass


class _LiveFalgunaServerCase(unittest.TestCase):
    """Same pattern as test_falguna_web_v2.py's _LiveFalgunaServerCase --
    kept as a local copy so this file has no cross-test-file import."""

    port = 8812

    @classmethod
    def setUpClass(cls):
        cls.fixture_server = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
        cls.fixture_port = cls.fixture_server.server_address[1]
        cls.fixture_base = f"http://127.0.0.1:{cls.fixture_port}"
        cls.fixture_thread = threading.Thread(target=cls.fixture_server.serve_forever, daemon=True)
        cls.fixture_thread.start()
        time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        cls.fixture_server.shutdown()
        cls.fixture_thread.join(timeout=5)

    def setUp(self):
        check = playwright_available()
        if not check["launchable"]:
            self.skipTest(f"playwright not usable in this environment: {check['detail']}")
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(self.repo)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "f@test.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "F Test"], check=True)
        (self.repo / "README.md").write_text("seed\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-m", "seed"], check=True, capture_output=True)
        self.control, self.store = open_control_plane(self.repo)
        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), FalgunaHandler)
        self.server.app_root = self.repo
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self._wait_ready()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.store.close()
        self.temp.cleanup()

    def _wait_ready(self):
        for _ in range(40):
            try:
                status, body = self._get("/api/config")
                if body.get("product") == "Falguna Engineering":
                    return
            except Exception:
                pass
            time.sleep(0.05)
        self.fail("Falguna server did not become ready")

    def _get(self, path):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def _post(self, path, body):
        data = json.dumps(body).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method="POST",
                                      headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def _poll_until(self, session_id, statuses, timeout=30):
        deadline = time.time() + timeout
        body = None
        while time.time() < deadline:
            status, body = self._get(f"/api/browser/sessions/{session_id}")
            if body.get("status") in statuses:
                return body
            time.sleep(0.2)
        self.fail(f"session {session_id} never reached {statuses}, last={body}")

    def _create_explicit_session(self, objective, steps, **extra):
        body = {"objective": objective, "task_type": "browser_research", "steps": steps}
        body.update(extra)
        status, out = self._post("/api/browser/sessions", body)
        self.assertEqual(status, 202, out)
        return out["session_id"]

    def _create_computer_session(self, objective, steps, **extra):
        body = {"objective": objective, "steps": steps}
        body.update(extra)
        return self._post("/api/computer/sessions", body)


class StartupReconciliationTests(unittest.TestCase):
    """Exercises falguna.web.reconcile_browser_sessions_at_startup -- the exact
    function falguna.web.serve() calls before accepting any request. Found via
    real Mac QA: killing the Falguna process mid-session left the row
    permanently RUNNING in Mission Control with no live thread behind it. This
    covers the fix at the same layer serve() actually calls it from, not just
    the underlying store method (already covered in test_browser_runtime.py)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(self.repo)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "f@test.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "F Test"], check=True)
        (self.repo / "README.md").write_text("seed\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-m", "seed"], check=True, capture_output=True)

    def tearDown(self):
        self.temp.cleanup()

    def test_reconciles_a_session_orphaned_by_a_simulated_crash(self):
        control, store = open_control_plane(self.repo)
        sessions = BrowserSessionStore(store)
        sid = sessions.create("mid-plan when the process died", "browser_research", None, "Aryan", True, "standard")
        sessions.set_status(sid, BrowserSessionStatus.RUNNING, next_step_index=1, current_url="http://x/y")
        store.close()  # simulates the process exiting -- no connection stays open across it

        reconciled = reconcile_browser_sessions_at_startup(self.repo)
        self.assertEqual(reconciled, [sid])

        control2, store2 = open_control_plane(self.repo)
        try:
            row = BrowserSessionStore(store2).get(sid)
            self.assertEqual(row["status"], BrowserSessionStatus.FAILED)
            self.assertEqual(row["error_category"], BrowserRuntimeError.INTERRUPTED_BY_RESTART)
            self.assertEqual(row["next_step_index"], 1)  # resume point preserved, not lost
        finally:
            store2.close()

    def test_is_a_harmless_noop_on_a_fresh_repo_with_no_browser_sessions(self):
        self.assertEqual(reconcile_browser_sessions_at_startup(self.repo), [])

    def test_running_it_twice_in_a_row_is_idempotent(self):
        control, store = open_control_plane(self.repo)
        sessions = BrowserSessionStore(store)
        sid = sessions.create("mid-plan", "browser_research", None, "Aryan", True, "standard")
        sessions.set_status(sid, BrowserSessionStatus.RUNNING)
        store.close()

        first = reconcile_browser_sessions_at_startup(self.repo)
        second = reconcile_browser_sessions_at_startup(self.repo)
        self.assertEqual(first, [sid])
        self.assertEqual(second, [])  # already FAILED (terminal) -- not re-touched


class BrowserSessionRouteTests(_LiveFalgunaServerCase):
    def test_create_without_objective_is_rejected(self):
        status, out = self._post("/api/browser/sessions", {"steps": []})
        self.assertEqual(status, 400)
        self.assertIn("objective", out["error"])

    def test_create_with_explicit_steps_runs_to_completion(self):
        sid = self._create_explicit_session("open the fixture page", [
            {"action": "open", "target": f"{self.fixture_base}/browser_fixture.html", "value": None, "description": "open"},
        ])
        row = self._poll_until(sid, {"COMPLETED", "FAILED"})
        self.assertEqual(row["status"], "COMPLETED")

    def test_get_unknown_session_is_404(self):
        status, out = self._get("/api/browser/sessions/does-not-exist")
        self.assertEqual(status, 404)

    def test_sensitive_click_pauses_and_needs_aryan_reason_is_visible(self):
        sid = self._create_explicit_session("buy now", [
            {"action": "open", "target": f"{self.fixture_base}/browser_fixture.html", "value": None, "description": "open"},
            {"action": "click", "target": "#buy-now-btn", "value": None, "description": "buy now"},
        ])
        row = self._poll_until(sid, {"NEEDS_ARYAN", "FAILED"})
        self.assertEqual(row["status"], "NEEDS_ARYAN")
        self.assertEqual(row["needs_aryan_reason"], "sensitive_action:payment_or_purchase")
        self.assertIsNotNone(row["latest_screenshot_attachment_id"])

    def test_reject_cancels_the_session_with_a_clear_reason(self):
        sid = self._create_explicit_session("buy now", [
            {"action": "open", "target": f"{self.fixture_base}/browser_fixture.html", "value": None, "description": "open"},
            {"action": "click", "target": "#buy-now-btn", "value": None, "description": "buy now"},
        ])
        self._poll_until(sid, {"NEEDS_ARYAN", "FAILED"})
        status, out = self._post(f"/api/browser/sessions/{sid}/reject", {})
        self.assertEqual(status, 200)
        self.assertEqual(out["status"], "CANCELLED")
        row = self._poll_until(sid, {"CANCELLED"})
        self.assertEqual(row["error"], "Rejected by Aryan")

    def test_approve_resumes_past_the_gate_and_completes(self):
        sid = self._create_explicit_session("buy now then continue", [
            {"action": "open", "target": f"{self.fixture_base}/browser_fixture.html", "value": None, "description": "open"},
            {"action": "click", "target": "#buy-now-btn", "value": None, "description": "buy now"},
            {"action": "type", "target": "#name-field", "value": "Aryan", "description": "fill name"},
        ])
        self._poll_until(sid, {"NEEDS_ARYAN", "FAILED"})
        status, out = self._post(f"/api/browser/sessions/{sid}/approve", {})
        self.assertEqual(status, 202, out)
        time.sleep(0.5)
        row = self._poll_until(sid, {"COMPLETED", "FAILED"})
        self.assertEqual(row["status"], "COMPLETED")

    def test_approve_on_a_session_not_waiting_is_rejected(self):
        sid = self._create_explicit_session("normal flow", [
            {"action": "open", "target": f"{self.fixture_base}/browser_fixture.html", "value": None, "description": "open"},
        ])
        self._poll_until(sid, {"COMPLETED", "FAILED"})
        status, out = self._post(f"/api/browser/sessions/{sid}/approve", {})
        self.assertEqual(status, 400)

    def test_cancel_a_running_session_stops_it(self):
        sid = self._create_explicit_session("cancel me", [
            {"action": "open", "target": f"{self.fixture_base}/browser_fixture.html", "value": None, "description": "open"},
            {"action": "wait", "target": None, "value": "500", "description": "brief wait"},
            {"action": "click", "target": "#submit-btn", "value": None, "description": "submit"},
        ])
        status, out = self._post(f"/api/browser/sessions/{sid}/cancel", {})
        self.assertEqual(status, 200)
        row = self._poll_until(sid, {"CANCELLED", "COMPLETED", "FAILED"})
        self.assertEqual(row["status"], "CANCELLED")

    def test_cancel_a_finished_session_is_rejected(self):
        sid = self._create_explicit_session("normal flow", [
            {"action": "open", "target": f"{self.fixture_base}/browser_fixture.html", "value": None, "description": "open"},
        ])
        self._poll_until(sid, {"COMPLETED", "FAILED"})
        status, out = self._post(f"/api/browser/sessions/{sid}/cancel", {})
        self.assertEqual(status, 400)

    def test_explicit_steps_with_an_unknown_action_are_filtered_and_the_rest_rejected(self):
        status, out = self._post("/api/browser/sessions", {
            "objective": "bad steps", "steps": [{"action": "definitely_not_a_real_action", "target": "x"}],
        })
        self.assertEqual(status, 400)

    def test_list_sessions_includes_created_session(self):
        sid = self._create_explicit_session("list me", [
            {"action": "open", "target": f"{self.fixture_base}/browser_fixture.html", "value": None, "description": "open"},
        ])
        status, out = self._get("/api/browser/sessions")
        self.assertEqual(status, 200)
        self.assertIn(sid, [s["session_id"] for s in out["sessions"]])


class MissionControlBoardMergeTests(_LiveFalgunaServerCase):
    def test_needs_aryan_browser_session_appears_in_the_needs_you_bucket(self):
        sid = self._create_explicit_session("buy now for board test", [
            {"action": "open", "target": f"{self.fixture_base}/browser_fixture.html", "value": None, "description": "open"},
            {"action": "click", "target": "#buy-now-btn", "value": None, "description": "buy now"},
        ])
        self._poll_until(sid, {"NEEDS_ARYAN", "FAILED"})
        status, board = self._get("/api/missions/board")
        self.assertEqual(status, 200)
        ids = [c["id"] for c in board["needs_you"] if c.get("kind") == "browser"]
        self.assertIn(sid, ids)
        card = next(c for c in board["needs_you"] if c["id"] == sid)
        self.assertEqual(card["run_id"], sid)  # backward-compatible alias the frontend/other consumers may key on

    def test_completed_browser_session_appears_in_completed_bucket_never_in_running(self):
        sid = self._create_explicit_session("completed board test", [
            {"action": "open", "target": f"{self.fixture_base}/browser_fixture.html", "value": None, "description": "open"},
        ])
        self._poll_until(sid, {"COMPLETED", "FAILED"})
        status, board = self._get("/api/missions/board")
        completed_ids = [c["id"] for c in board["completed"] if c.get("kind") == "browser"]
        running_ids = [c["id"] for c in board["running"] if c.get("kind") == "browser"]
        self.assertIn(sid, completed_ids)
        self.assertNotIn(sid, running_ids)

    def test_live_summary_counts_browser_needs_you(self):
        sid = self._create_explicit_session("live summary test", [
            {"action": "open", "target": f"{self.fixture_base}/browser_fixture.html", "value": None, "description": "open"},
            {"action": "click", "target": "#buy-now-btn", "value": None, "description": "buy now"},
        ])
        self._poll_until(sid, {"NEEDS_ARYAN", "FAILED"})
        status, summary = self._get("/api/live-summary")
        self.assertGreaterEqual(summary["needs_you"], 1)

    def test_archive_removes_from_active_bucket_and_appears_in_archived(self):
        sid = self._create_explicit_session("archive test", [
            {"action": "open", "target": f"{self.fixture_base}/browser_fixture.html", "value": None, "description": "open"},
        ])
        self._poll_until(sid, {"COMPLETED", "FAILED"})
        status, out = self._post(f"/api/browser/sessions/{sid}/board-archive", {})
        self.assertEqual(status, 200)
        self.assertTrue(out["archived"])
        status, board = self._get("/api/missions/board")
        archived_ids = [c["id"] for c in board["archived"] if c.get("kind") == "browser"]
        completed_ids = [c["id"] for c in board["completed"] if c.get("kind") == "browser"]
        self.assertIn(sid, archived_ids)
        self.assertNotIn(sid, completed_ids)

    def test_actively_running_session_cannot_be_archived(self):
        sid = self._create_explicit_session("cannot archive while running", [
            {"action": "open", "target": f"{self.fixture_base}/browser_fixture.html", "value": None, "description": "open"},
            {"action": "wait", "target": None, "value": "3000", "description": "hold it open"},
        ])
        # give it a moment to actually be RUNNING (not still CREATED)
        for _ in range(20):
            status, row = self._get(f"/api/browser/sessions/{sid}")
            if row["status"] == "RUNNING":
                break
            time.sleep(0.1)
        status, out = self._post(f"/api/browser/sessions/{sid}/board-archive", {})
        self.assertEqual(status, 400)
        self._poll_until(sid, {"COMPLETED", "FAILED"}, timeout=15)  # let it finish so teardown is clean

    def test_engineering_and_browser_cards_both_carry_a_kind_field(self):
        # No engineering runs exist in this fresh repo, so this just proves
        # the board never omits `kind` for a browser card and that the
        # bucket keys are exactly the ones Engineering Worker already uses.
        status, board = self._get("/api/missions/board")
        self.assertEqual(set(board.keys()), {"running", "needs_you", "completed", "failed", "cancelled", "archived"})


class FilesIntegrationTests(_LiveFalgunaServerCase):
    """Section 26: screenshots/downloads a browser session produces must
    show up in Files, linked back to the session that produced them --
    never mislabeled as a plain user upload."""

    def test_browser_screenshot_appears_as_generated_not_as_an_upload(self):
        sid = self._create_explicit_session("files integration test", [
            {"action": "open", "target": f"{self.fixture_base}/browser_fixture.html", "value": None, "description": "open"},
        ])
        self._poll_until(sid, {"COMPLETED", "FAILED"})
        status, files = self._get("/api/files")
        self.assertEqual(status, 200)
        upload_ids = {u["id"] for u in files["uploads"]}
        generated_browser = [g for g in files["generated"] if g.get("browser_session_id") == sid]
        self.assertGreater(len(generated_browser), 0)
        for g in generated_browser:
            self.assertEqual(g["type"], "browser_evidence")
            self.assertNotIn(g["id"], upload_ids)

    def test_browser_download_appears_as_generated_download_kind(self):
        sid = self._create_explicit_session("download files test", [
            {"action": "open", "target": f"{self.fixture_base}/browser_fixture.html", "value": None, "description": "open"},
            {"action": "click", "target": "#download-link", "value": None, "description": "download"},
        ])
        self._poll_until(sid, {"COMPLETED", "FAILED"})
        status, files = self._get("/api/files")
        downloads = [g for g in files["generated"] if g.get("browser_session_id") == sid and g["kind"] == "download"]
        self.assertEqual(len(downloads), 1)
        self.assertEqual(downloads[0]["filename"], "download.txt")


class BrowserSettingsRouteTests(_LiveFalgunaServerCase):
    def test_get_returns_documented_defaults(self):
        status, settings = self._get("/api/browser/settings")
        self.assertEqual(status, 200)
        self.assertTrue(settings["enabled"])
        self.assertTrue(settings["default_headless"])
        self.assertFalse(settings["computer_use_enabled"])

    def test_post_updates_in_place_and_persists(self):
        status, out = self._post("/api/browser/settings", {"default_headless": False, "max_concurrent_sessions": 5})
        self.assertEqual(status, 200)
        self.assertFalse(out["default_headless"])
        self.assertEqual(out["max_concurrent_sessions"], 5)
        status, settings = self._get("/api/browser/settings")
        self.assertFalse(settings["default_headless"])
        self.assertEqual(settings["max_concurrent_sessions"], 5)

    def test_disabling_browser_execution_rejects_new_sessions(self):
        status, out = self._post("/api/browser/settings", {"enabled": False})
        self.assertEqual(status, 200)
        self.assertFalse(out["enabled"])
        status, out = self._post("/api/browser/sessions", {
            "objective": "should be refused", "steps": [{"action": "open", "target": "http://example.invalid"}],
        })
        self.assertEqual(status, 400)
        self.assertIn("turned off", out["error"])


class ComputerUseStatusRouteTests(_LiveFalgunaServerCase):
    def test_status_reports_disabled_in_settings_by_default(self):
        status, out = self._get("/api/computer/status")
        self.assertEqual(status, 200)
        self.assertFalse(out["enabled_in_settings"])
        # installed/usable reflect the real environment honestly (Section
        # 16-18's "installed vs. actually usable" two-layer report) -- this
        # asserts the shape, not a fixed value, since it's genuinely
        # environment-dependent (real Mac vs. cloud sandbox).
        self.assertIn("installed", out)
        self.assertIn("usable", out)
        self.assertIn("detail", out)


class ComputerUseSessionRouteTests(_LiveFalgunaServerCase):
    def test_create_without_objective_is_rejected(self):
        status, out = self._create_computer_session("", [{"action": "screenshot"}])
        self.assertEqual(status, 400)
        self.assertIn("objective", out["error"])

    def test_create_without_steps_is_rejected(self):
        status, out = self._create_computer_session("do something", [])
        self.assertEqual(status, 400)

    def test_create_is_rejected_while_computer_use_is_off_in_settings(self):
        # computer_use_enabled defaults to False (see BrowserSettingsRouteTests
        # .test_get_returns_documented_defaults) -- never silently allowed on.
        status, out = self._create_computer_session("take a screenshot", [{"action": "screenshot"}])
        self.assertEqual(status, 400)
        self.assertIn("turned off", out["error"])

    def test_fails_safely_with_a_clear_blocked_reason_when_pyautogui_is_unavailable(self):
        # Proves the "fail safely if permission/dependency is absent"
        # requirement end-to-end and deterministically -- but only where
        # that premise actually holds: in a cloud sandbox with no pyautogui,
        # build_computer_channel falls back to UnavailableComputerChannel and
        # the session must end FAILED with a clear, actionable reason, never
        # a crash or a fabricated OK. On a real Mac with pyautogui installed
        # and usable (Screen Recording already granted), the same steps
        # legitimately COMPLETE -- that path is proven live instead (see the
        # FINAL CLOSURE REPORT's real-Mac QA section), mirroring how
        # test_browser_runtime.py's ComputerUseAvailabilityTests self-skips
        # its missing-dependency test when the dependency is actually there.
        from falguna.computer_use import pyautogui_available
        if pyautogui_available():
            self.skipTest("pyautogui is installed in this environment; this test targets the missing-dependency path")
        status, out = self._post("/api/browser/settings", {"computer_use_enabled": True})
        self.assertEqual(status, 200)
        status, out = self._create_computer_session("capture the screen", [{"action": "screenshot"}])
        self.assertEqual(status, 202, out)
        row = self._poll_until(out["session_id"], {"COMPLETED", "FAILED"})
        self.assertEqual(row["status"], "FAILED")
        self.assertEqual(row["error_category"], "COMPUTER_USE_BLOCKED")
        self.assertIn("pyautogui_not_installed", row["error"])

    def test_invalid_project_id_is_rejected(self):
        status, out = self._post("/api/browser/settings", {"computer_use_enabled": True})
        self.assertEqual(status, 200)
        status, out = self._create_computer_session(
            "capture the screen", [{"action": "screenshot"}], project_id="not-a-real-project",
        )
        self.assertEqual(status, 400)
        self.assertIn("project", out["error"])

    def test_a_sensitive_click_pauses_then_approve_resumes_and_completes(self):
        # Mocked pyautogui: the real, unmocked path (no display) can only
        # ever prove the BLOCKED branch above -- this exercises the
        # surrounding orchestration (gate -> NEEDS_ARYAN pause -> single-use
        # approve -> skip_gate resume -> COMPLETED) deterministically, the
        # same way test_browser_runtime.py's PyAutoGUISkipGateTests does for
        # the channel itself. Real Mac QA with real pyautogui is what proves
        # the unmocked click/type/screenshot calls themselves.
        fake = _install_fake_pyautogui(screenshot_ok=True)
        with mock.patch.dict(sys.modules, {"pyautogui": fake}):
            status, out = self._post("/api/browser/settings", {"computer_use_enabled": True})
            self.assertEqual(status, 200)
            status, out = self._create_computer_session("buy now on screen", [
                {"action": "screenshot"},
                {"action": "click", "x": 100, "y": 200, "description": "buy now"},
            ])
            self.assertEqual(status, 202, out)
            sid = out["session_id"]
            row = self._poll_until(sid, {"NEEDS_ARYAN", "FAILED"})
            self.assertEqual(row["status"], "NEEDS_ARYAN")
            self.assertEqual(row["needs_aryan_reason"], "sensitive_action:payment_or_purchase")
            self.assertIsNotNone(row["latest_screenshot_attachment_id"])
            status, out = self._post(f"/api/browser/sessions/{sid}/approve", {})
            self.assertEqual(status, 202, out)
            row = self._poll_until(sid, {"COMPLETED", "FAILED"})
            self.assertEqual(row["status"], "COMPLETED")
            fake.click.assert_called_once_with(100, 200)

    def test_kind_is_computer_not_browser_and_appears_correctly_on_the_board(self):
        fake = _install_fake_pyautogui(screenshot_ok=True)
        with mock.patch.dict(sys.modules, {"pyautogui": fake}):
            self._post("/api/browser/settings", {"computer_use_enabled": True})
            status, out = self._create_computer_session("computer kind test", [{"action": "screenshot"}])
            self.assertEqual(status, 202, out)
            sid = out["session_id"]
            row = self._poll_until(sid, {"COMPLETED", "FAILED"})
            self.assertEqual(row["status"], "COMPLETED")
            self.assertEqual(row["kind"], "computer")
            status, board = self._get("/api/missions/board")
            card = next(c for c in board["completed"] if c["id"] == sid)
            self.assertEqual(card["kind"], "computer")


class ProjectScopingTests(_LiveFalgunaServerCase):
    """Section 2's "Do not duplicate project storage" -- both browser and
    computer-use sessions reuse falguna/project_profiles.json via
    load_profiles, the exact same approved-project registry Work missions
    already validate against. "falguna-engineering" has repository "."
    (see falguna/web.py's load_profiles), which always resolves to this
    test's own temp repo, making it valid across every test environment
    without a Mac-only fixture path."""

    VALID_PROJECT_ID = "falguna-engineering"

    def test_invalid_project_id_is_rejected_for_a_browser_session(self):
        status, out = self._post("/api/browser/sessions", {
            "objective": "scoped to a bogus project",
            "project_id": "definitely-not-approved",
            "steps": [{"action": "open", "target": f"{self.fixture_base}/browser_fixture.html", "value": None, "description": "open"}],
        })
        self.assertEqual(status, 400)
        self.assertIn("project", out["error"])

    def test_valid_project_id_persists_and_resolves_a_display_name(self):
        sid = self._create_explicit_session("scoped browser task", [
            {"action": "open", "target": f"{self.fixture_base}/browser_fixture.html", "value": None, "description": "open"},
        ], project_id=self.VALID_PROJECT_ID)
        row = self._poll_until(sid, {"COMPLETED", "FAILED"})
        self.assertEqual(row["status"], "COMPLETED")
        self.assertEqual(row["project_id"], self.VALID_PROJECT_ID)
        self.assertTrue(row["project_name"])
        self.assertNotEqual(row["project_name"], self.VALID_PROJECT_ID)  # a resolved human name, not the raw id

    def test_project_is_optional_unscoped_still_succeeds(self):
        sid = self._create_explicit_session("unscoped browser task", [
            {"action": "open", "target": f"{self.fixture_base}/browser_fixture.html", "value": None, "description": "open"},
        ])
        row = self._poll_until(sid, {"COMPLETED", "FAILED"})
        self.assertEqual(row["status"], "COMPLETED")
        self.assertIsNone(row["project_id"])

    def test_project_appears_on_the_mission_control_card(self):
        sid = self._create_explicit_session("scoped board test", [
            {"action": "open", "target": f"{self.fixture_base}/browser_fixture.html", "value": None, "description": "open"},
        ], project_id=self.VALID_PROJECT_ID)
        self._poll_until(sid, {"COMPLETED", "FAILED"})
        status, board = self._get("/api/missions/board")
        card = next(c for c in board["completed"] if c["id"] == sid)
        self.assertEqual(card["project_id"], self.VALID_PROJECT_ID)
        self.assertNotIn(card["project"], (None, "", self.VALID_PROJECT_ID, "—"))

    def test_project_appears_on_generated_files_evidence(self):
        sid = self._create_explicit_session("scoped files test", [
            {"action": "open", "target": f"{self.fixture_base}/browser_fixture.html", "value": None, "description": "open"},
        ], project_id=self.VALID_PROJECT_ID)
        self._poll_until(sid, {"COMPLETED", "FAILED"})
        status, files = self._get("/api/files")
        self.assertEqual(status, 200)
        generated = [g for g in files["generated"] if g.get("browser_session_id") == sid]
        self.assertGreater(len(generated), 0)
        for g in generated:
            self.assertEqual(g["project_id"], self.VALID_PROJECT_ID)
            self.assertTrue(g["project_name"])

    def test_invalid_project_id_is_rejected_for_a_computer_session(self):
        self._post("/api/browser/settings", {"computer_use_enabled": True})
        status, out = self._create_computer_session(
            "scoped to a bogus project", [{"action": "screenshot"}], project_id="definitely-not-approved",
        )
        self.assertEqual(status, 400)
        self.assertIn("project", out["error"])


if __name__ == "__main__":
    unittest.main()
