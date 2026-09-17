"""Tests that the best-effort lifecycle hooks wired into OpportunityStore.
create(), QualificationStore.qualify(), ProposalStore.generate(), and
apply_decision_side_effect() actually advance the lifecycle when an
orchestrator is supplied, and that omitting the orchestrator (the default)
leaves every one of those methods' original, already-tested behavior
completely unchanged."""

import tempfile
import unittest
from pathlib import Path

from falguna.audit import AuditLog
from falguna.lifecycle import LifecycleOrchestrator
from falguna.opportunity_agent import build_qualification_engine
from falguna.revenue_hunter import (
    OpportunityStore,
    ProposalStore,
    QualificationStore,
    apply_decision_side_effect,
)
from falguna.store import StateStore
from falguna.ttt_hq import NeedsAryanQueue


class LifecycleHooksTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = StateStore(root / "state.db")
        self.store.migrate()
        self.audit = AuditLog(root / "audit.jsonl")
        self.orchestrator = LifecycleOrchestrator(self.store, self.audit)
        self.needs_aryan = NeedsAryanQueue(self.store, self.audit)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()


class OpportunityCreateHookTests(LifecycleHooksTestBase):
    def test_create_with_orchestrator_initializes_discovered(self):
        opps = OpportunityStore(self.store, self.audit, orchestrator=self.orchestrator)
        opp_id = opps.create({"title": "T"}, actor="Aryan")
        self.assertEqual(self.orchestrator.current_state(opp_id), "DISCOVERED")

    def test_create_without_orchestrator_leaves_lifecycle_state_null(self):
        opps = OpportunityStore(self.store, self.audit)
        opp_id = opps.create({"title": "T"}, actor="Aryan")
        opp = self.store.get("rh_opportunities", opp_id)
        self.assertIsNone(opp["lifecycle_state"])

    def test_create_without_orchestrator_still_creates_the_opportunity_normally(self):
        opps = OpportunityStore(self.store, self.audit)
        opp_id = opps.create({"title": "Unaffected"}, actor="Aryan")
        opp = self.store.get("rh_opportunities", opp_id)
        self.assertEqual(opp["title"], "Unaffected")
        self.assertEqual(opp["stage"], "New")


class QualifyHookTests(LifecycleHooksTestBase):
    def test_qualify_with_orchestrator_advances_to_qualified(self):
        opps = OpportunityStore(self.store, self.audit, orchestrator=self.orchestrator)
        opp_id = opps.create({"title": "Build a dashboard", "description": "d"}, actor="Aryan")
        quals = QualificationStore(self.store, self.audit, build_qualification_engine({}), orchestrator=self.orchestrator)
        quals.qualify(opp_id, actor="Aryan")
        self.assertEqual(self.orchestrator.current_state(opp_id), "QUALIFIED")

    def test_qualify_advances_to_qualified_regardless_of_recommendation(self):
        # A role whose text should qualify as IGNORE (admin work) still
        # reaches lifecycle QUALIFIED -- the hook mirrors the existing
        # stage-move behavior, which likewise never branches on
        # recommendation; only a later, explicit business decision (not
        # this hook) should ever move an IGNORE opportunity to LOST.
        opps = OpportunityStore(self.store, self.audit, orchestrator=self.orchestrator)
        opp_id = opps.create({"title": "Virtual Assistant", "description": "answer phones, data entry"}, actor="Aryan")
        quals = QualificationStore(self.store, self.audit, build_qualification_engine({}), orchestrator=self.orchestrator)
        result = quals.qualify(opp_id, actor="Aryan")
        self.assertEqual(result["recommendation"], "IGNORE")
        self.assertEqual(self.orchestrator.current_state(opp_id), "QUALIFIED")

    def test_requalify_all_style_repeated_qualify_is_a_safe_no_op(self):
        opps = OpportunityStore(self.store, self.audit, orchestrator=self.orchestrator)
        opp_id = opps.create({"title": "Build a dashboard", "description": "d"}, actor="Aryan")
        quals = QualificationStore(self.store, self.audit, build_qualification_engine({}), orchestrator=self.orchestrator)
        quals.qualify(opp_id, actor="Aryan")
        quals.qualify(opp_id, actor="system")  # simulates requalify_all()
        self.assertEqual(self.orchestrator.current_state(opp_id), "QUALIFIED")

    def test_qualify_without_orchestrator_still_works_normally(self):
        opps = OpportunityStore(self.store, self.audit)
        opp_id = opps.create({"title": "Build a dashboard", "description": "d"}, actor="Aryan")
        quals = QualificationStore(self.store, self.audit, build_qualification_engine({}))
        result = quals.qualify(opp_id, actor="Aryan")
        self.assertIn("recommendation", result)
        opp = self.store.get("rh_opportunities", opp_id)
        self.assertEqual(opp["stage"], "Qualified")


class ProposalGenerateHookTests(LifecycleHooksTestBase):
    def _qualified_opportunity(self, orchestrator):
        opps = OpportunityStore(self.store, self.audit, orchestrator=orchestrator)
        opp_id = opps.create({"title": "Build a dashboard", "description": "d"}, actor="Aryan")
        quals = QualificationStore(self.store, self.audit, build_qualification_engine({}), orchestrator=orchestrator)
        quals.qualify(opp_id, actor="Aryan")
        return opp_id

    def test_generate_with_orchestrator_and_needs_aryan_reaches_awaiting_approval(self):
        opp_id = self._qualified_opportunity(self.orchestrator)
        proposals = ProposalStore(self.store, self.audit, needs_aryan=self.needs_aryan, orchestrator=self.orchestrator)
        proposals.generate(opp_id, "short", actor="Aryan")
        self.assertEqual(self.orchestrator.current_state(opp_id), "AWAITING_APPROVAL")

    def test_generate_with_orchestrator_but_no_needs_aryan_stops_at_pitch_ready(self):
        opp_id = self._qualified_opportunity(self.orchestrator)
        proposals = ProposalStore(self.store, self.audit, needs_aryan=None, orchestrator=self.orchestrator)
        proposals.generate(opp_id, "short", actor="Aryan")
        self.assertEqual(self.orchestrator.current_state(opp_id), "PITCH_READY")

    def test_generate_without_orchestrator_still_works_normally(self):
        opp_id = self._qualified_opportunity(None)
        proposals = ProposalStore(self.store, self.audit, needs_aryan=self.needs_aryan)
        result = proposals.generate(opp_id, "short", actor="Aryan")
        self.assertIn("proposal_id", result)
        self.assertIsNotNone(result["needs_aryan_id"])


class ApplyDecisionSideEffectHookTests(LifecycleHooksTestBase):
    def _proposal_awaiting_approval(self):
        opps = OpportunityStore(self.store, self.audit, orchestrator=self.orchestrator)
        opp_id = opps.create({"title": "Build a dashboard", "description": "d"}, actor="Aryan")
        quals = QualificationStore(self.store, self.audit, build_qualification_engine({}), orchestrator=self.orchestrator)
        quals.qualify(opp_id, actor="Aryan")
        proposals = ProposalStore(self.store, self.audit, needs_aryan=self.needs_aryan, orchestrator=self.orchestrator)
        drafted = proposals.generate(opp_id, "short", actor="Aryan")
        return opp_id, drafted["proposal_id"], drafted["needs_aryan_id"]

    def test_approved_decision_advances_lifecycle_to_approved(self):
        opp_id, proposal_id, needs_aryan_id = self._proposal_awaiting_approval()
        item = self.store.get("needs_aryan_items", needs_aryan_id)
        apply_decision_side_effect(self.store, self.audit, item, "APPROVED", "Aryan", orchestrator=self.orchestrator)
        self.assertEqual(self.orchestrator.current_state(opp_id), "APPROVED")
        proposal = self.store.get("rh_proposals", proposal_id)
        self.assertEqual(proposal["status"], "APPROVED")

    def test_rejected_decision_bounces_lifecycle_back_to_pitch_ready(self):
        opp_id, proposal_id, needs_aryan_id = self._proposal_awaiting_approval()
        item = self.store.get("needs_aryan_items", needs_aryan_id)
        apply_decision_side_effect(self.store, self.audit, item, "REJECTED", "Aryan", orchestrator=self.orchestrator)
        self.assertEqual(self.orchestrator.current_state(opp_id), "PITCH_READY")

    def test_changes_requested_decision_bounces_lifecycle_back_to_pitch_ready(self):
        opp_id, proposal_id, needs_aryan_id = self._proposal_awaiting_approval()
        item = self.store.get("needs_aryan_items", needs_aryan_id)
        apply_decision_side_effect(self.store, self.audit, item, "CHANGES_REQUESTED", "Aryan", orchestrator=self.orchestrator)
        self.assertEqual(self.orchestrator.current_state(opp_id), "PITCH_READY")

    def test_deferred_decision_does_not_move_lifecycle(self):
        opp_id, proposal_id, needs_aryan_id = self._proposal_awaiting_approval()
        item = self.store.get("needs_aryan_items", needs_aryan_id)
        apply_decision_side_effect(self.store, self.audit, item, "DEFERRED", "Aryan", orchestrator=self.orchestrator)
        self.assertEqual(self.orchestrator.current_state(opp_id), "AWAITING_APPROVAL")

    def test_without_orchestrator_behavior_is_unchanged(self):
        opp_id, proposal_id, needs_aryan_id = self._proposal_awaiting_approval()
        item = self.store.get("needs_aryan_items", needs_aryan_id)
        apply_decision_side_effect(self.store, self.audit, item, "APPROVED", "Aryan")
        proposal = self.store.get("rh_proposals", proposal_id)
        self.assertEqual(proposal["status"], "APPROVED")

    def test_non_proposal_ref_type_is_still_a_no_op(self):
        item = {"ref_type": "something_else", "ref_id": "x"}
        # Must not raise even with an orchestrator wired in.
        apply_decision_side_effect(self.store, self.audit, item, "APPROVED", "Aryan", orchestrator=self.orchestrator)


if __name__ == "__main__":
    unittest.main()
