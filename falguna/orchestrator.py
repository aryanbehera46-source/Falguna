import hashlib
import json
from pathlib import Path
from typing import Optional

from .audit import AuditLog
from .gitops import GitWorktreeManager
from .models import RunPolicy, RunStatus
from .policy import PolicyViolation
from .store import StateStore, utcnow
from .verification import DefinitionOfDone
from .review import SemanticIndependentReviewer
from .workers import WorkerAdapter


class ControlPlane:
    def __init__(self, store: StateStore, audit: AuditLog, state_root: Path, reviewer=None):
        self.store = store
        self.audit = audit
        self.state_root = Path(state_root)
        self.reviewer = reviewer or SemanticIndependentReviewer()

    def create_mission(self, title: str, requirement: str, repository: Path, policy: RunPolicy) -> dict:
        now = utcnow()
        mission_id = self.store.create("missions", {"title": title, "status": "ACTIVE", "created_at": now, "updated_at": now})
        requirement_id = self.store.create("requirements", {"mission_id": mission_id, "body": requirement, "acceptance_json": "[]", "created_at": now, "updated_at": now})
        task_id = self.store.create("tasks", {"requirement_id": requirement_id, "title": title, "status": "READY", "repository": str(Path(repository).resolve()), "base_ref": "HEAD", "policy_json": json.dumps(policy, default=lambda value: value.__dict__, sort_keys=True), "created_at": now, "updated_at": now})
        for ordinal, kind in enumerate(("INSPECT_PLAN", "IMPLEMENT", "VERIFY", "INDEPENDENT_REVIEW", "PROPOSE_MERGE"), 1):
            self.store.create("task_steps", {"task_id": task_id, "ordinal": ordinal, "kind": kind, "status": "PENDING", "detail_json": "{}", "created_at": now, "updated_at": now})
        self.audit.append("MISSION_CREATED", {"mission_id": mission_id, "task_id": task_id})
        return {"mission_id": mission_id, "requirement_id": requirement_id, "task_id": task_id}

    def start(self, task_id: str, worker: WorkerAdapter, worker_name: str, model: str, policy: RunPolicy, force_stop_after: Optional[str] = None) -> str:
        task = self.store.get("tasks", task_id)
        if not task:
            raise ValueError("task not found")
        now = utcnow()
        run_id = self.store.create("runs", {"task_id": task_id, "status": RunStatus.CREATED.value, "attempt": 0, "worker": worker_name, "model": model, "worktree": None, "head_sha": None, "error": None, "created_at": now, "updated_at": now})
        self.audit.append("RUN_CREATED", {"run_id": run_id, "task_id": task_id})
        return self._continue(run_id, worker, policy, force_stop_after)

    def resume(self, run_id: str, worker: WorkerAdapter, policy: RunPolicy) -> str:
        checkpoint = self.store.latest_checkpoint(run_id)
        if not checkpoint:
            raise ValueError("no checkpoint")
        self.audit.append("RUN_RESUMED", {"run_id": run_id, "stage": checkpoint["stage"]})
        return self._continue(run_id, worker, policy, None)

    def _checkpoint(self, run_id: str, stage: str, **payload):
        self.store.checkpoint(run_id, stage, payload)
        self.audit.append("CHECKPOINT", {"run_id": run_id, "stage": stage})

    def _continue(self, run_id: str, worker: WorkerAdapter, policy: RunPolicy, force_stop_after: Optional[str]) -> str:
        run = self.store.get("runs", run_id)
        task = self.store.get("tasks", run["task_id"])
        requirement = self.store.get("requirements", task["requirement_id"])["body"]
        manager = GitWorktreeManager(Path(task["repository"]), self.state_root / "worktrees")
        checkpoint = self.store.latest_checkpoint(run_id)
        stage = checkpoint["stage"] if checkpoint else "CREATED"
        payload = json.loads(checkpoint["payload"]) if checkpoint else {}
        try:
            if stage == "CREATED":
                self.store.update("runs", run_id, status=RunStatus.PLANNING.value)
                worktree = manager.create(run_id, task["base_ref"])
                self.store.update("runs", run_id, worktree=str(worktree), head_sha=manager.head(worktree))
                self._checkpoint(run_id, "WORKTREE_READY", worktree=str(worktree))
                if force_stop_after == "WORKTREE_READY":
                    return run_id
                stage, payload = "WORKTREE_READY", {"worktree": str(worktree)}
            worktree = Path(payload.get("worktree") or self.store.get("runs", run_id)["worktree"])
            if stage == "WORKTREE_READY":
                self.store.update("runs", run_id, status=RunStatus.WORKING.value)
                result = None
                for attempt in range(1, policy.max_attempts + 1):
                    self.store.update("runs", run_id, attempt=attempt)
                    result = worker.execute(worktree, requirement, run_id)
                    self.audit.append("WORKER_ATTEMPT", {"run_id": run_id, "attempt": attempt, "success": result.success, "exit_code": result.exit_code})
                    for call in result.model_calls:
                        self.store.create("model_calls", {"run_id": run_id, "provider": call.get("provider", "unknown"), "model": call.get("model", self.store.get("runs", run_id)["model"]), "purpose": call.get("purpose", "implementation"), "input_tokens": int(call.get("input_tokens", 0)), "output_tokens": int(call.get("output_tokens", 0)), "cost_usd": float(call.get("cost_usd", 0)), "metadata_json": json.dumps(call.get("metadata", {}), sort_keys=True), "created_at": utcnow()})
                    if result.cost_usd:
                        self.store.create("cost_events", {"run_id": run_id, "category": "MODEL", "amount_usd": result.cost_usd, "metadata_json": json.dumps({"attempt": attempt}), "created_at": utcnow()})
                    if result.cost_usd > policy.max_cost_usd:
                        raise PolicyViolation("run cost cap exceeded")
                    if result.success:
                        break
                if not result or not result.success:
                    raise RuntimeError("worker failed within retry bound")
                evidence_dir = self.state_root / "evidence" / run_id
                evidence_dir.mkdir(parents=True, exist_ok=True)
                worker_path = evidence_dir / "worker-output.txt"
                worker_path.write_text(result.summary)
                self._record_artifact(run_id, "WORKER_OUTPUT", worker_path)
                self._checkpoint(run_id, "WORKER_COMPLETE", worktree=str(worktree))
                if force_stop_after == "WORKER_COMPLETE":
                    return run_id
                stage = "WORKER_COMPLETE"
            if stage == "WORKER_COMPLETE":
                self.store.update("runs", run_id, status=RunStatus.VERIFYING.value)
                passed, changed, results, browser, isolation, containment_probe = DefinitionOfDone(manager, policy).verify(worktree)
                evidence_dir = self.state_root / "evidence" / run_id
                evidence_dir.mkdir(parents=True, exist_ok=True)
                verification_path = evidence_dir / "verification.json"
                verification_evidence = {"passed": passed, "changed_files": changed, "results": results, "browser": browser, "isolation": isolation, "containment_probe": containment_probe}
                verification_path.write_text(json.dumps(verification_evidence, indent=2, sort_keys=True))
                self._record_artifact(run_id, "VERIFICATION", verification_path)
                if not passed:
                    current_attempt = int(self.store.get("runs", run_id)["attempt"])
                    if current_attempt >= policy.max_attempts:
                        raise RuntimeError("Definition of Done failed after bounded repair")
                    failure_summary = json.dumps(results, sort_keys=True)[-6000:]
                    repair_requirement = requirement + "\n\nThe control-plane verification failed. Diagnose and repair only the permitted files, then rerun tests. Verification evidence:\n" + failure_summary
                    self.store.update("runs", run_id, status=RunStatus.WORKING.value, attempt=current_attempt + 1)
                    repair = worker.execute(worktree, repair_requirement, run_id)
                    self.audit.append("REPAIR_ATTEMPT", {"run_id": run_id, "attempt": current_attempt + 1, "success": repair.success, "exit_code": repair.exit_code})
                    for call in repair.model_calls:
                        self.store.create("model_calls", {"run_id": run_id, "provider": call.get("provider", "unknown"), "model": call.get("model", self.store.get("runs", run_id)["model"]), "purpose": "repair", "input_tokens": int(call.get("input_tokens", 0)), "output_tokens": int(call.get("output_tokens", 0)), "cost_usd": float(call.get("cost_usd", 0)), "metadata_json": json.dumps(call.get("metadata", {}), sort_keys=True), "created_at": utcnow()})
                    if repair.cost_usd:
                        self.store.create("cost_events", {"run_id": run_id, "category": "MODEL_REPAIR", "amount_usd": repair.cost_usd, "metadata_json": json.dumps({"attempt": current_attempt + 1}), "created_at": utcnow()})
                    total_cost = sum(float(event["amount_usd"]) for event in self.store.list("cost_events", "run_id=?", (run_id,)))
                    if total_cost > policy.max_cost_usd:
                        raise PolicyViolation("cumulative run cost cap exceeded")
                    repair_path = evidence_dir / f"repair-output-{current_attempt + 1}.txt"
                    repair_path.write_text(repair.summary)
                    self._record_artifact(run_id, "REPAIR_OUTPUT", repair_path)
                    if not repair.success:
                        raise RuntimeError("repair worker failed")
                    self._checkpoint(run_id, "WORKER_COMPLETE", worktree=str(worktree))
                    return self._continue(run_id, worker, policy, None)
                self._checkpoint(run_id, "VERIFIED", worktree=str(worktree), changed_files=changed)
                stage, payload = "VERIFIED", {"worktree": str(worktree), "changed_files": changed}
            if stage == "VERIFIED":
                self.store.update("runs", run_id, status=RunStatus.REVIEWING.value)
                changed = payload.get("changed_files") or manager.changed_files(worktree)
                diff = manager.diff(worktree)
                verification_path = self.state_root / "evidence" / run_id / "verification.json"
                verification_evidence = json.loads(verification_path.read_text())
                review = self.reviewer.review(requirement, diff, changed, verification_evidence)
                evidence_dir = self.state_root / "evidence" / run_id
                (evidence_dir / "diff.patch").write_text(diff)
                (evidence_dir / "review.json").write_text(json.dumps(review.__dict__, indent=2, sort_keys=True))
                self._record_artifact(run_id, "DIFF", evidence_dir / "diff.patch")
                self._record_artifact(run_id, "REVIEW", evidence_dir / "review.json")
                for call in review.model_calls:
                    self.store.create("model_calls", {"run_id": run_id, "provider": call.get("provider", "unknown"), "model": call.get("model", self.store.get("runs", run_id)["model"]), "purpose": call.get("purpose", "independent-semantic-review"), "input_tokens": int(call.get("input_tokens", 0)), "output_tokens": int(call.get("output_tokens", 0)), "cost_usd": float(call.get("cost_usd", 0)), "metadata_json": json.dumps(call.get("metadata", {}), sort_keys=True), "created_at": utcnow()})
                if review.cost_usd:
                    self.store.create("cost_events", {"run_id": run_id, "category": "MODEL_REVIEW", "amount_usd": review.cost_usd, "metadata_json": "{}", "created_at": utcnow()})
                total_cost = sum(float(event["amount_usd"]) for event in self.store.list("cost_events", "run_id=?", (run_id,)))
                if total_cost > policy.max_cost_usd:
                    raise PolicyViolation("cumulative run cost cap exceeded")
                if not review.approved:
                    raise RuntimeError("independent review rejected candidate: " + "; ".join(review.findings))
                approval_id = self.store.create("approvals", {"run_id": run_id, "kind": "PROTECTED_BRANCH_MERGE", "status": "PENDING", "requested_at": utcnow(), "decided_at": None, "decided_by": None, "reason": None, "created_at": utcnow(), "updated_at": utcnow()})
                self.store.update("runs", run_id, status=RunStatus.DONE_CANDIDATE.value)
                self._checkpoint(run_id, "DONE_CANDIDATE", worktree=str(worktree), approval_id=approval_id)
                self.audit.append("DONE_CANDIDATE", {"run_id": run_id, "approval_id": approval_id})
            return run_id
        except PolicyViolation as exc:
            self.store.update("runs", run_id, status=RunStatus.QUARANTINED.value, error=str(exc))
            self.audit.append("RUN_QUARANTINED", {"run_id": run_id, "error": str(exc)})
            return run_id
        except Exception as exc:
            self.store.update("runs", run_id, status=RunStatus.FAILED.value, error=str(exc))
            self.audit.append("RUN_FAILED", {"run_id": run_id, "error": str(exc)})
            return run_id

    def _record_artifact(self, run_id: str, kind: str, path: Path) -> None:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        self.store.create("artifacts", {"run_id": run_id, "kind": kind, "path": str(path), "sha256": digest, "metadata_json": "{}", "created_at": utcnow()})

    def decide_merge(self, run_id: str, approved: bool, actor: str, reason: str) -> None:
        approvals = self.store.list("approvals", "run_id=? AND kind=?", (run_id, "PROTECTED_BRANCH_MERGE"))
        if not approvals:
            raise ValueError("merge approval not requested")
        approval = approvals[-1]
        self.store.update("approvals", approval["id"], status="APPROVED" if approved else "REJECTED", decided_at=utcnow(), decided_by=actor, reason=reason)
        self.audit.append("MERGE_DECISION_RECORDED", {"run_id": run_id, "approved": approved, "actor": actor})
        # Deliberately records intent only. There is no merge implementation in bootstrap v0.1.
