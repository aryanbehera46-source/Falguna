"""Tests for the Media Agents (falguna/media_agents.py) -- each one a real
WorkforceWorker driven by the shared WorkforceOrchestrator, not a separate
agent framework. Covers each agent individually plus one full end-to-end
walk of the pipeline (research -> idea -> script -> visual plan -> assets
-> voice -> video -> thumbnail) producing real, inspectable media files.
"""

import json
import tempfile
import unittest
from pathlib import Path

from falguna.analytics_growth import AnalyticsStore, GrowthAgent
from falguna.audit import AuditLog
from falguna.media import BrandStore, ContentStore, ScriptStore
from falguna.media_agents import (
    AnalyticsIngestionAgent,
    ContentStrategistAgent,
    CreativeDirectorAgent,
    GrowthRecommendationAgent,
    PublishingAgent,
    ScriptWriterAgent,
    ThumbnailAgent,
    VideoEditAgent,
    VisualAssetAgent,
    VoiceAgent,
)
from falguna.media_providers import MediaAssetStore, MediaProvider, MediaProviderRegistry, ProviderResult, UnavailableProvider
from falguna.publishing import ManualPublishingChannel, PublicationStore, SimulatedPublishingChannel
from falguna.research import SourceResult
from falguna.store import StateStore
from falguna.ttt_hq import NeedsAryanQueue
from falguna.video_pipeline import VideoPipeline
from falguna.workforce import WorkforceOrchestrator, WorkforceTaskStore
from falguna.workforce_workers import ResearchWorker


class MediaAgentTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = StateStore(self.root / "state.db")
        self.store.migrate()
        self.audit = AuditLog(self.root / "audit.jsonl")
        self.needs_aryan = NeedsAryanQueue(self.store, self.audit)
        self.tasks = WorkforceTaskStore(self.store, self.audit)
        self.brands = BrandStore(self.store, self.audit)
        self.content = ContentStore(self.store, self.audit)
        self.scripts = ScriptStore(self.store, self.audit)
        self.assets = MediaAssetStore(self.store, self.audit, output_root=self.root / "assets")
        self.pipeline = VideoPipeline(self.root / "video_out")
        self.publications = PublicationStore(self.store, self.audit, needs_aryan=self.needs_aryan)
        self.analytics = AnalyticsStore(self.store, self.audit)
        self.growth = GrowthAgent(self.analytics)
        self.brand_id = self.brands.create("TTT", actor="Aryan")

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _orch(self, *workers):
        o = WorkforceOrchestrator(self.store, self.audit, needs_aryan=self.needs_aryan)
        for w in workers:
            o.register_worker(w)
        return o


class ContentStrategistAgentTests(MediaAgentTestBase):
    def test_missing_inputs_fails(self):
        orch = self._orch(ContentStrategistAgent(self.content))
        task_id = self.tasks.create("media", "Idea", "content_idea_creation", actor="system", inputs={})
        result = orch.execute(task_id)
        self.assertEqual(result["status"], "READY")  # FAILED -> retried

    def test_creates_content_and_advances_to_content_plan(self):
        orch = self._orch(ContentStrategistAgent(self.content))
        task_id = self.tasks.create(
            "media", "Idea", "content_idea_creation", actor="system",
            inputs={"brand_id": self.brand_id, "format": "short_form_video", "title": "3 tips", "plan_notes": "hook+tips+cta"},
        )
        result = orch.execute(task_id)
        self.assertEqual(result["status"], "COMPLETED")
        content_id = json.loads(result["outputs_json"])["content_id"]
        content_row = self.content.get(content_id)
        self.assertEqual(content_row["content_state"], "CONTENT_PLAN")
        self.assertEqual(content_row["title"], "3 tips")


class ScriptWriterAgentTests(MediaAgentTestBase):
    def _content_in_plan(self):
        content_id = self.content.create(self.brand_id, "3 tips", "short_form_video", actor="Aryan")
        self.content.transition(content_id, "IDEA", actor="system")
        self.content.transition(content_id, "CONTENT_PLAN", actor="system")
        return content_id

    def test_missing_content_id_fails(self):
        orch = self._orch(ScriptWriterAgent(self.content, self.scripts))
        task_id = self.tasks.create("media", "Write script", "script_writing", actor="system", inputs={})
        result = orch.execute(task_id)
        self.assertEqual(result["status"], "READY")

    def test_unknown_content_id_fails(self):
        orch = self._orch(ScriptWriterAgent(self.content, self.scripts))
        task_id = self.tasks.create("media", "Write script", "script_writing", actor="system", inputs={"content_id": "does-not-exist"})
        result = orch.execute(task_id)
        self.assertEqual(result["status"], "READY")

    def test_creates_script_and_advances_to_script_state(self):
        content_id = self._content_in_plan()
        orch = self._orch(ScriptWriterAgent(self.content, self.scripts))
        task_id = self.tasks.create("media", "Write script", "script_writing", actor="system", inputs={"content_id": content_id, "hook": "Hook", "body": "Body"})
        result = orch.execute(task_id)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(self.content.get(content_id)["content_state"], "SCRIPT")
        script_id = json.loads(result["outputs_json"])["script_id"]
        self.assertEqual(self.scripts.get(script_id)["hook"], "Hook")


class CreativeDirectorAgentTests(MediaAgentTestBase):
    def _content_with_script(self, scenes=None):
        content_id = self.content.create(self.brand_id, "3 tips", "short_form_video", actor="Aryan")
        self.content.transition(content_id, "IDEA", actor="system")
        self.content.transition(content_id, "CONTENT_PLAN", actor="system")
        self.scripts.create_version(content_id, hook="Hook", body="Body", scenes=scenes, actor="Aryan")
        self.content.transition(content_id, "SCRIPT", actor="system")
        return content_id

    def test_no_script_fails(self):
        content_id = self.content.create(self.brand_id, "No script", "short_form_video", actor="Aryan")
        orch = self._orch(CreativeDirectorAgent(self.content, self.scripts))
        task_id = self.tasks.create("media", "Plan visuals", "visual_planning", actor="system", inputs={"content_id": content_id})
        result = orch.execute(task_id)
        self.assertEqual(result["status"], "READY")

    def test_no_scenes_available_fails(self):
        content_id = self._content_with_script(scenes=None)
        orch = self._orch(CreativeDirectorAgent(self.content, self.scripts))
        task_id = self.tasks.create("media", "Plan visuals", "visual_planning", actor="system", inputs={"content_id": content_id})
        result = orch.execute(task_id)
        self.assertEqual(result["status"], "READY")

    def test_builds_scene_plan_from_script_scenes(self):
        content_id = self._content_with_script(scenes=[{"text": "Scene A", "duration_seconds": 2}, {"text": "Scene B"}])
        orch = self._orch(CreativeDirectorAgent(self.content, self.scripts))
        task_id = self.tasks.create("media", "Plan visuals", "visual_planning", actor="system", inputs={"content_id": content_id})
        result = orch.execute(task_id)
        self.assertEqual(result["status"], "COMPLETED")
        scene_plan = json.loads(result["outputs_json"])["scene_plan"]
        self.assertEqual(len(scene_plan), 2)
        self.assertEqual(scene_plan[0]["text"], "Scene A")
        self.assertEqual(scene_plan[1]["duration_seconds"], 3.0)  # default fallback
        self.assertEqual(self.content.get(content_id)["content_state"], "VISUAL_PLAN")

    def test_scenes_can_come_from_task_inputs_instead_of_script(self):
        content_id = self._content_with_script(scenes=None)
        orch = self._orch(CreativeDirectorAgent(self.content, self.scripts))
        task_id = self.tasks.create(
            "media", "Plan visuals", "visual_planning", actor="system",
            inputs={"content_id": content_id, "scenes": [{"text": "Inline scene"}]},
        )
        result = orch.execute(task_id)
        self.assertEqual(result["status"], "COMPLETED")


class VisualAssetAgentTests(MediaAgentTestBase):
    def test_missing_inputs_fails(self):
        orch = self._orch(VisualAssetAgent(self.content, self.assets))
        task_id = self.tasks.create("media", "Gen assets", "visual_asset_generation", actor="system", inputs={})
        result = orch.execute(task_id)
        self.assertEqual(result["status"], "READY")

    def test_generates_real_assets_and_advances_state(self):
        content_id = self.content.create(self.brand_id, "Post", "short_form_video", actor="Aryan")
        for state in ["IDEA", "CONTENT_PLAN", "SCRIPT", "VISUAL_PLAN"]:
            self.content.transition(content_id, state, actor="system")
        orch = self._orch(VisualAssetAgent(self.content, self.assets))
        task_id = self.tasks.create(
            "media", "Gen assets", "visual_asset_generation", actor="system",
            inputs={"content_id": content_id, "scene_plan": [{"text": "Scene A"}, {"text": "Scene B"}]},
        )
        result = orch.execute(task_id)
        self.assertEqual(result["status"], "COMPLETED")
        asset_ids = json.loads(result["outputs_json"])["asset_ids"]
        self.assertEqual(len(asset_ids), 2)
        for asset_id in asset_ids:
            asset = self.assets.get(asset_id)
            self.assertEqual(asset["status"], "COMPLETED")
            self.assertTrue(Path(asset["output_path"]).is_file())
        self.assertEqual(self.content.get(content_id)["content_state"], "ASSET_CREATION")

    def test_blocked_provider_reports_blocked_not_completed(self):
        content_id = self.content.create(self.brand_id, "Post", "short_form_video", actor="Aryan")
        for state in ["IDEA", "CONTENT_PLAN", "SCRIPT", "VISUAL_PLAN"]:
            self.content.transition(content_id, state, actor="system")
        broken_registry = MediaProviderRegistry(providers={"image": UnavailableProvider("simulated outage")})
        broken_assets = MediaAssetStore(self.store, self.audit, registry=broken_registry, output_root=self.root / "assets")
        orch = self._orch(VisualAssetAgent(self.content, broken_assets))
        task_id = self.tasks.create(
            "media", "Gen assets", "visual_asset_generation", actor="system",
            inputs={"content_id": content_id, "scene_plan": [{"text": "Scene A"}]},
        )
        result = orch.execute(task_id)
        self.assertEqual(result["status"], "NEEDS_ARYAN")  # BLOCKED worker result -> escalated
        self.assertEqual(self.content.get(content_id)["content_state"], "VISUAL_PLAN")  # never advanced


class VoiceAgentTests(MediaAgentTestBase):
    def test_missing_inputs_fails(self):
        orch = self._orch(VoiceAgent(self.content, self.assets))
        task_id = self.tasks.create("media", "Gen voice", "voice_generation", actor="system", inputs={})
        result = orch.execute(task_id)
        self.assertEqual(result["status"], "READY")

    def test_generates_real_voice_and_advances_state(self):
        content_id = self.content.create(self.brand_id, "Post", "short_form_video", actor="Aryan")
        for state in ["IDEA", "CONTENT_PLAN", "SCRIPT", "VISUAL_PLAN", "ASSET_CREATION"]:
            self.content.transition(content_id, state, actor="system")
        orch = self._orch(VoiceAgent(self.content, self.assets))
        task_id = self.tasks.create("media", "Gen voice", "voice_generation", actor="system", inputs={"content_id": content_id, "voiceover_text": "Hello there, this is a real test."})
        result = orch.execute(task_id)
        self.assertEqual(result["status"], "COMPLETED", result)
        output_path = json.loads(result["outputs_json"])["output_path"]
        self.assertTrue(Path(output_path).is_file())
        self.assertEqual(self.content.get(content_id)["content_state"], "VOICE")


class VideoEditAgentTests(MediaAgentTestBase):
    def _content_with_assets(self, with_voice=True):
        content_id = self.content.create(self.brand_id, "Post", "short_form_video", actor="Aryan")
        for state in ["IDEA", "CONTENT_PLAN", "SCRIPT", "VISUAL_PLAN"]:
            self.content.transition(content_id, state, actor="system")
        self.assets.generate(content_id, "image", {"text": "Scene A"}, actor="system")
        self.assets.generate(content_id, "image", {"text": "Scene B"}, actor="system")
        self.content.transition(content_id, "ASSET_CREATION", actor="system")
        if with_voice:
            self.assets.generate(content_id, "voice", {"text": "Narration."}, actor="system")
        self.content.transition(content_id, "VOICE", actor="system")
        return content_id

    def test_missing_content_id_fails(self):
        orch = self._orch(VideoEditAgent(self.content, self.assets, self.pipeline))
        task_id = self.tasks.create("media", "Assemble", "video_assembly", actor="system", inputs={})
        result = orch.execute(task_id)
        self.assertEqual(result["status"], "READY")

    def test_no_image_assets_fails(self):
        content_id = self.content.create(self.brand_id, "Post", "short_form_video", actor="Aryan")
        orch = self._orch(VideoEditAgent(self.content, self.assets, self.pipeline))
        task_id = self.tasks.create("media", "Assemble", "video_assembly", actor="system", inputs={"content_id": content_id})
        result = orch.execute(task_id)
        self.assertEqual(result["status"], "READY")

    def test_assembles_real_video_and_advances_through_captions(self):
        content_id = self._content_with_assets(with_voice=True)
        orch = self._orch(VideoEditAgent(self.content, self.assets, self.pipeline))
        task_id = self.tasks.create("media", "Assemble", "video_assembly", actor="system", inputs={"content_id": content_id, "aspect_ratio": "16:9"})
        result = orch.execute(task_id)
        self.assertEqual(result["status"], "COMPLETED", result)
        outputs = json.loads(result["outputs_json"])
        self.assertTrue(Path(outputs["output_path"]).is_file())
        self.assertEqual(self.content.get(content_id)["content_state"], "CAPTIONS")

    def test_assembles_without_voice_track(self):
        content_id = self._content_with_assets(with_voice=False)
        orch = self._orch(VideoEditAgent(self.content, self.assets, self.pipeline))
        task_id = self.tasks.create("media", "Assemble", "video_assembly", actor="system", inputs={"content_id": content_id})
        result = orch.execute(task_id)
        self.assertEqual(result["status"], "COMPLETED", result)
        self.assertFalse(json.loads(result["evidence_json"])["has_audio"])


class ThumbnailAgentTests(MediaAgentTestBase):
    def test_missing_inputs_fails(self):
        orch = self._orch(ThumbnailAgent(self.content, self.assets))
        task_id = self.tasks.create("media", "Thumb", "thumbnail_generation", actor="system", inputs={})
        result = orch.execute(task_id)
        self.assertEqual(result["status"], "READY")

    def test_generates_real_thumbnail_and_advances_state(self):
        content_id = self.content.create(self.brand_id, "Post", "short_form_video", actor="Aryan")
        for state in ["IDEA", "CONTENT_PLAN", "SCRIPT", "VISUAL_PLAN", "ASSET_CREATION", "VOICE", "VIDEO_EDIT", "CAPTIONS"]:
            self.content.transition(content_id, state, actor="system")
        orch = self._orch(ThumbnailAgent(self.content, self.assets))
        task_id = self.tasks.create("media", "Thumb", "thumbnail_generation", actor="system", inputs={"content_id": content_id, "thumbnail_text": "3 Tips"})
        result = orch.execute(task_id)
        self.assertEqual(result["status"], "COMPLETED")
        output_path = json.loads(result["outputs_json"])["output_path"]
        self.assertTrue(Path(output_path).is_file())
        self.assertEqual(self.content.get(content_id)["content_state"], "THUMBNAIL")


class PublishingAgentTests(MediaAgentTestBase):
    def _content_in_approval(self):
        content_id = self.content.create(self.brand_id, "Post", "image_post", actor="Aryan")
        for state in ["IDEA", "CONTENT_PLAN", "SCRIPT", "VISUAL_PLAN", "ASSET_CREATION", "VOICE", "VIDEO_EDIT", "CAPTIONS", "THUMBNAIL", "APPROVAL"]:
            self.content.transition(content_id, state, actor="system")
        return content_id

    def test_missing_inputs_fails(self):
        orch = self._orch(PublishingAgent(self.content, self.publications))
        task_id = self.tasks.create("media", "Publish", "content_publishing", actor="system", inputs={})
        result = orch.execute(task_id)
        self.assertEqual(result["status"], "READY")

    def test_first_call_creates_publication_and_escalates_for_approval(self):
        content_id = self._content_in_approval()
        orch = self._orch(PublishingAgent(self.content, self.publications))
        task_id = self.tasks.create("media", "Publish", "content_publishing", actor="system", inputs={"content_id": content_id, "platform": "instagram"})
        result = orch.execute(task_id)
        self.assertEqual(result["status"], "NEEDS_ARYAN")
        self.assertEqual(self.content.get(content_id)["content_state"], "PUBLISH")
        pubs = self.publications.list(content_id=content_id)
        self.assertEqual(len(pubs), 1)
        self.assertEqual(pubs[0]["status"], "AWAITING_APPROVAL")

    def test_second_call_before_approval_stays_blocked(self):
        content_id = self._content_in_approval()
        orch = self._orch(PublishingAgent(self.content, self.publications))
        task_id = self.tasks.create("media", "Publish", "content_publishing", actor="system", inputs={"content_id": content_id, "platform": "instagram"})
        orch.execute(task_id)
        pub_id = self.publications.list(content_id=content_id)[0]["id"]
        task_id_2 = self.tasks.create("media", "Publish", "content_publishing", actor="system", inputs={"content_id": content_id, "platform": "instagram", "publication_id": pub_id})
        result = orch.execute(task_id_2)
        self.assertEqual(result["status"], "NEEDS_ARYAN")  # BLOCKED, not yet APPROVED

    def test_approved_but_no_real_adapter_escalates_manual_fallback(self):
        content_id = self._content_in_approval()
        orch = self._orch(PublishingAgent(self.content, self.publications))
        task_id = self.tasks.create("media", "Publish", "content_publishing", actor="system", inputs={"content_id": content_id, "platform": "instagram"})
        orch.execute(task_id)
        pub = self.publications.list(content_id=content_id)[0]
        self.needs_aryan.decide(pub["needs_aryan_id"], "approve", actor="Aryan", note="ok")
        self.publications.approve(pub["id"], actor="Aryan")

        task_id_2 = self.tasks.create("media", "Publish", "content_publishing", actor="system", inputs={"content_id": content_id, "platform": "instagram", "publication_id": pub["id"]})
        result = orch.execute(task_id_2)
        self.assertEqual(result["status"], "NEEDS_ARYAN")
        self.assertEqual(self.publications.get(pub["id"])["status"], "FAILED")

    def test_approved_with_simulated_channel_completes_and_advances_content(self):
        content_id = self._content_in_approval()
        orch = self._orch(PublishingAgent(self.content, self.publications, channel=SimulatedPublishingChannel()))
        task_id = self.tasks.create("media", "Publish", "content_publishing", actor="system", inputs={"content_id": content_id, "platform": "instagram"})
        orch.execute(task_id)
        pub = self.publications.list(content_id=content_id)[0]
        self.needs_aryan.decide(pub["needs_aryan_id"], "approve", actor="Aryan", note="ok")
        self.publications.approve(pub["id"], actor="Aryan")

        task_id_2 = self.tasks.create("media", "Publish", "content_publishing", actor="system", inputs={"content_id": content_id, "platform": "instagram", "publication_id": pub["id"]})
        result = orch.execute(task_id_2)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(self.publications.get(pub["id"])["status"], "PUBLISHED")
        self.assertEqual(self.content.get(content_id)["content_state"], "ANALYTICS")


class AnalyticsIngestionAgentTests(MediaAgentTestBase):
    def _published_publication(self):
        content_id = self.content.create(self.brand_id, "Post", "image_post", actor="Aryan")
        pub_id = self.publications.create(content_id, "instagram", actor="system")
        self.publications.mark_ready(pub_id, actor="system")
        pub = self.publications.submit_for_approval(pub_id, actor="system")
        self.needs_aryan.decide(pub["needs_aryan_id"], "approve", actor="Aryan", note="ok")
        self.publications.approve(pub_id, actor="Aryan")
        self.publications.publish(pub_id, self.content.get(content_id), SimulatedPublishingChannel(), actor="system")
        for state in ["IDEA", "CONTENT_PLAN", "SCRIPT", "VISUAL_PLAN", "ASSET_CREATION", "VOICE", "VIDEO_EDIT", "CAPTIONS", "THUMBNAIL", "APPROVAL", "PUBLISH"]:
            self.content.transition(content_id, state, actor="system")
        return content_id, pub_id

    def test_missing_inputs_fails(self):
        orch = self._orch(AnalyticsIngestionAgent(self.content, self.publications, self.analytics))
        task_id = self.tasks.create("media", "Ingest", "analytics_ingestion", actor="system", inputs={})
        result = orch.execute(task_id)
        self.assertEqual(result["status"], "READY")

    def test_non_published_publication_fails(self):
        content_id = self.content.create(self.brand_id, "Post", "image_post", actor="Aryan")
        pub_id = self.publications.create(content_id, "instagram", actor="system")
        orch = self._orch(AnalyticsIngestionAgent(self.content, self.publications, self.analytics))
        task_id = self.tasks.create("media", "Ingest", "analytics_ingestion", actor="system", inputs={"publication_id": pub_id, "metrics": {"views": 10}, "source": "manual_entry"})
        result = orch.execute(task_id)
        self.assertEqual(result["status"], "READY")

    def test_records_metrics_and_advances_content(self):
        content_id, pub_id = self._published_publication()
        orch = self._orch(AnalyticsIngestionAgent(self.content, self.publications, self.analytics))
        task_id = self.tasks.create(
            "media", "Ingest", "analytics_ingestion", actor="system",
            inputs={"publication_id": pub_id, "metrics": {"views": 500, "clicks": 20}, "source": "manual_entry"},
        )
        result = orch.execute(task_id)
        self.assertEqual(result["status"], "COMPLETED")
        rows = self.analytics.list(pub_id)
        self.assertEqual(len(rows), 2)
        self.assertEqual(self.content.get(content_id)["content_state"], "ANALYTICS")


class GrowthRecommendationAgentTests(MediaAgentTestBase):
    def test_missing_inputs_fails(self):
        orch = self._orch(GrowthRecommendationAgent(self.content, self.publications, self.growth))
        task_id = self.tasks.create("media", "Growth", "growth_recommendation", actor="system", inputs={})
        result = orch.execute(task_id)
        self.assertEqual(result["status"], "READY")

    def test_insufficient_data_still_completes_and_advances_to_learn(self):
        content_id = self.content.create(self.brand_id, "Post", "image_post", actor="Aryan")
        pub_id = self.publications.create(content_id, "instagram", actor="system")
        for state in ["IDEA", "CONTENT_PLAN", "SCRIPT", "VISUAL_PLAN", "ASSET_CREATION", "VOICE", "VIDEO_EDIT", "CAPTIONS", "THUMBNAIL", "APPROVAL", "PUBLISH", "ANALYTICS"]:
            self.content.transition(content_id, state, actor="system")
        orch = self._orch(GrowthRecommendationAgent(self.content, self.publications, self.growth))
        task_id = self.tasks.create("media", "Growth", "growth_recommendation", actor="system", inputs={"publication_id": pub_id})
        result = orch.execute(task_id)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(json.loads(result["outputs_json"])["decision"], "insufficient_data")
        self.assertEqual(self.content.get(content_id)["content_state"], "LEARN")

    def test_real_data_drives_recommendation(self):
        content_id = self.content.create(self.brand_id, "Post", "image_post", actor="Aryan")
        pub_id = self.publications.create(content_id, "instagram", actor="system")
        self.analytics.record(pub_id, "views", 1000, source="manual_entry")
        self.analytics.record(pub_id, "engagement", 150, source="manual_entry")
        for state in ["IDEA", "CONTENT_PLAN", "SCRIPT", "VISUAL_PLAN", "ASSET_CREATION", "VOICE", "VIDEO_EDIT", "CAPTIONS", "THUMBNAIL", "APPROVAL", "PUBLISH", "ANALYTICS"]:
            self.content.transition(content_id, state, actor="system")
        orch = self._orch(GrowthRecommendationAgent(self.content, self.publications, self.growth))
        task_id = self.tasks.create("media", "Growth", "growth_recommendation", actor="system", inputs={"publication_id": pub_id})
        result = orch.execute(task_id)
        self.assertEqual(json.loads(result["outputs_json"])["decision"], "repeat")


class EndToEndMediaPipelineTests(MediaAgentTestBase):
    def test_full_pipeline_research_through_thumbnail(self):
        def fake_search(query, count):
            return [SourceResult(url="https://example.com/trend", title="Trend", snippet="AI engineering speed")]

        orch = self._orch(
            ResearchWorker(search_provider=fake_search),
            ContentStrategistAgent(self.content),
            ScriptWriterAgent(self.content, self.scripts),
            CreativeDirectorAgent(self.content, self.scripts),
            VisualAssetAgent(self.content, self.assets),
            VoiceAgent(self.content, self.assets),
            VideoEditAgent(self.content, self.assets, self.pipeline),
            ThumbnailAgent(self.content, self.assets),
        )

        t_research = self.tasks.create("media", "Find trends", "trend_discovery", actor="system", inputs={"query": "AI trends"})
        self.assertEqual(orch.execute(t_research)["status"], "COMPLETED")

        t_idea = self.tasks.create(
            "media", "Create idea", "content_idea_creation", actor="system",
            inputs={"brand_id": self.brand_id, "format": "short_form_video", "title": "3 tips", "plan_notes": "hook+tips+cta"},
        )
        r_idea = orch.execute(t_idea)
        self.assertEqual(r_idea["status"], "COMPLETED")
        content_id = json.loads(r_idea["outputs_json"])["content_id"]

        t_script = self.tasks.create(
            "media", "Write script", "script_writing", actor="system",
            inputs={"content_id": content_id, "hook": "Hook", "body": "Body",
                    "scenes": [{"text": "Scene one"}, {"text": "Scene two"}], "cta": "Follow"},
        )
        self.assertEqual(orch.execute(t_script)["status"], "COMPLETED")

        t_plan = self.tasks.create("media", "Plan visuals", "visual_planning", actor="system", inputs={"content_id": content_id})
        r_plan = orch.execute(t_plan)
        self.assertEqual(r_plan["status"], "COMPLETED")
        scene_plan = json.loads(r_plan["outputs_json"])["scene_plan"]

        t_assets = self.tasks.create("media", "Gen images", "visual_asset_generation", actor="system", inputs={"content_id": content_id, "scene_plan": scene_plan})
        self.assertEqual(orch.execute(t_assets)["status"], "COMPLETED")

        t_voice = self.tasks.create("media", "Gen voice", "voice_generation", actor="system", inputs={"content_id": content_id, "voiceover_text": "Hook. Scene one. Scene two."})
        self.assertEqual(orch.execute(t_voice)["status"], "COMPLETED")

        t_video = self.tasks.create("media", "Assemble", "video_assembly", actor="system", inputs={"content_id": content_id})
        r_video = orch.execute(t_video)
        self.assertEqual(r_video["status"], "COMPLETED")
        self.assertTrue(Path(json.loads(r_video["outputs_json"])["output_path"]).is_file())

        t_thumb = self.tasks.create("media", "Thumb", "thumbnail_generation", actor="system", inputs={"content_id": content_id, "thumbnail_text": "3 Tips"})
        self.assertEqual(orch.execute(t_thumb)["status"], "COMPLETED")

        history = [h["to_state"] for h in self.content.history(content_id)]
        self.assertEqual(
            history,
            ["RESEARCH", "IDEA", "CONTENT_PLAN", "SCRIPT", "VISUAL_PLAN", "ASSET_CREATION", "VOICE", "VIDEO_EDIT", "CAPTIONS", "THUMBNAIL"],
        )


if __name__ == "__main__":
    unittest.main()
