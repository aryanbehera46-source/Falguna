"""TTT Department Performance + AI Workforce Performance v1 (Sections 13-14
of the Command Center / CEO Intelligence + Finance / Capital Engine
phase).

Both functions below are read-only aggregations over already-tested
systems' own tables -- output, failures, blocked work, and cost per
department; success/failure/retry/intervention rate and approximate cost
per Digital Workforce worker. Neither computes a composite "score": the
spec explicitly calls out avoiding meaningless scores, so this stays a
plain, sourced breakdown a person can read and judge for themselves.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from .store import StateStore, utcnow

_TERMINAL_RUN_FAILURE_STATUSES = {"FAILED", "QUARANTINED"}


def _ledger_spend(store: StateStore, business_unit: str, window_start: str) -> float:
    entries = store.list("cc_ledger_entries", "business_unit=? AND entry_type=? AND status=?", (business_unit, "OUTFLOW", "RECORDED"))
    return round(sum(e["amount"] for e in entries if e["created_at"] >= window_start), 2)


def department_performance(store: StateStore, period_days: int = 30) -> Dict[str, Any]:
    """Section 13. Every department's numbers are read straight from that
    department's own system -- Sales/Delivery from Revenue Hunter, Digital
    Workforce from wf_tasks, Media/Growth from media_*, Falguna Engineering
    from Falguna's own runs/model_calls/cost_events. Cost is whatever has
    actually been logged against that department in the Finance Ledger
    (business_unit-tagged) -- 0 when nothing has been logged, not a guess."""
    now = datetime.now(timezone.utc)
    window_start = (now - timedelta(days=period_days)).isoformat()

    from .command_center import kpi_snapshot  # local import: avoids a cycle at module load
    kpis = kpi_snapshot(store, period_days=period_days)

    opportunities = store.list("rh_opportunities")
    won_in_window = [o for o in opportunities if o["stage"] == "Won" and o["created_at"] >= window_start]
    lost_in_window = [o for o in opportunities if o["stage"] == "Lost" and o["created_at"] >= window_start]

    sales = {
        "output": {"value": kpis["sales"]["opportunities_found"]["value"], "source": "rh_opportunities.created_at in window"},
        "failures": {"value": len(lost_in_window), "source": "rh_opportunities.stage=Lost, created_at in window"},
        "blocked_work": {"value": None, "source": "not applicable -- Revenue Hunter has no blocked-work concept of its own"},
        "cost": {"value": _ledger_spend(store, "Sales", window_start), "source": "cc_ledger_entries (business_unit=Sales, OUTFLOW) in window"},
        "business_impact": {"value": kpis["finance"]["revenue_won_in_window"]["value"], "source": "rh_stage_history.to_stage=Won in window, matched to final_price"},
    }

    delivery = {
        "output": {"value": kpis["delivery"]["active_jobs"]["value"], "source": "rh_active_jobs"},
        "failures": {"value": None, "source": "not yet tracked -- no delivery failure/rework model exists yet"},
        "blocked_work": {"value": kpis["delivery"]["blocked_jobs"]["value"], "source": kpis["delivery"]["blocked_jobs"]["source"]},
        "cost": {"value": _ledger_spend(store, "Delivery", window_start), "source": "cc_ledger_entries (business_unit=Delivery, OUTFLOW) in window"},
        "business_impact": {"value": kpis["delivery"]["on_time_rate"]["value"], "source": kpis["delivery"]["on_time_rate"]["source"]},
    }

    workforce = {
        "output": {"value": kpis["workforce"]["tasks_completed"]["value"], "source": "wf_task_events.to_status=COMPLETED in window"},
        "failures": {"value": kpis["workforce"]["tasks_failed"]["value"], "source": "wf_task_events.to_status=FAILED in window"},
        "blocked_work": {"value": len([t for t in store.list("wf_tasks") if t["status"] in ("BLOCKED", "NEEDS_ARYAN")]), "source": "wf_tasks.status in (BLOCKED, NEEDS_ARYAN)"},
        "cost": {"value": _ledger_spend(store, "Digital Workforce", window_start), "source": "cc_ledger_entries (business_unit=Digital Workforce, OUTFLOW) in window"},
        "business_impact": {"value": None, "source": "not yet tracked -- no dollar-value-per-task model exists yet"},
    }

    media = {
        "output": {"value": kpis["media"]["content_produced"]["value"], "source": "media_content_items.created_at in window"},
        "failures": {
            "value": len([
                p for p in store.list("media_publications", "status=?", ("FAILED",))
                if p["created_at"] >= window_start
            ]),
            "source": "media_publications.status=FAILED, created_at in window",
        },
        "blocked_work": {"value": None, "source": "not applicable -- content pipeline states are not a blocked-work concept"},
        "cost": {"value": _ledger_spend(store, "Media/Growth", window_start), "source": "cc_ledger_entries (business_unit=Media/Growth, OUTFLOW) in window"},
        "business_impact": {"value": kpis["media"]["leads_generated"]["value"], "source": kpis["media"]["leads_generated"]["source"]},
    }

    runs_in_window = [r for r in store.list("runs") if r["created_at"] >= window_start]
    falguna_failed = [r for r in runs_in_window if r["status"] in _TERMINAL_RUN_FAILURE_STATUSES]
    falguna_done = [r for r in runs_in_window if r["status"] == "DONE_CANDIDATE"]
    model_call_cost = sum(c["cost_usd"] for c in store.list("model_calls") if c["created_at"] >= window_start)
    other_cost_events = sum(c["amount_usd"] for c in store.list("cost_events") if c["created_at"] >= window_start)

    falguna_engineering = {
        "output": {"value": len(falguna_done), "source": "runs.status=DONE_CANDIDATE, created_at in window"},
        "failures": {"value": len(falguna_failed), "source": "runs.status in (FAILED, QUARANTINED), created_at in window"},
        "blocked_work": {
            "value": len([
                r for r in runs_in_window
                if r["status"] != "DONE_CANDIDATE" and r["status"] not in _TERMINAL_RUN_FAILURE_STATUSES
            ]),
            "source": "runs not yet terminal in window",
        },
        "cost": {"value": round(model_call_cost + other_cost_events, 2), "source": "model_calls.cost_usd + cost_events.amount_usd, created_at in window"},
        "business_impact": {"value": None, "source": "not yet tracked -- Falguna missions are not yet linked to a dollar-value outcome"},
    }

    return {
        "generated_at": utcnow(),
        "period_days": period_days,
        "sales": sales,
        "delivery": delivery,
        "digital_workforce": workforce,
        "media_growth": media,
        "falguna_engineering": falguna_engineering,
    }


def ai_workforce_performance(store: StateStore, period_days: int = 30) -> Dict[str, Any]:
    """Section 14. Grouped by `wf_tasks.assigned_worker` -- the real
    identity of the worker that handled each task. Every rate is a real
    ratio of real counts (never a fabricated precision score), and is
    `None`, not 0, when there is no data to compute it from yet."""
    now = datetime.now(timezone.utc)
    window_start = (now - timedelta(days=period_days)).isoformat()

    tasks = [t for t in store.list("wf_tasks") if t["created_at"] >= window_start]
    by_worker: Dict[str, Dict[str, Any]] = {}
    for task in tasks:
        worker = task.get("assigned_worker") or "unassigned"
        entry = by_worker.setdefault(worker, {
            "tasks_total": 0, "completed": 0, "failed": 0, "retried": 0, "needs_aryan": 0,
            "durations": [], "cost_total": 0.0, "has_cost_data": False,
        })
        entry["tasks_total"] += 1
        if task["status"] == "COMPLETED":
            entry["completed"] += 1
        if task["status"] == "FAILED":
            entry["failed"] += 1
        if (task.get("retries") or 0) > 0:
            entry["retried"] += 1
        if task["status"] == "NEEDS_ARYAN":
            entry["needs_aryan"] += 1
        if task.get("cost"):
            entry["cost_total"] += task["cost"]
            entry["has_cost_data"] = True

        events = store.list("wf_task_events", "task_id=?", (task["id"],))
        starts = [e for e in events if e["to_status"] == "EXECUTING"]
        ends = [e for e in events if e["to_status"] in ("COMPLETED", "FAILED")]
        if starts and ends:
            try:
                start_t = datetime.fromisoformat(starts[0]["created_at"])
                end_t = datetime.fromisoformat(ends[-1]["created_at"])
                entry["durations"].append((end_t - start_t).total_seconds())
            except (TypeError, ValueError):
                pass

    result: Dict[str, Any] = {}
    for worker, entry in by_worker.items():
        total = entry["tasks_total"]
        result[worker] = {
            "tasks_total": total,
            "success_rate": round(entry["completed"] / total, 4) if total else None,
            "failure_rate": round(entry["failed"] / total, 4) if total else None,
            "retry_rate": round(entry["retried"] / total, 4) if total else None,
            "intervention_rate": round(entry["needs_aryan"] / total, 4) if total else None,
            "average_duration_seconds": round(sum(entry["durations"]) / len(entry["durations"]), 1) if entry["durations"] else None,
            "approximate_cost": round(entry["cost_total"], 2) if entry["has_cost_data"] else None,
        }

    return {
        "generated_at": utcnow(),
        "period_days": period_days,
        "by_worker": result,
        "source": "wf_tasks (assigned_worker, status, retries, cost) + wf_task_events (EXECUTING -> COMPLETED/FAILED timing), created_at in window",
    }
