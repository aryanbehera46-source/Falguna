import tempfile
import unittest
from pathlib import Path

from falguna.alerts import AlertAckStore, alerts_snapshot
from falguna.audit import AuditLog
from falguna.billing import BillingStore
from falguna.onboarding import OnboardingStore
from falguna.revenue_hunter import OpportunityStore, ProposalStore
from falguna.sales_ops import ClientStore
from falguna.store import StateStore
from falguna.ttt_hq import NeedsAryanQueue


class OperationalAlertsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.tmp.name) / "state.db")
        self.store.migrate()
        self.audit = AuditLog(Path(self.tmp.name) / "audit.jsonl")
        self.needs_aryan = NeedsAryanQueue(self.store, self.audit)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_empty_state_has_no_alerts(self):
        snapshot = alerts_snapshot(self.store, self.audit)
        self.assertEqual(snapshot["total"], 0)
        self.assertEqual(snapshot["active_count"], 0)

    def test_pending_proposal_creates_quotation_alert(self):
        opp_id = OpportunityStore(self.store, self.audit).create({"title": "Build", "client_name": "Acme"}, "Aryan")
        ProposalStore(self.store, self.audit, needs_aryan=self.needs_aryan).generate(opp_id, "detailed", "Aryan")
        snapshot = alerts_snapshot(self.store, self.audit)
        categories = {a["category"] for a in snapshot["alerts"]}
        self.assertIn("quotation_awaiting_approval", categories)

    def test_overdue_invoice_alert_and_read_only(self):
        client_id = ClientStore(self.store, self.audit).upsert("Acme", "Aryan")
        billing = BillingStore(self.store, self.audit)
        invoice_id = billing.create_invoice(client_id, "Aryan", 500, due_date="2000-01-01")
        billing.mark_sent(invoice_id, "Aryan")
        snapshot = alerts_snapshot(self.store, self.audit)
        overdue = [a for a in snapshot["alerts"] if a["category"] == "overdue_invoice"]
        self.assertEqual(len(overdue), 1)
        self.assertEqual(overdue[0]["affected_entity"]["id"], invoice_id)
        # Read-only: the invoice's real status is untouched by computing alerts.
        self.assertEqual(billing.get(invoice_id)["status"], "SENT")

    def test_held_commission_alert(self):
        now = "2026-09-30T00:00:00+00:00"
        partner_id = self.store.create("pm_partners", {"full_name": "P", "email": "p@example.test", "verification_status": "VERIFIED", "agreement_accepted": 1, "status": "APPROVED", "created_at": now, "updated_at": now})
        referral_id = self.store.create("pm_referrals", {"partner_id": partner_id, "prospect_name": "Prospect", "requested_service": "Build", "attribution_status": "ATTRIBUTED", "duplicate_flag": 0, "created_at": now, "updated_at": now})
        self.store.create("pm_commissions", {"referral_id": referral_id, "partner_id": partner_id, "rate": .1, "status": "HELD", "eligible_amount": 0, "refunded_amount": 0, "hold_reason": "duplicate referral under review", "created_at": now, "updated_at": now})
        snapshot = alerts_snapshot(self.store, self.audit)
        held = [a for a in snapshot["alerts"] if a["category"] == "held_commission"]
        self.assertEqual(len(held), 1)
        self.assertIn("duplicate referral", held[0]["explanation"])

    def test_project_awaiting_intake_alert(self):
        opp_id = OpportunityStore(self.store, self.audit).create({"title": "Build", "client_name": "Acme"}, "Aryan")
        client_id = ClientStore(self.store, self.audit).upsert("Acme", "Aryan")
        now = "2026-09-30T00:00:00+00:00"
        self.store.create("rh_closing_records", {"opportunity_id": opp_id, "client_id": client_id, "actor": "Aryan", "created_at": now})
        OnboardingStore(self.store, self.audit).init_checklist(opp_id, "Aryan")
        snapshot = alerts_snapshot(self.store, self.audit)
        categories = {a["category"] for a in snapshot["alerts"]}
        self.assertIn("project_awaiting_intake", categories)

    def test_alert_id_is_stable_across_calls(self):
        opp_id = OpportunityStore(self.store, self.audit).create({"title": "Build", "client_name": "Acme"}, "Aryan")
        ProposalStore(self.store, self.audit, needs_aryan=self.needs_aryan).generate(opp_id, "detailed", "Aryan")
        first = alerts_snapshot(self.store, self.audit)["alerts"][0]["id"]
        second = alerts_snapshot(self.store, self.audit)["alerts"][0]["id"]
        self.assertEqual(first, second)

    def test_resolved_underlying_problem_stops_appearing(self):
        opp_id = OpportunityStore(self.store, self.audit).create({"title": "Build", "client_name": "Acme"}, "Aryan")
        proposal = ProposalStore(self.store, self.audit, needs_aryan=self.needs_aryan).generate(opp_id, "detailed", "Aryan")
        before = alerts_snapshot(self.store, self.audit)
        self.assertTrue(any(a["category"] == "quotation_awaiting_approval" for a in before["alerts"]))
        self.needs_aryan.decide(proposal["needs_aryan_id"], "approve", "Aryan")
        after = alerts_snapshot(self.store, self.audit)
        self.assertFalse(any(a["category"] == "quotation_awaiting_approval" for a in after["alerts"]))

    def test_acknowledge_persists_and_marks_alert(self):
        opp_id = OpportunityStore(self.store, self.audit).create({"title": "Build", "client_name": "Acme"}, "Aryan")
        ProposalStore(self.store, self.audit, needs_aryan=self.needs_aryan).generate(opp_id, "detailed", "Aryan")
        alert_id = alerts_snapshot(self.store, self.audit)["alerts"][0]["id"]
        AlertAckStore(self.store, self.audit).acknowledge(alert_id, "Aryan", note="seen it")
        snapshot = alerts_snapshot(self.store, self.audit)
        alert = next(a for a in snapshot["alerts"] if a["id"] == alert_id)
        self.assertTrue(alert["acknowledged"])
        self.assertEqual(alert["acknowledged_by"], "Aryan")
        self.assertEqual(alert["note"], "seen it")
        self.assertEqual(snapshot["active_count"], 0)

    def test_acknowledge_requires_actor(self):
        with self.assertRaises(ValueError):
            AlertAckStore(self.store, self.audit).acknowledge("some-alert", "")


if __name__ == "__main__":
    unittest.main()
