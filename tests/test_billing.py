"""Tests for falguna/billing.py -- Invoicing, Completion, and
Retention/Upsell (Sections 11-13, Pass D)."""

import tempfile
import unittest
from pathlib import Path

from falguna.audit import AuditLog
from falguna.billing import (
    BillingError,
    BillingStore,
    CompletionError,
    CompletionService,
    RetentionError,
    RetentionStore,
)
from falguna.revenue_hunter import OpportunityStore
from falguna.sales_ops import ClientStore
from falguna.store import StateStore


class BillingTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = StateStore(root / "state.db")
        self.store.migrate()
        self.audit = AuditLog(root / "audit.jsonl")
        self.clients = ClientStore(self.store, self.audit)
        self.opportunities = OpportunityStore(self.store, self.audit)
        self.billing = BillingStore(self.store, self.audit)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _client(self):
        return self.clients.upsert("Acme Corp", "Aryan")


class CreateInvoiceTests(BillingTestBase):
    def test_unknown_client_raises(self):
        with self.assertRaises(BillingError):
            self.billing.create_invoice("does-not-exist", "Aryan", 1000)

    def test_non_positive_amount_raises(self):
        client_id = self._client()
        with self.assertRaises(BillingError):
            self.billing.create_invoice(client_id, "Aryan", 0)
        with self.assertRaises(BillingError):
            self.billing.create_invoice(client_id, "Aryan", -50)

    def test_creates_a_draft_invoice(self):
        client_id = self._client()
        invoice_id = self.billing.create_invoice(client_id, "Aryan", 1500, currency="USD", due_date="2026-12-01")
        invoice = self.billing.get(invoice_id)
        self.assertEqual(invoice["status"], "DRAFT")
        self.assertEqual(invoice["amount"], 1500)
        self.assertEqual(invoice["amount_received"], 0)


class InvoiceLifecycleTests(BillingTestBase):
    def _invoice(self, amount=1000):
        client_id = self._client()
        return client_id, self.billing.create_invoice(client_id, "Aryan", amount)

    def test_mark_ready_requires_draft(self):
        _, invoice_id = self._invoice()
        self.billing.mark_ready(invoice_id, "Aryan")
        with self.assertRaises(BillingError):
            self.billing.mark_ready(invoice_id, "Aryan")

    def test_mark_sent_from_draft(self):
        _, invoice_id = self._invoice()
        result = self.billing.mark_sent(invoice_id, "Aryan")
        self.assertEqual(result["status"], "SENT")

    def test_mark_sent_from_ready(self):
        _, invoice_id = self._invoice()
        self.billing.mark_ready(invoice_id, "Aryan")
        result = self.billing.mark_sent(invoice_id, "Aryan")
        self.assertEqual(result["status"], "SENT")

    def test_cancel_from_draft(self):
        _, invoice_id = self._invoice()
        result = self.billing.cancel(invoice_id, "Aryan", reason="deal fell through")
        self.assertEqual(result["status"], "CANCELLED")

    def test_cancel_already_paid_raises(self):
        _, invoice_id = self._invoice(100)
        self.billing.mark_sent(invoice_id, "Aryan")
        self.billing.record_payment(invoice_id, 100, "Aryan", evidence="bank confirmation #123")
        with self.assertRaises(BillingError):
            self.billing.cancel(invoice_id, "Aryan")


class RecordPaymentTests(BillingTestBase):
    def _sent_invoice(self, amount=1000):
        client_id = self._client()
        invoice_id = self.billing.create_invoice(client_id, "Aryan", amount)
        self.billing.mark_sent(invoice_id, "Aryan")
        return invoice_id

    def test_payment_without_evidence_raises(self):
        invoice_id = self._sent_invoice()
        with self.assertRaises(BillingError):
            self.billing.record_payment(invoice_id, 1000, "Aryan", evidence=None)
        with self.assertRaises(BillingError):
            self.billing.record_payment(invoice_id, 1000, "Aryan", evidence="")

    def test_full_payment_marks_paid(self):
        invoice_id = self._sent_invoice(1000)
        result = self.billing.record_payment(invoice_id, 1000, "Aryan", evidence="wire ref #456")
        self.assertEqual(result["status"], "PAID")
        self.assertEqual(result["amount_received"], 1000)

    def test_partial_payment_marks_partially_paid(self):
        invoice_id = self._sent_invoice(1000)
        result = self.billing.record_payment(invoice_id, 400, "Aryan", evidence="partial wire #1")
        self.assertEqual(result["status"], "PARTIALLY_PAID")
        self.assertEqual(result["amount_received"], 400)

    def test_two_partial_payments_accumulate_to_paid(self):
        invoice_id = self._sent_invoice(1000)
        self.billing.record_payment(invoice_id, 400, "Aryan", evidence="partial wire #1")
        result = self.billing.record_payment(invoice_id, 600, "Aryan", evidence="partial wire #2")
        self.assertEqual(result["status"], "PAID")
        self.assertEqual(result["amount_received"], 1000)

    def test_payment_evidence_history_accumulates(self):
        import json
        invoice_id = self._sent_invoice(1000)
        self.billing.record_payment(invoice_id, 400, "Aryan", evidence="partial wire #1")
        self.billing.record_payment(invoice_id, 600, "Aryan", evidence="partial wire #2")
        invoice = self.billing.get(invoice_id)
        history = json.loads(invoice["evidence_json"])
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["evidence"], "partial wire #1")
        self.assertEqual(history[1]["evidence"], "partial wire #2")

    def test_cannot_pay_a_cancelled_invoice(self):
        client_id = self._client()
        invoice_id = self.billing.create_invoice(client_id, "Aryan", 1000)
        self.billing.cancel(invoice_id, "Aryan")
        with self.assertRaises(BillingError):
            self.billing.record_payment(invoice_id, 500, "Aryan", evidence="ref")


class OverdueCheckTests(BillingTestBase):
    def test_past_due_date_moves_sent_invoice_to_overdue(self):
        client_id = self._client()
        invoice_id = self.billing.create_invoice(client_id, "Aryan", 1000, due_date="2020-01-01")
        self.billing.mark_sent(invoice_id, "Aryan")
        result = self.billing.check_overdue(invoice_id)
        self.assertEqual(result["status"], "OVERDUE")

    def test_future_due_date_does_not_move_to_overdue(self):
        client_id = self._client()
        invoice_id = self.billing.create_invoice(client_id, "Aryan", 1000, due_date="2099-01-01")
        self.billing.mark_sent(invoice_id, "Aryan")
        result = self.billing.check_overdue(invoice_id)
        self.assertEqual(result["status"], "SENT")

    def test_paid_invoice_never_becomes_overdue(self):
        client_id = self._client()
        invoice_id = self.billing.create_invoice(client_id, "Aryan", 1000, due_date="2020-01-01")
        self.billing.mark_sent(invoice_id, "Aryan")
        self.billing.record_payment(invoice_id, 1000, "Aryan", evidence="ref")
        result = self.billing.check_overdue(invoice_id)
        self.assertEqual(result["status"], "PAID")


class CompletionServiceTests(BillingTestBase):
    def setUp(self):
        super().setUp()
        self.completion = CompletionService(self.store, self.audit)

    def _opportunity(self):
        return self.opportunities.create({"title": "Build a CRM dashboard"}, actor="Aryan")

    def test_unknown_opportunity_raises(self):
        with self.assertRaises(CompletionError):
            self.completion.record_completion("does-not-exist", "Aryan", evidence={"note": "done"})

    def test_no_evidence_raises(self):
        opp_id = self._opportunity()
        with self.assertRaises(CompletionError):
            self.completion.record_completion(opp_id, "Aryan", evidence=None)
        with self.assertRaises(CompletionError):
            self.completion.record_completion(opp_id, "Aryan", evidence={})

    def test_records_completion_with_evidence(self):
        opp_id = self._opportunity()
        record_id = self.completion.record_completion(opp_id, "Aryan", evidence={"mission_id": "m1", "final_commit": "abc123"})
        record = self.completion.get_for_opportunity(opp_id)
        self.assertEqual(record["id"], record_id)

    def test_is_idempotent_per_opportunity(self):
        opp_id = self._opportunity()
        first = self.completion.record_completion(opp_id, "Aryan", evidence={"note": "done"})
        second = self.completion.record_completion(opp_id, "Aryan", evidence={"note": "done again"})
        self.assertEqual(first, second)


class RetentionStoreTests(BillingTestBase):
    def setUp(self):
        super().setUp()
        self.retention = RetentionStore(self.store, self.audit)

    def test_unknown_kind_raises(self):
        client_id = self._client()
        with self.assertRaises(RetentionError):
            self.retention.create_item(client_id, "not_a_real_kind", "Aryan")

    def test_unknown_client_raises(self):
        with self.assertRaises(RetentionError):
            self.retention.create_item("does-not-exist", "maintenance", "Aryan")

    def test_creates_an_open_item(self):
        client_id = self._client()
        item_id = self.retention.create_item(client_id, "maintenance", "Aryan", notes="quarterly check-in", follow_up_date="2026-12-01")
        item = self.retention.list_for_client(client_id)[0]
        self.assertEqual(item["id"], item_id)
        self.assertEqual(item["status"], "OPEN")

    def test_update_item_status(self):
        client_id = self._client()
        item_id = self.retention.create_item(client_id, "testimonial", "Aryan")
        updated = self.retention.update_item(item_id, "Aryan", status="COMPLETED")
        self.assertEqual(updated["status"], "COMPLETED")

    def test_unknown_status_raises(self):
        client_id = self._client()
        item_id = self.retention.create_item(client_id, "referral", "Aryan")
        with self.assertRaises(RetentionError):
            self.retention.update_item(item_id, "Aryan", status="NOT_A_REAL_STATUS")

    def test_list_due_only_returns_open_or_scheduled_with_past_due_date(self):
        client_id = self._client()
        due_soon = self.retention.create_item(client_id, "maintenance", "Aryan", follow_up_date="2020-01-01")
        not_due_yet = self.retention.create_item(client_id, "maintenance", "Aryan", follow_up_date="2099-01-01")
        completed_but_due = self.retention.create_item(client_id, "maintenance", "Aryan", follow_up_date="2020-01-01")
        self.retention.update_item(completed_but_due, "Aryan", status="COMPLETED")

        due = self.retention.list_due(as_of="2026-01-01")
        due_ids = {i["id"] for i in due}
        self.assertIn(due_soon, due_ids)
        self.assertNotIn(not_due_yet, due_ids)
        self.assertNotIn(completed_but_due, due_ids)


if __name__ == "__main__":
    unittest.main()
