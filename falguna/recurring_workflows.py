"""TTT Digital Workforce v1 -- Recurring workflow foundation (Section 8,
Pass B).

A lightweight, durable schedule model -- not an OS-level daemon. A
`RecurringWorkflowStore` persists a definition (what to run, how often,
what task to create) and tracks `next_due_at`; `run_due()` is a plain
callable that creates a real `wf_tasks` row for every definition whose time
has come and advances its schedule. Nothing here runs on its own -- an
external caller (a scheduled task, a cron-equivalent, or a person clicking
"run now" in TTT HQ) is what actually invokes `run_due()`, exactly per the
instruction: "make execution callable programmatically so scheduling can be
added cleanly" rather than building a fragile background daemon into this
codebase.
"""

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .audit import AuditLog
from .store import StateStore, utcnow
from .workforce import WorkforceTaskStore

SCHEDULE_KINDS = {"hourly", "daily", "weekly", "interval"}
WORKFLOW_STATUSES = {"ACTIVE", "PAUSED"}

_INTERVAL_BY_KIND = {"hourly": timedelta(hours=1), "daily": timedelta(days=1), "weekly": timedelta(days=7)}


class RecurringWorkflowError(ValueError):
    pass


def _compute_next_due(schedule_kind: str, schedule_config: Optional[Dict[str, Any]], from_time: Optional[datetime] = None) -> str:
    base = from_time or datetime.now(timezone.utc)
    if schedule_kind == "interval":
        hours = (schedule_config or {}).get("hours")
        if not hours:
            raise RecurringWorkflowError("interval schedules require schedule_config={'hours': N}")
        delta = timedelta(hours=hours)
    else:
        delta = _INTERVAL_BY_KIND[schedule_kind]
    return (base + delta).isoformat()


class RecurringWorkflowStore:
    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit
        self.tasks = WorkforceTaskStore(store, audit)

    def create(
        self, name: str, department: str, objective: str, task_type: str, schedule_kind: str,
        schedule_config: Optional[Dict[str, Any]] = None, task_template: Optional[Dict[str, Any]] = None, actor: str = "Aryan",
    ) -> str:
        if not name or not name.strip():
            raise RecurringWorkflowError("name is required")
        if schedule_kind not in SCHEDULE_KINDS:
            raise RecurringWorkflowError(f"schedule_kind must be one of {sorted(SCHEDULE_KINDS)}")
        now = utcnow()
        workflow_id = self.store.create("wf_recurring_workflows", {
            "name": name.strip(), "department": department, "objective": objective, "task_type": task_type,
            "schedule_kind": schedule_kind, "schedule_config_json": json.dumps(schedule_config) if schedule_config else None,
            "task_template_json": json.dumps(task_template) if task_template else None, "status": "ACTIVE",
            "last_run_at": None, "next_due_at": _compute_next_due(schedule_kind, schedule_config),
            "actor": actor, "created_at": now, "updated_at": now,
        })
        self.audit.append("WF_RECURRING_WORKFLOW_CREATED", {"workflow_id": workflow_id, "name": name, "schedule_kind": schedule_kind, "actor": actor})
        return workflow_id

    def pause(self, workflow_id: str, actor: str) -> Dict[str, Any]:
        self._require(workflow_id)
        self.store.update("wf_recurring_workflows", workflow_id, status="PAUSED")
        self.audit.append("WF_RECURRING_WORKFLOW_PAUSED", {"workflow_id": workflow_id, "actor": actor})
        return self.get(workflow_id)

    def resume(self, workflow_id: str, actor: str) -> Dict[str, Any]:
        self._require(workflow_id)
        self.store.update("wf_recurring_workflows", workflow_id, status="ACTIVE")
        self.audit.append("WF_RECURRING_WORKFLOW_RESUMED", {"workflow_id": workflow_id, "actor": actor})
        return self.get(workflow_id)

    def due(self, as_of: Optional[str] = None) -> List[Dict[str, Any]]:
        as_of = as_of or utcnow()
        return [
            w for w in self.store.list("wf_recurring_workflows", "status=?", ("ACTIVE",))
            if w.get("next_due_at") and w["next_due_at"] <= as_of
        ]

    def run_due(self, actor: str = "system", as_of: Optional[str] = None) -> List[str]:
        """Creates one real wf_tasks row per due workflow and advances its
        schedule. Returns the created task ids. Never executes the task
        itself -- that is WorkforceOrchestrator's job, kept separate so a
        recurring workflow only ever *creates work*, it doesn't silently
        run unattended external actions on its own schedule."""
        created_task_ids = []
        for workflow in self.due(as_of):
            template = json.loads(workflow["task_template_json"]) if workflow.get("task_template_json") else {}
            task_id = self.tasks.create(
                workflow["department"], template.get("objective", workflow["objective"]), workflow["task_type"],
                actor=actor, source=f"recurring:{workflow['id']}", priority=template.get("priority"),
                inputs=template.get("inputs"),
            )
            run_id = self.store.create("wf_recurring_runs", {
                "workflow_id": workflow["id"], "task_id": task_id, "status": "CREATED",
                "started_at": utcnow(), "completed_at": None, "created_at": utcnow(),
            })
            schedule_config = json.loads(workflow["schedule_config_json"]) if workflow.get("schedule_config_json") else None
            self.store.update(
                "wf_recurring_workflows", workflow["id"], last_run_at=utcnow(),
                next_due_at=_compute_next_due(workflow["schedule_kind"], schedule_config),
            )
            self.audit.append("WF_RECURRING_WORKFLOW_RUN", {"workflow_id": workflow["id"], "run_id": run_id, "task_id": task_id, "actor": actor})
            created_task_ids.append(task_id)
        return created_task_ids

    def _require(self, workflow_id: str) -> Dict[str, Any]:
        workflow = self.store.get("wf_recurring_workflows", workflow_id)
        if not workflow:
            raise RecurringWorkflowError("recurring workflow not found")
        return workflow

    def get(self, workflow_id: str) -> Optional[Dict[str, Any]]:
        return self.store.get("wf_recurring_workflows", workflow_id)

    def list(self) -> List[Dict[str, Any]]:
        return list(reversed(self.store.list("wf_recurring_workflows")))
