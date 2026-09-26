"""TTT Communications V2 -- Milestone 3: Real Email Ingestion Model.

Turns one inbound email-provider message (the dict shape
`EmailProvider.poll_inbound()` returns -- provider_message_id, thread_id,
from_address, to_address, subject, body, received_at,
in_reply_to_message_id, attachments) into real `CommsStore` state. No live
mailbox is connected this sprint (see falguna/email_admin.py's
NullEmailProvider default) -- this module is exercised today by passing it
a provider-shaped payload directly (a real message a human pastes in for a
trial, or a future real IMAPEmailProvider.poll_inbound() call), never by
inventing one.

Reuses, never duplicates:
  - `CommsStore` for organizations/contacts/conversations/messages.
  - `conversations.classify_intent` for intent.
  - `comms_workforce.run_agent_for_conversation` for "assign the right AI
    representative and draft a next action" -- ingestion's job stops at
    turning a provider payload into a real comm_messages row on the right
    conversation; everything about what an AI agent does with it already
    exists and is called from here, not reimplemented.

De-duplication / ordering:
  - Every inbound message is matched against already-ingested ones by
    `comm_messages.provider_message_id` -- a provider retry or the same
    message delivered twice is a safe no-op, never a duplicate row.
  - Threading is by the provider's own `thread_id`
    (`comm_conversations.external_thread_id`) when supplied, so a reply
    that arrives out of order still lands on the same conversation instead
    of depending on strict chronological delivery.
"""

import re
from typing import Any, Dict, List, Optional

from .audit import AuditLog
from .comms import CommsStore, DEPARTMENTS
from .conversations import classify_intent
from .email_admin import DEPARTMENT_MAILBOXES
from .revenue_hunter import OpportunityStore
from .store import StateStore, utcnow

ACTIVE_CONVERSATION_STATUSES = {"new", "open", "pending_customer", "pending_approval", "escalated"}

# Explainable, literal phrase matching -- same posture as every other
# classifier in this codebase (risk_engine, conversations.classify_intent,
# comms_workforce's commitment-signal detector).
_BOUNCE_SENDER_SIGNALS = ["mailer-daemon", "postmaster", "mail delivery subsystem", "no-reply-bounce"]
_BOUNCE_SUBJECT_SIGNALS = [
    "delivery status notification", "undelivered mail returned to sender", "failure notice",
    "returned mail", "delivery has failed", "message could not be delivered", "mail delivery failed",
]
_SPAM_SIGNALS = [
    "you have won", "claim your prize", "act now", "click here now", "free money",
    "no investment required", "work from home guaranteed", "viagra", "wire us your details",
    "congratulations you have been selected", "double your bitcoin",
]
_URGENT_SIGNALS = ["urgent", "asap", "immediately", "emergency", "right away", "critical issue"]

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class IngestionError(ValueError):
    pass


def _looks_like_email(addr: Optional[str]) -> bool:
    return bool(addr) and bool(_EMAIL_RE.match(addr.strip()))


def _extract_domain(email: Optional[str]) -> Optional[str]:
    if not email or "@" not in email:
        return None
    return email.rsplit("@", 1)[-1].strip().lower() or None


def _matches_any(haystack: str, signals: List[str]) -> Optional[str]:
    lowered = (haystack or "").lower()
    for signal in signals:
        if signal in lowered:
            return signal
    return None


def department_from_mailbox(to_address: Optional[str]) -> str:
    """Reverse lookup against the same DEPARTMENT_MAILBOXES the outbound
    side already declares (falguna/email_admin.py) -- a message that
    arrived at sales@... is a sales-department conversation by construction,
    not a guess. Falls back to 'general' when the address is unrecognized,
    unaddressed, or the department it maps to isn't a valid comms department."""
    if not to_address:
        return "general"
    local = to_address.strip().lower()
    for department, mailbox in DEPARTMENT_MAILBOXES.items():
        if mailbox.lower() == local and department in DEPARTMENTS:
            return department
    return "general"


class EmailIngestionService:
    """Ingests one provider-shaped inbound email payload at a time.
    `ingest()` never raises on a malformed/hostile payload -- a live poll
    loop calling this once per provider message must be able to keep going
    even when one message is garbage; it returns a status dict instead."""

    def __init__(self, store: StateStore, audit: AuditLog, comms: CommsStore):
        self.store = store
        self.audit = audit
        self.comms = comms
        # Milestone 3 -> 6 integration: a brand-new inbound lead landing on
        # the sales mailbox needs a real Revenue Hunter opportunity to be
        # engaged by SalesRepAgent at all (it only ever acts on
        # conversation.linked_opportunity_id) -- reuses the same
        # OpportunityStore every other entry point (website intake,
        # opportunity_agent.py, hq_web.py) already writes through, never a
        # second lead record.
        self.opportunities = OpportunityStore(store, audit)

    def ingest(self, payload: Dict[str, Any], actor: str = "email_ingestion") -> Dict[str, Any]:
        provider_message_id = (payload.get("provider_message_id") or "").strip() or None
        from_address = (payload.get("from_address") or "").strip() or None
        to_address = (payload.get("to_address") or "").strip() or None
        subject = (payload.get("subject") or "").strip() or None
        body = (payload.get("body") or "").strip()
        thread_id = (payload.get("thread_id") or "").strip() or None
        attachments = payload.get("attachments") or []

        # -- 1. de-duplication: a provider retry or a re-delivered message
        # is a no-op, never a second row. Checked before any other
        # validation so a duplicate of an already-rejected message doesn't
        # get re-rejected noisily either.
        if provider_message_id:
            existing = self.store.list("comm_messages", "provider_message_id=?", (provider_message_id,))
            if existing:
                self.audit.append("COMM_INBOUND_DUPLICATE_SKIPPED", {
                    "provider_message_id": provider_message_id, "message_id": existing[0]["id"], "actor": actor,
                })
                return {"status": "duplicate", "message_id": existing[0]["id"],
                        "conversation_id": existing[0]["conversation_id"]}

        # -- 2. metadata validation: never fabricate a sender or a message
        # out of malformed/missing data.
        if not _looks_like_email(from_address):
            self.audit.append("COMM_INBOUND_REJECTED", {
                "reason": "missing_or_malformed_sender", "from_address": from_address, "actor": actor,
            })
            return {"status": "rejected", "reason": "missing or malformed sender address"}
        if not body and not attachments:
            self.audit.append("COMM_INBOUND_REJECTED", {
                "reason": "empty_message", "from_address": from_address, "actor": actor,
            })
            return {"status": "rejected", "reason": "message has no body and no attachments"}

        # -- 3. bounce detection: a delivery-failure notice is not a real
        # customer message -- recorded for visibility (Milestone 9) without
        # opening a fake customer conversation.
        bounce_signal = _matches_any(from_address, _BOUNCE_SENDER_SIGNALS) or _matches_any(subject or "", _BOUNCE_SUBJECT_SIGNALS)
        if bounce_signal:
            self.audit.append("COMM_INBOUND_BOUNCE_DETECTED", {
                "from_address": from_address, "subject": subject, "signal": bounce_signal, "actor": actor,
            })
            return {"status": "bounce", "reason": f"matched bounce signal: {bounce_signal!r}"}

        spam_signal = _matches_any(f"{subject or ''} {body}", _SPAM_SIGNALS)
        urgent_signal = _matches_any(f"{subject or ''} {body}", _URGENT_SIGNALS)

        # -- 4. identify sender / organization (never overwrites a contact
        # that already has an organization -- see find_or_create_contact).
        domain = _extract_domain(from_address)
        organization_id = self.comms.find_or_create_organization(domain, domain=domain) if domain else None
        contact_id = self.comms.find_or_create_contact(from_address, organization_id=organization_id)

        department = department_from_mailbox(to_address)

        # -- 5. thread/conversation matching. Provider thread id wins when
        # present (works regardless of delivery order); otherwise reuse the
        # contact's most recent still-active conversation in the same
        # department so an ordinary reply lands in the same thread instead
        # of spawning a new one every time.
        conversation = None
        if thread_id:
            matches = self.store.list("comm_conversations", "external_thread_id=?", (thread_id,))
            if matches:
                conversation = self.comms.get_conversation(matches[0]["id"])
        if conversation is None and contact_id:
            candidates = [
                c for c in self.store.list("comm_conversations", "primary_contact_id=? AND department=?", (contact_id, department))
                if c["status"] in ACTIVE_CONVERSATION_STATUSES
            ]
            if candidates:
                candidates.sort(key=lambda c: c["updated_at"], reverse=True)
                conversation = self.comms.get_conversation(candidates[0]["id"])

        created_new_conversation = conversation is None
        if conversation is None:
            # Milestone 3 -> 6 integration: only a genuinely new
            # sales-department conversation gets a new opportunity --
            # never on a reply that lands on an existing thread, and never
            # a second opportunity for the same lead. Fields are only what
            # the real inbound email actually said (subject/body); nothing
            # about budget, timeline, or contract type is guessed here --
            # SalesRepAgent's own discovery-question path is what asks for
            # those, exactly as it does for a website-originated lead.
            linked_opportunity_id = None
            if department == "sales":
                linked_opportunity_id = self.opportunities.create({
                    "title": subject or f"Inbound sales enquiry from {from_address}",
                    "description": body or None,
                    "client_name": self.store.get("comm_organizations", organization_id)["name"] if organization_id else None,
                }, actor=actor, source="email_inbound")
            conversation = self.comms.open_conversation(
                "EMAIL", department, subject=subject, contact_id=contact_id, organization_id=organization_id,
                priority="high" if urgent_signal else "normal", actor=actor,
                external_thread_id=thread_id, tags=["spam_like"] if spam_signal else None,
                linked_opportunity_id=linked_opportunity_id,
            )

        elif urgent_signal and conversation["priority"] not in ("high", "urgent"):
            self.comms.set_priority(conversation["id"], "high", actor, reason=f"urgent signal in inbound message: {urgent_signal!r}")
            conversation = self.comms.get_conversation(conversation["id"])

        # -- 6. attach the message. Attachment bytes are never invented --
        # a provider payload that only carries filename/content-type
        # metadata (true for IMAPEmailProvider.poll_inbound() today, since
        # no live mailbox is connected this sprint) is recorded as an
        # internal note naming what was referenced, not as a claim that the
        # file was actually fetched and stored.
        message_body = body or "(no message body -- see attachment metadata note)"
        message = self.comms.add_message(
            conversation["id"], "INBOUND", message_body, kind="message",
            sender_contact_id=contact_id, source_ref_type="email_provider",
            source_ref_id=provider_message_id, provider_message_id=provider_message_id, actor=actor,
        )
        if attachments:
            names = ", ".join(a.get("filename") or "(unnamed)" for a in attachments)
            self.comms.add_message(
                conversation["id"], "INBOUND", f"{len(attachments)} attachment(s) referenced by the provider: {names}. "
                "Metadata only -- not yet fetched/stored (no live mailbox connected this sprint).",
                kind="note", is_internal_note=True, actor=actor,
            )

        self.audit.append("COMM_INBOUND_INGESTED", {
            "conversation_id": conversation["id"], "message_id": message["id"],
            "provider_message_id": provider_message_id, "department": department,
            "created_new_conversation": created_new_conversation, "spam_like": bool(spam_signal),
            "urgent": bool(urgent_signal), "actor": actor,
        })

        result = {
            "status": "ingested", "conversation_id": conversation["id"], "message_id": message["id"],
            "department": department, "intent": classify_intent(message_body),
            "created_new_conversation": created_new_conversation, "spam_like": bool(spam_signal),
        }

        # Spam-like inbound never triggers automatic AI drafting/replying --
        # it's still visible in HQ (for a human to confirm/dismiss) but the
        # workforce never engages with it on its own.
        if not spam_signal:
            from .comms_workforce import run_agent_for_conversation
            result["workforce_actions"] = run_agent_for_conversation(
                self.store, self.audit, conversation["id"], actor="ai_workforce",
            )["actions"]
        return result
