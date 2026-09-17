"""Tests for the Digital Workforce core (falguna/workforce.py).

Covers the task lifecycle state graph, the durable event history, the
common worker interface contract (evidence-required completion), worker
routing (including the "no worker registered" escalation path fixed in
this pass), bounded automatic retry on failure, and restart persistence
(a fresh StateStore against the same file sees the same task state).
"""

import json
import tempfile
import unittest
from pathlib import Path

from falguna.audit import AuditLog
from falguna.store import StateStore
from falguna.ttt_hq import NeedsAryanQueue
from falguna.workforce import (
    ALLOWED_TASK_TRANSITIONS,
    TASK_STATUSES,
    WorkerResult,
    WorkforceError,
    WorkforceOrchestrator,
    WorkforceTaskStore,
    WorkforceWorker,
)


class EchoWorker(WorkforceWorker):
    name = "echo_worker"

    def supports(self, task_type):
        return task_type == "echo"

    def execute(self, task):
        return WorkerResult(status="COMPLETED", result={"echo": task["objective"]}, evidence={"echoed": True}, execution_method="API")


class BlockingWorker(WorkforceWorker):
    name = "blocking_worker"

    def supports(self, task_type):
        return task_type == "blocked_type"

    def execute(self, task):
        return WorkerResult(status="BLOCKED", blockers=["needs a human"], next_action="review manually")


class AlwaysFailWorker(WorkforceWorker):
    name = "always_fail_worker"

    def supports(self, task_type):
        return task_type == "fail_type"

    def execute(self, task):
        return WorkerResult(status="FAILED", next_action="simulated failure")


class WorkforceTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = StateStore(self.root / "state.db")
        self.store.migrate()
        self.audit = AuditLog(self.root / "audit.jsonl")
        self.needs_aryan = NeedsAryanQueue(self.store, self.audit)
        self.tasks = WorkforceTaskStore(self.store, self.audit)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()


class TaskLifecycleTests(WorkforceTestBase):
    def test_create_starts_in_created(self):
        task_id = self.tasks.create("media", "Do a thing", "echo", actor="Aryan")
        task = self.tasks.get(task_id)
        self.assertEqual(task["status"], "CREATED")
        self.assertEqual(task["retries"], 0)
        self.assertIsNone(task["completed_at"])

    def test_create_requires_department_objective_task_type(self):
        with self.assertRaises(WorkforceError):
            self.tasks.create("", "objective", "echo")
        with self.assertRaises(WorkforceError):
            self.tasks.create("media", "", "echo")
        with self.assertRaises(WorkforceError):
            self.tasks.create("media", "objective", "")

    def test_valid_transition_recorded_in_history(self):
        task_id = self.tasks.create("media", "Do a thing", "echo", actor="Aryan")
        self.tasks.transition(task_id, "PLANNING", actor="Aryan", reason="planning")
        history = self.tasks.history(task_id)
        self.assertEqual(len(history), 2)  # CREATED (initial) + PLANNING
        self.assertEqual(history[-1]["from_status"], "CREATED")
        self.assertEqual(history[-1]["to_status"], "PLANNING")

    def test_same_state_transition_is_idempotent_noop(self):
        task_id = self.tasks.create("media", "Do a thing", "echo", actor="Aryan")
        before = self.tasks.history(task_id)
        result = self.tasks.transition(task_id, "CREATED", actor="Aryan")
        after = self.tasks.history(task_id)
        self.assertEqual(result["status"], "CREATED")
        self.assertEqual(len(before), len(after))  # no new event recorded

    def test_illegal_transition_raises_and_does_not_mutate(self):
        task_id = self.tasks.create("media", "Do a thing", "echo", actor="Aryan")
        with self.assertRaises(WorkforceError):
            self.tasks.transition(task_id, "COMPLETED", actor="Aryan")  # CREATED -> COMPLETED is not allowed
        self.assertEqual(self.tasks.get(task_id)["status"], "CREATED")

    def test_transition_unknown_status_raises(self):
        task_id = self.tasks.create("media", "Do a thing", "echo", actor="Aryan")
        with self.assertRaises(WorkforceError):
            self.tasks.transition(task_id, "NOT_A_REAL_STATUS", actor="Aryan")

    def test_transition_missing_task_raises(self):
        with self.assertRaises(WorkforceError):
            self.tasks.transition("does-not-exist", "PLANNING", actor="Aryan")

    def test_every_declared_status_is_reachable_or_terminal_by_construction(self):
        # Sanity check on the graph itself: every status is a key or a
        # target somewhere, so nothing was declared but orphaned.
        reachable = set(ALLOWED_TASK_TRANSITIONS.keys())
        targets = set()
        for edges in ALLOWED_TASK_TRANSITIONS.values():
            targets |= edges
        self.assertEqual(reachable | targets, TASK_STATUSES)


class WorkerResultTests(unittest.TestCase):
    def test_completed_without_evidence_raises(self):
        with self.assertRaises(WorkforceError):
            WorkerResult(status="COMPLETED", result={"x": 1})  # no evidence

    def test_completed_with_evidence_is_fine(self):
        r = WorkerResult(status="COMPLETED", result={"x": 1}, evidence={"proof": True})
        self.assertEqual(r.status, "COMPLETED")

    def test_blocked_and_failed_do_not_require_evidence(self):
        WorkerResult(status="BLOCKED", blockers=["x"])
        WorkerResult(status="FAILED", next_action="retry")

    def test_invalid_status_raises(self):
        with self.assertRaises(WorkforceError):
            WorkerResult(status="DONE_MAYBE")


class OrchestratorTests(WorkforceTestBase):
    def _orch(self, *workers):
        o = WorkforceOrchestrator(self.store, self.audit, needs_aryan=self.needs_aryan)
        for w in workers:
            o.register_worker(w)
        return o

    def test_completed_path_goes_through_verifying_to_completed(self):
        orch = self._orch(EchoWorker())
        task_id = self.tasks.create("media", "Say hi", "echo", actor="Aryan")
        result = orch.execute(task_id)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(json.loads(result["outputs_json"]), {"echo": "Say hi"})
        self.assertEqual(json.loads(result["evidence_json"]), {"echoed": True})
        self.assertIsNotNone(result["completed_at"])
        statuses = [e["to_status"] for e in self.tasks.history(task_id)]
        self.assertIn("VERIFYING", statuses)
        self.assertIn("COMPLETED", statuses)

    def test_blocked_worker_result_escalates_to_needs_aryan(self):
        orch = self._orch(BlockingWorker())
        task_id = self.tasks.create("media", "Do something risky", "blocked_type", actor="Aryan")
        result = orch.execute(task_id)
        self.assertEqual(result["status"], "NEEDS_ARYAN")
        self.assertIsNotNone(result["needs_aryan_id"])
        item = self.store.get("needs_aryan_items", result["needs_aryan_id"])
        self.assertEqual(item["kind"], "workforce_action_approval")
        self.assertEqual(item["status"], "PENDING")

    def test_blocked_without_needs_aryan_wired_still_reaches_needs_aryan_status(self):
        orch = WorkforceOrchestrator(self.store, self.audit, needs_aryan=None)
        orch.register_worker(BlockingWorker())
        task_id = self.tasks.create("media", "Do something risky", "blocked_type", actor="Aryan")
        result = orch.execute(task_id)
        self.assertEqual(result["status"], "NEEDS_ARYAN")
        self.assertIsNone(result["needs_aryan_id"])  # nothing to escalate to, but never fabricated

    def test_failed_result_retries_up_to_max_then_terminal(self):
        orch = self._orch(AlwaysFailWorker())
        task_id = self.tasks.create("media", "Doomed task", "fail_type", actor="Aryan")
        r1 = orch.execute(task_id)
        self.assertEqual(r1["status"], "READY")
        self.assertEqual(r1["retries"], 1)
        r2 = orch.execute(task_id)
        self.assertEqual(r2["status"], "READY")
        self.assertEqual(r2["retries"], 2)
        r3 = orch.execute(task_id)
        self.assertEqual(r3["status"], "FAILED")  # max_retries=2 exhausted, stops here
        self.assertIsNotNone(r3["error"])

    def test_unroutable_task_type_escalates_instead_of_raising(self):
        orch = self._orch(EchoWorker())  # only supports "echo"
        task_id = self.tasks.create("media", "Unroutable", "no_such_type", actor="Aryan")
        result = orch.execute(task_id)  # must not raise
        self.assertEqual(result["status"], "NEEDS_ARYAN")
        self.assertIsNotNone(result["needs_aryan_id"])
        item = self.store.get("needs_aryan_items", result["needs_aryan_id"])
        self.assertEqual(item["kind"], "workforce_action_approval")
        self.assertIn("no_such_type", item["what_is_needed"])

    def test_execute_on_terminal_task_raises(self):
        orch = self._orch(EchoWorker())
        task_id = self.tasks.create("media", "Say hi", "echo", actor="Aryan")
        orch.execute(task_id)  # -> COMPLETED
        with self.assertRaises(WorkforceError):
            orch.execute(task_id)

    def test_first_matching_worker_wins_routing(self):
        # Two workers both "could" support echo; registration order decides.
        class OtherEchoWorker(WorkforceWorker):
            name = "other_echo"

            def supports(self, task_type):
                return task_type == "echo"

            def execute(self, task):
                return WorkerResult(status="COMPLETED", result={"from": "other"}, evidence={"ok": True})

        orch = self._orch(EchoWorker(), OtherEchoWorker())
        task_id = self.tasks.create("media", "Say hi", "echo", actor="Aryan")
        result = orch.execute(task_id)
        self.assertEqual(result["assigned_worker"], "echo_worker")


class PersistenceTests(WorkforceTestBase):
    def test_task_state_survives_a_fresh_store_reconnect(self):
        orch = WorkforceOrchestrator(self.store, self.audit, needs_aryan=self.needs_aryan)
        orch.register_worker(EchoWorker())
        task_id = self.tasks.create("media", "Say hi", "echo", actor="Aryan")
        orch.execute(task_id)
        self.store.close()

        reopened_store = StateStore(self.root / "state.db")
        reopened_store.migrate()
        reopened_tasks = WorkforceTaskStore(reopened_store, self.audit)
        task = reopened_tasks.get(task_id)
        self.assertEqual(task["status"], "COMPLETED")
        self.assertEqual(json.loads(task["outputs_json"]), {"echo": "Say hi"})
        history = reopened_tasks.history(task_id)
        self.assertGreaterEqual(len(history), 3)  # CREATED, PLANNING, READY, EXECUTING, VERIFYING, COMPLETED
        self.store = reopened_store  # let tearDown close this live handle instead of the already-closed one


if __name__ == "__main__":
    unittest.main()
