"""Tests for falguna/customer_portal.py -- Phase 5 Final Client Experience,
Section 17 (Customer Portal Data/API Foundation). Real temp SQLite DB,
same convention as the rest of this suite; mirrors the two-customer
isolation fixture in tests/test_customer_context.py since this module is
a thin, scope-respecting composition on top of CustomerContextService."""

import json
import tempfile
import unittest
from pathlib import Path

from falguna.audit import AuditLog
from falguna.billing import BillingStore
from falguna.commercial import DisputeStore, ProjectStore
from falguna.comms import CommsStore
from falguna.customer_context import CustomerContextService, ScopeError
from falguna.customer_portal import CustomerPortalService
from falguna.revenue_hunter import OpportunityStore
from falguna.sales_ops import ClientStore
from falguna.store import StateStore
from falguna.store import utcnow


class _TwoCustomerPortalCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = StateStore(root / "state.db")
        self.store.migrate()
        self.audit = AuditLog(root / "audit.jsonl")
        self.comms = CommsStore(self.store, self.audit)
        self.opportunities = OpportunityStore(self.store, self.audit)
        self.clients = ClientStore(self.store, self.audit)
        self.billing = BillingStore(self.store, self.audit)
        self.disputes = DisputeStore(self.store, self.audit)
        self.projects = ProjectStore(self.store, self.audit)
        self.context = CustomerContextService(self.store, self.comms)
        self.portal = CustomerPortalService(
            self.store, self.audit, comms=self.comms, context=self.context,
            billing=self.billing, disputes=self.disputes,
        )
        self.org_a, self.client_a, self.invoice_a = self._make_customer("Acme Corp", "acme.example", 1200.0)
        self.org_b, self.client_b, self.invoice_b = self._make_customer("Beta LLC", "beta.example", 900.0)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def _make_customer(self, name, domain, invoice_amount):
        org_id = self.comms.find_or_create_organization(name, domain=domain)
        client_id = self.clients.upsert(name, "Aryan")
        opp_id = self.opportunities.create(
            {"title": f"{name} project", "description": "Confidential project details.",
             "client_name": name, "budget_rate": "$5000"}, actor="system",
        )
        self.projects.create_for_opportunity(opp_id, client_id, "Aryan", "CUSTOM_BUILD")
        contact_id = self.comms.find_or_create_contact(f"contact@{domain}", name=f"{name} Contact", organization_id=org_id)
        conv = self.comms.open_conversation(
            "EMAIL", "billing", subject=f"{name} billing", contact_id=contact_id, organization_id=org_id,
            actor="system", linked_client_id=client_id,
        )
        self.comms.add_message(conv["id"], "INBOUND", f"{name} has a billing question.", actor="website")
        self.comms.add_message(
            conv["id"], "OUTBOUND", f"Internal-only: {name} is a slow payer, watch this account.",
            kind="note", is_internal_note=True, actor="Aryan",
        )
        invoice_id = self.billing.create_invoice(client_id, "Aryan", invoice_amount, due_date="2026-12-01")
        return org_id, client_id, invoice_id


class PortalBundleContentTests(_TwoCustomerPortalCase):
    def test_bundle_includes_this_customers_own_invoice_project_and_conversation(self):
        bundle = self.portal.get_portal_bundle(self.org_a)
        self.assertEqual(bundle["organization"]["id"], self.org_a)
        self.assertEqual([c["id"] for c in bundle["clients"]], [self.client_a])
        self.assertEqual(len(bundle["invoices"]), 1)
        self.assertEqual(bundle["invoices"][0]["id"], self.invoice_a)
        self.assertEqual(bundle["invoices"][0]["amount"], 1200.0)
        self.assertEqual(len(bundle["projects"]), 1)
        self.assertEqual(len(bundle["conversations"]), 1)

    def test_internal_only_note_is_stripped_from_the_bundle(self):
        bundle = self.portal.get_portal_bundle(self.org_a)
        bodies = [m["body"] for conv in bundle["conversations"] for m in conv["messages"]]
        self.assertTrue(any("billing question" in b for b in bodies))
        self.assertFalse(any("slow payer" in b for b in bodies))

    def test_unknown_organization_raises(self):
        with self.assertRaises(ScopeError):
            self.portal.get_portal_bundle("does-not-exist")


class PortalDisputeVisibilityTests(_TwoCustomerPortalCase):
    def test_dispute_appears_with_only_customer_safe_fields(self):
        dispute_id = self.disputes.open(self.invoice_a, "Aryan", "late delivery", 100.0, evidence={"ticket": "T-1", "internal_note": "staff-only detail"})
        bundle = self.portal.get_portal_bundle(self.org_a)
        self.assertEqual(len(bundle["disputes"]), 1)
        dispute = bundle["disputes"][0]
        self.assertEqual(dispute["id"], dispute_id)
        self.assertEqual(dispute["status"], "OPEN")
        self.assertNotIn("evidence_json", dispute)
        self.assertNotIn("commission_impact_json", dispute)

    def test_refund_status_is_customer_visible_only_after_verified_confirmation(self):
        now = utcnow()
        base = {"organization_id": self.org_a, "payment_intent_id": "synthetic-payment",
                "invoice_id": self.invoice_a, "approval_request_id": "synthetic-approval", "amount": 25,
                "currency": "USD", "eligibility_basis_json": "{}", "reason": "synthetic test",
                "evidence_json": None, "created_at": now, "updated_at": now}
        pending_id = self.store.create("p6_refunds", {**base, "status": "PENDING_APPROVAL", "provider_ref": None,
            "idempotency_key": "pending-refund"})
        confirmed_id = self.store.create("p6_refunds", {**base, "status": "CONFIRMED", "provider_ref": "sandbox-ref",
            "evidence_json": '{"verified":true}', "idempotency_key": "confirmed-refund"})
        refunds = {r["id"]: r for r in self.portal.get_portal_bundle(self.org_a)["refunds"]}
        self.assertIsNone(refunds[pending_id]["provider_ref"])
        self.assertEqual(refunds[confirmed_id]["provider_ref"], "sandbox-ref")



class CrossCustomerPortalIsolationTests(_TwoCustomerPortalCase):
    """The core requirement for a customer-facing data path: Customer A's
    portal bundle must never contain anything of Customer B's -- no
    invoice, no project, no dispute, no conversation, under any field."""

    def test_org_a_bundle_contains_no_org_b_data(self):
        dispute_b = self.disputes.open(self.invoice_b, "Aryan", "overcharge", 50.0, evidence={"ticket": "T-2"})
        bundle = self.portal.get_portal_bundle(self.org_a)
        self.assertNotIn(self.client_b, [c["id"] for c in bundle["clients"]])
        self.assertNotIn(self.invoice_b, [i["id"] for i in bundle["invoices"]])
        self.assertNotIn(dispute_b, [d["id"] for d in bundle["disputes"]])
        all_bodies = " ".join(m["body"] for conv in bundle["conversations"] for m in conv["messages"])
        self.assertNotIn("Beta LLC", all_bodies)

    def test_requested_by_organization_mismatch_is_rejected(self):
        with self.assertRaises(ScopeError):
            self.portal.get_portal_bundle(self.org_a, requested_by_organization_id=self.org_b)

    def test_requested_by_matching_organization_succeeds(self):
        bundle = self.portal.get_portal_bundle(self.org_a, requested_by_organization_id=self.org_a)
        self.assertEqual(bundle["organization"]["id"], self.org_a)



class ProjectDetailTests(_TwoCustomerPortalCase):
    def _project_id_for(self, client_id):
        return self.store.list("cs_projects", "client_id=?", (client_id,))[0]["id"]

    def test_project_detail_includes_sow_and_milestones_matched_to_real_invoices(self):
        project_id = self._project_id_for(self.client_a)
        opportunity_id = self.store.get("cs_projects", project_id)["opportunity_id"]
        now = utcnow()
        self.store.create("rh_closing_records", {
            "id": "closing-test-1", "opportunity_id": opportunity_id, "client_id": self.client_a,
            "final_scope": "Build a CRM", "final_price": 5000, "currency": "USD",
            "payment_terms": "50% upfront", "deadline": "2026-12-31",
            "deliverables": "CRM application", "acceptance_criteria": "UAT sign-off",
            "milestones_json": json.dumps([{"name": "Kickoff", "amount": 2500}, {"name": "Delivery", "amount": 2500}]),
            "communication_channel": "EMAIL", "actor": "test", "created_at": now,
        })
        self.store.update("rh_invoices", self.invoice_a, milestone="Kickoff")
        detail = self.portal.get_project_detail(self.org_a, project_id)
        self.assertIsNotNone(detail)
        self.assertEqual(detail["project"]["id"], project_id)
        self.assertEqual(detail["sow"]["final_scope"], "Build a CRM")
        self.assertEqual(detail["sow"]["final_price"], 5000)
        milestones = {m["name"]: m for m in detail["milestones"]}
        self.assertEqual(set(milestones), {"Kickoff", "Delivery"})
        self.assertEqual(milestones["Kickoff"]["status"], "DRAFT")  # real invoice's own status, not invented
        self.assertEqual(milestones["Kickoff"]["invoice_id"], self.invoice_a)
        self.assertEqual(milestones["Delivery"]["status"], "SCOPED")  # no matching invoice -- honest fallback
        self.assertIsNone(milestones["Delivery"]["invoice_id"])

    def test_project_detail_has_no_sow_when_no_closing_record_exists(self):
        project_id = self._project_id_for(self.client_a)
        detail = self.portal.get_project_detail(self.org_a, project_id)
        self.assertIsNotNone(detail)
        self.assertIsNone(detail["sow"])
        self.assertEqual(detail["milestones"], [])

    def test_project_detail_returns_none_for_another_customers_project(self):
        project_b_id = self._project_id_for(self.client_b)
        self.assertIsNone(self.portal.get_project_detail(self.org_a, project_b_id))

    def test_project_detail_returns_none_for_an_unknown_project_id(self):
        self.assertIsNone(self.portal.get_project_detail(self.org_a, "does-not-exist"))

    def test_project_detail_rejects_organization_scope_mismatch(self):
        project_id = self._project_id_for(self.client_a)
        with self.assertRaises(ScopeError):
            self.portal.get_project_detail(self.org_a, project_id, requested_by_organization_id=self.org_b)


class ConversationSafetyTests(_TwoCustomerPortalCase):
    def test_unsent_draft_outbound_messages_are_never_shown_to_the_customer(self):
        conv_id = self.store.list("comm_conversations", "organization_id=?", (self.org_a,))[0]["id"]
        self.comms.add_message(conv_id, "OUTBOUND", "Draft reply not yet approved.", kind="message", actor="AI")
        bundle = self.portal.get_portal_bundle(self.org_a)
        bodies = [m["body"] for conv in bundle["conversations"] for m in conv["messages"]]
        self.assertFalse(any("Draft reply not yet approved" in b for b in bodies))

    def test_sent_outbound_messages_are_shown_to_the_customer(self):
        conv_id = self.store.list("comm_conversations", "organization_id=?", (self.org_a,))[0]["id"]
        msg_id = self.comms.add_message(conv_id, "OUTBOUND", "Here is the update you asked for.", kind="message", actor="Aryan")["id"]
        self.store.update("comm_messages", msg_id, status="SENT")
        bundle = self.portal.get_portal_bundle(self.org_a)
        bodies = [m["body"] for conv in bundle["conversations"] for m in conv["messages"]]
        self.assertTrue(any("Here is the update you asked for" in b for b in bodies))

    def test_approved_but_unsent_outbound_messages_are_not_shown_to_the_customer(self):
        conv_id = self.store.list("comm_conversations", "organization_id=?", (self.org_a,))[0]["id"]
        self.comms.add_message(
            conv_id, "OUTBOUND", "Approved internally but not actually sent.",
            kind="message", status="APPROVED", actor="Aryan",
        )
        bundle = self.portal.get_portal_bundle(self.org_a)
        bodies = [m["body"] for conv in bundle["conversations"] for m in conv["messages"]]
        self.assertFalse(any("Approved internally but not actually sent" in b for b in bodies))

    def test_customer_safe_message_never_exposes_internal_sender_agent(self):
        bundle = self.portal.get_portal_bundle(self.org_a)
        for conv in bundle["conversations"]:
            for m in conv["messages"]:
                self.assertNotIn("sender_agent", m)


if __name__ == "__main__":
    unittest.main()
