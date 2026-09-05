import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from falguna.audit import AuditLog
from falguna.models import CommandSpec, ReviewResult, RunPolicy, WorkerResult
from falguna.policy import PermissionEngine, PolicyViolation
from falguna.browser import BrowserDiscovery
from falguna.capabilities import TerminalCapability
from falguna.review import CalibrationCase, ModelSemanticReviewer, ReviewerCalibrator, ScriptedSemanticReviewer, SemanticIndependentReviewer
from falguna.runtime import open_control_plane
from falguna.workers import ScriptedWorker, StructuredEditWorker
from falguna.gateway import OpenAICompatibleGateway
from falguna.codex_transport import CodexCliJSONTransport
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
        self.control.reviewer = ScriptedSemanticReviewer(lambda requirement, diff, changed, verification: ReviewResult(
            True,
            "requirement satisfied with bounded scope and passing regression evidence",
            [],
            {
                "requirement_satisfaction": {"passed": True, "evidence": "diff sets required value"},
                "scope_compliance": {"passed": True, "evidence": "one permitted file"},
                "regression_evidence": {"passed": verification["passed"], "evidence": "native suite passed"},
                "unresolved_uncertainty": {"passed": True, "evidence": "none"},
            },
            [],
        ))

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

    def test_browser_discovery_supports_nested_manifest_without_fixed_executable(self):
        web = self.repo / "web"
        web.mkdir()
        (web / "package.json").write_text(json.dumps({"scripts": {"test:e2e": "playwright test"}, "devDependencies": {"@playwright/test": "1.62.1"}}))
        (web / "package-lock.json").write_text(json.dumps({"packages": {"node_modules/@playwright/test": {"version": "1.62.1"}}}))
        cli = web / "node_modules" / ".bin" / "playwright"
        cli.parent.mkdir(parents=True)
        cli.write_text("#!/bin/sh\necho 'Version 1.62.1'\n")
        cli.chmod(0o755)
        metadata = web / "node_modules" / "playwright-core" / "browsers.json"
        metadata.parent.mkdir(parents=True)
        metadata.write_text(json.dumps({"browsers": [{"name": "chromium-headless-shell", "revision": "1234"}]}))
        cache = Path(self.temp.name) / "browser-cache"
        executable = cache / "chromium_headless_shell-1234" / "chrome-headless-shell"
        executable.parent.mkdir(parents=True)
        executable.write_text("binary")
        executable.chmod(0o755)
        previous = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(cache)
        policy = RunPolicy(browser_base_url="http://127.0.0.1:4173", browser_project_roots=["web"])
        try:
            plan = BrowserDiscovery(self.repo, policy).discover()
        finally:
            if previous is None:
                os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)
            else:
                os.environ["PLAYWRIGHT_BROWSERS_PATH"] = previous
        self.assertEqual(plan.project_root, "web")
        self.assertEqual(plan.command.argv[:5], ["npm", "--prefix", "web", "run", "test:e2e"])
        self.assertNotIn(str(self.repo), plan.command.argv)
        self.assertEqual(plan.provisioning["status"], "READY_CACHED")
        self.assertFalse(plan.provisioning["network_install_required"])

    def test_browser_discovery_blocks_external_target(self):
        policy = RunPolicy(browser_base_url="https://example.com", browser_project_roots=["."])
        with self.assertRaises(PolicyViolation):
            BrowserDiscovery(self.repo, policy).discover()

    def test_semantic_review_fails_closed_despite_passing_tests(self):
        result = SemanticIndependentReviewer().review("set value to 2", "diff --git a/x b/x", ["falguna/feature.py"], {"passed": True})
        self.assertFalse(result.approved)
        self.assertIn("unresolved_uncertainty", result.dimensions)

    def test_semantic_review_rejects_missing_dimension(self):
        reviewer = ScriptedSemanticReviewer(lambda *args: ReviewResult(True, "looks fine", [], {"requirement_satisfaction": {"passed": True}}, []))
        result = reviewer.review("requirement", "diff", ["falguna/feature.py"], {"passed": True})
        self.assertFalse(result.approved)
        self.assertIn("missing required review dimensions", result.findings)

    def test_model_semantic_review_is_structured_and_metered(self):
        gateway = OpenAICompatibleGateway("test-model", "http://127.0.0.1:8765/v1", "/missing/real-key")
        dimensions = {
            "requirement_satisfaction": {"passed": True, "evidence": "assertion matches requirement"},
            "scope_compliance": {"passed": True, "evidence": "only allowed test file changed"},
            "regression_evidence": {"passed": True, "evidence": "17 tests passed"},
            "unresolved_uncertainty": {"passed": True, "evidence": "none"},
        }
        def response(config, payload, timeout):
            schema = payload["response_format"]["json_schema"]["schema"]
            self.assertIn("dimensions", schema["required"])
            return {"choices": [{"message": {"content": json.dumps({"summary": "satisfied", "blocking_findings": [], "unresolved_uncertainty": [], "dimensions": dimensions})}}], "usage": {"prompt_tokens": 200, "completion_tokens": 50}}
        result = ModelSemanticReviewer(gateway, transport=response).review("requirement", "diff", ["tests/test_bootstrap.py"], {"passed": True})
        self.assertTrue(result.approved)
        self.assertEqual(result.model_calls[0]["purpose"], "independent-semantic-review")
        self.assertGreater(result.cost_usd, 0)

    def test_reviewer_calibration_measures_false_accepts_and_false_rejects(self):
        def callback(requirement, diff, changed, verification):
            approved = "complete" in requirement and "unsafe" not in diff
            dimensions = {
                "requirement_satisfaction": {"passed": approved, "evidence": "labeled fixture evidence"},
                "scope_compliance": {"passed": "unsafe" not in diff, "evidence": "bounded fixture scope"},
                "regression_evidence": {"passed": verification["passed"], "evidence": "fixture verification"},
                "unresolved_uncertainty": {"passed": approved, "evidence": "none" if approved else "fixture incomplete"},
            }
            return ReviewResult(approved, "fixture verdict", [] if approved else ["candidate incomplete or unsafe"], dimensions, [] if approved else ["fixture uncertainty"])
        cases = [
            CalibrationCase("correct", True, "complete change", "safe diff", ["falguna/x.py"], {"passed": True}),
            CalibrationCase("incomplete", False, "partial change", "safe diff", ["falguna/x.py"], {"passed": True}),
            CalibrationCase("unsafe", False, "complete change", "unsafe policy bypass", ["falguna/x.py"], {"passed": True}),
        ]
        result = ReviewerCalibrator(ScriptedSemanticReviewer(callback)).run(cases)
        self.assertTrue(result["passed"])
        self.assertEqual((result["false_accepts"], result["false_rejects"]), (0, 0))
        self.assertNotIn("reasoning", result["cases"][0])

    def test_codex_transport_extracts_strict_json_and_subscription_metadata(self):
        def runner(argv, **kwargs):
            output = Path(argv[argv.index("--output-last-message") + 1])
            output.write_text(json.dumps({"summary": "ok"}))
            return subprocess.CompletedProcess(argv, 0, '{"usage":{"input_tokens":12,"output_tokens":3}}\n', '')
        transport = CodexCliJSONTransport(Path("/bin/echo"), Path(self.temp.name), runner=runner)
        decoded = transport({"model": "test"}, {"messages": [{"role": "user", "content": "test"}], "response_format": {"json_schema": {"schema": {"type": "object"}}}}, 30)
        self.assertEqual(decoded["usage"]["prompt_tokens"], 12)
        self.assertEqual(decoded["_falguna_cost_usd"], 0.0)
        self.assertEqual(decoded["_falguna_provider"], "codex-cli-subscription")

    def test_command_policy_blocks_inline_code_and_external_resource(self):
        terminal = TerminalCapability(PermissionEngine(self.repo, self.policy))
        with self.assertRaises(PolicyViolation):
            terminal.run(CommandSpec(["python3", "-c", "print('unsafe')"]))
        with self.assertRaises(PolicyViolation):
            terminal.run(CommandSpec(["npx", "tool", "https://example.com/resource"]))

    def test_isolation_allows_repo_test_and_records_backend(self):
        terminal = TerminalCapability(PermissionEngine(self.repo, self.policy))
        completed = terminal.run(CommandSpec(["python3", "-m", "unittest", "discover", "-s", "tests", "-v"], 60, "tests"))
        self.assertNotEqual(terminal.last_isolation_evidence.backend, "")
        self.assertIn(terminal.last_isolation_evidence.network_mode, {"deny", "loopback"})
        self.assertEqual(completed.returncode, 1)  # fixture intentionally fails before the worker edit

    def test_command_network_mode_is_restricted(self):
        terminal = TerminalCapability(PermissionEngine(self.repo, self.policy))
        with self.assertRaises(PolicyViolation):
            terminal.run(CommandSpec(["python3", "probe.py"], 30, "bad-network", "external"))

    def test_macos_isolation_blocks_write_outside_worktree_and_scrubs_secret(self):
        probe = self.repo / "probe.py"
        marker = Path("/tmp/falguna-v02-prohibited-write")
        if marker.exists():
            marker.unlink()
        probe.write_text("import os\nfrom pathlib import Path\nassert 'FALGUNA_HOST_SECRET_TEST' not in os.environ\nPath('/tmp/falguna-v02-prohibited-write').write_text('blocked')\n")
        os.environ["FALGUNA_HOST_SECRET_TEST"] = "must-not-cross-boundary"
        try:
            terminal = TerminalCapability(PermissionEngine(self.repo, self.policy))
            completed = terminal.run(CommandSpec(["python3", "probe.py"], 30, "containment-probe"))
        finally:
            os.environ.pop("FALGUNA_HOST_SECRET_TEST", None)
        if terminal.last_isolation_evidence.backend in {"macos-seatbelt", "inherited-macos-seatbelt"}:
            self.assertNotEqual(completed.returncode, 0)
            self.assertFalse(marker.exists())
        else:
            self.skipTest("host has no native filesystem sandbox; degraded mode is documented")


if __name__ == "__main__":
    unittest.main()
