"""Falguna V2.1: deterministic model routing + a persistent provider
registry, built on top of the provider-neutral interface in
falguna/providers.py.

Routing policy (never silent about paid/external usage):
  1. An explicit selection (a conversation's model_override, or a
     provider/model the person picked in Settings) is honored as long
     as that provider is currently reachable and, if it's external,
     Privacy Mode allows external calls at all.
  2. Otherwise, the configured "preferred local model" is tried.
  3. Otherwise, any other local/self-hosted provider that currently has
     at least one model available is tried.
  4. Otherwise -- only when Privacy Mode is not Local Only -- an
     explicitly-enabled external provider is tried.
  5. Otherwise NO_COMPATIBLE_MODEL_AVAILABLE is raised. Falguna never
     silently reaches for a paid provider the person did not enable,
     and never crosses the Local-Only boundary no matter what.

Privacy Mode is enforced structurally, not by filtering results after
the fact: in LOCAL_ONLY mode, `ModelRouter._visible_providers()` never
returns an external provider in the first place, so its
health_check()/generate() are never called and therefore never make a
network call -- this is what the Local-Only privacy test in
tests/test_model_router.py verifies.
"""
import json
from typing import Dict, List, Optional

from .providers import (
    CodexProvider, ErrorCategory, FalgunaModelError, GenericOpenAICompatibleProvider,
    HealthState, ModelProvider, OllamaProvider, ROUTABLE_HEALTH_STATES,
)
from .store import StateStore, utcnow


PRIVACY_MODES = ("LOCAL_ONLY", "HYBRID", "EXTERNAL_ALLOWED")
DEFAULT_PRIVACY_MODE = "HYBRID"

REGISTRY_SETTINGS_KEY = "model_registry"

DEFAULT_REGISTRY_SETTINGS = {
    "privacy_mode": DEFAULT_PRIVACY_MODE,
    "preferred_local_model": None,  # e.g. "ollama/llama3.2:3b"
    "providers": {
        "ollama": {"enabled": True, "base_url": "http://127.0.0.1:11434"},
        "codex": {"enabled": True},
        # A single generic OpenAI-compatible slot, disabled by default. The
        # person fills this in from Settings -> Models -> Provider Setup;
        # Falguna never enables an external provider on its own.
        "openai_compatible": {"enabled": False, "display_name": "Custom OpenAI-compatible", "base_url": "", "is_local": False, "api_key_env": "", "model_allowlist": []},
    },
}


def parse_selector(selector: Optional[str]):
    """('provider_id', 'model_id') from a 'provider/model' selector, or a
    bare legacy Codex model name (every conversation/research row's
    model_override written before this router existed is a bare Codex
    model id) resolved to ('codex', that id) for full backward
    compatibility. An unrecognized bare string is passed through as
    (None, string) so routing can still try to match it against
    whatever a provider's list_models() actually reports."""
    from .codex_transport import SUPPORTED_CODEX_MODELS
    if not selector:
        return None, None
    if "/" in selector:
        provider_id, _, model_id = selector.partition("/")
        return provider_id or None, model_id or None
    if selector in SUPPORTED_CODEX_MODELS:
        return "codex", selector
    return None, selector


class ModelRegistry:
    """Persists provider/routing configuration in the `model_settings`
    table (one row, key='model_registry'). Deep-merges onto
    DEFAULT_REGISTRY_SETTINGS so a registry saved before a new default
    field existed still comes back with that field populated -- the same
    additive-safety principle StateStore's own column migrations use,
    applied to a JSON blob instead of SQL columns."""

    def __init__(self, store: StateStore):
        self.store = store

    def _row(self):
        cur = self.store.db.execute("SELECT * FROM model_settings WHERE key=?", (REGISTRY_SETTINGS_KEY,))
        row = cur.fetchone()
        return dict(row) if row else None

    def load(self) -> Dict:
        row = self._row()
        if not row:
            return json.loads(json.dumps(DEFAULT_REGISTRY_SETTINGS))  # deep copy
        try:
            saved = json.loads(row["value_json"])
        except (json.JSONDecodeError, TypeError):
            saved = {}
        merged = json.loads(json.dumps(DEFAULT_REGISTRY_SETTINGS))
        merged["privacy_mode"] = saved.get("privacy_mode") or merged["privacy_mode"]
        if merged["privacy_mode"] not in PRIVACY_MODES:
            merged["privacy_mode"] = DEFAULT_PRIVACY_MODE
        merged["preferred_local_model"] = saved.get("preferred_local_model", merged["preferred_local_model"])
        for provider_id, defaults in merged["providers"].items():
            saved_provider = (saved.get("providers") or {}).get(provider_id) or {}
            defaults.update({k: v for k, v in saved_provider.items() if k in defaults})
        # A provider saved that isn't one of the known defaults (a future
        # provider type) is preserved rather than dropped.
        for provider_id, saved_provider in (saved.get("providers") or {}).items():
            if provider_id not in merged["providers"]:
                merged["providers"][provider_id] = saved_provider
        return merged

    def save(self, settings: Dict) -> None:
        settings = dict(settings)
        settings["privacy_mode"] = settings.get("privacy_mode") if settings.get("privacy_mode") in PRIVACY_MODES else DEFAULT_PRIVACY_MODE
        payload = json.dumps(settings, sort_keys=True)
        row = self._row()
        if row:
            self.store.update("model_settings", row["id"], value_json=payload)
        else:
            self.store.create("model_settings", {
                "key": REGISTRY_SETTINGS_KEY, "value_json": payload,
                "created_at": utcnow(), "updated_at": utcnow(),
            })

    def public_view(self) -> Dict:
        """Settings-safe rendering: an api_key_env NAME is fine to show
        (it identifies where a credential should come from, not the
        credential itself) but the actual environment value is never
        read into this dict."""
        settings = self.load()
        return {
            "privacy_mode": settings["privacy_mode"],
            "preferred_local_model": settings["preferred_local_model"],
            "providers": settings["providers"],
        }


def build_providers(registry_settings: Dict, codex_use_fallback: bool = True) -> List[ModelProvider]:
    """Constructs live ModelProvider instances from persisted settings.
    Construction itself never touches the network (see providers.py) --
    only the providers whose config marks them `enabled` are built at
    all, so a disabled provider is not merely filtered out later, it
    never exists as an object the router could call."""
    providers: List[ModelProvider] = []
    cfg = registry_settings.get("providers", {})

    ollama_cfg = cfg.get("ollama", {})
    if ollama_cfg.get("enabled", True):
        providers.append(OllamaProvider(base_url=ollama_cfg.get("base_url") or "http://127.0.0.1:11434"))

    codex_cfg = cfg.get("codex", {})
    if codex_cfg.get("enabled", True):
        providers.append(CodexProvider(use_fallback=codex_use_fallback))

    external_cfg = cfg.get("openai_compatible", {})
    if external_cfg.get("enabled") and external_cfg.get("base_url"):
        providers.append(GenericOpenAICompatibleProvider(
            provider_id="openai_compatible",
            display_name=external_cfg.get("display_name") or "Custom OpenAI-compatible",
            base_url=external_cfg.get("base_url", ""),
            is_local=bool(external_cfg.get("is_local", False)),
            api_key_env=external_cfg.get("api_key_env", ""),
            model_allowlist=external_cfg.get("model_allowlist") or [],
        ))

    return providers


class ModelRouter:
    """Drop-in replacement for a `transport: Callable` -- see the module
    docstring for the selection policy. Implements
    `__call__(config, payload, timeout_seconds)` with the exact same
    signature CodexCliJSONTransport/ResilientCodexTransport already use,
    so chat.py/research.py/workers.py need no changes at all."""

    def __init__(self, providers: List[ModelProvider], privacy_mode: str = DEFAULT_PRIVACY_MODE,
                 preferred_local_selector: Optional[str] = None):
        self.providers = {p.provider_id: p for p in providers}
        self.privacy_mode = privacy_mode if privacy_mode in PRIVACY_MODES else DEFAULT_PRIVACY_MODE
        self.preferred_local_selector = preferred_local_selector

    @classmethod
    def from_registry(cls, registry: ModelRegistry, codex_use_fallback: bool = True) -> "ModelRouter":
        settings = registry.load()
        return cls(
            providers=build_providers(settings, codex_use_fallback=codex_use_fallback),
            privacy_mode=settings["privacy_mode"],
            preferred_local_selector=settings.get("preferred_local_model"),
        )

    def _visible_providers(self) -> List[ModelProvider]:
        if self.privacy_mode == "LOCAL_ONLY":
            return [p for p in self.providers.values() if p.is_local]
        return list(self.providers.values())

    def resolve(self, requested_selector: Optional[str]):
        """Returns (provider, model_id) or raises
        FalgunaModelError(NO_COMPATIBLE_MODEL, ...)."""
        visible = self._visible_providers()
        visible_ids = {p.provider_id for p in visible}
        errors: List[str] = []

        provider_id, model_id = parse_selector(requested_selector)
        if provider_id:
            if provider_id in self.providers and provider_id not in visible_ids:
                raise FalgunaModelError(
                    ErrorCategory.NO_COMPATIBLE_MODEL,
                    f"'{self.providers[provider_id].display_name}' is an external provider and Privacy Mode is set to Local Only.",
                    provider_id=provider_id,
                )
            provider = self.providers.get(provider_id)
            if provider and model_id:
                health = provider.health_check()
                if health.state in ROUTABLE_HEALTH_STATES:
                    return provider, model_id
                errors.append(f"{provider_id}: {health.state} -- {health.detail}")

        preferred_provider_id = None
        if self.preferred_local_selector:
            preferred_provider_id, preferred_model_id = parse_selector(self.preferred_local_selector)
            provider = self.providers.get(preferred_provider_id)
            if provider and provider.is_local and provider.provider_id in visible_ids:
                health = provider.health_check()
                if health.state in ROUTABLE_HEALTH_STATES:
                    return provider, preferred_model_id
                errors.append(f"{preferred_provider_id} (preferred local): {health.state} -- {health.detail}")

        for provider in visible:
            if not provider.is_local or provider.provider_id == preferred_provider_id:
                continue
            health = provider.health_check()
            if health.state not in ROUTABLE_HEALTH_STATES:
                errors.append(f"{provider.provider_id}: {health.state} -- {health.detail}")
                continue
            models = provider.list_models()
            if models:
                return provider, models[0].model_id
            errors.append(f"{provider.provider_id}: no local model available yet")

        if self.privacy_mode != "LOCAL_ONLY":
            for provider in visible:
                if provider.is_local:
                    continue
                health = provider.health_check()
                if health.state not in ROUTABLE_HEALTH_STATES:
                    errors.append(f"{provider.provider_id}: {health.state} -- {health.detail}")
                    continue
                models = provider.list_models()
                if models:
                    return provider, models[0].model_id
                errors.append(f"{provider.provider_id}: no allow-listed model available")

        reason = (
            "Privacy Mode is Local Only and no local model runtime is reachable."
            if self.privacy_mode == "LOCAL_ONLY"
            else "No local or explicitly-enabled external provider is currently reachable."
        )
        raise FalgunaModelError(ErrorCategory.NO_COMPATIBLE_MODEL, reason, technical_detail=" | ".join(errors))

    def __call__(self, config: dict, payload: dict, timeout_seconds: int) -> dict:
        requested = config.get("model")
        provider, model_id = self.resolve(requested)
        result = provider.generate(model_id, payload, timeout_seconds)
        result.setdefault("_falguna_metadata", {})
        result["_falguna_metadata"].setdefault("requested_model", requested)
        result["_falguna_metadata"]["routed_provider"] = provider.provider_id
        result["_falguna_metadata"]["routed_model"] = model_id
        result["_falguna_metadata"]["privacy_mode"] = self.privacy_mode
        return result


def provider_status_snapshot(providers: List[ModelProvider]) -> List[Dict]:
    """Read-only status for Settings -> Models: every configured
    provider's health + model list, safe to return verbatim over HTTP
    (health_check()/list_models() never include a secret)."""
    out = []
    for provider in providers:
        health = provider.health_check()
        models = provider.list_models()
        out.append({
            "provider_id": provider.provider_id,
            "display_name": provider.display_name,
            "is_local": provider.is_local,
            "health": health.to_dict(),
            "models": [
                {"model_id": m.model_id, "display_name": m.display_name, "is_local": m.is_local, "notes": m.notes}
                for m in models
            ],
        })
    return out
