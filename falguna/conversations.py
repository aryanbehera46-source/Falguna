"""TTT Autonomous Revenue-to-Delivery Loop v1 -- Conversation Inbox and
Reply Agent (Section 6, Pass B).

Same conventions as the rest of Revenue Hunter: classification and reply
drafting are deterministic and rule-based, not model-generated -- a message
inbox needs to behave identically every time, offline, fully unit-testable.
A drafted reply is built only from real, already-on-file fields (the
opportunity's own title/description, the qualification's own suggested
price/timeline, the actual approved proposal's text, the actual saved Sales
Policy) -- never an invented fact, quote, or promise.

Nothing here ever actually sends a message. `ConversationStore.draft_reply`
always creates a DRAFT row; `mark_sent` is the one explicit, owner-triggered
action that marks it sent -- the exact same DRAFT -> SENT shape
`FollowupStore` already uses elsewhere in this codebase, not a new pattern.
Recording an inbound message never claims outbound delivery either: no
inbound polling/monitoring of any real channel exists in this codebase --
`record_inbound` is what a real channel integration would call once such an
integration exists (Pass B/E's outreach/outbound work), or what an owner
calls by hand today to log a reply that arrived elsewhere.
"""

import json
from typing import Any, Dict, List, Optional

from .audit import AuditLog
from .lifecycle import LifecycleOrchestrator
from .revenue_hunter import OpportunityStore
from .sales_ops import SalesPolicyStore
from .store import StateStore, utcnow

INTENTS = [
    "interested", "question", "scope_clarification", "price_objection", "timeline_objection",
    "meeting_request", "requirement_request", "rejection", "spam_irrelevant", "other",
]

# Deliberately narrow, literal phrase matching (same "occupation vs domain"
# philosophy as the qualification engine's own exclusion signals) -- a
# false classification here only affects which template gets suggested,
# never an automatic external action, but it should still be predictable
# and explainable rather than a black box.
_INTENT_SIGNALS: List[tuple] = [
    ("rejection", ["not interested", "no longer interested", "going with someone else", "decided to go with", "won't be moving forward", "not a fit"]),
    ("meeting_request", ["schedule a call", "jump on a call", "book a time", "set up a meeting", "hop on a call", "quick call this week", "available for a call"]),
    ("price_objection", ["too expensive", "over budget", "lower your price", "can you reduce", "any discount", "cheaper option", "out of our budget"]),
    ("timeline_objection", ["too long", "sooner than that", "need it faster", "tight deadline", "can you deliver sooner", "earlier timeline"]),
    ("scope_clarification", ["what exactly is included", "does this include", "scope of work", "clarify the scope", "what's included"]),
    ("requirement_request", ["send over your", "can you share", "need your resume", "portfolio link", "case studies", "references", "send documentation"]),
    ("question", ["?"]),
    ("interested", ["sounds good", "let's move forward", "i'm interested", "we're interested", "happy to proceed", "looks great", "let's proceed"]),
    ("spam_irrelevant", ["unsubscribe", "this is an automated", "no-reply", "out of office"]),
]


def classify_intent(body: str) -> str:
    """Pure, deterministic classification -- checked in a fixed priority
    order (a flat rejection/objection signal should win over an incidental
    question mark elsewhere in the same message)."""
    text = (body or "").lower()
    for intent, phrases in _INTENT_SIGNALS:
        if any(phrase in text for phrase in phrases):
            return intent
    return "other"


# Content that should never be handled by an automatic drafted reply alone
# -- Section 6's own list: unusual terms, legal/sensitive requests, identity
# verification. Detected the same way the qualification engine detects
# exclusion signals: literal phrase matching, explainable and auditable.
_SENSITIVE_CONTENT_SIGNALS = [
    "lawsuit", "legal action", "my attorney", "our lawyer", "sue us", "social security number",
    "passport number", "verify my identity", "identity verification", "wire transfer",
    "bank account number", "government id",
]


def _detect_sensitive_content(body: str) -> Optional[str]:
    text = (body or "").lower()
    for phrase in _SENSITIVE_CONTENT_SIGNALS:
        if phrase in text:
            return phrase
    return None


class ReplyAgent:
    """Drafts a reply from real, on-file context only. Returns None for
    intents where drafting anything would risk fabricating a commitment
    (spam/irrelevant, or an intent this agent has no safe template for) --
    a human-authored reply is better than an invented one."""

    def generate_reply(
        self, intent: str, opportunity: Dict[str, Any], qualification: Optional[Dict[str, Any]],
        approved_proposal_content: Optional[str], policy: Dict[str, Any],
    ) -> Optional[str]:
        title = opportunity.get("title") or "your project"
        price = (qualification or {}).get("suggested_price")
        timeline = (qualification or {}).get("suggested_timeline")

        if intent == "interested":
            proof = "the proposal I sent over" if approved_proposal_content else "my earlier proposal"
            return (
                f"Great to hear -- glad \"{title}\" sounds like a fit. Picking up from {proof}, "
                f"the next step on my end is confirming final scope and getting started. "
                f"Let me know if there's anything else you'd like clarified before we move forward."
            )
        if intent == "question" or intent == "scope_clarification":
            return (
                f"Happy to clarify -- could you tell me a bit more about what you'd like covered for "
                f"\"{title}\"? Once I know exactly what you're asking about I can give you a precise answer "
                f"rather than guessing at it."
            )
        if intent == "requirement_request":
            return (
                f"Sure, I can share what's relevant to \"{title}\" -- let me know specifically what you'd "
                f"like (portfolio examples, references, or anything else) and I'll get it over to you."
            )
        if intent == "meeting_request":
            return (
                f"Happy to hop on a call about \"{title}\" -- what does your availability look like this week? "
                f"I can work around most times."
            )
        if intent == "price_objection":
            terms_note = f" We're also flexible on payment terms ({', '.join(policy.get('allowed_payment_terms') or [])})." if policy.get("allowed_payment_terms") else ""
            price_note = f" The estimate I gave ({price}) reflects the scope as described." if price else ""
            return (
                f"I hear you on budget for \"{title}\".{price_note} Let me take a closer look at what flexibility "
                f"we have here and get back to you with options.{terms_note}"
            )
        if intent == "timeline_objection":
            timeline_note = f" (currently estimated at {timeline})" if timeline else ""
            return (
                f"Understood on timing for \"{title}\"{timeline_note} -- let me see what's realistic and I'll "
                f"follow up with a more specific answer rather than guessing here."
            )
        if intent == "rejection":
            return (
                f"Thanks for letting me know, and for considering me for \"{title}\". "
                f"If anything changes down the line or you have future work that's a fit, I'd welcome the chance to help."
            )
        # spam_irrelevant / other: no safe template -- a human should look at this one.
        return None


class ConversationError(ValueError):
    pass


class ConversationStore:
    def __init__(
        self, store: StateStore, audit: AuditLog, orchestrator: Optional[LifecycleOrchestrator] = None,
        needs_aryan=None, reply_agent: Optional[ReplyAgent] = None,
    ):
        self.store = store
        self.audit = audit
        self.orchestrator = orchestrator or LifecycleOrchestrator(store, audit)
        self.needs_aryan = needs_aryan
        self.reply_agent = reply_agent or ReplyAgent()
        self.opportunities = OpportunityStore(store, audit)

    def record_inbound(
        self, opportunity_id: str, channel: str, body: str, actor: str = "system",
        sender: Optional[str] = None, external_thread_id: Optional[str] = None,
        evidence: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        opportunity = self.store.get("rh_opportunities", opportunity_id)
        if not opportunity:
            raise ConversationError("opportunity not found")
        if not body or not body.strip():
            raise ConversationError("body is required")
        intent = classify_intent(body)
        now = utcnow()
        message_id = self.store.create("rh_conversation_messages", {
            "opportunity_id": opportunity_id, "client_id": None, "channel": channel,
            "external_thread_id": external_thread_id, "sender": sender, "direction": "INBOUND",
            "body": body, "intent": intent, "status": "RECEIVED",
            "evidence_json": json.dumps(evidence) if evidence is not None else None,
            "created_at": now, "updated_at": now,
        })
        self.audit.append("RH_CONVERSATION_MESSAGE_RECEIVED", {
            "opportunity_id": opportunity_id, "message_id": message_id, "channel": channel, "intent": intent,
        })
        # A real reply arriving is itself evidence contact was made -- best
        # effort, mirrors every other hook in this codebase.
        self.orchestrator.try_transition(opportunity_id, "REPLIED", actor=actor, reason=f"inbound message ({intent})", evidence={"message_id": message_id})

        needs_aryan_id = None
        sensitive_phrase = _detect_sensitive_content(body)
        if sensitive_phrase and self.needs_aryan is not None:
            needs_aryan_id = self.needs_aryan.create_item(
                "client_response_decision", f"Sensitive message needs review: {opportunity['title']}",
                "This message contains content that shouldn't be handled by an automatic reply -- "
                "review it and decide how to respond yourself.",
                actor=actor, rationale=f"matched: {sensitive_phrase!r}", risk="sensitive/legal content detected",
                ref_type="rh_conversation_message", ref_id=message_id,
            )
            self.store.update("rh_conversation_messages", message_id, status="NEEDS_REVIEW")

        return {"message_id": message_id, "intent": intent, "needs_aryan_id": needs_aryan_id}

    def draft_reply(self, message_id: str, actor: str = "system") -> Optional[Dict[str, Any]]:
        message = self.store.get("rh_conversation_messages", message_id)
        if not message:
            raise ConversationError("message not found")
        opportunity = self.opportunities.get(message["opportunity_id"])
        if not opportunity:
            raise ConversationError("opportunity not found")
        approved = [p for p in self.store.list("rh_proposals", "opportunity_id=? AND status=?", (message["opportunity_id"], "APPROVED"))]
        policy = SalesPolicyStore(self.store).get()
        body = self.reply_agent.generate_reply(
            message["intent"], opportunity, opportunity.get("qualification"),
            approved[-1]["content"] if approved else None, policy,
        )
        if body is None:
            return None
        now = utcnow()
        reply_id = self.store.create("rh_conversation_messages", {
            "opportunity_id": message["opportunity_id"], "client_id": message.get("client_id"),
            "channel": message["channel"], "external_thread_id": message.get("external_thread_id"),
            "sender": "Aryan", "direction": "OUTBOUND", "body": body, "intent": message["intent"],
            "status": "DRAFT", "evidence_json": None, "created_at": now, "updated_at": now,
        })
        self.audit.append("RH_CONVERSATION_REPLY_DRAFTED", {
            "opportunity_id": message["opportunity_id"], "message_id": reply_id, "in_reply_to": message_id,
        })
        return {"message_id": reply_id, "body": body}

    def mark_sent(self, message_id: str, actor: str) -> Dict[str, Any]:
        message = self.store.get("rh_conversation_messages", message_id)
        if not message:
            raise ConversationError("message not found")
        if message["direction"] != "OUTBOUND":
            raise ConversationError("only an outbound (drafted) message can be marked sent")
        if message["status"] != "DRAFT":
            raise ConversationError(f"message is already {message['status']}, not DRAFT")
        self.store.update("rh_conversation_messages", message_id, status="SENT")
        self.audit.append("RH_CONVERSATION_REPLY_SENT", {"message_id": message_id, "actor": actor})
        return self.store.get("rh_conversation_messages", message_id)

    def list_for_opportunity(self, opportunity_id: str) -> List[Dict[str, Any]]:
        return self.store.list("rh_conversation_messages", "opportunity_id=?", (opportunity_id,))
