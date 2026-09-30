"""Phase 5 Sprint 1 -- Commercial Operating Foundation (Sell -> Accept ->
Deliver -> Collect -> Retain).

This module adds the internal commercial machinery identified as the
Phase 4 acceptance report's genuine gap and the Phase 5 Sprint 1
instruction's five priorities:

  1. ServiceCatalogStore  -- cs_services: a real, internal sellable
     service catalogue (not the public marketing copy in site_services).
  2. IntakeStore           -- cs_intakes / cs_intake_events: customer
     intake -> structured requirements -> qualification -> (human-approved)
     conversion into a real rh_opportunities record, reusing the existing
     Revenue & Delivery Engine rather than building a parallel one.
  3. FoundationStore       -- cs_foundations: a registry of reusable
     product foundations/components, honestly labelled by maturity.
  4. ProjectStore          -- cs_projects / cs_project_events: the
     Sell->Deliver backbone linking an intake, a service, an optional
     foundation, and the opportunity/invoice trail together.
  5. DisputeStore          -- cs_disputes / cs_dispute_events: the refund
     and dispute-handling foundation the Phase 4 report flagged as
     missing. State-management only -- no external financial transfer is
     ever executed here. Never mutates `rh_invoices.amount_received`
     (billing.py's ledger of cash actually received stays authoritative
     and untouched); an approved refund is recorded on the dispute row
     itself and subtracted when computing net collected revenue and
     (where a partner referral exists) partner commission, via the
     existing, already-idempotent `CommissionStore.record_refund`.
  6. CostEntryStore / economics_for_project -- cs_project_costs and a
     read-only rollup: quoted/contracted/invoiced/collected/refunds/net
     collected/commission/model-API cost/infra/human/other-direct cost/
     gross contribution. Unknown costs are returned as `None`, never
     defaulted to 0 -- a 0 would claim "no cost" when the truth is
     "not recorded".
  7. recommend_route -- the evidence-based FALGUNA commercial routing
     function (Section 14): recommends, never binds. A human always
     approves before any commitment becomes binding.

Every store here follows the same shape as `falguna/billing.py` and
`falguna/partner_management.py`: a thin wrapper over `StateStore`, an
`AuditLog` entry per mutation, a `*_events` history table for every
status-carrying entity, and `ValueError` subclasses for invalid
transitions rather than silent coercion.
"""

import json
from typing import Any, Dict, List, Optional

from .audit import AuditLog
from .store import StateStore, utcnow

SERVICE_APPROVAL_STATUSES = {"DRAFT", "APPROVED", "RETIRED"}
FOUNDATION_MATURITIES = {"PRODUCTION_READY", "PROTOTYPE", "INTERNAL", "NEEDS_HARDENING"}
INTAKE_QUALIFICATION_STATUSES = {"NEW", "QUALIFYING", "QUALIFIED", "DISQUALIFIED", "CONVERTED"}
DELIVERY_ROUTES = {"USE_FOUNDATION", "CUSTOMIZE", "CUSTOM_BUILD", "ESCALATE", "DECLINE"}
PROJECT_STATUSES = {"SCOPED", "IN_DELIVERY", "QA", "HANDED_OVER", "INVOICED", "CLOSED"}
DISPUTE_STATUSES = {
    "OPEN", "UNDER_REVIEW", "RESOLVED_NO_REFUND",
    "PARTIAL_REFUND_APPROVED", "FULL_REFUND_APPROVED", "CLOSED",
}
_DISPUTE_TERMINAL = {"CLOSED"}
_DISPUTE_RESOLVED = {"RESOLVED_NO_REFUND", "PARTIAL_REFUND_APPROVED", "FULL_REFUND_APPROVED"}
COST_CATEGORIES = {"MODEL_API", "INFRASTRUCTURE", "HUMAN_EXPERT", "OTHER_DIRECT"}


class CommercialError(ValueError):
    pass


# ---------------------------------------------------------------------------
# 1. Sellable Service Catalogue
# ---------------------------------------------------------------------------

class ServiceCatalogStore:
    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit

    def create(
        self, service_key: str, category: str, title: str, customer_description: str,
        pricing_model: str, delivery_mode: str, actor: str = "Aryan",
        scope_boundaries: Optional[str] = None, supported_regions: Optional[List[str]] = None,
        price_min: Optional[float] = None, price_max: Optional[float] = None, currency: str = "USD",
        required_specialists: Optional[List[str]] = None, automation_eligible: bool = False,
        foundation_id: Optional[str] = None,
    ) -> str:
        service_key = (service_key or "").strip()
        title = (title or "").strip()
        if not service_key or not title:
            raise CommercialError("service_key and title are required")
        if not (customer_description or "").strip():
            raise CommercialError("a customer-facing description is required -- we do not publish services we cannot describe honestly")
        existing = self.store.list("cs_services", "service_key=?", (service_key,))
        if existing:
            raise CommercialError(f"service_key {service_key!r} already exists")
        now = utcnow()
        service_id = self.store.create("cs_services", {
            "service_key": service_key, "category": category, "title": title,
            "customer_description": customer_description, "scope_boundaries": scope_boundaries,
            "supported_regions_json": json.dumps(supported_regions or []),
            "pricing_model": pricing_model, "price_min": price_min, "price_max": price_max, "currency": currency,
            "delivery_mode": delivery_mode, "required_specialists_json": json.dumps(required_specialists or []),
            "automation_eligible": 1 if automation_eligible else 0, "foundation_id": foundation_id,
            "approval_status": "DRAFT", "active": 0,
            "actor": actor, "created_at": now, "updated_at": now,
        })
        self.audit.append("CS_SERVICE_CREATED", {"service_id": service_id, "service_key": service_key, "actor": actor})
        return service_id

    def set_approval(self, service_id: str, actor: str, approval_status: str) -> Dict[str, Any]:
        if approval_status not in SERVICE_APPROVAL_STATUSES:
            raise CommercialError(f"unknown approval_status: {approval_status!r}")
        service = self.store.get("cs_services", service_id)
        if not service:
            raise CommercialError("service not found")
        active = 1 if approval_status == "APPROVED" else (0 if approval_status == "RETIRED" else service["active"])
        self.store.update("cs_services", service_id, approval_status=approval_status, active=active)
        self.audit.append("CS_SERVICE_APPROVAL_SET", {"service_id": service_id, "approval_status": approval_status, "actor": actor})
        return self.store.get("cs_services", service_id)

    def get(self, service_id: str) -> Optional[Dict[str, Any]]:
        return self.store.get("cs_services", service_id)

    def list(self, active_only: bool = False, category: Optional[str] = None) -> List[Dict[str, Any]]:
        where, params = "1=1", []
        if active_only:
            where += " AND active=1 AND approval_status='APPROVED'"
        if category:
            where += " AND category=?"
            params.append(category)
        return list(reversed(self.store.list("cs_services", where, params)))


# ---------------------------------------------------------------------------
# 3. Product Foundation Registry
# ---------------------------------------------------------------------------

class FoundationStore:
    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit

    def register(
        self, name: str, category: str, maturity: str, actor: str = "Aryan",
        description: Optional[str] = None, repository_ref: Optional[str] = None,
        supported_versions: Optional[str] = None, service_categories: Optional[List[str]] = None,
        qa_requirements: Optional[str] = None, known_limitations: Optional[str] = None,
        reuse_restrictions: Optional[str] = None,
    ) -> str:
        if maturity not in FOUNDATION_MATURITIES:
            raise CommercialError(f"unknown maturity: {maturity!r} (expected one of {sorted(FOUNDATION_MATURITIES)})")
        if not (name or "").strip():
            raise CommercialError("name is required")
        now = utcnow()
        foundation_id = self.store.create("cs_foundations", {
            "name": name.strip(), "category": category, "description": description,
            "repository_ref": repository_ref, "supported_versions": supported_versions,
            "service_categories_json": json.dumps(service_categories or []),
            "qa_requirements": qa_requirements, "known_limitations": known_limitations,
            "reuse_restrictions": reuse_restrictions, "maturity": maturity,
            "actor": actor, "created_at": now, "updated_at": now,
        })
        self.audit.append("CS_FOUNDATION_REGISTERED", {"foundation_id": foundation_id, "name": name, "maturity": maturity, "actor": actor})
        return foundation_id

    def set_maturity(self, foundation_id: str, actor: str, maturity: str, note: Optional[str] = None) -> Dict[str, Any]:
        if maturity not in FOUNDATION_MATURITIES:
            raise CommercialError(f"unknown maturity: {maturity!r}")
        if not self.store.get("cs_foundations", foundation_id):
            raise CommercialError("foundation not found")
        self.store.update("cs_foundations", foundation_id, maturity=maturity)
        self.audit.append("CS_FOUNDATION_MATURITY_SET", {"foundation_id": foundation_id, "maturity": maturity, "note": note, "actor": actor})
        return self.store.get("cs_foundations", foundation_id)

    def get(self, foundation_id: str) -> Optional[Dict[str, Any]]:
        return self.store.get("cs_foundations", foundation_id)

    def list(self, category: Optional[str] = None, maturity: Optional[str] = None) -> List[Dict[str, Any]]:
        where, params = "1=1", []
        if category:
            where += " AND category=?"
            params.append(category)
        if maturity:
            where += " AND maturity=?"
            params.append(maturity)
        return list(reversed(self.store.list("cs_foundations", where, params)))

    def candidates_for_category(self, category: Optional[str]) -> List[Dict[str, Any]]:
        """Production-ready foundations matching a service category --
        the evidence `recommend_route` reasons over. A foundation that
        is PROTOTYPE/INTERNAL/NEEDS_HARDENING is never offered as a
        reuse candidate to a real customer commitment."""
        if not category:
            return []
        rows = self.store.list("cs_foundations", "maturity='PRODUCTION_READY'")
        matches = []
        for row in rows:
            cats = json.loads(row.get("service_categories_json") or "[]")
            if category in cats or row.get("category") == category:
                matches.append(row)
        return matches


# ---------------------------------------------------------------------------
# 14. FALGUNA commercial routing -- evidence-based recommendation only.
# This function never binds a customer to anything; it only proposes a
# route for a human to approve as part of IntakeStore.qualify/convert.
# ---------------------------------------------------------------------------

def recommend_route(store: StateStore, service: Optional[Dict[str, Any]], complexity: Optional[str]) -> Dict[str, Any]:
    """Evidence-based routing recommendation. Returns
    {route, rationale, foundation_candidates}. `route` is one of
    DELIVERY_ROUTES. Never returns a route without stating the evidence
    it used, so a human reviewer can see exactly why."""
    category = service.get("category") if service else None
    candidates = FoundationStore(store, _NullAudit()).candidates_for_category(category)
    if not service:
        return {"route": "ESCALATE", "rationale": "No catalogue service matched this intake; a human must scope this manually.", "foundation_candidates": []}
    if service.get("approval_status") != "APPROVED" or not service.get("active"):
        return {"route": "DECLINE", "rationale": f"Service {service.get('service_key')!r} is not an approved, active offering -- we do not commit to selling it yet.", "foundation_candidates": []}
    if candidates:
        names = ", ".join(c["name"] for c in candidates[:3])
        if (complexity or "").lower() in ("low", "standard", ""):
            return {"route": "USE_FOUNDATION", "rationale": f"Production-ready foundation(s) matched this service category ({names}) and requested complexity is standard.", "foundation_candidates": candidates}
        return {"route": "CUSTOMIZE", "rationale": f"Production-ready foundation(s) matched ({names}), but requested complexity ({complexity}) needs bounded customization beyond the foundation's default scope.", "foundation_candidates": candidates}
    if service.get("automation_eligible"):
        return {"route": "CUSTOM_BUILD", "rationale": "No matching production-ready foundation exists yet, but this service category is automation-eligible for a bounded custom build.", "foundation_candidates": []}
    return {"route": "ESCALATE", "rationale": "No matching production-ready foundation and this service is not automation-eligible -- needs a human specialist to scope delivery.", "foundation_candidates": []}


class _NullAudit:
    """Read-only helper path: `recommend_route` only ever calls
    FoundationStore.candidates_for_category (a pure read), so it never
    needs a working AuditLog. This stub exists only so the constructor
    signature does not have to special-case a read-only caller."""
    def append(self, *_args, **_kwargs):
        return None


# ---------------------------------------------------------------------------
# 2. Customer Intake & Qualification
# ---------------------------------------------------------------------------

class IntakeStore:
    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit

    def _record_event(self, intake_id: str, from_status, to_status: str, actor: str, reason: Optional[str] = None) -> None:
        self.store.create("cs_intake_events", {
            "intake_id": intake_id, "from_status": from_status, "to_status": to_status,
            "actor": actor, "reason": reason, "created_at": utcnow(),
        })

    def create(
        self, customer_name: str, requested_outcome: str, actor: str = "Aryan",
        customer_contact: Optional[str] = None, business_name: Optional[str] = None, industry: Optional[str] = None,
        region: Optional[str] = None, country: Optional[str] = None, service_id: Optional[str] = None,
        requirements: Optional[str] = None, timeline: Optional[str] = None,
        budget_amount: Optional[float] = None, budget_currency: Optional[str] = None,
        regulatory_flags: Optional[List[str]] = None, required_integrations: Optional[List[str]] = None,
        assets_provided: Optional[List[str]] = None, complexity: Optional[str] = None,
    ) -> str:
        customer_name = (customer_name or "").strip()
        requested_outcome = (requested_outcome or "").strip()
        if not customer_name or not requested_outcome:
            raise CommercialError("customer_name and requested_outcome are required")
        if service_id and not self.store.get("cs_services", service_id):
            raise CommercialError("service not found")
        now = utcnow()
        intake_id = self.store.create("cs_intakes", {
            "customer_name": customer_name, "customer_contact": customer_contact, "business_name": business_name,
            "industry": industry, "region": region, "country": country, "requested_outcome": requested_outcome,
            "service_id": service_id, "requirements": requirements, "timeline": timeline,
            "budget_amount": budget_amount, "budget_currency": budget_currency,
            "regulatory_flags_json": json.dumps(regulatory_flags or []),
            "required_integrations_json": json.dumps(required_integrations or []),
            "assets_provided_json": json.dumps(assets_provided or []),
            "complexity": complexity, "qualification_status": "NEW", "potential_value": None, "risks": None,
            "recommended_route": None, "ai_requirement_summary": None, "ai_missing_questions_json": None,
            "ai_proposed_scope": None, "human_approved": 0, "approved_by": None, "approved_at": None,
            "opportunity_id": None, "actor": actor, "created_at": now, "updated_at": now,
        })
        self._record_event(intake_id, None, "NEW", actor, "intake created")
        self.audit.append("CS_INTAKE_CREATED", {"intake_id": intake_id, "customer_name": customer_name, "actor": actor})
        return intake_id

    def ai_draft(
        self, intake_id: str, actor: str, requirement_summary: str,
        missing_questions: Optional[List[str]] = None, proposed_scope: Optional[str] = None,
    ) -> Dict[str, Any]:
        """FALGUNA's drafting step (Section 9): a requirement summary,
        missing-information questions, and a proposed scope. This is
        advisory only -- it never changes qualification_status or
        opportunity_id, and a human still has to call `approve` before
        `convert_to_project` will allow a binding opportunity to exist."""
        if not self.store.get("cs_intakes", intake_id):
            raise CommercialError("intake not found")
        self.store.update(
            "cs_intakes", intake_id, ai_requirement_summary=requirement_summary,
            ai_missing_questions_json=json.dumps(missing_questions or []), ai_proposed_scope=proposed_scope,
        )
        self.audit.append("CS_INTAKE_AI_DRAFTED", {"intake_id": intake_id, "actor": actor})
        return self.store.get("cs_intakes", intake_id)

    def qualify(
        self, intake_id: str, actor: str, qualification_status: str,
        potential_value: Optional[float] = None, risks: Optional[str] = None,
        recommended_route: Optional[str] = None, reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        if qualification_status not in INTAKE_QUALIFICATION_STATUSES:
            raise CommercialError(f"unknown qualification_status: {qualification_status!r}")
        intake = self.store.get("cs_intakes", intake_id)
        if not intake:
            raise CommercialError("intake not found")
        if intake["qualification_status"] == "CONVERTED":
            raise CommercialError("intake is already CONVERTED and cannot be re-qualified")
        if recommended_route and recommended_route not in DELIVERY_ROUTES:
            raise CommercialError(f"unknown recommended_route: {recommended_route!r}")
        changes: Dict[str, Any] = {"qualification_status": qualification_status}
        if potential_value is not None:
            changes["potential_value"] = potential_value
        if risks is not None:
            changes["risks"] = risks
        if recommended_route is not None:
            changes["recommended_route"] = recommended_route
        self.store.update("cs_intakes", intake_id, **changes)
        self._record_event(intake_id, intake["qualification_status"], qualification_status, actor, reason)
        self.audit.append("CS_INTAKE_QUALIFIED", {"intake_id": intake_id, "qualification_status": qualification_status, "actor": actor})
        return self.store.get("cs_intakes", intake_id)

    def approve(self, intake_id: str, actor: str) -> Dict[str, Any]:
        """The human-approval gate the Sprint 1 instruction requires:
        'Human approval remains required before binding quotation/
        commitment.' `convert_to_project` refuses to run without this."""
        intake = self.store.get("cs_intakes", intake_id)
        if not intake:
            raise CommercialError("intake not found")
        if intake["qualification_status"] not in ("QUALIFYING", "QUALIFIED"):
            raise CommercialError(f"intake must be QUALIFYING or QUALIFIED to approve (currently {intake['qualification_status']})")
        self.store.update("cs_intakes", intake_id, human_approved=1, approved_by=actor, approved_at=utcnow(), qualification_status="QUALIFIED")
        self._record_event(intake_id, intake["qualification_status"], "QUALIFIED", actor, "human-approved")
        self.audit.append("CS_INTAKE_APPROVED", {"intake_id": intake_id, "actor": actor})
        return self.store.get("cs_intakes", intake_id)

    def convert_to_project(self, intake_id: str, actor: str, delivery_route: str, foundation_id: Optional[str] = None) -> str:
        """Human-approved intake -> real `rh_opportunities` row (reusing
        the existing Revenue & Delivery Engine, never a parallel pipeline)
        -> `clients` row (reusing ClientStore) -> `cs_projects` row tying
        it all together. Refuses outright if the intake was never
        human-approved, or if delivery_route is DECLINE (a decline is a
        terminal outcome, not a project)."""
        from .sales_ops import ClientStore
        from .revenue_hunter import OpportunityStore

        if delivery_route not in DELIVERY_ROUTES:
            raise CommercialError(f"unknown delivery_route: {delivery_route!r}")
        if delivery_route == "DECLINE":
            raise CommercialError("DECLINE is a terminal outcome, not something to convert into a project")
        intake = self.store.get("cs_intakes", intake_id)
        if not intake:
            raise CommercialError("intake not found")
        if not intake["human_approved"]:
            raise CommercialError("intake has not been human-approved -- FALGUNA cannot convert an unapproved intake into a binding commitment")
        if intake["qualification_status"] == "CONVERTED":
            raise CommercialError("intake has already been converted")
        if foundation_id and not self.store.get("cs_foundations", foundation_id):
            raise CommercialError("foundation not found")

        client_id = ClientStore(self.store, self.audit).upsert(
            intake["customer_name"], actor, primary_contact=intake.get("customer_contact"),
        )
        opportunity_id = OpportunityStore(self.store, self.audit).create(
            {
                "title": f"{intake['requested_outcome']} -- {intake['customer_name']}",
                "client_name": intake["customer_name"],
                "description": intake.get("requirements") or intake.get("ai_proposed_scope"),
                "budget_rate": (f"{intake['budget_amount']} {intake.get('budget_currency') or ''}".strip() if intake.get("budget_amount") else None),
                "deadline": intake.get("timeline"),
                "location_timezone": intake.get("region"),
            },
            actor=actor, source="phase5_intake",
        )
        now = utcnow()
        project_id = self.store.create("cs_projects", {
            "intake_id": intake_id, "service_id": intake.get("service_id"), "opportunity_id": opportunity_id,
            "foundation_id": foundation_id, "client_id": client_id, "delivery_route": delivery_route,
            "status": "SCOPED", "mission_id": None, "actor": actor, "created_at": now, "updated_at": now,
        })
        self.store.update("cs_intakes", intake_id, qualification_status="CONVERTED", opportunity_id=opportunity_id)
        self._record_event(intake_id, intake["qualification_status"], "CONVERTED", actor, f"converted to project {project_id}")
        self.audit.append("CS_INTAKE_CONVERTED", {"intake_id": intake_id, "project_id": project_id, "opportunity_id": opportunity_id, "actor": actor})
        return project_id

    def get(self, intake_id: str) -> Optional[Dict[str, Any]]:
        return self.store.get("cs_intakes", intake_id)

    def list(self, qualification_status: Optional[str] = None) -> List[Dict[str, Any]]:
        where, params = "1=1", []
        if qualification_status:
            where += " AND qualification_status=?"
            params.append(qualification_status)
        return list(reversed(self.store.list("cs_intakes", where, params)))

    def history(self, intake_id: str) -> List[Dict[str, Any]]:
        return self.store.list("cs_intake_events", "intake_id=?", (intake_id,))


# ---------------------------------------------------------------------------
# 10. Delivery Foundation / Product Factory -- the Sell->Deliver backbone
# ---------------------------------------------------------------------------

_PROJECT_TRANSITIONS = {
    "SCOPED": {"IN_DELIVERY"},
    "IN_DELIVERY": {"QA"},
    "QA": {"HANDED_OVER", "IN_DELIVERY"},  # QA can bounce work back
    "HANDED_OVER": {"INVOICED"},
    "INVOICED": {"CLOSED"},
    "CLOSED": set(),
}


class ProjectStore:
    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit

    def _record_event(self, project_id: str, from_status, to_status: str, actor: str, reason: Optional[str] = None) -> None:
        self.store.create("cs_project_events", {
            "project_id": project_id, "from_status": from_status, "to_status": to_status,
            "actor": actor, "reason": reason, "created_at": utcnow(),
        })

    def transition(self, project_id: str, actor: str, to_status: str, reason: Optional[str] = None) -> Dict[str, Any]:
        if to_status not in PROJECT_STATUSES:
            raise CommercialError(f"unknown status: {to_status!r}")
        project = self.store.get("cs_projects", project_id)
        if not project:
            raise CommercialError("project not found")
        allowed = _PROJECT_TRANSITIONS.get(project["status"], set())
        if to_status not in allowed and to_status != project["status"]:
            raise CommercialError(f"cannot move project from {project['status']} to {to_status}")
        if to_status == project["status"]:
            return project  # idempotent no-op
        self.store.update("cs_projects", project_id, status=to_status, mission_id=project.get("mission_id"))
        self._record_event(project_id, project["status"], to_status, actor, reason)
        self.audit.append("CS_PROJECT_STATUS_CHANGED", {"project_id": project_id, "to_status": to_status, "actor": actor})
        return self.store.get("cs_projects", project_id)

    def link_mission(self, project_id: str, actor: str, mission_id: str) -> Dict[str, Any]:
        if not self.store.get("cs_projects", project_id):
            raise CommercialError("project not found")
        self.store.update("cs_projects", project_id, mission_id=mission_id)
        self.audit.append("CS_PROJECT_MISSION_LINKED", {"project_id": project_id, "mission_id": mission_id, "actor": actor})
        return self.store.get("cs_projects", project_id)

    def get(self, project_id: str) -> Optional[Dict[str, Any]]:
        return self.store.get("cs_projects", project_id)

    def list(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        where, params = "1=1", []
        if status:
            where += " AND status=?"
            params.append(status)
        return list(reversed(self.store.list("cs_projects", where, params)))

    def history(self, project_id: str) -> List[Dict[str, Any]]:
        return self.store.list("cs_project_events", "project_id=?", (project_id,))


# ---------------------------------------------------------------------------
# 11. Refund & Dispute Handling -- state-management only. Never executes
# an external financial transfer, never mutates rh_invoices.amount_received.
# ---------------------------------------------------------------------------

class DisputeStore:
    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit

    def _record_event(self, dispute_id: str, from_status, to_status: str, actor: str, reason: Optional[str] = None) -> None:
        self.store.create("cs_dispute_events", {
            "dispute_id": dispute_id, "from_status": from_status, "to_status": to_status,
            "actor": actor, "reason": reason, "created_at": utcnow(),
        })

    def open(
        self, invoice_id: str, actor: str, reason: str, amount_disputed: float, evidence: Any,
        project_id: Optional[str] = None,
    ) -> str:
        if not evidence:
            raise CommercialError("a dispute cannot be opened without evidence")
        if amount_disputed is None or amount_disputed <= 0:
            raise CommercialError("amount_disputed must be a positive number")
        invoice = self.store.get("rh_invoices", invoice_id)
        if not invoice:
            raise CommercialError("invoice not found")
        if not (reason or "").strip():
            raise CommercialError("a reason is required to open a dispute")
        now = utcnow()
        dispute_id = self.store.create("cs_disputes", {
            "invoice_id": invoice_id, "project_id": project_id, "client_id": invoice.get("client_id"),
            "reason": reason, "evidence_json": json.dumps(evidence if isinstance(evidence, (dict, list)) else {"note": str(evidence)}),
            "amount_disputed": amount_disputed, "status": "OPEN", "reviewer": None, "resolution": None,
            "refund_amount": 0.0, "commission_impact_json": None,
            "actor": actor, "created_at": now, "updated_at": now, "resolved_at": None,
        })
        self._record_event(dispute_id, None, "OPEN", actor, reason)
        self.audit.append("CS_DISPUTE_OPENED", {"dispute_id": dispute_id, "invoice_id": invoice_id, "amount_disputed": amount_disputed, "actor": actor})
        return dispute_id

    def start_review(self, dispute_id: str, actor: str, reviewer: str, note: Optional[str] = None) -> Dict[str, Any]:
        dispute = self.store.get("cs_disputes", dispute_id)
        if not dispute:
            raise CommercialError("dispute not found")
        if dispute["status"] != "OPEN":
            raise CommercialError(f"dispute must be OPEN to start review (currently {dispute['status']})")
        self.store.update("cs_disputes", dispute_id, status="UNDER_REVIEW", reviewer=reviewer)
        self._record_event(dispute_id, "OPEN", "UNDER_REVIEW", actor, note)
        self.audit.append("CS_DISPUTE_UNDER_REVIEW", {"dispute_id": dispute_id, "reviewer": reviewer, "actor": actor})
        return self.store.get("cs_disputes", dispute_id)

    def resolve(
        self, dispute_id: str, actor: str, resolution: str,
        refund_amount: Optional[float] = None, evidence: Any = None,
    ) -> Dict[str, Any]:
        """The one human-authorization gate for money leaving the books.
        `resolution` text is free-form (the human reviewer's own words);
        the *status* it produces is driven purely by whether a
        refund_amount was approved and how it compares to the invoice's
        amount_received -- never by parsing the resolution text. This
        never touches `rh_invoices.amount_received` (billing.py's ledger
        of real cash received stays untouched and inspectable); it
        records the approved refund on the dispute row, which is what
        `economics_for_project` and the commission adjustment below both
        read from."""
        dispute = self.store.get("cs_disputes", dispute_id)
        if not dispute:
            raise CommercialError("dispute not found")
        if dispute["status"] not in ("OPEN", "UNDER_REVIEW"):
            raise CommercialError(f"dispute must be OPEN or UNDER_REVIEW to resolve (currently {dispute['status']})")
        if not (resolution or "").strip():
            raise CommercialError("a resolution note is required")
        invoice = self.store.get("rh_invoices", dispute["invoice_id"])
        received = float(invoice.get("amount_received") or 0.0) if invoice else 0.0
        already_refunded = self._total_approved_refunds(dispute["invoice_id"], exclude_dispute_id=dispute_id)

        if refund_amount is None or refund_amount <= 0:
            new_status = "RESOLVED_NO_REFUND"
            approved_refund = 0.0
        else:
            if not evidence:
                raise CommercialError("an approved refund cannot be recorded without evidence")
            available = max(0.0, received - already_refunded)
            if refund_amount > available + 1e-9:
                raise CommercialError(f"refund_amount ({refund_amount}) exceeds the amount actually available to refund on this invoice ({available})")
            approved_refund = round(refund_amount, 2)
            new_status = "FULL_REFUND_APPROVED" if abs(approved_refund - available) < 1e-9 and available > 0 else "PARTIAL_REFUND_APPROVED"

        commission_impact = self._apply_commission_impact(dispute, approved_refund, actor, evidence) if approved_refund > 0 else None

        self.store.update(
            "cs_disputes", dispute_id, status=new_status, resolution=resolution, refund_amount=approved_refund,
            commission_impact_json=json.dumps(commission_impact) if commission_impact is not None else None,
            resolved_at=utcnow(),
        )
        self._record_event(dispute_id, dispute["status"], new_status, actor, resolution)
        self.audit.append("CS_DISPUTE_RESOLVED", {
            "dispute_id": dispute_id, "status": new_status, "refund_amount": approved_refund, "actor": actor,
        })
        return self.store.get("cs_disputes", dispute_id)

    def close(self, dispute_id: str, actor: str, reason: Optional[str] = None) -> Dict[str, Any]:
        dispute = self.store.get("cs_disputes", dispute_id)
        if not dispute:
            raise CommercialError("dispute not found")
        if dispute["status"] not in _DISPUTE_RESOLVED:
            raise CommercialError(f"dispute must be resolved before it can be closed (currently {dispute['status']})")
        self.store.update("cs_disputes", dispute_id, status="CLOSED")
        self._record_event(dispute_id, dispute["status"], "CLOSED", actor, reason)
        self.audit.append("CS_DISPUTE_CLOSED", {"dispute_id": dispute_id, "actor": actor})
        return self.store.get("cs_disputes", dispute_id)

    def _total_approved_refunds(self, invoice_id: str, exclude_dispute_id: Optional[str] = None) -> float:
        """Sums every dispute's approved refund_amount on this invoice,
        regardless of current status. refund_amount is only ever set to a
        positive number by an approved resolve() call (never anywhere
        else), so filtering by status here would be redundant -- and
        actively wrong once a resolved dispute is later CLOSED, since
        CLOSED is not in a status allow-list but the refund it already
        recorded is still real money that must keep counting."""
        rows = self.store.list("cs_disputes", "invoice_id=?", (invoice_id,))
        return sum(float(r["refund_amount"] or 0.0) for r in rows if r["id"] != exclude_dispute_id)

    def _apply_commission_impact(self, dispute: Dict[str, Any], approved_refund: float, actor: str, evidence: Any) -> Optional[Dict[str, Any]]:
        """If this invoice's opportunity has a partner referral attached,
        reverse the proportional commission via the existing, already-
        idempotent CommissionStore.record_refund (event_ref=dispute id,
        so replaying the same resolve() call twice never double-reverses
        commission). No referral -> no commission impact, and that
        absence is recorded explicitly rather than silently skipped."""
        invoice = self.store.get("rh_invoices", dispute["invoice_id"])
        opportunity_id = invoice.get("opportunity_id") if invoice else None
        if not opportunity_id:
            return {"partner_referral": None, "note": "invoice has no linked opportunity"}
        referrals = self.store.list("pm_referrals", "opportunity_id=?", (opportunity_id,))
        if not referrals:
            return {"partner_referral": None, "note": "no partner referral for this opportunity"}
        referral = referrals[-1]
        from .partner_management import CommissionStore
        commission_store = CommissionStore(self.store, self.audit)
        commission = commission_store.get_for_referral(referral["id"])
        if not commission:
            return {"partner_referral": referral["id"], "note": "no commission record exists for this referral"}
        try:
            updated = commission_store.record_refund(
                referral["id"], actor, approved_refund, evidence or {"dispute_id": dispute.get("id")},
                reason=f"invoice refund via dispute {dispute.get('id')}", event_ref=dispute["id"],
            )
            return {"partner_referral": referral["id"], "commission_id": updated["id"], "commission_status_after": updated["status"]}
        except Exception as exc:  # commission-side errors must not block a dispute resolution already approved by a human
            return {"partner_referral": referral["id"], "error": str(exc)}

    def get(self, dispute_id: str) -> Optional[Dict[str, Any]]:
        return self.store.get("cs_disputes", dispute_id)

    def list(self, status: Optional[str] = None, invoice_id: Optional[str] = None) -> List[Dict[str, Any]]:
        where, params = "1=1", []
        if status:
            where += " AND status=?"
            params.append(status)
        if invoice_id:
            where += " AND invoice_id=?"
            params.append(invoice_id)
        return list(reversed(self.store.list("cs_disputes", where, params)))

    def history(self, dispute_id: str) -> List[Dict[str, Any]]:
        return self.store.list("cs_dispute_events", "dispute_id=?", (dispute_id,))


# ---------------------------------------------------------------------------
# 12. Unit Economics
# ---------------------------------------------------------------------------

class CostEntryStore:
    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit

    def record(self, project_id: str, actor: str, cost_category: str, amount: float, currency: str = "USD", note: Optional[str] = None) -> str:
        if cost_category not in COST_CATEGORIES:
            raise CommercialError(f"unknown cost_category: {cost_category!r} (expected one of {sorted(COST_CATEGORIES)})")
        if not self.store.get("cs_projects", project_id):
            raise CommercialError("project not found")
        if amount is None or amount < 0:
            raise CommercialError("amount must be zero or a positive number")
        cost_id = self.store.create("cs_project_costs", {
            "project_id": project_id, "cost_category": cost_category, "amount": amount, "currency": currency,
            "note": note, "actor": actor, "created_at": utcnow(),
        })
        self.audit.append("CS_PROJECT_COST_RECORDED", {"cost_id": cost_id, "project_id": project_id, "cost_category": cost_category, "amount": amount, "actor": actor})
        return cost_id

    def list_for_project(self, project_id: str) -> List[Dict[str, Any]]:
        return self.store.list("cs_project_costs", "project_id=?", (project_id,))


def economics_for_project(store: StateStore, project_id: str) -> Dict[str, Any]:
    """Read-only rollup. Every figure that cannot be honestly derived
    from persisted data is returned as `None`, not 0 -- a 0 would claim
    'we know this cost nothing', when the truth is 'we have not recorded
    this cost yet'. Only quoted/contracted/invoiced/collected/refunds/
    net_collected/commission are ever guaranteed non-None, because those
    are always derivable once an opportunity/invoice exists (0 really
    does mean zero there, e.g. no invoice sent yet)."""
    project = store.get("cs_projects", project_id)
    if not project:
        raise CommercialError("project not found")

    opportunity_id = project.get("opportunity_id")
    opportunity = store.get("rh_opportunities", opportunity_id) if opportunity_id else None
    proposals = store.list("rh_proposals", "opportunity_id=?", (opportunity_id,)) if opportunity_id else []
    invoices = store.list("rh_invoices", "opportunity_id=?", (opportunity_id,)) if opportunity_id else []
    non_cancelled_invoices = [i for i in invoices if i["status"] != "CANCELLED"]

    quoted_value = opportunity.get("final_price") if opportunity else None
    contracted_value = None
    accepted_proposals = [p for p in proposals if p.get("status") == "approved"]
    if accepted_proposals:
        contracted_value = quoted_value  # the opportunity's own final_price is the authoritative contracted figure once a proposal is approved; we do not parse price back out of proposal prose

    invoiced_total = sum(float(i["amount"] or 0.0) for i in non_cancelled_invoices)
    collected_total = sum(float(i["amount_received"] or 0.0) for i in non_cancelled_invoices)

    invoice_ids = [i["id"] for i in invoices]
    all_disputes: List[Dict[str, Any]] = []
    for inv_id in invoice_ids:
        all_disputes.extend(store.list("cs_disputes", "invoice_id=?", (inv_id,)))
    # refund_amount is only ever set to a positive number by an approved
    # resolve() call, so summing it directly (rather than filtering by
    # status) stays correct even after a resolved dispute is later CLOSED
    # -- a refund does not stop being real money just because the dispute
    # record documenting it was archived.
    approved_refunds_total = sum(float(d["refund_amount"] or 0.0) for d in all_disputes)
    net_collected = round(collected_total - approved_refunds_total, 2)

    commission_total = 0.0
    commission_known = False
    if opportunity_id:
        referrals = store.list("pm_referrals", "opportunity_id=?", (opportunity_id,))
        for referral in referrals:
            commissions = store.list("pm_commissions", "referral_id=?", (referral["id"],))
            for commission in commissions:
                commission_known = True
                if commission["status"] in ("ELIGIBLE", "PAID", "HELD"):
                    commission_total += float(commission["eligible_amount"] or 0.0)

    cost_rows = store.list("cs_project_costs", "project_id=?", (project_id,))
    costs_by_category: Dict[str, Optional[float]] = {"MODEL_API": None, "INFRASTRUCTURE": None, "HUMAN_EXPERT": None, "OTHER_DIRECT": None}
    for category in list(costs_by_category):
        rows = [r for r in cost_rows if r["cost_category"] == category]
        if rows:
            costs_by_category[category] = round(sum(float(r["amount"] or 0.0) for r in rows), 2)

    known_costs = [v for v in costs_by_category.values() if v is not None]
    total_known_direct_cost = round(sum(known_costs), 2) if known_costs else None
    gross_contribution = None
    if total_known_direct_cost is not None:
        gross_contribution = round(net_collected - (commission_total if commission_known else 0.0) - total_known_direct_cost, 2)

    return {
        "project_id": project_id,
        "opportunity_id": opportunity_id,
        "quoted_value": quoted_value,
        "contracted_value": contracted_value,
        "invoiced_total": round(invoiced_total, 2),
        "collected_total": round(collected_total, 2),
        "refunds_total": round(approved_refunds_total, 2),
        "net_collected": net_collected,
        "partner_commission": round(commission_total, 2) if commission_known else None,
        "costs": costs_by_category,
        "total_known_direct_cost": total_known_direct_cost,
        "gross_contribution": gross_contribution,
        "currency": (invoices[0]["currency"] if invoices else None),
        "note": "Fields that are `None` have not been recorded yet -- they are unknown, not zero." if (not commission_known or total_known_direct_cost is None) else None,
    }
