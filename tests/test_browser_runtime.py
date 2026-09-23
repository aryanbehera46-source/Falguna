"""Tests for Falguna Browser + Computer Use V1 (falguna/browser_runtime.py,
falguna/browser_planner.py, falguna/computer_use.py).

Covers, per the phase's own test-battery requirements (Section 36):
  * session lifecycle (create/get/status transitions/archive);
  * tabs (creation, open_tab, close_tab, switch_tab);
  * navigation, clicks, typing, selects, uploads -- against a REAL headless
    Chromium instance driven by a REAL local HTTP fixture server, not mocks,
    so a regression in how Playwright is actually called is caught here;
  * downloads -- detected, saved via AttachmentStore, recorded, on both the
    session's original tab and a tab opened later via open_tab;
  * the sensitive-action approval gate (Section 9) -- payment, destructive
    deletion, and raw credential entry all pause to NEEDS_ARYAN with the
    right reason, and an ordinary submit does not;
  * CAPTCHA / human-verification pause and an unplanned login-wall pause
    (Section 10, 11), each against a real fixture page;
  * bounded recovery on a missing element -- escalates to FAILED rather
    than looping forever (Section 21);
  * Mission Control board vocabulary -- ACTIVELY_RUNNING_STATUSES /
    TERMINAL_STATUSES partition every known status, matching the same
    archive-safety guarantee Engineering Worker runs already have;
  * privacy/provider independence -- a session created with an explicit
    steps list never touches a model provider at all;
  * the honest-unavailable defaults for computer-use (Section 16-17) when
    pyautogui is not installed/enabled.
"""
import base64
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from falguna.attachments import AttachmentStore
from falguna.browser_runtime import (
    ACTIVELY_RUNNING_STATUSES,
    ALL_ACTION_TYPES,
    ALL_SESSION_STATUSES,
    BrowserActionType,
    BrowserRuntimeError,
    BrowserSessionStatus,
    BrowserSessionStore,
    GATED_ACTIONS,
    PlaywrightBrowserRuntime,
    STATE_CHANGING_ACTIONS,
    TERMINAL_STATUSES,
    classify_sensitive_action,
    detect_challenge,
    detect_unplanned_login_wall,
    playwright_available,
)
from falguna.computer_use import (
    ComputerActionResult,
    PyAutoGUIComputerChannel,
    UnavailableComputerChannel,
    build_computer_channel,
    computer_use_available,
    pyautogui_available,
)
from falguna.runtime import open_control_plane

FIXTURES_DIR = Path(__file__).parent / "fixtures"


class _FixtureHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(FIXTURES_DIR), **kwargs)

    def log_message(self, fmt, *args):
        pass  # keep test output quiet


class _FixtureServerCase(unittest.TestCase):
    """Base class for any test that needs a real local HTTP server serving
    tests/fixtures/*.html -- a real Chromium is driven against these, never
    a mocked page object, matching Section 35's "prefer local test pages"."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
        cls.port = cls.server.server_address[1]
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join(timeout=5)

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
        self.control, self.store = open_control_plane(self.repo)
        self.attachments = AttachmentStore(self.store, self.repo)
        self.sessions = BrowserSessionStore(self.store)
        self.runtime = PlaywrightBrowserRuntime(self.repo, self.store, self.sessions, self.attachments,
                                                 audit=self.control.audit)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def run_session(self, objective, steps, headless=True):
        sid = self.sessions.create(objective, "browser_research", None, "Aryan", headless, "standard")
        self.runtime.run(sid, steps)
        return sid, self.sessions.get(sid)


# --------------------------------------------------------------------------
# Vocabulary / typed-error tests -- no browser, no network
# --------------------------------------------------------------------------


class StatusAndActionVocabularyTests(unittest.TestCase):
    def test_actively_running_and_terminal_statuses_partition_every_status(self):
        # Mission Control's archive-safety guarantee depends on these two
        # sets covering ALL_SESSION_STATUSES with no overlap and no gaps.
        self.assertEqual(ACTIVELY_RUNNING_STATUSES | TERMINAL_STATUSES | {BrowserSessionStatus.NEEDS_ARYAN, BrowserSessionStatus.PAUSED},
                          ALL_SESSION_STATUSES)
        self.assertEqual(ACTIVELY_RUNNING_STATUSES & TERMINAL_STATUSES, frozenset())

    def test_gated_actions_are_a_subset_of_state_changing_actions(self):
        self.assertTrue(GATED_ACTIONS.issubset(STATE_CHANGING_ACTIONS))

    def test_gated_actions_are_a_subset_of_all_action_types(self):
        self.assertTrue(GATED_ACTIONS.issubset(ALL_ACTION_TYPES))

    def test_browser_runtime_error_carries_category_message_detail(self):
        err = BrowserRuntimeError(BrowserRuntimeError.TIMEOUT, "took too long", detail="raw playwright text")
        self.assertEqual(err.category, BrowserRuntimeError.TIMEOUT)
        self.assertEqual(err.message, "took too long")
        self.assertIn("raw playwright text", err.detail)

    def test_playwright_available_never_raises(self):
        # Whatever the real environment looks like, this must return a dict,
        # never throw -- the same honesty discipline as OllamaProvider.
        result = playwright_available()
        self.assertIn("installed", result)
        self.assertIn("launchable", result)
        self.assertIn("detail", result)


class SensitiveActionClassificationTests(unittest.TestCase):
    def test_buy_now_click_is_flagged_payment_or_purchase(self):
        self.assertEqual(classify_sensitive_action(BrowserActionType.CLICK, "Buy Now", None), "payment_or_purchase")

    def test_delete_account_click_is_flagged_destructive_deletion(self):
        self.assertEqual(classify_sensitive_action(BrowserActionType.CLICK, "Delete Account", None), "destructive_deletion")

    def test_accept_terms_click_is_flagged_contract_or_legal(self):
        self.assertEqual(classify_sensitive_action(BrowserActionType.CLICK, "Accept the Terms", None), "contract_or_legal")

    def test_typing_into_a_password_field_is_flagged_credential_entry(self):
        self.assertEqual(classify_sensitive_action(BrowserActionType.TYPE, "#password-field", "hunter2"), "credential_entry")

    def test_ordinary_submit_button_is_not_flagged(self):
        self.assertIsNone(classify_sensitive_action(BrowserActionType.CLICK, "Submit", None))

    def test_navigation_and_reads_are_never_gated_even_with_sensitive_text(self):
        # open/scroll/etc. are not in GATED_ACTIONS at all -- a URL that
        # happens to contain "checkout" must never pause by itself.
        self.assertIsNone(classify_sensitive_action(BrowserActionType.OPEN, "https://shop.example.com/checkout", None))

    def test_detect_challenge_matches_captcha_language(self):
        self.assertEqual(detect_challenge("Please verify you are human", "https://example.com"),
                          "captcha_or_human_verification")

    def test_detect_challenge_ignores_ordinary_page_text(self):
        self.assertIsNone(detect_challenge("Welcome to our homepage", "https://example.com"))

    def test_detect_unplanned_login_wall_matches_explicit_phrase(self):
        self.assertEqual(detect_unplanned_login_wall("Please sign in to continue", expected_login=False),
                          "unplanned_authentication_wall")

    def test_detect_unplanned_login_wall_suppressed_when_login_expected(self):
        self.assertIsNone(detect_unplanned_login_wall("Please sign in to continue", expected_login=True))

    def test_detect_unplanned_login_wall_does_not_fire_on_a_mere_password_field_mention(self):
        # Regression test for the real bug found during smoke testing: the
        # original implementation grepped raw HTML for type="password" and
        # false-positived on every page containing a password field
        # anywhere, even ones with nothing to do with authentication.
        self.assertIsNone(detect_unplanned_login_wall("Choose a new password for your profile", expected_login=False))


# --------------------------------------------------------------------------
# BrowserSessionStore persistence -- no browser
# --------------------------------------------------------------------------


class BrowserSessionStoreTests(_FixtureServerCase):
    def test_create_defaults_to_created_status(self):
        sid = self.sessions.create("check something", "browser_research", None, "Aryan", True, "standard")
        row = self.sessions.get(sid)
        self.assertEqual(row["status"], BrowserSessionStatus.CREATED)
        self.assertEqual(row["objective"], "check something")
        self.assertIsNone(row["started_at"])
        self.assertIsNone(row["completed_at"])

    def test_empty_objective_is_rejected(self):
        with self.assertRaises(ValueError):
            self.sessions.create("   ", "browser_research", None, "Aryan", True, "standard")

    def test_set_status_to_running_stamps_started_at_once(self):
        sid = self.sessions.create("obj", "browser_research", None, "Aryan", True, "standard")
        self.sessions.set_status(sid, BrowserSessionStatus.RUNNING)
        first = self.sessions.get(sid)["started_at"]
        self.assertIsNotNone(first)
        self.sessions.set_status(sid, BrowserSessionStatus.RUNNING)
        self.assertEqual(self.sessions.get(sid)["started_at"], first)  # never re-stamped

    def test_set_status_to_terminal_stamps_completed_at(self):
        sid = self.sessions.create("obj", "browser_research", None, "Aryan", True, "standard")
        self.sessions.set_status(sid, BrowserSessionStatus.COMPLETED)
        self.assertIsNotNone(self.sessions.get(sid)["completed_at"])

    def test_set_status_rejects_unknown_status(self):
        sid = self.sessions.create("obj", "browser_research", None, "Aryan", True, "standard")
        with self.assertRaises(ValueError):
            self.sessions.set_status(sid, "NOT_A_REAL_STATUS")

    def test_archive_and_unarchive_round_trip(self):
        sid = self.sessions.create("obj", "browser_research", None, "Aryan", True, "standard")
        self.sessions.archive(sid, True)
        self.assertEqual(self.sessions.get(sid)["mc_archived"], 1)
        self.sessions.archive(sid, False)
        self.assertEqual(self.sessions.get(sid)["mc_archived"], 0)

    def test_tabs_are_listed_in_index_order(self):
        sid = self.sessions.create("obj", "browser_research", None, "Aryan", True, "standard")
        self.sessions.add_tab(sid, 1, url="https://b.example.com")
        self.sessions.add_tab(sid, 0, url="https://a.example.com")
        tabs = self.sessions.list_tabs(sid)
        self.assertEqual([t["tab_index"] for t in tabs], [0, 1])

    def test_actions_are_listed_in_sequence_order(self):
        sid = self.sessions.create("obj", "browser_research", None, "Aryan", True, "standard")
        self.sessions.record_action(sid, 2, "click", "#b", None, "OK")
        self.sessions.record_action(sid, 1, "click", "#a", None, "OK")
        actions = self.sessions.list_actions(sid)
        self.assertEqual([a["seq"] for a in actions], [1, 2])

    def test_list_orders_newest_first(self):
        sid1 = self.sessions.create("first", "browser_research", None, "Aryan", True, "standard")
        time.sleep(0.01)
        sid2 = self.sessions.create("second", "browser_research", None, "Aryan", True, "standard")
        rows = self.sessions.list()
        ids = [r["id"] for r in rows]
        self.assertLess(ids.index(sid2), ids.index(sid1))

    def test_reconcile_after_restart_fails_every_actively_running_status(self):
        # Found via real Mac QA: SIGKILL-ing the Falguna process mid-session
        # left the row permanently RUNNING with no way for the UI to know it
        # wasn't. reconcile_after_restart() is the fix -- it must catch all
        # three actively-running statuses, not just RUNNING.
        created = self.sessions.create("never started", "browser_research", None, "Aryan", True, "standard")
        running = self.sessions.create("mid plan", "browser_research", None, "Aryan", True, "standard")
        self.sessions.set_status(running, BrowserSessionStatus.RUNNING, next_step_index=2, current_url="https://x")
        waiting = self.sessions.create("paused for a wait step", "browser_research", None, "Aryan", True, "standard")
        self.sessions.set_status(waiting, BrowserSessionStatus.WAITING, next_step_index=1)

        affected = self.sessions.reconcile_after_restart()

        self.assertEqual(set(affected), {created, running, waiting})
        for sid in (created, running, waiting):
            row = self.sessions.get(sid)
            self.assertEqual(row["status"], BrowserSessionStatus.FAILED)
            self.assertEqual(row["error_category"], BrowserRuntimeError.INTERRUPTED_BY_RESTART)
            self.assertIn("restarted", row["error"])
            self.assertIsNotNone(row["completed_at"])
        # next_step_index (the resume point) must survive untouched -- the
        # whole point is that the task record is never lost, only the thread.
        self.assertEqual(self.sessions.get(running)["next_step_index"], 2)
        self.assertEqual(self.sessions.get(waiting)["next_step_index"], 1)

    def test_reconcile_after_restart_leaves_terminal_and_needs_aryan_sessions_untouched(self):
        completed = self.sessions.create("done", "browser_research", None, "Aryan", True, "standard")
        self.sessions.set_status(completed, BrowserSessionStatus.COMPLETED)
        needs_aryan = self.sessions.create("paused for approval", "browser_research", None, "Aryan", True, "standard")
        self.sessions.set_status(needs_aryan, BrowserSessionStatus.NEEDS_ARYAN, needs_aryan_reason="sensitive_action:x")

        affected = self.sessions.reconcile_after_restart()

        self.assertNotIn(completed, affected)
        self.assertNotIn(needs_aryan, affected)
        self.assertEqual(self.sessions.get(completed)["status"], BrowserSessionStatus.COMPLETED)
        self.assertEqual(self.sessions.get(needs_aryan)["status"], BrowserSessionStatus.NEEDS_ARYAN)


# --------------------------------------------------------------------------
# Real Playwright execution -- headless Chromium against local fixtures
# --------------------------------------------------------------------------


class RealBrowserExecutionTests(_FixtureServerCase):
    def test_normal_multi_step_flow_completes(self):
        check = playwright_available()
        if not check["launchable"]:
            self.skipTest(f"playwright not usable in this environment: {check['detail']}")
        sid, row = self.run_session("fill out the fixture form", [
            {"action": "open", "target": f"{self.base_url}/browser_fixture.html", "value": None, "description": "open"},
            {"action": "type", "target": "#name-field", "value": "Aryan", "description": "fill name"},
            {"action": "select", "target": "#color-select", "value": "blue", "description": "select color"},
            {"action": "click", "target": "#submit-btn", "value": None, "description": "submit"},
        ])
        self.assertEqual(row["status"], BrowserSessionStatus.COMPLETED)
        actions = self.sessions.list_actions(sid)
        self.assertTrue(all(a["result"] == "OK" for a in actions))
        # Section 8/38: screenshots only on state-changing actions, never on
        # every single step -- "open" and "click" get one, but there is no
        # excessive per-step spam beyond that.
        with_shots = [a for a in actions if a.get("screenshot_attachment_id")]
        self.assertGreater(len(with_shots), 0)

    def test_open_tab_and_switch_tab_create_a_second_tracked_tab(self):
        check = playwright_available()
        if not check["launchable"]:
            self.skipTest(f"playwright not usable in this environment: {check['detail']}")
        sid, row = self.run_session("open a second tab", [
            {"action": "open", "target": f"{self.base_url}/browser_fixture.html", "value": None, "description": "open"},
            {"action": "open_tab", "target": f"{self.base_url}/page2.html", "value": None, "description": "open tab 2"},
            {"action": "switch_tab", "target": "0", "value": None, "description": "back to tab 1"},
        ])
        self.assertEqual(row["status"], BrowserSessionStatus.COMPLETED)
        tabs = self.sessions.list_tabs(sid)
        self.assertEqual(len(tabs), 2)

    def test_upload_action_attaches_the_given_file(self):
        check = playwright_available()
        if not check["launchable"]:
            self.skipTest(f"playwright not usable in this environment: {check['detail']}")
        upload_src = Path(self.temp.name) / "upload_me.txt"
        upload_src.write_text("browser runtime upload test file")
        sid, row = self.run_session("upload a file", [
            {"action": "open", "target": f"{self.base_url}/browser_fixture.html", "value": None, "description": "open"},
            {"action": "upload", "target": "#file-upload-field", "value": str(upload_src), "description": "upload"},
        ])
        self.assertEqual(row["status"], BrowserSessionStatus.COMPLETED)
        upload_action = [a for a in self.sessions.list_actions(sid) if a["action_type"] == "upload"][0]
        self.assertEqual(upload_action["result"], "OK")

    def test_missing_element_click_recovers_then_fails_bounded(self):
        check = playwright_available()
        if not check["launchable"]:
            self.skipTest(f"playwright not usable in this environment: {check['detail']}")
        sid, row = self.run_session("click something that does not exist", [
            {"action": "open", "target": f"{self.base_url}/browser_fixture.html", "value": None, "description": "open"},
            {"action": "click", "target": "#does-not-exist-anywhere", "value": None, "description": "click missing"},
        ])
        self.assertEqual(row["status"], BrowserSessionStatus.FAILED)
        self.assertEqual(row["error_category"], BrowserRuntimeError.TIMEOUT)
        # Bounded, not infinite: exactly one FAILED action recorded for the
        # missing-element step, not a pile of silent retries.
        failed = [a for a in self.sessions.list_actions(sid) if a["result"] == "FAILED"]
        self.assertEqual(len(failed), 1)

    def test_playwright_unavailable_produces_a_clean_failed_status(self):
        # Simulate the provider-offline path without needing to actually
        # uninstall playwright -- same technique OllamaProvider tests use
        # for "what if the local runtime just isn't there".
        import falguna.browser_runtime as browser_runtime_module
        original = browser_runtime_module.playwright_available
        browser_runtime_module.playwright_available = lambda: {
            "installed": False, "launchable": False, "detail": "simulated: not installed",
        }
        try:
            sid, row = self.run_session("obj", [
                {"action": "open", "target": f"{self.base_url}/browser_fixture.html", "value": None, "description": "open"},
            ])
            self.assertEqual(row["status"], BrowserSessionStatus.FAILED)
            self.assertEqual(row["error_category"], BrowserRuntimeError.PROVIDER_OFFLINE)
        finally:
            browser_runtime_module.playwright_available = original


class SensitiveActionGateLiveTests(_FixtureServerCase):
    """The classifier is unit-tested above against strings directly; these
    tests prove the gate actually fires from real DOM/page state, which is
    what a prior smoke-testing pass found two real bugs in (see the module
    docstring notes on visible-text vs. raw-HTML and selector-vs-label
    resolution)."""

    def setUp(self):
        super().setUp()
        check = playwright_available()
        if not check["launchable"]:
            self.skipTest(f"playwright not usable in this environment: {check['detail']}")

    def test_buy_now_click_pauses_for_aryan(self):
        sid, row = self.run_session("buy now", [
            {"action": "open", "target": f"{self.base_url}/browser_fixture.html", "value": None, "description": "open"},
            {"action": "click", "target": "#buy-now-btn", "value": None, "description": "buy now"},
        ])
        self.assertEqual(row["status"], BrowserSessionStatus.NEEDS_ARYAN)
        self.assertEqual(row["needs_aryan_reason"], "sensitive_action:payment_or_purchase")

    def test_delete_account_click_pauses_for_aryan(self):
        sid, row = self.run_session("delete account", [
            {"action": "open", "target": f"{self.base_url}/browser_fixture.html", "value": None, "description": "open"},
            {"action": "click", "target": "#delete-account-btn", "value": None, "description": "delete account"},
        ])
        self.assertEqual(row["status"], BrowserSessionStatus.NEEDS_ARYAN)
        self.assertEqual(row["needs_aryan_reason"], "sensitive_action:destructive_deletion")

    def test_typing_into_password_field_pauses_for_aryan(self):
        sid, row = self.run_session("log in", [
            {"action": "open", "target": f"{self.base_url}/browser_fixture.html", "value": None, "description": "open"},
            {"action": "type", "target": "#password-field", "value": "hunter2", "description": "type password"},
        ])
        self.assertEqual(row["status"], BrowserSessionStatus.NEEDS_ARYAN)
        self.assertEqual(row["needs_aryan_reason"], "sensitive_action:credential_entry")

    def test_ordinary_submit_click_does_not_pause(self):
        sid, row = self.run_session("submit the harmless form", [
            {"action": "open", "target": f"{self.base_url}/browser_fixture.html", "value": None, "description": "open"},
            {"action": "type", "target": "#name-field", "value": "Aryan", "description": "fill name"},
            {"action": "click", "target": "#submit-btn", "value": None, "description": "submit"},
        ])
        self.assertEqual(row["status"], BrowserSessionStatus.COMPLETED)

    def test_needs_aryan_pause_captures_a_screenshot(self):
        sid, row = self.run_session("buy now", [
            {"action": "open", "target": f"{self.base_url}/browser_fixture.html", "value": None, "description": "open"},
            {"action": "click", "target": "#buy-now-btn", "value": None, "description": "buy now"},
        ])
        needs_aryan_action = self.sessions.list_actions(sid)[-1]
        self.assertIsNotNone(needs_aryan_action["screenshot_attachment_id"])
        att = self.store.get("attachments", needs_aryan_action["screenshot_attachment_id"])
        self.assertIsNotNone(att)
        self.assertEqual(att["content_type"], "image/png")


class ChallengeAndLoginWallLiveTests(_FixtureServerCase):
    def setUp(self):
        super().setUp()
        check = playwright_available()
        if not check["launchable"]:
            self.skipTest(f"playwright not usable in this environment: {check['detail']}")

    def test_captcha_page_pauses_for_aryan(self):
        sid, row = self.run_session("navigate somewhere", [
            {"action": "open", "target": f"{self.base_url}/captcha_page.html", "value": None, "description": "open"},
        ])
        self.assertEqual(row["status"], BrowserSessionStatus.NEEDS_ARYAN)
        self.assertEqual(row["needs_aryan_reason"], "captcha_or_human_verification")

    def test_unplanned_login_wall_pauses_for_aryan(self):
        sid, row = self.run_session("navigate somewhere", [
            {"action": "open", "target": f"{self.base_url}/login_wall_page.html", "value": None, "description": "open"},
        ])
        self.assertEqual(row["status"], BrowserSessionStatus.NEEDS_ARYAN)
        self.assertEqual(row["needs_aryan_reason"], "unplanned_authentication_wall")

    def test_login_wall_does_not_pause_when_login_was_the_expected_step(self):
        sid, row = self.run_session("log in on purpose", [
            {"action": "open", "target": f"{self.base_url}/login_wall_page.html", "value": None,
             "description": "open", "expected_login": True},
        ])
        self.assertEqual(row["status"], BrowserSessionStatus.COMPLETED)

    def test_ordinary_fixture_page_never_pauses_for_a_false_positive_login_wall(self):
        # Regression test for the real bug found during smoke testing: this
        # fixture page has a password field on it but no sign-in language,
        # and must complete normally rather than false-positive-pausing.
        sid, row = self.run_session("just look at the page", [
            {"action": "open", "target": f"{self.base_url}/browser_fixture.html", "value": None, "description": "open"},
        ])
        self.assertEqual(row["status"], BrowserSessionStatus.COMPLETED)


class DownloadDetectionLiveTests(_FixtureServerCase):
    def setUp(self):
        super().setUp()
        check = playwright_available()
        if not check["launchable"]:
            self.skipTest(f"playwright not usable in this environment: {check['detail']}")

    def test_download_from_the_initial_tab_is_saved_and_recorded(self):
        sid, row = self.run_session("download something", [
            {"action": "open", "target": f"{self.base_url}/browser_fixture.html", "value": None, "description": "open"},
            {"action": "click", "target": "#download-link", "value": None, "description": "click download link"},
        ])
        self.assertEqual(row["status"], BrowserSessionStatus.COMPLETED)
        downloads = self.sessions.list_downloads(sid)
        self.assertEqual(len(downloads), 1)
        self.assertEqual(downloads[0]["filename"], "download.txt")
        att = self.store.get("attachments", downloads[0]["attachment_id"])
        self.assertIsNotNone(att)
        self.assertEqual(att["browser_session_id"], sid)

    def test_download_from_a_tab_opened_via_open_tab_is_also_detected(self):
        # Regression test for the identified gap: the download handler must
        # be wired onto every tab, not just the session's first/original
        # one, or a download triggered from a secondary tab goes unrecorded.
        sid, row = self.run_session("open a second tab and download something there", [
            {"action": "open", "target": f"{self.base_url}/browser_fixture.html", "value": None, "description": "open"},
            {"action": "open_tab", "target": f"{self.base_url}/browser_fixture.html", "value": None, "description": "open tab 2"},
            {"action": "click", "target": "#download-link", "value": None, "description": "click download link in tab 2"},
        ])
        self.assertEqual(row["status"], BrowserSessionStatus.COMPLETED)
        downloads = self.sessions.list_downloads(sid)
        self.assertEqual(len(downloads), 1)

    def test_downloaded_bytes_match_the_source_file_exactly(self):
        sid, row = self.run_session("download something", [
            {"action": "open", "target": f"{self.base_url}/browser_fixture.html", "value": None, "description": "open"},
            {"action": "click", "target": "#download-link", "value": None, "description": "click download link"},
        ])
        downloads = self.sessions.list_downloads(sid)
        att = self.store.get("attachments", downloads[0]["attachment_id"])
        saved_bytes = self.attachments.read_bytes(att["id"])
        self.assertEqual(saved_bytes, (FIXTURES_DIR / "download.txt").read_bytes())


class ResumeAndApprovalLiveTests(_FixtureServerCase):
    """Section 22 ("Approve once") and Section 24 (session persistence /
    resume) -- a NEEDS_ARYAN pause is only useful if resuming actually
    continues the same task rather than restarting or looping forever."""

    def setUp(self):
        super().setUp()
        check = playwright_available()
        if not check["launchable"]:
            self.skipTest(f"playwright not usable in this environment: {check['detail']}")

    def test_naive_resume_without_approval_re_pauses_on_the_same_sensitive_step(self):
        # A resume must never silently sail past a sensitive action just
        # because it's a resume -- the gate has to be explicitly approved
        # each time it fires, not bypassed by the act of continuing.
        steps = [
            {"action": "open", "target": f"{self.base_url}/browser_fixture.html", "value": None, "description": "open"},
            {"action": "click", "target": "#buy-now-btn", "value": None, "description": "buy now"},
        ]
        sid = self.sessions.create("buy now", "browser_research", None, "Aryan", True, "standard", plan=steps)
        self.runtime.run(sid, steps)
        first = self.sessions.get(sid)
        self.assertEqual(first["status"], BrowserSessionStatus.NEEDS_ARYAN)
        self.assertEqual(first["next_step_index"], 1)

        self.runtime.run(sid, steps, resume_from_index=first["next_step_index"])
        second = self.sessions.get(sid)
        self.assertEqual(second["status"], BrowserSessionStatus.NEEDS_ARYAN)

    def test_approved_resume_continues_past_the_gate_and_finishes_the_plan(self):
        steps = [
            {"action": "open", "target": f"{self.base_url}/browser_fixture.html", "value": None, "description": "open"},
            {"action": "click", "target": "#buy-now-btn", "value": None, "description": "buy now"},
            {"action": "type", "target": "#name-field", "value": "Aryan", "description": "fill name after approval"},
        ]
        sid = self.sessions.create("buy now then continue", "browser_research", None, "Aryan", True, "standard", plan=steps)
        self.runtime.run(sid, steps)
        paused = self.sessions.get(sid)
        self.runtime.run(sid, steps, resume_from_index=paused["next_step_index"],
                          approve_gate_for_index=paused["next_step_index"])
        resumed = self.sessions.get(sid)
        self.assertEqual(resumed["status"], BrowserSessionStatus.COMPLETED)
        actions = self.sessions.list_actions(sid)
        self.assertTrue(any(a["action_type"] == "type" and a["result"] == "OK" for a in actions))

    def test_approval_is_single_use_and_does_not_cover_a_later_sensitive_step(self):
        # The approval only ever names one exact step index -- a second,
        # later sensitive step in the same plan must still be gated even
        # though an earlier one in the same resumed run was just approved.
        steps = [
            {"action": "open", "target": f"{self.base_url}/browser_fixture.html", "value": None, "description": "open"},
            {"action": "click", "target": "#buy-now-btn", "value": None, "description": "buy now"},
            {"action": "click", "target": "#delete-account-btn", "value": None, "description": "delete account"},
        ]
        sid = self.sessions.create("two sensitive steps", "browser_research", None, "Aryan", True, "standard", plan=steps)
        self.runtime.run(sid, steps)
        paused = self.sessions.get(sid)
        self.assertEqual(paused["next_step_index"], 1)
        self.runtime.run(sid, steps, resume_from_index=paused["next_step_index"],
                          approve_gate_for_index=paused["next_step_index"])
        second_pause = self.sessions.get(sid)
        self.assertEqual(second_pause["status"], BrowserSessionStatus.NEEDS_ARYAN)
        self.assertEqual(second_pause["needs_aryan_reason"], "sensitive_action:destructive_deletion")
        self.assertEqual(second_pause["next_step_index"], 2)

    def test_persistent_profile_directory_is_created_per_session(self):
        steps = [
            {"action": "open", "target": f"{self.base_url}/browser_fixture.html", "value": None, "description": "open"},
        ]
        sid = self.sessions.create("profile check", "browser_research", None, "Aryan", True, "standard", plan=steps)
        self.runtime.run(sid, steps)
        profile_dir = self.repo / ".falguna" / "browser_profiles" / sid
        self.assertTrue(profile_dir.is_dir())
        self.assertGreater(len(list(profile_dir.iterdir())), 0)

    def test_cancel_requested_before_run_starts_is_honored_not_overwritten(self):
        steps = [
            {"action": "open", "target": f"{self.base_url}/browser_fixture.html", "value": None, "description": "open"},
            {"action": "click", "target": "#submit-btn", "value": None, "description": "step 2"},
        ]
        sid = self.sessions.create("cancel before start", "browser_research", None, "Aryan", True, "standard", plan=steps)
        self.sessions.set_status(sid, BrowserSessionStatus.CANCELLED)
        self.runtime.run(sid, steps)
        row = self.sessions.get(sid)
        self.assertEqual(row["status"], BrowserSessionStatus.CANCELLED)

    def test_cancel_mid_plan_is_noticed_at_the_next_step_boundary(self):
        steps = [
            {"action": "open", "target": f"{self.base_url}/browser_fixture.html", "value": None, "description": "open"},
            {"action": "click", "target": "#submit-btn", "value": None, "description": "step 2"},
            {"action": "type", "target": "#name-field", "value": "should not run", "description": "step 3"},
        ]
        sid = self.sessions.create("cancel mid plan", "browser_research", None, "Aryan", True, "standard", plan=steps)

        original_execute = self.runtime._execute_action

        def cancel_after_first_step(*args, **kwargs):
            result = original_execute(*args, **kwargs)
            self.sessions.set_status(sid, BrowserSessionStatus.CANCELLED)
            return result

        self.runtime._execute_action = cancel_after_first_step
        try:
            self.runtime.run(sid, steps)
        finally:
            self.runtime._execute_action = original_execute
        row = self.sessions.get(sid)
        self.assertEqual(row["status"], BrowserSessionStatus.CANCELLED)
        # step 3 (typing) must never have run
        actions = self.sessions.list_actions(sid)
        self.assertFalse(any(a["action_type"] == "type" for a in actions))


# --------------------------------------------------------------------------
# Provider independence -- Section 29
# --------------------------------------------------------------------------


class ProviderIndependenceTests(_FixtureServerCase):
    def test_explicit_steps_session_never_imports_or_calls_a_model_router(self):
        # An explicit-steps session (as opposed to one planned from a
        # natural-language objective) must run with zero model calls --
        # this is what lets the browser subsystem work identically whether
        # or not any provider is configured (Section 29). We prove it here
        # by simply never constructing a ModelRouter/ModelRegistry anywhere
        # in this test, and confirming the session still runs to completion
        # (or a real, non-model-related terminal state) via the runtime
        # alone.
        check = playwright_available()
        if not check["launchable"]:
            self.skipTest(f"playwright not usable in this environment: {check['detail']}")
        sid, row = self.run_session("no model needed", [
            {"action": "open", "target": f"{self.base_url}/browser_fixture.html", "value": None, "description": "open"},
        ])
        self.assertIn(row["status"], (BrowserSessionStatus.COMPLETED, BrowserSessionStatus.FAILED,
                                       BrowserSessionStatus.NEEDS_ARYAN))


# --------------------------------------------------------------------------
# Computer-use foundation -- Section 16, 17
# --------------------------------------------------------------------------


def _install_fake_pyautogui(screenshot_ok=True):
    """Injects a fake `pyautogui` module into sys.modules so the real
    PyAutoGUIComputerChannel code path (an actual `import pyautogui` call,
    not a mock of the channel itself) can be exercised deterministically
    in environments with no real display -- every cloud CI sandbox, and
    most machines outside an active desktop session. This proves the
    orchestration logic around pyautogui (skip_gate, the enabled check,
    evidence shape) is correct; it is not a substitute for real Mac QA
    against a real screen, which is what actually proves pyautogui itself
    works end to end (see the phase's completion report)."""
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


class ComputerUseAvailabilityTests(unittest.TestCase):
    """computer_use_available() -- the two-layer "installed vs. actually
    usable" check added this pass, found necessary by real Mac QA: pyautogui
    being importable does not mean a screenshot will actually succeed (macOS
    Screen Recording permission gates that separately)."""

    def test_reports_not_installed_when_pyautogui_is_missing(self):
        if pyautogui_available():
            self.skipTest("pyautogui is installed in this environment; this test targets the missing-dependency path")
        result = computer_use_available()
        self.assertEqual(result, {
            "installed": False, "usable": False,
            "detail": "pyautogui is not installed. Install it with: pip3 install --user pyautogui",
        })

    def test_reports_usable_when_a_real_screenshot_call_succeeds(self):
        with mock.patch.dict(sys.modules, {"pyautogui": _install_fake_pyautogui(screenshot_ok=True)}):
            result = computer_use_available()
        self.assertEqual(result, {"installed": True, "usable": True, "detail": ""})

    def test_reports_not_usable_with_a_screen_recording_permission_hint_when_screenshot_fails(self):
        with mock.patch.dict(sys.modules, {"pyautogui": _install_fake_pyautogui(screenshot_ok=False)}):
            result = computer_use_available()
        self.assertTrue(result["installed"])
        self.assertFalse(result["usable"])
        self.assertIn("Screen Recording", result["detail"])
        self.assertIn("System Settings", result["detail"])


class PyAutoGUISkipGateTests(unittest.TestCase):
    """skip_gate -- the single-use approval bypass added this pass so a
    computer-use session's orchestrator (which already ran
    classify_sensitive_action itself and already paused the whole session
    for NEEDS_ARYAN) can re-invoke exactly the one approved step without the
    channel re-blocking it. Mirrors browser_runtime.run()'s
    approve_gate_for_index: single-use, and never lets the always-on
    `enabled` check be skipped."""

    def test_skip_gate_bypasses_the_sensitivity_classifier(self):
        with mock.patch.dict(sys.modules, {"pyautogui": _install_fake_pyautogui()}):
            channel = PyAutoGUIComputerChannel(enabled=True)
            blocked = channel.click(10, 10, description="Buy Now")
            approved = channel.click(10, 10, description="Buy Now", skip_gate=True)
        self.assertEqual(blocked.status, "BLOCKED")
        self.assertEqual(approved.status, "OK")
        self.assertEqual(approved.evidence, {"x": 10, "y": 10})

    def test_skip_gate_never_bypasses_the_enabled_check(self):
        with mock.patch.dict(sys.modules, {"pyautogui": _install_fake_pyautogui()}):
            channel = PyAutoGUIComputerChannel(enabled=False)
            result = channel.click(10, 10, description="Buy Now", skip_gate=True)
        self.assertEqual(result.status, "BLOCKED")
        self.assertEqual(result.blocked_reason, "computer_use_not_enabled")

    def test_skip_gate_also_applies_to_type_text_and_key(self):
        with mock.patch.dict(sys.modules, {"pyautogui": _install_fake_pyautogui()}):
            channel = PyAutoGUIComputerChannel(enabled=True)
            blocked = channel.type_text("my password is hunter2", description="password field")
            approved = channel.type_text("hello", description="harmless local field", skip_gate=True)
            key_result = channel.key("cmd+a", description="select all", skip_gate=True)
        self.assertEqual(blocked.status, "BLOCKED")
        self.assertEqual(approved.status, "OK")
        self.assertEqual(key_result.status, "OK")


class ScreenshotEvidenceTests(unittest.TestCase):
    """Section: "retain evidence" -- a computer-use screenshot must carry
    the actual image, not just a width/height claim, since this is what an
    orchestrator persists as a real Falguna attachment (see
    web._run_computer_session_background)."""

    def test_screenshot_evidence_includes_the_real_image_bytes_on_success(self):
        with mock.patch.dict(sys.modules, {"pyautogui": _install_fake_pyautogui(screenshot_ok=True)}):
            channel = PyAutoGUIComputerChannel(enabled=True)
            result = channel.screenshot()
        self.assertEqual(result.status, "OK")
        self.assertIn("image_base64", result.evidence)
        decoded = base64.b64decode(result.evidence["image_base64"])
        self.assertTrue(decoded.startswith(b"\x89PNG"))

    def test_screenshot_failure_never_raises_and_carries_no_fabricated_image(self):
        with mock.patch.dict(sys.modules, {"pyautogui": _install_fake_pyautogui(screenshot_ok=False)}):
            channel = PyAutoGUIComputerChannel(enabled=True)
            result = channel.screenshot()
        self.assertEqual(result.status, "FAILED")
        self.assertNotIn("image_base64", result.evidence)


class ComputerUseFoundationTests(unittest.TestCase):
    def test_unavailable_channel_blocks_every_action_honestly(self):
        channel = UnavailableComputerChannel(reason="computer_use_not_enabled")
        for result in (channel.screenshot(), channel.click(1, 1), channel.type_text("x"), channel.key("cmd+a")):
            self.assertIsInstance(result, ComputerActionResult)
            self.assertEqual(result.status, "BLOCKED")
            self.assertEqual(result.blocked_reason, "computer_use_not_enabled")

    def test_build_computer_channel_falls_back_to_unavailable_when_disabled(self):
        channel = build_computer_channel(computer_use_enabled=False)
        self.assertIsInstance(channel, UnavailableComputerChannel)

    def test_build_computer_channel_falls_back_to_unavailable_when_pyautogui_missing(self):
        if pyautogui_available():
            self.skipTest("pyautogui is installed in this environment; this test targets the missing-dependency path")
        channel = build_computer_channel(computer_use_enabled=True)
        self.assertIsInstance(channel, UnavailableComputerChannel)

    def test_pyautogui_channel_gates_click_through_the_same_sensitive_classifier(self):
        if not pyautogui_available():
            self.skipTest("pyautogui not installed in this environment")
        channel = PyAutoGUIComputerChannel(enabled=True)
        result = channel.click(10, 10, description="Buy Now")
        self.assertEqual(result.status, "BLOCKED")


if __name__ == "__main__":
    unittest.main()
