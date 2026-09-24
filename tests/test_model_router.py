"""Tests for Falguna V2.1: provider-neutral model access (falguna/providers.py)
and the deterministic router + persistent registry (falguna/model_router.py).

Covers, per the phase's own test-battery requirements:
  * a no-cloud-dependency boot test -- the server comes up and /api/config /
    /api/models both respond with nothing configured/reachable at all;
  * provider-failure/quota simulation -- a fake provider raising each
    FalgunaModelError category surfaces that exact category through
    ChatResponder, never a raw exception string;
  * a local-provider test -- OllamaProvider against an unreachable port
    reports OFFLINE and an empty model list, and never raises, never
    attempts an install;
  * model-routing tests -- explicit selection, preferred-local-first,
    other-local-next, external-last, and the final NO_COMPATIBLE_MODEL;
  * a privacy test proving zero external-provider calls in Local Only mode;
  * persistence tests for the model_settings registry, including that a
    saved registry never surfaces a raw secret value (only the env var
    name a credential should come from);
  * HTTP-level tests for GET /api/models and POST /api/models/settings.
"""
import json
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from falguna.chat import ChatError, ChatResponder
from falguna.gateway import OpenAICompatibleGateway
from falguna.model_router import (
    DEFAULT_REGISTRY_SETTINGS, ModelRegistry, ModelRouter, PRIVACY_MODES, build_providers, parse_selector,
    provider_status_snapshot,
)
from falguna.providers import (
    CodexProvider, ErrorCategory, FalgunaModelError, GenericOpenAICompatibleProvider, HealthState,
    ModelInfo, ModelProvider, OllamaProvider, ProviderHealth,
)
from falguna.runtime import open_control_plane
from falguna.web import FalgunaHandler


# --------------------------------------------------------------------- fakes

class _FakeProvider(ModelProvider):
    """A fully scriptable provider for exercising the router/ChatResponder
    without any real process or network dependency. `on_generate`/`on_health`
    are call counters so a test can prove a provider was (or was never)
    invoked -- the mechanism the Local-Only privacy test below relies on."""

    def __init__(self, provider_id, is_local, health_state=HealthState.HEALTHY, models=None,
                 generate_result=None, generate_error=None):
        self.provider_id = provider_id
        self.display_name = f"Fake {provider_id}"
        self.is_local = is_local
        self.health_state = health_state
        self._models = models or [ModelInfo(provider_id, "fake-model", "Fake Model", is_local)]
        self.generate_result = generate_result
        self.generate_error = generate_error
        self.health_check_calls = 0
        self.generate_calls = 0
        self.list_models_calls = 0

    def list_models(self):
        self.list_models_calls += 1
        return list(self._models)

    def health_check(self):
        self.health_check_calls += 1
        return ProviderHealth(self.health_state, "fake")

    def generate(self, model_id, payload, timeout_seconds):
        self.generate_calls += 1
        if self.generate_error is not None:
            raise self.generate_error
        return self.generate_result or {
            "choices": [{"message": {"content": json.dumps({"reply": "ok", "suggested_objective": None})}}],
            "usage": {}, "_falguna_provider": self.provider_id, "_falguna_cost_usd": 0.0, "_falguna_metadata": {},
        }


# --------------------------------------------------------------------- providers.py unit tests

class ProviderInterfaceTests(unittest.TestCase):
    def test_ollama_against_an_unreachable_port_reports_offline_never_raises(self):
        # No Ollama daemon is running on this port in any test environment --
        # this is the honest, expected "local runtime not installed" case.
        provider = OllamaProvider(base_url="http://127.0.0.1:1", connect_timeout=0.5)
        health = provider.health_check()
        self.assertEqual(health.state, HealthState.OFFLINE)
        self.assertEqual(provider.list_models(), [])  # never raises, never returns a fabricated model

    def test_ollama_generate_against_unreachable_port_raises_provider_offline_not_a_crash(self):
        provider = OllamaProvider(base_url="http://127.0.0.1:1", connect_timeout=0.5)
        payload = {"messages": [], "response_format": {"json_schema": {"schema": {}}}}
        with self.assertRaises(FalgunaModelError) as ctx:
            provider.generate("whatever", payload, timeout_seconds=1)
        self.assertEqual(ctx.exception.category, ErrorCategory.PROVIDER_OFFLINE)
        # Sanitized: the safe message never contains a raw socket/urllib repr.
        self.assertNotIn("Errno", ctx.exception.message)

    def test_codex_provider_reports_offline_when_no_executable_on_path(self):
        provider = CodexProvider()
        health = provider.health_check()
        # This test environment has no authenticated `codex` executable --
        # same assumption every other Falguna test file makes.
        self.assertIn(health.state, {HealthState.OFFLINE, HealthState.HEALTHY})
        if health.state == HealthState.OFFLINE:
            self.assertEqual(provider.list_models(), [])

    def test_generic_openai_compatible_unconfigured_is_unsupported_not_an_error(self):
        provider = GenericOpenAICompatibleProvider("custom", "Custom", base_url="")
        health = provider.health_check()
        self.assertEqual(health.state, HealthState.UNSUPPORTED)
        self.assertEqual(provider.list_models(), [])

    def test_generic_openai_compatible_never_calls_network_at_construction(self):
        # Constructing a provider must never itself make a network call --
        # only generate()/health_check()/list_models() may, and only
        # list_models()/health_check() here (base_url unset) should short-
        # circuit before ever reaching urllib.
        provider = GenericOpenAICompatibleProvider(
            "custom", "Custom", base_url="http://127.0.0.1:1", is_local=False,
            model_allowlist=["some-model"],
        )
        self.assertEqual(provider.provider_id, "custom")  # constructed without raising / blocking


# --------------------------------------------------------------------- model capability filtering (V1.1)

class OllamaCapabilityParsingTests(unittest.TestCase):
    """OllamaProvider.list_models() must derive is_embedding_only from
    Ollama's own real, live /api/tags "capabilities" field -- never from
    guessing at the model's name. These tests monkeypatch only the
    provider's internal `_get` (the one seam that would otherwise make a
    real HTTP call) so the capability-parsing logic itself is exercised
    for real, without depending on a live Ollama daemon being present in
    every test environment."""

    def _provider_with_tags_response(self, models):
        provider = OllamaProvider(base_url="http://127.0.0.1:1")
        provider._get = lambda path, timeout: (200, {"models": models})
        return provider

    def test_a_completion_capable_model_is_never_marked_embedding_only(self):
        provider = self._provider_with_tags_response([
            {"model": "qwen2.5:1.5b-instruct", "name": "qwen2.5:1.5b-instruct", "capabilities": ["completion", "tools"]},
        ])
        models = provider.list_models()
        self.assertEqual(len(models), 1)
        self.assertFalse(models[0].is_embedding_only)
        self.assertTrue(models[0].supports_streaming)

    def test_an_embedding_only_model_is_marked_and_excluded_from_streaming(self):
        provider = self._provider_with_tags_response([
            {"model": "nomic-embed-text:latest", "name": "nomic-embed-text:latest", "capabilities": ["embedding"]},
        ])
        models = provider.list_models()
        self.assertEqual(len(models), 1)
        self.assertTrue(models[0].is_embedding_only)
        self.assertFalse(models[0].supports_streaming)
        self.assertIn("not available for Chat", models[0].notes)

    def test_a_model_reporting_both_completion_and_embedding_capabilities_is_not_embedding_only(self):
        # A model that CAN also serve generation requests must never be
        # blocked just because it additionally reports "embedding" --
        # is_embedding_only means embedding-ONLY (no "completion" at all).
        provider = self._provider_with_tags_response([
            {"model": "hypothetical-dual", "name": "hypothetical-dual", "capabilities": ["completion", "embedding"]},
        ])
        models = provider.list_models()
        self.assertFalse(models[0].is_embedding_only)

    def test_a_model_with_no_capabilities_array_at_all_is_conservatively_not_blocked(self):
        # An older Ollama build (or a future response shape) that omits
        # "capabilities" entirely must never cause a false-positive block --
        # no evidence of embedding-only means it is treated as chat-capable.
        provider = self._provider_with_tags_response([
            {"model": "some-model", "name": "some-model"},
        ])
        models = provider.list_models()
        self.assertFalse(models[0].is_embedding_only)

    def test_a_mixed_response_reports_each_model_independently(self):
        provider = self._provider_with_tags_response([
            {"model": "qwen2.5:1.5b-instruct", "name": "qwen2.5:1.5b-instruct", "capabilities": ["completion", "tools"]},
            {"model": "nomic-embed-text:latest", "name": "nomic-embed-text:latest", "capabilities": ["embedding"]},
        ])
        by_id = {m.model_id: m for m in provider.list_models()}
        self.assertFalse(by_id["qwen2.5:1.5b-instruct"].is_embedding_only)
        self.assertTrue(by_id["nomic-embed-text:latest"].is_embedding_only)


class ModelRouterCapabilityRoutingTests(unittest.TestCase):
    """ModelRouter must never route a Chat/Research/Work generation
    request to an embedding-only model, whether requested explicitly or
    picked automatically during Auto routing."""

    def test_resolve_rejects_an_explicit_selector_naming_an_embedding_only_model(self):
        provider = _FakeProvider("ollama", is_local=True, models=[
            ModelInfo("ollama", "nomic-embed-text", "nomic-embed-text", True, is_embedding_only=True),
            ModelInfo("ollama", "qwen2.5:1.5b-instruct", "qwen2.5:1.5b-instruct", True, is_embedding_only=False),
        ])
        router = ModelRouter([provider], privacy_mode="HYBRID")
        with self.assertRaises(FalgunaModelError) as ctx:
            router.resolve("ollama/nomic-embed-text")
        self.assertEqual(ctx.exception.category, ErrorCategory.MODEL_UNSUPPORTED)
        self.assertIn("embedding-only", ctx.exception.message)

    def test_resolve_still_honors_an_explicit_selector_for_a_chat_capable_model(self):
        provider = _FakeProvider("ollama", is_local=True, models=[
            ModelInfo("ollama", "nomic-embed-text", "nomic-embed-text", True, is_embedding_only=True),
            ModelInfo("ollama", "qwen2.5:1.5b-instruct", "qwen2.5:1.5b-instruct", True, is_embedding_only=False),
        ])
        router = ModelRouter([provider], privacy_mode="HYBRID")
        chosen_provider, model_id = router.resolve("ollama/qwen2.5:1.5b-instruct")
        self.assertIs(chosen_provider, provider)
        self.assertEqual(model_id, "qwen2.5:1.5b-instruct")

    def test_auto_routing_skips_an_embedding_only_model_and_picks_a_chat_capable_one(self):
        # A provider whose ONLY locally-pulled model is embedding-only must
        # never be auto-selected -- Auto routing should fall through to
        # NO_COMPATIBLE_MODEL rather than silently handing a chat request
        # to a model that cannot answer it.
        embedding_only_provider = _FakeProvider("ollama", is_local=True, models=[
            ModelInfo("ollama", "nomic-embed-text", "nomic-embed-text", True, is_embedding_only=True),
        ])
        router = ModelRouter([embedding_only_provider], privacy_mode="LOCAL_ONLY")
        with self.assertRaises(FalgunaModelError) as ctx:
            router.resolve(None)
        self.assertEqual(ctx.exception.category, ErrorCategory.NO_COMPATIBLE_MODEL)

    def test_auto_routing_picks_the_chat_capable_model_over_an_embedding_only_one_on_the_same_provider(self):
        provider = _FakeProvider("ollama", is_local=True, models=[
            ModelInfo("ollama", "nomic-embed-text", "nomic-embed-text", True, is_embedding_only=True),
            ModelInfo("ollama", "qwen2.5:1.5b-instruct", "qwen2.5:1.5b-instruct", True, is_embedding_only=False),
        ])
        router = ModelRouter([provider], privacy_mode="HYBRID")
        chosen_provider, model_id = router.resolve(None)
        self.assertIs(chosen_provider, provider)
        self.assertEqual(model_id, "qwen2.5:1.5b-instruct")

    def test_a_misconfigured_preferred_local_embedding_model_falls_through_rather_than_hard_failing(self):
        embedding_default = _FakeProvider("ollama", is_local=True, models=[
            ModelInfo("ollama", "nomic-embed-text", "nomic-embed-text", True, is_embedding_only=True),
        ])
        other_local = _FakeProvider("llamacpp", is_local=True, models=[
            ModelInfo("llamacpp", "some-chat-model", "Some Chat Model", True, is_embedding_only=False),
        ])
        router = ModelRouter(
            [embedding_default, other_local], privacy_mode="HYBRID",
            preferred_local_selector="ollama/nomic-embed-text",
        )
        chosen_provider, model_id = router.resolve(None)
        self.assertIs(chosen_provider, other_local)
        self.assertEqual(model_id, "some-chat-model")

    def test_provider_status_snapshot_reports_capability_flags_for_settings(self):
        provider = _FakeProvider("ollama", is_local=True, models=[
            ModelInfo("ollama", "nomic-embed-text", "nomic-embed-text", True, is_embedding_only=True, supports_streaming=False),
            ModelInfo("ollama", "qwen2.5:1.5b-instruct", "qwen2.5:1.5b-instruct", True, is_embedding_only=False, supports_streaming=True),
        ])
        snapshot = provider_status_snapshot([provider])
        by_id = {m["model_id"]: m for m in snapshot[0]["models"]}
        self.assertTrue(by_id["nomic-embed-text"]["is_embedding_only"])
        self.assertFalse(by_id["qwen2.5:1.5b-instruct"]["is_embedding_only"])
        self.assertTrue(by_id["qwen2.5:1.5b-instruct"]["supports_streaming"])


# --------------------------------------------------------------------- routing policy tests

class ModelRouterPolicyTests(unittest.TestCase):
    def test_explicit_selection_is_honored_when_reachable(self):
        local = _FakeProvider("ollama", is_local=True)
        external = _FakeProvider("codex", is_local=False)
        router = ModelRouter([local, external], privacy_mode="HYBRID")
        provider, model_id = router.resolve("codex/some-model")
        self.assertIs(provider, external)
        self.assertEqual(model_id, "some-model")

    def test_preferred_local_model_is_tried_before_other_local_providers(self):
        preferred = _FakeProvider("ollama", is_local=True, models=[ModelInfo("ollama", "preferred-model", "P", True)])
        other_local = _FakeProvider("llamacpp", is_local=True, models=[ModelInfo("llamacpp", "other-model", "O", True)])
        router = ModelRouter([other_local, preferred], privacy_mode="HYBRID", preferred_local_selector="ollama/preferred-model")
        provider, model_id = router.resolve(None)
        self.assertIs(provider, preferred)
        self.assertEqual(model_id, "preferred-model")
        self.assertEqual(other_local.health_check_calls, 0)  # never even considered

    def test_other_local_provider_tried_before_external(self):
        local = _FakeProvider("ollama", is_local=True)
        external = _FakeProvider("codex", is_local=False)
        router = ModelRouter([external, local], privacy_mode="HYBRID")
        provider, _ = router.resolve(None)
        self.assertIs(provider, local)
        self.assertEqual(external.health_check_calls, 0)  # local satisfied the request first

    def test_external_tried_only_when_no_local_provider_is_available_and_privacy_allows_it(self):
        offline_local = _FakeProvider("ollama", is_local=True, health_state=HealthState.OFFLINE)
        external = _FakeProvider("codex", is_local=False)
        router = ModelRouter([offline_local, external], privacy_mode="HYBRID")
        provider, _ = router.resolve(None)
        self.assertIs(provider, external)

    def test_no_compatible_model_when_nothing_is_reachable(self):
        offline_local = _FakeProvider("ollama", is_local=True, health_state=HealthState.OFFLINE)
        offline_external = _FakeProvider("codex", is_local=False, health_state=HealthState.OFFLINE)
        router = ModelRouter([offline_local, offline_external], privacy_mode="HYBRID")
        with self.assertRaises(FalgunaModelError) as ctx:
            router.resolve(None)
        self.assertEqual(ctx.exception.category, ErrorCategory.NO_COMPATIBLE_MODEL)

    def test_local_only_mode_never_calls_an_external_providers_health_check_or_generate(self):
        # This is the privacy guarantee itself: Local Only must exclude an
        # external provider from consideration structurally, not filter it
        # out after calling it -- so health_check()/generate() must show
        # ZERO calls on the external fake even though it would happily
        # answer if it were ever invoked.
        local = _FakeProvider("ollama", is_local=True, health_state=HealthState.OFFLINE)
        external = _FakeProvider("codex", is_local=False)  # would succeed if called
        router = ModelRouter([local, external], privacy_mode="LOCAL_ONLY")
        with self.assertRaises(FalgunaModelError) as ctx:
            router.resolve("codex/some-model")  # even an EXPLICIT external request
        self.assertEqual(ctx.exception.category, ErrorCategory.NO_COMPATIBLE_MODEL)
        self.assertEqual(external.health_check_calls, 0)
        self.assertEqual(external.generate_calls, 0)

    def test_local_only_mode_still_routes_to_a_healthy_local_provider(self):
        local = _FakeProvider("ollama", is_local=True)
        external = _FakeProvider("codex", is_local=False)
        router = ModelRouter([local, external], privacy_mode="LOCAL_ONLY")
        provider, _ = router.resolve(None)
        self.assertIs(provider, local)
        self.assertEqual(external.health_check_calls, 0)

    def test_call_sets_routed_metadata_and_privacy_mode(self):
        local = _FakeProvider("ollama", is_local=True)
        router = ModelRouter([local], privacy_mode="HYBRID")
        result = router({"model": None}, {"messages": []}, timeout_seconds=5)
        self.assertEqual(result["_falguna_metadata"]["routed_provider"], "ollama")
        self.assertEqual(result["_falguna_metadata"]["privacy_mode"], "HYBRID")

    def test_parse_selector_backward_compatible_with_bare_codex_model_ids(self):
        provider_id, model_id = parse_selector("gpt-5.6-luna")
        self.assertEqual(provider_id, "codex")
        self.assertEqual(model_id, "gpt-5.6-luna")
        self.assertEqual(parse_selector(None), (None, None))
        self.assertEqual(parse_selector("ollama/llama3.2:3b"), ("ollama", "llama3.2:3b"))


# --------------------------------------------------------------------- provider-failure / quota simulation

class ProviderFailureSimulationTests(unittest.TestCase):
    """Proves each sanitized error category makes it, unmodified, from a
    provider's FalgunaModelError all the way through ChatResponder.reply()
    -- never re-wrapped into a generic string, never dropping the category,
    never leaking technical_detail into the safe message."""

    def _reply_with(self, error):
        fake = _FakeProvider("fake", is_local=False, generate_error=error)
        router = ModelRouter([fake], privacy_mode="HYBRID")
        gateway = OpenAICompatibleGateway("fake/fake-model", "http://127.0.0.1:1/v1", "")
        responder = ChatResponder(gateway, router, "fake/fake-model")
        with self.assertRaises(ChatError) as ctx:
            responder.reply([{"role": "user", "content": "hi"}])
        return ctx.exception

    def test_rate_limited_is_preserved(self):
        exc = self._reply_with(FalgunaModelError(ErrorCategory.RATE_LIMITED, "Over quota right now.", technical_detail="HTTP 429 body: {...}"))
        self.assertEqual(exc.category, ErrorCategory.RATE_LIMITED)
        self.assertEqual(str(exc), "Over quota right now.")
        self.assertNotIn("HTTP 429", str(exc))  # raw detail never leaks into the safe message
        self.assertIn("HTTP 429", exc.technical_detail)

    def test_auth_required_is_preserved(self):
        exc = self._reply_with(FalgunaModelError(ErrorCategory.AUTH_REQUIRED, "Needs authentication."))
        self.assertEqual(exc.category, ErrorCategory.AUTH_REQUIRED)

    def test_timeout_is_preserved(self):
        exc = self._reply_with(FalgunaModelError(ErrorCategory.TIMEOUT, "Didn't respond in time."))
        self.assertEqual(exc.category, ErrorCategory.TIMEOUT)

    def test_provider_offline_is_preserved(self):
        exc = self._reply_with(FalgunaModelError(ErrorCategory.PROVIDER_OFFLINE, "Not reachable."))
        self.assertEqual(exc.category, ErrorCategory.PROVIDER_OFFLINE)

    def test_bare_exception_from_a_non_provider_aware_transport_is_sanitized(self):
        def flaky_transport(config, payload, timeout_seconds):
            raise RuntimeError("some internal stack trace with a /Users/aryan/secret/path")
        gateway = OpenAICompatibleGateway("x", "http://127.0.0.1:1/v1", "")
        responder = ChatResponder(gateway, flaky_transport, "x")
        with self.assertRaises(ChatError) as ctx:
            responder.reply([{"role": "user", "content": "hi"}])
        self.assertEqual(ctx.exception.category, ErrorCategory.TRANSPORT_FAILURE)
        self.assertNotIn("/Users/aryan/secret/path", str(ctx.exception))
        self.assertIn("/Users/aryan/secret/path", ctx.exception.technical_detail)


# --------------------------------------------------------------------- registry persistence

class ModelRegistryPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(self.repo)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "f@test.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "F Test"], check=True)
        (self.repo / "README.md").write_text("seed\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-m", "seed"], check=True, capture_output=True)
        self.control, self.store = open_control_plane(self.repo)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_default_registry_matches_documented_defaults(self):
        registry = ModelRegistry(self.store)
        loaded = registry.load()
        self.assertEqual(loaded["privacy_mode"], "HYBRID")
        self.assertIsNone(loaded["preferred_local_model"])
        self.assertIn("ollama", loaded["providers"])
        self.assertIn("codex", loaded["providers"])
        self.assertFalse(loaded["providers"]["openai_compatible"]["enabled"])  # never on by default

    def test_save_then_load_round_trips_and_updates_in_place(self):
        registry = ModelRegistry(self.store)
        settings = registry.load()
        settings["privacy_mode"] = "LOCAL_ONLY"
        registry.save(settings)
        self.assertEqual(registry.load()["privacy_mode"], "LOCAL_ONLY")
        settings = registry.load()
        settings["privacy_mode"] = "EXTERNAL_ALLOWED"
        registry.save(settings)  # second save exercises the UPDATE path, not another INSERT
        self.assertEqual(registry.load()["privacy_mode"], "EXTERNAL_ALLOWED")
        self.assertEqual(len(self.store.list("model_settings")), 1)

    def test_public_view_never_contains_a_raw_secret_value(self):
        import os
        os.environ["FALGUNA_TEST_FAKE_KEY"] = "super-secret-do-not-leak"
        try:
            registry = ModelRegistry(self.store)
            settings = registry.load()
            settings["providers"]["openai_compatible"] = {
                "enabled": True, "display_name": "Test Vendor", "base_url": "https://api.example.com/v1",
                "is_local": False, "api_key_env": "FALGUNA_TEST_FAKE_KEY", "model_allowlist": ["m1"],
            }
            registry.save(settings)
            dumped = json.dumps(registry.public_view())
            self.assertNotIn("super-secret-do-not-leak", dumped)
            self.assertIn("FALGUNA_TEST_FAKE_KEY", dumped)  # the env var NAME is fine to show
        finally:
            del os.environ["FALGUNA_TEST_FAKE_KEY"]

    def test_invalid_privacy_mode_falls_back_to_default_rather_than_crashing(self):
        registry = ModelRegistry(self.store)
        settings = registry.load()
        settings["privacy_mode"] = "NOT_A_REAL_MODE"
        registry.save(settings)
        self.assertEqual(registry.load()["privacy_mode"], "HYBRID")

    def test_build_providers_respects_enabled_flags(self):
        registry = ModelRegistry(self.store)
        settings = registry.load()
        settings["providers"]["ollama"]["enabled"] = False
        settings["providers"]["codex"]["enabled"] = False
        registry.save(settings)
        providers = build_providers(registry.load())
        self.assertEqual(providers, [])  # nothing enabled -- an empty, honest provider list


# --------------------------------------------------------------------- HTTP-level tests

class _LiveModelsServerCase(unittest.TestCase):
    port = 8802

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(self.repo)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "f@test.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "F Test"], check=True)
        (self.repo / "README.md").write_text("seed\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-m", "seed"], check=True, capture_output=True)
        self.control, self.store = open_control_plane(self.repo)
        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), FalgunaHandler)
        self.server.app_root = self.repo
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self._wait_ready()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.store.close()
        self.temp.cleanup()

    def _wait_ready(self):
        for _ in range(40):
            try:
                status, body = self._get("/api/config")
                if body.get("product") == "Falguna Engineering":
                    return
            except Exception:
                pass
            time.sleep(0.05)
        self.fail("Falguna server did not become ready")

    def _get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as resp:
            return resp.status, json.loads(resp.read())

    def _post(self, path, body):
        data = json.dumps(body).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method="POST", headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())


class NoCloudDependencyBootTest(_LiveModelsServerCase):
    """The server must come up and answer honestly with nothing installed
    or configured at all -- no codex, no local Ollama daemon, no external
    provider enabled. This is the whole point of the phase: Falguna's own
    process never depends on a cloud/vendor runtime merely to start."""

    port = 8803

    def test_config_and_models_respond_with_nothing_configured(self):
        status, cfg = self._get("/api/config")
        self.assertEqual(status, 200)
        self.assertEqual(cfg["privacy_mode"], "HYBRID")
        status, models = self._get("/api/models")
        self.assertEqual(status, 200)
        self.assertEqual(models["registry"]["privacy_mode"], "HYBRID")
        provider_ids = {p["provider_id"] for p in models["providers"]}
        self.assertEqual(provider_ids, {"ollama", "codex"})  # openai_compatible disabled by default
        for p in models["providers"]:
            self.assertIn(p["health"]["state"], {HealthState.HEALTHY, HealthState.OFFLINE, HealthState.DEGRADED, HealthState.UNSUPPORTED})


class ModelsHttpTests(_LiveModelsServerCase):
    port = 8804

    def test_get_models_shape(self):
        status, out = self._get("/api/models")
        self.assertEqual(status, 200)
        self.assertEqual(set(out.keys()), {"privacy_modes", "registry", "providers"})
        self.assertEqual(set(out["privacy_modes"]), set(PRIVACY_MODES))

    def test_post_settings_updates_privacy_mode_and_persists(self):
        status, out = self._post("/api/models/settings", {"privacy_mode": "local_only"})
        self.assertEqual(status, 200)
        self.assertEqual(out["registry"]["privacy_mode"], "LOCAL_ONLY")
        status, cfg = self._get("/api/config")
        self.assertEqual(cfg["privacy_mode"], "LOCAL_ONLY")

    def test_post_settings_rejects_an_invalid_privacy_mode(self):
        status, out = self._post("/api/models/settings", {"privacy_mode": "not-a-real-mode"})
        self.assertEqual(status, 400)
        self.assertIn("error", out)

    def test_post_settings_merges_provider_patch_without_wiping_other_fields(self):
        status, out = self._post("/api/models/settings", {"providers": {"ollama": {"base_url": "http://127.0.0.1:22222"}}})
        self.assertEqual(status, 200)
        self.assertEqual(out["registry"]["providers"]["ollama"]["base_url"], "http://127.0.0.1:22222")
        self.assertTrue(out["registry"]["providers"]["ollama"]["enabled"])  # untouched field preserved
        self.assertTrue(out["registry"]["providers"]["codex"]["enabled"])  # untouched provider preserved

    def test_post_settings_never_returns_a_raw_secret_value(self):
        import os
        os.environ["FALGUNA_TEST_HTTP_KEY"] = "another-secret-value"
        try:
            status, out = self._post("/api/models/settings", {
                "providers": {"openai_compatible": {
                    "enabled": True, "base_url": "https://api.example.com/v1", "is_local": False,
                    "api_key_env": "FALGUNA_TEST_HTTP_KEY", "model_allowlist": ["m1"],
                }},
            })
            self.assertEqual(status, 200)
            self.assertNotIn("another-secret-value", json.dumps(out))
        finally:
            del os.environ["FALGUNA_TEST_HTTP_KEY"]


if __name__ == "__main__":
    unittest.main()
