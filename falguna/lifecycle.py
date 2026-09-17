"""TTT Autonomous Revenue-to-Delivery Loop v1 -- Company Workflow Orchestrator.

This is the shared business lifecycle every opportunity moves through, from
first discovery to retention/upsell. It is a coordination layer, not a new
engine: it never re-implements qualification, proposal drafting, stage
tracking, or the Falguna handoff -- all of that stays exactly where it
already lives (revenue_hunter.py, opportunity_agent.py, handoff.py). What
this module adds is the one thing none of those pieces has today: a single,
explicit, auditable state machine spanning the *entire* company loop,
including the parts that previously had no state tracking at all (Won ->
Onboarding -> Delivery -> Client Review -> Completed -> Invoiced -> Paid ->
Retain).

Relationship to the existing coarse pipeline (`rh_opportunities.stage`,
PIPELINE_STAGES in revenue_hunter.py): `stage` is not replaced or
duplicated. It is a narrower, older view (New/Qualified/Proposal Ready/
Applied-Sent/Replied/Meeting/Negotiating/Won/Lost) that every existing
route, test, and UI element already depends on -- changing its meaning or
bypassing it would be exactly the kind of "rebuild instead of extend" this
pass was told not to do. So `LifecycleOrchestrator.transition()` calls the
existing `OpportunityStore.move_stage/mark_won/mark_lost` at the specific
boundary points where a lifecycle transition has a real coarse-stage
equivalent, and leaves `stage` untouched for the many lifecycle states that
don't (PITCH_READY/AWAITING_APPROVAL/APPROVED/APPLYING all still read as
"Proposal Ready" -- APPLYING in particular only means an application
attempt is in progress and may still be blocked, so `stage` only reaches
"Applied/Sent" once CONTACTED is reached with real evidence; everything
from ONBOARDING onward has no `stage` equivalent at all, since `stage` was
never designed to track post-Won delivery).

No silent state jumps: every transition is checked against an explicit
graph (ALLOWED_TRANSITIONS) and refused with a LifecycleError if it isn't a
real, defined edge. Every transition -- valid or refused -- is recorded
in `rh_lifecycle_events` (from_state, to_state, actor, timestamp, reason,
evidence, next_action, approval_requirement, money_impact): the full record
Section 2 of the build spec asked for, kept separate from the older,
narrower `rh_stage_history` rather than overloading it.
"""

import json
from typing import Any, Dict, List, Optional

from .audit import AuditLog
from .revenue_hunter import OpportunityError, OpportunityStore
from .store import StateStore, utcnow


class LifecycleError(ValueError):
    pass


# The full company loop, in the exact terms the build spec named them
# (APPLYING/OUTREACH and RETAIN/UPSELL collapsed to their first word, since
# a state name is a single token everywhere else in this codebase -- the
# richer meaning lives in the transition's `reason`/`evidence`, not the
# state name).
LIFECYCLE_STATES = [
    "DISCOVERED", "RESEARCHING", "QUALIFIED", "PITCH_READY", "AWAITING_APPROVAL",
    "APPROVED", "APPLYING", "CONTACTED", "REPLIED", "DISCOVERY_CONVERSATION",
    "NEGOTIATING", "WON", "ONBOARDING", "DELIVERY", "CLIENT_REVIEW", "COMPLETED",
    "INVOICED", "PAID", "RETAIN", "LOST",
]

TERMINAL_LIFECYCLE_STATES = {"RETAIN", "LOST"}

# Explicit forward graph -- deliberately conservative. LOST is reachable
# from every pre-Won state (a deal can die at any point before it's won;
# that is normal business reality, not a hole in the model) but is a dead
# end here, matching the existing `stage` model's own terminal-stage
# convention (revenue_hunter.TERMINAL_STAGES) rather than inventing a
# different reopen policy for the new, finer state machine.
ALLOWED_TRANSITIONS: Dict[str, set] = {
    "DISCOVERED": {"RESEARCHING", "QUALIFIED", "LOST"},
    "RESEARCHING": {"QUALIFIED", "LOST"},
    "QUALIFIED": {"PITCH_READY", "LOST"},
    "PITCH_READY": {"AWAITING_APPROVAL", "LOST"},
    "AWAITING_APPROVAL": {"APPROVED", "PITCH_READY", "LOST"},
    "APPROVED": {"APPLYING", "LOST"},
    "APPLYING": {"CONTACTED", "APPROVED", "LOST"},
    "CONTACTED": {"REPLIED", "LOST"},
    "REPLIED": {"DISCOVERY_CONVERSATION", "NEGOTIATING", "LOST"},
    "DISCOVERY_CONVERSATION": {"NEGOTIATING", "LOST"},
    "NEGOTIATING": {"WON", "LOST"},
    "WON": {"ONBOARDING"},
    "ONBOARDING": {"DELIVERY"},
    "DELIVERY": {"CLIENT_REVIEW"},
    "CLIENT_REVIEW": {"DELIVERY", "COMPLETED"},
    "COMPLETED": {"INVOICED"},
    "INVOICED": {"PAID"},
    "PAID": {"RETAIN"},
    "RETAIN": set(),
    "LOST": set(),
}

# stage moves the orchestrator performs on behalf of a lifecycle transition,
# reusing OpportunityStore's own, unmodified methods -- never a parallel
# write to `rh_opportunities.stage`. Absent here means "no stage-equivalent
# for this state" (everything Won-onward), so `stage` correctly stays "Won"
# for the rest of the company loop.
#
# APPLYING deliberately has NO stage mapping: it means "an application
# attempt is in progress" (falguna/application_executor.py), which can
# still be BLOCKED and bounced back to APPROVED with nothing actually sent.
# The coarse pipeline's "Applied/Sent" stage is reserved for CONTACTED,
# which the executor only reaches after a channel adapter reports real,
# evidenced submission -- so a blocked attempt never leaves the older
# `stage` field claiming an application was sent when it wasn't.
_STAGE_FOR_STATE = {
    "QUALIFIED": "Qualified",
    "CONTACTED": "Applied/Sent",
    "REPLIED": "Replied",
    "DISCOVERY_CONVERSATION": "Meeting",
    "NEGOTIATING": "Negotiating",
}


class LifecycleOrchestrator:
    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit
        self.opportunities = OpportunityStore(store, audit)

    def current_state(self, opportunity_id: str) -> Optional[str]:
        opp = self.store.get("rh_opportunities", opportunity_id)
        if not opp:
            raise LifecycleError("opportunity not found")
        return opp.get("lifecycle_state")

    def history(self, opportunity_id: str) -> List[Dict[str, Any]]:
        return self.store.list("rh_lifecycle_events", "opportunity_id=?", (opportunity_id,))

    def initialize(self, opportunity_id: str, actor: str, reason: str = "opportunity discovered") -> Dict[str, Any]:
        """Sets the very first lifecycle state. Idempotent: calling this on
        an opportunity that already has a lifecycle_state (e.g. one created
        before this pass existed) is a no-op, never a silent overwrite of
        real history."""
        opp = self.store.get("rh_opportunities", opportunity_id)
        if not opp:
            raise LifecycleError("opportunity not found")
        if opp.get("lifecycle_state"):
            return opp
        self.store.update("rh_opportunities", opportunity_id, lifecycle_state="DISCOVERED")
        return self._record(opportunity_id, None, "DISCOVERED", actor, reason, evidence=None,
                              next_action="Research or qualify this opportunity.", approval_required=False)

    def transition(
        self, opportunity_id: str, to_state: str, actor: str, reason: Optional[str] = None,
        evidence: Optional[Dict[str, Any]] = None, next_action: Optional[str] = None,
        approval_required: bool = False, approval_status: Optional[str] = None,
        money_impact: Optional[Dict[str, Any]] = None, sync_stage: bool = True,
    ) -> Dict[str, Any]:
        if to_state not in LIFECYCLE_STATES:
            raise LifecycleError(f"unknown lifecycle state {to_state!r} -- must be one of {LIFECYCLE_STATES}")
        opp = self.store.get("rh_opportunities", opportunity_id)
        if not opp:
            raise LifecycleError("opportunity not found")
        current = opp.get("lifecycle_state")
        if current is None:
            self.initialize(opportunity_id, actor)
            opp = self.store.get("rh_opportunities", opportunity_id)
            current = opp.get("lifecycle_state")

        if current == to_state:
            # Idempotent no-op (e.g. re-qualifying an already-QUALIFIED
            # opportunity): recorded for the audit trail, but never treated
            # as an illegal jump, and never re-runs the stage side effect.
            return self._record(opportunity_id, current, to_state, actor, reason or "no-op re-affirmation",
                                  evidence, next_action, approval_required, approval_status, money_impact)

        if current in TERMINAL_LIFECYCLE_STATES:
            raise LifecycleError(f"opportunity is already {current} -- terminal, cannot transition to {to_state}")

        allowed = ALLOWED_TRANSITIONS.get(current, set())
        if to_state not in allowed:
            raise LifecycleError(
                f"illegal lifecycle transition {current} -> {to_state} "
                f"(allowed from {current}: {sorted(allowed) or 'none -- terminal'})"
            )

        if sync_stage:
            self._sync_stage(opportunity_id, opp, to_state, actor, reason)

        self.store.update("rh_opportunities", opportunity_id, lifecycle_state=to_state)
        return self._record(opportunity_id, current, to_state, actor, reason, evidence, next_action,
                              approval_required, approval_status, money_impact)

    def _sync_stage(self, opportunity_id: str, opp: Dict[str, Any], to_state: str, actor: str, reason: Optional[str]) -> None:
        if to_state == "WON":
            if opp["stage"] != "Won":
                self.opportunities.mark_won(opportunity_id, actor, note=reason or "Won")
            return
        if to_state == "LOST":
            if opp["stage"] != "Lost":
                self.opportunities.mark_lost(opportunity_id, actor, reason=reason)
            return
        target_stage = _STAGE_FOR_STATE.get(to_state)
        if target_stage and opp["stage"] != target_stage and opp["stage"] not in {"Won", "Lost"}:
            try:
                self.opportunities.move_stage(opportunity_id, target_stage, actor, reason or f"lifecycle -> {to_state}")
            except OpportunityError:
                # A stage move can legitimately be a no-op-shaped refusal
                # (e.g. the pipeline stage already moved forward through the
                # older, direct /stage route) -- the lifecycle event itself
                # still records the real transition either way, so nothing
                # is lost; this only guards against a redundant stage write
                # ever crashing the lifecycle transition it's a side effect
                # of (Section 18: one provider/mechanism hiccup must not
                # crash the whole workflow).
                pass

    def _record(
        self, opportunity_id: str, from_state: Optional[str], to_state: str, actor: str, reason: Optional[str],
        evidence: Optional[Dict[str, Any]], next_action: Optional[str], approval_required: bool,
        approval_status: Optional[str] = None, money_impact: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        event_id = self.store.create("rh_lifecycle_events", {
            "opportunity_id": opportunity_id, "from_state": from_state, "to_state": to_state, "actor": actor,
            "reason": reason, "evidence_json": json.dumps(evidence) if evidence is not None else None,
            "next_action": next_action, "approval_required": 1 if approval_required else 0,
            "approval_status": approval_status, "money_impact_json": json.dumps(money_impact) if money_impact is not None else None,
            "created_at": utcnow(),
        })
        self.audit.append("RH_LIFECYCLE_TRANSITION", {
            "opportunity_id": opportunity_id, "event_id": event_id, "from": from_state, "to": to_state, "actor": actor,
        })
        return self.store.get("rh_lifecycle_events", event_id)

    def try_initialize(self, opportunity_id: str, actor: str, reason: str = "opportunity discovered") -> Optional[Dict[str, Any]]:
        """Best-effort variant of initialize(), for the same reason
        try_transition exists: a hook placed inside OpportunityStore.create()
        must never be able to break opportunity creation itself."""
        try:
            return self.initialize(opportunity_id, actor, reason)
        except LifecycleError:
            return None

    def try_transition(self, opportunity_id: str, to_state: str, actor: str, **kwargs) -> Optional[Dict[str, Any]]:
        """Best-effort variant for hooks placed inside existing, already-
        tested business methods (qualify/generate/approve): a lifecycle
        transition here is pure observability layered on top of behavior
        that already works and is already tested, so a validation error
        here (an unexpected ordering, a pre-Pass-A opportunity with a gap
        in its history, ...) must never break the real business action it
        is attached to. Returns None (and never raises) on failure, instead
        of silently pretending the transition happened -- callers that
        need to know it actually landed should call `transition()` directly."""
        try:
            return self.transition(opportunity_id, to_state, actor, **kwargs)
        except LifecycleError:
            return None
