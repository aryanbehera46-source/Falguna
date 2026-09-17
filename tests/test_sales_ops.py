"""Tests for falguna/sales_ops.py -- Negotiation Guardrails, Client records,
and the Closing Agent (Sections 7/8/9 groundwork)."""

import json
import tempfile
import unittest
from pathlib import Path

from falguna.audit import AuditLog
from falguna.lifecycle import LifecycleOrchestrator
from falguna.revenue_hunter import ActiveJobStore, OpportunityStore
from falguna.sales_ops import (
    ClientError,
    ClientStore,
    ClosingError,
    ClosingService,
    DEFAULT_SALES_POLICY,
    NegotiationGuardrails,
    SalesPolicyStore,
    evaluate_terms,
)
from falguna.store import StateStore
from falguna.ttt_hq import NeedsAryanQueue


class SalesOpsTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = StateStore(root / "state.db")
        self.store.migrate()
        self.audit = AuditLog(root / "audit.jsonl")
        self.opportunities = OpportunityStore(self.store, self.audit)
        self.needs_aryan = NeedsAryanQueue(self.store, self.audit)
        self.orchestrator = LifecycleOrchestrator(self.store, self.audit)
        self.active_jobs = ActiveJobStore(self.store, self.audit)
        self.clients = ClientStore(self.store, self.audit)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()


class SalesPolicyStoreTests(SalesOpsTestBase):
    def test_get_returns_defaults_when_nothing_saved(self):
        policy = SalesPolicyStore(self.store).get()
        self.assertEqual(policy, DEFAULT_SALES_POLICY)

    def test_save_persists_and_merges_with_defaults(self):
        store = SalesPolicyStore(self.store)
        store.save({"min_project_price": 500, "max_discount_pct": 10})
        policy = store.get()
        self.assertEqual(policy["min_project_price"], 500)
        self.assertEqual(policy["max_discount_pct"], 10)
        # Untouched defaults are still present.
        self.assertEqual(policy["allowed_currencies"], ["USD"])

    def test_save_ignores_unknown_fields(self):
        store = SalesPolicyStore(self.store)
        store.save({"not_a_real_field": 123})
        policy = store.get()
        self.assertNotIn("not_a_real_field", policy)

    def test_save_is_idempotent_across_repeated_calls(self):
        store = SalesPolicyStore(self.store)
        store.save({"min_project_price": 500})
        store.save({"max_discount_pct": 15})
        policy = store.get()
        self.assertEqual(policy["min_project_price"], 500)
        self.assertEqual(policy["max_discount_pct"], 15)
        self.assertEqual(len(self.store.list("rh_settings", "key=?", (SalesPolicyStore.KEY,))), 1)


class EvaluateTermsTests(unittest.TestCase):
    def setUp(self):
        self.policy = dict(DEFAULT_SALES_POLICY)
        self.policy.update({
            "min_project_price": 1000, "max_discount_pct": 10, "min_upfront_payment_pct": 30,
            "allowed_payment_terms": ["50% upfront, 50% on delivery"], "max_free_revisions": 2,
            "min_timeline_days": 5, "allowed_currencies": ["USD", "EUR"],
        })

    def test_all_terms_within_policy(self):
        result = evaluate_terms(self.policy, price=1500, currency="USD", discount_pct=5,
                                  upfront_payment_pct=50, payment_terms="50% upfront, 50% on delivery",
                                  free_revisions=1, timeline_days=10)
        self.assertTrue(result["within_policy"])
        self.assertEqual(result["violations"], [])

    def test_price_below_minimum_is_a_violation(self):
        result = evaluate_terms(self.policy, price=500)
        self.assertFalse(result["within_policy"])
        self.assertEqual(len(result["violations"]), 1)

    def test_currency_not_allowed_is_a_violation(self):
        result = evaluate_terms(self.policy, currency="GBP")
        self.assertFalse(result["within_policy"])

    def test_discount_above_max_is_a_violation(self):
        result = evaluate_terms(self.policy, discount_pct=25)
        self.assertFalse(result["within_policy"])

    def test_upfront_below_minimum_is_a_violation(self):
        result = evaluate_terms(self.policy, upfront_payment_pct=10)
        self.assertFalse(result["within_policy"])

    def test_payment_terms_not_allowed_is_a_violation(self):
        result = evaluate_terms(self.policy, payment_terms="net_90")
        self.assertFalse(result["within_policy"])

    def test_too_many_free_revisions_is_a_violation(self):
        result = evaluate_terms(self.policy, free_revisions=5)
        self.assertFalse(result["within_policy"])

    def test_timeline_too_short_is_a_violation(self):
        result = evaluate_terms(self.policy, timeline_days=1)
        self.assertFalse(result["within_policy"])

    def test_multiple_violations_are_all_reported_at_once(self):
        result = evaluate_terms(self.policy, price=100, currency="GBP", discount_pct=50)
        self.assertFalse(result["within_policy"])
        self.assertEqual(len(result["violations"]), 3)

    def test_absent_terms_are_never_flagged(self):
        result = evaluate_terms(self.policy)
        self.assertTrue(result["within_policy"])

    def test_unconfigured_policy_limits_never_flag_anything(self):
        result = evaluate_terms(DEFAULT_SALES_POLICY, price=1, currency="XYZ", timeline_days=0)
        # DEFAULT_SALES_POLICY has no min_project_price/min_timeline_days and
        # allows any currency implicitly disabled by empty defaults except
        # allowed_currencies=["USD"], so only currency should flag here.
        self.assertFalse(result["within_policy"])
        self.assertEqual(len(result["violations"]), 1)


class NegotiationGuardrailsTests(SalesOpsTestBase):
    def _opportunity(self):
        return self.opportunities.create({"title": "Build a CRM integration"}, actor="Aryan")

    def test_within_policy_terms_do_not_escalate(self):
        SalesPolicyStore(self.store).save({"min_project_price": 500})
        guardrails = NegotiationGuardrails(self.store, self.audit, needs_aryan=self.needs_aryan)
        opp_id = self._opportunity()
        result = guardrails.evaluate(opp_id, "Aryan", price=1000)
        self.assertTrue(result["within_policy"])
        self.assertIsNone(result["needs_aryan_id"])

    def test_out_of_policy_terms_escalate_to_needs_aryan(self):
        SalesPolicyStore(self.store).save({"min_project_price": 5000})
        guardrails = NegotiationGuardrails(self.store, self.audit, needs_aryan=self.needs_aryan)
        opp_id = self._opportunity()
        before = len(self.store.list("needs_aryan_items"))
        result = guardrails.evaluate(opp_id, "Aryan", price=1000)
        self.assertFalse(result["within_policy"])
        self.assertIsNotNone(result["needs_aryan_id"])
        self.assertEqual(len(self.store.list("needs_aryan_items")), before + 1)

    def test_needs_aryan_item_uses_the_correct_existing_kind(self):
        SalesPolicyStore(self.store).save({"min_project_price": 5000})
        guardrails = NegotiationGuardrails(self.store, self.audit, needs_aryan=self.needs_aryan)
        opp_id = self._opportunity()
        result = guardrails.evaluate(opp_id, "Aryan", price=1000)
        item = self.store.get("needs_aryan_items", result["needs_aryan_id"])
        self.assertEqual(item["kind"], "negotiation_response_approval")

    def test_unknown_opportunity_raises(self):
        guardrails = NegotiationGuardrails(self.store, self.audit, needs_aryan=self.needs_aryan)
        with self.assertRaises(ValueError):
            guardrails.evaluate("does-not-exist", "Aryan", price=1000)

    def test_evaluate_without_needs_aryan_wired_does_not_crash(self):
        SalesPolicyStore(self.store).save({"min_project_price": 5000})
        guardrails = NegotiationGuardrails(self.store, self.audit, needs_aryan=None)
        opp_id = self._opportunity()
        result = guardrails.evaluate(opp_id, "Aryan", price=1000)
        self.assertFalse(result["within_policy"])
        self.assertIsNone(result["needs_aryan_id"])


class ClientStoreTests(SalesOpsTestBase):
    def test_upsert_creates_a_new_client(self):
        client_id = self.clients.upsert("Acme Corp", "Aryan")
        client = self.clients.get(client_id)
        self.assertEqual(client["name"], "Acme Corp")
        self.assertEqual(client["status"], "ACTIVE")
        self.assertEqual(client["total_won_value"], 0.0)

    def test_upsert_is_idempotent_by_exact_name(self):
        first_id = self.clients.upsert("Acme Corp", "Aryan")
        second_id = self.clients.upsert("Acme Corp", "Aryan")
        self.assertEqual(first_id, second_id)
        self.assertEqual(len(self.clients.list()), 1)

    def test_upsert_requires_a_name(self):
        with self.assertRaises(ClientError):
            self.clients.upsert("   ", "Aryan")

    def test_upsert_fills_in_missing_contact_info_without_overwriting_existing(self):
        client_id = self.clients.upsert("Acme Corp", "Aryan", primary_contact="Jane")
        self.clients.upsert("Acme Corp", "Aryan", primary_contact="Bob")
        client = self.clients.get(client_id)
        self.assertEqual(client["primary_contact"], "Jane")

    def test_record_won_value_accumulates(self):
        client_id = self.clients.upsert("Acme Corp", "Aryan")
        self.clients.record_won_value(client_id, 1000, "Aryan")
        self.clients.record_won_value(client_id, 500, "Aryan")
        client = self.clients.get(client_id)
        self.assertEqual(client["total_won_value"], 1500.0)

    def test_record_won_value_unknown_client_raises(self):
        with self.assertRaises(ClientError):
            self.clients.record_won_value("does-not-exist", 100, "Aryan")


class ClosingServiceTests(SalesOpsTestBase):
    """These exercise the real closing mechanics once the Sales Policy has
    been deliberately configured -- see ClosingServiceCommercialSafetyDefaultTests
    below for the unconfigured-by-default gate itself."""

    def _opportunity(self):
        return self.opportunities.create({"title": "Build a CRM integration"}, actor="Aryan")

    def _configured_service(self):
        SalesPolicyStore(self.store).save({})  # deliberately configured, even with permissive defaults
        return ClosingService(self.store, self.audit, orchestrator=self.orchestrator, active_jobs=self.active_jobs, clients=self.clients, needs_aryan=self.needs_aryan)

    def test_close_requires_client_name(self):
        opp_id = self._opportunity()
        with self.assertRaises(ClosingError):
            self._configured_service().close(opp_id, "Aryan", client_name="   ")

    def test_close_unknown_opportunity_raises(self):
        with self.assertRaises(ClosingError):
            self._configured_service().close("does-not-exist", "Aryan", client_name="Acme")

    def test_close_marks_opportunity_won(self):
        opp_id = self._opportunity()
        result = self._configured_service().close(opp_id, "Aryan", client_name="Acme Corp", final_price=2000, currency="USD")
        self.assertEqual(result["status"], "CLOSED")
        opp = self.store.get("rh_opportunities", opp_id)
        self.assertEqual(opp["stage"], "Won")
        self.assertEqual(opp["final_price"], 2000)

    def test_close_creates_the_client_and_credits_won_value(self):
        opp_id = self._opportunity()
        result = self._configured_service().close(opp_id, "Aryan", client_name="Acme Corp", final_price=2000)
        client = self.clients.get(result["client_id"])
        self.assertEqual(client["name"], "Acme Corp")
        self.assertEqual(client["total_won_value"], 2000.0)

    def test_close_creates_an_active_job(self):
        opp_id = self._opportunity()
        result = self._configured_service().close(opp_id, "Aryan", client_name="Acme Corp", final_price=2000)
        job = self.active_jobs.get(result["active_job_id"])
        self.assertIsNotNone(job)
        self.assertEqual(job["opportunity_id"], opp_id)

    def test_close_persists_the_structured_closing_record(self):
        opp_id = self._opportunity()
        result = self._configured_service().close(
            opp_id, "Aryan", client_name="Acme Corp", final_scope="Full CRM build", final_price=2000,
            currency="USD", payment_terms="50/50", milestones=[{"name": "Kickoff", "amount": 1000}],
            deadline="2026-12-01", deliverables="Working CRM integration", acceptance_criteria="Passes UAT",
            communication_channel="email",
        )
        record = self.store.get("rh_closing_records", result["closing_record_id"])
        self.assertEqual(record["final_scope"], "Full CRM build")
        self.assertEqual(record["payment_terms"], "50/50")
        self.assertEqual(json.loads(record["milestones_json"]), [{"name": "Kickoff", "amount": 1000}])

    def test_close_advances_lifecycle_to_onboarding_when_reachable(self):
        opp_id = self._opportunity()
        for state in ["RESEARCHING", "QUALIFIED", "PITCH_READY", "AWAITING_APPROVAL", "APPROVED",
                      "APPLYING", "CONTACTED", "REPLIED", "NEGOTIATING"]:
            self.orchestrator.transition(opp_id, state, actor="Aryan")
        self._configured_service().close(opp_id, "Aryan", client_name="Acme Corp", final_price=2000)
        self.assertEqual(self.orchestrator.current_state(opp_id), "ONBOARDING")

    def test_close_never_raises_even_if_lifecycle_state_cannot_reach_won(self):
        # No lifecycle initialization at all -- try_transition must swallow
        # the illegal jump rather than blow up the whole closing action.
        opp_id = self._opportunity()
        result = self._configured_service().close(opp_id, "Aryan", client_name="Acme Corp", final_price=2000)
        self.assertFalse(result["already_closed"])
        opp = self.store.get("rh_opportunities", opp_id)
        self.assertEqual(opp["stage"], "Won")

    def test_calling_close_twice_is_idempotent(self):
        opp_id = self._opportunity()
        service = self._configured_service()
        first = service.close(opp_id, "Aryan", client_name="Acme Corp", final_price=2000)
        second = service.close(opp_id, "Aryan", client_name="Acme Corp", final_price=2000)
        self.assertFalse(first["already_closed"])
        self.assertTrue(second["already_closed"])
        self.assertEqual(first["closing_record_id"], second["closing_record_id"])
        self.assertEqual(first["active_job_id"], second["active_job_id"])
        self.assertEqual(len(self.store.list("rh_closing_records", "opportunity_id=?", (opp_id,))), 1)
        self.assertEqual(len(self.store.list("rh_active_jobs", "opportunity_id=?", (opp_id,))), 1)

    def test_calling_close_twice_does_not_double_credit_the_client(self):
        opp_id = self._opportunity()
        service = self._configured_service()
        result = service.close(opp_id, "Aryan", client_name="Acme Corp", final_price=2000)
        service.close(opp_id, "Aryan", client_name="Acme Corp", final_price=2000)
        client = self.clients.get(result["client_id"])
        self.assertEqual(client["total_won_value"], 2000.0)

    def test_close_without_final_price_does_not_crash_and_credits_nothing(self):
        opp_id = self._opportunity()
        result = self._configured_service().close(opp_id, "Aryan", client_name="Acme Corp")
        client = self.clients.get(result["client_id"])
        self.assertEqual(client["total_won_value"], 0.0)


class ClosingServiceCommercialSafetyDefaultTests(SalesOpsTestBase):
    """Regression tests for the commercial safety default: an unconfigured
    Sales Policy must never be read as unlimited closing authority."""

    def _opportunity(self):
        return self.opportunities.create({"title": "Build a CRM integration"}, actor="Aryan")

    def _service(self):
        return ClosingService(self.store, self.audit, orchestrator=self.orchestrator, active_jobs=self.active_jobs, clients=self.clients, needs_aryan=self.needs_aryan)

    def test_policy_starts_unconfigured(self):
        self.assertFalse(SalesPolicyStore(self.store).get()["configured"])

    def test_close_with_unconfigured_policy_does_not_execute_immediately(self):
        opp_id = self._opportunity()
        result = self._service().close(opp_id, "Aryan", client_name="Acme Corp", final_price=2000)
        self.assertEqual(result["status"], "AWAITING_APPROVAL")
        self.assertIsNotNone(result["needs_aryan_id"])

    def test_close_with_unconfigured_policy_touches_nothing(self):
        opp_id = self._opportunity()
        self._service().close(opp_id, "Aryan", client_name="Acme Corp", final_price=2000)
        opp = self.store.get("rh_opportunities", opp_id)
        self.assertEqual(opp["stage"], "New")  # never moved to Won
        self.assertEqual(len(self.clients.list()), 0)  # no client created
        self.assertEqual(len(self.active_jobs.list()), 0)  # no active job created
        self.assertEqual(len(self.store.list("rh_closing_records")), 0)  # no closing record

    def test_close_with_unconfigured_policy_creates_a_pricing_decision_needs_aryan_item(self):
        opp_id = self._opportunity()
        result = self._service().close(opp_id, "Aryan", client_name="Acme Corp", final_price=2000)
        item = self.store.get("needs_aryan_items", result["needs_aryan_id"])
        self.assertEqual(item["kind"], "pricing_decision")
        self.assertEqual(item["ref_type"], "rh_closing_package")
        self.assertEqual(item["ref_id"], opp_id)
        self.assertIsNotNone(item["payload_json"])

    def test_calling_close_twice_while_unconfigured_does_not_duplicate_the_needs_aryan_item(self):
        opp_id = self._opportunity()
        service = self._service()
        first = service.close(opp_id, "Aryan", client_name="Acme Corp", final_price=2000)
        second = service.close(opp_id, "Aryan", client_name="Acme Corp", final_price=2000)
        self.assertEqual(first["needs_aryan_id"], second["needs_aryan_id"])
        self.assertEqual(len(self.store.list("needs_aryan_items", "ref_type=?", ("rh_closing_package",))), 1)

    def test_approving_the_closing_package_and_finalizing_executes_the_real_close(self):
        opp_id = self._opportunity()
        service = self._service()
        prepared = service.close(opp_id, "Aryan", client_name="Acme Corp", final_price=2000, currency="USD")
        self.needs_aryan.decide(prepared["needs_aryan_id"], "approve", "Aryan", note="approved")
        result = service.finalize_pending_closing(prepared["needs_aryan_id"], "Aryan")
        self.assertEqual(result["status"], "CLOSED")
        self.assertFalse(result["already_closed"])
        opp = self.store.get("rh_opportunities", opp_id)
        self.assertEqual(opp["stage"], "Won")
        client = self.clients.get(result["client_id"])
        self.assertEqual(client["total_won_value"], 2000.0)
        job = self.active_jobs.get(result["active_job_id"])
        self.assertIsNotNone(job)

    def test_finalize_without_approval_raises(self):
        opp_id = self._opportunity()
        service = self._service()
        prepared = service.close(opp_id, "Aryan", client_name="Acme Corp", final_price=2000)
        with self.assertRaises(ClosingError):
            service.finalize_pending_closing(prepared["needs_aryan_id"], "Aryan")

    def test_finalize_unknown_item_raises(self):
        with self.assertRaises(ClosingError):
            self._service().finalize_pending_closing("does-not-exist", "Aryan")

    def test_finalize_on_a_non_closing_package_item_raises(self):
        item_id = self.needs_aryan.create_item("proposal_approval", "Some other item", "review it", actor="Aryan")
        self.needs_aryan.decide(item_id, "approve", "Aryan")
        with self.assertRaises(ClosingError):
            self._service().finalize_pending_closing(item_id, "Aryan")

    def test_finalizing_twice_is_idempotent(self):
        opp_id = self._opportunity()
        service = self._service()
        prepared = service.close(opp_id, "Aryan", client_name="Acme Corp", final_price=2000)
        self.needs_aryan.decide(prepared["needs_aryan_id"], "approve", "Aryan")
        first = service.finalize_pending_closing(prepared["needs_aryan_id"], "Aryan")
        second = service.finalize_pending_closing(prepared["needs_aryan_id"], "Aryan")
        self.assertFalse(first["already_closed"])
        self.assertTrue(second["already_closed"])
        self.assertEqual(len(self.store.list("rh_closing_records", "opportunity_id=?", (opp_id,))), 1)

    def test_once_policy_is_configured_close_executes_immediately_again(self):
        opp_id = self._opportunity()
        SalesPolicyStore(self.store).save({"min_project_price": 100})
        result = self._service().close(opp_id, "Aryan", client_name="Acme Corp", final_price=2000)
        self.assertEqual(result["status"], "CLOSED")
        opp = self.store.get("rh_opportunities", opp_id)
        self.assertEqual(opp["stage"], "Won")

    def test_close_without_needs_aryan_wired_still_prepares_a_package_without_crashing(self):
        opp_id = self._opportunity()
        service = ClosingService(self.store, self.audit, orchestrator=self.orchestrator, active_jobs=self.active_jobs, clients=self.clients, needs_aryan=None)
        result = service.close(opp_id, "Aryan", client_name="Acme Corp", final_price=2000)
        self.assertEqual(result["status"], "AWAITING_APPROVAL")
        self.assertIsNone(result["needs_aryan_id"])
        opp = self.store.get("rh_opportunities", opp_id)
        self.assertEqual(opp["stage"], "New")


if __name__ == "__main__":
    unittest.main()
