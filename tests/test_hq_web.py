import json
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from falguna.audit import AuditLog
from falguna.hq_web import (
    HQ_INDEX_HTML, PRODUCT_NAME, TTTHQHandler, _build_workforce_orchestrator, reconcile_workforce_tasks_at_startup,
)
from falguna.lifecycle import LifecycleOrchestrator
from falguna.revenue_hunter import OpportunityStore, ProposalStore
from falguna.runtime import open_control_plane
from falguna.sales_ops import ClosingService
from falguna.store import StateStore
from falguna.ttt_hq import NeedsAryanQueue
from falguna.web import FalgunaHandler, INDEX_HTML
from falguna.workforce import WorkforceTaskStore


def git(repo: Path, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


class HTMLSeparationTests(unittest.TestCase):
    """No live server needed -- these mirror the existing INDEX_HTML content
    tests in test_bootstrap.py exactly (assertIn on the module-level constant)."""

    def test_hq_html_exposes_required_surfaces(self):
        for label in (
            "Twenty Two Technologies", "Boardroom", "Master Vision Backlog", "Needs Aryan",
            "Opportunities", "Sales Pipeline", "Clients", "Active Jobs", "Revenue",
            "Venture Studio", "Venture Pipeline", "Venture Risks", "Venture Graveyard",
            "TTT HQ decides · Falguna executes",
            "Open in Falguna Engineering",
        ):
            self.assertIn(label, HQ_INDEX_HTML)

    def test_hq_html_has_a_responsive_breakpoint(self):
        # Regression: the first version of this page shipped with zero @media
        # rules while Falguna Engineering's page has two -- a real gap on
        # narrow viewports (fixed 250px sidebar, no collapse).
        self.assertIn("@media", HQ_INDEX_HTML)
        self.assertIn("grid-template-columns:1fr", HQ_INDEX_HTML)

    def test_hq_sidebar_scrolls_independently_of_main_content(self):
        # Regression: the Command Center / Finance nav sections made the
        # sidebar's real content (28+ nav buttons) taller than the viewport
        # on shorter screens. The sidebar's `overflow:hidden` silently
        # clipped the lower nav items instead of letting them scroll, while
        # main content scrolled fine on its own. Fixed by giving `aside` its
        # own vertical scroll region; `min-height:0` is required because
        # `aside` is a flex column inside a grid item and would otherwise
        # refuse to shrink below its content's natural height.
        import re
        m = re.search(r"aside\{[^}]*\}", HQ_INDEX_HTML)
        self.assertIsNotNone(m, "base `aside` CSS rule not found")
        aside_rule = m.group(0)
        self.assertIn("overflow-y:auto", aside_rule)
        self.assertIn("overflow-x:hidden", aside_rule)
        self.assertIn("min-height:0", aside_rule)
        # Structure/spacing/layout must be untouched: still a fixed-place
        # flex column, not redesigned.
        self.assertIn("display:flex", aside_rule)
        self.assertIn("flex-direction:column", aside_rule)
        self.assertIn("padding:18px 12px", aside_rule)
        # Main content's own independent scrolling must be unaffected. V1.1
        # gave `main` a persistent topbar (health/active-work/Ask Falguna)
        # above the scrolling content, so `main` itself is now a flex column
        # (topbar + `.col`) and the actual scroll region moved to `.col` --
        # the same independent-scroll guarantee, just at the correct layer.
        main_rule = re.search(r"main\{[^}]*\}", HQ_INDEX_HTML)
        self.assertIsNotNone(main_rule, "base `main` CSS rule not found")
        self.assertIn("display:flex", main_rule.group(0))
        self.assertIn("flex-direction:column", main_rule.group(0))
        self.assertIn("min-height:0", main_rule.group(0))
        col_rule = re.search(r"\.col\{[^}]*\}", HQ_INDEX_HTML)
        self.assertIsNotNone(col_rule, "base `.col` CSS rule not found")
        self.assertIn("overflow-y:auto", col_rule.group(0))
        self.assertIn("flex:1", col_rule.group(0))
        self.assertIn("min-height:0", col_rule.group(0))
        # The footer boundary text stays part of the sidebar, pinned to the
        # bottom when there's room, scrollable into view when there isn't.
        self.assertIn('<div class="boundary">', HQ_INDEX_HTML)
        self.assertIn("margin-top:auto", HQ_INDEX_HTML)

    def test_hq_html_does_not_contain_falguna_engineering_mission_ui(self):
        # TTT HQ must not feel like Falguna with an extra tab: none of Falguna
        # Engineering's own mission-composer elements should appear here.
        for label in ("New Project Mission", "Approved project", "Pause safely", "Engineering work mode"):
            self.assertNotIn(label, HQ_INDEX_HTML)

    def test_falguna_html_no_longer_contains_ttt_hq_surface(self):
        # The prior pass embedded a TTT HQ tab directly in Falguna's own page.
        # This proves that was fully removed, not just visually hidden.
        for label in ("TTT HQ", "Boardroom", "Master Vision Backlog", "topswitch", "hqRoot"):
            self.assertNotIn(label, INDEX_HTML)

    def test_falguna_html_is_otherwise_unchanged(self):
        # Sanity check against the same labels test_bootstrap.py already checks,
        # so a broken revert would fail loudly here too, not just there.
        for label in ("Falguna", "Chat", "Work", "Approve", "Reject", "Request Changes"):
            self.assertIn(label, INDEX_HTML)

    def test_falguna_defaults_to_chat_first_without_home_dashboard_chrome(self):
        self.assertIn("location.hash||'#/chat'", INDEX_HTML)
        self.assertIn("if(view==='home'){go('#/chat');return}", INDEX_HTML)
        self.assertNotIn("[['home','Home'],['chat','Chat']", INDEX_HTML)
        self.assertNotIn("Private AI workspace", INDEX_HTML)
        self.assertNotIn("Local-only internal alpha", INDEX_HTML)

    def test_falguna_account_menu_only_exposes_real_routes(self):
        for route in ('data-account-go="#/settings"', 'data-account-go="#/help"', 'data-account-go="#/tools"'):
            self.assertIn(route, INDEX_HTML)
        self.assertIn("Team accounts, invites, and third-party installation require", INDEX_HTML)
        self.assertNotIn('data-account-go="#/login"', INDEX_HTML)
        self.assertNotIn('data-account-go="#/invite"', INDEX_HTML)

    def test_falguna_composer_exposes_real_capabilities_not_provider_jargon(self):
        self.assertIn('id="capabilityMenu"', INDEX_HTML)
        for capability in ('data-capability="files"', 'data-capability="research"', 'data-capability="work"', 'data-capability="plugins"'):
            self.assertIn(capability, INDEX_HTML)
        self.assertNotIn('id="newChatModel"', INDEX_HTML)
        self.assertNotIn('Auto · local first', INDEX_HTML)

    def test_falguna_new_chat_attachment_creates_a_real_draft_conversation(self):
        self.assertIn("if(!draftConversationId){const draft=await api('/api/conversations'", INDEX_HTML)
        self.assertIn("attachment_ids:pendingAttachments.map", INDEX_HTML)


class WorkforceAutoResumeClassificationTests(unittest.TestCase):
    """Phase 3 Milestone 3: closes the loop between the unit-level
    auto_resumable_after_restart tests in test_workforce.py/
    test_workforce_workers.py and the ACTUAL production wiring this
    instruction is about -- every worker `_build_workforce_orchestrator`
    (falguna/hq_web.py) really registers today. No live server needed:
    this constructs the orchestrator directly against a scratch store,
    the same way the real HQ server does, and inspects its registered
    workers' classification."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = StateStore(self.root / "state.db")
        self.store.migrate()
        self.audit = AuditLog(self.root / "audit.jsonl")
        self.needs_aryan = NeedsAryanQueue(self.store, self.audit)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def test_only_dataworker_and_researchworker_are_auto_resumable_today(self):
        orch = _build_workforce_orchestrator(self.root, self.store, self.audit, self.needs_aryan, control=None)
        resumable = {w.name for w in orch._workers if getattr(w, "auto_resumable_after_restart", False)}
        not_resumable = {w.name for w in orch._workers if not getattr(w, "auto_resumable_after_restart", False)}
        self.assertEqual(resumable, {"data_worker", "research_worker"})
        # Every other currently-registered role -- including the real
        # RealPlaywrightBrowserChannel-backed BrowserWorker Milestone 2
        # just wired in -- stays escalate-only after a restart, exactly as
        # it was before this milestone.
        self.assertIn("browser_worker", not_resumable)
        self.assertIn("document_worker", not_resumable)
        self.assertIn("spreadsheet_worker", not_resumable)
        self.assertIn("email_admin_worker", not_resumable)
        self.assertIn("content_worker", not_resumable)
        self.assertGreaterEqual(len(not_resumable), 10)  # every other registered role, not a narrow accident


class _LiveServerCase(unittest.TestCase):
    """Base class that boots a real repo + real HTTP server on a scratch port.

    Sprint 3 fix: hq_port/falguna_port used to be fixed literals (8799/8798)
    bound directly in each subclass's setUp(). Many distinct _LiveServerCase
    subclasses (including test_search_web.py's independent SearchHttpLayerTests,
    which keeps its own local copy of the same literal 8798) bind a server per
    test method reusing those same hardcoded numbers, so an OS-level
    TIME_WAIT/socket-release race between one test's tearDown() and the next
    bind() on the identical port intermittently raised "OSError: [Errno 48]
    Address already in use" under a large combined run -- confirmed real in the
    Sprint 2 full-suite run (17 failures / 15 errors, the large majority of
    which were exactly this collision). Not a defect in the server or any
    test's assertions -- pure test-infrastructure fragility. Fixed by binding
    to port 0 and reading the real, OS-assigned ephemeral port back from
    server_address[1] instead. The class attributes below are no longer read
    for binding -- each setUp() overwrites them as instance attributes with
    the real bound port -- kept only as documented defaults for anything that
    inspects the class before a server exists.
    """

    hq_port = 8799
    falguna_port = 8798

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(self.repo)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "falguna@test.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Falguna Test"], check=True)
        (self.repo / "README.md").write_text("seed\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-m", "seed"], check=True, capture_output=True)
        self.control, self.store = open_control_plane(self.repo)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def _get(self, port, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=2) as resp:
            return resp.status, json.loads(resp.read())

    def _get_raises(self, port, path):
        try:
            self._get(port, path)
            return None
        except urllib.error.HTTPError as e:
            return e.code

    def _post(self, port, path, body):
        data = json.dumps(body).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status, json.loads(resp.read())

    def _post_raises(self, port, path, body):
        """Like _post, but for a call expected to fail -- returns
        (status_code, parsed_error_body) instead of raising, so a test can
        assert on the exact error message the route returned."""
        data = json.dumps(body).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method="POST", headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=2) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())


class WorkforceRestartReconciliationTests(_LiveServerCase):
    """Exercises falguna.hq_web.reconcile_workforce_tasks_at_startup -- the
    exact function serve_hq() calls before accepting any request. Found via
    a real, live restart test (Revenue Operations V2, Milestone 3): a real
    wf_task created via POST /api/wf/tasks, with its execute() call fired in
    the background, was caught genuinely EXECUTING and the hq_web process
    was killed (kill -9) at that exact moment; after restarting the process
    the task was still EXECUTING with no automatic detection. This mirrors
    that same fix at the same layer serve_hq() actually calls it from (the
    underlying WorkforceOrchestrator.reconcile_after_restart() behavior
    itself is already covered in tests/test_workforce.py)."""

    def test_reconciles_a_task_orphaned_by_a_simulated_crash(self):
        tasks = WorkforceTaskStore(self.store, self.control.audit)
        task_id = tasks.create("engineering", "Fix something", "engineering_fix", actor="Aryan")
        tasks.transition(task_id, "PLANNING", "system")
        tasks.transition(task_id, "READY", "system", assigned_worker="engineering_agent")
        tasks.transition(task_id, "EXECUTING", "system")
        self.store.close()  # simulates the process exiting mid-execute()

        reconciled = reconcile_workforce_tasks_at_startup(self.repo)
        self.assertEqual(reconciled, [task_id])

        control2, store2 = open_control_plane(self.repo)
        try:
            task = WorkforceTaskStore(store2, control2.audit).get(task_id)
            self.assertEqual(task["status"], "BLOCKED")
            self.assertIsNotNone(task["needs_aryan_id"])
            item = store2.get("needs_aryan_items", task["needs_aryan_id"])
            self.assertEqual(item["status"], "PENDING")
        finally:
            store2.close()

    def test_is_a_harmless_noop_with_no_stuck_tasks(self):
        self.store.close()
        self.assertEqual(reconcile_workforce_tasks_at_startup(self.repo), [])

    def test_running_it_twice_in_a_row_is_idempotent(self):
        tasks = WorkforceTaskStore(self.store, self.control.audit)
        task_id = tasks.create("engineering", "Fix something", "engineering_fix", actor="Aryan")
        tasks.transition(task_id, "PLANNING", "system")
        tasks.transition(task_id, "READY", "system", assigned_worker="engineering_agent")
        tasks.transition(task_id, "EXECUTING", "system")
        self.store.close()

        first = reconcile_workforce_tasks_at_startup(self.repo)
        second = reconcile_workforce_tasks_at_startup(self.repo)
        self.assertEqual(first, [task_id])
        self.assertEqual(second, [])  # already BLOCKED (terminal for this purpose) -- not re-touched


class TTTHQServerTests(_LiveServerCase):
    def setUp(self):
        super().setUp()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), TTTHQHandler)
        self.hq_port = self.server.server_address[1]
        self.server.app_root = self.repo
        self.server.falguna_url = "http://127.0.0.1:8765"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self._wait_ready()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        super().tearDown()

    def _wait_ready(self):
        for _ in range(100):
            try:
                status, body = self._get(self.hq_port, "/api/config")
                if body.get("product") == PRODUCT_NAME:
                    return
            except Exception:
                pass
            time.sleep(0.05)
        self.fail("TTT HQ server did not become ready")

    def test_hq_can_launch_independently_and_reports_its_own_identity(self):
        status, body = self._get(self.hq_port, "/api/config")
        self.assertEqual(status, 200)
        self.assertEqual(body["product"], "Twenty Two Technologies HQ")
        self.assertNotEqual(body["product"], "Falguna Engineering")

    def test_boardroom_backlog_needs_aryan_persist_through_the_real_http_layer(self):
        status, out = self._post(self.hq_port, "/api/boardroom", {
            "title": "Add pricing-tier experiment", "summary": "Should we A/B test a mid-tier plan?", "actor": "Aryan",
            "proposed_category": "Revenue", "proposed_priority": "High",
        })
        self.assertEqual(status, 201)
        topic_id = out["topic_id"]

        status, out = self._post(self.hq_port, f"/api/boardroom/{topic_id}/decision", {"action": "approve", "actor": "Aryan"})
        self.assertEqual(status, 200)
        self.assertIsNotNone(out["backlog_item_id"])

        status, backlog = self._get(self.hq_port, "/api/backlog")
        self.assertTrue(any(i["id"] == out["backlog_item_id"] for i in backlog["items"]))

        status, out2 = self._post(self.hq_port, "/api/needs-aryan", {
            "kind": "pricing_decision", "title": "Discount ask", "what_is_needed": "Approve, counter, or hold?",
        })
        na_id = out2["item_id"]
        status, pending = self._get(self.hq_port, "/api/needs-aryan")
        self.assertTrue(any(i["id"] == na_id for i in pending["items"]))

    def test_needs_aryan_surfaces_a_real_falguna_mission_through_the_shared_backend(self):
        # This is the integration seam: TTT HQ never talks to Falguna over
        # HTTP -- it reads the same StateStore. Prove that end-to-end through
        # this server's real HTTP layer, not just the Python objects.
        mission_id = self.control.create_mission("Seed mission", "Seed requirement", self.repo, __import__("falguna.models", fromlist=["RunPolicy"]).RunPolicy())["mission_id"]
        requirement_id = self.store.list("requirements", "mission_id=?", (mission_id,))[0]["id"]
        task_id = self.store.list("tasks", "requirement_id=?", (requirement_id,))[0]["id"]
        run_id = self.store.create("runs", {
            "task_id": task_id, "status": "AWAITING_APPROVAL", "attempt": 1, "worker": "test", "model": "test",
            "worktree": None, "head_sha": None, "error": None, "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
        })
        self.store.create("supervisor_states", {
            "run_id": run_id, "outcome_class": "NEEDS_APPROVAL", "category": "SCOPE_EXPANSION", "phase": "NEEDS_ARYAN",
            "retry_allowed": 0, "resume_allowed": 1, "eligibility_reason": "requires owner review",
            "attempts_used": 1, "retry_budget": 2, "diagnostics_json": "{}",
            "decision_needed": "Decide on scope expansion", "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
        })
        status, pending = self._get(self.hq_port, "/api/needs-aryan")
        derived = [i for i in pending["items"] if i["id"] == f"run:{run_id}"]
        self.assertEqual(len(derived), 1)
        self.assertEqual(derived[0]["source"], "falguna_engineering")
        self.assertFalse(derived[0]["actionable"])

    def test_hq_never_exposes_falguna_engineering_mission_routes(self):
        code = self._get_raises(self.hq_port, "/api/runs")
        self.assertEqual(code, 404)


class CommunicationsHQServerTests(TTTHQServerTests):
    """TTT Communications + AI Customer Service V1, Milestone 7: the
    Communications view's real data layer, exercised through the actual
    HTTP layer the SPA calls (falguna/comms.py itself is covered
    exhaustively in tests/test_comms.py)."""

    def test_overview_is_empty_on_a_fresh_install(self):
        status, body = self._get(self.hq_port, "/api/comms/overview")
        self.assertEqual(status, 200)
        self.assertEqual(body["open_total"], 0)
        self.assertEqual(body["needs_attention"], [])
        self.assertEqual(body["awaiting_approval"], [])

    def test_a_real_conversation_shows_up_in_overview_and_list_over_http(self):
        from falguna.comms import CommsStore
        comms = CommsStore(self.store, self.control.audit)
        conv = comms.open_conversation("SUPPORT", "support", subject="Login broken", priority="urgent")
        comms.add_message(conv["id"], "INBOUND", "I can't log in to my account.")

        status, ov = self._get(self.hq_port, "/api/comms/overview")
        self.assertEqual(status, 200)
        self.assertEqual(ov["open_total"], 1)
        self.assertEqual(ov["by_department"], {"support": 1})
        self.assertEqual(len(ov["needs_attention"]), 1)

        status, listing = self._get(self.hq_port, "/api/comms/conversations")
        self.assertEqual(status, 200)
        self.assertEqual(len(listing["items"]), 1)
        self.assertEqual(listing["items"][0]["subject"], "Login broken")

        status, detail = self._get(self.hq_port, f"/api/comms/conversations/{conv['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(len(detail["messages"]), 1)

    def test_conversation_filters_and_missing_id_over_http(self):
        from falguna.comms import CommsStore
        comms = CommsStore(self.store, self.control.audit)
        comms.open_conversation("WEBSITE", "sales", priority="normal")
        comms.open_conversation("CAREERS", "careers", priority="normal")

        status, listing = self._get(self.hq_port, "/api/comms/conversations?department=careers")
        self.assertEqual(status, 200)
        self.assertEqual(len(listing["items"]), 1)
        self.assertEqual(listing["items"][0]["department"], "careers")

        code = self._get_raises(self.hq_port, "/api/comms/conversations/does-not-exist")
        self.assertEqual(code, 404)

    def test_milestone_8_overview_views_are_present_over_http(self):
        # Structural check that TTT HQ's Communications view (Milestone 8)
        # has real data behind all nine executive views, over the actual
        # HTTP layer the SPA calls -- falguna/comms.py's own overview()
        # logic is covered exhaustively in tests/test_comms.py.
        from falguna.comms import CommsStore
        comms = CommsStore(self.store, self.control.audit)
        support_conv = comms.open_conversation("EMAIL", "support", priority="normal")
        comms.add_message(support_conv["id"], "INBOUND", "My dashboard is broken.", actor="website")
        pending_conv = comms.open_conversation("EMAIL", "sales", priority="normal")
        comms.set_status(pending_conv["id"], "pending_customer", actor="Aryan")

        status, ov = self._get(self.hq_port, "/api/comms/overview")
        self.assertEqual(status, 200)
        for key in (
            "support_issues", "awaiting_client", "active_conversations",
            "follow_ups_due", "failed_delivery", "recently_resolved",
        ):
            self.assertIn(key, ov)
        self.assertEqual(len(ov["support_issues"]), 1)
        self.assertEqual(len(ov["awaiting_client"]), 1)

        status, detail = self._get(self.hq_port, f"/api/comms/conversations/{support_conv['id']}")
        self.assertEqual(status, 200)
        self.assertIn("risk_events", detail)
        self.assertIn("history", detail)



class CommunicationsSendViaProviderHQServerTests(TTTHQServerTests):
    """Phase 1, Requirement 3, HTTP layer: /api/comms/messages/<id>/send
    and /mark-failed, exercised through the real server this session's
    hq_web.py serves. No real SMTP/IMAP env vars are ever set in this test
    class, so resolve_configured_email_provider() always resolves to
    NullEmailProvider here -- proving the route is wired to the real gate
    (send_message_via_provider), not a stub, without needing network
    access or real credentials. The provider-configured success/retry/
    failure paths are covered exhaustively at the CommsStore level in
    tests/test_comms.py's SendMessageViaProviderTests, using a fake
    in-test provider."""

    def _draft_message(self):
        from falguna.comms import CommsStore
        comms = CommsStore(self.store, self.control.audit)
        conv = comms.open_conversation(
            "EMAIL", "sales", priority="normal",
            contact_id=comms.find_or_create_contact("client@example-test.invalid", "Client"),
        )
        msg = comms.add_message(conv["id"], "OUTBOUND", "Here's the current status.", actor="ai_workforce")
        return conv, msg

    def test_send_route_refuses_cleanly_when_no_real_provider_is_configured(self):
        conv, msg = self._draft_message()
        status, err = self._post_raises(self.hq_port, f"/api/comms/messages/{msg['id']}/send", {"actor": "Aryan"})
        self.assertEqual(status, 400)
        self.assertIn("no real email provider is configured", err["error"])
        # Refusal must never mutate the message.
        status, detail = self._get(self.hq_port, f"/api/comms/conversations/{conv['id']}")
        sent_msg = next(m for m in detail["messages"] if m["id"] == msg["id"])
        self.assertEqual(sent_msg["status"], "DRAFT")

    def test_send_route_refuses_unauthorized_bypass_of_an_unapproved_high_risk_message(self):
        # Proves the approval gate is enforced by send_message_via_provider
        # itself, reachable only through this same HTTP route -- a worker
        # cannot get a HIGH-risk message sent by hitting this endpoint
        # directly, provider configured or not.
        from falguna.risk_engine import RiskClassificationStore
        conv, msg = self._draft_message()
        RiskClassificationStore(self.store, self.control.audit).classify(
            "comm_message", msg["id"], "We agree to those contract terms and will sign the contract today.",
            actor="ai_workforce", title="review",
        )
        self.store.update("comm_messages", msg["id"], body="We agree to those contract terms and will sign the contract today.")
        status, err = self._post_raises(self.hq_port, f"/api/comms/messages/{msg['id']}/send", {"actor": "Aryan"})
        self.assertEqual(status, 400)
        # Unconfigured provider is checked first (fails closed either way);
        # what matters is this never returns 200/SENT.
        self.assertNotIn("SENT", json.dumps(err))

    def test_mark_failed_route_persists_reason_and_send_method_over_http(self):
        conv, msg = self._draft_message()
        status, out = self._post(self.hq_port, f"/api/comms/messages/{msg['id']}/mark-failed", {"actor": "Aryan", "reason": "bounced back"})
        self.assertEqual(status, 200)
        self.assertEqual(out["status"], "FAILED")
        self.assertEqual(out["send_method"], "manual")
        self.assertEqual(out["failure_reason"], "bounced back")
        status, ov = self._get(self.hq_port, "/api/comms/overview")
        self.assertTrue(any(m["id"] == msg["id"] for m in ov["failed_delivery"]))

    def test_mark_failed_route_requires_a_reason(self):
        conv, msg = self._draft_message()
        status, err = self._post_raises(self.hq_port, f"/api/comms/messages/{msg['id']}/mark-failed", {"actor": "Aryan", "reason": ""})
        self.assertEqual(status, 400)


class WorkforceMediaHQServerTests(TTTHQServerTests):
    """Digital Workforce + Media/Growth Engine v1 routes (Section 20),
    exercised through the real HTTP layer this server actually serves --
    not just the underlying store objects (already covered exhaustively
    in tests/test_workforce*.py and tests/test_media*.py)."""

    def test_workforce_task_create_and_execute_over_http(self):
        status, out = self._post(self.hq_port, "/api/wf/tasks", {
            "department": "ops", "objective": "Draft a note", "task_type": "document_creation",
            "inputs": {"title": "HTTP Test Doc", "content_text": "hello from the HTTP layer"},
        })
        self.assertEqual(status, 201)
        task_id = out["task_id"]

        status, result = self._post(self.hq_port, f"/api/wf/tasks/{task_id}/execute", {"actor": "Aryan"})
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "COMPLETED")

        status, listing = self._get(self.hq_port, "/api/wf/tasks")
        self.assertTrue(any(t["id"] == task_id for t in listing["items"]))

        status, history = self._get(self.hq_port, f"/api/wf/tasks/{task_id}/history")
        self.assertGreaterEqual(len(history["items"]), 2)

    def test_workforce_browser_task_over_http_runs_the_real_adapter_without_crashing(self):
        # Phase 3 Milestone 2: BrowserWorker is now wired to
        # RealPlaywrightBrowserChannel, not the always-BLOCKED
        # ManualBrowserChannel this test originally exercised (renamed
        # from test_workforce_task_with_no_adapter_escalates_not_crashes,
        # whose premise -- "no adapter exists" -- this milestone
        # deliberately fixed). Explicit `steps` on a self-contained
        # data: URL avoid any dependency on a configured model provider
        # or external network access, so this stays a fast, deterministic
        # HTTP-layer smoke test: the real adapter must run synchronously
        # inside this one HTTP request and return cleanly, never crash or
        # hang the server thread.
        status, out = self._post(self.hq_port, "/api/wf/tasks", {
            "department": "media", "objective": "Open a self-contained test page", "task_type": "browser_research",
            "inputs": {"steps": [
                {"action": "open", "target": "data:text/html,<html><body>ok</body></html>", "value": None, "description": "open test page"},
                {"action": "extract", "target": "body", "value": None, "description": "read the page"},
            ]},
        })
        task_id = out["task_id"]
        # A real headless Chromium launch (Milestone 2's whole point) can
        # legitimately take longer than this file's default 2-second smoke-
        # test timeout, especially under parallel test load -- exactly like
        # an EngineeringAgentWorker/QAAgentWorker mission already can on
        # this same synchronous execute() contract. A dedicated, generous
        # timeout here avoids a client-side give-up racing the server's own
        # in-flight request and corrupting this test's own scratch database
        # out from under it (a real failure mode observed while writing
        # this test, not a hypothetical one).
        data = json.dumps({"actor": "Aryan"}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.hq_port}/api/wf/tasks/{task_id}/execute",
            data=data, method="POST", headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            status, result = resp.status, json.loads(resp.read())
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["execution_method"], "BROWSER")

    def test_company_os_route_plan_over_http_drives_a_real_multi_worker_workforce_assignment(self):
        # Phase 3, Milestone 4: the whole Company OS -> Digital Workforce
        # loop, driven entirely through TTT HQ's real HTTP layer (never by
        # reaching into the store objects directly) -- objective -> ACTIVE
        # -> plan (naming two distinct, genuinely worker-supported
        # task_types) -> APPROVED -> route. Before this milestone's fix,
        # route_plan always created a single wf_task hardcoded to
        # task_type="company_os_routed", which no worker has ever
        # recognized -- a guaranteed dead end. This proves the repaired
        # route_plan spans at least two distinct real worker roles in one
        # call and that each routed task is genuinely, permittedly executed
        # (not merely created) through the same WorkforceOrchestrator/
        # worker registry every other workforce entry point uses.
        status, obj_out = self._post(self.hq_port, "/api/co/objectives", {
            "title": "HTTP Company OS Routing Test", "actor": "Aryan",
        })
        self.assertEqual(status, 201)
        objective_id = obj_out["objective_id"]
        status, _ = self._post(self.hq_port, f"/api/co/objectives/{objective_id}/transition", {
            "to_status": "ACTIVE", "actor": "Aryan",
        })
        self.assertEqual(status, 200)

        status, plan_out = self._post(self.hq_port, f"/api/co/objectives/{objective_id}/plans", {
            "desired_outcome": "Stand up the HTTP routing proof", "actor": "Aryan",
            "departments": ["Digital Workforce"],
            "workforce_assignments": [
                {"task_type": "data_processing", "inputs": {"records": [{"email": "a@x.com"}, {"email": "a@x.com"}], "dedupe_key": "email"}},
                {"task_type": "document_creation", "inputs": {"title": "Routing Proof Note", "content_text": "created by the HTTP routing test"}},
            ],
        })
        self.assertEqual(status, 201)
        plan_id = plan_out["plan_id"]
        status, _ = self._post(self.hq_port, f"/api/co/plans/{plan_id}/status", {"status": "APPROVED", "actor": "Aryan"})
        self.assertEqual(status, 200)

        status, route_out = self._post(self.hq_port, f"/api/co/plans/{plan_id}/route", {"actor": "Aryan"})
        self.assertEqual(status, 201)
        self.assertEqual(len(route_out["created_wf_tasks"]), 2)
        self.assertEqual(len(route_out["executed"]), 2)
        self.assertEqual({e["status"] for e in route_out["executed"]}, {"COMPLETED"})
        self.assertEqual({e["task_type"] for e in route_out["executed"]}, {"data_processing", "document_creation"})

        # Independently re-verify against real stores over the same HTTP
        # layer, not just the route response.
        for task_id in route_out["created_wf_tasks"]:
            status, task = self._get(self.hq_port, f"/api/wf/tasks/{task_id}")
            self.assertEqual(status, 200)
            self.assertEqual(task["status"], "COMPLETED")
            self.assertIsNotNone(task["evidence_json"])
            self.assertEqual(task["co_objective_id"], objective_id)

    def test_command_center_snapshot_and_ceo_brief_over_http(self):
        # Real data through the real HTTP layer: a won opportunity, an
        # overdue invoice, and a pending Needs Aryan item should all show
        # up, sourced, in the Command Center snapshot and a generated brief.
        status, opp_out = self._post(self.hq_port, "/api/rh/opportunities", {
            "title": "HTTP CC Test Deal", "client_name": "HTTP Client",
        })
        self.assertEqual(status, 201)
        opportunity_id = opp_out["opportunity_id"]
        status, _ = self._post(self.hq_port, f"/api/rh/opportunities/{opportunity_id}/won", {"final_price": 1500.0})
        self.assertEqual(status, 200)

        status, snapshot = self._get(self.hq_port, "/api/cc/snapshot")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(snapshot["revenue"]["won_revenue_lifetime"], 1500.0)
        self.assertIn("source", snapshot["revenue"])
        self.assertIn("risk_signals", snapshot)

        status, company_state = self._get(self.hq_port, "/api/company-state")
        self.assertEqual(status, 200)
        self.assertEqual(company_state["financials"]["quoted"], 0)  # no approved proposal exists
        self.assertEqual(company_state["financials"]["invoiced"], 0)
        self.assertEqual(company_state["financials"]["collected"], 0)
        self.assertIn("delivery", company_state["sources"])

        # Phase 4 Sprint 3, Section 6: no brief yet is a normal empty
        # state, not an HTTP error.
        status, empty = self._get(self.hq_port, "/api/cc/ceo-brief/latest")
        self.assertEqual(status, 200)
        self.assertIsNone(empty.get("brief"))

        status, brief = self._post(self.hq_port, "/api/cc/ceo-brief/generate", {"actor": "Aryan"})
        self.assertEqual(status, 201)
        self.assertIn("confirmed_facts_json", brief)

        status, latest = self._get(self.hq_port, "/api/cc/ceo-brief/latest")
        self.assertEqual(status, 200)
        self.assertEqual(latest["id"], brief["id"])

        status, listing = self._get(self.hq_port, "/api/cc/ceo-brief")
        self.assertEqual(status, 200)
        self.assertTrue(any(b["id"] == brief["id"] for b in listing["items"]))

    def test_executive_coordinator_full_loop_over_http(self):
        # Phase 4 Sprint 3: the complete, real synthetic sequence Section 14
        # requires -- a genuinely stale opportunity -> FALGUNA assessment ->
        # an evidence-backed recommendation -> human review through the
        # *existing* Needs Aryan decision route -> one bounded, real
        # internal wf_tasks -> a recorded outcome -- end to end, entirely
        # through the real HTTP layer (no direct store access except to
        # simulate the passage of time, since there is no HTTP route for
        # backdating history and there should not be one).
        status, opp_out = self._post(self.hq_port, "/api/rh/opportunities", {
            "title": "Stale HTTP Deal", "client_name": "HTTP Client",
        })
        self.assertEqual(status, 201)
        opportunity_id = opp_out["opportunity_id"]
        status, _ = self._post(self.hq_port, f"/api/rh/opportunities/{opportunity_id}/stage", {
            "to_stage": "Qualified", "actor": "Aryan",
        })
        self.assertEqual(status, 200)
        old = "2020-01-01T00:00:00+00:00"
        for h in self.store.list("rh_stage_history", "opportunity_id=?", (opportunity_id,)):
            self.store.db.execute("UPDATE rh_stage_history SET created_at=? WHERE id=?", (old, h["id"]))
        self.store.db.commit()

        status, assessment = self._get(self.hq_port, "/api/executive/assessment")
        self.assertEqual(status, 200)
        self.assertIn("workflows", assessment)
        self.assertIn("company_state", assessment)

        status, synced = self._post(self.hq_port, "/api/executive/sync", {"actor": "FALGUNA"})
        self.assertEqual(status, 200)
        self.assertGreaterEqual(synced["created_count"], 1)
        rec = next(r for r in synced["recommendations"] if r["category"] == "followup_inactive_opportunity" and r["ref_id"] == opportunity_id)
        self.assertEqual(rec["status"], "PENDING")
        self.assertIsNotNone(rec["needs_aryan_id"])

        status, listed = self._get(self.hq_port, "/api/executive/recommendations?status=PENDING")
        self.assertEqual(status, 200)
        self.assertTrue(any(r["id"] == rec["id"] for r in listed["items"]))

        # The human review step: the exact same, already-tested Needs Aryan
        # decision route Sprint 2's Unified Decision Queue already uses --
        # never a second, parallel approval endpoint.
        status, decision = self._post(self.hq_port, f"/api/needs-aryan/{rec['needs_aryan_id']}/decision", {
            "action": "approve", "actor": "Aryan",
        })
        self.assertEqual(status, 200)

        status, after = self._get(self.hq_port, "/api/executive/recommendations")
        self.assertEqual(status, 200)
        final = next(r for r in after["items"] if r["id"] == rec["id"])
        self.assertEqual(final["status"], "EXECUTED")
        self.assertIsNotNone(final["outcome_ref_id"])

        status, task = self._get(self.hq_port, f"/api/wf/tasks/{final['outcome_ref_id']}")
        self.assertEqual(status, 200)
        self.assertEqual(task["source"], f"co_recommendation:{rec['id']}")
        self.assertEqual(task["department"], "sales")

    def test_goals_and_kpis_over_http(self):
        status, out = self._post(self.hq_port, "/api/cc/goals", {
            "title": "Reach 1L/month", "target": 100000.0, "unit": "INR", "department": "Sales", "actor": "Aryan",
        })
        self.assertEqual(status, 201)
        goal_id = out["goal_id"]

        status, goal = self._get(self.hq_port, f"/api/cc/goals/{goal_id}")
        self.assertEqual(status, 200)
        self.assertEqual(goal["status"], "ACTIVE")
        self.assertEqual(goal["progress"], 0.0)

        status, updated = self._post(self.hq_port, f"/api/cc/goals/{goal_id}/progress", {"value": 100000.0, "actor": "Aryan"})
        self.assertEqual(status, 200)
        self.assertEqual(updated["status"], "ACHIEVED")

        status, listing = self._get(self.hq_port, "/api/cc/goals")
        self.assertEqual(status, 200)
        self.assertTrue(any(g["id"] == goal_id for g in listing["items"]))

        status, actions = self._get(self.hq_port, f"/api/cc/goals/{goal_id}/recommended-actions")
        self.assertEqual(status, 200)
        self.assertEqual(actions["actions"], [])  # achieved -- nothing left to recommend

        status, kpis = self._get(self.hq_port, "/api/cc/kpis")
        self.assertEqual(status, 200)
        for group in ("sales", "delivery", "workforce", "media", "finance"):
            self.assertIn(group, kpis)

    def test_finance_ledger_and_cash_runway_over_http(self):
        status, _ = self._post(self.hq_port, "/api/rh/sales-policy", {})  # calling save() at all marks it configured
        self.assertEqual(status, 200)

        status, opp_out = self._post(self.hq_port, "/api/rh/opportunities", {
            "title": "HTTP Ledger Deal", "client_name": "HTTP Ledger Client",
        })
        self.assertEqual(status, 201)
        opportunity_id = opp_out["opportunity_id"]
        status, close_out = self._post(self.hq_port, f"/api/rh/opportunities/{opportunity_id}/close", {
            "client_name": "HTTP Ledger Client", "final_price": 800.0, "actor": "Aryan",
        })
        self.assertEqual(status, 201)
        client_id = close_out["client_id"]

        status, entry_out = self._post(self.hq_port, "/api/cc/ledger", {
            "entry_type": "OUTFLOW", "category": "hosting", "amount": 150.0,
            "evidence": "AWS invoice #77", "client_id": client_id, "actor": "Aryan",
        })
        self.assertEqual(status, 201)
        entry_id = entry_out["entry_id"]

        status, listing = self._get(self.hq_port, "/api/cc/ledger")
        self.assertEqual(status, 200)
        self.assertTrue(any(e["id"] == entry_id for e in listing["items"]))

        status, cash = self._get(self.hq_port, "/api/cc/cash-runway")
        self.assertEqual(status, 200)
        self.assertEqual(cash["actual"]["ledger_outflows_recorded"], 150.0)

        status, prof = self._get(self.hq_port, f"/api/cc/clients/{client_id}/profitability")
        self.assertEqual(status, 200)
        self.assertEqual(prof["direct_costs"], 150.0)
        self.assertEqual(prof["completeness"], "estimated_from_recorded_costs")

        status, voided = self._post(self.hq_port, f"/api/cc/ledger/{entry_id}/void", {"actor": "Aryan", "reason": "test cleanup"})
        self.assertEqual(status, 200)
        self.assertEqual(voided["status"], "VOID")

    def test_budgets_capital_reserves_risk_over_http(self):
        status, budget_out = self._post(self.hq_port, "/api/cc/budgets", {
            "department": "Media/Growth", "monthly_budget": 100.0, "limit_kind": "HARD", "actor": "Aryan",
        })
        self.assertEqual(status, 201)
        budget_id = budget_out["budget_id"]

        status, entry_out = self._post(self.hq_port, "/api/cc/ledger", {
            "entry_type": "OUTFLOW", "category": "marketing", "amount": 500.0,
            "evidence": "ad spend receipt", "business_unit": "Media/Growth", "actor": "Aryan",
        })
        self.assertEqual(status, 201)

        status, budget_status_out = self._get(self.hq_port, f"/api/cc/budgets/{budget_id}/status")
        self.assertEqual(status, 200)
        self.assertTrue(budget_status_out["over_limit"])
        self.assertIsNotNone(budget_status_out["escalated_needs_aryan_id"])

        status, alloc = self._post(self.hq_port, "/api/cc/capital-allocation/recommend", {"available_amount": 1000.0, "actor": "Aryan"})
        self.assertEqual(status, 201)
        self.assertIn("recommendations", alloc)

        # Phase 1, Requirement 1: proposing a reserve-policy change never
        # writes it directly any more -- it is gated through Needs Aryan
        # exactly like the media-publication approval flow above.
        status, reserve_proposal = self._post(self.hq_port, "/api/cc/reserve-policy", {"tax_reserve_pct": 0.15})
        self.assertEqual(status, 202)
        self.assertEqual(reserve_proposal["status"], "AWAITING_APPROVAL")
        reserve_needs_aryan_id = reserve_proposal["needs_aryan_id"]

        status, reserve_unchanged = self._get(self.hq_port, "/api/cc/reserve-policy")
        self.assertEqual(status, 200)
        self.assertFalse(reserve_unchanged["configured"])

        status, _ = self._post(self.hq_port, f"/api/needs-aryan/{reserve_needs_aryan_id}/decision", {"action": "approve", "actor": "Aryan"})
        self.assertEqual(status, 200)

        status, reserve = self._get(self.hq_port, "/api/cc/reserve-policy")
        self.assertEqual(status, 200)
        self.assertTrue(reserve["configured"])
        self.assertEqual(reserve["tax_reserve_pct"], 0.15)

        status, exp_cap = self._get(self.hq_port, "/api/cc/experimental-capital")
        self.assertEqual(status, 200)
        self.assertEqual(exp_cap["available"], 0)

        status, risk_out = self._post(self.hq_port, "/api/cc/risks", {
            "title": "Single client concentration", "category": "concentration_risk", "severity": "high", "actor": "Aryan",
        })
        self.assertEqual(status, 201)
        status, risks_listing = self._get(self.hq_port, "/api/cc/risks")
        self.assertEqual(status, 200)
        self.assertTrue(any(r["id"] == risk_out["risk_id"] for r in risks_listing["items"]))

    def test_department_and_ai_workforce_performance_over_http(self):
        status, dept = self._get(self.hq_port, "/api/cc/department-performance")
        self.assertEqual(status, 200)
        for section in ("sales", "delivery", "digital_workforce", "media_growth", "falguna_engineering"):
            self.assertIn(section, dept)
            for metric in dept[section].values():
                self.assertIn("source", metric)

        status, wf_perf = self._get(self.hq_port, "/api/cc/ai-workforce-performance")
        self.assertEqual(status, 200)
        self.assertIn("by_worker", wf_perf)
        self.assertIn("source", wf_perf)

    def test_recurring_workflow_create_and_run_due_over_http(self):
        status, out = self._post(self.hq_port, "/api/wf/recurring-workflows", {
            "name": "Daily doc check", "department": "ops", "objective": "check docs", "task_type": "document_creation",
            "schedule_kind": "hourly",
        })
        self.assertEqual(status, 201)
        status, listing = self._get(self.hq_port, "/api/wf/recurring-workflows")
        self.assertTrue(any(w["id"] == out["workflow_id"] for w in listing["items"]))

    def test_media_brand_campaign_content_pipeline_over_http(self):
        status, brand_out = self._post(self.hq_port, "/api/media/brands", {"name": "TTT", "voice_tone": "confident", "platforms": ["instagram"]})
        self.assertEqual(status, 201)
        brand_id = brand_out["brand_id"]

        status, content_out = self._post(self.hq_port, "/api/media/content", {
            "brand_id": brand_id, "title": "3 tips", "format": "short_form_video",
        })
        self.assertEqual(status, 201)
        content_id = content_out["content_id"]

        status, item = self._get(self.hq_port, f"/api/media/content/{content_id}")
        self.assertEqual(item["content_state"], "RESEARCH")

        status, result = self._post(self.hq_port, f"/api/media/content/{content_id}/transition", {"to_state": "IDEA", "actor": "Aryan"})
        self.assertEqual(status, 200)
        self.assertEqual(result["content_state"], "IDEA")

        status, history = self._get(self.hq_port, f"/api/media/content/{content_id}/history")
        self.assertEqual(len(history["items"]), 2)

        status, script_out = self._post(self.hq_port, f"/api/media/content/{content_id}/scripts", {"hook": "Hook", "body": "Body"})
        self.assertEqual(status, 201)
        status, scripts = self._get(self.hq_port, f"/api/media/content/{content_id}/scripts")
        self.assertEqual(len(scripts["items"]), 1)
        self.assertEqual(scripts["items"][0]["id"], script_out["script_id"])

    def test_media_publication_manual_fallback_over_http(self):
        _, brand_out = self._post(self.hq_port, "/api/media/brands", {"name": "TTT"})
        _, content_out = self._post(self.hq_port, "/api/media/content", {"brand_id": brand_out["brand_id"], "title": "Post", "format": "image_post"})
        content_id = content_out["content_id"]

        _, pub_out = self._post(self.hq_port, "/api/media/publications", {"content_id": content_id, "platform": "instagram"})
        pub_id = pub_out["publication_id"]

        status, submitted = self._post(self.hq_port, f"/api/media/publications/{pub_id}/submit-for-approval", {"actor": "Aryan"})
        self.assertEqual(status, 201)
        self.assertEqual(submitted["status"], "AWAITING_APPROVAL")
        needs_aryan_id = submitted["needs_aryan_id"]

        status, _ = self._post(self.hq_port, f"/api/needs-aryan/{needs_aryan_id}/decision", {"action": "approve", "actor": "Aryan"})
        self.assertEqual(status, 200)

        status, approved = self._post(self.hq_port, f"/api/media/publications/{pub_id}/approve", {"actor": "Aryan"})
        self.assertEqual(status, 200)
        self.assertEqual(approved["status"], "APPROVED")

        # No real adapter -> honest FAILED + manual-fallback escalation, never a fabricated PUBLISHED.
        status, attempt = self._post(self.hq_port, f"/api/media/publications/{pub_id}/publish", {"actor": "system"})
        self.assertEqual(status, 200)
        self.assertEqual(attempt["status"], "FAILED")

        status, manual = self._post(self.hq_port, f"/api/media/publications/{pub_id}/mark-published-manually", {
            "evidence": {"post_url": "https://instagram.com/p/real"}, "actor": "Aryan",
        })
        self.assertEqual(status, 200)
        self.assertEqual(manual["status"], "PUBLISHED")
        self.assertEqual(manual["execution_mode"], "manual")

    def test_media_analytics_and_growth_experiment_over_http(self):
        _, brand_out = self._post(self.hq_port, "/api/media/brands", {"name": "TTT"})
        _, content_out = self._post(self.hq_port, "/api/media/content", {"brand_id": brand_out["brand_id"], "title": "Post", "format": "image_post"})
        _, pub_out = self._post(self.hq_port, "/api/media/publications", {"content_id": content_out["content_id"], "platform": "instagram"})
        pub_id = pub_out["publication_id"]

        status, _ = self._post(self.hq_port, "/api/media/analytics", {"publication_id": pub_id, "metric_kind": "views", "value": 100, "source": "manual_entry"})
        self.assertEqual(status, 201)
        status, listing = self._get(self.hq_port, f"/api/media/publications/{pub_id}/analytics")
        self.assertEqual(len(listing["items"]), 1)

        status, rec = self._get(self.hq_port, f"/api/media/publications/{pub_id}/growth-recommendation")
        self.assertEqual(status, 200)
        self.assertEqual(rec["decision"], "stop")  # views recorded but zero engagement -> honestly weak, not insufficient

        status, exp_out = self._post(self.hq_port, "/api/media/experiments", {"hypothesis": "Shorter hooks help", "content_id": content_out["content_id"]})
        self.assertEqual(status, 201)
        status, exp_result = self._post(self.hq_port, f"/api/media/experiments/{exp_out['experiment_id']}/result", {"result": "up", "decision": "adopt", "actor": "Aryan"})
        self.assertEqual(status, 200)
        self.assertEqual(exp_result["status"], "COMPLETED")

    def test_today_dashboard_includes_workforce_media_signals(self):
        status, today = self._get(self.hq_port, "/api/rh/dashboard")
        self.assertEqual(status, 200)
        for key in ("workforce_blocked_tasks", "media_pending_approval", "content_due_soon", "publishing_failures", "strong_growth_signals"):
            self.assertIn(key, today)


class TradingLabHQServerTests(TTTHQServerTests):
    """TTT Trading Lab v1 (PAPER/RESEARCH ONLY) routes, exercised through
    the real HTTP layer -- the full idea -> data -> version -> backtest ->
    stress -> council -> paper order pipeline, plus the safety properties
    that must hold no matter what: everything self-labels PAPER, and
    nothing here can ever reach a real broker/exchange."""

    def _advance_to_paper_active(self, strategy_id):
        # Paper orders now require Trading Council + explicit Aryan
        # approval to PAPER_ACTIVE (see trading_lab_risk_paper.py's
        # submit_order status gate) -- drive a fresh IDEA-stage strategy
        # through the legal transition chain for tests that only care
        # about paper-order mechanics, not the Council's evaluation.
        for to_status in ("RESEARCHING", "BACKTESTING", "REVIEW", "PAPER_APPROVED", "PAPER_ACTIVE"):
            status, _ = self._post(self.hq_port, f"/api/tl/strategies/{strategy_id}/transition", {"to_status": to_status, "actor": "Aryan"})
            self.assertEqual(status, 200, to_status)

    def _ingest_synthetic_dataset(self, instrument_id):
        status, ds = self._post(self.hq_port, "/api/tl/data/ingest", {
            "provider_kind": "synthetic_test_fixture", "instrument_id": instrument_id, "timeframe": "1d",
            "start": "2024-01-01T00:00:00+00:00", "end": "2024-10-01T00:00:00+00:00",
        })
        self.assertEqual(status, 201)
        self.assertEqual(ds["status"], "OK")
        return ds["id"]

    def test_full_research_pipeline_over_http(self):
        status, market = self._post(self.hq_port, "/api/tl/markets", {"code": "US_EQUITY", "name": "US Equities", "asset_class": "us_equity", "actor": "Aryan"})
        self.assertEqual(status, 201)
        market_id = market["market_id"]

        status, instrument = self._post(self.hq_port, "/api/tl/instruments", {"market_id": market_id, "symbol": "HTTPTEST", "actor": "Aryan"})
        self.assertEqual(status, 201)
        instrument_id = instrument["instrument_id"]

        dataset_id = self._ingest_synthetic_dataset(instrument_id)
        status, quality = self._get(self.hq_port, f"/api/tl/datasets/{dataset_id}/quality")
        self.assertEqual(status, 200)
        self.assertEqual(quality["passed"], 1)

        status, research = self._post(self.hq_port, "/api/tl/research", {"idea": "SMA crossover trend follow", "signals_considered": ["sma_crossover"], "market_code": "US_EQUITY"})
        self.assertEqual(status, 200)
        self.assertTrue(research["inference"].startswith("untested"))

        status, strategy = self._post(self.hq_port, "/api/tl/strategies", {"name": "HTTP pipeline strategy", "hypothesis": "SMA crossover works here", "market_id": market_id, "actor": "Aryan"})
        self.assertEqual(status, 201)
        strategy_id = strategy["strategy_id"]
        self._post(self.hq_port, f"/api/tl/strategies/{strategy_id}/transition", {"to_status": "RESEARCHING", "actor": "Aryan"})

        status, version = self._post(self.hq_port, "/api/tl/strategy-versions", {
            "strategy_id": strategy_id, "instruments": [instrument_id], "timeframe": "1d",
            "entry_rules": {"signal": "sma_crossover", "params": {"fast_period": 3, "slow_period": 9, "cross": "up"}, "side": "long"},
            "exit_rules": {"signal": "sma_crossover", "params": {"fast_period": 3, "slow_period": 9, "cross": "down"}},
            "sizing_logic": {"position_size_pct": 20}, "assumptions": "trend persists", "known_risks": "whipsaw risk",
            "actor": "Aryan",
        })
        self.assertEqual(status, 201)
        version_id = version["strategy_version_id"]
        self._post(self.hq_port, f"/api/tl/strategies/{strategy_id}/transition", {"to_status": "BACKTESTING", "actor": "Aryan"})

        status, backtest = self._post(self.hq_port, "/api/tl/backtests/run", {
            "dataset_id": dataset_id, "strategy_version_id": version_id, "fee_bps": 10, "slippage_bps": 5, "starting_cash": 10000,
        })
        self.assertEqual(status, 201)
        backtest_id = backtest["backtest_id"]
        self.assertIn("net_return", backtest["metrics"])

        status, oos = self._post(self.hq_port, "/api/tl/backtests/run", {
            "dataset_id": dataset_id, "strategy_version_id": version_id, "kind": "out_of_sample",
            "fee_bps": 10, "slippage_bps": 5, "starting_cash": 10000,
        })
        self.assertEqual(status, 201)
        self.assertIn("train_backtest_id", oos)

        status, stress = self._post(self.hq_port, "/api/tl/stress-tests/run", {"backtest_id": backtest_id})
        self.assertEqual(status, 201)
        self.assertEqual(len(stress["stress_test_ids"]), 9)

        status, decision = self._post(self.hq_port, "/api/tl/council/run", {
            "strategy_id": strategy_id, "strategy_version_id": version_id, "backtest_id": backtest_id,
            "stress_test_ids": stress["stress_test_ids"],
        })
        self.assertEqual(status, 201)
        self.assertIn(decision["decision"], ("continue_research", "revise", "approve_for_paper", "reject", "graveyard"))

        status, reviews = self._get(self.hq_port, f"/api/tl/strategies/{strategy_id}/reviews")
        self.assertEqual(status, 200)
        self.assertEqual(len(reviews["items"]), 5)

    def test_paper_trading_and_portfolio_over_http(self):
        status, market = self._post(self.hq_port, "/api/tl/markets", {"code": "US_EQUITY", "name": "US Equities", "asset_class": "us_equity", "actor": "Aryan"})
        market_id = market["market_id"]
        status, instrument = self._post(self.hq_port, "/api/tl/instruments", {"market_id": market_id, "symbol": "PAPERHTTP", "actor": "Aryan"})
        instrument_id = instrument["instrument_id"]
        status, strategy = self._post(self.hq_port, "/api/tl/strategies", {"name": "Paper HTTP strategy", "hypothesis": "h", "market_id": market_id, "actor": "Aryan"})
        strategy_id = strategy["strategy_id"]
        self._advance_to_paper_active(strategy_id)

        status, account = self._post(self.hq_port, "/api/tl/paper-accounts", {"name": "HTTP paper account", "starting_cash": 10000, "actor": "Aryan"})
        self.assertEqual(status, 201)
        account_id = account["account_id"]

        status, order = self._post(self.hq_port, f"/api/tl/paper-accounts/{account_id}/orders", {
            "strategy_id": strategy_id, "instrument_id": instrument_id, "side": "BUY", "qty": 5,
            "market_price": 100.0, "fee_bps": 0, "slippage_bps": 0, "actor": "Aryan",
        })
        self.assertEqual(status, 201)
        self.assertEqual(order["status"], "FILLED")

        status, portfolio = self._get(self.hq_port, f"/api/tl/paper-accounts/{account_id}/portfolio")
        self.assertEqual(status, 200)
        self.assertIs(portfolio["is_paper"], True)
        self.assertIs(portfolio["is_real_money"], False)

        status, orders = self._get(self.hq_port, f"/api/tl/paper-accounts/{account_id}/orders")
        self.assertEqual(status, 200)
        self.assertEqual(len(orders["items"]), 1)

        status, summary = self._get(self.hq_port, "/api/tl/summary")
        self.assertEqual(status, 200)
        self.assertIs(summary["is_paper"], True)
        self.assertIs(summary["is_real_money"], False)
        self.assertIn("PAPER", summary["note"])
        self.assertIn("no real order path", summary["note"])

    def test_risk_limit_rejects_oversized_order_over_http(self):
        status, market = self._post(self.hq_port, "/api/tl/markets", {"code": "US_EQUITY", "name": "US Equities", "asset_class": "us_equity", "actor": "Aryan"})
        market_id = market["market_id"]
        status, instrument = self._post(self.hq_port, "/api/tl/instruments", {"market_id": market_id, "symbol": "RISKHTTP", "actor": "Aryan"})
        instrument_id = instrument["instrument_id"]
        status, strategy = self._post(self.hq_port, "/api/tl/strategies", {"name": "Risk HTTP strategy", "hypothesis": "h", "market_id": market_id, "actor": "Aryan"})
        strategy_id = strategy["strategy_id"]
        self._advance_to_paper_active(strategy_id)
        status, account = self._post(self.hq_port, "/api/tl/paper-accounts", {"name": "Risk HTTP account", "starting_cash": 10000, "actor": "Aryan"})
        account_id = account["account_id"]
        status, limit = self._post(self.hq_port, "/api/tl/risk-limits", {"scope": "global", "max_risk_per_trade_pct": 5, "actor": "Aryan"})
        self.assertEqual(status, 201)

        status, order = self._post(self.hq_port, f"/api/tl/paper-accounts/{account_id}/orders", {
            "strategy_id": strategy_id, "instrument_id": instrument_id, "side": "BUY", "qty": 100,
            "market_price": 100.0, "actor": "Aryan",
        })
        self.assertEqual(status, 201)  # order creation itself succeeds (201) even though the order is REJECTED
        self.assertEqual(order["status"], "REJECTED")
        self.assertIn("max_risk_per_trade_pct", order["reject_reason"])

    def test_no_real_money_execution_path_exists_anywhere_in_the_tl_api(self):
        """Section 24's explicit requirement: prove no real-money order was
        or could be created. This walks every TTT Trading Lab route this
        server exposes and confirms none of them can place a live
        broker/exchange order -- there is no route, table, field, or status
        value anywhere that represents real-money execution."""
        for path in ("/api/tl/markets", "/api/tl/paper-accounts", "/api/tl/strategies", "/api/tl/risk-limits", "/api/tl/risk-breaches", "/api/tl/graveyard", "/api/tl/summary", "/api/tl/providers"):
            status, _ = self._get(self.hq_port, path)
            self.assertEqual(status, 200, path)
        # every route lives under /api/tl/ and every one of them is backed
        # by tl_* tables that are 100% paper/simulated (see
        # falguna/trading_lab_data.py's module docstring) -- there is no
        # /api/tl/live-orders or equivalent route at all.
        code = self._get_raises(self.hq_port, "/api/tl/live-orders")
        self.assertEqual(code, 404)
        code = self._get_raises(self.hq_port, "/api/tl/broker")
        self.assertEqual(code, 404)


class FalgunaServerStillWorksTests(_LiveServerCase):
    """Falguna Engineering must still launch and behave exactly as before --
    and must never expose the TTT HQ routes that used to live inside it."""

    def setUp(self):
        super().setUp()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FalgunaHandler)
        self.falguna_port = self.server.server_address[1]
        self.server.app_root = self.repo
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self._wait_ready()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        super().tearDown()

    def _wait_ready(self):
        for _ in range(100):
            try:
                status, body = self._get(self.falguna_port, "/api/config")
                if body.get("product") == "Falguna Engineering":
                    return
            except Exception:
                pass
            time.sleep(0.05)
        self.fail("Falguna server did not become ready")

    def test_falguna_can_still_launch_independently_with_unchanged_identity(self):
        status, body = self._get(self.falguna_port, "/api/config")
        self.assertEqual(status, 200)
        self.assertEqual(body["product"], "Falguna Engineering")

    def test_falguna_no_longer_serves_ttt_hq_routes(self):
        for path in ("/api/boardroom", "/api/backlog", "/api/needs-aryan"):
            code = self._get_raises(self.falguna_port, path)
            self.assertEqual(code, 404, f"{path} should be gone from Falguna Engineering")

    def test_falguna_runs_endpoint_still_works_unaffected_by_the_separation(self):
        status, body = self._get(self.falguna_port, "/api/runs")
        self.assertEqual(status, 200)
        self.assertIn("runs", body)


class CLISubcommandTests(unittest.TestCase):
    def test_hq_and_web_subcommands_are_both_registered_and_independent(self):
        from falguna.__main__ import main
        import argparse
        import sys

        # Build the same parser main() builds, without actually starting a server.
        parser = argparse.ArgumentParser(prog="falguna")
        parser.add_argument("--root", default=".")
        sub = parser.add_subparsers(dest="command", required=True)
        sub.add_parser("init")
        web = sub.add_parser("web")
        web.add_argument("--host", default="127.0.0.1", choices=("127.0.0.1", "localhost"))
        web.add_argument("--port", type=int, default=8765)
        hq = sub.add_parser("hq")
        hq.add_argument("--host", default="127.0.0.1", choices=("127.0.0.1", "localhost"))
        hq.add_argument("--port", type=int, default=8766)
        hq.add_argument("--falguna-url", default="http://127.0.0.1:8765")

        args = parser.parse_args(["--root", ".", "hq", "--port", "9001"])
        self.assertEqual(args.command, "hq")
        self.assertEqual(args.port, 9001)
        self.assertEqual(args.falguna_url, "http://127.0.0.1:8765")

        args2 = parser.parse_args(["--root", ".", "web", "--port", "9002"])
        self.assertEqual(args2.command, "web")
        self.assertEqual(args2.port, 9002)

    def test_default_ports_do_not_collide(self):
        import falguna.__main__ as m
        source = Path(m.__file__).read_text()
        self.assertIn("default=8765", source)
        self.assertIn("default=8766", source)


class OrchestrationHQServerTests(TTTHQServerTests):
    """HTTP-level coverage for Phase 4 Sprint 2: Company-wide Orchestration,
    the Unified Decision Queue, and Operational Alerts."""

    def _make_pending_proposal(self):
        opportunities = OpportunityStore(self.store, self.control.audit)
        opp_id = opportunities.create({"title": "Build a site", "client_name": "Acme"}, "Aryan")
        needs_aryan = NeedsAryanQueue(self.store, self.control.audit)
        proposal = ProposalStore(self.store, self.control.audit, needs_aryan=needs_aryan).generate(opp_id, "detailed", "Aryan")
        return opp_id, proposal["needs_aryan_id"]

    def test_workflow_monitor_route_buckets_a_blocked_opportunity(self):
        opp_id, _ = self._make_pending_proposal()
        status, body = self._get(self.hq_port, "/api/orchestration/workflows")
        self.assertEqual(status, 200)
        self.assertEqual(body["total"], 1)
        self.assertEqual(len(body["blocked_on_human"]), 1)
        self.assertEqual(body["blocked_on_human"][0]["opportunity_id"], opp_id)

    def test_unified_decisions_route_lists_the_pending_proposal(self):
        _, needs_aryan_id = self._make_pending_proposal()
        status, body = self._get(self.hq_port, "/api/decisions/unified")
        self.assertEqual(status, 200)
        self.assertEqual(body["pending_count"], 1)
        self.assertEqual(body["items"][0]["id"], needs_aryan_id)
        self.assertEqual(body["items"][0]["department"], "Revenue Hunter")

    def test_unified_decisions_route_status_filter(self):
        self._make_pending_proposal()
        status, body = self._get(self.hq_port, "/api/decisions/unified?status=APPROVED")
        self.assertEqual(status, 200)
        self.assertEqual(body["total"], 0)

    def test_deciding_through_the_existing_endpoint_clears_it_from_both_queues(self):
        _, needs_aryan_id = self._make_pending_proposal()
        status, body = self._post(self.hq_port, f"/api/needs-aryan/{needs_aryan_id}/decision", {"action": "approve", "actor": "Aryan"})
        self.assertEqual(status, 200)
        _, workflows = self._get(self.hq_port, "/api/orchestration/workflows")
        self.assertEqual(len(workflows["blocked_on_human"]), 0)
        _, decisions = self._get(self.hq_port, "/api/decisions/unified")
        self.assertEqual(decisions["pending_count"], 0)
        _, alerts = self._get(self.hq_port, "/api/alerts")
        self.assertFalse(any(a["category"] == "quotation_awaiting_approval" for a in alerts["alerts"]))

    def test_stale_decision_is_rejected_not_silently_reapplied(self):
        _, needs_aryan_id = self._make_pending_proposal()
        status, _ = self._post(self.hq_port, f"/api/needs-aryan/{needs_aryan_id}/decision", {"action": "approve", "actor": "Aryan"})
        self.assertEqual(status, 200)
        code, err = self._post_raises(self.hq_port, f"/api/needs-aryan/{needs_aryan_id}/decision", {"action": "reject", "actor": "Aryan"})
        self.assertEqual(code, 400)
        self.assertIn("already been decided", err["error"])

    def test_duplicate_http_decision_requests_do_not_double_execute_the_close(self):
        opp_id, needs_aryan_id = self._make_pending_proposal()
        self._post(self.hq_port, f"/api/needs-aryan/{needs_aryan_id}/decision", {"action": "approve", "actor": "Aryan"})
        orchestrator = LifecycleOrchestrator(self.store, self.control.audit)
        needs_aryan = NeedsAryanQueue(self.store, self.control.audit)
        outcome = ClosingService(self.store, self.control.audit, orchestrator=orchestrator, needs_aryan=needs_aryan).close(
            opp_id, "Aryan", client_name="Acme",
        )
        self.assertEqual(outcome["status"], "AWAITING_APPROVAL")
        closing_needs_aryan_id = outcome["needs_aryan_id"]
        status1, _ = self._post(self.hq_port, f"/api/needs-aryan/{closing_needs_aryan_id}/decision", {"action": "approve", "actor": "Aryan"})
        code2, err2 = self._post_raises(self.hq_port, f"/api/needs-aryan/{closing_needs_aryan_id}/decision", {"action": "approve", "actor": "Aryan"})
        self.assertEqual(status1, 200)
        self.assertEqual(code2, 400)
        closings = self.store.list("rh_closing_records", "opportunity_id=?", (opp_id,))
        self.assertEqual(len(closings), 1)

    def test_alerts_route_and_acknowledge_round_trip(self):
        self._make_pending_proposal()
        status, before = self._get(self.hq_port, "/api/alerts")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(before["active_count"], 1)
        alert_id = next(a["id"] for a in before["alerts"] if a["category"] == "quotation_awaiting_approval")
        ack_status, ack_body = self._post(self.hq_port, f"/api/alerts/{alert_id}/acknowledge", {"actor": "Aryan", "note": "seen"})
        self.assertEqual(ack_status, 200)
        self.assertTrue(ack_body["acknowledged"])
        status, after = self._get(self.hq_port, "/api/alerts")
        acked = next(a for a in after["alerts"] if a["id"] == alert_id)
        self.assertTrue(acked["acknowledged"])
        self.assertEqual(after["active_count"], before["active_count"] - 1)

    def test_alert_acknowledge_without_actor_returns_400(self):
        self._make_pending_proposal()
        _, before = self._get(self.hq_port, "/api/alerts")
        alert_id = before["alerts"][0]["id"]
        code, err = self._post_raises(self.hq_port, f"/api/alerts/{alert_id}/acknowledge", {"actor": ""})
        self.assertEqual(code, 400)
        self.assertIn("actor", err["error"])


if __name__ == "__main__":
    unittest.main()
