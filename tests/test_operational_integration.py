"""Phase 1, Requirement 5: Complete Operational Integration.

Verifies the real, already-built end-to-end path holds together as one
coherent system, using real stores throughout (no mocked domain logic --
only a fake, in-test EmailProvider stands in for a real mailbox, exactly
as tests/test_comms.py's SendMessageViaProviderTests already does for
Requirement 3):

    Tally submission -> validated intake -> Communications record ->
    qualified opportunity -> appropriate AI role -> risk classification ->
    approval when required -> authorized response -> follow-up tracking.

And that Revenue Hunter, Communications, and TTT HQ's own overview() views
agree on record identity and status -- the same opportunity_id and
conversation_id, not three different local ideas of "the same lead."

This never claims a fabricated customer or real revenue -- every row
created here is clearly synthetic test data in a disposable temp SQLite
DB, and no real network/email send occurs (NullEmailProvider is the
default; the one send in this file goes through a fake, in-test provider,
never real SMTP/IMAP).
"""
import json
import tempfile
import unittest
from pathlib import Path

from falguna.audit import AuditLog
from falguna.comms import CommsStore
from falguna.comms_workforce import run_agent_for_conversation
from falguna.email_admin import EmailError, EmailProvider
from falguna.revenue_hunter import FollowupStore, OpportunityStore
from falguna.risk_engine import RiskClassificationStore
from falguna.store import StateStore
from falguna.tally_intake import TallyIntakeService
from falguna.ttt_hq import NeedsAryanQueue

PROJECT_FIELDS = [
    {"key": "q1", "label": "Name", "type": "INPUT_TEXT", "value": "Priya Sharma"},
    {"key": "q2", "label": "Email", "type": "INPUT_EMAIL", "value": "priya@bluewave-example.test"},
    {"key": "q3", "label": "Message", "type": "TEXTAREA", "value": "We need a new customer portal built for our logistics business."},
    {"key": "q4", "label": "Service Requested", "type": "INPUT_TEXT", "value": "Web development"},
    {"key": "q5", "label": "Budget", "type": "INPUT_TEXT", "value": "$15k-25k"},
    {"key": "q6", "label": "Timeline", "type": "INPUT_TEXT", "value": "Within 2 months"},
]


def _tally_payload(submission_id="proj-integration-1"):
    return {
        "eventId": f"evt-{submission_id}", "eventType": "FORM_RESPONSE", "createdAt": "2026-09-26T10:00:00Z",
        "data": {
            "responseId": f"resp-{submission_id}", "submissionId": submission_id,
            "respondentId": "r1", "formId": "vGkWW4", "formName": "Start a Project",
            "createdAt": "2026-09-26T10:00:00Z", "fields": PROJECT_FIELDS,
        },
    }


class _FakeEmailProvider(EmailProvider):
    """Same in-test-only double used in tests/test_comms.py -- never a
    real network/SMTP/IMAP call. Included here (rather than imported)
    because it is test infrastructure, not production code."""

    def __init__(self):
        self.calls = []

    def provider_name(self) -> str:
        return "IntegrationFakeProvider"

    def is_configured(self) -> bool:
        return True

    def validate_configuration(self):
        return {"valid": True, "errors": []}

    def health_check(self):
        return {"healthy": True, "detail": "fake"}

    def send(self, to_address, subject, body, from_address=None, cc=None, bcc=None, reply_to=None,
              thread_id=None, in_reply_to_message_id=None, attachments=None):
        self.calls.append({"to": to_address, "subject": subject, "body": body})
        return {"status": "SENT", "provider_message_id": f"fake-msg-{len(self.calls)}", "thread_id": thread_id, "failure_reason": None}

    def poll_inbound(self, since=None, limit=50):
        return []


class OperationalIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = StateStore(root / "state.db")
        self.store.migrate()
        self.audit = AuditLog(root / "audit.jsonl")
        self.needs_aryan = NeedsAryanQueue(self.store, self.audit)
        self.comms = CommsStore(self.store, self.audit, self.needs_aryan)
        self.intake = TallyIntakeService(self.store, self.audit, self.comms)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_full_tally_to_followup_pipeline_agrees_on_identity_throughout(self):
        # 1. Tally submission -> validated intake -> Communications record
        # + a real, linked Revenue Hunter opportunity (EnquiryStore's own,
        # already-tested behavior -- exercised here through the real
        # TallyIntakeService, not re-implemented).
        result = self.intake.ingest(_tally_payload(), actor="tally_intake")
        self.assertEqual(result["status"], "ingested")
        self.assertEqual(result["form_type"], "project")
        conversation_id = result["conversation_id"]
        opportunity_id = result["opportunity_id"]
        self.assertIsNotNone(conversation_id)
        self.assertIsNotNone(opportunity_id)

        # Record identity agreement, hop 1: Communications' own view of the
        # conversation must point at the exact same opportunity_id Revenue
        # Hunter has a real row for -- not a second, disconnected record.
        conversation = self.comms.get_conversation(conversation_id)
        self.assertEqual(conversation["linked_opportunity_id"], opportunity_id)
        self.assertEqual(conversation["department"], "sales")
        opportunity = self.store.get("rh_opportunities", opportunity_id)
        self.assertIsNotNone(opportunity)
        # TallyIntakeService.ingest() already dispatches
        # run_agent_for_conversation once as part of a successful ingest
        # (see falguna/tally_intake.py's own docstring / the Live Enquiry
        # Activation V1 report's Step 5) -- so by the time ingest() returns,
        # the real Sales Rep specialist has already qualified this
        # opportunity through the actual QualificationStore. This is a
        # genuine, already-built behavior this test discovers and verifies
        # rather than one it assumes.
        self.assertEqual(opportunity["stage"], "Qualified")
        self.assertIsNotNone(opportunity.get("lifecycle_state"))

        # A duplicate delivery of the exact same Tally submission (a real
        # webhook retry, or a re-run of the manual import script) must
        # never create a second conversation or a second opportunity --
        # this is the same idempotency tally_intake.py's own tests cover,
        # re-verified here at the integration-pipeline level.
        replay = self.intake.ingest(_tally_payload(), actor="tally_intake")
        self.assertEqual(replay["status"], "duplicate")
        self.assertEqual(len(self.store.list("rh_opportunities")), 1)
        self.assertEqual(len(self.store.list("comm_conversations")), 1)

        # 2. Qualified opportunity + 3. appropriate AI role, already
        # verified above via the real, automatic ingest-time dispatch.
        # 4. Risk classification: every real outbound draft the ingest-time
        # pass created must have been classified exactly once.
        qualifications = self.store.list("rh_qualifications", "opportunity_id=?", (opportunity_id,))
        self.assertEqual(len(qualifications), 1)

        conversation = self.comms.get_conversation(conversation_id)
        outbound_drafts = [m for m in conversation["messages"] if m["direction"] == "OUTBOUND" and not m["is_internal_note"]]
        self.assertGreaterEqual(len(outbound_drafts), 1, "the Receptionist must have drafted a real acknowledgement message")
        for message in outbound_drafts:
            events = RiskClassificationStore(self.store, self.audit, self.needs_aryan).list_for_subject("comm_message", message["id"])
            self.assertEqual(len(events), 1, f"message {message['id']} must be classified exactly once")

        # Re-running the dispatcher explicitly (exactly what re-running the
        # manual-import CLI, or a scheduled workforce pass, would do) must
        # be a safe no-op -- idempotent, never a duplicate qualification,
        # a duplicate draft, or a duplicate risk classification.
        pass_result = run_agent_for_conversation(self.store, self.audit, conversation_id, actor="ai_workforce", needs_aryan=self.needs_aryan)
        self.assertEqual(pass_result["actions"], [])
        self.assertEqual(len(self.store.list("rh_qualifications", "opportunity_id=?", (opportunity_id,))), 1)
        for message in outbound_drafts:
            events = RiskClassificationStore(self.store, self.audit, self.needs_aryan).list_for_subject("comm_message", message["id"])
            self.assertEqual(len(events), 1)

        # 5/6. Approval when required, 7. authorized response: force one
        # of the real drafts through a HIGH-risk commitment phrase (a
        # genuine, realistic case this pipeline must handle safely -- e.g.
        # an AI-drafted reply that goes further than a routine
        # acknowledgement) and prove it cannot be sent, manually or via a
        # real provider, until a real Needs Aryan approval is recorded.
        draft = outbound_drafts[0]
        self.store.update("comm_messages", draft["id"], body="We agree to those contract terms and will sign the contract today.")
        risk_store = RiskClassificationStore(self.store, self.audit, self.needs_aryan)
        # RiskClassificationStore.classify() has no built-in idempotency
        # guard of its own (that belongs to the workforce dispatcher's
        # _classify_unrisked_outbound_drafts, already proven above) -- a
        # human/agent explicitly re-classifying edited content is exactly
        # what this call represents, adding a second, real event alongside
        # the original LOW/MEDIUM one rather than replacing it.
        classification = risk_store.classify("comm_message", draft["id"], self.store.get("comm_messages", draft["id"])["body"], actor="ai_workforce", title="re-review")
        self.assertEqual(classification["risk"], "HIGH")
        needs_aryan_id = classification["needs_aryan_id"]
        self.assertIsNotNone(needs_aryan_id)

        fake_provider = _FakeEmailProvider()
        comms_send = CommsStore(self.store, self.audit, self.needs_aryan, provider=fake_provider)
        from falguna.comms import CommsError
        with self.assertRaises(CommsError):
            comms_send.send_message_via_provider(draft["id"], "Aryan")
        self.assertEqual(fake_provider.calls, [], "an unapproved HIGH-risk message must never reach the provider")

        # Approve, then confirm the exact same message can now be sent for
        # real (through the fake provider) -- authorized response.
        self.needs_aryan.decide(needs_aryan_id, "approve", "Aryan")
        sent = comms_send.send_message_via_provider(draft["id"], "Aryan")
        self.assertEqual(sent["status"], "SENT")
        self.assertEqual(len(fake_provider.calls), 1)
        self.assertEqual(fake_provider.calls[0]["to"], "priya@bluewave-example.test")

        # Sending the first real reply must stamp first_response_at on the
        # SAME conversation record every view reads from.
        conversation = self.comms.get_conversation(conversation_id)
        self.assertIsNotNone(conversation["first_response_at"])

        # 8. Follow-up tracking, using the real Revenue Hunter FollowupStore
        # (never a second, comms-only reminder system).
        followup_id = FollowupStore(self.store, self.audit).generate(opportunity_id, "response_followup")
        self.assertIsNotNone(followup_id)

        # Final cross-view agreement check: Communications' own overview()
        # (what TTT HQ's Communications page renders), Revenue Hunter's own
        # OpportunityStore, and the follow-up record must all agree this is
        # the SAME opportunity_id/conversation_id -- not three different
        # local ideas of the same lead.
        overview = self.comms.overview()
        self.assertTrue(any(f["id"] == followup_id and f["opportunity_id"] == opportunity_id for f in overview["follow_ups_due"]))
        self.assertFalse(any(m["id"] == draft["id"] for m in overview["failed_delivery"]), "a message that was actually SENT must never also appear as a failed delivery")
        opp_view = OpportunityStore(self.store, self.audit).get(opportunity_id)
        self.assertEqual(opp_view["id"], opportunity_id)
        self.assertEqual(opp_view["stage"], "Qualified")
        conv_view = self.comms.get_conversation(conversation_id)
        self.assertEqual(conv_view["linked_opportunity_id"], opp_view["id"])

    def test_a_rejected_tally_submission_is_never_silently_dropped(self):
        # Requirement 2/5 seam: a submission this system cannot confidently
        # route must be visible in the same Communications overview a real
        # operator watches -- never silently discarded.
        bad_payload = {
            "eventId": "evt-bad-1", "eventType": "FORM_RESPONSE", "createdAt": "2026-09-26T10:00:00Z",
            "data": {"responseId": "resp-bad-1", "submissionId": "bad-1", "formId": "unknown-form-id",
                     "formName": "Mystery form", "fields": [{"key": "q1", "label": "???", "value": "no idea"}]},
        }
        result = self.intake.ingest(bad_payload, actor="tally_intake")
        self.assertEqual(result["status"], "rejected")
        overview = self.comms.overview()
        self.assertTrue(any(e.get("tally_submission_id") == "bad-1" for e in overview["website_intake_errors"]))


if __name__ == "__main__":
    unittest.main()
