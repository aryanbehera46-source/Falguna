"""Tests for falguna/sales_manager.py -- the Sales Manager / Revenue
Director (Section 14, Pass E). A read-only aggregator: every test sets up
real rows in the underlying stores and checks the aggregation surfaces
them correctly -- nothing here is mocked."""

import tempfile
import unittest
from pathlib import Path

from falguna.application_executor import ApplicationExecutor
from falguna.audit import AuditLog
from falguna.billing import BillingStore, RetentionStore
from falguna.conversations import ConversationStore
from falguna.lifecycle import LifecycleOrchestrator
from falguna.onboarding import OnboardingStore
from falguna.opportunity_agent import build_qualification_engine
from falguna.revenue_hunter import OpportunityStore, ProposalStore, QualificationStore
from falguna.sales_manager import SalesManagerService
from falguna.sales_ops import ClosingService, NegotiationGuardrails, SalesPolicyStore
from falguna.store import StateStore
from falguna.ttt_hq import NeedsAryanQueue


class SalesManagerTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = StateStore(root / "state.db")
        self.store.migrate()
        self.audit = AuditLog(root / "audit.jsonl")
        self.orchestrator = LifecycleOrchestrator(self.store, self.audit)
        self.needs_aryan = NeedsAryanQueue(self.store, self.audit)
        self.opportunities = OpportunityStore(self.store, self.audit, orchestrator=self.orchestrator)
        self.manager = SalesManagerService(self.store, self.audit)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _opportunity(self, title="Build a CRM dashboard"):
        return self.opportunities.create({"title": title, "description": "d"}, actor="Aryan")


class ProposalsNeedingApprovalTests(SalesManagerTestBase):
    def test_surfaces_a_pending_proposal_approval(self):
        opp_id = self._opportunity()
        QualificationStore(self.store, self.audit, build_qualification_engine({})).qualify(opp_id, actor="Aryan")
        ProposalStore(self.store, self.audit, self.needs_aryan).generate(opp_id, "short", actor="Aryan")
        items = self.manager.proposals_needing_approval()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["kind"], "proposal_approval")


class BlockedApplicationsTests(SalesManagerTestBase):
    def test_surfaces_a_blocked_application_attempt(self):
        opp_id = self._opportunity()
        QualificationStore(self.store, self.audit, build_qualification_engine({})).qualify(opp_id, actor="Aryan")
        proposal = ProposalStore(self.store, self.audit, self.needs_aryan).generate(opp_id, "short", actor="Aryan")
        ProposalStore(self.store, self.audit, self.needs_aryan).mark_approved(proposal["proposal_id"], "Aryan")
        executor = ApplicationExecutor(self.store, self.audit, needs_aryan=self.needs_aryan, orchestrator=self.orchestrator)
        # manual_review has no application URL on file for this opportunity -> BLOCKED, real evidence-based.
        result = executor.apply(opp_id, proposal["proposal_id"], actor="Aryan", channel="manual_review")
        self.assertEqual(result["status"], "BLOCKED")
        blocked = self.manager.blocked_applications()
        self.assertEqual(len(blocked), 1)


class WhoRepliedTests(SalesManagerTestBase):
    def test_last_inbound_message_with_no_reply_yet_is_awaiting(self):
        opp_id = self._opportunity()
        conversations = ConversationStore(self.store, self.audit, orchestrator=self.orchestrator)
        conversations.record_inbound(opp_id, "email", "Sounds good, let's move forward.")
        awaiting = self.manager.who_replied()
        self.assertEqual(len(awaiting), 1)
        self.assertEqual(awaiting[0]["opportunity_id"], opp_id)

    def test_not_awaiting_once_a_reply_was_sent(self):
        opp_id = self._opportunity()
        conversations = ConversationStore(self.store, self.audit, orchestrator=self.orchestrator)
        inbound = conversations.record_inbound(opp_id, "email", "Sounds good, let's move forward.")
        drafted = conversations.draft_reply(inbound["message_id"])
        conversations.mark_sent(drafted["message_id"], "Aryan")
        awaiting = self.manager.who_replied()
        self.assertEqual(len(awaiting), 0)


class NegotiationsNeedingActionTests(SalesManagerTestBase):
    def test_out_of_policy_negotiation_surfaces_as_needing_action(self):
        SalesPolicyStore(self.store).save({"min_project_price": 5000})
        opp_id = self._opportunity()
        NegotiationGuardrails(self.store, self.audit, needs_aryan=self.needs_aryan).evaluate(opp_id, "Aryan", price=1000)
        items = self.manager.negotiations_needing_action()
        self.assertEqual(len(items), 1)


class WhatCanCloseSoonTests(SalesManagerTestBase):
    def test_within_policy_negotiating_opportunity_surfaces(self):
        SalesPolicyStore(self.store).save({"min_project_price": 500})
        opp_id = self._opportunity()
        self.orchestrator.transition(opp_id, "RESEARCHING", actor="Aryan")
        self.orchestrator.transition(opp_id, "QUALIFIED", actor="Aryan")
        self.orchestrator.transition(opp_id, "PITCH_READY", actor="Aryan")
        self.orchestrator.transition(opp_id, "AWAITING_APPROVAL", actor="Aryan")
        self.orchestrator.transition(opp_id, "APPROVED", actor="Aryan")
        self.orchestrator.transition(opp_id, "APPLYING", actor="Aryan")
        self.orchestrator.transition(opp_id, "CONTACTED", actor="Aryan")
        self.orchestrator.transition(opp_id, "REPLIED", actor="Aryan")
        self.orchestrator.transition(opp_id, "NEGOTIATING", actor="Aryan")
        NegotiationGuardrails(self.store, self.audit, needs_aryan=self.needs_aryan).evaluate(opp_id, "Aryan", price=1000)
        soon = self.manager.what_can_close_soon()
        self.assertEqual(len(soon), 1)
        self.assertEqual(soon[0]["opportunity_id"], opp_id)


class OnboardingClientsTests(SalesManagerTestBase):
    def test_won_and_onboarding_opportunity_surfaces_with_completeness(self):
        policy = SalesPolicyStore(self.store)
        policy.save({})
        closing = ClosingService(self.store, self.audit, orchestrator=self.orchestrator, needs_aryan=self.needs_aryan)
        opp_id = self._opportunity()
        for state in ["RESEARCHING", "QUALIFIED", "PITCH_READY", "AWAITING_APPROVAL",
                      "APPROVED", "APPLYING", "CONTACTED", "REPLIED", "NEGOTIATING"]:
            self.orchestrator.transition(opp_id, state, actor="Aryan")
        closing.close(opp_id, "Aryan", client_name="Acme Corp", final_price=2000)
        OnboardingStore(self.store, self.audit).init_checklist(opp_id)
        clients = self.manager.onboarding_clients()
        self.assertEqual(len(clients), 1)
        self.assertEqual(clients[0]["onboarding_completeness"]["complete"], False)


class BlockedDeliveriesTests(SalesManagerTestBase):
    def test_no_blocked_deliveries_when_nothing_handed_off(self):
        self.assertEqual(self.manager.blocked_deliveries(), [])


class OverdueInvoicesTests(SalesManagerTestBase):
    def test_surfaces_a_real_overdue_invoice(self):
        policy = SalesPolicyStore(self.store)
        policy.save({})
        closing = ClosingService(self.store, self.audit, orchestrator=self.orchestrator, needs_aryan=self.needs_aryan)
        opp_id = self._opportunity()
        result = closing.close(opp_id, "Aryan", client_name="Acme Corp", final_price=2000)
        billing = BillingStore(self.store, self.audit)
        invoice_id = billing.create_invoice(result["client_id"], "Aryan", 2000, due_date="2020-01-01")
        billing.mark_sent(invoice_id, "Aryan")
        overdue = self.manager.overdue_invoices()
        self.assertEqual(len(overdue), 1)
        self.assertEqual(overdue[0]["id"], invoice_id)


class UpsellReadyClientsTests(SalesManagerTestBase):
    def test_surfaces_a_client_with_a_due_retention_item(self):
        policy = SalesPolicyStore(self.store)
        policy.save({})
        closing = ClosingService(self.store, self.audit, orchestrator=self.orchestrator, needs_aryan=self.needs_aryan)
        opp_id = self._opportunity()
        result = closing.close(opp_id, "Aryan", client_name="Acme Corp", final_price=2000)
        retention = RetentionStore(self.store, self.audit)
        retention.create_item(result["client_id"], "maintenance", "Aryan", follow_up_date="2020-01-01")
        ready = self.manager.upsell_ready_clients()
        self.assertEqual(len(ready), 1)
        self.assertEqual(ready[0]["client_name"], "Acme Corp")


class BestLeadsTests(SalesManagerTestBase):
    def test_pursue_recommendation_ranks_above_pass(self):
        good_opp = self.opportunities.create({
            "title": "Booking site for Bella Salon", "client_name": "Bella Salon",
            "description": "Need a React + Node.js booking system with Stripe payments and a CRM.",
            "required_skills": "React, Node.js, Stripe, CRM", "budget_rate": "$3000",
        }, actor="Aryan")
        weak_opp = self._opportunity(title="Vague one-off task")
        QualificationStore(self.store, self.audit, build_qualification_engine({})).qualify(good_opp, actor="Aryan")
        QualificationStore(self.store, self.audit, build_qualification_engine({})).qualify(weak_opp, actor="Aryan")
        leads = self.manager.best_leads()
        ids_in_order = [l["opportunity_id"] for l in leads]
        self.assertLess(ids_in_order.index(good_opp), ids_in_order.index(weak_opp))

    def test_never_includes_a_numeric_probability_field(self):
        opp_id = self._opportunity()
        QualificationStore(self.store, self.audit, build_qualification_engine({})).qualify(opp_id, actor="Aryan")
        leads = self.manager.best_leads()
        for lead in leads:
            self.assertNotIn("probability", lead)
            self.assertIn(lead["priority"], {"High", "Medium", "Low"})


class OverviewAndAttentionTests(SalesManagerTestBase):
    def test_overview_returns_all_sections(self):
        overview = self.manager.overview()
        expected_keys = {
            "attention", "best_leads", "proposals_needing_approval", "blocked_applications",
            "who_replied", "negotiations_needing_action", "can_close_soon", "onboarding_clients",
            "blocked_deliveries", "overdue_invoices", "upsell_ready_clients",
        }
        self.assertEqual(set(overview.keys()), expected_keys)

    def test_attention_aggregates_needs_aryan_and_blocked_and_overdue(self):
        attention = self.manager.whats_needs_attention_now()
        self.assertIn("needs_aryan_pending", attention)
        self.assertIn("blocked_deliveries", attention)
        self.assertIn("overdue_invoices", attention)
        self.assertEqual(attention["total_attention_items"], 0)


if __name__ == "__main__":
    unittest.main()
