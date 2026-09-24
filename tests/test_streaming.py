"""Tests for Falguna Local AI Independence V1.1's real token-streaming
pass: OllamaProvider.generate_stream (falguna/providers.py),
ModelRouter.stream (falguna/model_router.py), and
ChatResponder.reply_stream (falguna/chat.py). Complements
tests/test_json_stream.py (the incremental JSON-string extractor these
all build on) and the live end-to-end streaming/cancellation coverage in
tests/test_falguna_web_v2.py.
"""
import json
import threading
import unittest
import unittest.mock

import falguna.providers as providers_module
from falguna.chat import CHAT_REPLY_SCHEMA, ChatError, ChatResponder
from falguna.gateway import OpenAICompatibleGateway
from falguna.model_router import ModelRouter
from falguna.providers import ErrorCategory, FalgunaModelError, ModelInfo, ModelProvider, OllamaProvider, ProviderHealth, HealthState


class _FakeStreamResponse:
    """Stands in for the object urllib.request.urlopen() returns: supports
    the context-manager protocol and line-by-line iteration of raw bytes,
    exactly like a real http.client.HTTPResponse streaming NDJSON."""

    def __init__(self, chunks):
        self._lines = [(json.dumps(c) + "\n").encode("utf-8") for c in chunks]
        self._iter = iter(self._lines)
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._iter)

    def close(self):
        self.closed = True


def _ndjson_chunks_for(reply_text: str, suggested_objective=None, chunk_size: int = 3):
    """Splits a full, valid CHAT_REPLY_SCHEMA JSON payload into small,
    arbitrary, non-token-aligned NDJSON delta chunks -- the same shape
    real Ollama streaming produces (verified against a live daemon; see
    tests/test_json_stream.py's captured-sequence test)."""
    full = json.dumps({"reply": reply_text, "suggested_objective": suggested_objective})
    pieces = [full[i:i + chunk_size] for i in range(0, len(full), chunk_size)]
    chunks = [{"message": {"content": p}, "done": False} for p in pieces]
    chunks.append({"message": {"content": ""}, "done": True, "prompt_eval_count": 11, "eval_count": 4})
    return chunks


class OllamaGenerateStreamTests(unittest.TestCase):
    def _payload(self):
        return {"messages": [], "response_format": {"json_schema": {"schema": CHAT_REPLY_SCHEMA}}}

    def test_delivers_every_real_delta_and_returns_the_full_accumulated_result(self):
        provider = OllamaProvider(base_url="http://127.0.0.1:1")
        fake_resp = _FakeStreamResponse(_ndjson_chunks_for("Hello there!"))
        deltas = []
        with unittest.mock.patch.object(providers_module.urllib.request, "urlopen", return_value=fake_resp):
            result = provider.generate_stream("qwen2.5:1.5b-instruct", self._payload(), timeout_seconds=5, on_delta=deltas.append)
        self.assertTrue(deltas)  # real incremental deltas actually fired, not zero/one fabricated call
        self.assertGreater(len(deltas), 1)
        # generate_stream's on_delta fires RAW JSON-syntax fragments (this
        # layer streams the wire bytes verbatim; decoding "reply" out of
        # them is ChatResponder.reply_stream's job, tested separately
        # below) -- so the reassembled text is the full JSON object, and
        # its "reply" field is what must match.
        reassembled = json.loads("".join(deltas))
        self.assertEqual(reassembled["reply"], "Hello there!")
        content = json.loads(result["choices"][0]["message"]["content"])
        self.assertEqual(content["reply"], "Hello there!")
        self.assertEqual(result["usage"]["completion_tokens"], 4)
        self.assertEqual(result["_falguna_metadata"]["streamed"], True)
        self.assertTrue(fake_resp.closed)

    def test_cancel_event_set_mid_stream_closes_the_connection_and_raises_cleanly(self):
        provider = OllamaProvider(base_url="http://127.0.0.1:1")
        # A long fake stream -- if cancellation didn't really interrupt the
        # read loop, every one of these lines would be consumed.
        fake_resp = _FakeStreamResponse(_ndjson_chunks_for("A" * 500))
        cancel_event = threading.Event()
        seen = []

        def on_delta(text):
            seen.append(text)
            if len(seen) == 3:
                cancel_event.set()  # simulate Stop being clicked mid-generation

        with unittest.mock.patch.object(providers_module.urllib.request, "urlopen", return_value=fake_resp):
            with self.assertRaises(FalgunaModelError) as ctx:
                provider.generate_stream("qwen2.5:1.5b-instruct", self._payload(), timeout_seconds=5, on_delta=on_delta, cancel_event=cancel_event)
        # The real connection was closed rather than drained to completion --
        # this is what proves cancellation reached the underlying stream,
        # not just that the caller stopped listening to it.
        self.assertTrue(fake_resp.closed)
        self.assertLess(len(seen), len(fake_resp._lines))
        self.assertEqual(ctx.exception.category, ErrorCategory.TRANSPORT_FAILURE)
        self.assertNotIn("Errno", ctx.exception.message)

    def test_a_broken_delta_callback_never_aborts_real_generation(self):
        provider = OllamaProvider(base_url="http://127.0.0.1:1")
        fake_resp = _FakeStreamResponse(_ndjson_chunks_for("Fine either way."))

        def flaky_on_delta(text):
            raise RuntimeError("a UI-side rendering bug")

        with unittest.mock.patch.object(providers_module.urllib.request, "urlopen", return_value=fake_resp):
            result = provider.generate_stream("qwen2.5:1.5b-instruct", self._payload(), timeout_seconds=5, on_delta=flaky_on_delta)
        content = json.loads(result["choices"][0]["message"]["content"])
        self.assertEqual(content["reply"], "Fine either way.")

    def test_a_malformed_ndjson_line_is_skipped_not_a_crash(self):
        provider = OllamaProvider(base_url="http://127.0.0.1:1")

        class _MixedResponse(_FakeStreamResponse):
            def __init__(self):
                super().__init__([])
                good = _ndjson_chunks_for("Still works.")
                lines = [b"not valid json at all\n"]
                lines += [(json.dumps(c) + "\n").encode("utf-8") for c in good]
                self._lines = lines
                self._iter = iter(self._lines)

        fake_resp = _MixedResponse()
        with unittest.mock.patch.object(providers_module.urllib.request, "urlopen", return_value=fake_resp):
            result = provider.generate_stream("qwen2.5:1.5b-instruct", self._payload(), timeout_seconds=5, on_delta=lambda t: None)
        content = json.loads(result["choices"][0]["message"]["content"])
        self.assertEqual(content["reply"], "Still works.")


class _StreamingFakeProvider(ModelProvider):
    """A ModelProvider that DOES implement generate_stream, for exercising
    ModelRouter.stream's dispatch without a real Ollama daemon."""

    def __init__(self, provider_id="fake-stream", is_local=True):
        self.provider_id = provider_id
        self.display_name = "Fake Streaming Provider"
        self.is_local = is_local
        self.generate_stream_calls = 0

    def list_models(self):
        return [ModelInfo(self.provider_id, "fake-model", "Fake Model", self.is_local)]

    def health_check(self):
        return ProviderHealth(HealthState.HEALTHY, "fake")

    def generate(self, model_id, payload, timeout_seconds):
        raise AssertionError("non-streaming generate() should not be called when generate_stream exists")

    def generate_stream(self, model_id, payload, timeout_seconds, on_delta, cancel_event=None):
        self.generate_stream_calls += 1
        for piece in ['{"reply": "', "Streamed", " reply", '"', ', "suggested_objective": null}']:
            on_delta(piece)
        return {
            "choices": [{"message": {"content": '{"reply": "Streamed reply", "suggested_objective": null}'}}],
            "usage": {}, "_falguna_provider": self.provider_id, "_falguna_cost_usd": 0.0, "_falguna_metadata": {},
        }


class _NonStreamingFakeProvider(ModelProvider):
    """A ModelProvider with no generate_stream at all -- the non-streaming
    fallback path ModelRouter.stream/ChatResponder.reply_stream must both
    still honestly deliver the real, complete reply exactly once."""

    def __init__(self, provider_id="fake-nonstream", is_local=False):
        self.provider_id = provider_id
        self.display_name = "Fake Non-Streaming Provider"
        self.is_local = is_local

    def list_models(self):
        return [ModelInfo(self.provider_id, "fake-model", "Fake Model", self.is_local)]

    def health_check(self):
        return ProviderHealth(HealthState.HEALTHY, "fake")

    def generate(self, model_id, payload, timeout_seconds):
        return {
            "choices": [{"message": {"content": json.dumps({"reply": "Complete non-streamed reply", "suggested_objective": None})}}],
            "usage": {}, "_falguna_provider": self.provider_id, "_falguna_cost_usd": 0.0, "_falguna_metadata": {},
        }


class ModelRouterStreamTests(unittest.TestCase):
    def test_stream_dispatches_to_generate_stream_when_the_provider_supports_it(self):
        provider = _StreamingFakeProvider()
        router = ModelRouter([provider], privacy_mode="HYBRID")
        deltas = []
        result = router.stream({"model": None}, {"messages": []}, timeout_seconds=5, on_delta=deltas.append)
        self.assertEqual(provider.generate_stream_calls, 1)
        self.assertTrue(deltas)
        self.assertEqual(result["_falguna_metadata"]["routed_provider"], provider.provider_id)

    def test_stream_falls_back_to_plain_generate_when_the_provider_has_no_streaming_support(self):
        provider = _NonStreamingFakeProvider()
        router = ModelRouter([provider], privacy_mode="HYBRID")
        deltas = []
        result = router.stream({"model": None}, {"messages": []}, timeout_seconds=5, on_delta=deltas.append)
        content = json.loads(result["choices"][0]["message"]["content"])
        self.assertEqual(content["reply"], "Complete non-streamed reply")
        # This router layer itself never invents a fallback delta -- that
        # is ChatResponder.reply_stream's job (JSON-schema aware); prove
        # this layer stays generic by asserting no delta was synthesized.
        self.assertEqual(deltas, [])

    def test_supports_streaming_reports_the_real_resolved_providers_capability(self):
        streaming = ModelRouter([_StreamingFakeProvider()], privacy_mode="HYBRID")
        self.assertTrue(streaming.supports_streaming(None))
        non_streaming = ModelRouter([_NonStreamingFakeProvider(is_local=True)], privacy_mode="HYBRID")
        self.assertFalse(non_streaming.supports_streaming(None))

    def test_supports_streaming_never_raises_for_an_unroutable_selector(self):
        router = ModelRouter([], privacy_mode="HYBRID")
        self.assertFalse(router.supports_streaming(None))


class ChatResponderStreamTests(unittest.TestCase):
    def test_reply_stream_delivers_only_the_decoded_reply_text_never_raw_json(self):
        provider = _StreamingFakeProvider()
        router = ModelRouter([provider], privacy_mode="HYBRID")
        gateway = OpenAICompatibleGateway("fake-model", "http://127.0.0.1:1/v1", "")
        responder = ChatResponder(gateway, router, "fake-model")
        deltas = []
        outcome = responder.reply_stream([{"role": "user", "content": "hi"}], deltas.append)
        self.assertEqual("".join(deltas), "Streamed reply")
        for d in deltas:
            self.assertNotIn("{", d)
            self.assertNotIn('"reply"', d)
        self.assertEqual(outcome["reply"], "Streamed reply")

    def test_reply_stream_falls_back_to_one_complete_delta_for_a_bare_callable_transport(self):
        def bare_transport(config, payload, timeout_seconds):
            return {
                "choices": [{"message": {"content": json.dumps({"reply": "From a bare callable", "suggested_objective": None})}}],
                "usage": {}, "_falguna_provider": "bare", "_falguna_cost_usd": 0.0, "_falguna_metadata": {},
            }
        gateway = OpenAICompatibleGateway("x", "http://127.0.0.1:1/v1", "")
        responder = ChatResponder(gateway, bare_transport, "x")
        deltas = []
        outcome = responder.reply_stream([{"role": "user", "content": "hi"}], deltas.append)
        self.assertEqual(deltas, ["From a bare callable"])  # exactly one real, complete delta
        self.assertEqual(outcome["reply"], "From a bare callable")

    def test_reply_stream_falls_back_to_one_complete_delta_when_the_router_has_no_streaming_provider(self):
        provider = _NonStreamingFakeProvider()
        router = ModelRouter([provider], privacy_mode="HYBRID")
        gateway = OpenAICompatibleGateway("fake-model", "http://127.0.0.1:1/v1", "")
        responder = ChatResponder(gateway, router, "fake-model")
        deltas = []
        outcome = responder.reply_stream([{"role": "user", "content": "hi"}], deltas.append)
        self.assertEqual(deltas, ["Complete non-streamed reply"])
        self.assertEqual(outcome["reply"], "Complete non-streamed reply")

    def test_reply_stream_preserves_chaterror_translation_on_provider_failure(self):
        class _FailingProvider(ModelProvider):
            provider_id = "failing"
            display_name = "Failing"
            is_local = True

            def list_models(self):
                return [ModelInfo(self.provider_id, "m", "m", True)]

            def health_check(self):
                return ProviderHealth(HealthState.HEALTHY, "fake")

            def generate(self, model_id, payload, timeout_seconds):
                raise FalgunaModelError(ErrorCategory.RATE_LIMITED, "Busy right now.")

            def generate_stream(self, model_id, payload, timeout_seconds, on_delta, cancel_event=None):
                raise FalgunaModelError(ErrorCategory.RATE_LIMITED, "Busy right now.")

        router = ModelRouter([_FailingProvider()], privacy_mode="HYBRID")
        gateway = OpenAICompatibleGateway("failing/m", "http://127.0.0.1:1/v1", "")
        responder = ChatResponder(gateway, router, "failing/m")
        with self.assertRaises(ChatError) as ctx:
            responder.reply_stream([{"role": "user", "content": "hi"}], lambda t: None)
        self.assertEqual(ctx.exception.category, ErrorCategory.RATE_LIMITED)


if __name__ == "__main__":
    unittest.main()
