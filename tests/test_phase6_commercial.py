import hashlib
import hmac
import tempfile
import unittest
from pathlib import Path

from falguna.audit import AuditLog
from falguna.phase6_commercial import (
    AccessContext, ApprovalWorkflow, CommercialIdentityStore,
    CommercialSecurityError, FinanceAccounts, PaymentOrchestrator,
)
from falguna.store import StateStore


class Phase6Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.tmp.name) / "synthetic-phase6.db")
        self.store.migrate()
        self.audit = AuditLog(Path(self.tmp.name) / "synthetic-phase6-audit.jsonl")
        self.identities = CommercialIdentityStore(self.store, self.audit)
        self.owner = self._identity("ttt", "aryan", "Aryan", "OWNER")
        self.finance = self._identity("ttt", "finance-1", "Finance One", "FINANCE_OPERATOR")
        self.finance2 = self._identity("ttt", "finance-2", "Finance Two", "FINANCE_OPERATOR")
        self.other_owner = self._identity("other", "aryan", "Aryan", "OWNER")
        self.payments = PaymentOrchestrator(self.store, self.audit, self.identities)
        self.approvals = ApprovalWorkflow(self.store, self.audit, self.identities)
        self.accounts = FinanceAccounts(self.store, self.audit, self.identities)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _identity(self, org, ref, name, role):
        identity_id = self.identities.create(org, "STAFF", ref, name, role, "test")
        return AccessContext(identity_id, org)

    def _intent(self, context=None, key="checkout-1"):
        return self.payments.create_intent(
            context or self.finance, "synthetic-customer", 12500, "inr", "ONE_TIME", key,
            "Twenty Two Technologies", "beneficiary:ttt-primary", ["UPI", "CARD"],
            metadata={"synthetic": True},
        )


class IdentityIsolationTests(Phase6Case):
    def test_cross_organization_authorization_fails_closed(self):
        with self.assertRaisesRegex(CommercialSecurityError, "cross-organization"):
            self.identities.authorize(self.other_owner, "finance:read", "ttt")

    def test_customer_cannot_create_payment_intent(self):
        customer = self._identity("ttt", "customer-1", "Synthetic Customer", "CUSTOMER")
        with self.assertRaisesRegex(CommercialSecurityError, "permission denied"):
            self._intent(customer)

    def test_unknown_role_is_rejected(self):
        with self.assertRaises(CommercialSecurityError):
            self.identities.create("ttt", "STAFF", "x", "X", "SUPERUSER", "test")


class PaymentSecurityTests(Phase6Case):
    def test_intent_is_sandbox_only_and_keeps_legal_owner_separate(self):
        row = self._intent()
        self.assertEqual(row["provider"], "SANDBOX_ADAPTER")
        self.assertEqual(row["legal_owner_name"], "Twenty Two Technologies")
        self.assertEqual(row["beneficiary_ref"], "beneficiary:ttt-primary")
        self.assertEqual(row["status"], "CREATED")

    def test_create_is_idempotent(self):
        first = self._intent(key="same")
        second = self._intent(key="same")
        self.assertEqual(first["id"], second["id"])

    def test_idempotency_key_cannot_be_reused_for_different_amount(self):
        self._intent(key="same")
        with self.assertRaisesRegex(CommercialSecurityError, "different payment data"):
            self.payments.create_intent(self.finance, "synthetic-customer", 1, "INR", "ONE_TIME", "same",
                                        "TTT", "beneficiary:ttt-primary", ["UPI"])

    def test_raw_card_details_are_rejected(self):
        with self.assertRaisesRegex(CommercialSecurityError, "raw card data"):
            self.payments.create_intent(self.finance, "c", 10, "INR", "ONE_TIME", "raw-card", "TTT", "b", ["CARD"],
                                        metadata={"card_number": "4111111111111111", "cvv": "123"})

    def test_wrong_amount_and_currency_provider_events_are_rejected(self):
        payment = self._intent()
        for amount, currency in ((12501, "INR"), (12500, "USD")):
            with self.assertRaisesRegex(CommercialSecurityError, "amount/currency mismatch"):
                self.payments.apply_verified_event(self.finance, payment["id"], "CAPTURED", amount, currency,
                                                   {"provider": "synthetic"}, f"bad:{amount}:{currency}")

    def test_invalid_state_jump_is_rejected(self):
        payment = self._intent()
        with self.assertRaisesRegex(CommercialSecurityError, "valid payment transition"):
            self.payments.apply_verified_event(self.finance, payment["id"], "SETTLED", 12500, "INR", {"provider": "synthetic"}, "jump")

    def test_duplicate_capture_event_is_idempotent(self):
        payment = self._intent()
        first = self.payments.apply_verified_event(self.finance, payment["id"], "CAPTURED", 12500, "INR", {"provider": "synthetic"}, "capture-1")
        second = self.payments.apply_verified_event(self.finance, payment["id"], "CAPTURED", 12500, "INR", {"provider": "synthetic"}, "capture-1")
        self.assertEqual(first["status"], "CAPTURED")
        self.assertEqual(second["status"], "CAPTURED")
        rows = self.store.list("p6_payment_events", "payment_intent_id=? AND idempotency_key=?", (payment["id"], "capture-1"))
        self.assertEqual(len(rows), 1)

    def test_forged_webhook_is_rejected_and_not_stored(self):
        payment = self._intent()
        with self.assertRaisesRegex(CommercialSecurityError, "signature"):
            self.payments.ingest_webhook("mockpay", "evt-1", "ttt", b"{}", "forged", "secret", payment["id"])
        self.assertEqual(self.store.list("p6_webhook_events"), [])

    def test_verified_webhook_replay_is_idempotent(self):
        payment = self._intent(); body = b'{"status":"captured"}'
        signature = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
        first = self.payments.ingest_webhook("mockpay", "evt-1", "ttt", body, signature, "secret", payment["id"])
        second = self.payments.ingest_webhook("mockpay", "evt-1", "ttt", body, signature, "secret", payment["id"])
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.store.list("p6_webhook_events")), 1)

    def test_replay_reference_with_changed_payload_is_rejected(self):
        payment = self._intent(); body = b"one"
        signature = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
        self.payments.ingest_webhook("mockpay", "evt-1", "ttt", body, signature, "secret", payment["id"])
        body2 = b"two"; signature2 = hmac.new(b"secret", body2, hashlib.sha256).hexdigest()
        with self.assertRaisesRegex(CommercialSecurityError, "different payload"):
            self.payments.ingest_webhook("mockpay", "evt-1", "ttt", body2, signature2, "secret", payment["id"])


class ApprovalControlTests(Phase6Case):
    def test_maker_cannot_verify_own_request(self):
        request = self.approvals.request(self.finance, "REFUND", "PAYMENT", "p1", {"reason": "synthetic"}, 500, "INR")
        with self.assertRaisesRegex(CommercialSecurityError, "independent verifier"):
            self.approvals.verify(self.finance, request["id"])

    def test_non_owner_cannot_final_approve(self):
        request = self.approvals.request(self.finance, "REFUND", "PAYMENT", "p1", {}, 500, "INR")
        self.approvals.verify(self.finance2, request["id"])
        with self.assertRaisesRegex(CommercialSecurityError, "Aryan owner"):
            self.approvals.approve_by_aryan(self.finance, request["id"])

    def test_full_gate_stops_at_approved_pending_execution(self):
        request = self.approvals.request(self.finance, "COMMISSION_RELEASE", "COMMISSION", "c1", {}, 500, "INR")
        self.approvals.verify(self.finance2, request["id"])
        approved = self.approvals.approve_by_aryan(self.owner, request["id"])
        self.assertEqual(approved["status"], "APPROVED_PENDING_EXECUTION")
        self.assertIsNone(approved["execution_reference"])
        self.assertFalse(hasattr(self.approvals, "execute"))

    def test_beneficiary_change_and_high_value_are_flagged(self):
        request = self.approvals.request(self.finance, "BENEFICIARY_CHANGE", "ACCOUNT", "a1", {}, 100000, "INR")
        self.assertIn("BENEFICIARY_CHANGE", request["risk_flags_json"])
        self.assertIn("HIGH_VALUE", request["risk_flags_json"])

    def test_ai_identity_cannot_request_money_action(self):
        ai = self._identity("ttt", "falguna", "FALGUNA", "FINANCE_OPERATOR")
        with self.assertRaisesRegex(CommercialSecurityError, "AI actors"):
            self.approvals.request(ai, "REFUND", "PAYMENT", "p1", {})


class FinanceInvariantTests(Phase6Case):
    def test_financial_event_requires_evidence(self):
        with self.assertRaisesRegex(CommercialSecurityError, "evidence"):
            self.accounts.record_event(self.finance, "COLLECTION", "CASH", "CREDIT", 100, "INR", "PAYMENT", "p1", None, "e1")

    def test_financial_event_replay_is_idempotent(self):
        args = (self.finance, "COLLECTION", "CASH", "CREDIT", 100, "INR", "PAYMENT", "p1", {"verified": True}, "e1")
        first = self.accounts.record_event(*args); second = self.accounts.record_event(*args)
        self.assertEqual(first["id"], second["id"])

    def test_dynamic_protected_cash_and_free_cash(self):
        self.accounts.set_reserve_policy(self.owner, "INR", essential_monthly_burn=1000)
        events = [
            ("COLLECTION", "CASH", "CREDIT", 10000, "cash"),
            ("TAX_PROVISION", "ESTIMATED_TAX", "CREDIT", 1200, "tax"),
            ("CUSTOMER_HOLD", "CUSTOMER_HELD", "CREDIT", 500, "held"),
            ("COMMISSION_ACCRUAL", "ACCRUED_PAYABLES", "CREDIT", 300, "commission"),
            ("COMMITTED_COST", "COMMITTED_COSTS", "CREDIT", 700, "cost"),
            ("DISPUTE_HOLD", "DISPUTED_FUNDS", "CREDIT", 400, "dispute"),
        ]
        for event_type, bucket, direction, amount, key in events:
            self.accounts.record_event(self.finance, event_type, bucket, direction, amount, "INR", "SYNTHETIC", key, {"synthetic": True}, key)
        position = self.accounts.cash_position(self.finance, "INR")
        self.assertEqual(position["cash_balance"], 10000)
        self.assertEqual(position["protected_cash"], 6100)  # 1200+500+300+700+400 + 3 months burn
        self.assertEqual(position["free_cash"], 3900)
        self.assertEqual(position["target_runway_months"], 6)

    def test_cross_org_cash_position_is_denied(self):
        with self.assertRaisesRegex(CommercialSecurityError, "cross-organization"):
            self.accounts.cash_position(AccessContext(self.finance.identity_id, "other"), "INR")


if __name__ == "__main__":
    unittest.main()
