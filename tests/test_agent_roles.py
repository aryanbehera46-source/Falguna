"""Phase 1, Requirement 4: dedicated tests for falguna/agent_roles.py's five
named AI Workforce roles (SalesResearcherWorker, ProposalSpecialistWorker,
EngineeringAgentWorker, QAAgentWorker, ChiefOfStaffWorker).

Before this test file, agent_roles.py had ZERO dedicated test coverage --
tests/test_workforce_workers.py covers the *other* module,
falguna/workforce_workers.py (browser/data/document/spreadsheet/email/
content workers), a genuinely different set of classes. This file closes
that gap without redesigning anything: every worker is exercised the same
way tests/test_workforce_workers.py already exercises its own workers --
through a real WorkforceTaskStore task and a real WorkforceOrchestrator,
so routing, escalation, and the resulting real domain rows (opportunities,
qualifications, proposals, documents) are all genuinely verified, not
mocked away. Engineering/QA Agent tests use a real, temporary Falguna
Engineering control plane (via falguna.runtime.open_control_plane) but
stub out the one genuinely expensive/network-dependent step (the local
model call inside ControlPlane.start) by patching the two call sites
agent_roles.py itself uses (accept_revenue_hunter_handoff, control.start)
-- ControlPlane's own internals already have their own test coverage
elsewhere; what's being tested here is agent_roles.py's own logic (input
validation, RunPolicy construction/protected-glob fencing, honest mapping
of run status to WorkerResult, and never fabricating success).
"""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from falguna.agent_roles import (
    ChiefOfStaffWorker, EngineeringAgentWorker, ProposalSpecialistWorker,
    QAAgentWorker, SalesResearcherWorker,
)
from falguna.models import RunPolicy
from falguna.audit import AuditLog
from falguna.documents import DocumentStore
from falguna.runtime import open_control_plane
from falguna.store import StateStore
from falguna.ttt_hq import NeedsAryanQueue
from falguna.workforce import WorkforceOrchestrator, WorkforceTaskStore


class AgentRolesTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = StateStore(root / "state.db")
        self.store.migrate()
        self.audit = AuditLog(root / "audit.jsonl")
        self.needs_aryan = NeedsAryanQueue(self.store, self.audit)
        self.tasks = WorkforceTaskStore(self.store, self.audit)
        self.docs = DocumentStore(self.store, self.audit)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _orch(self, *workers):
        o = WorkforceOrchestrator(self.store, self.audit, needs_aryan=self.needs_aryan)
        for w in workers:
            o.register_worker(w)
        return o


class SalesResearcherWorkerTests(AgentRolesTestBase):
    def test_supports_only_its_own_task_type(self):
        worker = SalesResearcherWorker(self.store, self.audit)
        self.assertTrue(worker.supports("sales_lead_qualification"))
        self.assertFalse(worker.supports("proposal_drafting"))
        self.assertFalse(worker.supports(""))

    def test_ordinary_routing_creates_and_qualifies_a_real_opportunity(self):
        orch = self._orch(SalesResearcherWorker(self.store, self.audit))
        task_id = self.tasks.create(
            "sales", "Qualify a referral", "sales_lead_qualification", actor="Aryan",
            inputs={"opportunity": {"title": "Acme website rebuild", "client_name": "Acme"}},
        )
        result = orch.execute(task_id)
        self.assertEqual(result["status"], "COMPLETED")
        outputs = json.loads(result["outputs_json"])
        opportunity_id = outputs["opportunity_id"]
        self.assertIsNotNone(self.store.get("rh_opportunities", opportunity_id))
        qualifications = self.store.list("rh_qualifications", "opportunity_id=?", (opportunity_id,))
        self.assertEqual(len(qualifications), 1)
        evidence = json.loads(result["evidence_json"])
        self.assertIn("fit_score", evidence)
        self.assertIn("recommendation", evidence)

    def test_ordinary_routing_against_an_existing_opportunity_id(self):
        from falguna.revenue_hunter import OpportunityStore
        opp_id = OpportunityStore(self.store, self.audit).create({"title": "Existing lead"}, actor="Aryan")
        orch = self._orch(SalesResearcherWorker(self.store, self.audit))
        task_id = self.tasks.create("sales", "Qualify existing lead", "sales_lead_qualification", actor="Aryan",
                                     inputs={"opportunity_id": opp_id})
        result = orch.execute(task_id)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(json.loads(result["outputs_json"])["opportunity_id"], opp_id)

    def test_missing_opportunity_id_and_title_blocks_and_escalates(self):
        # Malformed/insufficient input -- must never guess a title or
        # fabricate an opportunity; it escalates to a real Needs Aryan item.
        orch = self._orch(SalesResearcherWorker(self.store, self.audit))
        task_id = self.tasks.create("sales", "Qualify a vague lead", "sales_lead_qualification", actor="Aryan",
                                     inputs={"opportunity": {}})
        result = orch.execute(task_id)
        self.assertEqual(result["status"], "NEEDS_ARYAN")
        self.assertIsNotNone(result["needs_aryan_id"])
        item = self.store.get("needs_aryan_items", result["needs_aryan_id"])
        self.assertEqual(item["kind"], "workforce_action_approval")

    def test_no_inputs_at_all_blocks_rather_than_crashes(self):
        orch = self._orch(SalesResearcherWorker(self.store, self.audit))
        task_id = self.tasks.create("sales", "Qualify with no input", "sales_lead_qualification", actor="Aryan")
        result = orch.execute(task_id)
        self.assertEqual(result["status"], "NEEDS_ARYAN")

    def test_nonexistent_opportunity_id_fails_honestly_not_silently(self):
        orch = self._orch(SalesResearcherWorker(self.store, self.audit))
        task_id = self.tasks.create("sales", "Qualify a ghost opportunity", "sales_lead_qualification", actor="Aryan",
                                     inputs={"opportunity_id": "does-not-exist"})
        result = orch.execute(task_id)
        # A single FAILED attempt with retries remaining goes back to READY,
        # not straight to escalation -- this is the orchestrator's existing,
        # already-tested bounded-retry contract (see tests/test_workforce.py);
        # what matters here is agent_roles.py's own worker never reports a
        # fabricated COMPLETED for a lead that doesn't exist.
        self.assertIn(result["status"], ("FAILED", "READY"))
        self.assertNotEqual(result["status"], "COMPLETED")

    def test_customer_isolation_between_two_independent_qualifications(self):
        orch = self._orch(SalesResearcherWorker(self.store, self.audit))
        task_a = self.tasks.create("sales", "Qualify lead A", "sales_lead_qualification", actor="Aryan",
                                    inputs={"opportunity": {"title": "Client A project"}})
        task_b = self.tasks.create("sales", "Qualify lead B", "sales_lead_qualification", actor="Aryan",
                                    inputs={"opportunity": {"title": "Client B project"}})
        result_a = orch.execute(task_a)
        result_b = orch.execute(task_b)
        opp_a = json.loads(result_a["outputs_json"])["opportunity_id"]
        opp_b = json.loads(result_b["outputs_json"])["opportunity_id"]
        self.assertNotEqual(opp_a, opp_b)
        quals_a = self.store.list("rh_qualifications", "opportunity_id=?", (opp_a,))
        quals_b = self.store.list("rh_qualifications", "opportunity_id=?", (opp_b,))
        self.assertEqual(len(quals_a), 1)
        self.assertEqual(len(quals_b), 1)
        self.assertNotEqual(quals_a[0]["id"], quals_b[0]["id"])


class ProposalSpecialistWorkerTests(AgentRolesTestBase):
    def _qualified_opportunity(self):
        orch = self._orch(SalesResearcherWorker(self.store, self.audit))
        task_id = self.tasks.create("sales", "Qualify", "sales_lead_qualification", actor="Aryan",
                                     inputs={"opportunity": {"title": "Beta Corp integration"}})
        result = orch.execute(task_id)
        self.assertEqual(result["status"], "COMPLETED")
        return json.loads(result["outputs_json"])["opportunity_id"]

    def test_supports_only_its_own_task_type(self):
        worker = ProposalSpecialistWorker(self.store, self.audit, self.docs, needs_aryan=self.needs_aryan)
        self.assertTrue(worker.supports("proposal_drafting"))
        self.assertFalse(worker.supports("sales_lead_qualification"))

    def test_ordinary_routing_drafts_a_real_proposal_and_requires_approval(self):
        # Authorization: a drafted proposal must never auto-send -- it
        # always creates a real, still-pending Needs Aryan approval item.
        opp_id = self._qualified_opportunity()
        orch = self._orch(ProposalSpecialistWorker(self.store, self.audit, self.docs, needs_aryan=self.needs_aryan))
        task_id = self.tasks.create("sales", "Draft proposal", "proposal_drafting", actor="Aryan",
                                     inputs={"opportunity_id": opp_id})
        result = orch.execute(task_id)
        self.assertEqual(result["status"], "COMPLETED")
        outputs = json.loads(result["outputs_json"])
        self.assertIsNotNone(outputs["proposal_id"])
        self.assertIsNotNone(outputs["doc_id"])
        needs_aryan_id = outputs["needs_aryan_id"]
        self.assertIsNotNone(needs_aryan_id)
        item = self.store.get("needs_aryan_items", needs_aryan_id)
        self.assertEqual(item["status"], "PENDING")
        doc = self.store.get("wf_documents", outputs["doc_id"])
        self.assertIn("Milestones", doc["content_text"])
        self.assertIn("Acceptance criteria", doc["content_text"])

    def test_missing_opportunity_id_blocks(self):
        orch = self._orch(ProposalSpecialistWorker(self.store, self.audit, self.docs, needs_aryan=self.needs_aryan))
        task_id = self.tasks.create("sales", "Draft proposal", "proposal_drafting", actor="Aryan", inputs={})
        result = orch.execute(task_id)
        self.assertEqual(result["status"], "NEEDS_ARYAN")

    def test_opportunity_not_found_fails(self):
        orch = self._orch(ProposalSpecialistWorker(self.store, self.audit, self.docs, needs_aryan=self.needs_aryan))
        task_id = self.tasks.create("sales", "Draft proposal", "proposal_drafting", actor="Aryan",
                                     inputs={"opportunity_id": "does-not-exist"})
        result = orch.execute(task_id)
        self.assertNotEqual(result["status"], "COMPLETED")

    def test_unqualified_opportunity_blocks_with_clear_next_action(self):
        from falguna.revenue_hunter import OpportunityStore
        opp_id = OpportunityStore(self.store, self.audit).create({"title": "Not yet qualified"}, actor="Aryan")
        orch = self._orch(ProposalSpecialistWorker(self.store, self.audit, self.docs, needs_aryan=self.needs_aryan))
        task_id = self.tasks.create("sales", "Draft proposal too early", "proposal_drafting", actor="Aryan",
                                     inputs={"opportunity_id": opp_id})
        result = orch.execute(task_id)
        self.assertEqual(result["status"], "NEEDS_ARYAN")
        item = self.store.get("needs_aryan_items", result["needs_aryan_id"])
        self.assertIn("Sales Researcher", item["what_is_needed"])


class ChiefOfStaffWorkerTests(AgentRolesTestBase):
    def test_supports_only_its_own_task_type(self):
        worker = ChiefOfStaffWorker(self.store, self.audit, self.docs, needs_aryan=self.needs_aryan)
        self.assertTrue(worker.supports("executive_summary"))
        self.assertFalse(worker.supports("engineering_fix"))

    def test_ordinary_routing_summarizes_only_real_stored_numbers(self):
        from falguna.revenue_hunter import OpportunityStore
        OpportunityStore(self.store, self.audit).create({"title": "Won deal example"}, actor="Aryan")
        self.needs_aryan.create_item("pricing_decision", "Discount ask", "Approve or hold?", actor="Aryan")
        orch = self._orch(ChiefOfStaffWorker(self.store, self.audit, self.docs, needs_aryan=self.needs_aryan))
        task_id = self.tasks.create("executive", "Weekly summary", "executive_summary", actor="Aryan")
        result = orch.execute(task_id)
        self.assertEqual(result["status"], "COMPLETED")
        outputs = json.loads(result["outputs_json"])
        self.assertEqual(outputs["pipeline_counts"].get("New"), 1)
        self.assertEqual(outputs["needs_aryan_pending"], 1)
        doc = self.store.get("wf_documents", outputs["doc_id"])
        self.assertIn("Executive summary", doc["content_text"])

    def test_summary_never_reports_a_needs_aryan_count_without_a_queue_wired_in(self):
        worker = ChiefOfStaffWorker(self.store, self.audit, self.docs, needs_aryan=None)
        task = {"id": "t1", "department": "executive", "objective": "Summary", "actor": "Aryan"}
        result = worker.execute(task)
        self.assertEqual(result.status, "COMPLETED")
        self.assertEqual(result.result["needs_aryan_pending"], 0)


class _FakeEngineeringControl:
    """Wraps a real, temporary ControlPlane (real store/audit/state_root,
    and a real FK-valid mission/requirement/task row created the exact
    same way create_mission() always does) but replaces only the one call
    agent_roles.py itself makes into it (start()) with a scripted
    stand-in -- so EngineeringAgentWorker's own input-validation/
    RunPolicy/status-mapping logic is exercised for real, without needing
    a reachable local model gateway (none is running in this test
    environment, which is itself the realistic "unavailable external
    action" this worker must handle without crashing or fabricating
    success)."""

    def __init__(self, control, run_status, run_error=None, worktree="/fake/worktree", raise_exc=None, repo_path=None):
        self._control = control
        self.store = control.store
        self.audit = control.audit
        self.state_root = control.state_root
        self._raise_exc = raise_exc
        self._run_id = None
        self.task_id = control.create_mission("Fake eng task", "Fake requirement", repo_path or ".", RunPolicy())["task_id"]
        if raise_exc is None:
            now = "2026-01-01T00:00:00+00:00"
            self._run_id = self.store.create("runs", {
                "task_id": self.task_id, "status": run_status, "attempt": 1,
                "worker": "structured_edit_local", "model": "fake-model", "worktree": worktree,
                "head_sha": None, "error": run_error, "created_at": now, "updated_at": now,
            })

    def __setattr__(self, name, value):
        # EngineeringAgentWorker sets self.control.reviewer -- harmless, just record it.
        object.__setattr__(self, name, value)

    def start(self, task_id, worker, worker_name=None, model=None, policy=None):
        self.last_policy = policy
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._run_id


class EngineeringAgentWorkerTests(AgentRolesTestBase):
    def setUp(self):
        super().setUp()
        self._control_tmp = tempfile.TemporaryDirectory()
        self.control, _ = open_control_plane(Path(self._control_tmp.name))
        self._repo_tmp = tempfile.TemporaryDirectory()
        self.repo_path = Path(self._repo_tmp.name)
        subprocess.run(["git", "init", "-q", str(self.repo_path)], check=True)

    def tearDown(self):
        self.control.store.close()
        self._control_tmp.cleanup()
        self._repo_tmp.cleanup()
        super().tearDown()

    def _task(self, inputs):
        return {"id": "eng-task-1", "department": "engineering", "objective": "Fix a bug", "actor": "Aryan",
                "inputs_json": json.dumps(inputs)}

    def test_supports_only_its_own_task_type(self):
        worker = EngineeringAgentWorker(self.control)
        self.assertTrue(worker.supports("engineering_fix"))
        self.assertFalse(worker.supports("qa_independent_verification"))

    def test_missing_required_inputs_blocks_without_touching_the_control_plane(self):
        worker = EngineeringAgentWorker(self.control)
        result = worker.execute(self._task({}))
        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("missing required inputs", result.blockers[0])

    def test_non_git_repository_fails_honestly(self):
        worker = EngineeringAgentWorker(self.control)
        with tempfile.TemporaryDirectory() as not_a_repo:
            result = worker.execute(self._task({
                "repository": not_a_repo, "requirement": "Fix it",
                "editable_files": ["a.py"], "verification_commands": [{"argv": ["true"]}],
            }))
        self.assertEqual(result.status, "FAILED")
        self.assertIn("not a local git checkout", result.next_action)

    def test_protected_globs_exclude_a_default_glob_matched_by_an_editable_file(self):
        # Phase 1, Requirement 4 defect found by this test suite: the
        # original filter (`g not in editable_files`) compared each default
        # protected glob against editable_files with plain string
        # membership, so it only ever excluded a protected glob if a caller
        # listed that EXACT glob pattern itself (e.g. the literal string
        # "server/**") -- listing a real, concrete file that merely falls
        # under a protected pattern (e.g. "server/config.py") never removed
        # "server/**" from protected_globs. Since PermissionEngine.
        # require_write() (falguna/policy.py) checks protected_globs BEFORE
        # allowed_write_globs and protected always wins, that concrete,
        # explicitly-authorized file was silently blocked anyway --
        # directly contradicting this code's own comment ("a file the
        # caller explicitly listed as editable is allowed even if it would
        # otherwise match one of the broad protected globs"). Fixed to use
        # the same fnmatch semantics require_write() itself uses.
        fake_control = _FakeEngineeringControl(self.control, repo_path=self.repo_path, run_status="DONE_CANDIDATE")
        with patch("falguna.agent_roles.accept_revenue_hunter_handoff", return_value={"task_id": fake_control.task_id, "mission_id": "fake-mission"}):
            worker = EngineeringAgentWorker(fake_control)
            result = worker.execute(self._task({
                "repository": str(self.repo_path), "requirement": "Edit server config",
                "editable_files": ["server/config.py"], "verification_commands": [{"argv": ["true"]}],
            }))
        self.assertEqual(result.status, "COMPLETED")
        policy = fake_control.last_policy
        self.assertNotIn("server/**", policy.protected_globs)
        self.assertIn(".git/**", policy.protected_globs)  # untouched defaults remain

    def test_protected_globs_fix_does_not_widen_access_beyond_the_explicit_file(self):
        # The fix above must not become a way to unlock an entire protected
        # tree by listing just one file under it: allowed_write_globs stays
        # scoped to exactly editable_files, so a real PermissionEngine still
        # allows the one authorized file and still refuses everything else
        # that used to live behind "server/**".
        from falguna.policy import PermissionEngine, PolicyViolation
        fake_control = _FakeEngineeringControl(self.control, repo_path=self.repo_path, run_status="DONE_CANDIDATE")
        with patch("falguna.agent_roles.accept_revenue_hunter_handoff", return_value={"task_id": fake_control.task_id, "mission_id": "fake-mission"}):
            worker = EngineeringAgentWorker(fake_control)
            result = worker.execute(self._task({
                "repository": str(self.repo_path), "requirement": "Edit server config",
                "editable_files": ["server/config.py"], "verification_commands": [{"argv": ["true"]}],
            }))
        self.assertEqual(result.status, "COMPLETED")
        policy = fake_control.last_policy
        engine = PermissionEngine(self.repo_path, policy)
        (self.repo_path / "server").mkdir(exist_ok=True)
        (self.repo_path / "server" / "config.py").write_text("x = 1\n")
        (self.repo_path / "server" / "other.py").write_text("y = 2\n")
        engine.require_write("server/config.py")  # explicitly authorized -- must not raise
        with self.assertRaises(PolicyViolation):
            engine.require_write("server/other.py")  # still fenced -- never authorized

    def test_done_candidate_completes_with_pending_merge_approval_never_merged(self):
        fake_control = _FakeEngineeringControl(self.control, repo_path=self.repo_path, run_status="DONE_CANDIDATE")
        approval_id = fake_control.store.create("approvals", {
            "run_id": fake_control._run_id, "kind": "PROTECTED_BRANCH_MERGE", "status": "PENDING",
            "requested_at": "2026-01-01T00:00:00+00:00", "decided_at": None, "decided_by": None, "reason": None,
            "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:00+00:00",
        })
        with patch("falguna.agent_roles.accept_revenue_hunter_handoff", return_value={"task_id": fake_control.task_id, "mission_id": "fake-mission"}):
            worker = EngineeringAgentWorker(fake_control)
            result = worker.execute(self._task({
                "repository": str(self.repo_path), "requirement": "Fix the bug",
                "editable_files": ["app.py"], "verification_commands": [{"argv": ["true"]}],
            }))
        self.assertEqual(result.status, "COMPLETED")
        self.assertEqual(result.evidence["approval_status"], "PENDING")
        self.assertIn("not merged", result.evidence["note"])
        self.assertIn("PENDING", result.next_action or "" if False else str(result.next_action))
        # Authorization: nothing here ever flips that approval itself.
        self.assertEqual(fake_control.store.get("approvals", approval_id)["status"], "PENDING")

    def test_awaiting_approval_run_reports_blocked_not_completed(self):
        fake_control = _FakeEngineeringControl(self.control, repo_path=self.repo_path, run_status="AWAITING_APPROVAL", run_error="needs review")
        with patch("falguna.agent_roles.accept_revenue_hunter_handoff", return_value={"task_id": fake_control.task_id, "mission_id": "fake-mission"}):
            worker = EngineeringAgentWorker(fake_control)
            result = worker.execute(self._task({
                "repository": str(self.repo_path), "requirement": "Fix the bug",
                "editable_files": ["app.py"], "verification_commands": [{"argv": ["true"]}],
            }))
        self.assertEqual(result.status, "BLOCKED")

    def test_run_ended_failed_status_reports_failed_not_completed(self):
        fake_control = _FakeEngineeringControl(self.control, repo_path=self.repo_path, run_status="FAILED", run_error="verification failed")
        with patch("falguna.agent_roles.accept_revenue_hunter_handoff", return_value={"task_id": fake_control.task_id, "mission_id": "fake-mission"}):
            worker = EngineeringAgentWorker(fake_control)
            result = worker.execute(self._task({
                "repository": str(self.repo_path), "requirement": "Fix the bug",
                "editable_files": ["app.py"], "verification_commands": [{"argv": ["true"]}],
            }))
        self.assertEqual(result.status, "FAILED")

    def test_unavailable_local_model_gateway_blocks_rather_than_crashing(self):
        # Realistic "unavailable external action": the control plane's
        # start() raising (e.g. because no local model gateway is
        # reachable) must never propagate as an unhandled exception --
        # this worker always turns it into an honest BLOCKED result.
        fake_control = _FakeEngineeringControl(
            self.control, repo_path=self.repo_path, run_status="DONE_CANDIDATE",
            raise_exc=ConnectionError("local model gateway unreachable at 127.0.0.1:11434"),
        )
        with patch("falguna.agent_roles.accept_revenue_hunter_handoff", return_value={"task_id": fake_control.task_id, "mission_id": "fake-mission"}):
            worker = EngineeringAgentWorker(fake_control)
            result = worker.execute(self._task({
                "repository": str(self.repo_path), "requirement": "Fix the bug",
                "editable_files": ["app.py"], "verification_commands": [{"argv": ["true"]}],
            }))
        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("unreachable", result.blockers[0])


class QAAgentWorkerTests(AgentRolesTestBase):
    def setUp(self):
        super().setUp()
        self._control_tmp = tempfile.TemporaryDirectory()
        self.control, _ = open_control_plane(Path(self._control_tmp.name))
        self._worktree_tmp = tempfile.TemporaryDirectory()
        self.worktree = Path(self._worktree_tmp.name)

    def tearDown(self):
        self.control.store.close()
        self._control_tmp.cleanup()
        self._worktree_tmp.cleanup()
        super().tearDown()

    def _task(self, run_id):
        return {"id": "qa-task-1", "department": "engineering", "objective": "QA a run", "actor": "Aryan",
                "inputs_json": json.dumps({"run_id": run_id})}

    def _seed_run(self, status="DONE_CANDIDATE", verify_argv=None):
        from falguna.models import RunPolicy
        now = "2026-01-01T00:00:00+00:00"
        eng_task_id = self.control.create_mission("Fix a bug", "Fix a bug", self.worktree, RunPolicy())["task_id"]
        self.control.store.update(
            "tasks", eng_task_id, repository=str(self.worktree),
            policy_json=json.dumps({"verification_commands": [
                {"argv": verify_argv or ["python3", "-c", "import sys; sys.exit(0)"], "label": "tests", "timeout_seconds": 30},
            ]}),
        )
        run_id = self.control.store.create("runs", {
            "task_id": eng_task_id, "status": status, "attempt": 1, "worker": "structured_edit_local",
            "model": "fake-model", "worktree": str(self.worktree), "head_sha": None, "error": None,
            "created_at": now, "updated_at": now,
        })
        return run_id

    def _write_evidence(self, run_id, passed=True, review_approved=None):
        evidence_dir = self.control.state_root / "evidence" / run_id
        evidence_dir.mkdir(parents=True, exist_ok=True)
        (evidence_dir / "verification.json").write_text(json.dumps({"passed": passed}))
        if review_approved is not None:
            (evidence_dir / "review.json").write_text(json.dumps({"approved": review_approved}))

    def test_supports_only_its_own_task_type(self):
        worker = QAAgentWorker(self.control)
        self.assertTrue(worker.supports("qa_independent_verification"))
        self.assertFalse(worker.supports("engineering_fix"))

    def test_missing_run_id_blocks(self):
        worker = QAAgentWorker(self.control)
        result = worker.execute(self._task(None))
        self.assertEqual(result.status, "BLOCKED")

    def test_run_not_found_fails(self):
        worker = QAAgentWorker(self.control)
        result = worker.execute(self._task("does-not-exist"))
        self.assertEqual(result.status, "FAILED")

    def test_run_not_yet_done_candidate_blocks(self):
        run_id = self._seed_run(status="EXECUTING")
        worker = QAAgentWorker(self.control)
        result = worker.execute(self._task(run_id))
        self.assertEqual(result.status, "BLOCKED")

    def test_missing_verification_evidence_fails_rather_than_fabricating_a_pass(self):
        run_id = self._seed_run(status="DONE_CANDIDATE")
        # Deliberately no evidence written.
        worker = QAAgentWorker(self.control)
        result = worker.execute(self._task(run_id))
        self.assertEqual(result.status, "FAILED")

    def test_independent_rerun_agrees_and_passes_completes_with_high_confidence(self):
        run_id = self._seed_run(status="DONE_CANDIDATE", verify_argv=["python3", "-c", "import sys; sys.exit(0)"])
        self._write_evidence(run_id, passed=True, review_approved=True)
        worker = QAAgentWorker(self.control)
        result = worker.execute(self._task(run_id))
        self.assertEqual(result.status, "COMPLETED")
        self.assertEqual(result.confidence, "High")
        self.assertTrue(result.result["independent_rerun_passed"])
        self.assertTrue(result.result["matches_original_verification"])

    def test_independent_rerun_disagrees_with_original_fails_with_low_confidence(self):
        # The original claimed passed=True, but a genuine independent
        # re-execution of the same command fails -- QA must catch this
        # discrepancy rather than trusting the original self-report.
        run_id = self._seed_run(status="DONE_CANDIDATE", verify_argv=["python3", "-c", "import sys; sys.exit(1)"])
        self._write_evidence(run_id, passed=True, review_approved=True)
        worker = QAAgentWorker(self.control)
        result = worker.execute(self._task(run_id))
        self.assertEqual(result.status, "FAILED")
        self.assertEqual(result.confidence, "Low")
        self.assertFalse(result.result["matches_original_verification"])

    def test_customer_isolation_between_two_independent_runs(self):
        run_a = self._seed_run(status="DONE_CANDIDATE", verify_argv=["python3", "-c", "import sys; sys.exit(0)"])
        run_b = self._seed_run(status="DONE_CANDIDATE", verify_argv=["python3", "-c", "import sys; sys.exit(1)"])
        self._write_evidence(run_a, passed=True, review_approved=True)
        self._write_evidence(run_b, passed=True, review_approved=True)
        worker = QAAgentWorker(self.control)
        result_a = worker.execute(self._task(run_a))
        result_b = worker.execute(self._task(run_b))
        self.assertEqual(result_a.status, "COMPLETED")
        self.assertEqual(result_b.status, "FAILED")
        self.assertNotEqual(result_a.result["run_id"], result_b.result["run_id"])


if __name__ == "__main__":
    unittest.main()
