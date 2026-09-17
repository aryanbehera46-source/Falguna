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
    generic `rh_settings` table rather than a new one."""

    KEY = "cc_reserve_policy"

    def __init__(self, store: StateStore):
        self.store = store

    def get(self) -> Dict[str, Any]:
        rows = self.store.list("rh_settings", "key=?", (self.KEY,))
        if not rows:
            return json.loads(json.dumps(DEFAULT_RESERVE_POLICY))
        saved = json.loads(rows[-1]["value_json"])
        merged = json.loads(json.dumps(DEFAULT_RESERVE_POLICY))
        merged.update(saved)
        return merged

    def save(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        merged = self.get()
        merged.update({k: v for k, v in (updates or {}).items() if k in DEFAULT_RESERVE_POLICY})
        merged["configured"] = True
        rows = self.store.list("rh_settings", "key=?", (self.KEY,))
        now = utcnow()
        if rows:
            self.store.update("rh_settings", rows[-1]["id"], value_json=json.dumps(merged))
        else:
            self.store.create("rh_settings", {"key": self.KEY, "value_json": json.dumps(merged), "created_at": now, "updated_at": now})
        return merged


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
            "needs_aryan_id": None, "actor": actor, "created_at": now, "updated_at": now,
        })
        needs_aryan_id = None
        if severity in _ESCALATING_SEVERITIES and self.needs_aryan is not None:
            needs_aryan_id = self.needs_aryan.create_item(
                "risk_escalation", f"High-severity risk logged: {title.strip()}",
                "Review this risk and decide on mitigation, acceptance, or further investigation.",
                actor=actor, rationale=mitigation, risk=f"{severity} severity, {likelihood_band} likelihood",
                ref_type="cc_risk", ref_id=risk_id,
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

    def list(self, status: Optional[str] = None, category: Optional[str] = None) -> List[Dict[str, Any]]:
        if status and category:
            rows = self.store.list("cc_risks", "status=? AND category=?", (status, category))
        elif status:
            rows = self.store.list("cc_risks", "status=?", (status,))
        elif category:
            rows = self.store.list("cc_risks", "category=?", (category,))
        else:
            rows = self.store.list("cc_risks")
        return list(reversed(rows))
