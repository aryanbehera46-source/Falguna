"""Persisted Revenue & Delivery Engine V1 journey.

This module is deliberately an orchestration/read-model layer over the
existing enquiry, opportunity, proposal, closing, onboarding, Active Job,
completion and billing records.  It does not create a parallel CRM and it
never sends a proposal, invoice, email, deployment, or payment action.
"""

import json
from typing import Any, Dict, Optional

from .audit import AuditLog
from .billing import BillingStore, CompletionService
from .onboarding import DeliveryBriefService, OnboardingStore
from .revenue_hunter import OpportunityStore
from .store import StateStore


class RevenueDeliveryError(ValueError):
    pass


class RevenueDeliveryService:
    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit
        self.opportunities = OpportunityStore(store, audit)
        self.onboarding = OnboardingStore(store, audit)
        self.billing = BillingStore(store, audit)

    def snapshot(self, opportunity_id: str) -> Dict[str, Any]:
        opportunity = self.opportunities.get(opportunity_id)
        if not opportunity:
            raise RevenueDeliveryError("opportunity not found")
        proposals = self.store.list("rh_proposals", "opportunity_id=?", (opportunity_id,))
        approved = [row for row in proposals if row["status"] == "APPROVED"]
        closings = self.store.list("rh_closing_records", "opportunity_id=?", (opportunity_id,))
        closing = closings[-1] if closings else None
        jobs = self.store.list("rh_active_jobs", "opportunity_id=?", (opportunity_id,))
        active_job = jobs[-1] if jobs else None
        completion_rows = self.store.list("rh_completion_records", "opportunity_id=?", (opportunity_id,))
        completion = completion_rows[-1] if completion_rows else None
        invoices = self.store.list("rh_invoices", "opportunity_id=?", (opportunity_id,))
        onboarding = self.onboarding.completion_summary(opportunity_id)
        brief = DeliveryBriefService(self.store, self.audit).build(opportunity_id)

        steps = [
            {"key": "enquiry", "label": "Enquiry captured", "complete": opportunity.get("source") in {"website", "tally"} or bool(opportunity)},
            {"key": "opportunity", "label": "CRM opportunity", "complete": True},
            {"key": "proposal", "label": "Proposal approved", "complete": bool(approved), "needs_human": bool(proposals) and not approved},
            {"key": "project", "label": "Project accepted", "complete": bool(closing)},
            {"key": "intake", "label": "Project intake complete", "complete": onboarding["complete"]},
            {"key": "delivery", "label": "Falguna delivery linked", "complete": bool(active_job)},
            {"key": "handover", "label": "Evidence-backed handover", "complete": bool(completion)},
            {"key": "invoice", "label": "Invoice draft", "complete": bool(invoices)},
        ]
        return {
            "opportunity": opportunity,
            "steps": steps,
            "approved_proposal": approved[-1] if approved else None,
            "closing": closing,
            "client": self.store.get("clients", closing["client_id"]) if closing else None,
            "onboarding": onboarding,
            "delivery_brief": brief,
            "active_job": active_job,
            "completion": completion,
            "invoices": list(reversed(invoices)),
            "next_action": next((step["label"] for step in steps if not step["complete"]), "Review delivery and account growth"),
        }

    def prepare_handover_and_invoice_draft(
        self, opportunity_id: str, actor: str, *, evidence: Any,
        qa_checklist: Dict[str, bool], amount: Optional[float] = None,
        currency: Optional[str] = None, due_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Owner-triggered finalization; persists evidence and a DRAFT only."""
        state = self.snapshot(opportunity_id)
        if not state["approved_proposal"]:
            raise RevenueDeliveryError("an approved proposal is required before handover")
        if not state["closing"] or not state["client"]:
            raise RevenueDeliveryError("an approved project closing and client are required before handover")
        if not state["onboarding"]["complete"]:
            raise RevenueDeliveryError("project intake must be complete before handover")
        if not state["active_job"]:
            raise RevenueDeliveryError("a linked Falguna Active Job is required before handover")
        if not evidence:
            raise RevenueDeliveryError("real delivery evidence is required before handover")
        required_checks = {"delivery_verified", "independent_qa", "acceptance_criteria_met", "handover_ready"}
        missing = sorted(key for key in required_checks if qa_checklist.get(key) is not True)
        if missing:
            raise RevenueDeliveryError("handover QA is incomplete: " + ", ".join(missing))

        completion_id = CompletionService(self.store, self.audit).record_completion(
            opportunity_id, actor, evidence, active_job_id=state["active_job"]["id"], checklist=qa_checklist,
        )
        existing = [row for row in state["invoices"] if row["status"] != "CANCELLED"]
        if existing:
            invoice_id = existing[0]["id"]
        else:
            invoice_amount = amount if amount is not None else state["closing"].get("final_price")
            if invoice_amount is None or float(invoice_amount) <= 0:
                raise RevenueDeliveryError("a positive invoice amount is required")
            invoice_id = self.billing.create_invoice(
                state["client"]["id"], actor, float(invoice_amount),
                currency=currency or state["closing"].get("currency") or "USD",
                opportunity_id=opportunity_id, active_job_id=state["active_job"]["id"],
                milestone="Final delivery", due_date=due_date,
            )
        self.audit.append("RH_HANDOVER_PACKAGE_PREPARED", {
            "opportunity_id": opportunity_id, "completion_id": completion_id,
            "invoice_id": invoice_id, "actor": actor,
        })
        return {
            "completion_id": completion_id,
            "invoice_id": invoice_id,
            "invoice_status": self.billing.get(invoice_id)["status"],
            "external_action_taken": False,
        }
