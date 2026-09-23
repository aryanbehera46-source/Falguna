"""Tests for the Boardroom V2 additions in falguna/ttt_hq.py (TTT Group OS
/ Company Orchestrator v2, Section 18): a Boardroom topic can carry a link
to a Company OS objective, a venture, and/or a risk, plus a persistent
discussion summary and follow-up -- all additive to the existing Boardroom
engine, never a parallel table.
"""

import tempfile
import unittest
from pathlib import Path

from falguna.audit import AuditLog
from falguna.store import StateStore
from falguna.ttt_hq import BoardroomStore


class BoardroomV2Tests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = StateStore(self.root / "state.db")
        self.store.migrate()
        self.audit = AuditLog(self.root / "audit.jsonl")
        self.boardroom = BoardroomStore(self.store, self.audit)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def test_topic_can_be_created_with_links_and_discussion_summary(self):
        topic_id = self.boardroom.create_topic(
            "Should we raise a bridge round?", "Discussing runway extension", "Aryan",
            linked_objective_id="obj-1", linked_venture_id="venture-1",
            discussion_summary="Initial framing from finance.",
        )
        topic = self.boardroom.get_topic(topic_id)
        self.assertEqual(topic["linked_objective_id"], "obj-1")
        self.assertEqual(topic["linked_venture_id"], "venture-1")
        self.assertIsNone(topic["linked_risk_id"])
        self.assertEqual(topic["discussion_summary"], "Initial framing from finance.")
        self.assertIsNone(topic["follow_up"])

    def test_existing_create_topic_calls_are_unaffected(self):
        # Backward compatibility: every existing caller that creates a
        # topic without any v2 fields must keep working exactly as before.
        topic_id = self.boardroom.create_topic("Ship v2 pricing?", "Discuss pricing changes", "Aryan")
        topic = self.boardroom.get_topic(topic_id)
        self.assertIsNone(topic["linked_objective_id"])
        self.assertIsNone(topic["linked_venture_id"])
        self.assertIsNone(topic["linked_risk_id"])

    def test_link_only_changes_fields_actually_supplied(self):
        topic_id = self.boardroom.create_topic("Q?", "S", "Aryan", linked_objective_id="obj-1")
        updated = self.boardroom.link(topic_id, "Aryan", linked_venture_id="venture-1")
        self.assertEqual(updated["linked_objective_id"], "obj-1")  # untouched
        self.assertEqual(updated["linked_venture_id"], "venture-1")

    def test_link_requires_at_least_one_field_and_a_real_topic(self):
        topic_id = self.boardroom.create_topic("Q?", "S", "Aryan")
        with self.assertRaises(ValueError):
            self.boardroom.link(topic_id, "Aryan")
        with self.assertRaises(ValueError):
            self.boardroom.link("not-a-real-topic", "Aryan", linked_venture_id="venture-1")

    def test_set_discussion_summary_and_follow_up(self):
        topic_id = self.boardroom.create_topic("Q?", "S", "Aryan")
        self.boardroom.set_discussion_summary(topic_id, "Board leans toward waiting.", "Aryan")
        self.boardroom.set_follow_up(topic_id, "Revisit next quarter.", "Aryan")
        topic = self.boardroom.get_topic(topic_id)
        self.assertEqual(topic["discussion_summary"], "Board leans toward waiting.")
        self.assertEqual(topic["follow_up"], "Revisit next quarter.")

    def test_discussion_summary_and_follow_up_require_non_empty_content(self):
        topic_id = self.boardroom.create_topic("Q?", "S", "Aryan")
        with self.assertRaises(ValueError):
            self.boardroom.set_discussion_summary(topic_id, "   ", "Aryan")
        with self.assertRaises(ValueError):
            self.boardroom.set_follow_up(topic_id, "", "Aryan")

    def test_list_topics_carries_v2_fields_through(self):
        topic_id = self.boardroom.create_topic("Q?", "S", "Aryan", linked_risk_id="risk-1")
        topics = self.boardroom.list_topics()
        found = next(t for t in topics if t["id"] == topic_id)
        self.assertEqual(found["linked_risk_id"], "risk-1")


if __name__ == "__main__":
    unittest.main()
