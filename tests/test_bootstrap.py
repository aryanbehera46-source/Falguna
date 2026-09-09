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
from falguna.usability import evidence_summary, mission_view
from falguna.web import INDEX_HTML, load_profiles, validate_editable
from falguna.discovery import ProjectDiscovery


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
        self.assertEqual(len(self.store.list("artifacts", "run_id=?", (run_id,))), 5)
        self.assertEqual(len(self.store.list("task_steps", "task_id=?", (ids["task_id"],))), 5)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), run["head_sha"])
        self.assertTrue(AuditLog(self.repo / ".falguna" / "audit.jsonl").verify())

    def test_operational_view_summary_and_three_human_decisions(self):
        ids = self.control.create_mission("bounded internal task", "set value to 2", self.repo, self.policy)
        run_id = self.control.start(ids["task_id"], self.worker(), "scripted-offline-test", "none", self.policy)
        status = mission_view(self.store, self.repo / ".falguna", run_id)
        summary = evidence_summary(self.store, self.repo / ".falguna", self.control.audit, run_id)
        self.assertEqual(status["status"], "DONE_CANDIDATE")
        self.assertIn("Independent review passed", status["current_milestone"])
        self.assertEqual(summary["requirement_coverage"], "PASSED")
        self.assertEqual(summary["independent_review"], "PASSED")
        self.assertEqual(summary["files_changed"], ["falguna/feature.py"])
        self.assertEqual(summary["merge_approval"], "PENDING")
        self.assertEqual(summary["available_actions"], ["Approve Merge", "Reject", "Request Changes"])
        self.assertFalse(summary["protected_main_merge_performed"])
        self.assertTrue(summary["evidence_hashes_valid"])
        self.assertTrue(summary["audit_chain_valid"])
        self.control.decide_merge(run_id, "request-changes", "human-test", "add one edge case")
        approval = self.store.list("approvals", "run_id=?", (run_id,))[-1]
        self.assertEqual(approval["status"], "CHANGES_REQUESTED")
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.store.get("runs", run_id)["head_sha"])

    def test_failure_classification_is_actionable(self):
        ids = self.control.create_mission("bad", "make wrong edit", self.repo, self.policy)
        def wrong(worktree, requirement, run_id):
            (worktree / "falguna" / "feature.py").write_text("VALUE = 3\n")
            return WorkerResult(True, "wrong edit", 0)
        run_id = self.control.start(ids["task_id"], ScriptedWorker(wrong), "scripted-offline-test", "none", self.policy)
        failure = mission_view(self.store, self.repo / ".falguna", run_id)["failure"]
        self.assertEqual(failure["category"], "VERIFICATION_FAILURE")
        self.assertTrue(failure["action"])

    def test_worker_failure_preserves_specific_validation_detail(self):
        ids = self.control.create_mission("bad worker", "make bounded edit", self.repo, self.policy)
        failed = ScriptedWorker(lambda *_: WorkerResult(False, "Structured edit failed: model returned no patches", 65))
        run_id = self.control.start(ids["task_id"], failed, "scripted-offline-test", "none", self.policy)
        error = self.store.get("runs", run_id)["error"]
        self.assertIn("model returned no patches", error)

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
        evidence_dir = self.repo / ".falguna" / "evidence" / run_id
        first = json.loads((evidence_dir / "verification-attempt-1.json").read_text())
        second = json.loads((evidence_dir / "verification-attempt-2.json").read_text())
        final = json.loads((evidence_dir / "verification.json").read_text())
        self.assertFalse(first["passed"])
        self.assertTrue(second["passed"])
        self.assertEqual(final["attempt"], 2)
        self.assertTrue(evidence_summary(self.store, self.repo / ".falguna", self.control.audit, run_id)["evidence_hashes_valid"])
        events = [json.loads(line)["event"] for line in (self.repo / ".falguna" / "audit.jsonl").read_text().splitlines()]
        self.assertIn("REPAIR_ATTEMPT", events)

    def test_repair_is_available_after_two_worker_retries_and_stops_after_one(self):
        ids = self.control.create_mission("repair budget", "set value to 2", self.repo, self.policy)
        calls = {"count": 0}
        def delayed(worktree, requirement, run_id):
            calls["count"] += 1
            if calls["count"] == 1:
                return WorkerResult(False, "transient worker failure", 65)
            value = 3 if calls["count"] == 2 else 2
            (worktree / "falguna" / "feature.py").write_text(f"VALUE = {value}\n")
            return WorkerResult(True, f"attempt {calls['count']}", 0)
        run_id = self.control.start(ids["task_id"], ScriptedWorker(delayed), "scripted-offline-test", "none", self.policy)
        self.assertEqual(calls["count"], 3)
        self.assertEqual(self.store.get("runs", run_id)["status"], "DONE_CANDIDATE")
        repairs = self.store.list("checkpoints", "run_id=? AND stage=?", (run_id, "REPAIR_COMPLETE"))
        self.assertEqual(len(repairs), 1)

    def test_milestone_task_verification_survives_bounded_repair(self):
        ids = self.control.create_mission("milestone repair", "set value to 2", self.repo, self.policy)
        calls = {"count": 0}
        def milestone(worktree, requirement, run_id):
            calls["count"] += 1
            (worktree / "falguna" / "feature.py").write_text(f"VALUE = {3 if calls['count'] == 1 else 2}\n")
            worker.checkpoint(1, "implementation and regression")
            return WorkerResult(True, "milestone", 0)
        worker = ScriptedWorker(milestone)
        worker.checkpoint = None
        run_id = self.control.start(ids["task_id"], worker, "scripted-offline-test", "none", self.policy)
        final = json.loads((self.repo / ".falguna" / "evidence" / run_id / "verification.json").read_text())
        self.assertEqual(self.store.get("runs", run_id)["status"], "DONE_CANDIDATE")
        self.assertGreaterEqual(len(final["milestone_tasks"]), 2)
        self.assertTrue(all("per_task_verification" in item for item in final["milestone_tasks"]))

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

    def test_non_unique_old_text_replans_with_unique_anchors(self):
        target = self.repo / "falguna/feature.py"
        target.write_text("A\nVALUE = 1\nB\nVALUE = 1\nC\n")
        replies = iter([
            {"summary": "ambiguous", "patches": [{"path": "falguna/feature.py", "old": "VALUE = 1", "new": "VALUE = 2"}]},
            {"summary": "anchored", "patches": [{"path": "falguna/feature.py", "old": "VALUE = 1", "new": "VALUE = 2", "before": "B\n", "after": "\nC"}]},
        ])
        def response(config, payload, timeout):
            return {"choices": [{"message": {"content": json.dumps(next(replies))}}]}
        worker = StructuredEditWorker(OpenAICompatibleGateway("test-model", "http://127.0.0.1:1/v1", ""), ["falguna/feature.py"], transport=response)
        result = worker.execute(self.repo, "change the second value", "run")
        self.assertTrue(result.success)
        self.assertEqual(target.read_text(), "A\nVALUE = 1\nB\nVALUE = 2\nC\n")

    def test_unique_exact_patch_ignores_stale_optional_anchors(self):
        def response(config, payload, timeout):
            value = {"summary": "unique edit", "patches": [{
                "path": "falguna/feature.py", "old": "VALUE = 1", "new": "VALUE = 2",
                "before": "stale model context", "after": "also stale", "task": 1,
            }]}
            return {"choices": [{"message": {"content": json.dumps(value)}}]}
        worker = StructuredEditWorker(OpenAICompatibleGateway("test-model", "http://127.0.0.1:1/v1", ""), ["falguna/feature.py"], transport=response)
        result = worker.execute(self.repo, "deterministic unique edit", "run")
        self.assertTrue(result.success)
        self.assertEqual((self.repo / "falguna" / "feature.py").read_text(), "VALUE = 2\n")

    def test_stale_old_text_after_prior_edit_replans_against_latest_file(self):
        replies = iter([
            {"summary": "two edits", "patches": [
                {"path": "falguna/feature.py", "old": "VALUE = 1", "new": "VALUE = 2"},
                {"path": "falguna/feature.py", "old": "VALUE = 1", "new": "VALUE = 3"},
            ]},
            {"summary": "latest state", "patches": [{"path": "falguna/feature.py", "old": "VALUE = 2", "new": "VALUE = 3"}]},
        ])
        def response(config, payload, timeout):
            return {"choices": [{"message": {"content": json.dumps(next(replies))}}]}
        worker = StructuredEditWorker(OpenAICompatibleGateway("test-model", "http://127.0.0.1:1/v1", ""), ["falguna/feature.py"], transport=response)
        result = worker.execute(self.repo, "sequential update", "run")
        self.assertTrue(result.success)
        self.assertEqual((self.repo / "falguna/feature.py").read_text(), "VALUE = 3\n")

    def test_two_sequential_edits_to_same_file_see_latest_content(self):
        def response(config, payload, timeout):
            value = {"summary": "two edits", "patches": [
                {"path": "falguna/feature.py", "old": "VALUE = 1", "new": "VALUE = 2"},
                {"path": "falguna/feature.py", "old": "VALUE = 2", "new": "VALUE = 4"},
            ]}
            return {"choices": [{"message": {"content": json.dumps(value)}}]}
        result = StructuredEditWorker(OpenAICompatibleGateway("test-model", "http://127.0.0.1:1/v1", ""), ["falguna/feature.py"], transport=response).execute(self.repo, "two changes", "run")
        self.assertTrue(result.success)
        self.assertEqual((self.repo / "falguna/feature.py").read_text(), "VALUE = 4\n")

    def test_noop_patch_is_rejected_after_bounded_replans(self):
        def response(config, payload, timeout):
            value = {"summary": "noop", "patches": [{"path": "falguna/feature.py", "old": "VALUE = 1", "new": "VALUE = 1"}]}
            return {"choices": [{"message": {"content": json.dumps(value)}}]}
        result = StructuredEditWorker(OpenAICompatibleGateway("test-model", "http://127.0.0.1:1/v1", ""), ["falguna/feature.py"], transport=response).execute(self.repo, "change", "run")
        self.assertFalse(result.success)
        self.assertIn("PATCH_NOOP", result.summary)

    def test_milestone_checkpoints_between_up_to_three_tasks(self):
        checkpoints = []
        def response(config, payload, timeout):
            value = {"summary": "milestone", "patches": [
                {"task": 1, "path": "falguna/feature.py", "old": "VALUE = 1", "new": "VALUE = 2"},
                {"task": 2, "path": "falguna/feature.py", "old": "VALUE = 2", "new": "VALUE = 3"},
            ]}
            return {"choices": [{"message": {"content": json.dumps(value)}}]}
        worker = StructuredEditWorker(OpenAICompatibleGateway("test-model", "http://127.0.0.1:1/v1", ""), ["falguna/feature.py"], transport=response, checkpoint=lambda ordinal, summary: checkpoints.append((ordinal, summary)))
        self.assertTrue(worker.execute(self.repo, "milestone", "run").success)
        self.assertEqual([item[0] for item in checkpoints], [1, 2])

    def test_scope_expansion_requires_approval(self):
        def response(config, payload, timeout):
            value = {"summary": "expand", "patches": [{"path": "outside.py", "old": "x", "new": "y"}]}
            return {"choices": [{"message": {"content": json.dumps(value)}}]}
        result = StructuredEditWorker(OpenAICompatibleGateway("test-model", "http://127.0.0.1:1/v1", ""), ["falguna/feature.py"], transport=response).execute(self.repo, "expand", "run")
        self.assertFalse(result.success)
        self.assertIn("SCOPE_EXPANSION_REQUIRED", result.summary)

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
            self.assertIn("scope compliance from changed lines", payload["messages"][0]["content"])
            self.assertIn("normalization of stale fixture dates", payload["messages"][0]["content"])
            self.assertIn("when it says A or B", payload["messages"][0]["content"])
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
            context_root = Path(argv[argv.index("-C") + 1])
            self.assertNotEqual(context_root, self.repo.resolve())
            self.assertEqual((context_root / "falguna/feature.py").read_text(), "VALUE = 1\n")
            self.assertFalse((context_root / "tests/test_feature.py").exists())
            output = Path(argv[argv.index("--output-last-message") + 1])
            output.write_text(json.dumps({"summary": "ok"}))
            return subprocess.CompletedProcess(argv, 0, '{"usage":{"input_tokens":12,"output_tokens":3}}\n', '')
        transport = CodexCliJSONTransport(Path("/bin/echo"), Path(self.temp.name), runner=runner)
        decoded = transport({"model": "gpt-5.6-luna", "_falguna_worktree": str(self.repo), "_falguna_editable_files": ["falguna/feature.py"]}, {"messages": [{"role": "user", "content": "test"}], "response_format": {"json_schema": {"schema": {"type": "object"}}}}, 30)
        self.assertEqual(decoded["usage"]["prompt_tokens"], 12)
        self.assertEqual(decoded["_falguna_cost_usd"], 0.0)
        self.assertEqual(decoded["_falguna_provider"], "codex-cli-subscription")

    def test_codex_transport_preserves_stdout_error_when_stderr_is_empty(self):
        def runner(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 1, "The model is not supported when using Codex with a ChatGPT account.", "")
        transport = CodexCliJSONTransport(Path("/bin/echo"), Path(self.temp.name), runner=runner)
        with self.assertRaisesRegex(OSError, "model is not supported"):
            transport({"model": "gpt-5.6-luna"}, {"messages": [{"role": "user", "content": "test"}], "response_format": {"json_schema": {"schema": {"type": "object"}}}}, 30)

    def test_unsupported_model_is_rejected_before_transport_call(self):
        calls = {"count": 0}
        def runner(argv, **kwargs):
            calls["count"] += 1
        transport = CodexCliJSONTransport(Path("/bin/echo"), Path(self.temp.name), runner=runner)
        with self.assertRaisesRegex(OSError, "MODEL_UNSUPPORTED"):
            transport({"model": "gpt-5.4-mini"}, {"messages": [{"role": "user", "content": "test"}], "response_format": {"json_schema": {"schema": {"type": "object"}}}}, 30)
        self.assertEqual(calls["count"], 0)

    def test_web_uses_lowest_supported_codex_subscription_model(self):
        from falguna.web import MODEL
        self.assertEqual(MODEL, "gpt-5.6-luna")

    def test_structured_worker_gives_codex_transport_the_isolated_worktree(self):
        gateway = OpenAICompatibleGateway("test-model", "http://127.0.0.1:8765/v1", "/missing/real-key")

        def response(config, payload, timeout):
            self.assertEqual(Path(config["_falguna_worktree"]), self.repo.resolve())
            self.assertEqual(config["_falguna_editable_files"], ["falguna/feature.py"])
            self.assertEqual(config["_falguna_context_files"]["falguna/feature.py"], "VALUE = 1\n")
            return {"choices": [{"message": {"content": json.dumps({"summary": "bounded", "patches": [{"path": "falguna/feature.py", "old": "VALUE = 1", "new": "VALUE = 2"}]})}}]}
        response.uses_workspace_context = True

        result = StructuredEditWorker(gateway, ["falguna/feature.py"], transport=response).execute(self.repo, "bounded edit", "test-run")
        self.assertTrue(result.success)

    def test_workspace_transport_does_not_duplicate_full_file_contents_in_prompt(self):
        gateway = OpenAICompatibleGateway("test-model", "http://127.0.0.1:8765/v1", "/missing/real-key")

        class WorkspaceResponse:
            uses_workspace_context = True

            def __call__(self, config, payload, timeout):
                self_test.assertIn("focused regression assertion", payload["messages"][0]["content"])
                self_test.assertIn("source-text includes or regex assertion alone is insufficient", payload["messages"][0]["content"])
                self_test.assertIn("stale hardcoded calendar dates", payload["messages"][0]["content"])
                self_test.assertIn("repair the reported verification failure", payload["messages"][0]["content"])
                supplied = json.loads(payload["messages"][1]["content"])["editable_files"]
                self_test.assertEqual(supplied, ["falguna/feature.py"])
                return {"choices": [{"message": {"content": json.dumps({"summary": "bounded", "patches": [{"path": "falguna/feature.py", "old": "VALUE = 1", "new": "VALUE = 2"}]})}}]}

        self_test = self
        result = StructuredEditWorker(gateway, ["falguna/feature.py"], transport=WorkspaceResponse()).execute(self.repo, "bounded edit", "test-run")
        self.assertTrue(result.success)

    def test_workspace_context_excerpts_large_files_around_objective_terms(self):
        content = "".join(f"unrelated line {index}\n" for index in range(3000)) + "reservation status preservation defect\n" + "tail\n" * 3000
        excerpt = StructuredEditWorker._context_excerpt(content, "fix reservation status preservation")
        self.assertLessEqual(len(excerpt), 50000)
        self.assertIn("reservation status preservation defect", excerpt)
        self.assertNotEqual(excerpt, content)

    def test_structured_patch_schema_is_strict_runtime_compatible(self):
        gateway = OpenAICompatibleGateway("test-model", "http://127.0.0.1:1/v1", "")
        def response(config, payload, timeout):
            item = payload["response_format"]["json_schema"]["schema"]["properties"]["patches"]["items"]
            self.assertEqual(set(item["required"]), set(item["properties"]))
            value = {"summary": "edit", "patches": [{"path": "falguna/feature.py", "old": "VALUE = 1", "new": "VALUE = 2", "before": "", "after": "", "task": 1}]}
            return {"choices": [{"message": {"content": json.dumps(value)}}]}
        result = StructuredEditWorker(gateway, ["falguna/feature.py"], transport=response).execute(self.repo, "edit", "run")
        self.assertTrue(result.success)

    def test_stale_profile_root_is_actionable(self):
        with self.assertRaisesRegex(ValueError, "PROFILE_STALE"):
            ProjectDiscovery(self.repo, {"discovery_roots": ["missing"]}).discover("bounded objective")

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

    def test_local_web_shell_exposes_required_operator_controls(self):
        for label in (
            "Falguna", "internal alpha", "Approved projects", "Recent missions",
            "Run mission", "Engineering work mode", "Needs approval",
            "Approve", "Reject", "Request Changes", "Resume",
            "What should we build?", "Human approval stays required",
        ):
            self.assertIn(label, INDEX_HTML)

    def test_web_editable_scope_rejects_paths_outside_project(self):
        self.assertEqual(validate_editable(["falguna/web.py", "tests/*.py"]), ["falguna/web.py", "tests/*.py"])
        for unsafe in (["../outside.py"], ["/tmp/outside.py"], [""]):
            with self.assertRaises(ValueError):
                validate_editable(unsafe)

    def test_approved_project_profile_is_bounded_and_local(self):
        profiles = load_profiles(Path(__file__).parents[1])
        self.assertIn("falguna-engineering", [profile["id"] for profile in profiles])
        self.assertIn("serviceflow", [profile["id"] for profile in profiles])
        profile = next(item for item in profiles if item["id"] == "falguna-engineering")
        self.assertEqual(profile["default_budget_usd"], 0.05)
        self.assertEqual(profile["verification_profiles"]["native"], ["python3 -m unittest discover -s tests -v"])

    def test_project_discovery_proposes_scope_and_native_verification(self):
        (self.repo / "package.json").write_text(json.dumps({"scripts": {"test": "node --test", "start": "node server.js"}}))
        (self.repo / "server.js").write_text("function normalizeAppointmentCode(code) { return code.toUpperCase(); }\n")
        (self.repo / "tests" / "workflow.test.js").write_text("test appointment code update accepts lowercase\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-m", "discovery fixture"], check=True, capture_output=True)
        plan = ProjectDiscovery(self.repo, {"discovery_roots": ["server.js", "tests"]}).discover(
            "Make appointment code updates accept lowercase with regression coverage"
        )
        self.assertEqual(plan.confidence, "HIGH")
        self.assertFalse(plan.requires_approval)
        self.assertIn("server.js", plan.editable_files)
        self.assertIn("tests/workflow.test.js", plan.editable_files)
        self.assertEqual(plan.verification_commands[0].argv, ["npm", "run", "test"])
        self.assertEqual(plan.verification_commands[0].network_mode, "loopback")

    def test_project_discovery_fails_closed_when_scope_is_uncertain(self):
        (self.repo / "package.json").write_text(json.dumps({"scripts": {"start": "node server.js"}}))
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-m", "uncertain fixture"], check=True, capture_output=True)
        plan = ProjectDiscovery(self.repo, {"discovery_roots": ["falguna"]}).discover("change unrelated frobnicator behavior")
        self.assertEqual(plan.confidence, "UNCERTAIN")
        self.assertTrue(plan.requires_approval)

    def test_royal_table_nested_profile_discovers_admin_reservation_scope(self):
        (self.repo / "admin.html").write_text("admin reservation status update\n")
        (self.repo / "ui.js").write_text("reservation ui\n")
        (self.repo / "server" / "server.js").parent.mkdir()
        (self.repo / "server" / "server.js").write_text("update reservation status and preserve reservation state\n")
        (self.repo / "server" / "reservation-smoke-test.js").write_text("test admin reservation status update\n")
        (self.repo / "server" / "package.json").write_text(json.dumps({"scripts": {"test": "node reservation-smoke-test.js"}}))
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-m", "royal table fixture"], check=True, capture_output=True)

        plan = ProjectDiscovery(self.repo, {
            "discovery_roots": ["admin.html", "ui.js", "server"],
            "package_root": "server",
        }).discover("Inspect the admin reservation status-update workflow and preserve reservation state")

        self.assertEqual(plan.confidence, "HIGH")
        self.assertFalse(plan.requires_approval)
        self.assertIn("admin.html", plan.editable_files)
        self.assertIn("server/reservation-smoke-test.js", plan.editable_files)
        self.assertEqual(plan.verification_commands[0].argv, ["npm", "--prefix", "server", "run", "test"])
        self.assertEqual(plan.verification_commands[0].network_mode, "loopback")

    def test_discovery_runs_the_npm_script_for_the_selected_test_file(self):
        (self.repo / "admin.html").write_text("admin reservation status update\n")
        (self.repo / "server").mkdir()
        (self.repo / "server/final-qa-test.js").write_text("reservation status update final qa\n")
        (self.repo / "server/package.json").write_text(json.dumps({"scripts": {"test": "node old-smoke-test.js", "test:final-qa": "node final-qa-test.js"}}))
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-m", "selected test fixture"], check=True, capture_output=True)

        plan = ProjectDiscovery(self.repo, {"discovery_roots": ["admin.html", "server"], "package_root": "server"}).discover("admin reservation status update")

        self.assertEqual(plan.editable_files, ["admin.html", "server/final-qa-test.js"])
        self.assertEqual(plan.verification_commands[0].argv, ["npm", "--prefix", "server", "run", "test:final-qa"])

    def test_discovery_rejects_unsafe_approved_package_root(self):
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-m", "discovery fixture"], check=True, capture_output=True)
        with self.assertRaisesRegex(ValueError, "package_root"):
            ProjectDiscovery(self.repo, {"package_root": "../outside"}).discover("bounded objective")

    def test_discovery_rejects_invalid_verification_command(self):
        (self.repo / "package.json").write_text(json.dumps({"scripts": {"test": ""}}))
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-m", "invalid verification fixture"], check=True, capture_output=True)
        with self.assertRaisesRegex(ValueError, "VERIFY_COMMAND_INVALID"):
            ProjectDiscovery(self.repo, {"discovery_roots": ["."]}).discover("bounded objective")

    def test_macos_launcher_preserves_local_only_start_and_safe_stop(self):
        root = Path(__file__).parents[1]
        start = (root / "launcher/Falguna.app/Contents/MacOS/Falguna").read_text()
        stop = (root / "launcher/Stop Falguna.app/Contents/MacOS/Stop Falguna").read_text()
        self.assertIn("--host 127.0.0.1 --port 8765", start)
        self.assertIn('is_falguna_ready', start)
        self.assertIn('/usr/bin/open "$URL"', start)
        self.assertIn('server.pid', start)
        self.assertIn('"-m falguna"', stop)
        self.assertIn('/bin/kill -TERM "$pid"', stop)
        self.assertNotIn("kill -KILL", stop)


if __name__ == "__main__":
    unittest.main()
