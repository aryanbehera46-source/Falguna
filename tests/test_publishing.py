"""Tests for the Publishing layer (falguna/publishing.py).

No real platform adapter exists in v1 -- ManualPublishingChannel must
always BLOCK, a publication can only reach PUBLISHED with real evidence
(the channel's own, or a human's manual-fallback proof), and the
simulated channel must always label its evidence as simulated.
"""

import json
import tempfile
import unittest
from pathlib import Path

from falguna.audit import AuditLog
from falguna.media import BrandStore, ContentStore
from falguna.publishing import (
    ALLOWED_PUBLICATION_TRANSITIONS,
    ManualPublishingChannel,
    PublicationStore,
    PublishingError,
    PublishResult,
    SimulatedPublishingChannel,
)
from falguna.store import StateStore
from falguna.ttt_hq import NeedsAryanQueue


class PublishingTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = StateStore(root / "state.db")
        self.store.migrate()
        self.audit = AuditLog(root / "audit.jsonl")
        self.needs_aryan = NeedsAryanQueue(self.store, self.audit)
        self.publications = PublicationStore(self.store, self.audit, needs_aryan=self.needs_aryan)
        brands = BrandStore(self.store, self.audit)
        contents = ContentStore(self.store, self.audit)
        brand_id = brands.create("TTT", actor="Aryan")
        self.content_id = contents.create(brand_id, "Post", "image_post", actor="Aryan")
        self.content_row = contents.get(self.content_id)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _approved_publication(self):
        pub_id = self.publications.create(self.content_id, "instagram", actor="system")
        self.publications.mark_ready(pub_id, actor="system")
        pub = self.publications.submit_for_approval(pub_id, actor="system")
        self.needs_aryan.decide(pub["needs_aryan_id"], "approve", actor="Aryan", note="ok")
        self.publications.approve(pub_id, actor="Aryan")
        return pub_id


class PublishResultTests(unittest.TestCase):
    def test_invalid_status_raises(self):
        with self.assertRaises(PublishingError):
            PublishResult(status="PUBLISHED")  # only COMPLETED/BLOCKED are valid here


class ManualChannelTests(unittest.TestCase):
    def test_always_blocks(self):
        channel = ManualPublishingChannel()
        result = channel.attempt({"id": "c1"}, "instagram", {})
        self.assertEqual(result.status, "BLOCKED")
        self.assertEqual(result.blocked_reason, "no_publish_adapter_available")


class SimulatedChannelTests(unittest.TestCase):
    def test_completes_and_labels_itself_simulated(self):
        channel = SimulatedPublishingChannel()
        result = channel.attempt({"id": "c1"}, "instagram", {})
        self.assertEqual(result.status, "COMPLETED")
        self.assertTrue(result.evidence["simulated"])


class PublicationLifecycleTests(PublishingTestBase):
    def test_create_requires_platform(self):
        with self.assertRaises(PublishingError):
            self.publications.create(self.content_id, "")

    def test_create_starts_in_draft(self):
        pub_id = self.publications.create(self.content_id, "instagram", actor="Aryan")
        self.assertEqual(self.publications.get(pub_id)["status"], "DRAFT")

    def test_full_happy_path_to_approved(self):
        pub_id = self._approved_publication()
        self.assertEqual(self.publications.get(pub_id)["status"], "APPROVED")

    def test_approve_without_prior_decision_refuses(self):
        pub_id = self.publications.create(self.content_id, "instagram", actor="system")
        self.publications.mark_ready(pub_id, actor="system")
        self.publications.submit_for_approval(pub_id, actor="system")
        with self.assertRaises(PublishingError):
            self.publications.approve(pub_id, actor="Aryan")

    def test_illegal_transition_raises(self):
        pub_id = self.publications.create(self.content_id, "instagram", actor="system")
        with self.assertRaises(PublishingError):
            self.publications._transition(pub_id, "PUBLISHED", actor="system")

    def test_every_pre_publish_state_can_reach_failed(self):
        for state, edges in ALLOWED_PUBLICATION_TRANSITIONS.items():
            if state in ("PUBLISHED", "FAILED"):
                continue
            self.assertIn("FAILED", edges, f"{state} has no path to FAILED")


class PublishAttemptTests(PublishingTestBase):
    def test_publish_requires_approved_status(self):
        pub_id = self.publications.create(self.content_id, "instagram", actor="system")
        with self.assertRaises(PublishingError):
            self.publications.publish(pub_id, self.content_row, ManualPublishingChannel(), actor="system")

    def test_manual_channel_blocked_lands_in_failed_and_escalates(self):
        pub_id = self._approved_publication()
        result = self.publications.publish(pub_id, self.content_row, ManualPublishingChannel(), actor="system")
        self.assertEqual(result["status"], "FAILED")
        self.assertIsNotNone(result["needs_aryan_id"])
        item = self.store.get("needs_aryan_items", result["needs_aryan_id"])
        self.assertEqual(item["kind"], "publish_approval")

    def test_simulated_channel_completes_with_labeled_evidence(self):
        pub_id = self._approved_publication()
        result = self.publications.publish(pub_id, self.content_row, SimulatedPublishingChannel(), actor="system")
        self.assertEqual(result["status"], "PUBLISHED")
        self.assertEqual(result["execution_mode"], "simulated")
        evidence = json.loads(result["evidence_json"])
        self.assertTrue(evidence["simulated"])
        self.assertIsNotNone(result["published_at"])


class ManualFallbackTests(PublishingTestBase):
    def test_requires_nonempty_evidence(self):
        pub_id = self._approved_publication()
        self.publications.publish(pub_id, self.content_row, ManualPublishingChannel(), actor="system")  # -> FAILED
        with self.assertRaises(PublishingError):
            self.publications.mark_published_manually(pub_id, {}, actor="Aryan")

    def test_records_real_evidence_and_reaches_published(self):
        pub_id = self._approved_publication()
        self.publications.publish(pub_id, self.content_row, ManualPublishingChannel(), actor="system")  # -> FAILED
        result = self.publications.mark_published_manually(pub_id, {"post_url": "https://instagram.com/p/real"}, actor="Aryan")
        self.assertEqual(result["status"], "PUBLISHED")
        self.assertEqual(result["execution_mode"], "manual")
        self.assertEqual(json.loads(result["evidence_json"])["post_url"], "https://instagram.com/p/real")

    def test_cannot_mark_manual_from_draft(self):
        pub_id = self.publications.create(self.content_id, "instagram", actor="system")
        with self.assertRaises(PublishingError):
            self.publications.mark_published_manually(pub_id, {"post_url": "x"}, actor="Aryan")

    def test_published_is_terminal(self):
        pub_id = self._approved_publication()
        self.publications.publish(pub_id, self.content_row, SimulatedPublishingChannel(), actor="system")
        with self.assertRaises(PublishingError):
            self.publications.mark_published_manually(pub_id, {"post_url": "x"}, actor="Aryan")


class ListTests(PublishingTestBase):
    def test_list_filters_by_content_and_status(self):
        self.publications.create(self.content_id, "instagram", actor="system")
        self.publications.create(self.content_id, "youtube", actor="system")
        rows = self.publications.list(content_id=self.content_id)
        self.assertEqual(len(rows), 2)
        draft_rows = self.publications.list(status="DRAFT")
        self.assertEqual(len(draft_rows), 2)


if __name__ == "__main__":
    unittest.main()
