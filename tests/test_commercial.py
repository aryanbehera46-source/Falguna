"""Phase 5 Sprint 1 -- Commercial Operating Foundation tests.

Synthetic data only. Covers: service catalogue validity, intake
persistence, qualification transitions, human-approval gating,
reusable-foundation routing, refund/dispute lifecycle (state-management
only, never touches rh_invoices.amount_received), commission-impact
integration via the existing idempotent CommissionStore.record_refund,
unit economics (unknown != zero), and a full synthetic Phase 5 journey
(Section 16 acceptance test): service -> intake -> qualification ->
approved scope -> project -> foundation routing -> QA -> handover ->
invoice -> synthetic payment -> project economics -> synthetic dispute
-> refund -> corrected net revenue and commission state.
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

from falguna.billing import BillingStore
from falguna.commercial import (
    CommercialError, CostEntryStore, DisputeStore, FoundationStore,
    IntakeStore, ProjectStore, ServiceCatalogStore, economics_for_project,
    recommend_route,
)
from falguna.partner_management import CommissionStore, PartnerStore, ReferralStore
from falguna.runtime import open_control_plane


class CommercialTestCase(unittest.TestCase):
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
        self.services = ServiceCatalogStore(self.store, self.audit)
        self.foundations = FoundationStore(self.store, self.audit)
        self.intakes = IntakeStore(self.store, self.audit)
        self.projects = ProjectStore(self.store, self.audit)
        self.disputes = DisputeStore(self.store, self.audit)
        self.costs = CostEntryStore(self.store, self.audit)
        self.billing = BillingStore(self.store, self.audit)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def _approved_service(self, key="website-v1", category="web_dev", automation_eligible=False):
        sid = self.services.create(
            key, category, "Business Website V1", "A synthetic test service: a standard business website.",
            pricing_model="fixed", delivery_mode="remote", automation_eligible=automation_eligible,
        )
        self.services.set_approval(sid, "Aryan", "APPROVED")
        return sid

    # -- 1. Sellable Service Catalogue ------------------------------------

    def test_service_requires_customer_description(self):
        with self.assertRaises(CommercialError):
            self.services.create(
                "x", "web_dev", "Title", "", pricing_model="fixed", delivery_mode="remote",
            )

    def test_service_key_unique(self):
        self._approved_service(key="dup-key")
        with self.assertRaises(CommercialError):
            self.services.create(
                "dup-key", "web_dev", "Title 2", "desc", pricing_model="fixed", delivery_mode="remote",
            )

    def test_service_draft_not_publicly_active_until_approved(self):
        sid = self.services.create(
            "draft-svc", "web_dev", "Draft Service", "A service not yet approved.",
            pricing_model="fixed", delivery_mode="remote",
        )
        service = self.services.get(sid)
        self.assertEqual(service["approval_status"], "DRAFT")
        self.assertEqual(service["active"], 0)
        self.assertEqual(self.services.list(active_only=True), [])
        self.services.set_approval(sid, "Aryan", "APPROVED")
        active = self.services.list(active_only=True)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["id"], sid)
        # Retiring must remove it from the active catalogue again.
        self.services.set_approval(sid, "Aryan", "RETIRED")
        self.assertEqual(self.services.list(active_only=True), [])

    # -- 1b. International Services V1 (Phase 5 Continuation, Section 4) --

    def test_international_profile_unset_fields_stay_unknown(self):
        sid = self._approved_service()
        service = self.services.get(sid)
        self.assertIsNone(service["risk_level"])
        self.assertIsNone(service["baseline_complexity"])
        self.assertIsNone(service["standard_delivery_days"])
        self.assertIsNone(service["regulated"])

    def test_international_profile_set_and_persist(self):
        sid = self._approved_service()
        updated = self.services.set_international_profile(
            sid, "Aryan", supported_languages=["en", "hi"], risk_level="MEDIUM",
            baseline_complexity="MODERATE", standard_delivery_days=14,
            standard_assumptions="Client supplies content and brand assets.",
            qa_requirements="Cross-browser check + mobile responsiveness pass.",
        )
        self.assertEqual(updated["risk_level"], "MEDIUM")
        self.assertEqual(updated["baseline_complexity"], "MODERATE")
        self.assertEqual(updated["standard_delivery_days"], 14)
        import json as _json
        self.assertEqual(_json.loads(updated["supported_languages_json"]), ["en", "hi"])

    def test_international_profile_rejects_unknown_risk_level(self):
        sid = self._approved_service()
        with self.assertRaises(CommercialError):
            self.services.set_international_profile(sid, "Aryan", risk_level="EXTREME")

    def test_international_profile_rejects_unknown_complexity(self):
        sid = self._approved_service()
        with self.assertRaises(CommercialError):
            self.services.set_international_profile(sid, "Aryan", baseline_complexity="TRIVIAL")

    def test_international_profile_rejects_non_positive_delivery_days(self):
        sid = self._approved_service()
        with self.assertRaises(CommercialError):
            self.services.set_international_profile(sid, "Aryan", standard_delivery_days=0)

    def test_international_profile_regulated_notes_require_regulated_flag(self):
        sid = self._approved_service()
        with self.assertRaises(CommercialError):
            self.services.set_international_profile(sid, "Aryan", regulated_notes="Needs GDPR review.")
        # Setting regulated=True first, then notes, is fine.
        self.services.set_international_profile(sid, "Aryan", regulated=True)
        updated = self.services.set_international_profile(sid, "Aryan", regulated_notes="Needs GDPR review.")
        self.assertEqual(updated["regulated_notes"], "Needs GDPR review.")

    def test_international_profile_unknown_service_rejected(self):
        with self.assertRaises(CommercialError):
            self.services.set_international_profile("does-not-exist", "Aryan", risk_level="LOW")

    def test_regional_pricing_proposed_is_not_authoritative_until_approved(self):
        sid = self._approved_service()
        self.services.propose_regional_pricing(
            sid, "Falguna", region="EU", currency="EUR", price_min=800, price_max=1200,
            rationale="Indicative band based on comparable EU engagements.",
        )
        # A PROPOSED band must never be returned as authoritative.
        self.assertIsNone(self.services.regional_pricing_for(sid, "EU"))
        service = self.services.get(sid)
        import json as _json
        entries = _json.loads(service["regional_pricing_json"])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["status"], "PROPOSED")
        self.assertIsNone(entries[0]["approved_by"])

    def test_regional_pricing_approve_makes_it_authoritative(self):
        sid = self._approved_service()
        self.services.propose_regional_pricing(sid, "Falguna", region="EU", currency="EUR", price_min=800, price_max=1200)
        self.services.approve_regional_pricing(sid, "Aryan", "EU")
        band = self.services.regional_pricing_for(sid, "EU")
        self.assertIsNotNone(band)
        self.assertEqual(band["status"], "APPROVED")
        self.assertEqual(band["approved_by"], "Aryan")

    def test_regional_pricing_approve_without_proposal_rejected(self):
        sid = self._approved_service()
        with self.assertRaises(CommercialError):
            self.services.approve_regional_pricing(sid, "Aryan", "EU")

    def test_regional_pricing_new_proposal_supersedes_old_proposal_not_approved(self):
        sid = self._approved_service()
        self.services.propose_regional_pricing(sid, "Falguna", region="EU", currency="EUR", price_min=800, price_max=1200)
        self.services.approve_regional_pricing(sid, "Aryan", "EU")
        # A fresh proposal for the same region must not disturb the
        # existing approved band until it is itself approved.
        self.services.propose_regional_pricing(sid, "Falguna", region="EU", currency="EUR", price_min=900, price_max=1300)
        band = self.services.regional_pricing_for(sid, "EU")
        self.assertEqual(band["price_min"], 800)

    def test_regional_pricing_approving_new_proposal_retires_old_approved_band(self):
        sid = self._approved_service()
        self.services.propose_regional_pricing(sid, "Falguna", region="EU", currency="EUR", price_min=800, price_max=1200)
        self.services.approve_regional_pricing(sid, "Aryan", "EU")
        self.services.propose_regional_pricing(sid, "Falguna", region="EU", currency="EUR", price_min=900, price_max=1300)
        self.services.approve_regional_pricing(sid, "Aryan", "EU")
        service = self.services.get(sid)
        import json as _json
        entries = _json.loads(service["regional_pricing_json"])
        approved = [e for e in entries if e["status"] == "APPROVED"]
        self.assertEqual(len(approved), 1)
        self.assertEqual(approved[0]["price_min"], 900)
        retired = [e for e in entries if e["status"] == "RETIRED"]
        self.assertEqual(len(retired), 1)
        self.assertEqual(retired[0]["price_min"], 800)

    def test_regional_pricing_independent_regions_do_not_interfere(self):
        sid = self._approved_service()
        self.services.propose_regional_pricing(sid, "Falguna", region="EU", currency="EUR", price_min=800, price_max=1200)
        self.services.propose_regional_pricing(sid, "Falguna", region="APAC", currency="USD", price_min=600, price_max=900)
        self.services.approve_regional_pricing(sid, "Aryan", "EU")
        self.assertIsNotNone(self.services.regional_pricing_for(sid, "EU"))
        self.assertIsNone(self.services.regional_pricing_for(sid, "APAC"))

    def test_regional_pricing_requires_region_and_currency(self):
        sid = self._approved_service()
        with self.assertRaises(CommercialError):
            self.services.propose_regional_pricing(sid, "Falguna", region="", currency="EUR")
        with self.assertRaises(CommercialError):
            self.services.propose_regional_pricing(sid, "Falguna", region="EU", currency="")

    def test_regional_pricing_rejects_min_above_max(self):
        sid = self._approved_service()
        with self.assertRaises(CommercialError):
            self.services.propose_regional_pricing(sid, "Falguna", region="EU", currency="EUR", price_min=1500, price_max=1000)

    # -- 2. Customer Intake & Qualification -------------------------------

    def test_intake_requires_customer_name_and_outcome(self):
        with self.assertRaises(CommercialError):
            self.intakes.create("", "some outcome")
        with self.assertRaises(CommercialError):
            self.intakes.create("Acme Corp", "")

    def test_intake_persistence_roundtrip(self):
        sid = self._approved_service()
        iid = self.intakes.create(
            "Acme Corp", "A new customer-facing website", customer_contact="ops@acme.test",
            business_name="Acme Corp Pty", industry="retail", region="APAC", country="Singapore",
            service_id=sid, requirements="Needs a catalogue and contact form.", timeline="4 weeks",
            budget_amount=2500.0, budget_currency="USD", complexity="standard",
        )
        stored = self.intakes.get(iid)
        self.assertEqual(stored["customer_name"], "Acme Corp")
        self.assertEqual(stored["qualification_status"], "NEW")
        self.assertEqual(stored["service_id"], sid)
        self.assertEqual(stored["human_approved"], 0)
        history = self.intakes.history(iid)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["to_status"], "NEW")

    def test_intake_qualification_transitions_and_terminal_guard(self):
        iid = self.intakes.create("Beta LLC", "Needs a booking system")
        self.intakes.qualify(iid, "Aryan", "QUALIFYING", potential_value=1800.0)
        self.intakes.qualify(iid, "Aryan", "QUALIFIED", risks="none flagged", recommended_route="USE_FOUNDATION")
        self.intakes.approve(iid, "Aryan")
        stored = self.intakes.get(iid)
        self.assertEqual(stored["human_approved"], 1)
        self.assertEqual(stored["approved_by"], "Aryan")
        # An intake cannot be re-qualified after conversion.
        self.intakes.convert_to_project(iid, "Aryan", "USE_FOUNDATION")
        with self.assertRaises(CommercialError):
            self.intakes.qualify(iid, "Aryan", "QUALIFYING")

    def test_convert_to_project_requires_human_approval(self):
        iid = self.intakes.create("Gamma Inc", "Needs a CRM dashboard")
        self.intakes.qualify(iid, "Aryan", "QUALIFIED")
        with self.assertRaises(CommercialError):
            self.intakes.convert_to_project(iid, "Aryan", "CUSTOM_BUILD")

    def test_convert_to_project_rejects_decline_route(self):
        iid = self.intakes.create("Delta Co", "Needs something we cannot responsibly build")
        self.intakes.qualify(iid, "Aryan", "QUALIFIED")
        self.intakes.approve(iid, "Aryan")
        with self.assertRaises(CommercialError):
            self.intakes.convert_to_project(iid, "Aryan", "DECLINE")

    def test_convert_to_project_creates_real_opportunity_and_client(self):
        iid = self.intakes.create("Epsilon Ltd", "Needs an online store", budget_amount=5000.0, budget_currency="USD")
        self.intakes.qualify(iid, "Aryan", "QUALIFIED")
        self.intakes.approve(iid, "Aryan")
        project_id = self.intakes.convert_to_project(iid, "Aryan", "CUSTOM_BUILD")
        project = self.projects.get(project_id)
        self.assertEqual(project["status"], "SCOPED")
        opportunity = self.store.get("rh_opportunities", project["opportunity_id"])
        self.assertIsNotNone(opportunity)
        self.assertEqual(opportunity["client_name"], "Epsilon Ltd")
        client = self.store.get("clients", project["client_id"])
        self.assertIsNotNone(client)
        self.assertEqual(client["name"], "Epsilon Ltd")
        # The intake itself must now show CONVERTED with the opportunity linked.
        intake = self.intakes.get(iid)
        self.assertEqual(intake["qualification_status"], "CONVERTED")
        self.assertEqual(intake["opportunity_id"], project["opportunity_id"])

    # -- 3. Product Foundation Registry / 14. FALGUNA routing -------------

    def test_foundation_maturity_gates_reuse_candidacy(self):
        proto_id = self.foundations.register("Prototype Booking Kit", "booking", "PROTOTYPE")
        prod_id = self.foundations.register(
            "Royal Table (reference)", "restaurant", "PRODUCTION_READY",
            service_categories=["web_dev", "restaurant"],
        )
        self.assertEqual(self.foundations.candidates_for_category("booking"), [])  # prototype never offered
        candidates = self.foundations.candidates_for_category("web_dev")
        self.assertEqual([c["id"] for c in candidates], [prod_id])
        self.assertNotIn(proto_id, [c["id"] for c in candidates])

    def test_recommend_route_declines_unapproved_service(self):
        sid = self.services.create(
            "unapproved", "web_dev", "Unapproved Service", "desc", pricing_model="fixed", delivery_mode="remote",
        )
        service = self.services.get(sid)
        result = recommend_route(self.store, service, "standard")
        self.assertEqual(result["route"], "DECLINE")

    def test_recommend_route_no_service_escalates(self):
        result = recommend_route(self.store, None, "standard")
        self.assertEqual(result["route"], "ESCALATE")

    def test_recommend_route_uses_foundation_when_standard_complexity(self):
        sid = self._approved_service(category="web_dev")
        self.foundations.register("Reusable Site Kit", "web_dev", "PRODUCTION_READY", service_categories=["web_dev"])
        service = self.services.get(sid)
        result = recommend_route(self.store, service, "standard")
        self.assertEqual(result["route"], "USE_FOUNDATION")
        self.assertTrue(result["foundation_candidates"])

    def test_recommend_route_customizes_when_complexity_high_with_foundation(self):
        sid = self._approved_service(category="web_dev")
        self.foundations.register("Reusable Site Kit", "web_dev", "PRODUCTION_READY", service_categories=["web_dev"])
        service = self.services.get(sid)
        result = recommend_route(self.store, service, "high")
        self.assertEqual(result["route"], "CUSTOMIZE")

    def test_recommend_route_custom_build_when_automation_eligible_no_foundation(self):
        sid = self._approved_service(category="never_matched_category", automation_eligible=True)
        service = self.services.get(sid)
        result = recommend_route(self.store, service, "standard")
        self.assertEqual(result["route"], "CUSTOM_BUILD")

    def test_recommend_route_escalates_when_no_foundation_not_automation_eligible(self):
        sid = self._approved_service(category="never_matched_category", automation_eligible=False)
        service = self.services.get(sid)
        result = recommend_route(self.store, service, "standard")
        self.assertEqual(result["route"], "ESCALATE")

    # -- 10. Delivery / Product Factory backbone --------------------------

    def _scoped_project(self):
        iid = self.intakes.create("Zeta Inc", "Needs an appointment booking system")
        self.intakes.qualify(iid, "Aryan", "QUALIFIED")
        self.intakes.approve(iid, "Aryan")
        return self.intakes.convert_to_project(iid, "Aryan", "CUSTOM_BUILD")

    def test_project_transitions_follow_allowed_lifecycle(self):
        pid = self._scoped_project()
        self.projects.transition(pid, "Aryan", "IN_DELIVERY")
        self.projects.transition(pid, "Aryan", "QA")
        # QA can bounce back to IN_DELIVERY.
        self.projects.transition(pid, "Aryan", "IN_DELIVERY")
        self.projects.transition(pid, "Aryan", "QA")
        self.projects.transition(pid, "Aryan", "HANDED_OVER")
        self.projects.transition(pid, "Aryan", "INVOICED")
        self.projects.transition(pid, "Aryan", "CLOSED")
        history = self.projects.history(pid)
        self.assertGreaterEqual(len(history), 6)

    def test_project_transition_rejects_illegal_jump(self):
        pid = self._scoped_project()
        with self.assertRaises(CommercialError):
            self.projects.transition(pid, "Aryan", "CLOSED")  # cannot skip straight to CLOSED

    def test_project_transition_same_status_is_idempotent_noop(self):
        pid = self._scoped_project()
        before = self.projects.get(pid)
        after = self.projects.transition(pid, "Aryan", "SCOPED")
        self.assertEqual(before["status"], after["status"])

    # -- 11. Refund & Dispute Handling -------------------------------------

    def _invoice_with_payment(self, pid, amount=1000.0, paid=1000.0):
        project = self.projects.get(pid)
        invoice_id = self.billing.create_invoice(project["client_id"], "Aryan", amount, opportunity_id=project["opportunity_id"])
        self.billing.mark_ready(invoice_id, "Aryan")
        self.billing.mark_sent(invoice_id, "Aryan")
        if paid:
            self.billing.record_payment(invoice_id, paid, "Aryan", {"ref": "synthetic-bank-txn-001"})
        return invoice_id

    def test_dispute_requires_evidence_and_positive_amount(self):
        pid = self._scoped_project()
        invoice_id = self._invoice_with_payment(pid)
        with self.assertRaises(CommercialError):
            self.disputes.open(invoice_id, "Aryan", "quality issue", 100.0, evidence=None)
        with self.assertRaises(CommercialError):
            self.disputes.open(invoice_id, "Aryan", "quality issue", 0, evidence={"note": "x"})

    def test_dispute_full_lifecycle_no_refund(self):
        pid = self._scoped_project()
        invoice_id = self._invoice_with_payment(pid)
        did = self.disputes.open(invoice_id, "Aryan", "customer unhappy with color scheme", 200.0, evidence={"ticket": "T-1"})
        self.disputes.start_review(did, "Aryan", reviewer="Aryan")
        resolved = self.disputes.resolve(did, "Aryan", "Scope was delivered as agreed; no refund warranted.")
        self.assertEqual(resolved["status"], "RESOLVED_NO_REFUND")
        self.assertEqual(resolved["refund_amount"], 0.0)
        closed = self.disputes.close(did, "Aryan")
        self.assertEqual(closed["status"], "CLOSED")
        # rh_invoices.amount_received must be completely untouched by a no-refund resolution.
        invoice = self.billing.get(invoice_id)
        self.assertEqual(invoice["amount_received"], 1000.0)

    def test_dispute_cannot_resolve_twice_or_close_before_resolution(self):
        pid = self._scoped_project()
        invoice_id = self._invoice_with_payment(pid)
        did = self.disputes.open(invoice_id, "Aryan", "late delivery", 150.0, evidence={"ticket": "T-2"})
        with self.assertRaises(CommercialError):
            self.disputes.close(did, "Aryan")  # cannot close an OPEN dispute
        self.disputes.resolve(did, "Aryan", "no refund")
        with self.assertRaises(CommercialError):
            self.disputes.resolve(did, "Aryan", "trying again")  # already resolved

    def test_partial_refund_never_touches_invoice_amount_received(self):
        pid = self._scoped_project()
        invoice_id = self._invoice_with_payment(pid, amount=1000.0, paid=1000.0)
        did = self.disputes.open(invoice_id, "Aryan", "one deliverable missing", 300.0, evidence={"ticket": "T-3"})
        resolved = self.disputes.resolve(did, "Aryan", "Approved a partial refund for the missing deliverable.", refund_amount=300.0, evidence={"approved_by": "Aryan"})
        self.assertEqual(resolved["status"], "PARTIAL_REFUND_APPROVED")
        self.assertEqual(resolved["refund_amount"], 300.0)
        # billing.py's own ledger of cash actually received is untouched --
        # net_collected (computed below) is what reflects the refund, not amount_received.
        invoice = self.billing.get(invoice_id)
        self.assertEqual(invoice["amount_received"], 1000.0)

    def test_full_refund_detected_when_refund_equals_available(self):
        pid = self._scoped_project()
        invoice_id = self._invoice_with_payment(pid, amount=500.0, paid=500.0)
        did = self.disputes.open(invoice_id, "Aryan", "service not delivered", 500.0, evidence={"ticket": "T-4"})
        resolved = self.disputes.resolve(did, "Aryan", "Full refund approved -- service was never delivered.", refund_amount=500.0, evidence={"approved_by": "Aryan"})
        self.assertEqual(resolved["status"], "FULL_REFUND_APPROVED")

    def test_refund_cannot_exceed_amount_actually_received(self):
        pid = self._scoped_project()
        invoice_id = self._invoice_with_payment(pid, amount=1000.0, paid=400.0)  # only partially paid
        did = self.disputes.open(invoice_id, "Aryan", "overcharge dispute", 900.0, evidence={"ticket": "T-5"})
        with self.assertRaises(CommercialError):
            self.disputes.resolve(did, "Aryan", "approving more than was ever collected", refund_amount=900.0, evidence={"x": 1})

    def test_second_dispute_on_same_invoice_respects_already_refunded_amount(self):
        pid = self._scoped_project()
        invoice_id = self._invoice_with_payment(pid, amount=1000.0, paid=1000.0)
        d1 = self.disputes.open(invoice_id, "Aryan", "first issue", 600.0, evidence={"ticket": "T-6a"})
        self.disputes.resolve(d1, "Aryan", "partial refund for first issue", refund_amount=600.0, evidence={"x": 1})
        d2 = self.disputes.open(invoice_id, "Aryan", "second issue", 500.0, evidence={"ticket": "T-6b"})
        with self.assertRaises(CommercialError):
            # Only 400 remains available (1000 received - 600 already refunded).
            self.disputes.resolve(d2, "Aryan", "trying to over-refund", refund_amount=500.0, evidence={"x": 1})
        resolved = self.disputes.resolve(d2, "Aryan", "partial refund for second issue", refund_amount=400.0, evidence={"x": 1})
        self.assertEqual(resolved["status"], "FULL_REFUND_APPROVED")  # 400 == everything still available

    # -- Commission impact (reuses the existing idempotent CommissionStore) -

    def _project_with_partner_commission(self, invoice_amount=1000.0, paid_amount=1000.0):
        pid = self._scoped_project()
        project = self.projects.get(pid)
        partners = PartnerStore(self.store, self.audit)
        referrals = ReferralStore(self.store, self.audit, partners=partners)
        commissions = CommissionStore(self.store, self.audit)
        partner_id = partners.register({"full_name": "Jane Partner", "email": "jane@partnerco.test", "agreement_accepted": True}, actor="Aryan")
        partners.approve(partner_id, "Aryan")
        referral_id = referrals.register(partner_id, {"prospect_name": "Omega Referral Co", "organization_name": "Omega Referral Co", "requested_service": "custom build"}, actor="Aryan")
        referrals.attribute(referral_id, "Aryan")
        # Point this referral at the SAME opportunity the intake conversion
        # created -- simulating that this particular customer came in
        # through this partner, rather than building a second opportunity.
        self.store.update("pm_referrals", referral_id, opportunity_id=project["opportunity_id"])
        commissions.ensure_for_referral(referral_id, "Aryan")
        invoice_id = self._invoice_with_payment(pid, amount=invoice_amount, paid=paid_amount)
        commission = commissions.sync_from_invoice(referral_id, "Aryan")
        return pid, invoice_id, referral_id, commission

    def test_dispute_refund_reverses_proportional_commission(self):
        pid, invoice_id, referral_id, commission = self._project_with_partner_commission(invoice_amount=1000.0, paid_amount=1000.0)
        self.assertEqual(commission["status"], "ELIGIBLE")
        self.assertEqual(commission["eligible_amount"], 100.0)  # 10% of 1000
        did = self.disputes.open(invoice_id, "Aryan", "deliverable incomplete", 400.0, evidence={"ticket": "T-7"})
        resolved = self.disputes.resolve(did, "Aryan", "partial refund approved", refund_amount=400.0, evidence={"approved_by": "Aryan"})
        self.assertEqual(resolved["status"], "PARTIAL_REFUND_APPROVED")
        commission_store = CommissionStore(self.store, self.audit)
        updated_commission = commission_store.get_for_referral(referral_id)
        # record_refund() subtracts the refund amount from eligible_amount
        # (HELD if something remains, REVERSED if it does not) -- never
        # touches rh_invoices.amount_received, and never silently drops
        # the event: refunded_amount accumulates on the commission row.
        self.assertIn(updated_commission["status"], ("HELD", "REVERSED"))
        self.assertEqual(updated_commission["refunded_amount"], 400.0)
        self.assertLess(updated_commission["eligible_amount"], commission["eligible_amount"])
        import json as _json
        impact = _json.loads(resolved["commission_impact_json"])
        self.assertEqual(impact["partner_referral"], referral_id)
        self.assertEqual(impact["commission_status_after"], updated_commission["status"])

    def test_dispute_with_no_referral_records_explicit_absence_not_silent_skip(self):
        pid = self._scoped_project()
        invoice_id = self._invoice_with_payment(pid, amount=500.0, paid=500.0)
        did = self.disputes.open(invoice_id, "Aryan", "no partner involved", 200.0, evidence={"ticket": "T-8"})
        resolved = self.disputes.resolve(did, "Aryan", "partial refund, no partner on this deal", refund_amount=200.0, evidence={"x": 1})
        import json as _json
        impact = _json.loads(resolved["commission_impact_json"])
        self.assertIsNone(impact["partner_referral"])
        self.assertIn("note", impact)

    def test_commission_refund_replay_via_same_dispute_is_idempotent(self):
        """Directly exercises the existing CommissionStore.record_refund
        idempotency (event_ref replay) that DisputeStore._apply_commission_impact
        relies on -- confirms replaying the exact same event_ref never
        double-reverses commission, which is what makes it safe for this
        new Phase 5 integration point to call into."""
        pid, invoice_id, referral_id, commission = self._project_with_partner_commission()
        commission_store = CommissionStore(self.store, self.audit)
        first = commission_store.record_refund(referral_id, "Aryan", 50.0, {"dispute_id": "synthetic-dispute-1"}, reason="test", event_ref="synthetic-dispute-1")
        second = commission_store.record_refund(referral_id, "Aryan", 50.0, {"dispute_id": "synthetic-dispute-1"}, reason="test", event_ref="synthetic-dispute-1")
        self.assertEqual(first["refunded_amount"], second["refunded_amount"])
        self.assertEqual(first["eligible_amount"], second["eligible_amount"])

    # -- 12. Unit Economics: unknown must stay unknown, never default to 0 -

    def test_costs_unknown_when_never_recorded(self):
        pid = self._scoped_project()
        econ = economics_for_project(self.store, pid)
        for category in ("MODEL_API", "INFRASTRUCTURE", "HUMAN_EXPERT", "OTHER_DIRECT"):
            self.assertIsNone(econ["costs"][category])
        self.assertIsNone(econ["total_known_direct_cost"])
        self.assertIsNone(econ["gross_contribution"])  # cannot claim a margin when direct cost is entirely unknown
        self.assertIsNone(econ["partner_commission"])  # no referral/commission exists for this project at all
        self.assertIsNotNone(econ["note"])

    def test_partial_costs_recorded_leave_other_categories_none(self):
        pid = self._scoped_project()
        self.costs.record(pid, "Aryan", "MODEL_API", 12.50, note="synthetic LLM usage")
        self.costs.record(pid, "Aryan", "INFRASTRUCTURE", 5.00, note="synthetic hosting")
        econ = economics_for_project(self.store, pid)
        self.assertEqual(econ["costs"]["MODEL_API"], 12.50)
        self.assertEqual(econ["costs"]["INFRASTRUCTURE"], 5.00)
        self.assertIsNone(econ["costs"]["HUMAN_EXPERT"])
        self.assertIsNone(econ["costs"]["OTHER_DIRECT"])
        # total_known_direct_cost sums only what has actually been recorded --
        # it never assumes the unrecorded categories are zero.
        self.assertEqual(econ["total_known_direct_cost"], 17.50)

    def test_cost_entry_rejects_unknown_category_and_negative_amount(self):
        pid = self._scoped_project()
        with self.assertRaises(CommercialError):
            self.costs.record(pid, "Aryan", "NOT_A_REAL_CATEGORY", 10.0)
        with self.assertRaises(CommercialError):
            self.costs.record(pid, "Aryan", "MODEL_API", -5.0)

    # -- 16. Full synthetic Phase 5 journey (acceptance test) -------------

    def test_full_synthetic_phase5_journey_end_to_end(self):
        """Service selected -> customer intake -> qualification -> approved
        scope -> project created -> reusable foundation identified ->
        delivery path selected -> execution -> QA -> handover -> invoice ->
        synthetic payment -> project economics -> synthetic dispute/refund
        -> corrected net revenue and commission state. Synthetic data only."""
        # 1. Service selected (approved, active catalogue entry).
        sid = self._approved_service(key="e2e-website", category="web_dev")
        service = self.services.get(sid)

        # 2. A reusable, production-ready foundation exists for this category.
        foundation_id = self.foundations.register(
            "Royal Table (reference foundation)", "web_dev", "PRODUCTION_READY",
            service_categories=["web_dev"], qa_requirements="Lighthouse >= 90, no console errors",
        )

        # 3. Customer intake.
        iid = self.intakes.create(
            "Synthetic Test Customer Pty", "A new business website with a booking form",
            customer_contact="ops@synthetic-test.invalid", region="APAC", country="Australia",
            service_id=sid, requirements="5 pages, booking form, mobile-responsive.",
            timeline="3 weeks", budget_amount=1200.0, budget_currency="USD", complexity="standard",
        )

        # FALGUNA's advisory drafting step -- never binds anything by itself.
        self.intakes.ai_draft(
            iid, "Falguna", requirement_summary="Standard 5-page business site with a booking form.",
            missing_questions=["Does the customer have existing brand assets?"],
            proposed_scope="Use the Royal Table foundation, customize branding and the booking form.",
        )

        # 4. Qualification, informed by the evidence-based routing recommendation.
        route = recommend_route(self.store, service, "standard")
        self.assertEqual(route["route"], "USE_FOUNDATION")
        self.intakes.qualify(iid, "Aryan", "QUALIFIED", potential_value=1200.0, risks="none", recommended_route=route["route"])

        # 5. Human approval -- the one binding-commitment gate.
        self.intakes.approve(iid, "Aryan")

        # 6. Approved intake -> real project (reusing the existing Revenue & Delivery Engine).
        project_id = self.intakes.convert_to_project(iid, "Aryan", route["route"], foundation_id=foundation_id)
        project = self.projects.get(project_id)
        self.assertEqual(project["status"], "SCOPED")
        self.assertEqual(project["foundation_id"], foundation_id)

        # 7. Delivery: execution -> QA -> handover.
        self.projects.transition(project_id, "Falguna", "IN_DELIVERY")
        self.costs.record(project_id, "Falguna", "MODEL_API", 3.40, note="synthetic drafting/QA token usage")
        self.projects.transition(project_id, "Falguna", "QA")
        self.projects.transition(project_id, "Falguna", "HANDED_OVER")

        # 8. Invoice + synthetic payment.
        self.projects.transition(project_id, "Aryan", "INVOICED")
        invoice_id = self.billing.create_invoice(project["client_id"], "Aryan", 1200.0, opportunity_id=project["opportunity_id"])
        self.billing.mark_ready(invoice_id, "Aryan")
        self.billing.mark_sent(invoice_id, "Aryan")
        self.billing.record_payment(invoice_id, 1200.0, "Aryan", {"ref": "synthetic-e2e-payment-001"})

        # 9. Project economics before any dispute.
        econ_before = economics_for_project(self.store, project_id)
        self.assertEqual(econ_before["collected_total"], 1200.0)
        self.assertEqual(econ_before["net_collected"], 1200.0)
        self.assertEqual(econ_before["refunds_total"], 0.0)
        self.assertEqual(econ_before["costs"]["MODEL_API"], 3.40)
        self.assertIsNone(econ_before["costs"]["HUMAN_EXPERT"])  # never recorded -- stays unknown
        self.assertIsNone(econ_before["costs"]["OTHER_DIRECT"])  # never recorded -- stays unknown
        # gross_contribution is computed from whatever direct costs ARE
        # known (here, just the recorded MODEL_API cost) -- it is never
        # blocked on every category being recorded, but it also never
        # pretends an unrecorded category is zero when summing them.
        self.assertAlmostEqual(econ_before["gross_contribution"], 1200.0 - 3.40, places=2)

        # 10. Synthetic dispute + partial refund.
        dispute_id = self.disputes.open(invoice_id, "Aryan", "minor scope gap on booking form", 150.0, evidence={"ticket": "E2E-1"})
        self.disputes.start_review(dispute_id, "Aryan", reviewer="Aryan")
        resolved = self.disputes.resolve(dispute_id, "Aryan", "Partial refund approved for the booking-form gap.", refund_amount=150.0, evidence={"approved_by": "Aryan"})
        self.assertEqual(resolved["status"], "PARTIAL_REFUND_APPROVED")
        self.disputes.close(dispute_id, "Aryan")

        # 11. Corrected net revenue -- collected_total (the real cash ledger)
        # is untouched; net_collected and refunds_total reflect the refund.
        econ_after = economics_for_project(self.store, project_id)
        self.assertEqual(econ_after["collected_total"], 1200.0)
        self.assertEqual(econ_after["refunds_total"], 150.0)
        self.assertEqual(econ_after["net_collected"], 1050.0)
        invoice_after = self.billing.get(invoice_id)
        self.assertEqual(invoice_after["amount_received"], 1200.0)  # billing.py's real-cash ledger never mutated by a refund

        self.projects.transition(project_id, "Aryan", "CLOSED")
        self.assertEqual(self.projects.get(project_id)["status"], "CLOSED")

        # 12. Financial audit integrity: the append-only hash chain covering
        # every mutation in this journey (service/intake/project/invoice/
        # dispute/refund) must verify cleanly -- nothing silently dropped.
        self.assertTrue(self.audit.verify())


if __name__ == "__main__":
    unittest.main()
