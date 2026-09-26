"""Phase 2 Milestone 1: bounded retry/backoff for
GenericOpenAICompatibleProvider, and real on-demand capability metadata
(OllamaProvider.describe_model / context_window_from_show).

These are provider-level unit tests -- no real network dependency. The
retry tests monkeypatch only `urllib.request.urlopen` (the one seam that
would otherwise make a real HTTP call), and pass an explicit `sleep_fn`
so retry backoff never actually sleeps during the test run.
"""
import io
import json
import socket
import unittest
import urllib.error
from unittest.mock import patch

from falguna.providers import ErrorCategory, FalgunaModelError, GenericOpenAICompatibleProvider, OllamaProvider


class _FakeHTTPResponse:
    """Minimal stand-in for the object `urllib.request.urlopen` returns on
    success -- just enough to satisfy `with urlopen(...) as resp: resp.read()`."""

    def __init__(self, body: dict):
        self._body = json.dumps(body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def _http_error(code: int, body: bytes = b"{}"):
    return urllib.error.HTTPError(
        url="https://api.example.com/v1/chat/completions", code=code, msg="error",
        hdrs=None, fp=io.BytesIO(body),
    )


def _success_body():
    return {"choices": [{"message": {"content": "hello"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}


class GenericOpenAICompatibleRetryTests(unittest.TestCase):
    """Bounded retry for genuinely transient failures only -- a 401/403/404
    or an unexpected response shape must still fail on the very first
    attempt, exactly as before this change; only 429, 5xx, and a real
    timeout are ever retried, and only up to max_retries times."""

    def _provider(self, max_retries=2, sleep_calls=None):
        sleep_calls = sleep_calls if sleep_calls is not None else []
        return GenericOpenAICompatibleProvider(
            "custom", "Custom Vendor", base_url="https://api.example.com/v1", is_local=False,
            model_allowlist=["m1"], max_retries=max_retries, retry_backoff_base=0.5,
            sleep_fn=lambda seconds: sleep_calls.append(seconds),
        ), sleep_calls

    def _payload(self):
        return {"messages": [{"role": "user", "content": "hi"}], "response_format": None}

    def test_retries_on_429_then_succeeds_and_reports_attempt_count(self):
        provider, sleeps = self._provider(max_retries=2)
        with patch("falguna.providers.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = [_http_error(429), _http_error(429), _FakeHTTPResponse(_success_body())]
            result = provider.generate("m1", self._payload(), timeout_seconds=5)
        self.assertEqual(result["choices"][0]["message"]["content"], "hello")
        self.assertEqual(result["_falguna_metadata"]["retry_attempts"], 2)
        self.assertEqual(mock_urlopen.call_count, 3)
        self.assertEqual(sleeps, [0.5, 1.0])  # exponential backoff, base 0.5s

    def test_exhausts_retries_on_persistent_429_and_raises_rate_limited(self):
        provider, sleeps = self._provider(max_retries=1)
        with patch("falguna.providers.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = [_http_error(429), _http_error(429)]
            with self.assertRaises(FalgunaModelError) as ctx:
                provider.generate("m1", self._payload(), timeout_seconds=5)
        self.assertEqual(ctx.exception.category, ErrorCategory.RATE_LIMITED)
        self.assertEqual(mock_urlopen.call_count, 2)  # bounded: 1 + max_retries(1), never unbounded
        self.assertEqual(len(sleeps), 1)

    def test_retry_exhausted_message_states_how_many_attempts_were_made(self):
        # Phase 2 Milestone 4: chat's failure-explanation UI shows this
        # message verbatim -- before this, a retry-exhausted failure and a
        # first-try failure read identically, so a person had no way to
        # tell "Falguna already tried this twice" from "gave up instantly"
        # without opening Technical details.
        provider, _ = self._provider(max_retries=2)
        with patch("falguna.providers.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = [_http_error(429), _http_error(429), _http_error(429)]
            with self.assertRaises(FalgunaModelError) as ctx:
                provider.generate("m1", self._payload(), timeout_seconds=5)
        self.assertIn("after 3 attempts", ctx.exception.message)

    def test_first_try_failure_message_never_mentions_attempts(self):
        # A 401 never retries at all (attempt stays 0) -- the message must
        # not claim a retry count that never happened.
        provider, _ = self._provider(max_retries=2)
        with patch("falguna.providers.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = [_http_error(401)]
            with self.assertRaises(FalgunaModelError) as ctx:
                provider.generate("m1", self._payload(), timeout_seconds=5)
        self.assertNotIn("attempt", ctx.exception.message)

    def test_timeout_exhausted_message_states_how_many_attempts_were_made(self):
        provider, _ = self._provider(max_retries=1)
        with patch("falguna.providers.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = [socket.timeout("timed out"), socket.timeout("timed out")]
            with self.assertRaises(FalgunaModelError) as ctx:
                provider.generate("m1", self._payload(), timeout_seconds=5)
        self.assertEqual(ctx.exception.category, ErrorCategory.TIMEOUT)
        self.assertIn("after 2 attempts", ctx.exception.message)

    def test_retries_on_503_then_succeeds(self):
        provider, sleeps = self._provider(max_retries=2)
        with patch("falguna.providers.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = [_http_error(503), _FakeHTTPResponse(_success_body())]
            result = provider.generate("m1", self._payload(), timeout_seconds=5)
        self.assertEqual(result["_falguna_metadata"]["retry_attempts"], 1)

    def test_retries_on_real_timeout_then_succeeds(self):
        provider, sleeps = self._provider(max_retries=2)
        with patch("falguna.providers.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = [socket.timeout("timed out"), _FakeHTTPResponse(_success_body())]
            result = provider.generate("m1", self._payload(), timeout_seconds=5)
        self.assertEqual(result["_falguna_metadata"]["retry_attempts"], 1)
        self.assertEqual(len(sleeps), 1)

    def test_never_retries_a_401_fails_on_first_attempt(self):
        provider, sleeps = self._provider(max_retries=2)
        with patch("falguna.providers.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = [_http_error(401)]
            with self.assertRaises(FalgunaModelError) as ctx:
                provider.generate("m1", self._payload(), timeout_seconds=5)
        self.assertEqual(ctx.exception.category, ErrorCategory.AUTH_REQUIRED)
        self.assertEqual(mock_urlopen.call_count, 1)
        self.assertEqual(sleeps, [])  # never slept -- this is not a transient failure

    def test_never_retries_a_404_fails_on_first_attempt(self):
        provider, sleeps = self._provider(max_retries=2)
        with patch("falguna.providers.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = [_http_error(404)]
            with self.assertRaises(FalgunaModelError) as ctx:
                provider.generate("m1", self._payload(), timeout_seconds=5)
        self.assertEqual(ctx.exception.category, ErrorCategory.MODEL_UNSUPPORTED)
        self.assertEqual(mock_urlopen.call_count, 1)
        self.assertEqual(sleeps, [])

    def test_never_retries_a_connection_refused_reports_provider_offline_immediately(self):
        provider, sleeps = self._provider(max_retries=2)
        with patch("falguna.providers.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = [urllib.error.URLError(ConnectionRefusedError("refused"))]
            with self.assertRaises(FalgunaModelError) as ctx:
                provider.generate("m1", self._payload(), timeout_seconds=5)
        self.assertEqual(ctx.exception.category, ErrorCategory.PROVIDER_OFFLINE)
        self.assertEqual(mock_urlopen.call_count, 1)  # a down server is not worth retrying within one call
        self.assertEqual(sleeps, [])

    def test_max_retries_zero_disables_retry_entirely(self):
        provider, sleeps = self._provider(max_retries=0)
        with patch("falguna.providers.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = [_http_error(429)]
            with self.assertRaises(FalgunaModelError) as ctx:
                provider.generate("m1", self._payload(), timeout_seconds=5)
        self.assertEqual(ctx.exception.category, ErrorCategory.RATE_LIMITED)
        self.assertEqual(mock_urlopen.call_count, 1)
        self.assertEqual(sleeps, [])


class OllamaDescribeModelTests(unittest.TestCase):
    """describe_model()/context_window_from_show(): real, on-demand
    capability metadata via Ollama's own /api/show -- never guessed, never
    called from list_models()/health_check(), never raises."""

    def test_describe_model_returns_none_when_daemon_unreachable(self):
        provider = OllamaProvider(base_url="http://127.0.0.1:1", connect_timeout=0.3)
        self.assertIsNone(provider.describe_model("whatever"))

    def test_describe_model_returns_the_real_show_body_when_reachable(self):
        provider = OllamaProvider(base_url="http://127.0.0.1:1")
        provider._post = lambda path, body, timeout: (200, {"model_info": {"qwen2.context_length": 32768}})
        body = provider.describe_model("qwen2.5:1.5b-instruct")
        self.assertEqual(body["model_info"]["qwen2.context_length"], 32768)

    def test_context_window_from_show_extracts_family_prefixed_key(self):
        self.assertEqual(
            OllamaProvider.context_window_from_show({"model_info": {"qwen2.context_length": 32768}}), 32768,
        )
        self.assertEqual(
            OllamaProvider.context_window_from_show({"model_info": {"llama.context_length": 8192, "llama.other": 1}}), 8192,
        )

    def test_context_window_from_show_returns_none_not_zero_when_absent(self):
        self.assertIsNone(OllamaProvider.context_window_from_show({"model_info": {}}))
        self.assertIsNone(OllamaProvider.context_window_from_show({}))
        self.assertIsNone(OllamaProvider.context_window_from_show(None))

    def test_context_window_from_show_ignores_non_integer_values_rather_than_crashing(self):
        self.assertIsNone(OllamaProvider.context_window_from_show({"model_info": {"qwen2.context_length": "not-a-number"}}))


if __name__ == "__main__":
    unittest.main()
