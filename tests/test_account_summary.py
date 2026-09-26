"""TTT Communications V2, Milestone 7 -- Account Management expansion.

Focused tests for `AccountManagerService.account_summary()`: one factual,
evidence-linked account view built on top of the existing, already-tested
CustomerContextService (Milestone 4) and status_for_active_job (Pass D) --
no new tables, no invented sentiment, no fabricated percentages. Real temp
SQLite DBs; missions/requirements/tasks/runs rows are seeded directly
(plain columns, no git repo needed) only where a delivery-risk test
actually requires one.
"""

import json
import tempfile
import unittest
from pathlib import Path

from falguna.account_management import AccountManagerService
from falguna.audit import AuditLog
from falguna.comms import CommsStore
from falguna.revenue_hunter import FollowupStore, OpportunityStore
from falguna.store import StateStore, utcnow
from falguna.ttt_hq import NeedsAryanQueue


class _AccountSummaryCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = StateStore(root / "state.db")
        self.store.migrate()
        self.audit = AuditLog(root / "audit.jsonl")
        self.needs_aryan = NeedsAryanQueue(self.store, self.audit)
        self.comms = CommsStore(self.store, self.audit, self.needs_aryan)
        self.opportunities = OpportunityStore(self.store, self.audit)
        self.followups = FollowupStore(self.store, self.audit)
        self.account_manager = AccountManagerService(self.store, self.audit, needs_aryan=self.needs_aryan)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def _org_with_contact(self, name="Acme Corp", domain="acme.example"):
        org_id = self.comms.find_or_create_organization(name, domain=domain)
        contact_id = self.comms.find_or_create_contact(f"buyer@{domain}", name="Buyer", organization_id=org_id)
        return org_id, contact_id


class ScopedSummaryTests(_AccountSummaryCase):
    def test_summary_is_scoped_to_one_organization(self):
        org_a, contact_a = self._org_with_contact("Acme Corp", "acme.example")
        org_b, contact_b = self._org_with_contact("Beta LLC", "beta.example")
        self.comms.open_conversation(
            "EMAIL", "sales", contact_id=contact_a, organization_id=org_a, actor="test",
        )
        self.comms.open_conversation(
            "EMAIL", "sales", contact_id=contact_b, organization_id=org_b, actor="test",
        )
        summary = self.account_manager.account_summary(org_a)
        self.assertEqual(summary["organization"]["id"], org_a)
        self.assertNotIn(org_b, [c.get("organization_id") for c in summary.get("support_history", [])])

    def test_unanswered_conversation_is_surfaced_with_a_next_action(self):
        org_id, contact_id = self._org_with_contact()
        conv = self.comms.open_conversation(
            "EMAIL", "sales", contact_id=contact_id, organization_id=org_id, actor="test",
        )
        self.comms.add_message(conv["id"], "INBOUND", "Following up on our proposal request.", actor="website")
        summary = self.account_manager.account_summary(org_id)
        self.assertEqual(len(summary["unanswered_conversations"]), 1)
        self.assertTrue(any(conv["id"] in a["evidence"][0] for a in summary["next_actions"]))

    def test_answered_conversation_is_not_flagged_unanswered(self):
        org_id, contact_id = self._org_with_contact()
        conv = self.comms.open_conversation(
            "EMAIL", "sales", contact_id=contact_id, organization_id=org_id, actor="test",
        )
        self.comms.add_message(conv["id"], "INBOUND", "Question about pricing.", actor="website")
        self.comms.add_message(conv["id"], "OUTBOUND", "Happy to help -- here are the details.", actor="Aryan")
        summary = self.account_manager.account_summary(org_id)
        self.assertEqual(summary["unanswered_conversations"], [])


class FollowupAndProposalTests(_AccountSummaryCase):
    def test_outstanding_followup_is_surfaced(self):
        org_id, contact_id = self._org_with_contact()
        opp_id = self.opportunities.create({"title": "Website revamp", "budget_rate": "$2000"}, actor="website")
        conv = self.comms.open_conversation(
            "EMAIL", "sales", contact_id=contact_id, organization_id=org_id, actor="test",
            linked_opportunity_id=opp_id,
        )
        followup_id = self.followups.generate(opp_id, "response_followup")
        summary = self.account_manager.account_summary(org_id)
        self.assertEqual(len(summary["followups_outstanding"]), 1)
        self.assertEqual(summary["followups_outstanding"][0]["id"], followup_id)
        self.assertTrue(any(followup_id in a["evidence"][0] for a in summary["next_actions"]))

    def test_proposal_and_approved_agreement_state_is_factual(self):
        org_id, contact_id = self._org_with_contact()
        opp_id = self.opportunities.create({"title": "Website revamp", "budget_rate": "$2000"}, actor="website")
        conv = self.comms.open_conversation(
            "EMAIL", "sales", contact_id=contact_id, organization_id=org_id, actor="test",
            linked_opportunity_id=opp_id,
        )
        from falguna.revenue_hunter import ProposalStore
        proposals = ProposalStore(self.store, self.audit, needs_aryan=self.needs_aryan)
        result = proposals.generate(opp_id, kind="short", actor="ai_workforce")
        summary = self.account_manager.account_summary(org_id)
        self.assertEqual(len(summary["proposal_contract_state"]["proposals"]), 1)
        self.assertEqual(summary["proposal_contract_state"]["approved_agreements"], [])

        proposals.mark_approved(result["proposal_id"], "Aryan")
        summary = self.account_manager.account_summary(org_id)
        self.assertEqual(len(summary["proposal_contract_state"]["approved_agreements"]), 1)


class DeliveryRiskAndEscalationTests(_AccountSummaryCase):
    def _blocked_active_job(self, opp_id):
        now = utcnow()
        mission_id = self.store.create("missions", {"title": "Delivery", "status": "ACTIVE", "created_at": now, "updated_at": now})
        req_id = self.store.create("requirements", {"mission_id": mission_id, "body": "build it", "acceptance_json": "[]", "created_at": now, "updated_at": now})
        task_id = self.store.create("tasks", {"requirement_id": req_id, "title": "build", "status": "OPEN", "repository": "repo", "base_ref": "main", "policy_json": "{}", "created_at": now, "updated_at": now})
        self.store.create("runs", {"task_id": task_id, "status": "FAILED", "attempt": 1, "worker": "w", "model": "m", "error": "build failed", "created_at": now, "updated_at": now})
        return self.store.create("rh_active_jobs", {
            "opportunity_id": opp_id, "job_payload_json": json.dumps({"title": "Website revamp"}),
            "handoff_status": "HANDED_OFF", "mission_id": mission_id, "created_at": now, "updated_at": now,
        })

    def test_blocked_active_job_is_a_delivery_risk_with_real_evidence(self):
        org_id, contact_id = self._org_with_contact()
        opp_id = self.opportunities.create({"title": "Website revamp", "budget_rate": "$2000"}, actor="website")
        conv = self.comms.open_conversation(
            "EMAIL", "sales", contact_id=contact_id, organization_id=org_id, actor="test",
            linked_opportunity_id=opp_id,
        )
        job_id = self._blocked_active_job(opp_id)
        summary = self.account_manager.account_summary(org_id)
        self.assertEqual(len(summary["delivery_risks"]), 1)
        self.assertEqual(summary["delivery_risks"][0]["active_job_id"], job_id)
        self.assertIn("build failed", summary["delivery_risks"][0]["blocker_reason"])
        self.assertTrue(any("delivery blocker" in a["action"] for a in summary["next_actions"]))

    def test_recent_escalation_on_a_linked_conversation_is_surfaced(self):
        org_id, contact_id = self._org_with_contact()
        conv = self.comms.open_conversation(
            "EMAIL", "sales", contact_id=contact_id, organization_id=org_id, actor="test",
        )
        self.comms.add_message(conv["id"], "INBOUND", "We agree to the final price, ready to sign.", actor="website")
        esc = self.comms.escalate(
            conv["id"], "Pricing commitment needs review", "Needs a human decision.", actor="ai_workforce",
        )
        summary = self.account_manager.account_summary(org_id)
        self.assertTrue(any(i["ref_id"] == conv["id"] for i in summary["recent_escalations"]))

    def test_billing_issue_is_distinguished_from_other_support_issues(self):
        org_id, contact_id = self._org_with_contact()
        billing_conv = self.comms.open_conversation(
            "EMAIL", "billing", contact_id=contact_id, organization_id=org_id, actor="test",
        )
        support_conv = self.comms.open_conversation(
            "EMAIL", "support", contact_id=contact_id, organization_id=org_id, actor="test",
        )
        self.comms.add_message(billing_conv["id"], "INBOUND", "My invoice looks wrong.", actor="website")
        self.comms.add_message(support_conv["id"], "INBOUND", "How do I reset my password.", actor="website")
        summary = self.account_manager.account_summary(org_id)
        billing_ids = {c["id"] for c in summary["billing_issues"]}
        self.assertIn(billing_conv["id"], billing_ids)
        self.assertNotIn(support_conv["id"], billing_ids)


class RenewalAndExpansionTests(_AccountSummaryCase):
    def test_no_renewal_or_expansion_signal_for_a_single_fresh_opportunity(self):
        org_id, contact_id = self._org_with_contact()
        opp_id = self.opportunities.create({"title": "Website revamp"}, actor="website")
        self.comms.open_conversation(
            "EMAIL", "sales", contact_id=contact_id, organization_id=org_id, actor="test",
            linked_opportunity_id=opp_id,
        )
        summary = self.account_manager.account_summary(org_id)
        self.assertEqual(summary["renewal_candidates"], [])
        self.assertEqual(summary["expansion_opportunities"], [])

    def test_second_opportunity_for_the_same_org_is_a_factual_expansion_signal(self):
        org_id, contact_id = self._org_with_contact()
        opp_a = self.opportunities.create({"title": "Website revamp"}, actor="website")
        opp_b = self.opportunities.create({"title": "Mobile app follow-on"}, actor="website")
        self.comms.open_conversation(
            "EMAIL", "sales", contact_id=contact_id, organization_id=org_id, actor="test", linked_opportunity_id=opp_a,
        )
        self.comms.open_conversation(
            "EMAIL", "sales", contact_id=contact_id, organization_id=org_id, actor="test", linked_opportunity_id=opp_b,
        )
        summary = self.account_manager.account_summary(org_id)
        self.assertEqual(len(summary["expansion_opportunities"]), 1)


if __name__ == "__main__":
    unittest.main()
