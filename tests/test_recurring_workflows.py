"""Tests for the Recurring workflow foundation (falguna/recurring_workflows.py).

`run_due()` is the whole contract here: it must create a real, inspectable
wf_tasks row for each due definition and never execute anything itself --
scheduling is deliberately just "callable programmatically", not a daemon.
"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from falguna.audit import AuditLog
from falguna.recurring_workflows import RecurringWorkflowError, RecurringWorkflowStore
from falguna.store import StateStore


class RecurringWorkflowTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = StateStore(root / "state.db")
        self.store.migrate()
        self.audit = AuditLog(root / "audit.jsonl")
        self.workflows = RecurringWorkflowStore(self.store, self.audit)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()


class CreateTests(RecurringWorkflowTestBase):
    def test_create_requires_name(self):
        with self.assertRaises(RecurringWorkflowError):
            self.workflows.create("", "media", "Daily research", "research", "daily")

    def test_create_rejects_unknown_schedule_kind(self):
        with self.assertRaises(RecurringWorkflowError):
            self.workflows.create("Daily research", "media", "Daily research", "research", "monthly")

    def test_interval_schedule_requires_hours_config(self):
        with self.assertRaises(RecurringWorkflowError):
            self.workflows.create("Every N hours", "media", "obj", "research", "interval")

    def test_create_sets_active_and_next_due(self):
        workflow_id = self.workflows.create("Daily research", "media", "Research trends", "research", "daily")
        workflow = self.workflows.get(workflow_id)
        self.assertEqual(workflow["status"], "ACTIVE")
        self.assertIsNotNone(workflow["next_due_at"])
        self.assertIsNone(workflow["last_run_at"])


class PauseResumeTests(RecurringWorkflowTestBase):
    def test_pause_and_resume(self):
        workflow_id = self.workflows.create("Weekly report", "ops", "obj", "document_creation", "weekly")
        self.workflows.pause(workflow_id, actor="Aryan")
        self.assertEqual(self.workflows.get(workflow_id)["status"], "PAUSED")
        self.workflows.resume(workflow_id, actor="Aryan")
        self.assertEqual(self.workflows.get(workflow_id)["status"], "ACTIVE")

    def test_pause_missing_workflow_raises(self):
        with self.assertRaises(RecurringWorkflowError):
            self.workflows.pause("does-not-exist", actor="Aryan")


class DueAndRunDueTests(RecurringWorkflowTestBase):
    def test_workflow_not_yet_due_is_excluded(self):
        self.workflows.create("Hourly check", "ops", "obj", "data_processing", "hourly")
        due = self.workflows.due(as_of=datetime.now(timezone.utc).isoformat())
        self.assertEqual(due, [])

    def test_workflow_due_in_the_past_is_included(self):
        workflow_id = self.workflows.create("Hourly check", "ops", "obj", "data_processing", "hourly")
        far_future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        due = self.workflows.due(as_of=far_future)
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]["id"], workflow_id)

    def test_paused_workflow_is_never_due(self):
        workflow_id = self.workflows.create("Hourly check", "ops", "obj", "data_processing", "hourly")
        self.workflows.pause(workflow_id, actor="Aryan")
        far_future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        due = self.workflows.due(as_of=far_future)
        self.assertEqual(due, [])

    def test_run_due_creates_real_task_and_advances_schedule(self):
        workflow_id = self.workflows.create(
            "Daily research", "media", "Research industry trends", "research", "daily",
            task_template={"objective": "Research trends for today", "priority": "normal"},
        )
        far_future = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        task_ids = self.workflows.run_due(actor="system", as_of=far_future)
        self.assertEqual(len(task_ids), 1)

        task = self.store.get("wf_tasks", task_ids[0])
        self.assertIsNotNone(task)
        self.assertEqual(task["status"], "CREATED")  # run_due never executes, only creates
        self.assertEqual(task["objective"], "Research trends for today")
        self.assertEqual(task["source"], f"recurring:{workflow_id}")

        workflow = self.workflows.get(workflow_id)
        self.assertIsNotNone(workflow["last_run_at"])
        # next_due_at is anchored to the real current time run_due() executed at
        # (not the artificial far-future `as_of` used above just to make this
        # workflow due) -- so it should land roughly a day after actual "now".
        self.assertGreater(workflow["next_due_at"], datetime.now(timezone.utc).isoformat())

        runs = self.store.list("wf_recurring_runs", "workflow_id=?", (workflow_id,))
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["task_id"], task_ids[0])

    def test_run_due_is_a_noop_when_nothing_is_due(self):
        self.workflows.create("Hourly check", "ops", "obj", "data_processing", "hourly")
        task_ids = self.workflows.run_due(actor="system", as_of=datetime.now(timezone.utc).isoformat())
        self.assertEqual(task_ids, [])

    def test_interval_schedule_uses_configured_hours(self):
        workflow_id = self.workflows.create(
            "Every 6 hours", "ops", "obj", "data_processing", "interval", schedule_config={"hours": 6},
        )
        far_future = (datetime.now(timezone.utc) + timedelta(hours=7)).isoformat()
        task_ids = self.workflows.run_due(actor="system", as_of=far_future)
        self.assertEqual(len(task_ids), 1)


class ListTests(RecurringWorkflowTestBase):
    def test_list_returns_newest_first(self):
        first = self.workflows.create("First", "ops", "obj", "data_processing", "daily")
        second = self.workflows.create("Second", "ops", "obj", "data_processing", "daily")
        rows = self.workflows.list()
        self.assertEqual(rows[0]["id"], second)
        self.assertEqual(rows[1]["id"], first)


if __name__ == "__main__":
    unittest.main()
