"""TTT Group OS / Company Orchestrator v2.

The top-level operating intelligence that coordinates Twenty Two
Technologies Pvt. Ltd. as one system. The central loop this module
implements: Aryan objective -> company plan -> priorities -> ventures/
departments -> capital/resource allocation -> execution -> monitoring ->
risk detection -> replanning -> Needs Aryan when required -> CEO/Boardroom
visibility.

Hard constraints carried over from every prior phase, restated here because
this module sits above and composes all of them:

  * REUSE, NEVER DUPLICATE existing execution engines. This module never
    reimplements Revenue Hunter, Digital Workforce, Media/Growth, Falguna
    Engineering, Finance/Capital, Venture Studio, or Trading Lab -- it reads
    their tables (via `StateStore`) and, where it creates real work, creates
    it through their own existing stores (e.g. `WorkforceTaskStore`), never
    through a parallel table.
  * NO FABRICATED CERTAINTY. Priority scores are small, transparent,
    rule-based integer point totals with a plain-text rationale listing
    exactly which inputs contributed -- never a spurious float "confidence"
    or ML-flavored number nothing here can actually back.
  * NO AUTOMATIC HIGH-IMPACT ACTION (Section 23 -- the orchestrator autonomy
    boundary). Every function in this module that touches money, contracts,
    external communication, or a venture's lifecycle either (a) only
    *recommends* and persists the recommendation, or (b) routes to an
    existing engine's own existing, already-gated approval path (Needs
    Aryan). Nothing here calls a payment API, a contract-signing flow, or an
    external send. Nothing here calls `ReservePolicyStore.save` or
    `GraveyardStore.close_venture` directly -- those stay reachable only
    through their own existing routes/UI.
  * AVOID APPROVAL SPAM / AVOID ESCALATING ROUTINE LOW-RISK WORK. Every
    escalation path here (Decision Engine, Escalation Engine, Failure
    management) is a deterministic gate on impact/risk/cost/irreversibility/
    external-commitment -- routine, low-risk items are recorded but never
    pushed to Needs Aryan.
  * PERSISTENT, NEVER IN-MEMORY-ONLY. Every mutation goes through
    `StateStore`, exactly like every other module in this codebase.
"""

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .audit import AuditLog
from .store import StateStore, utcnow

# ---------------------------------------------------------------------------
# Shared vocabularies
# ---------------------------------------------------------------------------

OBJECTIVE_STATUSES = {"DRAFT", "ACTIVE", "AT_RISK", "BLOCKED", "COMPLETED", "CANCELLED"}
# Explicit, checked graph -- same discipline as wf_tasks.ALLOWED_TASK_TRANSITIONS.
# AT_RISK/BLOCKED <-> ACTIVE are two-way because those are health states an
# objective can recover from; DRAFT can only ever move forward into ACTIVE or
# be abandoned; COMPLETED/CANCELLED are terminal.
ALLOWED_OBJECTIVE_TRANSITIONS: Dict[str, set] = {
    "DRAFT": {"ACTIVE", "CANCELLED"},
    "ACTIVE": {"AT_RISK", "BLOCKED", "COMPLETED", "CANCELLED"},
    "AT_RISK": {"ACTIVE", "BLOCKED", "COMPLETED", "CANCELLED"},
    "BLOCKED": {"ACTIVE", "AT_RISK", "CANCELLED"},
    "COMPLETED": set(),
    "CANCELLED": set(),
}

PLAN_STATUSES = {"DRAFT", "APPROVED", "IN_EXECUTION", "SUPERSEDED", "COMPLETED", "CANCELLED"}
PRIORITY_LEVELS = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "PARKED"]
DEPARTMENTS = {
    "Sales", "Delivery", "Digital Workforce", "Media/Growth",
    "Falguna Engineering", "Finance", "Venture Studio", "Research",
}
DEPARTMENT_OBJECTIVE_STATUSES = {"PENDING", "ACTIVE", "BLOCKED", "COMPLETED", "CANCELLED"}
DECISION_STATUSES = {"PROPOSED", "APPROVED", "REJECTED", "DEFERRED"}
FAILURE_STATUSES = {"OPEN", "RETRIED", "REROUTED", "ESCALATED", "RESOLVED"}
POLICY_DOMAINS = {
    "spending", "client_commitments", "sales_discounts", "venture_budgets",
    "risk_thresholds", "external_communication", "destructive_actions",
}
POLICY_STATUSES = {"ACTIVE", "ARCHIVED"}


class CompanyOSError(ValueError):
    pass


def _json_loads(text: Optional[str], default=None):
    if not text:
        return default if default is not None else []
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default if default is not None else []


def _json_dumps(value) -> str:
    return json.dumps(value if value is not None else [])


# ---------------------------------------------------------------------------
# Section 2: Company Objective Model
# ---------------------------------------------------------------------------

class ObjectiveStore:
    """First-class company objectives. Lifecycle is explicit and checked
    (ALLOWED_OBJECTIVE_TRANSITIONS) -- no silent state jumps, same
    discipline as `wf_tasks`/`rh_lifecycle_events` elsewhere in this
    codebase. `current_progress` is free text the owner/system supplies
    (e.g. "3 of 5 milestones done") -- this module never invents a
    percentage no underlying data supports."""

    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit

    def create(
        self, title: str, actor: str = "Aryan", description: Optional[str] = None,
        owner: Optional[str] = None, priority: Optional[str] = None, target: Optional[str] = None,
        deadline: Optional[str] = None, linked_goals: Optional[List[str]] = None,
        linked_ventures: Optional[List[str]] = None, linked_departments: Optional[List[str]] = None,
        budget_scope: Optional[str] = None, risk_tolerance: Optional[str] = None,
        evidence: Optional[str] = None,
    ) -> str:
        if not title or not title.strip():
            raise CompanyOSError("title is required")
        if priority is not None and priority not in PRIORITY_LEVELS:
            raise CompanyOSError(f"priority must be one of {PRIORITY_LEVELS}")
        bad_departments = set(linked_departments or []) - DEPARTMENTS
        if bad_departments:
            raise CompanyOSError(f"unknown departments: {sorted(bad_departments)}")
        now = utcnow()
        objective_id = self.store.create("co_objectives", {
            "title": title.strip(), "description": description, "owner": owner, "priority": priority,
            "target": target, "deadline": deadline, "status": "DRAFT",
            "linked_goals_json": _json_dumps(linked_goals), "linked_ventures_json": _json_dumps(linked_ventures),
            "linked_departments_json": _json_dumps(linked_departments), "budget_scope": budget_scope,
            "risk_tolerance": risk_tolerance, "evidence": evidence, "current_progress": None,
            "actor": actor, "created_at": now, "updated_at": now,
        })
        self._record_event(objective_id, None, "DRAFT", actor, "objective created")
        self.audit.append("CO_OBJECTIVE_CREATED", {"objective_id": objective_id, "title": title, "actor": actor})
        return objective_id

    def _record_event(self, objective_id: str, from_status: Optional[str], to_status: str, actor: str, reason: Optional[str], evidence: Optional[Dict[str, Any]] = None) -> None:
        self.store.create("co_objective_status_events", {
            "objective_id": objective_id, "from_status": from_status, "to_status": to_status, "actor": actor,
            "reason": reason, "evidence_json": json.dumps(evidence) if evidence is not None else None, "created_at": utcnow(),
        })

    def transition(self, objective_id: str, to_status: str, actor: str, reason: Optional[str] = None, evidence: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        objective = self._require(objective_id)
        if to_status not in OBJECTIVE_STATUSES:
            raise CompanyOSError(f"unknown status: {to_status!r}")
        current = objective["status"]
        if current == to_status:
            return objective
        if to_status not in ALLOWED_OBJECTIVE_TRANSITIONS.get(current, set()):
            raise CompanyOSError(f"cannot transition an objective from {current} to {to_status}")
        self.store.update("co_objectives", objective_id, status=to_status)
        self._record_event(objective_id, current, to_status, actor, reason, evidence)
        self.audit.append("CO_OBJECTIVE_TRANSITIONED", {"objective_id": objective_id, "from": current, "to": to_status, "actor": actor})
        return self.get(objective_id)

    def update_progress(self, objective_id: str, current_progress: str, actor: str) -> Dict[str, Any]:
        self._require(objective_id)
        self.store.update("co_objectives", objective_id, current_progress=current_progress)
        self.audit.append("CO_OBJECTIVE_PROGRESS_UPDATED", {"objective_id": objective_id, "actor": actor})
        return self.get(objective_id)

    def _require(self, objective_id: str) -> Dict[str, Any]:
        objective = self.store.get("co_objectives", objective_id)
        if not objective:
            raise CompanyOSError("objective not found")
        return objective

    def get(self, objective_id: str) -> Optional[Dict[str, Any]]:
        objective = self.store.get("co_objectives", objective_id)
        return self._with_computed(objective) if objective else None

    def list(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        rows = self.store.list("co_objectives", "status=?", (status,)) if status else self.store.list("co_objectives")
        return [self._with_computed(o) for o in reversed(rows)]

    def history(self, objective_id: str) -> List[Dict[str, Any]]:
        return self.store.list("co_objective_status_events", "objective_id=?", (objective_id,))

    def _with_computed(self, objective: Dict[str, Any]) -> Dict[str, Any]:
        objective = dict(objective)
        objective["linked_goals"] = _json_loads(objective.get("linked_goals_json"))
        objective["linked_ventures"] = _json_loads(objective.get("linked_ventures_json"))
        objective["linked_departments"] = _json_loads(objective.get("linked_departments_json"))
        # Evidence-based, not a guess -- only flagged when a real stored
        # deadline has actually passed and the objective is not terminal.
        objective["past_deadline"] = bool(
            objective.get("deadline") and objective["status"] not in {"COMPLETED", "CANCELLED"} and objective["deadline"] < utcnow()[:10]
        )
        return objective


# ---------------------------------------------------------------------------
# Section 3: Company Plan Engine
# ---------------------------------------------------------------------------

class PlanStore:
    """Converts an APPROVED (i.e. status=ACTIVE) objective into a structured
    plan. Never generates fake certainty: milestones/risks/approvals are
    exactly what the caller supplies, persisted verbatim -- this store adds
    structure and status tracking, not invented content."""

    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit

    def create(
        self, objective_id: str, desired_outcome: str, actor: str = "Aryan",
        milestones: Optional[List[Dict[str, Any]]] = None, dependencies: Optional[List[str]] = None,
        ventures: Optional[List[str]] = None, departments: Optional[List[str]] = None,
        capital_requirement: Optional[str] = None, workforce_requirement: Optional[str] = None,
        falguna_work_requirement: Optional[str] = None, sales_media_needs: Optional[str] = None,
        risks: Optional[List[str]] = None, approvals: Optional[List[str]] = None,
        expected_evidence: Optional[str] = None,
    ) -> str:
        objective = self.store.get("co_objectives", objective_id)
        if not objective:
            raise CompanyOSError("objective not found")
        if objective["status"] not in {"ACTIVE", "AT_RISK", "BLOCKED"}:
            raise CompanyOSError("a plan can only be created for an approved (ACTIVE or recovering) objective")
        if not desired_outcome or not desired_outcome.strip():
            raise CompanyOSError("desired_outcome is required")
        bad_departments = set(departments or []) - DEPARTMENTS
        if bad_departments:
            raise CompanyOSError(f"unknown departments: {sorted(bad_departments)}")
        now = utcnow()
        plan_id = self.store.create("co_plans", {
            "objective_id": objective_id, "desired_outcome": desired_outcome.strip(),
            "milestones_json": _json_dumps(milestones), "dependencies_json": _json_dumps(dependencies),
            "ventures_json": _json_dumps(ventures), "departments_json": _json_dumps(departments),
            "capital_requirement": capital_requirement, "workforce_requirement": workforce_requirement,
            "falguna_work_requirement": falguna_work_requirement, "sales_media_needs": sales_media_needs,
            "risks_json": _json_dumps(risks), "approvals_json": _json_dumps(approvals),
            "expected_evidence": expected_evidence, "status": "DRAFT",
            "actor": actor, "created_at": now, "updated_at": now,
        })
        self.audit.append("CO_PLAN_CREATED", {"plan_id": plan_id, "objective_id": objective_id, "actor": actor})
        return plan_id

    def set_status(self, plan_id: str, status: str, actor: str) -> Dict[str, Any]:
        if status not in PLAN_STATUSES:
            raise CompanyOSError(f"status must be one of {sorted(PLAN_STATUSES)}")
        self._require(plan_id)
        self.store.update("co_plans", plan_id, status=status)
        self.audit.append("CO_PLAN_STATUS_CHANGED", {"plan_id": plan_id, "status": status, "actor": actor})
        return self.get(plan_id)

    def _require(self, plan_id: str) -> Dict[str, Any]:
        plan = self.store.get("co_plans", plan_id)
        if not plan:
            raise CompanyOSError("plan not found")
        return plan

    def get(self, plan_id: str) -> Optional[Dict[str, Any]]:
        plan = self.store.get("co_plans", plan_id)
        return self._with_computed(plan) if plan else None

    def list_for_objective(self, objective_id: str) -> List[Dict[str, Any]]:
        return [self._with_computed(p) for p in reversed(self.store.list("co_plans", "objective_id=?", (objective_id,)))]

    def active_plan(self, objective_id: str) -> Optional[Dict[str, Any]]:
        plans = [p for p in self.store.list("co_plans", "objective_id=?", (objective_id,)) if p["status"] in {"APPROVED", "IN_EXECUTION"}]
        return self._with_computed(sorted(plans, key=lambda p: p["created_at"])[-1]) if plans else None

    def _with_computed(self, plan: Dict[str, Any]) -> Dict[str, Any]:
        plan = dict(plan)
        for field in ("milestones", "dependencies", "ventures", "departments", "risks", "approvals"):
            plan[field] = _json_loads(plan.get(f"{field}_json"))
        return plan


# ---------------------------------------------------------------------------
# Section 4: Priority Engine
# ---------------------------------------------------------------------------

# Deterministic, transparent point bands -- small integers with a plain-text
# reason, never a fabricated float "score". Each input is optional; an input
# the caller doesn't supply simply contributes nothing (never guessed).
_BAND_POINTS = {"critical": 4, "high": 3, "medium": 2, "low": 1, "none": 0}


def compute_priority(inputs: Dict[str, Any]) -> Dict[str, Any]:
    """Transparent company-wide prioritization (Section 4). `inputs` may
    include any of: strategic_importance, revenue_potential, urgency,
    customer_impact, risk, resource_availability, venture_health (each a
    band: critical/high/medium/low/none), deadline_days (int, days until
    deadline), cost_band (critical/high/medium/low/none -- high cost pushes
    priority DOWN, not up), dependencies_count (int). Returns a small
    integer point total, the level it maps to, and a rationale listing
    exactly which inputs contributed -- never a spurious precision score."""
    contributions: List[str] = []
    points = 0

    for key in ("strategic_importance", "revenue_potential", "urgency", "customer_impact", "risk"):
        band = inputs.get(key)
        if band in _BAND_POINTS:
            weight = _BAND_POINTS[band]
            points += weight
            contributions.append(f"{key}={band} (+{weight})")

    deadline_days = inputs.get("deadline_days")
    if isinstance(deadline_days, (int, float)):
        if deadline_days <= 7:
            points += 3; contributions.append(f"deadline_days={deadline_days} (+3, due within a week)")
        elif deadline_days <= 30:
            points += 1; contributions.append(f"deadline_days={deadline_days} (+1, due within a month)")

    resource_availability = inputs.get("resource_availability")
    if resource_availability == "low":
        points -= 2; contributions.append("resource_availability=low (-2, capacity constrained)")
    elif resource_availability == "none":
        points -= 4; contributions.append("resource_availability=none (-4, no capacity)")

    venture_health = inputs.get("venture_health")
    if venture_health in ("low", "none"):
        points -= 2; contributions.append(f"venture_health={venture_health} (-2, struggling venture)")

    cost_band = inputs.get("cost_band")
    if cost_band in ("high", "critical"):
        points -= 2; contributions.append(f"cost_band={cost_band} (-2, expensive relative to benefit)")

    dependencies_count = inputs.get("dependencies_count")
    if isinstance(dependencies_count, (int, float)) and dependencies_count > 2:
        points -= 1; contributions.append(f"dependencies_count={dependencies_count} (-1, heavily blocked/blocking)")

    if points >= 10:
        level = "CRITICAL"
    elif points >= 6:
        level = "HIGH"
    elif points >= 3:
        level = "MEDIUM"
    elif points >= 0:
        level = "LOW"
    else:
        level = "PARKED"

    rationale = (
        f"Point total {points} from: " + ("; ".join(contributions) if contributions else "no inputs supplied")
        + f" -> {level}."
    )
    return {"priority": level, "points": points, "rationale": rationale}


class PriorityStore:
    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit

    def evaluate(self, ref_type: str, ref_id: str, inputs: Dict[str, Any], actor: str = "system") -> Dict[str, Any]:
        result = compute_priority(inputs)
        evaluation_id = self.store.create("co_priority_evaluations", {
            "ref_type": ref_type, "ref_id": ref_id, "inputs_json": json.dumps(inputs, sort_keys=True),
            "priority": result["priority"], "rationale": result["rationale"], "actor": actor, "created_at": utcnow(),
        })
        self.audit.append("CO_PRIORITY_EVALUATED", {"evaluation_id": evaluation_id, "ref_type": ref_type, "ref_id": ref_id, "priority": result["priority"], "actor": actor})
        return {"evaluation_id": evaluation_id, **result}

    def latest(self, ref_type: str, ref_id: str) -> Optional[Dict[str, Any]]:
        rows = self.store.list("co_priority_evaluations", "ref_type=? AND ref_id=?", (ref_type, ref_id))
        return sorted(rows, key=lambda r: r["created_at"])[-1] if rows else None

    def history(self, ref_type: str, ref_id: str) -> List[Dict[str, Any]]:
        return list(reversed(self.store.list("co_priority_evaluations", "ref_type=? AND ref_id=?", (ref_type, ref_id))))


# ---------------------------------------------------------------------------
# Section 5: Department Objectives
# ---------------------------------------------------------------------------

class DepartmentObjectiveStore:
    """Decomposes a company objective into department-level objectives.
    Each retains its parent link so Section 24 traceability always has a
    real row to walk, never an inferred one."""

    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit

    def create(
        self, company_objective_id: str, department: str, title: str, actor: str = "Aryan",
        owner: Optional[str] = None, due_date: Optional[str] = None, expected_output: Optional[str] = None,
        evidence: Optional[str] = None, dependencies: Optional[List[str]] = None,
    ) -> str:
        if department not in DEPARTMENTS:
            raise CompanyOSError(f"department must be one of {sorted(DEPARTMENTS)}")
        if not self.store.get("co_objectives", company_objective_id):
            raise CompanyOSError("company objective not found")
        if not title or not title.strip():
            raise CompanyOSError("title is required")
        now = utcnow()
        dept_objective_id = self.store.create("co_department_objectives", {
            "company_objective_id": company_objective_id, "department": department, "title": title.strip(),
            "owner": owner, "due_date": due_date, "expected_output": expected_output, "evidence": evidence,
            "dependencies_json": _json_dumps(dependencies), "status": "PENDING",
            "actor": actor, "created_at": now, "updated_at": now,
        })
        self.audit.append("CO_DEPARTMENT_OBJECTIVE_CREATED", {"dept_objective_id": dept_objective_id, "department": department, "company_objective_id": company_objective_id, "actor": actor})
        return dept_objective_id

    def set_status(self, dept_objective_id: str, status: str, actor: str) -> Dict[str, Any]:
        if status not in DEPARTMENT_OBJECTIVE_STATUSES:
            raise CompanyOSError(f"status must be one of {sorted(DEPARTMENT_OBJECTIVE_STATUSES)}")
        self._require(dept_objective_id)
        self.store.update("co_department_objectives", dept_objective_id, status=status)
        self.audit.append("CO_DEPARTMENT_OBJECTIVE_STATUS_CHANGED", {"dept_objective_id": dept_objective_id, "status": status, "actor": actor})
        return self.get(dept_objective_id)

    def _require(self, dept_objective_id: str) -> Dict[str, Any]:
        row = self.store.get("co_department_objectives", dept_objective_id)
        if not row:
            raise CompanyOSError("department objective not found")
        return row

    def get(self, dept_objective_id: str) -> Optional[Dict[str, Any]]:
        row = self.store.get("co_department_objectives", dept_objective_id)
        return self._with_computed(row) if row else None

    def list_for_objective(self, company_objective_id: str) -> List[Dict[str, Any]]:
        return [self._with_computed(r) for r in reversed(self.store.list("co_department_objectives", "company_objective_id=?", (company_objective_id,)))]

    def list_for_department(self, department: str, status: Optional[str] = None) -> List[Dict[str, Any]]:
        rows = self.store.list("co_department_objectives", "department=?", (department,))
        if status:
            rows = [r for r in rows if r["status"] == status]
        return [self._with_computed(r) for r in reversed(rows)]

    def list_all(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        rows = self.store.list("co_department_objectives", "status=?", (status,)) if status else self.store.list("co_department_objectives")
        return [self._with_computed(r) for r in reversed(rows)]

    def dependencies_satisfied(self, dept_objective_id: str) -> Dict[str, Any]:
        """Section 10: block downstream work when prerequisites are
        genuinely incomplete. `dependencies` holds other department
        objective ids -- satisfied only when every one of them is COMPLETED."""
        row = self._require(dept_objective_id)
        dep_ids = _json_loads(row.get("dependencies_json"))
        blocking = []
        for dep_id in dep_ids:
            dep = self.store.get("co_department_objectives", dep_id)
            if not dep or dep["status"] != "COMPLETED":
                blocking.append({"dept_objective_id": dep_id, "status": dep["status"] if dep else "NOT_FOUND"})
        return {"satisfied": not blocking, "blocking": blocking}

    def _with_computed(self, row: Dict[str, Any]) -> Dict[str, Any]:
        row = dict(row)
        row["dependencies"] = _json_loads(row.get("dependencies_json"))
        return row


# ---------------------------------------------------------------------------
# Section 6: Venture Alignment
# ---------------------------------------------------------------------------

class VentureAlignmentStore:
    """Links company objectives to ventures. Structurally cannot let one
    venture alter another: a link only ever references its own
    `venture_id`, and nothing here writes to `vs_ventures` or any other
    venture's rows -- see falguna/ventures.py for the only code that does."""

    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit

    def link(
        self, objective_id: str, venture_id: str, actor: str = "Aryan",
        contribution: Optional[str] = None, dependency: Optional[str] = None,
        priority: Optional[str] = None, budget_impact: Optional[str] = None,
        execution_health: Optional[str] = None,
    ) -> str:
        if not self.store.get("co_objectives", objective_id):
            raise CompanyOSError("objective not found")
        if not self.store.get("vs_ventures", venture_id):
            raise CompanyOSError("venture not found")
        if priority is not None and priority not in PRIORITY_LEVELS:
            raise CompanyOSError(f"priority must be one of {PRIORITY_LEVELS}")
        now = utcnow()
        link_id = self.store.create("co_venture_links", {
            "objective_id": objective_id, "venture_id": venture_id, "contribution": contribution,
            "dependency": dependency, "priority": priority, "budget_impact": budget_impact,
            "execution_health": execution_health, "actor": actor, "created_at": now, "updated_at": now,
        })
        self.audit.append("CO_VENTURE_LINK_CREATED", {"link_id": link_id, "objective_id": objective_id, "venture_id": venture_id, "actor": actor})
        return link_id

    def update(self, link_id: str, actor: str, **fields: Any) -> Dict[str, Any]:
        allowed = {"contribution", "dependency", "priority", "budget_impact", "execution_health"}
        unknown = set(fields) - allowed
        if unknown:
            raise CompanyOSError(f"unknown fields: {sorted(unknown)}")
        if not self.store.get("co_venture_links", link_id):
            raise CompanyOSError("venture link not found")
        self.store.update("co_venture_links", link_id, **fields)
        self.audit.append("CO_VENTURE_LINK_UPDATED", {"link_id": link_id, "changes": fields, "actor": actor})
        return self.store.get("co_venture_links", link_id)

    def list_for_objective(self, objective_id: str) -> List[Dict[str, Any]]:
        return list(reversed(self.store.list("co_venture_links", "objective_id=?", (objective_id,))))

    def list_for_venture(self, venture_id: str) -> List[Dict[str, Any]]:
        return list(reversed(self.store.list("co_venture_links", "venture_id=?", (venture_id,))))


# ---------------------------------------------------------------------------
# Section 7: Resource Allocation Engine v2
# ---------------------------------------------------------------------------

class ResourceRecommendationStore:
    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit

    def record(self, scope: str, finding: str, recommendation: str, severity: str, actor: str = "system", department: Optional[str] = None) -> str:
        now = utcnow()
        rec_id = self.store.create("co_resource_recommendations", {
            "scope": scope, "department": department, "finding": finding, "recommendation": recommendation,
            "severity": severity, "status": "OPEN", "actor": actor, "created_at": now, "updated_at": now,
        })
        self.audit.append("CO_RESOURCE_RECOMMENDATION_RECORDED", {"rec_id": rec_id, "scope": scope, "department": department, "severity": severity})
        return rec_id

    def list(self, status: Optional[str] = "OPEN") -> List[Dict[str, Any]]:
        rows = self.store.list("co_resource_recommendations", "status=?", (status,)) if status else self.store.list("co_resource_recommendations")
        return list(reversed(rows))

    def acknowledge(self, rec_id: str, actor: str) -> Dict[str, Any]:
        if not self.store.get("co_resource_recommendations", rec_id):
            raise CompanyOSError("recommendation not found")
        self.store.update("co_resource_recommendations", rec_id, status="ACKNOWLEDGED")
        self.audit.append("CO_RESOURCE_RECOMMENDATION_ACKNOWLEDGED", {"rec_id": rec_id, "actor": actor})
        return self.store.get("co_resource_recommendations", rec_id)


def resource_allocation_snapshot(store: StateStore, recommendations: Optional[ResourceRecommendationStore] = None, actor: str = "system") -> Dict[str, Any]:
    """Coordinates company-wide capacity across budget, workforce, media,
    sales attention, and research (Section 7). Read-only detection; when a
    `recommendations` store is supplied, findings are also persisted as
    OPEN co_resource_recommendations -- never auto-applied, exactly per the
    spec ("recommend reallocation but do not make high-impact changes
    automatically")."""
    findings: List[Dict[str, Any]] = []

    wf_tasks = store.list("wf_tasks")
    by_department: Dict[str, Dict[str, int]] = {}
    for task in wf_tasks:
        dept = by_department.setdefault(task["department"], {"total": 0, "active": 0, "blocked": 0, "idle_capacity": 0})
        dept["total"] += 1
        if task["status"] in ("EXECUTING", "READY", "PLANNING"):
            dept["active"] += 1
        if task["status"] in ("BLOCKED", "NEEDS_ARYAN", "FAILED"):
            dept["blocked"] += 1

    dept_objectives = store.list("co_department_objectives")
    pending_dept_objectives: Dict[str, int] = {}
    for row in dept_objectives:
        if row["status"] in ("PENDING", "ACTIVE"):
            pending_dept_objectives[row["department"]] = pending_dept_objectives.get(row["department"], 0) + 1

    # Resource starvation: a department with pending/active company work but
    # zero active workforce capacity behind it.
    for department, count in pending_dept_objectives.items():
        active = by_department.get(department, {}).get("active", 0)
        if active == 0:
            findings.append({
                "scope": "starvation", "department": department, "severity": "high",
                "finding": f"{department} has {count} pending/active department objective(s) but zero active workforce tasks behind them.",
                "recommendation": f"Assign workforce capacity to {department} or reprioritize its objectives.",
            })

    # Idle capacity: a department with active/ready workforce tasks but no
    # pending company objectives driving them (possible undirected spend).
    for department, counts in by_department.items():
        if counts["active"] > 0 and pending_dept_objectives.get(department, 0) == 0:
            findings.append({
                "scope": "idle_capacity", "department": department, "severity": "low",
                "finding": f"{department} has {counts['active']} active workforce task(s) with no pending company department objective driving them.",
                "recommendation": f"Confirm {department}'s active work is still aligned to a live company objective, or link it to one.",
            })

    # Over-allocation / budget conflict: reuse the existing Finance/Capital
    # Engine's own budget_status computation rather than recomputing spend.
    from .capital_and_risk import BudgetStore, budget_status
    for budget in BudgetStore(store, None).list():
        status = budget_status(store, budget)
        if status["over_limit"]:
            findings.append({
                "scope": "over_allocation", "department": budget["department"], "severity": "high",
                "finding": f"{budget['department']} has spent {status['spend_to_date']} against a {status['monthly_budget']} monthly budget.",
                "recommendation": f"Reduce {budget['department']} spend, or increase its budget explicitly.",
            })
        elif status["warning"]:
            findings.append({
                "scope": "over_allocation", "department": budget["department"], "severity": "medium",
                "finding": f"{budget['department']} spend ({status['spend_to_date']}) is approaching its {status['monthly_budget']} monthly budget.",
                "recommendation": f"Monitor {budget['department']} spend for the rest of the period.",
            })

    # Duplicated work: two open (non-terminal) tasks in the same department
    # with the exact same objective text -- a cheap, honest signal, never a
    # fuzzy-match guess.
    seen: Dict[tuple, List[str]] = {}
    for task in wf_tasks:
        if task["status"] in ("COMPLETED", "CANCELLED"):
            continue
        key = (task["department"], task["objective"].strip().lower())
        seen.setdefault(key, []).append(task["id"])
    for (department, objective_text), ids in seen.items():
        if len(ids) > 1:
            findings.append({
                "scope": "duplicated_work", "department": department, "severity": "medium",
                "finding": f"{len(ids)} open workforce tasks in {department} share the exact same objective text.",
                "recommendation": "Confirm these are not duplicates; cancel or merge if they are.",
            })

    # Conflicting deadlines: 3+ department objectives due the same department
    # on the same date -- a real capacity-contention signal.
    due_counts: Dict[tuple, int] = {}
    for row in dept_objectives:
        if row.get("due_date") and row["status"] in ("PENDING", "ACTIVE"):
            due_counts[(row["department"], row["due_date"])] = due_counts.get((row["department"], row["due_date"]), 0) + 1
    for (department, due_date), count in due_counts.items():
        if count >= 3:
            findings.append({
                "scope": "conflicting_deadlines", "department": department, "severity": "medium",
                "finding": f"{department} has {count} department objectives all due on {due_date}.",
                "recommendation": f"Re-sequence {department}'s deadlines or confirm capacity is sufficient.",
            })

    recorded_ids = []
    if recommendations is not None:
        for finding in findings:
            recorded_ids.append(recommendations.record(
                finding["scope"], finding["finding"], finding["recommendation"], finding["severity"],
                actor=actor, department=finding.get("department"),
            ))

    return {
        "generated_at": utcnow(), "by_department": by_department,
        "pending_department_objectives": pending_dept_objectives,
        "findings": findings, "recorded_recommendation_ids": recorded_ids,
        "source": "wf_tasks, co_department_objectives, cc_budgets (via BudgetStore.budget_status)",
        "note": "Recommendations only -- nothing here reallocates budget or capacity automatically.",
    }


# ---------------------------------------------------------------------------
# Section 8: Capital Allocation Orchestration
# ---------------------------------------------------------------------------

class CapitalRecommendationStore:
    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit

    def record(self, recommendation: str, rationale: str, actor: str = "system", objective_id: Optional[str] = None, amount: Optional[float] = None) -> str:
        rec_id = self.store.create("co_capital_recommendations", {
            "objective_id": objective_id, "recommendation": recommendation, "rationale": rationale,
            "amount": amount, "status": "OPEN", "actor": actor, "created_at": utcnow(),
        })
        self.audit.append("CO_CAPITAL_RECOMMENDATION_RECORDED", {"rec_id": rec_id, "objective_id": objective_id, "recommendation": recommendation})
        return rec_id

    def list(self, status: Optional[str] = "OPEN") -> List[Dict[str, Any]]:
        rows = self.store.list("co_capital_recommendations", "status=?", (status,)) if status else self.store.list("co_capital_recommendations")
        return list(reversed(rows))


def capital_orchestration_snapshot(store: StateStore) -> Dict[str, Any]:
    """Integrates the existing Finance/Capital Engine (Section 8) --
    cash, reserves, committed obligations, venture budgets, expected
    receivables, spend -- all read, nothing moved. `recommend_*` calls
    remain the caller's own explicit action against `available_amount`;
    this function never invents one."""
    from .capital_and_risk import BudgetStore, ReservePolicyStore, allowed_experimental_capital, budget_status
    from .command_center import command_center_snapshot

    snapshot = command_center_snapshot(store)
    reserve_policy = ReservePolicyStore(store).get()
    budgets = [budget_status(store, b) for b in BudgetStore(store, None).list()]
    committed_obligations = round(sum(b["monthly_budget"] for b in BudgetStore(store, None).list()), 2)
    venture_allocations = store.list("vs_capital_allocations")
    venture_committed: Dict[str, float] = {}
    for allocation in venture_allocations:
        # Same convention as ventures.py's own CapitalAllocationStore.net_allocated:
        # direction is "IN" (capital committed to this venture) or "OUT".
        sign = 1 if allocation["direction"] == "IN" else -1
        venture_committed[allocation["venture_id"]] = venture_committed.get(allocation["venture_id"], 0.0) + sign * allocation["amount"]

    return {
        "generated_at": utcnow(),
        "cash": snapshot["cash"],
        "reserve_policy": reserve_policy,
        "experimental_capital": allowed_experimental_capital(store),
        "committed_obligations": {"value": committed_obligations, "source": "cc_budgets.monthly_budget (sum)"},
        "department_budgets": budgets,
        "venture_committed_capital": {vid: round(amount, 2) for vid, amount in venture_committed.items()},
        "expected_receivables": snapshot["receivables"],
        "note": (
            "Read-only orchestration view. Recommending an allocation is a separate, explicit call "
            "(falguna.capital_and_risk.recommend_allocation) against a stated available_amount; nothing here "
            "moves money, and any recommendation still requires a Needs Aryan decision "
            "(kind=capital_allocation_execution) before real spend follows it."
        ),
    }


# ---------------------------------------------------------------------------
# Section 9: Execution Orchestrator
# ---------------------------------------------------------------------------

class ExecutionOrchestrator:
    """Routes an approved plan's department needs to existing execution
    engines -- it never reimplements them. Today this means: creating
    real, tracked `wf_tasks` (Digital Workforce's own store) for
    departments the plan names, and structured `co_department_objectives`
    for every named department, each traced back to the plan/objective.
    Falguna Engineering missions, real sales outreach, and financial
    execution stay their own systems' explicit actions (mission creation
    needs a real repository and policy the caller must supply; this
    orchestrator only records the *intent* via a traceability link and a
    company event so the need is visible, never silently invents a
    mission)."""

    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit
        self.dept_objectives = DepartmentObjectiveStore(store, audit)
        self.events = EventBus(store, audit)

    def route_plan(self, plan_id: str, actor: str = "system") -> Dict[str, Any]:
        plan = self.store.get("co_plans", plan_id)
        if not plan:
            raise CompanyOSError("plan not found")
        if plan["status"] not in {"APPROVED", "IN_EXECUTION"}:
            raise CompanyOSError("a plan must be APPROVED before it can be routed for execution")
        departments = _json_loads(plan.get("departments_json"))
        created_dept_objectives: List[str] = []
        created_wf_tasks: List[str] = []
        skipped: List[str] = []

        existing = {row["department"] for row in self.store.list("co_department_objectives", "company_objective_id=?", (plan["objective_id"],))}
        for department in departments:
            if department not in DEPARTMENTS:
                skipped.append(f"{department}: not a recognized department")
                continue
            if department in existing:
                skipped.append(f"{department}: department objective already exists for this company objective")
                continue
            dept_objective_id = self.dept_objectives.create(
                plan["objective_id"], department, f"{plan['desired_outcome']} -- {department}", actor=actor,
                expected_output=plan.get("expected_evidence"),
            )
            created_dept_objectives.append(dept_objective_id)
            link(self.store, self.audit, "co_plans", plan_id, "co_department_objectives", dept_objective_id, actor)

            if department == "Digital Workforce":
                from .workforce import WorkforceTaskStore
                task_id = WorkforceTaskStore(self.store, self.audit).create(
                    department=department, objective=plan["desired_outcome"], task_type="company_os_routed",
                    actor=actor, source="company_os",
                )
                self.store.update("wf_tasks", task_id, co_objective_id=plan["objective_id"])
                created_wf_tasks.append(task_id)
                link(self.store, self.audit, "co_department_objectives", dept_objective_id, "wf_tasks", task_id, actor)

        if plan["status"] == "APPROVED":
            self.store.update("co_plans", plan_id, status="IN_EXECUTION")

        self.events.emit("EXECUTION_ROUTED", "company_os", ref_type="co_plans", ref_id=plan_id, payload={
            "created_dept_objectives": created_dept_objectives, "created_wf_tasks": created_wf_tasks, "skipped": skipped,
        })
        self.audit.append("CO_EXECUTION_ORCHESTRATED", {"plan_id": plan_id, "created_dept_objectives": created_dept_objectives, "created_wf_tasks": created_wf_tasks, "actor": actor})
        return {
            "plan_id": plan_id, "created_dept_objectives": created_dept_objectives,
            "created_wf_tasks": created_wf_tasks, "skipped": skipped,
            "note": (
                "Falguna Engineering missions, real sales outreach, and financial execution are not created "
                "automatically here -- they remain explicit actions in their own systems. This call only "
                "routes internal planning/task-creation work, per the orchestrator autonomy boundary."
            ),
        }


# ---------------------------------------------------------------------------
# Section 11: Company Event Bus
# ---------------------------------------------------------------------------

class EventBus:
    """Lightweight, persistent, auditable, timestamped, source-tagged event
    system. Idempotent when the caller supplies an `idempotency_key` --
    emitting the same key twice returns the original event rather than
    creating a duplicate."""

    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit

    def emit(self, event_type: str, source: str, ref_type: Optional[str] = None, ref_id: Optional[str] = None, payload: Optional[Dict[str, Any]] = None, idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        if idempotency_key:
            existing = self.store.list("co_events", "idempotency_key=?", (idempotency_key,))
            if existing:
                return existing[-1]
        event_id = self.store.create("co_events", {
            "event_type": event_type, "source": source, "ref_type": ref_type, "ref_id": ref_id,
            "payload_json": json.dumps(payload) if payload is not None else None,
            "idempotency_key": idempotency_key, "created_at": utcnow(),
        })
        self.audit.append("CO_EVENT_EMITTED", {"event_id": event_id, "event_type": event_type, "source": source})
        return self.store.get("co_events", event_id)

    def list(self, event_type: Optional[str] = None, ref_type: Optional[str] = None, ref_id: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if event_type:
            clauses.append("event_type=?"); params.append(event_type)
        if ref_type:
            clauses.append("ref_type=?"); params.append(ref_type)
        if ref_id:
            clauses.append("ref_id=?"); params.append(ref_id)
        where = " AND ".join(clauses) if clauses else "1=1"
        rows = list(reversed(self.store.list("co_events", where, tuple(params))))
        return rows[:limit]


# ---------------------------------------------------------------------------
# Section 24: Objective -> Execution Traceability (defined early: used by
# the Execution Orchestrator above and the Company State Model below)
# ---------------------------------------------------------------------------

def link(store: StateStore, audit: AuditLog, from_type: str, from_id: str, to_type: str, to_id: str, actor: str = "system") -> str:
    existing = store.list("co_traceability_links", "from_type=? AND from_id=? AND to_type=? AND to_id=?", (from_type, from_id, to_type, to_id))
    if existing:
        return existing[0]["id"]
    link_id = store.create("co_traceability_links", {
        "from_type": from_type, "from_id": from_id, "to_type": to_type, "to_id": to_id,
        "actor": actor, "created_at": utcnow(),
    })
    audit.append("CO_TRACEABILITY_LINK_CREATED", {"link_id": link_id, "from": f"{from_type}:{from_id}", "to": f"{to_type}:{to_id}"})
    return link_id


def trace_objective(store: StateStore, objective_id: str) -> Dict[str, Any]:
    """Full Objective -> Plan -> Venture/Department Objective -> Task/
    Mission -> Evidence -> Result -> KPI traceability (Section 24). Walks
    real stored links (co_traceability_links) plus the direct foreign-key
    relationships that already exist (co_plans.objective_id,
    co_department_objectives.company_objective_id, wf_tasks.co_objective_id)
    -- never inferred by name matching."""
    objective = store.get("co_objectives", objective_id)
    if not objective:
        raise CompanyOSError("objective not found")

    plans = list(reversed(store.list("co_plans", "objective_id=?", (objective_id,))))
    dept_objectives = list(reversed(store.list("co_department_objectives", "company_objective_id=?", (objective_id,))))
    venture_links = list(reversed(store.list("co_venture_links", "objective_id=?", (objective_id,))))
    wf_tasks = list(reversed(store.list("wf_tasks", "co_objective_id=?", (objective_id,))))
    missions = list(reversed(store.list("missions", "co_objective_id=?", (objective_id,))))

    evidence = []
    for task in wf_tasks:
        if task.get("evidence_json"):
            evidence.append({"source": "wf_tasks", "id": task["id"], "evidence": _json_loads(task["evidence_json"], default={})})

    linked_goals = _json_loads(objective.get("linked_goals_json"))
    goals = [store.get("cc_goals", goal_id) for goal_id in linked_goals]
    goals = [g for g in goals if g]

    return {
        "objective": objective,
        "plans": plans,
        "department_objectives": dept_objectives,
        "venture_links": venture_links,
        "wf_tasks": wf_tasks,
        "missions": missions,
        "evidence": evidence,
        "linked_goals": goals,
        "explicit_links": list(reversed(store.list("co_traceability_links", "from_type=? AND from_id=?", ("co_objectives", objective_id)))),
        "note": "Why this work exists: every row above traces back to this one objective via a real stored foreign key or traceability link, never a name match.",
    }


# ---------------------------------------------------------------------------
# Section 12: Company State Model
# ---------------------------------------------------------------------------

def company_state_snapshot(store: StateStore) -> Dict[str, Any]:
    """A pure read/compute function -- never a persisted table, so it can
    never drift from the systems it summarizes (Section 12). This is the
    orchestrator's working context: objectives, priorities, active
    ventures, active clients, delivery, workforce, sales, media, finance,
    risks, Needs Aryan."""
    from .capital_and_risk import BudgetStore, RiskRegisterStore, budget_status
    from .command_center import command_center_snapshot, kpi_snapshot
    from .ttt_hq import NeedsAryanQueue
    from .ventures import VentureStore

    objectives = ObjectiveStore(store, None).list()
    objectives_by_status: Dict[str, int] = {}
    for o in objectives:
        objectives_by_status[o["status"]] = objectives_by_status.get(o["status"], 0) + 1

    priority_rows = store.list("co_priority_evaluations")
    latest_priority_by_ref: Dict[str, Dict[str, Any]] = {}
    for row in priority_rows:
        key = f"{row['ref_type']}:{row['ref_id']}"
        if key not in latest_priority_by_ref or row["created_at"] > latest_priority_by_ref[key]["created_at"]:
            latest_priority_by_ref[key] = row

    ventures = VentureStore(store, None).list(status="ACTIVE")
    clients = store.list("clients")
    active_clients = [c for c in clients if c.get("status") not in ("CHURNED", "LOST")]

    cc_snapshot = command_center_snapshot(store)
    kpis = kpi_snapshot(store)

    budgets = [budget_status(store, b) for b in BudgetStore(store, None).list()]
    open_risks = RiskRegisterStore(store, None).list(status="OPEN")

    needs_aryan_pending = NeedsAryanQueue(store, None, None).list_pending()

    return {
        "generated_at": utcnow(),
        "objectives": {"total": len(objectives), "by_status": objectives_by_status, "items": objectives},
        "priorities": {"latest_by_ref": latest_priority_by_ref},
        "ventures": {"active_count": len(ventures), "items": ventures},
        "clients": {"active_count": len(active_clients), "total_count": len(clients)},
        "delivery": cc_snapshot["delivery"],
        "workforce": cc_snapshot["workforce"],
        "sales": cc_snapshot["pipeline"],
        "media": cc_snapshot["media"],
        "finance": {"cash": cc_snapshot["cash"], "receivables": cc_snapshot["receivables"], "budgets": budgets},
        "risks": {"open_count": len(open_risks), "items": open_risks},
        "needs_aryan": {"pending_count": len(needs_aryan_pending), "items": needs_aryan_pending},
        "kpis": kpis,
        "source": "co_objectives, co_priority_evaluations, vs_ventures, clients, command_center_snapshot, cc_budgets, cc_risks, needs_aryan_items+supervisor_states",
    }


# ---------------------------------------------------------------------------
# Section 13: Replanning Engine
# ---------------------------------------------------------------------------

class ReplanStore:
    """Re-evaluates plans on deadline slip, budget change, venture failure,
    revenue change, client priority change, execution block, or a major
    risk. Preserves the original plan, the changed assumptions, the reason,
    the actor, and (once linked) the new plan -- nothing here overwrites or
    deletes the original plan row."""

    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit

    def replan(self, objective_id: str, reason: str, actor: str = "Aryan", changed_assumptions: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if not self.store.get("co_objectives", objective_id):
            raise CompanyOSError("objective not found")
        if not reason or not reason.strip():
            raise CompanyOSError("reason is required")
        active_plans = [p for p in self.store.list("co_plans", "objective_id=?", (objective_id,)) if p["status"] in {"APPROVED", "IN_EXECUTION"}]
        original_plan = sorted(active_plans, key=lambda p: p["created_at"])[-1] if active_plans else None
        if original_plan:
            self.store.update("co_plans", original_plan["id"], status="SUPERSEDED")
        now = utcnow()
        replan_id = self.store.create("co_replans", {
            "objective_id": objective_id, "original_plan_id": original_plan["id"] if original_plan else None,
            "new_plan_id": None, "reason": reason.strip(),
            "changed_assumptions_json": json.dumps(changed_assumptions) if changed_assumptions is not None else None,
            "original_plan_snapshot_json": json.dumps(original_plan) if original_plan else None,
            "actor": actor, "created_at": now, "updated_at": now,
        })
        self.audit.append("CO_REPLAN_TRIGGERED", {"replan_id": replan_id, "objective_id": objective_id, "reason": reason, "actor": actor})
        return self.store.get("co_replans", replan_id)

    def link_new_plan(self, replan_id: str, new_plan_id: str, actor: str = "Aryan") -> Dict[str, Any]:
        if not self.store.get("co_replans", replan_id):
            raise CompanyOSError("replan not found")
        if not self.store.get("co_plans", new_plan_id):
            raise CompanyOSError("new plan not found")
        self.store.update("co_replans", replan_id, new_plan_id=new_plan_id)
        self.audit.append("CO_REPLAN_LINKED_NEW_PLAN", {"replan_id": replan_id, "new_plan_id": new_plan_id, "actor": actor})
        return self.store.get("co_replans", replan_id)

    def list_for_objective(self, objective_id: str) -> List[Dict[str, Any]]:
        return list(reversed(self.store.list("co_replans", "objective_id=?", (objective_id,))))


# ---------------------------------------------------------------------------
# Section 16: Decision Engine
# ---------------------------------------------------------------------------

def is_high_impact(cost: Optional[float] = None, risk: Optional[str] = None, irreversibility: Optional[str] = None, external_commitment: Optional[bool] = None, cost_threshold: float = 100000.0) -> bool:
    """Shared deterministic gate used by both the Decision Engine and the
    Escalation Engine (Section 22), so the two never disagree on what
    counts as high-impact. Any one qualifying signal is enough."""
    if risk in ("high", "critical"):
        return True
    if irreversibility in ("irreversible", "hard_to_reverse"):
        return True
    if external_commitment:
        return True
    if cost is not None and cost >= cost_threshold:
        return True
    return False


class DecisionStore:
    """Structured decisions (Section 16): question, options, evidence,
    risks, cost, expected impact, recommendation, confidence/uncertainty.
    High-impact decisions are escalated to Needs Aryan on creation, exactly
    once, using the same idempotency guard `budget_status`/venture
    recommendations already use elsewhere."""

    def __init__(self, store: StateStore, audit: AuditLog, needs_aryan=None):
        self.store = store
        self.audit = audit
        self.needs_aryan = needs_aryan

    def create(
        self, question: str, options: List[Dict[str, Any]], actor: str = "system",
        evidence: Optional[str] = None, risks: Optional[str] = None, cost: Optional[str] = None,
        expected_impact: Optional[str] = None, recommendation: Optional[str] = None, confidence: Optional[str] = None,
        ref_type: Optional[str] = None, ref_id: Optional[str] = None,
        high_impact: bool = False,
    ) -> str:
        if not question or not question.strip():
            raise CompanyOSError("question is required")
        if not options:
            raise CompanyOSError("at least one option is required")
        now = utcnow()
        decision_id = self.store.create("co_decisions", {
            "question": question.strip(), "options_json": json.dumps(options), "evidence": evidence,
            "risks": risks, "cost": cost, "expected_impact": expected_impact, "recommendation": recommendation,
            "confidence": confidence, "status": "PROPOSED", "decided_by": None, "decision_note": None,
            "ref_type": ref_type, "ref_id": ref_id, "needs_aryan_id": None,
            "actor": actor, "created_at": now, "updated_at": now, "decided_at": None,
        })
        needs_aryan_id = None
        if high_impact and self.needs_aryan is not None:
            needs_aryan_id = self.needs_aryan.create_item(
                "strategic_decision", f"Decision needed: {question.strip()}",
                f"{len(options)} option(s) prepared with evidence and a recommendation. Approve one, reject, or defer.",
                actor=actor, recommendation=recommendation, rationale=evidence, risk=risks,
                ref_type="co_decisions", ref_id=decision_id,
            )
            self.store.update("co_decisions", decision_id, needs_aryan_id=needs_aryan_id)
        self.audit.append("CO_DECISION_CREATED", {"decision_id": decision_id, "high_impact": high_impact, "needs_aryan_id": needs_aryan_id, "actor": actor})
        return decision_id

    def decide(self, decision_id: str, status: str, actor: str, note: Optional[str] = None) -> Dict[str, Any]:
        if status not in DECISION_STATUSES or status == "PROPOSED":
            raise CompanyOSError(f"status must be one of {sorted(DECISION_STATUSES - {'PROPOSED'})}")
        decision = self.store.get("co_decisions", decision_id)
        if not decision:
            raise CompanyOSError("decision not found")
        self.store.update("co_decisions", decision_id, status=status, decided_by=actor, decision_note=note, decided_at=utcnow())
        self.audit.append("CO_DECISION_DECIDED", {"decision_id": decision_id, "status": status, "actor": actor})
        return self.get(decision_id)

    def get(self, decision_id: str) -> Optional[Dict[str, Any]]:
        row = self.store.get("co_decisions", decision_id)
        if not row:
            return None
        row = dict(row)
        row["options"] = _json_loads(row.get("options_json"))
        return row

    def list(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        rows = self.store.list("co_decisions", "status=?", (status,)) if status else self.store.list("co_decisions")
        return [dict(r, options=_json_loads(r.get("options_json"))) for r in reversed(rows)]


# ---------------------------------------------------------------------------
# Section 21: Policy Engine
# ---------------------------------------------------------------------------

class CompanyPolicyStore:
    """Reusable company policies gating when autonomous action is allowed
    vs. when Needs Aryan is required. `rule_json` is a plain threshold
    dict the caller defines (e.g. {"autonomous_max_amount": 5000}) --
    `evaluate` only ever compares real supplied context values against it,
    never infers a threshold on its own. No configured policy for a domain
    is a fail-closed default: Needs Aryan is required."""

    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit

    def create(self, domain: str, title: str, rule: Dict[str, Any], actor: str = "Aryan", requires_needs_aryan: bool = True) -> str:
        if domain not in POLICY_DOMAINS:
            raise CompanyOSError(f"domain must be one of {sorted(POLICY_DOMAINS)}")
        if not title or not title.strip():
            raise CompanyOSError("title is required")
        now = utcnow()
        policy_id = self.store.create("co_policies", {
            "domain": domain, "title": title.strip(), "rule_json": json.dumps(rule or {}),
            "requires_needs_aryan": 1 if requires_needs_aryan else 0, "status": "ACTIVE",
            "actor": actor, "created_at": now, "updated_at": now,
        })
        self.audit.append("CO_POLICY_CREATED", {"policy_id": policy_id, "domain": domain, "actor": actor})
        return policy_id

    def archive(self, policy_id: str, actor: str) -> Dict[str, Any]:
        if not self.store.get("co_policies", policy_id):
            raise CompanyOSError("policy not found")
        self.store.update("co_policies", policy_id, status="ARCHIVED")
        self.audit.append("CO_POLICY_ARCHIVED", {"policy_id": policy_id, "actor": actor})
        return self.store.get("co_policies", policy_id)

    def list(self, domain: Optional[str] = None, status: str = "ACTIVE") -> List[Dict[str, Any]]:
        rows = self.store.list("co_policies", "status=?", (status,)) if status else self.store.list("co_policies")
        if domain:
            rows = [r for r in rows if r["domain"] == domain]
        return [dict(r, rule=_json_loads(r.get("rule_json"), default={})) for r in reversed(rows)]


def evaluate_policy(store: StateStore, domain: str, context: Dict[str, Any]) -> Dict[str, Any]:
    """Evaluates `context` (e.g. {"amount": 3000}) against every ACTIVE
    policy in `domain`. autonomous_allowed is True only when a matching
    policy explicitly permits it AND the numeric fields it names as
    thresholds are all within bounds. No policy for the domain -> fail
    closed (Needs Aryan required)."""
    policies = CompanyPolicyStore(store, None).list(domain=domain, status="ACTIVE")
    if not policies:
        return {
            "autonomous_allowed": False, "requires_needs_aryan": True, "matched_policy_id": None,
            "reason": f"no active policy configured for domain={domain!r} -- defaulting to Needs Aryan (fail-closed)",
        }
    for policy in policies:
        rule = policy["rule"]
        if not policy["requires_needs_aryan"]:
            within_bounds = True
            for key, limit in rule.items():
                if key.startswith("autonomous_max_") and isinstance(limit, (int, float)):
                    context_key = key[len("autonomous_max_"):]
                    value = context.get(context_key)
                    if value is None or value > limit:
                        within_bounds = False
                        break
            if within_bounds:
                return {
                    "autonomous_allowed": True, "requires_needs_aryan": False, "matched_policy_id": policy["id"],
                    "reason": f"context is within {policy['title']}'s declared autonomous bounds",
                }
    return {
        "autonomous_allowed": False, "requires_needs_aryan": True, "matched_policy_id": policies[0]["id"],
        "reason": "no active policy for this domain explicitly clears context for autonomous action -- defaulting to Needs Aryan",
    }


# ---------------------------------------------------------------------------
# Section 22: Escalation Engine
# ---------------------------------------------------------------------------

def escalate_if_warranted(
    store: StateStore, needs_aryan, ref_type: str, ref_id: str, kind: str, title: str, what_is_needed: str,
    actor: str = "system", impact: Optional[str] = None, risk: Optional[str] = None, cost: Optional[float] = None,
    irreversibility: Optional[str] = None, external_commitment: bool = False, rationale: Optional[str] = None,
) -> Dict[str, Any]:
    """Escalation based on impact/risk/cost/irreversibility/external
    commitment (Section 22) -- routine, low-risk work is recorded but
    never pushed to Needs Aryan, avoiding approval spam. Always logs a
    `co_escalations` row (auditable either way); only creates a Needs
    Aryan item when the deterministic gate actually fires."""
    warranted = is_high_impact(cost=cost, risk=risk or impact, irreversibility=irreversibility, external_commitment=external_commitment)
    needs_aryan_id = None
    if warranted and needs_aryan is not None:
        needs_aryan_id = needs_aryan.create_item(
            kind, title, what_is_needed, actor=actor, rationale=rationale, risk=risk or impact,
            ref_type=ref_type, ref_id=ref_id,
        )
    decision = "ESCALATED" if warranted else "NOT_ESCALATED"
    escalation_id = store.create("co_escalations", {
        "ref_type": ref_type, "ref_id": ref_id, "impact": impact, "risk": risk, "cost": str(cost) if cost is not None else None,
        "irreversibility": irreversibility, "external_commitment": "yes" if external_commitment else "no",
        "decision": decision, "needs_aryan_id": needs_aryan_id, "actor": actor, "created_at": utcnow(),
    })
    return {"escalation_id": escalation_id, "decision": decision, "needs_aryan_id": needs_aryan_id}


# ---------------------------------------------------------------------------
# Section 25: Company Memory / Decision Memory
# ---------------------------------------------------------------------------

class CompanyMemoryStore:
    """Persists objectives, plans, decisions, reasons, assumptions,
    outcomes, and lessons -- company memory, not yet the future personal
    Falguna knowledge graph (explicitly excluded from this phase, Section
    34)."""

    KINDS = {"objective", "plan", "decision", "assumption", "outcome", "lesson"}

    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit

    def record(self, subject_type: str, kind: str, content: str, actor: str = "system", subject_id: Optional[str] = None) -> str:
        if kind not in self.KINDS:
            raise CompanyOSError(f"kind must be one of {sorted(self.KINDS)}")
        if not content or not content.strip():
            raise CompanyOSError("content is required")
        memory_id = self.store.create("co_memory", {
            "subject_type": subject_type, "subject_id": subject_id, "kind": kind, "content": content.strip(),
            "actor": actor, "created_at": utcnow(),
        })
        self.audit.append("CO_MEMORY_RECORDED", {"memory_id": memory_id, "subject_type": subject_type, "kind": kind})
        return memory_id

    def list_for(self, subject_type: str, subject_id: str) -> List[Dict[str, Any]]:
        return list(reversed(self.store.list("co_memory", "subject_type=? AND subject_id=?", (subject_type, subject_id))))

    def list_all(self, kind: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        rows = self.store.list("co_memory", "kind=?", (kind,)) if kind else self.store.list("co_memory")
        return list(reversed(rows))[:limit]


# ---------------------------------------------------------------------------
# Section 26: Failure / Blocker Management
# ---------------------------------------------------------------------------

class FailureStore:
    """When work fails: record it, note the dependency if any, allow a
    bounded retry, allow rerouting, escalate if needed -- but a failure is
    never silently abandoned: it must be moved forward to RETRIED/
    REROUTED/ESCALATED/RESOLVED, and stays OPEN (visibly, in listings)
    until one of those actually happens."""

    def __init__(self, store: StateStore, audit: AuditLog, needs_aryan=None):
        self.store = store
        self.audit = audit
        self.needs_aryan = needs_aryan

    def record(self, ref_type: str, ref_id: str, description: str, actor: str = "system", dependency: Optional[str] = None) -> str:
        if not description or not description.strip():
            raise CompanyOSError("description is required")
        now = utcnow()
        failure_id = self.store.create("co_failures", {
            "ref_type": ref_type, "ref_id": ref_id, "description": description.strip(), "dependency": dependency,
            "retried": 0, "rerouted": 0, "escalated": 0, "needs_aryan_id": None, "status": "OPEN",
            "actor": actor, "created_at": now, "updated_at": now,
        })
        self.audit.append("CO_FAILURE_RECORDED", {"failure_id": failure_id, "ref_type": ref_type, "ref_id": ref_id, "actor": actor})
        return failure_id

    def retry(self, failure_id: str, actor: str, note: Optional[str] = None) -> Dict[str, Any]:
        self._require(failure_id)
        self.store.update("co_failures", failure_id, status="RETRIED", retried=1)
        self.audit.append("CO_FAILURE_RETRIED", {"failure_id": failure_id, "actor": actor, "note": note})
        return self.get(failure_id)

    def reroute(self, failure_id: str, actor: str, note: Optional[str] = None) -> Dict[str, Any]:
        self._require(failure_id)
        self.store.update("co_failures", failure_id, status="REROUTED", rerouted=1)
        self.audit.append("CO_FAILURE_REROUTED", {"failure_id": failure_id, "actor": actor, "note": note})
        return self.get(failure_id)

    def escalate(self, failure_id: str, actor: str, title: str, what_is_needed: str) -> Dict[str, Any]:
        failure = self._require(failure_id)
        needs_aryan_id = None
        if self.needs_aryan is not None:
            needs_aryan_id = self.needs_aryan.create_item(
                "risky_action", title, what_is_needed, actor=actor, rationale=failure["description"],
                ref_type=failure["ref_type"], ref_id=failure["ref_id"],
            )
        self.store.update("co_failures", failure_id, status="ESCALATED", escalated=1, needs_aryan_id=needs_aryan_id)
        self.audit.append("CO_FAILURE_ESCALATED", {"failure_id": failure_id, "needs_aryan_id": needs_aryan_id, "actor": actor})
        return self.get(failure_id)

    def resolve(self, failure_id: str, actor: str, note: Optional[str] = None) -> Dict[str, Any]:
        self._require(failure_id)
        self.store.update("co_failures", failure_id, status="RESOLVED")
        self.audit.append("CO_FAILURE_RESOLVED", {"failure_id": failure_id, "actor": actor, "note": note})
        return self.get(failure_id)

    def _require(self, failure_id: str) -> Dict[str, Any]:
        row = self.store.get("co_failures", failure_id)
        if not row:
            raise CompanyOSError("failure not found")
        return row

    def get(self, failure_id: str) -> Optional[Dict[str, Any]]:
        return self.store.get("co_failures", failure_id)

    def list(self, status: Optional[str] = "OPEN") -> List[Dict[str, Any]]:
        rows = self.store.list("co_failures", "status=?", (status,)) if status else self.store.list("co_failures")
        return list(reversed(rows))


# ---------------------------------------------------------------------------
# Section 27: Cost Awareness
# ---------------------------------------------------------------------------

class CostEstimateStore:
    """Every major plan/task can know its estimated/actual AI, API,
    workforce, and project cost. Nullable throughout -- an unavailable
    exact cost never blocks the work it is attached to, per the spec."""

    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit

    def set_estimate(self, ref_type: str, ref_id: str, actor: str = "system", estimated_ai_cost: Optional[float] = None, estimated_api_cost: Optional[float] = None, estimated_workforce_cost: Optional[float] = None, estimated_project_cost: Optional[float] = None) -> str:
        now = utcnow()
        cost_id = self.store.create("co_cost_estimates", {
            "ref_type": ref_type, "ref_id": ref_id,
            "estimated_ai_cost": estimated_ai_cost, "estimated_api_cost": estimated_api_cost,
            "estimated_workforce_cost": estimated_workforce_cost, "estimated_project_cost": estimated_project_cost,
            "actual_ai_cost": None, "actual_api_cost": None, "actual_workforce_cost": None, "actual_project_cost": None,
            "actor": actor, "created_at": now, "updated_at": now,
        })
        self.audit.append("CO_COST_ESTIMATE_SET", {"cost_id": cost_id, "ref_type": ref_type, "ref_id": ref_id, "actor": actor})
        return cost_id

    def record_actual(self, cost_id: str, actor: str, actual_ai_cost: Optional[float] = None, actual_api_cost: Optional[float] = None, actual_workforce_cost: Optional[float] = None, actual_project_cost: Optional[float] = None) -> Dict[str, Any]:
        if not self.store.get("co_cost_estimates", cost_id):
            raise CompanyOSError("cost estimate not found")
        updates = {k: v for k, v in {
            "actual_ai_cost": actual_ai_cost, "actual_api_cost": actual_api_cost,
            "actual_workforce_cost": actual_workforce_cost, "actual_project_cost": actual_project_cost,
        }.items() if v is not None}
        self.store.update("co_cost_estimates", cost_id, **updates)
        self.audit.append("CO_COST_ACTUAL_RECORDED", {"cost_id": cost_id, "actor": actor})
        return self.store.get("co_cost_estimates", cost_id)

    def get_for(self, ref_type: str, ref_id: str) -> Optional[Dict[str, Any]]:
        rows = self.store.list("co_cost_estimates", "ref_type=? AND ref_id=?", (ref_type, ref_id))
        return sorted(rows, key=lambda r: r["created_at"])[-1] if rows else None


# ---------------------------------------------------------------------------
# Section 28: Goal Feedback Loop
# ---------------------------------------------------------------------------

def goal_feedback(store: StateStore, audit: AuditLog, goal_id: str, actor: str = "system") -> Dict[str, Any]:
    """Compares target -> actual -> variance -> likely reason ->
    recommended adjustment for an existing `cc_goals` row. Never rewrites
    the goal itself -- this only ever persists a feedback event and returns
    a recommendation."""
    goal = store.get("cc_goals", goal_id)
    if not goal:
        raise CompanyOSError("goal not found")
    target = goal.get("target")
    actual = goal.get("current_value") or 0.0
    variance = round(actual - target, 4) if target is not None else None

    likely_reason = None
    recommended_adjustment = None
    if target is not None and variance is not None:
        if variance < 0:
            past_deadline = bool(goal.get("deadline") and goal["status"] == "ACTIVE" and goal["deadline"] < utcnow()[:10])
            likely_reason = "past deadline with target unmet" if past_deadline else "on track but not yet at target"
            recommended_adjustment = (
                "Consider extending the deadline or revising the target downward if the shortfall is structural."
                if past_deadline else "No adjustment recommended yet -- still within the goal's timeframe."
            )
        elif variance == 0:
            likely_reason = "exactly at target"
            recommended_adjustment = "Consider raising the target if this was reached comfortably."
        else:
            likely_reason = "ahead of target"
            recommended_adjustment = "Consider raising the target to reflect real capacity."

    event_id = store.create("co_goal_feedback_events", {
        "goal_id": goal_id, "target": target, "actual": actual, "variance": variance,
        "likely_reason": likely_reason, "recommended_adjustment": recommended_adjustment,
        "actor": actor, "created_at": utcnow(),
    })
    audit.append("CO_GOAL_FEEDBACK_RECORDED", {"event_id": event_id, "goal_id": goal_id, "variance": variance, "actor": actor})
    return store.get("co_goal_feedback_events", event_id)


# ---------------------------------------------------------------------------
# Section 20: Company Timeline
# ---------------------------------------------------------------------------

class TimelineStore:
    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit

    def record(self, event_type: str, title: str, actor: str = "system", description: Optional[str] = None, ref_type: Optional[str] = None, ref_id: Optional[str] = None, occurred_at: Optional[str] = None) -> str:
        if not title or not title.strip():
            raise CompanyOSError("title is required")
        event_id = self.store.create("co_timeline_events", {
            "event_type": event_type, "title": title.strip(), "description": description,
            "ref_type": ref_type, "ref_id": ref_id, "occurred_at": occurred_at or utcnow(),
            "actor": actor, "created_at": utcnow(),
        })
        self.audit.append("CO_TIMELINE_EVENT_RECORDED", {"event_id": event_id, "event_type": event_type, "actor": actor})
        return event_id

    def list(self, limit: int = 100) -> List[Dict[str, Any]]:
        rows = sorted(self.store.list("co_timeline_events"), key=lambda r: r["occurred_at"], reverse=True)
        return rows[:limit]


# ---------------------------------------------------------------------------
# Section 14: Daily Operating Loop
# ---------------------------------------------------------------------------

def run_daily_loop(store: StateStore, audit: AuditLog, actor: str = "system") -> Dict[str, Any]:
    """Reads company state, identifies changes since the last loop, updates
    objective health (the one narrow, honest, forward-only auto-transition:
    ACTIVE -> AT_RISK when a real stored deadline has genuinely passed --
    it never auto-resolves an AT_RISK back to ACTIVE, that stays an owner
    call), surfaces blockers, refreshes priorities, and recommends next
    actions / department actions. No uncontrolled external actions -- this
    function never sends anything, never spends anything, never creates a
    Falguna Engineering mission on its own."""
    state = company_state_snapshot(store)
    objectives = ObjectiveStore(store, audit)

    changed_objectives = []
    for o in state["objectives"]["items"]:
        if o["status"] == "ACTIVE" and o.get("past_deadline"):
            objectives.transition(o["id"], "AT_RISK", actor, reason="deadline passed with objective still ACTIVE")
            changed_objectives.append({"objective_id": o["id"], "title": o["title"], "change": "ACTIVE -> AT_RISK (deadline passed)"})

    prior_rows = sorted(store.list("co_daily_loops"), key=lambda r: r["created_at"])
    prior = prior_rows[-1] if prior_rows else None
    prior_snapshot = _json_loads(prior.get("state_snapshot_json"), default={}) if prior else {}
    changes = list(changed_objectives)
    if prior_snapshot:
        prior_needs_aryan = prior_snapshot.get("needs_aryan", {}).get("pending_count")
        if prior_needs_aryan is not None and prior_needs_aryan != state["needs_aryan"]["pending_count"]:
            changes.append({"metric": "needs_aryan_pending_count", "from": prior_needs_aryan, "to": state["needs_aryan"]["pending_count"]})
        prior_objectives_active = prior_snapshot.get("objectives", {}).get("by_status", {}).get("ACTIVE")
        current_objectives_active = state["objectives"]["by_status"].get("ACTIVE")
        if prior_objectives_active is not None and prior_objectives_active != current_objectives_active:
            changes.append({"metric": "objectives_active_count", "from": prior_objectives_active, "to": current_objectives_active})

    blockers = {
        "workforce_blocked_tasks": [t for t in store.list("wf_tasks") if t["status"] in ("BLOCKED", "NEEDS_ARYAN", "FAILED")],
        "open_failures": FailureStore(store, audit).list(status="OPEN"),
        "unsatisfied_department_objectives": [
            {"dept_objective_id": row["id"], "department": row["department"], **DepartmentObjectiveStore(store, audit).dependencies_satisfied(row["id"])}
            for row in store.list("co_department_objectives") if row["status"] in ("PENDING", "ACTIVE") and _json_loads(row.get("dependencies_json"))
        ],
    }
    blockers["unsatisfied_department_objectives"] = [b for b in blockers["unsatisfied_department_objectives"] if not b["satisfied"]]

    priority_store = PriorityStore(store, audit)
    updated_priorities = []
    for o in state["objectives"]["items"]:
        if o["status"] not in {"ACTIVE", "AT_RISK", "BLOCKED"}:
            continue
        inputs = {
            "strategic_importance": "high" if o.get("priority") in ("CRITICAL", "HIGH") else "medium",
            "urgency": "critical" if o.get("past_deadline") else None,
            "risk": "high" if o["status"] in ("AT_RISK", "BLOCKED") else None,
        }
        deadline = o.get("deadline")
        if deadline:
            try:
                days = (datetime.fromisoformat(deadline) - datetime.now(timezone.utc).replace(tzinfo=None)).days
                inputs["deadline_days"] = days
            except (TypeError, ValueError):
                pass
        result = priority_store.evaluate("co_objectives", o["id"], {k: v for k, v in inputs.items() if v is not None}, actor=actor)
        updated_priorities.append({"objective_id": o["id"], "title": o["title"], "priority": result["priority"]})

    resource_recs = ResourceRecommendationStore(store, audit)
    resource_snapshot = resource_allocation_snapshot(store, recommendations=resource_recs, actor=actor)
    capital_snapshot = capital_orchestration_snapshot(store)

    recommended_actions = [f["recommendation"] for f in resource_snapshot["findings"]]
    if state["needs_aryan"]["pending_count"]:
        recommended_actions.append(f"Clear {state['needs_aryan']['pending_count']} pending Needs Aryan item(s) -- they may be blocking downstream work.")
    if blockers["unsatisfied_department_objectives"]:
        recommended_actions.append(f"Resolve {len(blockers['unsatisfied_department_objectives'])} department objective(s) blocked on unmet dependencies.")

    department_actions: Dict[str, List[str]] = {}
    for row in blockers["unsatisfied_department_objectives"]:
        department_actions.setdefault(row["department"], []).append(f"Unblock '{row['dept_objective_id']}': waiting on {len(row['blocking'])} dependency/ies.")
    for finding in resource_snapshot["findings"]:
        if finding.get("department"):
            department_actions.setdefault(finding["department"], []).append(finding["recommendation"])

    loop_id = store.create("co_daily_loops", {
        "run_date": utcnow()[:10], "state_snapshot_json": json.dumps(state, default=str),
        "changes_json": json.dumps(changes), "blockers_json": json.dumps(blockers, default=str),
        "priorities_json": json.dumps(updated_priorities), "recommended_actions_json": json.dumps(recommended_actions),
        "department_actions_json": json.dumps(department_actions), "ceo_brief_ref": None,
        "actor": actor, "created_at": utcnow(),
    })
    audit.append("CO_DAILY_LOOP_RUN", {"loop_id": loop_id, "changed_objectives": len(changed_objectives), "actor": actor})
    return {
        "loop_id": loop_id, "changes": changes, "blockers": blockers, "updated_priorities": updated_priorities,
        "recommended_actions": recommended_actions, "department_actions": department_actions,
        "resource_snapshot": resource_snapshot, "capital_snapshot": capital_snapshot,
    }


def list_daily_loops(store: StateStore, limit: int = 30) -> List[Dict[str, Any]]:
    rows = sorted(store.list("co_daily_loops"), key=lambda r: r["created_at"], reverse=True)[:limit]
    for row in rows:
        row["changes"] = _json_loads(row.get("changes_json"))
        row["recommended_actions"] = _json_loads(row.get("recommended_actions_json"))
    return rows


# ---------------------------------------------------------------------------
# Section 15: Weekly Operating Review
# ---------------------------------------------------------------------------

def run_weekly_review(store: StateStore, audit: AuditLog, actor: str = "system", week_start: Optional[str] = None, week_end: Optional[str] = None) -> Dict[str, Any]:
    """Objective progress, venture performance, sales, delivery, media,
    workforce, finance, risks, resource allocation, recommendations --
    all reused from existing snapshot functions, persisted as one durable
    review row (Section 15: "persist review history")."""
    from .command_center import kpi_snapshot
    from .ventures import VentureStore, venture_scorecard

    now = datetime.now(timezone.utc)
    if week_end is None:
        week_end = now.date().isoformat()
    if week_start is None:
        week_start = (now - timedelta(days=7)).date().isoformat()

    state = company_state_snapshot(store)
    kpis = kpi_snapshot(store, period_days=7)
    ventures = VentureStore(store, None).list(status="ACTIVE")
    venture_performance = [venture_scorecard(store, v) for v in ventures]
    resource_allocation = resource_allocation_snapshot(store)
    capital = capital_orchestration_snapshot(store)

    objective_progress = [
        {"objective_id": o["id"], "title": o["title"], "status": o["status"], "current_progress": o.get("current_progress")}
        for o in state["objectives"]["items"]
    ]

    recommendations = [f["recommendation"] for f in resource_allocation["findings"]]
    if state["risks"]["open_count"]:
        recommendations.append(f"Review {state['risks']['open_count']} open company risk(s).")

    review_id = store.create("co_weekly_reviews", {
        "week_start": week_start, "week_end": week_end,
        "objective_progress_json": json.dumps(objective_progress),
        "venture_performance_json": json.dumps(venture_performance, default=str),
        "sales_json": json.dumps(kpis["sales"]), "delivery_json": json.dumps(kpis["delivery"]),
        "media_json": json.dumps(kpis["media"]), "workforce_json": json.dumps(kpis["workforce"]),
        "finance_json": json.dumps(kpis["finance"]), "risks_json": json.dumps(state["risks"]["items"], default=str),
        "resource_allocation_json": json.dumps(resource_allocation, default=str),
        "recommendations_json": json.dumps(recommendations),
        "actor": actor, "created_at": utcnow(),
    })
    audit.append("CO_WEEKLY_REVIEW_RUN", {"review_id": review_id, "week_start": week_start, "week_end": week_end, "actor": actor})
    return store.get("co_weekly_reviews", review_id)


def list_weekly_reviews(store: StateStore, limit: int = 20) -> List[Dict[str, Any]]:
    rows = sorted(store.list("co_weekly_reviews"), key=lambda r: r["week_start"], reverse=True)[:limit]
    for row in rows:
        row["objective_progress"] = _json_loads(row.get("objective_progress_json"))
        row["recommendations"] = _json_loads(row.get("recommendations_json"))
    return rows


# ---------------------------------------------------------------------------
# Section 19: CEO Command Center V2
# ---------------------------------------------------------------------------

def ceo_command_center_v2_snapshot(store: StateStore) -> Dict[str, Any]:
    """Composes existing, already-computed data (command_center_snapshot,
    company_state_snapshot) into the exact shape Section 19 asks for --
    reshaping, never recomputing anything with new assumptions."""
    from .command_center import command_center_snapshot

    cc = command_center_snapshot(store)
    state = company_state_snapshot(store)
    now = datetime.now(timezone.utc)
    week_from_now = (now + timedelta(days=7)).date().isoformat()
    today = now.date().isoformat()

    top_objectives = sorted(
        [o for o in state["objectives"]["items"] if o["status"] in {"ACTIVE", "AT_RISK", "BLOCKED"}],
        key=lambda o: {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "PARKED": 4}.get(o.get("priority") or "LOW", 5),
    )[:10]

    resource_conflicts = [f for f in resource_allocation_snapshot(store)["findings"] if f["scope"] in {"over_allocation", "conflicting_deadlines", "duplicated_work"}]

    return {
        "generated_at": utcnow(),
        "top_company_objectives": top_objectives,
        "priorities": {o["id"]: o.get("priority") for o in top_objectives},
        "active_ventures": state["ventures"]["items"],
        "major_clients": sorted(store.list("clients"), key=lambda c: c.get("total_won_value") or 0, reverse=True)[:10],
        "revenue_cash": {"revenue": cc["revenue"], "cash": cc["cash"], "receivables": cc["receivables"]},
        "blocked_work": {
            "workforce": [t for t in store.list("wf_tasks") if t["status"] in ("BLOCKED", "NEEDS_ARYAN", "FAILED")],
            "department_objectives": [d for d in store.list("co_department_objectives") if d["status"] == "BLOCKED"],
        },
        "resource_conflicts": resource_conflicts,
        "risks": state["risks"]["items"],
        "decisions_required": DecisionStore(store, None).list(status="PROPOSED"),
        "needs_aryan": state["needs_aryan"]["items"],
        "today": today,
        "next_7_days": {
            "obligations_due": [o for o in cc["upcoming_obligations"] if o.get("due_date") and today <= o["due_date"] <= week_from_now],
        },
        "source": "command_center_snapshot + company_state_snapshot, reshaped -- no new computation",
    }


# ---------------------------------------------------------------------------
# Section 30: Company OS Home
# ---------------------------------------------------------------------------

def company_os_home(store: StateStore) -> Dict[str, Any]:
    """What matters now, what is running, what is blocked, what changed,
    what needs Aryan, what should happen next -- the single operational
    home for Company OS."""
    state = company_state_snapshot(store)
    daily_loops = list_daily_loops(store, limit=1)
    latest_loop = daily_loops[0] if daily_loops else None

    what_matters_now = sorted(
        [o for o in state["objectives"]["items"] if o["status"] in {"ACTIVE", "AT_RISK", "BLOCKED"}],
        key=lambda o: {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "PARKED": 4}.get(o.get("priority") or "LOW", 5),
    )[:5]
    what_is_running = [t for t in store.list("wf_tasks") if t["status"] == "EXECUTING"]
    what_is_blocked = {
        "workforce": [t for t in store.list("wf_tasks") if t["status"] in ("BLOCKED", "NEEDS_ARYAN")],
        "objectives": [o for o in state["objectives"]["items"] if o["status"] == "BLOCKED"],
        "failures": FailureStore(store, None).list(status="OPEN"),
    }
    what_changed = latest_loop["changes"] if latest_loop else []
    what_needs_aryan = state["needs_aryan"]["items"]
    what_should_happen_next = latest_loop["recommended_actions"] if latest_loop else []

    return {
        "generated_at": utcnow(),
        "what_matters_now": what_matters_now,
        "what_is_running": what_is_running,
        "what_is_blocked": what_is_blocked,
        "what_changed": what_changed,
        "what_needs_aryan": what_needs_aryan,
        "what_should_happen_next": what_should_happen_next,
        "last_daily_loop_at": latest_loop["created_at"] if latest_loop else None,
        "source": "company_state_snapshot + the most recent co_daily_loops row",
    }
