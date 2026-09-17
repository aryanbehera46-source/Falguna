"""Tests for the Email/Admin foundation (falguna/email_admin.py).

There is no real send adapter in v1 by design -- these tests confirm a
draft always escalates for review, `mark_sent` refuses until that review
is approved, and nothing here ever fabricates a SENT status.
"""

import tempfile
import unittest
from pathlib import Path

from falguna.audit import AuditLog
from falguna.email_admin import EmailError, EmailStore
from falguna.store import StateStore
from falguna.ttt_hq import NeedsAryanQueue


class EmailStoreTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = StateStore(root / "state.db")
        self.store.migrate()
        self.audit = AuditLog(root / "audit.jsonl")
        self.needs_aryan = NeedsAryanQueue(self.store, self.audit)
        self.emails = EmailStore(self.store, self.audit, needs_aryan=self.needs_aryan)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()


class DraftTests(EmailStoreTestBase):
    def test_draft_requires_body(self):
        with self.assertRaises(EmailError):
            self.emails.draft("client@example.com", "Subject", "")

    def test_draft_is_prepared_and_escalates(self):
        draft = self.emails.draft("client@example.com", "Follow up", "Checking in on our proposal.", actor="system")
        self.assertEqual(draft["status"], "PREPARED")
        self.assertEqual(draft["direction"], "OUTBOUND")
        self.assertIsNotNone(draft["needs_aryan_id"])
        item = self.store.get("needs_aryan_items", draft["needs_aryan_id"])
        self.assertEqual(item["kind"], "workforce_action_approval")
        self.assertEqual(item["status"], "PENDING")

    def test_draft_without_needs_aryan_wired_has_no_id_but_still_prepared(self):
        emails = EmailStore(self.store, self.audit, needs_aryan=None)
        draft = emails.draft("client@example.com", "Subject", "Body text")
        self.assertEqual(draft["status"], "PREPARED")
        self.assertIsNone(draft["needs_aryan_id"])

    def test_draft_classifies_intent(self):
        draft = self.emails.draft("client@example.com", "Pricing", "What is the price for this service?")
        self.assertIsNotNone(draft["intent"])


class RecordReceivedTests(EmailStoreTestBase):
    def test_record_received_requires_body(self):
        with self.assertRaises(EmailError):
            self.emails.record_received("lead@example.com", "Subject", "")

    def test_record_received_is_inbound(self):
        email = self.emails.record_received("lead@example.com", "Question", "Do you offer X?")
        self.assertEqual(email["status"], "RECEIVED")
        self.assertEqual(email["direction"], "INBOUND")
        self.assertEqual(email["from_address"], "lead@example.com")


class MarkSentTests(EmailStoreTestBase):
    def test_mark_sent_refuses_without_approval(self):
        draft = self.emails.draft("client@example.com", "Subject", "Body")
        with self.assertRaises(EmailError):
            self.emails.mark_sent(draft["id"], actor="Aryan")

    def test_mark_sent_succeeds_after_approval(self):
        draft = self.emails.draft("client@example.com", "Subject", "Body")
        self.needs_aryan.decide(draft["needs_aryan_id"], "approve", actor="Aryan", note="looks good")
        sent = self.emails.mark_sent(draft["id"], actor="Aryan")
        self.assertEqual(sent["status"], "SENT")

    def test_mark_sent_refuses_on_inbound_email(self):
        email = self.emails.record_received("lead@example.com", "Q", "body")
        with self.assertRaises(EmailError):
            self.emails.mark_sent(email["id"], actor="Aryan")

    def test_mark_sent_refuses_on_already_sent(self):
        draft = self.emails.draft("client@example.com", "Subject", "Body")
        self.needs_aryan.decide(draft["needs_aryan_id"], "approve", actor="Aryan", note="ok")
        self.emails.mark_sent(draft["id"], actor="Aryan")
        with self.assertRaises(EmailError):
            self.emails.mark_sent(draft["id"], actor="Aryan")


class FollowUpTests(EmailStoreTestBase):
    def test_schedule_and_due_follow_ups(self):
        draft = self.emails.draft("client@example.com", "Subject", "Body")
        self.emails.schedule_follow_up(draft["id"], "2000-01-01", actor="Aryan")  # far in the past -> due
        due = self.emails.due_follow_ups(as_of="2099-01-01")
        self.assertTrue(any(e["id"] == draft["id"] for e in due))

    def test_future_follow_up_is_not_due(self):
        draft = self.emails.draft("client@example.com", "Subject", "Body")
        self.emails.schedule_follow_up(draft["id"], "2099-01-01", actor="Aryan")
        due = self.emails.due_follow_ups(as_of="2000-01-01")
        self.assertFalse(any(e["id"] == draft["id"] for e in due))

    def test_sent_email_is_never_due_again(self):
        draft = self.emails.draft("client@example.com", "Subject", "Body")
        self.emails.schedule_follow_up(draft["id"], "2000-01-01", actor="Aryan")
        self.needs_aryan.decide(draft["needs_aryan_id"], "approve", actor="Aryan", note="ok")
        self.emails.mark_sent(draft["id"], actor="Aryan")
        due = self.emails.due_follow_ups(as_of="2099-01-01")
        self.assertFalse(any(e["id"] == draft["id"] for e in due))


class SummarizeAndListTests(EmailStoreTestBase):
    def test_summarize_returns_first_sentence(self):
        draft = self.emails.draft("client@example.com", "Subject", "Checking in on our proposal. Let me know.")
        summary = self.emails.summarize(draft["id"])
        self.assertEqual(summary, "Checking in on our proposal.")

    def test_get_missing_returns_none_but_operations_raise(self):
        self.assertIsNone(self.emails.get("does-not-exist"))
        with self.assertRaises(EmailError):
            self.emails.summarize("does-not-exist")

    def test_list_filters_by_department_and_status(self):
        self.emails.draft("a@example.com", "S1", "body", department="sales")
        self.emails.draft("b@example.com", "S2", "body", department="media")
        rows = self.emails.list(department="sales")
        self.assertEqual(len(rows), 1)
        rows_by_status = self.emails.list(status="PREPARED")
        self.assertEqual(len(rows_by_status), 2)


if __name__ == "__main__":
    unittest.main()
