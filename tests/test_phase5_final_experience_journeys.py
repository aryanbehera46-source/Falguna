"""Phase 5 Final Client Experience -- synthetic acceptance journeys A-G,
plus one full end-to-end acceptance journey stringing the whole build
together (language understanding, communication preferences, WhatsApp
channel architecture, minimum-interruption clarification, the payment
communication bridge, and the customer portal data foundation).

A fake transport stands in for the real ModelRouter throughout (no real
model call, no network) -- same convention as tests/test_language.py.
Every journey is grounded in real store state; nothing here asserts on an
invented outcome.
"""

import json
import tempfile
import unittest
from pathlib import Path

from falguna.audit import AuditLog
from falguna.billing import BillingStore
from falguna.clarification_coordinator import ClarificationCoordinator
from falguna.commercial import DisputeStore, ProjectStore
from falguna.comms import CommsStore
from falguna.customer_portal import CustomerPortalService
from falguna.customer_context import CustomerContextService
from falguna.gateway import OpenAICompatibleGateway
from falguna.language import LanguageInterpretationStore, LanguageUnderstandingService
from falguna.payment_comms import PaymentCommsBridge
from falguna.revenue_hunter import OpportunityStore
from falguna.sales_ops import ClientStore
from falguna.store import StateStore


def _fake_transport_returning(payload_dict):
    def _transport(config, payload, timeout_seconds):
        return {
            "choices": [{"message": {"content": json.dumps(payload_dict)}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            "_falguna_provider": "fake-test-provider",
            "_falguna_metadata": {"routed_model": "fake-model"},
        }
    return _transport


class _Phase5JourneyCase(unittest.TestCase):
    """Full fixture wiring every Phase 5 Final Experience module together,
    the same way a real deployment would construct them."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = StateStore(root / "state.db")
        self.store.migrate()
        self.audit = AuditLog(root / "audit.jsonl")
        self.comms = CommsStore(self.store, self.audit)
        self.interpretations = LanguageInterpretationStore(self.store, self.audit)
        self.coordinator = ClarificationCoordinator(self.store, self.audit, comms=self.comms, interpretations=self.interpretations)
        self.clients = ClientStore(self.store, self.audit)
        self.opportunities = OpportunityStore(self.store, self.audit)
        self.projects = ProjectStore(self.store, self.audit)
        self.billing = BillingStore(self.store, self.audit)
        self.disputes = DisputeStore(self.store, self.audit)
        self.payment_comms = PaymentCommsBridge(self.store, self.audit, comms=self.comms, billing=self.billing, disputes=self.disputes)
        self.context = CustomerContextService(self.store, self.comms)
        self.portal = CustomerPortalService(self.store, self.audit, comms=self.comms, context=self.context, billing=self.billing, disputes=self.disputes)
        self.gateway = OpenAICompatibleGateway(None, "http://127.0.0.1:1/v1", "")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def _service(self, interpretation_payload):
        return LanguageUnderstandingService(self.gateway, _fake_transport_returning(interpretation_payload), None, timeout_seconds=5)

    def _inbound(self, conversation_id, body):
        return self.comms.add_message(conversation_id, "INBOUND", body)


class JourneyABadlyWrittenEnglishTests(_Phase5JourneyCase):
    def test_broken_grammar_still_produces_a_clean_actionable_english_record(self):
        conv = self.comms.open_conversation("EMAIL", "sales", subject="Enquiry")
        message = self._inbound(conv["id"], "i want app for my shop customer can order food and i see order on phone pls")
        service = self._service({
            "detected_languages": ["en"],
            "normalized_english_summary": "The customer wants an ordering app for their shop so customers can place food orders and the owner can view orders on their phone.",
            "ambiguity_flags": [], "unresolved_questions": [], "confidence": "High",
            "inferred_requirement_candidates": ["customer ordering", "owner order notifications on phone"],
            "clarification_needed": False,
        })
        result = service.interpret(message["body"])
        interp_id = self.interpretations.record_for_message(message["id"], message["body"], result)
        stored = self.interpretations.get(interp_id)
        self.assertEqual(stored["confidence"], "High")
        self.assertIn("ordering app", stored["normalized_english_summary"])
        self.assertFalse(stored["clarification_needed"])
        # No contact needs to be bothered -- the coordinator agrees nothing is outstanding.
        self.assertIsNone(self.coordinator.draft_consolidated_clarification(conv["id"]))


class JourneyBNonEnglishCustomerTests(_Phase5JourneyCase):
    def test_spanish_only_message_is_understood_and_recorded_in_english_canonical_form(self):
        contact_id = self.comms.find_or_create_contact("cliente@ejemplo.mx", name="Cliente Ejemplo")
        self.comms.set_contact_preferences(contact_id, "system", preferred_language="es")
        conv = self.comms.open_conversation("EMAIL", "sales", subject="Consulta", contact_id=contact_id)
        message = self._inbound(conv["id"], "Hola, necesito una pagina web para mi restaurante con menu y reservas.")
        service = self._service({
            "detected_languages": ["es"],
            "normalized_english_summary": "The customer needs a website for their restaurant with a menu and a reservations feature.",
            "ambiguity_flags": [], "unresolved_questions": [], "confidence": "High",
            "inferred_requirement_candidates": ["restaurant website", "online menu", "reservations"],
            "clarification_needed": False,
        })
        result = service.interpret(message["body"])
        interp_id = self.interpretations.record_for_message(message["id"], message["body"], result)
        stored = self.interpretations.get(interp_id)
        self.assertEqual(json.loads(stored["detected_languages_json"]), ["es"])
        # Internal record stays English-canonical regardless of the source language.
        self.assertTrue(stored["normalized_english_summary"].startswith("The customer needs"))
        contact = self.comms.get_contact(contact_id)
        self.assertEqual(contact["preferred_language"], "es")


class JourneyCMixedLanguageTests(_Phase5JourneyCase):
    def test_message_mixing_two_languages_detects_both_and_still_resolves_cleanly(self):
        conv = self.comms.open_conversation("EMAIL", "sales", subject="Enquiry")
        message = self._inbound(conv["id"], "Hi team, mujhe ek booking system chahiye for my salon, thanks!")
        service = self._service({
            "detected_languages": ["en", "hi"],
            "normalized_english_summary": "The customer wants a booking system for their salon.",
            "ambiguity_flags": [], "unresolved_questions": [], "confidence": "Medium",
            "inferred_requirement_candidates": ["salon booking system"], "clarification_needed": False,
        })
        result = service.interpret(message["body"])
        interp_id = self.interpretations.record_for_message(message["id"], message["body"], result)
        stored = self.interpretations.get(interp_id)
        self.assertEqual(set(json.loads(stored["detected_languages_json"])), {"en", "hi"})
        self.assertIn("booking system", stored["normalized_english_summary"])


class JourneyDRepeatCustomerTests(_Phase5JourneyCase):
    def test_second_conversation_reuses_the_same_contact_and_keeps_prior_history(self):
        contact_id_1 = self.comms.find_or_create_contact("returning@client.example", name="Returning Client")
        self.comms.set_contact_preferences(contact_id_1, "system", tone="CONVERSATIONAL", update_cadence="WEEKLY")
        first_conv = self.comms.open_conversation("EMAIL", "sales", subject="First project", contact_id=contact_id_1)
        self._inbound(first_conv["id"], "Thanks for the great work on my first site!")

        # Same email, months later -- must resolve to the SAME contact record,
        # not a fresh duplicate, carrying forward whatever TTT already knows.
        contact_id_2 = self.comms.find_or_create_contact("returning@client.example", name="Returning Client")
        self.assertEqual(contact_id_1, contact_id_2)
        contact = self.comms.get_contact(contact_id_2)
        self.assertEqual(contact["tone"], "CONVERSATIONAL")
        self.assertEqual(contact["update_cadence"], "WEEKLY")

        second_conv = self.comms.open_conversation("EMAIL", "sales", subject="New project", contact_id=contact_id_2)
        self._inbound(second_conv["id"], "Hi again, I'd like a second site for my new business.")
        all_conversations = [
            c for c in self.comms.list_conversations() if c.get("primary_contact_id") == contact_id_2
        ]
        self.assertEqual({c["id"] for c in all_conversations}, {first_conv["id"], second_conv["id"]})


class JourneyEAmbiguousContradictoryTests(_Phase5JourneyCase):
    def test_contradictory_message_preserves_uncertainty_instead_of_guessing(self):
        conv = self.comms.open_conversation("EMAIL", "sales", subject="Enquiry")
        message = self._inbound(
            conv["id"],
            "I want the cheapest possible option but also need it to support 10000 users with real-time video, "
            "and actually maybe I don't need the video part, not sure, budget is tight but also flexible.",
        )
        service = self._service({
            "detected_languages": ["en"],
            "normalized_english_summary": "The customer's budget and scale requirements are unclear and partly contradictory.",
            "ambiguity_flags": [
                "Customer asked for the cheapest option while also requesting support for 10,000 users with real-time video, which are conflicting requirements.",
                "Customer said the video requirement may not be needed after all.",
            ],
            "unresolved_questions": [
                "Do you need real-time video support, or was that not actually required?",
                "What is your actual budget range, since you mentioned both 'cheapest possible' and 'flexible'?",
            ],
            "confidence": "Low",
            "inferred_requirement_candidates": [],  # nothing confirmed enough to call a requirement yet
            "clarification_needed": True,
        })
        result = service.interpret(message["body"])
        interp_id = self.interpretations.record_for_message(message["id"], message["body"], result)
        stored = self.interpretations.get(interp_id)
        self.assertTrue(stored["clarification_needed"])
        self.assertEqual(json.loads(stored["inferred_requirement_candidates_json"]), [])
        self.assertEqual(len(json.loads(stored["ambiguity_flags_json"])), 2)

        draft = self.coordinator.draft_consolidated_clarification(conv["id"])
        self.assertIsNotNone(draft)
        self.assertIn("real-time video", draft["body"])
        self.assertIn("budget range", draft["body"])
        # Exactly one consolidated message, not two separate ones.
        self.assertEqual(draft["body"].count("- "), 2)


class JourneyFMinimumInterruptionE2ETests(_Phase5JourneyCase):
    def test_two_inbound_messages_with_overlapping_gaps_produce_one_consolidated_ask(self):
        conv = self.comms.open_conversation("EMAIL", "sales", subject="New project enquiry")
        m1 = self._inbound(conv["id"], "I need an app for my gym, not sure about payments yet.")
        service1 = self._service({
            "detected_languages": ["en"], "normalized_english_summary": "Customer wants a gym app.",
            "ambiguity_flags": [], "confidence": "Medium",
            "unresolved_questions": ["Do members pay through the app, or is payment handled elsewhere?", "How many locations does the gym have?"],
            "inferred_requirement_candidates": ["gym app"], "clarification_needed": True,
        })
        self.interpretations.record_for_message(m1["id"], m1["body"], service1.interpret(m1["body"]))

        m2 = self._inbound(conv["id"], "Also forgot to say we want class scheduling too, still unsure on payments though.")
        service2 = self._service({
            "detected_languages": ["en"], "normalized_english_summary": "Customer also wants class scheduling.",
            "ambiguity_flags": [], "confidence": "Medium",
            "unresolved_questions": ["Do members pay through the app, or is payment handled elsewhere?", "Should class scheduling allow waitlists?"],
            "inferred_requirement_candidates": ["gym app", "class scheduling"], "clarification_needed": True,
        })
        self.interpretations.record_for_message(m2["id"], m2["body"], service2.interpret(m2["body"]))

        draft = self.coordinator.draft_consolidated_clarification(conv["id"])
        self.assertIsNotNone(draft)
        # Three distinct questions total (the payments question only once, despite appearing in both messages).
        self.assertEqual(draft["body"].count("- "), 3)
        self.assertIn("payment handled elsewhere", draft["body"])
        self.assertIn("locations", draft["body"])
        self.assertIn("waitlists", draft["body"])

        # A later re-assessment (e.g. a dashboard refresh calling the
        # coordinator again before the customer has replied) must not
        # fabricate a second, duplicate outbound ask.
        again = self.coordinator.draft_consolidated_clarification(conv["id"])
        self.assertEqual(draft["id"], again["id"])
        outbound_non_internal = [
            m for m in self.store.list("comm_messages", "conversation_id=?", (conv["id"],))
            if m["direction"] == "OUTBOUND" and not m["is_internal_note"]
        ]
        self.assertEqual(len(outbound_non_internal), 1)


class JourneyGHonestPaymentStateTests(_Phase5JourneyCase):
    def test_customer_claiming_payment_never_produces_a_fabricated_payment_confirmation(self):
        client_id = self.clients.upsert("Honest Payments Co", "Aryan")
        invoice_id = self.billing.create_invoice(client_id, "Aryan", 1000.0, due_date="2026-12-01")
        self.billing.mark_ready(invoice_id, "Aryan")
        self.billing.mark_sent(invoice_id, "Aryan")

        conv = self.comms.open_conversation("EMAIL", "billing", subject="Billing", linked_client_id=client_id)
        message = self._inbound(conv["id"], "I already paid this invoice in full yesterday via wire transfer, please mark it paid now.")
        service = self._service({
            "detected_languages": ["en"],
            "normalized_english_summary": "The customer claims to have already paid the invoice via wire transfer.",
            "ambiguity_flags": ["Customer's payment claim has not been independently confirmed by TTT's records."],
            "unresolved_questions": [], "confidence": "Medium",
            "inferred_requirement_candidates": [], "clarification_needed": False,
        })
        result = service.interpret(message["body"])
        self.interpretations.record_for_message(message["id"], message["body"], result)

        # The customer's own claim, however confident, is not evidence --
        # no real payment has been recorded in BillingStore, so no
        # "payment received" draft is produced.
        self.assertIsNone(self.payment_comms.draft_payment_received_notice(invoice_id))
        self.assertEqual(self.billing.get(invoice_id)["status"], "SENT")

        # Only once real, evidence-backed payment is recorded does an
        # honest confirmation become possible -- and it states only the
        # real recorded amount, never the customer's unverified claim.
        self.billing.record_payment(invoice_id, 1000.0, "Aryan", evidence={"bank_statement_ref": "WIRE-REF-998"})
        notice = self.payment_comms.draft_payment_received_notice(invoice_id)
        self.assertIsNotNone(notice)
        self.assertIn("1000.00", notice["body"])
        self.assertIn("fully paid", notice["body"])


class FullPhase5AcceptanceJourneyTest(_Phase5JourneyCase):
    """One continuous, realistic customer lifecycle exercising every
    Phase 5 Final Client Experience module together: intake, mixed-
    language ambiguous enquiry, minimum-interruption clarification,
    resolution, delivery project, honest invoicing and payment, a
    dispute and its resolution, and a scoped customer-portal view of all
    of it -- with internal notes never leaking and every outbound message
    staying a DRAFT throughout (this build never auto-sends)."""

    def test_end_to_end_acceptance(self):
        # -- Intake: a mixed-language, partly ambiguous first contact ---
        org_id = self.comms.find_or_create_organization("Riverside Bistro", domain="riversidebistro.example")
        client_id = self.clients.upsert("Riverside Bistro", "Aryan")
        contact_id = self.comms.find_or_create_contact(
            "owner@riversidebistro.example", name="Riverside Owner", organization_id=org_id,
        )
        self.comms.set_contact_preferences(contact_id, "system", preferred_language="en", tone="CONVERSATIONAL")

        conv = self.comms.open_conversation(
            "EMAIL", "sales", subject="New site enquiry", contact_id=contact_id,
            organization_id=org_id, linked_client_id=client_id,
        )
        first_message = self._inbound(
            conv["id"], "hi, necesito una pagina web for my restaurant, maybe with online ordering, not 100% sure yet",
        )
        first_result = self._service({
            "detected_languages": ["en", "es"],
            "normalized_english_summary": "The customer wants a website for their restaurant, possibly including online ordering, but is not fully decided.",
            "ambiguity_flags": ["Customer is unsure whether online ordering is actually required."],
            "unresolved_questions": ["Do you want online ordering included, or just an informational site for now?"],
            "confidence": "Medium", "inferred_requirement_candidates": ["restaurant website"],
            "clarification_needed": True,
        }).interpret(first_message["body"])
        self.interpretations.record_for_message(first_message["id"], first_message["body"], first_result)

        # -- Minimum-interruption: exactly one consolidated ask, never sent automatically --
        clarification_draft = self.coordinator.draft_consolidated_clarification(conv["id"])
        self.assertIsNotNone(clarification_draft)
        self.assertEqual(clarification_draft["status"], "DRAFT")
        self.assertIn("online ordering", clarification_draft["body"])
        # Simulate the clarification actually being sent before the
        # customer's follow-up reply arrives -- the realistic ordering.
        self.comms.mark_message_sent(clarification_draft["id"], "Aryan")

        # -- Customer resolves the ambiguity in a follow-up message ------
        second_message = self._inbound(conv["id"], "ok yes please include online ordering too, that would be great")
        second_result = self._service({
            "detected_languages": ["en"],
            "normalized_english_summary": "The customer confirmed they want online ordering included.",
            "ambiguity_flags": [], "unresolved_questions": [], "confidence": "High",
            "inferred_requirement_candidates": ["restaurant website", "online ordering"],
            "clarification_needed": False,
        }).interpret(second_message["body"])
        self.interpretations.record_for_message(second_message["id"], second_message["body"], second_result)
        self.assertIsNone(self.coordinator.draft_consolidated_clarification(conv["id"]))

        # Internal staff-only note on this conversation must never reach
        # anything customer-facing produced later in this journey.
        self.comms.add_message(
            conv["id"], "OUTBOUND", "Internal: this client negotiates hard, hold firm on price.",
            kind="note", is_internal_note=True, actor="Aryan",
        )

        # -- Delivery project exists for this won engagement --------------
        opportunity_id = self.opportunities.create({
            "title": "Riverside Bistro website", "description": "Restaurant website with online ordering.",
            "client_name": "Riverside Bistro", "budget_rate": "$3000",
        }, actor="system")
        project_id = self.projects.create_for_opportunity(opportunity_id, client_id, "Aryan", "CUSTOM_BUILD")
        self.assertIsNotNone(self.projects.get(project_id))

        # -- Honest invoicing: nothing is said to the customer until the --
        # -- invoice is actually ready -------------------------------------
        invoice_id = self.billing.create_invoice(client_id, "Aryan", 3000.0, due_date="2026-11-15")
        self.assertIsNone(self.payment_comms.draft_invoice_ready_notice(invoice_id))  # still DRAFT
        self.billing.mark_ready(invoice_id, "Aryan")
        ready_notice = self.payment_comms.draft_invoice_ready_notice(invoice_id)
        self.assertIsNotNone(ready_notice)
        self.assertEqual(ready_notice["status"], "DRAFT")  # never auto-sent
        self.billing.mark_sent(invoice_id, "Aryan")

        # -- Partial, evidence-backed payment ------------------------------
        self.billing.record_payment(invoice_id, 1500.0, "Aryan", evidence={"bank_ref": "RIV-WIRE-1"})
        payment_notice = self.payment_comms.draft_payment_received_notice(invoice_id)
        self.assertIn("received a payment of 1500.00", payment_notice["body"])
        self.assertIn("1500.00", payment_notice["body"].split("received a payment of 1500.00")[1])  # remaining balance, same figure here coincidentally

        # -- A dispute is raised, acknowledged, and fairly resolved --------
        dispute_id = self.disputes.open(
            invoice_id, "Aryan", "customer felt the online ordering module needed more polish",
            500.0, evidence={"ticket": "RIV-DSP-1"}, project_id=project_id,
        )
        ack_notice = self.payment_comms.draft_dispute_acknowledgement(dispute_id)
        self.assertIsNotNone(ack_notice)
        self.disputes.resolve(
            dispute_id, "Aryan", "we agreed to apply a partial credit for the extra polish requested",
            refund_amount=250.0, evidence={"note": "goodwill credit approved"},
        )
        resolution_notice = self.payment_comms.draft_dispute_resolution_notice(dispute_id)
        self.assertIn("250.00", resolution_notice["body"])
        self.assertNotIn("500.00", resolution_notice["body"])  # only the approved figure, never the original claim

        # -- Customer portal bundle: a correct, scoped, honest summary ----
        bundle = self.portal.get_portal_bundle(org_id)
        self.assertEqual(bundle["organization"]["id"], org_id)
        self.assertEqual([p["id"] for p in bundle["projects"]], [project_id])
        invoice_summary = next(i for i in bundle["invoices"] if i["id"] == invoice_id)
        self.assertEqual(invoice_summary["status"], "PARTIALLY_PAID")
        self.assertEqual(invoice_summary["amount_received"], 1500.0)
        dispute_summary = next(d for d in bundle["disputes"] if d["id"] == dispute_id)
        self.assertEqual(dispute_summary["status"], "PARTIAL_REFUND_APPROVED")
        self.assertEqual(dispute_summary["refund_amount"], 250.0)

        # -- No internal-only content ever reaches a customer-facing view --
        portal_bodies = " ".join(m["body"] for conv_row in bundle["conversations"] for m in conv_row["messages"])
        self.assertNotIn("negotiates hard", portal_bodies)
        for notice in (ready_notice, payment_notice, ack_notice, resolution_notice):
            self.assertNotIn("negotiates hard", notice["body"])

        # -- Nothing but the one explicitly-marked-sent clarification was --
        # -- ever auto-sent; every other customer-facing draft this whole --
        # -- journey produced is still sitting in DRAFT, awaiting a human --
        for notice in (ready_notice, payment_notice, ack_notice, resolution_notice):
            self.assertEqual(self.store.get("comm_messages", notice["id"])["status"], "DRAFT")


if __name__ == "__main__":
    unittest.main()
