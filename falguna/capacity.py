"""Phase 5 Continuation, Section 16 -- Delivery Capacity & Scheduling V1.

Gives TTT HQ a real, evidence-based picture of current delivery load and
a practical, categorical recommendation -- the instruction is explicit:
"no invented precise utilization percentages, use practical categorical
recommendations." This never computes or displays a fabricated percentage
like "73% utilized"; it counts real, already-persisted rows from three
proven systems and classifies the result against small, named,
documented thresholds instead:

  * `cs_projects` (falguna/commercial.py) -- how many delivery projects
    are open, broken down by status.
  * `wf_tasks` (falguna/workforce.py) -- the real Workforce backlog,
    split into "in flight" (moving through the pipeline on its own) and
    "stuck" (BLOCKED/NEEDS_ARYAN/FAILED -- needs something to unblock it).
  * `needs_aryan_items` (falguna/ttt_hq.py) -- the human-approval queue:
    how many items are PENDING, and how long the oldest one has waited.

This module creates no new table and owns no state of its own -- it is a
read-only rollup, exactly like `falguna/commercial.py`'s
`economics_for_project`.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List

from .store import StateStore

# -- Documented thresholds --------------------------------------------
#
# Deliberately small numbers for an early-stage agency's real current
# scale, not an enterprise-scale guess. Each one names the specific
# signal it reacts to; changing the number here is a one-line, reviewable
# policy change, not a buried magic constant.
CAPACITY_THRESHOLDS = {
    # More than this many wf_tasks genuinely in flight at once (not
    # stuck) means real, active delivery load worth naming.
    "in_flight_constrained": 5,
    # Past this many in-flight tasks, the backlog itself -- not any
    # single blocker -- is the bottleneck.
    "in_flight_bottleneck": 15,
    # More than this many wf_tasks sitting BLOCKED/NEEDS_ARYAN/FAILED at
    # once suggests a capability or specialist gap, not isolated noise.
    "stuck_specialist_constrained": 2,
    # More than this many PENDING needs_aryan_items at once means the
    # human-approval queue itself is the actual constraint.
    "needs_aryan_count_bottleneck": 5,
    # Any single PENDING needs_aryan_item older than this many hours
    # means the approval queue is stale, regardless of its size.
    "needs_aryan_age_hours_bottleneck": 24.0,
}

CAPACITY_RECOMMENDATIONS = {
    "AVAILABLE", "CONSTRAINED", "SPECIALIST_CONSTRAINED",
    "EXECUTION_BOTTLENECK", "APPROVAL_BOTTLENECK",
}

_IN_FLIGHT_TASK_STATUSES = {"CREATED", "PLANNING", "READY", "EXECUTING", "VERIFYING"}
_STUCK_TASK_STATUSES = {"BLOCKED", "NEEDS_ARYAN", "FAILED"}


def _count_by(rows: List[Dict[str, Any]], field: str) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for row in rows:
        key = row.get(field)
        counts[key] = counts.get(key, 0) + 1
    return counts


def _recommend(stuck_count: int, in_flight_count: int, needs_aryan_pending: int, needs_aryan_oldest_hours) -> Dict[str, Any]:
    """Priority order: an approval-queue problem is checked first because
    it is almost always the real constraint -- a long or stale human-
    decision queue blocks everything behind it regardless of how much
    other work looks "in flight." Only once that is clear do we look at
    whether tasks are stuck for a capability reason, then at raw backlog
    volume. Exactly one category is ever returned, never a fabricated
    blended score.
    """
    t = CAPACITY_THRESHOLDS
    if needs_aryan_pending > t["needs_aryan_count_bottleneck"] or (
        needs_aryan_oldest_hours is not None and needs_aryan_oldest_hours > t["needs_aryan_age_hours_bottleneck"]
    ):
        return {
            "recommendation": "APPROVAL_BOTTLENECK",
            "reason": (
                f"{needs_aryan_pending} item(s) pending human approval"
                + (f", oldest waiting {needs_aryan_oldest_hours:.1f}h" if needs_aryan_oldest_hours is not None else "")
                + f" (thresholds: >{t['needs_aryan_count_bottleneck']} pending or "
                f">{t['needs_aryan_age_hours_bottleneck']}h oldest) -- clearing this queue is the real constraint right now."
            ),
        }
    if stuck_count > t["stuck_specialist_constrained"]:
        return {
            "recommendation": "SPECIALIST_CONSTRAINED",
            "reason": (
                f"{stuck_count} workforce task(s) are BLOCKED/NEEDS_ARYAN/FAILED "
                f"(threshold: >{t['stuck_specialist_constrained']}) -- check the Capability Registry for the "
                f"worker(s) involved before committing to more work of the same kind."
            ),
        }
    if in_flight_count > t["in_flight_bottleneck"]:
        return {
            "recommendation": "EXECUTION_BOTTLENECK",
            "reason": (
                f"{in_flight_count} workforce task(s) are actively in flight "
                f"(threshold: >{t['in_flight_bottleneck']}) -- the backlog itself is the constraint, not a single blocker."
            ),
        }
    if in_flight_count > t["in_flight_constrained"]:
        return {
            "recommendation": "CONSTRAINED",
            "reason": (
                f"{in_flight_count} workforce task(s) are actively in flight "
                f"(threshold: >{t['in_flight_constrained']}) -- real load, but not yet a bottleneck."
            ),
        }
    return {
        "recommendation": "AVAILABLE",
        "reason": "No backlog, blocker, or approval-queue signal is past its documented threshold right now.",
    }


def capacity_snapshot(store: StateStore) -> Dict[str, Any]:
    """The Section 16 read-only rollup. Every count comes from a `list()`
    call against an existing, already-tested table -- nothing here is
    estimated, interpolated, or carried over from a previous snapshot.
    """
    now = datetime.now(timezone.utc)

    projects = store.list("cs_projects", "1=1")
    projects_by_status = _count_by(projects, "status")
    active_projects = sum(count for status, count in projects_by_status.items() if status != "CLOSED")

    tasks = store.list("wf_tasks", "1=1")
    tasks_by_status = _count_by(tasks, "status")
    in_flight_count = sum(count for status, count in tasks_by_status.items() if status in _IN_FLIGHT_TASK_STATUSES)
    stuck_count = sum(count for status, count in tasks_by_status.items() if status in _STUCK_TASK_STATUSES)

    pending_items = store.list("needs_aryan_items", "status=?", ("PENDING",))
    needs_aryan_pending = len(pending_items)
    needs_aryan_oldest_hours = None
    if pending_items:
        oldest_created_at = min(item["created_at"] for item in pending_items)
        oldest_dt = datetime.fromisoformat(oldest_created_at)
        if oldest_dt.tzinfo is None:
            oldest_dt = oldest_dt.replace(tzinfo=timezone.utc)
        needs_aryan_oldest_hours = round((now - oldest_dt).total_seconds() / 3600.0, 1)

    verdict = _recommend(stuck_count, in_flight_count, needs_aryan_pending, needs_aryan_oldest_hours)

    return {
        "generated_at": now.isoformat(),
        "projects_by_status": projects_by_status,
        "active_projects": active_projects,
        "wf_tasks_by_status": tasks_by_status,
        "wf_tasks_in_flight": in_flight_count,
        "wf_tasks_stuck": stuck_count,
        "needs_aryan_pending": needs_aryan_pending,
        "needs_aryan_oldest_pending_hours": needs_aryan_oldest_hours,
        "recommendation": verdict["recommendation"],
        "recommendation_reason": verdict["reason"],
        "thresholds": dict(CAPACITY_THRESHOLDS),
    }
