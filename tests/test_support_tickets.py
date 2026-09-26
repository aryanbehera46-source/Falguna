"""Tests for TTT Communications V2 Milestone 5 (support ticket workflow):
CommsStore.set_ticket_status/resolve_ticket, and the SupportAgent's
NEW -> TRIAGED -> WAITING_INTERNAL -> WAITING_CUSTOMER progression, plus
the "never auto-resolve" invariant."""

import tempfile
import unittest
from pathlib import Path

from falguna.audit import AuditLog
from falguna.comms import CommsError, CommsStore
from falguna.comms_workforce import SupportAgent, run_agent_for_conversation
from falguna.store import StateStore
from falguna.ttt_hq import NeedsAryanQueue


class _TicketCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = StateStore(root / "state.db")
        self.store.migrate()
        self.audit = AuditLog(root / "audit.jsonl")
        self.needs_aryan = NeedsAryanQueue(self.store, self.audit)
        self.comms = CommsStore(self.store, self.audit, self.needs_aryan)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def _support_conversation(self, body):
        conv = self.comms.open_conversation("SUPPORT", "support", priority="normal", actor="website")
        self.comms.add_message(conv["id"], "INBOUND", body, actor="website")
        return conv["id"]


class TicketStatusStoreTests(_TicketCase):
    def test_set_ticket_status_validates_and_records_history(self):
        conv_id = self._support_conversation("hi")
        self.comms.set_ticket_status(conv_id, "TRIAGED", "Aryan", reason="looked at it")
        conv = self.comms.get_conversation(conv_id)
        self.assertEqual(conv["ticket_status"], "TRIAGED")
        events = [h for h in conv["history"] if h["field"] == "ticket_status"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["new_value"], "TRIAGED")

    def test_invalid_ticket_status_is_rejected(self):
        conv_id = self._support_conversation("hi")
        with self.assertRaises(CommsError):
            self.comms.set_ticket_status(conv_id, "NOT_A_REAL_STATUS", "Aryan")

    def test_mark_message_sent_flips_to_waiting_customer(self):
        conv_id = self._support_conversation("hi")
        self.comms.set_ticket_status(conv_id, "WAITING_INTERNAL", "ai_workforce")
        msg = self.comms.add_message(conv_id, "OUTBOUND", "Here's the answer.", actor="ai_workforce")
        self.comms.mark_message_sent(msg["id"], "Aryan")
        conv = self.comms.get_conversation(conv_id)
        self.assertEqual(conv["ticket_status"], "WAITING_CUSTOMER")

    def test_mark_message_sent_never_overrides_a_resolved_or_closed_ticket(self):
        conv_id = self._support_conversation("hi")
        msg = self.comms.add_message(conv_id, "OUTBOUND", "Closing note.", actor="ai_workforce")
        self.comms.resolve_ticket(conv_id, "Aryan", "Confirmed fixed after redeploy; customer verified access restored.")
        self.comms.mark_message_sent(msg["id"], "Aryan")
        conv = self.comms.get_conversation(conv_id)
        self.assertEqual(conv["ticket_status"], "RESOLVED")


class ResolveTicketTests(_TicketCase):
    def test_resolve_ticket_requires_a_real_resolution_note(self):
        conv_id = self._support_conversation("hi")
        with self.assertRaises(CommsError):
            self.comms.resolve_ticket(conv_id, "Aryan", "")
        with self.assertRaises(CommsError):
            self.comms.resolve_ticket(conv_id, "Aryan", "   ")

    def test_resolve_ticket_sets_both_ticket_status_and_coarse_status(self):
        conv_id = self._support_conversation("hi")
        self.comms.resolve_ticket(conv_id, "Aryan", "Root cause was a stale cache entry; cleared and verified with customer.")
        conv = self.comms.get_conversation(conv_id)
        self.assertEqual(conv["ticket_status"], "RESOLVED")
        self.assertEqual(conv["status"], "resolved")
        self.assertIsNotNone(conv["resolved_at"])
        notes = [m for m in conv["messages"] if m.get("is_internal_note")]
        self.assertTrue(any("stale cache entry" in n["body"] for n in notes))

    def test_no_ai_agent_code_path_ever_calls_resolve_ticket(self):
        # Structural guarantee for "never invent a technical resolution":
        # resolve_ticket is never referenced anywhere in the workforce module.
        import inspect
        from falguna import comms_workforce
        source = inspect.getsource(comms_workforce)
        self.assertNotIn("resolve_ticket", source)


class SupportAgentTicketProgressionTests(_TicketCase):
    def _agent(self):
        return SupportAgent(self.store, self.audit, self.comms, self.needs_aryan)

    def test_first_touch_sets_new_then_triaged_before_drafting(self):
        conv_id = self._support_conversation("What exactly is included in the support plan?")
        actions = self._agent().run(self.comms.get_conversation(conv_id))
        self.assertTrue(any("ticket status set to NEW" in a for a in actions))
        self.assertTrue(any("ticket status set to TRIAGED" in a for a in actions))
        conv = self.comms.get_conversation(conv_id)
        self.assertEqual(conv["ticket_status"], "WAITING_INTERNAL")  # drafted, awaiting send

    def test_billing_department_escalation_is_tagged_billing(self):
        conv = self.comms.open_conversation("SUPPORT", "billing", priority="normal", actor="website")
        self.comms.add_message(conv["id"], "INBOUND", "unsubscribe, this is an automated message, no-reply.", actor="website")
        actions = self._agent().run(self.comms.get_conversation(conv["id"]))
        self.assertTrue(any("escalated to Billing" in a for a in actions))

    def test_sensitive_content_escalation_is_tagged_executive(self):
        conv_id = self._support_conversation("please send my social security number confirmation")
        actions = self._agent().run(self.comms.get_conversation(conv_id))
        self.assertTrue(any("escalated to Executive" in a for a in actions))
        conv = self.comms.get_conversation(conv_id)
        self.assertEqual(conv["ticket_status"], "WAITING_INTERNAL")

    def test_full_lifecycle_through_the_dispatcher_ends_waiting_customer_after_send(self):
        conv_id = self._support_conversation("What exactly is included in the support plan?")
        # First pass: the Receptionist's own acknowledgement is the first
        # customer-visible reply (same composition as every other
        # department) -- the ticket is opened (NEW) but Support hasn't
        # triaged yet, since the customer hasn't said anything since.
        run_agent_for_conversation(self.store, self.audit, conv_id, needs_aryan=self.needs_aryan)
        conv = self.comms.get_conversation(conv_id)
        self.assertEqual(conv["ticket_status"], "NEW")
        first_reply = [m for m in conv["messages"] if m["direction"] == "OUTBOUND" and not m.get("is_internal_note")][0]
        self.comms.mark_message_sent(first_reply["id"], "Aryan")

        # Customer follows up -- now Support actually triages and drafts.
        self.comms.add_message(conv_id, "INBOUND", "Specifically, does the support plan cover weekend incidents and holidays as well, or only weekday business hours?", actor="website")
        run_agent_for_conversation(self.store, self.audit, conv_id, needs_aryan=self.needs_aryan)
        conv = self.comms.get_conversation(conv_id)
        self.assertEqual(conv["ticket_status"], "WAITING_INTERNAL")
        second_reply = [m for m in conv["messages"] if m["direction"] == "OUTBOUND" and not m.get("is_internal_note")][-1]
        self.comms.mark_message_sent(second_reply["id"], "Aryan")
        conv = self.comms.get_conversation(conv_id)
        self.assertEqual(conv["ticket_status"], "WAITING_CUSTOMER")


if __name__ == "__main__":
    unittest.main()
