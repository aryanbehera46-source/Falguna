"""Falguna Browser + Computer Use V1: turns a natural-language objective into
a bounded list of browser steps via the provider-independent ModelRouter
(Section 29), and holds the browser subsystem's own scalar settings
(Section 31).

Provider independence: planning is the ONLY part of a browser task that
needs a model at all. A session created with an explicit `steps` list (the
Settings -> Models "Change model" style precision, or an internal caller
that already knows exactly what to do) never touches a model and runs with
zero provider calls, zero cost, and zero privacy exposure -- this is what
lets "explicit steps in, browser session out" work identically whether or
not any model provider is configured at all. Only `plan_steps_from_objective`
needs the ModelRouter, and if no compatible model is available it raises
the same `FalgunaModelError(NO_COMPATIBLE_MODEL, ...)` every other
model-backed feature already raises -- the browser subsystem itself still
started, the session simply cannot be auto-planned until a model is
configured (exactly Section 29's requirement)."""

import json
from typing import List, Optional

from .browser_runtime import ALL_ACTION_TYPES
from .model_router import ModelRegistry, ModelRouter
from .providers import ErrorCategory, FalgunaModelError
from .store import StateStore, utcnow

BROWSER_SETTINGS_KEY = "browser_settings"

DEFAULT_BROWSER_SETTINGS = {
    "enabled": True,
    "default_headless": True,
    "download_policy": "session_scoped",
    "approval_policy_visible": True,
    "session_persistence": True,
    "max_concurrent_sessions": 2,
    "computer_use_enabled": False,
}

_PLAN_SYSTEM_PROMPT = """You are Falguna's browser task planner. Given an objective, produce a
short, bounded list of browser steps to accomplish it. Output ONLY JSON
matching the given schema. Each step's "action" must be one of: open, back,
forward, click, type, select, scroll, hover, submit, wait, inspect,
screenshot, upload, open_tab, close_tab, switch_tab, extract. "target" is a
CSS selector or URL depending on the action; "value" is text/option/scroll
amount where relevant, or null. Never plan a step that submits a payment,
accepts a contract, deletes an account, changes a password/security
setting, or sends a message on the person's behalf -- Falguna will pause
for human approval before any such step regardless of what you plan, so
planning it is fine, but do not plan MORE such steps than the objective
strictly requires. Keep the plan under 15 steps. Never invent a URL that
was not given or clearly implied by the objective."""

_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "steps": {
            "type": "array", "maxItems": 20,
            "items": {
                "type": "object",
                "properties": {
                    "action": {"type": "string"},
                    "target": {"type": "string"},
                    "value": {"type": ["string", "null"]},
                    "description": {"type": "string"},
                },
                "required": ["action", "target", "description"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["steps"],
    "additionalProperties": False,
}


class BrowserSettingsStore:
    """Reuses the existing generic model_settings key/value table -- see
    falguna/model_router.py's ModelRegistry for the identical pattern."""

    def __init__(self, store: StateStore):
        self.store = store

    def load(self) -> dict:
        rows = self.store.list("model_settings", "key=?", (BROWSER_SETTINGS_KEY,))
        if not rows:
            return dict(DEFAULT_BROWSER_SETTINGS)
        saved = json.loads(rows[0]["value_json"])
        merged = dict(DEFAULT_BROWSER_SETTINGS)
        merged.update({k: v for k, v in saved.items() if k in DEFAULT_BROWSER_SETTINGS})
        return merged

    def save(self, settings: dict) -> None:
        current = self.load()
        current.update({k: v for k, v in settings.items() if k in DEFAULT_BROWSER_SETTINGS})
        rows = self.store.list("model_settings", "key=?", (BROWSER_SETTINGS_KEY,))
        now = utcnow()
        if rows:
            self.store.update("model_settings", rows[0]["id"], value_json=json.dumps(current, sort_keys=True))
        else:
            self.store.create("model_settings", {
                "key": BROWSER_SETTINGS_KEY, "value_json": json.dumps(current, sort_keys=True),
                "created_at": now, "updated_at": now,
            })


def plan_steps_from_objective(store: StateStore, objective: str, task_type: str,
                               model_override: Optional[str] = None, timeout_seconds: int = 60) -> List[dict]:
    """Raises FalgunaModelError (never a bare exception) if no model is
    available or the model's reply cannot be parsed into a valid plan."""
    router = ModelRouter.from_registry(ModelRegistry(store))
    config = {"model": model_override, "base_url": "", "api_key_file": ""}
    payload = {
        "model": model_override,
        "messages": [
            {"role": "system", "content": _PLAN_SYSTEM_PROMPT},
            {"role": "user", "content": f"Task type: {task_type}\nObjective: {objective}"},
        ],
        "response_format": {"type": "json_schema", "json_schema": {"name": "browser_plan", "strict": True, "schema": _PLAN_SCHEMA}},
    }
    decoded = router(config, payload, timeout_seconds)
    message = decoded["choices"][0]["message"]
    if message.get("refusal"):
        raise FalgunaModelError(ErrorCategory.TRANSPORT_FAILURE, "The model declined to plan this browser task.")
    try:
        parsed = json.loads(message["content"])
    except json.JSONDecodeError as exc:
        raise FalgunaModelError(
            ErrorCategory.TRANSPORT_FAILURE, "The model returned a plan Falguna could not parse.",
            technical_detail=str(message.get("content", ""))[:2000],
        ) from exc
    steps = [s for s in parsed.get("steps", []) if s.get("action") in ALL_ACTION_TYPES][:20]
    if not steps:
        raise FalgunaModelError(ErrorCategory.TRANSPORT_FAILURE, "The model returned an empty or invalid browser plan.")
    return steps
