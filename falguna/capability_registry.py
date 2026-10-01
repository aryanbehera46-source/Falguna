"""Phase 5 Continuation, Section 14 -- Capability Registry V1.

Answers "can we responsibly deliver this" from real evidence, not
guesswork or an inflated self-assessment. It does this by introspecting
the two things that already define what FALGUNA can actually do, rather
than hand-maintaining a third, parallel list that can silently drift out
of sync with what is really wired up:

  * The live `WorkforceOrchestrator`'s registered workers
    (falguna/workforce.py, workforce_workers.py, agent_roles.py,
    media_agents.py) -- each worker's `name`, the task types it
    `supports()`, and whether Phase 3's restart-safety review marked it
    `auto_resumable_after_restart` are read directly off the real,
    registered object every time `sync_from_workforce()` runs, so this
    can never claim a capability exists that isn't actually registered.
  * `falguna/commercial.py`'s `FoundationStore` -- reused as-is
    (`candidates_for_category`) rather than duplicated; a reusable
    product/foundation is a capability too, and the existing
    PRODUCTION_READY gate already is the evidence for whether it is
    safe to reuse.

What a worker object cannot honestly say about itself -- its model/tool
dependencies, whether it needs a human specialist, known limitations
beyond its own docstring, and whether it is currently fully available,
degraded, or unavailable -- is a small set of human-maintained fields on
the persisted `cs_capabilities` row, exactly the same shape as
`ServiceCatalogStore.set_international_profile`: independently settable,
and `None` (unknown/not yet assessed) until a human sets it. A fresh
`sync_from_workforce()` call always refreshes the introspected facts
(worker_class, supported_task_types, auto_resumable, description) but
never touches the human-maintained fields of a capability row that
already exists, so a repeated sync is non-destructive.

`can_deliver()` is the Section 14 query: whether a routable, registered
worker exists for a task_type right now, folded together with whatever a
human has recorded about that worker's availability and specialist
requirement. It never fabricates a confidence score or a precise
percentage -- only a routable/not-routable fact plus the honest
caveats recorded against it.
"""

import json
from typing import Any, Dict, List, Optional

from .audit import AuditLog
from .store import StateStore, utcnow

CAPABILITY_AVAILABILITY_STATES = {"AVAILABLE", "LIMITED", "UNAVAILABLE"}
CAPABILITY_COST_DRIVERS = {"MODEL_API", "INFRASTRUCTURE", "HUMAN_EXPERT", "OTHER_DIRECT"}


class CapabilityRegistryError(ValueError):
    pass


def _worker_task_types(worker: Any) -> List[str]:
    """Best-effort introspection of a worker's supported task types. Every
    current worker follows the `_SUPPORTED: set` convention (see
    workforce_workers.py/agent_roles.py/media_agents.py), but this is not
    part of the formal `WorkforceWorker` interface (only `supports()` and
    `execute()` are) -- a worker that doesn't follow the convention simply
    reports an empty list here (unknown), never a guess.
    """
    supported = getattr(worker, "_SUPPORTED", None)
    if not supported:
        return []
    return sorted(supported)


def _worker_description(worker: Any) -> Optional[str]:
    doc = (type(worker).__doc__ or "").strip()
    if not doc:
        return None
    # First sentence/line only -- the full docstring (often several
    # paragraphs explaining honest-BLOCKED behavior) stays the class's
    # own documentation; this is a short, human-scannable summary.
    first_line = doc.splitlines()[0].strip()
    return first_line or None


class CapabilityRegistryStore:
    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit

    def sync_from_workforce(self, orchestrator: Any, actor: str = "system") -> List[str]:
        """Upserts one `cs_capabilities` row per currently-registered
        worker on `orchestrator`, keyed by worker_name. Introspected facts
        (worker_class, supported_task_types, auto_resumable, description)
        are always refreshed to match the live object. Human-maintained
        fields (model_tool_dependencies, requires_specialist,
        known_limitations, availability, cost_driver) are left untouched
        on an existing row, and start out `None` (not yet assessed) on a
        newly-discovered one. Returns the list of capability ids touched
        (created or refreshed).
        """
        touched = []
        for worker in getattr(orchestrator, "_workers", []):
            touched.append(self._upsert_worker(worker, actor))
        return touched

    def _upsert_worker(self, worker: Any, actor: str) -> str:
        worker_name = getattr(worker, "name", None)
        if not worker_name:
            raise CapabilityRegistryError("a registered worker has no 'name' -- cannot register its capability")
        cls = type(worker)
        worker_class = f"{cls.__module__}.{cls.__qualname__}"
        supported_task_types = _worker_task_types(worker)
        auto_resumable = bool(getattr(worker, "auto_resumable_after_restart", False))
        description = _worker_description(worker)
        existing = self.store.list("cs_capabilities", "worker_name=?", (worker_name,))
        now = utcnow()
        if existing:
            capability_id = existing[0]["id"]
            self.store.update(
                "cs_capabilities", capability_id,
                worker_class=worker_class, supported_task_types_json=json.dumps(supported_task_types),
                auto_resumable_after_restart=1 if auto_resumable else 0, description=description,
            )
            return capability_id
        capability_id = self.store.create("cs_capabilities", {
            "worker_name": worker_name, "worker_class": worker_class,
            "supported_task_types_json": json.dumps(supported_task_types),
            "auto_resumable_after_restart": 1 if auto_resumable else 0, "description": description,
            "model_tool_dependencies_json": None, "requires_specialist": None,
            "known_limitations": None, "availability": None, "cost_driver": None,
            "actor": actor, "created_at": now, "updated_at": now,
        })
        self.audit.append("CS_CAPABILITY_DISCOVERED", {"capability_id": capability_id, "worker_name": worker_name, "actor": actor})
        return capability_id

    def set_profile(
        self, capability_id: str, actor: str, *,
        model_tool_dependencies: Optional[List[str]] = None,
        requires_specialist: Optional[bool] = None,
        known_limitations: Optional[str] = None,
        availability: Optional[str] = None,
        cost_driver: Optional[str] = None,
    ) -> Dict[str, Any]:
        """The human-maintained half of a capability's record -- see this
        module's docstring. Every field is independently settable and
        `None` means 'leave as-is', not 'clear this field' (use an empty
        string/list explicitly to clear a text/list field).
        """
        capability = self.store.get("cs_capabilities", capability_id)
        if not capability:
            raise CapabilityRegistryError("capability not found")
        if availability is not None and availability not in CAPABILITY_AVAILABILITY_STATES:
            raise CapabilityRegistryError(
                f"unknown availability: {availability!r} (expected one of {sorted(CAPABILITY_AVAILABILITY_STATES)})"
            )
        if cost_driver is not None and cost_driver not in CAPABILITY_COST_DRIVERS:
            raise CapabilityRegistryError(
                f"unknown cost_driver: {cost_driver!r} (expected one of {sorted(CAPABILITY_COST_DRIVERS)})"
            )
        updates: Dict[str, Any] = {}
        if model_tool_dependencies is not None:
            updates["model_tool_dependencies_json"] = json.dumps(model_tool_dependencies)
        if requires_specialist is not None:
            updates["requires_specialist"] = 1 if requires_specialist else 0
        if known_limitations is not None:
            updates["known_limitations"] = known_limitations
        if availability is not None:
            updates["availability"] = availability
        if cost_driver is not None:
            updates["cost_driver"] = cost_driver
        if not updates:
            return capability
        self.store.update("cs_capabilities", capability_id, **updates)
        self.audit.append(
            "CS_CAPABILITY_PROFILE_SET",
            {"capability_id": capability_id, "fields": sorted(updates), "actor": actor},
        )
        return self.store.get("cs_capabilities", capability_id)

    def get(self, capability_id: str) -> Optional[Dict[str, Any]]:
        return self.store.get("cs_capabilities", capability_id)

    def get_by_worker_name(self, worker_name: str) -> Optional[Dict[str, Any]]:
        rows = self.store.list("cs_capabilities", "worker_name=?", (worker_name,))
        return rows[0] if rows else None

    def list(self) -> List[Dict[str, Any]]:
        return self.store.list("cs_capabilities", "1=1")


def can_deliver(registry: CapabilityRegistryStore, orchestrator: Any, task_type: str) -> Dict[str, Any]:
    """The Section 14 evidence-based query: 'can we responsibly deliver a
    task of this type right now?' Routability (does a currently-registered
    worker's supports() return True) is checked live against the real
    orchestrator, never against a possibly-stale registry row. A human's
    recorded availability/specialist caveats are then folded in from the
    matching `cs_capabilities` row, if one has been synced and assessed.

    Returns a structured, evidence-carrying dict -- never a bare bool and
    never a fabricated confidence score:
      {"can_deliver": bool, "worker_name": str|None, "reason": str,
       "availability": str|None, "requires_specialist": bool|None,
       "known_limitations": str|None}
    """
    worker = None
    for candidate in getattr(orchestrator, "_workers", []):
        if candidate.supports(task_type):
            worker = candidate
            break
    if worker is None:
        return {
            "can_deliver": False, "worker_name": None,
            "reason": f"no registered worker supports task_type {task_type!r} -- this must escalate, not be guessed at",
            "availability": None, "requires_specialist": None, "known_limitations": None,
        }
    capability = registry.get_by_worker_name(worker.name)
    availability = capability.get("availability") if capability else None
    requires_specialist = bool(capability["requires_specialist"]) if capability and capability.get("requires_specialist") is not None else None
    known_limitations = capability.get("known_limitations") if capability else None
    if availability == "UNAVAILABLE":
        return {
            "can_deliver": False, "worker_name": worker.name,
            "reason": f"worker {worker.name!r} is registered for this task type but recorded as UNAVAILABLE",
            "availability": availability, "requires_specialist": requires_specialist,
            "known_limitations": known_limitations,
        }
    reason = f"routed to registered worker {worker.name!r}"
    if availability == "LIMITED":
        reason += " (recorded as LIMITED -- review known_limitations before committing)"
    elif availability is None:
        reason += " (availability not yet assessed by a human)"
    return {
        "can_deliver": True, "worker_name": worker.name, "reason": reason,
        "availability": availability, "requires_specialist": requires_specialist,
        "known_limitations": known_limitations,
    }
