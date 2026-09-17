"""TTT Autonomous Revenue-to-Delivery Loop v1 -- Client Onboarding + Delivery
Brief (Section 9, Pass C).

`OnboardingStore` manages a per-opportunity checklist of onboarding items
(contact info, requirements, repo access, hosting, domain, credentials,
brand assets, APIs, business rules, examples/references, deadline,
acceptance criteria). Each item has a status (REQUIRED/RECEIVED/MISSING/
BLOCKED) so an owner or the Account Manager (Pass D) can see exactly what's
outstanding before real delivery work starts.

Secrets handling: `credentials_access` is the one item type marked
`sensitive`. This store never accepts a raw secret into `value_text` for a
sensitive item -- `set_item` raises if a caller tries. The only thing a
sensitive item may carry is a *reference* (where the real credential lives:
"shared via 1Password vault X", "sent through the client's secure portal"),
via `notes`. This isn't a secret-detection heuristic (those are unreliable)
-- it's a hard API boundary: there is no code path in this module that ever
writes a credential value into a normal database field or into the audit
log.

`DeliveryBriefService` assembles a structured, honest Delivery Brief from
data that already exists on file (the opportunity, its qualification, the
approved proposal, the closing record, the client, and the onboarding
checklist) -- it never invents scope, price, or acceptance criteria that
weren't actually captured somewhere upstream. `enrich_active_job_payload`
then extends (never replaces) the Active Job's existing `job_payload_json`
so the real Falguna handoff (falguna/handoff.py, unchanged) carries this
richer context in its `requirement` text -- the only field Falguna's
acceptor actually reads as free text. This is additive and idempotent:
re-running it rebuilds the onboarding section from the *current* onboarding
state without duplicating or discarding the original requirement text,
which is preserved verbatim under `base_requirement`.
"""

import json
from typing import Any, Dict, List, Optional

from .audit import AuditLog
from .revenue_hunter import OpportunityStore
from .store import StateStore, utcnow

ONBOARDING_ITEM_TYPES = [
    "contact_information", "requirements", "repository_access", "hosting", "domain",
    "credentials_access", "brand_assets", "apis", "business_rules", "examples_references",
    "deadline", "acceptance_criteria",
]

SENSITIVE_ITEM_TYPES = {"credentials_access"}

ONBOARDING_STATUSES = {"REQUIRED", "RECEIVED", "MISSING", "BLOCKED"}


class OnboardingError(ValueError):
    pass


class OnboardingStore:
    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit
        self.opportunities = OpportunityStore(store, audit)

    def init_checklist(self, opportunity_id: str, actor: str = "Aryan") -> List[Dict[str, Any]]:
        """Idempotent: only creates rows for item types that don't already
        have one for this opportunity -- calling this again (e.g. after a
        schema addition, or a duplicate onboarding-start call) never
        resets or duplicates existing progress."""
        opportunity = self.store.get("rh_opportunities", opportunity_id)
        if not opportunity:
            raise OnboardingError("opportunity not found")
        existing_types = {row["item_type"] for row in self.store.list("rh_onboarding_items", "opportunity_id=?", (opportunity_id,))}
        now = utcnow()
        created = []
        for item_type in ONBOARDING_ITEM_TYPES:
            if item_type in existing_types:
                continue
            item_id = self.store.create("rh_onboarding_items", {
                "opportunity_id": opportunity_id, "item_type": item_type, "status": "REQUIRED",
                "sensitive": 1 if item_type in SENSITIVE_ITEM_TYPES else 0,
                "value_text": None, "notes": None, "actor": actor, "created_at": now, "updated_at": now,
            })
            created.append(self.store.get("rh_onboarding_items", item_id))
        if created:
            self.audit.append("RH_ONBOARDING_CHECKLIST_INITIALIZED", {
                "opportunity_id": opportunity_id, "items_created": len(created), "actor": actor,
            })
        return self.list_for_opportunity(opportunity_id)

    def set_item(
        self, opportunity_id: str, item_type: str, status: str, actor: str = "Aryan",
        value_text: Optional[str] = None, notes: Optional[str] = None,
    ) -> Dict[str, Any]:
        if item_type not in ONBOARDING_ITEM_TYPES:
            raise OnboardingError(f"unknown onboarding item_type: {item_type!r}")
        if status not in ONBOARDING_STATUSES:
            raise OnboardingError(f"unknown onboarding status: {status!r} (expected one of {sorted(ONBOARDING_STATUSES)})")
        sensitive = item_type in SENSITIVE_ITEM_TYPES
        if sensitive and value_text:
            raise OnboardingError(
                "credentials_access is a sensitive item -- it must never store a raw secret in "
                "value_text. Record where the credential actually lives instead, via notes= "
                "(e.g. 'shared through the client's secure portal')."
            )
        opportunity = self.store.get("rh_opportunities", opportunity_id)
        if not opportunity:
            raise OnboardingError("opportunity not found")
        existing = self.store.list("rh_onboarding_items", "opportunity_id=? AND item_type=?", (opportunity_id, item_type))
        now = utcnow()
        if existing:
            item_id = existing[-1]["id"]
            self.store.update("rh_onboarding_items", item_id, status=status, value_text=value_text, notes=notes, updated_at=now)
        else:
            item_id = self.store.create("rh_onboarding_items", {
                "opportunity_id": opportunity_id, "item_type": item_type, "status": status,
                "sensitive": 1 if sensitive else 0, "value_text": value_text, "notes": notes,
                "actor": actor, "created_at": now, "updated_at": now,
            })
        # Never put value_text into the audit log -- even a non-sensitive
        # item's free text could turn out to contain something the owner
        # didn't expect to see duplicated into a log file.
        self.audit.append("RH_ONBOARDING_ITEM_UPDATED", {
            "opportunity_id": opportunity_id, "item_id": item_id, "item_type": item_type,
            "status": status, "sensitive": sensitive, "actor": actor,
        })
        return self.store.get("rh_onboarding_items", item_id)

    def list_for_opportunity(self, opportunity_id: str) -> List[Dict[str, Any]]:
        return self.store.list("rh_onboarding_items", "opportunity_id=?", (opportunity_id,))

    def completion_summary(self, opportunity_id: str) -> Dict[str, Any]:
        items = self.list_for_opportunity(opportunity_id)
        received = sum(1 for i in items if i["status"] == "RECEIVED")
        missing = sum(1 for i in items if i["status"] == "MISSING")
        blocked = sum(1 for i in items if i["status"] == "BLOCKED")
        required_pending = sum(1 for i in items if i["status"] == "REQUIRED")
        return {
            "total": len(items), "received": received, "missing": missing,
            "blocked": blocked, "required_pending": required_pending,
            "complete": bool(items) and all(i["status"] == "RECEIVED" for i in items),
        }


class DeliveryBriefService:
    """Assembles a structured Delivery Brief from real, already-on-file
    data only, and extends (never replaces) the Active Job's job payload
    with it before a real Falguna handoff."""

    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit
        self.opportunities = OpportunityStore(store, audit)
        self.onboarding = OnboardingStore(store, audit)

    def _redacted_item(self, item: Dict[str, Any]) -> Dict[str, Any]:
        if item["sensitive"]:
            value = None
            if item["status"] == "RECEIVED":
                value = "[on file -- see notes for where it's stored; never shown here]"
        else:
            value = item.get("value_text")
        return {
            "item_type": item["item_type"], "status": item["status"], "value": value,
            "notes": item.get("notes"), "sensitive": bool(item["sensitive"]),
        }

    def build(self, opportunity_id: str) -> Dict[str, Any]:
        opportunity = self.opportunities.get(opportunity_id)
        if not opportunity:
            raise OnboardingError("opportunity not found")
        closing_records = self.store.list("rh_closing_records", "opportunity_id=?", (opportunity_id,))
        closing = closing_records[-1] if closing_records else None
        client = self.store.get("clients", closing["client_id"]) if closing else None
        approved_proposals = self.store.list("rh_proposals", "opportunity_id=? AND status=?", (opportunity_id, "APPROVED"))
        approved_proposal = approved_proposals[-1] if approved_proposals else None
        onboarding_items = [self._redacted_item(i) for i in self.onboarding.list_for_opportunity(opportunity_id)]
        by_type = {i["item_type"]: i for i in onboarding_items}

        final_scope = (closing or {}).get("final_scope") or (approved_proposal or {}).get("content") or opportunity.get("description")
        deliverables = (closing or {}).get("deliverables") or (approved_proposal or {}).get("content")
        acceptance_criteria = (closing or {}).get("acceptance_criteria") or (by_type.get("acceptance_criteria") or {}).get("value")
        deadline = (closing or {}).get("deadline") or opportunity.get("deadline") or (by_type.get("deadline") or {}).get("value")

        technical_constraint_types = ["repository_access", "hosting", "domain", "apis", "business_rules"]
        technical_constraints = "; ".join(
            f"{t}: {by_type[t]['value']}" for t in technical_constraint_types
            if by_type.get(t) and by_type[t]["value"]
        ) or None

        client_context_parts = []
        if client and client.get("name"):
            client_context_parts.append(f"client: {client['name']}")
        contact_item = by_type.get("contact_information")
        if contact_item and contact_item.get("value"):
            client_context_parts.append(f"contact: {contact_item['value']}")
        client_context = "; ".join(client_context_parts) or None

        return {
            "opportunity_id": opportunity_id,
            "title": opportunity["title"],
            "client_id": client["id"] if client else None,
            "client_name": client["name"] if client else opportunity.get("client_name"),
            "final_scope": final_scope,
            "deliverables": deliverables,
            "acceptance_criteria": acceptance_criteria,
            "deadline": deadline,
            "final_price": (closing or {}).get("final_price"),
            "currency": (closing or {}).get("currency"),
            "payment_terms": (closing or {}).get("payment_terms"),
            "milestones": json.loads(closing["milestones_json"]) if closing and closing.get("milestones_json") else None,
            "technical_constraints": technical_constraints,
            "client_context": client_context,
            "onboarding_items": onboarding_items,
            "onboarding_completeness": self.onboarding.completion_summary(opportunity_id),
        }

    def enrich_active_job_payload(self, active_job_id: str, actor: str = "system") -> Dict[str, Any]:
        job = self.store.get("rh_active_jobs", active_job_id)
        if not job:
            raise OnboardingError("active job not found")
        payload = json.loads(job["job_payload_json"])
        opportunity_id = job["opportunity_id"]
        brief = self.build(opportunity_id)

        # `base_requirement` is captured once, the first time this runs, so
        # repeated enrichment always rebuilds the onboarding section fresh
        # from current onboarding state rather than appending duplicate
        # sections onto an ever-growing string.
        base_requirement = payload.get("base_requirement", payload.get("requirement", ""))
        payload["base_requirement"] = base_requirement

        context_lines = []
        if brief["client_context"]:
            context_lines.append(f"Client context: {brief['client_context']}")
        if brief["technical_constraints"]:
            context_lines.append(f"Technical constraints: {brief['technical_constraints']}")
        if brief["acceptance_criteria"]:
            context_lines.append(f"Acceptance criteria: {brief['acceptance_criteria']}")
        if brief["deadline"]:
            context_lines.append(f"Deadline: {brief['deadline']}")

        if context_lines:
            payload["requirement"] = base_requirement + "\n\n--- Additional context from Client Onboarding ---\n" + "\n".join(context_lines)
        else:
            payload["requirement"] = base_requirement

        payload["client_context"] = brief["client_context"]
        payload["technical_constraints"] = brief["technical_constraints"]
        payload["acceptance_criteria"] = brief["acceptance_criteria"]
        payload["onboarding_completeness"] = brief["onboarding_completeness"]

        self.store.update("rh_active_jobs", active_job_id, job_payload_json=json.dumps(payload), updated_at=utcnow())
        self.audit.append("RH_ACTIVE_JOB_ENRICHED_FROM_ONBOARDING", {
            "active_job_id": active_job_id, "opportunity_id": opportunity_id, "actor": actor,
        })
        return payload
