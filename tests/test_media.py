"""Tests for the Media/Growth Engine core (falguna/media.py): brands,
campaigns, the content pipeline state graph, and versioned scripts."""

import json
import tempfile
import unittest
from pathlib import Path

from falguna.audit import AuditLog
from falguna.media import (
    ALLOWED_CONTENT_TRANSITIONS,
    CONTENT_STATES,
    BrandStore,
    CampaignStore,
    ContentStore,
    MediaError,
    ScriptStore,
)
from falguna.store import StateStore


class MediaTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = StateStore(root / "state.db")
        self.store.migrate()
        self.audit = AuditLog(root / "audit.jsonl")
        self.brands = BrandStore(self.store, self.audit)
        self.campaigns = CampaignStore(self.store, self.audit)
        self.content = ContentStore(self.store, self.audit)
        self.scripts = ScriptStore(self.store, self.audit)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _brand(self, **kwargs):
        defaults = dict(name="TTT", voice_tone="confident", audience="founders", platforms=["instagram"], content_pillars=["AI"])
        defaults.update(kwargs)
        return self.brands.create(**defaults, actor="Aryan")


class BrandStoreTests(MediaTestBase):
    def test_create_requires_name(self):
        with self.assertRaises(MediaError):
            self.brands.create("")

    def test_create_and_read_stores_lists_as_json(self):
        brand_id = self._brand(platforms=["instagram", "youtube"], content_pillars=["AI", "case studies"])
        brand = self.brands.get(brand_id)
        self.assertEqual(json.loads(brand["platforms_json"]), ["instagram", "youtube"])
        self.assertEqual(json.loads(brand["content_pillars_json"]), ["AI", "case studies"])

    def test_update_merges_fields_and_relists(self):
        brand_id = self._brand()
        self.brands.update(brand_id, actor="Aryan", voice_tone="playful", platforms=["linkedin"])
        brand = self.brands.get(brand_id)
        self.assertEqual(brand["voice_tone"], "playful")
        self.assertEqual(json.loads(brand["platforms_json"]), ["linkedin"])

    def test_update_missing_brand_raises(self):
        with self.assertRaises(MediaError):
            self.brands.update("does-not-exist", actor="Aryan", voice_tone="x")

    def test_list_returns_newest_first(self):
        first = self._brand(name="First")
        second = self._brand(name="Second")
        rows = self.brands.list()
        self.assertEqual(rows[0]["id"], second)
        self.assertEqual(rows[1]["id"], first)


class CampaignStoreTests(MediaTestBase):
    def test_create_requires_name(self):
        brand_id = self._brand()
        with self.assertRaises(MediaError):
            self.campaigns.create(brand_id, "")

    def test_create_defaults_to_active(self):
        brand_id = self._brand()
        campaign_id = self.campaigns.create(brand_id, "Q4 Launch", actor="Aryan")
        self.assertEqual(self.campaigns.get(campaign_id)["status"], "ACTIVE")

    def test_set_status_validates(self):
        brand_id = self._brand()
        campaign_id = self.campaigns.create(brand_id, "Q4 Launch", actor="Aryan")
        with self.assertRaises(MediaError):
            self.campaigns.set_status(campaign_id, "BOGUS", actor="Aryan")
        self.campaigns.set_status(campaign_id, "PAUSED", actor="Aryan")
        self.assertEqual(self.campaigns.get(campaign_id)["status"], "PAUSED")

    def test_list_filters_by_brand(self):
        brand_a = self._brand(name="A")
        brand_b = self._brand(name="B")
        self.campaigns.create(brand_a, "Camp A", actor="Aryan")
        self.campaigns.create(brand_b, "Camp B", actor="Aryan")
        self.assertEqual(len(self.campaigns.list(brand_id=brand_a)), 1)
        self.assertEqual(len(self.campaigns.list()), 2)


class ContentPipelineTests(MediaTestBase):
    def test_create_requires_title_and_valid_format(self):
        brand_id = self._brand()
        with self.assertRaises(MediaError):
            self.content.create(brand_id, "", "short_form_video")
        with self.assertRaises(MediaError):
            self.content.create(brand_id, "Title", "podcast_episode")

    def test_create_starts_in_research(self):
        brand_id = self._brand()
        content_id = self.content.create(brand_id, "3 tips", "short_form_video", actor="Aryan")
        self.assertEqual(self.content.get(content_id)["content_state"], "RESEARCH")
        history = self.content.history(content_id)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["to_state"], "RESEARCH")

    def test_full_pipeline_walk_reaches_learn(self):
        brand_id = self._brand()
        content_id = self.content.create(brand_id, "3 tips", "short_form_video", actor="Aryan")
        path = ["IDEA", "CONTENT_PLAN", "SCRIPT", "VISUAL_PLAN", "ASSET_CREATION", "VOICE",
                "VIDEO_EDIT", "CAPTIONS", "THUMBNAIL", "APPROVAL", "PUBLISH", "ANALYTICS", "LEARN"]
        for state in path:
            self.content.transition(content_id, state, actor="system")
        self.assertEqual(self.content.get(content_id)["content_state"], "LEARN")
        self.assertEqual(len(self.content.history(content_id)), len(path) + 1)

    def test_illegal_skip_ahead_raises(self):
        brand_id = self._brand()
        content_id = self.content.create(brand_id, "Post", "image_post", actor="Aryan")
        with self.assertRaises(MediaError):
            self.content.transition(content_id, "SCRIPT", actor="system")  # RESEARCH -> SCRIPT is not an edge
        self.assertEqual(self.content.get(content_id)["content_state"], "RESEARCH")

    def test_same_state_transition_is_idempotent(self):
        brand_id = self._brand()
        content_id = self.content.create(brand_id, "Post", "image_post", actor="Aryan")
        before = len(self.content.history(content_id))
        self.content.transition(content_id, "RESEARCH", actor="system")
        self.assertEqual(len(self.content.history(content_id)), before)

    def test_cancelled_is_reachable_from_every_pre_terminal_state(self):
        for state in CONTENT_STATES:
            if state in ("LEARN", "CANCELLED"):
                continue
            self.assertIn("CANCELLED", ALLOWED_CONTENT_TRANSITIONS[state], f"{state} cannot reach CANCELLED")

    def test_cancelled_is_terminal(self):
        brand_id = self._brand()
        content_id = self.content.create(brand_id, "Post", "image_post", actor="Aryan")
        self.content.transition(content_id, "CANCELLED", actor="Aryan", reason="scrapped")
        with self.assertRaises(MediaError):
            self.content.transition(content_id, "IDEA", actor="system")

    def test_transition_unknown_state_raises(self):
        brand_id = self._brand()
        content_id = self.content.create(brand_id, "Post", "image_post", actor="Aryan")
        with self.assertRaises(MediaError):
            self.content.transition(content_id, "NOT_A_STATE", actor="system")

    def test_list_filters_by_brand_campaign_and_state(self):
        brand_id = self._brand()
        campaign_id = self.campaigns.create(brand_id, "Camp", actor="Aryan")
        c1 = self.content.create(brand_id, "One", "image_post", campaign_id=campaign_id, actor="Aryan")
        self.content.create(brand_id, "Two", "image_post", actor="Aryan")
        self.assertEqual(len(self.content.list(brand_id=brand_id)), 2)
        self.assertEqual(len(self.content.list(campaign_id=campaign_id)), 1)
        self.assertEqual(self.content.list(campaign_id=campaign_id)[0]["id"], c1)
        self.assertEqual(len(self.content.list(content_state="RESEARCH")), 2)


class ScriptVersioningTests(MediaTestBase):
    def test_first_version_is_one(self):
        brand_id = self._brand()
        content_id = self.content.create(brand_id, "Post", "short_form_video", actor="Aryan")
        script_id = self.scripts.create_version(content_id, hook="Hook", body="Body", actor="Aryan")
        script = self.scripts.get(script_id)
        self.assertEqual(script["version"], 1)

    def test_versions_increment_and_never_mutate_prior(self):
        brand_id = self._brand()
        content_id = self.content.create(brand_id, "Post", "short_form_video", actor="Aryan")
        v1_id = self.scripts.create_version(content_id, hook="Hook v1", body="Body v1", actor="Aryan")
        v2_id = self.scripts.create_version(content_id, hook="Hook v2", body="Body v2", actor="Aryan")
        self.assertEqual(self.scripts.get(v1_id)["hook"], "Hook v1")  # untouched
        self.assertEqual(self.scripts.get(v2_id)["hook"], "Hook v2")
        latest = self.scripts.latest(content_id)
        self.assertEqual(latest["id"], v2_id)
        self.assertEqual(latest["version"], 2)

    def test_list_versions_newest_first(self):
        brand_id = self._brand()
        content_id = self.content.create(brand_id, "Post", "short_form_video", actor="Aryan")
        self.scripts.create_version(content_id, hook="v1", actor="Aryan")
        self.scripts.create_version(content_id, hook="v2", actor="Aryan")
        self.scripts.create_version(content_id, hook="v3", actor="Aryan")
        versions = self.scripts.list_versions(content_id)
        self.assertEqual([v["version"] for v in versions], [3, 2, 1])

    def test_scenes_and_title_options_round_trip_as_json(self):
        brand_id = self._brand()
        content_id = self.content.create(brand_id, "Post", "short_form_video", actor="Aryan")
        script_id = self.scripts.create_version(
            content_id, scenes=[{"n": 1, "text": "intro"}, {"n": 2, "text": "outro"}],
            title_options=["Title A", "Title B"], actor="Aryan",
        )
        script = self.scripts.get(script_id)
        self.assertEqual(json.loads(script["scenes_json"]), [{"n": 1, "text": "intro"}, {"n": 2, "text": "outro"}])
        self.assertEqual(json.loads(script["title_options_json"]), ["Title A", "Title B"])

    def test_no_scripts_returns_none(self):
        brand_id = self._brand()
        content_id = self.content.create(brand_id, "Post", "short_form_video", actor="Aryan")
        self.assertIsNone(self.scripts.latest(content_id))
        self.assertEqual(self.scripts.list_versions(content_id), [])


if __name__ == "__main__":
    unittest.main()
