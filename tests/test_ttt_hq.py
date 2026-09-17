import subprocess
import tempfile
import unittest
from pathlib import Path

from falguna.audit import AuditLog
from falguna.handoff import accept_revenue_hunter_handoff, validate_handoff_payload
from falguna.models import RunPolicy
from falguna.runtime import open_control_plane
from falguna.store import StateStore
from falguna.ttt_hq import BacklogStore, BoardroomStore, NeedsAryanQueue, hq_overview


def git(repo: Path, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


class TTTHQTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(self.repo)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "falguna@test.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Falguna Test"], check=True)
        (self.repo / "README.md").write_text("seed\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-m", "seed"], check=True, capture_output=True)
        self.control, self.store = open_control_plane(self.repo)
        self.state_dir = self.repo / ".falguna"

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def _reopen_store(self) -> StateStore:
        """Simulate a full process restart: a brand new StateStore against the same file."""
        fresh = StateStore(self.state_dir / "state.db")
        fresh.migrate()
        return fresh

    # ---------- Boardroom ----------

    def test_boardroom_topic_and_decision_survive_a_simulated_restart(self):
        audit = AuditLog(self.state_dir / "audit.jsonl")
        boardroom = BoardroomStore(self.store, audit)
        backlog = BacklogStore(self.store, audit)

        topic_id = boardroom.create_topic(
            "Add pricing-tier experiment", "Should we A/B test a mid-tier plan?", "Aryan",
            proposed_category="Revenue", proposed_phase="Planned", proposed_priority="High",
            proposed_revenue_impact="Could lift ARPU 10-15%",
        )
        boardroom.add_contribution(topic_id, "Revenue", "Comparable SaaS benchmarks suggest a mid-tier lifts ARPU.")
        boardroom.add_contribution(topic_id, "Technology", "Low implementation cost; feature-flag only.")
        result = boardroom.decide(topic_id, "approve", "Aryan", note="Let's try it next quarter.", backlog_store=backlog)
        self.assertIsNotNone(result["backlog_item_id"])
        self.store.close()

        # Fresh connection to the same DB file -- this is the actual restart test.
        reopened = self._reopen_store()
        try:
            fresh_boardroom = BoardroomStore(reopened, audit)
            topic = fresh_boardroom.get_topic(topic_id)
            self.assertIsNotNone(topic, "topic must survive a fresh connection -- this is the bug the prior attempt had")
            self.assertEqual(topic["status"], "DECIDED")
            self.assertEqual(len(topic["contributions"]), 2)
            self.assertEqual(len(topic["decisions"]), 1)
            self.assertEqual(topic["decisions"][0]["action"], "APPROVED")
            self.assertEqual(topic["decisions"][0]["note"], "Let's try it next quarter.")

            fresh_backlog = BacklogStore(reopened, audit)
            item = fresh_backlog.get_item(result["backlog_item_id"])
            self.assertIsNotNone(item)
            self.assertEqual(item["title"], "Add pricing-tier experiment")
            self.assertEqual(item["status"], "Planned")
            self.assertEqual(item["source_boardroom_topic_id"], topic_id)
        finally:
            reopened.close()
        self.store = StateStore(self.state_dir / "state.db")  # so tearDown's close() has a live handle

    def test_boardroom_rejects_unknown_perspective(self):
        audit = AuditLog(self.state_dir / "audit.jsonl")
        boardroom = BoardroomStore(self.store, audit)
        topic_id = boardroom.create_topic("Idea", "Summary", "Aryan")
        with self.assertRaises(ValueError):
            boardroom.add_contribution(topic_id, "Marketing", "not a real perspective")

    def test_boardroom_deferred_decision_does_not_close_topic(self):
        audit = AuditLog(self.state_dir / "audit.jsonl")
        boardroom = BoardroomStore(self.store, audit)
        topic_id = boardroom.create_topic("Idea", "Summary", "Aryan")
        boardroom.decide(topic_id, "defer", "Aryan", note="need more data")
        topic = boardroom.get_topic(topic_id)
        self.assertEqual(topic["status"], "OPEN")
        self.assertEqual(len(topic["decisions"]), 1)

    # ---------- Backlog ----------

    def test_backlog_update_records_history_only_for_changed_fields(self):
        audit = AuditLog(self.state_dir / "audit.jsonl")
        backlog = BacklogStore(self.store, audit)
        item_id = backlog.create_item("Ship X", "Aryan", category="Engineering", priority="Medium", status="Future")
        backlog.update_item(item_id, "Aryan", reason="promoted", status="Active", priority="Medium")
        item = backlog.get_item(item_id)
        self.assertEqual(item["status"], "Active")
        # priority was unchanged (still "Medium") so only one history row (status) plus the creation row.
        history_fields = [h["field"] for h in item["history"]]
        self.assertEqual(history_fields.count("status"), 2)  # created + updated
        self.assertEqual(history_fields.count("priority"), 0)

    def test_backlog_rejects_invalid_status(self):
        audit = AuditLog(self.state_dir / "audit.jsonl")
        backlog = BacklogStore(self.store, audit)
        with self.assertRaises(ValueError):
            backlog.create_item("Ship X", "Aryan", status="Someday")

    # ---------- Needs Aryan ----------

    def test_needs_aryan_business_item_lifecycle(self):
        audit = AuditLog(self.state_dir / "audit.jsonl")
        queue = NeedsAryanQueue(self.store, audit, control=self.control)
        item_id = queue.create_item(
            "pricing_decision", "Discount request from Pemberly & Co.",
            "10% discount ask -- approve, counter, or hold firm?",
            recommendation="Counter with 5% off", rationale="Established buyer, protect margin",
            risk="Low", expected_value="7500",
        )
        pending = queue.list_pending()
        self.assertTrue(any(item["id"] == item_id for item in pending))
        queue.decide(item_id, "approve", "Aryan", note="Go ahead")
        pending_after = queue.list_pending()
        self.assertFalse(any(item["id"] == item_id for item in pending_after))
        decided = queue.list_decided()
        self.assertTrue(any(item["id"] == item_id and item["status"] == "APPROVED" for item in decided))

    def test_needs_aryan_rejects_double_decision(self):
        audit = AuditLog(self.state_dir / "audit.jsonl")
        queue = NeedsAryanQueue(self.store, audit, control=self.control)
        item_id = queue.create_item("risky_action", "Title", "What is needed")
        queue.decide(item_id, "reject", "Aryan")
        with self.assertRaises(ValueError):
            queue.decide(item_id, "approve", "Aryan")

    def test_needs_aryan_surfaces_engineering_mission_as_inspect_only_when_not_at_merge_gate(self):
        audit = AuditLog(self.state_dir / "audit.jsonl")
        queue = NeedsAryanQueue(self.store, audit, control=self.control)
        run_id = self._seed_run_needing_approval(category="SCOPE_EXPANSION")

        pending = queue.list_pending()
        derived = [item for item in pending if item["id"] == f"run:{run_id}"]
        self.assertEqual(len(derived), 1)
        self.assertFalse(derived[0]["actionable"])
        self.assertEqual(derived[0]["source"], "falguna_engineering")

        with self.assertRaises(ValueError) as ctx:
            queue.decide(f"run:{run_id}", "approve", "Aryan")
        self.assertIn("Falguna Engineering", str(ctx.exception))

        # Deferring is always safe, even when not actionable, and mutates nothing.
        result = queue.decide(f"run:{run_id}", "defer", "Aryan")
        self.assertEqual(result["action"], "DEFERRED")

    def test_needs_aryan_engineering_item_is_actionable_at_the_real_merge_gate(self):
        audit = AuditLog(self.state_dir / "audit.jsonl")
        queue = NeedsAryanQueue(self.store, audit, control=self.control)
        run_id = self._seed_run_needing_approval(category="FINAL_MERGE")
        self.store.create("approvals", {
            "run_id": run_id, "kind": "PROTECTED_BRANCH_MERGE", "status": "PENDING",
            "requested_at": "2026-01-01T00:00:00+00:00", "decided_at": None, "decided_by": None,
            "reason": None, "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:00+00:00",
        })
        pending = queue.list_pending()
        derived = [item for item in pending if item["id"] == f"run:{run_id}"]
        self.assertTrue(derived[0]["actionable"])
        # This must route through the real, existing decide_merge -- not a parallel code path.
        queue.decide(f"run:{run_id}", "approve", "Aryan", note="looks good")
        approval = self.store.list("approvals", "run_id=?", (run_id,))[0]
        self.assertEqual(approval["status"], "APPROVED")
        self.assertEqual(approval["decided_by"], "Aryan")

    def test_needs_aryan_stops_listing_a_run_once_its_merge_decision_is_recorded(self):
        # Regression test: decide_merge only writes to the `approvals` table --
        # it never touches `supervisor_states` (that's Falguna's own existing
        # behavior, and this queue must not assume otherwise). Before this fix,
        # a decided item kept showing up here as PENDING forever, because the
        # underlying supervisor_state row doesn't change until the run itself
        # resumes.
        audit = AuditLog(self.state_dir / "audit.jsonl")
        queue = NeedsAryanQueue(self.store, audit, control=self.control)
        run_id = self._seed_run_needing_approval(category="FINAL_MERGE")
        self.store.create("approvals", {
            "run_id": run_id, "kind": "PROTECTED_BRANCH_MERGE", "status": "PENDING",
            "requested_at": "2026-01-01T00:00:00+00:00", "decided_at": None, "decided_by": None,
            "reason": None, "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:00+00:00",
        })
        self.assertTrue(any(i["id"] == f"run:{run_id}" for i in queue.list_pending()))
        queue.decide(f"run:{run_id}", "approve", "Aryan")
        remaining = [i for i in queue.list_pending() if i["id"] == f"run:{run_id}"]
        self.assertEqual(remaining, [], "a decided run must not linger in the pending queue")

    def test_needs_aryan_excludes_a_run_that_has_already_terminally_failed(self):
        # QA regression: found on the real production database during an
        # independent verification pass -- a run whose supervisor_state was
        # never cleaned up after the run itself moved to a terminal status
        # (with no pending merge decision) stayed in this queue forever,
        # showing as "pending" when there was nothing anyone could ever do
        # about it. Nothing about the run/supervisor_state/audit trail
        # changes here -- only whether it's *listed* as pending.
        audit = AuditLog(self.state_dir / "audit.jsonl")
        queue = NeedsAryanQueue(self.store, audit, control=self.control)
        for terminal_status in ("FAILED", "DONE_CANDIDATE", "CANCELLED", "QUARANTINED"):
            run_id = self._seed_run_needing_approval(category="VERIFICATION_FAILURE", run_status=terminal_status)
            pending = queue.list_pending()
            self.assertFalse(
                any(item["id"] == f"run:{run_id}" for item in pending),
                f"a {terminal_status} run with no pending merge must not clutter the Needs Aryan queue",
            )

    def test_needs_aryan_still_surfaces_a_non_terminal_run_with_no_pending_merge(self):
        # The fix above must not become overly broad: a run that is simply
        # not yet at the merge gate (e.g. still AWAITING_APPROVAL) is real,
        # active, unresolved work and must keep showing up as inspect-only,
        # exactly as test_needs_aryan_surfaces_engineering_mission_as_inspect_only_when_not_at_merge_gate
        # already covers for one status -- this checks PAUSED too, since
        # Falguna Engineering treats paused runs as resumable, not dead.
        audit = AuditLog(self.state_dir / "audit.jsonl")
        queue = NeedsAryanQueue(self.store, audit, control=self.control)
        run_id = self._seed_run_needing_approval(category="SCOPE_EXPANSION", run_status="PAUSED")
        pending = queue.list_pending()
        self.assertTrue(any(item["id"] == f"run:{run_id}" for item in pending), "a resumable PAUSED run must still surface")

    def _seed_run_needing_approval(self, category: str, run_status: str = "AWAITING_APPROVAL") -> str:
        mission_id = self.control.create_mission("Seed mission", "Seed requirement", self.repo, RunPolicy())["mission_id"]
        requirement_id = self.store.list("requirements", "mission_id=?", (mission_id,))[0]["id"]
        task_id = self.store.list("tasks", "requirement_id=?", (requirement_id,))[0]["id"]
        run_id = self.store.create("runs", {
            "task_id": task_id, "status": run_status, "attempt": 1, "worker": "test", "model": "test",
            "worktree": None, "head_sha": None, "error": None, "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
        })
        self.store.create("supervisor_states", {
            "run_id": run_id, "outcome_class": "NEEDS_APPROVAL", "category": category, "phase": "NEEDS_ARYAN",
            "retry_allowed": 0, "resume_allowed": 1, "eligibility_reason": f"{category} requires owner review",
            "attempts_used": 1, "retry_budget": 2, "diagnostics_json": "{}",
            "decision_needed": f"Decide on {category}", "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
        })
        return run_id

    # ---------- HQ overview ----------

    def test_hq_overview_encodes_the_ownership_hierarchy(self):
        overview = hq_overview()
        self.assertIn("Aryan", overview["hierarchy"])
        self.assertIn("Twenty Two Technologies", overview["hierarchy"])
        self.assertIn("Falguna", overview["hierarchy"])
        product_names = [p["name"] for p in overview["products"]]
        self.assertIn("Falguna", product_names)
        self.assertIn("Revenue Hunter", product_names)


class RevenueHunterHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(self.repo)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "falguna@test.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Falguna Test"], check=True)
        (self.repo / "README.md").write_text("seed\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-m", "seed"], check=True, capture_output=True)
        self.control, self.store = open_control_plane(self.repo)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_rejects_payload_missing_required_fields(self):
        with self.assertRaises(ValueError):
            validate_handoff_payload({"title": "Only a title"})

    def test_rejects_repository_that_is_not_a_real_git_checkout(self):
        with self.assertRaises(ValueError):
            validate_handoff_payload({"title": "T", "requirement": "R", "repository": "/nonexistent/path/xyz"})

    def test_accepts_valid_payload_and_creates_a_real_mission(self):
        payload = {
            "title": "Onboard automation for Halcyon Fitness",
            "requirement": "Automate the welcome-email + Slack-alert workflow described in the won proposal.",
            "repository": str(self.repo),
            "source_opportunity_id": "opp-123",
            "client_name": "Halcyon Fitness",
        }
        result = accept_revenue_hunter_handoff(self.control, payload)
        self.assertIn("mission_id", result)
        mission = self.store.get("missions", result["mission_id"])
        self.assertIsNotNone(mission)
        self.assertEqual(mission["title"], payload["title"])

    def test_no_mission_is_created_when_payload_is_invalid(self):
        before = len(self.store.list("missions"))
        with self.assertRaises(ValueError):
            accept_revenue_hunter_handoff(self.control, {"title": "Missing fields"})
        after = len(self.store.list("missions"))
        self.assertEqual(before, after, "an invalid payload must never create a partial/fake mission")


if __name__ == "__main__":
    unittest.main()
