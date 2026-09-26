"""TTT Budgets + Capital Allocation + Reserves + Risk Register v1
(Sections 10-12 and 15 of the Command Center / CEO Intelligence + Finance /
Capital Engine phase).

Four small, honest systems, none of which move money or change the
Master Vision Backlog on their own:

  * `BudgetStore` / `budget_status` -- a department budget's spend-to-date
    is never a separately entered number; it is always computed live from
    `cc_ledger_entries` (business_unit=department, OUTFLOW, RECORDED) for
    the period, so it can never drift out of sync with the ledger. Spend
    past a HARD limit escalates to Needs Aryan exactly once per open
    overage (guarded the same way the Digital Workforce's exhausted-retry
    escalation is: check for an existing PENDING item before creating one).
  * `recommend_allocation` -- capital allocation recommendations only,
    derived from real signals already in Command Center / KPIs (overdue
    receivables, deals in negotiation, workforce failures, published
    content). It never claims a computed ROI it cannot back, and every
    recommendation states its own confidence and risk plainly. Nothing
    here transfers money.
  * `ReservePolicyStore` / `allowed_experimental_capital` -- reserve
    buckets are a persisted settings row (same pattern as
    `SalesPolicyStore`), and `allowed_experimental_capital` encodes, now,
    the structural rule the spec calls for before any Trading Lab exists:
    speculative capital may only be drawn from money explicitly logged as
    an `owner_contribution` ledger inflow, never client revenue, tax
    reserve, operating runway, or emergency reserve.
  * `RiskRegisterStore` -- a persistent risk register (distinct from
    Command Center's live `risk_signals`, which are transient reads of
    current state). A high or critical severity risk escalates to Needs
    Aryan on creation, since an unresolved risk at that severity is, by
    definition, something Aryan has not yet seen.
"""

import json
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .audit import AuditLog
from .store import StateStore, utcnow

BUDGET_LIMIT_KINDS = {"HARD", "SOFT"}
DEFAULT_WARNING_THRESHOLD_PCT = 0.8

DEFAULT_RESERVE_POLICY = {
    "operating_reserve_pct": 0.0,
    "tax_reserve_pct": 0.0,
    "emergency_reserve_pct": 0.0,
    "reinvestment_pool_pct": 0.0,
    "owner_distribution_pct": 0.0,
    "experimental_capital_pct": 0.0,
    "configured": False,
}

RISK_CATEGORIES = {
    "revenue_risk", "client_risk", "delivery_risk", "finance_risk",
    "security_risk", "infrastructure_risk", "legal_compliance_risk", "concentration_risk",
}
RISK_SEVERITIES = {"low", "medium", "high", "critical"}
RISK_LIKELIHOOD_BANDS = {"unlikely", "possible", "likely", "near_certain", "unknown"}
RISK_STATUSES = {"OPEN", "MITIGATING", "MONITORING", "CLOSED"}
_ESCALATING_SEVERITIES = {"high", "critical"}


class CapitalRiskError(ValueError):
    pass


# Guards ReservePolicyStore.propose_change/apply_pending_change's
# check-then-act sections (list existing pending item, then create/apply).
# Every route in this app already opens a fresh StateStore connection per
# request (see hq_web.py's open_control_plane), so this in-process lock
# only closes the race between concurrent requests handled by this same
# process -- the same scope every other in-file lock in hq_web.py
# (_askf_operations_lock, _askf_cancel_events_lock) already covers, and the
# realistic concurrency here is one operator (Aryan) using the local HQ UI,
# not a distributed multi-writer system.
_RESERVE_POLICY_LOCK = threading.Lock()


def _validate_reserve_policy_updates(updates: Any, *, require_nonempty: bool = False) -> Dict[str, float]:
    """Shared validation for both the direct write (`ReservePolicyStore.save`)
    and the gated proposal path (`propose_change`). Rejects a malformed
    request (wrong type, non-numeric, out-of-range) explicitly instead of
    silently coercing it. Unknown fields are still silently ignored exactly
    as before this phase (`save()` calls this with `require_nonempty=False`,
    preserving `test_save_ignores_unknown_fields`); `propose_change` passes
    `require_nonempty=True` since a proposal with zero real, settable
    changes is a malformed request worth rejecting clearly, not a silent
    no-op approval item."""
    if updates is None:
        updates = {}
    if not isinstance(updates, dict):
        raise CapitalRiskError("updates must be an object")
    validated: Dict[str, float] = {}
    for key, value in updates.items():
        if key not in DEFAULT_RESERVE_POLICY or key == "configured":
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise CapitalRiskError(f"{key} must be a number")
        if value < 0 or value > 1:
            raise CapitalRiskError(f"{key} must be between 0 and 1 (a fraction, e.g. 0.15 for 15%)")
        validated[key] = float(value)
    if require_nonempty and not validated:
        raise CapitalRiskError("updates did not contain any recognized, settable reserve-policy field")
    return validated


class BudgetStore:
    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit

    def create(
        self, department: str, monthly_budget: float, actor: str = "Aryan",
        currency: str = "INR", limit_kind: str = "SOFT", warning_threshold_pct: float = DEFAULT_WARNING_THRESHOLD_PCT,
    ) -> str:
        if not department or not department.strip():
            raise CapitalRiskError("department is required")
        if monthly_budget is None or monthly_budget <= 0:
            raise CapitalRiskError("monthly_budget must be a positive number")
        if limit_kind not in BUDGET_LIMIT_KINDS:
            raise CapitalRiskError(f"limit_kind must be one of {sorted(BUDGET_LIMIT_KINDS)}")
        now = utcnow()
        budget_id = self.store.create("cc_budgets", {
            "department": department.strip(), "monthly_budget": monthly_budget, "currency": currency,
            "limit_kind": limit_kind, "warning_threshold_pct": warning_threshold_pct, "status": "ACTIVE",
            "actor": actor, "created_at": now, "updated_at": now,
        })
        self.audit.append("CC_BUDGET_CREATED", {"budget_id": budget_id, "department": department, "monthly_budget": monthly_budget, "actor": actor})
        return budget_id

    def archive(self, budget_id: str, actor: str) -> Dict[str, Any]:
        self._require(budget_id)
        self.store.update("cc_budgets", budget_id, status="ARCHIVED")
        self.audit.append("CC_BUDGET_ARCHIVED", {"budget_id": budget_id, "actor": actor})
        return self.get(budget_id)

    def _require(self, budget_id: str) -> Dict[str, Any]:
        budget = self.store.get("cc_budgets", budget_id)
        if not budget:
            raise CapitalRiskError("budget not found")
        return budget

    def get(self, budget_id: str) -> Optional[Dict[str, Any]]:
        return self.store.get("cc_budgets", budget_id)

    def list(self, status: str = "ACTIVE") -> List[Dict[str, Any]]:
        rows = self.store.list("cc_budgets", "status=?", (status,)) if status else self.store.list("cc_budgets")
        return list(reversed(rows))


def budget_status(
    store: StateStore, budget: Dict[str, Any], needs_aryan=None,
    period_start: Optional[str] = None, period_end: Optional[str] = None,
) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    if period_start is None:
        period_start = now.replace(day=1).date().isoformat()
    if period_end is None:
        period_end = now.date().isoformat()

    entries = store.list("cc_ledger_entries", "business_unit=? AND entry_type=? AND status=?", (budget["department"], "OUTFLOW", "RECORDED"))
    in_period = [e for e in entries if period_start <= e["occurred_on"] <= period_end]
    spend_to_date = round(sum(e["amount"] for e in in_period), 2)
    remaining_budget = round(budget["monthly_budget"] - spend_to_date, 2)
    warning_threshold = budget.get("warning_threshold_pct") or DEFAULT_WARNING_THRESHOLD_PCT
    warning = spend_to_date >= budget["monthly_budget"] * warning_threshold
    over_limit = spend_to_date > budget["monthly_budget"]

    escalated_needs_aryan_id = None
    if over_limit and budget["limit_kind"] == "HARD" and needs_aryan is not None:
        pending = store.list("needs_aryan_items", "kind=? AND ref_id=? AND status=?", ("budget_override", budget["id"], "PENDING"))
        if pending:
            escalated_needs_aryan_id = pending[0]["id"]
        else:
            escalated_needs_aryan_id = needs_aryan.create_item(
                "budget_override",
                f"{budget['department']} exceeded its hard monthly budget",
                (f"Spend to date ({spend_to_date}) exceeds the {budget['monthly_budget']} hard limit for "
                 f"{budget['department']}. Approve the overage, cut spend, or revise the budget."),
                actor="system", rationale=f"spend_to_date={spend_to_date}, monthly_budget={budget['monthly_budget']}",
                risk="unbudgeted spend continuing without an owner decision",
                ref_type="cc_budget", ref_id=budget["id"],
            )

    return {
        "budget_id": budget["id"], "department": budget["department"],
        "period_start": period_start, "period_end": period_end,
        "monthly_budget": budget["monthly_budget"], "spend_to_date": spend_to_date,
        "remaining_budget": remaining_budget, "limit_kind": budget["limit_kind"],
        "warning": warning, "over_limit": over_limit,
        "escalated_needs_aryan_id": escalated_needs_aryan_id,
        "source": "cc_ledger_entries (business_unit=department, entry_type=OUTFLOW, status=RECORDED) within the period",
    }


def recommend_allocation(store: StateStore, available_amount: float, actor: str = "Aryan") -> Dict[str, Any]:
    """Capital allocation recommendations (Section 11). Never moves money;
    every recommended amount is a suggested split of the amount the caller
    provided, derived from real, currently-live signals -- never a
    system-computed "optimal" split, and never a guaranteed ROI."""
    from .command_center import command_center_snapshot, kpi_snapshot  # local import: avoids a cycle at module load

    if available_amount is None or available_amount <= 0:
        raise CapitalRiskError("available_amount must be a positive number")

    snapshot = command_center_snapshot(store)
    kpis = kpi_snapshot(store)
    recommendations: List[Dict[str, Any]] = []

    if snapshot["receivables"]["overdue_total"] > 0:
        recommendations.append({
            "category": "reserves", "amount": 0.0,
            "reason": f"{snapshot['receivables']['overdue_count']} overdue invoice(s) totaling {snapshot['receivables']['overdue_total']} should be collected before committing new capital.",
            "expected_benefit": "Recovers money already owed rather than committing new capital.",
            "confidence": "high", "risk": "none -- this is a collections action, not a spending decision",
            "alternative": "Allocate anyway and accept the collections risk stays open.",
        })
    if snapshot["pipeline"]["negotiating_count"] > 0:
        amount = round(available_amount * 0.3, 2)
        recommendations.append({
            "category": "sales_acquisition", "amount": amount,
            "reason": f"{snapshot['pipeline']['negotiating_count']} deal(s) are actively in negotiation right now -- a directly visible pipeline to support.",
            "expected_benefit": "Faster follow-through on deals already in motion (not a guaranteed close).",
            "confidence": "medium", "risk": "deals in negotiation can still fall through",
            "alternative": "Hold this capital in reserves until deals actually close.",
        })
    if snapshot["workforce"]["needs_attention_count"] > 0:
        amount = round(available_amount * 0.15, 2)
        recommendations.append({
            "category": "tooling_api", "amount": amount,
            "reason": f"{snapshot['workforce']['needs_attention_count']} workforce task(s) are currently blocked, failed, or escalated -- a likely capability or tooling gap.",
            "expected_benefit": "Could reduce recurring manual intervention (not quantified -- too little failure history to size this precisely).",
            "confidence": "low", "risk": "root cause of the failures has not been diagnosed yet",
            "alternative": "Investigate the failures manually before spending on new tooling.",
        })
    content_published = kpis["media"]["content_published"]["value"]
    if content_published:
        amount = round(available_amount * 0.15, 2)
        recommendations.append({
            "category": "media_growth", "amount": amount,
            "reason": f"{content_published} piece(s) published in the last {kpis['period_days']} days -- an already-active channel.",
            "expected_benefit": "More reach on a channel already in motion (actual reach depends on real analytics, not guaranteed).",
            "confidence": "low", "risk": "no verified reach/engagement data yet confirms this channel converts",
            "alternative": "Wait for real analytics before increasing media spend.",
        })

    allocated_so_far = round(sum(r["amount"] for r in recommendations), 2)
    remainder = round(available_amount - allocated_so_far, 2)
    if remainder > 0:
        recommendations.append({
            "category": "reserves", "amount": remainder,
            "reason": "Unallocated remainder -- no further signal-backed recommendation covers this amount.",
            "expected_benefit": "Preserves optionality and runway.",
            "confidence": "high", "risk": "opportunity cost of not deploying it sooner",
            "alternative": "Deploy manually against a specific, owner-identified opportunity.",
        })

    return {
        "generated_at": utcnow(), "available_amount": available_amount, "actor": actor,
        "recommendations": recommendations,
        "note": "Recommendations only -- nothing here moves money. Executing any of these still requires an explicit Needs Aryan decision (kind=capital_allocation_execution).",
    }


class ReservePolicyStore:
    """A single persisted, editable settings row -- same pattern as
    `SalesPolicyStore`/`AcquisitionProfileStore`, stored in the existing
    generic `rh_settings` table rather than a new one.

    Approval gate (Phase 1, Requirement 1): a reserve-policy change is a
    capital-governance decision -- exactly the kind of thing
    `NEEDS_ARYAN_KINDS` already lists `reserve_policy_change` for. External
    callers (the HQ UI's `/api/cc/reserve-policy` route) must go through
    `propose_change()`, never `save()` directly. `propose_change()` never
    writes the policy: it creates exactly one pending Needs Aryan item, and
    the active policy stays unchanged until `apply_pending_change()` runs
    that item after it has genuinely been approved (called only from the
    same Needs Aryan decision route that already performs this exact
    pattern for `rh_closing_package` -- see
    `sales_ops.ClosingService.finalize_pending_closing`). `save()` remains
    as the low-level direct write: used internally by `apply_pending_change`
    once approved, and by tests/fixtures that deliberately set up a policy
    state directly -- Company OS's own docstring already states it never
    calls `ReservePolicyStore.save` for exactly this reason."""

    KEY = "cc_reserve_policy"

    def __init__(self, store: StateStore, audit: Optional[AuditLog] = None, needs_aryan=None):
        self.store = store
        self.audit = audit
        self.needs_aryan = needs_aryan

    def get(self) -> Dict[str, Any]:
        rows = self.store.list("rh_settings", "key=?", (self.KEY,))
        if not rows:
            return json.loads(json.dumps(DEFAULT_RESERVE_POLICY))
        saved = json.loads(rows[-1]["value_json"])
        merged = json.loads(json.dumps(DEFAULT_RESERVE_POLICY))
        merged.update(saved)
        return merged

    def save(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        """Direct, ungated write -- see the class docstring for who may call
        this. Validates numeric fields the same way `propose_change` does;
        unknown fields are still silently ignored and an empty/all-unknown
        update is still a harmless no-op (both unchanged from before this
        phase -- see test_save_ignores_unknown_fields)."""
        validated = _validate_reserve_policy_updates(updates, require_nonempty=False)
        merged = self.get()
        merged.update(validated)
        merged["configured"] = True
        rows = self.store.list("rh_settings", "key=?", (self.KEY,))
        now = utcnow()
        if rows:
            self.store.update("rh_settings", rows[-1]["id"], value_json=json.dumps(merged))
        else:
            self.store.create("rh_settings", {"key": self.KEY, "value_json": json.dumps(merged), "created_at": now, "updated_at": now})
        return merged

    def propose_change(self, updates: Dict[str, Any], actor: str = "Aryan") -> Dict[str, Any]:
        """The gated entry point every external caller must use. Never
        writes the policy -- creates one pending `reserve_policy_change`
        Needs Aryan item and returns AWAITING_APPROVAL. A second proposal
        while one is already pending returns that same pending item instead
        of creating a duplicate (idempotent, same convention as
        `ClosingService._prepare_closing_package`) -- this also structurally
        rules out a "stale" superseded proposal, since only one reserve-
        policy change can ever be in flight at a time."""
        validated = _validate_reserve_policy_updates(updates, require_nonempty=True)
        if not actor or not str(actor).strip():
            raise CapitalRiskError("actor is required")
        actor = str(actor).strip()

        with _RESERVE_POLICY_LOCK:
            pending = self.store.list("needs_aryan_items", "ref_type=? AND ref_id=? AND status=?", ("cc_reserve_policy", self.KEY, "PENDING"))
            if pending:
                item = pending[-1]
                return {
                    "status": "AWAITING_APPROVAL", "needs_aryan_id": item["id"],
                    "current_policy": self.get(),
                    "proposed_changes": json.loads(item["payload_json"]) if item.get("payload_json") else {},
                    "already_pending": True,
                }
            current = self.get()
            changes_desc = ", ".join(f"{k}: {current.get(k)} -> {v}" for k, v in sorted(validated.items()))
            needs_aryan_id = None
            if self.needs_aryan is not None:
                needs_aryan_id = self.needs_aryan.create_item(
                    "reserve_policy_change",
                    "Reserve policy change requested",
                    (f"A change to the reserve policy has been proposed ({changes_desc}). "
                     "Review and approve or reject before it takes effect -- the current policy "
                     "stays active until you decide."),
                    actor=actor, rationale=changes_desc,
                    ref_type="cc_reserve_policy", ref_id=self.KEY,
                    payload_json=json.dumps(validated),
                )
            if self.audit is not None:
                self.audit.append("CC_RESERVE_POLICY_PROPOSED", {"needs_aryan_id": needs_aryan_id, "actor": actor, "changes": validated})
            return {
                "status": "AWAITING_APPROVAL", "needs_aryan_id": needs_aryan_id,
                "current_policy": current, "proposed_changes": validated, "already_pending": False,
            }

    def apply_pending_change(self, needs_aryan_id: str, actor: str) -> Dict[str, Any]:
        """Performs the real reserve-policy write for a proposal that has
        just been approved. Called only from the Needs Aryan decision route,
        immediately after `NeedsAryanQueue.decide()` records the approval --
        never invoked on its own initiative, and `decide()` itself already
        refuses to decide the same item twice (raises if it is no longer
        PENDING), which is what makes a retried decision-request safe.
        Idempotent against a direct retry of this method too: an item
        already marked applied is detected and returned without writing
        again."""
        item = self.store.get("needs_aryan_items", needs_aryan_id)
        if not item:
            raise CapitalRiskError("needs aryan item not found")
        if item.get("ref_type") != "cc_reserve_policy":
            raise CapitalRiskError("this needs aryan item is not a reserve-policy change")
        if item.get("status") != "APPROVED":
            raise CapitalRiskError("reserve-policy change has not been approved")

        marker = f"[applied:{needs_aryan_id}]"
        with _RESERVE_POLICY_LOCK:
            if item.get("decision_note") and marker in item["decision_note"]:
                return self.get()
            updates = json.loads(item["payload_json"]) if item.get("payload_json") else {}
            policy = self.save(updates)
            note = f"{item.get('decision_note') or ''} {marker}".strip()
            self.store.update("needs_aryan_items", needs_aryan_id, decision_note=note)
        if self.audit is not None:
            self.audit.append("CC_RESERVE_POLICY_APPLIED", {"needs_aryan_id": needs_aryan_id, "actor": actor, "changes": updates})
        return policy


def allowed_experimental_capital(store: StateStore) -> Dict[str, Any]:
    """Structural separation for a future Trading Lab (Section 12),
    encoded now, before that lab exists. Experimental/speculative capital
    may only be drawn from money explicitly logged as an
    `owner_contribution` ledger inflow -- never client revenue (which lives
    in `rh_invoices`, never even in this ledger), never a `tax_reserve`
    ledger entry, and never plain operating cash. Spend against this pool
    is tracked by tagging a ledger OUTFLOW's `business_unit` as
    `experimental_capital`. Returns 0, never a fabricated positive number,
    when nothing has ever been contributed for this purpose."""
    contributed = sum(
        e["amount"] for e in store.list("cc_ledger_entries", "entry_type=? AND category=? AND status=?", ("INFLOW", "owner_contribution", "RECORDED"))
    )
    spent = sum(
        e["amount"] for e in store.list("cc_ledger_entries", "entry_type=? AND business_unit=? AND status=?", ("OUTFLOW", "experimental_capital", "RECORDED"))
    )
    return {
        "available": round(contributed - spent, 2),
        "contributed_total": round(contributed, 2),
        "spent_total": round(spent, 2),
        "excluded_sources": ["client revenue (rh_invoices)", "tax_reserve ledger entries", "operating cash/runway", "emergency_reserve"],
        "note": (
            "Experimental/speculative capital (e.g. a future Trading Lab) may only be drawn from money "
            "explicitly logged as an owner_contribution ledger inflow. It structurally excludes client funds, "
            "the tax reserve, operating runway, and the emergency reserve, even though no Trading Lab exists yet."
        ),
        "source": "cc_ledger_entries: INFLOW/owner_contribution minus OUTFLOW tagged business_unit=experimental_capital",
    }


class RiskRegisterStore:
    """Persistent company risk register (Section 15) -- distinct from
    Command Center's live `risk_signals`, which are a transient read of
    current state. A risk logged here stays logged (status changes, is
    never deleted) until explicitly closed."""

    def __init__(self, store: StateStore, audit: AuditLog, needs_aryan=None):
        self.store = store
        self.audit = audit
        self.needs_aryan = needs_aryan

    def create(
        self, title: str, category: str, severity: str, actor: str = "Aryan",
        likelihood_band: str = "unknown", owner: Optional[str] = None,
        mitigation: Optional[str] = None, evidence: Optional[str] = None,
        venture_id: Optional[str] = None,
    ) -> str:
        if not title or not title.strip():
            raise CapitalRiskError("title is required")
        if category not in RISK_CATEGORIES:
            raise CapitalRiskError(f"category must be one of {sorted(RISK_CATEGORIES)}")
        if severity not in RISK_SEVERITIES:
            raise CapitalRiskError(f"severity must be one of {sorted(RISK_SEVERITIES)}")
        if likelihood_band not in RISK_LIKELIHOOD_BANDS:
            raise CapitalRiskError(f"likelihood_band must be one of {sorted(RISK_LIKELIHOOD_BANDS)}")
        now = utcnow()
        risk_id = self.store.create("cc_risks", {
            "title": title.strip(), "category": category, "severity": severity, "likelihood_band": likelihood_band,
            "owner": owner, "mitigation": mitigation, "evidence": evidence, "status": "OPEN",
            # Venture Studio v1 (Section 27): a risk optionally belongs to
            # one venture -- NULL means company-wide, exactly as before.
            "venture_id": venture_id,
            "needs_aryan_id": None, "actor": actor, "created_at": now, "updated_at": now,
        })
        needs_aryan_id = None
        if severity in _ESCALATING_SEVERITIES and self.needs_aryan is not None:
            # A high/critical risk on a venture escalates through the
            # venture-specific kind (Section 22: "high-risk venture issue")
            # rather than the generic company-wide risk_escalation kind, so
            # Needs Aryan can distinguish the two at a glance.
            kind = "venture_risk_escalation" if venture_id else "risk_escalation"
            needs_aryan_id = self.needs_aryan.create_item(
                kind, f"High-severity risk logged: {title.strip()}",
                "Review this risk and decide on mitigation, acceptance, or further investigation.",
                actor=actor, rationale=mitigation, risk=f"{severity} severity, {likelihood_band} likelihood",
                ref_type="vs_venture" if venture_id else "cc_risk", ref_id=venture_id or risk_id,
            )
            self.store.update("cc_risks", risk_id, needs_aryan_id=needs_aryan_id)
        self.audit.append("CC_RISK_CREATED", {"risk_id": risk_id, "category": category, "severity": severity, "actor": actor, "needs_aryan_id": needs_aryan_id})
        return risk_id

    def update_status(self, risk_id: str, status: str, actor: str, note: Optional[str] = None) -> Dict[str, Any]:
        if status not in RISK_STATUSES:
            raise CapitalRiskError(f"status must be one of {sorted(RISK_STATUSES)}")
        self._require(risk_id)
        self.store.update("cc_risks", risk_id, status=status)
        self.audit.append("CC_RISK_STATUS_CHANGED", {"risk_id": risk_id, "status": status, "actor": actor, "note": note})
        return self.get(risk_id)

    def update_mitigation(self, risk_id: str, mitigation: str, actor: str) -> Dict[str, Any]:
        self._require(risk_id)
        self.store.update("cc_risks", risk_id, mitigation=mitigation)
        self.audit.append("CC_RISK_MITIGATION_UPDATED", {"risk_id": risk_id, "actor": actor})
        return self.get(risk_id)

    def _require(self, risk_id: str) -> Dict[str, Any]:
        risk = self.store.get("cc_risks", risk_id)
        if not risk:
            raise CapitalRiskError("risk not found")
        return risk

    def get(self, risk_id: str) -> Optional[Dict[str, Any]]:
        return self.store.get("cc_risks", risk_id)

    def list(self, status: Optional[str] = None, category: Optional[str] = None, venture_id: Optional[str] = None) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if status:
            clauses.append("status=?"); params.append(status)
        if category:
            clauses.append("category=?"); params.append(category)
        if venture_id:
            clauses.append("venture_id=?"); params.append(venture_id)
        where = " AND ".join(clauses) if clauses else "1=1"
        rows = self.store.list("cc_risks", where, tuple(params))
        return list(reversed(rows))
