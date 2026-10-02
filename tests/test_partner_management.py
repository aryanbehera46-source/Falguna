"""Sales Partner Pilot V1 (Priority 4) -- falguna/partner_management.py.

Internal pilot foundation: no public signup, no payout execution, no
payment provider integration, synthetic data only. These tests cover the
partner lifecycle, deterministic duplicate detection, time-limited
attribution, and provisional commission accounting (including the full
persisted commercial chain reusing the existing Revenue & Delivery Engine),
plus the HTTP layer's own enforcement of every gate (never just a UI-level
check).
"""

import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import json
from http.server import ThreadingHTTPServer
from pathlib import Path

from falguna.billing import BillingStore
from falguna.hq_web import TTTHQHandler
from falguna.onboarding import ONBOARDING_ITEM_TYPES, OnboardingStore
from falguna.partner_management import (
    CommissionError, CommissionStore, PartnerError, PartnerStore, ReferralError, ReferralStore,
)
from falguna.revenue_delivery import RevenueDeliveryService
from falguna.revenue_hunter import ActiveJobStore, ProposalStore
from falguna.runtime import open_control_plane
from falguna.sales_ops import ClosingService, SalesPolicyStore
from falguna.ttt_hq import NeedsAryanQueue


class PartnerManagementTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(self.repo)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "f@test.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "F Test"], check=True)
        (self.repo / "README.md").write_text("seed\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-m", "seed"], check=True, capture_output=True)
        self.control, self.store = open_control_plane(self.repo)
        self.audit = self.control.audit
        self.partners = PartnerStore(self.store, self.audit)
        self.referrals = ReferralStore(self.store, self.audit, partners=self.partners)
        self.commissions = CommissionStore(self.store, self.audit)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def _approved_partner(self, email="jane@partnerco.com", name="Jane Partner"):
        pid = self.partners.register({"full_name": name, "email": email, "agreement_accepted": True}, actor="Aryan")
        self.partners.approve(pid, actor="Aryan")
        return pid


class PartnerLifecycleTests(PartnerManagementTestCase):
    def test_registration_requires_agreement_acceptance(self):
        with self.assertRaisesRegex(PartnerError, "agreement"):
            self.partners.register({"full_name": "X", "email": "x@y.com"}, actor="Aryan")

    def test_new_partner_starts_pending_and_cannot_register_referrals(self):
        pid = self.partners.register({"full_name": "X", "email": "x@y.com", "agreement_accepted": True}, actor="Aryan")
        self.assertEqual(self.partners.get(pid)["status"], "PENDING")
        with self.assertRaisesRegex(ReferralError, "APPROVED"):
            self.referrals.register(pid, {"prospect_name": "Acme", "requested_service": "web"}, actor="jane")

    def test_partner_approval_transitions_pending_to_approved_with_audit_trail(self):
        pid = self.partners.register({"full_name": "X", "email": "x@y.com", "agreement_accepted": True}, actor="Aryan")
        approved = self.partners.approve(pid, actor="Aryan")
        self.assertEqual(approved["status"], "APPROVED")
        self.assertIsNotNone(approved["approved_at"])
        history = self.partners.get(pid)["status_history"]
        self.assertEqual([h["to_status"] for h in history], ["PENDING", "APPROVED"])

    def test_approving_an_already_approved_partner_is_rejected(self):
        pid = self._approved_partner()
        with self.assertRaisesRegex(PartnerError, "PENDING"):
            self.partners.approve(pid, actor="Aryan")

    def test_partner_suspension_requires_a_reason_and_blocks_new_referrals(self):
        pid = self._approved_partner()
        with self.assertRaisesRegex(PartnerError, "reason"):
            self.partners.suspend(pid, actor="Aryan", reason="")
        suspended = self.partners.suspend(pid, actor="Aryan", reason="policy violation")
        self.assertEqual(suspended["status"], "SUSPENDED")
        with self.assertRaisesRegex(ReferralError, "APPROVED"):
            self.referrals.register(pid, {"prospect_name": "Acme", "requested_service": "web"}, actor="jane")

    def test_reinstating_a_suspended_partner_restores_referral_registration(self):
        pid = self._approved_partner()
        self.partners.suspend(pid, actor="Aryan", reason="review")
        self.partners.reinstate(pid, actor="Aryan")
        self.assertEqual(self.partners.get(pid)["status"], "APPROVED")
        referral_id = self.referrals.register(pid, {"prospect_name": "Acme", "requested_service": "web"}, actor="jane")
        self.assertTrue(referral_id)

    def test_terminated_partner_permanently_blocks_new_referrals(self):
        pid = self._approved_partner()
        self.partners.terminate(pid, actor="Aryan", reason="contract ended")
        self.assertEqual(self.partners.get(pid)["status"], "TERMINATED")
        with self.assertRaisesRegex(ReferralError, "APPROVED"):
            self.referrals.register(pid, {"prospect_name": "Acme", "requested_service": "web"}, actor="jane")
        # TERMINATED is a true dead end -- cannot be reinstated back to APPROVED.
        with self.assertRaises(PartnerError):
            self.partners.reinstate(pid, actor="Aryan")

    def test_terminating_an_already_terminated_partner_is_rejected(self):
        pid = self._approved_partner()
        self.partners.terminate(pid, actor="Aryan", reason="done")
        with self.assertRaisesRegex(PartnerError, "already TERMINATED"):
            self.partners.terminate(pid, actor="Aryan", reason="again")

    def test_list_filters_by_status(self):
        approved = self._approved_partner("a@x.com", "A")
        pending = self.partners.register({"full_name": "B", "email": "b@x.com", "agreement_accepted": True}, actor="Aryan")
        self.assertEqual([p["id"] for p in self.partners.list("APPROVED")], [approved])
        self.assertEqual([p["id"] for p in self.partners.list("PENDING")], [pending])


class DuplicateDetectionTests(PartnerManagementTestCase):
    def setUp(self):
        super().setUp()
        self.partner_a = self._approved_partner("alice@partnerco.com", "Alice")
        self.partner_b = self._approved_partner("bob@partnerco.com", "Bob")

    def test_duplicate_email_flags_both_referrals_for_review(self):
        r1 = self.referrals.register(self.partner_a, {"prospect_name": "Acme", "contact_email": "ceo@acme.com", "requested_service": "web"}, actor="alice")
        r2 = self.referrals.register(self.partner_b, {"prospect_name": "Acme Inc", "contact_email": "CEO@Acme.com", "requested_service": "web"}, actor="bob")
        self.assertTrue(self.referrals.get(r1)["duplicate_flag"])
        self.assertTrue(self.referrals.get(r2)["duplicate_flag"])
        reviews = self.referrals.list_duplicate_reviews("OPEN")
        self.assertEqual(len(reviews), 1)
        self.assertIn("email", reviews[0]["detected_reason"])

    def test_duplicate_phone_flags_both_referrals(self):
        r1 = self.referrals.register(self.partner_a, {"prospect_name": "Acme", "contact_phone": "(555) 123-4567", "requested_service": "web"}, actor="alice")
        r2 = self.referrals.register(self.partner_b, {"prospect_name": "Acme LLC", "contact_phone": "555-123-4567", "requested_service": "web"}, actor="bob")
        self.assertTrue(self.referrals.get(r1)["duplicate_flag"])
        self.assertTrue(self.referrals.get(r2)["duplicate_flag"])
        reviews = self.referrals.list_duplicate_reviews("OPEN")
        self.assertIn("phone", reviews[0]["detected_reason"])

    def test_duplicate_domain_and_organization_flags_both_referrals(self):
        r1 = self.referrals.register(self.partner_a, {"prospect_name": "Acme", "organization_name": "Acme Corp", "contact_email": "ceo@acme.io", "requested_service": "web"}, actor="alice")
        r2 = self.referrals.register(self.partner_b, {"prospect_name": "Acme Team", "organization_name": "ACME CORP", "contact_email": "cfo@acme.io", "requested_service": "web"}, actor="bob")
        self.assertTrue(self.referrals.get(r1)["duplicate_flag"])
        self.assertTrue(self.referrals.get(r2)["duplicate_flag"])
        reasons = self.referrals.list_duplicate_reviews("OPEN")[0]["detected_reason"]
        self.assertIn("domain", reasons)
        self.assertIn("organization_name", reasons)

    def test_no_conflict_leaves_referral_unflagged(self):
        r1 = self.referrals.register(self.partner_a, {"prospect_name": "Acme", "contact_email": "a@acme.com", "requested_service": "web"}, actor="alice")
        r2 = self.referrals.register(self.partner_b, {"prospect_name": "Globex", "contact_email": "b@globex.com", "requested_service": "web"}, actor="bob")
        self.assertFalse(self.referrals.get(r1)["duplicate_flag"])
        self.assertFalse(self.referrals.get(r2)["duplicate_flag"])
        self.assertEqual(self.referrals.list_duplicate_reviews("OPEN"), [])

    def test_conflict_never_auto_awards_ownership(self):
        r1 = self.referrals.register(self.partner_a, {"prospect_name": "Acme", "contact_email": "a@acme.com", "requested_service": "web"}, actor="alice")
        r2 = self.referrals.register(self.partner_b, {"prospect_name": "Acme", "contact_email": "a@acme.com", "requested_service": "web"}, actor="bob")
        self.assertEqual(self.referrals.get(r1)["attribution_status"], "PENDING_REVIEW")
        self.assertEqual(self.referrals.get(r2)["attribution_status"], "PENDING_REVIEW")
        with self.assertRaisesRegex(ReferralError, "duplicate review"):
            self.referrals.attribute(r1, actor="Aryan")

    def test_competing_referral_claims_award_decision_rejects_the_loser(self):
        r1 = self.referrals.register(self.partner_a, {"prospect_name": "Acme", "contact_email": "a@acme.com", "requested_service": "web"}, actor="alice")
        r2 = self.referrals.register(self.partner_b, {"prospect_name": "Acme", "contact_email": "a@acme.com", "requested_service": "web"}, actor="bob")
        review = self.referrals.list_duplicate_reviews("OPEN")[0]
        resolved = self.referrals.resolve_duplicate_review(review["id"], actor="Aryan", decision="AWARD", awarded_referral_id=r1, reason="alice referred first")
        self.assertEqual(resolved["status"], "RESOLVED")
        self.assertEqual(resolved["reviewer"], "Aryan")
        self.assertEqual(resolved["decision"], "AWARD")
        self.assertIsNotNone(resolved["resolved_at"])
        self.assertEqual(self.referrals.get(r2)["attribution_status"], "REJECTED")
        self.assertFalse(self.referrals.get(r1)["duplicate_flag"])
        attributed = self.referrals.attribute(r1, actor="Aryan")
        self.assertEqual(attributed["attribution_status"], "ATTRIBUTED")

    def test_award_over_an_already_attributed_referral_is_refused(self):
        r1 = self.referrals.register(self.partner_a, {"prospect_name": "Acme", "contact_email": "a@acme.com", "requested_service": "web"}, actor="alice")
        self.referrals.attribute(r1, actor="Aryan")  # no conflict yet, attributes cleanly
        r2 = self.referrals.register(self.partner_b, {"prospect_name": "Acme", "contact_email": "a@acme.com", "requested_service": "web"}, actor="bob")
        review = self.referrals.list_duplicate_reviews("OPEN")[0]
        with self.assertRaisesRegex(ReferralError, "already ATTRIBUTED"):
            self.referrals.resolve_duplicate_review(review["id"], actor="Aryan", decision="AWARD", awarded_referral_id=r2, reason="bob claims it")
        # the original, valid attribution is completely untouched
        self.assertEqual(self.referrals.get(r1)["attribution_status"], "ATTRIBUTED")

    def test_existing_customer_linkage_is_flagged_as_a_duplicate_signal(self):
        from falguna.sales_ops import ClientStore
        ClientStore(self.store, self.audit).upsert("Acme Corp", actor="Aryan")
        r1 = self.referrals.register(self.partner_a, {"prospect_name": "Acme Corp", "organization_name": "Acme Corp", "requested_service": "web"}, actor="alice")
        self.assertTrue(self.referrals.get(r1)["duplicate_flag"])
        self.assertIn("existing_customer", self.referrals.list_duplicate_reviews("OPEN")[0]["detected_reason"])


class AttributionTests(PartnerManagementTestCase):
    def setUp(self):
        super().setUp()
        self.partner_id = self._approved_partner()
        self.referral_id = self.referrals.register(self.partner_id, {"prospect_name": "Acme", "requested_service": "web"}, actor="jane")

    def test_attribution_sets_window_and_links_a_real_opportunity(self):
        attributed = self.referrals.attribute(self.referral_id, actor="Aryan", attribution_days=30)
        self.assertEqual(attributed["attribution_status"], "ATTRIBUTED")
        self.assertIsNotNone(attributed["attribution_start"])
        self.assertIsNotNone(attributed["attribution_expiry"])
        opportunity_id = attributed["opportunity_id"]
        self.assertTrue(opportunity_id)
        opportunity = self.store.get("rh_opportunities", opportunity_id)
        self.assertEqual(opportunity["source"], "partner_referral")

    def test_attribution_is_never_silently_overwritten(self):
        self.referrals.attribute(self.referral_id, actor="Aryan")
        with self.assertRaisesRegex(ReferralError, "PENDING_REVIEW"):
            self.referrals.attribute(self.referral_id, actor="Aryan")

    def test_attribution_expiry_is_explicit_not_automatic(self):
        attributed = self.referrals.attribute(self.referral_id, actor="Aryan", attribution_days=30)
        # Force the expiry into the past directly (no background clock in
        # this codebase -- same convention as billing.check_overdue), then
        # prove nothing changes until check_expiry() is actually called.
        self.store.update("pm_referrals", self.referral_id, attribution_expiry="2000-01-01T00:00:00+00:00")
        self.assertEqual(self.referrals.get(self.referral_id)["attribution_status"], "ATTRIBUTED")
        expired = self.referrals.check_expiry(self.referral_id)
        self.assertEqual(expired["attribution_status"], "EXPIRED")

    def test_check_expiry_is_a_noop_before_the_window_passes(self):
        self.referrals.attribute(self.referral_id, actor="Aryan", attribution_days=90)
        unchanged = self.referrals.check_expiry(self.referral_id)
        self.assertEqual(unchanged["attribution_status"], "ATTRIBUTED")

    def test_attribution_dispute_pauses_then_can_be_reinstated(self):
        self.referrals.attribute(self.referral_id, actor="Aryan")
        with self.assertRaisesRegex(ReferralError, "reason"):
            self.referrals.raise_dispute(self.referral_id, actor="x", reason="")
        disputed = self.referrals.raise_dispute(self.referral_id, actor="Bob (partner)", reason="claims this was his lead")
        self.assertEqual(disputed["attribution_status"], "DISPUTED")
        resolved = self.referrals.resolve_dispute(self.referral_id, actor="Aryan", decision="REINSTATE", reason="original claim upheld")
        self.assertEqual(resolved["attribution_status"], "ATTRIBUTED")

    def test_attribution_dispute_can_resolve_to_rejected(self):
        self.referrals.attribute(self.referral_id, actor="Aryan")
        self.referrals.raise_dispute(self.referral_id, actor="Bob (partner)", reason="wrong attribution")
        resolved = self.referrals.resolve_dispute(self.referral_id, actor="Aryan", decision="REJECT", reason="Bob was correct")
        self.assertEqual(resolved["attribution_status"], "REJECTED")

    def test_cannot_dispute_a_referral_that_was_never_attributed(self):
        with self.assertRaisesRegex(ReferralError, "ATTRIBUTED"):
            self.referrals.raise_dispute(self.referral_id, actor="Bob", reason="too early")

    def test_reject_is_available_from_pending_review(self):
        rejected = self.referrals.reject(self.referral_id, actor="Aryan", reason="not a fit")
        self.assertEqual(rejected["attribution_status"], "REJECTED")
        with self.assertRaisesRegex(ReferralError, "PENDING_REVIEW"):
            self.referrals.attribute(self.referral_id, actor="Aryan")


class CommissionAccountingTests(PartnerManagementTestCase):
    """Drives the real, persisted commercial chain: Partner -> Referral ->
    CRM opportunity -> human-approved quote -> accepted project -> Falguna
    Workforce (Active Job) -> evidence-backed handover -> DRAFT invoice ->
    verified cleared payment -> commission eligibility -- composing the
    existing, already-tested Revenue & Delivery Engine primitives exactly
    as tests/test_revenue_delivery.py does, never a parallel flow."""

    def setUp(self):
        super().setUp()
        self.partner_id = self._approved_partner()
        self.referral_id = self.referrals.register(self.partner_id, {
            "prospect_name": "Acme Corp", "organization_name": "Acme Corp", "requested_service": "Website", "estimated_value": 2400,
        }, actor="jane")

    def _drive_to_won_active_job(self, final_price=2400.0):
        attributed = self.referrals.attribute(self.referral_id, actor="Aryan")
        opportunity_id = attributed["opportunity_id"]
        queue = NeedsAryanQueue(self.store, self.audit, self.control)
        proposal = ProposalStore(self.store, self.audit, queue).generate(opportunity_id, "short", actor="Aryan")
        ProposalStore(self.store, self.audit, queue).mark_approved(proposal["proposal_id"], "Aryan")
        SalesPolicyStore(self.store).save({"allowed_currencies": ["USD"]})
        ClosingService(self.store, self.audit, needs_aryan=queue).close(
            opportunity_id, "Aryan", client_name="Acme Corp", final_scope="Launch site",
            final_price=final_price, currency="USD", acceptance_criteria="Automated + browser QA pass",
        )
        onboarding = OnboardingStore(self.store, self.audit)
        onboarding.init_checklist(opportunity_id)
        for item_type in ONBOARDING_ITEM_TYPES:
            kwargs = {"notes": "vault reference"} if item_type == "credentials_access" else {"value_text": "confirmed"}
            onboarding.set_item(opportunity_id, item_type, "RECEIVED", **kwargs)
        ActiveJobStore(self.store, self.audit).create_from_won_opportunity(opportunity_id)
        return opportunity_id

    def _draft_invoice(self, opportunity_id):
        result = RevenueDeliveryService(self.store, self.audit).prepare_handover_and_invoice_draft(
            opportunity_id, "Aryan", evidence={"tests": "42 passed", "browser_qa": "desktop + mobile"},
            qa_checklist={"delivery_verified": True, "independent_qa": True, "acceptance_criteria_met": True, "handover_ready": True},
        )
        return result["invoice_id"]

    def test_referral_registration_creates_no_commission_row_yet(self):
        self.assertIsNone(self.commissions.get_for_referral(self.referral_id))

    def test_quote_to_project_linkage_produces_a_real_opportunity_and_active_job(self):
        opportunity_id = self._drive_to_won_active_job()
        opportunity = self.store.get("rh_opportunities", opportunity_id)
        self.assertEqual(opportunity["stage"], "Won")
        jobs = self.store.list("rh_active_jobs", "opportunity_id=?", (opportunity_id,))
        self.assertEqual(len(jobs), 1)
        commission = self.commissions.ensure_for_referral(self.referral_id, actor="Aryan")
        self.assertEqual(commission["opportunity_id"], opportunity_id)

    def test_draft_invoice_alone_never_creates_eligible_commission(self):
        opportunity_id = self._drive_to_won_active_job()
        self._draft_invoice(opportunity_id)
        commission = self.commissions.sync_from_invoice(self.referral_id, actor="Aryan")
        self.assertEqual(commission["status"], "NOT_ELIGIBLE")
        self.assertEqual(commission["eligible_amount"], 0.0)

    def test_sent_invoice_without_payment_is_only_provisional(self):
        opportunity_id = self._drive_to_won_active_job()
        invoice_id = self._draft_invoice(opportunity_id)
        billing = BillingStore(self.store, self.audit)
        billing.mark_ready(invoice_id, "Aryan")
        billing.mark_sent(invoice_id, "Aryan")
        commission = self.commissions.sync_from_invoice(self.referral_id, actor="Aryan")
        self.assertEqual(commission["status"], "PROVISIONAL")
        self.assertEqual(commission["eligible_amount"], 0.0)

    def test_cleared_payment_makes_commission_eligible_at_the_configured_rate(self):
        opportunity_id = self._drive_to_won_active_job(final_price=2400.0)
        invoice_id = self._draft_invoice(opportunity_id)
        billing = BillingStore(self.store, self.audit)
        billing.mark_ready(invoice_id, "Aryan")
        billing.mark_sent(invoice_id, "Aryan")
        billing.record_payment(invoice_id, 2400.0, "Aryan", evidence={"bank_ref": "TXN-001"})
        commission = self.commissions.sync_from_invoice(self.referral_id, actor="Aryan")
        self.assertEqual(commission["status"], "ELIGIBLE")
        self.assertAlmostEqual(commission["eligible_amount"], 240.0)  # 10% default rate

    def test_replayed_payment_observation_is_idempotent(self):
        opportunity_id = self._drive_to_won_active_job()
        invoice_id = self._draft_invoice(opportunity_id)
        billing = BillingStore(self.store, self.audit)
        billing.mark_ready(invoice_id, "Aryan")
        billing.mark_sent(invoice_id, "Aryan")
        billing.record_payment(invoice_id, 2400.0, "Aryan", evidence={"bank_ref": "TXN-002"})
        first = self.commissions.sync_from_invoice(self.referral_id, actor="Aryan")
        events_after_first = len(self.commissions.events_for(first["id"]))
        second = self.commissions.sync_from_invoice(self.referral_id, actor="Aryan")
        events_after_second = len(self.commissions.events_for(first["id"]))
        third = self.commissions.sync_from_invoice(self.referral_id, actor="Aryan")
        self.assertEqual(events_after_first, events_after_second, "a no-op replay must not append a redundant event")
        self.assertEqual(first["status"], "ELIGIBLE")
        self.assertEqual(second["status"], "ELIGIBLE")
        self.assertEqual(third["status"], "ELIGIBLE")
        self.assertEqual(first["eligible_amount"], second["eligible_amount"])
        self.assertEqual(events_after_second, len(self.commissions.events_for(first["id"])))

    def test_refund_event_is_idempotent_when_replayed_with_the_same_reference(self):
        opportunity_id = self._drive_to_won_active_job()
        invoice_id = self._draft_invoice(opportunity_id)
        billing = BillingStore(self.store, self.audit)
        billing.mark_ready(invoice_id, "Aryan")
        billing.mark_sent(invoice_id, "Aryan")
        billing.record_payment(invoice_id, 2400.0, "Aryan", evidence={"bank_ref": "TXN-003"})
        self.commissions.sync_from_invoice(self.referral_id, actor="Aryan")
        first = self.commissions.record_refund(self.referral_id, actor="Aryan", amount=240.0, evidence={"ref": "RF-1"}, reason="chargeback", event_ref="RF-1")
        events_after_first = len(self.commissions.events_for(first["id"]))
        replay = self.commissions.record_refund(self.referral_id, actor="Aryan", amount=240.0, evidence={"ref": "RF-1"}, reason="chargeback", event_ref="RF-1")
        events_after_replay = len(self.commissions.events_for(first["id"]))
        self.assertEqual(events_after_first, events_after_replay)
        self.assertEqual(first["status"], replay["status"])

    def test_full_chargeback_reverses_commission(self):
        opportunity_id = self._drive_to_won_active_job()
        invoice_id = self._draft_invoice(opportunity_id)
        billing = BillingStore(self.store, self.audit)
        billing.mark_ready(invoice_id, "Aryan")
        billing.mark_sent(invoice_id, "Aryan")
        billing.record_payment(invoice_id, 2400.0, "Aryan", evidence={"bank_ref": "TXN-004"})
        self.commissions.sync_from_invoice(self.referral_id, actor="Aryan")
        refunded = self.commissions.record_refund(self.referral_id, actor="Aryan", amount=240.0, evidence={"ref": "RF-2"}, reason="full chargeback")
        self.assertEqual(refunded["status"], "REVERSED")
        self.assertEqual(refunded["eligible_amount"], 0.0)

    def test_partial_refund_holds_commission_rather_than_reversing_it(self):
        opportunity_id = self._drive_to_won_active_job()
        invoice_id = self._draft_invoice(opportunity_id)
        billing = BillingStore(self.store, self.audit)
        billing.mark_ready(invoice_id, "Aryan")
        billing.mark_sent(invoice_id, "Aryan")
        billing.record_payment(invoice_id, 2400.0, "Aryan", evidence={"bank_ref": "TXN-005"})
        self.commissions.sync_from_invoice(self.referral_id, actor="Aryan")
        held = self.commissions.record_refund(self.referral_id, actor="Aryan", amount=100.0, evidence={"ref": "RF-3"}, reason="partial refund")
        self.assertEqual(held["status"], "HELD")
        self.assertAlmostEqual(held["eligible_amount"], 140.0)

    def test_refund_requires_evidence(self):
        opportunity_id = self._drive_to_won_active_job()
        invoice_id = self._draft_invoice(opportunity_id)
        billing = BillingStore(self.store, self.audit)
        billing.mark_ready(invoice_id, "Aryan")
        billing.mark_sent(invoice_id, "Aryan")
        billing.record_payment(invoice_id, 2400.0, "Aryan", evidence={"bank_ref": "TXN-006"})
        self.commissions.sync_from_invoice(self.referral_id, actor="Aryan")
        with self.assertRaisesRegex(CommissionError, "evidence"):
            self.commissions.record_refund(self.referral_id, actor="Aryan", amount=240.0, evidence=None, reason="no proof")

    def test_manual_hold_and_release_round_trip(self):
        opportunity_id = self._drive_to_won_active_job()
        invoice_id = self._draft_invoice(opportunity_id)
        billing = BillingStore(self.store, self.audit)
        billing.mark_ready(invoice_id, "Aryan")
        billing.mark_sent(invoice_id, "Aryan")
        self.commissions.sync_from_invoice(self.referral_id, actor="Aryan")
        held = self.commissions.hold(self.referral_id, actor="Aryan", reason="dispute under review")
        self.assertEqual(held["status"], "HELD")
        # while HELD, sync_from_invoice must never silently override the hold
        self.assertEqual(self.commissions.sync_from_invoice(self.referral_id, actor="Aryan")["status"], "HELD")
        released = self.commissions.release_hold(self.referral_id, actor="Aryan")
        self.assertEqual(released["status"], "PROVISIONAL")

    def test_no_payout_state_is_ever_reachable_by_any_code_path(self):
        opportunity_id = self._drive_to_won_active_job()
        invoice_id = self._draft_invoice(opportunity_id)
        billing = BillingStore(self.store, self.audit)
        billing.mark_ready(invoice_id, "Aryan")
        billing.mark_sent(invoice_id, "Aryan")
        billing.record_payment(invoice_id, 2400.0, "Aryan", evidence={"bank_ref": "TXN-007"})
        commission = self.commissions.sync_from_invoice(self.referral_id, actor="Aryan")
        self.assertNotEqual(commission["status"], "PAID")
        # No public method on CommissionStore ever assigns PAID -- it is a
        # defined-but-unreachable future state, matching the scope-
        # discipline instruction that payout execution is not this pass's
        # job. Scan every store.update(...) call in the module for a
        # literal status="PAID"/'PAID' assignment; the enum declaration
        # itself (COMMISSION_STATUSES) is the only permitted mention.
        import inspect
        import re as _re
        source = inspect.getsource(CommissionStore)
        assignments = _re.findall(r'status\s*=\s*["\']PAID["\']', source)
        self.assertEqual(assignments, [], "CommissionStore must never assign the PAID status anywhere")


class DataIsolationTests(PartnerManagementTestCase):
    """Section 8: one partner cannot see another partner's private referral
    data. There is no partner-facing self-service login in this internal
    pilot pass (out of scope: no public signup), so isolation is enforced
    at the query layer every list endpoint goes through -- this proves that
    layer never leaks rows across partner_id."""

    def test_partner_scoped_referral_listing_never_includes_another_partners_rows(self):
        partner_a = self._approved_partner("a@x.com", "A")
        partner_b = self._approved_partner("b@x.com", "B")
        ref_a = self.referrals.register(partner_a, {"prospect_name": "OnlyA", "requested_service": "web"}, actor="a")
        ref_b = self.referrals.register(partner_b, {"prospect_name": "OnlyB", "requested_service": "web"}, actor="b")
        listing_a = self.referrals.list(partner_id=partner_a)
        listing_b = self.referrals.list(partner_id=partner_b)
        self.assertEqual([r["id"] for r in listing_a], [ref_a])
        self.assertEqual([r["id"] for r in listing_b], [ref_b])

    def test_commission_listing_is_scoped_by_partner(self):
        partner_a = self._approved_partner("a@x.com", "A")
        partner_b = self._approved_partner("b@x.com", "B")
        ref_a = self.referrals.register(partner_a, {"prospect_name": "OnlyA", "requested_service": "web"}, actor="a")
        ref_b = self.referrals.register(partner_b, {"prospect_name": "OnlyB", "requested_service": "web"}, actor="b")
        self.referrals.attribute(ref_a, actor="Aryan")
        self.referrals.attribute(ref_b, actor="Aryan")
        self.commissions.ensure_for_referral(ref_a, actor="Aryan")
        self.commissions.ensure_for_referral(ref_b, actor="Aryan")
        only_a = self.commissions.list(partner_id=partner_a)
        self.assertEqual(len(only_a), 1)
        self.assertEqual(only_a[0]["partner_id"], partner_a)


class _LivePartnerHQServerCase(unittest.TestCase):
    """Same _LiveServerCase pattern already established in test_hq_web.py /
    test_browser_web.py -- a real TTTHQHandler on a dynamic ephemeral port,
    proving these gates hold at the HTTP layer itself and cannot be
    bypassed by a direct API call that skips whatever UI would otherwise
    have disabled a button."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(self.repo)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "f@test.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "F Test"], check=True)
        (self.repo / "README.md").write_text("seed\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-m", "seed"], check=True, capture_output=True)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), TTTHQHandler)
        self.server.app_root = self.repo
        self.server.falguna_url = "http://127.0.0.1:1"
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self._wait_ready()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def _wait_ready(self):
        for _ in range(100):
            try:
                status, _ = self._get("/api/config")
                if status == 200:
                    return
            except Exception:
                pass
            time.sleep(0.05)
        self.fail("TTT HQ server did not become ready")

    def _get(self, path):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def _post(self, path, body):
        data = json.dumps(body).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method="POST", headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())


class AuthorizationBypassOverHTTPTests(_LivePartnerHQServerCase):
    """Section 8: 'approval cannot be bypassed via direct API calls' --
    proves the guard lives in the store layer the HTTP handler calls, not
    only in client-side UI state, by calling the API directly."""

    def test_referral_registration_for_a_pending_partner_is_rejected_by_the_api_itself(self):
        status, body = self._post("/api/partners", {"full_name": "Jane", "email": "jane@partnerco.com", "agreement_accepted": True})
        self.assertEqual(status, 201)
        partner_id = body["partner_id"]
        status, body = self._post("/api/referrals", {"partner_id": partner_id, "prospect_name": "Acme", "requested_service": "web"})
        self.assertEqual(status, 400)
        self.assertIn("APPROVED", body["error"])

    def test_referral_registration_for_a_suspended_partner_is_rejected_by_the_api_itself(self):
        status, body = self._post("/api/partners", {"full_name": "Jane", "email": "jane@partnerco.com", "agreement_accepted": True})
        partner_id = body["partner_id"]
        self._post(f"/api/partners/{partner_id}/approve", {"actor": "Aryan"})
        self._post(f"/api/partners/{partner_id}/suspend", {"actor": "Aryan", "reason": "review"})
        status, body = self._post("/api/referrals", {"partner_id": partner_id, "prospect_name": "Acme", "requested_service": "web"})
        self.assertEqual(status, 400)
        self.assertIn("APPROVED", body["error"])

    def test_registering_a_referral_for_an_unknown_partner_id_is_rejected(self):
        status, body = self._post("/api/referrals", {"partner_id": "does-not-exist", "prospect_name": "Acme", "requested_service": "web"})
        self.assertEqual(status, 400)
        self.assertIn("not found", body["error"])

    def test_attribution_cannot_be_bypassed_while_a_duplicate_review_is_open(self):
        status, body = self._post("/api/partners", {"full_name": "Jane", "email": "jane@partnerco.com", "agreement_accepted": True})
        partner_id = body["partner_id"]
        self._post(f"/api/partners/{partner_id}/approve", {"actor": "Aryan"})
        status, body = self._post("/api/referrals", {"partner_id": partner_id, "prospect_name": "Acme", "contact_email": "a@acme.com", "requested_service": "web"})
        r1 = body["referral_id"]
        status, body = self._post("/api/partners", {"full_name": "Bob", "email": "bob@partnerco.com", "agreement_accepted": True})
        partner_id2 = body["partner_id"]
        self._post(f"/api/partners/{partner_id2}/approve", {"actor": "Aryan"})
        self._post("/api/referrals", {"partner_id": partner_id2, "prospect_name": "Acme", "contact_email": "a@acme.com", "requested_service": "web"})
        status, body = self._post(f"/api/referrals/{r1}/attribute", {"actor": "Aryan"})
        self.assertEqual(status, 400)
        self.assertIn("duplicate review", body["error"])

    def test_full_journey_over_http_ends_with_eligible_commission(self):
        status, body = self._post("/api/partners", {"full_name": "Jane", "email": "jane@partnerco.com", "agreement_accepted": True})
        partner_id = body["partner_id"]
        self._post(f"/api/partners/{partner_id}/approve", {"actor": "Aryan"})
        status, body = self._post("/api/referrals", {
            "partner_id": partner_id, "prospect_name": "Acme Corp", "organization_name": "Acme Corp",
            "requested_service": "Website", "estimated_value": 2400,
        })
        referral_id = body["referral_id"]
        status, body = self._post(f"/api/referrals/{referral_id}/attribute", {"actor": "Aryan"})
        self.assertEqual(status, 200)
        opportunity_id = body["opportunity_id"]

        status, commission = self._get(f"/api/referrals/{referral_id}/commission")
        self.assertEqual(status, 200)
        self.assertEqual(commission["status"], "NOT_ELIGIBLE")

        status, board = self._get("/api/rh/opportunities")
        self.assertEqual(status, 200)
        self.assertTrue(any(o["id"] == opportunity_id and o["source"] == "partner_referral" for o in board["items"]))


class PolicyAcknowledgementTests(PartnerManagementTestCase):
    """Phase 7, Section 8: a partner's own, dated acknowledgement of the
    anti-diversion/no-money-collection policy -- distinct from
    phase6_partner.PartnerNetworkService.configure_partner()'s staff-only
    hardcoded flags."""

    def test_approved_partner_can_acknowledge_policy(self):
        pid = self._approved_partner()
        updated = self.partners.acknowledge_policy(pid, actor="jane@partnerco.com")
        self.assertIsNotNone(updated["policy_acknowledged_at"])
        self.assertEqual(updated["policy_acknowledged_by"], "jane@partnerco.com")
        self.assertEqual(updated["no_side_deal_accepted"], 1)
        self.assertEqual(updated["no_unauthorized_subcontracting_accepted"], 1)

    def test_pending_partner_cannot_acknowledge_policy(self):
        pid = self.partners.register({"full_name": "Pending Partner", "email": "pending@partnerco.com", "agreement_accepted": True}, actor="Aryan")
        with self.assertRaises(PartnerError):
            self.partners.acknowledge_policy(pid, actor="pending@partnerco.com")

    def test_suspended_partner_cannot_acknowledge_policy(self):
        pid = self._approved_partner()
        self.partners.suspend(pid, actor="Aryan", reason="synthetic test suspension")
        with self.assertRaises(PartnerError):
            self.partners.acknowledge_policy(pid, actor="jane@partnerco.com")

    def test_unknown_partner_cannot_acknowledge_policy(self):
        with self.assertRaises(PartnerError):
            self.partners.acknowledge_policy("does-not-exist", actor="nobody")


class ReferralIntakeFieldsTests(PartnerManagementTestCase):
    """Phase 7, Section 5: the two new self-reported intake fields
    (industry, relationship_disclosure) persist on the referral exactly as
    submitted, without disturbing the already-proven duplicate detection
    and attribution lifecycle."""

    def test_industry_and_relationship_disclosure_are_stored_verbatim(self):
        pid = self._approved_partner()
        referral_id = self.referrals.register(pid, {
            "prospect_name": "New Co", "requested_service": "Website rebuild",
            "industry": "Hospitality", "relationship_disclosure": "I am a part-time consultant for this company",
        }, actor="jane@partnerco.com")
        row = self.store.get("pm_referrals", referral_id)
        self.assertEqual(row["industry"], "Hospitality")
        self.assertEqual(row["relationship_disclosure"], "I am a part-time consultant for this company")

    def test_industry_and_relationship_disclosure_are_optional(self):
        pid = self._approved_partner()
        referral_id = self.referrals.register(pid, {
            "prospect_name": "New Co 2", "requested_service": "Website rebuild",
        }, actor="jane@partnerco.com")
        row = self.store.get("pm_referrals", referral_id)
        self.assertIsNone(row["industry"])
        self.assertIsNone(row["relationship_disclosure"])
