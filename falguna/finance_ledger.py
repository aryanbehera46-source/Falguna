"""TTT Finance Ledger v1 (Sections 6-8 of the Command Center / CEO
Intelligence + Finance / Capital Engine phase): a lightweight internal
ledger, cash/runway, and profitability estimates.

Deliberately NOT full accounting software (per spec). Two structural rules
keep this honest:

  * `LedgerStore.record` hard-requires `evidence` for every entry, exactly
    like `BillingStore.record_payment` already does for invoice payments --
    no inflow or outflow is ever recorded without something backing it, and
    an entry is only ever voided (kept, marked VOID), never deleted, so the
    full record stays inspectable.
  * Client invoice payments (`rh_invoices`, via `BillingStore`) remain the
    single source of truth for client receivables -- this ledger tracks
    everything else (real outflows, and any non-invoice inflow). Ledger
    entries are never blended with `rh_invoices` totals without saying so:
    `cash_and_runway` reports the ledger's own net and the invoice-sourced
    cash-in separately, then a clearly labeled combined estimate, rather
    than a single opaque number that could double-count a payment entered
    in both places.
"""

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .audit import AuditLog
from .command_center import command_center_snapshot
from .store import StateStore, utcnow

LEDGER_ENTRY_TYPES = {"INFLOW", "OUTFLOW"}
LEDGER_CATEGORIES = {
    "client_revenue", "subscription_api_cost", "software_tooling", "hosting",
    "contractor", "marketing", "hardware", "tax_reserve", "owner_contribution", "other",
}
LEDGER_STATUSES = {"RECORDED", "VOID"}


class LedgerError(ValueError):
    pass


class LedgerStore:
    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit

    def record(
        self, entry_type: str, category: str, amount: float, evidence: Any, actor: str = "Aryan",
        currency: str = "INR", business_unit: Optional[str] = None, client_id: Optional[str] = None,
        project_ref: Optional[str] = None, occurred_on: Optional[str] = None, note: Optional[str] = None,
    ) -> str:
        if entry_type not in LEDGER_ENTRY_TYPES:
            raise LedgerError(f"entry_type must be one of {sorted(LEDGER_ENTRY_TYPES)}")
        if category not in LEDGER_CATEGORIES:
            raise LedgerError(f"category must be one of {sorted(LEDGER_CATEGORIES)}")
        if amount is None or amount <= 0:
            raise LedgerError("amount must be a positive number")
        if not evidence:
            raise LedgerError("a ledger entry cannot be recorded without evidence (receipt, invoice reference, bank/manual confirmation)")
        now = utcnow()
        entry_id = self.store.create("cc_ledger_entries", {
            "entry_type": entry_type, "category": category, "amount": amount, "currency": currency,
            "business_unit": business_unit, "client_id": client_id, "project_ref": project_ref,
            "occurred_on": occurred_on or now[:10], "evidence": json.dumps(evidence) if not isinstance(evidence, str) else evidence,
            "note": note, "status": "RECORDED", "actor": actor, "created_at": now, "updated_at": now,
        })
        self.audit.append("CC_LEDGER_ENTRY_RECORDED", {"entry_id": entry_id, "entry_type": entry_type, "category": category, "amount": amount, "actor": actor})
        return entry_id

    def void(self, entry_id: str, actor: str, reason: str) -> Dict[str, Any]:
        entry = self._require(entry_id)
        if entry["status"] == "VOID":
            raise LedgerError("entry is already VOID")
        if not reason:
            raise LedgerError("a reason is required to void a ledger entry")
        self.store.update("cc_ledger_entries", entry_id, status="VOID")
        self.audit.append("CC_LEDGER_ENTRY_VOIDED", {"entry_id": entry_id, "reason": reason, "actor": actor})
        return self.get(entry_id)

    def _require(self, entry_id: str) -> Dict[str, Any]:
        entry = self.store.get("cc_ledger_entries", entry_id)
        if not entry:
            raise LedgerError("ledger entry not found")
        return entry

    def get(self, entry_id: str) -> Optional[Dict[str, Any]]:
        return self.store.get("cc_ledger_entries", entry_id)

    def list(
        self, entry_type: Optional[str] = None, category: Optional[str] = None,
        business_unit: Optional[str] = None, client_id: Optional[str] = None,
        status: str = "RECORDED",
    ) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if status:
            clauses.append("status=?"); params.append(status)
        if entry_type:
            clauses.append("entry_type=?"); params.append(entry_type)
        if category:
            clauses.append("category=?"); params.append(category)
        if business_unit:
            clauses.append("business_unit=?"); params.append(business_unit)
        if client_id:
            clauses.append("client_id=?"); params.append(client_id)
        where = " AND ".join(clauses) if clauses else "1=1"
        return list(reversed(self.store.list("cc_ledger_entries", where, tuple(params))))


def cash_and_runway(store: StateStore, near_term_days: int = 14, burn_lookback_days: int = 90) -> Dict[str, Any]:
    """Cash / Runway (Section 7). Every figure is labeled actual, expected,
    or projected -- never blended silently."""
    now = datetime.now(timezone.utc)
    lookback_start = (now - timedelta(days=burn_lookback_days)).isoformat()

    entries = store.list("cc_ledger_entries", "status=?", ("RECORDED",))
    ledger_inflows = sum(e["amount"] for e in entries if e["entry_type"] == "INFLOW")
    ledger_outflows = sum(e["amount"] for e in entries if e["entry_type"] == "OUTFLOW")

    snapshot = command_center_snapshot(store)
    invoice_cash_in = snapshot["cash"]["cash_in_to_date"]

    actual_cash_estimate = round(invoice_cash_in + ledger_inflows - ledger_outflows, 2)

    due_soon_total = sum(
        o.get("amount", 0) for o in snapshot["upcoming_obligations"] if o["kind"] == "invoice_due"
    )

    recent_outflows = [e for e in entries if e["entry_type"] == "OUTFLOW" and e["created_at"] >= lookback_start]
    monthly_burn_rate = None
    if recent_outflows:
        total_recent_outflow = sum(e["amount"] for e in recent_outflows)
        months_in_window = max(burn_lookback_days / 30.0, 1e-9)
        monthly_burn_rate = round(total_recent_outflow / months_in_window, 2)

    runway_months = None
    if monthly_burn_rate:
        runway_months = round(actual_cash_estimate / monthly_burn_rate, 1)

    return {
        "generated_at": utcnow(),
        "actual": {
            "invoice_cash_in_to_date": invoice_cash_in,
            "ledger_inflows_recorded": round(ledger_inflows, 2),
            "ledger_outflows_recorded": round(ledger_outflows, 2),
            "combined_cash_estimate": actual_cash_estimate,
            "note": (
                "combined_cash_estimate = invoice payments + ledger inflows - ledger outflows. "
                "If a client payment was ALSO logged as a client_revenue ledger entry, this double-counts it -- "
                "invoice payments already flow through rh_invoices and should not be re-entered here."
            ),
            "source": "rh_invoices.amount_received + cc_ledger_entries (status=RECORDED)",
        },
        "expected": {
            "receivables_outstanding": snapshot["receivables"]["outstanding_total"],
            "receivables_overdue": snapshot["receivables"]["overdue_total"],
            "receivables_due_within_days": near_term_days,
            "receivables_due_soon": round(due_soon_total, 2),
            "source": "rh_invoices (not yet actual cash until a payment is recorded)",
        },
        "projected": {
            "near_term_cash": round(actual_cash_estimate + due_soon_total, 2),
            "note": f"actual combined cash estimate plus invoices due within {near_term_days} days -- a projection, not a guarantee.",
        },
        "runway": {
            "monthly_burn_rate": monthly_burn_rate,
            "runway_months": runway_months,
            "lookback_days": burn_lookback_days,
            "note": (
                "Conservative and evidence-based: burn rate is the real recorded outflow total over the "
                "lookback window, averaged to a monthly figure. Null when there isn't enough recorded outflow "
                "history yet -- never a guessed number."
            ),
            "source": "cc_ledger_entries (entry_type=OUTFLOW) over the lookback window",
        },
    }


def client_profitability(store: StateStore, client_id: str) -> Dict[str, Any]:
    """Project/client profitability estimate (Section 8). Only counts costs
    that were actually recorded against this client in the ledger --
    `completeness` is downgraded honestly when no costs have been recorded
    at all, since that almost certainly means costs exist but were never
    logged, not that the project was free to deliver."""
    client = store.get("clients", client_id)
    if not client:
        raise LedgerError("client not found")

    invoices = store.list("rh_invoices", "client_id=?", (client_id,))
    invoice_revenue = sum((inv["amount_received"] or 0.0) for inv in invoices)

    ledger_entries = store.list("cc_ledger_entries", "client_id=? AND status=?", (client_id, "RECORDED"))
    ledger_revenue = sum(e["amount"] for e in ledger_entries if e["entry_type"] == "INFLOW")
    direct_costs = sum(e["amount"] for e in ledger_entries if e["entry_type"] == "OUTFLOW")

    total_revenue = round(invoice_revenue + ledger_revenue, 2)
    gross_profit = round(total_revenue - direct_costs, 2)

    completeness = "partial_no_costs_recorded" if direct_costs == 0 else "estimated_from_recorded_costs"

    return {
        "client_id": client_id, "client_name": client["name"],
        "revenue": total_revenue, "direct_costs": round(direct_costs, 2), "gross_profit_estimate": gross_profit,
        "completeness": completeness,
        "note": (
            "Direct costs are only what has been explicitly logged against this client in the ledger "
            "(AI/API, contractor, hosting/tooling allocation, etc). Missing costs are never invented -- "
            "if nothing was logged, this is a partial view, not a claim of zero cost."
        ),
        "source": "rh_invoices.amount_received + cc_ledger_entries (client_id-tagged, status=RECORDED)",
    }
