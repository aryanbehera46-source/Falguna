import tempfile
import unittest
from pathlib import Path

from falguna.audit import AuditLog
from falguna.billing import BillingStore
from falguna.commercial import ProjectStore
from falguna.comms import CommsStore
from falguna.partner_management import PartnerStore, ReferralStore
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


if __name__ == "__main__":
    unittest.main()
