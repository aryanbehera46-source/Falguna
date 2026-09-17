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


def _first_sentence(text: Optional[str]) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    for sep in [". ", "! ", "? ", "\n"]:
        if sep in text:
            return text.split(sep)[0].strip() + sep.strip()
    return text[:160]


class EmailStore:
    def __init__(self, store: StateStore, audit: AuditLog, needs_aryan=None):
        self.store = store
        self.audit = audit
        self.needs_aryan = needs_aryan

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
