"""TTT Autonomous Revenue-to-Delivery Loop v1 -- Sales Execution.

Sections 7 (Negotiation Agent guardrails), 8 (Closing Agent), and the
minimal Client record groundwork Section 9 (Onboarding) builds on next.

Design choices, matched to the existing codebase's own conventions:

  * `SalesPolicyStore` is the exact settings-row pattern
    `opportunity_agent.AcquisitionProfileStore` already uses (a single row
    in the existing `rh_settings` table, merged with defaults on read) --
    not a new table, not hard-coded constants. This is what Section 7 means
    by "persistent settings."
  * `evaluate_terms`/`NegotiationGuardrails` never decide anything on their
    own. They check proposed terms against the saved policy and report
    which ones are within bounds; anything outside bounds is escalated to
    Needs Aryan (`negotiation_response_approval`, already a defined kind in
    ttt_hq.NEEDS_ARYAN_KINDS -- no new kind needed) rather than silently
    accepted, exactly as Section 7 requires ("must never silently accept
    unfavorable terms").
  * `ClientStore.upsert` is the one place a `clients` row is created --
    idempotent by name, so closing the same client's opportunity twice
    never creates two client records.
  * `ClosingService.close` is the Closing Agent (Section 8): it records the
    structured terms into `rh_closing_records`, marks the opportunity Won
    through the existing, tested `OpportunityStore.mark_won`, upserts the
    Client and credits it with the won value, and creates the Active Job
    through the existing, tested `ActiveJobStore.create_from_won_opportunity`
    (which is itself already idempotent). `close()` is idempotent at its own
    level too: calling it again for an opportunity that already has a
    closing record returns that same record rather than creating a second
    one -- "Ensure idempotency" (Section 8) holds at every layer this
    touches, not just the one method spec called it out on.
  * Lifecycle transitions (WON, ONBOARDING) are recorded with
    `try_transition` (best effort, never raises): closing a deal is a real
    business action built on already-tested primitives
    (mark_won/create_from_won_opportunity), so the same "observability,
    never breaks the action it's attached to" contract that governs the
    hooks inside qualify()/generate()/apply() governs this too.
"""

import json
from typing import Any, Dict, List, Optional

from .audit import AuditLog
from .lifecycle import LifecycleOrchestrator
from .revenue_hunter import ActiveJobStore, OpportunityStore
from .store import StateStore, utcnow

DEFAULT_SALES_POLICY: Dict[str, Any] = {
    "min_project_price": None,
    "recommended_price_range": None,  # [low, high]
    "max_discount_pct": 0,
    "min_upfront_payment_pct": 0,
    "allowed_payment_terms": [],
    "max_free_revisions": 0,
    "min_timeline_days": None,
    "scope_change_requires_approval": True,
    "allowed_currencies": ["USD"],
    # Commercial safety default (v1 continuation instruction): an
    # unconfigured policy must never be silently read as "no limits, so
    # anything is approved automatically." `configured` starts False and is
    # only ever set True by an explicit SalesPolicyStore.save() call -- i.e.
    # Aryan (or a test's own deliberate setup) actually decided the policy
    # is ready, even if some individual limits are still left permissive.
    # ClosingService checks this flag, not the individual limit values, so
    # an empty policy can never be mistaken for unrestricted authority.
    "configured": False,
}


class SalesPolicyStore:
    """A single persisted, editable settings row -- same pattern as
    AcquisitionProfileStore. Reading always returns the full default shape
    merged with whatever has actually been saved."""

    KEY = "sales_policy"

    def __init__(self, store: StateStore):
        self.store = store

    def get(self) -> Dict[str, Any]:
        rows = self.store.list("rh_settings", "key=?", (self.KEY,))
        if not rows:
            return json.loads(json.dumps(DEFAULT_SALES_POLICY))
        saved = json.loads(rows[-1]["value_json"])
        merged = json.loads(json.dumps(DEFAULT_SALES_POLICY))
        merged.update(saved)
        return merged

    def save(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        merged = self.get()
        merged.update({k: v for k, v in (updates or {}).items() if k in DEFAULT_SALES_POLICY})
        # Calling save() at all is the deliberate "policy is set up now"
        # moment -- even a save that leaves every individual limit at its
        # permissive default still means a real person (or a test's own
        # explicit setup) decided that. This is the one and only way
        # `configured` ever becomes True; there is no path that flips it on
        # by omission.
        merged["configured"] = True
        rows = self.store.list("rh_settings", "key=?", (self.KEY,))
        now = utcnow()
        if rows:
            self.store.update("rh_settings", rows[-1]["id"], value_json=json.dumps(merged))
        else:
            self.store.create("rh_settings", {"key": self.KEY, "value_json": json.dumps(merged), "created_at": now, "updated_at": now})
        return merged


def evaluate_terms(
    policy: Dict[str, Any], *, price: Optional[float] = None, currency: Optional[str] = None,
    discount_pct: Optional[float] = None, upfront_payment_pct: Optional[float] = None,
    payment_terms: Optional[str] = None, free_revisions: Optional[int] = None,
    timeline_days: Optional[int] = None,
) -> Dict[str, Any]:
    """Pure function: checks whichever proposed terms are given against the
    policy and returns every violation found (never just the first), so a
    Needs Aryan item can show the whole picture at once. A term that is
    None (not being proposed/changed) or a policy limit that is None/empty
    (not configured) is simply not checked -- absence is not a violation."""
    violations: List[str] = []
    if price is not None and policy.get("min_project_price") is not None and price < policy["min_project_price"]:
        violations.append(f"price {price} is below the policy minimum {policy['min_project_price']}")
    if currency is not None and policy.get("allowed_currencies") and currency not in policy["allowed_currencies"]:
        violations.append(f"currency {currency!r} is not in the allowed list {policy['allowed_currencies']}")
    if discount_pct is not None and policy.get("max_discount_pct") is not None and discount_pct > policy["max_discount_pct"]:
        violations.append(f"discount {discount_pct}% exceeds the policy maximum {policy['max_discount_pct']}%")
    if upfront_payment_pct is not None and policy.get("min_upfront_payment_pct") is not None and upfront_payment_pct < policy["min_upfront_payment_pct"]:
        violations.append(f"upfront payment {upfront_payment_pct}% is below the policy minimum {policy['min_upfront_payment_pct']}%")
    if payment_terms is not None and policy.get("allowed_payment_terms") and payment_terms not in policy["allowed_payment_terms"]:
        violations.append(f"payment terms {payment_terms!r} are not in the allowed list {policy['allowed_payment_terms']}")
    if free_revisions is not None and policy.get("max_free_revisions") is not None and free_revisions > policy["max_free_revisions"]:
        violations.append(f"{free_revisions} free revisions exceeds the policy maximum {policy['max_free_revisions']}")
    if timeline_days is not None and policy.get("min_timeline_days") is not None and timeline_days < policy["min_timeline_days"]:
        violations.append(f"timeline of {timeline_days} days is shorter than the policy minimum {policy['min_timeline_days']} days")
    return {"within_policy": not violations, "violations": violations}


class NegotiationGuardrails:
    """Section 7: evaluates proposed terms against the persisted
    SalesPolicyStore and escalates anything outside those boundaries to
    Needs Aryan rather than accepting it. Never negotiates or replies
    itself -- that is the Reply/Conversation Agent's job (Section 6,
    deferred to a later pass); this is purely the boundary check every
    accept/counter decision must pass through first."""

    def __init__(self, store: StateStore, audit: AuditLog, needs_aryan=None, policy_store: Optional[SalesPolicyStore] = None):
        self.store = store
        self.audit = audit
        self.needs_aryan = needs_aryan
        self.policy_store = policy_store or SalesPolicyStore(store)

    def evaluate(self, opportunity_id: str, actor: str, **terms: Any) -> Dict[str, Any]:
        opportunity = self.store.get("rh_opportunities", opportunity_id)
        if not opportunity:
            raise ValueError("opportunity not found")
        policy = self.policy_store.get()
        result = evaluate_terms(policy, **terms)
        self.audit.append("RH_NEGOTIATION_TERMS_EVALUATED", {
            "opportunity_id": opportunity_id, "actor": actor, "terms": terms, "within_policy": result["within_policy"],
        })
        result["needs_aryan_id"] = None
        if not result["within_policy"] and self.needs_aryan is not None:
            result["needs_aryan_id"] = self.needs_aryan.create_item(
                "negotiation_response_approval",
                f"Negotiation terms outside policy: {opportunity['title']}",
                "Proposed terms fall outside the saved sales policy boundaries -- "
                "review and approve, reject, or counter before anything is agreed to.",
                actor=actor, rationale="; ".join(result["violations"]), risk="terms outside policy",
                ref_type="rh_opportunity", ref_id=opportunity_id,
            )
        # Negotiation history/evidence (Section 7): every evaluation is a
        # permanent row here, not just an audit-log line, so the full
        # back-and-forth on a deal's terms is its own queryable history.
        self.store.create("rh_negotiation_terms", {
            "opportunity_id": opportunity_id, "actor": actor,
            "price": terms.get("price"), "currency": terms.get("currency"),
            "discount_pct": terms.get("discount_pct"), "upfront_payment_pct": terms.get("upfront_payment_pct"),
            "payment_terms": terms.get("payment_terms"), "free_revisions": terms.get("free_revisions"),
            "timeline_days": terms.get("timeline_days"), "within_policy": 1 if result["within_policy"] else 0,
            "violations_json": json.dumps(result["violations"]), "needs_aryan_id": result["needs_aryan_id"],
            "created_at": utcnow(),
        })
        return result

    def history(self, opportunity_id: str) -> List[Dict[str, Any]]:
        return self.store.list("rh_negotiation_terms", "opportunity_id=?", (opportunity_id,))


class ClientError(ValueError):
    pass


class ClientStore:
    """The real `clients` table (Section 8/9), replacing the earlier
    client-name-grouping-only view with an actual persisted record per
    client. `upsert` is idempotent by exact name match -- closing several
    opportunities for the same client accumulates onto one row rather than
    creating a new client every time."""

    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit

    def upsert(self, name: str, actor: str, primary_contact: Optional[str] = None, contact_channel: Optional[str] = None) -> str:
        name = (name or "").strip()
        if not name:
            raise ClientError("name is required")
        existing = self.store.list("clients", "name=?", (name,))
        if existing:
            client_id = existing[-1]["id"]
            changes = {}
            if primary_contact and not existing[-1].get("primary_contact"):
                changes["primary_contact"] = primary_contact
            if contact_channel and not existing[-1].get("contact_channel"):
                changes["contact_channel"] = contact_channel
            if changes:
                self.store.update("clients", client_id, **changes)
            return client_id
        now = utcnow()
        client_id = self.store.create("clients", {
            "name": name, "primary_contact": primary_contact, "contact_channel": contact_channel,
            "status": "ACTIVE", "total_won_value": 0.0, "created_at": now, "updated_at": now,
        })
        self.audit.append("RH_CLIENT_CREATED", {"client_id": client_id, "name": name, "actor": actor})
        return client_id

    def record_won_value(self, client_id: str, amount: float, actor: str) -> Dict[str, Any]:
        client = self.store.get("clients", client_id)
        if not client:
            raise ClientError("client not found")
        self.store.update("clients", client_id, total_won_value=(client["total_won_value"] or 0.0) + amount)
        self.audit.append("RH_CLIENT_WON_VALUE_RECORDED", {"client_id": client_id, "amount": amount, "actor": actor})
        return self.store.get("clients", client_id)

    def get(self, client_id: str) -> Optional[Dict[str, Any]]:
        return self.store.get("clients", client_id)

    def list(self) -> List[Dict[str, Any]]:
        return list(reversed(self.store.list("clients")))


class ClosingError(ValueError):
    pass


class ClosingService:
    """Section 8: the Closing Agent. Records the agreed terms, marks the
    opportunity Won, creates/updates the Client, and creates the Active Job
    -- composing existing, tested primitives (OpportunityStore.mark_won,
    ActiveJobStore.create_from_won_opportunity) rather than duplicating
    their logic, per the "extend, don't rebuild" instruction.

    Commercial safety default: `close()` only executes a real, binding
    close immediately when SalesPolicyStore reports `configured: True` --
    i.e. Aryan (or a test's own deliberate setup) has actually saved real
    limits. Until then, `close()` never marks anything Won, never touches a
    Client, never creates an Active Job: it prepares a Closing Package (the
    exact terms it would otherwise have applied, serialized whole) and asks
    for a Needs Aryan decision, same as an out-of-policy negotiation would.
    `finalize_pending_closing()` performs the real close once that item is
    approved. An unconfigured policy is never read as unlimited authority."""

    def __init__(
        self, store: StateStore, audit: AuditLog, orchestrator: Optional[LifecycleOrchestrator] = None,
        active_jobs: Optional[ActiveJobStore] = None, clients: Optional[ClientStore] = None,
        needs_aryan=None, policy_store: Optional[SalesPolicyStore] = None,
    ):
        self.store = store
        self.audit = audit
        self.orchestrator = orchestrator or LifecycleOrchestrator(store, audit)
        self.opportunities = OpportunityStore(store, audit)
        self.active_jobs = active_jobs or ActiveJobStore(store, audit)
        self.clients = clients or ClientStore(store, audit)
        self.needs_aryan = needs_aryan
        self.policy_store = policy_store or SalesPolicyStore(store)

    def close(
        self, opportunity_id: str, actor: str, *, client_name: str, final_scope: Optional[str] = None,
        final_price: Optional[float] = None, currency: Optional[str] = None, payment_terms: Optional[str] = None,
        milestones: Optional[List[Dict[str, Any]]] = None, deadline: Optional[str] = None,
        deliverables: Optional[str] = None, acceptance_criteria: Optional[str] = None,
        communication_channel: Optional[str] = None,
    ) -> Dict[str, Any]:
        opportunity = self.store.get("rh_opportunities", opportunity_id)
        if not opportunity:
            raise ClosingError("opportunity not found")
        if not client_name or not client_name.strip():
            raise ClosingError("client_name is required")

        # Idempotency: a second close() call for an opportunity that was
        # already closed returns the existing record rather than creating a
        # duplicate closing record, a duplicate client credit, or (via
        # create_from_won_opportunity's own idempotency) a duplicate job.
        existing = self.store.list("rh_closing_records", "opportunity_id=?", (opportunity_id,))
        if existing:
            record = existing[-1]
            active_job_id = self.active_jobs.create_from_won_opportunity(opportunity_id, actor)
            return {
                "closing_record_id": record["id"], "client_id": record["client_id"],
                "active_job_id": active_job_id, "already_closed": True, "status": "CLOSED",
            }

        payload = {
            "client_name": client_name.strip(), "final_scope": final_scope, "final_price": final_price,
            "currency": currency, "payment_terms": payment_terms, "milestones": milestones, "deadline": deadline,
            "deliverables": deliverables, "acceptance_criteria": acceptance_criteria,
            "communication_channel": communication_channel,
        }

        policy = self.policy_store.get()
        if not policy.get("configured"):
            return self._prepare_closing_package(opportunity, opportunity_id, actor, payload)

        return self._execute_close(opportunity_id, actor, **payload)

    def _prepare_closing_package(self, opportunity: Dict[str, Any], opportunity_id: str, actor: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        # A second close() call while one closing package is already
        # pending must not create a duplicate Needs Aryan item -- return
        # the existing pending one instead.
        pending = self.store.list("needs_aryan_items", "ref_type=? AND ref_id=? AND status=?", ("rh_closing_package", opportunity_id, "PENDING"))
        if pending:
            return {
                "status": "AWAITING_APPROVAL", "needs_aryan_id": pending[-1]["id"],
                "opportunity_id": opportunity_id, "already_closed": False,
            }
        needs_aryan_id = None
        if self.needs_aryan is not None:
            needs_aryan_id = self.needs_aryan.create_item(
                "pricing_decision", f"Final commercial acceptance needed: {opportunity['title']}",
                "The Sales Policy has no configured commercial limits yet, so final binding "
                "acceptance stays with you by default. Review the prepared closing package below "
                "and approve, reject, or request changes before the deal is actually closed.",
                actor=actor, rationale=f"final_price={payload.get('final_price')} {payload.get('currency') or ''}".strip(),
                ref_type="rh_closing_package", ref_id=opportunity_id,
                expected_value=str(payload["final_price"]) if payload.get("final_price") is not None else None,
                payload_json=json.dumps(payload),
            )
        self.audit.append("RH_CLOSING_PACKAGE_PREPARED", {
            "opportunity_id": opportunity_id, "needs_aryan_id": needs_aryan_id, "actor": actor,
        })
        return {
            "status": "AWAITING_APPROVAL", "needs_aryan_id": needs_aryan_id,
            "opportunity_id": opportunity_id, "already_closed": False,
        }

    def finalize_pending_closing(self, needs_aryan_id: str, actor: str) -> Dict[str, Any]:
        """Performs the real close for a Closing Package Needs Aryan item
        that has just been approved. Called from the same decision route
        that already handles proposal approvals -- never invoked on its
        own initiative."""
        item = self.store.get("needs_aryan_items", needs_aryan_id)
        if not item:
            raise ClosingError("needs aryan item not found")
        if item.get("ref_type") != "rh_closing_package":
            raise ClosingError("this needs aryan item is not a closing package")
        if item.get("status") != "APPROVED":
            raise ClosingError("closing package has not been approved")
        opportunity_id = item["ref_id"]
        existing = self.store.list("rh_closing_records", "opportunity_id=?", (opportunity_id,))
        if existing:
            record = existing[-1]
            active_job_id = self.active_jobs.create_from_won_opportunity(opportunity_id, actor)
            return {
                "closing_record_id": record["id"], "client_id": record["client_id"],
                "active_job_id": active_job_id, "already_closed": True, "status": "CLOSED",
            }
        payload = json.loads(item["payload_json"])
        return self._execute_close(opportunity_id, actor, **payload)

    def _execute_close(
        self, opportunity_id: str, actor: str, *, client_name: str, final_scope: Optional[str] = None,
        final_price: Optional[float] = None, currency: Optional[str] = None, payment_terms: Optional[str] = None,
        milestones: Optional[List[Dict[str, Any]]] = None, deadline: Optional[str] = None,
        deliverables: Optional[str] = None, acceptance_criteria: Optional[str] = None,
        communication_channel: Optional[str] = None,
    ) -> Dict[str, Any]:
        client_id = self.clients.upsert(client_name.strip(), actor)
        now = utcnow()
        closing_record_id = self.store.create("rh_closing_records", {
            "opportunity_id": opportunity_id, "client_id": client_id, "final_scope": final_scope,
            "final_price": final_price, "currency": currency, "payment_terms": payment_terms,
            "milestones_json": json.dumps(milestones) if milestones is not None else None,
            "deadline": deadline, "deliverables": deliverables, "acceptance_criteria": acceptance_criteria,
            "communication_channel": communication_channel, "actor": actor, "created_at": now,
        })
        self.audit.append("RH_OPPORTUNITY_CLOSED", {
            "opportunity_id": opportunity_id, "closing_record_id": closing_record_id, "client_id": client_id,
            "final_price": final_price, "currency": currency, "actor": actor,
        })

        opportunity = self.store.get("rh_opportunities", opportunity_id)
        if opportunity["stage"] != "Won":
            self.opportunities.mark_won(opportunity_id, actor, final_price=final_price, note="deal closed")
        self.orchestrator.try_transition(
            opportunity_id, "WON", actor=actor, reason="deal closed",
            money_impact={"final_price": final_price, "currency": currency} if final_price is not None else None,
        )

        if final_price:
            self.clients.record_won_value(client_id, final_price, actor)

        active_job_id = self.active_jobs.create_from_won_opportunity(opportunity_id, actor)
        self.orchestrator.try_transition(opportunity_id, "ONBOARDING", actor=actor, reason="active job created, onboarding started")

        return {
            "closing_record_id": closing_record_id, "client_id": client_id, "active_job_id": active_job_id,
            "already_closed": False, "status": "CLOSED",
        }
