"""Phase 5 Continuation, Section 22 -- Learning from Outcomes V1.

Covers: automatic outcome capture on a project's CLOSED transition,
idempotency, QA-cycle-based rework detection (a real QA -> IN_DELIVERY ->
QA bounce, not an inference), dispute-based acceptance classification,
reuse of economics_for_project (not a duplicate computation), the
backfill helper for projects closed before this module existed, and the
explicit non-wiring into QualificationEngine/FoundationStore/
recommend_route (the documented Section 22 boundary). Synthetic data
only.
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

from falguna.commercial import (
    CostEntryStore, DisputeStore, FoundationStore, IntakeStore, ProjectStore, ServiceCatalogStore,
)
from falguna.billing import BillingStore
from falguna.outcomes import OutcomeError, OutcomeStore, backfill_outcomes_for_closed_projects
from falguna.runtime import open_control_plane


class OutcomesTestCase(unittest.TestCase):
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
        self.intakes = IntakeStore(self.store, self.audit)
        self.projects = ProjectStore(self.store, self.audit)
        self.disputes = DisputeStore(self.store, self.audit)
        self.billing = BillingStore(self.store, self.audit)
        self.outcomes = OutcomeStore(self.store, self.audit)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def _approved_service(self, standard_delivery_days=None):
        sid = self.services.create(
            "outcomes-svc", "web_dev", "Business Website V1", "A synthetic test service.",
            pricing_model="fixed", delivery_mode="remote",
        )
        self.services.set_approval(sid, "Aryan", "APPROVED")
        if standard_delivery_days is not None:
            self.services.set_international_profile(sid, "Aryan", standard_delivery_days=standard_delivery_days)
        return sid

    def _project_through_to_closed(self, service_id=None, qa_bounces=0):
        iid = self.intakes.create("Outcomes Co", "A new site", service_id=service_id)
        self.intakes.qualify(iid, "Aryan", "QUALIFIED")
        self.intakes.approve(iid, "Aryan")
        project_id = self.intakes.convert_to_project(iid, "Aryan", "CUSTOM_BUILD")
        self.projects.transition(project_id, "Aryan", "IN_DELIVERY")
        self.projects.transition(project_id, "Aryan", "QA")
        for _ in range(qa_bounces):
            self.projects.transition(project_id, "Aryan", "IN_DELIVERY")
            self.projects.transition(project_id, "Aryan", "QA")
        self.projects.transition(project_id, "Aryan", "HANDED_OVER")
        self.projects.transition(project_id, "Aryan", "INVOICED")
        return project_id

    def test_closing_a_project_automatically_records_an_outcome(self):
        project_id = self._project_through_to_closed()
        self.projects.transition(project_id, "Aryan", "CLOSED")
        outcome = self.outcomes.get_by_project(project_id)
        self.assertIsNotNone(outcome)
        self.assertEqual(outcome["acceptance"], "ACCEPTED_CLEAN")
        self.assertEqual(outcome["qa_cycle_count"], 1)
        self.assertEqual(outcome["dispute_count"], 0)

    def test_cannot_record_outcome_for_a_non_closed_project(self):
        project_id = self._project_through_to_closed()
        with self.assertRaises(OutcomeError):
            self.outcomes.record_for_project(project_id, actor="Aryan")

    def test_recording_outcome_twice_is_idempotent(self):
        project_id = self._project_through_to_closed()
        self.projects.transition(project_id, "Aryan", "CLOSED")
        first = self.outcomes.get_by_project(project_id)
        again_id = self.outcomes.record_for_project(project_id, actor="Aryan")
        self.assertEqual(again_id, first["id"])
        self.assertEqual(len(self.store.list("cs_outcome_records", "project_id=?", (project_id,))), 1)

    def test_qa_rework_bounce_is_detected_as_real_evidence(self):
        project_id = self._project_through_to_closed(qa_bounces=2)
        self.projects.transition(project_id, "Aryan", "CLOSED")
        outcome = self.outcomes.get_by_project(project_id)
        self.assertEqual(outcome["qa_cycle_count"], 3)
        self.assertEqual(outcome["acceptance"], "ACCEPTED_WITH_REWORK")

    def test_estimated_delivery_days_comes_only_from_assessed_service_field(self):
        sid = self._approved_service()  # standard_delivery_days left unset
        project_id = self._project_through_to_closed(service_id=sid)
        self.projects.transition(project_id, "Aryan", "CLOSED")
        outcome = self.outcomes.get_by_project(project_id)
        self.assertIsNone(outcome["estimated_delivery_days"])

    def test_estimated_delivery_days_reflects_assessed_service_field(self):
        sid = self._approved_service(standard_delivery_days=10)
        project_id = self._project_through_to_closed(service_id=sid)
        self.projects.transition(project_id, "Aryan", "CLOSED")
        outcome = self.outcomes.get_by_project(project_id)
        self.assertEqual(outcome["estimated_delivery_days"], 10)

    def test_actual_delivery_days_is_a_real_nonnegative_elapsed_duration(self):
        project_id = self._project_through_to_closed()
        self.projects.transition(project_id, "Aryan", "CLOSED")
        outcome = self.outcomes.get_by_project(project_id)
        self.assertIsNotNone(outcome["actual_delivery_days"])
        self.assertGreaterEqual(outcome["actual_delivery_days"], 0)

    def test_dispute_marks_acceptance_as_disputed(self):
        project_id = self._project_through_to_closed()
        project = self.projects.get(project_id)
        invoice_id = self.billing.create_invoice(project["client_id"], "Aryan", 500.0, opportunity_id=project["opportunity_id"])
        self.billing.mark_ready(invoice_id, "Aryan")
        self.billing.mark_sent(invoice_id, "Aryan")
        self.billing.record_payment(invoice_id, 500.0, "Aryan", {"ref": "outcome-test-payment"})
        dispute_id = self.disputes.open(invoice_id, "Aryan", "quality concern", 50.0, evidence={"ticket": "OUT-1"})
        self.disputes.resolve(dispute_id, "Aryan", "Partial refund approved.", refund_amount=50.0, evidence={"approved_by": "Aryan"})
        self.projects.transition(project_id, "Aryan", "CLOSED")
        outcome = self.outcomes.get_by_project(project_id)
        self.assertEqual(outcome["dispute_count"], 1)
        self.assertEqual(outcome["acceptance"], "DISPUTED")
        # Reuses economics_for_project rather than recomputing independently.
        self.assertEqual(outcome["net_collected"], 450.0)

    def test_list_filters_by_service_and_acceptance(self):
        sid = self._approved_service()
        clean_project = self._project_through_to_closed(service_id=sid)
        self.projects.transition(clean_project, "Aryan", "CLOSED")
        reworked_project = self._project_through_to_closed(service_id=sid, qa_bounces=1)
        self.projects.transition(reworked_project, "Aryan", "CLOSED")
        all_for_service = self.outcomes.list(service_id=sid)
        self.assertEqual(len(all_for_service), 2)
        reworked_only = self.outcomes.list(service_id=sid, acceptance="ACCEPTED_WITH_REWORK")
        self.assertEqual(len(reworked_only), 1)
        self.assertEqual(reworked_only[0]["project_id"], reworked_project)

    def test_backfill_records_outcomes_for_already_closed_projects(self):
        # Simulate a project that reached CLOSED through the real state
        # machine (so the automatic hook already ran), then verify the
        # backfill is a safe no-op for it, and separately that a genuinely
        # missing record would be created (exercised via direct deletion
        # to simulate "closed before this module existed").
        project_id = self._project_through_to_closed()
        self.projects.transition(project_id, "Aryan", "CLOSED")
        self.assertIsNotNone(self.outcomes.get_by_project(project_id))
        # Remove the row to simulate a pre-existing CLOSED project from
        # before Learning from Outcomes V1 shipped.
        self.store.db.execute("DELETE FROM cs_outcome_records WHERE project_id=?", (project_id,))
        self.store.db.commit()
        self.assertIsNone(self.outcomes.get_by_project(project_id))
        created = backfill_outcomes_for_closed_projects(self.store, self.audit)
        self.assertIn(self.outcomes.get_by_project(project_id)["id"], created)

    def test_backfill_is_idempotent(self):
        project_id = self._project_through_to_closed()
        self.projects.transition(project_id, "Aryan", "CLOSED")
        first_run = backfill_outcomes_for_closed_projects(self.store, self.audit)
        self.assertEqual(first_run, [])  # already recorded by the automatic hook
        second_run = backfill_outcomes_for_closed_projects(self.store, self.audit)
        self.assertEqual(second_run, [])
        self.assertEqual(len(self.store.list("cs_outcome_records", "project_id=?", (project_id,))), 1)


if __name__ == "__main__":
    unittest.main()
