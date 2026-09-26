"""TTT Communications V2, Milestone 6 -- Sales Conversation Workflow.

Focused tests for the one real addition this milestone makes: wiring
`SalesRepAgent` (falguna/comms_workforce.py) to the already-built, already-
tested `LifecycleOrchestrator` (falguna/lifecycle.py) that every other
Revenue Hunter entry point (opportunity_agent.py, hq_web.py's routes)
already injects into OpportunityStore/QualificationStore/ProposalStore.
No new stage enum, no new table, no second CRM -- this reuses the one real
state machine that already spans DISCOVERED -> ... -> WON/LOST, plus two
small genuinely-new behaviors the mission asked for that had no home yet:
a customer-visible discovery question on a later pass when structured
buyer fields are still missing, and an Engineering-feasibility escalation
when the buyer's own words ask whether something is technically possible.

Real temp SQLite DBs, no mocking -- same convention as every other
comms_workforce test file.
"""

import tempfile
import unittest
from pathlib import Path

from falguna.audit import AuditLog
from falguna.comms import CommsStore
from falguna.comms_workforce import (
    SalesRepAgent, run_agent_for_conversation,
)
from falguna.lifecycle import LifecycleOrchestrator
from falguna.revenue_hunter import OpportunityStore, apply_decision_side_effect
from falguna.store import StateStore
from falguna.ttt_hq import NeedsAryanQueue

PURSUE_OPPORTUNITY = {
    "title": "Booking platform rebuild",
    "description": "Full booking system rebuild with React, Node.js, Stripe integration, and CRM.",
    "required_skills": "React, Node.js, Stripe, CRM",
    "budget_rate": "$4000",
}


class _SalesLifecycleCase(unittest.TestCase):
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
        self.lifecycle = LifecycleOrchestrator(self.store, self.audit)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def _agent(self):
        return SalesRepAgent(self.store, self.audit, self.comms, self.needs_aryan)

    def _pursue_conversation(self):
        opp_id = self.opportunities.create(dict(PURSUE_OPPORTUNITY), actor="website")
        conv = self.comms.open_conversation(
            "WEBSITE", "sales", subject="Project enquiry", priority="high", actor="website",
            linked_opportunity_id=opp_id,
        )
        self.comms.add_message(conv["id"], "INBOUND", PURSUE_OPPORTUNITY["description"], actor="website")
        return opp_id, conv["id"]

    def _weak_conversation_with_seeded_maybe(self):
        """A lead deliberately given no budget/deadline/contract_type and a
        qualification row seeded directly (recommendation=MAYBE) rather
        than left to the real scoring engine -- this test is about the
        discovery-question side channel, not qualification scoring, which
        is already covered by tests/test_revenue_hunter.py."""
        opp_id = self.opportunities.create({
            "title": "General inquiry", "description": "Not sure exactly what we need yet, just exploring options.",
            "required_skills": "",
        }, actor="website")
        conv = self.comms.open_conversation(
            "WEBSITE", "sales", subject="General inquiry", priority="normal", actor="website",
            linked_opportunity_id=opp_id,
        )
        self.comms.add_message(conv["id"], "INBOUND", "Not sure exactly what we need yet, just exploring options.", actor="website")
        self.store.create("rh_qualifications", {
            "opportunity_id": opp_id, "fit_score": 20, "budget_quality": "unknown",
            "effort_vs_return": "unclear", "recurring_potential": "unknown", "urgency": "unknown",
            "recommendation": "MAYBE", "created_at": self.store.get("rh_opportunities", opp_id)["created_at"],
        })
        return opp_id, conv["id"]


class LifecycleWiringTests(_SalesLifecycleCase):
    def test_first_touch_initializes_lifecycle_discovered_or_beyond(self):
        opp_id, conv_id = self._pursue_conversation()
        self.assertIsNone(self.store.get("rh_opportunities", opp_id)["lifecycle_state"])
        self._agent().run(self.comms.get_conversation(conv_id))
        state = self.store.get("rh_opportunities", opp_id)["lifecycle_state"]
        self.assertIsNotNone(state)
        events = self.lifecycle.history(opp_id)
        self.assertTrue(any(e["to_state"] == "DISCOVERED" for e in events))

    def test_pursue_lead_reaches_awaiting_approval_on_first_pass(self):
        opp_id, conv_id = self._pursue_conversation()
        actions = self._agent().run(self.comms.get_conversation(conv_id))
        self.assertTrue(any("drafted proposal" in a for a in actions))
        opp = self.store.get("rh_opportunities", opp_id)
        self.assertEqual(opp["lifecycle_state"], "AWAITING_APPROVAL")
        to_states = [e["to_state"] for e in self.lifecycle.history(opp_id)]
        self.assertEqual(to_states, ["DISCOVERED", "QUALIFIED", "PITCH_READY", "AWAITING_APPROVAL"])

    def test_does_not_re_transition_or_re_escalate_on_a_second_identical_pass(self):
        opp_id, conv_id = self._pursue_conversation()
        agent = self._agent()
        agent.run(self.comms.get_conversation(conv_id))
        events_after_first = len(self.lifecycle.history(opp_id))
        second = agent.run(self.comms.get_conversation(conv_id))
        self.assertFalse(any("drafted proposal" in a for a in second))
        self.assertEqual(len(self.lifecycle.history(opp_id)), events_after_first)

    def test_approval_is_caught_up_on_the_next_pass_after_a_real_hq_decision(self):
        opp_id, conv_id = self._pursue_conversation()
        self._agent().run(self.comms.get_conversation(conv_id))
        proposal = self.store.list("rh_proposals", "opportunity_id=?", (opp_id,))[0]
        pending = [i for i in self.needs_aryan.list_pending() if i["ref_type"] == "rh_proposal" and i["ref_id"] == proposal["id"]]
        self.assertEqual(len(pending), 1)

        # The exact same real decision path TTT HQ's Approve button uses --
        # never a private/simulated "mark approved" shortcut.
        decision = self.needs_aryan.decide(pending[0]["id"], "approve", "Aryan", note="looks good")
        apply_decision_side_effect(self.store, self.audit, pending[0], decision["action"], "Aryan")
        self.assertEqual(self.store.get("rh_proposals", proposal["id"])["status"], "APPROVED")
        self.assertEqual(self.store.get("rh_opportunities", opp_id)["lifecycle_state"], "AWAITING_APPROVAL")

        actions = self._agent().run(self.comms.get_conversation(conv_id))
        self.assertTrue(any("lifecycle -> APPROVED" in a for a in actions))
        self.assertEqual(self.store.get("rh_opportunities", opp_id)["lifecycle_state"], "APPROVED")

    def test_never_auto_transitions_to_won_or_lost(self):
        opp_id, conv_id = self._pursue_conversation()
        self._agent().run(self.comms.get_conversation(conv_id))
        self.assertNotIn(self.store.get("rh_opportunities", opp_id)["lifecycle_state"], {"WON", "LOST"})


class DiscoveryQuestionTests(_SalesLifecycleCase):
    def test_no_discovery_question_on_the_very_first_pass(self):
        opp_id, conv_id = self._weak_conversation_with_seeded_maybe()
        result = run_agent_for_conversation(self.store, self.audit, conv_id, needs_aryan=self.needs_aryan)
        self.assertFalse(any("discovery question" in a for a in result["actions"]))
        self.assertTrue(any("acknowledgement" in a for a in result["actions"]))

    def test_discovery_question_drafted_on_a_later_pass_when_gaps_remain(self):
        opp_id, conv_id = self._weak_conversation_with_seeded_maybe()
        run_agent_for_conversation(self.store, self.audit, conv_id, needs_aryan=self.needs_aryan)

        self.comms.add_message(conv_id, "INBOUND", "We're still figuring out the details, happy to discuss.", actor="website")
        result = run_agent_for_conversation(self.store, self.audit, conv_id, needs_aryan=self.needs_aryan)
        self.assertTrue(any("drafted discovery question" in a for a in result["actions"]))

        conv = self.comms.get_conversation(conv_id)
        sales_replies = [m for m in conv["messages"] if m["direction"] == "OUTBOUND" and m.get("sender_agent") == "AI Sales Rep" and not m.get("is_internal_note")]
        self.assertEqual(len(sales_replies), 1)
        self.assertIn("budget", sales_replies[0]["body"])
        self.assertEqual(self.store.get("rh_opportunities", opp_id)["lifecycle_state"], "RESEARCHING")

    def test_does_not_draft_a_second_discovery_question_on_a_repeat_pass(self):
        opp_id, conv_id = self._weak_conversation_with_seeded_maybe()
        run_agent_for_conversation(self.store, self.audit, conv_id, needs_aryan=self.needs_aryan)
        self.comms.add_message(conv_id, "INBOUND", "We're still figuring out the details.", actor="website")
        run_agent_for_conversation(self.store, self.audit, conv_id, needs_aryan=self.needs_aryan)
        second = run_agent_for_conversation(self.store, self.audit, conv_id, needs_aryan=self.needs_aryan)
        self.assertFalse(any("discovery question" in a for a in second["actions"]))

    def test_pursue_recommendation_skips_discovery_question_and_goes_straight_to_proposal(self):
        # Same missing fields, but a PURSUE-worthy opportunity: the
        # existing, already-tested proposal path must win, unchanged.
        opp_id, conv_id = self._pursue_conversation()
        result = run_agent_for_conversation(self.store, self.audit, conv_id, needs_aryan=self.needs_aryan)
        self.assertFalse(any("discovery question" in a for a in result["actions"]))
        self.assertTrue(any("drafted proposal" in a for a in result["actions"]))


class FeasibilityEscalationTests(_SalesLifecycleCase):
    def test_feasibility_question_escalates_to_engineering_instead_of_drafting_a_proposal(self):
        opp_id, conv_id = self._pursue_conversation()
        self.comms.add_message(
            conv_id, "INBOUND",
            "Before we go further -- is this technically feasible given our legacy system integration?",
            actor="website",
        )
        actions = self._agent().run(self.comms.get_conversation(conv_id))
        self.assertTrue(any("requested Engineering feasibility review" in a for a in actions))
        self.assertFalse(any("drafted proposal" in a for a in actions))
        self.assertEqual(self.store.list("rh_proposals", "opportunity_id=?", (opp_id,)), [])

        pending = self.needs_aryan.list_pending()
        self.assertTrue(any(
            i["kind"] == "communications_approval" and "[Engineering feasibility]" in i["title"] for i in pending
        ))

    def test_does_not_double_escalate_feasibility_on_a_second_pass(self):
        opp_id, conv_id = self._pursue_conversation()
        self.comms.add_message(
            conv_id, "INBOUND", "Is this feasible with our legacy system integration?", actor="website",
        )
        agent = self._agent()
        agent.run(self.comms.get_conversation(conv_id))
        second = agent.run(self.comms.get_conversation(conv_id))
        self.assertFalse(any("requested Engineering feasibility review" in a for a in second))

    def test_dispatcher_never_fabricates_a_proposal_while_feasibility_is_unresolved(self):
        # The real safety net: escalate() sets the conversation itself to
        # pending_approval, which is outside ACTIVE_STATUSES -- so through
        # the real entrypoint (run_agent_for_conversation), a second pass
        # is a full no-op, not just a "don't re-escalate" guard. No
        # proposal is ever drafted while Engineering hasn't weighed in.
        opp_id, conv_id = self._pursue_conversation()
        self.comms.add_message(
            conv_id, "INBOUND", "Is this feasible with our legacy system integration?", actor="website",
        )
        run_agent_for_conversation(self.store, self.audit, conv_id, needs_aryan=self.needs_aryan)
        self.assertEqual(self.comms.get_conversation(conv_id)["status"], "pending_approval")
        second = run_agent_for_conversation(self.store, self.audit, conv_id, needs_aryan=self.needs_aryan)
        self.assertEqual(second["actions"], [])
        self.assertEqual(self.store.list("rh_proposals", "opportunity_id=?", (opp_id,)), [])


class CommitmentLanguageLifecycleTests(_SalesLifecycleCase):
    def test_commitment_language_attempts_negotiating_transition_without_crashing(self):
        opp_id, conv_id = self._pursue_conversation()
        self.comms.add_message(conv_id, "INBOUND", "Great -- let's finalize, we agree to the final price.", actor="website")
        actions = self._agent().run(self.comms.get_conversation(conv_id))
        self.assertTrue(any("escalated pricing/contract commitment" in a for a in actions))
        # NEGOTIATING is only a legal transition from REPLIED/
        # DISCOVERY_CONVERSATION in the real graph. By this point the
        # opportunity has already reached QUALIFIED (via the qualify()
        # step that always runs first) -- QUALIFIED's only real edges are
        # PITCH_READY/LOST, so the best-effort NEGOTIATING attempt must be
        # a safe no-op, never a crash and never a fabricated jump.
        self.assertEqual(self.store.get("rh_opportunities", opp_id)["lifecycle_state"], "QUALIFIED")


if __name__ == "__main__":
    unittest.main()
