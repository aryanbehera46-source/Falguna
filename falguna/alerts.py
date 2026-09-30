"""Operational Alerts V1 (Phase 4 Sprint 2).

Deterministic, rule-based alerts computed fresh from real persisted state
on every call -- never a parallel business record and never a fabricated
risk score. An alert's id is a stable hash of (category, ref_type,
ref_id), so the same underlying problem always produces the same alert id
across calls (dedup), and once the underlying condition clears, the alert
simply stops being generated on the next call: a resolved problem never
lingers as "active" because nothing here persists the alert itself.

The one piece of state this module does persist is a human's
acknowledgement -- via the existing `company_os.EventBus` (co_events),
never a new table -- so acknowledging a still-active problem does not
require inventing separate alert storage.
"""

import hashlib
import json
from typing import Any, Dict, List, Optional

from .audit import AuditLog
from .company_os import EventBus
from .decisions import HIGH_RISK_KINDS
from .onboarding import OnboardingStore
from .revenue_delivery import RevenueDeliveryError, RevenueDeliveryService
from .store import StateStore, utcnow

ALERT_SEVERITY = {
    "quotation_awaiting_approval": "MEDIUM",
    "project_awaiting_intake": "LOW",
    "blocked_execution": "HIGH",
    "repeated_workforce_failure": "HIGH",
    "failed_qa": "HIGH",
    "handover_awaiting_approval": "MEDIUM",
    "overdue_invoice": "HIGH",
    "unresolved_partner_conflict": "MEDIUM",
    "held_commission": "MEDIUM",
    "unresolved_high_risk_decision": "HIGH",
}

_QA_TASK_TYPE_HINTS = ("qa", "verification")
_SEVERITY_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}


def _alert_id(category: str, ref_type: str, ref_id: str) -> str:
    return hashlib.sha256(f"{category}:{ref_type}:{ref_id}".encode()).hexdigest()[:24]


class AlertAckStore:
    """Persists only the human acknowledgement, via the existing company
    event bus. The alert itself is always recomputed live, so
    acknowledging a still-active problem never hides it, and a resolved
    problem needs no explicit 'resolve' action -- it stops being
    generated on its own the moment the underlying state clears."""

    def __init__(self, store: StateStore, audit: AuditLog):
        self.events = EventBus(store, audit)

    def get(self, alert_id: str) -> Optional[Dict[str, Any]]:
        rows = self.events.list(event_type="OP_ALERT_ACKNOWLEDGED", ref_type="op_alert", ref_id=alert_id, limit=1)
        return rows[0] if rows else None

    def acknowledge(self, alert_id: str, actor: str, note: Optional[str] = None) -> Dict[str, Any]:
        if not actor or not str(actor).strip():
            raise ValueError("actor is required")
        return self.events.emit(
            "OP_ALERT_ACKNOWLEDGED", "operational_alerts", ref_type="op_alert", ref_id=alert_id,
            payload={"actor": actor, "note": note},
        )


def _ack_info(ack: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not ack:
        return {"acknowledged": False, "acknowledged_by": None, "acknowledged_at": None, "note": None}
    try:
        payload = json.loads(ack.get("payload_json") or "{}")
    except (TypeError, ValueError):
        payload = {}
    return {
        "acknowledged": True, "acknowledged_by": payload.get("actor"),
        "acknowledged_at": ack.get("created_at"), "note": payload.get("note"),
    }


def _mk_alert(category: str, ref_type: str, ref_id: str, source: str, explanation: str, current_state: str, acks: AlertAckStore) -> Dict[str, Any]:
    alert_id = _alert_id(category, ref_type, ref_id)
    return {
        "id": alert_id, "category": category, "severity": ALERT_SEVERITY[category],
        "source": source, "affected_entity": {"type": ref_type, "id": ref_id},
        "explanation": explanation, "current_state": current_state, "computed_at": utcnow(),
        **_ack_info(acks.get(alert_id)),
    }


def alerts_snapshot(store: StateStore, audit: AuditLog) -> Dict[str, Any]:
    acks = AlertAckStore(store, audit)
    alerts: List[Dict[str, Any]] = []
    today = utcnow()[:10]

    for item in store.list("needs_aryan_items", "kind=? AND status=?", ("proposal_approval", "PENDING")):
        alerts.append(_mk_alert(
            "quotation_awaiting_approval", "rh_proposal", item.get("ref_id") or item["id"],
            "Revenue Hunter", f"Proposal awaiting approval: {item['title']}", "PENDING", acks,
        ))
    for item in store.list("needs_aryan_items", "kind=? AND status=?", ("pricing_decision", "PENDING")):
        alerts.append(_mk_alert(
            "quotation_awaiting_approval", "rh_closing_package", item.get("ref_id") or item["id"],
            "Revenue Hunter", f"Closing package awaiting approval: {item['title']}", "PENDING", acks,
        ))

    onboarding = OnboardingStore(store, audit)
    for closing in store.list("rh_closing_records"):
        opp_id = closing["opportunity_id"]
        summary = onboarding.completion_summary(opp_id)
        if not summary["complete"]:
            opp = store.get("rh_opportunities", opp_id)
            title = opp["title"] if opp else opp_id
            alerts.append(_mk_alert(
                "project_awaiting_intake", "rh_opportunities", opp_id, "Revenue & Delivery Engine",
                f"Project intake incomplete for {title} ({summary['received']}/{summary['total']} received)",
                f"{summary['missing']} missing, {summary['blocked']} blocked", acks,
            ))

    for task in store.list("wf_tasks", "status IN (?,?)", ("BLOCKED", "NEEDS_ARYAN")):
        alerts.append(_mk_alert(
            "blocked_execution", "wf_tasks", task["id"], "Digital Workforce",
            f"Workforce task blocked: {task['objective']}", task["status"], acks,
        ))

    for task in store.list("wf_tasks", "status=?", ("FAILED",)):
        if (task.get("retries") or 0) > 0:
            alerts.append(_mk_alert(
                "repeated_workforce_failure", "wf_tasks", task["id"], "Digital Workforce",
                f"Workforce task failed after {task.get('retries')} retr"
                f"{'y' if task.get('retries') == 1 else 'ies'}: {task['objective']}",
                "FAILED", acks,
            ))

    for task in store.list("wf_tasks"):
        task_type = (task.get("task_type") or "").lower()
        if task["status"] in ("FAILED", "BLOCKED", "NEEDS_ARYAN") and any(h in task_type for h in _QA_TASK_TYPE_HINTS):
            alerts.append(_mk_alert(
                "failed_qa", "wf_tasks", task["id"], "Independent QA",
                f"QA task {task['status'].lower()}: {task['objective']}", task["status"], acks,
            ))

    delivery = RevenueDeliveryService(store, audit)
    for opp in store.list("rh_opportunities"):
        try:
            snap = delivery.snapshot(opp["id"])
        except RevenueDeliveryError:
            continue
        by_key = {s["key"]: s for s in snap["steps"]}
        if by_key["delivery"]["complete"] and not by_key["handover"]["complete"]:
            alerts.append(_mk_alert(
                "handover_awaiting_approval", "rh_opportunities", opp["id"], "Revenue & Delivery Engine",
                f"Delivery linked and ready for evidence-backed handover: {opp['title']}",
                "AWAITING_HANDOVER", acks,
            ))

    for invoice in store.list("rh_invoices", "status IN (?,?)", ("SENT", "PARTIALLY_PAID")):
        due = invoice.get("due_date")
        if due and str(due)[:10] < today:
            alerts.append(_mk_alert(
                "overdue_invoice", "rh_invoices", invoice["id"], "Billing",
                f"Invoice overdue since {due}: {invoice.get('amount')} {invoice.get('currency')}",
                invoice["status"], acks,
            ))

    for review in store.list("pm_duplicate_reviews", "status=?", ("OPEN",)):
        alerts.append(_mk_alert(
            "unresolved_partner_conflict", "pm_duplicate_reviews", review["id"], "Sales Partners",
            f"Unresolved duplicate/attribution conflict: {review.get('detected_reason') or 'match'}",
            "OPEN", acks,
        ))

    for commission in store.list("pm_commissions", "status=?", ("HELD",)):
        alerts.append(_mk_alert(
            "held_commission", "pm_commissions", commission["id"], "Sales Partners",
            f"Commission held: {commission.get('hold_reason') or 'no reason recorded'}", "HELD", acks,
        ))

    for item in store.list("needs_aryan_items", "status=?", ("PENDING",)):
        if item.get("kind") in HIGH_RISK_KINDS:
            alerts.append(_mk_alert(
                "unresolved_high_risk_decision", "needs_aryan_items", item["id"], "Company OS",
                f"High-risk decision pending: {item['title']}", "PENDING", acks,
            ))

    alerts.sort(key=lambda a: (_SEVERITY_ORDER[a["severity"]], a["acknowledged"]))
    active = [a for a in alerts if not a["acknowledged"]]
    return {
        "generated_at": utcnow(),
        "total": len(alerts), "active_count": len(active),
        "by_severity": {lvl: sum(1 for a in active if a["severity"] == lvl) for lvl in ("HIGH", "MEDIUM", "LOW")},
        "alerts": alerts,
        "source": "rh_/wf_/pm_/needs_aryan_items (live, deterministic rules) + company_os.EventBus (acknowledgement only)",
    }
