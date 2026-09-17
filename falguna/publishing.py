"""TTT Media/Growth Engine v1 -- Publishing layer (Section 17, Pass D).

Same channel/adapter discipline as `falguna/application_executor.py` and
`falguna/workforce_workers.py`'s browser foundation: `ManualPublishingChannel`
is the only channel wired in by default and it always reports BLOCKED --
there is no real Instagram/YouTube/LinkedIn API or account connection in
this codebase yet, so a "publish" is never silently faked as having
happened. `SimulatedPublishingChannel` exists strictly for QA and is never
reachable through ordinary construction. A publication is marked
PUBLISHED only with real evidence: either a real channel's real evidence,
or evidence a human explicitly supplies after publishing manually outside
this system (`mark_published_manually`) -- exactly the "Needs Aryan/manual
fallback" the build spec asks for when no legitimate API/account
connection exists.
"""

from typing import Any, Callable, Dict, List, Optional

from .audit import AuditLog
from .store import StateStore, utcnow

PUBLICATION_STATUSES = {"DRAFT", "READY", "AWAITING_APPROVAL", "APPROVED", "PUBLISHING", "PUBLISHED", "FAILED"}
TERMINAL_PUBLICATION_STATUSES = {"PUBLISHED"}

# FAILED -> APPROVED lets a real-adapter retry happen after a transient
# failure; FAILED -> PUBLISHED is the manual-fallback path (a blocked "no
# adapter" attempt, then a human publishes it themselves and supplies real
# evidence of that).
ALLOWED_PUBLICATION_TRANSITIONS: Dict[str, set] = {
    "DRAFT": {"READY", "FAILED"},
    "READY": {"AWAITING_APPROVAL", "FAILED"},
    "AWAITING_APPROVAL": {"APPROVED", "FAILED"},
    "APPROVED": {"PUBLISHING", "FAILED"},
    "PUBLISHING": {"PUBLISHED", "FAILED"},
    "FAILED": {"APPROVED", "PUBLISHED"},
    "PUBLISHED": set(),
}


class PublishingError(ValueError):
    pass


class PublishResult:
    def __init__(self, status: str, evidence: Optional[Dict[str, Any]] = None, blocked_reason: Optional[str] = None):
        if status not in {"COMPLETED", "BLOCKED"}:
            raise PublishingError("PublishResult.status must be COMPLETED or BLOCKED")
        self.status = status
        self.evidence = evidence or {}
        self.blocked_reason = blocked_reason


class PublishingChannel:
    name = "publishing_channel"
    kind = "unavailable"

    def attempt(self, content: Dict[str, Any], platform: str, spec: Dict[str, Any]) -> PublishResult:
        raise NotImplementedError


class ManualPublishingChannel(PublishingChannel):
    """The honest default: no real publish adapter/account connection
    exists for any platform yet. Never fabricates a publish."""

    name = "manual_publishing"
    kind = "manual"

    def attempt(self, content: Dict[str, Any], platform: str, spec: Dict[str, Any]) -> PublishResult:
        return PublishResult(
            status="BLOCKED",
            evidence={"reason_detail": f"no real publish adapter/account connection configured for {platform!r}"},
            blocked_reason="no_publish_adapter_available",
        )


class SimulatedPublishingChannel(PublishingChannel):
    """Test/QA-only. Never reachable from ordinary construction. Always
    labels its evidence `simulated: True` so simulated publications can
    never be mistaken for real ones downstream (analytics, reporting)."""

    name = "simulated_publishing"
    kind = "simulated"

    def attempt(self, content: Dict[str, Any], platform: str, spec: Dict[str, Any]) -> PublishResult:
        return PublishResult(status="COMPLETED", evidence={"simulated": True, "platform": platform, "note": "test/QA adapter only -- nothing was really published"})


class PublicationStore:
    def __init__(self, store: StateStore, audit: AuditLog, needs_aryan=None):
        self.store = store
        self.audit = audit
        self.needs_aryan = needs_aryan

    def create(self, content_id: str, platform: str, actor: str = "system") -> str:
        if not platform or not platform.strip():
            raise PublishingError("platform is required")
        now = utcnow()
        pub_id = self.store.create("media_publications", {
            "content_id": content_id, "platform": platform.strip(), "status": "DRAFT", "execution_mode": None,
            "evidence_json": None, "needs_aryan_id": None, "published_at": None,
            "actor": actor, "created_at": now, "updated_at": now,
        })
        self.audit.append("MEDIA_PUBLICATION_CREATED", {"publication_id": pub_id, "content_id": content_id, "platform": platform, "actor": actor})
        return pub_id

    def _transition(self, pub_id: str, to_status: str, actor: str, **fields: Any) -> Dict[str, Any]:
        pub = self._require(pub_id)
        current = pub["status"]
        if current == to_status:
            return pub
        if to_status not in ALLOWED_PUBLICATION_TRANSITIONS.get(current, set()):
            raise PublishingError(f"cannot transition publication from {current} to {to_status}")
        self.store.update("media_publications", pub_id, status=to_status, **fields)
        self.audit.append("MEDIA_PUBLICATION_TRANSITIONED", {"publication_id": pub_id, "from": current, "to": to_status, "actor": actor})
        return self.get(pub_id)

    def mark_ready(self, pub_id: str, actor: str) -> Dict[str, Any]:
        return self._transition(pub_id, "READY", actor)

    def submit_for_approval(self, pub_id: str, actor: str) -> Dict[str, Any]:
        pub = self._require(pub_id)
        needs_aryan_id = None
        if self.needs_aryan is not None:
            needs_aryan_id = self.needs_aryan.create_item(
                "publish_approval", f"Publish approval needed: {pub['platform']}",
                f"Content is ready to publish to {pub['platform']}. Review it and approve or reject before it goes out.",
                actor=actor, ref_type="media_publication", ref_id=pub_id,
            )
        return self._transition(pub_id, "AWAITING_APPROVAL", actor, needs_aryan_id=needs_aryan_id)

    def approve(self, pub_id: str, actor: str) -> Dict[str, Any]:
        pub = self._require(pub_id)
        if pub.get("needs_aryan_id"):
            item = self.store.get("needs_aryan_items", pub["needs_aryan_id"])
            if not item or item["status"] != "APPROVED":
                raise PublishingError("this publication has not been approved yet")
        return self._transition(pub_id, "APPROVED", actor)

    def reject(self, pub_id: str, actor: str, reason: Optional[str] = None) -> Dict[str, Any]:
        return self._transition(pub_id, "FAILED", actor, evidence_json=None)

    def publish(self, pub_id: str, content: Dict[str, Any], channel: PublishingChannel, actor: str = "system", spec: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Requires APPROVED. Never marks PUBLISHED without the channel's
        own real (or, for QA, explicitly simulated) evidence. A BLOCKED
        real-channel attempt (the honest default -- no adapter exists)
        lands in FAILED and escalates a manual-publish Needs Aryan item
        rather than silently doing nothing."""
        pub = self._require(pub_id)
        if pub["status"] != "APPROVED":
            raise PublishingError(f"publication must be APPROVED to publish (currently {pub['status']})")
        self._transition(pub_id, "PUBLISHING", actor)
        result = channel.attempt(content, pub["platform"], spec or {})
        if result.status == "COMPLETED":
            return self._transition(
                pub_id, "PUBLISHED", actor, execution_mode=channel.kind,
                evidence_json=_dumps(result.evidence), published_at=utcnow(),
            )
        needs_aryan_id = pub.get("needs_aryan_id")
        if self.needs_aryan is not None:
            needs_aryan_id = self.needs_aryan.create_item(
                "publish_approval", f"Manual publish needed: {pub['platform']}",
                f"No real publish adapter exists for {pub['platform']} yet. Publish this manually, then "
                "record it as published with real evidence (a link/screenshot).",
                actor=actor, rationale=result.blocked_reason, ref_type="media_publication", ref_id=pub_id,
            )
        return self._transition(pub_id, "FAILED", actor, execution_mode=channel.kind, evidence_json=_dumps(result.evidence), needs_aryan_id=needs_aryan_id)

    def mark_published_manually(self, pub_id: str, evidence: Dict[str, Any], actor: str) -> Dict[str, Any]:
        """The manual fallback: a human published outside this system and
        supplies real proof (a post URL, a screenshot reference). Refuses
        an empty evidence payload -- "manually published" without proof is
        exactly the fabricated-success case this system refuses to allow."""
        if not evidence:
            raise PublishingError("real evidence is required to record a manual publish")
        pub = self._require(pub_id)
        if pub["status"] not in {"FAILED", "APPROVED"}:
            raise PublishingError(f"cannot record a manual publish from status {pub['status']}")
        return self._transition(pub_id, "PUBLISHED", actor, execution_mode="manual", evidence_json=_dumps(evidence), published_at=utcnow())

    def _require(self, pub_id: str) -> Dict[str, Any]:
        pub = self.store.get("media_publications", pub_id)
        if not pub:
            raise PublishingError("publication not found")
        return pub

    def get(self, pub_id: str) -> Optional[Dict[str, Any]]:
        return self.store.get("media_publications", pub_id)

    def list(self, content_id: Optional[str] = None, status: Optional[str] = None) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if content_id:
            clauses.append("content_id=?")
            params.append(content_id)
        if status:
            clauses.append("status=?")
            params.append(status)
        rows = self.store.list("media_publications", " AND ".join(clauses), tuple(params)) if clauses else self.store.list("media_publications")
        return list(reversed(rows))


def _dumps(obj: Any) -> str:
    import json
    return json.dumps(obj)
