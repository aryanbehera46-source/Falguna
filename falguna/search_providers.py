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

from .research import ProviderResult, SourceResult


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
            # Network failure, DNS failure, timeout, non-200, rate-limit,
            # or any parsing surprise all degrade the same way: no sources,
            # never a raised exception that could take down a request.
            return ProviderResult(sources=[], provider_name=self.name)
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
        return ProviderResult(sources=sources, provider_name=self.name)
