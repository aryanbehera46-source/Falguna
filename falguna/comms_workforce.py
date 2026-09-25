"""TTT AI Communications Workforce v1 -- Receptionist, Sales Rep, Support
Rep, Account Manager.

Thin orchestration over infrastructure that already exists in this codebase
-- no new state machine, no new approval system, no new scoring engine:
  - Conversations/messages: `falguna.comms.CommsStore` (Milestone 1).
  - Approvals: the existing `NeedsAryanQueue` (falguna/ttt_hq.py) -- the
    only approval mechanism used anywhere here.
  - Lead qualification / proposals / follow-ups / pricing guardrails:
    `falguna.revenue_hunter` and `falguna.sales_ops`, unmodified.
  - Delivery status truth: `falguna.account_management.AccountManagerService`.
  - Intent classification: `falguna.conversations.classify_intent` (one
    classifier, reused everywhere a message's intent matters).

Hard invariants, enforced throughout this module (see also the security
tests in tests/test_comms_workforce.py):
  - No function here ever creates a comm_messages row with status "SENT",
    calls CommsStore in a way that marks one sent, or calls any email
    provider's send(). Every drafted reply is created DRAFT and needs an
    explicit, separate, owner-performed action to go out.
  - No function here invents a client fact, a price, a timeline, or a
    commitment. Every drafted message is built only from fields already on
    a real row (the conversation, the opportunity, the qualification, the
    active job's own evidence) -- never a guess.
  - Anything that reads as a commercial or contractual commitment escalates
    to Needs Aryan instead of being answered automatically.

Each agent's `run(conversation, actor=...)` returns a list of short,
human-readable strings describing what it did (or an empty list if there
was nothing safe/useful to do) -- this is what the end-to-end trial and the
TTT HQ "recent agent activity" view are built from.
"""

from typing import Any, Dict, List, Optional

from .account_management import AccountManagerService
from .audit import AuditLog
from .comms import CommsStore
from .conversations import classify_intent
from .conversations import _detect_sensitive_content as detect_sensitive_content
from .revenue_hunter import (
    FollowupStore, OpportunityStore, ProposalStore, QualificationStore,
)
from .risk_engine import RiskClassificationStore
from .sales_ops import NegotiationGuardrails
from .store import StateStore
from .ttt_hq import NeedsAryanQueue

ACTIVE_STATUSES = {"new", "open", "escalated"}

# Phrases that mean "someone is asking for a binding commitment" -- literal,
# explainable phrase matching, same philosophy as every other classifier in
# this codebase (QualificationEngine's exclusion signals, ConversationStore's
# sensitive-content list). A false positive here only costs one extra human
# review; a false negative could mean an invented commitment, which is not
# an acceptable trade.
_COMMITMENT_SIGNALS = [
    "final price", "we agree to", "sign the contract", "lock in this rate",
    "guarantee delivery by", "committed price", "binding agreement", "purchase order",
    "confirmed price", "accept these terms", "let's finalize", "ready to sign",
]

# What each department's Receptionist checklist asks about when the
# corresponding structured field is missing. Grounded in real fields on the
# linked opportunity/application -- never a guess at what might be missing.
_DEPARTMENT_LABELS = {
    "general": "our team", "sales": "our sales team", "support": "our support team",
    "billing": "billing", "careers": "our hiring team", "projects": "your account team",
    "media": "our media team",
}


def _visible_messages(conversation: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [m for m in (conversation.get("messages") or []) if not m.get("is_internal_note")]


def _last_inbound_body(conversation: Dict[str, Any]) -> Optional[str]:
    inbound = [m for m in (conversation.get("messages") or []) if m["direction"] == "INBOUND"]
    return inbound[-1]["body"] if inbound else None


def _already_responded(conversation: Dict[str, Any]) -> bool:
    """True once the last customer-visible message is our own OUTBOUND
    reply -- the signal every agent below uses to avoid drafting a second,
    duplicate response before the first one has even been reviewed."""
    visible = _visible_messages(conversation)
    return bool(visible) and visible[-1]["direction"] == "OUTBOUND"


def _has_note_prefix(conversation: Dict[str, Any], prefix: str) -> bool:
    return any(
        m.get("is_internal_note") and (m.get("body") or "").startswith(prefix)
        for m in (conversation.get("messages") or [])
    )


def _contains_commitment_signal(body: Optional[str]) -> Optional[str]:
    text = (body or "").lower()
    for phrase in _COMMITMENT_SIGNALS:
        if phrase in text:
            return phrase
    return None


class ReceptionistAgent:
    """Role 1: classifies a genuine incoming enquiry, confirms department
    routing, drafts a truthful acknowledgement, and calls out any
    information still missing -- grounded only in real structured fields
    already on file (the opportunity's or application's own columns), not
    a guess about what the free-text message did or didn't mention."""

    def __init__(self, store: StateStore, comms: CommsStore):
        self.store = store
        self.comms = comms

    def _missing_for_sales(self, opportunity_id: Optional[str]) -> List[str]:
        if not opportunity_id:
            return []
        opp = self.store.get("rh_opportunities", opportunity_id)
        if not opp:
            return []
        missing = []
        if not opp.get("budget_rate"):
            missing.append("a rough budget range")
        if not opp.get("deadline") and not opp.get("urgency"):
            missing.append("your target timeline")
        if not opp.get("contract_type"):
            missing.append("whether this is a fixed-scope project or ongoing work")
        return missing

    def _missing_for_careers(self, application_id: Optional[str]) -> List[str]:
        if not application_id:
            return []
        app = self.store.get("site_applications", application_id)
        if not app:
            return []
        return ["a resume (PDF or Word)"] if not app.get("resume_filename") else []

    def _missing_requirements(self, conversation: Dict[str, Any]) -> List[str]:
        department = conversation["department"]
        if department == "sales":
            return self._missing_for_sales(conversation.get("linked_opportunity_id"))
        if department == "careers":
            return self._missing_for_careers(conversation.get("linked_application_id"))
        if department in ("general", "support", "billing", "media"):
            body = _last_inbound_body(conversation) or ""
            if len(body.split()) < 12:
                return ["a bit more detail on what this is about"]
        return []


    def run(self, conversation: Dict[str, Any], actor: str = "ai_workforce") -> List[str]:
        actions: List[str] = []
        if conversation["status"] not in ACTIVE_STATUSES or _already_responded(conversation):
            return actions
        if not conversation.get("messages"):
            return actions

        department = conversation["department"]
        contact = conversation.get("primary_contact") or {}
        name = (contact.get("name") or "").split(" ")[0].strip() if contact.get("name") else None
        dept_label = _DEPARTMENT_LABELS.get(department, "our team")
        missing = self._missing_requirements(conversation)

        greeting = f"Hi {name}, " if name else "Hi, "
        body = (
            f"{greeting}thanks for reaching out -- this has been routed to {dept_label} "
            f"and someone will follow up personally."
        )
        if missing:
            body += " To help us respond quickly, could you also share " + ", ".join(missing) + "?"

        self.comms.add_message(
            conversation["id"], "OUTBOUND", body, sender_agent="AI Receptionist",
            actor=actor,
        )
        actions.append("drafted acknowledgement" + (" (requested missing details)" if missing else ""))

        if not conversation.get("assigned_agent"):
            self.comms.assign(conversation["id"], "AI Receptionist", actor)
            actions.append("assigned to AI Receptionist pending specialist handoff")

        return actions


class SalesRepAgent:
    """Role 2: qualifies real leads against the linked opportunity record,
    prepares tailored proposals and follow-ups through the existing Revenue
    Hunter engines, and escalates -- rather than answers -- anything that
    reads as a pricing or contractual commitment. Never negotiates and
    never drafts a number of its own; `NegotiationGuardrails` is the one
    place a proposed term is ever checked, exactly as it already is for the
    rest of Revenue Hunter."""

    def __init__(self, store: StateStore, audit: AuditLog, comms: CommsStore, needs_aryan: NeedsAryanQueue):
        self.store = store
        self.comms = comms
        self.needs_aryan = needs_aryan
        self.opportunities = OpportunityStore(store, audit)
        self.proposals = ProposalStore(store, audit, needs_aryan=needs_aryan)
        self.followups = FollowupStore(store, audit)
        self.guardrails = NegotiationGuardrails(store, audit, needs_aryan=needs_aryan)
        self.qualifications = QualificationStore(store, audit)

    def _qualify(self, opportunity: Dict[str, Any], actor: str) -> Dict[str, Any]:
        """Uses the real, persisting qualification step (same one the TTT
        HQ "Qualify" button calls) rather than a private, unpersisted
        score -- this is what actually moves New -> Qualified so
        ProposalStore.generate's own Qualified -> Proposal Ready bump, and
        this agent's own follow-up tracking below, both see real state."""
        existing = opportunity.get("qualification")
        if existing:
            return existing
        self.qualifications.qualify(opportunity["id"], actor=actor)
        return self.opportunities.get(opportunity["id"])["qualification"]

    def run(self, conversation: Dict[str, Any], actor: str = "ai_workforce") -> List[str]:
        actions: List[str] = []
        opp_id = conversation.get("linked_opportunity_id")
        if not opp_id:
            return actions
        opportunity = self.opportunities.get(opp_id)
        if not opportunity:
            return actions
        qualification = self._qualify(opportunity, actor)
        opportunity = self.opportunities.get(opp_id)  # re-fetch: stage/qualification may have just changed

        last_inbound = _last_inbound_body(conversation)
        commitment = _contains_commitment_signal(last_inbound)
        if commitment and conversation["status"] != "pending_approval":
            self.guardrails.evaluate(opp_id, actor, price=qualification.get("suggested_price"))
            esc = self.comms.escalate(
                conversation["id"], f"Pricing/contract commitment requested: {opportunity['title']}",
                "The client's latest message uses pricing or contractual commitment language "
                f"(matched: {commitment!r}). Nothing has been agreed to on our side -- review and "
                "decide how to respond.",
                actor=actor, kind="negotiation_response_approval",
                rationale=f"inbound message matched {commitment!r}", risk="unauthorized commercial commitment",
            )
            actions.append(f"escalated pricing/contract commitment (needs_aryan={esc['needs_aryan_id']})")
            return actions


        if qualification.get("recommendation") == "PURSUE" and not opportunity.get("proposals"):
            result = self.proposals.generate(opp_id, kind="detailed", actor=actor)
            self.comms.add_message(
                conversation["id"], "OUTBOUND",
                f"Draft proposal prepared for review (Revenue Hunter proposal {result['proposal_id']}, "
                f"opportunity {opp_id}). Awaiting approval before anything is sent.",
                kind="note", is_internal_note=True, sender_agent="AI Sales Rep", actor=actor,
            )
            actions.append(
                f"drafted proposal {result['proposal_id']} for opportunity {opp_id} "
                f"(needs_aryan={result['needs_aryan_id']})"
            )
            opportunity = self.opportunities.get(opp_id)  # picks up the Qualified -> Proposal Ready bump

        stage = opportunity.get("stage")
        pending_followups = [f for f in (opportunity.get("followups") or []) if f["status"] == "DRAFT"]
        if stage in ("Applied/Sent", "Proposal Ready", "Replied", "Negotiating") and not pending_followups:
            kind = "response_followup" if stage in ("Applied/Sent", "Proposal Ready") else "proposal_followup"
            followup_id = self.followups.generate(opp_id, kind)
            actions.append(f"drafted {kind} {followup_id} for opportunity {opp_id}")

        return actions


_SUPPORT_REPLY_TEMPLATES = {
    "question": "Thanks for the question -- could you give a bit more detail so we answer the right thing?",
    "scope_clarification": "Happy to clarify what's included -- let us know exactly what you'd like covered.",
    "requirement_request": "Sure, let us know specifically what you need and we'll get it over to you.",
    "meeting_request": "Happy to get a call scheduled -- what does your availability look like?",
    "interested": "Great to hear -- someone from our team will follow up with next steps shortly.",
}


class SupportAgent:
    """Role 3: handles authorized routine enquiries with a safe, templated
    acknowledgement, opens/keeps the conversation itself as the support
    case record (comm_conversations already models exactly that -- status,
    priority, SLA, assignment -- so this does not introduce a second,
    competing "ticket" table), surfaces the customer's own on-file history
    as permitted context, and escalates anything it has no safe template
    for or that matches sensitive/legal content signals."""

    def __init__(self, store: StateStore, audit: AuditLog, comms: CommsStore, needs_aryan: NeedsAryanQueue):
        self.store = store
        self.comms = comms
        self.needs_aryan = needs_aryan

    def _customer_context_note(self, conversation: Dict[str, Any]) -> Optional[str]:
        contact_id = conversation.get("primary_contact_id")
        if not contact_id:
            return None
        prior = [
            c for c in self.store.list("comm_conversations", "primary_contact_id=?", (contact_id,))
            if c["id"] != conversation["id"]
        ]
        contact = conversation.get("primary_contact") or {}
        org = conversation.get("organization") or {}
        parts = [f"Known contact: {contact.get('name') or contact.get('email') or contact_id}"]
        if org.get("name"):
            parts.append(f"organization {org['name']}")
        parts.append(f"{len(prior)} prior conversation(s) on file")
        return " -- ".join(parts)


    def run(self, conversation: Dict[str, Any], actor: str = "ai_workforce") -> List[str]:
        actions: List[str] = []
        if conversation["department"] not in ("support", "billing"):
            return actions
        if not conversation.get("assigned_agent"):
            self.comms.assign(conversation["id"], "AI Support Rep", actor)
            actions.append("assigned to AI Support Rep")

        context_note = self._customer_context_note(conversation)
        if context_note and not _has_note_prefix(conversation, "Known contact:"):
            self.comms.add_message(
                conversation["id"], "OUTBOUND", context_note, kind="note", is_internal_note=True,
                sender_agent="AI Support Rep", actor=actor,
            )
            actions.append("retrieved and recorded permitted customer context")

        if conversation["status"] not in ACTIVE_STATUSES or _already_responded(conversation):
            return actions
        last_inbound = _last_inbound_body(conversation)
        if not last_inbound:
            return actions

        sensitive = detect_sensitive_content(last_inbound)
        intent = classify_intent(last_inbound)
        template = _SUPPORT_REPLY_TEMPLATES.get(intent)

        if sensitive or template is None:
            reason = f"message matched sensitive/legal content ({sensitive!r})" if sensitive else \
                f"no safe automatic reply template for intent {intent!r} -- needs a human read"
            esc = self.comms.escalate(
                conversation["id"], f"Support enquiry needs review: {conversation.get('subject') or conversation['id']}",
                reason, actor=actor, kind="communications_approval", risk=reason,
            )
            actions.append(f"escalated unresolved enquiry (needs_aryan={esc['needs_aryan_id']})")
            return actions

        self.comms.add_message(
            conversation["id"], "OUTBOUND", template, sender_agent="AI Support Rep", actor=actor,
        )
        actions.append(f"drafted routine reply (intent={intent})")
        return actions


class AccountManagerAgent:
    """Role 4: tracks genuine client conversations against real delivery
    evidence (`AccountManagerService`, reading Falguna Engineering's own
    mission/run/checkpoint tables -- never a guessed percentage or date),
    drafts a truthful status update when the client asks, and escalates a
    real blocker via the same Needs Aryan queue everything else uses."""

    def __init__(self, store: StateStore, audit: AuditLog, comms: CommsStore, needs_aryan: NeedsAryanQueue):
        self.store = store
        self.comms = comms
        self.needs_aryan = needs_aryan
        self.account_manager = AccountManagerService(store, audit)

    def _active_job_for(self, opportunity_id: str) -> Optional[Dict[str, Any]]:
        jobs = self.store.list("rh_active_jobs", "opportunity_id=?", (opportunity_id,))
        return jobs[-1] if jobs else None

    def run(self, conversation: Dict[str, Any], actor: str = "ai_workforce") -> List[str]:
        actions: List[str] = []
        opp_id = conversation.get("linked_opportunity_id")
        if not opp_id:
            return actions
        job = self._active_job_for(opp_id)
        if not job:
            return actions

        status = self.account_manager.status_for_active_job(job["id"])

        if status["blocked"] and conversation["status"] != "pending_approval":
            esc = self.comms.escalate(
                conversation["id"], f"Account issue needs a decision: {status.get('title') or opp_id}",
                f"Delivery is blocked -- {status['blocker_reason']}. Evidence: {'; '.join(status['evidence'])}.",
                actor=actor, kind="client_response_decision", rationale=status["blocker_reason"],
                risk="client-visible delivery blocker",
            )
            actions.append(f"escalated account-level blocker (needs_aryan={esc['needs_aryan_id']})")

        if conversation["status"] in ACTIVE_STATUSES and not _already_responded(conversation):
            last_inbound = _last_inbound_body(conversation)
            if last_inbound and classify_intent(last_inbound) in ("question", "scope_clarification"):
                update = self.account_manager.client_update_draft(job["id"])
                self.comms.add_message(
                    conversation["id"], "OUTBOUND", update, sender_agent="AI Account Manager", actor=actor,
                )
                actions.append("drafted evidence-based client status update")

        return actions


# Department -> specialist role, applied after the Receptionist's own pass.
# "general"/"media" get only the Receptionist -- there's no specialist role
# for them in this mission, and inventing one would be exactly the
# "unnecessary new architecture" the mission asked to avoid.
_SPECIALIST_AGENT_CLASSES = {
    "sales": SalesRepAgent,
    "support": SupportAgent,
    "billing": SupportAgent,
    "projects": AccountManagerAgent,
}


def _classify_unrisked_outbound_drafts(
    store: StateStore, audit: AuditLog, comms: CommsStore, needs_aryan: NeedsAryanQueue,
    conversation_id: str, actor: str,
) -> List[str]:
    """Milestone 2 integration point: every OUTBOUND DRAFT message an agent
    just drafted (or left unclassified from an earlier, interrupted pass)
    gets a deterministic risk classification -- HIGH risk escalates through
    the same single NeedsAryanQueue used everywhere else in this module.
    Idempotent: a message that already has a comm_risk_events row is left
    alone, so re-running the workforce never reclassifies or double-escalates
    the same draft. This never changes a message's status -- classification
    is visibility, not a second send gate (see risk_engine.py's own note)."""
    risk = RiskClassificationStore(store, audit, needs_aryan)
    conversation = comms.get_conversation(conversation_id)
    notes: List[str] = []
    for message in conversation.get("messages", []):
        if message["direction"] != "OUTBOUND" or message["status"] != "DRAFT" or message["is_internal_note"]:
            continue
        if risk.list_for_subject("comm_message", message["id"]):
            continue
        result = risk.classify(
            "comm_message", message["id"], message["body"], actor=actor,
            title=f"High-risk draft reply needs review (conversation {conversation_id})",
        )
        if result["risk"] == "HIGH":
            notes.append(f"Risk classified HIGH for draft message {message['id']} -- escalated to Needs Aryan")
        else:
            notes.append(f"Risk classified {result['risk']} for draft message {message['id']}")
    return notes


def run_agent_for_conversation(
    store: StateStore, audit: AuditLog, conversation_id: str, actor: str = "ai_workforce",
    needs_aryan: Optional[NeedsAryanQueue] = None,
) -> Dict[str, Any]:
    """Runs the Receptionist, then the department's specialist (if any),
    once, for a single conversation. Safe to call repeatedly: every agent
    above no-ops once the conversation is already answered, escalated,
    resolved, or closed -- so a second call on an unchanged conversation
    reports zero new actions rather than drafting a duplicate."""
    needs_aryan = needs_aryan or NeedsAryanQueue(store, audit)
    comms = CommsStore(store, audit, needs_aryan)
    conversation = comms.get_conversation(conversation_id)
    if not conversation:
        raise ValueError("conversation not found")

    department = conversation["department"]
    if conversation["status"] not in ACTIVE_STATUSES:
        return {"conversation_id": conversation_id, "department": department, "actions": [],
                "note": f"skipped -- conversation is {conversation['status']}"}

    actions = ReceptionistAgent(store, comms).run(conversation, actor=actor)
    if actions:
        conversation = comms.get_conversation(conversation_id)

    specialist_cls = _SPECIALIST_AGENT_CLASSES.get(department)
    if specialist_cls is not None:
        specialist = specialist_cls(store, audit, comms, needs_aryan)
        actions += specialist.run(conversation, actor=actor)

    actions += _classify_unrisked_outbound_drafts(store, audit, comms, needs_aryan, conversation_id, actor)

    return {"conversation_id": conversation_id, "department": department, "actions": actions}


def run_workforce_pass(
    store: StateStore, audit: AuditLog, actor: str = "ai_workforce", limit: int = 50,
    needs_aryan: Optional[NeedsAryanQueue] = None,
) -> Dict[str, Any]:
    """Runs the full workforce over every currently-active conversation --
    the batch entrypoint behind TTT HQ's "Run AI Workforce now" action."""
    needs_aryan = needs_aryan or NeedsAryanQueue(store, audit)
    comms = CommsStore(store, audit, needs_aryan)
    candidates = [c for c in comms.list_conversations(limit=limit) if c["status"] in ACTIVE_STATUSES]
    results = [
        run_agent_for_conversation(store, audit, c["id"], actor=actor, needs_aryan=needs_aryan)
        for c in candidates
    ]
    acted = [r for r in results if r["actions"]]
    return {
        "conversations_checked": len(results), "conversations_acted_on": len(acted),
        "results": results,
    }
