"""Concrete SearchProvider implementations.

Kept deliberately separate from falguna/research.py: research.py owns the
provider abstraction, persistence, and citation synthesis, and must never
depend on any one vendor or contain any network code itself. This file is
where a real, swappable backend lives instead -- today one keyless
HTML-search provider; tomorrow a paid web-search API, browser-based
retrieval, a provider-native search tool, or a self-hosted index could live
here (or in a sibling module) implementing the exact same SearchProvider
contract. Swapping the default only ever means changing the one import and
one line in falguna/web.py -- nothing else in the app knows or cares which
provider is wired in.

Every provider here only performs a plain HTTP GET and parses the response
as inert text (regex extraction over HTML, no script execution, no
headless browser, no cookies, no credentials). A network or parsing
failure degrades to zero sources -- exactly like NullSearchProvider -- and
is never raised up into the caller as a hard error; downstream code (see
ResearchResponder.reply and falguna.web._run_research) already treats
"zero sources" as a normal, honest outcome rather than a failure.
"""
import html
import re
import urllib.parse
import urllib.request
from typing import List

from .research import ProviderResult, SearchProvider, SourceResult


class DuckDuckGoHTMLSearchProvider:
    """Keyless web search over DuckDuckGo's plain HTML results endpoint.
    No account, no API key, no browser required -- one GET request, parsed
    as plain text. This is Falguna Search's out-of-the-box default so
    Search does real work without any setup; it is not baked into the
    abstraction (falguna/research.py never imports or knows about this
    class) and can be replaced by any other SearchProvider from
    falguna/web.py alone.
    """

    name = "duckduckgo-html"
    ENDPOINT = "https://html.duckduckgo.com/html/"
    _RESULT_RE = re.compile(
        r'<a[^>]+class="result__a"[^>]+href="(?P<url>[^"]+)"[^>]*>(?P<title>.*?)</a>.*?'
        r'class="result__snippet"[^>]*>(?P<snippet>.*?)</a>',
        re.DOTALL,
    )
    _TAG_RE = re.compile(r"<[^>]+>")

    def __init__(self, timeout_seconds: int = 8):
        self.timeout_seconds = timeout_seconds

    @classmethod
    def _clean(cls, fragment: str) -> str:
        return html.unescape(cls._TAG_RE.sub("", fragment)).strip()

    def search(self, query: str, max_results: int = 6) -> ProviderResult:
        query = (query or "").strip()
        if not query:
            return ProviderResult(sources=[], provider_name=self.name)
        data = urllib.parse.urlencode({"q": query}).encode()
        request = urllib.request.Request(
            self.ENDPOINT, data=data,
            headers={"User-Agent": "Mozilla/5.0 (compatible; FalgunaResearch/1.0)"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read(1_000_000).decode("utf-8", errors="replace")
        except Exception:
            # Network failure, DNS failure, timeout, non-200, rate-limit --
            # this is Falguna's OWN network/transport failing, never the
            # query's fault, so it's marked `degraded` (Phase 2 Milestone
            # 4). A raised exception here would still take down the
            # caller's request, which this must never do -- degrade to
            # zero sources, just an honestly-labeled zero this time.
            return ProviderResult(sources=[], provider_name=self.name, degraded=True)
        sources: List[SourceResult] = []
        for match in self._RESULT_RE.finditer(body):
            url = html.unescape(match.group("url"))
            if not url.lower().startswith(("http://", "https://")):
                continue  # never accept a non-http(s) result, even from a "trusted" endpoint
            sources.append(SourceResult(
                url=url,
                title=self._clean(match.group("title"))[:200],
                snippet=self._clean(match.group("snippet"))[:600],
            ))
            if len(sources) >= max_results:
                break
        # A 200 response that yields zero regex matches is NOT the same
        # confidence as a network failure -- it's either a genuinely
        # unmatched query, or DuckDuckGo's markup changed under this
        # provider's regex. FallbackSearchProvider is what turns "zero
        # matches from provider A" into a real second attempt with an
        # independently-implemented provider B, rather than trusting this
        # single regex's silence.
        return ProviderResult(sources=sources, provider_name=self.name)


class DuckDuckGoLiteHTMLSearchProvider:
    """A second, independent keyless web-search backend: DuckDuckGo's own
    "lite" endpoint, a plain-table HTML page structurally unrelated to the
    JS-rendered-looking markup `html.duckduckgo.com` serves (different
    host, different template, parsed with an entirely separate regex).
    Phase 2 Milestone 4: this exists so a change to ONE of DuckDuckGo's two
    HTML surfaces doesn't take down Falguna Search entirely -- Falguna
    still has no paid/keyed search provider (none was authorized for this
    phase), so real resilience here means an independently-implemented
    second free path, not a fabricated one wired to fail identically for
    the same reason as the first.
    """

    name = "duckduckgo-lite-html"
    ENDPOINT = "https://lite.duckduckgo.com/lite/"
    # lite.duckduckgo.com renders each result as a link with class
    # 'result-link' (single-quoted attributes -- confirmed against the real
    # live endpoint, not assumed; this markup is a genuinely different DOM
    # shape and quoting style from html.duckduckgo.com's double-quoted
    # result__a/result__snippet anchor pair, so the two providers do not
    # share a single point of markup failure) followed by a 'result-snippet'
    # cell in a later row of the same result block.
    _RESULT_RE = re.compile(
        r"<a[^>]+href=\"(?P<url>[^\"]+)\"[^>]*class='result-link'[^>]*>(?P<title>.*?)</a>.*?"
        r"class='result-snippet'[^>]*>(?P<snippet>.*?)</td>",
        re.DOTALL,
    )
    _TAG_RE = re.compile(r"<[^>]+>")

    def __init__(self, timeout_seconds: int = 8):
        self.timeout_seconds = timeout_seconds

    @classmethod
    def _clean(cls, fragment: str) -> str:
        return html.unescape(cls._TAG_RE.sub("", fragment)).strip()

    def search(self, query: str, max_results: int = 6) -> ProviderResult:
        query = (query or "").strip()
        if not query:
            return ProviderResult(sources=[], provider_name=self.name)
        data = urllib.parse.urlencode({"q": query}).encode()
        request = urllib.request.Request(
            self.ENDPOINT, data=data,
            headers={"User-Agent": "Mozilla/5.0 (compatible; FalgunaResearch/1.0)"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read(1_000_000).decode("utf-8", errors="replace")
        except Exception:
            return ProviderResult(sources=[], provider_name=self.name, degraded=True)
        sources: List[SourceResult] = []
        for match in self._RESULT_RE.finditer(body):
            url = html.unescape(match.group("url"))
            if not url.lower().startswith(("http://", "https://")):
                continue
            sources.append(SourceResult(
                url=url,
                title=self._clean(match.group("title"))[:200],
                snippet=self._clean(match.group("snippet"))[:600],
            ))
            if len(sources) >= max_results:
                break
        return ProviderResult(sources=sources, provider_name=self.name)


class FallbackSearchProvider:
    """Phase 2 Milestone 4: wraps an ordered list of SearchProviders and
    tries each in turn, returning the first one that actually finds any
    sources. This is the "second, more resilient search path" the Phase 2
    inspection called for (falguna's own README of gaps: "the only concrete
    provider... will silently degrade to zero sources if DuckDuckGo changes
    markup -- no retry/fallback provider exists").

    Behavior:
      * The first provider to return 1+ sources wins outright -- its result
        is returned as-is (with `providers_tried` recording every provider
        that was actually attempted, including ones tried before it).
      * If every provider returns zero sources, the result is zero sources
        with `degraded=True` if *any* attempted provider reported its own
        `degraded=True` (a real failure, not just an empty match), or
        `degraded=False` if every provider cleanly agreed on zero results
        (multiple independent implementations agreeing is real evidence the
        query genuinely has no matches, not that scraping broke).
      * A provider that raises (contrary to its contract) is caught and
        treated as a degraded zero-result attempt rather than taking down
        the whole search -- one misbehaving provider must never break the
        others already in the chain.
    """

    name = "fallback"

    def __init__(self, providers: List[SearchProvider]):
        if not providers:
            raise ValueError("FallbackSearchProvider needs at least one provider")
        self.providers = list(providers)

    def search(self, query: str, max_results: int = 6) -> ProviderResult:
        tried: List[str] = []
        any_degraded = False
        for provider in self.providers:
            tried.append(provider.name)
            try:
                result = provider.search(query, max_results=max_results)
            except Exception:
                any_degraded = True
                continue
            if result.degraded:
                any_degraded = True
            if result.sources:
                return ProviderResult(
                    sources=result.sources, provider_name=result.provider_name,
                    degraded=False, providers_tried=list(tried),
                )
        return ProviderResult(sources=[], provider_name=self.name, degraded=any_degraded, providers_tried=tried)
