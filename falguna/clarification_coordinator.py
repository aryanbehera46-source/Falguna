"""Phase 5 Final Client Experience, Section 5 -- Minimum-Interruption /
Anti-Annoyance Coordinator.

Before contacting a customer with questions, this reads the entire
conversation's stored language interpretations (falguna/language.py),
removes anything already asked, groups whatever genuinely remains into
one consolidated draft, and is capable of concluding that no contact is
needed at all right now. It never asks one question at a time when
several are outstanding, and it never re-asks something the conversation
already covers.

Deliberately deterministic and explainable rather than a second model
call: "has this exact question already been put to the customer" is
answered by a precise, inspectable text comparison against this
conversation's own outbound messages -- not a fuzzy LLM guess layered on
top of another LLM guess (the interpretation itself). This matches the
rest of this codebase's "explainable, not a black box" posture (see e.g.
ConversationStore's confidence labels, capacity.py's named thresholds).

This module only ever creates a DRAFT message (the same draft-then-
approve convention every outbound message in falguna/comms.py already
follows) -- it never sends anything itself, and
comms_workforce.py's existing single-owner/no-duplicate-reply machinery
still governs whether/when that draft actually gets approved and sent.
"""

import json
from typing import Any, Dict, List, Optional

from .audit import AuditLog
from .comms import CommsStore
from .language import LanguageInterpretationStore
from .store import StateStore

_PENDING_MESSAGE_STATUSES = {"DRAFT", "APPROVED"}
_CLARIFICATION_SOURCE_REF_TYPE = "clarification_coordinator"


def _normalize(text: Optional[str]) -> str:
    return " ".join((text or "").strip().lower().split())


class ClarificationCoordinator:
    def __init__(
        self, store: StateStore, audit: AuditLog,
        comms: Optional[CommsStore] = None,
        interpretations: Optional[LanguageInterpretationStore] = None,
    ):
        self.store = store
        self.audit = audit
        self.comms = comms or CommsStore(store, audit)
        self.interpretations = interpretations or LanguageInterpretationStore(store, audit)

    def assess_conversation(self, conversation_id: str) -> Dict[str, Any]:
        """Read-only: decides what, if anything, still genuinely needs to
        be asked. Never writes anything -- see draft_consolidated_clarification
        for the one write this module performs."""
        conv = self.store.get("comm_conversations", conversation_id)
        if not conv:
            raise ValueError("conversation not found")
        interpretations = self.interpretations.for_conversation(conversation_id)
        outbound_messages = [
            m for m in self.store.list("comm_messages", "conversation_id=?", (conversation_id,))
            if m["direction"] == "OUTBOUND" and not m["is_internal_note"]
        ]
        already_asked_blob = "\n".join(_normalize(m["body"]) for m in outbound_messages)

        seen = set()
        outstanding: List[str] = []
        source_ids: List[str] = []
        for interp in interpretations:
            if interp["unavailable"] or not interp["clarification_needed"]:
                continue
            questions = json.loads(interp["unresolved_questions_json"])
            touched = False
            for question in questions:
                norm_q = _normalize(question)
                if not norm_q or norm_q in seen:
                    continue
                touched = True
                if already_asked_blob and norm_q in already_asked_blob:
                    continue
                seen.add(norm_q)
                outstanding.append(question)
            if touched:
                source_ids.append(interp["id"])

        if not outstanding:
            return {
                "conversation_id": conversation_id, "action": "NO_ACTION_NEEDED",
                "consolidated_questions": [], "source_interpretation_ids": source_ids,
                "reason": (
                    "Everything needed has already been asked, or nothing genuinely "
                    "requires the customer's input right now -- no further contact is needed."
                ),
            }
        return {
            "conversation_id": conversation_id, "action": "SEND_CONSOLIDATED_CLARIFICATION",
            "consolidated_questions": outstanding, "source_interpretation_ids": source_ids,
            "reason": (
                f"{len(outstanding)} question(s) across {len(source_ids)} interpreted message(s) "
                "still genuinely need an answer -- grouped into one message rather than asked one at a time."
            ),
        }

    def draft_consolidated_clarification(self, conversation_id: str, actor: str = "system") -> Optional[Dict[str, Any]]:
        """The one write this module performs: a single consolidated
        DRAFT outbound message listing every still-outstanding question.
        Returns None (and writes nothing) when assess_conversation()
        concludes no contact is needed. Idempotent: if a clarification
        draft from this coordinator is already pending (DRAFT/APPROVED,
        not yet sent) in this conversation, that existing draft is
        returned unchanged rather than creating a duplicate.

        The idempotency check runs BEFORE assess_conversation() is called.
        This ordering matters: a pending draft's own body text quotes the
        very questions it is asking, so if assess_conversation() ran first
        it would see those questions as "already asked" (they appear
        verbatim in the conversation's outbound messages) and conclude
        NO_ACTION_NEEDED, short-circuiting this method to None before it
        ever reached the existing-draft lookup below. Checking for an
        existing pending draft first avoids that self-invalidation."""
        existing_pending = [
            m for m in self.store.list("comm_messages", "conversation_id=?", (conversation_id,))
            if m["direction"] == "OUTBOUND" and m.get("source_ref_type") == _CLARIFICATION_SOURCE_REF_TYPE
            and m["status"] in _PENDING_MESSAGE_STATUSES
        ]
        if existing_pending:
            return existing_pending[-1]
        assessment = self.assess_conversation(conversation_id)
        if assessment["action"] != "SEND_CONSOLIDATED_CLARIFICATION":
            return None
        body_lines = ["To keep things moving, could you help us with a couple of quick points?", ""]
        body_lines += [f"- {q}" for q in assessment["consolidated_questions"]]
        body = "\n".join(body_lines)
        message = self.comms.add_message(
            conversation_id, "OUTBOUND", body, kind="message", actor=actor,
            source_ref_type=_CLARIFICATION_SOURCE_REF_TYPE, source_ref_id=None,
        )
        self.audit.append("COMM_CONSOLIDATED_CLARIFICATION_DRAFTED", {
            "conversation_id": conversation_id, "message_id": message["id"],
            "question_count": len(assessment["consolidated_questions"]),
            "source_interpretation_ids": assessment["source_interpretation_ids"], "actor": actor,
        })
        return message
