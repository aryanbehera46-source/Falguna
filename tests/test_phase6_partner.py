import tempfile
import unittest
from pathlib import Path

from falguna.audit import AuditLog
from falguna.partner_management import PartnerStore, ReferralStore
from falguna.phase6_commercial import AccessContext, CommercialIdentityStore, CommercialSecurityError, PaymentOrchestrator
from falguna.phase6_partner import (
    CommercialRiskService, CustomerVerificationService, PartnerNetworkService, PUBLIC_PAYMENT_WARNING,
)
from falguna.store import StateStore


class Phase6PartnerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.tmp.name) / "partner.db"); self.store.migrate()
        self.audit = AuditLog(Path(self.tmp.name) / "audit.jsonl")
        self.identities = CommercialIdentityStore(self.store, self.audit)
        self.admin = self._identity("admin", "Admin", "ADMIN")
        self.finance = self._identity("finance", "Finance", "FINANCE_OPERATOR")
        self.sales = self._identity("sales", "Sales", "SALES_OPERATOR")
        self.other = self._identity("other", "Other", "OWNER", organization="other")
        self.network = PartnerNetworkService(self.store, self.audit, self.identities)
        self.risks = CommercialRiskService(self.store, self.audit, self.identities)
        self.verify = CustomerVerificationService(self.store)
        partners = PartnerStore(self.store, self.audit)
        self.partner_id = partners.register({"full_name": "Synthetic Partner", "email": "partner@invalid.test",
            "agreement_accepted": True}, "test")
        partners.approve(self.partner_id, "test")

    def tearDown(self):
        self.store.close(); self.tmp.cleanup()

    def _identity(self, ref, name, role, organization="ttt"):
        identity_id = self.identities.create(organization, "STAFF", ref, name, role, "test")
        return AccessContext(identity_id, organization)

    def _configure_public(self):
        return self.network.configure_partner(self.admin, self.partner_id, "REFERRAL_PARTNER", "VERIFIED", "VERIFIED", True)


class PartnerProfileAndCommissionPlanTests(Phase6PartnerCase):
    def test_partner_profile_supports_role_tier_and_kyc_seam(self):
        partner = self._configure_public()
        self.assertEqual(partner["role_type"], "REFERRAL_PARTNER")
        self.assertEqual(partner["maturity_tier"], "VERIFIED")
        self.assertEqual(partner["kyc_status"], "VERIFIED")
        self.assertEqual(partner["no_side_deal_accepted"], 1)

    def test_invalid_partner_role_fails(self):
        with self.assertRaises(CommercialSecurityError):
            self.network.configure_partner(self.admin, self.partner_id, "MONEY_COLLECTOR")

    def test_commission_plan_is_configurable_not_global(self):
        plan = self.network.create_commission_plan(self.finance, "Referral 8", 0.08, True, False, ["tax", "hardware"])
        self.assertEqual(plan["rate"], 0.08)
        self.assertEqual(plan["recurring_enabled"], 1)
        self.assertIn("hardware", plan["excluded_pass_through_types_json"])

    def test_plan_assignment_uses_real_referral_commission_record(self):
        self._configure_public()
        referral_id = ReferralStore(self.store, self.audit).register(self.partner_id, {
            "prospect_name": "Synthetic Lead", "requested_service": "Automation"}, "test")
        plan = self.network.create_commission_plan(self.finance, "Specialist 12", 0.12)
        commission = self.network.assign_plan(self.finance, referral_id, plan["id"])
        self.assertEqual(commission["commission_plan_id"], plan["id"])
        self.assertEqual(commission["rate"], 0.12)

    def test_cross_org_plan_access_fails(self):
        plan = self.network.create_commission_plan(self.finance, "Referral 8", 0.08)
        with self.assertRaises(CommercialSecurityError):
            self.network.assign_plan(self.other, "missing", plan["id"])


class AntiDiversionRiskTests(Phase6PartnerCase):
    def test_falguna_can_flag_evidence_but_cannot_take_human_action(self):
        event = self.risks.flag("ttt", "OFF_PLATFORM_PAYMENT_REQUEST", "HIGH",
                                {"message_id": "synthetic"}, "FALGUNA", partner_id=self.partner_id)
        self.assertEqual(event["status"], "OPEN")
        with self.assertRaises(CommercialSecurityError):
            self.risks.act(self.other, event["id"], "HOLD", "review")

    def test_human_hold_is_append_only_and_moves_no_money(self):
        event = self.risks.flag("ttt", "CUSTOMER_PAID_REPRESENTATIVE", "CRITICAL",
                                {"customer_report": "synthetic"}, "CUSTOMER", partner_id=self.partner_id)
        held = self.risks.act(self.finance, event["id"], "HOLD", "verify payment claim")
        self.assertEqual(held["status"], "HELD")
        actions = self.store.list("p6_risk_event_actions", "risk_event_id=?", (event["id"],))
        self.assertEqual(actions[0]["action"], "HOLD")
        self.assertEqual(self.store.list("p6_financial_events"), [])

    def test_suspend_access_requires_human_and_real_partner_link(self):
        event = self.risks.flag("ttt", "BRAND_MISUSE", "HIGH", {"artifact": "synthetic"},
                                "STAFF", partner_id=self.partner_id)
        result = self.risks.act(self.finance, event["id"], "SUSPEND_ACCESS", "pending evidence review")
        self.assertEqual(result["status"], "ESCALATED")
        self.assertEqual(self.store.get("pm_partners", self.partner_id)["status"], "SUSPENDED")

    def test_clear_requires_reason_and_closes_further_actions(self):
        event = self.risks.flag("ttt", "UNUSUAL_COMMISSION_SPIKE", "MEDIUM", {"delta": 3}, "SYSTEM")
        with self.assertRaisesRegex(CommercialSecurityError, "reason"):
            self.risks.act(self.finance, event["id"], "CLEAR", "")
        cleared = self.risks.act(self.finance, event["id"], "CLEAR", "explained by three settled invoices")
        self.assertEqual(cleared["status"], "CLEARED")
        with self.assertRaisesRegex(CommercialSecurityError, "terminal"):
            self.risks.act(self.finance, event["id"], "HOLD", "late")


class CustomerVerificationTests(Phase6PartnerCase):
    def test_partner_verification_is_minimal_and_warns_against_collection(self):
        self._configure_public()
        result = self.verify.verify_partner(self.partner_id)
        self.assertTrue(result["valid"])
        self.assertFalse(result["may_collect_customer_money"])
        self.assertEqual(result["warning"], PUBLIC_PAYMENT_WARNING)
        self.assertNotIn("email", result)

    def test_nonpublic_partner_cannot_be_verified(self):
        self.assertFalse(self.verify.verify_partner(self.partner_id)["valid"])

    def test_payment_instruction_requires_exact_customer_and_beneficiary(self):
        payment = PaymentOrchestrator(self.store, self.audit, self.identities).create_intent(
            self.finance, "customer-secret-ref", 500, "INR", "ONE_TIME", "verify-payment", "TTT",
            "beneficiary:official", ["UPI"])
        valid = self.verify.verify_payment_instruction(payment["id"], "customer-secret-ref", "beneficiary:official")
        self.assertTrue(valid["valid"])
        self.assertEqual(valid["approved_beneficiary_ref"], "beneficiary:official")
        forged = self.verify.verify_payment_instruction(payment["id"], "customer-secret-ref", "beneficiary:attacker")
        self.assertFalse(forged["valid"])
        self.assertIsNone(forged["approved_beneficiary_ref"])

    def test_forged_customer_reference_reveals_no_payment_details(self):
        payment = PaymentOrchestrator(self.store, self.audit, self.identities).create_intent(
            self.finance, "customer-secret-ref", 500, "INR", "ONE_TIME", "verify-payment", "TTT",
            "beneficiary:official", ["UPI"])
        result = self.verify.verify_payment_instruction(payment["id"], "wrong", "beneficiary:official")
        self.assertFalse(result["valid"])
        self.assertNotIn("amount", result)


if __name__ == "__main__":
    unittest.main()
