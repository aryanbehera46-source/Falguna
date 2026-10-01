import tempfile
import unittest
from pathlib import Path

from falguna.audit import AuditLog
from falguna.phase6_commercial import (AccessContext, ApprovalWorkflow, CommercialIdentityStore,
                                       CommercialOperations, CommercialSecurityError, PaymentOrchestrator)
from falguna.phase6_financial_flows import (CommissionReleaseService, CommercialEconomicsService,
                                            PayableService, RefundService, SubscriptionService)
from falguna.store import StateStore, utcnow


class FinancialFlowsCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); root = Path(self.tmp.name)
        self.store = StateStore(root / "state.db"); self.store.migrate(); self.audit = AuditLog(root / "audit.jsonl")
        self.identities = CommercialIdentityStore(self.store, self.audit)
        self.maker = self._id("maker", "Maker", "FINANCE_OPERATOR")
        self.verifier = self._id("verifier", "Verifier", "FINANCE_OPERATOR")
        self.owner = self._id("aryan", "Aryan", "OWNER")
        self.approvals = ApprovalWorkflow(self.store, self.audit, self.identities)
        now = utcnow()
        self.client_id = self.store.create("clients", {"name": "Synthetic", "primary_contact": None, "contact_channel": None,
            "status": "ACTIVE", "total_won_value": 1000, "created_at": now, "updated_at": now})
        self.store.create("comm_organizations", {"name": "Synthetic Org", "domain": "synthetic.invalid",
            "linked_client_id": self.client_id, "notes": None, "created_at": now, "updated_at": now}, record_id="ttt")
        self.opp_id = self.store.create("rh_opportunities", {"source": "partner_referral", "source_url": None,
            "client_name": "Synthetic", "title": "Synthetic", "description": None, "budget_rate": None,
            "required_skills": None, "deadline": None, "contract_type": None, "location_timezone": None,
            "urgency": None, "stage": "Won", "final_price": 1000, "lost_reason": None, "created_at": now, "updated_at": now})
        self.invoice_id = self.store.create("rh_invoices", {"client_id": self.client_id, "opportunity_id": self.opp_id,
            "active_job_id": None, "amount": 1000, "currency": "INR", "milestone": None, "due_date": None,
            "amount_received": 1000, "status": "PAID", "evidence_json": "[]", "created_at": now, "updated_at": now})
        self.partner_id = self.store.create("pm_partners", {"full_name": "Partner", "organization_name": None,
            "email": "p@invalid.test", "phone": None, "region": None, "country": None, "service_categories_json": "[]",
            "assigned_manager": None, "verification_status": "VERIFIED", "agreement_accepted": 1,
            "agreement_accepted_at": now, "agreement_reference": None, "status": "APPROVED", "approved_at": now,
            "approved_by": "test", "suspended_at": None, "suspended_by": None, "suspension_reason": None,
            "terminated_at": None, "terminated_by": None, "termination_reason": None,
            "created_at": now, "updated_at": now})
        self.referral_id = self.store.create("pm_referrals", {"partner_id": self.partner_id, "prospect_name": "Lead",
            "organization_name": "Synthetic", "contact_email": None, "contact_phone": None, "region": None,
            "requested_service": "Build", "estimated_value": 1000, "referral_source": "partner", "notes": None,
            "normalized_email": None, "normalized_phone": None, "normalized_domain": None, "normalized_org": "synthetic",
            "attribution_status": "ATTRIBUTED", "attribution_start": now, "attribution_expiry": None,
            "opportunity_id": self.opp_id, "duplicate_flag": 0, "created_at": now, "updated_at": now})
        self.commission_id = self.store.create("pm_commissions", {"referral_id": self.referral_id, "partner_id": self.partner_id,
            "opportunity_id": self.opp_id, "invoice_id": self.invoice_id, "rate": 0.1, "status": "ELIGIBLE",
            "eligible_amount": 100, "refunded_amount": 0, "hold_reason": None, "created_at": now, "updated_at": now})

    def tearDown(self):
        self.store.close(); self.tmp.cleanup()

    def _id(self, ref, name, role):
        iid = self.identities.create("ttt", "STAFF", ref, name, role, "test")
        return AccessContext(iid, "ttt")

    def approve(self, approval_id):
        self.approvals.verify(self.verifier, approval_id)
        return self.approvals.approve_by_aryan(self.owner, approval_id)


class CommissionReleaseFlowTests(FinancialFlowsCase):
    def test_full_release_flow_stops_at_releasable(self):
        service = CommissionReleaseService(self.store, self.audit, self.identities)
        release = service.prepare(self.maker, self.commission_id, "INR", 100, "release-1")
        self.assertEqual(release["eligible_base"], 900)
        self.assertEqual(release["commission_amount"], 90)
        self.approve(release["approval_request_id"])
        ready = service.mark_releasable(self.maker, release["id"])
        self.assertEqual(ready["status"], "RELEASABLE")
        self.assertEqual(len(self.store.list("p6_financial_events", "event_type='COMMISSION_PAYABLE'")), 1)

    def test_duplicate_release_is_idempotent(self):
        service = CommissionReleaseService(self.store, self.audit, self.identities)
        a = service.prepare(self.maker, self.commission_id, "INR", 0, "same")
        b = service.prepare(self.maker, self.commission_id, "INR", 0, "same")
        self.assertEqual(a["id"], b["id"])

    def test_cross_organization_commission_release_is_denied(self):
        """Tenant-isolation adversarial test: pm_commissions/pm_referrals/
        rh_invoices predate Phase 6's organization layer and carry no
        organization_id of their own. A commercial identity from a
        DIFFERENT organization than the one that actually owns this
        commission (via its invoice's linked client in comm_organizations)
        must never be able to prepare a release against it."""
        other_identity_id = self.identities.create("acme", "STAFF", "maker", "Acme Maker", "FINANCE_OPERATOR", "test")
        from falguna.phase6_commercial import AccessContext
        other = AccessContext(other_identity_id, "acme")
        service = CommissionReleaseService(self.store, self.audit, self.identities)
        with self.assertRaisesRegex(CommercialSecurityError, "does not belong to this organization"):
            service.prepare(other, self.commission_id, "INR", 0, "cross-tenant-attempt")

    def test_open_risk_blocks_release(self):
        now = utcnow()
        self.store.create("p6_risk_events", {"organization_id": "ttt", "event_type": "BRAND_MISUSE", "severity": "HIGH",
            "partner_id": self.partner_id, "customer_ref": None, "project_id": None, "payment_intent_id": None,
            "evidence_json": "{}", "status": "OPEN", "source": "test", "reviewer_identity_id": None,
            "current_action": None, "created_at": now, "updated_at": now})
        with self.assertRaisesRegex(CommercialSecurityError, "risk"):
            CommissionReleaseService(self.store, self.audit, self.identities).prepare(self.maker, self.commission_id, "INR", 0, "r")


class RefundFlowTests(FinancialFlowsCase):
    def setUp(self):
        super().setUp(); now = utcnow()
        self.payment_id = self.store.create("p6_payment_intents", {"organization_id": "ttt", "customer_ref": "customer",
            "invoice_id": self.invoice_id, "kind": "ONE_TIME", "amount": 1000, "currency": "INR", "status": "SETTLED",
            "capture_mode": "MANUAL", "allowed_methods_json": "[]", "provider": "SANDBOX_ADAPTER",
            "provider_session_ref": None, "provider_transaction_ref": "txn", "payment_method_token_ref": None,
            "mandate_token_ref": None, "milestone_ref": None, "legal_owner_name": "TTT", "beneficiary_ref": "official",
            "idempotency_key": "payment", "metadata_json": "{}", "actor": self.maker.identity_id,
            "created_at": now, "updated_at": now})

    def test_full_partial_refund_flow_and_commission_clawback(self):
        service = RefundService(self.store, self.audit, self.identities)
        refund = service.request(self.maker, self.payment_id, 200, "INR", "customer cancellation", "refund-1")
        self.approve(refund["approval_request_id"])
        confirmed = service.confirm_sandbox(self.maker, refund["id"], "sandbox-refund-1", {"verified": True})
        self.assertEqual(confirmed["status"], "CONFIRMED")
        self.assertEqual(self.store.list("p6_financial_events", "event_type='REFUND_CONFIRMED'")[0]["amount"], 200)
        commission = self.store.get("pm_commissions", self.commission_id)
        self.assertEqual(commission["eligible_amount"], 80)
        self.assertEqual(commission["refunded_amount"], 20)

    def test_duplicate_confirmation_has_one_consequence(self):
        service = RefundService(self.store, self.audit, self.identities)
        refund = service.request(self.maker, self.payment_id, 100, "INR", "test", "refund-1")
        self.approve(refund["approval_request_id"])
        service.confirm_sandbox(self.maker, refund["id"], "provider-1", {"verified": True})
        service.confirm_sandbox(self.maker, refund["id"], "provider-1", {"verified": True})
        self.assertEqual(len(self.store.list("p6_financial_events", "event_type='REFUND_CONFIRMED'")), 1)
        self.assertEqual(self.store.get("pm_commissions", self.commission_id)["refunded_amount"], 10)
        recon = self.store.list("p6_refund_reconciliations", "refund_id=?", (refund["id"],))
        self.assertEqual(recon[0]["status"], "MATCHED")

    def test_wrong_currency_and_over_refund_fail(self):
        service = RefundService(self.store, self.audit, self.identities)
        with self.assertRaisesRegex(CommercialSecurityError, "currency"):
            service.request(self.maker, self.payment_id, 10, "USD", "x", "bad-currency")
        with self.assertRaisesRegex(CommercialSecurityError, "exceeds"):
            service.request(self.maker, self.payment_id, 1001, "INR", "x", "too-much")


class SubscriptionAndPayableTests(FinancialFlowsCase):
    def test_cross_organization_subscription_creation_is_denied(self):
        """Tenant-isolation adversarial test: `clients` predates Phase 6's
        organization layer and carries no organization_id of its own. A
        commercial identity from a DIFFERENT organization than the one that
        actually owns this client (via comm_organizations) must never be
        able to create a recurring subscription against it."""
        other_identity_id = self.identities.create("acme", "STAFF", "maker", "Acme Maker", "FINANCE_OPERATOR", "test")
        from falguna.phase6_commercial import AccessContext
        other = AccessContext(other_identity_id, "acme")
        service = SubscriptionService(self.store, self.audit, self.identities)
        with self.assertRaisesRegex(CommercialSecurityError, "does not belong to this organization"):
            service.create(other, self.client_id, "Hijacked Plan", 500, "INR", "MONTHLY",
                           "2026-11-01", "token", "mandate")

    def test_recurring_invoice_is_idempotent_for_due_cycle(self):
        service = SubscriptionService(self.store, self.audit, self.identities)
        sub = service.create(self.maker, self.client_id, "Monthly maintenance", 100, "INR", "MONTHLY",
                             "2026-10-01", "token_ref_1", "mandate_ref_1", self.opp_id)
        first = service.create_due_invoice(self.maker, sub["id"], "2026-10-01")
        with self.assertRaisesRegex(CommercialSecurityError, "not due"):
            service.create_due_invoice(self.maker, sub["id"], "2026-10-01")
        self.assertEqual(first["status"], "DRAFT")

    def test_successful_monthly_recurring_collection_end_to_end(self):
        service = SubscriptionService(self.store, self.audit, self.identities)
        sub = service.create(self.maker, self.client_id, "Monthly maintenance", 100, "INR", "MONTHLY",
                             "2026-10-01", "token_ref_1", "mandate_ref_1", self.opp_id)
        invoice = service.create_due_invoice(self.maker, sub["id"], "2026-10-01")
        attempt = service.create_autopay_attempt(self.maker, sub["id"], "cycle-1")
        payment = PaymentOrchestrator(self.store, self.audit, self.identities)
        payment.apply_verified_event(self.maker, attempt["payment_intent_id"], "CAPTURED", 100, "INR",
                                     {"verified": True}, "capture-1", "provider-capture-1")
        payment.apply_verified_event(self.maker, attempt["payment_intent_id"], "SETTLED", 100, "INR",
                                     {"verified": True}, "settled-1", "provider-settled-1")
        CommercialOperations(self.store, self.audit, self.identities).reconcile_settlement(
            self.maker, attempt["payment_intent_id"], "settlement-1", 100, "INR", {"verified": True}, "recon-1")
        collected = service.sync_collected(self.maker, attempt["id"])
        self.assertEqual(collected["status"], "COLLECTED")
        self.assertEqual(self.store.get("rh_invoices", invoice["id"])["status"], "PAID")
        self.assertEqual(len(self.store.list("p6_receipts", "invoice_id=?", (invoice["id"],))), 1)
        self.assertEqual(len(self.store.list("p6_financial_events", "event_type='SETTLED_COLLECTION'")), 1)

    def test_failed_retry_pause_resume_and_cancel_are_honest(self):
        service = SubscriptionService(self.store, self.audit, self.identities)
        sub = service.create(self.maker, self.client_id, "Monthly", 100, "INR", "MONTHLY",
                             "2026-10-01", "token", "mandate", self.opp_id)
        service.create_due_invoice(self.maker, sub["id"], "2026-10-01")
        first = service.create_autopay_attempt(self.maker, sub["id"], "try-1")
        service.mark_attempt_failed(self.maker, first["id"], "synthetic decline")
        retry = service.create_autopay_attempt(self.maker, sub["id"], "try-2")
        self.assertEqual(retry["attempt_number"], 2)
        service.set_status(self.maker, sub["id"], "PAUSED")
        with self.assertRaisesRegex(CommercialSecurityError, "active"):
            service.create_autopay_attempt(self.maker, sub["id"], "blocked")
        service.set_status(self.maker, sub["id"], "ACTIVE")
        self.assertEqual(service.create_autopay_attempt(self.maker, sub["id"], "try-2")["id"], retry["id"])
        service.set_status(self.maker, sub["id"], "CANCELLED")
        with self.assertRaisesRegex(CommercialSecurityError, "terminal"):
            service.set_status(self.maker, sub["id"], "ACTIVE")

    def test_payable_requires_full_approval_and_never_executes(self):
        service = PayableService(self.store, self.audit, self.identities)
        payable = service.create(self.maker, "VENDOR", "vendor:synthetic", 300, "INR", {"expense": "hosting"}, "payable-1")
        self.assertIn("NEW_BENEFICIARY", self.store.get("p6_approval_requests", payable["approval_request_id"])["risk_flags_json"])
        with self.assertRaises(CommercialSecurityError):
            service.mark_execution_ready(self.maker, payable["id"])
        self.approve(payable["approval_request_id"])
        ready = service.mark_execution_ready(self.maker, payable["id"])
        self.assertEqual(ready["status"], "SANDBOX_EXECUTION_READY")
        self.assertEqual(self.store.list("p6_financial_events", "source_type='PAYABLE'"), [])


class CommercialEconomicsTests(FinancialFlowsCase):
    def test_channel_analytics_only_computes_contribution_with_cost_evidence(self):
        service = CommercialEconomicsService(self.store, self.audit, self.identities)
        service.record(self.maker, self.opp_id, "REFERRAL_PARTNER", {"campaign": "synthetic"},
                       self.partner_id, quoted_value=1000, contracted_value=1000)
        channel = service.analytics(self.maker)["channels"]["REFERRAL_PARTNER"]
        self.assertEqual(channel["settled_revenue"], 1000)
        self.assertIsNone(channel["known_contribution"])
        service.record(self.maker, self.opp_id, "REFERRAL_PARTNER", {"campaign": "synthetic"},
                       self.partner_id, quoted_value=1000, contracted_value=1000,
                       known_delivery_cost=400, cost_evidence={"synthetic": True}, gateway_fee=20)
        channel = service.analytics(self.maker)["channels"]["REFERRAL_PARTNER"]
        self.assertEqual(channel["known_contribution"], 580)

    def test_known_cost_without_evidence_is_rejected(self):
        with self.assertRaisesRegex(CommercialSecurityError, "evidence"):
            CommercialEconomicsService(self.store, self.audit, self.identities).record(
                self.maker, self.opp_id, "WEBSITE_INBOUND", {}, known_delivery_cost=1)


if __name__ == "__main__":
    unittest.main()
