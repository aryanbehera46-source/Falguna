"""Tests for falguna/outbound.py -- Outbound Lead + Outreach foundation
(Sections 4/5, Pass E). Confirms the hard safety property: nothing here
ever actually sends a message -- draft_message only ever produces a
PREPARED row, and mark_sent requires an explicit owner approval first."""

import tempfile
import unittest
from pathlib import Path

from falguna.audit import AuditLog
from falguna.outbound import OutboundLeadError, OutboundLeadStore, OutreachError, OutreachService
from falguna.store import StateStore
from falguna.ttt_hq import NeedsAryanQueue


class OutboundTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = StateStore(root / "state.db")
        self.store.migrate()
        self.audit = AuditLog(root / "audit.jsonl")
        self.needs_aryan = NeedsAryanQueue(self.store, self.audit)
        self.leads = OutboundLeadStore(self.store, self.audit)
        self.outreach = OutreachService(self.store, self.audit, needs_aryan=self.needs_aryan)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()


class CreateLeadTests(OutboundTestBase):
    def test_empty_company_name_raises(self):
        with self.assertRaises(OutboundLeadError):
            self.leads.create_lead("   ", "Aryan")

    def test_invalid_confidence_raises(self):
        with self.assertRaises(OutboundLeadError):
            self.leads.create_lead("Acme Corp", "Aryan", confidence="87%")

    def test_creates_a_new_lead(self):
        lead_id = self.leads.create_lead(
            "Acme Corp", "Aryan", website="acme.example", likely_need="Needs a booking system",
            confidence="Medium", relevance_notes="Found via their public job posting",
        )
        lead = self.leads.get(lead_id)
        self.assertEqual(lead["status"], "NEW")
        self.assertEqual(lead["confidence"], "Medium")

    def test_confidence_is_always_a_plain_label_never_a_percentage(self):
        lead_id = self.leads.create_lead("Acme Corp", "Aryan", confidence="High")
        lead = self.leads.get(lead_id)
        self.assertNotIn("%", lead["confidence"] or "")
        self.assertIn(lead["confidence"], {"Low", "Medium", "High"})


class UpdateStatusTests(OutboundTestBase):
    def test_unknown_status_raises(self):
        lead_id = self.leads.create_lead("Acme Corp", "Aryan")
        with self.assertRaises(OutboundLeadError):
            self.leads.update_status(lead_id, "NOT_A_REAL_STATUS", "Aryan")

    def test_unknown_lead_raises(self):
        with self.assertRaises(OutboundLeadError):
            self.leads.update_status("does-not-exist", "RESEARCHED", "Aryan")

    def test_updates_status(self):
        lead_id = self.leads.create_lead("Acme Corp", "Aryan")
        updated = self.leads.update_status(lead_id, "RESEARCHED", "Aryan")
        self.assertEqual(updated["status"], "RESEARCHED")


class DraftMessageTests(OutboundTestBase):
    def test_unknown_lead_raises(self):
        with self.assertRaises(OutreachError):
            self.outreach.draft_message("does-not-exist", "email", "Hi there")

    def test_empty_message_raises(self):
        lead_id = self.leads.create_lead("Acme Corp", "Aryan")
        with self.assertRaises(OutreachError):
            self.outreach.draft_message(lead_id, "email", "   ")

    def test_draft_is_always_prepared_never_sent(self):
        lead_id = self.leads.create_lead("Acme Corp", "Aryan")
        draft = self.outreach.draft_message(lead_id, "email", "Hi, we noticed you might need a booking system.")
        self.assertEqual(draft["status"], "PREPARED")

    def test_draft_creates_a_needs_aryan_outreach_approval_item(self):
        lead_id = self.leads.create_lead("Acme Corp", "Aryan")
        before = len(self.store.list("needs_aryan_items"))
        draft = self.outreach.draft_message(lead_id, "email", "Hi there")
        self.assertIsNotNone(draft["needs_aryan_id"])
        after = len(self.store.list("needs_aryan_items"))
        self.assertEqual(after, before + 1)
        item = self.store.get("needs_aryan_items", draft["needs_aryan_id"])
        self.assertEqual(item["kind"], "outreach_approval")

    def test_drafting_updates_lead_status(self):
        lead_id = self.leads.create_lead("Acme Corp", "Aryan")
        self.outreach.draft_message(lead_id, "email", "Hi there")
        lead = self.leads.get(lead_id)
        self.assertEqual(lead["status"], "OUTREACH_PREPARED")

    def test_draft_without_needs_aryan_wired_still_prepares(self):
        service = OutreachService(self.store, self.audit)  # no needs_aryan
        lead_id = self.leads.create_lead("Acme Corp", "Aryan")
        draft = service.draft_message(lead_id, "email", "Hi there")
        self.assertEqual(draft["status"], "PREPARED")
        self.assertIsNone(draft["needs_aryan_id"])


class MarkSentTests(OutboundTestBase):
    def test_unknown_draft_raises(self):
        with self.assertRaises(OutreachError):
            self.outreach.mark_sent("does-not-exist", "Aryan")

    def test_cannot_mark_sent_before_approval(self):
        lead_id = self.leads.create_lead("Acme Corp", "Aryan")
        draft = self.outreach.draft_message(lead_id, "email", "Hi there")
        with self.assertRaises(OutreachError):
            self.outreach.mark_sent(draft["id"], "Aryan")

    def test_can_mark_sent_after_approval(self):
        lead_id = self.leads.create_lead("Acme Corp", "Aryan")
        draft = self.outreach.draft_message(lead_id, "email", "Hi there")
        self.needs_aryan.decide(draft["needs_aryan_id"], "approve", "Aryan")
        result = self.outreach.mark_sent(draft["id"], "Aryan")
        self.assertEqual(result["status"], "SENT")
        lead = self.leads.get(lead_id)
        self.assertEqual(lead["status"], "CONTACTED")

    def test_cannot_mark_sent_twice(self):
        lead_id = self.leads.create_lead("Acme Corp", "Aryan")
        draft = self.outreach.draft_message(lead_id, "email", "Hi there")
        self.needs_aryan.decide(draft["needs_aryan_id"], "approve", "Aryan")
        self.outreach.mark_sent(draft["id"], "Aryan")
        with self.assertRaises(OutreachError):
            self.outreach.mark_sent(draft["id"], "Aryan")

    def test_can_mark_sent_when_no_needs_aryan_item_exists(self):
        service = OutreachService(self.store, self.audit)  # no needs_aryan wired
        lead_id = self.leads.create_lead("Acme Corp", "Aryan")
        draft = service.draft_message(lead_id, "email", "Hi there")
        result = service.mark_sent(draft["id"], "Aryan")
        self.assertEqual(result["status"], "SENT")


class ListForLeadTests(OutboundTestBase):
    def test_lists_drafts_for_a_lead(self):
        lead_id = self.leads.create_lead("Acme Corp", "Aryan")
        self.outreach.draft_message(lead_id, "email", "Draft 1")
        drafts = self.outreach.list_for_lead(lead_id)
        self.assertEqual(len(drafts), 1)


if __name__ == "__main__":
    unittest.main()
