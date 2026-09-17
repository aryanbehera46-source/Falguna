"""TTT Autonomous Revenue-to-Delivery Loop v1 -- Outbound Lead + Outreach
Foundation (Sections 4/5, Pass E).

`OutboundLeadStore` holds one real, researched record per prospect --
quality over volume by construction. There is no bulk-import or scraping
path here; every lead is created one at a time with a stated reason
(`likely_need`, `relevance_notes`), matching the existing codebase's
"never fabricate, always explainable" philosophy. `confidence` is
deliberately a plain Low/Medium/High label, not a manufactured percentage
-- this system has no real basis for a decimal probability on a cold lead.

`OutreachService` drafts outreach messages only -- it never sends anything.
Like `ApplicationExecutor`, every draft escalates to Needs Aryan
(`outreach_approval`) before it could ever be sent, and unlike
`ApplicationExecutor` there is no send adapter at all yet, real or
simulated: cold outbound messaging carries more risk (spam, deceptive
contact, CAPTCHA bypass) than replying to an inbound message, so this
module deliberately stops at PREPARED. `mark_sent` exists only to let an
owner honestly record that *they* sent a message themselves outside this
system (the exact same DRAFT/PREPARED -> SENT shape FollowupStore and
ConversationStore already use) -- it requires the draft's Needs Aryan item
to already be APPROVED, since nothing here should let an unapproved
message be recorded as sent.
"""

from typing import Any, Dict, List, Optional

from .audit import AuditLog
from .store import StateStore, utcnow

LEAD_STATUSES = {"NEW", "RESEARCHED", "OUTREACH_PREPARED", "CONTACTED", "DECLINED"}
CONFIDENCE_LEVELS = {"Low", "Medium", "High"}
DRAFT_STATUSES = {"PREPARED", "SENT"}


class OutboundLeadError(ValueError):
    pass


class OutboundLeadStore:
    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit

    def create_lead(
        self, company_name: str, actor: str, website: Optional[str] = None,
        contact_name: Optional[str] = None, contact_channel: Optional[str] = None,
        likely_need: Optional[str] = None, proposed_offer: Optional[str] = None,
        confidence: Optional[str] = None, relevance_notes: Optional[str] = None, source: Optional[str] = None,
    ) -> str:
        company_name = (company_name or "").strip()
        if not company_name:
            raise OutboundLeadError("company_name is required")
        if confidence is not None and confidence not in CONFIDENCE_LEVELS:
            raise OutboundLeadError(f"confidence must be one of {sorted(CONFIDENCE_LEVELS)} (a plain label, not a percentage)")
        now = utcnow()
        lead_id = self.store.create("rh_outbound_leads", {
            "company_name": company_name, "website": website, "contact_name": contact_name,
            "contact_channel": contact_channel, "likely_need": likely_need, "proposed_offer": proposed_offer,
            "confidence": confidence, "relevance_notes": relevance_notes, "source": source,
            "status": "NEW", "created_at": now, "updated_at": now,
        })
        self.audit.append("RH_OUTBOUND_LEAD_CREATED", {"lead_id": lead_id, "company_name": company_name, "actor": actor})
        return lead_id

    def update_status(self, lead_id: str, status: str, actor: str) -> Dict[str, Any]:
        if status not in LEAD_STATUSES:
            raise OutboundLeadError(f"unknown lead status: {status!r} (expected one of {sorted(LEAD_STATUSES)})")
        lead = self.store.get("rh_outbound_leads", lead_id)
        if not lead:
            raise OutboundLeadError("lead not found")
        self.store.update("rh_outbound_leads", lead_id, status=status, updated_at=utcnow())
        self.audit.append("RH_OUTBOUND_LEAD_STATUS_UPDATED", {"lead_id": lead_id, "status": status, "actor": actor})
        return self.store.get("rh_outbound_leads", lead_id)

    def get(self, lead_id: str) -> Optional[Dict[str, Any]]:
        return self.store.get("rh_outbound_leads", lead_id)

    def list(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        if status:
            return list(reversed(self.store.list("rh_outbound_leads", "status=?", (status,))))
        return list(reversed(self.store.list("rh_outbound_leads")))


class OutreachError(ValueError):
    pass


class OutreachService:
    """Drafts only -- see module docstring. There is no channel/provider
    that actually transmits anything; `channel` is recorded purely as a
    label for what a future, explicitly-authorized integration (business
    email, an approved contact form, a legitimate platform API) would use,
    per the provider/channel abstraction Section 5 calls for."""

    def __init__(self, store: StateStore, audit: AuditLog, needs_aryan=None):
        self.store = store
        self.audit = audit
        self.needs_aryan = needs_aryan
        self.leads = OutboundLeadStore(store, audit)

    def draft_message(self, lead_id: str, channel: str, message: str, actor: str = "system") -> Dict[str, Any]:
        lead = self.leads.get(lead_id)
        if not lead:
            raise OutreachError("lead not found")
        if not message or not message.strip():
            raise OutreachError("message is required")
        now = utcnow()
        draft_id = self.store.create("rh_outreach_drafts", {
            "lead_id": lead_id, "channel": channel, "message": message, "status": "PREPARED",
            "needs_aryan_id": None, "actor": actor, "created_at": now, "updated_at": now,
        })
        needs_aryan_id = None
        if self.needs_aryan is not None:
            needs_aryan_id = self.needs_aryan.create_item(
                "outreach_approval", f"Outbound outreach ready for review: {lead['company_name']}",
                "A cold outreach message has been drafted for this lead. Nothing has been sent -- "
                "this system has no capability to send outbound messages on its own. Review the "
                "draft and, if it's appropriate to send, send it yourself and mark it sent.",
                actor=actor, ref_type="rh_outreach_draft", ref_id=draft_id,
                rationale=lead.get("relevance_notes"),
            )
            self.store.update("rh_outreach_drafts", draft_id, needs_aryan_id=needs_aryan_id)
        self.audit.append("RH_OUTREACH_DRAFT_PREPARED", {"draft_id": draft_id, "lead_id": lead_id, "channel": channel, "actor": actor})
        self.leads.update_status(lead_id, "OUTREACH_PREPARED", actor)
        return self.store.get("rh_outreach_drafts", draft_id)

    def mark_sent(self, draft_id: str, actor: str) -> Dict[str, Any]:
        """Owner-triggered only, and only for a message the owner actually
        sent themselves outside this system -- this call records that fact,
        it does not perform any transmission. Requires the draft's own
        Needs Aryan item (if one exists) to already be APPROVED."""
        draft = self.store.get("rh_outreach_drafts", draft_id)
        if not draft:
            raise OutreachError("draft not found")
        if draft["status"] != "PREPARED":
            raise OutreachError(f"draft is already {draft['status']}, not PREPARED")
        if draft.get("needs_aryan_id"):
            item = self.store.get("needs_aryan_items", draft["needs_aryan_id"])
            if not item or item["status"] != "APPROVED":
                raise OutreachError("this outreach draft has not been approved yet")
        self.store.update("rh_outreach_drafts", draft_id, status="SENT", updated_at=utcnow())
        self.audit.append("RH_OUTREACH_DRAFT_SENT", {"draft_id": draft_id, "actor": actor})
        self.leads.update_status(draft["lead_id"], "CONTACTED", actor)
        return self.store.get("rh_outreach_drafts", draft_id)

    def list_for_lead(self, lead_id: str) -> List[Dict[str, Any]]:
        return list(reversed(self.store.list("rh_outreach_drafts", "lead_id=?", (lead_id,))))
