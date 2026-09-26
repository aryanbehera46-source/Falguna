"""TTT Communications V2, Milestone 11 -- Digital Marketing Operations
Foundation.

The one real piece this milestone adds: linking a real inbound lead
(`comm_conversations`, Milestone 3's own ingestion) back to the real
marketing work that (evidence permitting) produced it -- campaign,
content item, and/or publication (`falguna/media.py`,
`falguna/publishing.py`, already-built TTT Media/Growth Engine v1).

This is deliberately the only new surface here. Campaign/objective/
audience/channel/asset/owner/status/approval/publish-target/analytics-
snapshot already exist and are already real (media_campaigns, the content
pipeline's own objective/platform/target_audience fields, the Approval/
Publish content states, `PublicationStore`'s approval-gated publish flow,
`AnalyticsStore`'s sourced metric records) -- building a second version of
any of them would be exactly the duplicate architecture this whole
mission has been told to avoid. `command_center.py`'s own media KPIs
already say the honest thing about this specific gap before this file
existed: `"leads_generated": {"value": None, "source": "not yet tracked
-- no media-to-lead attribution model exists yet"}`.

No inference, ever: `attribute()` requires a real, already-existing
conversation id, a real evidence string explaining *why* this lead is
believed to trace back to this campaign/content/publication (a customer's
own words, a matched reference code, a human's own read of the message --
never a guess), and at least one real, already-existing campaign/content/
publication id. An attribution with no evidence, or pointing at a record
that doesn't exist, is rejected outright -- the same "never fabricate"
posture as `resolve_ticket`'s required resolution note and
`AnalyticsStore.record`'s required source.

`campaign_summary()` reports only what is real: attributed lead/opportunity
counts and their evidence, and won-opportunity revenue actually linked to
this campaign through a real attribution row. It never invents a
follower/reach/click number -- those remain `AnalyticsStore`'s own
sourced rows, listed here for visibility, not recomputed or guessed.
"""

from typing import Any, Dict, List, Optional

from .audit import AuditLog
from .store import StateStore, utcnow

# Documented convention only (matches the channel list TTT's own mission
# text uses) -- not a hard-enforced enum. `media_content_items.platform`
# and `media_publications.platform` are free text in this codebase today
# (see falguna/media.py, falguna/publishing.py); conversion_source follows
# that same, already-established convention rather than inventing a new,
# stricter one just for this table.
SUGGESTED_CONVERSION_SOURCES = {
    "seo_content", "linkedin", "instagram", "facebook", "x", "youtube",
    "paid_ads", "email_marketing", "website_conversion", "referral", "direct_inbound", "unknown",
}


class AttributionError(ValueError):
    pass


class LeadAttributionStore:
    """Real, evidence-required links from a real inbound conversation to
    the real marketing record(s) believed responsible for it."""

    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit

    def _require(self, table: str, record_id: str, label: str) -> Dict[str, Any]:
        row = self.store.get(table, record_id)
        if not row:
            raise AttributionError(f"{label} not found: {record_id!r}")
        return row

    def attribute(
        self, conversation_id: str, conversion_source: str, evidence: str, actor: str,
        campaign_id: Optional[str] = None, content_id: Optional[str] = None,
        publication_id: Optional[str] = None, note: Optional[str] = None,
    ) -> Dict[str, Any]:
        self._require("comm_conversations", conversation_id, "conversation")
        if not conversion_source or not conversion_source.strip():
            raise AttributionError("conversion_source is required")
        if not evidence or not evidence.strip():
            raise AttributionError(
                "evidence is required -- an attribution without a real reason to believe it "
                "(the customer's own words, a matched reference, a human's own read of the message) "
                "is exactly the fabricated marketing analytics this mission forbids"
            )
        if not campaign_id and not content_id and not publication_id:
            raise AttributionError("at least one of campaign_id, content_id, or publication_id is required")
        if campaign_id:
            self._require("media_campaigns", campaign_id, "campaign")
        if content_id:
            self._require("media_content_items", content_id, "content item")
        if publication_id:
            self._require("media_publications", publication_id, "publication")

        now = utcnow()
        attribution_id = self.store.create("media_lead_attributions", {
            "conversation_id": conversation_id, "campaign_id": campaign_id, "content_id": content_id,
            "publication_id": publication_id, "conversion_source": conversion_source.strip(),
            "evidence": evidence.strip(), "note": note, "actor": actor, "created_at": now,
        })
        self.audit.append("MEDIA_LEAD_ATTRIBUTED", {
            "attribution_id": attribution_id, "conversation_id": conversation_id, "campaign_id": campaign_id,
            "content_id": content_id, "publication_id": publication_id, "conversion_source": conversion_source.strip(),
            "actor": actor,
        })
        return self.store.get("media_lead_attributions", attribution_id)

    def list_for_campaign(self, campaign_id: str) -> List[Dict[str, Any]]:
        return list(reversed(self.store.list("media_lead_attributions", "campaign_id=?", (campaign_id,))))

    def list_for_conversation(self, conversation_id: str) -> List[Dict[str, Any]]:
        return list(reversed(self.store.list("media_lead_attributions", "conversation_id=?", (conversation_id,))))

    def campaign_summary(self, campaign_id: str) -> Dict[str, Any]:
        """Real counts only -- content produced, publications by status,
        real (sourced) analytics rows if any exist, and attributed leads/
        opportunities/revenue traced through real `media_lead_attributions`
        rows. Every count that has no real data behind it is reported as
        `None` with a `*_source` string explaining why, never as a zero or
        a guess -- the same honest-gap convention `command_center.py`
        already uses everywhere else in this codebase."""
        campaign = self._require("media_campaigns", campaign_id, "campaign")
        content_items = self.store.list("media_content_items", "campaign_id=?", (campaign_id,))
        content_ids = [c["id"] for c in content_items]
        publications = [
            p for cid in content_ids for p in self.store.list("media_publications", "content_id=?", (cid,))
        ]
        publication_ids = [p["id"] for p in publications]
        analytics = [
            a for pid in publication_ids for a in self.store.list("media_analytics", "publication_id=?", (pid,))
        ]

        attributions = self.list_for_campaign(campaign_id)
        opportunities = []
        for attribution in attributions:
            conv = self.store.get("comm_conversations", attribution["conversation_id"])
            opp_id = conv.get("linked_opportunity_id") if conv else None
            if opp_id:
                opp = self.store.get("rh_opportunities", opp_id)
                if opp:
                    opportunities.append(opp)
        won = [o for o in opportunities if o.get("stage") == "Won"]
        revenue = sum(o["final_price"] for o in won if o.get("final_price") is not None)

        return {
            "campaign": campaign,
            "content_items_count": len(content_items),
            "publications_by_status": {
                status: len([p for p in publications if p["status"] == status])
                for status in sorted({p["status"] for p in publications})
            },
            "analytics_rows": analytics if analytics else None,
            "analytics_source": (
                "media_analytics, sourced rows only" if analytics
                else "no analytics recorded for this campaign's publications yet"
            ),
            "attributed_leads_count": len(attributions),
            "attributed_leads": attributions,
            "attributed_opportunities_count": len(opportunities),
            "attributed_opportunities_won": len(won),
            "attributed_revenue_won": round(revenue, 2) if won else None,
            "attributed_revenue_source": (
                "media_lead_attributions -> comm_conversations.linked_opportunity_id -> "
                "rh_opportunities (stage=Won, final_price)" if won
                else "no attributed lead has reached a Won opportunity yet"
            ),
        }
