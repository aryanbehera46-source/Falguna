"""Tests for falguna/onboarding.py -- Client Onboarding checklist and the
Delivery Brief / Falguna handoff enrichment (Section 9, Pass C)."""

import json
import tempfile
import unittest
from pathlib import Path

from falguna.audit import AuditLog
from falguna.lifecycle import LifecycleOrchestrator
from falguna.onboarding import (
    DeliveryBriefService,
    ONBOARDING_ITEM_TYPES,
    OnboardingError,
    OnboardingStore,
)
from falguna.revenue_hunter import ActiveJobStore, OpportunityStore, ProposalStore, QualificationStore
from falguna.opportunity_agent import build_qualification_engine
from falguna.sales_ops import ClosingService, SalesPolicyStore
from falguna.store import StateStore
from falguna.ttt_hq import NeedsAryanQueue


class OnboardingTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = StateStore(root / "state.db")
        self.store.migrate()
        self.audit = AuditLog(root / "audit.jsonl")
        self.orchestrator = LifecycleOrchestrator(self.store, self.audit)
        self.needs_aryan = NeedsAryanQueue(self.store, self.audit)
        self.opportunities = OpportunityStore(self.store, self.audit, orchestrator=self.orchestrator)
        self.onboarding = OnboardingStore(self.store, self.audit)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _opportunity(self):
        return self.opportunities.create({"title": "Build a CRM dashboard", "description": "d"}, actor="Aryan")


class InitChecklistTests(OnboardingTestBase):
    def test_unknown_opportunity_raises(self):
        with self.assertRaises(OnboardingError):
            self.onboarding.init_checklist("does-not-exist")

    def test_creates_one_row_per_item_type(self):
        opp_id = self._opportunity()
        items = self.onboarding.init_checklist(opp_id)
        self.assertEqual(len(items), len(ONBOARDING_ITEM_TYPES))
        self.assertEqual({i["item_type"] for i in items}, set(ONBOARDING_ITEM_TYPES))
        self.assertTrue(all(i["status"] == "REQUIRED" for i in items))

    def test_is_idempotent_and_preserves_progress(self):
        opp_id = self._opportunity()
        self.onboarding.init_checklist(opp_id)
        self.onboarding.set_item(opp_id, "contact_information", "RECEIVED", value_text="client@example.com")
        self.onboarding.init_checklist(opp_id)  # called again
        items = self.onboarding.list_for_opportunity(opp_id)
        self.assertEqual(len(items), len(ONBOARDING_ITEM_TYPES))  # no duplicates
        contact = next(i for i in items if i["item_type"] == "contact_information")
        self.assertEqual(contact["status"], "RECEIVED")  # not reset

    def test_credentials_access_item_is_flagged_sensitive(self):
        opp_id = self._opportunity()
        items = self.onboarding.init_checklist(opp_id)
        creds = next(i for i in items if i["item_type"] == "credentials_access")
        self.assertTrue(creds["sensitive"])


class SetItemTests(OnboardingTestBase):
    def test_unknown_opportunity_raises(self):
        with self.assertRaises(OnboardingError):
            self.onboarding.set_item("does-not-exist", "requirements", "RECEIVED")

    def test_unknown_item_type_raises(self):
        opp_id = self._opportunity()
        with self.assertRaises(OnboardingError):
            self.onboarding.set_item(opp_id, "not_a_real_item_type", "RECEIVED")

    def test_unknown_status_raises(self):
        opp_id = self._opportunity()
        with self.assertRaises(OnboardingError):
            self.onboarding.set_item(opp_id, "requirements", "NOT_A_REAL_STATUS")

    def test_lazily_creates_item_if_no_checklist_was_initialized(self):
        opp_id = self._opportunity()
        item = self.onboarding.set_item(opp_id, "requirements", "RECEIVED", value_text="See attached spec")
        self.assertEqual(item["status"], "RECEIVED")
        self.assertEqual(item["value_text"], "See attached spec")

    def test_updates_existing_item_rather_than_duplicating(self):
        opp_id = self._opportunity()
        self.onboarding.init_checklist(opp_id)
        self.onboarding.set_item(opp_id, "hosting", "RECEIVED", value_text="AWS us-east-1")
        self.onboarding.set_item(opp_id, "hosting", "RECEIVED", value_text="AWS us-east-1, updated")
        items = [i for i in self.onboarding.list_for_opportunity(opp_id) if i["item_type"] == "hosting"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["value_text"], "AWS us-east-1, updated")

    def test_credentials_access_rejects_a_raw_value_text(self):
        opp_id = self._opportunity()
        with self.assertRaises(OnboardingError):
            self.onboarding.set_item(opp_id, "credentials_access", "RECEIVED", value_text="hunter2")

    def test_credentials_access_accepts_a_reference_via_notes(self):
        opp_id = self._opportunity()
        item = self.onboarding.set_item(opp_id, "credentials_access", "RECEIVED", notes="Shared via client's 1Password vault")
        self.assertIsNone(item["value_text"])
        self.assertEqual(item["notes"], "Shared via client's 1Password vault")

    def test_audit_log_never_contains_value_text(self):
        opp_id = self._opportunity()
        self.onboarding.set_item(opp_id, "requirements", "RECEIVED", value_text="a very specific secret-looking string XYZ123")
        content = Path(self.audit.path).read_text()
        self.assertNotIn("XYZ123", content)


class CompletionSummaryTests(OnboardingTestBase):
    def test_empty_checklist_is_not_complete(self):
        opp_id = self._opportunity()
        summary = self.onboarding.completion_summary(opp_id)
        self.assertFalse(summary["complete"])
        self.assertEqual(summary["total"], 0)

    def test_all_received_is_complete(self):
        opp_id = self._opportunity()
        self.onboarding.init_checklist(opp_id)
        for item_type in ONBOARDING_ITEM_TYPES:
            if item_type == "credentials_access":
                self.onboarding.set_item(opp_id, item_type, "RECEIVED", notes="ref")
            else:
                self.onboarding.set_item(opp_id, item_type, "RECEIVED", value_text="x")
        summary = self.onboarding.completion_summary(opp_id)
        self.assertTrue(summary["complete"])
        self.assertEqual(summary["received"], len(ONBOARDING_ITEM_TYPES))

    def test_partial_completion_is_not_complete(self):
        opp_id = self._opportunity()
        self.onboarding.init_checklist(opp_id)
        self.onboarding.set_item(opp_id, "requirements", "RECEIVED", value_text="x")
        self.onboarding.set_item(opp_id, "hosting", "BLOCKED", notes="waiting on client IT")
        summary = self.onboarding.completion_summary(opp_id)
        self.assertFalse(summary["complete"])
        self.assertEqual(summary["blocked"], 1)


class DeliveryBriefServiceTestBase(OnboardingTestBase):
    def setUp(self):
        super().setUp()
        self.brief = DeliveryBriefService(self.store, self.audit)
        self.policy = SalesPolicyStore(self.store)
        self.policy.save({})  # commercial safety default: configure so close() executes immediately
        self.closing = ClosingService(self.store, self.audit, orchestrator=self.orchestrator, needs_aryan=self.needs_aryan)

    def _won_opportunity(self):
        opp_id = self._opportunity()
        result = self.closing.close(
            opp_id, "Aryan", client_name="Acme Corp", final_price=3000, currency="USD",
            final_scope="Build a CRM dashboard with reporting", deadline="2026-12-01",
        )
        return opp_id, result


class DeliveryBriefBuildTests(DeliveryBriefServiceTestBase):
    def test_unknown_opportunity_raises(self):
        with self.assertRaises(OnboardingError):
            self.brief.build("does-not-exist")

    def test_build_includes_real_closing_data(self):
        opp_id, result = self._won_opportunity()
        brief = self.brief.build(opp_id)
        self.assertEqual(brief["client_name"], "Acme Corp")
        self.assertEqual(brief["final_price"], 3000)
        self.assertEqual(brief["deadline"], "2026-12-01")
        self.assertEqual(brief["final_scope"], "Build a CRM dashboard with reporting")

    def test_build_before_closing_never_fabricates_client_or_price(self):
        opp_id = self._opportunity()
        brief = self.brief.build(opp_id)
        self.assertIsNone(brief["client_id"])
        self.assertIsNone(brief["final_price"])

    def test_sensitive_onboarding_item_is_redacted_in_brief(self):
        opp_id, _ = self._won_opportunity()
        self.onboarding.init_checklist(opp_id)
        self.onboarding.set_item(opp_id, "credentials_access", "RECEIVED", notes="Shared via 1Password")
        brief = self.brief.build(opp_id)
        creds_item = next(i for i in brief["onboarding_items"] if i["item_type"] == "credentials_access")
        self.assertIn("on file", creds_item["value"])
        self.assertNotIn("1Password", creds_item["value"])  # the pointer stays in notes, not baked into "value"

    def test_technical_constraints_pulled_from_real_onboarding_items(self):
        opp_id, _ = self._won_opportunity()
        self.onboarding.init_checklist(opp_id)
        self.onboarding.set_item(opp_id, "hosting", "RECEIVED", value_text="AWS us-east-1")
        self.onboarding.set_item(opp_id, "apis", "RECEIVED", value_text="Stripe, SendGrid")
        brief = self.brief.build(opp_id)
        self.assertIn("AWS us-east-1", brief["technical_constraints"])
        self.assertIn("Stripe, SendGrid", brief["technical_constraints"])


class EnrichActiveJobPayloadTests(DeliveryBriefServiceTestBase):
    def _active_job(self, opp_id):
        jobs = self.store.list("rh_active_jobs", "opportunity_id=?", (opp_id,))
        return jobs[-1]["id"]

    def test_unknown_active_job_raises(self):
        with self.assertRaises(OnboardingError):
            self.brief.enrich_active_job_payload("does-not-exist")

    def test_enrichment_preserves_base_requirement(self):
        opp_id, _ = self._won_opportunity()
        job_id = self._active_job(opp_id)
        original_payload = json.loads(self.store.get("rh_active_jobs", job_id)["job_payload_json"])
        original_requirement = original_payload["requirement"]

        self.onboarding.init_checklist(opp_id)
        self.onboarding.set_item(opp_id, "hosting", "RECEIVED", value_text="AWS us-east-1")
        enriched = self.brief.enrich_active_job_payload(job_id)

        self.assertEqual(enriched["base_requirement"], original_requirement)
        self.assertIn(original_requirement, enriched["requirement"])
        self.assertIn("AWS us-east-1", enriched["requirement"])

    def test_enrichment_is_idempotent_and_does_not_duplicate_sections(self):
        opp_id, _ = self._won_opportunity()
        job_id = self._active_job(opp_id)
        self.onboarding.init_checklist(opp_id)
        self.onboarding.set_item(opp_id, "hosting", "RECEIVED", value_text="AWS us-east-1")

        self.brief.enrich_active_job_payload(job_id)
        second = self.brief.enrich_active_job_payload(job_id)

        self.assertEqual(second["requirement"].count("AWS us-east-1"), 1)
        self.assertEqual(second["requirement"].count("Additional context from Client Onboarding"), 1)

    def test_enrichment_reflects_updated_onboarding_state_on_rerun(self):
        opp_id, _ = self._won_opportunity()
        job_id = self._active_job(opp_id)
        self.onboarding.init_checklist(opp_id)
        self.onboarding.set_item(opp_id, "hosting", "RECEIVED", value_text="AWS us-east-1")
        self.brief.enrich_active_job_payload(job_id)

        self.onboarding.set_item(opp_id, "hosting", "RECEIVED", value_text="GCP us-central1")
        second = self.brief.enrich_active_job_payload(job_id)

        self.assertNotIn("AWS us-east-1", second["requirement"])
        self.assertIn("GCP us-central1", second["requirement"])

    def test_persisted_to_the_active_job_row(self):
        opp_id, _ = self._won_opportunity()
        job_id = self._active_job(opp_id)
        self.onboarding.init_checklist(opp_id)
        self.onboarding.set_item(opp_id, "hosting", "RECEIVED", value_text="AWS us-east-1")
        self.brief.enrich_active_job_payload(job_id)

        reloaded = json.loads(self.store.get("rh_active_jobs", job_id)["job_payload_json"])
        self.assertIn("AWS us-east-1", reloaded["requirement"])


if __name__ == "__main__":
    unittest.main()
