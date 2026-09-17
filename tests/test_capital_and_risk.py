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
