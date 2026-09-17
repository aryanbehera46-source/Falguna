"""Tests for the Application Executor (falguna/application_executor.py)."""

import json
import tempfile
import unittest
from pathlib import Path

from falguna.application_executor import (
    ApplicationExecutor,
    ApplicationExecutorError,
    ApplicationResult,
    ManualReviewChannel,
    SimulatedTestChannel,
    UnsupportedChannel,
)
from falguna.audit import AuditLog
from falguna.lifecycle import LifecycleOrchestrator
from falguna.revenue_hunter import OpportunityStore, ProposalStore, QualificationStore
from falguna.store import StateStore
from falguna.ttt_hq import NeedsAryanQueue


class ApplicationExecutorTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = StateStore(root / "state.db")
        self.store.migrate()
        self.audit = AuditLog(root / "audit.jsonl")
        self.opportunities = OpportunityStore(self.store, self.audit)
        self.needs_aryan = NeedsAryanQueue(self.store, self.audit)
        self.qualifications = QualificationStore(self.store, self.audit)
        self.proposals = ProposalStore(self.store, self.audit, needs_aryan=self.needs_aryan)
        self.orchestrator = LifecycleOrchestrator(self.store, self.audit)
        self.executor = ApplicationExecutor(self.store, self.audit, needs_aryan=self.needs_aryan, orchestrator=self.orchestrator)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _approved_proposal(self, source_url="https://example.com/jobs/123"):
        opp_id = self.opportunities.create({"title": "Build a dashboard", "description": "d", "source_url": source_url}, actor="Aryan")
        self.qualifications.qualify(opp_id, actor="Aryan")
        drafted = self.proposals.generate(opp_id, "short", actor="Aryan")
        self.proposals.mark_approved(drafted["proposal_id"], actor="Aryan")
        self.orchestrator.transition(opp_id, "RESEARCHING", actor="Aryan")
        self.orchestrator.transition(opp_id, "QUALIFIED", actor="Aryan")
        self.orchestrator.transition(opp_id, "PITCH_READY", actor="Aryan")
        self.orchestrator.transition(opp_id, "AWAITING_APPROVAL", actor="Aryan")
        self.orchestrator.transition(opp_id, "APPROVED", actor="Aryan")
        return opp_id, drafted["proposal_id"]


class ApplicationResultTests(unittest.TestCase):
    def test_blocked_requires_a_known_reason(self):
        with self.assertRaises(ApplicationExecutorError):
            ApplicationResult("BLOCKED", blocked_reason="not_a_real_reason")

    def test_unknown_status_rejected(self):
        with self.assertRaises(ApplicationExecutorError):
            ApplicationResult("MAYBE_SENT")

    def test_sent_needs_no_blocked_reason(self):
        result = ApplicationResult("SENT", evidence={"ok": True})
        self.assertIsNone(result.blocked_reason)


class ManualReviewChannelTests(unittest.TestCase):
    def test_no_source_url_blocks_with_no_channel_available(self):
        result = ManualReviewChannel().attempt({"id": "x", "source_url": None}, {"id": "p"})
        self.assertEqual(result.status, "BLOCKED")
        self.assertEqual(result.blocked_reason, "no_application_channel_available")

    def test_with_source_url_still_blocks_pending_manual_review(self):
        # This is the honest default: no live adapter exists yet, so even a
        # present URL cannot be safely auto-submitted.
        result = ManualReviewChannel().attempt({"id": "x", "source_url": "https://example.com/job"}, {"id": "p"})
        self.assertEqual(result.status, "BLOCKED")
        self.assertEqual(result.blocked_reason, "ambiguous_consent_required")
        self.assertIn("source_url", result.evidence)


class UnsupportedChannelTests(unittest.TestCase):
    def test_always_reports_unsupported(self):
        result = UnsupportedChannel("some_new_platform").attempt({}, {})
        self.assertEqual(result.status, "UNSUPPORTED")
        self.assertEqual(result.evidence["requested_channel"], "some_new_platform")


class ExecutorValidationTests(ApplicationExecutorTestBase):
    def test_unknown_opportunity_raises(self):
        with self.assertRaises(ApplicationExecutorError):
            self.executor.apply("does-not-exist", "also-fake", actor="Aryan")

    def test_unknown_proposal_raises(self):
        opp_id = self.opportunities.create({"title": "T"}, actor="Aryan")
        with self.assertRaises(ApplicationExecutorError):
            self.executor.apply(opp_id, "does-not-exist", actor="Aryan")

    def test_non_approved_proposal_is_refused(self):
        opp_id = self.opportunities.create({"title": "T"}, actor="Aryan")
        self.qualifications.qualify(opp_id, actor="Aryan")
        drafted = self.proposals.generate(opp_id, "short", actor="Aryan")
        with self.assertRaises(ApplicationExecutorError):
            self.executor.apply(opp_id, drafted["proposal_id"], actor="Aryan")

    def test_proposal_for_a_different_opportunity_is_refused(self):
        opp_id, proposal_id = self._approved_proposal()
        other_opp_id = self.opportunities.create({"title": "Other"}, actor="Aryan")
        with self.assertRaises(ApplicationExecutorError):
            self.executor.apply(other_opp_id, proposal_id, actor="Aryan")


class ExecutorManualReviewPathTests(ApplicationExecutorTestBase):
    def test_default_channel_blocks_and_raises_needs_aryan(self):
        opp_id, proposal_id = self._approved_proposal()
        before = len(self.store.list("needs_aryan_items"))
        result = self.executor.apply(opp_id, proposal_id, actor="Aryan")
        self.assertEqual(result["status"], "BLOCKED")
        after = len(self.store.list("needs_aryan_items"))
        self.assertEqual(after, before + 1)

    def test_blocked_attempt_is_recorded_with_evidence(self):
        opp_id, proposal_id = self._approved_proposal()
        result = self.executor.apply(opp_id, proposal_id, actor="Aryan")
        attempt = self.store.get("rh_application_attempts", result["attempt_id"])
        self.assertEqual(attempt["status"], "BLOCKED")
        self.assertIsNotNone(attempt["blocked_reason"])
        evidence = json.loads(attempt["evidence_json"])
        self.assertIn("source_url", evidence)

    def test_blocked_attempt_never_advances_lifecycle_past_approved(self):
        opp_id, proposal_id = self._approved_proposal()
        self.executor.apply(opp_id, proposal_id, actor="Aryan")
        self.assertEqual(self.orchestrator.current_state(opp_id), "APPROVED")

    def test_blocked_attempt_never_marks_opportunity_applied_sent_stage(self):
        opp_id, proposal_id = self._approved_proposal()
        self.executor.apply(opp_id, proposal_id, actor="Aryan")
        opp = self.store.get("rh_opportunities", opp_id)
        self.assertNotEqual(opp["stage"], "Applied/Sent")

    def test_no_source_url_still_creates_an_auditable_attempt_and_needs_aryan_item(self):
        opp_id, proposal_id = self._approved_proposal(source_url=None)
        result = self.executor.apply(opp_id, proposal_id, actor="Aryan")
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["blocked_reason"], "no_application_channel_available")


class ExecutorUnsupportedChannelTests(ApplicationExecutorTestBase):
    def test_unknown_channel_name_is_unsupported_not_silently_ignored(self):
        opp_id, proposal_id = self._approved_proposal()
        result = self.executor.apply(opp_id, proposal_id, actor="Aryan", channel="some_platform_with_no_adapter")
        self.assertEqual(result["status"], "UNSUPPORTED")
        attempt = self.store.get("rh_application_attempts", result["attempt_id"])
        self.assertEqual(attempt["status"], "UNSUPPORTED")

    def test_unsupported_channel_also_raises_needs_aryan(self):
        opp_id, proposal_id = self._approved_proposal()
        before = len(self.store.list("needs_aryan_items"))
        self.executor.apply(opp_id, proposal_id, actor="Aryan", channel="some_platform_with_no_adapter")
        after = len(self.store.list("needs_aryan_items"))
        self.assertEqual(after, before + 1)


class ExecutorSimulatedChannelTests(ApplicationExecutorTestBase):
    """The only path in this codebase where an application attempt can
    report SENT -- explicitly opt-in, for tests/QA only."""

    def test_simulated_channel_requires_explicit_opt_in(self):
        opp_id, proposal_id = self._approved_proposal()
        # Without allow_simulated=True, "simulated_test" is just an unknown
        # channel name -- it must never be reachable by accident.
        result = self.executor.apply(opp_id, proposal_id, actor="Aryan", channel="simulated_test")
        self.assertEqual(result["status"], "UNSUPPORTED")

    def test_simulated_channel_with_opt_in_reports_sent(self):
        opp_id, proposal_id = self._approved_proposal()
        result = self.executor.apply(opp_id, proposal_id, actor="Aryan", channel="simulated_test", allow_simulated=True)
        self.assertEqual(result["status"], "SENT")
        self.assertTrue(result["evidence"]["simulated"])

    def test_simulated_sent_advances_lifecycle_to_contacted(self):
        opp_id, proposal_id = self._approved_proposal()
        self.executor.apply(opp_id, proposal_id, actor="Aryan", channel="simulated_test", allow_simulated=True)
        self.assertEqual(self.orchestrator.current_state(opp_id), "CONTACTED")

    def test_simulated_sent_does_not_raise_needs_aryan(self):
        opp_id, proposal_id = self._approved_proposal()
        before = len(self.store.list("needs_aryan_items"))
        self.executor.apply(opp_id, proposal_id, actor="Aryan", channel="simulated_test", allow_simulated=True)
        after = len(self.store.list("needs_aryan_items"))
        self.assertEqual(after, before)

    def test_direct_simulated_channel_instance_never_touches_anything_external(self):
        # Sanity check on the adapter itself: it only ever reads its inputs
        # and returns a result -- no network/file side effects to assert
        # against, which is the point.
        result = SimulatedTestChannel().attempt({"id": "o1"}, {"id": "p1"})
        self.assertEqual(result.status, "SENT")
        self.assertTrue(result.evidence["simulated"])


if __name__ == "__main__":
    unittest.main()
