import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from falguna.audit import AuditLog
from falguna.models import RunPolicy, WorkerResult
from falguna.policy import PermissionEngine, PolicyViolation
from falguna.runtime import open_control_plane
from falguna.workers import ScriptedWorker


def git(repo: Path, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(self.repo)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "falguna@test.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Falguna Test"], check=True)
        (self.repo / "falguna").mkdir()
        (self.repo / "falguna" / "feature.py").write_text("VALUE = 1\n")
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "test_feature.py").write_text("import unittest\nfrom falguna.feature import VALUE\nclass T(unittest.TestCase):\n def test_value(self): self.assertEqual(VALUE, 2)\n")
        (self.repo / "falguna" / "__init__.py").write_text("")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-m", "seed"], check=True, capture_output=True)
        self.control, self.store = open_control_plane(self.repo)
        self.policy = RunPolicy()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def worker(self):
        def edit(worktree, requirement, run_id):
            (worktree / "falguna" / "feature.py").write_text("VALUE = 2\n")
            return WorkerResult(True, "bounded edit", 0)
        return ScriptedWorker(edit)

    def test_end_to_end_done_candidate_requires_pending_human_approval(self):
        ids = self.control.create_mission("self change", "set value to 2", self.repo, self.policy)
        run_id = self.control.start(ids["task_id"], self.worker(), "scripted-offline-test", "none", self.policy)
        run = self.store.get("runs", run_id)
        self.assertEqual(run["status"], "DONE_CANDIDATE")
        approvals = self.store.list("approvals", "run_id=?", (run_id,))
        self.assertEqual(approvals[0]["status"], "PENDING")
        self.assertEqual(len(self.store.list("artifacts", "run_id=?", (run_id,))), 3)
        self.assertEqual(len(self.store.list("task_steps", "task_id=?", (ids["task_id"],))), 5)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), run["head_sha"])
        self.assertTrue(AuditLog(self.repo / ".falguna" / "audit.jsonl").verify())

    def test_forced_interruption_resumes_from_checkpoint(self):
        ids = self.control.create_mission("resume", "set value to 2", self.repo, self.policy)
        run_id = self.control.start(ids["task_id"], self.worker(), "scripted-offline-test", "none", self.policy, force_stop_after="WORKTREE_READY")
        self.assertEqual(self.store.latest_checkpoint(run_id)["stage"], "WORKTREE_READY")
        self.control.resume(run_id, self.worker(), self.policy)
        self.assertEqual(self.store.get("runs", run_id)["status"], "DONE_CANDIDATE")

    def test_path_escape_and_policy_change_are_quarantined(self):
        permissions = PermissionEngine(self.repo, self.policy)
        with self.assertRaises(PolicyViolation):
            permissions.require_write(Path("../escape.txt"))
        with self.assertRaises(PolicyViolation):
            permissions.require_write(Path("falguna/policy.py"))

    def test_audit_tampering_is_detected(self):
        audit = AuditLog(self.repo / ".falguna" / "tamper.jsonl")
        audit.append("A", {"value": 1})
        audit.append("B", {"value": 2})
        self.assertTrue(audit.verify())
        path = self.repo / ".falguna" / "tamper.jsonl"
        path.write_text(path.read_text().replace('"value": 1', '"value": 9'))
        self.assertFalse(audit.verify())

    def test_failed_verification_does_not_request_merge(self):
        ids = self.control.create_mission("bad", "make wrong edit", self.repo, self.policy)
        def wrong(worktree, requirement, run_id):
            (worktree / "falguna" / "feature.py").write_text("VALUE = 3\n")
            return WorkerResult(True, "wrong edit", 0)
        run_id = self.control.start(ids["task_id"], ScriptedWorker(wrong), "scripted-offline-test", "none", self.policy)
        self.assertEqual(self.store.get("runs", run_id)["status"], "FAILED")
        self.assertEqual(self.store.list("approvals", "run_id=?", (run_id,)), [])


if __name__ == "__main__":
    unittest.main()
