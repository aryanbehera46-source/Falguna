"""End-to-end HTTP tests for the new TTT Autonomous Company Loop v1 routes
in falguna/hq_web.py: lifecycle history/transition, application executor,
sales policy, negotiation guardrails, and closing -- all through the real
HTTP layer, on a real (temp, disposable) repo/state.db, exactly like the
existing live-server tests in test_hq_web.py."""

import threading
import time
import unittest
import urllib.error
from http.server import ThreadingHTTPServer

from falguna.hq_web import PRODUCT_NAME, TTTHQHandler
from tests.test_hq_web import _LiveServerCase


class LifecycleRoutesTests(_LiveServerCase):
    hq_port = 8811

    def setUp(self):
        super().setUp()
        self.server = ThreadingHTTPServer(("127.0.0.1", self.hq_port), TTTHQHandler)
        self.server.app_root = self.repo
        self.server.falguna_url = "http://127.0.0.1:8765"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self._wait_ready()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        super().tearDown()

    def _wait_ready(self):
        for _ in range(40):
            try:
                status, body = self._get(self.hq_port, "/api/config")
                if body.get("product") == PRODUCT_NAME:
                    return
            except Exception:
                pass
            time.sleep(0.05)
        self.fail("TTT HQ server did not become ready")

    def _create_opportunity(self, title="Build a CRM dashboard", source_url="https://example.com/jobs/1"):
        status, out = self._post(self.hq_port, "/api/rh/opportunities", {
            "title": title, "description": "Build a small CRM dashboard.", "source_url": source_url, "actor": "Aryan",
        })
        self.assertEqual(status, 201)
        return out["opportunity_id"]

    def test_creating_an_opportunity_initializes_lifecycle_discovered(self):
        opp_id = self._create_opportunity()
        status, out = self._get(self.hq_port, f"/api/rh/opportunities/{opp_id}/lifecycle")
        self.assertEqual(status, 200)
        self.assertEqual(out["current_state"], "DISCOVERED")
        self.assertEqual(out["history"][0]["to_state"], "DISCOVERED")

    def test_lifecycle_route_404s_for_unknown_opportunity(self):
        code = self._get_raises(self.hq_port, "/api/rh/opportunities/does-not-exist/lifecycle")
        self.assertEqual(code, 404)

    def test_qualify_advances_lifecycle_to_qualified(self):
        opp_id = self._create_opportunity()
        status, _ = self._post(self.hq_port, f"/api/rh/opportunities/{opp_id}/qualify", {"actor": "Aryan"})
        self.assertEqual(status, 201)
        _, out = self._get(self.hq_port, f"/api/rh/opportunities/{opp_id}/lifecycle")
        self.assertEqual(out["current_state"], "QUALIFIED")

    def test_explicit_transition_route_advances_state(self):
        opp_id = self._create_opportunity()
        status, event = self._post(self.hq_port, f"/api/rh/opportunities/{opp_id}/lifecycle/transition", {
            "to_state": "RESEARCHING", "actor": "Aryan", "reason": "starting research",
        })
        self.assertEqual(status, 201)
        self.assertEqual(event["to_state"], "RESEARCHING")

    def test_explicit_transition_route_rejects_illegal_jump(self):
        opp_id = self._create_opportunity()
        code = self._post_raises(f"/api/rh/opportunities/{opp_id}/lifecycle/transition", {"to_state": "WON", "actor": "Aryan"})
        self.assertEqual(code, 400)

    def _pipeline_to_proposal_awaiting_approval(self):
        opp_id = self._create_opportunity()
        self._post(self.hq_port, f"/api/rh/opportunities/{opp_id}/qualify", {"actor": "Aryan"})
        status, proposal_out = self._post(self.hq_port, f"/api/rh/opportunities/{opp_id}/proposals", {"kind": "short", "actor": "Aryan"})
        self.assertEqual(status, 201)
        return opp_id, proposal_out["proposal_id"]

    def test_full_pipeline_to_proposal_reaches_awaiting_approval(self):
        opp_id, _ = self._pipeline_to_proposal_awaiting_approval()
        _, out = self._get(self.hq_port, f"/api/rh/opportunities/{opp_id}/lifecycle")
        self.assertEqual(out["current_state"], "AWAITING_APPROVAL")

    def _pipeline_to_approved(self):
        opp_id, proposal_id = self._pipeline_to_proposal_awaiting_approval()
        status, items = self._get(self.hq_port, "/api/needs-aryan")
        needs_aryan_id = next(i["id"] for i in items["items"] if i.get("ref_id") == proposal_id)
        status, _ = self._post(self.hq_port, f"/api/needs-aryan/{needs_aryan_id}/decision", {"action": "approve", "actor": "Aryan"})
        self.assertEqual(status, 200)
        return opp_id, proposal_id

    def test_approving_the_proposal_advances_lifecycle_to_approved(self):
        opp_id, _ = self._pipeline_to_approved()
        _, out = self._get(self.hq_port, f"/api/rh/opportunities/{opp_id}/lifecycle")
        self.assertEqual(out["current_state"], "APPROVED")

    def test_apply_with_default_channel_blocks_and_creates_needs_aryan(self):
        opp_id, proposal_id = self._pipeline_to_approved()
        _, before = self._get(self.hq_port, "/api/needs-aryan")
        status, result = self._post(self.hq_port, f"/api/rh/opportunities/{opp_id}/apply", {"proposal_id": proposal_id, "actor": "Aryan"})
        self.assertEqual(status, 201)
        self.assertEqual(result["status"], "BLOCKED")
        _, after = self._get(self.hq_port, "/api/needs-aryan")
        self.assertEqual(len(after["items"]), len(before["items"]) + 1)

    def test_apply_with_simulated_channel_reaches_contacted(self):
        opp_id, proposal_id = self._pipeline_to_approved()
        status, result = self._post(self.hq_port, f"/api/rh/opportunities/{opp_id}/apply", {
            "proposal_id": proposal_id, "actor": "Aryan", "channel": "simulated_test", "allow_simulated": True,
        })
        self.assertEqual(status, 201)
        self.assertEqual(result["status"], "SENT")
        _, out = self._get(self.hq_port, f"/api/rh/opportunities/{opp_id}/lifecycle")
        self.assertEqual(out["current_state"], "CONTACTED")

    def test_sales_policy_get_returns_defaults(self):
        status, policy = self._get(self.hq_port, "/api/rh/sales-policy")
        self.assertEqual(status, 200)
        self.assertEqual(policy["allowed_currencies"], ["USD"])

    def test_sales_policy_post_saves_and_persists(self):
        status, policy = self._post(self.hq_port, "/api/rh/sales-policy", {"min_project_price": 750})
        self.assertEqual(status, 200)
        self.assertEqual(policy["min_project_price"], 750)
        _, refetched = self._get(self.hq_port, "/api/rh/sales-policy")
        self.assertEqual(refetched["min_project_price"], 750)

    def test_negotiation_evaluate_within_policy(self):
        self._post(self.hq_port, "/api/rh/sales-policy", {"min_project_price": 500})
        opp_id = self._create_opportunity()
        status, result = self._post(self.hq_port, f"/api/rh/opportunities/{opp_id}/negotiation/evaluate", {"actor": "Aryan", "price": 1000})
        self.assertEqual(status, 201)
        self.assertTrue(result["within_policy"])

    def test_negotiation_evaluate_out_of_policy_escalates(self):
        self._post(self.hq_port, "/api/rh/sales-policy", {"min_project_price": 5000})
        opp_id = self._create_opportunity()
        _, before = self._get(self.hq_port, "/api/needs-aryan")
        status, result = self._post(self.hq_port, f"/api/rh/opportunities/{opp_id}/negotiation/evaluate", {"actor": "Aryan", "price": 1000})
        self.assertEqual(status, 201)
        self.assertFalse(result["within_policy"])
        self.assertIsNotNone(result["needs_aryan_id"])
        _, after = self._get(self.hq_port, "/api/needs-aryan")
        self.assertEqual(len(after["items"]), len(before["items"]) + 1)

    def _configure_sales_policy(self):
        # Commercial safety default: closing only executes immediately once
        # the Sales Policy has been deliberately configured.
        self._post(self.hq_port, "/api/rh/sales-policy", {})

    def test_close_with_unconfigured_policy_awaits_approval_over_http(self):
        opp_id = self._create_opportunity()
        status, result = self._post(self.hq_port, f"/api/rh/opportunities/{opp_id}/close", {
            "actor": "Aryan", "client_name": "Acme Corp", "final_price": 3000, "currency": "USD",
        })
        self.assertEqual(status, 201)
        self.assertEqual(result["status"], "AWAITING_APPROVAL")
        _, opp = self._get(self.hq_port, f"/api/rh/opportunities/{opp_id}")
        self.assertEqual(opp["stage"], "New")

    def test_approving_the_closing_package_over_http_executes_the_real_close(self):
        opp_id = self._create_opportunity()
        _, prepared = self._post(self.hq_port, f"/api/rh/opportunities/{opp_id}/close", {
            "actor": "Aryan", "client_name": "Acme Corp", "final_price": 3000, "currency": "USD",
        })
        status, _ = self._post(self.hq_port, f"/api/needs-aryan/{prepared['needs_aryan_id']}/decision", {"action": "approve", "actor": "Aryan"})
        self.assertEqual(status, 200)
        _, opp = self._get(self.hq_port, f"/api/rh/opportunities/{opp_id}")
        self.assertEqual(opp["stage"], "Won")
        _, clients = self._get(self.hq_port, "/api/rh/clients")
        self.assertTrue(any(c["name"] == "Acme Corp" for c in clients["items"]))

    def test_close_marks_opportunity_won_and_creates_client_and_active_job(self):
        self._configure_sales_policy()
        opp_id = self._create_opportunity()
        status, result = self._post(self.hq_port, f"/api/rh/opportunities/{opp_id}/close", {
            "actor": "Aryan", "client_name": "Acme Corp", "final_price": 3000, "currency": "USD",
        })
        self.assertEqual(status, 201)
        self.assertEqual(result["status"], "CLOSED")
        self.assertIn("client_id", result)
        self.assertIn("active_job_id", result)
        _, opp = self._get(self.hq_port, f"/api/rh/opportunities/{opp_id}")
        self.assertEqual(opp["stage"], "Won")
        _, clients = self._get(self.hq_port, "/api/rh/clients")
        self.assertTrue(any(c["name"] == "Acme Corp" for c in clients["items"]))
        _, active_jobs = self._get(self.hq_port, "/api/rh/active-jobs")
        self.assertTrue(any(j["opportunity_id"] == opp_id for j in active_jobs["items"]))

    def test_close_is_idempotent_over_http(self):
        self._configure_sales_policy()
        opp_id = self._create_opportunity()
        status1, result1 = self._post(self.hq_port, f"/api/rh/opportunities/{opp_id}/close", {"actor": "Aryan", "client_name": "Acme Corp", "final_price": 1000})
        status2, result2 = self._post(self.hq_port, f"/api/rh/opportunities/{opp_id}/close", {"actor": "Aryan", "client_name": "Acme Corp", "final_price": 1000})
        self.assertEqual(status1, 201)
        self.assertEqual(status2, 201)
        self.assertFalse(result1["already_closed"])
        self.assertTrue(result2["already_closed"])

    def test_get_single_client(self):
        self._configure_sales_policy()
        opp_id = self._create_opportunity()
        _, result = self._post(self.hq_port, f"/api/rh/opportunities/{opp_id}/close", {"actor": "Aryan", "client_name": "Acme Corp", "final_price": 500})
        status, client = self._get(self.hq_port, f"/api/rh/clients/{result['client_id']}")
        self.assertEqual(status, 200)
        self.assertEqual(client["name"], "Acme Corp")

    def test_get_unknown_client_404s(self):
        code = self._get_raises(self.hq_port, "/api/rh/clients/does-not-exist")
        self.assertEqual(code, 404)

    def test_manual_won_route_updates_lifecycle_state_once_the_chain_was_walked(self):
        opp_id = self._create_opportunity()
        for state in ["RESEARCHING", "QUALIFIED", "PITCH_READY", "AWAITING_APPROVAL",
                      "APPROVED", "APPLYING", "CONTACTED", "REPLIED", "NEGOTIATING"]:
            self._post(self.hq_port, f"/api/rh/opportunities/{opp_id}/lifecycle/transition", {"to_state": state, "actor": "Aryan"})
        self._post(self.hq_port, f"/api/rh/opportunities/{opp_id}/won", {"actor": "Aryan", "final_price": 1200})
        _, out = self._get(self.hq_port, f"/api/rh/opportunities/{opp_id}/lifecycle")
        self.assertEqual(out["current_state"], "WON")

    def test_manual_lost_route_updates_lifecycle_state_from_a_reachable_state(self):
        opp_id = self._create_opportunity()
        self._post(self.hq_port, f"/api/rh/opportunities/{opp_id}/lifecycle/transition", {"to_state": "RESEARCHING", "actor": "Aryan"})
        self._post(self.hq_port, f"/api/rh/opportunities/{opp_id}/lost", {"actor": "Aryan", "reason": "client went dark"})
        _, out = self._get(self.hq_port, f"/api/rh/opportunities/{opp_id}/lifecycle")
        self.assertEqual(out["current_state"], "LOST")

    def test_manual_won_route_never_fakes_a_jump_it_cannot_legitimately_make(self):
        # No lifecycle initialization/walk at all -- the coarse `stage` field
        # still moves to "Won" via the older, tested mark_won path (below),
        # but the finer lifecycle_state correctly refuses the illegal
        # DISCOVERED -> WON jump rather than silently pretending it happened.
        # This is the "no silent state jumps" rule working as intended, not
        # a bug: a caller that skips the real pipeline stages only gets the
        # coarse stage updated, not a fabricated fine-grained history.
        opp_id = self._create_opportunity()
        status, opp = self._post(self.hq_port, f"/api/rh/opportunities/{opp_id}/won", {"actor": "Aryan", "final_price": 1200})
        self.assertEqual(status, 200)
        self.assertEqual(opp["stage"], "Won")
        _, out = self._get(self.hq_port, f"/api/rh/opportunities/{opp_id}/lifecycle")
        self.assertEqual(out["current_state"], "DISCOVERED")

    def _post_raises(self, path, body):
        try:
            self._post(self.hq_port, path, body)
            return None
        except urllib.error.HTTPError as e:
            return e.code

    # -- Conversation Inbox / Reply Agent (Pass B, Section 6) --

    def test_record_inbound_conversation_message_over_http(self):
        opp_id = self._create_opportunity()
        status, result = self._post(self.hq_port, f"/api/rh/opportunities/{opp_id}/conversations", {
            "channel": "email", "body": "Sounds good, let's move forward.", "sender": "client@example.com",
        })
        self.assertEqual(status, 201)
        self.assertEqual(result["intent"], "interested")
        self.assertIsNone(result["needs_aryan_id"])

    def test_record_inbound_unknown_opportunity_is_rejected(self):
        # ConversationError is a ValueError, so it's caught by the generic
        # handler and returned as 400 (not 404) -- same as every other
        # ValueError-raising route in this handler.
        code = self._post_raises("/api/rh/opportunities/does-not-exist/conversations", {"channel": "email", "body": "hello"})
        self.assertEqual(code, 400)

    def test_record_inbound_with_sensitive_content_escalates_to_needs_aryan_over_http(self):
        opp_id = self._create_opportunity()
        _, before = self._get(self.hq_port, "/api/needs-aryan")
        status, result = self._post(self.hq_port, f"/api/rh/opportunities/{opp_id}/conversations", {
            "channel": "email", "body": "My attorney says we may pursue legal action.",
        })
        self.assertEqual(status, 201)
        self.assertIsNotNone(result["needs_aryan_id"])
        _, after = self._get(self.hq_port, "/api/needs-aryan")
        self.assertEqual(len(after["items"]), len(before["items"]) + 1)

    def test_list_conversations_for_opportunity_over_http(self):
        opp_id = self._create_opportunity()
        self._post(self.hq_port, f"/api/rh/opportunities/{opp_id}/conversations", {"channel": "email", "body": "Sounds good, let's move forward."})
        status, out = self._get(self.hq_port, f"/api/rh/opportunities/{opp_id}/conversations")
        self.assertEqual(status, 200)
        self.assertEqual(len(out["items"]), 1)
        self.assertEqual(out["items"][0]["direction"], "INBOUND")

    def test_draft_reply_over_http(self):
        opp_id = self._create_opportunity()
        _, inbound = self._post(self.hq_port, f"/api/rh/opportunities/{opp_id}/conversations", {"channel": "email", "body": "Sounds good, let's move forward."})
        status, drafted = self._post(self.hq_port, f"/api/rh/conversations/{inbound['message_id']}/draft-reply", {"actor": "Aryan"})
        self.assertEqual(status, 201)
        self.assertIn("Build a CRM dashboard", drafted["body"])
        status, out = self._get(self.hq_port, f"/api/rh/opportunities/{opp_id}/conversations")
        self.assertEqual(len(out["items"]), 2)
        self.assertEqual(out["items"][1]["status"], "DRAFT")

    def test_draft_reply_for_spam_yields_no_draft_over_http(self):
        opp_id = self._create_opportunity()
        _, inbound = self._post(self.hq_port, f"/api/rh/opportunities/{opp_id}/conversations", {"channel": "email", "body": "This is an automated message."})
        status, drafted = self._post(self.hq_port, f"/api/rh/conversations/{inbound['message_id']}/draft-reply", {"actor": "Aryan"})
        self.assertEqual(status, 201)
        self.assertFalse(drafted["drafted"])

    def test_mark_conversation_reply_sent_over_http(self):
        opp_id = self._create_opportunity()
        _, inbound = self._post(self.hq_port, f"/api/rh/opportunities/{opp_id}/conversations", {"channel": "email", "body": "Sounds good, let's move forward."})
        _, drafted = self._post(self.hq_port, f"/api/rh/conversations/{inbound['message_id']}/draft-reply", {"actor": "Aryan"})
        status, sent = self._post(self.hq_port, f"/api/rh/conversations/{drafted['message_id']}/sent", {"actor": "Aryan"})
        self.assertEqual(status, 200)
        self.assertEqual(sent["status"], "SENT")

    def test_mark_conversation_reply_sent_twice_raises_over_http(self):
        opp_id = self._create_opportunity()
        _, inbound = self._post(self.hq_port, f"/api/rh/opportunities/{opp_id}/conversations", {"channel": "email", "body": "Sounds good."})
        _, drafted = self._post(self.hq_port, f"/api/rh/conversations/{inbound['message_id']}/draft-reply", {"actor": "Aryan"})
        self._post(self.hq_port, f"/api/rh/conversations/{drafted['message_id']}/sent", {"actor": "Aryan"})
        code = self._post_raises(f"/api/rh/conversations/{drafted['message_id']}/sent", {"actor": "Aryan"})
        self.assertEqual(code, 400)

    # -- Negotiation history --

    def test_negotiation_history_over_http(self):
        self._post(self.hq_port, "/api/rh/sales-policy", {"min_project_price": 500})
        opp_id = self._create_opportunity()
        self._post(self.hq_port, f"/api/rh/opportunities/{opp_id}/negotiation/evaluate", {"actor": "Aryan", "price": 1000})
        self._post(self.hq_port, f"/api/rh/opportunities/{opp_id}/negotiation/evaluate", {"actor": "Aryan", "price": 1200})
        status, out = self._get(self.hq_port, f"/api/rh/opportunities/{opp_id}/negotiations")
        self.assertEqual(status, 200)
        self.assertEqual(len(out["items"]), 2)

    # -- Onboarding + Delivery Brief (Pass C, Section 9) --

    def test_onboarding_init_creates_full_checklist_over_http(self):
        opp_id = self._create_opportunity()
        status, out = self._post(self.hq_port, f"/api/rh/opportunities/{opp_id}/onboarding/init", {"actor": "Aryan"})
        self.assertEqual(status, 201)
        self.assertGreater(len(out["items"]), 0)
        status, listed = self._get(self.hq_port, f"/api/rh/opportunities/{opp_id}/onboarding")
        self.assertEqual(status, 200)
        self.assertEqual(len(listed["items"]), len(out["items"]))

    def test_onboarding_set_item_over_http(self):
        opp_id = self._create_opportunity()
        self._post(self.hq_port, f"/api/rh/opportunities/{opp_id}/onboarding/init", {"actor": "Aryan"})
        status, item = self._post(self.hq_port, f"/api/rh/opportunities/{opp_id}/onboarding", {
            "item_type": "hosting", "status": "RECEIVED", "value_text": "AWS us-east-1", "actor": "Aryan",
        })
        self.assertEqual(status, 201)
        self.assertEqual(item["status"], "RECEIVED")
        self.assertEqual(item["value_text"], "AWS us-east-1")

    def test_onboarding_credentials_access_rejects_raw_secret_over_http(self):
        opp_id = self._create_opportunity()
        code = self._post_raises(f"/api/rh/opportunities/{opp_id}/onboarding", {
            "item_type": "credentials_access", "status": "RECEIVED", "value_text": "hunter2",
        })
        self.assertEqual(code, 400)

    def _configured_won_opportunity(self, final_price=3000):
        self._configure_sales_policy()
        opp_id = self._create_opportunity()
        _, result = self._post(self.hq_port, f"/api/rh/opportunities/{opp_id}/close", {
            "actor": "Aryan", "client_name": "Acme Corp", "final_price": final_price, "currency": "USD",
        })
        return opp_id, result

    def test_delivery_brief_over_http(self):
        opp_id, result = self._configured_won_opportunity()
        status, brief = self._get(self.hq_port, f"/api/rh/opportunities/{opp_id}/delivery-brief")
        self.assertEqual(status, 200)
        self.assertEqual(brief["client_name"], "Acme Corp")
        self.assertEqual(brief["final_price"], 3000)

    def test_enrich_active_job_over_http(self):
        opp_id, result = self._configured_won_opportunity()
        self._post(self.hq_port, f"/api/rh/opportunities/{opp_id}/onboarding/init", {"actor": "Aryan"})
        self._post(self.hq_port, f"/api/rh/opportunities/{opp_id}/onboarding", {
            "item_type": "hosting", "status": "RECEIVED", "value_text": "AWS us-east-1", "actor": "Aryan",
        })
        status, active_jobs = self._get(self.hq_port, "/api/rh/active-jobs")
        job_id = next(j["id"] for j in active_jobs["items"] if j["opportunity_id"] == opp_id)
        status, enriched = self._post(self.hq_port, f"/api/rh/active-jobs/{job_id}/enrich", {"actor": "Aryan"})
        self.assertEqual(status, 201)
        self.assertIn("AWS us-east-1", enriched["requirement"])

    def test_real_handoff_automatically_carries_onboarding_context(self):
        # Full real chain: Won -> Active Job -> onboarding data recorded ->
        # real handoff (against the disposable git repo _LiveServerCase
        # sets up) -> a real Falguna mission whose requirement text
        # actually contains the onboarding context, without a separate
        # manual "enrich" call.
        opp_id, result = self._configured_won_opportunity()
        self._post(self.hq_port, f"/api/rh/opportunities/{opp_id}/onboarding/init", {"actor": "Aryan"})
        self._post(self.hq_port, f"/api/rh/opportunities/{opp_id}/onboarding", {
            "item_type": "hosting", "status": "RECEIVED", "value_text": "AWS us-east-1", "actor": "Aryan",
        })
        status, active_jobs = self._get(self.hq_port, "/api/rh/active-jobs")
        job_id = next(j["id"] for j in active_jobs["items"] if j["opportunity_id"] == opp_id)
        status, handoff_result = self._post(self.hq_port, f"/api/rh/active-jobs/{job_id}/handoff", {
            "repository": str(self.repo), "actor": "Aryan",
        })
        self.assertEqual(status, 201)
        self.assertIn("mission_id", handoff_result)
        # The requirement text actually stored on the real mission's
        # requirement row is what matters -- confirm the onboarding
        # context made it all the way through, not just into the active
        # job's own payload.
        requirements = self.store.list("requirements", "mission_id=?", (handoff_result["mission_id"],))
        self.assertEqual(len(requirements), 1)
        self.assertIn("AWS us-east-1", requirements[0]["body"])


class PassDRoutesTests(_LiveServerCase):
    """HTTP tests for Billing, Completion, Retention, and Account
    Management (Pass D)."""

    hq_port = 8812

    def setUp(self):
        super().setUp()
        self.server = ThreadingHTTPServer(("127.0.0.1", self.hq_port), TTTHQHandler)
        self.server.app_root = self.repo
        self.server.falguna_url = "http://127.0.0.1:8765"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self._wait_ready()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        super().tearDown()

    def _wait_ready(self):
        for _ in range(40):
            try:
                status, body = self._get(self.hq_port, "/api/config")
                if body.get("product") == PRODUCT_NAME:
                    return
            except Exception:
                pass
            time.sleep(0.05)
        self.fail("TTT HQ server did not become ready")

    def _post_raises(self, path, body):
        try:
            self._post(self.hq_port, path, body)
            return None
        except urllib.error.HTTPError as e:
            return e.code

    def _create_opportunity(self, title="Build a CRM dashboard", source_url="https://example.com/jobs/2"):
        status, out = self._post(self.hq_port, "/api/rh/opportunities", {
            "title": title, "description": "Build a small CRM dashboard.", "source_url": source_url, "actor": "Aryan",
        })
        self.assertEqual(status, 201)
        return out["opportunity_id"]

    def _configured_won_opportunity_and_client(self, final_price=3000):
        self._post(self.hq_port, "/api/rh/sales-policy", {})
        opp_id = self._create_opportunity()
        _, result = self._post(self.hq_port, f"/api/rh/opportunities/{opp_id}/close", {
            "actor": "Aryan", "client_name": "Acme Corp", "final_price": final_price, "currency": "USD",
        })
        return opp_id, result["client_id"]

    # -- Billing --

    def test_create_and_list_invoice_over_http(self):
        _, client_id = self._configured_won_opportunity_and_client()
        status, out = self._post(self.hq_port, f"/api/rh/clients/{client_id}/invoices", {
            "actor": "Aryan", "amount": 1500, "currency": "USD",
        })
        self.assertEqual(status, 201)
        status, listed = self._get(self.hq_port, f"/api/rh/clients/{client_id}/invoices")
        self.assertEqual(status, 200)
        self.assertEqual(len(listed["items"]), 1)
        self.assertEqual(listed["items"][0]["id"], out["invoice_id"])
        status, all_invoices = self._get(self.hq_port, "/api/rh/invoices")
        self.assertTrue(any(i["id"] == out["invoice_id"] for i in all_invoices["items"]))

    def test_invoice_lifecycle_over_http(self):
        _, client_id = self._configured_won_opportunity_and_client()
        _, out = self._post(self.hq_port, f"/api/rh/clients/{client_id}/invoices", {"actor": "Aryan", "amount": 1000})
        invoice_id = out["invoice_id"]
        status, ready = self._post(self.hq_port, f"/api/rh/invoices/{invoice_id}/ready", {"actor": "Aryan"})
        self.assertEqual(ready["status"], "READY")
        status, sent = self._post(self.hq_port, f"/api/rh/invoices/{invoice_id}/sent", {"actor": "Aryan"})
        self.assertEqual(sent["status"], "SENT")
        status, paid = self._post(self.hq_port, f"/api/rh/invoices/{invoice_id}/payment", {
            "actor": "Aryan", "amount": 1000, "evidence": "wire ref #789",
        })
        self.assertEqual(status, 201)
        self.assertEqual(paid["status"], "PAID")

    def test_payment_without_evidence_rejected_over_http(self):
        _, client_id = self._configured_won_opportunity_and_client()
        _, out = self._post(self.hq_port, f"/api/rh/clients/{client_id}/invoices", {"actor": "Aryan", "amount": 1000})
        self._post(self.hq_port, f"/api/rh/invoices/{out['invoice_id']}/sent", {"actor": "Aryan"})
        code = self._post_raises(f"/api/rh/invoices/{out['invoice_id']}/payment", {"actor": "Aryan", "amount": 1000})
        self.assertEqual(code, 400)

    def test_cancel_invoice_over_http(self):
        _, client_id = self._configured_won_opportunity_and_client()
        _, out = self._post(self.hq_port, f"/api/rh/clients/{client_id}/invoices", {"actor": "Aryan", "amount": 1000})
        status, result = self._post(self.hq_port, f"/api/rh/invoices/{out['invoice_id']}/cancel", {"actor": "Aryan", "reason": "deal fell through"})
        self.assertEqual(result["status"], "CANCELLED")

    def test_overdue_check_over_http(self):
        _, client_id = self._configured_won_opportunity_and_client()
        _, out = self._post(self.hq_port, f"/api/rh/clients/{client_id}/invoices", {"actor": "Aryan", "amount": 1000, "due_date": "2020-01-01"})
        self._post(self.hq_port, f"/api/rh/invoices/{out['invoice_id']}/sent", {"actor": "Aryan"})
        status, result = self._post(self.hq_port, f"/api/rh/invoices/{out['invoice_id']}/overdue-check", {"actor": "Aryan"})
        self.assertEqual(result["status"], "OVERDUE")

    # -- Completion --

    def test_record_and_get_completion_over_http(self):
        opp_id, _ = self._configured_won_opportunity_and_client()
        status, out = self._post(self.hq_port, f"/api/rh/opportunities/{opp_id}/completion", {
            "actor": "Aryan", "evidence": {"final_commit": "abc123"},
        })
        self.assertEqual(status, 201)
        status, record = self._get(self.hq_port, f"/api/rh/opportunities/{opp_id}/completion")
        self.assertEqual(status, 200)
        self.assertEqual(record["id"], out["record_id"])

    def test_completion_without_evidence_rejected_over_http(self):
        opp_id, _ = self._configured_won_opportunity_and_client()
        code = self._post_raises(f"/api/rh/opportunities/{opp_id}/completion", {"actor": "Aryan"})
        self.assertEqual(code, 400)

    def test_get_completion_404s_when_none_recorded(self):
        opp_id = self._create_opportunity()
        code = self._get_raises(self.hq_port, f"/api/rh/opportunities/{opp_id}/completion")
        self.assertEqual(code, 404)

    # -- Retention --

    def test_create_and_list_retention_item_over_http(self):
        _, client_id = self._configured_won_opportunity_and_client()
        status, out = self._post(self.hq_port, f"/api/rh/clients/{client_id}/retention", {
            "actor": "Aryan", "kind": "maintenance", "follow_up_date": "2020-01-01",
        })
        self.assertEqual(status, 201)
        status, listed = self._get(self.hq_port, f"/api/rh/clients/{client_id}/retention")
        self.assertEqual(len(listed["items"]), 1)
        status, due = self._get(self.hq_port, "/api/rh/retention/due")
        self.assertTrue(any(i["id"] == out["item_id"] for i in due["items"]))

    def test_update_retention_item_over_http(self):
        _, client_id = self._configured_won_opportunity_and_client()
        _, out = self._post(self.hq_port, f"/api/rh/clients/{client_id}/retention", {"actor": "Aryan", "kind": "referral"})
        status, updated = self._post(self.hq_port, f"/api/rh/retention/{out['item_id']}", {"actor": "Aryan", "status": "COMPLETED"})
        self.assertEqual(updated["status"], "COMPLETED")

    # -- Account Management --

    def test_account_status_over_http_before_handoff(self):
        opp_id, _ = self._configured_won_opportunity_and_client()
        status, active_jobs = self._get(self.hq_port, "/api/rh/active-jobs")
        job_id = next(j["id"] for j in active_jobs["items"] if j["opportunity_id"] == opp_id)
        status, result = self._get(self.hq_port, f"/api/rh/active-jobs/{job_id}/account-status")
        self.assertEqual(status, 200)
        self.assertEqual(result["delivery_status"], "NOT_STARTED")

    def test_client_update_draft_over_http(self):
        opp_id, _ = self._configured_won_opportunity_and_client()
        status, active_jobs = self._get(self.hq_port, "/api/rh/active-jobs")
        job_id = next(j["id"] for j in active_jobs["items"] if j["opportunity_id"] == opp_id)
        status, result = self._get(self.hq_port, f"/api/rh/active-jobs/{job_id}/client-update-draft")
        self.assertEqual(status, 200)
        self.assertIn("hasn't started", result["draft"])

    def test_scope_signals_over_http(self):
        opp_id, _ = self._configured_won_opportunity_and_client()
        self._post(self.hq_port, f"/api/rh/opportunities/{opp_id}/conversations", {
            "channel": "email", "body": "Can you share some case studies from past clients?",
        })
        status, result = self._get(self.hq_port, f"/api/rh/opportunities/{opp_id}/scope-signals")
        self.assertEqual(status, 200)
        self.assertEqual(len(result["items"]), 1)

    def test_portfolio_overview_over_http(self):
        self._configured_won_opportunity_and_client()
        self._configured_won_opportunity_and_client()
        status, result = self._get(self.hq_port, "/api/rh/portfolio-overview")
        self.assertEqual(status, 200)
        self.assertEqual(len(result["items"]), 2)

    # -- Sales Manager (Pass E) --

    def test_sales_manager_overview_over_http(self):
        status, result = self._get(self.hq_port, "/api/rh/sales-manager/overview")
        self.assertEqual(status, 200)
        for key in ("attention", "best_leads", "proposals_needing_approval", "overdue_invoices", "upsell_ready_clients"):
            self.assertIn(key, result)

    # -- Outbound Lead + Outreach (Pass E) --

    def test_create_and_get_outbound_lead_over_http(self):
        status, out = self._post(self.hq_port, "/api/rh/outbound-leads", {
            "company_name": "Acme Corp", "actor": "Aryan", "confidence": "Medium", "likely_need": "Needs a booking system",
        })
        self.assertEqual(status, 201)
        status, lead = self._get(self.hq_port, f"/api/rh/outbound-leads/{out['lead_id']}")
        self.assertEqual(status, 200)
        self.assertEqual(lead["company_name"], "Acme Corp")
        status, listed = self._get(self.hq_port, "/api/rh/outbound-leads")
        self.assertTrue(any(l["id"] == out["lead_id"] for l in listed["items"]))

    def test_outbound_lead_rejects_percentage_confidence_over_http(self):
        code = self._post_raises("/api/rh/outbound-leads", {"company_name": "Acme Corp", "actor": "Aryan", "confidence": "87%"})
        self.assertEqual(code, 400)

    def test_outreach_draft_never_sent_without_approval_over_http(self):
        _, out = self._post(self.hq_port, "/api/rh/outbound-leads", {"company_name": "Acme Corp", "actor": "Aryan"})
        status, draft = self._post(self.hq_port, f"/api/rh/outbound-leads/{out['lead_id']}/outreach", {
            "actor": "Aryan", "channel": "email", "message": "Hi, we noticed you might need a booking system.",
        })
        self.assertEqual(status, 201)
        self.assertEqual(draft["status"], "PREPARED")
        code = self._post_raises(f"/api/rh/outreach/{draft['id']}/sent", {"actor": "Aryan"})
        self.assertEqual(code, 400)

    def test_outreach_draft_can_be_marked_sent_after_approval_over_http(self):
        _, out = self._post(self.hq_port, "/api/rh/outbound-leads", {"company_name": "Acme Corp", "actor": "Aryan"})
        _, draft = self._post(self.hq_port, f"/api/rh/outbound-leads/{out['lead_id']}/outreach", {
            "actor": "Aryan", "channel": "email", "message": "Hi there",
        })
        status, _ = self._post(self.hq_port, f"/api/needs-aryan/{draft['needs_aryan_id']}/decision", {"action": "approve", "actor": "Aryan"})
        self.assertEqual(status, 200)
        status, sent = self._post(self.hq_port, f"/api/rh/outreach/{draft['id']}/sent", {"actor": "Aryan"})
        self.assertEqual(status, 200)
        self.assertEqual(sent["status"], "SENT")
        status, listed = self._get(self.hq_port, f"/api/rh/outbound-leads/{out['lead_id']}/outreach")
        self.assertEqual(len(listed["items"]), 1)


if __name__ == "__main__":
    unittest.main()
