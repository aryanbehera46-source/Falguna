"""Tests for falguna/finance_ledger.py -- LedgerStore, cash_and_runway, and
client_profitability (Pass C of the TTT Command Center / CEO Intelligence +
Finance / Capital Engine v1 phase).
"""

import tempfile
import unittest
from pathlib import Path

from falguna.audit import AuditLog
from falguna.billing import BillingStore
from falguna.finance_ledger import LedgerError, LedgerStore, cash_and_runway, client_profitability
from falguna.sales_ops import ClientStore
from falguna.store import StateStore


class LedgerBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = StateStore(self.root / "state.db")
        self.store.migrate()
        self.audit = AuditLog(self.root / "audit.jsonl")
        self.ledger = LedgerStore(self.store, self.audit)
        self.clients = ClientStore(self.store, self.audit)
        self.billing = BillingStore(self.store, self.audit)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()


class LedgerStoreTests(LedgerBase):
    def test_record_requires_evidence(self):
        with self.assertRaises(LedgerError):
            self.ledger.record("OUTFLOW", "hosting", 50.0, None, actor="Aryan")
        with self.assertRaises(LedgerError):
            self.ledger.record("OUTFLOW", "hosting", 50.0, "", actor="Aryan")

    def test_record_requires_valid_entry_type_category_and_positive_amount(self):
        with self.assertRaises(LedgerError):
            self.ledger.record("SIDEWAYS", "hosting", 50.0, "receipt", actor="Aryan")
        with self.assertRaises(LedgerError):
            self.ledger.record("OUTFLOW", "not_a_real_category", 50.0, "receipt", actor="Aryan")
        with self.assertRaises(LedgerError):
            self.ledger.record("OUTFLOW", "hosting", -5.0, "receipt", actor="Aryan")
        with self.assertRaises(LedgerError):
            self.ledger.record("OUTFLOW", "hosting", 0, "receipt", actor="Aryan")

    def test_recorded_entry_round_trips(self):
        entry_id = self.ledger.record("OUTFLOW", "hosting", 199.0, "AWS invoice #42", actor="Aryan", currency="USD")
        entry = self.ledger.get(entry_id)
        self.assertEqual(entry["status"], "RECORDED")
        self.assertEqual(entry["amount"], 199.0)
        self.assertEqual(entry["currency"], "USD")
        self.assertEqual(entry["evidence"], "AWS invoice #42")

    def test_void_requires_reason_and_never_deletes(self):
        entry_id = self.ledger.record("OUTFLOW", "hosting", 100.0, "receipt", actor="Aryan")
        with self.assertRaises(LedgerError):
            self.ledger.void(entry_id, "Aryan", "")
        voided = self.ledger.void(entry_id, "Aryan", "duplicate entry")
        self.assertEqual(voided["status"], "VOID")
        # Still retrievable -- never deleted.
        self.assertIsNotNone(self.ledger.get(entry_id))
        with self.assertRaises(LedgerError):
            self.ledger.void(entry_id, "Aryan", "again")  # already VOID

    def test_list_defaults_to_recorded_only_and_filters_by_type_and_category(self):
        e1 = self.ledger.record("OUTFLOW", "hosting", 100.0, "r1", actor="Aryan")
        self.ledger.record("OUTFLOW", "marketing", 50.0, "r2", actor="Aryan")
        self.ledger.record("INFLOW", "client_revenue", 500.0, "r3", actor="Aryan")
        self.ledger.void(e1, "Aryan", "mistake")

        recorded = self.ledger.list()
        self.assertEqual(len(recorded), 2)  # the voided one is excluded by default
        outflows = self.ledger.list(entry_type="OUTFLOW")
        self.assertEqual(len(outflows), 1)  # only the still-RECORDED marketing outflow
        voided_only = self.ledger.list(status="VOID")
        self.assertEqual(len(voided_only), 1)

    def test_entry_survives_restart(self):
        entry_id = self.ledger.record("OUTFLOW", "software_tooling", 29.0, "receipt.pdf", actor="Aryan")
        self.store.close()
        reopened_store = StateStore(self.root / "state.db")
        reopened_store.migrate()
        reopened_ledger = LedgerStore(reopened_store, self.audit)
        entry = reopened_ledger.get(entry_id)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["amount"], 29.0)
        self.store = reopened_store  # let tearDown close this live handle


class CashAndRunwayTests(LedgerBase):
    def test_empty_store_has_zero_actual_cash_and_no_fabricated_runway(self):
        result = cash_and_runway(self.store)
        self.assertEqual(result["actual"]["combined_cash_estimate"], 0)
        self.assertIsNone(result["runway"]["monthly_burn_rate"])
        self.assertIsNone(result["runway"]["runway_months"])

    def test_combined_cash_estimate_reflects_invoice_and_ledger_activity(self):
        client_id = self.clients.upsert("Acme Co", "Aryan")
        invoice_id = self.billing.create_invoice(client_id, "Aryan", 1000.0)
        self.billing.mark_sent(invoice_id, "Aryan")
        self.billing.record_payment(invoice_id, 1000.0, "Aryan", evidence="bank ref 1")
        self.ledger.record("OUTFLOW", "hosting", 200.0, "AWS invoice", actor="Aryan")
        self.ledger.record("OUTFLOW", "contractor", 300.0, "contractor invoice", actor="Aryan")

        result = cash_and_runway(self.store)
        self.assertEqual(result["actual"]["invoice_cash_in_to_date"], 1000.0)
        self.assertEqual(result["actual"]["ledger_outflows_recorded"], 500.0)
        self.assertEqual(result["actual"]["combined_cash_estimate"], 500.0)
        self.assertIsNotNone(result["runway"]["monthly_burn_rate"])
        self.assertIsNotNone(result["runway"]["runway_months"])

    def test_voided_ledger_entries_are_excluded_from_cash_and_runway(self):
        entry_id = self.ledger.record("OUTFLOW", "hosting", 200.0, "receipt", actor="Aryan")
        self.ledger.void(entry_id, "Aryan", "duplicate")
        result = cash_and_runway(self.store)
        self.assertEqual(result["actual"]["ledger_outflows_recorded"], 0)
        self.assertIsNone(result["runway"]["monthly_burn_rate"])


class ClientProfitabilityTests(LedgerBase):
    def test_unknown_client_raises(self):
        with self.assertRaises(LedgerError):
            client_profitability(self.store, "does-not-exist")

    def test_no_costs_recorded_is_marked_partial_not_free(self):
        client_id = self.clients.upsert("Acme Co", "Aryan")
        invoice_id = self.billing.create_invoice(client_id, "Aryan", 1000.0)
        self.billing.mark_sent(invoice_id, "Aryan")
        self.billing.record_payment(invoice_id, 1000.0, "Aryan", evidence="bank ref 1")

        result = client_profitability(self.store, client_id)
        self.assertEqual(result["revenue"], 1000.0)
        self.assertEqual(result["direct_costs"], 0)
        self.assertEqual(result["gross_profit_estimate"], 1000.0)
        self.assertEqual(result["completeness"], "partial_no_costs_recorded")

    def test_recorded_costs_reduce_gross_profit_and_mark_estimated(self):
        client_id = self.clients.upsert("Acme Co", "Aryan")
        invoice_id = self.billing.create_invoice(client_id, "Aryan", 1000.0)
        self.billing.mark_sent(invoice_id, "Aryan")
        self.billing.record_payment(invoice_id, 1000.0, "Aryan", evidence="bank ref 1")
        self.ledger.record("OUTFLOW", "contractor", 400.0, "contractor invoice", actor="Aryan", client_id=client_id)

        result = client_profitability(self.store, client_id)
        self.assertEqual(result["direct_costs"], 400.0)
        self.assertEqual(result["gross_profit_estimate"], 600.0)
        self.assertEqual(result["completeness"], "estimated_from_recorded_costs")

    def test_voided_cost_entries_do_not_count_against_profitability(self):
        client_id = self.clients.upsert("Acme Co", "Aryan")
        entry_id = self.ledger.record("OUTFLOW", "contractor", 400.0, "contractor invoice", actor="Aryan", client_id=client_id)
        self.ledger.void(entry_id, "Aryan", "duplicate")
        result = client_profitability(self.store, client_id)
        self.assertEqual(result["direct_costs"], 0)
        self.assertEqual(result["completeness"], "partial_no_costs_recorded")


if __name__ == "__main__":
    unittest.main()
