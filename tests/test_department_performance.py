"""Tests for falguna/department_performance.py -- department_performance()
and ai_workforce_performance() (Sections 13-14 of the TTT Command Center /
CEO Intelligence + Finance / Capital Engine v1 phase).

Covers: honest empty-store output (0/None, never fabricated), real-data
correctness for each department's output/failures/cost, ledger-based cost
attribution by business_unit, worker-grouping correctness in the AI
workforce breakdown, and the None-vs-zero distinction for data that has no
model backing it yet.
"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from falguna.audit import AuditLog
from falguna.department_performance import ai_workforce_performance, department_performance
from falguna.finance_ledger import LedgerStore
from falguna.revenue_hunter import OpportunityStore
from falguna.runtime import open_control_plane
from falguna.store import StateStore
from falguna.ttt_hq import NeedsAryanQueue
from falguna.workforce import WorkerResult, WorkforceOrchestrator, WorkforceTaskStore, WorkforceWorker


class EchoWorker(WorkforceWorker):
    name = "echo_worker"

    def supports(self, task_type):
        return task_type == "echo"

    def execute(self, task):
        return WorkerResult(status="COMPLETED", result={"echo": task["objective"]}, evidence={"echoed": True}, execution_method="API")


class AlwaysFailWorker(WorkforceWorker):
    name = "always_fail_worker"

    def supports(self, task_type):
        return task_type == "fail_type"

    def execute(self, task):
        return WorkerResult(status="FAILED", next_action="simulated failure", evidence={"attempted": True, "reason": "simulated failure"})


class DepartmentPerformanceBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = StateStore(self.root / "state.db")
        self.store.migrate()
        self.audit = AuditLog(self.root / "audit.jsonl")
        self.needs_aryan = NeedsAryanQueue(self.store, self.audit)
        self.opportunities = OpportunityStore(self.store, self.audit)
        self.ledger = LedgerStore(self.store, self.audit)
        self.tasks = WorkforceTaskStore(self.store, self.audit)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()


class DepartmentPerformanceEmptyStoreTests(DepartmentPerformanceBase):
    def test_empty_store_never_fabricates_a_value(self):
        result = department_performance(self.store)
        self.assertEqual(result["sales"]["output"]["value"], 0)
        self.assertEqual(result["sales"]["failures"]["value"], 0)
        self.assertIsNone(result["sales"]["blocked_work"]["value"])
        self.assertEqual(result["sales"]["cost"]["value"], 0)
        self.assertEqual(result["digital_workforce"]["cost"]["value"], 0)
        self.assertEqual(result["falguna_engineering"]["output"]["value"], 0)
        self.assertIsNone(result["falguna_engineering"]["business_impact"]["value"])
        for dept in ("sales", "delivery", "digital_workforce", "media_growth", "falguna_engineering"):
            for metric in result[dept].values():
                self.assertIn("source", metric)

    def test_empty_store_ai_workforce_performance_has_no_workers(self):
        result = ai_workforce_performance(self.store)
        self.assertEqual(result["by_worker"], {})
        self.assertIn("source", result)


class DepartmentPerformanceRealDataTests(DepartmentPerformanceBase):
    def test_sales_output_failures_cost_and_business_impact(self):
        won_id = self.opportunities.create({"title": "Acme deal", "client_name": "Acme Co"}, actor="Aryan", source="test")
        self.opportunities.mark_won(won_id, actor="Aryan", final_price=5000.0, note="closed")
        lost_id = self.opportunities.create({"title": "Doomed deal", "client_name": "Doomed Co"}, actor="Aryan", source="test")
        self.opportunities.mark_lost(lost_id, actor="Aryan", reason="budget cut")
        self.ledger.record("OUTFLOW", "marketing", 200.0, evidence="ad spend receipt", actor="Aryan", business_unit="Sales")

        result = department_performance(self.store)
        sales = result["sales"]
        self.assertEqual(sales["output"]["value"], 2)  # both opportunities created in window
        self.assertEqual(sales["failures"]["value"], 1)
        self.assertEqual(sales["cost"]["value"], 200.0)
        self.assertEqual(sales["business_impact"]["value"], 5000.0)

    def test_ledger_cost_is_scoped_to_the_matching_business_unit_only(self):
        self.ledger.record("OUTFLOW", "contractor", 500.0, evidence="contractor invoice", actor="Aryan", business_unit="Digital Workforce")
        self.ledger.record("OUTFLOW", "marketing", 75.0, evidence="ad spend", actor="Aryan", business_unit="Media/Growth")

        result = department_performance(self.store)
        self.assertEqual(result["digital_workforce"]["cost"]["value"], 500.0)
        self.assertEqual(result["media_growth"]["cost"]["value"], 75.0)
        self.assertEqual(result["sales"]["cost"]["value"], 0)
        self.assertEqual(result["delivery"]["cost"]["value"], 0)

    def test_void_ledger_entries_are_excluded_from_department_cost(self):
        entry_id = self.ledger.record("OUTFLOW", "software_tooling", 300.0, evidence="subscription", actor="Aryan", business_unit="Sales")
        self.ledger.void(entry_id, actor="Aryan", reason="duplicate charge")

        result = department_performance(self.store)
        self.assertEqual(result["sales"]["cost"]["value"], 0)

    def test_digital_workforce_output_and_failures_come_from_task_events(self):
        orch = WorkforceOrchestrator(self.store, self.audit, needs_aryan=self.needs_aryan)
        orch.register_worker(EchoWorker())
        orch.register_worker(AlwaysFailWorker())

        ok_id = self.tasks.create("media", "Say hi", "echo", actor="Aryan")
        orch.execute(ok_id)

        fail_id = self.tasks.create("media", "Doomed task", "fail_type", actor="Aryan")
        # A single execute() call records exactly one FAILED transition
        # event, whether or not the task is later retried -- kpi_snapshot
        # (which this reuses) counts FAILED *events* in the window, not
        # distinct terminal tasks, so one call here means exactly one.
        orch.execute(fail_id)

        result = department_performance(self.store)
        self.assertEqual(result["digital_workforce"]["output"]["value"], 1)
        self.assertEqual(result["digital_workforce"]["failures"]["value"], 1)

    def test_falguna_engineering_counts_done_failed_and_in_flight_runs_without_crashing(self):
        # Regression: the original blocked_work expression used
        # `("DONE_CANDIDATE",) | _TERMINAL_RUN_FAILURE_STATUSES` (a tuple
        # unioned with a set), which raises TypeError as soon as any run
        # exists in the window. This seeds one run of each shape to prove
        # the fixed version both runs and counts correctly.
        repo = self.root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "falguna@test.invalid"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Falguna Test"], check=True)
        (repo / "README.md").write_text("seed\n")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "seed"], check=True, capture_output=True)

        from falguna.models import RunPolicy
        control, store = open_control_plane(repo)
        try:
            mission = control.create_mission("Seed mission", "Seed requirement", repo, RunPolicy())
            requirement_id = store.list("requirements", "mission_id=?", (mission["mission_id"],))[0]["id"]
            task_id = store.list("tasks", "requirement_id=?", (requirement_id,))[0]["id"]

            now = "2026-09-17T12:00:00+00:00"
            run_done = store.create("runs", {"task_id": task_id, "status": "DONE_CANDIDATE", "attempt": 1, "worker": "w", "model": "m", "worktree": None, "head_sha": None, "error": None, "created_at": now, "updated_at": now})
            store.create("runs", {"task_id": task_id, "status": "FAILED", "attempt": 1, "worker": "w", "model": "m", "worktree": None, "head_sha": None, "error": "boom", "created_at": now, "updated_at": now})
            store.create("runs", {"task_id": task_id, "status": "RUNNING", "attempt": 1, "worker": "w", "model": "m", "worktree": None, "head_sha": None, "error": None, "created_at": now, "updated_at": now})
            store.create("model_calls", {"run_id": run_done, "provider": "anthropic", "model": "m", "purpose": "code", "input_tokens": 100, "output_tokens": 50, "cost_usd": 0.05, "metadata_json": "{}", "created_at": now})
            store.create("cost_events", {"run_id": run_done, "category": "compute", "amount_usd": 0.10, "metadata_json": "{}", "created_at": now})

            result = department_performance(store)
            eng = result["falguna_engineering"]
            self.assertEqual(eng["output"]["value"], 1)
            self.assertEqual(eng["failures"]["value"], 1)
            self.assertEqual(eng["blocked_work"]["value"], 1)
            self.assertEqual(eng["cost"]["value"], 0.15)
        finally:
            store.close()


class AiWorkforcePerformanceTests(DepartmentPerformanceBase):
    def test_success_and_failure_rates_are_computed_per_worker(self):
        orch = WorkforceOrchestrator(self.store, self.audit, needs_aryan=self.needs_aryan)
        orch.register_worker(EchoWorker())
        orch.register_worker(AlwaysFailWorker())

        ok_id = self.tasks.create("media", "Say hi", "echo", actor="Aryan")
        orch.execute(ok_id)

        fail_id = self.tasks.create("media", "Doomed task", "fail_type", actor="Aryan")
        for _ in range(3):
            orch.execute(fail_id)

        result = ai_workforce_performance(self.store)
        by_worker = result["by_worker"]
        self.assertIn("echo_worker", by_worker)
        self.assertIn("always_fail_worker", by_worker)
        self.assertEqual(by_worker["echo_worker"]["success_rate"], 1.0)
        self.assertEqual(by_worker["echo_worker"]["failure_rate"], 0.0)
        self.assertEqual(by_worker["always_fail_worker"]["failure_rate"], 1.0)
        self.assertGreater(by_worker["always_fail_worker"]["retry_rate"], 0.0)

    def test_average_duration_is_none_without_executing_and_terminal_events(self):
        # A task that never leaves CREATED has no EXECUTING->terminal pair,
        # so duration must stay None -- never a fabricated 0.
        task_id = self.tasks.create("media", "Unstarted", "echo", actor="Aryan")
        self.tasks.transition(task_id, "PLANNING", "Aryan")
        result = ai_workforce_performance(self.store)
        entry = result["by_worker"]["unassigned"]
        self.assertIsNone(entry["average_duration_seconds"])
        self.assertIsNone(entry["approximate_cost"])  # no cost ever recorded on the task

    def test_unassigned_tasks_are_grouped_separately_from_named_workers(self):
        self.tasks.create("media", "No worker yet", "echo", actor="Aryan")
        orch = WorkforceOrchestrator(self.store, self.audit, needs_aryan=self.needs_aryan)
        orch.register_worker(EchoWorker())
        ok_id = self.tasks.create("media", "Say hi", "echo", actor="Aryan")
        orch.execute(ok_id)

        result = ai_workforce_performance(self.store)
        self.assertIn("unassigned", result["by_worker"])
        self.assertIn("echo_worker", result["by_worker"])
        self.assertEqual(result["by_worker"]["unassigned"]["tasks_total"], 1)
        self.assertEqual(result["by_worker"]["echo_worker"]["tasks_total"], 1)


if __name__ == "__main__":
    unittest.main()
