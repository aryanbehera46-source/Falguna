"""Tests for TTT Communications V2 Milestone 4
(falguna/customer_context.py): scoped customer context and explicit
cross-customer isolation. Real temp SQLite DB, same convention as the rest
of this suite."""

import json
import tempfile
import unittest
from pathlib import Path

from falguna.audit import AuditLog
from falguna.comms import CommsStore
from falguna.customer_context import CustomerContextService, ScopeError
from falguna.revenue_hunter import OpportunityStore, ProposalStore
from falguna.store import StateStore
from falguna.ttt_hq import NeedsAryanQueue


class _TwoCustomerCase(unittest.TestCase):
    """Two entirely separate customers (Acme and Beta), each with a
    contact, a conversation with an internal note, and a linked
    opportunity/proposal -- the fixture every isolation test needs."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = StateStore(root / "state.db")
        self.store.migrate()
        self.audit = AuditLog(root / "audit.jsonl")
        self.needs_aryan = NeedsAryanQueue(self.store, self.audit)
        self.comms = CommsStore(self.store, self.audit, self.needs_aryan)
        self.opportunities = OpportunityStore(self.store, self.audit)
        self.proposals = ProposalStore(self.store, self.audit, self.needs_aryan)
        self.context = CustomerContextService(self.store, self.comms)

        self.org_a, self.conv_a, self.opp_a = self._make_customer("Acme Corp", "acme.example", "acme-contact@acme.example")
        self.org_b, self.conv_b, self.opp_b = self._make_customer("Beta LLC", "beta.example", "beta-contact@beta.example")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def _make_customer(self, org_name, domain, email):
        org_id = self.comms.find_or_create_organization(org_name, domain=domain)
        contact_id = self.comms.find_or_create_contact(email, name=org_name + " Contact", organization_id=org_id)
        opp_id = self.opportunities.create({
            "title": f"{org_name} project", "description": "Confidential project details.",
            "client_name": org_name, "budget_rate": "$5000",
        }, actor="system")
        conv = self.comms.open_conversation(
            "EMAIL", "sales", subject=f"{org_name} enquiry", contact_id=contact_id, organization_id=org_id,
            actor="system", linked_opportunity_id=opp_id,
        )
        self.comms.add_message(conv["id"], "INBOUND", f"{org_name} wants a quote.", actor="website")
        self.comms.add_message(
            conv["id"], "OUTBOUND", f"Internal-only: {org_name} pays late, watch this account.",
            kind="note", is_internal_note=True, actor="Aryan",
        )
        return org_id, conv["id"], opp_id


class ScopedRetrievalTests(_TwoCustomerCase):
    def test_context_includes_only_this_organizations_own_data(self):
        ctx = self.context.internal_context(self.org_a)
        self.assertEqual(ctx["organization"]["id"], self.org_a)
        self.assertEqual(len(ctx["conversations"]), 1)
        self.assertEqual(ctx["conversations"][0]["id"], self.conv_a)
        self.assertEqual(len(ctx["opportunities"]), 1)
        self.assertEqual(ctx["opportunities"][0]["id"], self.opp_a)

    def test_unknown_organization_raises_rather_than_returning_empty_silently(self):
        with self.assertRaises(ScopeError):
            self.context.internal_context("does-not-exist")


class CrossCustomerLeakageTests(_TwoCustomerCase):
    """The core Milestone 4 requirement: Customer A must never retrieve
    Customer B's information, under any of the fields this service returns."""

    def _assert_no_org_b_leakage(self, ctx):
        blob = json.dumps(ctx, default=str)
        self.assertNotIn(self.org_b, blob)
        self.assertNotIn(self.conv_b, blob)
        self.assertNotIn(self.opp_b, blob)
        self.assertNotIn("Beta", blob)
        self.assertNotIn("beta.example", blob)

    def test_internal_context_for_org_a_contains_no_trace_of_org_b(self):
        ctx = self.context.internal_context(self.org_a)
        self._assert_no_org_b_leakage(ctx)

    def test_customer_facing_context_for_org_a_contains_no_trace_of_org_b(self):
        ctx = self.context.customer_facing_context(self.org_a)
        self._assert_no_org_b_leakage(ctx)

    def test_contacts_returned_are_only_this_organizations_contacts(self):
        ctx = self.context.internal_context(self.org_a)
        for contact in ctx["contacts"]:
            self.assertEqual(contact["organization_id"], self.org_a)

    def test_proposals_and_followups_are_scoped_to_this_organizations_opportunities_only(self):
        # Generate a proposal for BOTH customers, then confirm org A's
        # context only ever surfaces org A's proposal.
        self.proposals.generate(self.opp_a, "short", actor="ai_workforce")
        self.proposals.generate(self.opp_b, "short", actor="ai_workforce")
        ctx_a = self.context.internal_context(self.org_a)
        self.assertTrue(all(p["opportunity_id"] == self.opp_a for p in ctx_a["proposals"]))
        ctx_b = self.context.internal_context(self.org_b)
        self.assertTrue(all(p["opportunity_id"] == self.opp_b for p in ctx_b["proposals"]))

    def test_assert_scope_rejects_a_mismatched_authorization_claim(self):
        with self.assertRaises(ScopeError):
            self.context.assert_scope(self.org_b, self.org_a)

    def test_assert_scope_allows_a_matching_claim_and_a_null_internal_caller(self):
        self.context.assert_scope(self.org_a, self.org_a)  # matching -- no raise
        self.context.assert_scope(None, self.org_a)  # internal/unscoped caller -- no raise

    def test_internal_context_raises_before_returning_anything_on_a_scope_mismatch(self):
        with self.assertRaises(ScopeError):
            self.context.internal_context(self.org_a, requested_by_organization_id=self.org_b)


class InternalNoteIsolationTests(_TwoCustomerCase):
    def test_internal_context_includes_internal_notes(self):
        ctx = self.context.internal_context(self.org_a)
        notes = [m for conv in ctx["conversations"] for m in conv["messages"] if m.get("is_internal_note")]
        self.assertTrue(any("Internal-only" in n["body"] for n in notes))

    def test_customer_facing_context_never_includes_internal_notes(self):
        ctx = self.context.customer_facing_context(self.org_a)
        for conv in ctx["conversations"]:
            for message in conv["messages"]:
                self.assertFalse(message.get("is_internal_note"))
                self.assertNotIn("Internal-only", message["body"])
                self.assertNotIn("pays late", message["body"])


class FalgunaInternalOperationsSeparationTests(_TwoCustomerCase):
    """Milestone 4: 'TTT internal operations' (Falguna's own engineering
    mission system -- missions/requirements/tasks/runs) must never surface
    through the customer-scoped read path."""

    def test_context_never_touches_falguna_engineering_tables(self):
        # A real mission row exists in the same database (created by other
        # parts of this codebase in normal operation) -- prove it never
        # leaks into a customer context bundle regardless.
        self.store.create("missions", {
            "title": "Internal engineering work", "status": "in_progress",
            "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
        })
        ctx = self.context.internal_context(self.org_a)
        blob = json.dumps(ctx, default=str)
        self.assertNotIn("Internal engineering work", blob)
        self.assertNotIn("mission", blob.lower().replace("commission", ""))


if __name__ == "__main__":
    unittest.main()
