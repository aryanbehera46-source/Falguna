import tempfile
import unittest
from pathlib import Path

from falguna.audit import AuditLog
from falguna.billing import BillingStore
from falguna.commercial import ProjectStore
from falguna.comms import CommsStore
from falguna.partner_management import PartnerStore, ReferralError, ReferralStore
from falguna.phase6_commercial import CommercialIdentityStore
from falguna.phase7_portals import ExternalPortalAuth, ExternalPortalService
from falguna.revenue_hunter import OpportunityStore
from falguna.sales_ops import ClientStore
from falguna.site_auth import AuthError
from falguna.store import StateStore


class Phase7PortalCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = StateStore(root / "phase7.db"); self.store.migrate()
        self.audit = AuditLog(root / "audit.jsonl")
        self.identities = CommercialIdentityStore(self.store, self.audit)
        self.auth = ExternalPortalAuth(self.store)
        self.portals = ExternalPortalService(self.store, self.audit)
        self.comms = CommsStore(self.store, self.audit)
        self.clients = ClientStore(self.store, self.audit)
        self.opps = OpportunityStore(self.store, self.audit)
        self.projects = ProjectStore(self.store, self.audit)
        self.billing = BillingStore(self.store, self.audit)

    def tearDown(self):
        self.store.close(); self.temp.cleanup()

    def customer(self, name, amount):
        org = self.comms.find_or_create_organization(name, domain=f"{name.lower()}.invalid")
        client = self.clients.upsert(name, "test")
        self.store.update("comm_organizations", org, linked_client_id=client)
        contact = self.comms.find_or_create_contact(f"contact@{name.lower()}.invalid", name=name, organization_id=org)
        self.comms.open_conversation("EMAIL", "projects", subject=f"{name} project", contact_id=contact,
                                     organization_id=org, actor="test", linked_client_id=client)
        opp = self.opps.create({"title": f"{name} portal", "description": "Synthetic", "client_name": name}, "test")
        self.projects.create_for_opportunity(opp, client, "test", "CUSTOM_BUILD")
        invoice = self.billing.create_invoice(client, "test", amount)
        identity = self.identities.create("ttt", "CUSTOMER_ORG", org, name, "CUSTOMER", "test")
        return org, client, invoice, identity


class ExternalAuthenticationTests(Phase7PortalCase):
    def test_only_customer_or_partner_identity_can_be_provisioned(self):
        staff = self.identities.create("ttt", "STAFF", "staff-1", "Staff", "ADMIN", "test")
        with self.assertRaises(AuthError):
            self.auth.provision(staff, "staff@example.test", "correct-horse-battery")

    def test_login_resolves_immutable_identity_and_session_revocation(self):
        _, _, _, identity_id = self.customer("Acme", 1200)
        self.auth.provision(identity_id, "customer@example.test", "correct-horse-battery")
        result = self.auth.login("customer@example.test", "correct-horse-battery")
        self.assertEqual(result["identity"]["id"], identity_id)
        self.assertEqual(self.auth.identity(result["session_id"])["role"], "CUSTOMER")
        self.assertTrue(self.auth.check_csrf(result["session_id"], result["csrf_token"]))
        self.auth.logout(result["session_id"])
        self.assertIsNone(self.auth.identity(result["session_id"]))


class CustomerPortalIsolationTests(Phase7PortalCase):
    def test_customer_identity_cannot_select_another_customer(self):
        org_a, _, invoice_a, id_a = self.customer("Acme", 1200)
        _, _, invoice_b, _ = self.customer("Beta", 900)
        identity_a = self.store.get("p6_commercial_identities", id_a)
        bundle = self.portals.customer_bundle(identity_a)
        self.assertEqual(bundle["organization"]["id"], org_a)
        self.assertEqual([i["id"] for i in bundle["invoices"]], [invoice_a])
        self.assertNotIn(invoice_b, [i["id"] for i in bundle["invoices"]])

    def test_customer_sees_only_own_phase6_payment_records(self):
        org_a, _, _, id_a = self.customer("Acme", 1200)
        org_b, _, _, _ = self.customer("Beta", 900)
        now = "2026-10-02T00:00:00+00:00"
        base = {"organization_id": "ttt", "invoice_id": None, "kind": "ONE_TIME", "amount": 10,
                "currency": "INR", "status": "CREATED", "capture_mode": "MANUAL",
                "allowed_methods_json": '["UPI"]', "provider": "SANDBOX_ADAPTER", "provider_session_ref": None,
                "provider_transaction_ref": None, "payment_method_token_ref": None, "mandate_token_ref": None,
                "milestone_ref": None, "legal_owner_name": "TTT", "beneficiary_ref": "official",
                "metadata_json": "{}", "actor": "test", "created_at": now, "updated_at": now}
        own = self.store.create("p6_payment_intents", {**base, "customer_ref": org_a, "idempotency_key": "a"})
        other = self.store.create("p6_payment_intents", {**base, "customer_ref": org_b, "idempotency_key": "b"})
        result = self.portals.customer_payments(self.store.get("p6_commercial_identities", id_a))
        self.assertEqual([p["id"] for p in result["payments"]], [own])
        self.assertNotIn(other, [p["id"] for p in result["payments"]])


class PartnerPortalIsolationTests(Phase7PortalCase):
    def test_partner_bundle_contains_only_linked_partner_records(self):
        partners = PartnerStore(self.store, self.audit)
        p1 = partners.register({"full_name": "Partner One", "email": "one@example.test", "agreement_accepted": True}, "test")
        p2 = partners.register({"full_name": "Partner Two", "email": "two@example.test", "agreement_accepted": True}, "test")
        partners.approve(p1, "test"); partners.approve(p2, "test")
        r1 = ReferralStore(self.store, self.audit).register(p1, {"prospect_name": "Lead One", "requested_service": "Software"}, "test")
        r2 = ReferralStore(self.store, self.audit).register(p2, {"prospect_name": "Lead Two", "requested_service": "AI"}, "test")
        identity_id = self.identities.create("ttt", "PARTNER", p1, "Partner One", "PARTNER", "test")
        bundle = self.portals.partner_bundle(self.store.get("p6_commercial_identities", identity_id))
        self.assertEqual([r["id"] for r in bundle["referrals"]], [r1])
        self.assertNotIn(r2, [r["id"] for r in bundle["referrals"]])



class PartnerLeadRegistrationTests(Phase7PortalCase):
    def _partner_identity(self):
        partners = PartnerStore(self.store, self.audit)
        pid = partners.register({"full_name": "Policy Partner", "email": "policy@example.test", "agreement_accepted": True}, "test")
        partners.approve(pid, "test")
        identity_id = self.identities.create("ttt", "PARTNER", pid, "Policy Partner", "PARTNER", "test")
        return pid, self.store.get("p6_commercial_identities", identity_id)

    def test_lead_registration_is_blocked_until_policy_is_acknowledged(self):
        pid, identity = self._partner_identity()
        with self.assertRaises(ReferralError):
            self.portals.partner_register_lead(identity, {"prospect_name": "Acme", "requested_service": "Website"})
        self.assertEqual(self.store.list("pm_referrals", "partner_id=?", (pid,)), [])

    def test_lead_registration_succeeds_after_policy_acknowledgement(self):
        pid, identity = self._partner_identity()
        self.portals.partner_acknowledge_policy(identity)
        self.assertIsNotNone(self.portals.partner_bundle(identity)["partner"]["policy_acknowledged_at"])
        referral_id = self.portals.partner_register_lead(identity, {
            "prospect_name": "Acme", "requested_service": "Website", "industry": "Retail",
        })
        row = self.store.get("pm_referrals", referral_id)
        self.assertEqual(row["partner_id"], pid)
        self.assertEqual(row["industry"], "Retail")

    def test_customer_identity_cannot_register_a_partner_lead(self):
        _, _, _, customer_identity_id = self.customer("Acme", 1000)
        customer_identity = self.store.get("p6_commercial_identities", customer_identity_id)
        with self.assertRaises(PermissionError):
            self.portals.partner_register_lead(customer_identity, {"prospect_name": "X", "requested_service": "Y"})

    def test_partner_identity_cannot_acknowledge_policy_for_another_partner(self):
        # partner_acknowledge_policy always resolves the partner id from the
        # identity's own immutable subject_ref -- there is no field in the
        # call that could name a different partner, so this proves the
        # absence of such a path rather than a specific bypass attempt.
        pid, identity = self._partner_identity()
        updated = self.portals.partner_acknowledge_policy(identity)
        self.assertEqual(updated["id"], pid)


class CustomerProjectDetailIsolationTests(Phase7PortalCase):
    def test_customer_cannot_fetch_another_customers_project_detail(self):
        org_a, client_a, _, id_a = self.customer("Acme", 1200)
        org_b, client_b, _, _ = self.customer("Beta", 900)
        identity_a = self.store.get("p6_commercial_identities", id_a)
        project_b = self.store.list("cs_projects", "client_id=?", (client_b,))[0]
        self.assertIsNone(self.portals.customer_project_detail(identity_a, project_b["id"]))

    def test_customer_can_fetch_own_project_detail(self):
        org_a, client_a, _, id_a = self.customer("Acme", 1200)
        identity_a = self.store.get("p6_commercial_identities", id_a)
        project_a = self.store.list("cs_projects", "client_id=?", (client_a,))[0]
        detail = self.portals.customer_project_detail(identity_a, project_a["id"])
        self.assertIsNotNone(detail)
        self.assertEqual(detail["project"]["id"], project_a["id"])

    def test_partner_identity_cannot_call_customer_project_detail(self):
        partners = PartnerStore(self.store, self.audit)
        pid = partners.register({"full_name": "P", "email": "p@example.test", "agreement_accepted": True}, "test")
        partners.approve(pid, "test")
        identity_id = self.identities.create("ttt", "PARTNER", pid, "P", "PARTNER", "test")
        identity = self.store.get("p6_commercial_identities", identity_id)
        with self.assertRaises(PermissionError):
            self.portals.customer_project_detail(identity, "whatever")


if __name__ == "__main__":
    unittest.main()
