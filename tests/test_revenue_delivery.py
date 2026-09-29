import subprocess
import tempfile
import unittest
from pathlib import Path

from falguna.onboarding import ONBOARDING_ITEM_TYPES, OnboardingStore
from falguna.revenue_delivery import RevenueDeliveryError, RevenueDeliveryService
from falguna.revenue_hunter import ActiveJobStore, OpportunityStore, ProposalStore
from falguna.runtime import open_control_plane
from falguna.sales_ops import ClosingService, SalesPolicyStore
from falguna.ttt_hq import NeedsAryanQueue


class RevenueDeliveryJourneyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(self.repo)], check=True, capture_output=True)
        (self.repo / "README.md").write_text("seed\n")
        self.control, self.store = open_control_plane(self.repo)
        self.audit = self.control.audit

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def _approved_project(self):
        opp_id = OpportunityStore(self.store, self.audit).create({
            "title": "Website enquiry", "client_name": "Example Client",
            "description": "Deliver an accessible launch site", "final_price": 2400,
        }, actor="website", source="website")
        queue = NeedsAryanQueue(self.store, self.audit, self.control)
        proposal = ProposalStore(self.store, self.audit, queue).generate(opp_id, "short", actor="Aryan")
        ProposalStore(self.store, self.audit, queue).mark_approved(proposal["proposal_id"], "Aryan")
        SalesPolicyStore(self.store).save({"allowed_currencies": ["USD"]})
        closed = ClosingService(self.store, self.audit, needs_aryan=queue).close(
            opp_id, "Aryan", client_name="Example Client", final_scope="Accessible launch site",
            final_price=2400, currency="USD", acceptance_criteria="Automated and browser QA pass",
        )
        onboarding = OnboardingStore(self.store, self.audit)
        onboarding.init_checklist(opp_id)
        for item_type in ONBOARDING_ITEM_TYPES:
            kwargs = {"notes": "secure vault reference"} if item_type == "credentials_access" else {"value_text": "confirmed"}
            onboarding.set_item(opp_id, item_type, "RECEIVED", **kwargs)
        ActiveJobStore(self.store, self.audit).create_from_won_opportunity(opp_id)
        return opp_id, closed

    def test_full_persisted_journey_ends_with_draft_invoice_only(self):
        opp_id, _ = self._approved_project()
        service = RevenueDeliveryService(self.store, self.audit)
        before = service.snapshot(opp_id)
        self.assertEqual(before["next_action"], "Evidence-backed handover")
        result = service.prepare_handover_and_invoice_draft(
            opp_id, "Aryan", evidence={"tests": "42 passed", "browser_qa": "desktop and mobile"},
            qa_checklist={"delivery_verified": True, "independent_qa": True, "acceptance_criteria_met": True, "handover_ready": True},
        )
        self.assertEqual(result["invoice_status"], "DRAFT")
        self.assertFalse(result["external_action_taken"])
        after = service.snapshot(opp_id)
        self.assertTrue(all(step["complete"] for step in after["steps"]))
        self.assertEqual(after["invoices"][0]["status"], "DRAFT")

    def test_handover_refuses_missing_independent_qa(self):
        opp_id, _ = self._approved_project()
        with self.assertRaisesRegex(RevenueDeliveryError, "independent_qa"):
            RevenueDeliveryService(self.store, self.audit).prepare_handover_and_invoice_draft(
                opp_id, "Aryan", evidence={"tests": "passed"},
                qa_checklist={"delivery_verified": True, "acceptance_criteria_met": True, "handover_ready": True},
            )
        self.assertEqual(self.store.list("rh_completion_records"), [])
        self.assertEqual(self.store.list("rh_invoices"), [])

    def test_finalization_is_idempotent(self):
        opp_id, _ = self._approved_project()
        service = RevenueDeliveryService(self.store, self.audit)
        kwargs = {"evidence": {"tests": "passed"}, "qa_checklist": {"delivery_verified": True, "independent_qa": True, "acceptance_criteria_met": True, "handover_ready": True}}
        first = service.prepare_handover_and_invoice_draft(opp_id, "Aryan", **kwargs)
        second = service.prepare_handover_and_invoice_draft(opp_id, "Aryan", **kwargs)
        self.assertEqual(first["completion_id"], second["completion_id"])
        self.assertEqual(first["invoice_id"], second["invoice_id"])
        self.assertEqual(len(self.store.list("rh_invoices")), 1)


if __name__ == "__main__":
    unittest.main()
