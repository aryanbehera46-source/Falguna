import tempfile
import unittest
from pathlib import Path

from falguna.audit import AuditLog
from falguna.billing import BillingStore, CompletionService
from falguna.company_state import CompanyStateService
from falguna.revenue_hunter import OpportunityStore, ProposalStore
from falguna.sales_ops import ClientStore
from falguna.store import StateStore


class CompanyStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.tmp.name) / "state.db")
        self.store.migrate()
        self.audit = AuditLog(Path(self.tmp.name) / "audit.jsonl")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_empty_snapshot_is_truthful_and_sourced(self):
        state = CompanyStateService(self.store).snapshot()
        self.assertEqual(state["financials"]["quoted"], 0)
        self.assertEqual(state["financials"]["invoiced"], 0)
        self.assertEqual(state["financials"]["collected"], 0)
        self.assertIn("commercial", state["sources"])

    def test_financial_states_are_distinct_and_not_double_counted(self):
        opportunities = OpportunityStore(self.store, self.audit)
        opp_id = opportunities.create({"title": "Build", "client_name": "Acme"}, "Aryan")
        opportunities.mark_won(opp_id, "Aryan", final_price=1000)
        proposal_id = ProposalStore(self.store, self.audit).generate(opp_id, "detailed", "Aryan")["proposal_id"]
        ProposalStore(self.store, self.audit).mark_approved(proposal_id, "Aryan")
        client_id = ClientStore(self.store, self.audit).upsert("Acme", "Aryan")
        billing = BillingStore(self.store, self.audit)
        invoice_id = billing.create_invoice(client_id, "Aryan", 800, opportunity_id=opp_id)
        billing.mark_sent(invoice_id, "Aryan")
        billing.record_payment(invoice_id, 300, "Aryan", evidence="synthetic receipt")
        state = CompanyStateService(self.store).snapshot()
        self.assertEqual(state["financials"]["quoted"], 1000)
        self.assertEqual(state["financials"]["invoiced"], 800)
        self.assertEqual(state["financials"]["collected"], 300)
        self.assertEqual(state["financials"]["outstanding"], 500)
        self.assertEqual(state["financials"]["receipt_count"], 1)

    def test_delivery_qa_and_commission_statuses_are_persisted_reads(self):
        opp_id = OpportunityStore(self.store, self.audit).create({"title": "QA build"}, "Aryan")
        CompletionService(self.store, self.audit).record_completion(
            opp_id, "Aryan", {"artifact": "synthetic"}, checklist={"independent_qa": True, "handover_ready": True}
        )
        now = "2026-09-30T00:00:00+00:00"
        partner_id = self.store.create("pm_partners", {"full_name": "Synthetic Partner", "email": "synthetic@example.test", "verification_status": "VERIFIED", "agreement_accepted": 1, "status": "APPROVED", "created_at": now, "updated_at": now})
        referral_id = self.store.create("pm_referrals", {"partner_id": partner_id, "prospect_name": "Synthetic Prospect", "requested_service": "Build", "attribution_status": "ATTRIBUTED", "duplicate_flag": 0, "created_at": now, "updated_at": now})
        self.store.create("pm_commissions", {"referral_id": referral_id, "partner_id": partner_id, "rate": .1, "status": "ELIGIBLE", "eligible_amount": 100, "refunded_amount": 0, "created_at": now, "updated_at": now})
        state = CompanyStateService(self.store).snapshot()
        self.assertEqual(state["delivery"]["handovers"], 1)
        self.assertEqual(state["delivery"]["qa_passed"], 1)
        self.assertEqual(state["partners"]["referrals_by_status"]["ATTRIBUTED"], 1)
        self.assertEqual(state["partners"]["eligible"], 100)


if __name__ == "__main__":
    unittest.main()
