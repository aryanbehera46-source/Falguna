"""Unified, read-only company state for the Executive Command Center.

This service composes existing persisted systems. It owns no tables and
performs no writes, so CRM, billing, workforce and partner modules remain
the only sources of truth.
"""

import json
from typing import Any, Dict, List

from .command_center import command_center_snapshot
from .store import StateStore, utcnow


def _count_by(rows: List[Dict[str, Any]], key: str) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for row in rows:
        value = str(row.get(key) or "UNKNOWN")
        counts[value] = counts.get(value, 0) + 1
    return counts


class CompanyStateService:
    """Build one sourced snapshot without persisting derived figures."""

    def __init__(self, store: StateStore):
        self.store = store

    def snapshot(self) -> Dict[str, Any]:
        base = command_center_snapshot(self.store)
        opportunities = self.store.list("rh_opportunities")
        proposals = self.store.list("rh_proposals")
        invoices = [i for i in self.store.list("rh_invoices") if i["status"] != "CANCELLED"]
        jobs = self.store.list("rh_active_jobs")
        completions = self.store.list("rh_completion_records")
        workforce = self.store.list("wf_tasks")
        decisions = self.store.list("needs_aryan_items", "status=?", ("PENDING",))
        partners = self.store.list("pm_partners")
        referrals = self.store.list("pm_referrals")
        commissions = self.store.list("pm_commissions")

        approved_opportunity_ids = {p["opportunity_id"] for p in proposals if p["status"] == "APPROVED"}
        quoted_rows = [o for o in opportunities if o["id"] in approved_opportunity_ids and o.get("final_price") is not None]
        payment_receipts = []
        for invoice in invoices:
            try:
                evidence = json.loads(invoice.get("evidence_json") or "[]")
            except (TypeError, ValueError):
                evidence = []
            for receipt in evidence if isinstance(evidence, list) else []:
                payment_receipts.append({"invoice_id": invoice["id"], "amount": receipt.get("amount", 0), "recorded_at": receipt.get("recorded_at")})

        active_projects = [j for j in jobs if j["handoff_status"] not in ("HANDED_OFF", "CANCELLED")]
        attention_tasks = [t for t in workforce if t["status"] in ("BLOCKED", "FAILED", "NEEDS_ARYAN")]
        qa_passed = 0
        for row in completions:
            try:
                checklist = json.loads(row.get("checklist_json") or "{}")
            except (TypeError, ValueError):
                checklist = {}
            if checklist and all(checklist.values()):
                qa_passed += 1

        return {
            "generated_at": utcnow(),
            "sources": {
                "commercial": "rh_opportunities + rh_proposals + rh_invoices",
                "delivery": "rh_active_jobs + rh_completion_records + wf_tasks",
                "decisions": "needs_aryan_items",
                "partners": "pm_partners + pm_referrals + pm_commissions",
            },
            "financials": {
                "quoted": round(sum(float(o.get("final_price") or 0) for o in quoted_rows), 2),
                "quoted_count": len(quoted_rows),
                "invoiced": round(sum(float(i.get("amount") or 0) for i in invoices), 2),
                "invoice_count": len(invoices),
                "collected": round(sum(float(i.get("amount_received") or 0) for i in invoices), 2),
                "receipt_count": len(payment_receipts),
                "outstanding": round(sum(max(0, float(i.get("amount") or 0) - float(i.get("amount_received") or 0)) for i in invoices), 2),
                "currency_note": "Amounts retain their stored currencies; no FX conversion is inferred.",
            },
            "sales": {
                "leads": {"total": len(opportunities), "by_stage": _count_by(opportunities, "stage")},
                "approved_proposals": len([p for p in proposals if p["status"] == "APPROVED"]),
                "key_opportunities": base["pipeline"]["key_opportunities"],
            },
            "delivery": {
                "active_projects": len(active_projects), "by_handoff_status": _count_by(jobs, "handoff_status"),
                "workforce_by_status": _count_by(workforce, "status"), "workforce_attention": len(attention_tasks),
                "handovers": len(completions), "qa_passed": qa_passed,
            },
            "decisions": {
                "pending": len(decisions), "by_kind": _count_by(decisions, "kind"),
                "items": [{"id": d["id"], "title": d["title"], "kind": d["kind"], "created_at": d["created_at"]} for d in decisions[:8]],
            },
            "partners": {
                "by_status": _count_by(partners, "status"), "referrals_by_status": _count_by(referrals, "attribution_status"),
                "commissions_by_status": _count_by(commissions, "status"),
                "provisional": round(sum(float(c.get("eligible_amount") or 0) for c in commissions if c["status"] == "PROVISIONAL"), 2),
                "eligible": round(sum(float(c.get("eligible_amount") or 0) for c in commissions if c["status"] == "ELIGIBLE"), 2),
                "held": round(sum(float(c.get("eligible_amount") or 0) for c in commissions if c["status"] == "HELD"), 2),
            },
            "risks": base["risk_signals"],
            "upcoming": base["upcoming_obligations"],
        }
