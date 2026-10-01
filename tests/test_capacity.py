"""Phase 5 Continuation, Section 16 -- Delivery Capacity & Scheduling V1.

Covers: pure counting correctness against real wf_tasks/cs_projects/
needs_aryan_items rows, the categorical recommendation priority order
(APPROVAL_BOTTLENECK > SPECIALIST_CONSTRAINED > EXECUTION_BOTTLENECK >
CONSTRAINED > AVAILABLE), and the explicit "no fabricated percentage"
contract (no field anywhere in the snapshot is a computed ratio).
Synthetic data only.
"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from falguna.audit import AuditLog
from falguna.capacity import CAPACITY_RECOMMENDATIONS, CAPACITY_THRESHOLDS, capacity_snapshot
from falguna.store import StateStore


class CapacityTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = StateStore(self.root / "state.db")
        self.store.migrate()
        self.audit = AuditLog(self.root / "audit.jsonl")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def _wf_task(self, status, created_at=None):
        now = (created_at or datetime.now(timezone.utc)).isoformat()
        return self.store.create("wf_tasks", {
            "department": "engineering", "objective": "synthetic", "task_type": "engineering_fix",
            "source": None, "assigned_worker": None, "status": status, "priority": None,
            "inputs_json": None, "outputs_json": None, "evidence_json": None, "blockers_json": None,
            "approval_required": 0, "needs_aryan_id": None, "execution_method": None,
            "cost": None, "retries": 0, "error": None, "actor": "system",
            "created_at": now, "started_at": None, "completed_at": None, "updated_at": now,
        })

    def _cs_project(self, status):
        now = datetime.now(timezone.utc).isoformat()
        return self.store.create("cs_projects", {
            "intake_id": None, "service_id": None, "opportunity_id": None, "foundation_id": None,
            "client_id": None, "delivery_route": "CUSTOM_BUILD", "status": status, "mission_id": None,
            "actor": "system", "created_at": now, "updated_at": now,
        })

    def _needs_aryan_item(self, created_at=None):
        now = (created_at or datetime.now(timezone.utc)).isoformat()
        return self.store.create("needs_aryan_items", {
            "kind": "pricing_decision", "title": "synthetic", "what_is_needed": "a decision",
            "recommendation": None, "rationale": None, "risk": None, "expected_value": None,
            "ref_type": None, "ref_id": None, "status": "PENDING",
            "decided_by": None, "decision_note": None,
            "created_at": now, "updated_at": now, "decided_at": None,
        })

    def test_empty_state_is_available(self):
        snap = capacity_snapshot(self.store)
        self.assertEqual(snap["recommendation"], "AVAILABLE")
        self.assertEqual(snap["wf_tasks_in_flight"], 0)
        self.assertEqual(snap["wf_tasks_stuck"], 0)
        self.assertEqual(snap["needs_aryan_pending"], 0)
        self.assertIsNone(snap["needs_aryan_oldest_pending_hours"])

    def test_counts_projects_by_status_and_excludes_closed_from_active(self):
        self._cs_project("SCOPED")
        self._cs_project("IN_DELIVERY")
        self._cs_project("CLOSED")
        snap = capacity_snapshot(self.store)
        self.assertEqual(snap["projects_by_status"]["SCOPED"], 1)
        self.assertEqual(snap["projects_by_status"]["IN_DELIVERY"], 1)
        self.assertEqual(snap["projects_by_status"]["CLOSED"], 1)
        self.assertEqual(snap["active_projects"], 2)

    def test_in_flight_counts_only_non_stuck_statuses(self):
        for _ in range(3):
            self._wf_task("EXECUTING")
        self._wf_task("BLOCKED")
        self._wf_task("COMPLETED")
        snap = capacity_snapshot(self.store)
        self.assertEqual(snap["wf_tasks_in_flight"], 3)
        self.assertEqual(snap["wf_tasks_stuck"], 1)

    def test_recommendation_available_below_all_thresholds(self):
        self._wf_task("EXECUTING")
        snap = capacity_snapshot(self.store)
        self.assertEqual(snap["recommendation"], "AVAILABLE")

    def test_recommendation_constrained_above_in_flight_constrained_threshold(self):
        for _ in range(CAPACITY_THRESHOLDS["in_flight_constrained"] + 1):
            self._wf_task("EXECUTING")
        snap = capacity_snapshot(self.store)
        self.assertEqual(snap["recommendation"], "CONSTRAINED")

    def test_recommendation_execution_bottleneck_above_in_flight_bottleneck_threshold(self):
        for _ in range(CAPACITY_THRESHOLDS["in_flight_bottleneck"] + 1):
            self._wf_task("EXECUTING")
        snap = capacity_snapshot(self.store)
        self.assertEqual(snap["recommendation"], "EXECUTION_BOTTLENECK")

    def test_recommendation_specialist_constrained_above_stuck_threshold(self):
        for _ in range(CAPACITY_THRESHOLDS["stuck_specialist_constrained"] + 1):
            self._wf_task("BLOCKED")
        snap = capacity_snapshot(self.store)
        self.assertEqual(snap["recommendation"], "SPECIALIST_CONSTRAINED")

    def test_recommendation_specialist_constrained_beats_execution_bottleneck(self):
        # Both signals present: stuck tasks take priority over raw in-flight
        # volume, because a stuck task needs a different kind of fix.
        for _ in range(CAPACITY_THRESHOLDS["in_flight_bottleneck"] + 1):
            self._wf_task("EXECUTING")
        for _ in range(CAPACITY_THRESHOLDS["stuck_specialist_constrained"] + 1):
            self._wf_task("BLOCKED")
        snap = capacity_snapshot(self.store)
        self.assertEqual(snap["recommendation"], "SPECIALIST_CONSTRAINED")

    def test_recommendation_approval_bottleneck_above_pending_count_threshold(self):
        for _ in range(CAPACITY_THRESHOLDS["needs_aryan_count_bottleneck"] + 1):
            self._needs_aryan_item()
        snap = capacity_snapshot(self.store)
        self.assertEqual(snap["recommendation"], "APPROVAL_BOTTLENECK")

    def test_recommendation_approval_bottleneck_from_a_single_stale_item(self):
        stale = datetime.now(timezone.utc) - timedelta(hours=CAPACITY_THRESHOLDS["needs_aryan_age_hours_bottleneck"] + 1)
        self._needs_aryan_item(created_at=stale)
        snap = capacity_snapshot(self.store)
        self.assertEqual(snap["recommendation"], "APPROVAL_BOTTLENECK")
        self.assertGreater(snap["needs_aryan_oldest_pending_hours"], CAPACITY_THRESHOLDS["needs_aryan_age_hours_bottleneck"])

    def test_recommendation_approval_bottleneck_beats_every_other_signal(self):
        # The approval queue is checked first in priority order, even when
        # every other signal is also past its threshold.
        for _ in range(CAPACITY_THRESHOLDS["in_flight_bottleneck"] + 1):
            self._wf_task("EXECUTING")
        for _ in range(CAPACITY_THRESHOLDS["stuck_specialist_constrained"] + 1):
            self._wf_task("BLOCKED")
        for _ in range(CAPACITY_THRESHOLDS["needs_aryan_count_bottleneck"] + 1):
            self._needs_aryan_item()
        snap = capacity_snapshot(self.store)
        self.assertEqual(snap["recommendation"], "APPROVAL_BOTTLENECK")

    def test_recommendation_is_always_one_of_the_declared_categories(self):
        snap = capacity_snapshot(self.store)
        self.assertIn(snap["recommendation"], CAPACITY_RECOMMENDATIONS)

    def test_snapshot_never_contains_a_fabricated_percentage_field(self):
        for _ in range(4):
            self._wf_task("EXECUTING")
        snap = capacity_snapshot(self.store)
        for key, value in snap.items():
            if isinstance(value, float):
                self.fail(f"unexpected float field {key!r}={value!r} -- capacity snapshots must only ever be integer counts, categorical labels, or an explicit age in hours")


if __name__ == "__main__":
    unittest.main()
