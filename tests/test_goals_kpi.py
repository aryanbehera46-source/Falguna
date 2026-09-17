"""Tests for falguna/goals.py (Goal Engine) and
falguna/command_center.py's kpi_snapshot (KPI Engine) -- Pass B of the TTT
Command Center / CEO Intelligence + Finance / Capital Engine v1 phase.
"""

import tempfile
import unittest
from pathlib import Path

from falguna.audit import AuditLog
from falguna.billing import BillingStore
from falguna.command_center import kpi_snapshot
from falguna.goals import GoalError, GoalStore
from falguna.revenue_hunter import OpportunityStore
from falguna.sales_ops import ClientStore
from falguna.store import StateStore


class GoalsBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = StateStore(self.root / "state.db")
        self.store.migrate()
        self.audit = AuditLog(self.root / "audit.jsonl")
        self.goals = GoalStore(self.store, self.audit)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()


class GoalStoreTests(GoalsBase):
    def test_create_requires_title_target_unit(self):
        with self.assertRaises(GoalError):
            self.goals.create("", 100000.0, "INR", actor="Aryan")
        with self.assertRaises(GoalError):
            self.goals.create("Title", None, "INR", actor="Aryan")
        with self.assertRaises(GoalError):
            self.goals.create("Title", 100000.0, "", actor="Aryan")

    def test_new_goal_starts_active_with_zero_progress(self):
        goal_id = self.goals.create("Reach 1L/month", 100000.0, "INR", actor="Aryan", department="Sales")
        goal = self.goals.get(goal_id)
        self.assertEqual(goal["status"], "ACTIVE")
        self.assertEqual(goal["current_value"], 0.0)
        self.assertEqual(goal["progress"], 0.0)
        self.assertFalse(goal["at_risk"])

    def test_update_progress_records_history_and_recomputes_progress(self):
        goal_id = self.goals.create("Reach 1L/month", 100000.0, "INR", actor="Aryan")
        self.goals.update_progress(goal_id, 25000.0, "Aryan", note="first invoice")
        goal = self.goals.get(goal_id)
        self.assertEqual(goal["progress"], 0.25)
        history = self.goals.progress_history(goal_id)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["to_value"], 25000.0)
        self.assertEqual(history[0]["note"], "first invoice")

    def test_reaching_target_auto_achieves_but_never_auto_unachieves(self):
        goal_id = self.goals.create("Reach 1L/month", 100000.0, "INR", actor="Aryan")
        self.goals.update_progress(goal_id, 100000.0, "Aryan")
        goal = self.goals.get(goal_id)
        self.assertEqual(goal["status"], "ACHIEVED")
        # A later, smaller value never silently un-achieves the goal.
        self.goals.update_progress(goal_id, 90000.0, "Aryan", note="correction")
        goal = self.goals.get(goal_id)
        self.assertEqual(goal["status"], "ACHIEVED")

    def test_set_status_requires_a_known_status(self):
        goal_id = self.goals.create("Reach 1L/month", 100000.0, "INR", actor="Aryan")
        with self.assertRaises(GoalError):
            self.goals.set_status(goal_id, "SOMEWHAT_DONE", "Aryan")
        goal = self.goals.set_status(goal_id, "PAUSED", "Aryan", reason="waiting on client")
        self.assertEqual(goal["status"], "PAUSED")

    def test_at_risk_only_true_for_active_goal_past_a_real_deadline(self):
        goal_id = self.goals.create("Reach 1L/month", 100000.0, "INR", actor="Aryan", deadline="2020-01-01")
        goal = self.goals.get(goal_id)
        self.assertTrue(goal["at_risk"])
        # A goal with no deadline at all is never fabricated as at_risk.
        goal_id2 = self.goals.create("No deadline goal", 5, "clients", actor="Aryan")
        self.assertFalse(self.goals.get(goal_id2)["at_risk"])
        # A paused goal past its deadline is not flagged at_risk (it's an
        # explicit owner decision, not neglect).
        self.goals.set_status(goal_id, "PAUSED", "Aryan")
        self.assertFalse(self.goals.get(goal_id)["at_risk"])

    def test_recommended_actions_reflect_real_remaining_gap(self):
        goal_id = self.goals.create("Reach 10 clients", 10, "clients", actor="Aryan")
        self.goals.update_progress(goal_id, 4, "Aryan")
        recs = self.goals.recommended_actions(goal_id)
        self.assertTrue(any("6" in r and "clients" in r for r in recs))

    def test_recommended_actions_empty_once_achieved(self):
        goal_id = self.goals.create("Reach 5 clients", 5, "clients", actor="Aryan")
        self.goals.update_progress(goal_id, 5, "Aryan")
        self.assertEqual(self.goals.recommended_actions(goal_id), [])

    def test_list_filters_by_status_and_department(self):
        self.goals.create("Sales goal", 1, "deal", actor="Aryan", department="Sales")
        g2 = self.goals.create("Ops goal", 1, "task", actor="Aryan", department="Operations")
        self.goals.set_status(g2, "PAUSED", "Aryan")
        active_only = self.goals.list(status="ACTIVE")
        self.assertEqual(len(active_only), 1)
        sales_only = self.goals.list(department="Sales")
        self.assertEqual(len(sales_only), 1)

    def test_goal_survives_restart(self):
        goal_id = self.goals.create("Reach 1L/month", 100000.0, "INR", actor="Aryan")
        self.goals.update_progress(goal_id, 40000.0, "Aryan")
        self.store.close()

        reopened_store = StateStore(self.root / "state.db")
        reopened_store.migrate()
        reopened_goals = GoalStore(reopened_store, self.audit)
        goal = reopened_goals.get(goal_id)
        self.assertIsNotNone(goal)
        self.assertEqual(goal["current_value"], 40000.0)
        self.assertEqual(len(reopened_goals.progress_history(goal_id)), 1)
        self.store = reopened_store  # let tearDown close this live handle


class KpiSnapshotTests(GoalsBase):
    def setUp(self):
        super().setUp()
        self.clients = ClientStore(self.store, self.audit)
        self.billing = BillingStore(self.store, self.audit)
        self.opportunities = OpportunityStore(self.store, self.audit)

    def test_empty_store_kpis_are_zero_or_explicitly_unavailable_never_fabricated(self):
        kpi = kpi_snapshot(self.store)
        self.assertEqual(kpi["sales"]["opportunities_found"]["value"], 0)
        self.assertIsNone(kpi["sales"]["win_rate"]["value"])  # no closed deals yet -- not zero, not fabricated
        self.assertIsNone(kpi["delivery"]["revision_count"]["value"])  # honestly not tracked
        self.assertIn("not yet tracked", kpi["delivery"]["revision_count"]["source"])
        self.assertIsNone(kpi["finance"]["gross_profit_estimate"]["value"])
        for group in ("sales", "delivery", "workforce", "media", "finance"):
            for metric in kpi[group].values():
                self.assertIn("source", metric)

    def test_win_rate_and_average_deal_value_from_real_opportunities(self):
        won_id = self.opportunities.create({"title": "Won deal", "client_name": "Acme"}, actor="Aryan")
        self.opportunities.mark_won(won_id, "Aryan", final_price=4000.0)
        lost_id = self.opportunities.create({"title": "Lost deal", "client_name": "Beta"}, actor="Aryan")
        self.opportunities.move_stage(lost_id, "Lost", "Aryan")

        kpi = kpi_snapshot(self.store)
        self.assertEqual(kpi["sales"]["win_rate"]["value"], 0.5)
        self.assertEqual(kpi["sales"]["average_deal_value"]["value"], 4000.0)
        self.assertEqual(kpi["finance"]["revenue_won_in_window"]["value"], 4000.0)

    def test_opportunities_outside_the_window_are_excluded(self):
        opp_id = self.opportunities.create({"title": "Old deal", "client_name": "Acme"}, actor="Aryan")
        # Backdate creation far outside a 7-day window.
        self.store.update("rh_opportunities", opp_id, created_at="2020-01-01T00:00:00+00:00")
        kpi = kpi_snapshot(self.store, period_days=7)
        self.assertEqual(kpi["sales"]["opportunities_found"]["value"], 0)

    def test_finance_kpis_reuse_command_center_receivables(self):
        client_id = self.clients.upsert("Acme Co", "Aryan")
        invoice_id = self.billing.create_invoice(client_id, "Aryan", 1000.0)
        self.billing.mark_sent(invoice_id, "Aryan")
        self.billing.record_payment(invoice_id, 300.0, "Aryan", evidence="bank ref 9")
        kpi = kpi_snapshot(self.store)
        self.assertEqual(kpi["finance"]["cash_in_to_date"]["value"], 300.0)
        self.assertEqual(kpi["finance"]["receivables_outstanding"]["value"], 700.0)


if __name__ == "__main__":
    unittest.main()
