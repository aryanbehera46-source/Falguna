"""TTT HQ: Boardroom, Master Vision Backlog, and the unified Needs Aryan queue.

Hierarchy this module encodes (PASS 1, item 5):
    Aryan -> Twenty Two Technologies Pvt. Ltd. (TTT) -> Falguna + other TTT products.
TTT HQ owns company decisions, revenue ops, ventures, Boardroom, and owner
approvals. Falguna Engineering (orchestrator.py / web.py's existing mission
control plane) powers the underlying intelligence/work and is left untouched
by this module -- it is composed with, not modified.

Persistence note: every mutation here goes through StateStore (SQLite), the
same durable adapter Falguna Engineering already uses. Nothing is held only
in memory. This directly addresses the defect an earlier self-build attempt
was correctly rejected for (see .falguna/evidence/f6a35494-...): a Boardroom
decision that only appended to the audit log and never wrote to the store
that reads happen from, so decisions vanished on reload.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import AuditLog
from .store import StateStore, utcnow

PERSPECTIVES = ["Strategy", "Technology", "Revenue", "Finance-Risk", "Operations"]

BOARDROOM_ACTIONS = {"approve": "APPROVED", "reject": "REJECTED", "defer": "DEFERRED", "request-changes": "CHANGES_REQUESTED"}
BACKLOG_STATUSES = ["Future", "Planned", "Active", "Done", "Deferred"]
NEEDS_ARYAN_KINDS = {
    "strategic_approval", "proposal_approval", "pricing_decision", "scope_expansion",
    "risky_action", "final_delivery_approval", "client_response_decision",
    # Added for Revenue Hunter (PASS 2): outreach and negotiation-response
    # approvals are business decisions like the others above, surfaced
    # through this same queue rather than a parallel approval system.
    "outreach_approval", "negotiation_response_approval",
}
NEEDS_ARYAN_ACTIONS = {"approve": "APPROVED", "reject": "REJECTED", "defer": "DEFERRED", "request-changes": "CHANGES_REQUESTED"}


class BoardroomStore:
    """Discussion threads with named perspectives and a persistent decision history."""

    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit

    def create_topic(
        self, title: str, summary: str, actor: str,
        proposed_category: Optional[str] = None, proposed_phase: Optional[str] = None,
        proposed_priority: Optional[str] = None, proposed_revenue_impact: Optional[str] = None,
    ) -> str:
        if not title or not title.strip():
            raise ValueError("title is required")
        if not summary or not summary.strip():
            raise ValueError("summary is required")
        now = utcnow()
        topic_id = self.store.create("boardroom_topics", {
            "title": title.strip(), "summary": summary.strip(), "status": "OPEN",
            "proposed_category": proposed_category, "proposed_phase": proposed_phase,
            "proposed_priority": proposed_priority, "proposed_revenue_impact": proposed_revenue_impact,
            "created_by": actor, "created_at": now, "updated_at": now,
        })
        self.audit.append("BOARDROOM_TOPIC_CREATED", {"topic_id": topic_id, "title": title, "actor": actor})
        return topic_id

    def add_contribution(self, topic_id: str, perspective: str, content: str) -> str:
        if perspective not in PERSPECTIVES:
            raise ValueError(f"perspective must be one of {PERSPECTIVES}")
        if not self.store.get("boardroom_topics", topic_id):
            raise ValueError("topic not found")
        if not content or not content.strip():
            raise ValueError("content is required")
        contribution_id = self.store.create("boardroom_contributions", {
            "topic_id": topic_id, "perspective": perspective, "content": content.strip(), "created_at": utcnow(),
        })
        self.audit.append("BOARDROOM_CONTRIBUTION_ADDED", {"topic_id": topic_id, "perspective": perspective})
        return contribution_id

    def decide(self, topic_id: str, action: str, actor: str, note: Optional[str] = None, backlog_store: Optional["BacklogStore"] = None) -> Dict[str, Any]:
        topic = self.store.get("boardroom_topics", topic_id)
        if not topic:
            raise ValueError("topic not found")
        if action not in BOARDROOM_ACTIONS:
            raise ValueError(f"action must be one of {sorted(BOARDROOM_ACTIONS)}")
        backlog_item_id = None
        if action == "approve" and backlog_store is not None:
            backlog_item_id = backlog_store.create_from_boardroom_topic(topic, actor)
        decision_id = self.store.create("boardroom_decisions", {
            "topic_id": topic_id, "action": BOARDROOM_ACTIONS[action], "note": note,
            "decided_by": actor, "backlog_item_id": backlog_item_id, "created_at": utcnow(),
        })
        new_status = "DECIDED" if action in {"approve", "reject"} else topic["status"]
        self.store.update("boardroom_topics", topic_id, status=new_status)
        self.audit.append("BOARDROOM_DECISION_RECORDED", {
            "topic_id": topic_id, "action": BOARDROOM_ACTIONS[action], "actor": actor, "backlog_item_id": backlog_item_id,
        })
        return {"decision_id": decision_id, "backlog_item_id": backlog_item_id}

    def get_topic(self, topic_id: str) -> Optional[Dict[str, Any]]:
        topic = self.store.get("boardroom_topics", topic_id)
        if not topic:
            return None
        topic = dict(topic)
        topic["contributions"] = self.store.list("boardroom_contributions", "topic_id=?", (topic_id,))
        topic["decisions"] = self.store.list("boardroom_decisions", "topic_id=?", (topic_id,))
        return topic

    def list_topics(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        if status:
            return list(reversed(self.store.list("boardroom_topics", "status=?", (status,))))
        return list(reversed(self.store.list("boardroom_topics")))


class BacklogStore:
    """The Master Vision Backlog. Every field change is logged to backlog_history."""

    STATUSES = BACKLOG_STATUSES

    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit

    def create_item(
        self, title: str, actor: str, category: Optional[str] = None, phase: Optional[str] = None,
        priority: Optional[str] = None, dependency: Optional[str] = None, revenue_impact: Optional[str] = None,
        status: str = "Future", source_boardroom_topic_id: Optional[str] = None,
    ) -> str:
        if not title or not title.strip():
            raise ValueError("title is required")
        if status not in self.STATUSES:
            raise ValueError(f"status must be one of {self.STATUSES}")
        now = utcnow()
        item_id = self.store.create("backlog_items", {
            "title": title.strip(), "category": category, "phase": phase, "priority": priority,
            "dependency": dependency, "revenue_impact": revenue_impact, "status": status,
            "source_boardroom_topic_id": source_boardroom_topic_id, "created_at": now, "updated_at": now,
        })
        self._record_history(item_id, "status", None, status, actor, "created")
        self.audit.append("BACKLOG_ITEM_CREATED", {"item_id": item_id, "title": title, "status": status, "actor": actor})
        return item_id

    def update_item(self, item_id: str, actor: str, reason: Optional[str] = None, **fields: Any) -> Dict[str, Any]:
        current = self.store.get("backlog_items", item_id)
        if not current:
            raise ValueError("backlog item not found")
        allowed_fields = {"title", "category", "phase", "priority", "dependency", "revenue_impact", "status"}
        unknown = set(fields) - allowed_fields
        if unknown:
            raise ValueError(f"unknown fields: {sorted(unknown)}")
        if "status" in fields and fields["status"] not in self.STATUSES:
            raise ValueError(f"status must be one of {self.STATUSES}")
        changes = {k: v for k, v in fields.items() if current.get(k) != v}
        if not changes:
            return current
        for field, new_value in changes.items():
            self._record_history(item_id, field, current.get(field), new_value, actor, reason)
        self.store.update("backlog_items", item_id, **changes)
        self.audit.append("BACKLOG_ITEM_UPDATED", {"item_id": item_id, "changes": changes, "actor": actor})
        return self.store.get("backlog_items", item_id)

    def _record_history(self, item_id: str, field: str, old_value, new_value, actor: str, reason: Optional[str]) -> None:
        self.store.create("backlog_history", {
            "item_id": item_id, "field": field,
            "old_value": None if old_value is None else str(old_value),
            "new_value": None if new_value is None else str(new_value),
            "actor": actor, "reason": reason, "created_at": utcnow(),
        })

    def create_from_boardroom_topic(self, topic: Dict[str, Any], actor: str) -> str:
        return self.create_item(
            title=topic["title"], actor=actor,
            category=topic.get("proposed_category"), phase=topic.get("proposed_phase"),
            priority=topic.get("proposed_priority"), revenue_impact=topic.get("proposed_revenue_impact"),
            status="Planned", source_boardroom_topic_id=topic["id"],
        )

    def get_item(self, item_id: str) -> Optional[Dict[str, Any]]:
        item = self.store.get("backlog_items", item_id)
        if not item:
            return None
        item = dict(item)
        item["history"] = self.store.list("backlog_history", "item_id=?", (item_id,))
        return item

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        if status:
            return list(reversed(self.store.list("backlog_items", "status=?", (status,))))
        return list(reversed(self.store.list("backlog_items")))


class NeedsAryanQueue:
    """Centralized owner-facing approval queue.

    Unifies two sources, in one list, rather than living as two separate
    screens:
      1. Business-originated items created directly here (proposal approvals,
         pricing decisions, client-response decisions, etc.) -- fully owned
         by this module, persisted in needs_aryan_items.
      2. Falguna Engineering missions currently sitting in a NEEDS_APPROVAL
         supervisor state -- read live from supervisor_states, never copied,
         so this queue can never drift from the engineering control plane's
         own source of truth.

    Deciding an engineering-derived item never touches Falguna Engineering's
    tables directly except through its own existing, tested entrypoint
    (ControlPlane.decide_merge) -- this module adds zero new ways to mutate
    mission state. If a mission is stuck in NEEDS_APPROVAL for a reason other
    than the final merge decision (e.g. mid-run scope expansion), decide_merge
    would correctly refuse it (no pending PROTECTED_BRANCH_MERGE approval to
    decide), so those items are surfaced as inspect-only: real status, no
    button that would silently fail or fake success.
    """

    def __init__(self, store: StateStore, audit: AuditLog, control=None):
        self.store = store
        self.audit = audit
        self.control = control

    def create_item(
        self, kind: str, title: str, what_is_needed: str, actor: str = "system",
        recommendation: Optional[str] = None, rationale: Optional[str] = None,
        risk: Optional[str] = None, expected_value: Optional[str] = None,
        ref_type: Optional[str] = None, ref_id: Optional[str] = None,
    ) -> str:
        if kind not in NEEDS_ARYAN_KINDS:
            raise ValueError(f"kind must be one of {sorted(NEEDS_ARYAN_KINDS)}")
        if not title or not title.strip():
            raise ValueError("title is required")
        if not what_is_needed or not what_is_needed.strip():
            raise ValueError("what_is_needed is required")
        now = utcnow()
        item_id = self.store.create("needs_aryan_items", {
            "kind": kind, "ref_type": ref_type, "ref_id": ref_id, "title": title.strip(),
            "what_is_needed": what_is_needed.strip(), "recommendation": recommendation, "rationale": rationale,
            "risk": risk, "expected_value": expected_value, "status": "PENDING",
            "decision_note": None, "decided_by": None, "created_at": now, "updated_at": now, "decided_at": None,
        })
        self.audit.append("NEEDS_ARYAN_ITEM_CREATED", {"item_id": item_id, "kind": kind, "title": title})
        return item_id

    def _engineering_items(self) -> List[Dict[str, Any]]:
        items = []
        for state in self.store.list("supervisor_states", "outcome_class=?", ("NEEDS_APPROVAL",)):
            run = self.store.get("runs", state["run_id"])
            if not run:
                continue
            merge_decisions = self.store.list("approvals", "run_id=? AND kind=?", (run["id"], "PROTECTED_BRANCH_MERGE"))
            if merge_decisions and merge_decisions[-1]["status"] != "PENDING":
                # Aryan already decided the merge for this run through the real
                # decide_merge path (here or in Falguna Engineering directly).
                # The underlying supervisor_state only changes when the run
                # itself resumes -- that's Falguna's own behavior, untouched --
                # so without this check a decided item would linger here
                # showing PENDING even though it was already acted on.
                continue
            task = self.store.get("tasks", run["task_id"])
            requirement = self.store.get("requirements", task["requirement_id"]) if task else None
            mission = self.store.get("missions", requirement["mission_id"]) if requirement else None
            pending_merge = self.store.list("approvals", "run_id=? AND kind=? AND status=?", (run["id"], "PROTECTED_BRANCH_MERGE", "PENDING"))
            items.append({
                "id": f"run:{run['id']}",
                "kind": "final_delivery_approval" if pending_merge else "risky_action",
                "ref_type": "run", "ref_id": run["id"],
                "title": mission["title"] if mission else run["id"],
                "what_is_needed": state.get("decision_needed") or f"Engineering mission needs a decision (category: {state['category']})",
                "recommendation": "Approve/reject the pending merge in Falguna Engineering" if pending_merge else "Open this mission in Falguna Engineering to inspect and resume",
                "rationale": state.get("eligibility_reason"),
                "risk": state.get("category"),
                "expected_value": None,
                "status": "PENDING",
                "decision_note": None, "decided_by": None,
                "created_at": state["created_at"], "decided_at": None,
                "actionable": bool(pending_merge),
                "source": "falguna_engineering",
            })
        return items

    def list_pending(self) -> List[Dict[str, Any]]:
        business = [dict(item, source="business", actionable=True) for item in reversed(self.store.list("needs_aryan_items", "status=?", ("PENDING",)))]
        return sorted(business + self._engineering_items(), key=lambda item: item["created_at"], reverse=True)

    def list_decided(self, limit: int = 20) -> List[Dict[str, Any]]:
        rows = [dict(item, source="business") for item in self.store.list("needs_aryan_items", "status!=?", ("PENDING",))]
        return list(reversed(rows))[:limit]

    def decide(self, item_id: str, action: str, actor: str, note: Optional[str] = None) -> Dict[str, Any]:
        if action not in NEEDS_ARYAN_ACTIONS:
            raise ValueError(f"action must be one of {sorted(NEEDS_ARYAN_ACTIONS)}")

        if item_id.startswith("run:"):
            run_id = item_id[len("run:"):]
            if action == "defer":
                self.audit.append("NEEDS_ARYAN_ENGINEERING_ITEM_DEFERRED", {"run_id": run_id, "actor": actor})
                return {"run_id": run_id, "action": "DEFERRED", "note": "deferred at the queue level; mission state unchanged"}
            pending_merge = self.store.list("approvals", "run_id=? AND kind=? AND status=?", (run_id, "PROTECTED_BRANCH_MERGE", "PENDING"))
            if not pending_merge:
                raise ValueError(
                    "this mission is not at the final merge decision yet -- open it in Falguna Engineering "
                    "to inspect diagnostics and Resume after adjusting scope, rather than approve/reject here"
                )
            if self.control is None:
                raise ValueError("no Falguna Engineering control plane available to record this decision")
            self.control.decide_merge(run_id, action, actor, note or "Decided via Needs Aryan queue")
            return {"run_id": run_id, "action": NEEDS_ARYAN_ACTIONS[action]}

        item = self.store.get("needs_aryan_items", item_id)
        if not item:
            raise ValueError("needs-aryan item not found")
        if item["status"] != "PENDING":
            raise ValueError("this item has already been decided")
        self.store.update("needs_aryan_items", item_id, status=NEEDS_ARYAN_ACTIONS[action], decision_note=note, decided_by=actor, decided_at=utcnow())
        self.audit.append("NEEDS_ARYAN_DECIDED", {"item_id": item_id, "action": NEEDS_ARYAN_ACTIONS[action], "actor": actor})
        return {"item_id": item_id, "action": NEEDS_ARYAN_ACTIONS[action]}


def hq_overview() -> Dict[str, Any]:
    """Static org-structure data for the TTT HQ landing area (PASS 1, item 5)."""
    return {
        "owner": "Aryan",
        "company": "Twenty Two Technologies Pvt. Ltd.",
        "products": [
            {"name": "Falguna", "role": "Engineering intelligence/work execution (isolated worktrees, verification, review, supervisor/recovery)"},
            {"name": "Revenue Hunter", "role": "Client acquisition -- built into TTT HQ, separate from Falguna Engineering; see falguna/handoff.py for the Won -> Active Job integration boundary"},
        ],
        "hierarchy": "Aryan -> Twenty Two Technologies Pvt. Ltd. -> Falguna + other TTT products",
        "note": "TTT HQ owns company decisions, revenue ops, ventures, Boardroom, and owner approvals. Falguna powers the underlying work.",
    }
