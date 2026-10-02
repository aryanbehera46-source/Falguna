import tempfile
import unittest
from pathlib import Path

from falguna.audit import AuditLog
from falguna.ecosystem import EcosystemError, EcosystemService
from falguna.store import StateStore


class EcosystemCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.tmp.name) / "phase8-synthetic.db")
        self.store.migrate()
        self.service = EcosystemService(self.store, AuditLog(Path(self.tmp.name) / "audit.jsonl"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def intake(self, **overrides):
        fields = dict(organization_id="ttt", customer_ref="synthetic-customer",
                      original_message="Need a bilingual ordering workflow for two shops",
                      normalized_meaning="Customer needs a bilingual ordering workflow for two retail locations",
                      source_language="hi", geography="India", preferred_language="Hindi",
                      category="AI_AUTOMATION", industry="Retail", budget_min=50000,
                      budget_max=90000, currency="INR", timing="6 weeks")
        fields.update(overrides)
        return self.service.create_intake(**fields)

    def routed_intake(self, mode="PARTNER_OR_SPECIALIST_COORDINATION"):
        intake_id = self.intake()
        route_id = self.service.recommend_route(
            intake_id, mode, ["requires evidenced delivery capability"],
            [{"type": "intake", "field": "category", "value": "AI_AUTOMATION"}],
            ["final integration scope is unknown"],
        )
        self.service.decide_route(route_id, mode, "owner-identity", "Reviewed against current capability")
        return intake_id, route_id


class IntakeRoutingTests(EcosystemCase):
    def test_preserves_original_and_normalized_meaning_and_clarification(self):
        intake_id = self.intake(clarification_questions=["Which POS is in use?"])
        row = self.store.get("p8_intakes", intake_id)
        self.assertIn("bilingual ordering", row["original_message"])
        self.assertIn("retail locations", row["normalized_meaning"])
        self.assertEqual(row["status"], "NEEDS_CLARIFICATION")

    def test_recommendation_is_not_a_commercial_decision(self):
        intake_id = self.intake()
        route_id = self.service.recommend_route(intake_id, "DIRECT_TTT_DELIVERY", ["catalog fit"],
                                                [{"service": "synthetic"}], ["timeline unconfirmed"])
        route = self.store.get("p8_routing_decisions", route_id)
        self.assertEqual(route["review_status"], "PENDING_HUMAN_REVIEW")
        self.assertIsNone(route["decided_mode"])
        decided = self.service.decide_route(route_id, "TTT_ADVISORY", "owner", "Scope needs discovery first")
        self.assertEqual(decided["decided_mode"], "TTT_ADVISORY")
        with self.assertRaisesRegex(EcosystemError, "already final"):
            self.service.decide_route(route_id, "DIRECT_TTT_DELIVERY", "owner", "overwrite")

    def test_invalid_route_and_inverted_budget_fail(self):
        with self.assertRaises(EcosystemError):
            self.intake(budget_min=2, budget_max=1)
        intake_id = self.intake()
        with self.assertRaises(EcosystemError):
            self.service.recommend_route(intake_id, "MAGIC", ["x"], [{"x": 1}], [])


class NetworkAndMarketplaceTests(EcosystemCase):
    def profile(self, **overrides):
        fields = dict(organization_id="ttt", profile_type="DELIVERY_SPECIALIST", display_name="Synthetic Specialist",
                      capabilities=[{"tag": "workflow-automation", "evidence_status": "EVIDENCE_REVIEWED", "evidence": [{"test": "synthetic"}]}],
                      geography=["India"], languages=["Hindi", "English"],
                      verification_level="EVIDENCE_REVIEWED", verification_evidence=[{"review": "synthetic"}])
        fields.update(overrides)
        return self.service.create_profile(**fields)

    def opportunity(self, requirements=("workflow-automation",), verification="EVIDENCE_REVIEWED"):
        intake_id, _ = self.routed_intake()
        return self.service.publish_opportunity(intake_id, customer_safe_brief="Bilingual retail workflow implementation",
                                                capability_requirements=requirements,
                                                required_verification_level=verification,
                                                budget_visibility="RANGE", actor_identity_id="owner")

    def test_matching_is_reasoned_not_magic_score_and_feed_is_customer_safe(self):
        profile_id, opportunity_id = self.profile(), self.opportunity()
        match = self.service.evaluate_match(opportunity_id, profile_id)
        self.assertEqual(match["eligible"], 1)
        self.assertNotIn("score", match)
        feed = self.service.feed_for_profile(profile_id)
        self.assertEqual(len(feed), 1)
        self.assertEqual(feed[0]["customer_relationship_owner"], "TTT")
        self.assertNotIn("customer_ref", feed[0])
        self.assertNotIn("original_message", feed[0])

    def test_missing_capability_verification_language_or_conflict_fails_closed(self):
        opportunity_id = self.opportunity()
        profile_id = self.profile(capabilities=[{"tag": "graphic-design", "evidence_status": "SELF_REPORTED"}],
                                  languages=["English"], verification_level="SELF_REPORTED",
                                  conflicts=["related party"])
        match = self.service.evaluate_match(opportunity_id, profile_id)
        self.assertEqual(match["eligible"], 0)
        self.assertEqual(self.service.feed_for_profile(profile_id), [])

    def test_verified_licence_requires_actual_evidence(self):
        with self.assertRaisesRegex(EcosystemError, "without evidence"):
            self.profile(profile_type="REGULATED_PROFESSIONAL", licensing_status="VERIFIED", licensing_evidence=[])

    def test_application_assignment_is_human_gated_and_never_allows_money_collection(self):
        profile_id, opportunity_id = self.profile(), self.opportunity()
        self.service.evaluate_match(opportunity_id, profile_id)
        application_id = self.service.apply(opportunity_id, profile_id, "Available for the evidenced scope")
        self.assertEqual(application_id, self.service.apply(opportunity_id, profile_id, "Repeated browser submit"))
        with self.assertRaisesRegex(EcosystemError, "approval evidence"):
            self.service.assign(application_id, "owner", "Implement workflow", [])
        assignment_id = self.service.assign(application_id, "owner", "Implement workflow",
                                            [{"approval": "synthetic owner review"}], True)
        assignment = self.store.get("p8_assignments", assignment_id)
        self.assertEqual(assignment["money_collection_allowed"], 0)
        self.assertEqual(assignment["customer_contact_allowed"], 1)


class BusinessLaunchAndAnalyticsTests(EcosystemCase):
    def test_blg_requires_human_route_and_evidenced_sequential_progress(self):
        intake_id = self.intake()
        with self.assertRaises(EcosystemError):
            self.service.start_blg(intake_id, "STARTUP_SMB", [])
        route_id = self.service.recommend_route(intake_id, "BUSINESS_LAUNCH_AND_GROWTH", ["end-to-end need"],
                                                [{"intake": "synthetic"}], ["tax structure unresolved"])
        self.service.decide_route(route_id, "BUSINESS_LAUNCH_AND_GROWTH", "owner", "Reviewed as BLG engagement")
        engagement_id = self.service.start_blg(intake_id, "STARTUP_SMB", ["legal and tax work requires qualified professionals"])
        with self.assertRaises(EcosystemError):
            self.service.advance_blg(engagement_id, "LAUNCH", "owner", [{"jump": True}])
        advanced = self.service.advance_blg(engagement_id, "VALIDATION", "owner", [{"interviews": 5}])
        self.assertEqual(advanced["current_stage"], "VALIDATION")
        self.assertIn("does not promise profit", advanced["outcome_disclaimer"])

    def test_analytics_does_not_invent_financial_metrics(self):
        self.routed_intake("TTT_ADVISORY")
        analytics = self.service.analytics("ttt")
        self.assertEqual(analytics["human_decided_routes"], 1)
        self.assertIsNone(analytics["collected_revenue"])
        self.assertIsNone(analytics["contribution"])


if __name__ == "__main__":
    unittest.main()
