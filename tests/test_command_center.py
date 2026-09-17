"""Tests for falguna/command_center.py -- the TTT Command Center snapshot
aggregation and the persistent CEO Brief generator (Pass A of the TTT
Command Center / CEO Intelligence + Finance / Capital Engine v1 phase).

Every assertion here checks that a number in the snapshot or a brief is
traceable to a real row this test itself created -- never a value that
"looks right" -- consistent with how the rest of this codebase is tested
(see tests/test_workforce.py, tests/test_billing.py).
"""

import json
import tempfile
import unittest
from pathlib import Path

from falguna.audit import AuditLog
from falguna.billing import BillingStore
from falguna.command_center import CEOBriefStore, command_center_snapshot
from falguna.revenue_hunter import OpportunityStore
from falguna.sales_ops import ClientStore
from falguna.store import StateStore
from falguna.ttt_hq import NeedsAryanQueue
from falguna.workforce import WorkerResult, WorkforceOrchestrator, WorkforceTaskStore, WorkforceWorker


class AlwaysFailWorker(WorkforceWorker):
    name = "always_fail_worker"

    def supports(self, task_type):
        return task_type == "fail_type"

    def execute(self, task):
        return WorkerResult(status="FAILED", next_action="simulated failure", evidence={"attempted": True})


class CommandCenterBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = StateStore(self.root / "state.db")
        self.store.migrate()
        self.audit = AuditLog(self.root / "audit.jsonl")
        self.clients = ClientStore(self.store, self.audit)
        self.billing = BillingStore(self.store, self.audit)
        self.opportunities = OpportunityStore(self.store, self.audit)
        self.needs_aryan = NeedsAryanQueue(self.store, self.audit)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()


class SnapshotTests(CommandCenterBase):
    def test_empty_store_has_no_fabricated_numbers(self):
        snap = command_center_snapshot(self.store)
        self.assertEqual(snap["revenue"]["won_revenue_lifetime"], 0)
        self.assertEqual(snap["cash"]["cash_in_to_date"], 0)
        self.assertEqual(snap["receivables"]["outstanding_total"], 0)
        self.assertEqual(snap["pipeline"]["active_count"], 0)
        self.assertEqual(snap["clients"]["total"], 0)
        self.assertEqual(snap["risk_signals"], [])
        # Every top-level section names exactly where its numbers came from.
        for key in ("revenue", "cash", "receivables", "pipeline", "clients", "delivery", "workforce", "media", "needs_aryan"):
            self.assertIn("source", snap[key])

    def test_cash_in_to_date_is_evidence_backed_payments_only_not_invoice_face_value(self):
        client_id = self.clients.upsert("Acme Co", "Aryan")
        invoice_id = self.billing.create_invoice(client_id, "Aryan", 1000.0)
        self.billing.mark_sent(invoice_id, "Aryan")
        self.billing.record_payment(invoice_id, 400.0, "Aryan", evidence="bank transfer ref 123")

        snap = command_center_snapshot(self.store)
        self.assertEqual(snap["cash"]["cash_in_to_date"], 400.0)  # not the full 1000 face value
        self.assertEqual(snap["receivables"]["outstanding_total"], 600.0)
        self.assertIn("Outflows are not yet", snap["cash"]["note"])  # never presented as net cash

    def test_overdue_invoice_surfaces_in_receivables_and_risk_signals(self):
        client_id = self.clients.upsert("Acme Co", "Aryan")
        invoice_id = self.billing.create_invoice(client_id, "Aryan", 500.0, due_date="2020-01-01")
        self.billing.mark_sent(invoice_id, "Aryan")
        self.billing.check_overdue(invoice_id)  # real, evidence-based recheck against today's date

        snap = command_center_snapshot(self.store)
        self.assertEqual(snap["receivables"]["overdue_count"], 1)
        self.assertEqual(snap["receivables"]["overdue_total"], 500.0)
        risk_categories = [r["category"] for r in snap["risk_signals"]]
        self.assertIn("finance_risk", risk_categories)

    def test_won_opportunity_counts_toward_revenue(self):
        opp_id = self.opportunities.create({"title": "Website rebuild", "client_name": "Acme Co"}, actor="Aryan")
        self.opportunities.mark_won(opp_id, "Aryan", final_price=2500.0)
        snap = command_center_snapshot(self.store)
        self.assertEqual(snap["revenue"]["won_revenue_lifetime"], 2500.0)
        self.assertEqual(snap["revenue"]["won_deals_count"], 1)

    def test_negotiating_opportunity_surfaces_in_pipeline(self):
        opp_id = self.opportunities.create({"title": "Mobile app", "client_name": "Beta Inc"}, actor="Aryan")
        self.opportunities.move_stage(opp_id, "Negotiating", "Aryan")
        snap = command_center_snapshot(self.store)
        self.assertEqual(snap["pipeline"]["negotiating_count"], 1)
        self.assertEqual(snap["pipeline"]["negotiating"][0]["id"], opp_id)

    def test_needs_aryan_pending_count_is_real(self):
        self.needs_aryan.create_item("workforce_action_approval", "Review this", "Decide something", actor="Aryan")
        snap = command_center_snapshot(self.store)
        self.assertEqual(snap["needs_aryan"]["pending_count"], 1)
        risk_categories = [r["category"] for r in snap["risk_signals"]]
        self.assertIn("decision_backlog", risk_categories)

    def test_workforce_attention_tasks_surface_in_snapshot(self):
        orch = WorkforceOrchestrator(self.store, self.audit, needs_aryan=self.needs_aryan)
        orch.register_worker(AlwaysFailWorker())
        tasks = WorkforceTaskStore(self.store, self.audit)
        task_id = tasks.create("media", "Doomed task", "fail_type", actor="Aryan")
        orch.execute(task_id)
        orch.execute(task_id)
        orch.execute(task_id)  # exhausted -> FAILED

        snap = command_center_snapshot(self.store)
        self.assertEqual(snap["workforce"]["needs_attention_count"], 1)
        risk_categories = [r["category"] for r in snap["risk_signals"]]
        self.assertIn("delivery_risk", risk_categories)


class CEOBriefTests(CommandCenterBase):
    def test_first_brief_defaults_to_24h_lookback_when_none_exists(self):
        brief_store = CEOBriefStore(self.store, self.audit)
        self.assertIsNone(brief_store.latest())
        brief = brief_store.generate("Aryan")
        self.assertIsNotNone(brief["period_start"])
        self.assertIsNotNone(brief["period_end"])
        self.assertLess(brief["period_start"], brief["period_end"])

    def test_brief_confirmed_facts_reflect_real_payment_in_window(self):
        client_id = self.clients.upsert("Acme Co", "Aryan")
        invoice_id = self.billing.create_invoice(client_id, "Aryan", 1000.0)
        self.billing.mark_sent(invoice_id, "Aryan")
        self.billing.record_payment(invoice_id, 250.0, "Aryan", evidence="wire ref 42")

        brief_store = CEOBriefStore(self.store, self.audit)
        brief = brief_store.generate("Aryan")
        facts = json.loads(brief["confirmed_facts_json"])
        self.assertEqual(facts["payments_received_count"], 1)
        self.assertEqual(facts["payments_received_total"], 250.0)

    def test_brief_separates_confirmed_facts_estimates_and_recommendations(self):
        client_id = self.clients.upsert("Acme Co", "Aryan")
        invoice_id = self.billing.create_invoice(client_id, "Aryan", 500.0, due_date="2020-01-01")
        self.billing.mark_sent(invoice_id, "Aryan")
        self.billing.check_overdue(invoice_id)

        brief_store = CEOBriefStore(self.store, self.audit)
        brief = brief_store.generate("Aryan")
        facts = json.loads(brief["confirmed_facts_json"])
        estimates = json.loads(brief["estimates_json"])
        recommendations = json.loads(brief["recommendations_json"])
        self.assertIsInstance(facts, dict)
        self.assertIsInstance(estimates, list)
        self.assertIsInstance(recommendations, list)
        self.assertTrue(any("Estimate:" in e for e in estimates))
        self.assertTrue(any("Recommendation:" in r for r in recommendations))
        # A recommendation must never silently read as a confirmed fact.
        for r in recommendations:
            self.assertNotIn(r, facts.values())

    def test_second_brief_period_starts_where_first_ended(self):
        brief_store = CEOBriefStore(self.store, self.audit)
        first = brief_store.generate("Aryan")
        second = brief_store.generate("Aryan")
        self.assertEqual(second["period_start"], first["period_end"])

    def test_latest_and_list_return_persisted_briefs(self):
        brief_store = CEOBriefStore(self.store, self.audit)
        first = brief_store.generate("Aryan")
        second = brief_store.generate("Aryan")
        self.assertEqual(brief_store.latest()["id"], second["id"])
        listed_ids = [b["id"] for b in brief_store.list()]
        self.assertIn(first["id"], listed_ids)
        self.assertIn(second["id"], listed_ids)

    def test_brief_survives_restart(self):
        brief_store = CEOBriefStore(self.store, self.audit)
        brief = brief_store.generate("Aryan")
        self.store.close()

        reopened_store = StateStore(self.root / "state.db")
        reopened_store.migrate()
        reopened_brief_store = CEOBriefStore(reopened_store, self.audit)
        persisted = reopened_brief_store.get(brief["id"])
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted["period_start"], brief["period_start"])
        self.assertEqual(persisted["confirmed_facts_json"], brief["confirmed_facts_json"])
        self.store = reopened_store  # let tearDown close this live handle


if __name__ == "__main__":
    unittest.main()
