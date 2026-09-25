"""TTT Digital Workforce v1 -- Email/Admin foundation (Section 7, Pass B).

Same posture as `falguna/outbound.py`'s `OutreachService`: drafting,
classifying, and summarizing an email is a real, verifiable local action, so
a draft always completes; actually sending one to a real recipient is not --
there is no send adapter here at all in v1, real or simulated. A draft
always escalates to Needs Aryan before it could ever be sent, and
`mark_sent` only ever records that the owner sent it themselves outside this
system. Classification reuses `conversations.classify_intent` rather than a
second, diverging rule set for the same problem.
"""

from typing import Any, Dict, List, Optional

from .audit import AuditLog
from .conversations import classify_intent
from .store import StateStore, utcnow

EMAIL_STATUSES = {"PREPARED", "APPROVED", "SENT", "RECEIVED"}


class EmailError(ValueError):
    pass


class EmailProvider:
    """Provider-independent send boundary for future official
    @twentytwotechnologies.com mailboxes (Communications workforce v1,
    requirement 7). Wiring in a real provider later (SMTP, SES, Postmark,
    the Gmail API, ...) means writing one small adapter class here that
    implements these two methods -- nothing else in this codebase changes,
    and no credentials for any provider are ever stored in code."""

    def is_configured(self) -> bool:
        raise NotImplementedError

    def send(self, to_address: str, subject: str, body: str, from_address: Optional[str] = None) -> Dict[str, Any]:
        raise NotImplementedError


class NullEmailProvider(EmailProvider):
    """The only provider wired in today. Deliberately incapable of sending
    anything: `is_configured()` is always False and `send()` always
    raises. This is what keeps `mark_sent` a manual, owner-performed action
    -- this code can never send email on its own -- and it never assumes an
    unprovisioned @twentytwotechnologies.com mailbox is actually live."""

    def is_configured(self) -> bool:
        return False

    def send(self, to_address: str, subject: str, body: str, from_address: Optional[str] = None) -> Dict[str, Any]:
        raise EmailError(
            "no email provider is configured -- this system cannot send email on its own. "
            "Send this message yourself, then call mark_sent()."
        )


# Planned departmental mailboxes -- addresses only, never asserted as live,
# provisioned inboxes. A department shows here once it has a planned
# identity in the rest of the app (see falguna.comms.DEPARTMENTS); treat as
# operational only once a human confirms it and wires a real EmailProvider
# to it below.
DEPARTMENT_MAILBOXES: Dict[str, str] = {
    "general": "hello@twentytwotechnologies.com",
    "sales": "sales@twentytwotechnologies.com",
    "projects": "projects@twentytwotechnologies.com",
    "support": "support@twentytwotechnologies.com",
    "billing": "billing@twentytwotechnologies.com",
    "careers": "careers@twentytwotechnologies.com",
    "media": "media@twentytwotechnologies.com",
}


def _first_sentence(text: Optional[str]) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    for sep in [". ", "! ", "? ", "\n"]:
        if sep in text:
            return text.split(sep)[0].strip() + sep.strip()
    return text[:160]


class EmailStore:
    def __init__(self, store: StateStore, audit: AuditLog, needs_aryan=None, provider: Optional[EmailProvider] = None):
        self.store = store
        self.audit = audit
        self.needs_aryan = needs_aryan
        # Defaults to a provider that structurally cannot send anything --
        # see NullEmailProvider above. Passing a real provider here is the
        # only way this class's send-readiness ever changes.
        self.provider = provider or NullEmailProvider()

    def provider_status(self) -> Dict[str, Any]:
        """What TTT HQ shows for email-send readiness -- never claims a
        department mailbox is live just because it's in DEPARTMENT_MAILBOXES."""
        return {
            "provider": type(self.provider).__name__,
            "configured": self.provider.is_configured(),
            "planned_mailboxes": dict(DEPARTMENT_MAILBOXES),
        }

    def draft(
        self, to_address: str, subject: str, body: str, department: Optional[str] = None,
        thread_id: Optional[str] = None, source_task_id: Optional[str] = None, actor: str = "system",
    ) -> Dict[str, Any]:
        if not body or not body.strip():
            raise EmailError("body is required")
        now = utcnow()
        email_id = self.store.create("wf_email_messages", {
            "department": department, "direction": "OUTBOUND", "to_address": to_address, "from_address": None,
            "subject": subject, "body": body, "intent": classify_intent(body), "status": "PREPARED",
            "needs_aryan_id": None, "thread_id": thread_id, "follow_up_date": None,
            "source_task_id": source_task_id, "actor": actor, "created_at": now, "updated_at": now,
        })
        needs_aryan_id = None
        if self.needs_aryan is not None:
            needs_aryan_id = self.needs_aryan.create_item(
                "workforce_action_approval", f"Email ready for review: {subject or '(no subject)'}",
                "An email has been drafted. Nothing has been sent -- this system has no capability to "
                "send email on its own. Review it and, if it's appropriate to send, send it yourself "
                "and mark it sent.",
                actor=actor, ref_type="wf_email_message", ref_id=email_id,
            )
            self.store.update("wf_email_messages", email_id, needs_aryan_id=needs_aryan_id)
        self.audit.append("WF_EMAIL_DRAFTED", {"email_id": email_id, "to": to_address, "actor": actor})
        return self.get(email_id)

    def record_received(
        self, from_address: str, subject: str, body: str, department: Optional[str] = None,
        thread_id: Optional[str] = None, actor: str = "system",
    ) -> Dict[str, Any]:
        """Logs an email that actually arrived through some real channel
        outside this system -- mirrors ConversationStore.record_inbound.
        This never claims an inbound polling/monitoring capability that
        doesn't exist."""
        if not body or not body.strip():
            raise EmailError("body is required")
        now = utcnow()
        email_id = self.store.create("wf_email_messages", {
            "department": department, "direction": "INBOUND", "to_address": None, "from_address": from_address,
            "subject": subject, "body": body, "intent": classify_intent(body), "status": "RECEIVED",
            "needs_aryan_id": None, "thread_id": thread_id, "follow_up_date": None,
            "source_task_id": None, "actor": actor, "created_at": now, "updated_at": now,
        })
        self.audit.append("WF_EMAIL_RECEIVED", {"email_id": email_id, "from": from_address, "actor": actor})
        return self.get(email_id)

    def summarize(self, email_id: str) -> str:
        email = self._require(email_id)
        return _first_sentence(email["body"])

    def mark_sent(self, email_id: str, actor: str) -> Dict[str, Any]:
        email = self._require(email_id)
        if email["direction"] != "OUTBOUND":
            raise EmailError("only an outbound draft can be marked sent")
        if email["status"] != "PREPARED":
            raise EmailError(f"email is already {email['status']}, not PREPARED")
        if email.get("needs_aryan_id"):
            item = self.store.get("needs_aryan_items", email["needs_aryan_id"])
            if not item or item["status"] != "APPROVED":
                raise EmailError("this email has not been approved yet")
        self.store.update("wf_email_messages", email_id, status="SENT")
        self.audit.append("WF_EMAIL_SENT", {"email_id": email_id, "actor": actor})
        return self.get(email_id)

    def schedule_follow_up(self, email_id: str, follow_up_date: str, actor: str) -> Dict[str, Any]:
        self._require(email_id)
        self.store.update("wf_email_messages", email_id, follow_up_date=follow_up_date)
        self.audit.append("WF_EMAIL_FOLLOWUP_SCHEDULED", {"email_id": email_id, "follow_up_date": follow_up_date, "actor": actor})
        return self.get(email_id)

    def due_follow_ups(self, as_of: Optional[str] = None) -> List[Dict[str, Any]]:
        as_of = (as_of or utcnow())[:10]
        return [
            e for e in self.store.list("wf_email_messages")
            if e.get("follow_up_date") and e["follow_up_date"] <= as_of and e["status"] != "SENT"
        ]

    def _require(self, email_id: str) -> Dict[str, Any]:
        email = self.store.get("wf_email_messages", email_id)
        if not email:
            raise EmailError("email not found")
        return email

    def get(self, email_id: str) -> Optional[Dict[str, Any]]:
        return self.store.get("wf_email_messages", email_id)

    def list(self, department: Optional[str] = None, status: Optional[str] = None) -> List[Dict[str, Any]]:
        if department and status:
            rows = self.store.list("wf_email_messages", "department=? AND status=?", (department, status))
        elif department:
            rows = self.store.list("wf_email_messages", "department=?", (department,))
        elif status:
            rows = self.store.list("wf_email_messages", "status=?", (status,))
        else:
            rows = self.store.list("wf_email_messages")
        return list(reversed(rows))
