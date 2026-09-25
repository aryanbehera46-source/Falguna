"""Real, in-process tests for the TTT Communications V2 Outbound Approval
Engine (falguna/risk_engine.py). Same convention as
tests/test_comms_workforce.py: no mocking of the store, real temp SQLite DB
+ real AuditLog + real NeedsAryanQueue.

Covers: pure classification of the mission's own worked examples (LOW /
MEDIUM / HIGH), persistence of every classification as its own
comm_risk_events row, HIGH-risk auto-escalation through the single existing
NeedsAryanQueue, decision recording, and the no-second-approval-queue /
no-auto-send invariants.
"""

import tempfile
import unittest
from pathlib import Path

from falguna.audit import AuditLog
from falguna.risk_engine import RiskError, RiskClassificationStore, classify_message_risk
from falguna.store import StateStore
from falguna.ttt_hq import NeedsAryanQueue


class ClassifyMessageRiskTests(unittest.TestCase):
    """Pure function -- no store involved. Worked examples straight from the
    mission text for Milestone 2."""

    def test_low_risk_examples(self):
        examples = [
            "Thanks for reaching out -- acknowledging your message, we'll follow up shortly.",
            "Could you share your preferred timeline so we can plan next steps?",
            "What does your availability look like next week for a quick call?",
            "Here's the current status: your request is currently in progress.",
            "Happy to clarify -- let us know if you have further questions.",
        ]
        for body in examples:
            with self.subTest(body=body):
                self.assertEqual(classify_message_risk(body)["risk"], "LOW")

    def test_medium_risk_examples(self):
        examples = [
            "Here's our estimate for the project based on your requirements.",
            "As a workaround, you can disable that setting until the fix ships.",
            "This message is about your upcoming renewal -- let's discuss options.",
            "This is a minor scope change to the original requirements.",
        ]
        for body in examples:
            with self.subTest(body=body):
                self.assertEqual(classify_message_risk(body)["risk"], "MEDIUM")

    def test_high_risk_examples(self):
        examples = [
            "Please sign the contract attached and we'll get started.",
            "We're prepared to offer you the position starting next month.",
            "We can process a full refund for your last invoice.",
            "Our lawyer will be in touch regarding this legal action.",
            "This investment carries guaranteed returns for all participants.",
        ]
        for body in examples:
            with self.subTest(body=body):
                self.assertEqual(classify_message_risk(body)["risk"], "HIGH")

    def test_high_risk_wins_over_lower_signals_in_the_same_message(self):
        body = "Thanks for reaching out -- here's an update, and we agree to sign the contract today."
        result = classify_message_risk(body)
        self.assertEqual(result["risk"], "HIGH")

    def test_empty_or_no_signal_body_defaults_to_low_not_silently_unclassified(self):
        result = classify_message_risk("")
        self.assertEqual(result["risk"], "LOW")
        self.assertIn("no risk signal matched", result["reasons"][0])

    def test_context_policy_violation_forces_high_even_without_phrase_match(self):
        result = classify_message_risk(
            "Here is the number we discussed.",
            context={"final_price": 9999, "policy_violation": True},
        )
        self.assertEqual(result["risk"], "HIGH")


class RiskClassificationStoreTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = StateStore(root / "state.db")
        self.store.migrate()
        self.audit = AuditLog(root / "audit.jsonl")
        self.needs_aryan = NeedsAryanQueue(self.store, self.audit)
        self.risk = RiskClassificationStore(self.store, self.audit, self.needs_aryan)

    def test_low_risk_classification_is_persisted_without_escalation(self):
        result = self.risk.classify(
            "comm_message", "msg-low-1",
            "Thanks for reaching out -- here's an update on your request.",
            actor="ai_workforce",
        )
        self.assertEqual(result["risk"], "LOW")
        self.assertIsNone(result["needs_aryan_id"])
        rows = self.risk.list_for_subject("comm_message", "msg-low-1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["risk"], "LOW")
        self.assertIsNone(rows[0]["needs_aryan_id"])

    def test_medium_risk_classification_is_persisted_without_escalation(self):
        result = self.risk.classify(
            "comm_message", "msg-med-1",
            "Here's our estimate with a 10% discount applied.",
            actor="ai_workforce",
        )
        self.assertEqual(result["risk"], "MEDIUM")
        self.assertIsNone(result["needs_aryan_id"])

    def test_high_risk_classification_escalates_through_the_single_needs_aryan_queue(self):
        result = self.risk.classify(
            "comm_message", "msg-high-1",
            "We agree to sign the contract for a full refund.",
            actor="ai_workforce",
        )
        self.assertEqual(result["risk"], "HIGH")
        self.assertIsNotNone(result["needs_aryan_id"])

        pending = self.needs_aryan.list_pending()
        matched = [p for p in pending if p.get("id") == result["needs_aryan_id"]]
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]["kind"], "communications_approval")
        self.assertEqual(matched[0]["ref_type"], "comm_message")
        self.assertEqual(matched[0]["ref_id"], "msg-high-1")

        row = self.risk.list_for_subject("comm_message", "msg-high-1")[0]
        self.assertEqual(row["needs_aryan_id"], result["needs_aryan_id"])

    def test_record_decision_persists_final_action_and_audit_trail(self):
        result = self.risk.classify(
            "comm_message", "msg-high-2", "Please sign the contract today.", actor="ai_workforce",
        )
        decided = self.risk.record_decision(result["event_id"], "approved", "Aryan")
        self.assertEqual(decided["final_action"], "approved")
        self.assertEqual(decided["decided_by"], "Aryan")
        self.assertIsNotNone(decided["decided_at"])

    def test_record_decision_on_unknown_event_raises(self):
        with self.assertRaises(RiskError):
            self.risk.record_decision("does-not-exist", "approved", "Aryan")

    def test_list_recent_reflects_every_classification_regardless_of_risk_level(self):
        self.risk.classify("comm_message", "a", "Thanks for reaching out.", actor="ai_workforce")
        self.risk.classify("comm_message", "b", "Here's our estimate with a discount.", actor="ai_workforce")
        self.risk.classify("comm_message", "c", "We agree to sign the contract.", actor="ai_workforce")
        recent = self.risk.list_recent(10)
        self.assertEqual(len(recent), 3)

    def test_classification_never_marks_anything_sent_or_bypasses_draft_only_workflow(self):
        """Milestone 2's own invariant: risk classification adds visibility,
        not a new send path. Even a LOW-risk classification must never touch
        message status or create any kind of "sent" state on its own."""
        result = self.risk.classify(
            "comm_message", "msg-safe-1", "Thanks for reaching out.", actor="ai_workforce",
        )
        row = self.risk.list_for_subject("comm_message", "msg-safe-1")[0]
        self.assertIsNone(row["final_action"])
        self.assertIsNone(row["decided_by"])
        self.assertIsNone(row["decided_at"])


if __name__ == "__main__":
    unittest.main()
