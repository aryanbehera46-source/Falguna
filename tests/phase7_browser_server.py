"""Synthetic local Phase 7 browser fixture. Never uses live data or providers."""
import os
import tempfile
from http.server import ThreadingHTTPServer
from pathlib import Path

from falguna.audit import AuditLog
from falguna.billing import BillingStore
from falguna.commercial import ProjectStore
from falguna.comms import CommsStore
from falguna.ecosystem import EcosystemService
from falguna.partner_management import PartnerStore, ReferralStore, CommissionStore
from falguna.phase6_commercial import CommercialIdentityStore
from falguna.phase7_portals import ExternalPortalAuth
from falguna.revenue_hunter import OpportunityStore
from falguna.runtime import open_control_plane
from falguna.sales_ops import ClientStore
from falguna.site_web import SiteHandler


root = Path(tempfile.mkdtemp(prefix="phase7-browser-"))
os.system(f"git init -q -b main {root}")
(root / "README.md").write_text("synthetic phase 7 browser fixture\n")
control, store = open_control_plane(root)
audit = control.audit
comms, clients, opps = CommsStore(store, audit), ClientStore(store, audit), OpportunityStore(store, audit)
org = comms.find_or_create_organization("Browser Customer", domain="browser.invalid")
client = clients.upsert("Browser Customer", "browser-test")
store.update("comm_organizations", org, linked_client_id=client)
contact = comms.find_or_create_contact("customer@example.test", name="Browser Customer", organization_id=org)
conversation = comms.open_conversation("EMAIL", "support", subject="Browser support", contact_id=contact, organization_id=org, actor="test", linked_client_id=client)
comms.add_message(conversation["id"], "INBOUND", "Browser support history", actor="test")
opp = opps.create({"title": "Browser project", "description": "Synthetic", "client_name": "Browser Customer"}, "test")
project = ProjectStore(store, audit).create_for_opportunity(opp, client, "test", "CUSTOM_BUILD")
invoice = BillingStore(store, audit).create_invoice(client, "test", 1200, "INR", milestone="Browser milestone")
identity = CommercialIdentityStore(store, audit).create("ttt", "CUSTOMER_ORG", org, "Browser Customer", "CUSTOMER", "test")
ExternalPortalAuth(store).provision(identity, "customer@example.test", "correct-horse-battery-customer")

partners = PartnerStore(store, audit)
partner = partners.register({"full_name": "Browser Partner", "email": "partner@example.test", "agreement_accepted": True}, "test")
partners.approve(partner, "test")
partners.acknowledge_policy(partner, "test")
referral = ReferralStore(store, audit).register(partner, {"prospect_name": "Browser Lead", "requested_service": "Software"}, "test")
CommissionStore(store, audit).ensure_for_referral(referral, "test")
now = store.get("pm_referrals", referral)["created_at"]
store.create("p6_partner_contributions", {"organization_id": "ttt", "partner_id": partner, "referral_id": referral, "opportunity_id": None, "contribution_type": "INTRODUCTION", "evidence_json": "{}", "status": "RECORDED", "actor": "test", "created_at": now, "updated_at": now})
partner_identity = CommercialIdentityStore(store, audit).create("ttt", "PARTNER", partner, "Browser Partner", "PARTNER", "test")
ExternalPortalAuth(store).provision(partner_identity, "partner@example.test", "correct-horse-battery-partner")

# Phase 8 synthetic opportunity/feed fixture: human-reviewed route, evidenced
# profile and deterministic reasoned match. No external provider or real data.
ecosystem = EcosystemService(store, audit)
intake = ecosystem.create_intake(
    organization_id="ttt", customer_ref=org,
    original_message="Need a bilingual workflow for two synthetic shops",
    normalized_meaning="Customer needs a bilingual workflow for two synthetic retail locations",
    geography="India", preferred_language="Hindi", category="AI_AUTOMATION",
)
route = ecosystem.recommend_route(intake, "PARTNER_OR_SPECIALIST_COORDINATION",
                                  ["evidenced specialist capability is required"],
                                  [{"fixture": "synthetic browser evidence"}], [])
ecosystem.decide_route(route, "PARTNER_OR_SPECIALIST_COORDINATION", "test-owner", "Synthetic fixture review")
profile = ecosystem.create_profile(
    organization_id="ttt", profile_type="DELIVERY_SPECIALIST", display_name="Browser Partner",
    partner_id=partner, capabilities=[{"tag": "workflow-automation", "evidence_status": "EVIDENCE_REVIEWED", "evidence": [{"fixture": True}]}],
    geography=["India"], languages=["Hindi"], verification_level="EVIDENCE_REVIEWED",
    verification_evidence=[{"fixture": True}],
)
opportunity = ecosystem.publish_opportunity(
    intake, customer_safe_brief="Bilingual retail workflow implementation",
    capability_requirements=["workflow-automation"], required_verification_level="EVIDENCE_REVIEWED",
    actor_identity_id="test-owner",
)
ecosystem.evaluate_match(opportunity, profile)

server = ThreadingHTTPServer(("127.0.0.1", 8878), SiteHandler)
server.app_root = root
try:
    server.serve_forever()
finally:
    store.close()
