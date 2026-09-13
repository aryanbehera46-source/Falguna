import hashlib
import json
import time
from pathlib import Path
from typing import Optional

from .audit import AuditLog
from .gitops import GitWorktreeManager
from .models import RunPolicy, RunStatus
from .policy import PolicyViolation
from .store import StateStore, utcnow
from .verification import DefinitionOfDone
from .review import ReviewerCalibrator, SemanticIndependentReviewer
from .workers import WorkerAdapter
from .supervisor import AutonomySupervisor


class ControlPlane:
    def __init__(self, store: StateStore, audit: AuditLog, state_root: Path, reviewer=None, calibration_cases=None):
        self.store = store
        self.audit = audit
        self.state_root = Path(state_root)
        self.reviewer = reviewer or SemanticIndependentReviewer()
        self.calibration_cases = list(calibration_cases or [])
        self.supervisor = AutonomySupervisor(store, audit)

    def create_mission(self, title: str, requirement: str, repository: Path, policy: RunPolicy) -> dict:
        now = utcnow()
        mission_id = self.store.create("missions", {"title": title, "status": "ACTIVE", "created_at": now, "updated_at": now})
        requirement_id = self.store.create("requirements", {"mission_id": mission_id, "body": requirement, "acceptance_json": "[]", "created_at": now, "updated_at": now})
        task_id = self.store.create("tasks", {"requirement_id": requirement_id, "title": title, "status": "READY", "repository": str(Path(repository).resolve()), "base_ref": "HEAD", "policy_json": json.dumps(policy, default=lambda value: value.__dict__, sort_keys=True), "created_at": now, "updated_at": now})
        for ordinal, kind in enumerate(("INSPECT_PLAN", "IMPLEMENT", "VERIFY", "INDEPENDENT_REVIEW", "PROPOSE_MERGE"), 1):
            self.store.create("task_steps", {"task_id": task_id, "ordinal": ordinal, "kind": kind, "status": "PENDING", "detail_json": "{}", "created_at": now, "updated_at": now})
        self.audit.append("MISSION_CREATED", {"mission_id": mission_id, "task_id": task_id})
        return {"mission_id": mission_id, "requirement_id": requirement_id, "task_id": task_id}

    def start(self, task_id: str, worker: WorkerAdapter, worker_name: str, model: str, policy: RunPolicy, force_stop_after: Optional[str] = None, on_run_created=None) -> str:
        task = self.store.get("tasks", task_id)
        if not task:
            raise ValueError("task not found")
        now = utcnow()
        run_id = self.store.create("runs", {"task_id": task_id, "status": RunStatus.CREATED.value, "attempt": 0, "worker": worker_name, "model": model, "worktree": None, "head_sha": None, "error": None, "created_at": now, "updated_at": now})
        self.audit.append("RUN_CREATED", {"run_id": run_id, "task_id": task_id})
        if hasattr(worker, "checkpoint") and worker.checkpoint is None:
            worker.checkpoint = lambda ordinal, summary: self._milestone_checkpoint(run_id, policy, ordinal, summary)
        if on_run_created:
            on_run_created(run_id)
        return self._continue(run_id, worker, policy, force_stop_after)

    def resume(self, run_id: str, worker: WorkerAdapter, policy: RunPolicy) -> str:
        run = self.store.get("runs", run_id)
        if not run:
            raise ValueError("run not found")
        if run["status"] == RunStatus.CANCELLED.value:
            raise ValueError("cancelled missions cannot be resumed")
        states = self.store.list("supervisor_states", "run_id=?", (run_id,))
        if run["status"] in {RunStatus.FAILED.value, RunStatus.QUARANTINED.value} and states and not states[-1]["resume_allowed"]:
            raise ValueError(states[-1]["eligibility_reason"])
        if run["status"] == RunStatus.FAILED.value and states and states[-1]["resume_allowed"]:
            diagnostics = json.loads(states[-1]["diagnostics_json"])
            self._checkpoint(run_id, "WORKTREE_READY", worktree=run["worktree"], recovery_diagnostics=diagnostics, supervisor_retry=True)
        checkpoint = self.store.latest_checkpoint(run_id)
        if not checkpoint:
            raise ValueError("no checkpoint")
        self.audit.append("RUN_RESUMED", {"run_id": run_id, "stage": checkpoint["stage"]})
        if run and run["status"] == RunStatus.PAUSED.value:
            for control in self.store.list("run_controls", "run_id=? AND status=?", (run_id, "APPLIED")):
                self.store.update("run_controls", control["id"], status="CLEARED")
        if hasattr(worker, "checkpoint") and worker.checkpoint is None:
            worker.checkpoint = lambda ordinal, summary: self._milestone_checkpoint(run_id, policy, ordinal, summary)
        return self._continue(run_id, worker, policy, None)

    def request_control(self, run_id: str, action: str) -> None:
        if action not in {"PAUSE", "CANCEL"}:
            raise ValueError("control action must be PAUSE or CANCEL")
        run = self.store.get("runs", run_id)
        if not run or run["status"] in {RunStatus.DONE_CANDIDATE.value, RunStatus.CANCELLED.value}:
            raise ValueError("run is not controllable")
        self.store.create("run_controls", {"run_id": run_id, "action": action, "status": "REQUESTED", "detail_json": json.dumps({"mode": "safe-boundary"}), "created_at": utcnow(), "updated_at": utcnow()})
        self.audit.append(f"RUN_{action}_REQUESTED", {"run_id": run_id, "mode": "safe-boundary"})

    def _control_boundary(self, run_id: str, stage: str) -> bool:
        pending = self.store.list("run_controls", "run_id=? AND status=?", (run_id, "REQUESTED"))
        if not pending:
            return False
        control = pending[-1]
        action = control["action"]
        self._checkpoint(run_id, stage, safe_boundary=True, control=action, worktree=self.store.get("runs", run_id).get("worktree"))
        self.store.update("run_controls", control["id"], status="APPLIED")
        status = RunStatus.PAUSED.value if action == "PAUSE" else RunStatus.CANCELLED.value
        self.store.update("runs", run_id, status=status, error=None)
        self.audit.append(f"RUN_{action}D", {"run_id": run_id, "stage": stage, "partial_merge": False})
        return True

    def _timed(self, run_id: str, stage: str, started: float, **metadata) -> None:
        self.store.create("mission_timings", {"run_id": run_id, "stage": stage, "duration_ms": max(0, round((time.monotonic() - started) * 1000)), "metadata_json": json.dumps(metadata, sort_keys=True), "created_at": utcnow()})

    def _checkpoint(self, run_id: str, stage: str, **payload):
        self.store.checkpoint(run_id, stage, payload)
        self.audit.append("CHECKPOINT", {"run_id": run_id, "stage": stage})

    def _continue(self, run_id: str, worker: WorkerAdapter, policy: RunPolicy, force_stop_after: Optional[str]) -> str:
        total_started = time.monotonic()
        run = self.store.get("runs", run_id)
        task = self.store.get("tasks", run["task_id"])
        requirement = self.store.get("requirements", task["requirement_id"])["body"]
        manager = GitWorktreeManager(Path(task["repository"]), self.state_root / "worktrees")
        checkpoint = self.store.latest_checkpoint(run_id)
        stage = checkpoint["stage"] if checkpoint else "CREATED"
        payload = json.loads(checkpoint["payload"]) if checkpoint else {}
        try:
            if stage == "CREATED":
                if self._control_boundary(run_id, "CREATED"):
                    return run_id
                self.store.update("runs", run_id, status=RunStatus.PLANNING.value)
                worktree = manager.create(run_id, task["base_ref"])
                self.store.update("runs", run_id, worktree=str(worktree), head_sha=manager.head(worktree))
                self._checkpoint(run_id, "WORKTREE_READY", worktree=str(worktree))
                if force_stop_after == "WORKTREE_READY":
                    return run_id
                stage, payload = "WORKTREE_READY", {"worktree": str(worktree)}
            worktree = Path(payload.get("worktree") or self.store.get("runs", run_id)["worktree"])
            if stage == "WORKTREE_READY":
                if self._control_boundary(run_id, "WORKTREE_READY"):
                    return run_id
                self.store.update("runs", run_id, status=RunStatus.WORKING.value)
                worker_started = time.monotonic()
                result = None
                worker_requirement = requirement
                if payload.get("recovery_diagnostics"):
                    worker_requirement += "\n\nAutonomy Supervisor retry. Preserve approved scope and correct the prior failure using these diagnostics:\n" + json.dumps(payload["recovery_diagnostics"], sort_keys=True)
                base_attempt = int(self.store.get("runs", run_id)["attempt"])
                for local_attempt in range(1, policy.max_attempts + 1):
                    attempt = base_attempt + local_attempt
                    self.store.update("runs", run_id, attempt=attempt)
                    try:
                        result = worker.execute(worktree, worker_requirement, run_id)
                    except Exception as exc:
                        from .models import WorkerResult
                        result = WorkerResult(False, str(exc), 1)
                    self.audit.append("WORKER_ATTEMPT", {"run_id": run_id, "attempt": attempt, "success": result.success, "exit_code": result.exit_code})
                    for call in result.model_calls:
                        self.store.create("model_calls", {"run_id": run_id, "provider": call.get("provider", "unknown"), "model": call.get("model", self.store.get("runs", run_id)["model"]), "purpose": call.get("purpose", "implementation"), "input_tokens": int(call.get("input_tokens", 0)), "output_tokens": int(call.get("output_tokens", 0)), "cost_usd": float(call.get("cost_usd", 0)), "metadata_json": json.dumps(call.get("metadata", {}), sort_keys=True), "created_at": utcnow()})
                    if result.model_calls or result.cost_usd:
                        self.store.create("cost_events", {"run_id": run_id, "category": "MODEL", "amount_usd": result.cost_usd, "metadata_json": json.dumps({"attempt": attempt}), "created_at": utcnow()})
                    if result.cost_usd > policy.max_cost_usd:
                        raise PolicyViolation("run cost cap exceeded")
                    if result.success:
                        break
                if not result or not result.success:
                    self._timed(run_id, "worker", worker_started, attempts=self.store.get("runs", run_id)["attempt"], success=False)
                    detail = result.summary if result else "worker returned no result"
                    raise RuntimeError(f"worker failed within retry bound: {detail}")
                self._timed(run_id, "worker", worker_started, attempts=self.store.get("runs", run_id)["attempt"], success=True)
                evidence_dir = self.state_root / "evidence" / run_id
                evidence_dir.mkdir(parents=True, exist_ok=True)
                worker_path = evidence_dir / f"worker-output-attempt-{self.store.get('runs', run_id)['attempt']}.txt"
                worker_path.write_text(result.summary)
                self._record_artifact(run_id, "WORKER_OUTPUT", worker_path)
                self._checkpoint(run_id, "WORKER_COMPLETE", worktree=str(worktree))
                if force_stop_after == "WORKER_COMPLETE":
                    return run_id
                stage = "WORKER_COMPLETE"
            if stage == "WORKER_COMPLETE":
                if self._control_boundary(run_id, "WORKER_COMPLETE"):
                    return run_id
                self.store.update("runs", run_id, status=RunStatus.VERIFYING.value)
                verification_started = time.monotonic()
                passed, changed, results, browser, isolation, containment_probe = DefinitionOfDone(manager, policy).verify(worktree)
                self._timed(run_id, "verification", verification_started, passed=passed, browser_applicable=policy.browser_applicable)
                evidence_dir = self.state_root / "evidence" / run_id
                evidence_dir.mkdir(parents=True, exist_ok=True)
                prior_attempts = sorted(evidence_dir.glob("verification-attempt-*.json"))
                verification_attempt = len(prior_attempts) + 1
                verification_path = evidence_dir / f"verification-attempt-{verification_attempt}.json"
                task_checks = []
                for item in self.store.list("checkpoints", "run_id=?", (run_id,)):
                    checkpoint_payload = json.loads(item["payload"])
                    if checkpoint_payload.get("completed_milestone_task"):
                        task_checks.append(checkpoint_payload)
                task_checks = self._resolve_task_checks(task_checks, passed, changed)
                verification_evidence = {"attempt": verification_attempt, "passed": passed, "changed_files": changed, "results": results, "browser": browser, "isolation": isolation, "containment_probe": containment_probe, "milestone_tasks": task_checks}
                verification_path.write_text(json.dumps(verification_evidence, indent=2, sort_keys=True))
                self._record_artifact(run_id, "VERIFICATION_ATTEMPT", verification_path)
                if not passed:
                    repairs = self.store.list("checkpoints", "run_id=? AND stage=?", (run_id, "REPAIR_COMPLETE"))
                    if repairs:
                        raise RuntimeError("Definition of Done failed after bounded repair")
                    failure_summary = json.dumps(results, sort_keys=True)[-6000:]
                    self.supervisor.record(run_id, "VERIFICATION_FAILURE: " + failure_summary, retry_budget=policy.max_attempts, phase="REPAIRING_VERIFICATION")
                    repair_requirement = requirement + "\n\nThe control-plane verification failed. Diagnose and repair only the permitted files, then rerun tests. Verification evidence:\n" + failure_summary
                    repair_attempt = int(self.store.get("runs", run_id)["attempt"]) + 1
                    self.store.update("runs", run_id, status=RunStatus.WORKING.value, attempt=repair_attempt)
                    repair = worker.execute(worktree, repair_requirement, run_id)
                    self.audit.append("REPAIR_ATTEMPT", {"run_id": run_id, "attempt": repair_attempt, "verification_attempt": verification_attempt, "success": repair.success, "exit_code": repair.exit_code})
                    for call in repair.model_calls:
                        self.store.create("model_calls", {"run_id": run_id, "provider": call.get("provider", "unknown"), "model": call.get("model", self.store.get("runs", run_id)["model"]), "purpose": "repair", "input_tokens": int(call.get("input_tokens", 0)), "output_tokens": int(call.get("output_tokens", 0)), "cost_usd": float(call.get("cost_usd", 0)), "metadata_json": json.dumps(call.get("metadata", {}), sort_keys=True), "created_at": utcnow()})
                    if repair.model_calls or repair.cost_usd:
                        self.store.create("cost_events", {"run_id": run_id, "category": "MODEL_REPAIR", "amount_usd": repair.cost_usd, "metadata_json": json.dumps({"attempt": repair_attempt}), "created_at": utcnow()})
                    total_cost = sum(float(event["amount_usd"]) for event in self.store.list("cost_events", "run_id=?", (run_id,)))
                    if total_cost > policy.max_cost_usd:
                        raise PolicyViolation("cumulative run cost cap exceeded")
                    repair_path = evidence_dir / f"repair-output-{repair_attempt}.txt"
                    repair_path.write_text(repair.summary)
                    self._record_artifact(run_id, "REPAIR_OUTPUT", repair_path)
                    if not repair.success:
                        raise RuntimeError(f"repair worker failed: {repair.summary}")
                    self._checkpoint(run_id, "REPAIR_COMPLETE", worktree=str(worktree), verification_attempt=verification_attempt, repair_attempt=repair_attempt)
                    self._checkpoint(run_id, "WORKER_COMPLETE", worktree=str(worktree))
                    return self._continue(run_id, worker, policy, None)
                self._checkpoint(run_id, "VERIFIED", worktree=str(worktree), changed_files=changed)
                stage, payload = "VERIFIED", {"worktree": str(worktree), "changed_files": changed}
            if stage == "VERIFIED":
                if self._control_boundary(run_id, "VERIFIED"):
                    return run_id
                self.store.update("runs", run_id, status=RunStatus.REVIEWING.value)
                review_started = time.monotonic()
                if self.calibration_cases:
                    calibration = ReviewerCalibrator(self.reviewer).run(self.calibration_cases)
                    evidence_dir = self.state_root / "evidence" / run_id
                    calibration_path = evidence_dir / "review-calibration.json"
                    calibration_path.write_text(json.dumps({key: value for key, value in calibration.items() if key not in {"model_calls", "cost_usd"}}, indent=2, sort_keys=True))
                    self._record_artifact(run_id, "REVIEW_CALIBRATION", calibration_path)
                    for call in calibration["model_calls"]:
                        self.store.create("model_calls", {"run_id": run_id, "provider": call.get("provider", "unknown"), "model": call.get("model", self.store.get("runs", run_id)["model"]), "purpose": "review-calibration", "input_tokens": int(call.get("input_tokens", 0)), "output_tokens": int(call.get("output_tokens", 0)), "cost_usd": float(call.get("cost_usd", 0)), "metadata_json": json.dumps(call.get("metadata", {}), sort_keys=True), "created_at": utcnow()})
                    if calibration["model_calls"] or calibration["cost_usd"]:
                        self.store.create("cost_events", {"run_id": run_id, "category": "MODEL_REVIEW_CALIBRATION", "amount_usd": calibration["cost_usd"], "metadata_json": json.dumps({"cases": calibration["total"]}), "created_at": utcnow()})
                    calibration_total = sum(float(event["amount_usd"]) for event in self.store.list("cost_events", "run_id=?", (run_id,)))
                    if calibration_total > policy.max_cost_usd:
                        raise PolicyViolation("cumulative run cost cap exceeded during reviewer calibration")
                    if not calibration["passed"]:
                        raise RuntimeError(f"independent reviewer failed calibration: {calibration['false_accepts']} false accepts, {calibration['false_rejects']} false rejects")
                changed = payload.get("changed_files") or manager.changed_files(worktree)
                diff = manager.diff(worktree)
                verification_attempts = sorted((self.state_root / "evidence" / run_id).glob("verification-attempt-*.json"))
                if not verification_attempts:
                    raise RuntimeError("INTERNAL_ORCHESTRATION_ERROR: verified checkpoint has no verification attempt evidence")
                verification_path = verification_attempts[-1]
                verification_evidence = json.loads(verification_path.read_text())
                review = self.reviewer.review(requirement, diff, changed, verification_evidence)
                self._timed(run_id, "review", review_started, approved=review.approved)
                evidence_dir = self.state_root / "evidence" / run_id
                review_number = len(list(evidence_dir.glob("review-attempt-*.json"))) + 1
                diff_path = evidence_dir / f"diff-attempt-{review_number}.patch"
                diff_path.write_text(diff)
                prior_reviews = sorted(evidence_dir.glob("review-attempt-*.json"))
                review_path = evidence_dir / ("review.json" if review.approved else f"review-attempt-{len(prior_reviews) + 1}.json")
                review_path.write_text(json.dumps(review.__dict__, indent=2, sort_keys=True))
                self._record_artifact(run_id, "DIFF", diff_path)
                self._record_artifact(run_id, "REVIEW" if review.approved else "REVIEW_ATTEMPT", review_path)
                for call in review.model_calls:
                    self.store.create("model_calls", {"run_id": run_id, "provider": call.get("provider", "unknown"), "model": call.get("model", self.store.get("runs", run_id)["model"]), "purpose": call.get("purpose", "independent-semantic-review"), "input_tokens": int(call.get("input_tokens", 0)), "output_tokens": int(call.get("output_tokens", 0)), "cost_usd": float(call.get("cost_usd", 0)), "metadata_json": json.dumps(call.get("metadata", {}), sort_keys=True), "created_at": utcnow()})
                if review.model_calls or review.cost_usd:
                    self.store.create("cost_events", {"run_id": run_id, "category": "MODEL_REVIEW", "amount_usd": review.cost_usd, "metadata_json": "{}", "created_at": utcnow()})
                total_cost = sum(float(event["amount_usd"]) for event in self.store.list("cost_events", "run_id=?", (run_id,)))
                if total_cost > policy.max_cost_usd:
                    raise PolicyViolation("cumulative run cost cap exceeded")
                if not review.approved:
                    prior = self.store.list("checkpoints", "run_id=? AND stage=?", (run_id, "REVIEW_REPAIR_COMPLETE"))
                    if prior:
                        raise RuntimeError("REVIEW_FAILURE: independent review rejected candidate after bounded correction: " + "; ".join(review.findings))
                    correction = requirement + "\n\nIndependent review rejected the candidate. Correct only the approved scope, preserve passing verification, then allow fresh verification and review. Findings:\n" + "\n".join(f"- {finding}" for finding in review.findings)
                    self.supervisor.record(run_id, "REVIEW_FAILURE: " + "; ".join(review.findings), retry_budget=policy.max_attempts, phase="ADDRESSING_REVIEW")
                    repair_attempt = int(self.store.get("runs", run_id)["attempt"]) + 1
                    self.store.update("runs", run_id, status=RunStatus.WORKING.value, attempt=repair_attempt)
                    repair = worker.execute(worktree, correction, run_id)
                    self.audit.append("REVIEW_REPAIR_ATTEMPT", {"run_id": run_id, "attempt": repair_attempt, "success": repair.success, "findings": review.findings})
                    repair_path = evidence_dir / f"review-repair-output-{repair_attempt}.txt"
                    repair_path.write_text(repair.summary)
                    self._record_artifact(run_id, "REVIEW_REPAIR_OUTPUT", repair_path)
                    if not repair.success:
                        raise RuntimeError(f"REVIEW_FAILURE: corrective worker failed: {repair.summary}")
                    self._checkpoint(run_id, "REVIEW_REPAIR_COMPLETE", worktree=str(worktree), findings=review.findings, repair_attempt=repair_attempt)
                    self._checkpoint(run_id, "WORKER_COMPLETE", worktree=str(worktree), preserved_review_findings=review.findings)
                    return self._continue(run_id, worker, policy, None)
                canonical_verification = evidence_dir / "verification.json"
                canonical_verification.write_text(json.dumps(verification_evidence, indent=2, sort_keys=True))
                self._record_artifact(run_id, "VERIFICATION_FINAL", canonical_verification)
                approval_id = self.store.create("approvals", {"run_id": run_id, "kind": "PROTECTED_BRANCH_MERGE", "status": "PENDING", "requested_at": utcnow(), "decided_at": None, "decided_by": None, "reason": None, "created_at": utcnow(), "updated_at": utcnow()})
                self.store.update("runs", run_id, status=RunStatus.DONE_CANDIDATE.value, error=None)
                states = self.store.list("supervisor_states", "run_id=?", (run_id,))
                if states:
                    self.store.update("supervisor_states", states[-1]["id"], phase="DONE_CANDIDATE", retry_allowed=0, resume_allowed=0, eligibility_reason="Mission completed; human merge approval remains pending.")
                self._checkpoint(run_id, "DONE_CANDIDATE", worktree=str(worktree), approval_id=approval_id)
                self.audit.append("DONE_CANDIDATE", {"run_id": run_id, "approval_id": approval_id})
            self._timed(run_id, "total", total_started, terminal_status=self.store.get("runs", run_id)["status"])
            return run_id
        except PolicyViolation as exc:
            self.store.update("runs", run_id, status=RunStatus.QUARANTINED.value, error=str(exc))
            self.supervisor.record(run_id, str(exc), retry_budget=policy.max_attempts, attempts_used=self.store.get("runs", run_id)["attempt"], decision_needed="Review the blocked policy boundary; Falguna will not expand permissions itself.")
            self.audit.append("RUN_QUARANTINED", {"run_id": run_id, "error": str(exc)})
            return run_id
        except Exception as exc:
            self.store.update("runs", run_id, status=RunStatus.FAILED.value, error=str(exc))
            prior_states = self.store.list("supervisor_states", "run_id=?", (run_id,))
            attempts_used = (prior_states[-1]["attempts_used"] if prior_states else 0) + 1
            self.supervisor.record(run_id, str(exc), retry_budget=policy.max_attempts, attempts_used=attempts_used)
            self.audit.append("RUN_FAILED", {"run_id": run_id, "error": str(exc)})
            return run_id

    def _milestone_checkpoint(self, run_id: str, policy: RunPolicy, ordinal: int, summary: str) -> None:
        worktree = Path(self.store.get("runs", run_id)["worktree"])
        manager = GitWorktreeManager(Path(self.store.get("tasks", self.store.get("runs", run_id)["task_id"])["repository"]), self.state_root / "worktrees")
        passed, changed, results, browser, isolation, containment_probe = DefinitionOfDone(manager, policy).verify(worktree)
        self._checkpoint(run_id, "WORKTREE_READY", worktree=str(worktree), completed_milestone_task=ordinal,
                         summary=summary, per_task_verification={"passed": passed, "changed_files": changed,
                         "results": results, "browser": browser, "isolation": isolation,
                                                                "containment_probe": containment_probe})

    @staticmethod
    def _resolve_task_checks(task_checks: list, final_passed: bool, final_changed: list) -> list:
        if not final_passed:
            return task_checks
        final_files = set(final_changed)
        for index, item in enumerate(task_checks):
            check = item.get("per_task_verification", {})
            failed_files = set(check.get("changed_files", []))
            if check.get("passed") or not failed_files or not failed_files.issubset(final_files):
                continue
            for later in task_checks[index + 1:]:
                later_check = later.get("per_task_verification", {})
                if later_check.get("passed") and failed_files.issubset(set(later_check.get("changed_files", []))):
                    check["resolution"] = {
                        "status": "SUPERSEDED_BY_LATER_PASS",
                        "resolved_by_milestone_task": later["completed_milestone_task"],
                        "final_verification_passed": True,
                    }
                    break
        return task_checks
    def _record_artifact(self, run_id: str, kind: str, path: Path) -> None:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        self.store.create("artifacts", {"run_id": run_id, "kind": kind, "path": str(path), "sha256": digest, "metadata_json": "{}", "created_at": utcnow()})

    def decide_merge(self, run_id: str, decision: str, actor: str, reason: str) -> None:
        statuses = {"approve": "APPROVED", "reject": "REJECTED", "request-changes": "CHANGES_REQUESTED"}
        if decision not in statuses:
            raise ValueError("decision must be approve, reject, or request-changes")
        approvals = self.store.list("approvals", "run_id=? AND kind=?", (run_id, "PROTECTED_BRANCH_MERGE"))
        if not approvals:
            raise ValueError("merge approval not requested")
        approval = approvals[-1]
        if approval["status"] != "PENDING":
            raise ValueError("merge decision already recorded")
        self.store.update("approvals", approval["id"], status=statuses[decision], decided_at=utcnow(), decided_by=actor, reason=reason)
        self.audit.append("MERGE_DECISION_RECORDED", {"run_id": run_id, "decision": statuses[decision], "actor": actor})
        # Deliberately records intent only. There is no merge implementation in bootstrap v0.1.
