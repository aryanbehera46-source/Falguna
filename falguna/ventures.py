"""TTT Venture Studio / Multi-Venture Operating System v1.

The parent-company layer that lets TTT create, operate, fund, measure,
pause, scale, or kill multiple ventures/products/business units while
sharing common TTT/Falguna capabilities (shared departments, Falguna
Engineering, the Finance Ledger, Needs Aryan, and Command Center) instead
of rebuilding a separate company inside each one.

Governance (Section 3): TTT remains the parent. A venture is a persistent
row Aryan controls through this module -- it can operate inside its own
scoped budget/tasks/goals, but venture creation, closure, capital
allocation, policy, and any high-impact decision always route back through
this module's own Needs Aryan escalations, never silently. Nothing here
gives a venture an independent ability to move money, spend beyond its
logged allocation, or act on another venture's resources.

Financial safety (Section 26, strict):
  * No automatic real-money transfers -- nothing in this module calls a
    payment/banking API; capital "allocation" is a logged internal
    accounting event only (`CapitalAllocationStore`), the same posture
    `capital_and_risk.py`'s `recommend_allocation` already takes.
  * No fake venture bank balances -- a venture's capital account
    (`venture_capital_account`) is always derived live from real,
    evidence-backed `cc_ledger_entries` rows (venture_id-tagged) plus real
    `rh_invoices` receipts for its linked opportunities, exactly like
    `finance_ledger.cash_and_runway` already does company-wide. Nothing is
    stored as a standalone balance that could drift from the ledger.
  * No spending beyond a venture's allocated capital without an explicit
    Needs Aryan escalation -- large/cross-venture allocations escalate
    (`CapitalAllocationStore`).
  * No cross-venture capital movement without a logged allocation record on
    both sides (`CapitalAllocationStore.reallocate`).
  * No Trading Lab capital mixing -- this module never reads or writes any
    `tl_*` table. A venture's capital account is computed exclusively from
    `cc_ledger_entries` / `rh_invoices`; Trading Lab paper P&L
    (`tl_paper_accounts` et al, itself already 100% paper/simulated) is
    never included in any venture or company-level capital figure here.

Data isolation (Section 27): every venture-scoped row (capital allocation,
goal, workforce task, Falguna mission link, risk, resource request,
experiment, validation signal, asset, graveyard record, brand) carries an
explicit `venture_id` column/field -- nothing is inferred by name matching.
Shared resources (departments, Falguna Engineering capacity) are explicitly
modelled as shared in `SHARED_DEPARTMENTS`, never duplicated per venture.

Scale/pause/kill (Section 12): `recommend_venture_action` only ever
produces a recommendation, logged to `vs_recommendations`. It never closes,
pauses, or scales a venture itself -- every PAUSE/KILL recommendation (and
every SCALE recommendation with a material budget implication) escalates to
Needs Aryan; routine CONTINUE/ITERATE recommendations do not.
"""

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .audit import AuditLog
from .store import StateStore, utcnow

VENTURE_TYPES = {
    "software_services", "saas", "ai_product", "media_content",
    "internal_platform", "experimental", "investment_research",
}

VENTURE_STATUSES = [
    "IDEA", "VALIDATING", "BUILDING", "PRELAUNCH", "ACTIVE",
    "PAUSED", "SCALING", "SUNSETTING", "CLOSED", "REJECTED",
]

# Explicit, checked graph -- same discipline as workforce.ALLOWED_TASK_TRANSITIONS
# and trading_lab_strategy.ALLOWED_TRANSITIONS. No silent state jumps.
ALLOWED_VENTURE_TRANSITIONS: Dict[str, set] = {
    "IDEA": {"VALIDATING", "REJECTED"},
    "VALIDATING": {"BUILDING", "IDEA", "REJECTED"},
    "BUILDING": {"PRELAUNCH", "PAUSED", "CLOSED"},
    "PRELAUNCH": {"ACTIVE", "PAUSED", "CLOSED"},
    "ACTIVE": {"PAUSED", "SCALING", "SUNSETTING", "CLOSED"},
    "PAUSED": {"ACTIVE", "BUILDING", "SUNSETTING", "CLOSED"},
    "SCALING": {"ACTIVE", "PAUSED", "SUNSETTING", "CLOSED"},
    "SUNSETTING": {"CLOSED", "ACTIVE"},
    "CLOSED": set(),
    "REJECTED": set(),
}
TERMINAL_VENTURE_STATUSES = {"CLOSED", "REJECTED"}
# Pipeline (Section 16) is a read-only grouping over the same status field,
# never a second parallel state machine.
PIPELINE_STAGE_GROUPS = {
    "incoming_ideas": {"IDEA"},
    "under_research": {"VALIDATING"},
    "building": {"BUILDING", "PRELAUNCH"},
    "active": {"ACTIVE", "SCALING"},
    "paused": {"PAUSED", "SUNSETTING"},
    "rejected": {"REJECTED"},
    "closed": {"CLOSED"},
}

# Transitions that always represent a launch/first-activation decision
# (Section 22: "launch approval" must escalate).
_LAUNCH_TRANSITIONS = {("PRELAUNCH", "ACTIVE")}
# Transitions that represent stopping/reducing a venture (Section 22:
# "pause recommendation" -- here, an actual pause/sunset/close action).
_STOP_TRANSITIONS = {
    ("ACTIVE", "PAUSED"), ("SCALING", "PAUSED"), ("BUILDING", "PAUSED"), ("PRELAUNCH", "PAUSED"),
    ("ACTIVE", "SUNSETTING"), ("SCALING", "SUNSETTING"), ("PAUSED", "SUNSETTING"),
}

SHARED_DEPARTMENTS = [
    "Revenue/Sales", "Digital Workforce", "Media/Growth",
    "Falguna Engineering", "Finance", "Research",
]

VALIDATION_SIGNAL_TYPES = {
    "customer_interview", "demand_signal", "lead_interest", "preorder",
    "waitlist_signup", "paid_pilot", "manual_service_proof",
    "traffic_conversion", "competitor_research_evidence",
}
VALIDATION_STRENGTHS = {"weak", "moderate", "strong"}

EXPERIMENT_STATUSES = {"PLANNED", "RUNNING", "COMPLETED", "CANCELLED"}
EXPERIMENT_DECISIONS = {"CONTINUE", "ITERATE", "SCALE", "PAUSE", "KILL"}

ALLOCATION_DIRECTIONS = {"IN", "OUT"}
# A capital commitment at or above this amount is "non-trivial" (Section 22)
# and always escalates, regardless of direction or source.
LARGE_ALLOCATION_THRESHOLD = 50000.0

RESOURCE_DEPARTMENTS = set(SHARED_DEPARTMENTS)
RESOURCE_TYPES = {
    "workforce_task_capacity", "engineering_capacity", "media_slot",
    "sales_attention", "budget",
}
RESOURCE_REQUEST_STATUSES = {"REQUESTED", "ALLOCATED", "CONFLICT", "DENIED"}
# Deliberately small, explicit weekly capacity units per department/resource
# type -- an honest, documented constant, never a fabricated "AI-optimized"
# number. Aryan can revise these; nothing here claims to model real
# staffing/infra capacity precisely.
DEFAULT_WEEKLY_CAPACITY = {
    ("Digital Workforce", "workforce_task_capacity"): 40,
    ("Falguna Engineering", "engineering_capacity"): 20,
    ("Media/Growth", "media_slot"): 10,
    ("Revenue/Sales", "sales_attention"): 15,
}

RECOMMENDATION_VALUES = {"SCALE", "CONTINUE", "ITERATE", "PAUSE", "KILL"}

ASSET_TYPES = {
    "code", "domain", "brand", "logo", "dataset", "research",
    "contract", "content", "product_ip",
}
ASSET_STATUSES = {"ACTIVE", "RETIRED"}
# Metadata-only register (Section 18): reject anything that looks like a
# real secret/credential rather than silently accepting and storing it.
_SECRET_KEY_MARKERS = ("secret", "password", "api_key", "apikey", "token", "credential", "private_key")

RELATIONSHIP_TYPES = {
    "shared_technology", "shared_customers", "shared_distribution",
    "shared_brand_assets", "internal_service",
}

SCORE_BANDS = ["healthy", "watch", "at_risk", "critical"]


class VentureError(ValueError):
    pass


def _slugify(name: str) -> str:
    slug = "".join(c.lower() if c.isalnum() else "-" for c in name.strip())
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-") or "venture"


class VentureStore:
    """Core venture lifecycle (Pass A). Every mutation is durable and every
    status change is recorded to `vs_venture_status_events` with actor,
    reason, evidence, and -- when the transition is high-impact -- a Needs
    Aryan escalation id, exactly the shape Section 11 (orchestrator) asks
    for."""

    def __init__(self, store: StateStore, audit: AuditLog, needs_aryan=None):
        self.store = store
        self.audit = audit
        self.needs_aryan = needs_aryan

    def create(
        self, name: str, venture_type: str, actor: str = "Aryan",
        description: Optional[str] = None, thesis: Optional[str] = None,
        owner: Optional[str] = None, slug: Optional[str] = None,
        linked_product: Optional[str] = None, linked_brand_id: Optional[str] = None,
        linked_departments: Optional[List[str]] = None,
        initial_capital_commitment: Optional[float] = None,
    ) -> Dict[str, Any]:
        if not name or not name.strip():
            raise VentureError("name is required")
        if venture_type not in VENTURE_TYPES:
            raise VentureError(f"venture_type must be one of {sorted(VENTURE_TYPES)}")
        final_slug = _slugify(slug or name)
        if self.store.list("vs_ventures", "slug=?", (final_slug,)):
            raise VentureError(f"a venture with slug {final_slug!r} already exists")
        if linked_brand_id and not self.store.get("media_brands", linked_brand_id):
            raise VentureError("linked_brand_id does not reference an existing brand")
        departments = [d for d in (linked_departments or []) if d in SHARED_DEPARTMENTS]
        now = utcnow()
        venture_id = self.store.create("vs_ventures", {
            "name": name.strip(), "slug": final_slug, "venture_type": venture_type,
            "description": description, "thesis": thesis, "owner": owner,
            "status": "IDEA", "launch_date": None, "parent_company": "Twenty Two Technologies Pvt. Ltd.",
            "linked_product": linked_product, "linked_brand_id": linked_brand_id,
            "linked_departments_json": json.dumps(departments),
            "actor": actor, "created_at": now, "updated_at": now,
        })
        self.store.create("vs_venture_status_events", {
            "venture_id": venture_id, "from_status": None, "to_status": "IDEA",
            "actor": actor, "reason": "venture created", "evidence_json": None,
            "financial_impact": None, "next_action": None, "needs_aryan_id": None, "created_at": now,
        })
        self.audit.append("VS_VENTURE_CREATED", {"venture_id": venture_id, "name": name, "venture_type": venture_type, "actor": actor})

        needs_aryan_id = None
        if self.needs_aryan is not None and initial_capital_commitment and initial_capital_commitment >= LARGE_ALLOCATION_THRESHOLD:
            # Section 22: escalate venture creation when it carries a
            # non-trivial capital commitment. This never blocks creation --
            # the venture exists either way -- it only ensures Aryan sees it.
            needs_aryan_id = self.needs_aryan.create_item(
                "venture_creation_approval",
                f"New venture created with a non-trivial capital commitment: {name.strip()}",
                (f"A new venture ({venture_type}) was created with an initial capital commitment of "
                 f"{initial_capital_commitment}. Review the thesis and either approve the commitment via a "
                 "capital allocation, or revise/reject it."),
                actor=actor, rationale=thesis, risk="non-trivial capital commitment on a new, unvalidated venture",
                ref_type="vs_venture", ref_id=venture_id,
            )
        return {"venture_id": venture_id, "slug": final_slug, "needs_aryan_id": needs_aryan_id}

    def update_details(self, venture_id: str, actor: str, **fields: Any) -> Dict[str, Any]:
        self._require(venture_id)
        allowed = {"description", "thesis", "owner", "linked_product", "linked_brand_id", "linked_departments"}
        unknown = set(fields) - allowed
        if unknown:
            raise VentureError(f"unknown fields: {sorted(unknown)}")
        updates: Dict[str, Any] = {}
        if "linked_departments" in fields:
            updates["linked_departments_json"] = json.dumps([d for d in (fields.pop("linked_departments") or []) if d in SHARED_DEPARTMENTS])
        if "linked_brand_id" in fields and fields["linked_brand_id"] and not self.store.get("media_brands", fields["linked_brand_id"]):
            raise VentureError("linked_brand_id does not reference an existing brand")
        updates.update(fields)
        if updates:
            self.store.update("vs_ventures", venture_id, **updates)
            self.audit.append("VS_VENTURE_UPDATED", {"venture_id": venture_id, "fields": sorted(updates), "actor": actor})
        return self.get(venture_id)

    def transition(
        self, venture_id: str, to_status: str, actor: str, reason: Optional[str] = None,
        evidence: Optional[Dict[str, Any]] = None, financial_impact: Optional[str] = None,
        next_action: Optional[str] = None, approval_ref: Optional[str] = None,
    ) -> Dict[str, Any]:
        venture = self._require(venture_id)
        if to_status not in VENTURE_STATUSES:
            raise VentureError(f"unknown status: {to_status!r}")
        current = venture["status"]
        if current == to_status:
            return self.get(venture_id)
        if to_status not in ALLOWED_VENTURE_TRANSITIONS.get(current, set()):
            raise VentureError(f"cannot transition a venture from {current} to {to_status}")

        needs_aryan_id = None
        edge = (current, to_status)
        if self.needs_aryan is not None:
            if edge in _LAUNCH_TRANSITIONS:
                needs_aryan_id = self.needs_aryan.create_item(
                    "venture_launch_approval", f"Venture launch approval requested: {venture['name']}",
                    f"{venture['name']} is ready to move from PRELAUNCH to ACTIVE. Approve the launch.",
                    actor=actor, rationale=reason, risk=financial_impact, ref_type="vs_venture", ref_id=venture_id,
                )
            elif edge in _STOP_TRANSITIONS:
                needs_aryan_id = self.needs_aryan.create_item(
                    "venture_pause_recommendation", f"Venture pause/sunset requested: {venture['name']}",
                    f"{venture['name']} is being moved from {current} to {to_status}. Reason: {reason or '(none given)'}.",
                    actor=actor, rationale=reason, risk=financial_impact, ref_type="vs_venture", ref_id=venture_id,
                )
            elif to_status == "CLOSED":
                needs_aryan_id = self.needs_aryan.create_item(
                    "venture_kill_recommendation", f"Venture close requested: {venture['name']}",
                    f"{venture['name']} is being closed from {current}. Reason: {reason or '(none given)'}.",
                    actor=actor, rationale=reason, risk=financial_impact, ref_type="vs_venture", ref_id=venture_id,
                )

        updates = {"status": to_status}
        if to_status == "ACTIVE" and not venture.get("launch_date"):
            updates["launch_date"] = utcnow()
        self.store.update("vs_ventures", venture_id, **updates)
        self.store.create("vs_venture_status_events", {
            "venture_id": venture_id, "from_status": current, "to_status": to_status, "actor": actor,
            "reason": reason, "evidence_json": json.dumps(evidence) if evidence is not None else None,
            "financial_impact": financial_impact, "next_action": next_action,
            "needs_aryan_id": needs_aryan_id, "created_at": utcnow(),
        })
        self.audit.append("VS_VENTURE_TRANSITIONED", {"venture_id": venture_id, "from": current, "to": to_status, "actor": actor, "needs_aryan_id": needs_aryan_id})
        return self.get(venture_id)

    def status_events(self, venture_id: str) -> List[Dict[str, Any]]:
        return self.store.list("vs_venture_status_events", "venture_id=?", (venture_id,))

    def _require(self, venture_id: str) -> Dict[str, Any]:
        venture = self.store.get("vs_ventures", venture_id)
        if not venture:
            raise VentureError("venture not found")
        return venture

    def get(self, venture_id: str) -> Optional[Dict[str, Any]]:
        venture = self.store.get("vs_ventures", venture_id)
        if not venture:
            return None
        return self._with_computed_fields(venture)

    def get_by_slug(self, slug: str) -> Optional[Dict[str, Any]]:
        rows = self.store.list("vs_ventures", "slug=?", (slug,))
        return self._with_computed_fields(rows[0]) if rows else None

    def list(self, status: Optional[str] = None, venture_type: Optional[str] = None) -> List[Dict[str, Any]]:
        if status and venture_type:
            rows = self.store.list("vs_ventures", "status=? AND venture_type=?", (status, venture_type))
        elif status:
            rows = self.store.list("vs_ventures", "status=?", (status,))
        elif venture_type:
            rows = self.store.list("vs_ventures", "venture_type=?", (venture_type,))
        else:
            rows = self.store.list("vs_ventures")
        return [self._with_computed_fields(v) for v in reversed(rows)]

    def pipeline(self) -> Dict[str, Any]:
        """Venture pipeline (Section 16): incoming ideas -> research ->
        validation -> approved -> building -> active -> paused -> rejected.
        Rejected ideas are preserved (never deleted), always with a reason
        on their status_events history."""
        all_ventures = self.list()
        groups: Dict[str, List[Dict[str, Any]]] = {name: [] for name in PIPELINE_STAGE_GROUPS}
        for venture in all_ventures:
            for group_name, statuses in PIPELINE_STAGE_GROUPS.items():
                if venture["status"] in statuses:
                    groups[group_name].append(venture)
                    break
        return groups

    def _with_computed_fields(self, venture: Dict[str, Any]) -> Dict[str, Any]:
        venture = dict(venture)
        venture["linked_departments"] = json.loads(venture["linked_departments_json"]) if venture.get("linked_departments_json") else []
        venture["is_terminal"] = venture["status"] in TERMINAL_VENTURE_STATUSES
        return venture


class ValidationStore:
    """Validation engine (Section 8). Evidence is recorded, never
    fabricated; `summary` describes what evidence exists in plain,
    descriptive terms (a signal count and type breakdown), and deliberately
    never turns that into a synthetic score or a pass/fail gate -- "do not
    require all signals; do not fabricate validation" is Section 8's own
    instruction, so a venture with zero signals can still be moved forward
    by Aryan; the summary is read-only context, not a blocker."""

    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit

    def add_signal(
        self, venture_id: str, signal_type: str, description: str, actor: str = "Aryan",
        evidence: Optional[str] = None, strength: str = "moderate",
    ) -> str:
        if not self.store.get("vs_ventures", venture_id):
            raise VentureError("venture not found")
        if signal_type not in VALIDATION_SIGNAL_TYPES:
            raise VentureError(f"signal_type must be one of {sorted(VALIDATION_SIGNAL_TYPES)}")
        if strength not in VALIDATION_STRENGTHS:
            raise VentureError(f"strength must be one of {sorted(VALIDATION_STRENGTHS)}")
        if not description or not description.strip():
            raise VentureError("description is required")
        signal_id = self.store.create("vs_validation_signals", {
            "venture_id": venture_id, "signal_type": signal_type, "description": description.strip(),
            "evidence": evidence, "strength": strength, "actor": actor, "created_at": utcnow(),
        })
        self.audit.append("VS_VALIDATION_SIGNAL_ADDED", {"venture_id": venture_id, "signal_id": signal_id, "signal_type": signal_type, "actor": actor})
        return signal_id

    def list(self, venture_id: str) -> List[Dict[str, Any]]:
        return list(reversed(self.store.list("vs_validation_signals", "venture_id=?", (venture_id,))))

    def summary(self, venture_id: str) -> Dict[str, Any]:
        signals = self.list(venture_id)
        by_type: Dict[str, int] = {}
        for s in signals:
            by_type[s["signal_type"]] = by_type.get(s["signal_type"], 0) + 1
        strong_count = sum(1 for s in signals if s["strength"] == "strong")
        return {
            "venture_id": venture_id, "signal_count": len(signals), "strong_signal_count": strong_count,
            "distinct_signal_types": len(by_type), "by_type": by_type,
            "note": "Descriptive only -- a raw count of recorded evidence, never a synthetic validation score.",
            "source": "vs_validation_signals",
        }


class ExperimentStore:
    """Venture experiments (Section 7): hypothesis, metric, target, owner,
    budget, window, evidence, result, decision. `decision` is always a
    human call recorded here, never computed automatically."""

    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit

    def create(
        self, venture_id: str, hypothesis: str, metric: str, target: Any, actor: str = "Aryan",
        owner: Optional[str] = None, budget: Optional[float] = None,
        start_date: Optional[str] = None, end_date: Optional[str] = None,
    ) -> str:
        if not self.store.get("vs_ventures", venture_id):
            raise VentureError("venture not found")
        if not hypothesis or not hypothesis.strip():
            raise VentureError("hypothesis is required")
        if not metric or not metric.strip():
            raise VentureError("metric is required")
        now = utcnow()
        experiment_id = self.store.create("vs_experiments", {
            "venture_id": venture_id, "hypothesis": hypothesis.strip(), "metric": metric.strip(),
            "target": json.dumps(target) if not isinstance(target, str) else target,
            "owner": owner, "budget": budget, "start_date": start_date, "end_date": end_date,
            "evidence_json": None, "result": None, "decision": None, "status": "PLANNED",
            "actor": actor, "created_at": now, "updated_at": now,
        })
        self.audit.append("VS_EXPERIMENT_CREATED", {"venture_id": venture_id, "experiment_id": experiment_id, "actor": actor})
        return experiment_id

    def start(self, experiment_id: str, actor: str) -> Dict[str, Any]:
        exp = self._require(experiment_id)
        if exp["status"] != "PLANNED":
            raise VentureError(f"experiment must be PLANNED to start (currently {exp['status']})")
        self.store.update("vs_experiments", experiment_id, status="RUNNING")
        self.audit.append("VS_EXPERIMENT_STARTED", {"experiment_id": experiment_id, "actor": actor})
        return self.get(experiment_id)

    def record_result(
        self, experiment_id: str, actor: str, evidence: Any, result: str, decision: str,
    ) -> Dict[str, Any]:
        exp = self._require(experiment_id)
        if decision not in EXPERIMENT_DECISIONS:
            raise VentureError(f"decision must be one of {sorted(EXPERIMENT_DECISIONS)}")
        if not evidence:
            raise VentureError("evidence is required to record an experiment result")
        if not result or not str(result).strip():
            raise VentureError("result is required")
        self.store.update(
            "vs_experiments", experiment_id, status="COMPLETED",
            evidence_json=json.dumps(evidence) if not isinstance(evidence, str) else evidence,
            result=str(result).strip(), decision=decision,
        )
        self.audit.append("VS_EXPERIMENT_RESULT_RECORDED", {"experiment_id": experiment_id, "decision": decision, "actor": actor})
        return self.get(experiment_id)

    def _require(self, experiment_id: str) -> Dict[str, Any]:
        exp = self.store.get("vs_experiments", experiment_id)
        if not exp:
            raise VentureError("experiment not found")
        return exp

    def get(self, experiment_id: str) -> Optional[Dict[str, Any]]:
        return self.store.get("vs_experiments", experiment_id)

    def list(self, venture_id: str, status: Optional[str] = None) -> List[Dict[str, Any]]:
        if status:
            rows = self.store.list("vs_experiments", "venture_id=? AND status=?", (venture_id, status))
        else:
            rows = self.store.list("vs_experiments", "venture_id=?", (venture_id,))
        return list(reversed(rows))


class CapitalAllocationStore:
    """Logged internal capital allocation (Sections 4, 26). Never a bank
    account, never a real money movement -- purely an accounting record
    that makes every unit of capital a venture is said to have traceable to
    an explicit, auditable entry, exactly like `RiskRegisterStore` makes
    every high-severity risk traceable to an escalation. A cross-venture
    reallocation always writes two linked rows (OUT of the source, IN to
    the destination) inside one transaction-shaped call, so capital can
    never appear to move without a paired record on both sides."""

    def __init__(self, store: StateStore, audit: AuditLog, needs_aryan=None):
        self.store = store
        self.audit = audit
        self.needs_aryan = needs_aryan

    def allocate(
        self, venture_id: str, amount: float, actor: str = "Aryan",
        source_note: Optional[str] = None, related_venture_id: Optional[str] = None,
        direction: str = "IN",
    ) -> Dict[str, Any]:
        venture = self.store.get("vs_ventures", venture_id)
        if not venture:
            raise VentureError("venture not found")
        if direction not in ALLOCATION_DIRECTIONS:
            raise VentureError(f"direction must be one of {sorted(ALLOCATION_DIRECTIONS)}")
        if amount is None or amount <= 0:
            raise VentureError("amount must be a positive number")
        if not source_note or not source_note.strip():
            raise VentureError("source_note is required -- every allocation must say where the capital came from or where it is going")

        needs_aryan_id = None
        is_large = amount >= LARGE_ALLOCATION_THRESHOLD
        is_cross_venture = related_venture_id is not None
        if self.needs_aryan is not None and (is_large or is_cross_venture):
            needs_aryan_id = self.needs_aryan.create_item(
                "venture_budget_allocation",
                f"Capital allocation ({direction}) of {amount} for {venture['name']}",
                (f"{'A cross-venture reallocation' if is_cross_venture else 'A major budget allocation'} "
                 f"of {amount} ({direction}) is requested for {venture['name']}. Source/reason: {source_note.strip()}. "
                 "Approve, revise, or reject."),
                actor=actor, rationale=source_note, risk="non-trivial capital movement",
                ref_type="vs_venture", ref_id=venture_id,
            )
        now = utcnow()
        allocation_id = self.store.create("vs_capital_allocations", {
            "venture_id": venture_id, "direction": direction, "amount": amount,
            "source_note": source_note.strip(), "related_venture_id": related_venture_id,
            "needs_aryan_id": needs_aryan_id, "actor": actor, "created_at": now,
        })
        self.audit.append("VS_CAPITAL_ALLOCATED", {"venture_id": venture_id, "allocation_id": allocation_id, "direction": direction, "amount": amount, "actor": actor, "needs_aryan_id": needs_aryan_id})
        return {"allocation_id": allocation_id, "needs_aryan_id": needs_aryan_id}

    def reallocate(self, from_venture_id: str, to_venture_id: str, amount: float, actor: str, reason: str) -> Dict[str, Any]:
        if from_venture_id == to_venture_id:
            raise VentureError("cannot reallocate a venture's capital to itself")
        out_result = self.allocate(from_venture_id, amount, actor, source_note=reason, related_venture_id=to_venture_id, direction="OUT")
        in_result = self.allocate(to_venture_id, amount, actor, source_note=reason, related_venture_id=from_venture_id, direction="IN")
        return {"out_allocation_id": out_result["allocation_id"], "in_allocation_id": in_result["allocation_id"], "needs_aryan_id": out_result["needs_aryan_id"] or in_result["needs_aryan_id"]}

    def list(self, venture_id: str) -> List[Dict[str, Any]]:
        return list(reversed(self.store.list("vs_capital_allocations", "venture_id=?", (venture_id,))))

    def net_allocated(self, venture_id: str) -> float:
        rows = self.list(venture_id)
        net = sum(r["amount"] for r in rows if r["direction"] == "IN") - sum(r["amount"] for r in rows if r["direction"] == "OUT")
        return round(net, 2)


def venture_capital_account(store: StateStore, venture: Dict[str, Any]) -> Dict[str, Any]:
    """Venture capital account (Section 4/5). Every figure is labeled
    actual/allocated/estimated, mirroring `finance_ledger.cash_and_runway`'s
    own discipline. Reads only `cc_ledger_entries` (venture_id-tagged),
    `rh_invoices` (via venture-linked `rh_opportunities`), and
    `vs_capital_allocations` -- never any `tl_*` (Trading Lab) table, so
    Trading Lab paper capital can never mix into a venture's figures."""
    venture_id = venture["id"]
    ledger_entries = store.list("cc_ledger_entries", "venture_id=? AND status=?", (venture_id, "RECORDED"))
    ledger_revenue = round(sum(e["amount"] for e in ledger_entries if e["entry_type"] == "INFLOW"), 2)
    direct_costs = round(sum(e["amount"] for e in ledger_entries if e["entry_type"] == "OUTFLOW"), 2)
    cost_by_category: Dict[str, float] = {}
    for e in ledger_entries:
        if e["entry_type"] != "OUTFLOW":
            continue
        cost_by_category[e["category"]] = round(cost_by_category.get(e["category"], 0.0) + e["amount"], 2)

    opportunities = store.list("rh_opportunities", "venture_id=?", (venture_id,))
    opportunity_ids = {o["id"] for o in opportunities}
    invoices = [inv for inv in store.list("rh_invoices") if inv.get("opportunity_id") in opportunity_ids]
    invoice_revenue = round(sum((inv["amount_received"] or 0.0) for inv in invoices), 2)
    outstanding_invoices = [inv for inv in invoices if inv["status"] not in ("PAID", "CANCELLED")]
    receivables_outstanding = round(sum((inv["amount"] or 0.0) - (inv["amount_received"] or 0.0) for inv in outstanding_invoices), 2)

    running_experiments = store.list("vs_experiments", "venture_id=? AND status=?", (venture_id, "RUNNING"))
    committed_spend = round(sum((e.get("budget") or 0.0) for e in running_experiments), 2)

    net_allocated = _net_allocated(store, venture_id)

    total_revenue = round(ledger_revenue + invoice_revenue, 2)
    estimated_gross_profit = round(total_revenue - direct_costs, 2)

    return {
        "venture_id": venture_id, "generated_at": utcnow(),
        "actual": {
            "assigned_budget_net_allocated": net_allocated,
            "spend_to_date": direct_costs, "spend_by_category": cost_by_category,
            "revenue_ledger": ledger_revenue, "revenue_invoices_received": invoice_revenue,
            "revenue_total": total_revenue,
            "source": "cc_ledger_entries (venture_id-tagged, RECORDED) + rh_invoices (via venture-linked rh_opportunities)",
        },
        "expected": {
            "receivables_outstanding": receivables_outstanding,
            "source": "rh_invoices (not yet actual cash until a payment is recorded)",
        },
        "estimated": {
            "committed_spend_running_experiments": committed_spend,
            "estimated_gross_profit": estimated_gross_profit,
            "note": "committed_spend is experiment budget earmarked for RUNNING experiments -- not yet necessarily recorded as ledger spend.",
        },
        "cash_allocation": net_allocated,
        "note": "No bank account is invented here -- assigned_budget_net_allocated is a logged internal accounting figure (vs_capital_allocations), never a real balance. No Trading Lab (tl_*) data is included in any figure on this page.",
    }


def _net_allocated(store: StateStore, venture_id: str) -> float:
    rows = store.list("vs_capital_allocations", "venture_id=?", (venture_id,))
    return round(sum(r["amount"] for r in rows if r["direction"] == "IN") - sum(r["amount"] for r in rows if r["direction"] == "OUT"), 2)


class ResourceAllocationStore:
    """Shared-capability resource allocator (Sections 9, 10). Ventures
    request capacity from shared departments rather than owning duplicate
    department systems. `allocate` applies a small, explicit weekly
    capacity constant per (department, resource_type) -- never a fabricated
    "optimal" split -- and surfaces a CONFLICT (with the competing venture
    requests named) rather than silently over-allocating when two ventures'
    requests would exceed it."""

    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit

    def request(
        self, venture_id: str, department: str, resource_type: str, amount_or_qty: float,
        actor: str = "Aryan", note: Optional[str] = None,
    ) -> str:
        if not self.store.get("vs_ventures", venture_id):
            raise VentureError("venture not found")
        if department not in RESOURCE_DEPARTMENTS:
            raise VentureError(f"department must be one of {sorted(RESOURCE_DEPARTMENTS)}")
        if resource_type not in RESOURCE_TYPES:
            raise VentureError(f"resource_type must be one of {sorted(RESOURCE_TYPES)}")
        if amount_or_qty is None or amount_or_qty <= 0:
            raise VentureError("amount_or_qty must be a positive number")
        now = utcnow()
        request_id = self.store.create("vs_resource_requests", {
            "venture_id": venture_id, "department": department, "resource_type": resource_type,
            "amount_or_qty": amount_or_qty, "status": "REQUESTED", "conflict_with_json": None,
            "actor": actor, "note": note, "created_at": now, "updated_at": now,
        })
        self.audit.append("VS_RESOURCE_REQUESTED", {"venture_id": venture_id, "request_id": request_id, "department": department, "resource_type": resource_type, "actor": actor})
        return request_id

    def allocate(self, request_id: str, actor: str) -> Dict[str, Any]:
        req = self._require(request_id)
        if req["status"] != "REQUESTED":
            raise VentureError(f"request must be REQUESTED to allocate (currently {req['status']})")
        capacity = DEFAULT_WEEKLY_CAPACITY.get((req["department"], req["resource_type"]))
        if capacity is not None:
            already_allocated = self.store.list("vs_resource_requests", "department=? AND resource_type=? AND status=?", (req["department"], req["resource_type"], "ALLOCATED"))
            used = sum(r["amount_or_qty"] for r in already_allocated)
            if used + req["amount_or_qty"] > capacity:
                competing = [{"request_id": r["id"], "venture_id": r["venture_id"], "amount_or_qty": r["amount_or_qty"]} for r in already_allocated]
                self.store.update("vs_resource_requests", request_id, status="CONFLICT", conflict_with_json=json.dumps(competing))
                self.audit.append("VS_RESOURCE_CONFLICT", {"request_id": request_id, "department": req["department"], "resource_type": req["resource_type"], "actor": actor})
                return self.get(request_id)
        self.store.update("vs_resource_requests", request_id, status="ALLOCATED")
        self.audit.append("VS_RESOURCE_ALLOCATED", {"request_id": request_id, "actor": actor})
        return self.get(request_id)

    def deny(self, request_id: str, actor: str, reason: Optional[str] = None) -> Dict[str, Any]:
        self._require(request_id)
        self.store.update("vs_resource_requests", request_id, status="DENIED")
        self.audit.append("VS_RESOURCE_DENIED", {"request_id": request_id, "actor": actor, "reason": reason})
        return self.get(request_id)

    def _require(self, request_id: str) -> Dict[str, Any]:
        req = self.store.get("vs_resource_requests", request_id)
        if not req:
            raise VentureError("resource request not found")
        return req

    def get(self, request_id: str) -> Optional[Dict[str, Any]]:
        return self.store.get("vs_resource_requests", request_id)

    def list(self, venture_id: Optional[str] = None, department: Optional[str] = None) -> List[Dict[str, Any]]:
        if venture_id and department:
            rows = self.store.list("vs_resource_requests", "venture_id=? AND department=?", (venture_id, department))
        elif venture_id:
            rows = self.store.list("vs_resource_requests", "venture_id=?", (venture_id,))
        elif department:
            rows = self.store.list("vs_resource_requests", "department=?", (department,))
        else:
            rows = self.store.list("vs_resource_requests")
        return list(reversed(rows))


class AssetRegisterStore:
    """IP/asset register (Section 18). Metadata only -- V1 hard-refuses any
    metadata key that looks like a real secret/credential rather than
    silently storing it."""

    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit

    def register(
        self, venture_id: str, asset_type: str, name: str, actor: str = "Aryan",
        description: Optional[str] = None, location_or_ref: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        if not self.store.get("vs_ventures", venture_id):
            raise VentureError("venture not found")
        if asset_type not in ASSET_TYPES:
            raise VentureError(f"asset_type must be one of {sorted(ASSET_TYPES)}")
        if not name or not name.strip():
            raise VentureError("name is required")
        for key in (metadata or {}):
            lowered = key.lower()
            if any(marker in lowered for marker in _SECRET_KEY_MARKERS):
                raise VentureError(
                    f"metadata key {key!r} looks like a secret/credential -- the asset register is metadata-only "
                    "(V1) and never stores secrets; keep credentials in a real secrets manager"
                )
        now = utcnow()
        asset_id = self.store.create("vs_assets", {
            "venture_id": venture_id, "asset_type": asset_type, "name": name.strip(),
            "description": description, "location_or_ref": location_or_ref,
            "metadata_json": json.dumps(metadata) if metadata else None,
            "status": "ACTIVE", "actor": actor, "created_at": now, "updated_at": now,
        })
        self.audit.append("VS_ASSET_REGISTERED", {"venture_id": venture_id, "asset_id": asset_id, "asset_type": asset_type, "actor": actor})
        return asset_id

    def retire(self, asset_id: str, actor: str) -> Dict[str, Any]:
        asset = self.store.get("vs_assets", asset_id)
        if not asset:
            raise VentureError("asset not found")
        self.store.update("vs_assets", asset_id, status="RETIRED")
        self.audit.append("VS_ASSET_RETIRED", {"asset_id": asset_id, "actor": actor})
        return self.store.get("vs_assets", asset_id)

    def list(self, venture_id: str) -> List[Dict[str, Any]]:
        return list(reversed(self.store.list("vs_assets", "venture_id=?", (venture_id,))))


class RelationshipStore:
    """Inter-venture relationships (Section 19) -- avoid duplicate work
    where one venture can reuse another's asset/technology/distribution."""

    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit

    def link(self, venture_a_id: str, venture_b_id: str, relationship_type: str, actor: str = "Aryan", description: Optional[str] = None) -> str:
        if venture_a_id == venture_b_id:
            raise VentureError("a venture cannot have a relationship with itself")
        if not self.store.get("vs_ventures", venture_a_id) or not self.store.get("vs_ventures", venture_b_id):
            raise VentureError("both ventures must exist")
        if relationship_type not in RELATIONSHIP_TYPES:
            raise VentureError(f"relationship_type must be one of {sorted(RELATIONSHIP_TYPES)}")
        relationship_id = self.store.create("vs_relationships", {
            "venture_a_id": venture_a_id, "venture_b_id": venture_b_id, "relationship_type": relationship_type,
            "description": description, "actor": actor, "created_at": utcnow(),
        })
        self.audit.append("VS_RELATIONSHIP_CREATED", {"venture_a_id": venture_a_id, "venture_b_id": venture_b_id, "relationship_type": relationship_type, "actor": actor})
        return relationship_id

    def list_for_venture(self, venture_id: str) -> List[Dict[str, Any]]:
        a_side = self.store.list("vs_relationships", "venture_a_id=?", (venture_id,))
        b_side = self.store.list("vs_relationships", "venture_b_id=?", (venture_id,))
        return list(reversed(a_side + b_side))


class GraveyardStore:
    """Venture graveyard (Section 17). Closing a venture always transitions
    it to CLOSED through `VentureStore.transition` first (so the normal
    Needs Aryan escalation for a close still fires), then writes one
    graveyard record capturing thesis, total invested, experiments,
    evidence, reason, lessons, and assets produced -- preserved permanently,
    never deleted, so the same failed thesis is never silently retried."""

    def __init__(self, store: StateStore, audit: AuditLog, venture_store: VentureStore):
        self.store = store
        self.audit = audit
        self.venture_store = venture_store

    def close_venture(self, venture_id: str, reason_killed: str, actor: str, lessons: Optional[str] = None) -> Dict[str, Any]:
        venture = self.store.get("vs_ventures", venture_id)
        if not venture:
            raise VentureError("venture not found")
        if not reason_killed or not reason_killed.strip():
            raise VentureError("reason_killed is required")

        if venture["status"] != "CLOSED":
            self.venture_store.transition(venture_id, "CLOSED", actor, reason=reason_killed)

        account = venture_capital_account(self.store, venture)
        experiments = self.store.list("vs_experiments", "venture_id=?", (venture_id,))
        signals = self.store.list("vs_validation_signals", "venture_id=?", (venture_id,))
        assets = self.store.list("vs_assets", "venture_id=?", (venture_id,))

        now = utcnow()
        grave_id = self.store.create("vs_graveyard", {
            "venture_id": venture_id, "original_thesis": venture.get("thesis"),
            "total_invested": account["actual"]["assigned_budget_net_allocated"],
            "experiments_summary_json": json.dumps([{"hypothesis": e["hypothesis"], "decision": e.get("decision")} for e in experiments]),
            "evidence_summary_json": json.dumps([{"signal_type": s["signal_type"], "strength": s["strength"]} for s in signals]),
            "reason_killed": reason_killed.strip(), "lessons": lessons,
            "assets_produced_json": json.dumps([{"asset_type": a["asset_type"], "name": a["name"]} for a in assets]),
            "actor": actor, "killed_at": now, "created_at": now,
        })
        self.audit.append("VS_VENTURE_GRAVEYARDED", {"venture_id": venture_id, "grave_id": grave_id, "actor": actor})
        return self.store.get("vs_graveyard", grave_id)

    def list(self) -> List[Dict[str, Any]]:
        return list(reversed(self.store.list("vs_graveyard")))

    def check_similar_thesis(self, thesis: str) -> List[Dict[str, Any]]:
        """Naive keyword-overlap check (Section 17) against past graveyard
        theses -- mirrors `trading_lab_council.check_graveyard_for_similar`.
        Advisory only, never blocking."""
        if not thesis or not thesis.strip():
            return []
        words = {w for w in thesis.lower().split() if len(w) > 4}
        hits = []
        for grave in self.list():
            past = (grave.get("original_thesis") or "").lower()
            past_words = {w for w in past.split() if len(w) > 4}
            overlap = words & past_words
            if len(overlap) >= 3:
                hits.append({"grave_id": grave["id"], "venture_id": grave["venture_id"], "reason_killed": grave["reason_killed"], "overlap_words": sorted(overlap)})
        return hits


def venture_scorecard(store: StateStore, venture: Dict[str, Any]) -> Dict[str, Any]:
    """Venture scorecard (Section 13): descriptive bands
    (healthy/watch/at_risk/critical), never a fabricated 0-100 score. Every
    band is derived from a plain, stated rule over real counts."""
    venture_id = venture["id"]

    tasks = store.list("wf_tasks", "venture_id=?", (venture_id,))
    attention_tasks = [t for t in tasks if t["status"] in ("BLOCKED", "NEEDS_ARYAN", "FAILED")]
    if not tasks:
        execution_band = "watch"
        execution_reason = "no workforce tasks recorded yet"
    else:
        attention_ratio = len(attention_tasks) / len(tasks)
        if attention_ratio == 0:
            execution_band, execution_reason = "healthy", "no workforce tasks currently blocked/failed/needing a decision"
        elif attention_ratio < 0.34:
            execution_band, execution_reason = "watch", f"{len(attention_tasks)}/{len(tasks)} tasks need attention"
        elif attention_ratio < 0.67:
            execution_band, execution_reason = "at_risk", f"{len(attention_tasks)}/{len(tasks)} tasks need attention"
        else:
            execution_band, execution_reason = "critical", f"{len(attention_tasks)}/{len(tasks)} tasks need attention"

    account = venture_capital_account(store, venture)
    gross_profit = account["estimated"]["estimated_gross_profit"]
    spend = account["actual"]["spend_to_date"]
    if spend == 0 and account["actual"]["revenue_total"] == 0:
        financial_band, financial_reason = "watch", "no recorded spend or revenue yet"
    elif gross_profit >= 0:
        financial_band, financial_reason = "healthy", "estimated gross profit is non-negative"
    elif spend > 0 and account["actual"]["assigned_budget_net_allocated"] > 0 and spend >= account["actual"]["assigned_budget_net_allocated"]:
        financial_band, financial_reason = "critical", "spend to date has reached or exceeded net allocated capital"
    else:
        financial_band, financial_reason = "at_risk", "estimated gross profit is negative"

    signals = store.list("vs_validation_signals", "venture_id=?", (venture_id,))
    strong = sum(1 for s in signals if s["strength"] == "strong")
    if venture["status"] in ("ACTIVE", "SCALING"):
        traction_band = "healthy" if strong >= 2 else ("watch" if signals else "at_risk")
        traction_reason = f"{len(signals)} validation signal(s) recorded, {strong} strong"
    else:
        traction_band = "healthy" if signals else "watch"
        traction_reason = f"{len(signals)} validation signal(s) recorded (pre-launch stage)"

    open_risks = [r for r in store.list("cc_risks", "venture_id=?", (venture_id,)) if r["status"] in ("OPEN", "MITIGATING")]
    critical_risks = [r for r in open_risks if r["severity"] == "critical"]
    if critical_risks:
        risk_band, risk_reason = "critical", f"{len(critical_risks)} open critical risk(s)"
    elif any(r["severity"] == "high" for r in open_risks):
        risk_band, risk_reason = "at_risk", "open high-severity risk(s)"
    elif open_risks:
        risk_band, risk_reason = "watch", f"{len(open_risks)} open low/medium risk(s)"
    else:
        risk_band, risk_reason = "healthy", "no open risks logged"

    goals = store.list("cc_goals", "venture_id=?", (venture_id,))
    achieved = sum(1 for g in goals if g["status"] == "ACHIEVED")
    at_risk_goals = sum(1 for g in goals if g.get("deadline") and g["status"] == "ACTIVE" and g["deadline"] < utcnow()[:10])
    if not goals:
        milestone_band, milestone_reason = "watch", "no goals set for this venture yet"
    elif at_risk_goals:
        milestone_band, milestone_reason = "at_risk", f"{at_risk_goals} goal(s) past deadline, not yet achieved"
    else:
        milestone_band, milestone_reason = "healthy", f"{achieved}/{len(goals)} goal(s) achieved, none overdue"

    return {
        "venture_id": venture_id, "generated_at": utcnow(),
        "execution": {"band": execution_band, "reason": execution_reason, "source": "wf_tasks.status"},
        "financials": {"band": financial_band, "reason": financial_reason, "source": "venture_capital_account"},
        "traction": {"band": traction_band, "reason": traction_reason, "source": "vs_validation_signals"},
        "risks": {"band": risk_band, "reason": risk_reason, "source": "cc_risks.venture_id"},
        "milestones": {"band": milestone_band, "reason": milestone_reason, "source": "cc_goals.venture_id"},
        "note": "Every band is a plain rule over real counts -- never a fabricated 0-100 composite score.",
    }


def recommend_venture_action(store: StateStore, venture: Dict[str, Any], actor: str = "system", needs_aryan=None) -> Dict[str, Any]:
    """Scale/pause/kill recommendation engine (Section 12). Deterministic,
    rule-based, evidence-derived -- mirrors the shape of
    `trading_lab_council.run_trading_council`: it only ever produces and
    logs a recommendation (`vs_recommendations`), it never closes, pauses,
    or scales a venture itself. Every PAUSE/KILL recommendation, and every
    SCALE recommendation with a material budget implication, escalates to
    Needs Aryan; CONTINUE/ITERATE do not (Section 22: "routine reporting
    should not escalate")."""
    scorecard = venture_scorecard(store, venture)
    account = venture_capital_account(store, venture)
    experiments = store.list("vs_experiments", "venture_id=?", (venture["id"],))
    kill_decisions = [e for e in experiments if e.get("decision") == "KILL"]
    scale_decisions = [e for e in experiments if e.get("decision") == "SCALE"]

    bands = [scorecard["execution"]["band"], scorecard["financials"]["band"], scorecard["risks"]["band"]]
    critical_count = bands.count("critical")
    at_risk_count = bands.count("at_risk")
    rationale: List[str] = [
        f"execution: {scorecard['execution']['reason']}", f"financials: {scorecard['financials']['reason']}",
        f"traction: {scorecard['traction']['reason']}", f"risks: {scorecard['risks']['reason']}",
    ]

    if kill_decisions:
        recommendation = "KILL"
        rationale.append(f"{len(kill_decisions)} experiment(s) already recommended KILL")
    elif critical_count >= 1:
        recommendation = "PAUSE"
        rationale.append(f"{critical_count} scorecard dimension(s) are CRITICAL")
    elif at_risk_count >= 2:
        recommendation = "ITERATE"
        rationale.append(f"{at_risk_count} scorecard dimensions are AT_RISK")
    elif scale_decisions and scorecard["financials"]["band"] == "healthy" and venture["status"] in ("ACTIVE", "SCALING"):
        recommendation = "SCALE"
        rationale.append(f"{len(scale_decisions)} experiment(s) recommended SCALE and financials are healthy")
    else:
        recommendation = "CONTINUE"
        rationale.append("no dimension is critical and no strong scale/kill signal yet")

    needs_aryan_id = None
    if needs_aryan is not None:
        if recommendation == "KILL":
            needs_aryan_id = needs_aryan.create_item(
                "venture_kill_recommendation", f"Kill recommended for venture: {venture['name']}",
                "The recommendation engine flagged this venture for KILL. Review the rationale and decide.",
                actor=actor, rationale="; ".join(rationale), risk="continued investment in a venture with a kill-decision experiment",
                ref_type="vs_venture", ref_id=venture["id"],
            )
        elif recommendation == "PAUSE":
            needs_aryan_id = needs_aryan.create_item(
                "venture_pause_recommendation", f"Pause recommended for venture: {venture['name']}",
                "The recommendation engine flagged this venture for PAUSE. Review the rationale and decide.",
                actor=actor, rationale="; ".join(rationale), risk="at least one scorecard dimension is critical",
                ref_type="vs_venture", ref_id=venture["id"],
            )
        elif recommendation == "SCALE" and account["actual"]["assigned_budget_net_allocated"] >= LARGE_ALLOCATION_THRESHOLD:
            needs_aryan_id = needs_aryan.create_item(
                "venture_budget_allocation", f"Scale recommended for venture: {venture['name']} (material budget)",
                "The recommendation engine flagged this venture for SCALE, with a material existing allocation. Review before committing further capital.",
                actor=actor, rationale="; ".join(rationale), risk="scaling capital commitment",
                ref_type="vs_venture", ref_id=venture["id"],
            )

    now = utcnow()
    rec_id = store.create("vs_recommendations", {
        "venture_id": venture["id"], "recommendation": recommendation, "rationale_json": json.dumps(rationale),
        "inputs_snapshot_json": json.dumps({"scorecard": scorecard, "capital_account": account}),
        "needs_aryan_id": needs_aryan_id, "actor": actor, "created_at": now,
    })
    return {
        "recommendation_id": rec_id, "venture_id": venture["id"], "recommendation": recommendation,
        "rationale": rationale, "needs_aryan_id": needs_aryan_id,
        "note": "Recommendation only -- nothing here pauses, kills, or scales the venture automatically.",
    }


def venture_recommendation_history(store: StateStore, venture_id: str) -> List[Dict[str, Any]]:
    return list(reversed(store.list("vs_recommendations", "venture_id=?", (venture_id,))))


def command_center_venture_rollup(store: StateStore) -> Dict[str, Any]:
    """CEO/Command Center venture rollup (Section 20): active ventures,
    validating ventures, venture revenue, venture spend, venture
    profitability, venture risks, upcoming milestones, ventures requiring a
    decision. Read-only aggregation over real rows, same discipline as
    `command_center.command_center_snapshot`."""
    ventures = store.list("vs_ventures")
    by_status: Dict[str, int] = {}
    for v in ventures:
        by_status[v["status"]] = by_status.get(v["status"], 0) + 1

    total_revenue = 0.0
    total_spend = 0.0
    ventures_requiring_decision: List[Dict[str, Any]] = []
    upcoming_milestones: List[Dict[str, Any]] = []
    now = datetime.now(timezone.utc)

    for v in ventures:
        if v["status"] in TERMINAL_VENTURE_STATUSES:
            continue
        account = venture_capital_account(store, v)
        total_revenue += account["actual"]["revenue_total"]
        total_spend += account["actual"]["spend_to_date"]

        open_recs = store.list("vs_recommendations", "venture_id=? AND recommendation IN (?,?)", (v["id"], "PAUSE", "KILL"))
        pending_alloc = [a for a in store.list("vs_capital_allocations", "venture_id=?", (v["id"],)) if a.get("needs_aryan_id")]
        if open_recs or pending_alloc:
            ventures_requiring_decision.append({
                "venture_id": v["id"], "name": v["name"], "status": v["status"],
                "reason": "pending pause/kill recommendation" if open_recs else "pending capital allocation needing a decision",
            })

        for goal in store.list("cc_goals", "venture_id=?", (v["id"],)):
            if goal["status"] != "ACTIVE" or not goal.get("deadline"):
                continue
            try:
                days = (datetime.fromisoformat(goal["deadline"]).replace(tzinfo=timezone.utc) - now).days
            except (TypeError, ValueError):
                continue
            if 0 <= days <= 30:
                upcoming_milestones.append({"venture_id": v["id"], "venture_name": v["name"], "goal_title": goal["title"], "deadline": goal["deadline"]})

    open_venture_risks = [r for r in store.list("needs_aryan_items", "status=?", ("PENDING",)) if r["kind"] in {
        "venture_creation_approval", "venture_budget_allocation", "venture_launch_approval",
        "venture_resource_shift_approval", "venture_pause_recommendation", "venture_kill_recommendation",
        "venture_risk_escalation", "venture_large_spend_override",
    }]

    return {
        "generated_at": utcnow(),
        "venture_count": len(ventures), "by_status": by_status,
        "active_venture_count": by_status.get("ACTIVE", 0) + by_status.get("SCALING", 0),
        "validating_venture_count": by_status.get("VALIDATING", 0),
        "venture_revenue_total": round(total_revenue, 2),
        "venture_spend_total": round(total_spend, 2),
        "venture_profitability_estimate": round(total_revenue - total_spend, 2),
        "ventures_requiring_decision": ventures_requiring_decision,
        "upcoming_milestones": upcoming_milestones,
        "pending_needs_aryan_venture_items": len(open_venture_risks),
        "note": "Excludes CLOSED/REJECTED ventures from revenue/spend totals. No Trading Lab (tl_*) figures are included.",
        "source": "vs_ventures, cc_ledger_entries (venture_id), rh_invoices (via venture-linked rh_opportunities), cc_goals (venture_id), needs_aryan_items",
    }
