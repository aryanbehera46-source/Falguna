"""Phase 5 Continuation, Section 22 -- Learning from Outcomes V1.

Captures one durable, evidence-only outcome record the moment a delivery
project (falguna/commercial.py's `cs_projects`) reaches CLOSED. Every
field is derived strictly from that project's own persisted history and
the already-proven `economics_for_project` rollup -- nothing here is
estimated, interpolated, or guessed:

  * `qa_cycle_count` counts real `to_status == "QA"` transitions in the
    project's own `cs_project_events` history. `QA -> IN_DELIVERY` is a
    legitimate state-machine transition (`_PROJECT_TRANSITIONS` in
    commercial.py: "QA can bounce work back") -- a project re-entering QA
    more than once is direct, first-party evidence of rework, not an
    inference.
  * `actual_delivery_days` is the real elapsed time from the project's
    `created_at` to its own CLOSED transition event.
  * `estimated_delivery_days` comes only from the linked service's own
    `standard_delivery_days` (Section 4, International Services V1) --
    `None` when that was never assessed, never a guessed baseline.
  * `dispute_count` and every economics figure are read straight from
    `cs_disputes` and `economics_for_project`, reusing that rollup
    rather than recomputing it a second, possibly-diverging way.

Explicit, documented boundary (do not remove without separate
authorization): this module does NOT feed into `QualificationEngine`
scoring (Section 7), `FoundationStore` maturity, or `recommend_route`.
Wiring a feedback loop into those is a real behavior change to an
already-tested system, and the standing instruction is explicit --
"Do not implement uncontrolled self-modifying production code. Learning
should update structured knowledge/recommendations, not silently rewrite
core systems." This V1 is the structured-knowledge half only: a durable,
queryable record of what actually happened, for a human (or a future,
separately-authorized pass) to read and act on.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .audit import AuditLog
from .commercial import economics_for_project
from .store import StateStore, utcnow

OUTCOME_ACCEPTANCE_STATES = {"ACCEPTED_CLEAN", "ACCEPTED_WITH_REWORK", "DISPUTED"}


class OutcomeError(ValueError):
    pass


def _parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class OutcomeStore:
    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit

    def record_for_project(self, project_id: str, actor: str = "system") -> str:
        """Idempotent: a project that already has an outcome record
        returns that record's id unchanged rather than creating a
        duplicate (a project cannot leave CLOSED, so there is never a
        legitimate reason to recompute one)."""
        project = self.store.get("cs_projects", project_id)
        if not project:
            raise OutcomeError("project not found")
        if project["status"] != "CLOSED":
            raise OutcomeError("an outcome record can only be captured once a project reaches CLOSED")
        existing = self.store.list("cs_outcome_records", "project_id=?", (project_id,))
        if existing:
            return existing[-1]["id"]

        history = self.store.list("cs_project_events", "project_id=?", (project_id,))
        qa_cycle_count = sum(1 for event in history if event["to_status"] == "QA")
        closed_event = next((event for event in reversed(history) if event["to_status"] == "CLOSED"), None)
        closed_at = closed_event["created_at"] if closed_event else project["updated_at"]
        actual_delivery_days = round((_parse_dt(closed_at) - _parse_dt(project["created_at"])).total_seconds() / 86400.0, 1)

        estimated_delivery_days = None
        if project.get("service_id"):
            service = self.store.get("cs_services", project["service_id"])
            if service:
                estimated_delivery_days = service.get("standard_delivery_days")

        econ = economics_for_project(self.store, project_id)

        dispute_count = 0
        if project.get("opportunity_id"):
            invoices = self.store.list("rh_invoices", "opportunity_id=?", (project["opportunity_id"],))
            for invoice in invoices:
                dispute_count += len(self.store.list("cs_disputes", "invoice_id=?", (invoice["id"],)))

        if dispute_count > 0:
            acceptance = "DISPUTED"
        elif qa_cycle_count > 1:
            acceptance = "ACCEPTED_WITH_REWORK"
        else:
            acceptance = "ACCEPTED_CLEAN"

        now = utcnow()
        outcome_id = self.store.create("cs_outcome_records", {
            "project_id": project_id, "service_id": project.get("service_id"),
            "foundation_id": project.get("foundation_id"), "delivery_route": project["delivery_route"],
            "estimated_delivery_days": estimated_delivery_days, "actual_delivery_days": actual_delivery_days,
            "qa_cycle_count": qa_cycle_count, "dispute_count": dispute_count, "acceptance": acceptance,
            "quoted_value": econ.get("quoted_value"), "net_collected": econ.get("net_collected"),
            "total_known_direct_cost": econ.get("total_known_direct_cost"),
            "gross_contribution": econ.get("gross_contribution"),
            "actor": actor, "created_at": now, "updated_at": now,
        })
        self.audit.append(
            "CS_OUTCOME_RECORDED",
            {"outcome_id": outcome_id, "project_id": project_id, "acceptance": acceptance, "actor": actor},
        )
        return outcome_id

    def get(self, outcome_id: str) -> Optional[Dict[str, Any]]:
        return self.store.get("cs_outcome_records", outcome_id)

    def get_by_project(self, project_id: str) -> Optional[Dict[str, Any]]:
        rows = self.store.list("cs_outcome_records", "project_id=?", (project_id,))
        return rows[-1] if rows else None

    def list(self, service_id: Optional[str] = None, acceptance: Optional[str] = None) -> List[Dict[str, Any]]:
        where, params = "1=1", []
        if service_id:
            where += " AND service_id=?"
            params.append(service_id)
        if acceptance:
            where += " AND acceptance=?"
            params.append(acceptance)
        return list(reversed(self.store.list("cs_outcome_records", where, params)))


def backfill_outcomes_for_closed_projects(store: StateStore, audit: AuditLog, actor: str = "system") -> List[str]:
    """Mirrors `commercial.backfill_projects_for_closed_opportunities`:
    walks every CLOSED `cs_projects` row and records the matching outcome
    for any that doesn't already have one (a project closed before this
    module existed, or before this call runs). Idempotent and never
    raises -- a single bad project is skipped and logged rather than
    blocking every other one."""
    created = []
    outcomes = OutcomeStore(store, audit)
    for project in store.list("cs_projects", "status=?", ("CLOSED",)):
        try:
            if outcomes.get_by_project(project["id"]):
                continue
            created.append(outcomes.record_for_project(project["id"], actor=actor))
        except Exception as exc:
            print(f"backfill_outcomes_for_closed_projects: skipped project {project.get('id')} ({exc})")
    return created
