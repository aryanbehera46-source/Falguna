"""TTT Command Center + CEO Brief v1.

Part of "TTT COMMAND CENTER / CEO INTELLIGENCE + FINANCE / CAPITAL ENGINE
V1" (Pass A). This is the top-level management layer inside TTT HQ: it
aggregates live data already produced by existing, already-tested systems
(Revenue Hunter, billing, Digital Workforce, Media/Growth, Needs Aryan) so
Aryan can open TTT HQ and immediately see what the company is doing, where
money is coming from, what is owed, what is due, what is blocked, and what
needs attention -- without rebuilding any of those systems.

Two non-negotiable rules, consistent with every other module in this
codebase:
  * Every number in `command_center_snapshot` carries a `source` string
    naming exactly which table(s) it was read from. Nothing here is
    recomputed with hidden assumptions.
  * `cash_in_to_date` is explicitly NOT a "cash on hand" figure. This
    codebase does not yet track outflows or non-invoice inflows (that is
    the Finance Ledger, a later pass of this same phase) -- reporting a
    netted cash position now would be a fabricated number wearing a real
    one's clothes. What is reported is the sum of real, evidence-backed
    payments already recorded on `rh_invoices` (BillingStore.record_payment
    hard-requires evidence for every entry), clearly labeled as inflow-only.

The CEO Brief (`CEOBriefStore`) is a persistent, generated-on-demand
summary of what changed since the previous brief (or the last
`CEO_BRIEF_DEFAULT_LOOKBACK_HOURS` if there is no previous one). It keeps
three kinds of content strictly separate and labeled: confirmed facts (real
rows created or changed inside the covered window, read from each system's
own durable event/history table -- never inferred from current state,
which cannot tell you what changed), estimates (explicitly marked as
non-authoritative reads of current state), and recommendations (explicitly
marked as suggestions, never auto-applied).
"""

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .audit import AuditLog
from .revenue_hunter import DashboardService, TERMINAL_STAGES, _STAGE_INDEX
from .store import StateStore, utcnow

CEO_BRIEF_DEFAULT_LOOKBACK_HOURS = 24


def _days_until(iso_date: Optional[str], now: datetime) -> Optional[int]:
    if not iso_date:
        return None
    try:
        due = datetime.fromisoformat(iso_date)
    except (TypeError, ValueError):
        return None
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    return (due - now).days


def command_center_snapshot(store: StateStore, upcoming_within_days: int = 14) -> Dict[str, Any]:
    """Real, sourced aggregation of company state (Section 2). Read-only:
    never mutates anything, never invents a number no row supports."""
    now = datetime.now(timezone.utc)

    opportunities = store.list("rh_opportunities")
    active_pipeline = [o for o in opportunities if o["stage"] not in TERMINAL_STAGES]
    negotiating = [o for o in opportunities if o["stage"] == "Negotiating"]
    won = [o for o in opportunities if o["stage"] == "Won"]
    key_opportunities = sorted(active_pipeline, key=lambda o: _STAGE_INDEX.get(o["stage"], -1), reverse=True)[:5]

    invoices = store.list("rh_invoices")
    cash_in_to_date = sum((inv["amount_received"] or 0.0) for inv in invoices)
    outstanding_invoices = [inv for inv in invoices if inv["status"] not in ("PAID", "CANCELLED")]
    outstanding_total = sum((inv["amount"] or 0.0) - (inv["amount_received"] or 0.0) for inv in outstanding_invoices)
    overdue_invoices = [inv for inv in invoices if inv["status"] == "OVERDUE"]
    overdue_total = sum((inv["amount"] or 0.0) - (inv["amount_received"] or 0.0) for inv in overdue_invoices)
    due_soon_invoices = []
    for inv in outstanding_invoices:
        if inv["status"] == "OVERDUE":
            continue
        days = _days_until(inv.get("due_date"), now)
        if days is not None and 0 <= days <= upcoming_within_days:
            due_soon_invoices.append(inv)

    clients = store.list("clients")

    active_jobs = store.list("rh_active_jobs")
    active_jobs_by_status: Dict[str, int] = {}
    for job in active_jobs:
        active_jobs_by_status[job["handoff_status"]] = active_jobs_by_status.get(job["handoff_status"], 0) + 1

    wf_tasks = store.list("wf_tasks")
    wf_by_status: Dict[str, int] = {}
    for task in wf_tasks:
        wf_by_status[task["status"]] = wf_by_status.get(task["status"], 0) + 1
    wf_attention = [t for t in wf_tasks if t["status"] in ("BLOCKED", "NEEDS_ARYAN", "FAILED")]

    media_content = store.list("media_content_items")
    media_by_state: Dict[str, int] = {}
    for item in media_content:
        media_by_state[item["content_state"]] = media_by_state.get(item["content_state"], 0) + 1
    media_publications = store.list("media_publications")
    media_failures = [p for p in media_publications if p["status"] == "FAILED"]

    needs_aryan_pending = store.list("needs_aryan_items", "status=?", ("PENDING",))
    needs_aryan_by_kind: Dict[str, int] = {}
    for item in needs_aryan_pending:
        needs_aryan_by_kind[item["kind"]] = needs_aryan_by_kind.get(item["kind"], 0) + 1

    followups_due_soon = []
    for fu in store.list("rh_followups", "status=?", ("DRAFT",)):
        days = _days_until(fu.get("due_at"), now)
        if days is not None and 0 <= days <= upcoming_within_days:
            followups_due_soon.append(fu)

    content_due_soon = []
    for item in media_content:
        if item["content_state"] in ("LEARN", "CANCELLED") or not item.get("planned_publish_date"):
            continue
        days = _days_until(item["planned_publish_date"], now)
        if days is not None and 0 <= days <= upcoming_within_days:
            content_due_soon.append(item)

    upcoming_obligations: List[Dict[str, Any]] = []
    for inv in due_soon_invoices:
        upcoming_obligations.append({"kind": "invoice_due", "ref_id": inv["id"], "client_id": inv["client_id"], "amount": inv["amount"], "due_date": inv["due_date"]})
    for fu in followups_due_soon:
        upcoming_obligations.append({"kind": "followup_due", "ref_id": fu["id"], "opportunity_id": fu["opportunity_id"], "due_date": fu.get("due_at")})
    for item in content_due_soon:
        upcoming_obligations.append({"kind": "content_due", "ref_id": item["id"], "title": item["title"], "due_date": item["planned_publish_date"]})

    risk_signals: List[Dict[str, Any]] = []
    if overdue_invoices:
        risk_signals.append({
            "category": "finance_risk", "severity": "high",
            "summary": f"{len(overdue_invoices)} invoice(s) overdue totaling {round(overdue_total, 2)}",
            "source": "rh_invoices.status=OVERDUE",
        })
    if wf_attention:
        risk_signals.append({
            "category": "delivery_risk", "severity": "medium",
            "summary": f"{len(wf_attention)} workforce task(s) blocked, failed, or awaiting a decision",
            "source": "wf_tasks.status in (BLOCKED, NEEDS_ARYAN, FAILED)",
        })
    if media_failures:
        risk_signals.append({
            "category": "delivery_risk", "severity": "low",
            "summary": f"{len(media_failures)} media publication(s) failed to publish",
            "source": "media_publications.status=FAILED",
        })
    if needs_aryan_pending:
        risk_signals.append({
            "category": "decision_backlog", "severity": "medium" if len(needs_aryan_pending) >= 5 else "low",
            "summary": f"{len(needs_aryan_pending)} item(s) pending an owner decision in Needs Aryan",
            "source": "needs_aryan_items.status=PENDING",
        })

    return {
        "generated_at": utcnow(),
        "revenue": {
            "won_revenue_lifetime": round(sum(o["final_price"] for o in won if o.get("final_price")), 2),
            "won_deals_count": len(won),
            "source": "rh_opportunities: stage=Won, final_price",
        },
        "cash": {
            "cash_in_to_date": round(cash_in_to_date, 2),
            "note": ("Inflow-only (evidence-backed payments recorded on invoices). Outflows are not yet "
                     "tracked here -- this is not a net cash-on-hand figure. The Finance Ledger pass adds that."),
            "source": "rh_invoices.amount_received (sum)",
        },
        "receivables": {
            "outstanding_total": round(outstanding_total, 2),
            "outstanding_count": len(outstanding_invoices),
            "overdue_total": round(overdue_total, 2),
            "overdue_count": len(overdue_invoices),
            "due_soon_count": len(due_soon_invoices),
            "source": "rh_invoices",
        },
        "pipeline": {
            "active_count": len(active_pipeline),
            "negotiating_count": len(negotiating),
            "negotiating": [{"id": o["id"], "title": o["title"], "client_name": o.get("client_name")} for o in negotiating],
            "key_opportunities": [{"id": o["id"], "title": o["title"], "stage": o["stage"], "client_name": o.get("client_name")} for o in key_opportunities],
            "source": "rh_opportunities.stage",
        },
        "clients": {
            "total": len(clients),
            "source": "clients",
        },
        "delivery": {
            "active_jobs_total": len(active_jobs),
            "active_jobs_by_handoff_status": active_jobs_by_status,
            "source": "rh_active_jobs.handoff_status",
        },
        "workforce": {
            "total_tasks": len(wf_tasks),
            "by_status": wf_by_status,
            "needs_attention_count": len(wf_attention),
            "source": "wf_tasks.status",
        },
        "media": {
            "content_items_total": len(media_content),
            "content_by_state": media_by_state,
            "publishing_failures_count": len(media_failures),
            "source": "media_content_items / media_publications",
        },
        "needs_aryan": {
            "pending_count": len(needs_aryan_pending),
            "pending_by_kind": needs_aryan_by_kind,
            "source": "needs_aryan_items.status=PENDING",
        },
        "upcoming_obligations": upcoming_obligations,
        "risk_signals": risk_signals,
    }


class CEOBriefStore:
    """Persistent, generated-on-demand CEO Brief (Section 3). Stored in
    `cc_ceo_briefs` so a brief, once generated, is a durable record --
    never regenerated silently or overwritten."""

    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit

    def latest(self) -> Optional[Dict[str, Any]]:
        rows = self.store.list("cc_ceo_briefs")
        if not rows:
            return None
        return sorted(rows, key=lambda r: r["period_end"])[-1]

    def list(self, limit: int = 30) -> List[Dict[str, Any]]:
        rows = sorted(self.store.list("cc_ceo_briefs"), key=lambda r: r["period_end"], reverse=True)
        return rows[:limit]

    def get(self, brief_id: str) -> Optional[Dict[str, Any]]:
        return self.store.get("cc_ceo_briefs", brief_id)

    def generate(self, actor: str = "system", period_start: Optional[str] = None) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        period_end = now.isoformat()
        if period_start is None:
            previous = self.latest()
            if previous:
                period_start = previous["period_end"]
            else:
                period_start = (now - timedelta(hours=CEO_BRIEF_DEFAULT_LOOKBACK_HOURS)).isoformat()

        confirmed_facts = self._confirmed_facts(period_start, period_end)
        snapshot = command_center_snapshot(self.store)
        estimates = self._estimates(snapshot)
        recommendations = self._recommendations(snapshot)
        top_priorities = self._top_priorities(snapshot)

        brief_id = self.store.create("cc_ceo_briefs", {
            "period_start": period_start, "period_end": period_end,
            "confirmed_facts_json": json.dumps(confirmed_facts),
            "estimates_json": json.dumps(estimates),
            "recommendations_json": json.dumps(recommendations),
            "top_priorities_json": json.dumps(top_priorities),
            "risks_json": json.dumps(snapshot["risk_signals"]),
            "actor": actor, "created_at": now.isoformat(),
        })
        self.audit.append("CEO_BRIEF_GENERATED", {"brief_id": brief_id, "period_start": period_start, "period_end": period_end, "actor": actor})
        return self.store.get("cc_ceo_briefs", brief_id)

    def _confirmed_facts(self, period_start: str, period_end: str) -> Dict[str, Any]:
        stage_events = self.store.list("rh_stage_history")
        won_since = [e for e in stage_events if e["to_stage"] == "Won" and period_start <= e["created_at"] < period_end]
        lost_since = [e for e in stage_events if e["to_stage"] == "Lost" and period_start <= e["created_at"] < period_end]

        opportunities = self.store.list("rh_opportunities")
        new_opportunities = [o for o in opportunities if period_start <= o["created_at"] < period_end]

        conversation_messages = self.store.list("rh_conversation_messages")
        replies_since = [m for m in conversation_messages if m["direction"] == "INBOUND" and period_start <= m["created_at"] < period_end]

        invoices = self.store.list("rh_invoices")
        payments_since: List[Dict[str, Any]] = []
        for inv in invoices:
            history = json.loads(inv["evidence_json"]) if inv.get("evidence_json") else []
            for entry in history:
                recorded_at = entry.get("recorded_at") or ""
                if period_start <= recorded_at < period_end:
                    payments_since.append({"invoice_id": inv["id"], "amount": entry.get("amount")})

        wf_task_events = self.store.list("wf_task_events")
        wf_failures_since = [e for e in wf_task_events if e["to_status"] == "FAILED" and period_start <= e["created_at"] < period_end]

        needs_aryan_items = self.store.list("needs_aryan_items")
        needs_aryan_created_since = [i for i in needs_aryan_items if period_start <= i["created_at"] < period_end]
        needs_aryan_decided_since = [i for i in needs_aryan_items if i.get("decided_at") and period_start <= i["decided_at"] < period_end]

        media_content_events = self.store.list("media_content_events")
        media_state_changes_since = [e for e in media_content_events if period_start <= e["created_at"] < period_end]

        return {
            "period_start": period_start, "period_end": period_end,
            "deals_won": len(won_since), "deals_lost": len(lost_since),
            "new_opportunities": len(new_opportunities),
            "client_replies": len(replies_since),
            "payments_received_count": len(payments_since),
            "payments_received_total": round(sum((p["amount"] or 0.0) for p in payments_since), 2),
            "workforce_failures": len(wf_failures_since),
            "needs_aryan_created": len(needs_aryan_created_since),
            "needs_aryan_decided": len(needs_aryan_decided_since),
            "media_state_changes": len(media_state_changes_since),
        }

    def _estimates(self, snapshot: Dict[str, Any]) -> List[str]:
        estimates = []
        if snapshot["pipeline"]["active_count"]:
            estimates.append(
                f"Estimate: {snapshot['pipeline']['negotiating_count']} of {snapshot['pipeline']['active_count']} "
                "active opportunities are currently in negotiation -- an early signal only, not a win probability."
            )
        if snapshot["receivables"]["overdue_count"]:
            estimates.append(
                f"Estimate: {snapshot['receivables']['overdue_count']} overdue invoice(s) totaling "
                f"{snapshot['receivables']['overdue_total']} are collection risk, not yet written off."
            )
        return estimates

    def _recommendations(self, snapshot: Dict[str, Any]) -> List[str]:
        recs = []
        if snapshot["needs_aryan"]["pending_count"]:
            recs.append(f"Recommendation: clear the {snapshot['needs_aryan']['pending_count']} pending Needs Aryan item(s) -- they are blocking downstream work.")
        if snapshot["receivables"]["overdue_count"]:
            recs.append("Recommendation: follow up on overdue invoices before they age further.")
        if snapshot["workforce"]["needs_attention_count"]:
            recs.append(f"Recommendation: review the {snapshot['workforce']['needs_attention_count']} workforce task(s) needing attention.")
        if not recs:
            recs.append("Recommendation: no urgent action identified from current data -- keep pipeline and delivery moving.")
        return recs

    def _top_priorities(self, snapshot: Dict[str, Any]) -> List[str]:
        priorities = []
        if snapshot["needs_aryan"]["pending_count"]:
            priorities.append(f"Clear {snapshot['needs_aryan']['pending_count']} Needs Aryan item(s)")
        if snapshot["receivables"]["overdue_count"]:
            priorities.append(f"Collect {snapshot['receivables']['overdue_count']} overdue invoice(s)")
        if snapshot["pipeline"]["negotiating_count"]:
            priorities.append(f"Close {snapshot['pipeline']['negotiating_count']} deal(s) in negotiation")
        return priorities[:3]


def kpi_snapshot(store: StateStore, period_days: int = 30) -> Dict[str, Any]:
    """KPI framework (Section 5): Sales / Delivery / Workforce / Media /
    Finance, each metric computed live from existing tables for a trailing
    window -- never hand-entered, never carried over stale. Where this
    codebase genuinely has no data model backing a listed metric yet
    (delivery revision count, client acceptance, media leads generated,
    finance gross profit / operating spend -- the latter two need the
    Finance Ledger pass), the metric is returned as `value: None` with a
    `source` explaining exactly why, rather than a fabricated number."""
    now = datetime.now(timezone.utc)
    window_start = (now - timedelta(days=period_days)).isoformat()

    opportunities = store.list("rh_opportunities")
    opp_by_id = {o["id"]: o for o in opportunities}
    new_in_window = [o for o in opportunities if o["created_at"] >= window_start]

    stage_events = store.list("rh_stage_history")
    events_in_window = [e for e in stage_events if e["created_at"] >= window_start]
    pursued = [e for e in events_in_window if e.get("from_stage") == "New" and e["to_stage"] != "New"]
    sent_in_window = [e for e in events_in_window if e["to_stage"] == "Applied/Sent"]
    replied_in_window = [e for e in events_in_window if e["to_stage"] == "Replied"]
    meetings_in_window = [e for e in events_in_window if e["to_stage"] == "Meeting"]
    won_in_window = [e for e in events_in_window if e["to_stage"] == "Won"]
    lost_in_window = [e for e in events_in_window if e["to_stage"] == "Lost"]

    win_rate = None
    closed = len(won_in_window) + len(lost_in_window)
    if closed:
        win_rate = round(len(won_in_window) / closed, 4)

    won_values = [o["final_price"] for o in opportunities if o["stage"] == "Won" and o.get("final_price")]
    average_deal_value = round(sum(won_values) / len(won_values), 2) if won_values else None

    proposals_in_window = [p for p in store.list("rh_proposals") if p["created_at"] >= window_start]

    sales = {
        "opportunities_found": {"value": len(new_in_window), "source": "rh_opportunities.created_at in window"},
        "pursue_rate": {
            "value": round(len(pursued) / len(new_in_window), 4) if new_in_window else None,
            "source": "rh_stage_history: New -> any other stage, over opportunities found in window",
        },
        "proposals_drafted": {"value": len(proposals_in_window), "source": "rh_proposals.created_at in window"},
        "proposals_sent": {"value": len(sent_in_window), "source": "rh_stage_history.to_stage=Applied/Sent in window"},
        "reply_rate": {
            "value": round(len(replied_in_window) / len(sent_in_window), 4) if sent_in_window else None,
            "source": "rh_stage_history: Replied over Applied/Sent in window",
        },
        "meetings": {"value": len(meetings_in_window), "source": "rh_stage_history.to_stage=Meeting in window"},
        "win_rate": {"value": win_rate, "source": "rh_stage_history: Won / (Won + Lost) in window"},
        "pipeline_value": {"value": DashboardService(store).today()["pipeline_value"], "source": "DashboardService.today() -- suggested_price on each active opportunity's latest qualification"},
        "average_deal_value": {"value": average_deal_value, "source": "rh_opportunities: mean final_price where stage=Won (lifetime)"},
    }

    active_jobs = store.list("rh_active_jobs")
    on_time, off_time = [], []
    for rec in store.list("rh_completion_records"):
        job = store.get("rh_active_jobs", rec["active_job_id"]) if rec.get("active_job_id") else None
        if not job:
            continue
        try:
            payload = json.loads(job["job_payload_json"])
        except (TypeError, ValueError):
            continue
        deadline = payload.get("deadline")
        if not deadline:
            continue
        (on_time if rec["created_at"][:10] <= deadline else off_time).append(rec)
    on_time_rate = round(len(on_time) / (len(on_time) + len(off_time)), 4) if (on_time or off_time) else None

    delivery = {
        "active_jobs": {"value": len(active_jobs), "source": "rh_active_jobs"},
        "on_time_rate": {"value": on_time_rate, "source": "rh_completion_records.created_at vs the job's own stored deadline (only jobs with both)"},
        "blocked_jobs": {"value": None, "source": "not yet tracked -- rh_active_jobs has no blocked state of its own yet"},
        "revision_count": {"value": None, "source": "not yet tracked -- no revision data model exists yet"},
        "client_acceptance": {"value": None, "source": "not yet tracked -- no acceptance field exists yet"},
    }

    wf_events = store.list("wf_task_events")
    events_wf_in_window = [e for e in wf_events if e["created_at"] >= window_start]
    completed_events = [e for e in events_wf_in_window if e["to_status"] == "COMPLETED"]
    failed_events = [e for e in events_wf_in_window if e["to_status"] == "FAILED"]
    executing_events = [e for e in events_wf_in_window if e["to_status"] == "EXECUTING"]
    intervention_events = [e for e in events_wf_in_window if e["to_status"] == "NEEDS_ARYAN"]
    durations = []
    for ce in completed_events:
        starts = [e for e in wf_events if e["task_id"] == ce["task_id"] and e["to_status"] == "EXECUTING"]
        if not starts:
            continue
        try:
            start_t = datetime.fromisoformat(starts[0]["created_at"])
            end_t = datetime.fromisoformat(ce["created_at"])
        except (TypeError, ValueError):
            continue
        durations.append((end_t - start_t).total_seconds())
    average_task_seconds = round(sum(durations) / len(durations), 1) if durations else None

    workforce = {
        "tasks_completed": {"value": len(completed_events), "source": "wf_task_events.to_status=COMPLETED in window"},
        "tasks_failed": {"value": len(failed_events), "source": "wf_task_events.to_status=FAILED in window"},
        "intervention_rate": {
            "value": round(len(intervention_events) / len(executing_events), 4) if executing_events else None,
            "source": "wf_task_events: NEEDS_ARYAN / EXECUTING in window",
        },
        "average_task_time_seconds": {"value": average_task_seconds, "source": "wf_task_events: EXECUTING -> COMPLETED elapsed real time, per task"},
    }

    media_content = store.list("media_content_items")
    content_in_window = [c for c in media_content if c["created_at"] >= window_start]
    published_in_window = [p for p in store.list("media_publications") if p["status"] == "PUBLISHED" and (p.get("published_at") or "") >= window_start]
    media = {
        "content_produced": {"value": len(content_in_window), "source": "media_content_items.created_at in window"},
        "content_published": {"value": len(published_in_window), "source": "media_publications.status=PUBLISHED, published_at in window"},
        "reach_engagement": {"value": None, "source": "not summarized here -- see media_analytics for raw, human-sourced metric entries per publication"},
        "leads_generated": {"value": None, "source": "not yet tracked -- no media-to-lead attribution model exists yet"},
    }

    snapshot = command_center_snapshot(store)
    monthly_revenue_won = sum((opp_by_id.get(e["opportunity_id"], {}).get("final_price") or 0.0) for e in won_in_window)
    finance = {
        "cash_in_to_date": {"value": snapshot["cash"]["cash_in_to_date"], "source": snapshot["cash"]["source"]},
        "receivables_outstanding": {"value": snapshot["receivables"]["outstanding_total"], "source": snapshot["receivables"]["source"]},
        "receivables_overdue": {"value": snapshot["receivables"]["overdue_total"], "source": snapshot["receivables"]["source"]},
        "revenue_won_in_window": {"value": round(monthly_revenue_won, 2), "source": "rh_stage_history.to_stage=Won in window, matched to rh_opportunities.final_price"},
        "gross_profit_estimate": {"value": None, "source": "not yet computable -- requires the Finance Ledger pass (outflow tracking)"},
        "operating_spend": {"value": None, "source": "not yet tracked -- requires the Finance Ledger pass"},
    }

    return {
        "generated_at": utcnow(),
        "period_days": period_days,
        "window_start": window_start,
        "window_end": now.isoformat(),
        "sales": sales,
        "delivery": delivery,
        "workforce": workforce,
        "media": media,
        "finance": finance,
    }
