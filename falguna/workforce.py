"""TTT Digital Workforce v1 -- Core Architecture (Pass A).

A general execution layer for computer-based work beyond software
engineering: browser research, data collection, forms/admin, document and
spreadsheet work, email preparation, and recurring digital processes.

Same conventions the rest of this codebase already uses, deliberately kept
consistent rather than reinvented:
  * A durable task model (`WorkforceTaskStore`) with an explicit, checked
    state graph (`ALLOWED_TASK_TRANSITIONS`) and a full event history
    (`wf_task_events`) -- the same shape as `falguna/lifecycle.py`'s
    `rh_lifecycle_events`, just for one task instead of one opportunity.
  * A common worker interface (`WorkforceWorker`) so concrete roles (Research
    Worker, Browser Worker, ...) are pluggable, not hardcoded into the
    orchestrator -- adding a new role never means touching this file.
  * "Never claim an action happened unless verified": `mark_completed`
    hard-requires `evidence`, exactly like `BillingStore.record_payment` and
    `CompletionService.record_completion` already do.
  * Execution method preference is explicit and recorded per task
    (`execution_method`): API before browser automation before computer/UI
    automation, because each step down that list is more fragile and easier
    to misrepresent as having "really happened."
  * Anything a worker cannot safely/legitimately finish alone escalates to
    Needs Aryan (`workforce_action_approval`) rather than guessing --
    the same posture `ApplicationExecutor`'s `ManualReviewChannel` already
    takes for Revenue Hunter.
"""

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .audit import AuditLog
from .store import StateStore, utcnow

TASK_STATUSES = {
    "CREATED", "PLANNING", "READY", "EXECUTING", "BLOCKED", "NEEDS_ARYAN",
    "VERIFYING", "COMPLETED", "FAILED", "CANCELLED",
}
TERMINAL_TASK_STATUSES = {"COMPLETED", "CANCELLED"}

# Explicit, checked graph -- no silent state jumps, same discipline as
# lifecycle.ALLOWED_TRANSITIONS. FAILED -> READY is the one edge that exists
# purely to let a bounded retry happen without inventing a new status.
ALLOWED_TASK_TRANSITIONS: Dict[str, set] = {
    "CREATED": {"PLANNING", "CANCELLED"},
    "PLANNING": {"READY", "BLOCKED", "CANCELLED"},
    "READY": {"EXECUTING", "CANCELLED"},
    "EXECUTING": {"VERIFYING", "BLOCKED", "NEEDS_ARYAN", "FAILED", "CANCELLED"},
    "BLOCKED": {"READY", "NEEDS_ARYAN", "CANCELLED", "FAILED"},
    "NEEDS_ARYAN": {"READY", "EXECUTING", "CANCELLED", "FAILED"},
    "VERIFYING": {"COMPLETED", "FAILED", "BLOCKED"},
    "COMPLETED": set(),
    "FAILED": {"READY", "CANCELLED"},
    "CANCELLED": set(),
}

# Prefer the least fragile execution method available -- an API call is
# harder to misinterpret as "worked" when it didn't than a browser click,
# and a browser click is harder to misinterpret than a raw computer/UI
# automation step. Workers record which one they actually used, not which
# one was merely preferred.
EXECUTION_METHODS = ["API", "BROWSER", "COMPUTER"]

DEFAULT_MAX_RETRIES = 2


class WorkforceError(ValueError):
    pass


@dataclass
class WorkerResult:
    """What every worker returns, regardless of role. `status` decides what
    the orchestrator does next -- it is not free text: one of COMPLETED,
    BLOCKED, FAILED. `evidence` is required whenever `status == COMPLETED`;
    a worker that cannot produce evidence for what it did has not verified
    it, and per this codebase's standing rule that means it did not happen."""

    status: str  # "COMPLETED" | "BLOCKED" | "FAILED"
    result: Any = None
    evidence: Optional[Dict[str, Any]] = None
    confidence: Optional[str] = None  # a plain label ("Low"/"Medium"/"High"), never a fabricated percentage
    limitations: Optional[str] = None
    blockers: List[str] = field(default_factory=list)
    next_action: Optional[str] = None
    execution_method: Optional[str] = None

    def __post_init__(self):
        if self.status not in {"COMPLETED", "BLOCKED", "FAILED"}:
            raise WorkforceError(f"WorkerResult.status must be COMPLETED/BLOCKED/FAILED, got {self.status!r}")
        if self.status == "COMPLETED" and not self.evidence:
            raise WorkforceError("a worker cannot report COMPLETED without evidence")


class WorkforceWorker:
    """Common worker interface. A concrete role subclasses this and
    implements `supports`/`execute`; the orchestrator never hardcodes which
    roles exist. `execute` must never claim an action happened unless it can
    point to real evidence for it -- if it can't verify something, the
    honest result is BLOCKED (escalate) or FAILED, not a guessed COMPLETED."""

    name = "worker"

    def supports(self, task_type: str) -> bool:
        raise NotImplementedError

    def execute(self, task: Dict[str, Any]) -> WorkerResult:
        raise NotImplementedError


class WorkforceTaskStore:
    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit

    def create(
        self, department: str, objective: str, task_type: str, actor: str = "Aryan",
        source: Optional[str] = None, priority: Optional[str] = None,
        inputs: Optional[Dict[str, Any]] = None,
    ) -> str:
        if not department or not department.strip():
            raise WorkforceError("department is required")
        if not objective or not objective.strip():
            raise WorkforceError("objective is required")
        if not task_type or not task_type.strip():
            raise WorkforceError("task_type is required")
        now = utcnow()
        task_id = self.store.create("wf_tasks", {
            "department": department.strip(), "objective": objective.strip(), "task_type": task_type.strip(),
            "source": source, "assigned_worker": None, "status": "CREATED", "priority": priority,
            "inputs_json": json.dumps(inputs) if inputs is not None else None,
            "outputs_json": None, "evidence_json": None, "blockers_json": None,
            "approval_required": 0, "needs_aryan_id": None, "execution_method": None,
            "cost": None, "retries": 0, "error": None, "actor": actor,
            "created_at": now, "started_at": None, "completed_at": None, "updated_at": now,
        })
        self._record_event(task_id, None, "CREATED", actor, "task created")
        self.audit.append("WF_TASK_CREATED", {"task_id": task_id, "department": department, "task_type": task_type, "actor": actor})
        return task_id

    def get(self, task_id: str) -> Optional[Dict[str, Any]]:
        return self.store.get("wf_tasks", task_id)

    def list(self, department: Optional[str] = None, status: Optional[str] = None) -> List[Dict[str, Any]]:
        if department and status:
            rows = self.store.list("wf_tasks", "department=? AND status=?", (department, status))
        elif department:
            rows = self.store.list("wf_tasks", "department=?", (department,))
        elif status:
            rows = self.store.list("wf_tasks", "status=?", (status,))
        else:
            rows = self.store.list("wf_tasks")
        return list(reversed(rows))

    def _record_event(self, task_id: str, from_status: Optional[str], to_status: str, actor: str, reason: Optional[str], evidence: Optional[Dict[str, Any]] = None) -> None:
        self.store.create("wf_task_events", {
            "task_id": task_id, "from_status": from_status, "to_status": to_status, "actor": actor,
            "reason": reason, "evidence_json": json.dumps(evidence) if evidence is not None else None,
            "created_at": utcnow(),
        })

    def transition(self, task_id: str, to_status: str, actor: str, reason: Optional[str] = None, evidence: Optional[Dict[str, Any]] = None, **fields: Any) -> Dict[str, Any]:
        task = self.get(task_id)
        if not task:
            raise WorkforceError("task not found")
        if to_status not in TASK_STATUSES:
            raise WorkforceError(f"unknown task status: {to_status!r}")
        current = task["status"]
        if current == to_status:
            return task  # idempotent no-op, mirrors LifecycleOrchestrator
        if to_status not in ALLOWED_TASK_TRANSITIONS.get(current, set()):
            raise WorkforceError(f"cannot transition a task from {current} to {to_status}")
        updates = {"status": to_status, **fields}
        self.store.update("wf_tasks", task_id, **updates)
        self._record_event(task_id, current, to_status, actor, reason, evidence)
        self.audit.append("WF_TASK_TRANSITIONED", {"task_id": task_id, "from": current, "to": to_status, "actor": actor})
        return self.get(task_id)

    def history(self, task_id: str) -> List[Dict[str, Any]]:
        return self.store.list("wf_task_events", "task_id=?", (task_id,))


class WorkforceOrchestrator:
    """Routes a task to the first registered worker whose `supports()`
    returns True for its task_type, drives the CREATED -> ... -> COMPLETED/
    FAILED/CANCELLED lifecycle, and turns a worker's honest BLOCKED/FAILED
    result into the right real-world consequence (an escalation, or a
    bounded retry) rather than papering over it."""

    def __init__(self, store: StateStore, audit: AuditLog, needs_aryan=None, max_retries: int = DEFAULT_MAX_RETRIES):
        self.store = store
        self.audit = audit
        self.tasks = WorkforceTaskStore(store, audit)
        self.needs_aryan = needs_aryan
        self.max_retries = max_retries
        self._workers: List[WorkforceWorker] = []

    def register_worker(self, worker: WorkforceWorker) -> None:
        self._workers.append(worker)

    def _find_worker(self, task_type: str) -> Optional[WorkforceWorker]:
        for worker in self._workers:
            if worker.supports(task_type):
                return worker
        return None

    def plan(self, task_id: str, actor: str = "system") -> Dict[str, Any]:
        task = self.tasks.get(task_id)
        if not task:
            raise WorkforceError("task not found")
        self.tasks.transition(task_id, "PLANNING", actor, reason="planning execution")
        worker = self._find_worker(task["task_type"])
        if worker is None:
            reason = f"no registered worker supports task_type {task['task_type']!r}"
            self.tasks.transition(task_id, "BLOCKED", actor, reason=reason, blockers_json=json.dumps([reason]))
            # An unroutable task is an architecture gap, not something a
            # retry or a different input will fix -- escalate it the same
            # way a worker's own BLOCKED result escalates, rather than
            # leaving it stuck in BLOCKED with no one notified.
            needs_aryan_id = None
            if self.needs_aryan is not None:
                needs_aryan_id = self.needs_aryan.create_item(
                    "workforce_action_approval",
                    f"Workforce task has no capable worker: {task['objective']}",
                    f"{reason}. Register a worker for this task type, reassign the task to a supported "
                    "type, or cancel it.",
                    actor=actor, rationale=reason, risk="workforce task cannot be routed to any worker",
                    ref_type="wf_task", ref_id=task_id,
                )
            self.tasks.transition(
                task_id, "NEEDS_ARYAN", actor, reason="escalated: no capable worker registered",
                needs_aryan_id=needs_aryan_id,
            )
            return self.tasks.get(task_id)
        self.tasks.transition(task_id, "READY", actor, reason="worker assigned", assigned_worker=worker.name)
        return self.tasks.get(task_id)

    def execute(self, task_id: str, actor: str = "system") -> Dict[str, Any]:
        task = self.tasks.get(task_id)
        if not task:
            raise WorkforceError("task not found")
        if task["status"] == "CREATED":
            task = self.plan(task_id, actor)
        if task["status"] != "READY":
            if task["status"] in {"BLOCKED", "NEEDS_ARYAN"}:
                # plan() already recorded the reason (and escalated, if a
                # queue was wired in) -- return the honest current state
                # instead of raising and losing that context.
                return task
            raise WorkforceError(f"task must be READY to execute (currently {task['status']})")

        worker = self._find_worker(task["task_type"])
        if worker is None:
            raise WorkforceError("no worker available for this task_type")

        self.tasks.transition(task_id, "EXECUTING", actor, reason=f"executing via {worker.name}", started_at=utcnow())
        result = worker.execute(task)

        if result.status == "COMPLETED":
            self.tasks.transition(task_id, "VERIFYING", actor, reason="worker reported completion", evidence=result.evidence)
            self.tasks.transition(
                task_id, "COMPLETED", actor, reason="verified against real evidence",
                outputs_json=json.dumps(result.result) if result.result is not None else None,
                evidence_json=json.dumps(result.evidence),
                execution_method=result.execution_method,
                completed_at=utcnow(),
            )
            self.audit.append("WF_TASK_COMPLETED", {"task_id": task_id, "worker": worker.name, "actor": actor})
            return self.tasks.get(task_id)

        if result.status == "BLOCKED":
            needs_aryan_id = None
            if self.needs_aryan is not None:
                needs_aryan_id = self.needs_aryan.create_item(
                    "workforce_action_approval",
                    f"Workforce task blocked: {task['objective']}",
                    (result.next_action or "Review the blocker and decide how to proceed -- approve an alternative "
                                             "approach, provide missing access, or cancel this task."),
                    actor=actor, rationale="; ".join(result.blockers) if result.blockers else None,
                    risk="workforce task blocked", ref_type="wf_task", ref_id=task_id,
                )
            self.tasks.transition(
                task_id, "NEEDS_ARYAN", actor, reason="worker could not safely complete this task alone",
                evidence=result.evidence, blockers_json=json.dumps(result.blockers),
                needs_aryan_id=needs_aryan_id, execution_method=result.execution_method,
            )
            self.audit.append("WF_TASK_BLOCKED", {"task_id": task_id, "worker": worker.name, "blockers": result.blockers, "actor": actor})
            return self.tasks.get(task_id)

        # FAILED -- bounded retry, then stop (never loop forever). Once
        # retries are exhausted the task is terminal: escalate exactly once
        # (guarded by the task's own needs_aryan_id, the same idempotency
        # marker plan()/BLOCKED already use) so a human is notified, rather
        # than leaving an exhausted-retry task silently stuck in FAILED.
        retries = (task.get("retries") or 0)
        exhausted = retries >= self.max_retries
        needs_aryan_id = task.get("needs_aryan_id") if exhausted else None
        if exhausted and needs_aryan_id is None and self.needs_aryan is not None:
            needs_aryan_id = self.needs_aryan.create_item(
                "workforce_action_approval",
                f"Workforce task failed after {self.max_retries} retries: {task['objective']}",
                (result.next_action or "Review the failure evidence and decide how to proceed -- retry "
                                         "manually with different inputs, reassign, or cancel this task."),
                actor=actor, rationale=(result.next_action or "worker reported failure"),
                risk="workforce task exhausted its retry budget and is stuck in FAILED",
                ref_type="wf_task", ref_id=task_id,
            )
        fail_fields = {"error": (result.next_action or "worker reported failure")}
        if exhausted:
            fail_fields["needs_aryan_id"] = needs_aryan_id
        self.tasks.transition(
            task_id, "FAILED", actor, reason="worker execution failed",
            evidence=result.evidence, **fail_fields,
        )
        self.audit.append("WF_TASK_FAILED", {
            "task_id": task_id, "worker": worker.name, "retries": retries, "actor": actor,
            "exhausted": exhausted, "needs_aryan_id": needs_aryan_id,
        })
        if not exhausted:
            self.tasks.transition(task_id, "READY", actor, reason="retrying after failure", retries=retries + 1)
        return self.tasks.get(task_id)
