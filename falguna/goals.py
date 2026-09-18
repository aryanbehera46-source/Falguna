"""TTT Company Goal Engine v1 (Section 4 of the Command Center / CEO
Intelligence + Finance / Capital Engine phase).

Persistent company goals Aryan sets explicitly. This module never invents a
goal, never changes one on its own, and never silently marks a goal
ACHIEVED/PAUSED/CANCELLED except the one narrow, honest case documented on
`update_progress`. `recommended_actions` derives Goal -> department
objective -> recommended action purely from a goal's own stored fields --
it is read-only text, and it never writes to the Master Vision Backlog
(`falguna/ttt_hq.py`'s `BacklogStore`); any real change there still goes
through an explicit Boardroom/Needs Aryan decision, exactly like every
other cross-system write in this codebase.
"""

import json
from typing import Any, Dict, List, Optional

from .audit import AuditLog
from .store import StateStore, utcnow

GOAL_STATUSES = {"ACTIVE", "ACHIEVED", "PAUSED", "CANCELLED"}


class GoalError(ValueError):
    pass


class GoalStore:
    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit

    def create(
        self, title: str, target: float, unit: str, actor: str = "Aryan",
        start_date: Optional[str] = None, deadline: Optional[str] = None,
        owner: Optional[str] = None, department: Optional[str] = None,
        linked_kpis: Optional[List[str]] = None, linked_actions: Optional[List[str]] = None,
        venture_id: Optional[str] = None,
    ) -> str:
        if not title or not title.strip():
            raise GoalError("title is required")
        if target is None:
            raise GoalError("target is required")
        if not unit or not unit.strip():
            raise GoalError("unit is required")
        now = utcnow()
        goal_id = self.store.create("cc_goals", {
            "title": title.strip(), "target": target, "unit": unit.strip(),
            "start_date": start_date, "deadline": deadline, "current_value": 0.0,
            "owner": owner, "department": department, "status": "ACTIVE",
            "linked_kpis_json": json.dumps(linked_kpis or []),
            "linked_actions_json": json.dumps(linked_actions or []),
            # Venture Studio v1 (Section 6): a goal optionally belongs to one
            # venture, so it rolls up into both that venture's scorecard and
            # the company-wide goal list untouched -- this reuses the
            # existing goal engine rather than building a parallel one.
            "venture_id": venture_id,
            "actor": actor, "created_at": now, "updated_at": now,
        })
        self.audit.append("CC_GOAL_CREATED", {"goal_id": goal_id, "title": title, "target": target, "unit": unit, "actor": actor})
        return goal_id

    def update_progress(self, goal_id: str, new_value: float, actor: str, note: Optional[str] = None) -> Dict[str, Any]:
        goal = self._require(goal_id)
        from_value = goal["current_value"]
        self.store.update("cc_goals", goal_id, current_value=new_value)
        self.store.create("cc_goal_progress_events", {
            "goal_id": goal_id, "from_value": from_value, "to_value": new_value,
            "actor": actor, "note": note, "created_at": utcnow(),
        })
        self.audit.append("CC_GOAL_PROGRESS_UPDATED", {"goal_id": goal_id, "from": from_value, "to": new_value, "actor": actor})
        # The one auto-transition this module makes: ACTIVE -> ACHIEVED, and
        # only forward, only when the real new value has genuinely reached
        # the real target. It never auto-un-achieves, auto-pauses, or
        # auto-cancels -- those stay owner decisions via set_status.
        goal = self._require(goal_id)
        if goal["status"] == "ACTIVE" and goal["target"] and new_value >= goal["target"]:
            self.store.update("cc_goals", goal_id, status="ACHIEVED")
            self.audit.append("CC_GOAL_ACHIEVED", {"goal_id": goal_id, "actor": actor})
        return self.get(goal_id)

    def set_status(self, goal_id: str, status: str, actor: str, reason: Optional[str] = None) -> Dict[str, Any]:
        if status not in GOAL_STATUSES:
            raise GoalError(f"status must be one of {sorted(GOAL_STATUSES)}")
        self._require(goal_id)
        self.store.update("cc_goals", goal_id, status=status)
        self.audit.append("CC_GOAL_STATUS_CHANGED", {"goal_id": goal_id, "status": status, "actor": actor, "reason": reason})
        return self.get(goal_id)

    def progress_history(self, goal_id: str) -> List[Dict[str, Any]]:
        return self.store.list("cc_goal_progress_events", "goal_id=?", (goal_id,))

    def _require(self, goal_id: str) -> Dict[str, Any]:
        goal = self.store.get("cc_goals", goal_id)
        if not goal:
            raise GoalError("goal not found")
        return goal

    def get(self, goal_id: str) -> Optional[Dict[str, Any]]:
        goal = self.store.get("cc_goals", goal_id)
        if not goal:
            return None
        return self._with_computed_fields(goal)

    def list(self, status: Optional[str] = None, department: Optional[str] = None, venture_id: Optional[str] = None) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if status:
            clauses.append("status=?"); params.append(status)
        if department:
            clauses.append("department=?"); params.append(department)
        if venture_id:
            clauses.append("venture_id=?"); params.append(venture_id)
        where = " AND ".join(clauses) if clauses else "1=1"
        rows = self.store.list("cc_goals", where, tuple(params))
        return [self._with_computed_fields(g) for g in reversed(rows)]

    def _with_computed_fields(self, goal: Dict[str, Any]) -> Dict[str, Any]:
        goal = dict(goal)
        progress = None
        if goal.get("target"):
            progress = round((goal["current_value"] or 0.0) / goal["target"], 4)
        goal["progress"] = progress
        # Evidence-based, not a guess: only flagged when a real stored
        # deadline has actually passed and the goal is still ACTIVE.
        goal["at_risk"] = bool(
            goal.get("deadline") and goal["status"] == "ACTIVE" and goal["deadline"] < utcnow()[:10]
        )
        goal["linked_kpis"] = json.loads(goal["linked_kpis_json"]) if goal.get("linked_kpis_json") else []
        goal["linked_actions"] = json.loads(goal["linked_actions_json"]) if goal.get("linked_actions_json") else []
        return goal

    def recommended_actions(self, goal_id: str) -> List[str]:
        """Goal -> department objective -> recommended action (Section 4).
        Plain, derived-only text -- never applied automatically anywhere."""
        goal = self._require(goal_id)
        if goal["status"] != "ACTIVE":
            return []
        recs: List[str] = []
        progress = (goal["current_value"] or 0.0) / goal["target"] if goal.get("target") else None
        if progress is not None and progress < 1.0:
            remaining = goal["target"] - (goal["current_value"] or 0.0)
            recs.append(
                f"{goal['title']}: {remaining} {goal['unit']} remaining to reach target "
                f"({round(progress * 100, 1)}% there)."
            )
        if goal.get("deadline") and goal["deadline"] < utcnow()[:10] and progress is not None and progress < 1.0:
            recs.append(
                f"{goal['title']} is past its deadline ({goal['deadline']}) and not yet achieved -- "
                "review with Aryan: revise the target, extend the deadline, or mark it cancelled."
            )
        return recs
