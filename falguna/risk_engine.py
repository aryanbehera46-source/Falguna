"""TTT Communications V2 -- Milestone 2: Outbound Approval Engine.

A deterministic, explainable risk classifier for any outbound communication
(a comms message, an email draft, a proposal) -- same phrase-matching
philosophy as every other classifier in this codebase
(QualificationEngine's exclusion signals, conversations.classify_intent,
comms_workforce's commitment-signal detector). No model call, no
network -- a risk level must be reproducible and auditable.

Every classification is persisted as its own row (same pattern as
`sales_ops.NegotiationGuardrails` persisting every term evaluation to
`rh_negotiation_terms`) so TTT HQ and any later audit can see exactly what
was classified, why, and what happened to it -- not just the final state.

HIGH risk always creates a Needs Aryan item -- the existing, single
approval queue, never a second one. LOW/MEDIUM are recorded but not
escalated on their own: every outbound message in this codebase is already
DRAFT-only and requires an explicit human "mark sent" action regardless of
risk level (see CommsStore.mark_message_sent / EmailStore.mark_sent), so
risk classification adds visibility and a mandatory human checkpoint for
HIGH risk, not a new send gate.
"""

import json
from typing import Any, Dict, List, Optional

from .audit import AuditLog
from .store import StateStore, utcnow
from .ttt_hq import NeedsAryanQueue

RISK_LEVELS = ("LOW", "MEDIUM", "HIGH")

# Ordered high -> low: the first matching level wins, so a message that
# reads as both a routine update AND a contract commitment is treated as
# the commitment -- the safer failure mode.
_HIGH_RISK_SIGNALS = [
    "sign the contract", "binding agreement", "purchase order", "final price",
    "confirmed price", "we agree to", "lock in this rate", "guarantee delivery by",
    "accept these terms", "let's finalize", "ready to sign", "committed price",
    "refund", "full refund", "money back", "offer you the position", "job offer",
    "employment offer", "we guarantee", "legally binding", "our lawyer", "legal action",
    "wire transfer", "bank account number", "sue us", "lawsuit",
    "regulatory", "sec filing", "securities", "guaranteed returns", "guaranteed return",
]
_MEDIUM_RISK_SIGNALS = [
    "here's our estimate", "estimated price", "quote is", "discount", "% off",
    "workaround", "renewal", "renew your", "change of scope", "scope change",
    "additional cost", "extend the timeline", "revised timeline",
]
_LOW_RISK_SIGNALS = [
    "thanks for reaching out", "acknowledg", "could you also share", "could you share",
    "what does your availability", "here's the current status", "currently in progress",
    "here's an update", "happy to clarify", "let us know",
]


def classify_message_risk(body: Optional[str], context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Pure function: never touches the store. `context` can carry
    structured signals the caller already knows (e.g. {"is_refund": True})
    that this classifier checks in addition to the free-text phrase scan,
    without ever inventing a fact of its own."""
    text = (body or "").lower()
    context = context or {}
    reasons: List[str] = []

    if context.get("final_price") is not None and context.get("policy_violation"):
        reasons.append("proposed price/terms fall outside the saved sales policy")
    for phrase in _HIGH_RISK_SIGNALS:
        if phrase in text:
            reasons.append(f"matched high-risk phrase: {phrase!r}")
    if reasons:
        return {"risk": "HIGH", "reasons": reasons}

    for phrase in _MEDIUM_RISK_SIGNALS:
        if phrase in text:
            reasons.append(f"matched medium-risk phrase: {phrase!r}")
    if reasons:
        return {"risk": "MEDIUM", "reasons": reasons}

    for phrase in _LOW_RISK_SIGNALS:
        if phrase in text:
            reasons.append(f"matched low-risk phrase: {phrase!r}")
    if not reasons:
        reasons.append("no risk signal matched -- treated as routine/low risk by default")
    return {"risk": "LOW", "reasons": reasons}


class RiskError(ValueError):
    pass


class RiskClassificationStore:
    """Persists every classification (`comm_risk_events`) and, for HIGH
    risk, escalates through the one existing NeedsAryanQueue -- kind
    "communications_approval", same as every other comms escalation."""

    def __init__(self, store: StateStore, audit: AuditLog, needs_aryan: Optional[NeedsAryanQueue] = None):
        self.store = store
        self.audit = audit
        self.needs_aryan = needs_aryan or NeedsAryanQueue(store, audit)

    def classify(
        self, subject_type: str, subject_id: str, body: Optional[str], actor: str = "system",
        context: Optional[Dict[str, Any]] = None, title: Optional[str] = None,
    ) -> Dict[str, Any]:
        result = classify_message_risk(body, context)
        now = utcnow()
        event_id = self.store.create("comm_risk_events", {
            "subject_type": subject_type, "subject_id": subject_id, "risk": result["risk"],
            "reasons_json": json.dumps(result["reasons"]), "actor": actor,
            "needs_aryan_id": None, "final_action": None, "decided_by": None, "decided_at": None,
            "created_at": now, "updated_at": now,
        })
        self.audit.append("COMM_RISK_CLASSIFIED", {
            "event_id": event_id, "subject_type": subject_type, "subject_id": subject_id,
            "risk": result["risk"], "actor": actor,
        })
        needs_aryan_id = None
        if result["risk"] == "HIGH":
            needs_aryan_id = self.needs_aryan.create_item(
                "communications_approval", title or f"High-risk outbound content needs review ({subject_type})",
                "This was classified HIGH risk and requires explicit approval before anything is sent: "
                + "; ".join(result["reasons"]),
                actor=actor, rationale="; ".join(result["reasons"]), risk="HIGH",
                ref_type=subject_type, ref_id=subject_id,
            )
            self.store.update("comm_risk_events", event_id, needs_aryan_id=needs_aryan_id)
        return {**result, "event_id": event_id, "needs_aryan_id": needs_aryan_id}

    def record_decision(self, event_id: str, action: str, actor: str) -> Dict[str, Any]:
        """Called after a human decides the linked Needs Aryan item (or,
        for LOW/MEDIUM events with no item, after a human otherwise acts on
        the drafted content) -- keeps the classification's own audit trail
        complete rather than only living on the Needs Aryan item."""
        event = self.store.get("comm_risk_events", event_id)
        if not event:
            raise RiskError("risk event not found")
        self.store.update("comm_risk_events", event_id, final_action=action, decided_by=actor, decided_at=utcnow())
        self.audit.append("COMM_RISK_DECIDED", {"event_id": event_id, "action": action, "actor": actor})
        return self.store.get("comm_risk_events", event_id)

    def list_for_subject(self, subject_type: str, subject_id: str) -> List[Dict[str, Any]]:
        return self.store.list("comm_risk_events", "subject_type=? AND subject_id=?", (subject_type, subject_id))

    def list_recent(self, limit: int = 50) -> List[Dict[str, Any]]:
        rows = list(reversed(self.store.list("comm_risk_events")))
        return rows[:limit]
