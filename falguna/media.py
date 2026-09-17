"""TTT Media/Growth Engine v1 -- Core (Sections 9-14, Pass C).

Brands, campaigns, and the content pipeline itself: Research -> Idea ->
Content Plan -> Script -> Visual Plan -> Asset Creation -> Voice ->
Video/Edit -> Captions -> Thumbnail -> Approval -> Publish -> Analytics ->
Learn, as a durable, checked state graph -- the exact same discipline
`falguna/lifecycle.py` already uses for the sales pipeline (explicit
forward graph, a CANCELLED dead end reachable from every pre-terminal
state the same way LOST is, a full event history table, idempotent
same-state no-ops), reused rather than reinvented for a second kind of
pipeline.

Scripts are versioned, never overwritten in place: every edit is a new
`media_scripts` row for the same content_id, so revision history is real
history, not a single mutable field. Brand voice/tone/audience/platforms/
pillars/guidelines/policy are stored once per brand and read by every
piece of content and every downstream media agent, so a brand is defined
in exactly one place.
"""

import json
from typing import Any, Dict, List, Optional

from .audit import AuditLog
from .store import StateStore, utcnow

CONTENT_FORMATS = {
    "short_form_video", "long_form_video", "image_post", "carousel", "article_blog", "social_caption",
}

CAMPAIGN_STATUSES = {"ACTIVE", "PAUSED", "COMPLETED"}

CONTENT_STATES = [
    "RESEARCH", "IDEA", "CONTENT_PLAN", "SCRIPT", "VISUAL_PLAN", "ASSET_CREATION",
    "VOICE", "VIDEO_EDIT", "CAPTIONS", "THUMBNAIL", "APPROVAL", "PUBLISH", "ANALYTICS", "LEARN", "CANCELLED",
]
TERMINAL_CONTENT_STATES = {"LEARN", "CANCELLED"}

# Explicit forward graph, deliberately conservative -- same shape as
# lifecycle.ALLOWED_TRANSITIONS. CANCELLED is reachable from every
# pre-terminal stage (content can be scrapped at any point) but is itself
# a dead end. Not every format needs every production step (a static image
# post has nothing for VOICE/VIDEO_EDIT to do) -- those stages are still
# walked through as checkpoints rather than skipped, so the same durable
# history and evidence trail applies uniformly to every format instead of
# branching the graph per format and doubling the surface to get wrong.
ALLOWED_CONTENT_TRANSITIONS: Dict[str, set] = {
    "RESEARCH": {"IDEA", "CANCELLED"},
    "IDEA": {"CONTENT_PLAN", "CANCELLED"},
    "CONTENT_PLAN": {"SCRIPT", "CANCELLED"},
    "SCRIPT": {"VISUAL_PLAN", "CANCELLED"},
    "VISUAL_PLAN": {"ASSET_CREATION", "CANCELLED"},
    "ASSET_CREATION": {"VOICE", "CANCELLED"},
    "VOICE": {"VIDEO_EDIT", "CANCELLED"},
    "VIDEO_EDIT": {"CAPTIONS", "CANCELLED"},
    "CAPTIONS": {"THUMBNAIL", "CANCELLED"},
    "THUMBNAIL": {"APPROVAL", "CANCELLED"},
    "APPROVAL": {"PUBLISH", "CANCELLED"},
    "PUBLISH": {"ANALYTICS", "CANCELLED"},
    "ANALYTICS": {"LEARN", "CANCELLED"},
    "LEARN": set(),
    "CANCELLED": set(),
}


class MediaError(ValueError):
    pass


class BrandStore:
    """One persistent profile per brand -- TTT, Falguna, a future venture,
    or a client brand -- read by every content item and every media agent
    rather than re-specified per piece of content."""

    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit

    def create(
        self, name: str, voice_tone: Optional[str] = None, audience: Optional[str] = None,
        platforms: Optional[List[str]] = None, content_pillars: Optional[List[str]] = None,
        visual_guidelines: Optional[str] = None, publishing_rules: Optional[str] = None,
        approval_policy: Optional[str] = None, actor: str = "Aryan",
    ) -> str:
        if not name or not name.strip():
            raise MediaError("name is required")
        now = utcnow()
        brand_id = self.store.create("media_brands", {
            "name": name.strip(), "voice_tone": voice_tone, "audience": audience,
            "platforms_json": json.dumps(platforms) if platforms is not None else None,
            "content_pillars_json": json.dumps(content_pillars) if content_pillars is not None else None,
            "visual_guidelines": visual_guidelines, "publishing_rules": publishing_rules,
            "approval_policy": approval_policy, "actor": actor, "created_at": now, "updated_at": now,
        })
        self.audit.append("MEDIA_BRAND_CREATED", {"brand_id": brand_id, "name": name, "actor": actor})
        return brand_id

    def update(self, brand_id: str, actor: str, **fields: Any) -> Dict[str, Any]:
        self._require(brand_id)
        allowed = {"voice_tone", "audience", "visual_guidelines", "publishing_rules", "approval_policy"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if "platforms" in fields:
            updates["platforms_json"] = json.dumps(fields["platforms"])
        if "content_pillars" in fields:
            updates["content_pillars_json"] = json.dumps(fields["content_pillars"])
        if updates:
            self.store.update("media_brands", brand_id, **updates)
            self.audit.append("MEDIA_BRAND_UPDATED", {"brand_id": brand_id, "fields": sorted(updates), "actor": actor})
        return self.get(brand_id)

    def _require(self, brand_id: str) -> Dict[str, Any]:
        brand = self.store.get("media_brands", brand_id)
        if not brand:
            raise MediaError("brand not found")
        return brand

    def get(self, brand_id: str) -> Optional[Dict[str, Any]]:
        return self.store.get("media_brands", brand_id)

    def list(self) -> List[Dict[str, Any]]:
        return list(reversed(self.store.list("media_brands")))


class CampaignStore:
    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit

    def create(
        self, brand_id: str, name: str, objective: Optional[str] = None,
        start_date: Optional[str] = None, end_date: Optional[str] = None, actor: str = "Aryan",
    ) -> str:
        if not name or not name.strip():
            raise MediaError("name is required")
        now = utcnow()
        campaign_id = self.store.create("media_campaigns", {
            "brand_id": brand_id, "name": name.strip(), "objective": objective, "status": "ACTIVE",
            "start_date": start_date, "end_date": end_date, "actor": actor, "created_at": now, "updated_at": now,
        })
        self.audit.append("MEDIA_CAMPAIGN_CREATED", {"campaign_id": campaign_id, "brand_id": brand_id, "actor": actor})
        return campaign_id

    def set_status(self, campaign_id: str, status: str, actor: str) -> Dict[str, Any]:
        self._require(campaign_id)
        if status not in CAMPAIGN_STATUSES:
            raise MediaError(f"status must be one of {sorted(CAMPAIGN_STATUSES)}")
        self.store.update("media_campaigns", campaign_id, status=status)
        self.audit.append("MEDIA_CAMPAIGN_STATUS_CHANGED", {"campaign_id": campaign_id, "status": status, "actor": actor})
        return self.get(campaign_id)

    def _require(self, campaign_id: str) -> Dict[str, Any]:
        campaign = self.store.get("media_campaigns", campaign_id)
        if not campaign:
            raise MediaError("campaign not found")
        return campaign

    def get(self, campaign_id: str) -> Optional[Dict[str, Any]]:
        return self.store.get("media_campaigns", campaign_id)

    def list(self, brand_id: Optional[str] = None) -> List[Dict[str, Any]]:
        rows = self.store.list("media_campaigns", "brand_id=?", (brand_id,)) if brand_id else self.store.list("media_campaigns")
        return list(reversed(rows))


class ContentStore:
    """The content pipeline's durable task -- one row per piece of
    content, one `media_content_events` row per real transition, same
    shape as `WorkforceTaskStore`/`LifecycleOrchestrator`."""

    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit

    def create(
        self, brand_id: str, title: str, format: str, objective: Optional[str] = None,
        campaign_id: Optional[str] = None, platform: Optional[str] = None, target_audience: Optional[str] = None,
        cta: Optional[str] = None, planned_publish_date: Optional[str] = None, actor: str = "Aryan",
    ) -> str:
        if not title or not title.strip():
            raise MediaError("title is required")
        if format not in CONTENT_FORMATS:
            raise MediaError(f"format must be one of {sorted(CONTENT_FORMATS)}")
        now = utcnow()
        content_id = self.store.create("media_content_items", {
            "brand_id": brand_id, "campaign_id": campaign_id, "title": title.strip(), "objective": objective,
            "format": format, "platform": platform, "target_audience": target_audience, "cta": cta,
            "content_state": "RESEARCH", "planned_publish_date": planned_publish_date,
            "actor": actor, "created_at": now, "updated_at": now,
        })
        self._record_event(content_id, None, "RESEARCH", actor, "content item created")
        self.audit.append("MEDIA_CONTENT_CREATED", {"content_id": content_id, "brand_id": brand_id, "format": format, "actor": actor})
        return content_id

    def get(self, content_id: str) -> Optional[Dict[str, Any]]:
        return self.store.get("media_content_items", content_id)

    def list(self, brand_id: Optional[str] = None, campaign_id: Optional[str] = None, content_state: Optional[str] = None) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if brand_id:
            clauses.append("brand_id=?")
            params.append(brand_id)
        if campaign_id:
            clauses.append("campaign_id=?")
            params.append(campaign_id)
        if content_state:
            clauses.append("content_state=?")
            params.append(content_state)
        rows = self.store.list("media_content_items", " AND ".join(clauses), tuple(params)) if clauses else self.store.list("media_content_items")
        return list(reversed(rows))

    def _record_event(self, content_id: str, from_state: Optional[str], to_state: str, actor: str, reason: Optional[str], evidence: Optional[Dict[str, Any]] = None) -> None:
        self.store.create("media_content_events", {
            "content_id": content_id, "from_state": from_state, "to_state": to_state, "actor": actor,
            "reason": reason, "evidence_json": json.dumps(evidence) if evidence is not None else None, "created_at": utcnow(),
        })

    def transition(self, content_id: str, to_state: str, actor: str, reason: Optional[str] = None, evidence: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        content = self.get(content_id)
        if not content:
            raise MediaError("content item not found")
        if to_state not in CONTENT_STATES:
            raise MediaError(f"unknown content state: {to_state!r}")
        current = content["content_state"]
        if current == to_state:
            return content  # idempotent no-op
        if to_state not in ALLOWED_CONTENT_TRANSITIONS.get(current, set()):
            raise MediaError(f"cannot transition content from {current} to {to_state}")
        self.store.update("media_content_items", content_id, content_state=to_state)
        self._record_event(content_id, current, to_state, actor, reason, evidence)
        self.audit.append("MEDIA_CONTENT_TRANSITIONED", {"content_id": content_id, "from": current, "to": to_state, "actor": actor})
        return self.get(content_id)

    def history(self, content_id: str) -> List[Dict[str, Any]]:
        return self.store.list("media_content_events", "content_id=?", (content_id,))


class ScriptStore:
    """Versioned scripts -- each edit is a new row, never a mutation of
    the last one, so `list_versions` is real revision history."""

    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit

    def create_version(
        self, content_id: str, hook: Optional[str] = None, body: Optional[str] = None,
        scenes: Optional[List[Dict[str, Any]]] = None, voiceover: Optional[str] = None,
        visual_cues: Optional[str] = None, cta: Optional[str] = None, caption: Optional[str] = None,
        title_options: Optional[List[str]] = None, actor: str = "Aryan",
    ) -> str:
        existing = self.list_versions(content_id)
        next_version = (existing[0]["version"] + 1) if existing else 1
        script_id = self.store.create("media_scripts", {
            "content_id": content_id, "version": next_version, "hook": hook, "body": body,
            "scenes_json": json.dumps(scenes) if scenes is not None else None, "voiceover": voiceover,
            "visual_cues": visual_cues, "cta": cta, "caption": caption,
            "title_options_json": json.dumps(title_options) if title_options is not None else None,
            "actor": actor, "created_at": utcnow(),
        })
        self.audit.append("MEDIA_SCRIPT_VERSION_CREATED", {"script_id": script_id, "content_id": content_id, "version": next_version, "actor": actor})
        return script_id

    def latest(self, content_id: str) -> Optional[Dict[str, Any]]:
        versions = self.list_versions(content_id)
        return versions[0] if versions else None

    def list_versions(self, content_id: str) -> List[Dict[str, Any]]:
        rows = self.store.list("media_scripts", "content_id=?", (content_id,))
        return sorted(rows, key=lambda r: r["version"], reverse=True)

    def get(self, script_id: str) -> Optional[Dict[str, Any]]:
        return self.store.get("media_scripts", script_id)
