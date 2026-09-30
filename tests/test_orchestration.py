import tempfile
import unittest
from pathlib import Path

from falguna.audit import AuditLog
from falguna.lifecycle import LifecycleOrchestrator
from falguna.onboarding import ONBOARDING_ITEM_TYPES, OnboardingStore
from falguna.orchestration import OrchestrationError, WorkflowOrchestrator, workflow_monitor_snapshot
from falguna.revenue_hunter import OpportunityStore, ProposalStore
from falguna.sales_ops import ClosingService
from falguna.store import StateStore
from falguna.ttt_hq import NeedsAryanQueue


class WorkflowOrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.tmp.name) / "state.db")
        self.store.migrate()
        self.audit = AuditLog(Path(self.tmp.name) / "audit.jsonl")
        self.orchestrator = WorkflowOrchestrator(self.store, self.audit)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_unknown_opportunity_raises(self):
        with self.assertRaises(OrchestrationError):
            self.orchestrator.sync_opportunity("does-not-exist")

    def test_fresh_opportunity_is_blocked_before_first_proposal(self):
        opp_id = OpportunityStore(self.store, self.audit).create({"title": "Build a site", "client_name": "Acme"}, "Aryan")
        result = self.orchestrator.sync_opportunity(opp_id)
        self.assertFalse(result["done"])
        self.assertFalse(result["blocked_needs_human"])
        self.assertEqual(result["current_step"], "proposal")

    def test_pending_proposal_is_surfaced_as_a_decision(self):
        opp_id = OpportunityStore(self.store, self.audit).create({"title": "Build a site", "client_name": "Acme"}, "Aryan")
        needs_aryan = NeedsAryanQueue(self.store, self.audit)
        proposal = ProposalStore(self.store, self.audit, needs_aryan=needs_aryan).generate(opp_id, "detailed", "Aryan")
        result = self.orchestrator.sync_opportunity(opp_id)
        self.assertTrue(result["blocked_needs_human"])
        self.assertEqual(result["decision"]["id"], proposal["needs_aryan_id"])
        self.assertEqual(result["decision"]["kind"], "proposal_approval")

    def test_approving_proposal_advances_the_workflow(self):
        opp_id = OpportunityStore(self.store, self.audit).create({"title": "Build a site", "client_name": "Acme"}, "Aryan")
        proposal = ProposalStore(self.store, self.audit).generate(opp_id, "detailed", "Aryan")
        ProposalStore(self.store, self.audit).mark_approved(proposal["proposal_id"], "Aryan")
        result = self.orchestrator.sync_opportunity(opp_id)
        self.assertEqual(result["current_step"], "project")
        # No closing package exists yet -- surfaced as a plain next action,
        # not a fabricated decision.
        self.assertFalse(result["blocked_needs_human"])
        self.assertIsNone(result["decision"])

    def test_pending_closing_package_is_surfaced_as_a_decision(self):
        opp_id = OpportunityStore(self.store, self.audit).create({"title": "Build a site", "client_name": "Acme"}, "Aryan")
        proposal = ProposalStore(self.store, self.audit).generate(opp_id, "detailed", "Aryan")
        ProposalStore(self.store, self.audit).mark_approved(proposal["proposal_id"], "Aryan")
        orch = LifecycleOrchestrator(self.store, self.audit)
        needs_aryan = NeedsAryanQueue(self.store, self.audit)
        closing = ClosingService(self.store, self.audit, orchestrator=orch, needs_aryan=needs_aryan)
        outcome = closing.close(opp_id, "Aryan", client_name="Acme")
        self.assertEqual(outcome["status"], "AWAITING_APPROVAL")
        result = self.orchestrator.sync_opportunity(opp_id)
        self.assertEqual(result["current_step"], "project")
        self.assertTrue(result["blocked_needs_human"])
        self.assertEqual(result["decision"]["id"], outcome["needs_aryan_id"])
        self.assertEqual(result["decision"]["kind"], "pricing_decision")

    def test_repeated_sync_is_idempotent_no_duplicate_events(self):
        opp_id = OpportunityStore(self.store, self.audit).create({"title": "Build a site", "client_name": "Acme"}, "Aryan")
        self.orchestrator.sync_opportunity(opp_id)
        self.orchestrator.sync_opportunity(opp_id)
        self.orchestrator.sync_opportunity(opp_id)
        history = self.orchestrator.history(opp_id)
        self.assertEqual(len(history), 1)

    def test_sync_records_new_event_only_when_state_actually_changes(self):
        opp_id = OpportunityStore(self.store, self.audit).create({"title": "Build a site", "client_name": "Acme"}, "Aryan")
        self.orchestrator.sync_opportunity(opp_id)
        proposal = ProposalStore(self.store, self.audit).generate(opp_id, "detailed", "Aryan")
        ProposalStore(self.store, self.audit).mark_approved(proposal["proposal_id"], "Aryan")
        self.orchestrator.sync_opportunity(opp_id)
        history = self.orchestrator.history(opp_id)
        self.assertEqual(len(history), 2)

    def test_recovery_after_interruption_replays_the_same_idempotency_key(self):
        opp_id = OpportunityStore(self.store, self.audit).create({"title": "Build a site", "client_name": "Acme"}, "Aryan")
        first = self.orchestrator.sync_opportunity(opp_id)
        # Simulate a fresh orchestrator instance after a restart -- state
        # is read from the database, not from in-memory orchestrator state.
        fresh = WorkflowOrchestrator(self.store, self.audit)
        second = fresh.sync_opportunity(opp_id)
        self.assertEqual(first["last_event_id"], second["last_event_id"])


class WorkflowMonitorSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.tmp.name) / "state.db")
        self.store.migrate()
        self.audit = AuditLog(Path(self.tmp.name) / "audit.jsonl")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_buckets_workflows_by_state(self):
        opportunities = OpportunityStore(self.store, self.audit)
        blocked_id = opportunities.create({"title": "Blocked deal"}, "Aryan")
        proposal = ProposalStore(self.store, self.audit).generate(blocked_id, "detailed", "Aryan")
        in_progress_id = opportunities.create({"title": "Fresh deal"}, "Aryan")
        snapshot = workflow_monitor_snapshot(self.store, self.audit)
        self.assertEqual(snapshot["total"], 2)
        blocked_ids = {w["opportunity_id"] for w in snapshot["blocked_on_human"]}
        in_progress_ids = {w["opportunity_id"] for w in snapshot["in_progress"]}
        self.assertIn(blocked_id, blocked_ids)
        self.assertIn(in_progress_id, in_progress_ids)
        self.assertEqual(snapshot["done"], [])


if __name__ == "__main__":
    unittest.main()
