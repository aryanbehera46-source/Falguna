import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from falguna.audit import AuditLog
from falguna.models import RunPolicy, WorkerResult
from falguna.policy import PermissionEngine, PolicyViolation
from falguna.runtime import open_control_plane
from falguna.workers import ScriptedWorker, StructuredEditWorker
from falguna.gateway import OpenAICompatibleGateway
from falguna.workers import AiderWorker


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
        self.assertEqual(len(self.store.list("artifacts", "run_id=?", (run_id,))), 4)
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

    def test_verification_failure_gets_one_bounded_repair(self):
        ids = self.control.create_mission("repair", "set value to 2", self.repo, self.policy)
        calls = {"count": 0}
        def repairable(worktree, requirement, run_id):
            calls["count"] += 1
            value = 3 if calls["count"] == 1 else 2
            (worktree / "falguna" / "feature.py").write_text(f"VALUE = {value}\n")
            return WorkerResult(True, f"attempt {calls['count']}", 0)
        run_id = self.control.start(ids["task_id"], ScriptedWorker(repairable), "scripted-offline-test", "none", self.policy)
        self.assertEqual(calls["count"], 2)
        self.assertEqual(self.store.get("runs", run_id)["status"], "DONE_CANDIDATE")
        events = [json.loads(line)["event"] for line in (self.repo / ".falguna" / "audit.jsonl").read_text().splitlines()]
        self.assertIn("REPAIR_ATTEMPT", events)

    def test_local_proxy_does_not_require_real_key_file(self):
        gateway = OpenAICompatibleGateway("test-model", "http://127.0.0.1:8765/v1", "/missing/real-key")
        worker = AiderWorker(Path("/missing/aider"), gateway)
        config = worker.gateway.configuration()
        self.assertTrue(config["base_url"].startswith("http://127.0.0.1:"))

    def test_structured_worker_applies_only_declared_file(self):
        gateway = OpenAICompatibleGateway("test-model", "http://127.0.0.1:8765/v1", "/missing/real-key")
        def response(config, payload, timeout):
            self.assertEqual(set(json.loads(payload["messages"][1]["content"])["editable_files"]), {"falguna/feature.py"})
            return {
                "choices": [{"message": {"content": json.dumps({"summary": "set value", "patches": [{"path": "falguna/feature.py", "old": "VALUE = 1", "new": "VALUE = 2"}]})}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "prompt_tokens_details": {"cached_tokens": 10}},
            }
        worker = StructuredEditWorker(gateway, ["falguna/feature.py"], transport=response)
        result = worker.execute(self.repo, "set value to 2", "test-run")
        self.assertTrue(result.success)
        self.assertEqual((self.repo / "falguna" / "feature.py").read_text(), "VALUE = 2\n")
        self.assertEqual(result.model_calls[0]["metadata"]["adapter"], "structured-edit")

    def test_structured_worker_rejects_unapproved_path(self):
        gateway = OpenAICompatibleGateway("test-model", "http://127.0.0.1:8765/v1", "/missing/real-key")
        def response(config, payload, timeout):
            return {"choices": [{"message": {"content": json.dumps({"summary": "bad", "patches": [{"path": ".env", "old": "x", "new": "SECRET=x"}]})}}]}
        worker = StructuredEditWorker(gateway, ["falguna/feature.py"], transport=response)
        result = worker.execute(self.repo, "bounded edit", "test-run")
        self.assertFalse(result.success)
        self.assertFalse((self.repo / ".env").exists())

    def test_structured_worker_rejects_non_unique_patch(self):
        gateway = OpenAICompatibleGateway("test-model", "http://127.0.0.1:8765/v1", "/missing/real-key")
        def response(config, payload, timeout):
            return {"choices": [{"message": {"content": json.dumps({"summary": "ambiguous", "patches": [{"path": "falguna/feature.py", "old": " ", "new": "  "}]})}}]}
        worker = StructuredEditWorker(gateway, ["falguna/feature.py"], transport=response)
        result = worker.execute(self.repo, "bounded edit", "test-run")
        self.assertFalse(result.success)
        self.assertEqual((self.repo / "falguna" / "feature.py").read_text(), "VALUE = 1\n")


if __name__ == "__main__":
    unittest.main()
