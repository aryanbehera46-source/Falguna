import json

from .models import RunStatus
from .store import utcnow

RECOVERABLE = "RECOVERABLE"
NEEDS_APPROVAL = "NEEDS_APPROVAL"
TERMINAL = "TERMINAL"


class AutonomySupervisor:
    """Conservative, durable recovery decisions for one bounded mission."""

    RECOVERABLE_CATEGORIES = {"VERIFICATION_FAILURE", "REVIEW_FAILURE", "TRANSPORT_FAILURE", "MODEL_UNSUPPORTED", "PATCH_AMBIGUOUS", "PATCH_STALE", "PATCH_NOOP", "CONTEXT_TOO_LARGE", "PROFILE_STALE", "VERIFY_COMMAND_INVALID", "BROWSER_VERIFICATION_FAILURE"}
    APPROVAL_CATEGORIES = {"SCOPE_EXPANSION", "SCOPE_EXPANSION_REQUIRED", "PERMISSION_INCREASE", "DESTRUCTIVE_ACTION", "SECRET_OR_PROD_REQUIRED", "AMBIGUOUS_CONTINUATION", "SECURITY_CONTAINMENT", "BUDGET_STOP"}

    def __init__(self, store, audit):
        self.store, self.audit = store, audit

    def classify(self, error, status=RunStatus.FAILED.value):
        upper = str(error or "").upper()
        prefix = upper.split(":", 1)[0].strip()
        known = ("VERIFICATION_FAILURE", "BROWSER_VERIFICATION_FAILURE", "REVIEW_FAILURE", "MODEL_UNSUPPORTED", "TRANSPORT_FAILURE", "PATCH_AMBIGUOUS", "PATCH_STALE", "PATCH_NOOP", "CONTEXT_TOO_LARGE", "PROFILE_STALE", "VERIFY_COMMAND_INVALID", "SCOPE_EXPANSION_REQUIRED", "SCOPE_EXPANSION", "PERMISSION_INCREASE", "DESTRUCTIVE_ACTION", "SECRET_OR_PROD_REQUIRED", "AMBIGUOUS_CONTINUATION", "INTERNAL_ORCHESTRATION_ERROR")
        category = prefix if prefix in known else next((name for name in known if name in upper[:1000]), None)
        if not category and status == RunStatus.QUARANTINED.value:
            category = "SECURITY_CONTAINMENT"
        if not category and "BROWSER" in upper and ("FAIL" in upper or "ERROR" in upper):
            category = "BROWSER_VERIFICATION_FAILURE"
        if not category and ("DEFINITION OF DONE" in upper or "VERIFICATION" in upper):
            category = "VERIFICATION_FAILURE"
        category = category or "INTERNAL_ORCHESTRATION_ERROR"
        outcome = RECOVERABLE if category in self.RECOVERABLE_CATEGORIES else NEEDS_APPROVAL if category in self.APPROVAL_CATEGORIES else TERMINAL
        return outcome, category

    def record(self, run_id, error, *, retry_budget, attempts_used=0, decision_needed=None, phase=None):
        run = self.store.get("runs", run_id)
        outcome, category = self.classify(error, (run or {}).get("status", RunStatus.FAILED.value))
        checkpoint = self.store.latest_checkpoint(run_id)
        cancelled = bool(run and run["status"] == RunStatus.CANCELLED.value)
        remaining = max(0, int(retry_budget) - int(attempts_used))
        eligible = outcome == RECOVERABLE and checkpoint is not None and remaining > 0 and not cancelled
        if outcome == RECOVERABLE and remaining == 0:
            outcome = NEEDS_APPROVAL
            decision_needed = decision_needed or "Repair budget is exhausted; approve a new bounded attempt or stop."
        reason = "A valid checkpoint and bounded repair budget are available." if eligible else decision_needed or self._blocked_reason(outcome, checkpoint, cancelled, remaining)
        recovery_phase = "RETRYING_MODEL" if category in {"MODEL_UNSUPPORTED", "TRANSPORT_FAILURE"} else "REPAIRING_VERIFICATION" if category in {"VERIFICATION_FAILURE", "BROWSER_VERIFICATION_FAILURE"} else "ADDRESSING_REVIEW" if category == "REVIEW_FAILURE" else "RECOVERING"
        values = {"run_id": run_id, "outcome_class": outcome, "category": category, "phase": phase or ("NEEDS_ARYAN" if outcome == NEEDS_APPROVAL else ("TERMINAL" if outcome == TERMINAL else recovery_phase)), "retry_allowed": int(eligible), "resume_allowed": int(eligible), "eligibility_reason": reason, "attempts_used": int(attempts_used), "retry_budget": int(retry_budget), "diagnostics_json": json.dumps({"error": str(error)}, sort_keys=True), "decision_needed": decision_needed, "updated_at": utcnow()}
        existing = self.store.list("supervisor_states", "run_id=?", (run_id,))
        if existing:
            self.store.update("supervisor_states", existing[-1]["id"], **values)
        else:
            values["created_at"] = utcnow()
            self.store.create("supervisor_states", values)
        self.audit.append("SUPERVISOR_DECISION", {"run_id": run_id, "outcome_class": outcome, "category": category, "retry_allowed": eligible, "remaining": remaining})
        return values

    @staticmethod
    def _blocked_reason(outcome, checkpoint, cancelled, remaining):
        if cancelled: return "Cancelled missions remain cancelled and cannot be resumed."
        if checkpoint is None: return "No valid checkpoint exists for a safe resume."
        if remaining <= 0: return "The bounded repair budget is exhausted."
        if outcome == NEEDS_APPROVAL: return "Aryan must decide before scope, permission, or risk can change."
        return "This failure is terminal and cannot be retried safely."
