"""Tests for TTT Communications V2 Milestone 3 (falguna/email_ingestion.py):
real inbound email ingestion without a live mailbox connected. Real temp
SQLite DB, real AuditLog, real CommsStore/NeedsAryanQueue -- same convention
as tests/test_comms_workforce.py.
"""

import tempfile
import unittest
from pathlib import Path

from falguna.audit import AuditLog
from falguna.comms import CommsStore
from falguna.email_ingestion import EmailIngestionService, department_from_mailbox
from falguna.store import StateStore
from falguna.ttt_hq import NeedsAryanQueue

BASE_PAYLOAD = {
    "provider_message_id": "msg-001", "thread_id": "thread-abc",
    "from_address": "jordan@acmecorp.example", "to_address": "sales@twentytwotechnologies.com",
    "subject": "Project inquiry", "body": "We need help building a booking platform. What is your availability?",
    "received_at": "2026-09-26T10:00:00Z", "attachments": [],
}


class _IngestionCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = StateStore(root / "state.db")
        self.store.migrate()
        self.audit = AuditLog(root / "audit.jsonl")
        self.needs_aryan = NeedsAryanQueue(self.store, self.audit)
        self.comms = CommsStore(self.store, self.audit, self.needs_aryan)
        self.ingest = EmailIngestionService(self.store, self.audit, self.comms)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()


class DepartmentFromMailboxTests(unittest.TestCase):
    def test_known_mailbox_maps_to_its_department(self):
        self.assertEqual(department_from_mailbox("sales@twentytwotechnologies.com"), "sales")
        self.assertEqual(department_from_mailbox("SUPPORT@twentytwotechnologies.com"), "support")

    def test_unknown_or_missing_mailbox_defaults_to_general(self):
        self.assertEqual(department_from_mailbox("random@somewhere.example"), "general")
        self.assertEqual(department_from_mailbox(None), "general")


class BasicIngestionTests(_IngestionCase):
    def test_new_message_creates_contact_org_conversation_and_message(self):
        result = self.ingest.ingest(BASE_PAYLOAD)
        self.assertEqual(result["status"], "ingested")
        self.assertEqual(result["department"], "sales")
        self.assertTrue(result["created_new_conversation"])
        conv = self.comms.get_conversation(result["conversation_id"])
        self.assertEqual(conv["channel"], "EMAIL")
        self.assertEqual(conv["external_thread_id"], "thread-abc")
        self.assertEqual(conv["organization"]["domain"], "acmecorp.example")
        self.assertEqual(conv["primary_contact"]["email"], "jordan@acmecorp.example")
        inbound = [m for m in conv["messages"] if m["direction"] == "INBOUND"]
        self.assertEqual(len(inbound), 1)
        self.assertEqual(inbound[0]["provider_message_id"], "msg-001")

    def test_second_contact_from_the_same_domain_reuses_the_organization(self):
        self.ingest.ingest(BASE_PAYLOAD)
        second = dict(BASE_PAYLOAD, provider_message_id="msg-999", thread_id="thread-xyz",
                      from_address="alex@acmecorp.example")
        result = self.ingest.ingest(second)
        conv = self.comms.get_conversation(result["conversation_id"])
        first_conv = self.comms.get_conversation(
            self.store.list("comm_messages", "provider_message_id=?", ("msg-001",))[0]["conversation_id"]
        )
        self.assertEqual(conv["organization_id"], first_conv["organization_id"])

    def test_triggers_the_ai_workforce_and_returns_its_actions(self):
        result = self.ingest.ingest(BASE_PAYLOAD)
        self.assertIn("workforce_actions", result)
        self.assertTrue(any("acknowledgement" in a for a in result["workforce_actions"]))


class DeduplicationAndRetryTests(_IngestionCase):
    def test_retrying_the_same_provider_message_id_is_a_safe_no_op(self):
        first = self.ingest.ingest(BASE_PAYLOAD)
        second = self.ingest.ingest(BASE_PAYLOAD)
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(second["message_id"], first["message_id"])
        conv = self.comms.get_conversation(first["conversation_id"])
        inbound = [m for m in conv["messages"] if m["direction"] == "INBOUND" and m["kind"] == "message"]
        self.assertEqual(len(inbound), 1)

    def test_a_message_with_no_provider_id_is_never_deduplicated_against_another(self):
        no_id_a = dict(BASE_PAYLOAD, provider_message_id=None, thread_id=None)
        no_id_b = dict(BASE_PAYLOAD, provider_message_id=None, thread_id=None)
        r1 = self.ingest.ingest(no_id_a)
        r2 = self.ingest.ingest(no_id_b)
        self.assertEqual(r1["status"], "ingested")
        self.assertEqual(r2["status"], "ingested")


class ThreadMatchingTests(_IngestionCase):
    def test_reply_with_the_same_thread_id_lands_on_the_same_conversation(self):
        first = self.ingest.ingest(BASE_PAYLOAD)
        reply = dict(BASE_PAYLOAD, provider_message_id="msg-002", body="Any update on this?", subject="Re: Project inquiry")
        second = self.ingest.ingest(reply)
        self.assertEqual(second["conversation_id"], first["conversation_id"])
        self.assertFalse(second["created_new_conversation"])

    def test_out_of_order_delivery_still_lands_on_the_same_thread(self):
        # The "reply" arrives before the "original" -- still matches by
        # thread id once both exist, regardless of delivery order.
        reply = dict(BASE_PAYLOAD, provider_message_id="msg-002", body="Any update on this?")
        first = self.ingest.ingest(reply)
        original = dict(BASE_PAYLOAD, provider_message_id="msg-001")
        second = self.ingest.ingest(original)
        self.assertEqual(second["conversation_id"], first["conversation_id"])

    def test_no_thread_id_falls_back_to_the_contacts_active_conversation_in_the_same_department(self):
        no_thread = dict(BASE_PAYLOAD, provider_message_id="msg-a", thread_id=None)
        first = self.ingest.ingest(no_thread)
        second_payload = dict(BASE_PAYLOAD, provider_message_id="msg-b", thread_id=None, body="One more thing...")
        second = self.ingest.ingest(second_payload)
        self.assertEqual(second["conversation_id"], first["conversation_id"])

    def test_resolved_conversation_is_not_reused_a_new_one_opens_instead(self):
        first = self.ingest.ingest(dict(BASE_PAYLOAD, thread_id=None))
        self.comms.set_status(first["conversation_id"], "resolved", actor="Aryan")
        second = self.ingest.ingest(dict(BASE_PAYLOAD, provider_message_id="msg-later", thread_id=None))
        self.assertNotEqual(second["conversation_id"], first["conversation_id"])
        self.assertTrue(second["created_new_conversation"])


class ValidationAndSafetyTests(_IngestionCase):
    def test_missing_sender_is_rejected_not_crashed_on(self):
        result = self.ingest.ingest({"from_address": None, "body": "hi"})
        self.assertEqual(result["status"], "rejected")

    def test_malformed_sender_is_rejected(self):
        result = self.ingest.ingest({"from_address": "not-an-email-address", "body": "hi"})
        self.assertEqual(result["status"], "rejected")

    def test_empty_message_with_no_attachments_is_rejected(self):
        result = self.ingest.ingest({"from_address": "a@b.com", "body": "", "attachments": []})
        self.assertEqual(result["status"], "rejected")

    def test_message_with_only_attachments_and_no_body_is_accepted(self):
        result = self.ingest.ingest({
            "from_address": "a@b.com", "to_address": "support@twentytwotechnologies.com", "body": "",
            "attachments": [{"filename": "invoice.pdf", "content_type": "application/pdf"}],
        })
        self.assertEqual(result["status"], "ingested")
        conv = self.comms.get_conversation(result["conversation_id"])
        notes = [m for m in conv["messages"] if m["kind"] == "note"]
        self.assertTrue(any("invoice.pdf" in n["body"] for n in notes))

    def test_a_malformed_message_never_raises_an_exception(self):
        # A live poll loop must survive one garbage payload without crashing.
        try:
            result = self.ingest.ingest({"garbage": True})
        except Exception as exc:  # pragma: no cover -- this is exactly what must never happen
            self.fail(f"ingest() raised on malformed input instead of returning a status: {exc}")
        self.assertEqual(result["status"], "rejected")


class BounceSpamUrgencyTests(_IngestionCase):
    def test_bounce_notification_is_recorded_but_never_opens_a_conversation(self):
        before = len(self.store.list("comm_conversations"))
        result = self.ingest.ingest({
            "from_address": "mailer-daemon@somehost.com",
            "subject": "Delivery Status Notification (Failure)", "body": "The following message could not be delivered.",
        })
        self.assertEqual(result["status"], "bounce")
        self.assertEqual(len(self.store.list("comm_conversations")), before)

    def test_spam_like_message_is_still_visible_but_never_auto_dispatched(self):
        result = self.ingest.ingest({
            "provider_message_id": "msg-spam", "from_address": "promo@spammy.example",
            "to_address": "sales@twentytwotechnologies.com",
            "subject": "You have won a prize", "body": "Act now, click here now, free money guaranteed!",
        })
        self.assertEqual(result["status"], "ingested")
        self.assertTrue(result["spam_like"])
        self.assertNotIn("workforce_actions", result)
        conv = self.comms.get_conversation(result["conversation_id"])
        self.assertIn("spam_like", conv["tags"])

    def test_urgent_language_opens_a_high_priority_conversation(self):
        result = self.ingest.ingest({
            "provider_message_id": "msg-urgent", "from_address": "client@bigclient.example",
            "to_address": "support@twentytwotechnologies.com",
            "subject": "URGENT -- production down", "body": "This is an emergency, please respond immediately.",
        })
        conv = self.comms.get_conversation(result["conversation_id"])
        self.assertEqual(conv["priority"], "high")

    def test_urgent_follow_up_bumps_priority_on_an_existing_normal_conversation(self):
        first = self.ingest.ingest(dict(BASE_PAYLOAD, thread_id=None, subject="Question"))
        conv_before = self.comms.get_conversation(first["conversation_id"])
        self.assertEqual(conv_before["priority"], "normal")
        urgent_followup = dict(
            BASE_PAYLOAD, provider_message_id="msg-urgent-followup", thread_id=None,
            subject="URGENT", body="This is now an emergency, please respond immediately.",
        )
        self.ingest.ingest(urgent_followup)
        conv_after = self.comms.get_conversation(first["conversation_id"])
        self.assertEqual(conv_after["priority"], "high")


if __name__ == "__main__":
    unittest.main()
