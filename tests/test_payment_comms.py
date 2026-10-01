"""Tests for falguna/payment_comms.py -- Phase 5 Final Client Experience,
Section 16 (Payment Communication Bridge)."""

import tempfile
import unittest
from pathlib import Path

from falguna.audit import AuditLog
from falguna.billing import BillingStore
from falguna.commercial import DisputeStore
from falguna.comms import CommsStore
from falguna.payment_comms import PaymentCommsBridge, PaymentCommsError
from falguna.sales_ops import ClientStore
from falguna.store import StateStore


class PaymentCommsTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = StateStore(root / "state.db")
        self.store.migrate()
        self.audit = AuditLog(root / "audit.jsonl")
        self.clients = ClientStore(self.store, self.audit)
        self.billing = BillingStore(self.store, self.audit)
        self.disputes = DisputeStore(self.store, self.audit)
        self.comms = CommsStore(self.store, self.audit)
        self.bridge = PaymentCommsBridge(
            self.store, self.audit, comms=self.comms, billing=self.billing, disputes=self.disputes,
        )

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _client(self):
        return self.clients.upsert("Acme Corp", "Aryan")

    def _invoice(self, client_id=None, amount=1000.0, due_date=None):
        client_id = client_id or self._client()
        return client_id, self.billing.create_invoice(client_id, "Aryan", amount, due_date=due_date)


class InvoiceReadyNoticeTests(PaymentCommsTestBase):
    def test_unknown_invoice_raises(self):
        with self.assertRaises(PaymentCommsError):
            self.bridge.draft_invoice_ready_notice("does-not-exist")

    def test_draft_invoice_produces_no_draft(self):
        _, invoice_id = self._invoice()
        self.assertIsNone(self.bridge.draft_invoice_ready_notice(invoice_id))

    def test_ready_invoice_drafts_a_message(self):
        client_id, invoice_id = self._invoice(amount=2500.0, due_date="2026-12-01")
        self.billing.mark_ready(invoice_id, "Aryan")
        message = self.bridge.draft_invoice_ready_notice(invoice_id)
        self.assertIsNotNone(message)
        self.assertIn("2500.00", message["body"])
        self.assertIn("2026-12-01", message["body"])
        self.assertEqual(message["status"], "DRAFT")
        conv = self.comms.get_conversation(message["conversation_id"])
        self.assertEqual(conv["linked_client_id"], client_id)
        self.assertEqual(conv["department"], "billing")

    def test_calling_twice_does_not_duplicate(self):
        _, invoice_id = self._invoice()
        self.billing.mark_ready(invoice_id, "Aryan")
        first = self.bridge.draft_invoice_ready_notice(invoice_id)
        second = self.bridge.draft_invoice_ready_notice(invoice_id)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.bridge.list_for_invoice(invoice_id)), 1)



class PaymentReceivedNoticeTests(PaymentCommsTestBase):
    def test_no_payment_on_file_produces_no_draft(self):
        _, invoice_id = self._invoice()
        self.assertIsNone(self.bridge.draft_payment_received_notice(invoice_id))

    def test_partial_payment_mentions_remaining_balance(self):
        _, invoice_id = self._invoice(amount=1000.0)
        self.billing.record_payment(invoice_id, 400.0, "Aryan", evidence={"ref": "wire-1"})
        message = self.bridge.draft_payment_received_notice(invoice_id)
        self.assertIn("400.00", message["body"])
        self.assertIn("600.00", message["body"])

    def test_full_payment_says_fully_paid(self):
        _, invoice_id = self._invoice(amount=1000.0)
        self.billing.record_payment(invoice_id, 1000.0, "Aryan", evidence={"ref": "wire-2"})
        message = self.bridge.draft_payment_received_notice(invoice_id)
        self.assertIn("fully paid", message["body"])

    def test_two_distinct_payments_produce_two_drafts_one_duplicate_does_not(self):
        _, invoice_id = self._invoice(amount=1000.0)
        self.billing.record_payment(invoice_id, 400.0, "Aryan", evidence={"ref": "wire-3"})
        first = self.bridge.draft_payment_received_notice(invoice_id)
        again = self.bridge.draft_payment_received_notice(invoice_id)
        self.assertEqual(first["id"], again["id"])  # same evidence -> same draft
        self.billing.record_payment(invoice_id, 600.0, "Aryan", evidence={"ref": "wire-4"})
        second = self.bridge.draft_payment_received_notice(invoice_id)
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(len(self.bridge.list_for_invoice(invoice_id)), 2)



class OverdueReminderTests(PaymentCommsTestBase):
    def test_not_overdue_produces_no_draft(self):
        _, invoice_id = self._invoice(due_date="2099-01-01")
        self.billing.mark_ready(invoice_id, "Aryan")
        self.billing.mark_sent(invoice_id, "Aryan")
        self.assertIsNone(self.bridge.draft_overdue_reminder(invoice_id))

    def test_actually_overdue_invoice_drafts_a_reminder(self):
        _, invoice_id = self._invoice(amount=750.0, due_date="2000-01-01")
        self.billing.mark_ready(invoice_id, "Aryan")
        self.billing.mark_sent(invoice_id, "Aryan")
        self.billing.check_overdue(invoice_id)
        message = self.bridge.draft_overdue_reminder(invoice_id)
        self.assertIsNotNone(message)
        self.assertIn("750.00", message["body"])
        self.assertIn("2000-01-01", message["body"])

    def test_calling_twice_does_not_duplicate(self):
        _, invoice_id = self._invoice(due_date="2000-01-01")
        self.billing.mark_ready(invoice_id, "Aryan")
        self.billing.mark_sent(invoice_id, "Aryan")
        self.billing.check_overdue(invoice_id)
        first = self.bridge.draft_overdue_reminder(invoice_id)
        second = self.bridge.draft_overdue_reminder(invoice_id)
        self.assertEqual(first["id"], second["id"])



class DisputeAcknowledgementTests(PaymentCommsTestBase):
    def test_unknown_dispute_raises(self):
        with self.assertRaises(PaymentCommsError):
            self.bridge.draft_dispute_acknowledgement("does-not-exist")

    def test_open_dispute_drafts_an_acknowledgement(self):
        client_id, invoice_id = self._invoice(amount=500.0)
        dispute_id = self.disputes.open(invoice_id, "Aryan", "quality issue", 100.0, evidence={"ticket": "T-1"})
        message = self.bridge.draft_dispute_acknowledgement(dispute_id)
        self.assertIsNotNone(message)
        self.assertIn(invoice_id, message["body"])
        conv = self.comms.get_conversation(message["conversation_id"])
        self.assertEqual(conv["linked_client_id"], client_id)

    def test_calling_twice_does_not_duplicate(self):
        _, invoice_id = self._invoice(amount=500.0)
        dispute_id = self.disputes.open(invoice_id, "Aryan", "quality issue", 100.0, evidence={"ticket": "T-2"})
        first = self.bridge.draft_dispute_acknowledgement(dispute_id)
        second = self.bridge.draft_dispute_acknowledgement(dispute_id)
        self.assertEqual(first["id"], second["id"])


class DisputeResolutionNoticeTests(PaymentCommsTestBase):
    def test_unresolved_dispute_produces_no_draft(self):
        _, invoice_id = self._invoice(amount=500.0)
        dispute_id = self.disputes.open(invoice_id, "Aryan", "quality issue", 100.0, evidence={"ticket": "T-3"})
        self.assertIsNone(self.bridge.draft_dispute_resolution_notice(dispute_id))

    def test_no_refund_resolution_is_honest_about_no_refund(self):
        _, invoice_id = self._invoice(amount=500.0)
        dispute_id = self.disputes.open(invoice_id, "Aryan", "quality issue", 100.0, evidence={"ticket": "T-4"})
        self.disputes.resolve(dispute_id, "Aryan", "we reviewed the work and it met the agreed scope")
        message = self.bridge.draft_dispute_resolution_notice(dispute_id)
        self.assertIn("No refund applies", message["body"])

    def test_approved_refund_states_the_real_approved_amount_only(self):
        _, invoice_id = self._invoice(amount=500.0)
        self.billing.record_payment(invoice_id, 500.0, "Aryan", evidence={"ref": "wire-5"})
        dispute_id = self.disputes.open(invoice_id, "Aryan", "overcharge", 200.0, evidence={"ticket": "T-5"})
        self.disputes.resolve(dispute_id, "Aryan", "partial credit approved", refund_amount=150.0, evidence={"note": "approved"})
        message = self.bridge.draft_dispute_resolution_notice(dispute_id)
        self.assertIn("150.00", message["body"])
        self.assertNotIn("200.00", message["body"])



class SharedBillingConversationTests(PaymentCommsTestBase):
    def test_invoice_and_dispute_drafts_for_same_client_share_one_conversation(self):
        client_id, invoice_id = self._invoice(amount=800.0)
        self.billing.mark_ready(invoice_id, "Aryan")
        ready_message = self.bridge.draft_invoice_ready_notice(invoice_id)
        dispute_id = self.disputes.open(invoice_id, "Aryan", "late delivery", 50.0, evidence={"ticket": "T-6"})
        ack_message = self.bridge.draft_dispute_acknowledgement(dispute_id)
        self.assertEqual(ready_message["conversation_id"], ack_message["conversation_id"])
        billing_conversations = [
            c for c in self.comms.list_conversations(department="billing")
            if c["linked_client_id"] == client_id
        ]
        self.assertEqual(len(billing_conversations), 1)


if __name__ == "__main__":
    unittest.main()
