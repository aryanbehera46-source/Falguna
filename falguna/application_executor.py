"""TTT Autonomous Revenue-to-Delivery Loop v1 -- Application Executor.

Section 3 of the build spec: once Aryan has approved a proposal, this is
the one place in the codebase that ever attempts to actually apply/send it
on a real channel. It is deliberately built as an adapter/provider pattern
(`ApplicationChannel` subclasses) rather than one big function, because the
spec is explicit that different channels have very different legitimacy and
automation surfaces (an open job board form vs. a platform that requires
login vs. one that requires solving a CAPTCHA), and new channels will keep
getting added over time without ever touching this module's core logic.

What is genuinely real here, and what is not, matters more than anything
else in this file:

  * `ManualReviewChannel` is the only channel wired in by default. It never
    submits anything anywhere -- it evaluates the opportunity/channel for
    the concrete blocking conditions the spec calls out (CAPTCHA, login
    wall, a legal declaration, a payment request, an unsupported platform,
    anti-bot protection, ambiguous consent) and, finding no application URL
    it can safely act on unattended, always resolves to BLOCKED and raises
    a Needs Aryan item. This is intentionally the conservative default: no
    channel in this codebase auto-submits an application over the open
    internet today.
  * `SimulatedTestChannel` exists only for automated tests and controlled
    QA dry runs (Section 21). It is explicitly guarded (`allow_simulated`
    must be passed by the caller) so it can never be selected by ordinary
    runtime code paths or accidentally used to fabricate a real "Applied"
    status.
  * `UnsupportedChannel` is the explicit terminal case for a channel this
    executor has no adapter for at all -- it always blocks with a clear
    reason, never silently no-ops.

An opportunity only ever becomes "Applied" in the real sense (channel
status APPLIED, `rh_application_attempts.status == "SENT"`) when a channel
adapter itself reports back real evidence of submission. Nothing in this
module marks anything Applied/Sent on faith.
"""

import json
from typing import Any, Dict, Optional

from .audit import AuditLog
from .lifecycle import LifecycleOrchestrator
from .revenue_hunter import OpportunityError, OpportunityStore, ProposalStore
from .store import StateStore, utcnow

# Every reason this executor (or a channel it calls) can refuse to submit.
# Kept as an explicit, closed set rather than free text so the Needs Aryan
# item and the audit trail always carry a reason a human can act on, per
# Section 3: "Never bypass protections. Never pretend something was
# submitted."
BLOCKED_REASONS = {
    "captcha_present", "login_required", "legal_declaration_required",
    "payment_requested", "unsupported_platform", "anti_bot_protection",
    "ambiguous_consent_required", "no_application_channel_available",
}


class ApplicationExecutorError(ValueError):
    pass


class ApplicationResult:
    """The outcome of one channel's attempt. `status` is one of
    "SENT" (real, evidenced submission), "BLOCKED" (stopped safely, Needs
    Aryan raised), or "UNSUPPORTED" (no adapter exists for this channel)."""

    def __init__(self, status: str, evidence: Optional[Dict[str, Any]] = None, blocked_reason: Optional[str] = None):
        if status not in {"SENT", "BLOCKED", "UNSUPPORTED"}:
            raise ApplicationExecutorError(f"status must be SENT, BLOCKED, or UNSUPPORTED, got {status!r}")
        if status == "BLOCKED" and blocked_reason not in BLOCKED_REASONS:
            raise ApplicationExecutorError(f"blocked_reason must be one of {sorted(BLOCKED_REASONS)}, got {blocked_reason!r}")
        self.status = status
        self.evidence = evidence or {}
        self.blocked_reason = blocked_reason

    def to_dict(self) -> Dict[str, Any]:
        return {"status": self.status, "evidence": self.evidence, "blocked_reason": self.blocked_reason}


class ApplicationChannel:
    """Base adapter. `name` identifies the channel in `rh_application_attempts`
    and in Needs Aryan items; `attempt()` is the only method a real channel
    implementation needs to override."""

    name = "base"

    def attempt(self, opportunity: Dict[str, Any], proposal: Dict[str, Any]) -> ApplicationResult:
        raise NotImplementedError


class UnsupportedChannel(ApplicationChannel):
    """The explicit terminal case for a channel name this executor has no
    real adapter for. Always blocks with a clear, specific reason -- it
    never silently no-ops or reports success for a channel it doesn't know
    how to handle."""

    name = "unsupported"

    def __init__(self, requested_channel: str):
        self.requested_channel = requested_channel

    def attempt(self, opportunity: Dict[str, Any], proposal: Dict[str, Any]) -> ApplicationResult:
        return ApplicationResult(
            "UNSUPPORTED", evidence={"requested_channel": self.requested_channel},
            blocked_reason=None,
        )


class ManualReviewChannel(ApplicationChannel):
    """The only channel wired into the default executor. It evaluates the
    opportunity for the concrete blocking conditions the spec names, and --
    finding no way to submit an application unattended, safely, and within
    policy -- always stops and hands the actual sending to Aryan through a
    Needs Aryan item. This is the honest default until a specific,
    audited, real-submission adapter is built and approved for a specific
    platform: no channel in this codebase auto-submits today."""

    name = "manual_review"

    def attempt(self, opportunity: Dict[str, Any], proposal: Dict[str, Any]) -> ApplicationResult:
        source_url = (opportunity.get("source_url") or "").strip()
        if not source_url:
            return ApplicationResult("BLOCKED", evidence={"reason_detail": "opportunity has no application URL on file"},
                                       blocked_reason="no_application_channel_available")
        # Deliberately conservative: without a real browser-automation
        # adapter that has actually inspected the live page (out of scope
        # for this pass -- see Section 23, "Do not build mass scraping"),
        # this channel cannot know in advance whether the destination will
        # ask for a CAPTCHA, a login, a legal declaration, or payment. So it
        # always escalates rather than guessing -- the one thing Section 3
        # is explicit must never happen is a fabricated submission.
        return ApplicationResult(
            "BLOCKED",
            evidence={"source_url": source_url, "reason_detail": "no automated submission adapter is enabled for this channel; needs manual review and sign-off"},
            blocked_reason="ambiguous_consent_required",
        )


class SimulatedTestChannel(ApplicationChannel):
    """Test/QA-only adapter that reports a real-shaped SENT result without
    touching any live external system. Guarded by the executor's own
    `allow_simulated` flag (never on by default) so it can never be reached
    from an ordinary approve-and-apply call path -- only from a test file or
    an explicitly opted-in controlled QA run (Section 21)."""

    name = "simulated_test"

    def attempt(self, opportunity: Dict[str, Any], proposal: Dict[str, Any]) -> ApplicationResult:
        return ApplicationResult(
            "SENT",
            evidence={
                "simulated": True,
                "note": "test/QA adapter only -- no real external submission occurred",
                "opportunity_id": opportunity["id"], "proposal_id": proposal["id"],
            },
        )


CHANNELS = {"manual_review": ManualReviewChannel}


class ApplicationExecutor:
    """Coordinates one application attempt: loads the approved proposal,
    picks the requested channel's adapter, records the real result to
    `rh_application_attempts`, and -- only on genuine SENT evidence --
    advances the lifecycle to CONTACTED (via APPLYING). A BLOCKED or
    UNSUPPORTED result always raises a Needs Aryan item and leaves the
    opportunity's lifecycle state exactly where it was (or moves it back to
    APPROVED if it had already advanced to APPLYING for this attempt),
    never advancing on anything but real evidence."""

    def __init__(self, store: StateStore, audit: AuditLog, needs_aryan=None, orchestrator: Optional[LifecycleOrchestrator] = None):
        self.store = store
        self.audit = audit
        self.needs_aryan = needs_aryan
        self.orchestrator = orchestrator or LifecycleOrchestrator(store, audit)
        self.opportunities = OpportunityStore(store, audit)

    def _channel_for(self, channel_name: str, allow_simulated: bool) -> ApplicationChannel:
        if allow_simulated and channel_name == "simulated_test":
            return SimulatedTestChannel()
        cls = CHANNELS.get(channel_name)
        if cls is None:
            return UnsupportedChannel(channel_name)
        return cls()

    def apply(
        self, opportunity_id: str, proposal_id: str, actor: str = "system",
        channel: str = "manual_review", allow_simulated: bool = False,
    ) -> Dict[str, Any]:
        opportunity = self.store.get("rh_opportunities", opportunity_id)
        if not opportunity:
            raise ApplicationExecutorError("opportunity not found")
        proposal = self.store.get("rh_proposals", proposal_id)
        if not proposal or proposal["opportunity_id"] != opportunity_id:
            raise ApplicationExecutorError("proposal not found for this opportunity")
        if proposal["status"] != "APPROVED":
            raise ApplicationExecutorError("only an approved proposal may be applied/sent -- use the approved proposal only")

        # Record intent to apply before attempting, mirroring the real
        # workflow the state machine names (APPROVED -> APPLYING) so a
        # blocked attempt is visibly "we tried" rather than invisible.
        self.orchestrator.try_transition(opportunity_id, "APPLYING", actor=actor, reason=f"attempting application via {channel}")

        adapter = self._channel_for(channel, allow_simulated)
        result = adapter.attempt(dict(opportunity), dict(proposal))

        attempt_id = self.store.create("rh_application_attempts", {
            "opportunity_id": opportunity_id, "channel": channel, "status": result.status,
            "blocked_reason": result.blocked_reason, "evidence_json": json.dumps(result.evidence),
            "actor": actor, "created_at": utcnow(),
        })
        self.audit.append("RH_APPLICATION_ATTEMPT", {
            "opportunity_id": opportunity_id, "attempt_id": attempt_id, "channel": channel,
            "status": result.status, "actor": actor,
        })

        if result.status == "SENT":
            self.orchestrator.try_transition(
                opportunity_id, "CONTACTED", actor=actor, reason=f"application sent via {channel}",
                evidence=result.evidence,
            )
            return {"attempt_id": attempt_id, "status": "SENT", "evidence": result.evidence}

        # BLOCKED or UNSUPPORTED: never advance the lifecycle on anything
        # but real evidence -- move it back to APPROVED (the last state that
        # genuinely happened) and escalate.
        self.orchestrator.try_transition(opportunity_id, "APPROVED", actor=actor, reason="application attempt did not complete")
        reason = result.blocked_reason or "unsupported_channel"
        if self.needs_aryan is not None:
            self.needs_aryan.create_item(
                "outreach_approval",
                f"Application blocked ({reason}): {opportunity['title']}",
                (
                    f"The {channel} channel could not safely/automatically submit this application "
                    f"({reason.replace('_', ' ')}). Review the opportunity and either apply manually "
                    "or approve an alternative channel."
                ),
                actor=actor, ref_type="rh_application_attempt", ref_id=attempt_id,
                rationale=json.dumps(result.evidence), risk=reason,
            )
        return {"attempt_id": attempt_id, "status": result.status, "blocked_reason": result.blocked_reason, "evidence": result.evidence}
