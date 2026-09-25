"""Tests for the Email/Admin foundation (falguna/email_admin.py).

There is no real send adapter in v1 by design -- these tests confirm a
draft always escalates for review, `mark_sent` refuses until that review
is approved, and nothing here ever fabricates a SENT status.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from falguna.audit import AuditLog
from falguna.email_admin import (
    EmailError, EmailStore, IMAPEmailProvider, NullEmailProvider,
    ProviderAPIEmailProvider, SMTPEmailProvider, SMTPIMAPEmailProvider,
)
from falguna.store import StateStore
from falguna.ttt_hq import NeedsAryanQueue

_SMTP_ENV = {"SMTP_HOST": "smtp.example-test.invalid", "SMTP_PORT": "587",
             "SMTP_USERNAME": "bot@example-test.invalid", "SMTP_PASSWORD": "fake-not-real"}
_IMAP_ENV = {"IMAP_HOST": "imap.example-test.invalid", "IMAP_PORT": "993",
             "IMAP_USERNAME": "bot@example-test.invalid", "IMAP_PASSWORD": "fake-not-real"}
_ALL_PROVIDER_ENV_KEYS = [
    "SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_USE_TLS", "SMTP_DEFAULT_FROM_ADDRESS",
    "IMAP_HOST", "IMAP_PORT", "IMAP_USERNAME", "IMAP_PASSWORD", "IMAP_USE_SSL", "IMAP_MAILBOX_FOLDER",
]


class _CleanProviderEnv(unittest.TestCase):
    """Ensures no real SMTP/IMAP env leaks in from the developer's own
    shell and that anything this test sets is removed afterward."""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in _ALL_PROVIDER_ENV_KEYS}
        for k in _ALL_PROVIDER_ENV_KEYS:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


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


class NullProviderInterfaceTests(unittest.TestCase):
    """The safe default stays safe under the upgraded interface too."""

    def test_never_configured_never_sends_never_polls(self):
        p = NullEmailProvider()
        self.assertFalse(p.is_configured())
        self.assertFalse(any(p.capabilities().values()))
        self.assertFalse(p.validate_configuration()["valid"])
        self.assertFalse(p.health_check()["healthy"])
        with self.assertRaises(EmailError):
            p.send("a@b.com", "s", "body")
        with self.assertRaises(EmailError):
            p.poll_inbound()

    def test_provider_status_reports_null_provider_by_name(self):
        store = EmailStore.__new__(EmailStore)  # not used; just checking name plumbing below
        p = NullEmailProvider()
        self.assertEqual(p.provider_name(), "NullEmailProvider")


class SMTPProviderInterfaceTests(_CleanProviderEnv):
    def test_unconfigured_without_env_vars(self):
        p = SMTPEmailProvider()
        self.assertFalse(p.is_configured())
        errors = p.validate_configuration()["errors"]
        self.assertIn("SMTP_HOST is not set", errors)
        self.assertIn("SMTP_PASSWORD is not set", errors)
        with self.assertRaises(EmailError):
            p.send("client@example.com", "Subject", "Body")

    def test_configured_once_all_required_env_vars_are_set(self):
        os.environ.update(_SMTP_ENV)
        p = SMTPEmailProvider()
        self.assertTrue(p.is_configured())
        self.assertEqual(p.validate_configuration(), {"valid": True, "errors": []})
        caps = p.capabilities()
        self.assertTrue(caps["send"] and caps["cc_bcc"] and caps["reply_to"] and caps["attachments"])
        self.assertFalse(caps["poll_inbound"])  # SMTP alone cannot receive

    def test_send_uses_smtplib_without_leaking_the_password_in_the_result(self):
        os.environ.update(_SMTP_ENV)
        p = SMTPEmailProvider()
        with patch("falguna.email_admin.smtplib.SMTP") as mock_smtp:
            instance = mock_smtp.return_value.__enter__.return_value
            result = p.send(
                "client@example.com", "Hello", "Test body",
                cc=["watcher@example.com"], reply_to="sales@twentytwotechnologies.com",
            )
        self.assertEqual(result["status"], "SENT")
        self.assertIsNone(result["failure_reason"])
        self.assertTrue(instance.login.called)
        self.assertTrue(instance.send_message.called)
        self.assertNotIn(_SMTP_ENV["SMTP_PASSWORD"], str(result))

    def test_send_failure_raises_emailerror_without_leaking_the_password(self):
        os.environ.update(_SMTP_ENV)
        p = SMTPEmailProvider()
        with patch("falguna.email_admin.smtplib.SMTP", side_effect=RuntimeError("boom")):
            with self.assertRaises(EmailError) as ctx:
                p.send("client@example.com", "Hello", "Test body")
        self.assertNotIn(_SMTP_ENV["SMTP_PASSWORD"], str(ctx.exception))

    def test_smtp_provider_is_send_only_and_refuses_to_poll(self):
        os.environ.update(_SMTP_ENV)
        with self.assertRaises(EmailError):
            SMTPEmailProvider().poll_inbound()


class IMAPProviderInterfaceTests(_CleanProviderEnv):
    def test_unconfigured_without_env_vars(self):
        p = IMAPEmailProvider()
        self.assertFalse(p.is_configured())
        with self.assertRaises(EmailError):
            p.poll_inbound()

    def test_configured_once_all_required_env_vars_are_set(self):
        os.environ.update(_IMAP_ENV)
        p = IMAPEmailProvider()
        self.assertTrue(p.is_configured())
        caps = p.capabilities()
        self.assertTrue(caps["poll_inbound"])
        self.assertFalse(caps["send"])  # IMAP alone cannot send

    def test_imap_provider_is_poll_only_and_refuses_to_send(self):
        os.environ.update(_IMAP_ENV)
        with self.assertRaises(EmailError):
            IMAPEmailProvider().send("a@b.com", "s", "body")

    def test_poll_failure_raises_emailerror_without_leaking_the_password(self):
        os.environ.update(_IMAP_ENV)
        p = IMAPEmailProvider()
        with patch("falguna.email_admin.imaplib.IMAP4_SSL", side_effect=RuntimeError("boom")):
            with self.assertRaises(EmailError) as ctx:
                p.poll_inbound()
        self.assertNotIn(_IMAP_ENV["IMAP_PASSWORD"], str(ctx.exception))


class SMTPIMAPComposedProviderTests(_CleanProviderEnv):
    def test_not_configured_unless_both_halves_are_configured(self):
        p = SMTPIMAPEmailProvider()
        self.assertFalse(p.is_configured())
        os.environ.update(_SMTP_ENV)
        self.assertFalse(SMTPIMAPEmailProvider().is_configured())  # IMAP half still missing
        os.environ.update(_IMAP_ENV)
        self.assertTrue(SMTPIMAPEmailProvider().is_configured())

    def test_capabilities_cover_both_send_and_poll(self):
        caps = SMTPIMAPEmailProvider().capabilities()
        self.assertTrue(caps["send"] and caps["poll_inbound"])


class ProviderAPIEmailProviderTests(unittest.TestCase):
    """The unselected-provider template stays honestly non-functional."""

    def test_is_never_configured_and_every_action_raises(self):
        p = ProviderAPIEmailProvider()
        self.assertFalse(p.is_configured())
        self.assertFalse(any(p.capabilities().values()))
        with self.assertRaises(EmailError):
            p.send("a@b.com", "s", "body")
        with self.assertRaises(EmailError):
            p.poll_inbound()


class ProviderStatusTests(EmailStoreTestBase, _CleanProviderEnv):
    """EmailStore.provider_status() is what TTT HQ's Communications view
    reads -- must never claim readiness the provider doesn't have."""

    def setUp(self):
        _CleanProviderEnv.setUp(self)
        EmailStoreTestBase.setUp(self)

    def tearDown(self):
        EmailStoreTestBase.tearDown(self)
        _CleanProviderEnv.tearDown(self)

    def test_default_provider_status_is_null_and_unconfigured(self):
        status = self.emails.provider_status()
        self.assertEqual(status["provider"], "NullEmailProvider")
        self.assertFalse(status["configured"])
        self.assertFalse(any(status["capabilities"].values()))
        self.assertFalse(status["validation"]["valid"])
        self.assertIsNone(status["health"])  # never probed when not configured

    def test_configured_smtp_provider_status_includes_health(self):
        os.environ.update(_SMTP_ENV)
        emails = EmailStore(self.store, self.audit, needs_aryan=self.needs_aryan, provider=SMTPEmailProvider())
        with patch("falguna.email_admin.smtplib.SMTP") as mock_smtp:
            mock_smtp.return_value.__enter__.return_value.login.return_value = None
            status = emails.provider_status()
        self.assertEqual(status["provider"], "SMTPEmailProvider")
        self.assertTrue(status["configured"])
        self.assertTrue(status["validation"]["valid"])
        self.assertTrue(status["health"]["healthy"])
        self.assertNotIn(_SMTP_ENV["SMTP_PASSWORD"], str(status))


if __name__ == "__main__":
    unittest.main()
