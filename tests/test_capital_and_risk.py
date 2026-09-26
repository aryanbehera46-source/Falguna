"""Tests for falguna/capital_and_risk.py -- BudgetStore/budget_status,
recommend_allocation, ReservePolicyStore/allowed_experimental_capital, and
RiskRegisterStore (Pass D of the TTT Command Center / CEO Intelligence +
Finance / Capital Engine v1 phase).
"""

import tempfile
import unittest
from pathlib import Path

from falguna.audit import AuditLog
from falguna.capital_and_risk import (
    BudgetStore, CapitalRiskError, ReservePolicyStore, RiskRegisterStore,
    allowed_experimental_capital, budget_status, recommend_allocation,
)
from falguna.finance_ledger import LedgerStore
from falguna.store import StateStore
from falguna.ttt_hq import NeedsAryanQueue


class CapitalRiskBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = StateStore(self.root / "state.db")
        self.store.migrate()
        self.audit = AuditLog(self.root / "audit.jsonl")
        self.needs_aryan = NeedsAryanQueue(self.store, self.audit)
        self.ledger = LedgerStore(self.store, self.audit)
        self.budgets = BudgetStore(self.store, self.audit)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()


class BudgetTests(CapitalRiskBase):
    def test_create_requires_department_and_positive_budget(self):
        with self.assertRaises(CapitalRiskError):
            self.budgets.create("", 100.0, actor="Aryan")
        with self.assertRaises(CapitalRiskError):
            self.budgets.create("Sales", 0, actor="Aryan")
        with self.assertRaises(CapitalRiskError):
            self.budgets.create("Sales", 100.0, actor="Aryan", limit_kind="MEDIUM")

    def test_spend_to_date_is_computed_live_from_ledger_never_stored(self):
        budget_id = self.budgets.create("Media/Growth", 1000.0, actor="Aryan")
        self.ledger.record("OUTFLOW", "marketing", 300.0, "ad spend", actor="Aryan", business_unit="Media/Growth")
        status = budget_status(self.store, self.budgets.get(budget_id))
        self.assertEqual(status["spend_to_date"], 300.0)
        self.assertEqual(status["remaining_budget"], 700.0)
        self.assertFalse(status["over_limit"])
        self.assertFalse(status["warning"])

    def test_spend_outside_department_is_never_counted(self):
        budget_id = self.budgets.create("Media/Growth", 1000.0, actor="Aryan")
        self.ledger.record("OUTFLOW", "hosting", 500.0, "AWS", actor="Aryan", business_unit="Infrastructure")
        status = budget_status(self.store, self.budgets.get(budget_id))
        self.assertEqual(status["spend_to_date"], 0)

    def test_soft_limit_over_budget_never_escalates(self):
        budget_id = self.budgets.create("Media/Growth", 100.0, actor="Aryan", limit_kind="SOFT")
        self.ledger.record("OUTFLOW", "marketing", 500.0, "ad spend", actor="Aryan", business_unit="Media/Growth")
        status = budget_status(self.store, self.budgets.get(budget_id), needs_aryan=self.needs_aryan)
        self.assertTrue(status["over_limit"])
        self.assertIsNone(status["escalated_needs_aryan_id"])

    def test_hard_limit_over_budget_escalates_exactly_once(self):
        budget_id = self.budgets.create("Media/Growth", 100.0, actor="Aryan", limit_kind="HARD")
        self.ledger.record("OUTFLOW", "marketing", 500.0, "ad spend", actor="Aryan", business_unit="Media/Growth")
        first = budget_status(self.store, self.budgets.get(budget_id), needs_aryan=self.needs_aryan)
        self.assertIsNotNone(first["escalated_needs_aryan_id"])
        second = budget_status(self.store, self.budgets.get(budget_id), needs_aryan=self.needs_aryan)
        self.assertEqual(first["escalated_needs_aryan_id"], second["escalated_needs_aryan_id"])
        pending = self.store.list("needs_aryan_items", "kind=?", ("budget_override",))
        self.assertEqual(len(pending), 1)

    def test_archive_removes_from_default_active_listing(self):
        budget_id = self.budgets.create("Falguna", 500.0, actor="Aryan")
        self.budgets.archive(budget_id, "Aryan")
        self.assertNotIn(budget_id, [b["id"] for b in self.budgets.list("ACTIVE")])
        self.assertIn(budget_id, [b["id"] for b in self.budgets.list("ARCHIVED")])


class CapitalAllocationTests(CapitalRiskBase):
    def test_rejects_non_positive_amount(self):
        with self.assertRaises(CapitalRiskError):
            recommend_allocation(self.store, 0)
        with self.assertRaises(CapitalRiskError):
            recommend_allocation(self.store, -100)

    def test_never_recommends_more_than_available_amount(self):
        from falguna.revenue_hunter import OpportunityStore
        opportunities = OpportunityStore(self.store, self.audit)
        opp_id = opportunities.create({"title": "Deal", "client_name": "Acme"}, actor="Aryan")
        opportunities.move_stage(opp_id, "Negotiating", "Aryan")

        result = recommend_allocation(self.store, 10000.0, actor="Aryan")
        total_recommended = sum(r["amount"] for r in result["recommendations"])
        self.assertLessEqual(round(total_recommended, 2), 10000.0)

    def test_every_recommendation_states_confidence_and_risk_never_a_guaranteed_roi(self):
        result = recommend_allocation(self.store, 5000.0, actor="Aryan")
        for rec in result["recommendations"]:
            self.assertIn("confidence", rec)
            self.assertIn("risk", rec)
            self.assertIn("alternative", rec)
            self.assertNotIn("guaranteed", rec["expected_benefit"].lower())

    def test_never_moves_money_only_returns_recommendations(self):
        before = self.store.list("cc_ledger_entries")
        recommend_allocation(self.store, 5000.0, actor="Aryan")
        after = self.store.list("cc_ledger_entries")
        self.assertEqual(before, after)


class ReservePolicyTests(CapitalRiskBase):
    def test_defaults_are_unconfigured(self):
        policy = ReservePolicyStore(self.store).get()
        self.assertFalse(policy["configured"])

    def test_save_marks_configured_and_persists(self):
        store = ReservePolicyStore(self.store)
        store.save({"tax_reserve_pct": 0.15})
        policy = store.get()
        self.assertTrue(policy["configured"])
        self.assertEqual(policy["tax_reserve_pct"], 0.15)

    def test_save_ignores_unknown_fields(self):
        store = ReservePolicyStore(self.store)
        store.save({"totally_made_up_field": 999})
        self.assertNotIn("totally_made_up_field", store.get())

    def test_save_rejects_non_numeric_value(self):
        store = ReservePolicyStore(self.store)
        with self.assertRaises(CapitalRiskError):
            store.save({"tax_reserve_pct": "fifteen percent"})

    def test_save_rejects_out_of_range_value(self):
        store = ReservePolicyStore(self.store)
        with self.assertRaises(CapitalRiskError):
            store.save({"tax_reserve_pct": 1.5})
        with self.assertRaises(CapitalRiskError):
            store.save({"tax_reserve_pct": -0.1})


class ReservePolicyApprovalGateTests(CapitalRiskBase):
    """Phase 1, Requirement 1: /api/cc/reserve-policy must never write the
    policy directly any more -- a change is proposed, gated through
    NeedsAryanQueue exactly like every other capital-governance decision,
    and only takes effect once approved. Exercises ReservePolicyStore
    directly (the same primitives hq_web.py's route and decision hook call)
    so these tests do not depend on spinning up the HTTP server."""

    def setUp(self):
        super().setUp()
        self.reserve = ReservePolicyStore(self.store, self.audit, needs_aryan=self.needs_aryan)

    def test_propose_change_does_not_write_the_active_policy(self):
        result = self.reserve.propose_change({"tax_reserve_pct": 0.2}, actor="Aryan")
        self.assertEqual(result["status"], "AWAITING_APPROVAL")
        self.assertIsNotNone(result["needs_aryan_id"])
        # The policy is unchanged -- still the default, still unconfigured.
        policy = self.reserve.get()
        self.assertFalse(policy["configured"])
        self.assertEqual(policy["tax_reserve_pct"], 0.0)

    def test_propose_change_creates_exactly_one_pending_needs_aryan_item(self):
        self.reserve.propose_change({"tax_reserve_pct": 0.2}, actor="Aryan")
        pending = self.needs_aryan.list_pending()
        reserve_items = [i for i in pending if i["kind"] == "reserve_policy_change"]
        self.assertEqual(len(reserve_items), 1)
        self.assertEqual(reserve_items[0]["ref_type"], "cc_reserve_policy")

    def test_second_proposal_while_one_pending_is_idempotent_not_duplicated(self):
        first = self.reserve.propose_change({"tax_reserve_pct": 0.2}, actor="Aryan")
        second = self.reserve.propose_change({"tax_reserve_pct": 0.3}, actor="Aryan")
        self.assertEqual(second["needs_aryan_id"], first["needs_aryan_id"])
        self.assertTrue(second["already_pending"])
        pending = [i for i in self.needs_aryan.list_pending() if i["kind"] == "reserve_policy_change"]
        self.assertEqual(len(pending), 1)

    def test_approval_applies_the_proposed_change_exactly_once(self):
        result = self.reserve.propose_change({"tax_reserve_pct": 0.2}, actor="Aryan")
        needs_aryan_id = result["needs_aryan_id"]
        self.needs_aryan.decide(needs_aryan_id, "approve", "Aryan")
        applied = self.reserve.apply_pending_change(needs_aryan_id, "Aryan")
        self.assertEqual(applied["tax_reserve_pct"], 0.2)
        self.assertTrue(applied["configured"])
        # Re-applying the same already-applied item is a safe no-op, not a
        # second write or a second audit entry's worth of duplicated state.
        applied_again = self.reserve.apply_pending_change(needs_aryan_id, "Aryan")
        self.assertEqual(applied_again["tax_reserve_pct"], 0.2)

    def test_rejection_leaves_the_existing_policy_unchanged(self):
        self.reserve.save({"tax_reserve_pct": 0.1})  # an existing, already-configured baseline
        result = self.reserve.propose_change({"tax_reserve_pct": 0.9}, actor="Aryan")
        self.needs_aryan.decide(result["needs_aryan_id"], "reject", "Aryan")
        policy = self.reserve.get()
        self.assertEqual(policy["tax_reserve_pct"], 0.1)
        with self.assertRaises(CapitalRiskError):
            self.reserve.apply_pending_change(result["needs_aryan_id"], "Aryan")

    def test_apply_refuses_an_item_that_is_still_pending(self):
        result = self.reserve.propose_change({"tax_reserve_pct": 0.2}, actor="Aryan")
        with self.assertRaises(CapitalRiskError):
            self.reserve.apply_pending_change(result["needs_aryan_id"], "Aryan")
        # Confirms it truly was never written.
        self.assertEqual(self.reserve.get()["tax_reserve_pct"], 0.0)

    def test_apply_refuses_a_needs_aryan_item_of_the_wrong_ref_type(self):
        other_id = self.needs_aryan.create_item(
            "pricing_decision", "Unrelated approval", "not a reserve-policy change",
            actor="Aryan", ref_type="rh_closing_package", ref_id="some-opportunity",
        )
        self.needs_aryan.decide(other_id, "approve", "Aryan")
        with self.assertRaises(CapitalRiskError):
            self.reserve.apply_pending_change(other_id, "Aryan")

    def test_apply_refuses_an_unknown_needs_aryan_id(self):
        with self.assertRaises(CapitalRiskError):
            self.reserve.apply_pending_change("does-not-exist", "Aryan")

    def test_propose_change_rejects_malformed_updates(self):
        with self.assertRaises(CapitalRiskError):
            self.reserve.propose_change("not-a-dict", actor="Aryan")
        with self.assertRaises(CapitalRiskError):
            self.reserve.propose_change({}, actor="Aryan")
        with self.assertRaises(CapitalRiskError):
            self.reserve.propose_change({"totally_made_up_field": 1}, actor="Aryan")
        with self.assertRaises(CapitalRiskError):
            self.reserve.propose_change({"tax_reserve_pct": "a lot"}, actor="Aryan")
        with self.assertRaises(CapitalRiskError):
            self.reserve.propose_change({"tax_reserve_pct": 2.0}, actor="Aryan")

    def test_propose_change_requires_a_non_empty_actor(self):
        with self.assertRaises(CapitalRiskError):
            self.reserve.propose_change({"tax_reserve_pct": 0.2}, actor="")

    def test_proposal_survives_restart_before_approval(self):
        result = self.reserve.propose_change({"tax_reserve_pct": 0.2}, actor="Aryan")
        self.store.close()
        reopened_store = StateStore(self.root / "state.db")
        reopened_store.migrate()
        reopened_reserve = ReservePolicyStore(reopened_store, self.audit, needs_aryan=NeedsAryanQueue(reopened_store, self.audit))
        item = reopened_store.get("needs_aryan_items", result["needs_aryan_id"])
        self.assertIsNotNone(item)
        self.assertEqual(item["status"], "PENDING")
        self.assertFalse(reopened_reserve.get()["configured"])
        self.store = reopened_store  # let tearDown close this live handle


class ExperimentalCapitalTests(CapitalRiskBase):
    def test_zero_when_nothing_contributed(self):
        result = allowed_experimental_capital(self.store)
        self.assertEqual(result["available"], 0)

    def test_only_owner_contribution_inflows_count_never_client_revenue(self):
        self.ledger.record("INFLOW", "client_revenue", 5000.0, "invoice", actor="Aryan")
        result = allowed_experimental_capital(self.store)
        self.assertEqual(result["available"], 0)  # client_revenue never counts toward this pool

    def test_owner_contribution_counts_and_earmarked_spend_reduces_it(self):
        self.ledger.record("INFLOW", "owner_contribution", 2000.0, "personal transfer", actor="Aryan")
        result = allowed_experimental_capital(self.store)
        self.assertEqual(result["available"], 2000.0)
        self.ledger.record("OUTFLOW", "other", 500.0, "trading lab seed", actor="Aryan", business_unit="experimental_capital")
        result2 = allowed_experimental_capital(self.store)
        self.assertEqual(result2["available"], 1500.0)

    def test_excluded_sources_are_named_explicitly(self):
        result = allowed_experimental_capital(self.store)
        joined = " ".join(result["excluded_sources"]).lower()
        for term in ("client revenue", "tax_reserve", "operating", "emergency"):
            self.assertIn(term, joined)


class RiskRegisterTests(CapitalRiskBase):
    def test_create_validates_category_severity_and_likelihood(self):
        risks = RiskRegisterStore(self.store, self.audit)
        with self.assertRaises(CapitalRiskError):
            risks.create("Bad category", "not_a_real_category", "low", actor="Aryan")
        with self.assertRaises(CapitalRiskError):
            risks.create("Bad severity", "finance_risk", "catastrophic", actor="Aryan")
        with self.assertRaises(CapitalRiskError):
            risks.create("Bad likelihood", "finance_risk", "low", actor="Aryan", likelihood_band="certain")

    def test_low_and_medium_severity_do_not_escalate(self):
        risks = RiskRegisterStore(self.store, self.audit, needs_aryan=self.needs_aryan)
        risk_id = risks.create("Minor vendor delay", "delivery_risk", "low", actor="Aryan")
        self.assertIsNone(risks.get(risk_id)["needs_aryan_id"])

    def test_high_and_critical_severity_escalate_on_creation(self):
        risks = RiskRegisterStore(self.store, self.audit, needs_aryan=self.needs_aryan)
        risk_id = risks.create("Single client concentration", "concentration_risk", "high", actor="Aryan", likelihood_band="possible")
        risk = risks.get(risk_id)
        self.assertIsNotNone(risk["needs_aryan_id"])
        item = self.store.get("needs_aryan_items", risk["needs_aryan_id"])
        self.assertEqual(item["kind"], "risk_escalation")

        risk_id2 = risks.create("Key infra single point of failure", "infrastructure_risk", "critical", actor="Aryan")
        self.assertIsNotNone(risks.get(risk_id2)["needs_aryan_id"])

    def test_update_status_and_mitigation_persist(self):
        risks = RiskRegisterStore(self.store, self.audit)
        risk_id = risks.create("Vendor risk", "delivery_risk", "medium", actor="Aryan")
        risks.update_mitigation(risk_id, "Diversify vendors", "Aryan")
        updated = risks.update_status(risk_id, "MITIGATING", "Aryan", note="in progress")
        self.assertEqual(updated["status"], "MITIGATING")
        self.assertEqual(updated["mitigation"], "Diversify vendors")

    def test_list_filters_by_status_and_category(self):
        risks = RiskRegisterStore(self.store, self.audit)
        r1 = risks.create("Risk A", "finance_risk", "low", actor="Aryan")
        r2 = risks.create("Risk B", "security_risk", "medium", actor="Aryan")
        risks.update_status(r2, "CLOSED", "Aryan")
        open_only = risks.list(status="OPEN")
        self.assertEqual(len(open_only), 1)
        finance_only = risks.list(category="finance_risk")
        self.assertEqual(len(finance_only), 1)

    def test_risk_survives_restart(self):
        risks = RiskRegisterStore(self.store, self.audit)
        risk_id = risks.create("Persisted risk", "revenue_risk", "medium", actor="Aryan")
        self.store.close()
        reopened_store = StateStore(self.root / "state.db")
        reopened_store.migrate()
        reopened_risks = RiskRegisterStore(reopened_store, self.audit)
        risk = reopened_risks.get(risk_id)
        self.assertIsNotNone(risk)
        self.assertEqual(risk["status"], "OPEN")
        self.store = reopened_store  # let tearDown close this live handle


if __name__ == "__main__":
    unittest.main()
