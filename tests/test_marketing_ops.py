"""TTT Communications V2, Milestone 11 -- Digital Marketing Operations
Foundation.

Focused tests for the one real addition this milestone makes:
`LeadAttributionStore` (falguna/marketing_ops.py), linking a real inbound
conversation to the real marketing campaign/content/publication believed
responsible for it -- never fabricated, never pointing at a record that
doesn't exist. Also covers the real wiring of `command_center.py`'s own
`leads_generated` KPI to this real data.

Real temp SQLite DBs, no mocking -- same convention as every other test
file in this repo.
"""

import tempfile
import unittest
from pathlib import Path

from falguna.audit import AuditLog
from falguna.command_center import kpi_snapshot
from falguna.comms import CommsStore
from falguna.marketing_ops import AttributionError, LeadAttributionStore
from falguna.media import BrandStore, CampaignStore, ContentStore
from falguna.publishing import PublicationStore
from falguna.revenue_hunter import OpportunityStore
from falguna.store import StateStore
from falguna.ttt_hq import NeedsAryanQueue


class _MarketingOpsCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = StateStore(root / "state.db")
        self.store.migrate()
        self.audit = AuditLog(root / "audit.jsonl")
        self.needs_aryan = NeedsAryanQueue(self.store, self.audit)
        self.comms = CommsStore(self.store, self.audit, self.needs_aryan)
        self.brands = BrandStore(self.store, self.audit)
        self.campaigns = CampaignStore(self.store, self.audit)
        self.content = ContentStore(self.store, self.audit)
        self.publications = PublicationStore(self.store, self.audit)
        self.opportunities = OpportunityStore(self.store, self.audit)
        self.attributions = LeadAttributionStore(self.store, self.audit)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def _campaign(self):
        brand_id = self.brands.create("TTT", voice_tone="confident", audience="founders",
                                       platforms=["instagram"], content_pillars=["AI"], actor="Aryan")
        campaign_id = self.campaigns.create(brand_id, "TEST-TRIAL launch campaign", objective="awareness", actor="Aryan")
        return brand_id, campaign_id

    def _conversation(self):
        conv = self.comms.open_conversation("EMAIL", "sales", subject="Saw your Instagram post", actor="website")
        return conv["id"]


class AttributionValidationTests(_MarketingOpsCase):
    def test_rejects_a_conversation_that_does_not_exist(self):
        _, campaign_id = self._campaign()
        with self.assertRaises(AttributionError):
            self.attributions.attribute(
                "does-not-exist", "instagram", "customer said they saw our Instagram post", "Aryan",
                campaign_id=campaign_id,
            )

    def test_rejects_empty_evidence(self):
        _, campaign_id = self._campaign()
        conv_id = self._conversation()
        with self.assertRaises(AttributionError):
            self.attributions.attribute(conv_id, "instagram", "   ", "Aryan", campaign_id=campaign_id)

    def test_rejects_empty_conversion_source(self):
        _, campaign_id = self._campaign()
        conv_id = self._conversation()
        with self.assertRaises(AttributionError):
            self.attributions.attribute(conv_id, "", "customer mentioned it", "Aryan", campaign_id=campaign_id)

    def test_rejects_with_no_marketing_record_referenced(self):
        conv_id = self._conversation()
        with self.assertRaises(AttributionError):
            self.attributions.attribute(conv_id, "instagram", "customer mentioned it", "Aryan")

    def test_rejects_a_campaign_that_does_not_exist(self):
        conv_id = self._conversation()
        with self.assertRaises(AttributionError):
            self.attributions.attribute(conv_id, "instagram", "customer mentioned it", "Aryan", campaign_id="does-not-exist")

    def test_rejects_a_content_item_that_does_not_exist(self):
        conv_id = self._conversation()
        with self.assertRaises(AttributionError):
            self.attributions.attribute(conv_id, "instagram", "customer mentioned it", "Aryan", content_id="does-not-exist")

    def test_rejects_a_publication_that_does_not_exist(self):
        conv_id = self._conversation()
        with self.assertRaises(AttributionError):
            self.attributions.attribute(conv_id, "instagram", "customer mentioned it", "Aryan", publication_id="does-not-exist")


class AttributionRecordingTests(_MarketingOpsCase):
    def test_a_real_attribution_is_recorded_with_its_evidence(self):
        _, campaign_id = self._campaign()
        conv_id = self._conversation()
        row = self.attributions.attribute(
            conv_id, "instagram", "customer's own words: 'saw your Instagram post about the booking platform'",
            "Aryan", campaign_id=campaign_id, note="TEST-TRIAL",
        )
        self.assertEqual(row["conversation_id"], conv_id)
        self.assertEqual(row["campaign_id"], campaign_id)
        self.assertIn("saw your Instagram post", row["evidence"])
        self.assertEqual(row["conversion_source"], "instagram")

    def test_list_for_campaign_and_for_conversation(self):
        _, campaign_id = self._campaign()
        conv_id = self._conversation()
        self.attributions.attribute(conv_id, "instagram", "customer said so directly", "Aryan", campaign_id=campaign_id)
        by_campaign = self.attributions.list_for_campaign(campaign_id)
        by_conversation = self.attributions.list_for_conversation(conv_id)
        self.assertEqual(len(by_campaign), 1)
        self.assertEqual(len(by_conversation), 1)
        self.assertEqual(by_campaign[0]["id"], by_conversation[0]["id"])

    def test_attribution_can_reference_a_content_item_or_publication_instead_of_a_campaign(self):
        brand_id, campaign_id = self._campaign()
        content_id = self.content.create(brand_id, "Booking platform reel", "short_form_video",
                                          campaign_id=campaign_id, platform="instagram", actor="Aryan")
        pub_id = self.publications.create(content_id, "instagram", actor="Aryan")
        conv_id = self._conversation()
        row = self.attributions.attribute(
            conv_id, "instagram", "matched the link-in-bio reference code from this exact reel", "Aryan",
            content_id=content_id, publication_id=pub_id,
        )
        self.assertEqual(row["content_id"], content_id)
        self.assertEqual(row["publication_id"], pub_id)


class CampaignSummaryTests(_MarketingOpsCase):
    def test_summary_reports_no_analytics_honestly_rather_than_a_zero(self):
        _, campaign_id = self._campaign()
        summary = self.attributions.campaign_summary(campaign_id)
        self.assertIsNone(summary["analytics_rows"])
        self.assertIn("no analytics recorded", summary["analytics_source"])
        self.assertEqual(summary["attributed_leads_count"], 0)
        self.assertIsNone(summary["attributed_revenue_won"])

    def test_summary_counts_real_attributed_leads_and_won_revenue(self):
        brand_id, campaign_id = self._campaign()
        opp_id = self.opportunities.create({"title": "Booking platform rebuild"}, actor="Aryan")
        conv = self.comms.open_conversation(
            "EMAIL", "sales", subject="Saw your Instagram post", actor="website", linked_opportunity_id=opp_id,
        )
        self.attributions.attribute(
            conv["id"], "instagram", "customer said they saw our Instagram post", "Aryan", campaign_id=campaign_id,
        )
        self.opportunities.mark_won(opp_id, "Aryan", final_price=4000.0)

        summary = self.attributions.campaign_summary(campaign_id)
        self.assertEqual(summary["attributed_leads_count"], 1)
        self.assertEqual(summary["attributed_opportunities_count"], 1)
        self.assertEqual(summary["attributed_opportunities_won"], 1)
        self.assertEqual(summary["attributed_revenue_won"], 4000.0)

    def test_an_attributed_lead_that_has_not_won_yet_shows_no_revenue(self):
        brand_id, campaign_id = self._campaign()
        opp_id = self.opportunities.create({"title": "Booking platform rebuild"}, actor="Aryan")
        conv = self.comms.open_conversation(
            "EMAIL", "sales", subject="Saw your Instagram post", actor="website", linked_opportunity_id=opp_id,
        )
        self.attributions.attribute(
            conv["id"], "instagram", "customer said they saw our Instagram post", "Aryan", campaign_id=campaign_id,
        )
        summary = self.attributions.campaign_summary(campaign_id)
        self.assertEqual(summary["attributed_leads_count"], 1)
        self.assertEqual(summary["attributed_opportunities_won"], 0)
        self.assertIsNone(summary["attributed_revenue_won"])


class CampaignOwnerTests(_MarketingOpsCase):
    def test_owner_defaults_to_none_and_can_be_set(self):
        _, campaign_id = self._campaign()
        self.assertIsNone(self.campaigns.get(campaign_id)["owner"])
        self.campaigns.set_owner(campaign_id, "Aryan", actor="Aryan")
        self.assertEqual(self.campaigns.get(campaign_id)["owner"], "Aryan")

    def test_set_owner_rejects_blank(self):
        _, campaign_id = self._campaign()
        from falguna.media import MediaError
        with self.assertRaises(MediaError):
            self.campaigns.set_owner(campaign_id, "  ", actor="Aryan")


class CommandCenterWiringTests(_MarketingOpsCase):
    def test_leads_generated_kpi_is_zero_not_none_once_the_model_exists(self):
        kpis = kpi_snapshot(self.store)
        self.assertEqual(kpis["media"]["leads_generated"]["value"], 0)
        self.assertNotIn("not yet tracked", kpis["media"]["leads_generated"]["source"])

    def test_leads_generated_kpi_reflects_a_real_attribution_in_window(self):
        _, campaign_id = self._campaign()
        conv_id = self._conversation()
        self.attributions.attribute(conv_id, "instagram", "customer said so directly", "Aryan", campaign_id=campaign_id)
        kpis = kpi_snapshot(self.store)
        self.assertEqual(kpis["media"]["leads_generated"]["value"], 1)


if __name__ == "__main__":
    unittest.main()
