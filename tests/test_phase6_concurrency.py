import hashlib
import hmac
import tempfile
import threading
import unittest
from pathlib import Path

from falguna.audit import AuditLog
from falguna.phase6_commercial import AccessContext, CommercialIdentityStore, PaymentOrchestrator
from falguna.phase6_financial_flows import SubscriptionService
from falguna.store import StateStore, utcnow


class Phase6MultiConnectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name); self.db_path = self.root / "state.db"
        store = StateStore(self.db_path); store.migrate(); audit = AuditLog(self.root / "audit.jsonl")
        identities = CommercialIdentityStore(store, audit)
        self.identity_id = identities.create("ttt", "STAFF", "worker", "Worker", "FINANCE_OPERATOR", "test")
        self.context = AccessContext(self.identity_id, "ttt")
        now = utcnow()
        self.client_id = store.create("clients", {"name": "Synthetic", "primary_contact": None, "contact_channel": None,
            "status": "ACTIVE", "total_won_value": 100, "created_at": now, "updated_at": now})
        self.subscription = SubscriptionService(store, audit, identities).create(
            self.context, self.client_id, "Monthly", 100, "INR", "MONTHLY", "2026-10-01", "token", "mandate")
        self.payment = PaymentOrchestrator(store, audit, identities).create_intent(
            self.context, "ttt", 100, "INR", "SUBSCRIPTION", "webhook-race-payment", "TTT",
            "beneficiary:official", ["CARD"])
        store.close()

    def tearDown(self):
        self.tmp.cleanup()

    def _worker(self, function, barrier, results, errors):
        store = StateStore(self.db_path); audit = AuditLog(self.root / "audit.jsonl")
        try:
            barrier.wait(); results.append(function(store, audit))
        except Exception as exc:
            errors.append(exc)
        finally:
            store.close()

    def test_identical_signed_webhooks_from_separate_connections_have_one_record(self):
        raw = b'{"status":"captured"}'; secret = "synthetic-secret"
        signature = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        barrier = threading.Barrier(2); results = []; errors = []
        def call(store, audit):
            identities = CommercialIdentityStore(store, audit)
            return PaymentOrchestrator(store, audit, identities).ingest_webhook(
                "SANDBOX", "same-event", "ttt", raw, signature, secret, self.payment["id"])["id"]
        threads = [threading.Thread(target=self._worker, args=(call, barrier, results, errors)) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(set(results)), 1)
        check = StateStore(self.db_path)
        self.assertEqual(len(check.list("p6_webhook_events", "provider_event_ref='same-event'")), 1)
        check.close()

    def test_recurring_invoice_generation_from_separate_connections_creates_one_invoice(self):
        barrier = threading.Barrier(2); results = []; errors = []
        def call(store, audit):
            identities = CommercialIdentityStore(store, audit)
            return SubscriptionService(store, audit, identities).create_due_invoice(
                self.context, self.subscription["id"], "2026-10-01")["id"]
        threads = [threading.Thread(target=self._worker, args=(call, barrier, results, errors)) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        check = StateStore(self.db_path)
        invoices = check.list("rh_invoices", "client_id=?", (self.client_id,))
        cycles = check.list("p6_subscription_cycles", "subscription_id=?", (self.subscription["id"],))
        check.close()
        self.assertEqual(len(invoices), 1)
        self.assertEqual(len(cycles), 1)
        self.assertEqual(cycles[0]["invoice_id"], invoices[0]["id"])
        self.assertFalse(any("locked" in str(exc).lower() for exc in errors))


if __name__ == "__main__":
    unittest.main()
