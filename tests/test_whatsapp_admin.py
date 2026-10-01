"""Phase 5 Final Client Experience, Section 14 -- WhatsApp Business
architecture. Mirrors tests/test_comms.py's _FakeEmailProvider pattern for
WhatsApp: a real temp SQLite DB per test, an in-test-only WhatsAppProvider
double that never touches a real network, and explicit confirmation that
the default (NullWhatsAppProvider) structurally cannot send or poll.
"""

import tempfile
import unittest
from pathlib import Path

from falguna.audit import AuditLog
from falguna.comms import CommsError, CommsStore
from falguna.store import StateStore
from falguna.ttt_hq import NeedsAryanQueue
from falguna.whatsapp_admin import (
    NullWhatsAppProvider, WhatsAppBusinessCloudAPIProvider, WhatsAppError, WhatsAppProvider,
    resolve_configured_whatsapp_provider,
)


class _FakeWhatsAppProvider(WhatsAppProvider):
    def __init__(self, configured: bool = True, fail_times: int = 0, error_message: str = "simulated provider failure"):
        self.configured = configured
        self.fail_times = fail_times
        self.error_message = error_message
        self.calls = []

    def provider_name(self) -> str:
        return "FakeTestWhatsAppProvider"

    def capabilities(self):
        return {"send": True, "poll_inbound": False, "delivery_status": False, "media": False, "templates": False}

    def is_configured(self) -> bool:
        return self.configured

    def validate_configuration(self):
        return {"valid": self.configured, "errors": [] if self.configured else ["fake provider not configured"]}

    def health_check(self):
        return {"healthy": self.configured, "detail": "fake provider"}

    def send(self, to_phone, body, thread_id=None, in_reply_to_message_id=None, template_name=None, media=None):
        self.calls.append({"to": to_phone, "body": body, "in_reply_to": in_reply_to_message_id})
        if len(self.calls) <= self.fail_times:
            raise WhatsAppError(self.error_message)
        return {"status": "SENT", "provider_message_id": f"fake-wa-{len(self.calls)}", "thread_id": thread_id, "failure_reason": None}

    def poll_inbound(self, since=None, limit=50):
        return []


class NullWhatsAppProviderTests(unittest.TestCase):
    def test_null_provider_is_never_configured_and_always_refuses(self):
        provider = NullWhatsAppProvider()
        self.assertFalse(provider.is_configured())
        self.assertFalse(provider.capabilities()["send"])
        with self.assertRaises(WhatsAppError):
            provider.send("+10000000000", "hello")
        with self.assertRaises(WhatsAppError):
            provider.poll_inbound()

    def test_cloud_api_template_is_never_configured_in_this_assignment(self):
        provider = WhatsAppBusinessCloudAPIProvider()
        self.assertFalse(provider.is_configured())
        with self.assertRaises(WhatsAppError):
            provider.send("+10000000000", "hello")

    def test_resolver_returns_null_provider_when_nothing_is_configured(self):
        provider = resolve_configured_whatsapp_provider()
        self.assertIsInstance(provider, NullWhatsAppProvider)


class _WhatsAppCommsTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = StateStore(root / "state.db")
        self.store.migrate()
        self.audit = AuditLog(root / "audit.jsonl")
        self.needs_aryan = NeedsAryanQueue(self.store, self.audit)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def _conversation_with_outbound_draft(self, comms, phone="+15551234567"):
        contact_id = comms.find_or_create_contact(None, "WA Customer", phone=phone)
        conv = comms.open_conversation("WHATSAPP", "support", subject="WA enquiry", contact_id=contact_id)
        comms.store.update("comm_conversations", conv["id"], primary_contact_id=contact_id)
        msg = comms.add_message(conv["id"], "OUTBOUND", "Thanks for reaching out -- we'll follow up shortly.")
        return conv["id"], msg["id"]


class CommsWhatsAppChannelTests(_WhatsAppCommsTestCase):
    def test_whatsapp_is_a_valid_channel(self):
        comms = CommsStore(self.store, self.audit, self.needs_aryan)
        conv = comms.open_conversation("WHATSAPP", "support", subject="WA enquiry")
        self.assertEqual(conv["channel"], "WHATSAPP")

    def test_default_whatsapp_provider_refuses_to_send(self):
        comms = CommsStore(self.store, self.audit, self.needs_aryan)
        _, msg_id = self._conversation_with_outbound_draft(comms)
        with self.assertRaises(CommsError):
            comms.send_message_via_whatsapp_provider(msg_id, "Aryan")

    def test_configured_fake_provider_sends_and_records_evidence(self):
        provider = _FakeWhatsAppProvider()
        comms = CommsStore(self.store, self.audit, self.needs_aryan, whatsapp_provider=provider)
        _, msg_id = self._conversation_with_outbound_draft(comms)
        sent = comms.send_message_via_whatsapp_provider(msg_id, "Aryan")
        self.assertEqual(sent["status"], "SENT")
        self.assertEqual(sent["provider_name"], "FakeTestWhatsAppProvider")
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(provider.calls[0]["to"], "+15551234567")

    def test_send_fails_cleanly_after_exhausting_retries(self):
        provider = _FakeWhatsAppProvider(fail_times=5)
        comms = CommsStore(self.store, self.audit, self.needs_aryan, whatsapp_provider=provider)
        _, msg_id = self._conversation_with_outbound_draft(comms)
        result = comms.send_message_via_whatsapp_provider(msg_id, "Aryan", max_attempts=2)
        self.assertEqual(result["status"], "FAILED")
        self.assertIsNotNone(result["failure_reason"])

    def test_send_without_a_phone_number_on_file_is_refused(self):
        provider = _FakeWhatsAppProvider()
        comms = CommsStore(self.store, self.audit, self.needs_aryan, whatsapp_provider=provider)
        conv = comms.open_conversation("WHATSAPP", "support", subject="No phone")
        msg = comms.add_message(conv["id"], "OUTBOUND", "hello")
        with self.assertRaises(CommsError):
            comms.send_message_via_whatsapp_provider(msg["id"], "Aryan")
        self.assertEqual(provider.calls, [])


if __name__ == "__main__":
    unittest.main()
