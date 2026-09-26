"""TTT Communications V2, Milestone 9 -- Failure, Recovery and Durability.

These tests prove the mission's explicit invariants hold across the real
failure modes it lists, using real temp SQLite DBs and a real
close()/reopen() cycle rather than mocking persistence away:
  - no silent data loss
  - no false SENT status
  - no false RESOLVED status
  - state transitions persist correctly across a restart

Duplicate inbound / replayed provider events, malformed attachments, and
unauthorized customer scope are already covered exhaustively in
tests/test_email_ingestion.py and tests/test_customer_context.py -- not
duplicated here. Provider-unavailable/missing-credentials are covered in
tests/test_email_admin.py; this file adds the one scenario that wasn't
explicitly named there (a provider reconnecting once configuration is
fixed, no restart required).
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from falguna.audit import AuditLog
from falguna.comms import CommsError, CommsStore
from falguna.comms_workforce import run_agent_for_conversation
from falguna.email_admin import SMTPEmailProvider
from falguna.email_ingestion import EmailIngestionService
from falguna.revenue_hunter import FollowupStore, OpportunityStore
from falguna.risk_engine import RiskClassificationStore
from falguna.store import StateStore
from falguna.ttt_hq import NeedsAryanQueue


class _DurabilityCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.db_path = root / "state.db"
        self.audit_path = root / "audit.jsonl"
        self.store = StateStore(self.db_path)
        self.store.migrate()
        self.audit = AuditLog(self.audit_path)
        self.needs_aryan = NeedsAryanQueue(self.store, self.audit)
        self.comms = CommsStore(self.store, self.audit, self.needs_aryan)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def _reopen(self):
        """The real restart: close this handle, open a brand new
        StateStore against the same file, migrate it again (exactly what
        a real process restart does), and swap every dependent object over
        to it -- nothing here is simulated in memory."""
        self.store.close()
        self.store = StateStore(self.db_path)
        self.store.migrate()
        self.audit = AuditLog(self.audit_path)
        self.needs_aryan = NeedsAryanQueue(self.store, self.audit)
        self.comms = CommsStore(self.store, self.audit, self.needs_aryan)


class FailedSendInvariantTests(_DurabilityCase):
    def test_failed_send_never_produces_a_sent_status(self):
        conv = self.comms.open_conversation("EMAIL", "sales", priority="normal")
        msg = self.comms.add_message(conv["id"], "OUTBOUND", "Draft reply", actor="ai_workforce")
        self.comms.mark_message_failed(msg["id"], "system", reason="SMTP connection refused")
        stored = self.store.get("comm_messages", msg["id"])
        self.assertEqual(stored["status"], "FAILED")
        self.assertNotEqual(stored["status"], "SENT")

    def test_failed_send_does_not_falsely_advance_ticket_status(self):
        # Only a real SENT reply puts the ball in the customer's court
        # (mark_message_sent's WAITING_CUSTOMER auto-flip, Milestone 5) --
        # a failed attempt must never claim the same thing happened.
        conv = self.comms.open_conversation("EMAIL", "support", priority="normal")
        self.comms.add_message(conv["id"], "INBOUND", "Question", actor="website")
        self.comms.set_ticket_status(conv["id"], "TRIAGED", actor="ai_workforce")
        msg = self.comms.add_message(conv["id"], "OUTBOUND", "Draft reply", actor="ai_workforce")
        self.comms.mark_message_failed(msg["id"], "system", reason="provider timeout")
        conv_after = self.store.get("comm_conversations", conv["id"])
        self.assertEqual(conv_after["ticket_status"], "TRIAGED")
        self.assertNotEqual(conv_after["ticket_status"], "WAITING_CUSTOMER")

    def test_a_failed_message_can_still_be_retried_only_through_a_fresh_draft(self):
        # No silent overwrite/resurrection of a FAILED message -- the
        # honest recovery path is a new draft, never mutating the failed
        # one back to DRAFT/SENT behind the scenes.
        conv = self.comms.open_conversation("EMAIL", "sales", priority="normal")
        msg = self.comms.add_message(conv["id"], "OUTBOUND", "Draft reply", actor="ai_workforce")
        self.comms.mark_message_failed(msg["id"], "system", reason="provider timeout")
        with self.assertRaises(CommsError):
            self.comms.mark_message_sent(msg["id"], "Aryan")
        retry = self.comms.add_message(conv["id"], "OUTBOUND", "Draft reply (retry)", actor="ai_workforce")
        self.comms.mark_message_sent(retry["id"], "Aryan")
        self.assertEqual(self.store.get("comm_messages", msg["id"])["status"], "FAILED")
        self.assertEqual(self.store.get("comm_messages", retry["id"])["status"], "SENT")


class ProviderReconnectTests(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ("SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD")}
        for k in self._saved:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_the_same_provider_instance_recovers_once_configuration_is_fixed(self):
        # "Provider reconnect" in a codebase with no live mailbox this
        # sprint: config missing -> unhealthy, config fixed -> healthy,
        # with no restart and no new object required.
        provider = SMTPEmailProvider()
        self.assertFalse(provider.health_check()["healthy"])
        os.environ.update({
            "SMTP_HOST": "smtp.example-test.invalid", "SMTP_PORT": "587",
            "SMTP_USERNAME": "bot@example-test.invalid", "SMTP_PASSWORD": "fake-not-real",
        })
        with patch("falguna.email_admin.smtplib.SMTP") as mock_smtp:
            mock_smtp.return_value.__enter__.return_value.ehlo.return_value = None
            result = provider.health_check()
        self.assertTrue(result["healthy"])


class RestartDuringIngestionTests(_DurabilityCase):
    PAYLOAD = {
        "provider_message_id": "durability-msg-1", "thread_id": None,
        "from_address": "buyer@durability-test.invalid", "to_address": "sales@twentytwotechnologies.com",
        "subject": "Project enquiry", "body": "We'd like a quote for a booking platform rebuild.",
        "received_at": "2026-01-01T00:00:00Z", "in_reply_to_message_id": None, "attachments": [],
    }

    def test_ingested_conversation_and_dedup_key_survive_a_restart(self):
        ingestion = EmailIngestionService(self.store, self.audit, self.comms)
        result = ingestion.ingest(dict(self.PAYLOAD))
        self.assertEqual(result["status"], "ingested")
        conv_id = result["conversation_id"]

        self._reopen()

        recovered_conv = self.store.get("comm_conversations", conv_id)
        self.assertIsNotNone(recovered_conv)
        # One INBOUND (the ingested email) plus the AI workforce's own
        # first-touch acknowledgement, dispatched as part of ingest() --
        # both must survive the restart intact.
        recovered_messages = self.store.list("comm_messages", "conversation_id=?", (conv_id,))
        self.assertEqual(len(recovered_messages), 2)
        inbound = [m for m in recovered_messages if m["direction"] == "INBOUND"]
        self.assertEqual(len(inbound), 1)
        self.assertEqual(inbound[0]["provider_message_id"], "durability-msg-1")

        # A provider retry of the exact same message after the restart
        # must still be recognized as a duplicate, not re-ingested.
        ingestion_after_restart = EmailIngestionService(self.store, self.audit, self.comms)
        replay = ingestion_after_restart.ingest(dict(self.PAYLOAD))
        self.assertEqual(replay["status"], "duplicate")
        self.assertEqual(len(self.store.list("comm_messages", "conversation_id=?", (conv_id,))), 2)

    def test_restart_mid_ai_processing_leaves_no_partial_draft_confusion(self):
        # Ingest (which itself dispatches the AI workforce once) captures a
        # real snapshot of dispatcher output, then a restart must show the
        # exact same persisted state, never a half-written draft.
        ingestion = EmailIngestionService(self.store, self.audit, self.comms)
        result = ingestion.ingest(dict(self.PAYLOAD))
        conv_id = result["conversation_id"]
        before = self.store.list("comm_messages", "conversation_id=?", (conv_id,))

        self._reopen()

        after = self.store.list("comm_messages", "conversation_id=?", (conv_id,))
        self.assertEqual({m["id"] for m in before}, {m["id"] for m in after})
        self.assertEqual({m["status"] for m in before}, {m["status"] for m in after})


class RestartWithApprovalPendingTests(_DurabilityCase):
    def test_pending_approval_survives_restart_and_can_still_be_decided(self):
        conv = self.comms.open_conversation("EMAIL", "sales", priority="normal")
        msg = self.comms.add_message(conv["id"], "OUTBOUND", "We agree to those contract terms.", actor="ai_workforce")
        risk = RiskClassificationStore(self.store, self.audit, self.needs_aryan)
        result = risk.classify("comm_message", msg["id"], msg["body"], actor="ai_workforce", title="review")
        self.assertEqual(result["risk"], "HIGH")
        pending = self.needs_aryan.list_pending()
        self.assertEqual(len(pending), 1)
        item_id = pending[0]["id"]

        self._reopen()

        still_pending = self.needs_aryan.list_pending()
        self.assertTrue(any(i["id"] == item_id for i in still_pending))
        decision = self.needs_aryan.decide(item_id, "approve", "Aryan", note="reviewed after restart")
        self.assertEqual(decision["action"], "APPROVED")
        self.assertEqual(self.store.get("needs_aryan_items", item_id)["status"], "APPROVED")


class RestartWithFollowupDueTests(_DurabilityCase):
    def test_followup_survives_restart_and_can_still_be_marked_sent(self):
        opportunities = OpportunityStore(self.store, self.audit)
        opp_id = opportunities.create({"title": "Website revamp"}, actor="website")
        followups = FollowupStore(self.store, self.audit)
        followup_id = followups.generate(opp_id, "response_followup")

        self._reopen()

        recovered = self.store.get("rh_followups", followup_id)
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered["status"], "DRAFT")
        FollowupStore(self.store, self.audit).mark_sent(followup_id, "Aryan")
        self.assertEqual(self.store.get("rh_followups", followup_id)["status"], "SENT")


class StaleConversationAssignmentTests(_DurabilityCase):
    def test_the_real_dispatcher_never_reassigns_or_duplicates_the_assignment_across_many_passes(self):
        conv = self.comms.open_conversation("SUPPORT", "support", priority="normal")
        self.comms.add_message(conv["id"], "INBOUND", "What's included in the support plan?", actor="website")
        for _ in range(5):
            run_agent_for_conversation(self.store, self.audit, conv["id"], needs_aryan=self.needs_aryan)
        conv_after = self.store.get("comm_conversations", conv["id"])
        self.assertEqual(conv_after["assigned_agent"], "AI Support Rep")
        assignment_events = self.store.list(
            "comm_status_events", "conversation_id=? AND field=?", (conv["id"], "assigned_agent"),
        )
        # Exactly the real one-time handoff -- Receptionist's own
        # placeholder self-assign, then Support Rep claiming it -- never
        # more, however many times the dispatcher re-runs afterward.
        self.assertEqual(len(assignment_events), 2)
        self.assertEqual(assignment_events[0]["new_value"], "AI Receptionist")
        self.assertEqual(assignment_events[1]["old_value"], "AI Receptionist")
        self.assertEqual(assignment_events[1]["new_value"], "AI Support Rep")
        participants = [
            p for p in self.store.list("comm_participants", "conversation_id=?", (conv["id"],))
            if p["participant_type"] == "agent"
        ]
        self.assertEqual(len(participants), 2)

    def test_an_explicit_reassignment_to_a_different_role_is_recorded_not_silently_dropped(self):
        conv = self.comms.open_conversation("SUPPORT", "support", priority="normal")
        self.comms.assign(conv["id"], "AI Support Rep", "Aryan")
        self.comms.assign(conv["id"], "AI Account Manager", "Aryan")
        conv_after = self.store.get("comm_conversations", conv["id"])
        self.assertEqual(conv_after["assigned_agent"], "AI Account Manager")
        history = self.store.list(
            "comm_status_events", "conversation_id=? AND field=?", (conv["id"], "assigned_agent"),
        )
        self.assertEqual(len(history), 2)
        self.assertEqual(history[-1]["old_value"], "AI Support Rep")
        self.assertEqual(history[-1]["new_value"], "AI Account Manager")


class PartialOperationFailureTests(_DurabilityCase):
    def test_a_failed_escalation_leaves_the_conversation_status_unchanged(self):
        conv = self.comms.open_conversation("EMAIL", "sales", priority="normal")
        with self.assertRaises(ValueError):
            self.comms.escalate(conv["id"], "t", "w", actor="system", kind="not-a-real-kind")
        conv_after = self.store.get("comm_conversations", conv["id"])
        self.assertEqual(conv_after["status"], "new")
        self.assertEqual(self.needs_aryan.list_pending(), [])

    def test_a_failed_resolve_ticket_leaves_ticket_status_unchanged(self):
        conv = self.comms.open_conversation("SUPPORT", "support", priority="normal")
        self.comms.set_ticket_status(conv["id"], "IN_PROGRESS", actor="Aryan")
        with self.assertRaises(CommsError):
            self.comms.resolve_ticket(conv["id"], actor="Aryan", resolution_note="")
        conv_after = self.store.get("comm_conversations", conv["id"])
        self.assertEqual(conv_after["ticket_status"], "IN_PROGRESS")
        self.assertNotEqual(conv_after["ticket_status"], "RESOLVED")


class NoFalseTerminalStatusAcrossRestartTests(_DurabilityCase):
    def test_ticket_status_never_reaches_resolved_without_evidence_even_after_a_restart(self):
        conv = self.comms.open_conversation("SUPPORT", "support", priority="normal")
        self.comms.set_ticket_status(conv["id"], "IN_PROGRESS", actor="Aryan")

        self._reopen()

        with self.assertRaises(CommsError):
            self.comms.resolve_ticket(conv["id"], actor="Aryan", resolution_note="   ")
        self.assertEqual(self.store.get("comm_conversations", conv["id"])["ticket_status"], "IN_PROGRESS")
        # The real, evidence-bearing resolution still works after restart.
        self.comms.resolve_ticket(conv["id"], actor="Aryan", resolution_note="Confirmed fixed via patch 1.2.3.")
        self.assertEqual(self.store.get("comm_conversations", conv["id"])["ticket_status"], "RESOLVED")


if __name__ == "__main__":
    unittest.main()
