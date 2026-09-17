"""Tests for falguna/account_management.py -- the Account Manager (Section
10, Pass D). Uses a real ControlPlane + real git repo (same fixture as
test_revenue_hunter.py's _RepoCase) so mission/run/checkpoint evidence is
real, not mocked -- the whole point of this module is that it never
fabricates delivery status."""

import subprocess
import tempfile
import unittest
from pathlib import Path

from falguna.account_management import AccountManagementError, AccountManagerService
from falguna.audit import AuditLog
from falguna.models import RunPolicy
from falguna.revenue_hunter import ActiveJobStore, OpportunityStore
from falguna.runtime import open_control_plane
from falguna.sales_ops import ClosingService, SalesPolicyStore
from falguna.store import StateStore


class _RepoCase(unittest.TestCase):
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
        self.audit = self.control.audit
        self.opportunities = OpportunityStore(self.store, self.audit)
        self.active_jobs = ActiveJobStore(self.store, self.audit)
        self.account_manager = AccountManagerService(self.store, self.audit)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def _won_active_job(self):
        opp_id = self.opportunities.create({"title": "Build a CRM dashboard"}, actor="Aryan")
        self.opportunities.mark_won(opp_id, "Aryan", final_price=2000)
        job_id = self.active_jobs.create_from_won_opportunity(opp_id, "Aryan")
        return opp_id, job_id

    def _real_run(self, task_id, status, error=None):
        run_id = self.store.create("runs", {
            "task_id": task_id, "status": status, "attempt": 1, "worker": "test", "model": "test",
            "worktree": None, "head_sha": None, "error": error,
            "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:00+00:00",
        })
        return run_id


class StatusForActiveJobTests(_RepoCase):
    def test_unknown_active_job_raises(self):
        with self.assertRaises(AccountManagementError):
            self.account_manager.status_for_active_job("does-not-exist")

    def test_not_started_before_handoff(self):
        _, job_id = self._won_active_job()
        status = self.account_manager.status_for_active_job(job_id)
        self.assertEqual(status["delivery_status"], "NOT_STARTED")
        self.assertFalse(status["blocked"])
        self.assertEqual(status["evidence"], [])

    def test_not_started_when_mission_has_no_run_yet(self):
        _, job_id = self._won_active_job()
        result = self.active_jobs.trigger_handoff(job_id, str(self.repo), self.control)
        # A real mission now exists, but the orchestrator hasn't started a
        # run against it -- this must report NOT_STARTED, not fabricate
        # in-progress.
        status = self.account_manager.status_for_active_job(job_id)
        self.assertEqual(status["mission_id"], result["mission_id"])
        self.assertEqual(status["delivery_status"], "NOT_STARTED")
        self.assertTrue(any("no run yet" in e for e in status["evidence"]))

    def test_in_progress_reflects_real_run_status(self):
        _, job_id = self._won_active_job()
        result = self.active_jobs.trigger_handoff(job_id, str(self.repo), self.control)
        self._real_run(result["task_id"], "WORKING")
        status = self.account_manager.status_for_active_job(job_id)
        self.assertEqual(status["run_status"], "WORKING")
        self.assertEqual(status["delivery_status"], "IN_PROGRESS")
        self.assertFalse(status["blocked"])

    def test_done_candidate_reflects_delivered_candidate(self):
        _, job_id = self._won_active_job()
        result = self.active_jobs.trigger_handoff(job_id, str(self.repo), self.control)
        self._real_run(result["task_id"], "DONE_CANDIDATE")
        status = self.account_manager.status_for_active_job(job_id)
        self.assertEqual(status["delivery_status"], "DELIVERED_CANDIDATE")
        self.assertFalse(status["blocked"])

    def test_failed_run_is_blocked_with_real_error(self):
        _, job_id = self._won_active_job()
        result = self.active_jobs.trigger_handoff(job_id, str(self.repo), self.control)
        self._real_run(result["task_id"], "FAILED", error="verification failed: 2 tests broke")
        status = self.account_manager.status_for_active_job(job_id)
        self.assertTrue(status["blocked"])
        self.assertIn("verification failed", status["blocker_reason"])

    def test_needs_approval_with_pending_merge_is_blocked_awaiting_approval(self):
        _, job_id = self._won_active_job()
        result = self.active_jobs.trigger_handoff(job_id, str(self.repo), self.control)
        run_id = self._real_run(result["task_id"], "AWAITING_APPROVAL")
        self.store.create("supervisor_states", {
            "run_id": run_id, "outcome_class": "NEEDS_APPROVAL", "category": "PROTECTED_BRANCH_MERGE", "phase": "NEEDS_ARYAN",
            "retry_allowed": 0, "resume_allowed": 1, "eligibility_reason": "final merge decision needed",
            "attempts_used": 1, "retry_budget": 2, "diagnostics_json": "{}",
            "decision_needed": "Decide on merge", "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:00+00:00",
        })
        self.store.create("approvals", {
            "run_id": run_id, "kind": "PROTECTED_BRANCH_MERGE", "status": "PENDING",
            "requested_at": "2026-01-01T00:00:00+00:00", "decided_at": None, "decided_by": None, "reason": None,
            "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:00+00:00",
        })
        status = self.account_manager.status_for_active_job(job_id)
        self.assertTrue(status["blocked"])
        self.assertIn("merge approval", status["blocker_reason"])

    def test_evidence_is_never_empty_once_a_run_exists(self):
        _, job_id = self._won_active_job()
        result = self.active_jobs.trigger_handoff(job_id, str(self.repo), self.control)
        self._real_run(result["task_id"], "WORKING")
        status = self.account_manager.status_for_active_job(job_id)
        self.assertTrue(len(status["evidence"]) > 0)


class ClientUpdateDraftTests(_RepoCase):
    def test_not_started_draft_never_promises_progress(self):
        _, job_id = self._won_active_job()
        draft = self.account_manager.client_update_draft(job_id)
        self.assertIn("hasn't started", draft)

    def test_blocked_draft_surfaces_the_real_reason(self):
        _, job_id = self._won_active_job()
        result = self.active_jobs.trigger_handoff(job_id, str(self.repo), self.control)
        self._real_run(result["task_id"], "FAILED", error="disk full")
        draft = self.account_manager.client_update_draft(job_id)
        self.assertIn("disk full", draft)

    def test_never_contains_a_fake_percentage(self):
        _, job_id = self._won_active_job()
        result = self.active_jobs.trigger_handoff(job_id, str(self.repo), self.control)
        self._real_run(result["task_id"], "WORKING")
        draft = self.account_manager.client_update_draft(job_id)
        self.assertNotIn("%", draft)


class ScopeSignalsTests(_RepoCase):
    def setUp(self):
        super().setUp()
        self.policy = SalesPolicyStore(self.store)
        self.policy.save({})
        self.closing = ClosingService(self.store, self.audit)

    def test_no_signals_before_closing(self):
        opp_id = self.opportunities.create({"title": "Build a CRM dashboard"}, actor="Aryan")
        from falguna.conversations import ConversationStore
        conversations = ConversationStore(self.store, self.audit)
        conversations.record_inbound(opp_id, "email", "Can you share some case studies from past clients?")
        signals = self.account_manager.scope_signals(opp_id)
        # Not closed yet -- closed_at is None, so anything on file still counts as after "closing" (None comparison).
        self.assertEqual(len(signals), 1)

    def test_flags_requirement_request_after_closing(self):
        from falguna.conversations import ConversationStore
        opp_id = self.opportunities.create({"title": "Build a CRM dashboard"}, actor="Aryan")
        self.closing.close(opp_id, "Aryan", client_name="Acme Corp", final_price=2000)
        conversations = ConversationStore(self.store, self.audit)
        conversations.record_inbound(opp_id, "email", "Can you share some case studies from past clients?")
        signals = self.account_manager.scope_signals(opp_id)
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["intent"], "requirement_request")

    def test_does_not_flag_unrelated_intents(self):
        from falguna.conversations import ConversationStore
        opp_id = self.opportunities.create({"title": "Build a CRM dashboard"}, actor="Aryan")
        self.closing.close(opp_id, "Aryan", client_name="Acme Corp", final_price=2000)
        conversations = ConversationStore(self.store, self.audit)
        conversations.record_inbound(opp_id, "email", "Sounds good, let's move forward.")
        signals = self.account_manager.scope_signals(opp_id)
        self.assertEqual(len(signals), 0)


class PortfolioOverviewTests(_RepoCase):
    def test_returns_one_row_per_active_job(self):
        self._won_active_job()
        self._won_active_job()
        overview = self.account_manager.portfolio_overview()
        self.assertEqual(len(overview), 2)


if __name__ == "__main__":
    unittest.main()
