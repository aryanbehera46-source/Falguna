"""Phase 5 Continuation, Section 14 -- Capability Registry V1.

Covers: introspection-based sync from a live WorkforceOrchestrator (never
a hand-maintained duplicate list), the human-maintained profile overlay
staying independent of and surviving repeated syncs, validation of the
overlay's constrained fields, and the evidence-based can_deliver() query
(routable vs not, and the UNAVAILABLE/LIMITED folding). Synthetic data
only.
"""

import json
import tempfile
import unittest
from pathlib import Path

from falguna.audit import AuditLog
from falguna.capability_registry import (
    CapabilityRegistryError, CapabilityRegistryStore, can_deliver,
)
from falguna.store import StateStore
from falguna.workforce import WorkerResult, WorkforceOrchestrator, WorkforceWorker


class _EchoWorker(WorkforceWorker):
    """A worker with no _SUPPORTED set -- exercises the introspection
    fallback (empty list, not a guess)."""
    name = "test_echo_worker"

    def supports(self, task_type):
        return task_type == "echo"

    def execute(self, task):
        return WorkerResult(status="COMPLETED", result={}, evidence={"echoed": True})


class _ConventionalWorker(WorkforceWorker):
    """A worker following the real _SUPPORTED convention every production
    worker uses."""
    name = "test_conventional_worker"
    _SUPPORTED = {"alpha_task", "beta_task"}
    auto_resumable_after_restart = True

    def supports(self, task_type):
        return task_type in self._SUPPORTED

    def execute(self, task):
        return WorkerResult(status="COMPLETED", result={}, evidence={"ok": True})


class CapabilityRegistryTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = StateStore(self.root / "state.db")
        self.store.migrate()
        self.audit = AuditLog(self.root / "audit.jsonl")
        self.registry = CapabilityRegistryStore(self.store, self.audit)
        self.orch = WorkforceOrchestrator(self.store, self.audit)
        self.orch.register_worker(_EchoWorker())
        self.orch.register_worker(_ConventionalWorker())

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_sync_discovers_every_registered_worker(self):
        touched = self.registry.sync_from_workforce(self.orch)
        self.assertEqual(len(touched), 2)
        names = {c["worker_name"] for c in self.registry.list()}
        self.assertEqual(names, {"test_echo_worker", "test_conventional_worker"})

    def test_sync_captures_supported_task_types_from_introspection(self):
        self.registry.sync_from_workforce(self.orch)
        conv = self.registry.get_by_worker_name("test_conventional_worker")
        self.assertEqual(json.loads(conv["supported_task_types_json"]), ["alpha_task", "beta_task"])
        self.assertEqual(conv["auto_resumable_after_restart"], 1)

    def test_sync_worker_without_supported_convention_reports_empty_not_guessed(self):
        self.registry.sync_from_workforce(self.orch)
        echo = self.registry.get_by_worker_name("test_echo_worker")
        self.assertEqual(json.loads(echo["supported_task_types_json"]), [])

    def test_newly_discovered_capability_overlay_fields_are_unknown(self):
        self.registry.sync_from_workforce(self.orch)
        conv = self.registry.get_by_worker_name("test_conventional_worker")
        self.assertIsNone(conv["availability"])
        self.assertIsNone(conv["requires_specialist"])
        self.assertIsNone(conv["known_limitations"])
        self.assertIsNone(conv["model_tool_dependencies_json"])

    def test_repeated_sync_is_idempotent_and_preserves_human_overlay(self):
        self.registry.sync_from_workforce(self.orch)
        conv = self.registry.get_by_worker_name("test_conventional_worker")
        self.registry.set_profile(conv["id"], "Aryan", availability="AVAILABLE", requires_specialist=False)
        # A second sync must refresh introspected facts but never clobber
        # the human-set overlay, and must not create a duplicate row.
        self.registry.sync_from_workforce(self.orch)
        self.assertEqual(len(self.registry.list()), 2)
        conv_after = self.registry.get_by_worker_name("test_conventional_worker")
        self.assertEqual(conv_after["availability"], "AVAILABLE")
        self.assertEqual(conv_after["requires_specialist"], 0)

    def test_sync_refreshes_supported_task_types_if_worker_changes(self):
        self.registry.sync_from_workforce(self.orch)
        _ConventionalWorker._SUPPORTED = {"alpha_task", "beta_task", "gamma_task"}
        try:
            self.registry.sync_from_workforce(self.orch)
            conv = self.registry.get_by_worker_name("test_conventional_worker")
            self.assertIn("gamma_task", json.loads(conv["supported_task_types_json"]))
        finally:
            _ConventionalWorker._SUPPORTED = {"alpha_task", "beta_task"}

    def test_set_profile_rejects_unknown_availability(self):
        self.registry.sync_from_workforce(self.orch)
        conv = self.registry.get_by_worker_name("test_conventional_worker")
        with self.assertRaises(CapabilityRegistryError):
            self.registry.set_profile(conv["id"], "Aryan", availability="SORT_OF")

    def test_set_profile_rejects_unknown_cost_driver(self):
        self.registry.sync_from_workforce(self.orch)
        conv = self.registry.get_by_worker_name("test_conventional_worker")
        with self.assertRaises(CapabilityRegistryError):
            self.registry.set_profile(conv["id"], "Aryan", cost_driver="MAGIC")

    def test_set_profile_unknown_capability_rejected(self):
        with self.assertRaises(CapabilityRegistryError):
            self.registry.set_profile("does-not-exist", "Aryan", availability="AVAILABLE")

    # -- can_deliver() -----------------------------------------------------

    def test_can_deliver_true_for_routable_task_type(self):
        self.registry.sync_from_workforce(self.orch)
        result = can_deliver(self.registry, self.orch, "alpha_task")
        self.assertTrue(result["can_deliver"])
        self.assertEqual(result["worker_name"], "test_conventional_worker")

    def test_can_deliver_false_for_unroutable_task_type(self):
        self.registry.sync_from_workforce(self.orch)
        result = can_deliver(self.registry, self.orch, "no_such_task_type")
        self.assertFalse(result["can_deliver"])
        self.assertIsNone(result["worker_name"])
        self.assertIn("no registered worker", result["reason"])

    def test_can_deliver_is_routable_even_before_any_sync(self):
        # can_deliver() checks live routability against the orchestrator,
        # not a possibly-stale/never-synced registry row.
        result = can_deliver(self.registry, self.orch, "alpha_task")
        self.assertTrue(result["can_deliver"])
        self.assertIsNone(result["availability"])

    def test_can_deliver_false_when_human_marked_unavailable(self):
        self.registry.sync_from_workforce(self.orch)
        conv = self.registry.get_by_worker_name("test_conventional_worker")
        self.registry.set_profile(conv["id"], "Aryan", availability="UNAVAILABLE", known_limitations="API quota exhausted this month.")
        result = can_deliver(self.registry, self.orch, "alpha_task")
        self.assertFalse(result["can_deliver"])
        self.assertEqual(result["known_limitations"], "API quota exhausted this month.")

    def test_can_deliver_true_but_flagged_when_limited(self):
        self.registry.sync_from_workforce(self.orch)
        conv = self.registry.get_by_worker_name("test_conventional_worker")
        self.registry.set_profile(conv["id"], "Aryan", availability="LIMITED")
        result = can_deliver(self.registry, self.orch, "alpha_task")
        self.assertTrue(result["can_deliver"])
        self.assertIn("LIMITED", result["reason"])


if __name__ == "__main__":
    unittest.main()
