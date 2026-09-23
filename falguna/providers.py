"""Falguna V2.1: provider-neutral model access layer.

Chat, Research, and Work must never hard-depend on any one vendor's
runtime being installed, authenticated, or under quota. This module is
the seam: every concrete provider (a local Ollama runtime, a generic
OpenAI-compatible HTTP endpoint that may itself be self-hosted, or the
pre-existing Codex CLI adapter) implements the same ModelProvider
interface and returns the same transport-call result shape
(`falguna.codex_transport.CodexCliJSONTransport.__call__`'s return
value) so a `ModelRouter` (falguna/model_router.py) can be dropped in
anywhere a `transport: Callable` was previously accepted -- chat.py,
research.py, and workers.py do not need to change to become
provider-neutral.

Hard constraints carried over from the rest of this codebase:
 - Never fabricate a successful reply. A provider that cannot actually
   answer raises FalgunaModelError with a sanitized, user-safe
   category and message; only `technical_detail` may carry raw
   provider output, and callers must keep that out of the primary
   error surface (see chat.py / web.py's chat error rendering).
 - No constructor here makes a network call, spawns a process, or
   attempts a model install/download. `list_models()` and
   `health_check()` may probe a *local* daemon that is already
   supposed to be running (a quick, short-timeout HTTP GET), but they
   never trigger a download and never raise -- an absent runtime is
   reported as OFFLINE, not an exception.
 - Local means "inference happens on this machine, nothing leaves it"
   -- not merely "the client process runs locally". The Codex CLI
   process runs locally but calls out to OpenAI's servers, so it is
   `is_local = False`; a generic OpenAI-compatible endpoint is
   `is_local` only when the person has told Falguna its base URL is
   their own self-hosted server.
"""
import json
import os
import shutil
import socket
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .codex_transport import (
    CodexCliJSONTransport,
    ModelUnsupportedError,
    ResilientCodexTransport,
    SUPPORTED_CODEX_MODELS,
)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------- health

class HealthState:
    """Every state a provider can honestly report about itself. There is
    deliberately no "AUTHENTICATED"/"WORKING" state that would require a
    real model call to prove -- HEALTHY means "reachable and configured",
    which is the most a health check can verify without spending a real
    request."""
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    RATE_LIMITED = "RATE_LIMITED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    OFFLINE = "OFFLINE"
    UNSUPPORTED = "UNSUPPORTED"


ALL_HEALTH_STATES = frozenset({
    HealthState.HEALTHY, HealthState.DEGRADED, HealthState.RATE_LIMITED,
    HealthState.AUTH_REQUIRED, HealthState.OFFLINE, HealthState.UNSUPPORTED,
})

# A router may route to a provider reporting one of these states; the rest
# mean "do not even try this provider right now".
ROUTABLE_HEALTH_STATES = frozenset({HealthState.HEALTHY, HealthState.DEGRADED})


class ErrorCategory:
    """Sanitized, user-facing failure categories. Every FalgunaModelError
    carries exactly one of these -- never a raw exception class name or
    vendor-specific error code -- so the UI can show a consistent,
    honest, non-alarming message and a consistent set of recovery
    actions (Retry / Change model / Use local model / Open Settings)
    regardless of which provider or transport actually failed."""
    RATE_LIMITED = "RATE_LIMITED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    PROVIDER_OFFLINE = "PROVIDER_OFFLINE"
    MODEL_UNSUPPORTED = "MODEL_UNSUPPORTED"
    TIMEOUT = "TIMEOUT"
    TRANSPORT_FAILURE = "TRANSPORT_FAILURE"
    NO_COMPATIBLE_MODEL = "NO_COMPATIBLE_MODEL"
    # Not a provider problem at all -- Falguna's own code raised
    # something unexpected. Kept distinct so this is never mislabeled as
    # "the provider is having trouble" when it might be a real bug.
    UNEXPECTED_FAILURE = "UNEXPECTED_FAILURE"


ALL_ERROR_CATEGORIES = frozenset({
    ErrorCategory.RATE_LIMITED, ErrorCategory.AUTH_REQUIRED, ErrorCategory.PROVIDER_OFFLINE,
    ErrorCategory.MODEL_UNSUPPORTED, ErrorCategory.TIMEOUT, ErrorCategory.TRANSPORT_FAILURE,
    ErrorCategory.NO_COMPATIBLE_MODEL, ErrorCategory.UNEXPECTED_FAILURE,
})

# Plain-language sentence for a category when the raising site did not
# already compose a more specific one. Every FalgunaModelError below sets
# its own specific `message`, so this map is mainly a safety net for any
# future caller that raises with only a category.
CATEGORY_SAFE_MESSAGE = {
    ErrorCategory.RATE_LIMITED: "The model provider is rate-limited or over quota right now.",
    ErrorCategory.AUTH_REQUIRED: "This model provider needs authentication Falguna doesn't currently have.",
    ErrorCategory.PROVIDER_OFFLINE: "This model provider isn't reachable right now.",
    ErrorCategory.MODEL_UNSUPPORTED: "The selected model isn't available through this provider right now.",
    ErrorCategory.TIMEOUT: "The model didn't respond in time.",
    ErrorCategory.TRANSPORT_FAILURE: "Something went wrong talking to the model provider.",
    ErrorCategory.NO_COMPATIBLE_MODEL: "No compatible model is currently available.",
    ErrorCategory.UNEXPECTED_FAILURE: "Falguna hit an unexpected internal error.",
}


class FalgunaModelError(RuntimeError):
    """Raised by any ModelProvider or the ModelRouter on failure.

    `message` must always be safe to show a user verbatim. `technical_detail`
    may contain raw provider stdout/stderr/HTTP bodies -- it exists only for
    an expandable "technical details" panel and must never be concatenated
    into `message` or into anything rendered by default.
    """

    def __init__(self, category: str, message: str, technical_detail: str = "", provider_id: Optional[str] = None):
        self.category = category if category in ALL_ERROR_CATEGORIES else ErrorCategory.TRANSPORT_FAILURE
        self.message = message or CATEGORY_SAFE_MESSAGE[self.category]
        self.technical_detail = technical_detail or ""
        self.provider_id = provider_id
        super().__init__(f"{self.category}: {self.message}")


# --------------------------------------------------------------------- data

@dataclass
class ModelInfo:
    provider_id: str
    model_id: str
    display_name: str
    is_local: bool
    supports_json_schema: bool = True
    supports_streaming: bool = False
    supports_tools: bool = False
    supports_vision: bool = False
    context_window: Optional[int] = None
    notes: str = ""


@dataclass
class ProviderHealth:
    state: str
    detail: str = ""
    checked_at: str = field(default_factory=_utcnow)

    def to_dict(self) -> Dict[str, str]:
        return {"state": self.state, "detail": self.detail, "checked_at": self.checked_at}


# --------------------------------------------------------------------- interface

class ModelProvider(ABC):
    """One vendor/runtime. Constructing an instance must never touch the
    network or the filesystem beyond reading already-resolved config --
    all I/O happens inside list_models/health_check/generate, and only
    list_models/health_check are ever called speculatively (e.g. to
    populate Settings -> Models), so both must be cheap, short-timeout,
    and exception-free from the caller's point of view."""

    provider_id: str = ""
    display_name: str = ""
    is_local: bool = False

    @abstractmethod
    def list_models(self) -> List[ModelInfo]:
        """Models this provider can actually serve right now, without
        installing or downloading anything. An empty list is a normal,
        honest answer (nothing pulled locally yet; nothing allow-listed)."""

    @abstractmethod
    def health_check(self) -> ProviderHealth:
        """Never raises. Any failure to reach the provider is itself the
        answer (OFFLINE), not an exception."""

    @abstractmethod
    def generate(self, model_id: str, payload: dict, timeout_seconds: int) -> dict:
        """Same call/return contract as
        CodexCliJSONTransport.__call__(config, payload, timeout_seconds),
        minus the `config` wrapper (the router already resolved the model).
        Raises FalgunaModelError on any failure; never returns a
        fabricated/partial success."""


# --------------------------------------------------------------------- Codex

class CodexProvider(ModelProvider):
    """Wraps the pre-existing CodexCliJSONTransport/ResilientCodexTransport
    behind the ModelProvider interface, unchanged in behavior (same
    executable discovery, same CODEX_HOME resolution, same ephemeral
    read-only sandbox, same supported-model fallback chain) -- Codex
    remains a fully supported, optional adapter; this class only adapts
    its calling convention and its exceptions.

    `is_local = False`: the CLI process runs on this machine, but every
    real request leaves it for OpenAI's servers, so Privacy Mode = Local
    Only must exclude Codex exactly like any other external provider.
    """

    provider_id = "codex"
    display_name = "Codex CLI (optional, subscription-billed, external)"
    is_local = False

    def __init__(self, codex_home: Optional[Path] = None, timeout_seconds: int = 180, use_fallback: bool = True):
        self.codex_home = Path(codex_home) if codex_home else Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        self.timeout_seconds = timeout_seconds
        # Work Mode (Section 6 of Product Experience V2): FAST trades
        # resilience for a quick, single-attempt answer -- use_fallback=False
        # keeps that promise true by calling the bare CodexCliJSONTransport
        # instead of wrapping it in ResilientCodexTransport's cross-model
        # fallback chain. BALANCED/DEEP set this True, exactly as before this
        # class existed (_build_transport's `use_fallback` flag).
        self.use_fallback = use_fallback

    @staticmethod
    def _executable() -> Optional[str]:
        return shutil.which("codex")

    def list_models(self) -> List[ModelInfo]:
        if not self._executable():
            return []
        return [
            ModelInfo(
                provider_id=self.provider_id, model_id=model, display_name=model, is_local=False,
                supports_json_schema=True,
                notes="Requires an authenticated local Codex CLI runtime; billed to the existing subscription.",
            )
            for model in sorted(SUPPORTED_CODEX_MODELS)
        ]

    def health_check(self) -> ProviderHealth:
        if not self._executable():
            return ProviderHealth(HealthState.OFFLINE, "No authenticated Codex executable found on PATH.")
        return ProviderHealth(HealthState.HEALTHY, "Codex executable found on PATH.")

    def generate(self, model_id: str, payload: dict, timeout_seconds: int) -> dict:
        executable = self._executable()
        if not executable:
            raise FalgunaModelError(
                ErrorCategory.PROVIDER_OFFLINE,
                "The Codex runtime is not installed or authenticated on this machine.",
                provider_id=self.provider_id,
            )
        base = CodexCliJSONTransport(Path(executable), self.codex_home, timeout_seconds=self.timeout_seconds)
        transport = ResilientCodexTransport(base) if self.use_fallback else base
        try:
            return transport({"model": model_id}, payload, timeout_seconds)
        except ModelUnsupportedError as exc:
            raise FalgunaModelError(
                ErrorCategory.MODEL_UNSUPPORTED, f"Codex does not support model '{model_id}' right now.",
                technical_detail=str(exc), provider_id=self.provider_id,
            ) from exc
        except OSError as exc:
            detail = str(exc)
            if "timed out" in detail.lower():
                raise FalgunaModelError(
                    ErrorCategory.TIMEOUT, "The Codex runtime did not respond in time.",
                    technical_detail=detail, provider_id=self.provider_id,
                ) from exc
            raise FalgunaModelError(
                ErrorCategory.TRANSPORT_FAILURE, "The Codex runtime failed to produce a response.",
                technical_detail=detail, provider_id=self.provider_id,
            ) from exc


# --------------------------------------------------------------------- Ollama

class OllamaProvider(ModelProvider):
    """First-class local provider. Talks to an already-running Ollama
    daemon over plain HTTP (stdlib urllib only -- no new dependency).
    Never issues a `pull` -- `list_models()` only reports what the
    daemon already reports as locally present, so this can never trigger
    a silent multi-gigabyte download. Appropriate for an Intel Mac: no
    assumption of Apple Silicon/CUDA/large VRAM is made anywhere in this
    class; model *choice* (small vs. large) is entirely up to whatever
    the person has already pulled themselves.
    """

    provider_id = "ollama"
    display_name = "Ollama (local, self-hosted, on this machine)"
    is_local = True

    def __init__(self, base_url: str = "http://127.0.0.1:11434", connect_timeout: float = 1.5):
        self.base_url = (base_url or "http://127.0.0.1:11434").rstrip("/")
        self.connect_timeout = connect_timeout

    def _get(self, path: str, timeout: float):
        with urllib.request.urlopen(urllib.request.Request(f"{self.base_url}{path}"), timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")

    def _post(self, path: str, body: dict, timeout: float):
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(f"{self.base_url}{path}", data=data, method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")

    def list_models(self) -> List[ModelInfo]:
        try:
            _, body = self._get("/api/tags", self.connect_timeout)
        except Exception:
            return []
        models = body.get("models", []) if isinstance(body, dict) else []
        out = []
        for entry in models:
            model_id = entry.get("model") or entry.get("name")
            if not model_id:
                continue
            out.append(ModelInfo(
                provider_id=self.provider_id, model_id=model_id, display_name=entry.get("name", model_id),
                is_local=True, supports_json_schema=True,
                notes="Already pulled locally -- Falguna never downloads a model without explicit approval.",
            ))
        return out

    def health_check(self) -> ProviderHealth:
        try:
            status, body = self._get("/api/tags", self.connect_timeout)
        except Exception:
            return ProviderHealth(HealthState.OFFLINE, f"No local Ollama runtime reachable at {self.base_url}.")
        if status == 200:
            count = len(body.get("models", [])) if isinstance(body, dict) else 0
            if count:
                return ProviderHealth(HealthState.HEALTHY, f"{count} model(s) available locally.")
            return ProviderHealth(HealthState.DEGRADED, "Ollama is running but no model has been pulled yet.")
        return ProviderHealth(HealthState.DEGRADED, f"Unexpected response ({status}) from the local Ollama runtime.")

    def generate(self, model_id: str, payload: dict, timeout_seconds: int) -> dict:
        schema = payload["response_format"]["json_schema"]["schema"]
        body = {"model": model_id, "messages": payload["messages"], "stream": False, "format": schema}
        try:
            _, decoded = self._post("/api/chat", body, timeout_seconds)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:2000] if hasattr(exc, "read") else str(exc)
            if exc.code == 404:
                raise FalgunaModelError(
                    ErrorCategory.MODEL_UNSUPPORTED, f"Model '{model_id}' is not pulled in the local Ollama runtime.",
                    technical_detail=detail, provider_id=self.provider_id,
                ) from exc
            if exc.code == 429:
                raise FalgunaModelError(
                    ErrorCategory.RATE_LIMITED, "The local Ollama runtime is busy handling another request.",
                    technical_detail=detail, provider_id=self.provider_id,
                ) from exc
            raise FalgunaModelError(
                ErrorCategory.TRANSPORT_FAILURE, "The local Ollama runtime returned an error.",
                technical_detail=detail, provider_id=self.provider_id,
            ) from exc
        except (socket.timeout, TimeoutError) as exc:
            raise FalgunaModelError(
                ErrorCategory.TIMEOUT, "The local Ollama runtime did not respond in time.",
                technical_detail=str(exc), provider_id=self.provider_id,
            ) from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (socket.timeout, TimeoutError)):
                raise FalgunaModelError(
                    ErrorCategory.TIMEOUT, "The local Ollama runtime did not respond in time.",
                    technical_detail=str(exc), provider_id=self.provider_id,
                ) from exc
            raise FalgunaModelError(
                ErrorCategory.PROVIDER_OFFLINE, "No local Ollama runtime is reachable.",
                technical_detail=str(exc), provider_id=self.provider_id,
            ) from exc
        except OSError as exc:
            raise FalgunaModelError(
                ErrorCategory.PROVIDER_OFFLINE, "No local Ollama runtime is reachable.",
                technical_detail=str(exc), provider_id=self.provider_id,
            ) from exc
        message = decoded.get("message") or {}
        content = message.get("content", "")
        if not content:
            raise FalgunaModelError(
                ErrorCategory.TRANSPORT_FAILURE, "The local model returned an empty response.",
                technical_detail=json.dumps(decoded)[:2000], provider_id=self.provider_id,
            )
        try:
            json.loads(content)
        except json.JSONDecodeError as exc:
            raise FalgunaModelError(
                ErrorCategory.TRANSPORT_FAILURE, "The local model's response could not be parsed as the requested format.",
                technical_detail=content[:2000], provider_id=self.provider_id,
            ) from exc
        usage = {
            "prompt_tokens": int(decoded.get("prompt_eval_count", 0) or 0),
            "completion_tokens": int(decoded.get("eval_count", 0) or 0),
            "prompt_tokens_details": {"cached_tokens": 0},
        }
        return {
            "choices": [{"message": {"content": content}}],
            "usage": usage,
            "_falguna_provider": "ollama-local",
            "_falguna_cost_usd": 0.0,
            "_falguna_metadata": {
                "adapter": "ollama-local", "billing": "local-compute-no-cash-cost",
                "cost_basis": "local-zero-marginal-cost", "routed_model": model_id,
            },
        }


# --------------------------------------------------------------------- generic OpenAI-compatible

class GenericOpenAICompatibleProvider(ModelProvider):
    """Any HTTP endpoint speaking the OpenAI chat-completions shape --
    a real external vendor, or a self-hosted server the person runs
    themselves (llama.cpp's server, vLLM, LM Studio, a future Falguna
    self-hosted inference box). `is_local` is not guessed from the URL;
    it is whatever the person told Falguna when they configured this
    provider (Settings -> Models -> Provider Setup), because only they
    know whether that base URL is their own machine or a vendor's cloud.

    Disabled and unconfigured by default. An API key is never stored in
    plaintext here or in the registry -- only the name of an environment
    variable Falguna should read it from at call time, so the registry
    (which is visible in a Settings response) never contains a secret.
    """

    def __init__(self, provider_id: str, display_name: str, base_url: str = "", is_local: bool = False,
                 api_key_env: str = "", model_allowlist: Optional[List[str]] = None, connect_timeout: float = 3.0):
        self.provider_id = provider_id
        self.display_name = display_name
        self.base_url = (base_url or "").rstrip("/")
        self.is_local = bool(is_local)
        self.api_key_env = api_key_env or ""
        self.model_allowlist = list(model_allowlist or [])
        self.connect_timeout = connect_timeout

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key_env:
            key = os.environ.get(self.api_key_env)
            if key:
                headers["Authorization"] = f"Bearer {key}"
        return headers

    def list_models(self) -> List[ModelInfo]:
        if not self.base_url:
            return []
        return [
            ModelInfo(
                provider_id=self.provider_id, model_id=model, display_name=model, is_local=self.is_local,
                supports_json_schema=True,
                notes="Explicitly allow-listed in Settings -- Falguna never auto-discovers or auto-enables an external model.",
            )
            for model in self.model_allowlist
        ]

    def health_check(self) -> ProviderHealth:
        if not self.base_url or not self.model_allowlist:
            return ProviderHealth(HealthState.UNSUPPORTED, "Not configured -- no base URL or allow-listed model set in Settings -> Models.")
        if self.api_key_env and not os.environ.get(self.api_key_env):
            return ProviderHealth(HealthState.AUTH_REQUIRED, f"Expected a credential in the {self.api_key_env} environment variable.")
        return ProviderHealth(HealthState.DEGRADED, "Configured but not yet verified -- reachability is confirmed on first real request.")

    def generate(self, model_id: str, payload: dict, timeout_seconds: int) -> dict:
        if self.model_allowlist and model_id not in self.model_allowlist:
            raise FalgunaModelError(
                ErrorCategory.MODEL_UNSUPPORTED, f"'{model_id}' is not on the allow-listed model list for {self.display_name}.",
                provider_id=self.provider_id,
            )
        body = {"model": model_id, "messages": payload["messages"], "response_format": payload.get("response_format")}
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(f"{self.base_url}/chat/completions", data=data, method="POST", headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
                decoded = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:2000] if hasattr(exc, "read") else str(exc)
            if exc.code in (401, 403):
                raise FalgunaModelError(
                    ErrorCategory.AUTH_REQUIRED, f"{self.display_name} rejected the request as unauthenticated.",
                    technical_detail=detail, provider_id=self.provider_id,
                ) from exc
            if exc.code == 429:
                raise FalgunaModelError(
                    ErrorCategory.RATE_LIMITED, f"{self.display_name} reported it is rate-limited or over quota.",
                    technical_detail=detail, provider_id=self.provider_id,
                ) from exc
            if exc.code == 404:
                raise FalgunaModelError(
                    ErrorCategory.MODEL_UNSUPPORTED, f"{self.display_name} does not recognize model '{model_id}'.",
                    technical_detail=detail, provider_id=self.provider_id,
                ) from exc
            raise FalgunaModelError(
                ErrorCategory.TRANSPORT_FAILURE, f"{self.display_name} returned an error.",
                technical_detail=detail, provider_id=self.provider_id,
            ) from exc
        except (socket.timeout, TimeoutError) as exc:
            raise FalgunaModelError(
                ErrorCategory.TIMEOUT, f"{self.display_name} did not respond in time.",
                technical_detail=str(exc), provider_id=self.provider_id,
            ) from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (socket.timeout, TimeoutError)):
                raise FalgunaModelError(
                    ErrorCategory.TIMEOUT, f"{self.display_name} did not respond in time.",
                    technical_detail=str(exc), provider_id=self.provider_id,
                ) from exc
            raise FalgunaModelError(
                ErrorCategory.PROVIDER_OFFLINE, f"{self.display_name} is not reachable.",
                technical_detail=str(exc), provider_id=self.provider_id,
            ) from exc
        except OSError as exc:
            raise FalgunaModelError(
                ErrorCategory.PROVIDER_OFFLINE, f"{self.display_name} is not reachable.",
                technical_detail=str(exc), provider_id=self.provider_id,
            ) from exc
        try:
            content = decoded["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise FalgunaModelError(
                ErrorCategory.TRANSPORT_FAILURE, f"{self.display_name} returned an unexpected response shape.",
                technical_detail=json.dumps(decoded)[:2000], provider_id=self.provider_id,
            ) from exc
        result = {
            "choices": [{"message": {"content": content}}],
            "usage": decoded.get("usage", {}),
            "_falguna_provider": self.provider_id,
            "_falguna_metadata": {
                "adapter": self.provider_id, "billing": "local-self-hosted-no-cash-cost" if self.is_local else "external-api-estimated",
                "cost_basis": "local-zero-marginal-cost" if self.is_local else "estimated-token-based",
                "routed_model": model_id,
            },
        }
        if self.is_local:
            result["_falguna_cost_usd"] = 0.0
        return result
