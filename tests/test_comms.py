"""Real, in-process tests for the Unified Communications Center
(falguna/comms.py, Milestone 1 of the TTT Communications + AI Customer
Service V1 sprint). No mocking of the store -- a real temp SQLite DB per
test, exactly like the rest of this codebase's non-live-server test files
(e.g. tests/test_revenue_hunter.py)."""
import json
import tempfile
import unittest
from pathlib import Path

from falguna.audit import AuditLog
from falguna.comms import CommsError, CommsStore
from falguna.email_admin import EmailError, EmailProvider
from falguna.store import StateStore
from falguna.ttt_hq import NeedsAryanQueue


class _FakeEmailProvider(EmailProvider):
    """An in-test-only EmailProvider double -- never touches a real
    network/SMTP/IMAP connection. `fail_times` lets a test simulate N
    transient provider failures before a send eventually succeeds (or,
    set >= max_attempts, a permanent failure that exhausts retries)."""

    def __init__(self, configured: bool = True, fail_times: int = 0, error_message: str = "simulated provider failure"):
        self.configured = configured
        self.fail_times = fail_times
        self.error_message = error_message
        self.calls = []

    def provider_name(self) -> str:
        return "FakeTestProvider"

    def capabilities(self):
        return {"send": True, "poll_inbound": False, "delivery_status": False, "attachments": False, "cc_bcc": False, "reply_to": False}

    def is_configured(self) -> bool:
        return self.configured

    def validate_configuration(self):
        return {"valid": self.configured, "errors": [] if self.configured else ["fake provider not configured"]}

    def health_check(self):
        return {"healthy": self.configured, "detail": "fake provider"}

    def send(self, to_address, subject, body, from_address=None, cc=None, bcc=None, reply_to=None,
              thread_id=None, in_reply_to_message_id=None, attachments=None):
        self.calls.append({"to": to_address, "subject": subject, "body": body, "in_reply_to": in_reply_to_message_id})
        if len(self.calls) <= self.fail_times:
            raise EmailError(self.error_message)
        return {"status": "SENT", "provider_message_id": f"fake-msg-{len(self.calls)}", "thread_id": thread_id, "failure_reason": None}

    def poll_inbound(self, since=None, limit=50):
        return []


class _CommsTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = StateStore(root / "state.db")
        self.store.migrate()
        self.audit = AuditLog(root / "audit.jsonl")
        self.needs_aryan = NeedsAryanQueue(self.store, self.audit)
        self.comms = CommsStore(self.store, self.audit, self.needs_aryan)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()


class OrganizationAndContactTests(_CommsTestCase):
    def test_organization_dedups_on_domain(self):
        a = self.comms.find_or_create_organization("Acme Co", "acme.com")
        b = self.comms.find_or_create_organization("Acme Incorporated", "acme.com")
        self.assertEqual(a, b)

    def test_contact_dedups_on_email_and_backfills_organization(self):
        c1 = self.comms.find_or_create_contact("jane@acme.com", "Jane")
        org = self.comms.find_or_create_organization("Acme Co", "acme.com")
        c2 = self.comms.find_or_create_contact("jane@acme.com", "Jane Doe", org)
        self.assertEqual(c1, c2)
        self.assertEqual(self.store.get("comm_contacts", c1)["organization_id"], org)


class ContactCommunicationPreferenceTests(_CommsTestCase):
    """Phase 5 Final Client Experience, Section 8 -- Customer Communication
    Profile. Every field independently settable, None leaves the field
    untouched, invalid values rejected against the closed enums, and
    nothing here infers anything the test didn't explicitly state."""

    def test_preferences_start_unset(self):
        c = self.comms.find_or_create_contact("new@acme.com", "New Contact")
        contact = self.comms.get_contact(c)
        for field in ("preferred_language", "preferred_channel", "tone", "detail_level",
                      "technical_level", "update_cadence", "timezone", "call_preference",
                      "communication_restrictions"):
            self.assertIsNone(contact[field])

    def test_set_contact_preferences_persists_only_given_fields(self):
        c = self.comms.find_or_create_contact("pref@acme.com", "Pref Contact")
        updated = self.comms.set_contact_preferences(
            c, "Aryan", preferred_language="es", tone="CONVERSATIONAL", detail_level="CONCISE",
        )
        self.assertEqual(updated["preferred_language"], "es")
        self.assertEqual(updated["tone"], "CONVERSATIONAL")
        self.assertEqual(updated["detail_level"], "CONCISE")
        self.assertIsNone(updated["technical_level"])
        self.assertEqual(updated["preferences_set_by"], "Aryan")
        self.assertIsNotNone(updated["preferences_updated_at"])

    def test_set_contact_preferences_is_additive_across_calls(self):
        c = self.comms.find_or_create_contact("pref2@acme.com", "Pref Two")
        self.comms.set_contact_preferences(c, "Aryan", preferred_language="fr")
        second = self.comms.set_contact_preferences(c, "Aryan", technical_level="NON_TECHNICAL")
        self.assertEqual(second["preferred_language"], "fr")
        self.assertEqual(second["technical_level"], "NON_TECHNICAL")

    def test_set_contact_preferences_rejects_invalid_values(self):
        c = self.comms.find_or_create_contact("pref3@acme.com", "Pref Three")
        with self.assertRaises(CommsError):
            self.comms.set_contact_preferences(c, "Aryan", tone="SARCASTIC")
        with self.assertRaises(CommsError):
            self.comms.set_contact_preferences(c, "Aryan", detail_level="VERBOSE")
        with self.assertRaises(CommsError):
            self.comms.set_contact_preferences(c, "Aryan", technical_level="EXPERT")
        with self.assertRaises(CommsError):
            self.comms.set_contact_preferences(c, "Aryan", update_cadence="HOURLY")
        with self.assertRaises(CommsError):
            self.comms.set_contact_preferences(c, "Aryan", call_preference="ALWAYS_CALL")
        with self.assertRaises(CommsError):
            self.comms.set_contact_preferences(c, "Aryan", preferred_channel="CARRIER_PIGEON")

    def test_set_contact_preferences_rejects_unknown_contact(self):
        with self.assertRaises(CommsError):
            self.comms.set_contact_preferences("not-a-real-id", "Aryan", tone="FORMAL")

    def test_set_contact_preferences_with_no_fields_is_a_no_op(self):
        c = self.comms.find_or_create_contact("pref4@acme.com", "Pref Four")
        before = self.comms.get_contact(c)
        after = self.comms.set_contact_preferences(c, "Aryan")
        self.assertEqual(before, after)
        self.assertIsNone(after["preferences_set_by"])


class ConversationLifecycleTests(_CommsTestCase):
    def test_open_conversation_rejects_invalid_channel_department_priority(self):
        with self.assertRaises(CommsError):
            self.comms.open_conversation("CARRIER_PIGEON", "sales")
        with self.assertRaises(CommsError):
            self.comms.open_conversation("EMAIL", "not_a_real_department")
        with self.assertRaises(CommsError):
            self.comms.open_conversation("EMAIL", "sales", priority="asap")

    def test_open_conversation_sets_a_deterministic_sla_by_priority(self):
        urgent = self.comms.open_conversation("SUPPORT", "support", priority="urgent")
        low = self.comms.open_conversation("SUPPORT", "support", priority="low")
        self.assertLess(urgent["first_response_due_at"], low["first_response_due_at"])

    def test_new_conversation_becomes_open_on_first_message(self):
        conv = self.comms.open_conversation("WEBSITE", "general", priority="normal")
        self.assertEqual(conv["status"], "new")
        self.comms.add_message(conv["id"], "INBOUND", "Hello, is anyone there?")
        self.assertEqual(self.comms.get_conversation(conv["id"])["status"], "open")

    def test_add_message_validates_direction_and_kind_and_body(self):
        conv = self.comms.open_conversation("WEBSITE", "general")
        with self.assertRaises(CommsError):
            self.comms.add_message(conv["id"], "SIDEWAYS", "hi")
        with self.assertRaises(CommsError):
            self.comms.add_message(conv["id"], "INBOUND", "hi", kind="not_a_kind")
        with self.assertRaises(CommsError):
            self.comms.add_message(conv["id"], "INBOUND", "   ")

    def test_add_message_on_missing_conversation_raises(self):
        with self.assertRaises(CommsError):
            self.comms.add_message("does-not-exist", "INBOUND", "hi")

    def test_internal_note_is_flagged_and_never_the_customer_facing_first_response(self):
        conv = self.comms.open_conversation("SUPPORT", "support")
        note = self.comms.add_message(
            conv["id"], "OUTBOUND", "Checked the account -- looks fine internally.",
            kind="note", is_internal_note=True, sender_agent="AI Support Representative",
        )
        self.assertEqual(note["is_internal_note"], 1)
        self.assertIsNone(self.comms.get_conversation(conv["id"])["first_response_at"])

    def test_status_and_priority_transitions_are_recorded_in_history(self):
        conv = self.comms.open_conversation("EMAIL", "billing", priority="low")
        self.comms.set_status(conv["id"], "open", actor="Aryan", reason="picked up")
        self.comms.set_priority(conv["id"], "high", actor="Aryan", reason="customer escalated")
        updated = self.comms.get_conversation(conv["id"])
        self.assertEqual(updated["status"], "open")
        self.assertEqual(updated["priority"], "high")
        fields = {row["field"] for row in updated["history"]}
        self.assertEqual(fields, {"status", "priority"})

    def test_resolving_a_conversation_stamps_resolved_at_once(self):
        conv = self.comms.open_conversation("EMAIL", "support")
        resolved = self.comms.set_status(conv["id"], "resolved", actor="Aryan")
        self.assertIsNotNone(resolved["resolved_at"])
        first_stamp = resolved["resolved_at"]
        # Re-resolving (e.g. a duplicate action) must not overwrite the original timestamp.
        again = self.comms.set_status(conv["id"], "resolved", actor="Aryan")
        self.assertEqual(again["resolved_at"], first_stamp)

    def test_assign_records_an_agent_participant_and_history_entry(self):
        conv = self.comms.open_conversation("EMAIL", "sales")
        self.comms.assign(conv["id"], "AI Sales Representative", actor="system")
        updated = self.comms.get_conversation(conv["id"])
        self.assertEqual(updated["assigned_agent"], "AI Sales Representative")
        agent_participants = [p for p in updated["participants"] if p["participant_type"] == "agent"]
        self.assertEqual(len(agent_participants), 1)
        self.assertEqual(agent_participants[0]["agent_role"], "AI Sales Representative")


class EscalationTests(_CommsTestCase):
    def test_escalate_creates_a_real_needs_aryan_item_not_a_parallel_queue(self):
        conv = self.comms.open_conversation("EMAIL", "billing", priority="high")
        result = self.comms.escalate(
            conv["id"], "Refund above policy limit", "Customer wants a $2,000 refund; policy caps at $500.",
            actor="AI Billing Assistant", risk="customer may churn if delayed",
        )
        item = self.store.get("needs_aryan_items", result["needs_aryan_id"])
        self.assertIsNotNone(item)
        self.assertEqual(item["kind"], "communications_approval")
        self.assertEqual(item["ref_type"], "comm_conversation")
        self.assertEqual(item["ref_id"], conv["id"])
        self.assertEqual(result["conversation"]["status"], "pending_approval")
        pending = self.needs_aryan.list_pending()
        self.assertTrue(any(p["id"] == result["needs_aryan_id"] for p in pending))

    def test_escalate_on_missing_conversation_raises(self):
        with self.assertRaises(CommsError):
            self.comms.escalate("nope", "title", "what is needed")


class OverviewTests(_CommsTestCase):
    def test_overview_reflects_real_live_state_not_a_cached_count(self):
        self.assertEqual(self.comms.overview()["open_total"], 0)
        c1 = self.comms.open_conversation("WEBSITE", "sales", priority="urgent")
        c2 = self.comms.open_conversation("WEBSITE", "support", priority="low")
        ov = self.comms.overview()
        self.assertEqual(ov["open_total"], 2)
        self.assertEqual(ov["by_department"], {"sales": 1, "support": 1})
        self.assertEqual(len(ov["needs_attention"]), 1)  # only the urgent one
        self.comms.set_status(c1["id"], "resolved", actor="Aryan")
        self.assertEqual(self.comms.overview()["open_total"], 1)
        self.comms.set_status(c2["id"], "closed", actor="Aryan")
        self.assertEqual(self.comms.overview()["open_total"], 0)

    def test_overview_awaiting_approval_only_counts_comm_conversation_refs(self):
        conv = self.comms.open_conversation("EMAIL", "sales")
        self.comms.escalate(conv["id"], "t", "w", actor="system")
        # An unrelated needs_aryan item (a different ref_type) must not leak in.
        self.needs_aryan.create_item(
            "pricing_decision", "unrelated", "unrelated business decision",
            actor="system", ref_type="rh_opportunities", ref_id="some-opp",
        )
        ov = self.comms.overview()
        self.assertEqual(len(ov["awaiting_approval"]), 1)
        self.assertEqual(ov["awaiting_approval"][0]["ref_id"], conv["id"])


class Milestone8OverviewViewsTests(_CommsTestCase):
    """TTT Communications V2, Milestone 8: the five overview() buckets
    added for TTT HQ's Communications UX, on top of the four
    OverviewTests above already covers."""

    def test_support_issues_covers_support_and_billing_only(self):
        self.comms.open_conversation("EMAIL", "support", priority="normal")
        self.comms.open_conversation("EMAIL", "billing", priority="normal")
        self.comms.open_conversation("EMAIL", "sales", priority="normal")
        ov = self.comms.overview()
        self.assertEqual(len(ov["support_issues"]), 2)
        self.assertTrue(all(c["department"] in ("support", "billing") for c in ov["support_issues"]))

    def test_awaiting_client_reflects_pending_customer_status(self):
        conv = self.comms.open_conversation("EMAIL", "sales", priority="normal")
        self.assertEqual(self.comms.overview()["awaiting_client"], [])
        self.comms.set_status(conv["id"], "pending_customer", actor="Aryan")
        ov = self.comms.overview()
        self.assertEqual(len(ov["awaiting_client"]), 1)
        self.assertEqual(ov["awaiting_client"][0]["id"], conv["id"])

    def test_follow_ups_due_lists_draft_followups(self):
        from falguna.revenue_hunter import FollowupStore, OpportunityStore
        opportunities = OpportunityStore(self.store, self.audit)
        opp_id = opportunities.create({"title": "A project"}, actor="website")
        followup_id = FollowupStore(self.store, self.audit).generate(opp_id, "response_followup")
        ov = self.comms.overview()
        self.assertTrue(any(f["id"] == followup_id for f in ov["follow_ups_due"]))

    def test_failed_delivery_is_empty_until_a_message_is_actually_marked_failed(self):
        conv = self.comms.open_conversation("EMAIL", "sales", priority="normal")
        msg = self.comms.add_message(conv["id"], "OUTBOUND", "Draft reply", actor="ai_workforce")
        self.assertEqual(self.comms.overview()["failed_delivery"], [])
        self.comms.mark_message_failed(msg["id"], "system", reason="provider rejected the send")
        ov = self.comms.overview()
        self.assertEqual(len(ov["failed_delivery"]), 1)
        self.assertEqual(ov["failed_delivery"][0]["id"], msg["id"])
        self.assertEqual(self.store.get("comm_messages", msg["id"])["status"], "FAILED")

    def test_recently_resolved_lists_resolved_and_closed(self):
        c1 = self.comms.open_conversation("EMAIL", "sales", priority="normal")
        c2 = self.comms.open_conversation("EMAIL", "support", priority="normal")
        self.comms.set_status(c1["id"], "resolved", actor="Aryan")
        self.comms.set_status(c2["id"], "closed", actor="Aryan")
        ov = self.comms.overview()
        ids = {c["id"] for c in ov["recently_resolved"]}
        self.assertEqual(ids, {c1["id"], c2["id"]})

    def test_active_conversations_excludes_resolved_and_closed(self):
        c1 = self.comms.open_conversation("EMAIL", "sales", priority="normal")
        c2 = self.comms.open_conversation("EMAIL", "support", priority="normal")
        self.comms.set_status(c2["id"], "resolved", actor="Aryan")
        ids = {c["id"] for c in self.comms.overview()["active_conversations"]}
        self.assertEqual(ids, {c1["id"]})



class SendMessageViaProviderTests(_CommsTestCase):
    """Phase 1, Requirement 3: send_message_via_provider() -- the one
    real, in-system send path. Every test here uses _FakeEmailProvider,
    never a real network/SMTP/IMAP call."""

    def _comms_with(self, provider):
        return CommsStore(self.store, self.audit, self.needs_aryan, provider=provider)

    def _draft(self, comms=None, body="Here's the current status on your project."):
        comms = comms or self.comms
        conv = comms.open_conversation("EMAIL", "sales", priority="normal", contact_id=comms.find_or_create_contact("client@example-test.invalid", "Client"))
        return conv, comms.add_message(conv["id"], "OUTBOUND", body, actor="ai_workforce")

    def _audit_events(self, event_name):
        lines = self.audit.path.read_text().splitlines()
        return [json.loads(line) for line in lines if json.loads(line)["event"] == event_name]

    def test_refuses_when_no_real_provider_is_configured(self):
        # Default CommsStore() (no provider kwarg) uses NullEmailProvider,
        # matching every existing call site -- this must never silently
        # attempt a send.
        conv, msg = self._draft()
        with self.assertRaises(CommsError):
            self.comms.send_message_via_provider(msg["id"], "Aryan")
        self.assertEqual(self.store.get("comm_messages", msg["id"])["status"], "DRAFT")

    def test_refuses_inbound_message(self):
        comms = self._comms_with(_FakeEmailProvider())
        conv = comms.open_conversation("EMAIL", "sales", priority="normal")
        msg = comms.add_message(conv["id"], "INBOUND", "Customer message", actor="website")
        with self.assertRaises(CommsError):
            comms.send_message_via_provider(msg["id"], "Aryan")

    def test_refuses_internal_note(self):
        comms = self._comms_with(_FakeEmailProvider())
        conv = comms.open_conversation("EMAIL", "sales", priority="normal")
        note = comms.add_message(conv["id"], "OUTBOUND", "Internal note", kind="note", is_internal_note=True, actor="ai_workforce")
        with self.assertRaises(CommsError):
            comms.send_message_via_provider(note["id"], "Aryan")

    def test_refuses_a_message_that_is_not_draft(self):
        comms = self._comms_with(_FakeEmailProvider())
        conv, msg = self._draft(comms)
        comms.mark_message_sent(msg["id"], "Aryan")
        with self.assertRaises(CommsError):
            comms.send_message_via_provider(msg["id"], "Aryan")

    def test_duplicate_send_protection_after_provider_success(self):
        # Second attempt on an already-SENT message must raise cleanly,
        # never silently re-send or fabricate a second success.
        provider = _FakeEmailProvider()
        comms = self._comms_with(provider)
        conv, msg = self._draft(comms)
        comms.send_message_via_provider(msg["id"], "Aryan")
        with self.assertRaises(CommsError):
            comms.send_message_via_provider(msg["id"], "Aryan")
        self.assertEqual(len(provider.calls), 1)

    def test_refuses_without_a_contact_email_on_file(self):
        comms = self._comms_with(_FakeEmailProvider())
        conv = comms.open_conversation("EMAIL", "sales", priority="normal")  # no contact_id
        msg = comms.add_message(conv["id"], "OUTBOUND", "Draft reply", actor="ai_workforce")
        with self.assertRaises(CommsError):
            comms.send_message_via_provider(msg["id"], "Aryan")

    def test_high_risk_unapproved_blocks_send_even_with_a_real_provider(self):
        from falguna.risk_engine import RiskClassificationStore
        comms = self._comms_with(_FakeEmailProvider())
        conv, msg = self._draft(comms, body="We agree to those contract terms and will sign the contract today.")
        RiskClassificationStore(self.store, self.audit, self.needs_aryan).classify(
            "comm_message", msg["id"], msg["body"], actor="ai_workforce", title="review",
        )
        with self.assertRaises(CommsError):
            comms.send_message_via_provider(msg["id"], "Aryan")
        self.assertEqual(self.store.get("comm_messages", msg["id"])["status"], "DRAFT")

    def test_high_risk_approved_allows_send(self):
        from falguna.risk_engine import RiskClassificationStore
        provider = _FakeEmailProvider()
        comms = self._comms_with(provider)
        conv, msg = self._draft(comms, body="We agree to those contract terms and will sign the contract today.")
        result = RiskClassificationStore(self.store, self.audit, self.needs_aryan).classify(
            "comm_message", msg["id"], msg["body"], actor="ai_workforce", title="review",
        )
        self.assertEqual(result["risk"], "HIGH")
        self.needs_aryan.decide(result["needs_aryan_id"], "approve", "Aryan")
        sent = comms.send_message_via_provider(msg["id"], "Aryan")
        self.assertEqual(sent["status"], "SENT")

    def test_successful_send_records_provider_evidence_not_a_delivery_guarantee(self):
        provider = _FakeEmailProvider()
        comms = self._comms_with(provider)
        conv, msg = self._draft(comms)
        sent = comms.send_message_via_provider(msg["id"], "Aryan")
        self.assertEqual(sent["status"], "SENT")
        self.assertEqual(sent["send_method"], "provider")
        self.assertEqual(sent["provider_name"], "FakeTestProvider")
        self.assertEqual(sent["send_attempts"], 1)
        self.assertIsNotNone(sent["provider_message_id"])
        self.assertIsNone(sent["failure_reason"])
        events = self._audit_events("COMM_MESSAGE_SENT_VIA_PROVIDER")
        self.assertEqual(len(events), 1)
        self.assertIn("not a confirmation it reached the recipient's inbox", events[0]["data"]["note"])

    def test_succeeds_after_transient_failures_within_bounded_retries(self):
        provider = _FakeEmailProvider(fail_times=2)
        comms = self._comms_with(provider)
        conv, msg = self._draft(comms)
        sent = comms.send_message_via_provider(msg["id"], "Aryan", max_attempts=3)
        self.assertEqual(sent["status"], "SENT")
        self.assertEqual(sent["send_attempts"], 3)
        self.assertEqual(len(provider.calls), 3)

    def test_exhausts_bounded_retries_and_marks_failed_with_reason(self):
        provider = _FakeEmailProvider(fail_times=99, error_message="mailbox rejected the message")
        comms = self._comms_with(provider)
        conv, msg = self._draft(comms)
        failed = comms.send_message_via_provider(msg["id"], "Aryan", max_attempts=3)
        self.assertEqual(failed["status"], "FAILED")
        self.assertEqual(failed["send_method"], "provider")
        self.assertEqual(failed["send_attempts"], 3)
        self.assertEqual(failed["failure_reason"], "mailbox rejected the message")
        self.assertEqual(len(provider.calls), 3)
        events = self._audit_events("COMM_MESSAGE_SEND_FAILED")
        self.assertEqual(len(events), 1)
        # A message that failed via a real provider must still show up in
        # the existing Failed Delivery panel, same as a manual failure.
        ov = comms.overview()
        self.assertTrue(any(m["id"] == msg["id"] for m in ov["failed_delivery"]))

    def test_never_calls_the_provider_at_all_when_unconfigured(self):
        provider = _FakeEmailProvider(configured=False)
        comms = self._comms_with(provider)
        conv, msg = self._draft(comms)
        with self.assertRaises(CommsError):
            comms.send_message_via_provider(msg["id"], "Aryan")
        self.assertEqual(provider.calls, [])
        self.assertEqual(self.store.get("comm_messages", msg["id"])["status"], "DRAFT")


class MarkMessageFailedRecordsProviderFieldsTests(_CommsTestCase):
    def test_manual_mark_failed_records_send_method_and_reason_on_the_row(self):
        conv = self.comms.open_conversation("EMAIL", "sales", priority="normal")
        msg = self.comms.add_message(conv["id"], "OUTBOUND", "Draft", actor="ai_workforce")
        self.comms.mark_message_failed(msg["id"], "Aryan", reason="bounced")
        row = self.store.get("comm_messages", msg["id"])
        self.assertEqual(row["send_method"], "manual")
        self.assertEqual(row["failure_reason"], "bounced")

class MarkMessageFailedTests(_CommsTestCase):
    def test_requires_a_reason(self):
        conv = self.comms.open_conversation("EMAIL", "sales", priority="normal")
        msg = self.comms.add_message(conv["id"], "OUTBOUND", "Draft", actor="ai_workforce")
        with self.assertRaises(CommsError):
            self.comms.mark_message_failed(msg["id"], "system", reason="")

    def test_only_a_draft_outbound_message_can_be_marked_failed(self):
        conv = self.comms.open_conversation("EMAIL", "sales", priority="normal")
        msg = self.comms.add_message(conv["id"], "INBOUND", "Customer message", actor="website")
        with self.assertRaises(CommsError):
            self.comms.mark_message_failed(msg["id"], "system", reason="n/a")

    def test_a_failed_message_cannot_also_be_marked_sent(self):
        conv = self.comms.open_conversation("EMAIL", "sales", priority="normal")
        msg = self.comms.add_message(conv["id"], "OUTBOUND", "Draft", actor="ai_workforce")
        self.comms.mark_message_failed(msg["id"], "system", reason="provider timeout")
        with self.assertRaises(CommsError):
            self.comms.mark_message_sent(msg["id"], "Aryan")


class ConversationDrillDownTests(_CommsTestCase):
    def test_get_conversation_embeds_risk_events_for_its_own_messages(self):
        from falguna.risk_engine import RiskClassificationStore
        conv = self.comms.open_conversation("EMAIL", "sales", priority="normal")
        msg = self.comms.add_message(conv["id"], "OUTBOUND", "We agree to those contract terms.", actor="ai_workforce")
        RiskClassificationStore(self.store, self.audit, self.needs_aryan).classify(
            "comm_message", msg["id"], msg["body"], actor="ai_workforce", title="review",
        )
        full = self.comms.get_conversation(conv["id"])
        self.assertEqual(len(full["risk_events"]), 1)
        self.assertEqual(full["risk_events"][0]["subject_id"], msg["id"])

    def test_get_conversation_never_shows_another_conversations_risk_events(self):
        from falguna.risk_engine import RiskClassificationStore
        conv_a = self.comms.open_conversation("EMAIL", "sales", priority="normal")
        conv_b = self.comms.open_conversation("EMAIL", "sales", priority="normal")
        msg_b = self.comms.add_message(conv_b["id"], "OUTBOUND", "Confirmed price, ready to sign.", actor="ai_workforce")
        RiskClassificationStore(self.store, self.audit, self.needs_aryan).classify(
            "comm_message", msg_b["id"], msg_b["body"], actor="ai_workforce", title="review",
        )
        full_a = self.comms.get_conversation(conv_a["id"])
        self.assertEqual(full_a["risk_events"], [])


if __name__ == "__main__":
    unittest.main()
