import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from falguna.frontier import AutonomyPolicy, FrontierControlPlane, FrontierError
from falguna.frontier_supervisor import GraphSupervisor
from falguna.runtime import open_control_plane


class Phase9SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime, self.store = open_control_plane(Path(self.tmp.name) / "state")
        self.control = FrontierControlPlane(self.store, self.runtime.audit)
        self.org = "phase9-supervisor-tests"

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def objective(self):
        return self.control.create_objective(self.org, "Durable work", "Synthetic only", "WORK",
                                             "AUTONOMOUS_WITHIN_POLICY")["objective"]

    def test_supervisor_runs_dependencies_and_reports_status(self):
        objective = self.objective()
        first = self.control.add_node(objective["id"], self.org, "Plan", "INSPECT")
        second = self.control.add_node(objective["id"], self.org, "Report", "REPORT", [first["id"]])
        calls = []
        supervisor = GraphSupervisor(self.control, self.org, {
            "INSPECT": lambda i, c: calls.append(("lead", c["idempotency_key"])) or {"ok": True},
            "REPORT": lambda i, c: calls.append(("report", c["idempotency_key"])) or {"ok": True},
        }, poll_seconds=.01)
        self.assertTrue(supervisor.run_once())
        self.assertTrue(supervisor.run_once())
        self.assertFalse(supervisor.run_once())
        self.assertEqual([c[0] for c in calls], ["lead", "report"])
        self.assertEqual(supervisor.status()["node_counts"], {"COMPLETED": 2})

    def test_pause_resume_and_emergency_stop_are_enforced(self):
        objective = self.objective()
        node = self.control.add_node(objective["id"], self.org, "Plan", "INSPECT")
        supervisor = GraphSupervisor(self.control, self.org, {"INSPECT": lambda *_: {"ok": True}})
        self.control.pause_objective(objective["id"], self.org, "operator")
        self.assertFalse(supervisor.run_once())
        self.control.resume_objective(objective["id"], self.org, "operator")
        self.assertTrue(supervisor.run_once())
        other = self.objective()
        other_node = self.control.add_node(other["id"], self.org, "Blocked", "INSPECT")
        self.control.emergency_stop(other["id"], self.org, "operator", "test stop")
        self.assertEqual(self.store.get("p9_graph_nodes", other_node["id"])["status"], "CANCELLED")

    def test_expired_process_lease_restarts_without_repeating_completed_node(self):
        objective = self.objective()
        completed = self.control.add_node(objective["id"], self.org, "Completed", "INSPECT")
        recovered = self.control.add_node(objective["id"], self.org, "Recover", "REPORT", [completed["id"]],
                                          inputs={"side_effect_key": "restart-side-effect-v1"})
        counts = {"completed": 0, "side_effect": 0}
        first = GraphSupervisor(self.control, self.org, {
            "INSPECT": lambda *_: counts.__setitem__("completed", counts["completed"] + 1) or {"ok": True},
            "REPORT": lambda *_: {"ok": True},
        })
        first.run_once()
        self.control.claim_node(recovered["id"], self.org, "dead-process", 30)
        self.control.heartbeat_node(recovered["id"], self.org, "dead-process",
                                    checkpoint={"stage": "MEANINGFUL_WORK", "side_effect_performed": False})
        self.store.update("p9_graph_nodes", recovered["id"], lease_expires_at="2000-01-01T00:00:00+00:00")
        second = GraphSupervisor(self.control, self.org, {
            "INSPECT": lambda *_: counts.__setitem__("completed", counts["completed"] + 1) or {"ok": True},
            "REPORT": lambda i, c: counts.__setitem__("side_effect", counts["side_effect"] + 1) or
                                      {"idempotency_key": c["idempotency_key"]},
        })
        second.run_once()
        self.assertEqual(counts, {"completed": 1, "side_effect": 1})
        self.assertEqual(self.store.get("p9_graph_nodes", recovered["id"])["attempt"], 2)
        with self.assertRaisesRegex(FrontierError, "active lease owner"):
            self.control.complete_node(recovered["id"], self.org, "dead-process", {"stale": True})
        events = self.store.list("p9_events", "objective_id=?", (objective["id"],))
        self.assertTrue({"NODE_CHECKPOINT", "NODE_RECOVERED"}.issubset({e["event_type"] for e in events}))

    def test_every_autonomy_level_and_consequential_class_fails_closed(self):
        for level in ("ASK", "ASSIST", "EXECUTE_WITH_APPROVAL", "AUTONOMOUS_WITHIN_POLICY", "COMPANY_AUTONOMOUS"):
            self.assertFalse(AutonomyPolicy.decision(level, "PAYMENT")["allowed"])
        for action in ("REFUND", "PAYOUT", "BENEFICIARY_CHANGE", "PRODUCTION_DEPLOY", "PUSH", "MERGE",
                       "MAJOR_SPEND", "LEGAL_COMMITMENT", "EXTERNAL_OUTREACH", "LIVE_TRADE"):
            self.assertFalse(AutonomyPolicy.decision("COMPANY_AUTONOMOUS", action)["allowed"])


if __name__ == "__main__":
    unittest.main()
