"""Phase 5 Final Client Experience, Section 5 -- Minimum-Interruption /
Anti-Annoyance Coordinator. Includes Journey F from Section 27: several
missing requirements surfaced at once must produce ONE consolidated
clarification, never one message per question.
"""

import tempfile
import unittest
from pathlib import Path

from falguna.audit import AuditLog
from falguna.clarification_coordinator import ClarificationCoordinator
from falguna.comms import CommsStore
from falguna.language import LanguageInterpretationStore, unavailable_interpretation
from falguna.store import StateStore
from falguna.ttt_hq import NeedsAryanQueue


def _interpretation(questions, clarification_needed=True, unavailable=False):
    result = unavailable_interpretation("n/a") if unavailable else {
        "detected_languages": ["en"], "normalized_english_summary": "summary",
        "ambiguity_flags": [], "unresolved_questions": questions, "confidence": "Medium",
        "inferred_requirement_candidates": [], "clarification_needed": clarification_needed,
        "unavailable": False, "unavailable_reason": None, "model_call": None,
    }
    if not unavailable:
        result["unresolved_questions"] = questions
        result["clarification_needed"] = clarification_needed
    return result


class _CoordinatorTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = StateStore(root / "state.db")
        self.store.migrate()
        self.audit = AuditLog(root / "audit.jsonl")
        self.comms = CommsStore(self.store, self.audit, NeedsAryanQueue(self.store, self.audit))
        self.interpretations = LanguageInterpretationStore(self.store, self.audit)
        self.coordinator = ClarificationCoordinator(self.store, self.audit, self.comms, self.interpretations)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def _conversation(self):
        return self.comms.open_conversation("EMAIL", "sales", subject="New enquiry")["id"]

    def _inbound(self, conv_id, body):
        return self.comms.add_message(conv_id, "INBOUND", body)["id"]


class JourneyFMinimumInterruptionTests(_CoordinatorTestCase):
    def test_several_missing_requirements_produce_one_consolidated_message(self):
        conv_id = self._conversation()
        m1 = self._inbound(conv_id, "need app custmr book and staff see all payment later maybe")
        self.interpretations.record_for_message(m1, "raw", _interpretation([
            "Should customers be able to book for one branch or multiple branches?",
            "Do you want staff to see payment history, or only process new payments?",
        ]))
        m2 = self._inbound(conv_id, "also need it in hindi too")
        self.interpretations.record_for_message(m2, "raw", _interpretation([
            "Should the customer-facing app support Hindi in addition to English?",
        ]))

        assessment = self.coordinator.assess_conversation(conv_id)
        self.assertEqual(assessment["action"], "SEND_CONSOLIDATED_CLARIFICATION")
        self.assertEqual(len(assessment["consolidated_questions"]), 3)

        draft = self.coordinator.draft_consolidated_clarification(conv_id, actor="system")
        self.assertIsNotNone(draft)
        self.assertEqual(draft["status"], "DRAFT")
        for q in assessment["consolidated_questions"]:
            self.assertIn(q, draft["body"])
        # Exactly one new outbound message was created for all three questions.
        outbound = [m for m in self.store.list("comm_messages", "conversation_id=?", (conv_id,)) if m["direction"] == "OUTBOUND"]
        self.assertEqual(len(outbound), 1)

    def test_calling_draft_twice_before_send_does_not_duplicate(self):
        conv_id = self._conversation()
        m1 = self._inbound(conv_id, "need something idk what")
        self.interpretations.record_for_message(m1, "raw", _interpretation(["What problem are you trying to solve?"]))
        first = self.coordinator.draft_consolidated_clarification(conv_id)
        second = self.coordinator.draft_consolidated_clarification(conv_id)
        self.assertEqual(first["id"], second["id"])
        outbound = [m for m in self.store.list("comm_messages", "conversation_id=?", (conv_id,)) if m["direction"] == "OUTBOUND"]
        self.assertEqual(len(outbound), 1)


class NoActionNeededTests(_CoordinatorTestCase):
    def test_no_outstanding_questions_means_no_action_needed(self):
        conv_id = self._conversation()
        m1 = self._inbound(conv_id, "please build me a simple landing page, nothing else")
        self.interpretations.record_for_message(m1, "raw", _interpretation([], clarification_needed=False))
        assessment = self.coordinator.assess_conversation(conv_id)
        self.assertEqual(assessment["action"], "NO_ACTION_NEEDED")
        draft = self.coordinator.draft_consolidated_clarification(conv_id)
        self.assertIsNone(draft)
        outbound = [m for m in self.store.list("comm_messages", "conversation_id=?", (conv_id,)) if m["direction"] == "OUTBOUND"]
        self.assertEqual(outbound, [])

    def test_a_question_already_answered_in_an_outbound_message_is_not_reasked(self):
        conv_id = self._conversation()
        m1 = self._inbound(conv_id, "need booking app")
        self.interpretations.record_for_message(m1, "raw", _interpretation([
            "Should this work for one branch or multiple branches?",
        ]))
        # Someone (a human or another part of the system) already asked
        # this exact question in a prior outbound message.
        self.comms.add_message(conv_id, "OUTBOUND", "Quick question: should this work for one branch or multiple branches?")
        assessment = self.coordinator.assess_conversation(conv_id)
        self.assertEqual(assessment["action"], "NO_ACTION_NEEDED")

    def test_unavailable_interpretations_are_never_treated_as_clarification_needed(self):
        conv_id = self._conversation()
        m1 = self._inbound(conv_id, "some message the interpreter could not reach a model for")
        self.interpretations.record_for_message(m1, "raw", unavailable_interpretation("provider offline"))
        assessment = self.coordinator.assess_conversation(conv_id)
        # An unavailable interpretation is not silently treated as "ask a
        # vague clarification" -- it simply contributes no questions, since
        # unresolved_questions on an unavailable result is always [].
        self.assertEqual(assessment["consolidated_questions"], [])

    def test_duplicate_identical_questions_across_messages_are_deduped(self):
        conv_id = self._conversation()
        m1 = self._inbound(conv_id, "first message")
        self.interpretations.record_for_message(m1, "raw", _interpretation(["Which region should this serve?"]))
        m2 = self._inbound(conv_id, "second message, still vague")
        self.interpretations.record_for_message(m2, "raw", _interpretation(["which region should this serve?"]))
        assessment = self.coordinator.assess_conversation(conv_id)
        self.assertEqual(len(assessment["consolidated_questions"]), 1)

    def test_assess_conversation_raises_for_unknown_conversation(self):
        with self.assertRaises(ValueError):
            self.coordinator.assess_conversation("not-a-real-id")


if __name__ == "__main__":
    unittest.main()
