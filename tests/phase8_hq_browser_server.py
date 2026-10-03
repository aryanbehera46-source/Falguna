"""Synthetic local Phase 8 HQ browser fixture. No live data or providers."""
import subprocess
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

from falguna.ecosystem import EcosystemService
from falguna.hq_web import TTTHQHandler
from falguna.phase6_commercial import CommercialIdentityStore
from falguna.runtime import open_control_plane
from falguna.site_auth import StaffAuthService
from falguna.site_web import SiteHandler
from falguna.store import utcnow

root = Path(tempfile.mkdtemp(prefix="phase8-hq-browser-"))
subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
control, store = open_control_plane(root)
auth = StaffAuthService(store)
user = auth.create_user("owner@example.test", "Synthetic Owner", "correct-horse-battery-owner", "staff")
CommercialIdentityStore(store, control.audit).create("ttt", "STAFF_USER", user, "Synthetic Owner", "OWNER", "fixture")
service = EcosystemService(store, control.audit)

pending_intake = service.create_intake(organization_id="ttt", customer_ref="synthetic-customer",
    original_message="Need a bilingual ordering workflow", normalized_meaning="Bilingual retail automation request",
    source_language="Hindi", geography="India", preferred_language="Hindi", category="AI_AUTOMATION", industry="Retail",
    risk_flags=["scope_unconfirmed"], clarification_questions=["Which POS is in use?"])
service.recommend_route(pending_intake, "PARTNER_OR_SPECIALIST_COORDINATION", ["specialist capability required"],
    [{"kind": "intake", "source": "synthetic"}], ["integration scope is unconfirmed"])
second_pending = service.create_intake(organization_id="ttt", original_message="Need a second synthetic advisory review",
    normalized_meaning="Advisory request awaiting owner review", category="ADVISORY", industry="Services")
service.recommend_route(second_pending, "TTT_ADVISORY", ["discovery required"], [{"kind": "fixture"}], [])
routed_intake = service.create_intake(organization_id="ttt", original_message="Launch a synthetic local service",
    normalized_meaning="Business launch and growth engagement", category="BUSINESS_LAUNCH", industry="Services")
route = service.recommend_route(routed_intake, "BUSINESS_LAUNCH_AND_GROWTH", ["end-to-end launch need"],
    [{"kind": "fixture"}], [])
service.decide_route(route, "BUSINESS_LAUNCH_AND_GROWTH", "fixture-owner", "Reviewed for staged launch workflow")
profile = service.create_profile(organization_id="ttt", profile_type="DELIVERY_SPECIALIST", display_name="Synthetic Specialist",
    capabilities=[{"tag": "workflow-automation", "evidence_status": "EVIDENCE_REVIEWED", "evidence": [{"fixture": True}]}],
    geography=["India"], languages=["Hindi"], verification_level="EVIDENCE_REVIEWED",
    verification_evidence=[{"kind": "portfolio", "fixture": True}], commercial_relationship="TTT_COORDINATED")
opportunity = service.publish_opportunity(routed_intake, customer_safe_brief="Synthetic launch workflow delivery",
    capability_requirements=["workflow-automation"], required_verification_level="EVIDENCE_REVIEWED", actor_identity_id="fixture-owner")
match = service.evaluate_match(opportunity, profile)
application = service.apply(opportunity, profile, "Available for the reviewed synthetic scope", "No conflict")
blg = service.start_blg(routed_intake, "STARTUP_SMB", ["regulated work requires a qualified professional"])
governance = service.create_governance_event("ttt", "UNAUTHORIZED_PAYMENT_INSTRUCTION", "HIGH",
    [{"kind": "synthetic message"}], "fixture-owner", profile, opportunity)
now = utcnow()
store.create("p8_product_signals", {"organization_id": "ttt", "problem_signature": "repeated workflow setup",
    "supporting_intake_ids_json": f'["{pending_intake}","{routed_intake}"]', "signal_type": "PRODUCTIZED_SERVICE",
    "evidence_count": 2, "recommendation_only": 1, "status": "PENDING_HUMAN_REVIEW", "created_at": now, "updated_at": now})
store.close()

site = ThreadingHTTPServer(("127.0.0.1", 8881), SiteHandler); site.app_root = root
hq = ThreadingHTTPServer(("127.0.0.1", 8882), TTTHQHandler); hq.app_root = root; hq.falguna_url = "http://127.0.0.1:1"
threading.Thread(target=site.serve_forever, daemon=True).start()
try:
    hq.serve_forever()
finally:
    site.shutdown(); site.server_close(); hq.server_close()
