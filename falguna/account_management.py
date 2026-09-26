"""TTT Autonomous Revenue-to-Delivery Loop v1 -- Account Management
(Section 10, Pass D).

`AccountManagerService` monitors real Falguna Engineering delivery state --
reading missions/requirements/tasks/runs/checkpoints/supervisor_states/
approvals read-only, the exact same join pattern
`ttt_hq.NeedsAryanQueue._engineering_items` already uses -- and turns that
into a truthful, evidence-backed status per Active Job. It never estimates
progress percentages, invents timelines, or reports anything not directly
supported by a real row in those tables: no mission means NOT_STARTED, no
run means NOT_STARTED, a run sitting in NEEDS_APPROVAL means blocked with
the real reason attached. `client_update_draft` turns that same evidence
into a plain-language update -- still grounded only in what
`status_for_active_job` actually found.

`scope_signals` is intentionally narrow: it flags real inbound conversation
messages (Section 6) whose classified intent suggests a scope question or a
new requirement, that arrived *after* the deal closed -- a lightweight,
evidence-based nudge for a human to look closer, not an automated scope
determination.
"""

import json
from typing import Any, Dict, List, Optional

from .audit import AuditLog
from .comms import CommsStore
from .conversations import classify_intent
from .customer_context import CustomerContextService
from .store import StateStore
from .ttt_hq import NeedsAryanQueue

# TTT Communications V2, Milestone 7: an opportunity whose engagement is
# genuinely finished -- delivered, invoiced, and paid -- is real evidence
# an account manager should look at renewal, never a guess about what the
# client might want next.
_RENEWAL_CANDIDATE_STATES = {"PAID", "RETAIN"}

BLOCKED_RUN_STATES = {"FAILED", "QUARANTINED"}

_RUN_STATUS_TO_DELIVERY_STATUS = {
    "CREATED": "STARTED", "PLANNING": "IN_PROGRESS", "WORKING": "IN_PROGRESS",
    "VERIFYING": "IN_PROGRESS", "REVIEWING": "IN_PROGRESS", "AWAITING_APPROVAL": "AWAITING_APPROVAL",
    "DONE_CANDIDATE": "DELIVERED_CANDIDATE", "FAILED": "BLOCKED", "PAUSED": "PAUSED",
    "CANCELLED": "CANCELLED", "QUARANTINED": "BLOCKED",
}

_SCOPE_SIGNAL_INTENTS = {"requirement_request", "scope_clarification"}


class AccountManagementError(ValueError):
    pass


class AccountManagerService:
    def __init__(self, store: StateStore, audit: AuditLog, needs_aryan: Optional[NeedsAryanQueue] = None):
        self.store = store
        self.audit = audit
        # Optional: only constructed when account_summary() actually needs
        # a CommsStore/CustomerContextService. Every existing caller
        # (comms_workforce.AccountManagerAgent, hq_web.py's routes,
        # sales_manager.py) already constructs this with just
        # (store, audit), so this stays backward compatible rather than
        # forcing every call site to also thread a queue through.
        self._needs_aryan = needs_aryan

    def status_for_active_job(self, active_job_id: str) -> Dict[str, Any]:
        job = self.store.get("rh_active_jobs", active_job_id)
        if not job:
            raise AccountManagementError("active job not found")

        payload = json.loads(job["job_payload_json"]) if job.get("job_payload_json") else {}
        base = {
            "active_job_id": active_job_id, "opportunity_id": job["opportunity_id"],
            "handoff_status": job["handoff_status"], "title": payload.get("title"),
            "deadline": payload.get("deadline"), "mission_id": job.get("mission_id"),
        }

        if not job.get("mission_id"):
            return {
                **base, "mission_status": None, "run_status": None, "delivery_status": "NOT_STARTED",
                "blocked": False, "blocker_reason": None, "last_milestone": None, "evidence": [],
            }

        mission = self.store.get("missions", job["mission_id"])
        requirements = self.store.list("requirements", "mission_id=?", (job["mission_id"],))
        requirement = requirements[-1] if requirements else None
        tasks = self.store.list("tasks", "requirement_id=?", (requirement["id"],)) if requirement else []
        task = tasks[-1] if tasks else None
        runs = self.store.list("runs", "task_id=?", (task["id"],)) if task else []
        run = runs[-1] if runs else None

        if not run:
            return {
                **base, "mission_status": mission["status"] if mission else None, "run_status": None,
                "delivery_status": "NOT_STARTED", "blocked": False, "blocker_reason": None,
                "last_milestone": None, "evidence": [f"mission {job['mission_id']} has no run yet"],
            }

        checkpoint = self.store.latest_checkpoint(run["id"])
        evidence = [f"run {run['id']} status={run['status']}"]
        if checkpoint:
            evidence.append(f"last checkpoint: {checkpoint['stage']}")

        blocked = False
        blocker_reason = None
        if run["status"] in BLOCKED_RUN_STATES:
            blocked = True
            blocker_reason = run.get("error") or f"run is {run['status']}"
            evidence.append(f"error on file: {run['error']}" if run.get("error") else f"run status is {run['status']}")
        else:
            pending_states = self.store.list("supervisor_states", "run_id=? AND outcome_class=?", (run["id"], "NEEDS_APPROVAL"))
            if pending_states:
                pending_merge = self.store.list("approvals", "run_id=? AND kind=? AND status=?", (run["id"], "PROTECTED_BRANCH_MERGE", "PENDING"))
                blocked = True
                blocker_reason = "awaiting merge approval" if pending_merge else (pending_states[-1].get("eligibility_reason") or "needs a decision in Falguna Engineering")
                evidence.append(f"supervisor_state: {pending_states[-1]['category']}")

        return {
            **base, "mission_status": mission["status"] if mission else None, "run_status": run["status"],
            "delivery_status": _RUN_STATUS_TO_DELIVERY_STATUS.get(run["status"], run["status"]),
            "blocked": blocked, "blocker_reason": blocker_reason,
            "last_milestone": checkpoint["stage"] if checkpoint else None, "evidence": evidence,
        }

    def client_update_draft(self, active_job_id: str) -> str:
        """A truthful, plain-language update -- grounded entirely in
        `status_for_active_job`'s own evidence, never an invented
        percentage or promised date that isn't actually on file."""
        status = self.status_for_active_job(active_job_id)
        title = status.get("title") or "your project"
        if status["delivery_status"] == "NOT_STARTED":
            return f"Work on \"{title}\" hasn't started yet -- I'll follow up as soon as it's underway."
        if status["blocked"]:
            return f"\"{title}\" hit a snag ({status['blocker_reason']}) -- I'm looking into it and will update you shortly."
        if status["delivery_status"] == "DELIVERED_CANDIDATE":
            return f"\"{title}\" has reached a delivery candidate stage -- final review is in progress."
        if status["delivery_status"] == "AWAITING_APPROVAL":
            return f"\"{title}\" is ready for a final decision on my end before it goes out."
        milestone = status.get("last_milestone") or status.get("run_status")
        return f"\"{title}\" is actively in progress (currently: {milestone})."

    def scope_signals(self, opportunity_id: str) -> List[Dict[str, Any]]:
        closing_records = self.store.list("rh_closing_records", "opportunity_id=?", (opportunity_id,))
        closed_at = closing_records[-1]["created_at"] if closing_records else None
        messages = self.store.list("rh_conversation_messages", "opportunity_id=? AND direction=?", (opportunity_id, "INBOUND"))
        return [
            m for m in messages
            if m["intent"] in _SCOPE_SIGNAL_INTENTS and (closed_at is None or m["created_at"] > closed_at)
        ]

    def comms_signals(self, opportunity_id: str) -> List[Dict[str, Any]]:
        """Same purpose as `scope_signals`, additive rather than a
        replacement: reads the newer Unified Communications Center
        (falguna.comms) tables instead of the older
        rh_conversation_messages ones, so a scope/requirement signal that
        came in through the real Communications Center (Milestone 1) is
        just as visible to Account Management as one that came in through
        Revenue Hunter's own conversation inbox. `scope_signals` above is
        left untouched, so any existing caller or test keeps working."""
        closing_records = self.store.list("rh_closing_records", "opportunity_id=?", (opportunity_id,))
        closed_at = closing_records[-1]["created_at"] if closing_records else None
        conversations = self.store.list("comm_conversations", "linked_opportunity_id=?", (opportunity_id,))
        signals: List[Dict[str, Any]] = []
        for conv in conversations:
            messages = self.store.list("comm_messages", "conversation_id=? AND direction=?", (conv["id"], "INBOUND"))
            for message in messages:
                intent = classify_intent(message["body"])
                if intent in _SCOPE_SIGNAL_INTENTS and (closed_at is None or message["created_at"] > closed_at):
                    signals.append({**message, "conversation_id": conv["id"], "intent": intent})
        return signals

    def portfolio_overview(self) -> List[Dict[str, Any]]:
        """One truthful status row per Active Job -- what the Sales
        Manager (Pass E) and TTT HQ UI build their delivery view on."""
        return [self.status_for_active_job(job["id"]) for job in self.store.list("rh_active_jobs")]

    # -- TTT Communications V2, Milestone 7: Account Management expansion --

    @staticmethod
    def _unanswered_conversations(conversations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """A conversation whose last customer-visible message is still
        INBOUND -- the same "have we actually replied" signal
        comms_workforce._already_responded uses, duplicated here in one
        small static method rather than imported, since comms_workforce
        already imports AccountManagerService and importing back would be
        circular."""
        out = []
        for conv in conversations:
            if conv["status"] not in ("new", "open", "escalated", "pending_customer"):
                continue
            visible = [m for m in (conv.get("messages") or []) if not m.get("is_internal_note")]
            if visible and visible[-1]["direction"] == "INBOUND":
                out.append(conv)
        return out

    def account_summary(
        self, organization_id: str, actor: str = "system", requested_by_organization_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """One factual, evidence-linked account summary. Reuses
        `CustomerContextService` (Milestone 4) for the scoped org/contacts/
        conversations/opportunities/proposals/followups/active_jobs bundle
        -- the exact same isolation guarantees apply here, since this
        calls the same scoped method rather than re-querying -- then
        layers the account-management-specific signals Milestone 7 asks
        for on top: unanswered customer messages, follow-ups still
        outstanding, per-active-job delivery risk (real evidence from
        `status_for_active_job`, never a guessed percentage), recent
        escalations, billing issues, and a conservative, evidence-only
        renewal/expansion flag. Nothing here infers sentiment or predicts
        an outcome that isn't already a real row on file; every entry in
        `next_actions` carries the evidence it was derived from."""
        needs_aryan = self._needs_aryan or NeedsAryanQueue(self.store, self.audit)
        comms = CommsStore(self.store, self.audit, needs_aryan)
        ctx = CustomerContextService(self.store, comms).internal_context(
            organization_id, actor=actor, requested_by_organization_id=requested_by_organization_id,
        )

        unanswered = self._unanswered_conversations(ctx["conversations"])
        followups_outstanding = [f for f in ctx["followups"] if f["status"] == "DRAFT"]
        delivery = [self.status_for_active_job(j["id"]) for j in ctx["active_jobs"]]
        delivery_risks = [d for d in delivery if d["blocked"]]
        billing_issues = [c for c in ctx["open_support_issues"] if c["department"] == "billing"]

        opportunity_ids = {o["id"] for o in ctx["opportunities"]}
        proposal_ids = {p["id"] for p in ctx["proposals"]}
        conversation_ids = {c["id"] for c in ctx["conversations"]}
        recent_escalations = [
            item for item in self.store.list("needs_aryan_items")
            if (item["ref_type"] == "comm_conversation" and item["ref_id"] in conversation_ids)
            or (item["ref_type"] == "rh_proposal" and item["ref_id"] in proposal_ids)
            or (item["ref_type"] == "rh_opportunity" and item["ref_id"] in opportunity_ids)
        ]
        recent_escalations.sort(key=lambda i: i["created_at"], reverse=True)

        renewal_candidates = [
            {"active_job_id": j["id"], "opportunity_id": j["opportunity_id"],
             "lifecycle_state": next((o["lifecycle_state"] for o in ctx["opportunities"] if o["id"] == j["opportunity_id"]), None)}
            for j in ctx["active_jobs"]
            if next((o["lifecycle_state"] for o in ctx["opportunities"] if o["id"] == j["opportunity_id"]), None) in _RENEWAL_CANDIDATE_STATES
        ]
        # Expansion is simply "this organization has more than one real
        # opportunity on file" -- a factual repeat-engagement count, never
        # a prediction of future spend.
        expansion_opportunities = sorted(ctx["opportunities"], key=lambda o: o["created_at"])[1:]

        next_actions: List[Dict[str, Any]] = []
        for conv in unanswered:
            next_actions.append({
                "action": f"Reply to {conv.get('subject') or conv['id']}",
                "evidence": [f"conversation {conv['id']} last message is INBOUND, status={conv['status']}"],
            })
        for f in followups_outstanding:
            next_actions.append({
                "action": f"Send follow-up ({f['kind']}) for opportunity {f['opportunity_id']}",
                "evidence": [f"rh_followup {f['id']} status=DRAFT"],
            })
        for d in delivery_risks:
            next_actions.append({
                "action": f"Resolve delivery blocker on {d.get('title') or d['active_job_id']}",
                "evidence": [f"active_job {d['active_job_id']}: {d['blocker_reason']}"] + d["evidence"],
            })
        for r in renewal_candidates:
            next_actions.append({
                "action": f"Consider renewal outreach for opportunity {r['opportunity_id']}",
                "evidence": [f"active_job {r['active_job_id']} opportunity lifecycle_state={r['lifecycle_state']}"],
            })

        return {
            "organization": ctx["organization"], "contacts": ctx["contacts"],
            "active_projects": ctx["active_jobs"], "unanswered_conversations": unanswered,
            "followups_outstanding": followups_outstanding, "delivery_risks": delivery_risks,
            "support_history": ctx["support_history"], "open_support_issues": ctx["open_support_issues"],
            "billing_issues": billing_issues, "billing_status": ctx["billing_status"],
            "proposal_contract_state": {
                "proposals": ctx["proposals"], "approved_agreements": ctx["approved_agreements"],
            },
            "recent_escalations": recent_escalations, "renewal_candidates": renewal_candidates,
            "expansion_opportunities": expansion_opportunities, "next_actions": next_actions,
        }
