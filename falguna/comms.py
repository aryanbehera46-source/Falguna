"""TTT Communications + AI Customer Service V1 -- Milestone 1: Unified
Communications Center.

One durable, channel-agnostic model for every inbound/outbound interaction
TTT has with the outside world: general enquiries, sales, support, project
conversations, billing, careers, and media -- with room for future channels
(WhatsApp, LinkedIn, website chat) to plug in as adapters without changing
this core model.

Deliberately reuses, rather than duplicates, what this codebase already has:
  - Escalations/approvals go through the existing `NeedsAryanQueue`
    (falguna/ttt_hq.py) -- there is no second, parallel approval system.
  - The audit trail is the existing `AuditLog` hash chain -- every module in
    this codebase uses the same one.
  - Attachments reuse the existing generic `attachments` table (it already
    has conversation_id/message_id columns for exactly this).
  - A conversation LINKS to a real `rh_opportunities` row, `clients` row, or
    `site_applications` row when one exists -- it never creates a second,
    competing CRM record for the same real-world thing.

Channel and Department are small, closed, code-level enums (not separate
reference tables), matching the existing convention for rh_opportunities'
`stage` and ConversationStore's `INTENTS`.

Nothing here fabricates activity: creating a conversation/message always
reflects something that actually happened (a real form submission, a real
recorded inbound message, a real drafted/sent reply) -- there is no
synthetic traffic generator anywhere in this module.
"""

import json
from typing import Any, Dict, List, Optional

from .audit import AuditLog
from .store import StateStore, utcnow
from .ttt_hq import NeedsAryanQueue

CHANNELS = {"EMAIL", "WEBSITE", "SUPPORT", "CAREERS", "PROJECT", "INTERNAL"}
DEPARTMENTS = {"general", "sales", "support", "projects", "billing", "careers", "media"}
STATUSES = {"new", "open", "pending_customer", "pending_approval", "escalated", "resolved", "closed"}
PRIORITIES = {"low", "normal", "high", "urgent"}
MESSAGE_DIRECTIONS = {"INBOUND", "OUTBOUND"}
MESSAGE_KINDS = {"message", "note", "system"}
MESSAGE_STATUSES = {"RECEIVED", "DRAFT", "APPROVED", "SENT", "FAILED"}
# FAILED is added for Milestone 8/9 forward-compatibility: no live
# mailbox is connected this sprint (NullEmailProvider only), so nothing
# in this codebase sets it today -- it exists so a real provider
# integration later has somewhere real to record a failed send
# without a schema/status-enum change at that point.
PARTICIPANT_TYPES = {"customer", "agent", "watcher"}
# TTT Communications V2, Milestone 5: a finer-grained lifecycle for support
# and billing conversations, layered on top of the coarse `status` field
# above rather than replacing it (every existing view/test keeps working
# off `status` unchanged). Only meaningful for department in
# {"support", "billing"} -- NULL/unset for every other conversation.
TICKET_STATUSES = {"NEW", "TRIAGED", "IN_PROGRESS", "WAITING_CUSTOMER", "WAITING_INTERNAL", "RESOLVED", "CLOSED"}

# Deterministic, disclosed first-response SLA targets by priority (hours).
# Not a machine-learned or guessed number -- a plain, explainable default
# Aryan can change later; matches this codebase's "explainable, not a black
# box" posture (see e.g. ConversationStore's confidence labels).
_SLA_HOURS_BY_PRIORITY = {"urgent": 1, "high": 4, "normal": 24, "low": 72}


class CommsError(ValueError):
    pass


def _add_hours(iso_ts: str, hours: int) -> str:
    from datetime import datetime, timedelta, timezone
    dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00")) if iso_ts else datetime.now(timezone.utc)
    return (dt + timedelta(hours=hours)).isoformat()


class CommsStore:
    """The Unified Communications Center: organizations, contacts,
    conversations, messages, participants -- plus escalation (via the
    existing Needs Aryan queue) and a per-conversation status/priority/
    assignment history."""

    def __init__(self, store: StateStore, audit: AuditLog, needs_aryan: Optional[NeedsAryanQueue] = None):
        self.store = store
        self.audit = audit
        self.needs_aryan = needs_aryan or NeedsAryanQueue(store, audit)

    # -- organizations / contacts --------------------------------------

    def find_or_create_organization(self, name: Optional[str], domain: Optional[str] = None) -> Optional[str]:
        name = (name or "").strip()
        domain = (domain or "").strip().lower() or None
        if not name and not domain:
            return None
        if domain:
            existing = self.store.list("comm_organizations", "domain=?", (domain,))
            if existing:
                return existing[0]["id"]
        now = utcnow()
        return self.store.create("comm_organizations", {
            "name": name or domain, "domain": domain, "linked_client_id": None,
            "notes": None, "created_at": now, "updated_at": now,
        })

    def find_or_create_contact(
        self, email: Optional[str], name: Optional[str] = None,
        organization_id: Optional[str] = None, phone: Optional[str] = None,
    ) -> Optional[str]:
        email = (email or "").strip().lower() or None
        name = (name or "").strip() or None
        if not email and not name:
            return None
        if email:
            existing = self.store.list("comm_contacts", "email=?", (email,))
            if existing:
                contact = existing[0]
                # Backfill organization linkage if this contact didn't have one yet.
                if organization_id and not contact.get("organization_id"):
                    self.store.update("comm_contacts", contact["id"], organization_id=organization_id, updated_at=utcnow())
                return contact["id"]
        now = utcnow()
        return self.store.create("comm_contacts", {
            "organization_id": organization_id, "name": name, "email": email, "phone": phone,
            "role_title": None, "notes": None, "created_at": now, "updated_at": now,
        })

    # -- conversations ----------------------------------------------------

    def open_conversation(
        self, channel: str, department: str, subject: Optional[str] = None,
        contact_id: Optional[str] = None, organization_id: Optional[str] = None,
        priority: str = "normal", tags: Optional[List[str]] = None, actor: str = "system",
        linked_opportunity_id: Optional[str] = None, linked_client_id: Optional[str] = None,
        linked_application_id: Optional[str] = None, linked_project_id: Optional[str] = None,
        source_ref_type: Optional[str] = None, source_ref_id: Optional[str] = None,
        external_thread_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if channel not in CHANNELS:
            raise CommsError(f"channel must be one of {sorted(CHANNELS)}")
        if department not in DEPARTMENTS:
            raise CommsError(f"department must be one of {sorted(DEPARTMENTS)}")
        if priority not in PRIORITIES:
            raise CommsError(f"priority must be one of {sorted(PRIORITIES)}")
        now = utcnow()
        conv_id = self.store.create("comm_conversations", {
            "channel": channel, "department": department, "subject": subject,
            "status": "new", "priority": priority,
            "tags_json": json.dumps(tags) if tags else None,
            "organization_id": organization_id, "primary_contact_id": contact_id,
            "assigned_agent": None,
            "linked_opportunity_id": linked_opportunity_id, "linked_project_id": linked_project_id,
            "linked_client_id": linked_client_id, "linked_application_id": linked_application_id,
            "source_ref_type": source_ref_type, "source_ref_id": source_ref_id,
            "external_thread_id": external_thread_id,
            "first_response_due_at": _add_hours(now, _SLA_HOURS_BY_PRIORITY[priority]),
            "first_response_at": None, "resolution_due_at": None, "resolved_at": None,
            "created_at": now, "updated_at": now,
        })
        if contact_id:
            self.store.create("comm_participants", {
                "conversation_id": conv_id, "participant_type": "customer",
                "contact_id": contact_id, "agent_role": None, "created_at": now,
            })
        self.audit.append("COMM_CONVERSATION_OPENED", {
            "conversation_id": conv_id, "channel": channel, "department": department, "actor": actor,
        })
        return self.get_conversation(conv_id)

    def get_conversation(self, conversation_id: str) -> Optional[Dict[str, Any]]:
        conv = self.store.get("comm_conversations", conversation_id)
        if not conv:
            return None
        conv = dict(conv)
        conv["tags"] = json.loads(conv["tags_json"]) if conv.get("tags_json") else []
        conv["messages"] = self.store.list("comm_messages", "conversation_id=?", (conversation_id,))
        conv["participants"] = self.store.list("comm_participants", "conversation_id=?", (conversation_id,))
        conv["attachments"] = self.store.list("attachments", "conversation_id=?", (conversation_id,))
        conv["history"] = self.store.list("comm_status_events", "conversation_id=?", (conversation_id,))
        # Milestone 8: real risk classification evidence for this
        # conversation's own messages, for the HQ drill-down view --
        # reuses risk_engine.py's own comm_risk_events rows rather than
        # duplicating or re-deriving a classification here.
        message_ids = [m["id"] for m in conv["messages"]]
        conv["risk_events"] = (
            [e for e in self.store.list("comm_risk_events", "subject_type=?", ("comm_message",)) if e["subject_id"] in message_ids]
            if message_ids else []
        )
        if conv.get("organization_id"):
            conv["organization"] = self.store.get("comm_organizations", conv["organization_id"])
        if conv.get("primary_contact_id"):
            conv["primary_contact"] = self.store.get("comm_contacts", conv["primary_contact_id"])
        return conv

    def list_conversations(
        self, status: Optional[str] = None, department: Optional[str] = None,
        priority: Optional[str] = None, limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if status:
            clauses.append("status=?"); params.append(status)
        if department:
            clauses.append("department=?"); params.append(department)
        if priority:
            clauses.append("priority=?"); params.append(priority)
        where = " AND ".join(clauses) if clauses else "1=1"
        rows = list(reversed(self.store.list("comm_conversations", where, tuple(params))))
        return rows[:limit] if limit else rows

    # -- messages -----------------------------------------------------

    def add_message(
        self, conversation_id: str, direction: str, body: str, kind: str = "message",
        sender_contact_id: Optional[str] = None, sender_agent: Optional[str] = None,
        status: Optional[str] = None, is_internal_note: bool = False,
        source_ref_type: Optional[str] = None, source_ref_id: Optional[str] = None,
        actor: str = "system", provider_message_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        conv = self.store.get("comm_conversations", conversation_id)
        if not conv:
            raise CommsError("conversation not found")
        if direction not in MESSAGE_DIRECTIONS:
            raise CommsError(f"direction must be one of {sorted(MESSAGE_DIRECTIONS)}")
        if kind not in MESSAGE_KINDS:
            raise CommsError(f"kind must be one of {sorted(MESSAGE_KINDS)}")
        if not body or not body.strip():
            raise CommsError("body is required")
        status = status or ("RECEIVED" if direction == "INBOUND" else "DRAFT")
        if status not in MESSAGE_STATUSES:
            raise CommsError(f"status must be one of {sorted(MESSAGE_STATUSES)}")
        now = utcnow()
        msg_id = self.store.create("comm_messages", {
            "conversation_id": conversation_id, "direction": direction, "kind": kind,
            "sender_contact_id": sender_contact_id, "sender_agent": sender_agent,
            "body": body.strip(), "status": status, "is_internal_note": 1 if is_internal_note else 0,
            "source_ref_type": source_ref_type, "source_ref_id": source_ref_id,
            "provider_message_id": provider_message_id,
            "created_at": now, "updated_at": now,
        })
        updates: Dict[str, Any] = {"updated_at": now}
        # A conversation still sitting in 'new' becomes 'open' the moment
        # anyone (customer or agent) actually engages with it.
        if conv["status"] == "new":
            updates["status"] = "open"
        if direction == "OUTBOUND" and status == "SENT" and not conv.get("first_response_at"):
            updates["first_response_at"] = now
        self.store.update("comm_conversations", conversation_id, **updates)
        self.audit.append("COMM_MESSAGE_ADDED", {
            "conversation_id": conversation_id, "message_id": msg_id, "direction": direction,
            "kind": kind, "is_internal_note": is_internal_note, "actor": actor,
        })
        return self.store.get("comm_messages", msg_id)

    def mark_message_sent(self, message_id: str, actor: str) -> Dict[str, Any]:
        """The one, explicit, owner-performed action that turns a drafted
        OUTBOUND message into one that was actually sent -- same shape as
        `EmailStore.mark_sent` / `FollowupStore` elsewhere in this
        codebase. Nothing in this module ever calls this itself; it exists
        only for a human (via the TTT HQ UI) to call after reviewing a
        DRAFT and sending it through whatever real channel applies."""
        message = self.store.get("comm_messages", message_id)
        if not message:
            raise CommsError("message not found")
        if message["direction"] != "OUTBOUND":
            raise CommsError("only an outbound (drafted) message can be marked sent")
        if message["status"] != "DRAFT":
            raise CommsError(f"message is already {message['status']}, not DRAFT")
        now = utcnow()
        self.store.update("comm_messages", message_id, status="SENT", updated_at=now)
        conv = self.store.get("comm_conversations", message["conversation_id"])
        if conv and not conv.get("first_response_at"):
            self.store.update("comm_conversations", message["conversation_id"], first_response_at=now, updated_at=now)
        # Milestone 5: once TTT has actually sent a reply on an open
        # support/billing ticket, the ball is in the customer's court --
        # flips WAITING_CUSTOMER automatically only from a non-terminal
        # ticket_status, never overriding an explicit RESOLVED/CLOSED.
        if conv and conv["department"] in ("support", "billing") and conv.get("ticket_status") not in (None, "RESOLVED", "CLOSED"):
            self.set_ticket_status(message["conversation_id"], "WAITING_CUSTOMER", actor="system",
                                    reason="an outbound reply was sent on this ticket")
        self.audit.append("COMM_MESSAGE_SENT", {"message_id": message_id, "conversation_id": message["conversation_id"], "actor": actor})
        return self.store.get("comm_messages", message_id)

    def mark_message_failed(self, message_id: str, actor: str, reason: str) -> Dict[str, Any]:
        """The FAILED counterpart to mark_message_sent -- for a real
        provider integration (later, once a mailbox is connected) to
        record a genuine delivery failure rather than silently leaving a
        message stuck at DRAFT or falsely marked SENT. Nothing in this
        codebase calls this yet (see MESSAGE_STATUSES' own note); it's
        forward-compatible plumbing so TTT HQ's Failed Delivery view
        (Milestone 8) has a real, structurally-correct place to read from
        the moment a provider is actually wired in, with no later
        redesign."""
        if not reason or not reason.strip():
            raise CommsError("reason is required to mark a message failed")
        message = self.store.get("comm_messages", message_id)
        if not message:
            raise CommsError("message not found")
        if message["direction"] != "OUTBOUND":
            raise CommsError("only an outbound (drafted) message can be marked failed")
        if message["status"] != "DRAFT":
            raise CommsError(f"message is already {message['status']}, not DRAFT")
        now = utcnow()
        self.store.update("comm_messages", message_id, status="FAILED", updated_at=now)
        self.audit.append("COMM_MESSAGE_FAILED", {
            "message_id": message_id, "conversation_id": message["conversation_id"], "actor": actor, "reason": reason,
        })
        return self.store.get("comm_messages", message_id)

    def set_ticket_status(self, conversation_id: str, ticket_status: str, actor: str, reason: Optional[str] = None) -> Dict[str, Any]:
        """Milestone 5's finer-grained support/billing lifecycle -- layered
        on top of `status`, never a replacement for it. Setting RESOLVED
        here does NOT by itself resolve the conversation's coarse `status`;
        use `resolve_ticket()` for that, which requires an explicit
        resolution note (this codebase's "never invent a resolution"
        invariant -- see comms_workforce.SupportAgent)."""
        if ticket_status not in TICKET_STATUSES:
            raise CommsError(f"ticket_status must be one of {sorted(TICKET_STATUSES)}")
        conv = self.store.get("comm_conversations", conversation_id)
        if not conv:
            raise CommsError("conversation not found")
        now = utcnow()
        self.store.update("comm_conversations", conversation_id, ticket_status=ticket_status, updated_at=now)
        self.store.create("comm_status_events", {
            "conversation_id": conversation_id, "field": "ticket_status", "old_value": conv.get("ticket_status"),
            "new_value": ticket_status, "actor": actor, "reason": reason, "needs_aryan_id": None, "created_at": now,
        })
        self.audit.append("COMM_TICKET_STATUS_CHANGED", {
            "conversation_id": conversation_id, "from": conv.get("ticket_status"), "to": ticket_status, "actor": actor,
        })
        return self.store.get("comm_conversations", conversation_id)

    def resolve_ticket(self, conversation_id: str, actor: str, resolution_note: str) -> Dict[str, Any]:
        """The one, explicit, evidence-requiring way a support/billing
        ticket becomes RESOLVED -- never called automatically by an AI
        agent. `resolution_note` must be a real, non-empty statement of
        what was actually done/found; this never accepts a blank or
        placeholder close."""
        if not resolution_note or not resolution_note.strip():
            raise CommsError("resolve_ticket requires a real, non-empty resolution_note (evidence of what was resolved)")
        conv = self.store.get("comm_conversations", conversation_id)
        if not conv:
            raise CommsError("conversation not found")
        self.add_message(conversation_id, "OUTBOUND", resolution_note.strip(), kind="note",
                          is_internal_note=True, actor=actor)
        self.set_ticket_status(conversation_id, "RESOLVED", actor, reason=resolution_note.strip())
        return self.set_status(conversation_id, "resolved", actor, reason=resolution_note.strip())

    # -- status / priority / assignment --------------------------------

    def set_status(self, conversation_id: str, status: str, actor: str, reason: Optional[str] = None) -> Dict[str, Any]:
        if status not in STATUSES:
            raise CommsError(f"status must be one of {sorted(STATUSES)}")
        conv = self.store.get("comm_conversations", conversation_id)
        if not conv:
            raise CommsError("conversation not found")
        now = utcnow()
        updates: Dict[str, Any] = {"status": status, "updated_at": now}
        if status in ("resolved", "closed") and not conv.get("resolved_at"):
            updates["resolved_at"] = now
        self.store.update("comm_conversations", conversation_id, **updates)
        self.store.create("comm_status_events", {
            "conversation_id": conversation_id, "field": "status", "old_value": conv["status"],
            "new_value": status, "actor": actor, "reason": reason, "needs_aryan_id": None, "created_at": now,
        })
        self.audit.append("COMM_STATUS_CHANGED", {"conversation_id": conversation_id, "from": conv["status"], "to": status, "actor": actor})
        return self.store.get("comm_conversations", conversation_id)

    def set_priority(self, conversation_id: str, priority: str, actor: str, reason: Optional[str] = None) -> Dict[str, Any]:
        if priority not in PRIORITIES:
            raise CommsError(f"priority must be one of {sorted(PRIORITIES)}")
        conv = self.store.get("comm_conversations", conversation_id)
        if not conv:
            raise CommsError("conversation not found")
        now = utcnow()
        self.store.update("comm_conversations", conversation_id, priority=priority, updated_at=now)
        self.store.create("comm_status_events", {
            "conversation_id": conversation_id, "field": "priority", "old_value": conv["priority"],
            "new_value": priority, "actor": actor, "reason": reason, "needs_aryan_id": None, "created_at": now,
        })
        return self.store.get("comm_conversations", conversation_id)

    def assign(self, conversation_id: str, agent_role: str, actor: str) -> Dict[str, Any]:
        conv = self.store.get("comm_conversations", conversation_id)
        if not conv:
            raise CommsError("conversation not found")
        now = utcnow()
        self.store.update("comm_conversations", conversation_id, assigned_agent=agent_role, updated_at=now)
        self.store.create("comm_participants", {
            "conversation_id": conversation_id, "participant_type": "agent",
            "contact_id": None, "agent_role": agent_role, "created_at": now,
        })
        self.store.create("comm_status_events", {
            "conversation_id": conversation_id, "field": "assigned_agent", "old_value": conv.get("assigned_agent"),
            "new_value": agent_role, "actor": actor, "reason": None, "needs_aryan_id": None, "created_at": now,
        })
        self.audit.append("COMM_CONVERSATION_ASSIGNED", {"conversation_id": conversation_id, "agent_role": agent_role, "actor": actor})
        return self.store.get("comm_conversations", conversation_id)

    # -- escalation / approval (reuses NeedsAryanQueue, no parallel system) --

    def escalate(
        self, conversation_id: str, title: str, what_is_needed: str, actor: str = "system",
        kind: str = "communications_approval", recommendation: Optional[str] = None,
        rationale: Optional[str] = None, risk: Optional[str] = None,
    ) -> Dict[str, Any]:
        conv = self.store.get("comm_conversations", conversation_id)
        if not conv:
            raise CommsError("conversation not found")
        needs_aryan_id = self.needs_aryan.create_item(
            kind, title, what_is_needed, actor=actor, recommendation=recommendation,
            rationale=rationale, risk=risk, ref_type="comm_conversation", ref_id=conversation_id,
        )
        now = utcnow()
        self.store.update("comm_conversations", conversation_id, status="pending_approval", updated_at=now)
        self.store.create("comm_status_events", {
            "conversation_id": conversation_id, "field": "escalation", "old_value": conv["status"],
            "new_value": "pending_approval", "actor": actor, "reason": title,
            "needs_aryan_id": needs_aryan_id, "created_at": now,
        })
        self.audit.append("COMM_CONVERSATION_ESCALATED", {
            "conversation_id": conversation_id, "needs_aryan_id": needs_aryan_id, "kind": kind, "actor": actor,
        })
        return {"needs_aryan_id": needs_aryan_id, "conversation": self.store.get("comm_conversations", conversation_id)}

    # -- HQ overview (Milestone 7 data layer) --------------------------

    def overview(self) -> Dict[str, Any]:
        """Real, live aggregate counts -- computed from these tables on every
        call, never cached/stored, so it can never drift from reality.

        TTT Communications V2, Milestone 8: five more views added here on
        top of the four this method already had (needs_attention/
        new_leads/awaiting_approval/by_department), all the same way --
        a real filter over comm_conversations/comm_messages/rh_followups,
        nothing cached, nothing estimated."""
        all_open = self.store.list("comm_conversations", "status NOT IN ('resolved','closed')")
        by_status: Dict[str, int] = {}
        by_department: Dict[str, int] = {}
        for row in all_open:
            by_status[row["status"]] = by_status.get(row["status"], 0) + 1
            by_department[row["department"]] = by_department.get(row["department"], 0) + 1
        needs_attention = [r for r in all_open if r["priority"] in ("high", "urgent") or r["status"] == "escalated"]
        awaiting_approval = [
            item for item in self.needs_aryan.list_pending()
            if item.get("ref_type") == "comm_conversation"
        ] if hasattr(self.needs_aryan, "list_pending") else []
        new_recent = [r for r in all_open if r["status"] == "new"]
        recent_messages = self.store.list("comm_messages", "1=1")
        recent_messages = list(reversed(recent_messages))[:20]

        support_issues = [r for r in all_open if r["department"] in ("support", "billing")]
        awaiting_client = [r for r in all_open if r["status"] == "pending_customer"]
        # "Active" is the general working set every other bucket above is a
        # slice of -- everything genuinely open, most-recent first, same
        # rows `list_conversations()` already returns unfiltered.
        active_conversations = all_open
        follow_ups_due = self.store.list("rh_followups", "status=?", ("DRAFT",))
        follow_ups_due = list(reversed(follow_ups_due))[:20]
        # See MESSAGE_STATUSES' own note: nothing in this codebase can set
        # FAILED yet (no live provider is connected this sprint), so this
        # is honestly empty today -- structurally real, not simulated.
        failed_delivery = self.store.list("comm_messages", "status=?", ("FAILED",))
        failed_delivery = list(reversed(failed_delivery))[:20]
        recently_resolved = self.store.list("comm_conversations", "status IN ('resolved','closed')")
        recently_resolved = list(reversed(recently_resolved))[:20]

        return {
            "open_total": len(all_open),
            "by_status": by_status,
            "by_department": by_department,
            "needs_attention": needs_attention[:20],
            "new_leads": new_recent[:20],
            "awaiting_approval": awaiting_approval,
            "recent_messages": recent_messages,
            "support_issues": support_issues[:20],
            "awaiting_client": awaiting_client[:20],
            "active_conversations": active_conversations[:20],
            "follow_ups_due": follow_ups_due,
            "failed_delivery": failed_delivery,
            "recently_resolved": recently_resolved,
        }
