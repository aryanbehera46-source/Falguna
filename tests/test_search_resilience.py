"""Phase 2 Milestone 4: search provider resilience.

Covers the two things the Phase 2 inspection flagged as a real gap in
falguna/search_providers.py: DuckDuckGoHTMLSearchProvider was the only
concrete provider and any failure (network, timeout, or DuckDuckGo
changing its HTML) silently degraded to a plain empty result -- identical
to a query that genuinely has no matches. This suite proves:
  * a network/transport failure is now distinguishable (`degraded=True`)
    from a clean zero-match response (`degraded=False`);
  * a second, independently-implemented keyless provider
    (DuckDuckGoLiteHTMLSearchProvider) exists and is exercised through
    FallbackSearchProvider, which is the actual "second, more resilient
    search path" wired into falguna.web.SEARCH_PROVIDER;
  * ResearchResponder.reply() surfaces an honestly different message when
    the zero-source result was a provider failure versus a clean miss.

No real network call is made anywhere in this file -- every provider's
`urllib.request.urlopen` is monkeypatched, exactly like the existing
tests/test_providers.py pattern for the model-provider retry logic.
"""
import io
import unittest
import urllib.error
from unittest.mock import patch

from falguna.research import ProviderResult, ResearchResponder, SourceResult, NO_SOURCES_ANSWER, NO_SOURCES_DEGRADED_ANSWER
from falguna.search_providers import (
    DuckDuckGoHTMLSearchProvider,
    DuckDuckGoLiteHTMLSearchProvider,
    FallbackSearchProvider,
)


class _FakeHTTPResponse:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, *_args):
        return self._body


PRIMARY_RESULT_HTML = (
    '<a class="result__a" href="https://real-primary.example.com/page">Primary Title</a>'
    '<a class="result__snippet">Primary snippet text</a>'
)

LITE_RESULT_HTML = (
    # Mirrors the REAL markup fetched live from lite.duckduckgo.com during
    # Phase 2 Milestone 4 development (mixed quoting: href double-quoted,
    # class single-quoted, class after href) -- an earlier version of this
    # fixture used double-quoted, class-before-href markup that matched an
    # earlier (wrong) version of the provider's regex but not the real
    # page; both were corrected together after live verification caught
    # the mismatch, exactly the kind of mocked-test false confidence the
    # Phase 2 mission warns about.
    '<table><tr><td>1.&nbsp;</td>'
    '<td><a rel="nofollow" href="https://real-lite.example.com/page" class=\'result-link\'>Lite Title</a></td></tr>'
    '<tr><td>&nbsp;</td><td class=\'result-snippet\'>Lite snippet text</td></tr></table>'
)


class DuckDuckGoHTMLSearchProviderResilienceTests(unittest.TestCase):
    """Phase 2 Milestone 4: distinguishing a real transport failure from a
    clean empty result -- the existing behavior (never raising, always
    degrading to zero sources) is unchanged; only the new `degraded` flag
    is added."""

    def test_network_failure_returns_zero_sources_marked_degraded(self):
        provider = DuckDuckGoHTMLSearchProvider(timeout_seconds=1)
        with patch("falguna.search_providers.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urllib.error.URLError(ConnectionRefusedError("refused"))
            result = provider.search("anything")
        self.assertEqual(result.sources, [])
        self.assertTrue(result.degraded)
        self.assertEqual(result.provider_name, "duckduckgo-html")

    def test_clean_response_with_no_matches_is_not_marked_degraded(self):
        provider = DuckDuckGoHTMLSearchProvider(timeout_seconds=1)
        with patch("falguna.search_providers.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _FakeHTTPResponse(b"<html><body>no results markup here</body></html>")
            result = provider.search("a query with genuinely nothing")
        self.assertEqual(result.sources, [])
        self.assertFalse(result.degraded)

    def test_successful_parse_is_never_marked_degraded(self):
        provider = DuckDuckGoHTMLSearchProvider(timeout_seconds=1)
        with patch("falguna.search_providers.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _FakeHTTPResponse(PRIMARY_RESULT_HTML.encode())
            result = provider.search("real query")
        self.assertEqual(len(result.sources), 1)
        self.assertEqual(result.sources[0].url, "https://real-primary.example.com/page")
        self.assertFalse(result.degraded)

    def test_empty_query_returns_zero_sources_without_any_network_call(self):
        provider = DuckDuckGoHTMLSearchProvider(timeout_seconds=1)
        with patch("falguna.search_providers.urllib.request.urlopen") as mock_urlopen:
            result = provider.search("   ")
        mock_urlopen.assert_not_called()
        self.assertEqual(result.sources, [])
        self.assertFalse(result.degraded)


class DuckDuckGoLiteHTMLSearchProviderTests(unittest.TestCase):
    """The second, independent keyless provider -- a materially different
    endpoint and regex from the primary, so the two do not share a single
    point of markup failure."""

    def test_parses_the_lite_endpoint_markup_shape(self):
        provider = DuckDuckGoLiteHTMLSearchProvider(timeout_seconds=1)
        with patch("falguna.search_providers.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _FakeHTTPResponse(LITE_RESULT_HTML.encode())
            result = provider.search("real query")
        self.assertEqual(len(result.sources), 1)
        self.assertEqual(result.sources[0].url, "https://real-lite.example.com/page")
        self.assertEqual(result.sources[0].title, "Lite Title")
        self.assertFalse(result.degraded)

    def test_network_failure_is_marked_degraded(self):
        provider = DuckDuckGoLiteHTMLSearchProvider(timeout_seconds=1)
        with patch("falguna.search_providers.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = TimeoutError("timed out")
            result = provider.search("anything")
        self.assertEqual(result.sources, [])
        self.assertTrue(result.degraded)
        self.assertEqual(result.provider_name, "duckduckgo-lite-html")

    def test_rejects_a_non_http_url_even_if_matched(self):
        provider = DuckDuckGoLiteHTMLSearchProvider(timeout_seconds=1)
        malicious = LITE_RESULT_HTML.replace("https://real-lite.example.com/page", "javascript:alert(1)")
        with patch("falguna.search_providers.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _FakeHTTPResponse(malicious.encode())
            result = provider.search("q")
        self.assertEqual(result.sources, [])


class _StubProvider:
    """A minimal SearchProvider stub for FallbackSearchProvider tests --
    returns a pre-built ProviderResult (or raises) so the fallback chain's
    own logic can be tested in isolation from any real parsing."""

    def __init__(self, name, result=None, raises=None):
        self.name = name
        self._result = result
        self._raises = raises
        self.calls = 0

    def search(self, query, max_results=6):
        self.calls += 1
        if self._raises:
            raise self._raises
        return self._result


class FallbackSearchProviderTests(unittest.TestCase):
    """Phase 2 Milestone 4: the actual "second, more resilient search
    path" -- an ordered chain of independent providers, honestly reporting
    which ones were tried and whether the eventual zero-result outcome was
    a clean miss or a real failure."""

    def test_first_provider_success_short_circuits_and_is_returned_as_is(self):
        first = _StubProvider("first", ProviderResult(sources=[SourceResult(url="https://a.example.com")], provider_name="first"))
        second = _StubProvider("second", ProviderResult(sources=[SourceResult(url="https://b.example.com")], provider_name="second"))
        fb = FallbackSearchProvider([first, second])
        result = fb.search("q")
        self.assertEqual(result.provider_name, "first")
        self.assertEqual([s.url for s in result.sources], ["https://a.example.com"])
        self.assertEqual(result.providers_tried, ["first"])
        self.assertEqual(second.calls, 0)  # never tried -- first already succeeded
        self.assertFalse(result.degraded)

    def test_first_provider_degraded_falls_through_to_second_which_succeeds(self):
        first = _StubProvider("first", ProviderResult(sources=[], provider_name="first", degraded=True))
        second = _StubProvider("second", ProviderResult(sources=[SourceResult(url="https://b.example.com")], provider_name="second"))
        fb = FallbackSearchProvider([first, second])
        result = fb.search("q")
        self.assertEqual(result.provider_name, "second")
        self.assertEqual(result.providers_tried, ["first", "second"])
        self.assertFalse(result.degraded)  # the chain as a whole succeeded

    def test_all_providers_degraded_reports_degraded_true(self):
        first = _StubProvider("first", ProviderResult(sources=[], provider_name="first", degraded=True))
        second = _StubProvider("second", ProviderResult(sources=[], provider_name="second", degraded=True))
        fb = FallbackSearchProvider([first, second])
        result = fb.search("q")
        self.assertEqual(result.sources, [])
        self.assertTrue(result.degraded)
        self.assertEqual(result.providers_tried, ["first", "second"])

    def test_all_providers_cleanly_empty_reports_degraded_false(self):
        # Two independent implementations both cleanly finding nothing is
        # real evidence the query has no matches -- never reported as a
        # failure.
        first = _StubProvider("first", ProviderResult(sources=[], provider_name="first", degraded=False))
        second = _StubProvider("second", ProviderResult(sources=[], provider_name="second", degraded=False))
        fb = FallbackSearchProvider([first, second])
        result = fb.search("q")
        self.assertEqual(result.sources, [])
        self.assertFalse(result.degraded)

    def test_a_provider_that_raises_is_caught_and_treated_as_degraded_never_crashes_the_chain(self):
        first = _StubProvider("first", raises=RuntimeError("boom"))
        second = _StubProvider("second", ProviderResult(sources=[SourceResult(url="https://b.example.com")], provider_name="second"))
        fb = FallbackSearchProvider([first, second])
        result = fb.search("q")  # must not raise
        self.assertEqual(result.provider_name, "second")
        self.assertEqual(result.providers_tried, ["first", "second"])

    def test_requires_at_least_one_provider(self):
        with self.assertRaises(ValueError):
            FallbackSearchProvider([])


class ResearchResponderDegradedMessageTests(unittest.TestCase):
    """Phase 2 Milestone 4: ResearchResponder.reply()'s zero-sources
    branch now honestly distinguishes a provider failure from a clean
    miss, but every pre-existing call site (passing no `provider_result`)
    keeps producing the exact original NO_SOURCES_ANSWER string."""

    def _responder(self):
        # Never actually called in these tests (zero sources short-circuits
        # before any transport call), so None stand-ins are safe.
        return ResearchResponder(gateway=None, transport=None, model="does-not-matter")

    def test_zero_sources_without_provider_result_uses_the_original_message(self):
        outcome = self._responder().reply("q", [])
        self.assertEqual(outcome["answer"], NO_SOURCES_ANSWER)
        self.assertIsNone(outcome["model_call"])

    def test_zero_sources_with_a_clean_non_degraded_provider_result_uses_the_original_message(self):
        pr = ProviderResult(sources=[], provider_name="fallback", degraded=False, providers_tried=["a", "b"])
        outcome = self._responder().reply("q", [], provider_result=pr)
        self.assertEqual(outcome["answer"], NO_SOURCES_ANSWER)

    def test_zero_sources_with_a_degraded_provider_result_uses_the_honest_failure_message(self):
        pr = ProviderResult(sources=[], provider_name="fallback", degraded=True, providers_tried=["a", "b"])
        outcome = self._responder().reply("q", [], provider_result=pr)
        self.assertEqual(outcome["answer"], NO_SOURCES_DEGRADED_ANSWER)
        self.assertNotEqual(NO_SOURCES_DEGRADED_ANSWER, NO_SOURCES_ANSWER)

    def test_non_empty_sources_are_unaffected_by_provider_result_degraded_flag(self):
        # degraded is only ever consulted on the zero-sources path -- a
        # successful fallback (degraded=False, but sources present from a
        # not-first provider) must still go through normal synthesis, not
        # this test's concern beyond confirming it doesn't short-circuit.
        pr = ProviderResult(sources=[SourceResult(url="https://x.example.com")], provider_name="second", degraded=False)
        with self.assertRaises(Exception):
            # gateway=None means calling .configuration() blows up -- proof
            # this path actually attempted synthesis instead of taking the
            # zero-sources shortcut.
            self._responder().reply("q", [SourceResult(url="https://x.example.com")], provider_result=pr)


if __name__ == "__main__":
    unittest.main()
