"""Opportunity Agent v1: automatic opportunity acquisition for Revenue
Hunter, built inside TTT HQ (falguna/hq_web.py serves its routes; nothing
here runs in Falguna Engineering).

Operating model, exactly as specified:
    AUTO-FIND -> AUTO-ANALYZE -> AUTO-DRAFT -> ARYAN APPROVES
A discovery run never sends a proposal, message, application, or email --
it only ever creates DRAFT rows and, for a small number of high-confidence
(PURSUE) opportunities, one Needs Aryan approval item. Everything else
(MAYBE/IGNORE) is qualified and kept in history, never surfaced as an
action item, and never deleted.

Design choices worth stating up front, mirroring revenue_hunter.py's own
opening docstring:

* Pluggable provider abstraction (OpportunitySource). Every concrete
  provider returns the same NormalizedOpportunity shape; the discovery
  engine, dedup, filtering, qualification and drafting code never know or
  care which website or feed a listing came from. Adding a new source
  later means writing one class with a `discover()` method -- nothing else
  in this module, in hq_web.py, or in the UI needs to change.
* Free-first, no logins, no scraping that fights a platform's own rules.
  RemotiveSource and WeWorkRemotelyRSSSource call each site's own public,
  keyless, documented feed (a JSON API and an RSS feed respectively, both
  explicitly published by their owners for exactly this kind of
  programmatic consumption) with one plain HTTP GET each, parsed as inert
  text/JSON/XML -- no browser, no cookies, no credentials, no JavaScript
  execution. Sources that would require a paid API application, an OAuth
  login, or working around anti-bot/CAPTCHA protection (Upwork,
  Freelancer.com, LinkedIn) are represented as honest, clearly-labeled
  unavailable placeholders (see UnavailableSource) -- they are listed in
  the provider registry so wiring in real credentials later is a one-line
  change, but they are never called, and a discovery run never claims to
  have searched them.
* A provider's own network/parsing failure never takes down a discovery
  run. DiscoveryEngine.run_now() calls each source in its own try/except
  and records {provider, started_at, completed_at, found, new,
  duplicates, error} for every one of them, succeeding or not -- exactly
  the same "isolate failure, keep going, stay auditable" pattern
  falguna/search_providers.py and falguna/research.py already use for web
  research.
* Deduplication uses three independent signals, in order: (1) the
  provider's own (source, external_id) pair, when the provider supplies
  one; (2) a canonicalized version of the listing's URL; (3) a content
  fingerprint (hash of normalized title + client + the first slice of the
  description). Re-running discovery against the same live feeds must
  never create duplicate opportunities -- this is checked before an
  opportunity is created, not cleaned up afterward.
* AI research enrichment reuses Falguna Search's existing, already-tested
  provider-abstracted infrastructure (search_providers.py + research.py)
  instead of duplicating it. It is best-effort and always optional: no
  client_name to look up, zero search results, no local model transport
  available, or any other failure all degrade to "skip research, continue
  with the opportunity's original data" -- never a hard error, never an
  invented fact. Every synthesized answer keeps its citations exactly as
  research.py already stores them.
* The acquisition profile (what we sell, target budgets/countries,
  keywords, which sources are enabled, ...) is a single persisted,
  editable settings row (rh_settings), not scattered hard-coded constants.
  It can be changed from the UI at any time and takes effect on the next
  discovery run.
"""

import hashlib
import json
import os
import re
import shutil
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol

from .audit import AuditLog
from .codex_transport import CodexCliJSONTransport, DEFAULT_CODEX_MODEL, ResilientCodexTransport
from .gateway import OpenAICompatibleGateway
from .research import ResearchResponder, ResearchStore, SourceResult, rank_sources
from .revenue_hunter import (
    DEFAULT_EXCLUSION_ROLE_SIGNALS, DEFAULT_POSITIVE_SERVICE_SIGNALS, OpportunityStore, ProposalStore,
    QualificationEngine, QualificationStore,
)
from .search_providers import DuckDuckGoHTMLSearchProvider
from .store import StateStore, utcnow

MODEL = DEFAULT_CODEX_MODEL

# The same keyless web-search provider Falguna Search already uses for
# research -- not a second, duplicate implementation.
_RESEARCH_SEARCH_PROVIDER = DuckDuckGoHTMLSearchProvider()

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    import html as _html
    return _html.unescape(_TAG_RE.sub(" ", text or "")).strip()


def _first_number(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    numbers = re.findall(r"[\d,]+(?:\.\d+)?", text)
    if not numbers:
        return None
    return float(numbers[0].replace(",", ""))


def _parse_date(value: Optional[str]) -> Optional[datetime]:
    """Best-effort parse of a posted-at value from either provider (RFC 2822
    for RSS pubDate, ISO 8601 for Remotive). Never raises -- an unparsable
    date just means age filtering is skipped for that one listing."""
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
    except (TypeError, ValueError):
        pass
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Provider abstraction
# ---------------------------------------------------------------------------

@dataclass
class NormalizedOpportunity:
    """The one shape every OpportunitySource must produce. Nothing
    downstream (dedup, filtering, qualification, the opportunity record
    itself) ever looks at a provider-specific field -- only this."""
    source: str
    external_id: str
    url: str
    title: str
    description: str = ""
    client_name: Optional[str] = None
    budget_text: Optional[str] = None
    currency: Optional[str] = None
    location: Optional[str] = None
    remote: Optional[bool] = None
    posted_at: Optional[str] = None
    skills: List[str] = field(default_factory=list)
    raw_metadata: Dict[str, Any] = field(default_factory=dict)


class OpportunitySource(Protocol):
    """Minimal provider contract, deliberately shaped like
    research.py's SearchProvider: any object with a matching `name`,
    `available`, `unavailable_reason`, and `discover()` can be plugged into
    DEFAULT_SOURCES or passed explicitly to DiscoveryEngine -- a future
    real Upwork/Freelancer connector, a different job board, a self-hosted
    feed, anything. `discover()` is allowed to raise on a real failure
    (network, parsing, rate limit); DiscoveryEngine isolates that failure
    per-provider and keeps going."""

    name: str
    available: bool
    unavailable_reason: Optional[str]

    def discover(self, profile: Dict[str, Any], limit: int) -> List[NormalizedOpportunity]:
        ...


class RemotiveSource:
    """Remotive's public JSON API (https://remotive.com/api/remote-jobs) --
    free, keyless, no login, explicitly published by Remotive for
    programmatic/public use. One plain HTTP GET; the response is parsed as
    JSON data only, nothing is executed."""

    name = "remotive"
    available = True
    unavailable_reason = None
    ENDPOINT = "https://remotive.com/api/remote-jobs"

    def __init__(self, timeout_seconds: int = 12):
        self.timeout_seconds = timeout_seconds

    def discover(self, profile: Dict[str, Any], limit: int = 25) -> List[NormalizedOpportunity]:
        keywords = profile.get("keywords") or []
        search = keywords[0] if keywords else ""
        params = {"category": "software-dev"}
        if search:
            params["search"] = search
        url = f"{self.ENDPOINT}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; FalgunaOpportunityAgent/1.0)"})
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            data = json.loads(response.read(3_000_000).decode("utf-8", errors="replace"))
        jobs = (data.get("jobs") or [])[:limit]
        out = []
        for job in jobs:
            out.append(NormalizedOpportunity(
                source=self.name,
                external_id=str(job.get("id") or job.get("url") or ""),
                url=job.get("url") or "",
                title=job.get("title") or "",
                description=_strip_html(job.get("description") or "")[:4000],
                client_name=job.get("company_name") or None,
                budget_text=job.get("salary") or None,
                currency=None,
                location=job.get("candidate_required_location") or None,
                remote=True,
                posted_at=job.get("publication_date"),
                skills=[str(t) for t in (job.get("tags") or [])],
                raw_metadata=job,
            ))
        return out


class WeWorkRemotelyRSSSource:
    """WeWorkRemotely's public "remote programming jobs" RSS feed -- free,
    keyless, no login, explicitly published for syndication. One plain
    HTTP GET; the response is parsed with the standard library's XML
    parser as inert data, never executed."""

    name = "weworkremotely"
    available = True
    unavailable_reason = None
    ENDPOINT = "https://weworkremotely.com/categories/remote-programming-jobs.rss"

    def __init__(self, timeout_seconds: int = 12):
        self.timeout_seconds = timeout_seconds

    def discover(self, profile: Dict[str, Any], limit: int = 25) -> List[NormalizedOpportunity]:
        import xml.etree.ElementTree as ET

        request = urllib.request.Request(self.ENDPOINT, headers={"User-Agent": "Mozilla/5.0 (compatible; FalgunaOpportunityAgent/1.0)"})
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            body = response.read(3_000_000)
        root = ET.fromstring(body)
        out = []
        for item in root.findall(".//item")[:limit]:
            link = (item.findtext("link") or "").strip()
            raw_title = (item.findtext("title") or "").strip()
            guid = (item.findtext("guid") or link or raw_title).strip()
            description = _strip_html(item.findtext("description") or "")[:4000]
            pubdate = item.findtext("pubDate")
            # WeWorkRemotely titles are conventionally "Company: Job Title".
            client_name, job_title = (None, raw_title)
            if ":" in raw_title:
                left, right = raw_title.split(":", 1)
                if left.strip() and right.strip():
                    client_name, job_title = left.strip(), right.strip()
            out.append(NormalizedOpportunity(
                source=self.name,
                external_id=guid,
                url=link,
                title=job_title or raw_title,
                description=description,
                client_name=client_name,
                budget_text=None,
                currency=None,
                location="Remote",
                remote=True,
                posted_at=pubdate,
                skills=[],
                raw_metadata={"title": raw_title, "link": link},
            ))
        return out


class UnavailableSource:
    """An honest placeholder for a source we deliberately do not connect to
    yet, because doing so would require a paid API application, an OAuth
    login, or working around anti-bot/CAPTCHA protection -- all explicitly
    out of scope. Listed in the registry (so it shows up in the UI and in
    every discovery run's provider report as "unavailable: <reason>") but
    `discover()` is never called by DiscoveryEngine while `available` is
    False; it exists only as a documented, safe fallback if it were ever
    called by mistake."""

    def __init__(self, name: str, reason: str):
        self.name = name
        self.available = False
        self.unavailable_reason = reason

    def discover(self, profile: Dict[str, Any], limit: int = 25) -> List[NormalizedOpportunity]:
        raise RuntimeError(self.unavailable_reason)


UPWORK_SOURCE = UnavailableSource(
    "upwork",
    "Requires an approved Upwork API application (paid/OAuth) -- not connected. "
    "No scraping or login bypass will be attempted.",
)
FREELANCER_SOURCE = UnavailableSource(
    "freelancer",
    "Requires Freelancer.com OAuth API credentials -- not connected. "
    "No scraping or login bypass will be attempted.",
)
LINKEDIN_SOURCE = UnavailableSource(
    "linkedin",
    "LinkedIn's terms prohibit automated scraping and job data requires an "
    "authenticated session -- not supported.",
)

DEFAULT_SOURCES: List[OpportunitySource] = [
    RemotiveSource(),
    WeWorkRemotelyRSSSource(),
    UPWORK_SOURCE,
    FREELANCER_SOURCE,
    LINKEDIN_SOURCE,
]


# ---------------------------------------------------------------------------
# Normalization -> dedup signals
# ---------------------------------------------------------------------------

def canonicalize_url(url: Optional[str]) -> str:
    """Strips scheme-insensitive `www.`, trailing slashes, and case
    differences so the same real listing linked two slightly different
    ways is still recognized as the same opportunity. Never used as the
    *only* dedup signal -- see DiscoveryEngine._find_duplicate."""
    if not url:
        return ""
    try:
        parsed = urllib.parse.urlparse(url.strip().lower())
    except ValueError:
        return url.strip().lower()
    netloc = parsed.netloc[4:] if parsed.netloc.startswith("www.") else parsed.netloc
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme}://{netloc}{path}" if netloc else ""


def content_fingerprint(title: Optional[str], client_name: Optional[str], description: Optional[str]) -> str:
    """A stable hash of normalized title + client + a slice of the
    description -- the last-resort dedup signal when a listing has neither
    a provider external_id nor (a matching) URL, e.g. the same role posted
    to two different feeds with different links."""
    basis = "|".join([
        re.sub(r"\s+", " ", (title or "").strip().lower()),
        re.sub(r"\s+", " ", (client_name or "").strip().lower()),
        re.sub(r"\s+", " ", (description or "")[:300].strip().lower()),
    ])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def _matches_profile(n: NormalizedOpportunity, profile: Dict[str, Any]) -> bool:
    """Central, provider-agnostic filtering against the acquisition
    profile. A provider is free to ignore query params it doesn't support
    (e.g. WeWorkRemotely's RSS feed has no server-side keyword search) --
    this is what actually enforces the profile regardless of provider
    capability."""
    text = f"{n.title} {n.description}".lower()
    excluded = [k.lower() for k in (profile.get("excluded_keywords") or []) if k]
    if any(k in text for k in excluded):
        return False
    keywords = [k.lower() for k in (profile.get("keywords") or []) if k]
    if keywords and not any(k in text for k in keywords):
        return False
    min_budget = profile.get("min_budget_usd")
    if min_budget:
        amount = _first_number(n.budget_text)
        if amount is not None and amount < float(min_budget):
            return False
    max_age = profile.get("max_age_days")
    if max_age and n.posted_at:
        posted = _parse_date(n.posted_at)
        if posted and (datetime.now(timezone.utc) - posted).days > int(max_age):
            return False
    remote_pref = profile.get("remote_preference")
    if remote_pref == "remote_only" and n.remote is False:
        return False
    target_countries = [c.lower() for c in (profile.get("target_countries") or []) if c]
    if target_countries and n.location:
        if not any(c in n.location.lower() for c in target_countries) and "remote" not in n.location.lower():
            return False
    return True


# ---------------------------------------------------------------------------
# Acquisition profile (persisted configuration, not scattered constants)
# ---------------------------------------------------------------------------

DEFAULT_ACQUISITION_PROFILE: Dict[str, Any] = {
    "services": [
        "full-stack websites/apps", "business software", "dashboards/admin systems",
        "booking/reservation systems", "e-commerce", "automation", "AI integrations",
        "SaaS/MVP development", "backend/API work", "frontend work", "maintenance/improvements",
    ],
    "skills": [
        "javascript", "typescript", "react", "node.js", "node", "express", "python",
        "sqlite", "postgresql", "rest api", "stripe", "payments", "e-commerce",
        "ecommerce", "booking system", "crm", "automation", "ai", "saas", "html", "css",
    ],
    "opportunity_types": [
        "full-stack", "backend", "frontend", "automation", "ai-integration", "saas", "maintenance",
    ],
    "min_budget_usd": None,
    "preferred_currencies": ["USD"],
    "target_countries": [],
    "remote_preference": "remote_ok",  # "remote_ok" | "remote_only" | "any"
    "keywords": [],
    "excluded_keywords": ["unpaid", "equity only", "no budget", "exposure only"],
    "max_age_days": 30,
    "source_settings": {
        "remotive": {"enabled": True},
        "weworkremotely": {"enabled": True},
        "upwork": {"enabled": False},
        "freelancer": {"enabled": False},
        "linkedin": {"enabled": False},
    },
    # Relevance hardening pass: these two lists gate PURSUE independent of
    # the numeric fit score (see QualificationEngine._service_relevance) --
    # editable from Acquisition Settings rather than hardcoded, per spec.
    # Defaults mirror revenue_hunter.py's own defaults so a profile saved
    # before these fields existed still gets sane values on read.
    "positive_service_signals": list(DEFAULT_POSITIVE_SERVICE_SIGNALS),
    "exclusion_role_signals": list(DEFAULT_EXCLUSION_ROLE_SIGNALS),
}


def build_qualification_engine(profile: Dict[str, Any]) -> QualificationEngine:
    """The one place a real discovery/qualify call builds its
    QualificationEngine, so the acquisition profile's positive/exclusion
    signal lists (editable in Acquisition Settings) actually take effect --
    a bare `QualificationEngine()` still works with sane defaults for tests
    and any call site that doesn't have a profile handy."""
    return QualificationEngine(
        positive_service_signals=profile.get("positive_service_signals"),
        exclusion_role_signals=profile.get("exclusion_role_signals"),
    )


class AcquisitionProfileStore:
    """A single persisted, editable settings row -- not hard-coded
    constants. Reading always returns the full default shape merged with
    whatever has actually been saved, so a profile saved before a new
    default field was introduced still comes back complete."""

    KEY = "acquisition_profile"

    def __init__(self, store: StateStore):
        self.store = store

    def get(self) -> Dict[str, Any]:
        rows = self.store.list("rh_settings", "key=?", (self.KEY,))
        if not rows:
            return json.loads(json.dumps(DEFAULT_ACQUISITION_PROFILE))  # deep copy
        saved = json.loads(rows[-1]["value_json"])
        merged = json.loads(json.dumps(DEFAULT_ACQUISITION_PROFILE))
        merged.update(saved)
        return merged

    def save(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        merged = self.get()
        merged.update({k: v for k, v in (updates or {}).items() if k in DEFAULT_ACQUISITION_PROFILE})
        rows = self.store.list("rh_settings", "key=?", (self.KEY,))
        now = utcnow()
        if rows:
            self.store.update("rh_settings", rows[-1]["id"], value_json=json.dumps(merged))
        else:
            self.store.create("rh_settings", {"key": self.KEY, "value_json": json.dumps(merged), "created_at": now, "updated_at": now})
        return merged


# ---------------------------------------------------------------------------
# AI research enrichment (reuses Falguna Search's existing infrastructure)
# ---------------------------------------------------------------------------

def enrich_opportunity_with_research(store: StateStore, opportunity: Dict[str, Any]) -> Optional[str]:
    """Best-effort, citation-preserving research on the opportunity's
    client/company, reusing the exact same provider (DuckDuckGoHTMLSearchProvider)
    and synthesis path (ResearchResponder over the local authenticated Codex
    transport) falguna/web.py's own Search feature uses -- not a duplicate
    implementation. Returns the new research_queries id on success, or None
    on ANY failure or missing precondition (no client name to look up, zero
    web sources found, no local model transport available, a synthesis
    error) -- the caller always continues gracefully with the opportunity's
    original data, per spec. Never invents a company fact: every claim in
    the synthesized answer is grounded in and cited to a retrieved source,
    exactly as research.py's own security model requires."""
    client_name = (opportunity.get("client_name") or "").strip()
    if not client_name:
        return None
    query = f"{client_name} company website business"
    try:
        provider_result = _RESEARCH_SEARCH_PROVIDER.search(query, max_results=5)
        sources: List[SourceResult] = rank_sources(provider_result.sources)
        if not sources:
            return None
        codex = shutil.which("codex")
        if not codex:
            return None
        gateway = OpenAICompatibleGateway(MODEL, "http://127.0.0.1:1/v1", "")
        codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        transport = ResilientCodexTransport(CodexCliJSONTransport(Path(codex), codex_home, timeout_seconds=45))
        outcome = ResearchResponder(gateway, transport, MODEL, timeout_seconds=45).reply(query, sources)
    except Exception:
        # Network failure, DNS failure, timeout, model/transport error, a
        # malformed reply -- all degrade the same way: no enrichment, never
        # a raised exception that could take down a discovery run.
        return None
    try:
        rs = ResearchStore(store)
        research_id = rs.create_query(query, _RESEARCH_SEARCH_PROVIDER.name)
        rs.save_result(research_id, outcome["answer"], sources, outcome["citations"], outcome.get("suggested_objective"))
        store.create("rh_opportunity_research", {
            "opportunity_id": opportunity["id"], "research_id": research_id, "created_at": utcnow(),
        })
        return research_id
    except Exception:
        return None


def get_research_for_opportunity(store: StateStore, opportunity_id: str) -> List[Dict[str, Any]]:
    links = store.list("rh_opportunity_research", "opportunity_id=?", (opportunity_id,))
    rs = ResearchStore(store)
    out = []
    for link in links:
        record = rs.get_query(link["research_id"])
        if not record:
            continue
        out.append({
            "research_id": link["research_id"],
            "query": record["query"],
            "answer": record["answer"],
            "status": record["status"],
            "sources": rs.get_sources(link["research_id"]),
        })
    return out


# ---------------------------------------------------------------------------
# Discovery runs (auditable, per-provider isolated)
# ---------------------------------------------------------------------------

class DiscoveryRunStore:
    def __init__(self, store: StateStore):
        self.store = store

    def create(self, actor: str, profile_snapshot: Dict[str, Any]) -> str:
        now = utcnow()
        return self.store.create("rh_discovery_runs", {
            "started_at": now, "completed_at": None, "actor": actor,
            "profile_snapshot_json": json.dumps(profile_snapshot),
            "providers_json": json.dumps([]),
            "opportunities_found": 0, "opportunities_new": 0, "opportunities_duplicate": 0,
            "created_at": now, "updated_at": now,
        })

    def complete(self, run_id: str, providers: List[Dict[str, Any]], found: int, new: int, duplicate: int,
                 filtered: int = 0, invalid: int = 0) -> None:
        self.store.update(
            "rh_discovery_runs", run_id, completed_at=utcnow(),
            providers_json=json.dumps(providers),
            opportunities_found=found, opportunities_new=new, opportunities_duplicate=duplicate,
            opportunities_filtered=filtered, opportunities_invalid=invalid,
        )

    def get(self, run_id: str) -> Optional[Dict[str, Any]]:
        row = self.store.get("rh_discovery_runs", run_id)
        if row:
            row = dict(row)
            row["providers"] = json.loads(row.pop("providers_json") or "[]")
            row["profile_snapshot"] = json.loads(row.pop("profile_snapshot_json") or "{}")
            # NULL on a run recorded before this additive column existed --
            # 0 is an honest "not tracked then", never a fabricated count.
            row["opportunities_filtered"] = row.get("opportunities_filtered") or 0
            row["opportunities_invalid"] = row.get("opportunities_invalid") or 0
        return row

    def list(self, limit: int = 20) -> List[Dict[str, Any]]:
        rows = self.store.list("rh_discovery_runs")
        rows = list(reversed(rows))[:limit]
        out = []
        for row in rows:
            row = dict(row)
            row["providers"] = json.loads(row.pop("providers_json") or "[]")
            row["profile_snapshot"] = json.loads(row.pop("profile_snapshot_json") or "{}")
            row["opportunities_filtered"] = row.get("opportunities_filtered") or 0
            row["opportunities_invalid"] = row.get("opportunities_invalid") or 0
            out.append(row)
        return out


class DiscoveryEngine:
    """AUTO-FIND -> AUTO-ANALYZE -> AUTO-DRAFT, one call. ARYAN APPROVES
    happens afterward, through the existing Needs Aryan queue -- this
    class never sends or applies to anything."""

    def __init__(self, store: StateStore, audit: AuditLog, needs_aryan=None, sources: Optional[List[OpportunitySource]] = None, orchestrator=None):
        self.store = store
        self.audit = audit
        self.needs_aryan = needs_aryan
        self.sources = sources if sources is not None else DEFAULT_SOURCES
        self.orchestrator = orchestrator

    def _find_duplicate(self, source: str, external_id: Optional[str], url_canonical: str, fingerprint: str) -> Optional[str]:
        if external_id:
            rows = self.store.list("rh_discovered_sources", "source=? AND external_id=?", (source, external_id))
            if rows:
                return rows[-1]["opportunity_id"]
        if url_canonical:
            rows = self.store.list("rh_discovered_sources", "url_canonical=?", (url_canonical,))
            if rows:
                return rows[-1]["opportunity_id"]
        rows = self.store.list("rh_discovered_sources", "content_fingerprint=?", (fingerprint,))
        if rows:
            return rows[-1]["opportunity_id"]
        return None

    def run_now(self, actor: str = "Aryan", limit_per_source: int = 25, research: bool = True) -> Dict[str, Any]:
        profile_store = AcquisitionProfileStore(self.store)
        profile = profile_store.get()
        run_store = DiscoveryRunStore(self.store)
        run_id = run_store.create(actor, profile)

        opportunities = OpportunityStore(self.store, self.audit, orchestrator=self.orchestrator)
        qualifier = QualificationStore(self.store, self.audit, build_qualification_engine(profile), orchestrator=self.orchestrator)
        source_settings = profile.get("source_settings") or {}

        provider_records: List[Dict[str, Any]] = []
        total_found = total_new = total_duplicate = total_filtered = total_invalid = 0
        created_ids: List[str] = []

        for source in self.sources:
            record: Dict[str, Any] = {"provider": source.name, "started_at": utcnow(), "available": bool(source.available)}
            if not source.available:
                record.update(completed_at=utcnow(), found=0, new=0, duplicates=0, filtered=0, invalid=0, error=source.unavailable_reason)
                provider_records.append(record)
                continue
            if not (source_settings.get(source.name) or {}).get("enabled", True):
                record.update(completed_at=utcnow(), found=0, new=0, duplicates=0, filtered=0, invalid=0, error="disabled in acquisition profile")
                provider_records.append(record)
                continue

            found = new = duplicates = filtered = invalid = 0
            error = None
            try:
                normalized_list = source.discover(profile, limit_per_source)
            except Exception as exc:
                # A single provider's network/parsing failure must never
                # kill the whole discovery run -- isolate it and continue
                # with the remaining providers.
                error = str(exc)[:300]
                normalized_list = []

            for n in normalized_list:
                found += 1
                if not n.title or not n.title.strip():
                    invalid += 1
                    continue  # never fabricate a title for a malformed listing
                if not _matches_profile(n, profile):
                    filtered += 1
                    continue
                url_canonical = canonicalize_url(n.url)
                fingerprint = content_fingerprint(n.title, n.client_name, n.description)
                existing = self._find_duplicate(n.source, n.external_id or None, url_canonical, fingerprint)
                if existing:
                    duplicates += 1
                    continue
                fields = {
                    "title": n.title.strip(),
                    "client_name": n.client_name,
                    "description": n.description or None,
                    "budget_rate": n.budget_text,
                    "required_skills": ", ".join(n.skills) if n.skills else None,
                    "location_timezone": n.location,
                    "source_url": n.url or None,
                }
                opportunity_id = opportunities.create(fields, actor=actor, source=n.source)
                self.store.create("rh_discovered_sources", {
                    "opportunity_id": opportunity_id, "source": n.source, "external_id": n.external_id or None,
                    "url_canonical": url_canonical or None, "content_fingerprint": fingerprint,
                    "discovery_run_id": run_id,
                    "raw_metadata_json": json.dumps(n.raw_metadata, default=str)[:20000],
                    "discovered_at": utcnow(), "created_at": utcnow(),
                })
                new += 1
                created_ids.append(opportunity_id)

                # AUTO-ANALYZE: qualify immediately.
                qualification = qualifier.qualify(opportunity_id, actor=actor)

                # AUTO-DRAFT, but only for high-confidence PURSUE opportunities --
                # MAYBE/IGNORE stay qualified and in history without flooding
                # the owner with drafts or approval items for weak leads.
                if qualification["recommendation"] == "PURSUE":
                    if research:
                        enrich_opportunity_with_research(self.store, opportunities.get(opportunity_id))
                    if self.needs_aryan is not None:
                        ProposalStore(self.store, self.audit, self.needs_aryan, orchestrator=self.orchestrator).generate(opportunity_id, "short", actor=actor)

            total_found += found
            total_new += new
            total_duplicate += duplicates
            total_filtered += filtered
            total_invalid += invalid
            record.update(completed_at=utcnow(), found=found, new=new, duplicates=duplicates, filtered=filtered, invalid=invalid, error=error)
            provider_records.append(record)

        run_store.complete(run_id, provider_records, total_found, total_new, total_duplicate, total_filtered, total_invalid)
        self.audit.append("RH_DISCOVERY_RUN_COMPLETED", {
            "run_id": run_id, "found": total_found, "new": total_new, "duplicates": total_duplicate,
            "filtered": total_filtered, "invalid": total_invalid,
        })
        return {
            "run_id": run_id,
            "providers": provider_records,
            "opportunities_found": total_found,
            "opportunities_new": total_new,
            "opportunities_filtered": total_filtered,
            "opportunities_invalid": total_invalid,
            "opportunities_duplicate": total_duplicate,
            "created_opportunity_ids": created_ids,
        }


# ---------------------------------------------------------------------------
# Safe re-qualification (relevance hardening pass, spec item 6)
# ---------------------------------------------------------------------------

def requalify_all(store: StateStore, audit: AuditLog, needs_aryan=None, actor: str = "Aryan", orchestrator=None) -> Dict[str, Any]:
    """Safe re-qualification of every non-terminal opportunity already in
    the pipeline, using the CURRENT acquisition profile and qualification
    logic (e.g. after a relevance hardening pass to the scoring engine).

    Never deletes anything: qualify() always appends a new qualification
    row, so full history is preserved and auditable -- calling this
    repeatedly is safe. A PURSUE -> MAYBE/IGNORE downgrade is handled
    without leaving anything misleading: any still-PENDING Needs Aryan
    approval for that opportunity's proposal is rejected (never silently
    left dangling as an active approval for a call that's no longer
    recommended), and the DRAFT proposal itself is marked SUPERSEDED --
    not deleted, and it was never sent regardless. An already-APPROVED
    proposal is left alone; that was a real human decision, not something
    this pass should override. A MAYBE/IGNORE -> PURSUE upgrade (e.g.
    something the old scoring bug wrongly buried) drafts a proposal +
    Needs Aryan item exactly as a fresh PURSUE discovery would, but only
    when the opportunity doesn't already have an active (DRAFT/APPROVED)
    proposal, so repeated calls never create duplicates."""
    profile = AcquisitionProfileStore(store).get()
    engine = build_qualification_engine(profile)
    qualifier = QualificationStore(store, audit, engine, orchestrator=orchestrator)
    opportunities = OpportunityStore(store, audit, orchestrator=orchestrator)
    proposals = ProposalStore(store, audit, needs_aryan, orchestrator=orchestrator)

    distribution = {"PURSUE": 0, "MAYBE": 0, "IGNORE": 0}
    downgraded_from_pursue: List[str] = []
    upgraded_to_pursue: List[str] = []
    superseded_proposal_ids: List[str] = []
    rejected_needs_aryan_ids: List[str] = []
    requalified = 0

    for opp in opportunities.list():
        if opp["stage"] in ("Won", "Lost"):
            continue  # a real human decision already happened -- never revisit it here
        previous_recommendation = (opp.get("qualification") or {}).get("recommendation")
        new_qualification = qualifier.qualify(opp["id"], actor=actor)
        new_recommendation = new_qualification["recommendation"]
        requalified += 1
        distribution[new_recommendation] = distribution.get(new_recommendation, 0) + 1

        if previous_recommendation == "PURSUE" and new_recommendation != "PURSUE":
            downgraded_from_pursue.append(opp["id"])
            reason = f"Opportunity requalified {previous_recommendation} -> {new_recommendation} after relevance hardening."
            for proposal in store.list("rh_proposals", "opportunity_id=?", (opp["id"],)):
                if proposal["status"] == "DRAFT":
                    proposals.mark_superseded(proposal["id"], actor, reason)
                    superseded_proposal_ids.append(proposal["id"])
                if needs_aryan is not None:
                    for item in store.list("needs_aryan_items", "ref_type=? AND ref_id=? AND status=?", ("rh_proposal", proposal["id"], "PENDING")):
                        needs_aryan.decide(item["id"], "reject", actor, note=f"Superseded: {reason}")
                        rejected_needs_aryan_ids.append(item["id"])
        elif previous_recommendation != "PURSUE" and new_recommendation == "PURSUE":
            upgraded_to_pursue.append(opp["id"])
            has_active_proposal = any(p["status"] in ("DRAFT", "APPROVED") for p in store.list("rh_proposals", "opportunity_id=?", (opp["id"],)))
            if not has_active_proposal:
                enrich_opportunity_with_research(store, opportunities.get(opp["id"]))
                proposals.generate(opp["id"], "short", actor=actor)

    audit.append("RH_REQUALIFICATION_RUN", {
        "actor": actor, "requalified": requalified, "distribution": distribution,
        "downgraded_from_pursue": downgraded_from_pursue, "upgraded_to_pursue": upgraded_to_pursue,
    })
    return {
        "requalified": requalified,
        "distribution": distribution,
        "downgraded_from_pursue": downgraded_from_pursue,
        "upgraded_to_pursue": upgraded_to_pursue,
        "superseded_proposal_ids": superseded_proposal_ids,
        "rejected_needs_aryan_ids": rejected_needs_aryan_ids,
    }
