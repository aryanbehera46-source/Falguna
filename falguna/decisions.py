"""Unified Decision Queue V1 (Phase 4 Sprint 2).

A single read model over the existing owner-approval queue
(`ttt_hq.NeedsAryanQueue`, table `needs_aryan_items`, plus its own
already-integrated Falguna Engineering items). This module creates no new
decision table and no new approval path: every item this queue lists is
still decided through the exact same, already-tested
`POST /api/needs-aryan/<id>/decision` endpoint and its existing
ref_type-specific side effects (rh_proposal, rh_closing_package,
cc_reserve_policy, comm_conversation, and the Falguna Engineering merge
decision path) -- this module never bypasses that dispatch with a generic
approval button.

Urgency is a small, transparent, rule-based label (age + a fixed
high-risk kind list), never a fabricated numeric "confidence" score --
consistent with the project's existing no-fabricated-certainty stance.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .audit import AuditLog
from .store import StateStore, utcnow
from .ttt_hq import NeedsAryanQueue

HIGH_RISK_KINDS = {
    "risky_action", "major_client_risk", "risk_escalation", "financial_writeoff",
    "financial_refund", "large_financial_commitment", "venture_risk_escalation",
    "venture_kill_recommendation", "trading_risk_breach", "capital_allocation_execution",
    "reserve_policy_change", "budget_override", "policy_exception",
}

DEPARTMENT_BY_REF_TYPE = {
    "rh_proposal": "Revenue Hunter", "rh_closing_package": "Revenue Hunter",
    "comm_conversation": "Communications", "cc_reserve_policy": "Finance & Capital",
    "run": "Falguna Engineering", "co_decisions": "Company OS",
}

VIEW_BY_REF_TYPE = {
    "rh_proposal": "rhPipeline", "rh_closing_package": "rhDeliveryEngine",
    "comm_conversation": "communications", "cc_reserve_policy": "ccCapital",
    "co_decisions": "coDecisions",
}


def _age_hours(created_at: Optional[str]) -> Optional[float]:
    if not created_at:
        return None
    try:
        created = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - created).total_seconds() / 3600.0
    except (ValueError, TypeError):
        return None


def _urgency(item: Dict[str, Any]) -> str:
    if item.get("kind") in HIGH_RISK_KINDS:
        return "HIGH"
    age_hours = _age_hours(item.get("created_at"))
    if age_hours is not None and age_hours >= 48:
        return "HIGH"
    if age_hours is not None and age_hours >= 12:
        return "MEDIUM"
    return "LOW"


def normalize_decision(item: Dict[str, Any]) -> Dict[str, Any]:
    ref_type = item.get("ref_type")
    status = item.get("status", "PENDING")
    actionable = item.get("actionable", True)
    return {
        "id": item["id"],
        "department": DEPARTMENT_BY_REF_TYPE.get(ref_type, "Company OS"),
        "ref_type": ref_type, "ref_id": item.get("ref_id"),
        "decision_type": item.get("kind"),
        "status": status,
        "urgency": _urgency(item),
        "reason": item.get("what_is_needed"),
        "financial_impact": item.get("expected_value"),
        "evidence": {
            "recommendation": item.get("recommendation"),
            "rationale": item.get("rationale"),
            "risk": item.get("risk"),
        },
        "requested_action": item.get("title"),
        "authorized_actions": ["approve", "reject", "defer", "request-changes"] if (actionable and status == "PENDING") else [],
        "created_at": item.get("created_at"),
        "responsible_human": "Aryan",
        "decided_at": item.get("decided_at"),
        "outcome": status if status != "PENDING" else None,
        "source_link": {"view": VIEW_BY_REF_TYPE.get(ref_type), "ref_type": ref_type, "ref_id": item.get("ref_id")},
        "source": item.get("source", "business"),
        "actionable": actionable,
    }


def unified_decision_queue(
    store: StateStore, audit: AuditLog, control: Any = None, *,
    status: Optional[str] = None, kind: Optional[str] = None, ref_type: Optional[str] = None,
    decided_limit: int = 50,
) -> Dict[str, Any]:
    """One list combining pending + recently-decided items, normalized to
    the fields Phase 4 Sprint 2 Section 4 requires. Every item is still
    decided through NeedsAryanQueue.decide() (the existing endpoint) --
    this function only reads and reshapes."""
    queue = NeedsAryanQueue(store, audit, control)
    raw = queue.list_pending() + queue.list_decided(limit=decided_limit)
    items = [normalize_decision(item) for item in raw]
    if status:
        items = [i for i in items if i["status"] == status]
    if kind:
        items = [i for i in items if i["decision_type"] == kind]
    if ref_type:
        items = [i for i in items if i["ref_type"] == ref_type]
    pending_count = sum(1 for i in items if i["status"] == "PENDING")
    return {
        "generated_at": utcnow(),
        "total": len(items),
        "pending_count": pending_count,
        "by_urgency": {level: sum(1 for i in items if i["status"] == "PENDING" and i["urgency"] == level) for level in ("HIGH", "MEDIUM", "LOW")},
        "items": items,
        "source": (
            "needs_aryan_items + supervisor_states, via the existing NeedsAryanQueue -- "
            "decided only through POST /api/needs-aryan/<id>/decision"
        ),
    }
