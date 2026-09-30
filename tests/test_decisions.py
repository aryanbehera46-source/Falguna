import tempfile
import unittest
from pathlib import Path

from falguna.audit import AuditLog
from falguna.decisions import normalize_decision, unified_decision_queue
from falguna.revenue_hunter import OpportunityStore, ProposalStore
from falguna.store import StateStore
from falguna.ttt_hq import NeedsAryanQueue


class UnifiedDecisionQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.tmp.name) / "state.db")
        self.store.migrate()
        self.audit = AuditLog(Path(self.tmp.name) / "audit.jsonl")
        self.needs_aryan = NeedsAryanQueue(self.store, self.audit)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_empty_queue(self):
        result = unified_decision_queue(self.store, self.audit)
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["pending_count"], 0)

    def test_pending_proposal_surfaces_with_required_fields(self):
        opp_id = OpportunityStore(self.store, self.audit).create({"title": "Build", "client_name": "Acme"}, "Aryan")
        proposal = ProposalStore(self.store, self.audit, needs_aryan=self.needs_aryan).generate(opp_id, "detailed", "Aryan")
        result = unified_decision_queue(self.store, self.audit)
        self.assertEqual(result["pending_count"], 1)
        item = result["items"][0]
        self.assertEqual(item["id"], proposal["needs_aryan_id"])
        self.assertEqual(item["department"], "Revenue Hunter")
        self.assertEqual(item["decision_type"], "proposal_approval")
        self.assertEqual(item["status"], "PENDING")
        self.assertIn("approve", item["authorized_actions"])
        self.assertEqual(item["source_link"]["view"], "rhPipeline")
        self.assertIsNone(item["outcome"])

    def test_decided_item_shows_outcome_and_decided_at(self):
        opp_id = OpportunityStore(self.store, self.audit).create({"title": "Build", "client_name": "Acme"}, "Aryan")
        proposal = ProposalStore(self.store, self.audit, needs_aryan=self.needs_aryan).generate(opp_id, "detailed", "Aryan")
        self.needs_aryan.decide(proposal["needs_aryan_id"], "approve", "Aryan", note="looks good")
        result = unified_decision_queue(self.store, self.audit)
        self.assertEqual(result["pending_count"], 0)
        item = result["items"][0]
        self.assertEqual(item["status"], "APPROVED")
        self.assertEqual(item["outcome"], "APPROVED")
        self.assertIsNotNone(item["decided_at"])

    def test_status_and_kind_filters(self):
        opp_id = OpportunityStore(self.store, self.audit).create({"title": "Build", "client_name": "Acme"}, "Aryan")
        ProposalStore(self.store, self.audit, needs_aryan=self.needs_aryan).generate(opp_id, "detailed", "Aryan")
        self.needs_aryan.create_item(
            "risky_action", "Unrelated risky item", "Needs review",
            actor="system", ref_type="run", ref_id="run-1",
        )
        pending_only = unified_decision_queue(self.store, self.audit, status="PENDING")
        self.assertEqual(pending_only["total"], 2)
        by_kind = unified_decision_queue(self.store, self.audit, kind="proposal_approval")
        self.assertEqual(by_kind["total"], 1)

    def test_high_risk_kind_is_always_high_urgency(self):
        item = {"id": "x", "kind": "risky_action", "created_at": None}
        normalized = normalize_decision(item)
        self.assertEqual(normalized["urgency"], "HIGH")

    def test_low_risk_recent_item_is_low_urgency(self):
        from falguna.store import utcnow
        item = {"id": "x", "kind": "proposal_approval", "created_at": utcnow()}
        normalized = normalize_decision(item)
        self.assertEqual(normalized["urgency"], "LOW")


if __name__ == "__main__":
    unittest.main()
