"""Tests for falguna/conversations.py -- the Conversation Inbox and Reply
Agent (Section 6)."""

import tempfile
import unittest
from pathlib import Path

from falguna.audit import AuditLog
from falguna.conversations import (
    ConversationError,
    ConversationStore,
    ReplyAgent,
    classify_intent,
)
from falguna.lifecycle import LifecycleOrchestrator
from falguna.revenue_hunter import OpportunityStore, ProposalStore, QualificationStore
from falguna.opportunity_agent import build_qualification_engine
from falguna.sales_ops import SalesPolicyStore
from falguna.store import StateStore
from falguna.ttt_hq import NeedsAryanQueue


class ClassifyIntentTests(unittest.TestCase):
    def test_rejection(self):
        self.assertEqual(classify_intent("Thanks but we've decided to go with someone else."), "rejection")

    def test_meeting_request(self):
        self.assertEqual(classify_intent("Can we schedule a call this week?"), "meeting_request")

    def test_price_objection(self):
        self.assertEqual(classify_intent("This is a bit too expensive for us."), "price_objection")

    def test_timeline_objection(self):
        self.assertEqual(classify_intent("We need it sooner than that, can you deliver sooner?"), "timeline_objection")

    def test_scope_clarification(self):
        self.assertEqual(classify_intent("Does this include ongoing maintenance? What's included exactly?"), "scope_clarification")

    def test_requirement_request(self):
        self.assertEqual(classify_intent("Can you share some case studies from past clients?"), "requirement_request")

    def test_question_fallback_on_bare_question_mark(self):
        self.assertEqual(classify_intent("How long have you been doing this?"), "question")

    def test_interested(self):
        self.assertEqual(classify_intent("Sounds good, let's move forward."), "interested")

    def test_spam(self):
        self.assertEqual(classify_intent("This is an automated message, please do not reply."), "spam_irrelevant")

    def test_other_fallback(self):
        self.assertEqual(classify_intent("Thanks."), "other")

    def test_empty_body_is_other(self):
        self.assertEqual(classify_intent(""), "other")

    def test_priority_rejection_wins_over_incidental_question_mark(self):
        self.assertEqual(classify_intent("Are you kidding? We've decided to go with someone else."), "rejection")


class ReplyAgentTests(unittest.TestCase):
    def setUp(self):
        self.agent = ReplyAgent()
        self.opportunity = {"title": "Build a CRM dashboard"}
        self.policy = {"allowed_payment_terms": ["50% upfront, 50% on delivery"]}

    def test_interested_reply_mentions_title(self):
        reply = self.agent.generate_reply("interested", self.opportunity, None, "some proposal text", self.policy)
        self.assertIn("Build a CRM dashboard", reply)

    def test_price_objection_never_invents_a_number_not_on_file(self):
        reply = self.agent.generate_reply("price_objection", self.opportunity, {"suggested_price": "$1,500"}, None, self.policy)
        self.assertIn("$1,500", reply)

    def test_price_objection_without_a_suggested_price_does_not_fabricate_one(self):
        reply = self.agent.generate_reply("price_objection", self.opportunity, None, None, {})
        self.assertNotIn("$", reply)

    def test_spam_gets_no_drafted_reply(self):
        reply = self.agent.generate_reply("spam_irrelevant", self.opportunity, None, None, {})
        self.assertIsNone(reply)

    def test_other_gets_no_drafted_reply(self):
        reply = self.agent.generate_reply("other", self.opportunity, None, None, {})
        self.assertIsNone(reply)

    def test_every_real_intent_except_spam_and_other_gets_a_reply(self):
        for intent in ["interested", "question", "scope_clarification", "price_objection",
                       "timeline_objection", "meeting_request", "requirement_request", "rejection"]:
            with self.subTest(intent=intent):
                reply = self.agent.generate_reply(intent, self.opportunity, None, None, {})
                self.assertIsNotNone(reply)


class ConversationStoreTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = StateStore(root / "state.db")
        self.store.migrate()
        self.audit = AuditLog(root / "audit.jsonl")
        self.orchestrator = LifecycleOrchestrator(self.store, self.audit)
        self.needs_aryan = NeedsAryanQueue(self.store, self.audit)
        self.opportunities = OpportunityStore(self.store, self.audit, orchestrator=self.orchestrator)
        self.conversations = ConversationStore(self.store, self.audit, orchestrator=self.orchestrator, needs_aryan=self.needs_aryan)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _opportunity(self):
        return self.opportunities.create({"title": "Build a CRM dashboard", "description": "d"}, actor="Aryan")


class RecordInboundTests(ConversationStoreTestBase):
    def test_unknown_opportunity_raises(self):
        with self.assertRaises(ConversationError):
            self.conversations.record_inbound("does-not-exist", "email", "hello")

    def test_empty_body_raises(self):
        opp_id = self._opportunity()
        with self.assertRaises(ConversationError):
            self.conversations.record_inbound(opp_id, "email", "   ")

    def test_records_a_message_with_classified_intent(self):
        opp_id = self._opportunity()
        result = self.conversations.record_inbound(opp_id, "email", "Sounds good, let's move forward.")
        self.assertEqual(result["intent"], "interested")
        message = self.store.get("rh_conversation_messages", result["message_id"])
        self.assertEqual(message["direction"], "INBOUND")
        self.assertEqual(message["status"], "RECEIVED")

    def test_advances_lifecycle_to_replied(self):
        opp_id = self._opportunity()
        self.orchestrator.transition(opp_id, "RESEARCHING", actor="Aryan")
        self.orchestrator.transition(opp_id, "QUALIFIED", actor="Aryan")
        self.orchestrator.transition(opp_id, "PITCH_READY", actor="Aryan")
        self.orchestrator.transition(opp_id, "AWAITING_APPROVAL", actor="Aryan")
        self.orchestrator.transition(opp_id, "APPROVED", actor="Aryan")
        self.orchestrator.transition(opp_id, "APPLYING", actor="Aryan")
        self.orchestrator.transition(opp_id, "CONTACTED", actor="Aryan")
        self.conversations.record_inbound(opp_id, "email", "Sounds good, let's move forward.")
        self.assertEqual(self.orchestrator.current_state(opp_id), "REPLIED")

    def test_never_raises_even_when_lifecycle_cannot_reach_replied(self):
        opp_id = self._opportunity()  # still DISCOVERED
        result = self.conversations.record_inbound(opp_id, "email", "Sounds good.")
        self.assertIsNotNone(result["message_id"])  # did not crash

    def test_sensitive_content_escalates_to_needs_aryan(self):
        opp_id = self._opportunity()
        before = len(self.store.list("needs_aryan_items"))
        result = self.conversations.record_inbound(opp_id, "email", "Just a heads up, my attorney says we may pursue legal action.")
        self.assertIsNotNone(result["needs_aryan_id"])
        after = len(self.store.list("needs_aryan_items"))
        self.assertEqual(after, before + 1)
        message = self.store.get("rh_conversation_messages", result["message_id"])
        self.assertEqual(message["status"], "NEEDS_REVIEW")

    def test_ordinary_content_does_not_escalate(self):
        opp_id = self._opportunity()
        result = self.conversations.record_inbound(opp_id, "email", "Sounds good, let's move forward.")
        self.assertIsNone(result["needs_aryan_id"])

    def test_evidence_is_persisted(self):
        opp_id = self._opportunity()
        result = self.conversations.record_inbound(opp_id, "email", "Sounds good.", evidence={"raw_headers": "..."})
        message = self.store.get("rh_conversation_messages", result["message_id"])
        self.assertIn("raw_headers", message["evidence_json"])


class DraftReplyTests(ConversationStoreTestBase):
    def test_unknown_message_raises(self):
        with self.assertRaises(ConversationError):
            self.conversations.draft_reply("does-not-exist")

    def test_drafts_a_reply_for_a_real_intent(self):
        opp_id = self._opportunity()
        inbound = self.conversations.record_inbound(opp_id, "email", "Sounds good, let's move forward.")
        drafted = self.conversations.draft_reply(inbound["message_id"])
        self.assertIsNotNone(drafted)
        message = self.store.get("rh_conversation_messages", drafted["message_id"])
        self.assertEqual(message["direction"], "OUTBOUND")
        self.assertEqual(message["status"], "DRAFT")

    def test_no_draft_for_spam(self):
        opp_id = self._opportunity()
        inbound = self.conversations.record_inbound(opp_id, "email", "This is an automated message.")
        drafted = self.conversations.draft_reply(inbound["message_id"])
        self.assertIsNone(drafted)

    def test_reply_uses_approved_proposal_context_when_available(self):
        opp_id = self._opportunity()
        quals = QualificationStore(self.store, self.audit, build_qualification_engine({}))
        quals.qualify(opp_id, actor="Aryan")
        proposals = ProposalStore(self.store, self.audit, self.needs_aryan)
        drafted_proposal = proposals.generate(opp_id, "short", actor="Aryan")
        proposals.mark_approved(drafted_proposal["proposal_id"], "Aryan")
        inbound = self.conversations.record_inbound(opp_id, "email", "Sounds good, let's move forward.")
        drafted = self.conversations.draft_reply(inbound["message_id"])
        self.assertIn("proposal I sent over", drafted["body"])


class MarkSentTests(ConversationStoreTestBase):
    def test_unknown_message_raises(self):
        with self.assertRaises(ConversationError):
            self.conversations.mark_sent("does-not-exist", "Aryan")

    def test_marks_a_draft_sent(self):
        opp_id = self._opportunity()
        inbound = self.conversations.record_inbound(opp_id, "email", "Sounds good.")
        drafted = self.conversations.draft_reply(inbound["message_id"])
        result = self.conversations.mark_sent(drafted["message_id"], "Aryan")
        self.assertEqual(result["status"], "SENT")

    def test_cannot_mark_an_inbound_message_sent(self):
        opp_id = self._opportunity()
        inbound = self.conversations.record_inbound(opp_id, "email", "Sounds good.")
        with self.assertRaises(ConversationError):
            self.conversations.mark_sent(inbound["message_id"], "Aryan")

    def test_cannot_mark_an_already_sent_message_sent_again(self):
        opp_id = self._opportunity()
        inbound = self.conversations.record_inbound(opp_id, "email", "Sounds good.")
        drafted = self.conversations.draft_reply(inbound["message_id"])
        self.conversations.mark_sent(drafted["message_id"], "Aryan")
        with self.assertRaises(ConversationError):
            self.conversations.mark_sent(drafted["message_id"], "Aryan")


class ListForOpportunityTests(ConversationStoreTestBase):
    def test_lists_all_messages_in_order(self):
        opp_id = self._opportunity()
        inbound = self.conversations.record_inbound(opp_id, "email", "Sounds good, let's move forward.")
        self.conversations.draft_reply(inbound["message_id"])
        messages = self.conversations.list_for_opportunity(opp_id)
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]["direction"], "INBOUND")
        self.assertEqual(messages[1]["direction"], "OUTBOUND")


if __name__ == "__main__":
    unittest.main()
