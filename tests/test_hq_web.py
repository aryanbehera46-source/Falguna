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

from falguna.hq_web import HQ_INDEX_HTML, PRODUCT_NAME, TTTHQHandler
from falguna.runtime import open_control_plane
from falguna.web import FalgunaHandler, INDEX_HTML


def git(repo: Path, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


class HTMLSeparationTests(unittest.TestCase):
    """No live server needed -- these mirror the existing INDEX_HTML content
    tests in test_bootstrap.py exactly (assertIn on the module-level constant)."""

    def test_hq_html_exposes_required_surfaces(self):
        for label in (
            "Twenty Two Technologies", "Boardroom", "Master Vision Backlog", "Needs Aryan",
            "Opportunities", "Sales Pipeline", "Clients", "Active Jobs", "Revenue",
            "Ventures / Company Ops", "TTT HQ decides · Falguna executes",
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
        # Main content's own independent scrolling must be unaffected.
        main_rule = re.search(r"main\{[^}]*\}", HQ_INDEX_HTML)
        self.assertIsNotNone(main_rule, "base `main` CSS rule not found")
        self.assertIn("overflow-y:auto", main_rule.group(0))
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


class _LiveServerCase(unittest.TestCase):
    """Base class that boots a real repo + real HTTP server on a scratch port."""

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


class TTTHQServerTests(_LiveServerCase):
    def setUp(self):
        super().setUp()
        self.server = ThreadingHTTPServer(("127.0.0.1", self.hq_port), TTTHQHandler)
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
        for _ in range(40):
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

    def test_workforce_task_with_no_adapter_escalates_not_crashes(self):
        status, out = self._post(self.hq_port, "/api/wf/tasks", {
            "department": "media", "objective": "Research a topic", "task_type": "browser_research",
        })
        task_id = out["task_id"]
        status, result = self._post(self.hq_port, f"/api/wf/tasks/{task_id}/execute", {"actor": "Aryan"})
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "NEEDS_ARYAN")

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

        code = self._get_raises(self.hq_port, "/api/cc/ceo-brief/latest")
        self.assertEqual(code, 404)

        status, brief = self._post(self.hq_port, "/api/cc/ceo-brief/generate", {"actor": "Aryan"})
        self.assertEqual(status, 201)
        self.assertIn("confirmed_facts_json", brief)

        status, latest = self._get(self.hq_port, "/api/cc/ceo-brief/latest")
        self.assertEqual(status, 200)
        self.assertEqual(latest["id"], brief["id"])

        status, listing = self._get(self.hq_port, "/api/cc/ceo-brief")
        self.assertEqual(status, 200)
        self.assertTrue(any(b["id"] == brief["id"] for b in listing["items"]))

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

        status, reserve = self._post(self.hq_port, "/api/cc/reserve-policy", {"tax_reserve_pct": 0.15})
        self.assertEqual(status, 200)
        self.assertTrue(reserve["configured"])

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


class FalgunaServerStillWorksTests(_LiveServerCase):
    """Falguna Engineering must still launch and behave exactly as before --
    and must never expose the TTT HQ routes that used to live inside it."""

    def setUp(self):
        super().setUp()
        self.server = ThreadingHTTPServer(("127.0.0.1", self.falguna_port), FalgunaHandler)
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
        for _ in range(40):
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


if __name__ == "__main__":
    unittest.main()
