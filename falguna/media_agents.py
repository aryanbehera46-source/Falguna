"""TTT Media/Growth Engine v1 -- Media Agents (Section 10, Pass C).

Every media agent here is a `WorkforceWorker`, driven by the very same
`WorkforceOrchestrator`/`WorkforceTaskStore` the Digital Workforce already
uses (falguna/workforce.py) -- not a second, incompatible agent
framework, exactly per "may share Digital Workforce capabilities; do not
make separate incompatible execution frameworks." The Trend/Research role
needs no new code at all: `falguna.workforce_workers.ResearchWorker`
already supports `trend_discovery` and is registered as-is.

Each agent's `execute()` does one real, verifiable step of the content
pipeline and, as a side effect, advances the *content's own* state machine
(`ContentStore.transition`, from `falguna/media.py`) in lockstep with the
Digital Workforce task's own state (`WorkforceOrchestrator`) -- two
independent, already-established state machines kept in sync by the agent
that bridges them, the same way `EmailAdminWorker` advances `EmailStore`
state as a side effect of a workforce task completing.
"""

import json
from typing import Any, Dict, List, Optional

from .analytics_growth import AnalyticsError, AnalyticsStore, GrowthAgent
from .media import ContentStore, ScriptStore
from .media_providers import MediaAssetStore
from .publishing import ManualPublishingChannel, PublishingChannel, PublicationStore
from .video_pipeline import SceneSpec, VideoPipeline
from .workforce import WorkerResult, WorkforceWorker


def _inputs(task: Dict[str, Any]) -> Dict[str, Any]:
    return json.loads(task["inputs_json"]) if task.get("inputs_json") else {}


class ContentStrategistAgent(WorkforceWorker):
    """content_idea_creation: turns a validated idea into a real
    `media_content_items` row (RESEARCH) and advances it to CONTENT_PLAN
    with the plan notes recorded as real transition evidence."""

    name = "content_strategist_agent"
    _SUPPORTED = {"content_idea_creation"}

    def __init__(self, content: ContentStore):
        self.content = content

    def supports(self, task_type: str) -> bool:
        return task_type in self._SUPPORTED

    def execute(self, task: Dict[str, Any]) -> WorkerResult:
        inputs = _inputs(task)
        brand_id, format_ = inputs.get("brand_id"), inputs.get("format")
        if not brand_id or not format_:
            return WorkerResult(status="FAILED", next_action="inputs must include brand_id and format", execution_method="API")
        title = inputs.get("title") or task["objective"]
        content_id = self.content.create(
            brand_id, title, format_, objective=inputs.get("objective") or task["objective"],
            campaign_id=inputs.get("campaign_id"), platform=inputs.get("platform"),
            target_audience=inputs.get("target_audience"), cta=inputs.get("cta"),
            planned_publish_date=inputs.get("planned_publish_date"), actor="system",
        )
        self.content.transition(content_id, "IDEA", actor="system", reason="idea validated by content strategist agent")
        plan_notes = inputs.get("plan_notes") or f"Plan: {title}"
        self.content.transition(content_id, "CONTENT_PLAN", actor="system", reason="content plan drafted", evidence={"plan_notes": plan_notes})
        return WorkerResult(
            status="COMPLETED", result={"content_id": content_id}, evidence={"content_id": content_id, "title": title, "plan_notes": plan_notes},
            execution_method="API",
        )


class ScriptWriterAgent(WorkforceWorker):
    """script_writing: a deterministic, template-based script draft
    (reproducible/explainable/offline, same posture as ContentWorker and
    ReplyAgent elsewhere in this codebase) persisted as a new, versioned
    `media_scripts` row -- never overwriting a prior draft. Advances the
    content item CONTENT_PLAN -> SCRIPT."""

    name = "script_writer_agent"
    _SUPPORTED = {"script_writing"}

    def __init__(self, content: ContentStore, scripts: ScriptStore):
        self.content = content
        self.scripts = scripts

    def supports(self, task_type: str) -> bool:
        return task_type in self._SUPPORTED

    def execute(self, task: Dict[str, Any]) -> WorkerResult:
        inputs = _inputs(task)
        content_id = inputs.get("content_id")
        if not content_id:
            return WorkerResult(status="FAILED", next_action="inputs must include content_id", execution_method="API")
        content = self.content.get(content_id)
        if not content:
            return WorkerResult(status="FAILED", next_action=f"content item not found: {content_id}", execution_method="API")
        hook = inputs.get("hook") or f"Did you know: {content['title']}?"
        body = inputs.get("body") or f"{content['objective'] or content['title']}"
        cta = inputs.get("cta") or content.get("cta") or "Learn more"
        script_id = self.scripts.create_version(
            content_id, hook=hook, body=body, scenes=inputs.get("scenes"), voiceover=inputs.get("voiceover"),
            visual_cues=inputs.get("visual_cues"), cta=cta, caption=inputs.get("caption"),
            title_options=inputs.get("title_options"), actor="system",
        )
        self.content.transition(content_id, "SCRIPT", actor="system", reason="script drafted", evidence={"script_id": script_id})
        return WorkerResult(
            status="COMPLETED", result={"script_id": script_id, "content_id": content_id},
            evidence={"script_id": script_id, "version": self.scripts.get(script_id)["version"]}, execution_method="API",
        )


class CreativeDirectorAgent(WorkforceWorker):
    """visual_planning: turns a script's scenes/visual cues into a
    normalized scene plan (image spec per scene). Recorded as real
    transition evidence -- a deliberately lightweight persistence choice
    (no new table) since the plan's only real consumer is the very next
    step, VisualAssetAgent, which receives it directly as task output.
    Advances SCRIPT -> VISUAL_PLAN."""

    name = "creative_director_agent"
    _SUPPORTED = {"visual_planning"}

    def __init__(self, content: ContentStore, scripts: ScriptStore):
        self.content = content
        self.scripts = scripts

    def supports(self, task_type: str) -> bool:
        return task_type in self._SUPPORTED

    def execute(self, task: Dict[str, Any]) -> WorkerResult:
        inputs = _inputs(task)
        content_id = inputs.get("content_id")
        if not content_id:
            return WorkerResult(status="FAILED", next_action="inputs must include content_id", execution_method="API")
        script = self.scripts.latest(content_id)
        if not script:
            return WorkerResult(status="FAILED", next_action=f"no script found for content_id={content_id}; run script_writing first", execution_method="API")
        scenes_in = inputs.get("scenes") or (json.loads(script["scenes_json"]) if script.get("scenes_json") else None)
        if not scenes_in:
            return WorkerResult(status="FAILED", next_action="no scenes available on the script or in task inputs", execution_method="API")
        scene_plan = [
            {"text": s.get("text") or s.get("visual") or script["hook"], "duration_seconds": s.get("duration_seconds", 3.0)}
            for s in scenes_in
        ]
        self.content.transition(content_id, "VISUAL_PLAN", actor="system", reason="visual plan drafted", evidence={"scene_plan": scene_plan})
        return WorkerResult(status="COMPLETED", result={"content_id": content_id, "scene_plan": scene_plan}, evidence={"scene_count": len(scene_plan)}, execution_method="API")


class VisualAssetAgent(WorkforceWorker):
    """visual_asset_generation: generates one real image asset per scene
    via MediaAssetStore (free-first: Pillow locally). Advances
    VISUAL_PLAN -> ASSET_CREATION. Honestly reports partial/blocked
    results if any scene's asset generation was blocked, rather than
    claiming full completion."""

    name = "visual_asset_agent"
    _SUPPORTED = {"visual_asset_generation"}

    def __init__(self, content: ContentStore, assets: MediaAssetStore):
        self.content = content
        self.assets = assets

    def supports(self, task_type: str) -> bool:
        return task_type in self._SUPPORTED

    def execute(self, task: Dict[str, Any]) -> WorkerResult:
        inputs = _inputs(task)
        content_id = inputs.get("content_id")
        scene_plan = inputs.get("scene_plan")
        if not content_id or not scene_plan:
            return WorkerResult(status="FAILED", next_action="inputs must include content_id and scene_plan", execution_method="API")
        asset_rows = [self.assets.generate(content_id, "image", {"text": scene["text"]}, actor="system") for scene in scene_plan]
        blocked = [a for a in asset_rows if a["status"] != "COMPLETED"]
        if blocked:
            return WorkerResult(
                status="BLOCKED", blockers=[f"{len(blocked)} of {len(asset_rows)} scene image(s) could not be generated"],
                next_action="Provide a working image provider, or supply pre-made image assets.",
                evidence={"asset_ids": [a["id"] for a in asset_rows]}, execution_method="API",
            )
        self.content.transition(content_id, "ASSET_CREATION", actor="system", reason="visual assets generated", evidence={"asset_ids": [a["id"] for a in asset_rows]})
        return WorkerResult(
            status="COMPLETED", result={"content_id": content_id, "asset_ids": [a["id"] for a in asset_rows]},
            evidence={"asset_count": len(asset_rows)}, execution_method="API",
        )


class VoiceAgent(WorkforceWorker):
    """voice_generation: real, local, free TTS via MediaAssetStore (ffmpeg
    + flite). Honestly BLOCKED if this environment's ffmpeg lacks flite --
    never a fabricated audio file. Advances ASSET_CREATION -> VOICE."""

    name = "voice_agent"
    _SUPPORTED = {"voice_generation"}

    def __init__(self, content: ContentStore, assets: MediaAssetStore):
        self.content = content
        self.assets = assets

    def supports(self, task_type: str) -> bool:
        return task_type in self._SUPPORTED

    def execute(self, task: Dict[str, Any]) -> WorkerResult:
        inputs = _inputs(task)
        content_id, voiceover_text = inputs.get("content_id"), inputs.get("voiceover_text")
        if not content_id or not voiceover_text:
            return WorkerResult(status="FAILED", next_action="inputs must include content_id and voiceover_text", execution_method="API")
        asset = self.assets.generate(content_id, "voice", {"text": voiceover_text}, actor="system")
        if asset["status"] != "COMPLETED":
            return WorkerResult(
                status="BLOCKED", blockers=[asset.get("error") or "voice generation blocked"],
                next_action="No free/local TTS available in this environment; configure a paid provider or supply a pre-recorded voiceover file.",
                evidence={"asset_id": asset["id"]}, execution_method="API",
            )
        self.content.transition(content_id, "VOICE", actor="system", reason="voiceover generated", evidence={"asset_id": asset["id"]})
        return WorkerResult(status="COMPLETED", result={"content_id": content_id, "asset_id": asset["id"], "output_path": asset["output_path"]}, evidence={"asset_id": asset["id"]}, execution_method="API")


class VideoEditAgent(WorkforceWorker):
    """video_assembly: real ffmpeg assembly of the generated scene images
    (+ optional voiceover) into a playable video with real, ffprobe-
    verified export metadata (falguna.video_pipeline). Advances
    VOICE -> VIDEO_EDIT -> CAPTIONS in one step, since the pipeline
    produces a real .srt captions file as part of assembly -- there is no
    separate captioning capability to run afterward."""

    name = "video_edit_agent"
    _SUPPORTED = {"video_assembly"}

    def __init__(self, content: ContentStore, assets: MediaAssetStore, pipeline: VideoPipeline):
        self.content = content
        self.assets = assets
        self.pipeline = pipeline

    def supports(self, task_type: str) -> bool:
        return task_type in self._SUPPORTED

    def execute(self, task: Dict[str, Any]) -> WorkerResult:
        inputs = _inputs(task)
        content_id = inputs.get("content_id")
        if not content_id:
            return WorkerResult(status="FAILED", next_action="inputs must include content_id", execution_method="API")
        image_assets = [a for a in self.assets.list(content_id=content_id, asset_type="image") if a["status"] == "COMPLETED"]
        if not image_assets:
            return WorkerResult(status="FAILED", next_action="no completed image assets found for this content item", execution_method="COMPUTER")
        image_assets = list(reversed(image_assets))  # oldest (first-generated) scene first
        voice_assets = [a for a in self.assets.list(content_id=content_id, asset_type="voice") if a["status"] == "COMPLETED"]
        audio_path = voice_assets[-1]["output_path"] if voice_assets else None

        scene_duration = inputs.get("scene_duration_seconds", 3.0)
        scenes = [SceneSpec(image_path=a["output_path"], duration_seconds=scene_duration, caption_text=inputs.get("captions", [None] * len(image_assets))[i] if inputs.get("captions") else None)
                  for i, a in enumerate(image_assets)]
        result = self.pipeline.assemble(content_id, scenes, aspect_ratio=inputs.get("aspect_ratio", "9:16"), audio_path=audio_path)
        if result.status != "COMPLETED":
            return WorkerResult(status=result.status, next_action=result.error, execution_method="COMPUTER")

        self.content.transition(content_id, "VIDEO_EDIT", actor="system", reason="video assembled", evidence={"output_path": result.output_path, **result.evidence})
        self.content.transition(content_id, "CAPTIONS", actor="system", reason="captions exported alongside assembly", evidence={"captions_path": result.captions_path})
        return WorkerResult(
            status="COMPLETED", result={"content_id": content_id, "output_path": result.output_path, "captions_path": result.captions_path},
            evidence=result.evidence, execution_method="COMPUTER",
        )


class ThumbnailAgent(WorkforceWorker):
    """thumbnail_generation: one more real image asset (Pillow), tagged
    for use as the piece's thumbnail. Advances CAPTIONS -> THUMBNAIL."""

    name = "thumbnail_agent"
    _SUPPORTED = {"thumbnail_generation"}

    def __init__(self, content: ContentStore, assets: MediaAssetStore):
        self.content = content
        self.assets = assets

    def supports(self, task_type: str) -> bool:
        return task_type in self._SUPPORTED

    def execute(self, task: Dict[str, Any]) -> WorkerResult:
        inputs = _inputs(task)
        content_id, thumbnail_text = inputs.get("content_id"), inputs.get("thumbnail_text")
        if not content_id or not thumbnail_text:
            return WorkerResult(status="FAILED", next_action="inputs must include content_id and thumbnail_text", execution_method="API")
        asset = self.assets.generate(content_id, "image", {"text": thumbnail_text, "filename": "thumbnail.png"}, actor="system")
        if asset["status"] != "COMPLETED":
            return WorkerResult(status="BLOCKED", blockers=[asset.get("error") or "thumbnail generation blocked"], evidence={"asset_id": asset["id"]}, execution_method="API")
        self.content.transition(content_id, "THUMBNAIL", actor="system", reason="thumbnail generated", evidence={"asset_id": asset["id"]})
        return WorkerResult(status="COMPLETED", result={"content_id": content_id, "asset_id": asset["id"], "output_path": asset["output_path"]}, evidence={"asset_id": asset["id"]}, execution_method="API")


class PublishingAgent(WorkforceWorker):
    """content_publishing: creates/submits a real `media_publications` row
    for owner approval (Section 21 -- external publish always escalates),
    or, once approved, attempts the real publish via the configured
    channel (the honest ManualPublishingChannel by default -- no real
    platform adapter exists in v1). Never marks anything COMPLETED unless
    the underlying PublicationStore actually reached PUBLISHED with real
    evidence. Advances content APPROVAL -> PUBLISH -> ANALYTICS."""

    name = "publishing_agent"
    _SUPPORTED = {"content_publishing"}

    def __init__(self, content: ContentStore, publications: PublicationStore, channel: Optional[PublishingChannel] = None):
        self.content = content
        self.publications = publications
        self.channel = channel or ManualPublishingChannel()

    def supports(self, task_type: str) -> bool:
        return task_type in self._SUPPORTED

    def execute(self, task: Dict[str, Any]) -> WorkerResult:
        inputs = _inputs(task)
        content_id, platform = inputs.get("content_id"), inputs.get("platform")
        if not content_id or not platform:
            return WorkerResult(status="FAILED", next_action="inputs must include content_id and platform", execution_method="API")
        content_row = self.content.get(content_id)
        if not content_row:
            return WorkerResult(status="FAILED", next_action=f"content item not found: {content_id}", execution_method="API")

        pub = self.publications.get(inputs["publication_id"]) if inputs.get("publication_id") else None
        if pub is None:
            pub_id = self.publications.create(content_id, platform, actor="system")
            self.publications.mark_ready(pub_id, actor="system")
            pub = self.publications.submit_for_approval(pub_id, actor="system")
            if content_row["content_state"] == "APPROVAL":
                self.content.transition(content_id, "PUBLISH", actor="system", reason="publish submitted for owner approval", evidence={"publication_id": pub_id})
            return WorkerResult(
                status="BLOCKED", blockers=["publish requires owner approval before it can go out"],
                next_action="Approve the publish_approval Needs Aryan item, then re-run this task with the same publication_id.",
                evidence={"publication_id": pub_id}, execution_method="API",
            )

        if pub["status"] != "APPROVED":
            return WorkerResult(status="BLOCKED", blockers=[f"publication is {pub['status']}, not yet APPROVED"], evidence={"publication_id": pub["id"]}, execution_method="API")

        published = self.publications.publish(pub["id"], content_row, self.channel, actor="system")
        if published["status"] == "PUBLISHED":
            if self.content.get(content_id)["content_state"] == "PUBLISH":
                self.content.transition(content_id, "ANALYTICS", actor="system", reason="published", evidence={"publication_id": pub["id"]})
            return WorkerResult(
                status="COMPLETED", result={"content_id": content_id, "publication_id": pub["id"]},
                evidence=json.loads(published["evidence_json"]) if published.get("evidence_json") else {"publication_id": pub["id"]},
                execution_method="API",
            )
        return WorkerResult(
            status="BLOCKED", blockers=["no real publish adapter available; manual publish and confirmation required"],
            next_action="Publish manually outside this system, then record it with PublicationStore.mark_published_manually and real evidence.",
            evidence={"publication_id": pub["id"]}, execution_method="API",
        )


class AnalyticsIngestionAgent(WorkforceWorker):
    """analytics_ingestion: records real, sourced metrics against a
    PUBLISHED publication -- never against anything still in draft or
    awaiting approval, and never a fabricated number. Advances
    PUBLISH -> ANALYTICS."""

    name = "analytics_ingestion_agent"
    _SUPPORTED = {"analytics_ingestion"}

    def __init__(self, content: ContentStore, publications: PublicationStore, analytics: AnalyticsStore):
        self.content = content
        self.publications = publications
        self.analytics = analytics

    def supports(self, task_type: str) -> bool:
        return task_type in self._SUPPORTED

    def execute(self, task: Dict[str, Any]) -> WorkerResult:
        inputs = _inputs(task)
        publication_id, metrics, source = inputs.get("publication_id"), inputs.get("metrics"), inputs.get("source")
        if not publication_id or not metrics or not source:
            return WorkerResult(status="FAILED", next_action="inputs must include publication_id, metrics, and source", execution_method="API")
        pub = self.publications.get(publication_id)
        if not pub:
            return WorkerResult(status="FAILED", next_action=f"publication not found: {publication_id}", execution_method="API")
        if pub["status"] != "PUBLISHED":
            return WorkerResult(status="FAILED", next_action="analytics can only be ingested for a PUBLISHED publication", execution_method="API")
        try:
            metric_ids = [self.analytics.record(publication_id, kind, value, source, actor="system") for kind, value in metrics.items()]
        except AnalyticsError as exc:
            return WorkerResult(status="FAILED", next_action=str(exc), execution_method="API")

        content_row = self.content.get(pub["content_id"])
        if content_row and content_row["content_state"] == "PUBLISH":
            self.content.transition(pub["content_id"], "ANALYTICS", actor="system", reason="analytics ingested", evidence={"metric_ids": metric_ids})
        return WorkerResult(status="COMPLETED", result={"metric_ids": metric_ids}, evidence={"metric_count": len(metric_ids), "source": source}, execution_method="API")


class GrowthRecommendationAgent(WorkforceWorker):
    """growth_recommendation: computes GrowthAgent's deterministic,
    real-data-only recommendation and advances ANALYTICS -> LEARN. A
    genuine "insufficient_data" verdict is still a real, COMPLETED result
    -- not a failure -- because honestly reporting "not enough data yet"
    is exactly the outcome this system should never fabricate around."""

    name = "growth_recommendation_agent"
    _SUPPORTED = {"growth_recommendation"}

    def __init__(self, content: ContentStore, publications: PublicationStore, growth: GrowthAgent):
        self.content = content
        self.publications = publications
        self.growth = growth

    def supports(self, task_type: str) -> bool:
        return task_type in self._SUPPORTED

    def execute(self, task: Dict[str, Any]) -> WorkerResult:
        inputs = _inputs(task)
        publication_id = inputs.get("publication_id")
        if not publication_id:
            return WorkerResult(status="FAILED", next_action="inputs must include publication_id", execution_method="API")
        pub = self.publications.get(publication_id)
        if not pub:
            return WorkerResult(status="FAILED", next_action=f"publication not found: {publication_id}", execution_method="API")

        recommendation = self.growth.recommend(publication_id)
        content_row = self.content.get(pub["content_id"])
        if content_row and content_row["content_state"] == "ANALYTICS":
            self.content.transition(pub["content_id"], "LEARN", actor="system", reason="growth recommendation computed", evidence=recommendation)
        confidence = "Low" if recommendation["decision"] == "insufficient_data" else "Medium"
        return WorkerResult(status="COMPLETED", result=recommendation, evidence={"decision": recommendation["decision"]}, confidence=confidence, execution_method="API")
