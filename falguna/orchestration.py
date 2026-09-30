"""Company-wide Orchestration V1 (Phase 4 Sprint 2).

This module detects the current state of each existing commercial
delivery journey, determines the next permitted internal action, and
records each detected transition as a persisted, idempotent company
event -- it does not implement a second commercial state machine.

Reuse, never duplicate:
  * Step detection is delegated entirely to the existing, already-tested
    `RevenueDeliveryService.snapshot()` (falguna/revenue_delivery.py).
    This module never re-derives what "proposal approved" or "intake
    complete" means.
  * Persistence of the detected transition uses the existing
    `company_os.EventBus` (co_events), with an idempotency key derived
    from the opportunity id and its current set of completed steps, so a
    repeated sync while nothing has changed returns the original event
    rather than creating a duplicate -- interruption-safe by
    construction, no separate "workflow run" table required.
  * Surfacing a human decision means looking up the *existing* pending
    Needs Aryan item for that step (rh_proposal / rh_closing_package),
    never creating a second, parallel approval for something the
    existing module already gates.

This module creates no client, closing record, invoice, mission or
commission of its own, and never calls a mutating method on any other
service -- it is a read + record layer only.
"""

from typing import Any, Dict, List, Optional

from .audit import AuditLog
from .company_os import EventBus
from .revenue_delivery import RevenueDeliveryError, RevenueDeliveryService
from .revenue_hunter import OpportunityStore
from .store import StateStore, utcnow

WORKFLOW_KEY = "commercial_delivery"

# Steps whose "blocked" state corresponds to an existing, real pending
# Needs Aryan item this module can surface (never create a new one for).
# Maps step key -> (needs_aryan ref_type, how to resolve ref_id for the
# needs_aryan_items row from the opportunity_id).
_DECISION_LOOKUPS = {"proposal": "rh_proposal", "project": "rh_closing_package"}


class OrchestrationError(ValueError):
    pass


def _blocked_step(steps: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    for step in steps:
        if not step["complete"]:
            return step
    return None


class WorkflowOrchestrator:
    """Persisted, idempotent detection over the existing Revenue & Delivery
    Engine journey (one opportunity = one commercial_delivery workflow)."""

    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit
        self.delivery = RevenueDeliveryService(store, audit)
        self.opportunities = OpportunityStore(store, audit)
        self.events = EventBus(store, audit)

    def _pending_decision(self, opportunity_id: str, blocked_key: Optional[str]) -> Optional[Dict[str, Any]]:
        ref_type = _DECISION_LOOKUPS.get(blocked_key or "")
        if not ref_type:
            return None
        if ref_type == "rh_closing_package":
            candidates = self.store.list(
                "needs_aryan_items", "ref_type=? AND ref_id=? AND status=?",
                (ref_type, opportunity_id, "PENDING"),
            )
        else:  # rh_proposal: ref_id is the proposal id, not the opportunity id
            proposal_ids = [p["id"] for p in self.store.list("rh_proposals", "opportunity_id=?", (opportunity_id,))]
            candidates = []
            for proposal_id in proposal_ids:
                candidates.extend(self.store.list(
                    "needs_aryan_items", "ref_type=? AND ref_id=? AND status=?",
                    (ref_type, proposal_id, "PENDING"),
                ))
        if not candidates:
            return None
        item = candidates[-1]
        return {
            "id": item["id"], "kind": item["kind"], "title": item["title"],
            "status": item["status"], "created_at": item["created_at"],
        }

    def sync_opportunity(self, opportunity_id: str, actor: str = "system") -> Dict[str, Any]:
        try:
            state = self.delivery.snapshot(opportunity_id)
        except RevenueDeliveryError as exc:
            raise OrchestrationError(str(exc)) from exc
        steps = state["steps"]
        blocked = _blocked_step(steps)
        completed_keys = [s["key"] for s in steps if s["complete"]]
        # `needs_human` on the step itself only ever flags the proposal
        # step (RevenueDeliveryService's own definition) -- but a real
        # pending decision can also exist for the project/closing step
        # (a closing package awaiting approval under the commercial
        # safety default). Looking the decision up directly, rather than
        # trusting the step flag alone, is what lets this module surface
        # that existing decision instead of only ever seeing "Project
        # accepted" as an inert, unexplained blocker.
        decision = self._pending_decision(opportunity_id, blocked["key"] if blocked else None)
        blocked_needs_human = bool(blocked and (blocked.get("needs_human") or decision is not None))

        # Idempotent transition record: one event per (opportunity, exact
        # set of completed steps). A repeated sync while nothing changed
        # returns the original event (EventBus's own idempotency_key
        # guard) instead of creating a duplicate -- safe to call on every
        # dashboard/API read, and safe to resume after an interruption.
        idem_key = f"orch:{WORKFLOW_KEY}:{opportunity_id}:{','.join(completed_keys)}"
        event = self.events.emit(
            "ORCH_STEP_STATE", "orchestration", ref_type="rh_opportunities", ref_id=opportunity_id,
            payload={
                "completed_steps": completed_keys,
                "blocked_step": blocked["key"] if blocked else None,
                "blocked_needs_human": blocked_needs_human,
                "next_action": state["next_action"],
            },
            idempotency_key=idem_key,
        )
        opp = state["opportunity"]
        return {
            "opportunity_id": opportunity_id,
            "title": opp.get("title"), "client_name": opp.get("client_name"),
            "steps": steps,
            "current_step": blocked["key"] if blocked else "done",
            "current_label": blocked["label"] if blocked else "Complete",
            "next_action": state["next_action"],
            "blocked": bool(blocked),
            "blocked_needs_human": blocked_needs_human,
            "decision": decision,
            "done": blocked is None,
            "last_event_id": event["id"],
            "last_event_at": event["created_at"],
        }

    def sync_all(self, actor: str = "system") -> List[Dict[str, Any]]:
        results = []
        for opportunity in self.opportunities.list():
            try:
                results.append(self.sync_opportunity(opportunity["id"], actor=actor))
            except OrchestrationError:
                continue
        return results

    def history(self, opportunity_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        return self.events.list(event_type="ORCH_STEP_STATE", ref_type="rh_opportunities", ref_id=opportunity_id, limit=limit)


def workflow_monitor_snapshot(store: StateStore, audit: AuditLog, actor: str = "system") -> Dict[str, Any]:
    """Read model for the HQ Workflow Monitor. Detects current state for
    every opportunity, buckets into in-progress / blocked-on-human / done.
    Creates no project, invoice, mission or commission of its own."""
    orchestrator = WorkflowOrchestrator(store, audit)
    workflows = orchestrator.sync_all(actor=actor)
    blocked_on_human = [w for w in workflows if w["blocked_needs_human"]]
    done = [w for w in workflows if w["done"]]
    in_progress = [w for w in workflows if not w["done"] and not w["blocked_needs_human"]]
    return {
        "generated_at": utcnow(),
        "total": len(workflows),
        "in_progress": in_progress,
        "blocked_on_human": blocked_on_human,
        "done": done,
        "workflows": workflows,
        "source": (
            "RevenueDeliveryService.snapshot (per opportunity, existing read model) "
            "+ company_os.EventBus (persisted, idempotent transition record)"
        ),
    }
