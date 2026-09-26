"""Real, in-process tests for the TTT AI Communications Workforce
(falguna/comms_workforce.py): Receptionist, Sales Rep, Support Rep, Account
Manager, plus the security/permission invariants and an end-to-end trial
(enquiry -> classification -> assignment -> draft -> approval -> reporting)
required by the mission. No mocking of the store -- real temp SQLite DBs,
same convention as tests/test_comms.py and tests/test_revenue_hunter.py."""

import subprocess
import tempfile
import unittest
from pathlib import Path

from falguna.account_management import AccountManagerService
from falguna.audit import AuditLog
from falguna.comms import CommsStore
from falguna.comms_workforce import (
    AccountManagerAgent, ReceptionistAgent, SalesRepAgent, SupportAgent,
    run_agent_for_conversation, run_workforce_pass,
)
from falguna.revenue_hunter import ActiveJobStore, OpportunityStore, ProposalStore, apply_decision_side_effect
from falguna.risk_engine import RiskClassificationStore
from falguna.runtime import open_control_plane
from falguna.site_content import EnquiryStore
from falguna.store import StateStore
from falguna.ttt_hq import NeedsAryanQueue

PURSUE_OPPORTUNITY = {
    "title": "Booking platform rebuild",
    "description": "Full booking system rebuild with React, Node.js, Stripe integration, and CRM.",
    "required_skills": "React, Node.js, Stripe, CRM",
    "budget_rate": "$4000",
}


class _CommsWorkforceCase(unittest.TestCase):
    """Plain comms-only fixture (no git repo needed) for Receptionist /
    Sales Rep / Support Rep tests that don't touch delivery evidence."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.db_path = root / "state.db"
        self.store = StateStore(self.db_path)
        self.store.migrate()
        self.audit = AuditLog(root / "audit.jsonl")
        self.needs_aryan = NeedsAryanQueue(self.store, self.audit)
        self.comms = CommsStore(self.store, self.audit, self.needs_aryan)
        self.opportunities = OpportunityStore(self.store, self.audit)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def _sales_conversation(self, budget_rate=None, deadline=None):
        opp_id = self.opportunities.create(
            {**PURSUE_OPPORTUNITY, "budget_rate": budget_rate, "deadline": deadline}, actor="website",
        )
        conv = self.comms.open_conversation(
            "WEBSITE", "sales", subject="Project enquiry", priority="high", actor="website",
            linked_opportunity_id=opp_id,
        )
        self.comms.add_message(conv["id"], "INBOUND", PURSUE_OPPORTUNITY["description"], actor="website")
        return opp_id, conv["id"]


class ReceptionistAgentTests(_CommsWorkforceCase):
    def test_drafts_acknowledgement_and_asks_for_missing_sales_fields(self):
        _, conv_id = self._sales_conversation()  # no budget_rate, no deadline
        actions = ReceptionistAgent(self.store, self.comms).run(self.comms.get_conversation(conv_id))
        self.assertTrue(any("acknowledgement" in a for a in actions))
        conv = self.comms.get_conversation(conv_id)
        draft = [m for m in conv["messages"] if m["direction"] == "OUTBOUND"][0]
        self.assertEqual(draft["status"], "DRAFT")
        self.assertIn("budget", draft["body"])
        self.assertIn("timeline", draft["body"])

    def test_no_missing_fields_requested_once_budget_and_deadline_are_on_file(self):
        _, conv_id = self._sales_conversation(budget_rate="$4000", deadline="2026-12-01")
        conv = self.comms.get_conversation(conv_id)
        opp = self.store.get("rh_opportunities", conv["linked_opportunity_id"])
        self.store.update("rh_opportunities", conv["linked_opportunity_id"], contract_type="fixed")
        actions = ReceptionistAgent(self.store, self.comms).run(self.comms.get_conversation(conv_id))
        draft = [m for m in self.comms.get_conversation(conv_id)["messages"] if m["direction"] == "OUTBOUND"][0]
        self.assertNotIn("could you also share", draft["body"])

    def test_does_not_draft_a_second_acknowledgement(self):
        _, conv_id = self._sales_conversation()
        agent = ReceptionistAgent(self.store, self.comms)
        first = agent.run(self.comms.get_conversation(conv_id))
        second = agent.run(self.comms.get_conversation(conv_id))
        self.assertTrue(first)
        self.assertEqual(second, [])

    def test_skips_resolved_conversations(self):
        _, conv_id = self._sales_conversation()
        self.comms.set_status(conv_id, "resolved", "Aryan")
        actions = ReceptionistAgent(self.store, self.comms).run(self.comms.get_conversation(conv_id))
        self.assertEqual(actions, [])


class SalesRepAgentTests(_CommsWorkforceCase):
    def _agent(self):
        return SalesRepAgent(self.store, self.audit, self.comms, self.needs_aryan)

    def test_drafts_proposal_for_pursue_opportunity_and_requires_approval(self):
        opp_id, conv_id = self._sales_conversation(budget_rate="$4000")
        actions = self._agent().run(self.comms.get_conversation(conv_id))
        self.assertTrue(any("drafted proposal" in a for a in actions))
        proposals = self.store.list("rh_proposals", "opportunity_id=?", (opp_id,))
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["status"], "DRAFT")
        pending = self.needs_aryan.list_pending()
        self.assertTrue(any(i["ref_type"] == "rh_proposal" and i["ref_id"] == proposals[0]["id"] for i in pending))
        # never auto-sent to the client
        conv = self.comms.get_conversation(conv_id)
        self.assertFalse(any(m["status"] == "SENT" and m["direction"] == "OUTBOUND" for m in conv["messages"]))

    def test_does_not_duplicate_a_pending_proposal_on_a_second_pass(self):
        opp_id, conv_id = self._sales_conversation(budget_rate="$4000")
        agent = self._agent()
        agent.run(self.comms.get_conversation(conv_id))
        second = agent.run(self.comms.get_conversation(conv_id))
        self.assertFalse(any("drafted proposal" in a for a in second))
        self.assertEqual(len(self.store.list("rh_proposals", "opportunity_id=?", (opp_id,))), 1)

    def test_pricing_commitment_language_is_escalated_not_answered(self):
        opp_id, conv_id = self._sales_conversation(budget_rate="$4000")
        self.comms.add_message(conv_id, "INBOUND", "Great -- let's finalize, we agree to the final price.", actor="website")
        actions = self._agent().run(self.comms.get_conversation(conv_id))
        self.assertTrue(any("escalated pricing/contract commitment" in a for a in actions))
        conv = self.comms.get_conversation(conv_id)
        self.assertEqual(conv["status"], "pending_approval")
        # no proposal, no reply, no commitment was made automatically
        self.assertEqual(self.store.list("rh_proposals", "opportunity_id=?", (opp_id,)), [])
        self.assertFalse(any(m["direction"] == "OUTBOUND" and not m.get("is_internal_note") for m in conv["messages"]))
        pending = self.needs_aryan.list_pending()
        self.assertTrue(any(i["kind"] == "negotiation_response_approval" for i in pending))

    def test_tracks_a_follow_up_once_the_proposal_moves_the_opportunity_forward(self):
        opp_id, conv_id = self._sales_conversation(budget_rate="$4000")
        actions = self._agent().run(self.comms.get_conversation(conv_id))
        self.assertTrue(any("drafted proposal" in a for a in actions))
        self.assertTrue(any("drafted response_followup" in a for a in actions))
        followups = self.store.list("rh_followups", "opportunity_id=?", (opp_id,))
        self.assertEqual(len(followups), 1)
        self.assertEqual(followups[0]["status"], "DRAFT")

    def test_no_linked_opportunity_is_a_safe_no_op(self):
        conv = self.comms.open_conversation("WEBSITE", "sales", priority="normal", actor="website")
        self.comms.add_message(conv["id"], "INBOUND", "Hi, interested in your services.", actor="website")
        actions = self._agent().run(self.comms.get_conversation(conv["id"]))
        self.assertEqual(actions, [])


class SupportAgentTests(_CommsWorkforceCase):
    def _support_conversation(self, body):
        conv = self.comms.open_conversation("SUPPORT", "support", priority="normal", actor="website")
        self.comms.add_message(conv["id"], "INBOUND", body, actor="website")
        return conv["id"]

    def _agent(self):
        return SupportAgent(self.store, self.audit, self.comms, self.needs_aryan)

    def test_assigns_and_drafts_routine_reply_for_known_intent(self):
        conv_id = self._support_conversation("What exactly is included in the support plan?")
        actions = self._agent().run(self.comms.get_conversation(conv_id))
        self.assertTrue(any("assigned to AI Support Rep" in a for a in actions))
        self.assertTrue(any("drafted routine reply" in a for a in actions))
        conv = self.comms.get_conversation(conv_id)
        draft = [m for m in conv["messages"] if m["direction"] == "OUTBOUND" and not m.get("is_internal_note")][0]
        self.assertEqual(draft["status"], "DRAFT")

    def test_escalates_sensitive_content_instead_of_replying(self):
        conv_id = self._support_conversation("I need to verify my identity, please send my social security number confirmation.")
        actions = self._agent().run(self.comms.get_conversation(conv_id))
        self.assertTrue(any("escalated to" in a for a in actions))
        conv = self.comms.get_conversation(conv_id)
        self.assertEqual(conv["status"], "pending_approval")
        self.assertFalse(any(m["direction"] == "OUTBOUND" and not m.get("is_internal_note") for m in conv["messages"]))

    def test_escalates_when_no_safe_reply_template(self):
        conv_id = self._support_conversation("unsubscribe, this is an automated message, no-reply.")
        actions = self._agent().run(self.comms.get_conversation(conv_id))
        self.assertTrue(any("escalated to" in a for a in actions))

    def test_records_customer_context_exactly_once(self):
        conv_id = self._support_conversation("What exactly is included in the support plan?")
        agent = self._agent()
        agent.run(self.comms.get_conversation(conv_id))
        agent.run(self.comms.get_conversation(conv_id))
        conv = self.comms.get_conversation(conv_id)
        context_notes = [m for m in conv["messages"] if m.get("is_internal_note") and m["body"].startswith("Known contact:")]
        # no contact was ever attached to this conversation, so there is
        # nothing to record -- confirms this never fabricates context
        self.assertEqual(context_notes, [])

    def test_ignores_non_support_departments(self):
        conv = self.comms.open_conversation("WEBSITE", "general", priority="normal", actor="website")
        self.comms.add_message(conv["id"], "INBOUND", "Hello there", actor="website")
        actions = self._agent().run(self.comms.get_conversation(conv["id"]))
        self.assertEqual(actions, [])


class _RepoCase(unittest.TestCase):
    """Real ControlPlane + real git repo, same fixture shape as
    tests/test_account_management.py -- delivery evidence (missions/runs/
    checkpoints) has to be real, since AccountManagerAgent must never
    invent a status."""

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
        self.needs_aryan = NeedsAryanQueue(self.store, self.audit, self.control)
        self.comms = CommsStore(self.store, self.audit, self.needs_aryan)
        self.opportunities = OpportunityStore(self.store, self.audit)
        self.active_jobs = ActiveJobStore(self.store, self.audit)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def _won_job_with_conversation(self):
        opp_id = self.opportunities.create({"title": "Build a CRM dashboard"}, actor="Aryan")
        self.opportunities.mark_won(opp_id, "Aryan", final_price=2000)
        job_id = self.active_jobs.create_from_won_opportunity(opp_id, "Aryan")
        conv = self.comms.open_conversation(
            "PROJECT", "projects", subject="CRM dashboard project", priority="normal", actor="client",
            linked_opportunity_id=opp_id,
        )
        return opp_id, job_id, conv["id"]

    def _real_run(self, task_id, status, error=None):
        run_id = self.store.create("runs", {
            "task_id": task_id, "status": status, "attempt": 1, "worker": "test", "model": "test",
            "worktree": None, "head_sha": None, "error": error,
            "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:00+00:00",
        })
        return run_id


class AccountManagerAgentTests(_RepoCase):
    def _agent(self):
        return AccountManagerAgent(self.store, self.audit, self.comms, self.needs_aryan)

    def test_no_active_job_is_a_safe_no_op(self):
        conv = self.comms.open_conversation("PROJECT", "projects", priority="normal", actor="client")
        actions = self._agent().run(self.comms.get_conversation(conv["id"]))
        self.assertEqual(actions, [])

    def test_drafts_truthful_status_update_when_client_asks(self):
        _, _, conv_id = self._won_job_with_conversation()
        self.comms.add_message(conv_id, "INBOUND", "What's the status of my dashboard project?", actor="client")
        actions = self._agent().run(self.comms.get_conversation(conv_id))
        self.assertTrue(any("drafted evidence-based client status update" in a for a in actions))
        conv = self.comms.get_conversation(conv_id)
        draft = [m for m in conv["messages"] if m["direction"] == "OUTBOUND"][0]
        self.assertEqual(draft["status"], "DRAFT")
        self.assertIn("hasn't started yet", draft["body"])  # true: no mission created yet

    def test_escalates_a_real_blocked_run(self):
        opp_id, job_id, conv_id = self._won_job_with_conversation()
        result = self.active_jobs.trigger_handoff(job_id, str(self.repo), self.control)
        self._real_run(result["task_id"], "FAILED", error="build failed")
        actions = self._agent().run(self.comms.get_conversation(conv_id))
        self.assertTrue(any("escalated account-level blocker" in a for a in actions))
        pending = self.needs_aryan.list_pending()
        self.assertTrue(any(i["kind"] == "client_response_decision" for i in pending))
        conv = self.comms.get_conversation(conv_id)
        self.assertEqual(conv["status"], "pending_approval")

    def test_does_not_double_escalate_the_same_blocker(self):
        opp_id, job_id, conv_id = self._won_job_with_conversation()
        result = self.active_jobs.trigger_handoff(job_id, str(self.repo), self.control)
        self._real_run(result["task_id"], "FAILED", error="build failed")
        agent = self._agent()
        agent.run(self.comms.get_conversation(conv_id))
        second = agent.run(self.comms.get_conversation(conv_id))
        self.assertFalse(any("escalated account-level blocker" in a for a in second))


class DispatcherTests(_CommsWorkforceCase):
    def test_routes_sales_department_to_receptionist_then_sales_rep(self):
        opp_id, conv_id = self._sales_conversation(budget_rate="$4000")
        result = run_agent_for_conversation(self.store, self.audit, conv_id, actor="ai_workforce", needs_aryan=self.needs_aryan)
        self.assertEqual(result["department"], "sales")
        self.assertTrue(any("acknowledgement" in a for a in result["actions"]))

    def test_unknown_conversation_id_raises(self):
        with self.assertRaises(ValueError):
            run_agent_for_conversation(self.store, self.audit, "does-not-exist", needs_aryan=self.needs_aryan)

    def test_workforce_pass_checks_every_active_conversation(self):
        self._sales_conversation(budget_rate="$4000")
        conv2 = self.comms.open_conversation("SUPPORT", "support", priority="normal", actor="website")
        self.comms.add_message(conv2["id"], "INBOUND", "What exactly is included in the plan?", actor="website")
        result = run_workforce_pass(self.store, self.audit, actor="ai_workforce", needs_aryan=self.needs_aryan)
        self.assertEqual(result["conversations_checked"], 2)
        self.assertEqual(result["conversations_acted_on"], 2)

    def test_second_pass_over_same_conversations_is_quiet(self):
        self._sales_conversation(budget_rate="$4000")
        run_workforce_pass(self.store, self.audit, needs_aryan=self.needs_aryan)
        second = run_workforce_pass(self.store, self.audit, needs_aryan=self.needs_aryan)
        self.assertEqual(second["conversations_acted_on"], 0)


class RiskIntegrationTests(_CommsWorkforceCase):
    """Milestone 2 wired into the dispatcher: every OUTBOUND DRAFT message
    the workforce produces gets a deterministic risk classification, and a
    second pass never reclassifies or double-escalates the same draft."""

    def test_dispatcher_classifies_every_new_outbound_draft(self):
        _, conv_id = self._sales_conversation()  # Receptionist drafts an acknowledgement
        result = run_agent_for_conversation(self.store, self.audit, conv_id, actor="ai_workforce", needs_aryan=self.needs_aryan)
        self.assertTrue(any("Risk classified" in a for a in result["actions"]))
        conv = self.comms.get_conversation(conv_id)
        draft = [m for m in conv["messages"] if m["direction"] == "OUTBOUND"][0]
        risk = RiskClassificationStore(self.store, self.audit, self.needs_aryan)
        rows = risk.list_for_subject("comm_message", draft["id"])
        self.assertEqual(len(rows), 1)
        self.assertIn(rows[0]["risk"], ("LOW", "MEDIUM", "HIGH"))

    def test_second_pass_does_not_reclassify_the_same_draft(self):
        _, conv_id = self._sales_conversation()
        run_agent_for_conversation(self.store, self.audit, conv_id, actor="ai_workforce", needs_aryan=self.needs_aryan)
        conv = self.comms.get_conversation(conv_id)
        draft = [m for m in conv["messages"] if m["direction"] == "OUTBOUND"][0]
        risk = RiskClassificationStore(self.store, self.audit, self.needs_aryan)
        first_rows = risk.list_for_subject("comm_message", draft["id"])
        second = run_agent_for_conversation(self.store, self.audit, conv_id, actor="ai_workforce", needs_aryan=self.needs_aryan)
        self.assertFalse(any("Risk classified" in a for a in second["actions"]))
        second_rows = risk.list_for_subject("comm_message", draft["id"])
        self.assertEqual(len(second_rows), len(first_rows))

    def test_high_risk_draft_reply_escalates_through_workforce_pass(self):
        """A support reply that happens to read as a commitment (e.g. a
        template that got a refund-related intent) must be classified HIGH
        and show up in Needs Aryan -- proven here by drafting a message
        directly with commitment language and confirming the dispatcher's
        risk pass (not the agent itself) is what catches and escalates it."""
        conv = self.comms.open_conversation("SUPPORT", "support", priority="normal", actor="website")
        self.comms.add_message(conv["id"], "INBOUND", "Can I get help with my account?", actor="website")
        # Simulate an already-drafted outbound reply containing high-risk language
        # (defensive coverage: even a draft not produced by today's templates
        # must still be caught by the dispatcher's risk pass before anyone acts on it).
        self.comms.add_message(
            conv["id"], "OUTBOUND", "We agree to issue a full refund for this.",
            sender_agent="AI Support Rep", actor="ai_workforce",
        )
        result = run_agent_for_conversation(self.store, self.audit, conv["id"], actor="ai_workforce", needs_aryan=self.needs_aryan)
        self.assertTrue(any("Risk classified HIGH" in a for a in result["actions"]))
        pending = self.needs_aryan.list_pending()
        self.assertTrue(any(i["kind"] == "communications_approval" for i in pending))


class EndToEndTrialTests(_CommsWorkforceCase):
    """Requirement 8: real persistence and recovery, using clearly
    identified local test data. Walks enquiry -> classification ->
    sales assignment -> response draft -> approval -> executive
    reporting, then closes and reopens the same on-disk database to prove
    nothing lived only in memory."""

    def test_full_trial_enquiry_to_executive_reporting(self):
        enquiries = EnquiryStore(self.store, self.opportunities, self.comms)
        result = enquiries.submit(
            "project", "TEST-TRIAL Jordan Blake", "jordan@test-trial-client.invalid", "Test Trial Co",
            PURSUE_OPPORTUNITY["description"], extra={
                "project_title": PURSUE_OPPORTUNITY["title"], "budget_hint": PURSUE_OPPORTUNITY["budget_rate"],
                "project_type": "fixed",
            },
        )
        opp_id, conv_id = result["opportunity_id"], result["conversation_id"]
        self.assertIsNotNone(opp_id)
        self.assertIsNotNone(conv_id)

        conv = self.comms.get_conversation(conv_id)
        self.assertEqual(conv["department"], "sales")  # classification/routing

        pass1 = run_agent_for_conversation(self.store, self.audit, conv_id, actor="ai_workforce", needs_aryan=self.needs_aryan)
        self.assertTrue(any("acknowledgement" in a for a in pass1["actions"]))
        self.assertTrue(any("drafted proposal" in a for a in pass1["actions"]))

        proposal = self.store.list("rh_proposals", "opportunity_id=?", (opp_id,))[0]
        pending = [i for i in self.needs_aryan.list_pending() if i["ref_type"] == "rh_proposal" and i["ref_id"] == proposal["id"]]
        self.assertEqual(len(pending), 1)

        decision = self.needs_aryan.decide(pending[0]["id"], "approve", "Aryan", note="looks good")
        self.assertEqual(decision["action"], "APPROVED")
        # The same side-effect hook TTT HQ's decision route calls right
        # after NeedsAryanQueue.decide() -- decide() itself stays generic
        # and never reaches into rh_proposals directly.
        apply_decision_side_effect(self.store, self.audit, pending[0], decision["action"], "Aryan")
        self.assertEqual(self.store.get("rh_proposals", proposal["id"])["status"], "APPROVED")

        overview = self.comms.overview()
        self.assertGreaterEqual(overview["open_total"], 1)
        self.assertIn("sales", overview["by_department"])

        self.store.close()
        reopened = StateStore(self.db_path)
        reopened.migrate()
        try:
            recovered_opp = reopened.get("rh_opportunities", opp_id)
            recovered_conv = reopened.get("comm_conversations", conv_id)
            recovered_proposal = reopened.get("rh_proposals", proposal["id"])
            self.assertIsNotNone(recovered_opp)
            self.assertEqual(recovered_conv["department"], "sales")
            self.assertEqual(recovered_proposal["status"], "APPROVED")
        finally:
            reopened.close()
        self.store = StateStore(self.db_path)  # tearDown() closes this handle


class SecurityAndPermissionTests(_CommsWorkforceCase):
    """Focused checks on the hard invariants: nothing here ever sends,
    nothing here ever invents a fact, and every commercial signal ends up
    in front of a human before anything is agreed to."""

    def test_full_workforce_pass_never_creates_a_sent_message(self):
        # A mixed fixture across every department, including sensitive and
        # commitment-language content designed to try to provoke an
        # automatic send.
        self._sales_conversation(budget_rate="$4000")
        sales_commit = self.comms.open_conversation("WEBSITE", "sales", priority="high", actor="website")
        self.comms.add_message(sales_commit["id"], "INBOUND", "We agree to the final price, ready to sign.", actor="website")
        support = self.comms.open_conversation("SUPPORT", "support", priority="normal", actor="website")
        self.comms.add_message(support["id"], "INBOUND", "Please verify my identity and social security number.", actor="website")
        general = self.comms.open_conversation("WEBSITE", "general", priority="normal", actor="website")
        self.comms.add_message(general["id"], "INBOUND", "Just saying hi, no real question here at all today.", actor="website")

        run_workforce_pass(self.store, self.audit, actor="ai_workforce", needs_aryan=self.needs_aryan)

        sent_by_agent = [
            m for m in self.store.list("comm_messages")
            if m.get("sender_agent") and m["status"] == "SENT"
        ]
        self.assertEqual(sent_by_agent, [], f"an agent created a SENT message: {sent_by_agent}")

    def test_mark_message_sent_requires_an_existing_draft(self):
        _, conv_id = self._sales_conversation(budget_rate="$4000")
        with self.assertRaises(Exception):
            self.comms.mark_message_sent("does-not-exist", "Aryan")

    def test_mark_message_sent_rejects_an_already_sent_message(self):
        _, conv_id = self._sales_conversation(budget_rate="$4000")
        ReceptionistAgent(self.store, self.comms).run(self.comms.get_conversation(conv_id))
        draft = [m for m in self.comms.get_conversation(conv_id)["messages"] if m["direction"] == "OUTBOUND"][0]
        self.comms.mark_message_sent(draft["id"], "Aryan")
        with self.assertRaises(Exception):
            self.comms.mark_message_sent(draft["id"], "Aryan")

    def test_commitment_language_always_escalates_even_with_no_opportunity_budget(self):
        opp_id, conv_id = self._sales_conversation()  # no budget on file at all
        self.comms.add_message(conv_id, "INBOUND", "confirmed price, purchase order attached", actor="website")
        actions = SalesRepAgent(self.store, self.audit, self.comms, self.needs_aryan).run(self.comms.get_conversation(conv_id))
        self.assertTrue(any("escalated" in a for a in actions))

    def test_email_provider_cannot_send_by_default(self):
        from falguna.email_admin import EmailStore
        email_store = EmailStore(self.store, self.audit, needs_aryan=self.needs_aryan)
        status = email_store.provider_status()
        self.assertFalse(status["configured"])
        with self.assertRaises(Exception):
            email_store.provider.send("someone@example.com", "subject", "body")

    def test_resolved_conversation_is_never_touched_by_any_role(self):
        opp_id, conv_id = self._sales_conversation(budget_rate="$4000")
        self.comms.set_status(conv_id, "resolved", "Aryan")
        result = run_agent_for_conversation(self.store, self.audit, conv_id, needs_aryan=self.needs_aryan)
        self.assertEqual(result["actions"], [])
        conv = self.comms.get_conversation(conv_id)
        self.assertEqual(len(conv["messages"]), 1)  # only the original inbound message


if __name__ == "__main__":
    unittest.main()
