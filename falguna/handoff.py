"""Revenue Hunter -> Falguna Engineering handoff boundary.

PASS 1, item 6: Revenue Hunter stays a separate app for now. This module is
the clean seam for later -- a won opportunity becomes a structured job
payload, which becomes a real Falguna Engineering mission. Nothing calls
this yet (no wiring exists on the Revenue Hunter side), but the acceptor
itself is fully functional: a valid payload creates a real mission through
the same ControlPlane.create_mission every other mission goes through.
No fake handoff, no UI-only placeholder -- if this raises, nothing was
silently accepted; if it returns, a real mission row exists.
"""

from pathlib import Path
from typing import Any, Dict, Optional

from .models import RunPolicy

REQUIRED_FIELDS = {"title", "requirement", "repository"}


def validate_handoff_payload(payload: Dict[str, Any]) -> None:
    if not isinstance(payload, dict):
        raise ValueError("handoff payload must be an object")
    missing = REQUIRED_FIELDS - payload.keys()
    if missing:
        raise ValueError(f"handoff payload missing required fields: {sorted(missing)}")
    if not isinstance(payload["title"], str) or not payload["title"].strip():
        raise ValueError("title must be a non-empty string")
    if not isinstance(payload["requirement"], str) or not payload["requirement"].strip():
        raise ValueError("requirement must be a non-empty string")
    if not isinstance(payload["repository"], str) or not payload["repository"].strip():
        raise ValueError("repository must be a non-empty path string")
    repository = Path(payload["repository"])
    if not repository.is_dir() or not (repository / ".git").exists():
        raise ValueError(f"repository is not a local git checkout: {repository}")


def accept_revenue_hunter_handoff(control, payload: Dict[str, Any], policy: Optional[RunPolicy] = None) -> Dict[str, Any]:
    """Turn a won-opportunity job payload into a real Falguna Engineering mission.

    Expected payload shape (the future Revenue Hunter -> Falguna contract):
        {
          "title": str,                       # required
          "requirement": str,                 # required -- the job/scope description
          "repository": str,                  # required -- local path to the target repo
          "source_opportunity_id": str|None,  # optional -- Revenue Hunter's own id, for traceability
          "client_name": str|None,            # optional
          "price": number|None,               # optional
          "policy_overrides": dict|None,      # optional -- passed to RunPolicy(**overrides)
        }
    """
    validate_handoff_payload(payload)
    repository = Path(payload["repository"]).resolve()
    if policy is None:
        overrides = payload.get("policy_overrides") or {}
        policy = RunPolicy(**overrides) if overrides else RunPolicy()
    result = control.create_mission(payload["title"].strip(), payload["requirement"].strip(), repository, policy)
    control.audit.append("REVENUE_HUNTER_HANDOFF_ACCEPTED", {
        "mission_id": result["mission_id"],
        "source_opportunity_id": payload.get("source_opportunity_id"),
        "client_name": payload.get("client_name"),
    })
    return result
