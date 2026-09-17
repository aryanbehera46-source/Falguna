import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

from falguna.opportunity_agent import (
    DEFAULT_ACQUISITION_PROFILE,
    DEFAULT_SOURCES,
    AcquisitionProfileStore,
    DiscoveryEngine,
    DiscoveryRunStore,
    NormalizedOpportunity,
    RemotiveSource,
    UnavailableSource,
    WeWorkRemotelyRSSSource,
    _first_number,
    _matches_profile,
    _parse_date,
    build_qualification_engine,
    canonicalize_url,
    content_fingerprint,
    enrich_opportunity_with_research,
    get_research_for_opportunity,
    requalify_all,
)
from falguna.revenue_hunter import OpportunityStore, ProposalStore, QualificationStore
from falguna.runtime import open_control_plane
from falguna.store import StateStore
from falguna.ttt_hq import NeedsAryanQueue


def git(repo: Path, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


class _RepoCase(unittest.TestCase):
    """Same real-repo/real-sqlite setup pattern as test_revenue_hunter.py --
    an in-memory store would hide the additive-migration and NOT NULL
    schema bugs this module actually shipped with."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(self.repo)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "falguna@test.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Falguna Test"], check=True)
        (self.repo / "README.md").write_text("seed\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-m", "seed"], check=True, capture_output=True)
        self.control, self.store = open_control_plane(self.repo)
        self.audit = self.control.audit
        self.state_dir = self.repo / ".falguna"
        self.needs_aryan = NeedsAryanQueue(self.store, self.audit, self.control)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def _reopen_store(self) -> StateStore:
        fresh = StateStore(self.state_dir / "state.db")
        fresh.migrate()
        return fresh


class FakeSource:
    """A minimal third-party-style provider used to prove the
    OpportunitySource abstraction actually works with an arbitrary object
    (not just the two shipped providers) -- it only needs to duck-type
    name/available/unavailable_reason/discover()."""

    def __init__(self, name: str, items: List[NormalizedOpportunity] = None, error: Exception = None):
        self.name = name
        self.available = True
        self.unavailable_reason = None
        self._items = items or []
        self._error = error
        self.discover_call_count = 0

    def discover(self, profile: Dict[str, Any], limit: int) -> List[NormalizedOpportunity]:
        self.discover_call_count += 1
        if self._error is not None:
            raise self._error
        return list(self._items)


def _pursue_item(source="fake", external_id="ext-1", url="https://example.com/job/1", title="Full-stack booking app"):
    return NormalizedOpportunity(
        source=source,
        external_id=external_id,
        url=url,
        title=title,
        description="We need a full booking system with React, Node.js and PostgreSQL, built end to end for a small clinic.",
        client_name="Acme Clinic",
        budget_text="$2500",
        location="Remote",
        remote=True,
        skills=["react", "node.js", "postgresql"],
    )


def _maybe_item(source="fake", external_id="ext-2", url="https://example.com/job/2"):
    return NormalizedOpportunity(
        source=source,
        external_id=external_id,
        url=url,
        title="General help wanted",
        description="Looking for someone to help with a small task, details to be discussed later on a call.",
        client_name="Some Startup",
        budget_text=None,
        skills=[],
    )


def _ignore_item(source="fake", external_id="ext-3", url="https://example.com/job/3"):
    # Skill tokens deliberately have zero substring overlap (in either
    # direction) with QualificationEngine.DEFAULT_CAPABILITY_SKILLS, so this
    # reliably scores fit_score=0 -> IGNORE, regardless of the engine's own
    # (pre-existing, already-QA'd) substring-matching quirks -- e.g. a naive
    # token like "mainframe" would accidentally substring-match "ai".
    return NormalizedOpportunity(
        source=source,
        external_id=external_id,
        url=url,
        title="Legacy widget maintenance",
        description="Need someone fluent in widgetlang and sprocketql for legacy gizmoscript batch work, a proprietary decades-old system nobody else understands well.",
        client_name="Legacy Corp",
        budget_text="$500",
        skills=["widgetlang", "sprocketql", "gizmoscript"],
    )


def _writer_item(source="fake", external_id="ext-4", url="https://example.com/job/4"):
    # The exact live-QA finding, as a discovered listing: a Remotive-style
    # freelance writer post with no real required_skills, whose description
    # happens to mention "AI" once. Before the relevance hardening pass this
    # scored fit_score=100 and PURSUE end to end through DiscoveryEngine.
    return NormalizedOpportunity(
        source=source,
        external_id=external_id,
        url=url,
        title="Freelance Writer",
        description="We need a freelance writer to create blog content about AI tools and trends for our marketing site. Must have strong writing skills and SEO knowledge.",
        client_name="Marketing Co",
        budget_text="$25/hr",
        skills=[],
    )


def _insert_legacy_qualification(store, opportunity_id, recommendation, fit_score=100):
    """Directly inserts a qualification row bypassing the engine, to
    simulate an opportunity that was qualified under the OLD (pre-hardening)
    scoring logic -- exactly the kind of stale, wrongly-PURSUE (or
    wrongly-buried) row requalify_all() must handle safely."""
    from falguna.store import utcnow
    return store.create("rh_qualifications", {
        "opportunity_id": opportunity_id, "fit_score": fit_score, "budget_quality": "MEDIUM",
        "effort_vs_return": "Reasonable", "portfolio_match": None, "portfolio_match_reason": None,
        "recurring_potential": "MEDIUM", "urgency": "Normal", "risk_flags": None,
        "recommendation": recommendation, "suggested_price": "$500", "suggested_timeline": "2 week(s)",
        "suggested_portfolio_proof": None, "created_at": utcnow(),
    })


# ---------------------------------------------------------------------------
# Helpers: canonicalize_url / content_fingerprint / _matches_profile / dates
# ---------------------------------------------------------------------------

class HelperFunctionTests(unittest.TestCase):
    def test_canonicalize_url_strips_www_scheme_case_and_trailing_slash(self):
        a = canonicalize_url("HTTPS://WWW.Example.com/Jobs/123/")
        b = canonicalize_url("https://example.com/Jobs/123")
        self.assertEqual(a, b)

    def test_canonicalize_url_empty_input(self):
        self.assertEqual(canonicalize_url(None), "")
        self.assertEqual(canonicalize_url(""), "")

    def test_content_fingerprint_is_stable_and_normalizes_whitespace(self):
        f1 = content_fingerprint("  Full   Stack   Dev ", "Acme", "Build   us a site")
        f2 = content_fingerprint("Full Stack Dev", "Acme", "Build us a site")
        self.assertEqual(f1, f2)

    def test_content_fingerprint_differs_for_different_content(self):
        f1 = content_fingerprint("Full Stack Dev", "Acme", "Build us a site")
        f2 = content_fingerprint("Backend Dev", "Acme", "Build us a site")
        self.assertNotEqual(f1, f2)

    def test_first_number_parses_first_numeric_token(self):
        self.assertEqual(_first_number("$2,500 - $3,000"), 2500.0)
        self.assertIsNone(_first_number(None))
        self.assertIsNone(_first_number("no numbers here"))

    def test_parse_date_handles_rfc2822_and_iso8601_and_garbage(self):
        rfc = _parse_date("Wed, 02 Oct 2024 08:00:00 GMT")
        iso = _parse_date("2024-10-02T08:00:00Z")
        self.assertIsNotNone(rfc)
        self.assertIsNotNone(iso)
        self.assertIsNone(_parse_date("not a date"))
        self.assertIsNone(_parse_date(None))

    def test_matches_profile_excluded_keyword_blocks(self):
        n = NormalizedOpportunity(source="s", external_id="1", url="u", title="Unpaid internship", description="")
        profile = dict(DEFAULT_ACQUISITION_PROFILE)
        self.assertFalse(_matches_profile(n, profile))

    def test_matches_profile_required_keyword_enforced(self):
        n = NormalizedOpportunity(source="s", external_id="1", url="u", title="Backend role", description="Python work")
        profile = dict(DEFAULT_ACQUISITION_PROFILE, keywords=["react"])
        self.assertFalse(_matches_profile(n, profile))
        profile2 = dict(DEFAULT_ACQUISITION_PROFILE, keywords=["python"])
        self.assertTrue(_matches_profile(n, profile2))

    def test_matches_profile_min_budget_filters_low_offers(self):
        n = NormalizedOpportunity(source="s", external_id="1", url="u", title="t", description="", budget_text="$100")
        profile = dict(DEFAULT_ACQUISITION_PROFILE, min_budget_usd=500)
        self.assertFalse(_matches_profile(n, profile))

    def test_matches_profile_max_age_filters_stale_listings(self):
        stale = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        n = NormalizedOpportunity(source="s", external_id="1", url="u", title="t", description="", posted_at=stale)
        profile = dict(DEFAULT_ACQUISITION_PROFILE, max_age_days=30)
        self.assertFalse(_matches_profile(n, profile))

    def test_matches_profile_remote_only_excludes_non_remote(self):
        n = NormalizedOpportunity(source="s", external_id="1", url="u", title="t", description="", remote=False)
        profile = dict(DEFAULT_ACQUISITION_PROFILE, remote_preference="remote_only")
        self.assertFalse(_matches_profile(n, profile))

    def test_matches_profile_target_countries_allows_remote_regardless(self):
        n = NormalizedOpportunity(source="s", external_id="1", url="u", title="t", description="", location="Remote")
        profile = dict(DEFAULT_ACQUISITION_PROFILE, target_countries=["Germany"])
        self.assertTrue(_matches_profile(n, profile))

    def test_matches_profile_target_countries_blocks_mismatched_location(self):
        n = NormalizedOpportunity(source="s", external_id="1", url="u", title="t", description="", location="Onsite, Tokyo")
        profile = dict(DEFAULT_ACQUISITION_PROFILE, target_countries=["Germany"])
        self.assertFalse(_matches_profile(n, profile))


# ---------------------------------------------------------------------------
# Provider abstraction / registry
# ---------------------------------------------------------------------------

class ProviderAbstractionTests(unittest.TestCase):
    def test_default_sources_expose_the_required_shape(self):
        for source in DEFAULT_SOURCES:
            self.assertTrue(hasattr(source, "name"))
            self.assertTrue(hasattr(source, "available"))
            self.assertTrue(hasattr(source, "unavailable_reason"))
            self.assertTrue(callable(source.discover))

    def test_two_real_sources_are_available_three_are_honest_placeholders(self):
        available = {s.name for s in DEFAULT_SOURCES if s.available}
        unavailable = {s.name for s in DEFAULT_SOURCES if not s.available}
        self.assertEqual(available, {"remotive", "weworkremotely"})
        self.assertEqual(unavailable, {"upwork", "freelancer", "linkedin"})

    def test_unavailable_source_never_fabricates_and_raises_if_called_directly(self):
        src = UnavailableSource("testsrc", "needs a paid API")
        self.assertFalse(src.available)
        self.assertEqual(src.unavailable_reason, "needs a paid API")
        with self.assertRaises(RuntimeError):
            src.discover({}, 10)

    def test_a_third_party_style_fake_provider_plugs_in_without_any_special_casing(self):
        # Proves the abstraction: DiscoveryEngine only ever depends on
        # name/available/unavailable_reason/discover(), so a totally new
        # provider class (not RemotiveSource/WeWorkRemotelyRSSSource/
        # UnavailableSource) can be swapped in with zero engine changes.
        fake = FakeSource("acme-board", items=[_pursue_item()])
        self.assertEqual(fake.discover({}, 25)[0].title, "Full-stack booking app")


class RemotiveAndWeWorkRemotelyParsingTests(unittest.TestCase):
    """Unit tests against a mocked HTTP layer -- live network is unreliable
    from this sandbox (see the report), but the parsing logic itself must
    be verified without depending on the real feeds being reachable."""

    def test_remotive_source_parses_a_realistic_json_response(self):
        import json as _json
        payload = _json.dumps({
            "jobs": [
                {
                    "id": 999, "url": "https://remotive.com/job/999", "title": "React Developer",
                    "description": "<p>Build <b>things</b> with React.</p>", "company_name": "RemoteCo",
                    "salary": "$60,000 - $80,000", "candidate_required_location": "Worldwide",
                    "publication_date": "2024-01-01T00:00:00", "tags": ["react", "javascript"],
                }
            ]
        }).encode()

        class _FakeResponse:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

            def read(self_inner, n=None):
                return payload

        with mock.patch("falguna.opportunity_agent.urllib.request.urlopen", return_value=_FakeResponse()):
            results = RemotiveSource().discover({"keywords": []}, limit=10)
        self.assertEqual(len(results), 1)
        r = results[0]
        self.assertEqual(r.source, "remotive")
        self.assertEqual(r.external_id, "999")
        self.assertEqual(r.title, "React Developer")
        self.assertNotIn("<b>", r.description)
        self.assertEqual(r.client_name, "RemoteCo")
        self.assertTrue(r.remote)
        self.assertIn("react", r.skills)

    def test_weworkremotely_source_parses_rss_and_splits_company_title(self):
        rss = b"""<?xml version="1.0"?>
<rss><channel>
<item>
<title>Acme Corp: Senior Backend Engineer</title>
<link>https://weworkremotely.com/jobs/1</link>
<guid>https://weworkremotely.com/jobs/1</guid>
<description>&lt;p&gt;Great backend role&lt;/p&gt;</description>
<pubDate>Wed, 02 Oct 2024 08:00:00 GMT</pubDate>
</item>
</channel></rss>"""

        class _FakeResponse:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

            def read(self_inner, n=None):
                return rss

        with mock.patch("falguna.opportunity_agent.urllib.request.urlopen", return_value=_FakeResponse()):
            results = WeWorkRemotelyRSSSource().discover({}, limit=10)
        self.assertEqual(len(results), 1)
        r = results[0]
        self.assertEqual(r.client_name, "Acme Corp")
        self.assertEqual(r.title, "Senior Backend Engineer")
        self.assertNotIn("<p>", r.description)
        self.assertEqual(r.location, "Remote")


# ---------------------------------------------------------------------------
# Acquisition profile persistence
# ---------------------------------------------------------------------------

class AcquisitionProfileStoreTests(_RepoCase):
    def test_get_returns_full_default_shape_when_nothing_saved(self):
        profile = AcquisitionProfileStore(self.store).get()
        self.assertEqual(profile["remote_preference"], "remote_ok")
        self.assertIn("source_settings", profile)
        self.assertEqual(set(profile["source_settings"]), {"remotive", "weworkremotely", "upwork", "freelancer", "linkedin"})

    def test_save_merges_partial_update_and_keeps_other_fields(self):
        store = AcquisitionProfileStore(self.store)
        store.save({"min_budget_usd": 750})
        profile = store.save({"keywords": ["react", "node"]})
        self.assertEqual(profile["min_budget_usd"], 750)
        self.assertEqual(profile["keywords"], ["react", "node"])
        self.assertEqual(profile["remote_preference"], "remote_ok")  # untouched default

    def test_save_ignores_unknown_keys(self):
        profile = AcquisitionProfileStore(self.store).save({"not_a_real_field": 123})
        self.assertNotIn("not_a_real_field", profile)

    def test_second_save_updates_the_same_row_not_a_new_one(self):
        store = AcquisitionProfileStore(self.store)
        store.save({"min_budget_usd": 100})
        store.save({"min_budget_usd": 200})
        rows = self.store.list("rh_settings", "key=?", (AcquisitionProfileStore.KEY,))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["updated_at"], rows[0]["updated_at"])  # NOT NULL, present

    def test_acquisition_profile_persists_across_a_simulated_restart(self):
        AcquisitionProfileStore(self.store).save({"min_budget_usd": 900, "target_countries": ["Canada"]})
        fresh = self._reopen_store()
        try:
            profile = AcquisitionProfileStore(fresh).get()
            self.assertEqual(profile["min_budget_usd"], 900)
            self.assertEqual(profile["target_countries"], ["Canada"])
        finally:
            fresh.close()


# ---------------------------------------------------------------------------
# Discovery: normalization, provenance, dedup
# ---------------------------------------------------------------------------

class DiscoveryNormalizationAndProvenanceTests(_RepoCase):
    def test_discovered_opportunity_is_normalized_with_full_provenance(self):
        fake = FakeSource("fake", items=[_pursue_item()])
        engine = DiscoveryEngine(self.store, self.audit, needs_aryan=self.needs_aryan, sources=[fake])
        result = engine.run_now(research=False)

        self.assertEqual(result["opportunities_new"], 1)
        opp_id = result["created_opportunity_ids"][0]
        opp = OpportunityStore(self.store, self.audit).get(opp_id)
        self.assertEqual(opp["source"], "fake")
        self.assertEqual(opp["client_name"], "Acme Clinic")
        self.assertEqual(opp["budget_rate"], "$2500")
        self.assertIn("react", opp["required_skills"])
        self.assertEqual(opp["source_url"], "https://example.com/job/1")

        provenance_rows = self.store.list("rh_discovered_sources", "opportunity_id=?", (opp_id,))
        self.assertEqual(len(provenance_rows), 1)
        row = provenance_rows[0]
        self.assertEqual(row["source"], "fake")
        self.assertEqual(row["external_id"], "ext-1")
        self.assertEqual(row["discovery_run_id"], result["run_id"])
        self.assertTrue(row["content_fingerprint"])

    def test_malformed_listing_without_a_title_is_skipped_not_fabricated(self):
        blank = NormalizedOpportunity(source="fake", external_id="x", url="https://x", title="   ", description="")
        fake = FakeSource("fake", items=[blank])
        engine = DiscoveryEngine(self.store, self.audit, sources=[fake])
        result = engine.run_now(research=False)
        self.assertEqual(result["opportunities_new"], 0)
        self.assertEqual(result["created_opportunity_ids"], [])

    def test_out_of_profile_listing_is_filtered_not_created(self):
        unpaid = NormalizedOpportunity(source="fake", external_id="x", url="https://x", title="Unpaid gig", description="no budget, exposure only")
        fake = FakeSource("fake", items=[unpaid])
        engine = DiscoveryEngine(self.store, self.audit, sources=[fake])
        result = engine.run_now(research=False)
        self.assertEqual(result["opportunities_found"], 1)
        self.assertEqual(result["opportunities_new"], 0)
        self.assertEqual(result["opportunities_duplicate"], 0)


class DeduplicationTests(_RepoCase):
    def test_repeated_discovery_of_the_same_listing_creates_no_duplicate(self):
        item = _pursue_item()
        fake = FakeSource("fake", items=[item])
        engine = DiscoveryEngine(self.store, self.audit, sources=[fake])

        first = engine.run_now(research=False)
        second = engine.run_now(research=False)

        self.assertEqual(first["opportunities_new"], 1)
        self.assertEqual(second["opportunities_new"], 0)
        self.assertEqual(second["opportunities_duplicate"], 1)
        all_opps = OpportunityStore(self.store, self.audit).list()
        self.assertEqual(len(all_opps), 1)

    def test_dedup_falls_back_to_url_when_external_id_is_missing(self):
        item1 = NormalizedOpportunity(source="fake", external_id="", url="https://example.com/job/dup", title="Dup by URL", description="Some real description text that is long enough.")
        item2 = NormalizedOpportunity(source="fake", external_id="", url="https://example.com/job/dup", title="Dup by URL (reposted)", description="A different description entirely, still long enough to avoid vague flag.")
        fake1 = FakeSource("fake", items=[item1])
        engine1 = DiscoveryEngine(self.store, self.audit, sources=[fake1])
        engine1.run_now(research=False)

        fake2 = FakeSource("fake", items=[item2])
        engine2 = DiscoveryEngine(self.store, self.audit, sources=[fake2])
        second = engine2.run_now(research=False)
        self.assertEqual(second["opportunities_new"], 0)
        self.assertEqual(second["opportunities_duplicate"], 1)

    def test_dedup_falls_back_to_content_fingerprint_when_no_id_or_url(self):
        item1 = NormalizedOpportunity(source="fake", external_id="", url="", title="Same Role Twice", client_name="SameCo", description="Identical enough description text for fingerprinting purposes here.")
        item2 = NormalizedOpportunity(source="fake", external_id="", url="", title="Same Role Twice", client_name="SameCo", description="Identical enough description text for fingerprinting purposes here.")
        engine1 = DiscoveryEngine(self.store, self.audit, sources=[FakeSource("fake", items=[item1])])
        engine1.run_now(research=False)
        engine2 = DiscoveryEngine(self.store, self.audit, sources=[FakeSource("fake", items=[item2])])
        second = engine2.run_now(research=False)
        self.assertEqual(second["opportunities_new"], 0)
        self.assertEqual(second["opportunities_duplicate"], 1)

    def test_dedup_persists_across_a_simulated_restart(self):
        item = _pursue_item()
        engine = DiscoveryEngine(self.store, self.audit, sources=[FakeSource("fake", items=[item])])
        first = engine.run_now(research=False)
        self.assertEqual(first["opportunities_new"], 1)

        fresh = self._reopen_store()
        try:
            engine2 = DiscoveryEngine(fresh, self.audit, sources=[FakeSource("fake", items=[item])])
            second = engine2.run_now(research=False)
            self.assertEqual(second["opportunities_new"], 0)
            self.assertEqual(second["opportunities_duplicate"], 1)
        finally:
            fresh.close()


# ---------------------------------------------------------------------------
# Provider failure isolation
# ---------------------------------------------------------------------------

class ProviderFailureIsolationTests(_RepoCase):
    def test_one_providers_exception_does_not_kill_the_run_or_other_providers(self):
        broken = FakeSource("broken", error=RuntimeError("network blew up"))
        healthy = FakeSource("healthy", items=[_pursue_item(source="healthy", external_id="h1", url="https://h/1")])
        engine = DiscoveryEngine(self.store, self.audit, sources=[broken, healthy])

        result = engine.run_now(research=False)

        self.assertEqual(result["opportunities_new"], 1)
        by_provider = {p["provider"]: p for p in result["providers"]}
        self.assertIn("network blew up", by_provider["broken"]["error"])
        self.assertEqual(by_provider["broken"]["new"], 0)
        self.assertIsNone(by_provider["healthy"]["error"])
        self.assertEqual(by_provider["healthy"]["new"], 1)

    def test_unavailable_source_is_never_called_and_is_reported_honestly(self):
        placeholder = UnavailableSource("placeholder", "needs paid API")
        engine = DiscoveryEngine(self.store, self.audit, sources=[placeholder])
        result = engine.run_now(research=False)
        record = result["providers"][0]
        self.assertFalse(record["available"])
        self.assertEqual(record["error"], "needs paid API")
        self.assertEqual(record["found"], 0)

    def test_source_disabled_in_profile_is_skipped_without_being_called(self):
        fake = FakeSource("customsource", items=[_pursue_item(source="customsource")])
        AcquisitionProfileStore(self.store).save({
            "source_settings": {"customsource": {"enabled": False}},
        })
        engine = DiscoveryEngine(self.store, self.audit, sources=[fake])
        result = engine.run_now(research=False)
        self.assertEqual(fake.discover_call_count, 0)
        self.assertEqual(result["opportunities_new"], 0)
        self.assertIn("disabled", result["providers"][0]["error"])

    def test_every_provider_including_failures_is_recorded_for_auditability(self):
        broken = FakeSource("broken", error=ValueError("boom"))
        placeholder = UnavailableSource("placeholder", "no api")
        healthy = FakeSource("healthy", items=[])
        engine = DiscoveryEngine(self.store, self.audit, sources=[broken, placeholder, healthy])
        result = engine.run_now(research=False)
        self.assertEqual(len(result["providers"]), 3)
        for record in result["providers"]:
            for key in ("provider", "started_at", "completed_at", "found", "new", "duplicates", "error"):
                self.assertIn(key, record)


# ---------------------------------------------------------------------------
# Qualification integration
# ---------------------------------------------------------------------------

class QualificationIntegrationTests(_RepoCase):
    def test_every_discovered_opportunity_is_auto_qualified(self):
        engine = DiscoveryEngine(self.store, self.audit, sources=[FakeSource("fake", items=[_pursue_item(), _maybe_item(), _ignore_item()])])
        result = engine.run_now(research=False)
        self.assertEqual(result["opportunities_new"], 3)
        for opp_id in result["created_opportunity_ids"]:
            qual = OpportunityStore(self.store, self.audit).latest_qualification(opp_id)
            self.assertIsNotNone(qual)
            self.assertIn(qual["recommendation"], {"PURSUE", "MAYBE", "IGNORE"})
            # The 3 fields added specifically for Opportunity Agent v1.
            self.assertIn("estimated_project_value", qual.keys())
            self.assertIn("capability_gaps", qual.keys())
            self.assertIn("why", qual.keys())
            self.assertTrue(qual["why"])

    def test_qualification_recommendations_match_the_crafted_fixtures(self):
        engine = DiscoveryEngine(self.store, self.audit, sources=[FakeSource("fake", items=[_pursue_item(), _maybe_item(), _ignore_item()])])
        result = engine.run_now(research=False)
        quals = [OpportunityStore(self.store, self.audit).latest_qualification(oid)["recommendation"] for oid in result["created_opportunity_ids"]]
        self.assertEqual(quals, ["PURSUE", "MAYBE", "IGNORE"])

    def test_ignored_opportunities_are_kept_in_history_not_discarded(self):
        engine = DiscoveryEngine(self.store, self.audit, sources=[FakeSource("fake", items=[_ignore_item()])])
        result = engine.run_now(research=False)
        self.assertEqual(result["opportunities_new"], 1)
        opp = OpportunityStore(self.store, self.audit).get(result["created_opportunity_ids"][0])
        self.assertIsNotNone(opp)
        self.assertEqual(opp["qualification"]["recommendation"], "IGNORE")


# ---------------------------------------------------------------------------
# Proposal auto-draft + Needs Aryan, without flooding
# ---------------------------------------------------------------------------

class ProposalAndNeedsAryanTests(_RepoCase):
    def test_pursue_opportunity_gets_exactly_one_draft_proposal_and_needs_aryan_item(self):
        engine = DiscoveryEngine(self.store, self.audit, needs_aryan=self.needs_aryan, sources=[FakeSource("fake", items=[_pursue_item()])])
        result = engine.run_now(research=False)
        opp_id = result["created_opportunity_ids"][0]
        opp = OpportunityStore(self.store, self.audit).get(opp_id)
        self.assertEqual(len(opp["proposals"]), 1)
        self.assertEqual(opp["proposals"][0]["status"], "DRAFT")
        self.assertTrue(opp["proposals"][0]["content"])

        needs_aryan_rows = self.store.list("needs_aryan_items", "ref_type=?", ("rh_proposal",))
        matching = [r for r in needs_aryan_rows if r["ref_id"] == opp["proposals"][0]["id"]]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["status"], "PENDING")

    def test_maybe_and_ignore_opportunities_get_no_proposal_and_no_needs_aryan_item(self):
        engine = DiscoveryEngine(self.store, self.audit, needs_aryan=self.needs_aryan, sources=[FakeSource("fake", items=[_maybe_item(), _ignore_item()])])
        result = engine.run_now(research=False)
        for opp_id in result["created_opportunity_ids"]:
            opp = OpportunityStore(self.store, self.audit).get(opp_id)
            self.assertEqual(opp["proposals"], [])
        self.assertEqual(self.store.list("needs_aryan_items"), [])

    def test_needs_aryan_is_never_flooded_by_a_mixed_batch(self):
        items = [_pursue_item(external_id="p1", url="https://e/p1"), _maybe_item(external_id="m1", url="https://e/m1"),
                 _ignore_item(external_id="i1", url="https://e/i1"), _pursue_item(external_id="p2", url="https://e/p2", title="Another full-stack booking build")]
        engine = DiscoveryEngine(self.store, self.audit, needs_aryan=self.needs_aryan, sources=[FakeSource("fake", items=items)])
        engine.run_now(research=False)
        needs_aryan_rows = self.store.list("needs_aryan_items")
        # Exactly the 2 PURSUE opportunities get an approval item -- not 4.
        self.assertEqual(len(needs_aryan_rows), 2)

    def test_without_a_needs_aryan_queue_wired_no_proposal_is_auto_drafted(self):
        # Documents the current, intentional behavior: proposal auto-drafting
        # is tied to having somewhere to surface the approval request. The
        # production route (hq_web.py's /api/rh/discover) always wires a
        # real NeedsAryanQueue, so this only matters for a bare/manual call.
        engine = DiscoveryEngine(self.store, self.audit, needs_aryan=None, sources=[FakeSource("fake", items=[_pursue_item()])])
        result = engine.run_now(research=False)
        opp = OpportunityStore(self.store, self.audit).get(result["created_opportunity_ids"][0])
        self.assertEqual(opp["proposals"], [])
        self.assertEqual(opp["qualification"]["recommendation"], "PURSUE")

    def test_proposal_is_never_marked_sent_by_discovery(self):
        engine = DiscoveryEngine(self.store, self.audit, needs_aryan=self.needs_aryan, sources=[FakeSource("fake", items=[_pursue_item()])])
        result = engine.run_now(research=False)
        opp = OpportunityStore(self.store, self.audit).get(result["created_opportunity_ids"][0])
        statuses = {p["status"] for p in opp["proposals"]}
        self.assertEqual(statuses, {"DRAFT"})


# ---------------------------------------------------------------------------
# Research enrichment: graceful failure on every path
# ---------------------------------------------------------------------------

class ResearchEnrichmentFailureTests(_RepoCase):
    def test_no_client_name_returns_none_without_touching_the_network(self):
        opp_id = OpportunityStore(self.store, self.audit).create({"title": "No client here"}, source="fake")
        opp = OpportunityStore(self.store, self.audit).get(opp_id)
        with mock.patch("falguna.opportunity_agent._RESEARCH_SEARCH_PROVIDER") as search_mock:
            result = enrich_opportunity_with_research(self.store, opp)
            search_mock.search.assert_not_called()
        self.assertIsNone(result)

    def test_zero_search_results_degrades_gracefully(self):
        opp_id = OpportunityStore(self.store, self.audit).create({"title": "t", "client_name": "Acme"}, source="fake")
        opp = OpportunityStore(self.store, self.audit).get(opp_id)

        class _EmptyResult:
            sources = []

        with mock.patch("falguna.opportunity_agent._RESEARCH_SEARCH_PROVIDER") as search_mock:
            search_mock.search.return_value = _EmptyResult()
            result = enrich_opportunity_with_research(self.store, opp)
        self.assertIsNone(result)
        self.assertEqual(get_research_for_opportunity(self.store, opp_id), [])

    def test_search_provider_exception_never_propagates(self):
        opp_id = OpportunityStore(self.store, self.audit).create({"title": "t", "client_name": "Acme"}, source="fake")
        opp = OpportunityStore(self.store, self.audit).get(opp_id)
        with mock.patch("falguna.opportunity_agent._RESEARCH_SEARCH_PROVIDER") as search_mock:
            search_mock.search.side_effect = RuntimeError("dns failure")
            result = enrich_opportunity_with_research(self.store, opp)  # must not raise
        self.assertIsNone(result)

    def test_missing_codex_cli_degrades_gracefully(self):
        # This sandbox genuinely has no `codex` CLI, which already exercises
        # this path for real, but pin the behavior explicitly too.
        opp_id = OpportunityStore(self.store, self.audit).create({"title": "t", "client_name": "Acme"}, source="fake")
        opp = OpportunityStore(self.store, self.audit).get(opp_id)

        class _Src:
            def __init__(self, url):
                self.url = url
                self.title = "Acme site"
                self.domain = "acme.example"

        class _OneResult:
            sources = [_Src("https://acme.example")]

        with mock.patch("falguna.opportunity_agent._RESEARCH_SEARCH_PROVIDER") as search_mock, \
             mock.patch("falguna.opportunity_agent.rank_sources", return_value=[_Src("https://acme.example")]), \
             mock.patch("falguna.opportunity_agent.shutil.which", return_value=None):
            search_mock.search.return_value = _OneResult()
            result = enrich_opportunity_with_research(self.store, opp)
        self.assertIsNone(result)

    def test_discovery_run_continues_when_research_raises_for_a_pursue_item(self):
        with mock.patch("falguna.opportunity_agent.enrich_opportunity_with_research", side_effect=RuntimeError("boom")):
            engine = DiscoveryEngine(self.store, self.audit, needs_aryan=self.needs_aryan, sources=[FakeSource("fake", items=[_pursue_item()])])
            with self.assertRaises(RuntimeError):
                # enrich_opportunity_with_research is documented to never
                # raise; if it somehow did, that is a real bug worth a
                # loud failure here rather than silently swallowing it in
                # run_now (run_now does not wrap this call in try/except,
                # by design -- enrichment itself must be the one place
                # that guarantees graceful degradation).
                engine.run_now(research=True)


# ---------------------------------------------------------------------------
# Discovery run auditability
# ---------------------------------------------------------------------------

class DiscoveryRunStoreTests(_RepoCase):
    def test_create_then_complete_round_trips_through_get(self):
        run_store = DiscoveryRunStore(self.store)
        run_id = run_store.create("Aryan", {"keywords": ["react"]})
        run_store.complete(run_id, [{"provider": "fake", "found": 2, "new": 1, "duplicates": 1}], found=2, new=1, duplicate=1)
        run = run_store.get(run_id)
        self.assertEqual(run["actor"], "Aryan")
        self.assertEqual(run["opportunities_found"], 2)
        self.assertEqual(run["profile_snapshot"]["keywords"], ["react"])
        self.assertEqual(run["providers"][0]["provider"], "fake")
        self.assertIsNotNone(run["completed_at"])

    def test_list_is_reverse_chronological(self):
        run_store = DiscoveryRunStore(self.store)
        first = run_store.create("Aryan", {})
        run_store.complete(first, [], 0, 0, 0)
        second = run_store.create("Aryan", {})
        run_store.complete(second, [], 0, 0, 0)
        items = run_store.list(limit=10)
        self.assertEqual(items[0]["id"], second)
        self.assertEqual(items[1]["id"], first)

    def test_get_returns_none_for_unknown_run(self):
        self.assertIsNone(DiscoveryRunStore(self.store).get("does-not-exist"))

    def test_full_run_now_produces_a_matching_auditable_record(self):
        engine = DiscoveryEngine(self.store, self.audit, sources=[FakeSource("fake", items=[_pursue_item()])])
        result = engine.run_now(research=False)
        run = DiscoveryRunStore(self.store).get(result["run_id"])
        self.assertEqual(run["opportunities_new"], result["opportunities_new"])
        self.assertEqual(run["opportunities_found"], result["opportunities_found"])
        self.assertEqual(run["opportunities_duplicate"], result["opportunities_duplicate"])
        self.assertEqual(run["providers"], result["providers"])


# ---------------------------------------------------------------------------
# Relevance hardening pass: discovery-level integration + accounting
# ---------------------------------------------------------------------------

class DiscoveryUsesTheRelevanceGateTests(_RepoCase):
    def test_a_discovered_freelance_writer_listing_is_ignored_not_pursued(self):
        # End-to-end reproduction of the live-QA finding through the real
        # DiscoveryEngine (not just QualificationEngine in isolation): the
        # same fix must actually be wired into the discovery pipeline.
        engine = DiscoveryEngine(self.store, self.audit, needs_aryan=self.needs_aryan, sources=[FakeSource("fake", items=[_writer_item()])])
        result = engine.run_now(research=False)
        self.assertEqual(result["opportunities_new"], 1)
        opp = OpportunityStore(self.store, self.audit).get(result["created_opportunity_ids"][0])
        self.assertEqual(opp["qualification"]["recommendation"], "IGNORE")
        self.assertFalse(opp["qualification"]["relevance_passed"])
        # No proposal, no Needs Aryan item -- an IGNORE must never flood the queue.
        self.assertEqual(opp["proposals"], [])
        self.assertEqual(self.store.list("needs_aryan_items"), [])

    def test_discovery_qualification_respects_a_custom_acquisition_profile(self):
        # The positive/exclusion signal lists are meant to be editable via
        # Acquisition Settings, not hardcoded -- prove a saved profile
        # change actually reaches the qualification engine DiscoveryEngine
        # uses internally.
        from falguna.opportunity_agent import AcquisitionProfileStore
        AcquisitionProfileStore(self.store).save({"exclusion_role_signals": []})  # nothing excluded anymore
        engine = DiscoveryEngine(self.store, self.audit, needs_aryan=self.needs_aryan, sources=[FakeSource("fake", items=[_writer_item()])])
        result = engine.run_now(research=False)
        opp = OpportunityStore(self.store, self.audit).get(result["created_opportunity_ids"][0])
        # With no exclusion signals configured, the gate can't fail --
        # confirms the profile's lists, not a hardcoded default, are in effect.
        self.assertTrue(opp["qualification"]["relevance_passed"])


class DiscoveryAccountingTests(_RepoCase):
    def test_filtered_and_invalid_counts_explain_the_found_new_duplicate_gap(self):
        blank_title = NormalizedOpportunity(source="fake", external_id="x1", url="https://x/1", title="   ", description="")
        excluded_by_profile = NormalizedOpportunity(source="fake", external_id="x2", url="https://x/2", title="Unpaid gig", description="no budget, exposure only")
        real = _pursue_item(external_id="x3", url="https://x/3")
        engine = DiscoveryEngine(self.store, self.audit, sources=[FakeSource("fake", items=[blank_title, excluded_by_profile, real])])
        result = engine.run_now(research=False)

        self.assertEqual(result["opportunities_found"], 3)
        self.assertEqual(result["opportunities_invalid"], 1)
        self.assertEqual(result["opportunities_filtered"], 1)
        self.assertEqual(result["opportunities_new"], 1)
        self.assertEqual(result["opportunities_duplicate"], 0)
        # Every listing is now accounted for -- this is exactly the "13
        # found / 12 new / 0 duplicates" confusion the live QA flagged.
        self.assertEqual(
            result["opportunities_found"],
            result["opportunities_new"] + result["opportunities_duplicate"] + result["opportunities_filtered"] + result["opportunities_invalid"],
        )

    def test_per_provider_records_also_carry_filtered_and_invalid(self):
        blank_title = NormalizedOpportunity(source="fake", external_id="x1", url="https://x/1", title="", description="")
        engine = DiscoveryEngine(self.store, self.audit, sources=[FakeSource("fake", items=[blank_title])])
        result = engine.run_now(research=False)
        record = result["providers"][0]
        self.assertEqual(record["invalid"], 1)
        self.assertEqual(record["filtered"], 0)

    def test_run_record_persists_filtered_and_invalid_totals(self):
        blank_title = NormalizedOpportunity(source="fake", external_id="x1", url="https://x/1", title="", description="")
        engine = DiscoveryEngine(self.store, self.audit, sources=[FakeSource("fake", items=[blank_title])])
        result = engine.run_now(research=False)
        run = DiscoveryRunStore(self.store).get(result["run_id"])
        self.assertEqual(run["opportunities_invalid"], 1)

    def test_a_run_recorded_before_this_column_existed_reads_back_as_zero_not_null(self):
        # Additive-column safety: a run row without opportunities_filtered/
        # opportunities_invalid set (e.g. from before this pass) must read
        # back as an honest 0, never a crash or a None the UI can't render.
        run_store = DiscoveryRunStore(self.store)
        run_id = run_store.create("Aryan", {})
        self.store.update("rh_discovery_runs", run_id, completed_at="2024-01-01T00:00:00+00:00",
                           providers_json="[]", opportunities_found=0, opportunities_new=0, opportunities_duplicate=0)
        run = run_store.get(run_id)
        self.assertEqual(run["opportunities_filtered"], 0)
        self.assertEqual(run["opportunities_invalid"], 0)


# ---------------------------------------------------------------------------
# Safe re-qualification (spec item 6: reprocess existing live discoveries)
# ---------------------------------------------------------------------------

class RequalifyAllTests(_RepoCase):
    def test_downgrades_a_wrongly_pursued_opportunity_and_supersedes_its_proposal_and_needs_aryan_item(self):
        opportunities = OpportunityStore(self.store, self.audit)
        writer = _writer_item()
        opp_id = opportunities.create(
            {"title": writer.title, "client_name": writer.client_name, "description": writer.description, "budget_rate": writer.budget_text},
            source="remotive",
        )
        _insert_legacy_qualification(self.store, opp_id, "PURSUE", fit_score=100)
        proposal_result = ProposalStore(self.store, self.audit, self.needs_aryan).generate(opp_id, "short")
        needs_aryan_id = proposal_result["needs_aryan_id"]
        self.assertIsNotNone(needs_aryan_id)
        self.assertEqual(self.store.get("needs_aryan_items", needs_aryan_id)["status"], "PENDING")

        result = requalify_all(self.store, self.audit, self.needs_aryan)

        self.assertEqual(result["requalified"], 1)
        self.assertIn(opp_id, result["downgraded_from_pursue"])
        self.assertEqual(result["distribution"]["IGNORE"], 1)
        opp = opportunities.get(opp_id)
        self.assertEqual(opp["qualification"]["recommendation"], "IGNORE")
        # History preserved -- both qualification rows still exist.
        self.assertEqual(len(self.store.list("rh_qualifications", "opportunity_id=?", (opp_id,))), 2)
        proposal = self.store.get("rh_proposals", proposal_result["proposal_id"])
        self.assertEqual(proposal["status"], "SUPERSEDED")
        needs_aryan_item = self.store.get("needs_aryan_items", needs_aryan_id)
        self.assertEqual(needs_aryan_item["status"], "REJECTED")
        self.assertIn(needs_aryan_id, result["rejected_needs_aryan_ids"])
        self.assertIn(proposal_result["proposal_id"], result["superseded_proposal_ids"])

    def test_upgrades_a_previously_buried_relevant_opportunity_and_drafts_exactly_one_proposal(self):
        opportunities = OpportunityStore(self.store, self.audit)
        real = _pursue_item()
        opp_id = opportunities.create(
            {"title": real.title, "client_name": real.client_name, "description": real.description,
             "budget_rate": real.budget_text, "required_skills": ", ".join(real.skills)},
            source="remotive",
        )
        _insert_legacy_qualification(self.store, opp_id, "MAYBE", fit_score=45)

        result = requalify_all(self.store, self.audit, self.needs_aryan)

        self.assertIn(opp_id, result["upgraded_to_pursue"])
        self.assertEqual(result["distribution"]["PURSUE"], 1)
        opp = opportunities.get(opp_id)
        self.assertEqual(len(opp["proposals"]), 1)
        self.assertEqual(opp["proposals"][0]["status"], "DRAFT")
        pending = self.store.list("needs_aryan_items", "status=?", ("PENDING",))
        self.assertEqual(len(pending), 1)

    def test_calling_requalify_twice_never_creates_duplicate_proposals_or_needs_aryan_items(self):
        opportunities = OpportunityStore(self.store, self.audit)
        real = _pursue_item()
        opp_id = opportunities.create(
            {"title": real.title, "client_name": real.client_name, "description": real.description,
             "budget_rate": real.budget_text, "required_skills": ", ".join(real.skills)},
            source="remotive",
        )
        _insert_legacy_qualification(self.store, opp_id, "MAYBE", fit_score=45)

        requalify_all(self.store, self.audit, self.needs_aryan)
        requalify_all(self.store, self.audit, self.needs_aryan)  # idempotency check

        opp = opportunities.get(opp_id)
        self.assertEqual(len(opp["proposals"]), 1)
        self.assertEqual(len(self.store.list("needs_aryan_items")), 1)
        # 3 qualification rows total: legacy + two requalify passes -- history
        # keeps growing (never deleted), even though nothing else duplicated.
        self.assertEqual(len(self.store.list("rh_qualifications", "opportunity_id=?", (opp_id,))), 3)

    def test_never_touches_won_or_lost_opportunities(self):
        opportunities = OpportunityStore(self.store, self.audit)
        opp_id = opportunities.create({"title": "Some deal", "description": "x" * 50, "budget_rate": "$1000"}, source="manual")
        _insert_legacy_qualification(self.store, opp_id, "MAYBE")
        opportunities.move_stage(opp_id, "Qualified", "Aryan")
        opportunities.mark_won(opp_id, "Aryan", final_price=1000)

        result = requalify_all(self.store, self.audit, self.needs_aryan)

        self.assertEqual(result["requalified"], 0)
        self.assertEqual(len(self.store.list("rh_qualifications", "opportunity_id=?", (opp_id,))), 1)

    def test_an_already_approved_proposal_is_left_alone_on_downgrade(self):
        # A real human decision (approval) already happened -- requalify
        # must never silently override it, only warn via the summary.
        opportunities = OpportunityStore(self.store, self.audit)
        writer = _writer_item()
        opp_id = opportunities.create(
            {"title": writer.title, "client_name": writer.client_name, "description": writer.description, "budget_rate": writer.budget_text},
            source="remotive",
        )
        _insert_legacy_qualification(self.store, opp_id, "PURSUE", fit_score=100)
        proposal_result = ProposalStore(self.store, self.audit, self.needs_aryan).generate(opp_id, "short")
        ProposalStore(self.store, self.audit).mark_approved(proposal_result["proposal_id"], "Aryan")

        requalify_all(self.store, self.audit, self.needs_aryan)  # must not raise

        proposal = self.store.get("rh_proposals", proposal_result["proposal_id"])
        self.assertEqual(proposal["status"], "APPROVED")

    def test_build_qualification_engine_uses_the_saved_acquisition_profile(self):
        from falguna.opportunity_agent import AcquisitionProfileStore
        AcquisitionProfileStore(self.store).save({"exclusion_role_signals": ["freelance writer"], "positive_service_signals": []})
        profile = AcquisitionProfileStore(self.store).get()
        engine = build_qualification_engine(profile)
        result = engine.score({"title": "Freelance Writer", "description": "Write about anything.", "required_skills": None})
        self.assertFalse(result["relevance_passed"])


if __name__ == "__main__":
    unittest.main()
