"""Tests for Twenty Two Technologies -- Live Enquiry Activation V1
(falguna/tally_intake.py): real Tally.so form-submission intake without a
live Tally dashboard/API connection. Real temp SQLite DB, real AuditLog,
real CommsStore/NeedsAryanQueue -- same convention as
tests/test_email_ingestion.py.
"""

import json
import tempfile
import unittest
from pathlib import Path

from falguna.audit import AuditLog
from falguna.comms import CommsStore
from falguna.email_admin import NullEmailProvider
from falguna.store import StateStore
from falguna.tally_intake import TALLY_FORM_TYPES, TallyIntakeService
from falguna.ttt_hq import NeedsAryanQueue

GENERAL_FIELDS = [
    {"key": "q1", "label": "Name", "type": "INPUT_TEXT", "value": "Jordan Lee"},
    {"key": "q2", "label": "Email", "type": "INPUT_EMAIL", "value": "jordan@acmecorp.example"},
    {"key": "q3", "label": "Message", "type": "TEXTAREA", "value": "We would like a quote for a new website."},
]

PROJECT_FIELDS = GENERAL_FIELDS + [
    {"key": "q4", "label": "Service Requested", "type": "INPUT_TEXT", "value": "Web development"},
    {"key": "q5", "label": "Budget", "type": "INPUT_TEXT", "value": "$10k-20k"},
    {"key": "q6", "label": "Timeline", "type": "INPUT_TEXT", "value": "Within 2 months"},
]

CAREERS_FIELDS = [
    {"key": "q1", "label": "Full Name", "type": "INPUT_TEXT", "value": "Alex Kim"},
    {"key": "q2", "label": "Email Address", "type": "INPUT_EMAIL", "value": "alex@example.com"},
    {"key": "q3", "label": "Role Applying For", "type": "INPUT_TEXT", "value": "Backend Engineer"},
    {"key": "q4", "label": "Upload your resume", "type": "FILE_UPLOAD", "value": [
        {"id": "f1", "name": "resume.pdf", "url": "https://tally.so/f1", "mimeType": "application/pdf", "size": 12345},
    ]},
]

MEDIA_FIELDS = [
    {"key": "q1", "label": "Name", "type": "INPUT_TEXT", "value": "Sam Reporter"},
    {"key": "q2", "label": "Email", "type": "INPUT_EMAIL", "value": "sam@presswire.example"},
    {"key": "q3", "label": "Media Outlet", "type": "INPUT_TEXT", "value": "Press Wire"},
    {"key": "q4", "label": "Media Request", "type": "TEXTAREA", "value": "Requesting comment for a story."},
]

# Exact labels observed in the four live Tally forms during the supervised
# Phase 1 verification. Values remain synthetic and clearly test-only.
GENUINE_GENERAL_FIELDS = [
    {"key": "live1", "label": "Full Name", "value": "TEST — Aryan Phase 1 Verification"},
    {"key": "live2", "label": "Work Email", "value": "aryanbehera46@gmail.com"},
    {"key": "live3", "label": "Enquiry Message", "value": "TEST ONLY — general enquiry"},
    {"key": "live4", "label": "Privacy Consent", "value": "I agree to be contacted"},
]

GENUINE_PROJECT_FIELDS = [
    {"key": "live1", "label": "Full Name", "value": "TEST — Aryan Phase 1 Verification"},
    {"key": "live2", "label": "Work Email", "value": "aryanbehera46@gmail.com"},
    {"key": "live3", "label": "Company", "value": "TEST ONLY — project company"},
    {"key": "live4", "label": "Project Type", "value": "Web Development"},
    {"key": "live5", "label": "Budget Range", "value": "$1k - $5k"},
    {"key": "live6", "label": "Project Goals", "value": "TEST ONLY — project goals"},
    {"key": "live7", "label": "Privacy Consent", "value": "I agree to be contacted"},
]

GENUINE_CAREERS_FIELDS = [
    {"key": "live1", "label": "Full Name", "value": "TEST — Aryan Phase 1 Verification"},
    {"key": "live2", "label": "Work Email", "value": "aryanbehera46@gmail.com"},
    {"key": "live3", "label": "Role You Are Applying For", "value": "TEST ONLY — Verification Role"},
    {"key": "live4", "label": "LinkedIn or Portfolio URL", "value": "https://example.com/test-only"},
    {"key": "live5", "label": "Résumé/CV (PDF or DOCX only)", "value": [
        {"id": "test-file", "name": "TTT-P1-CAREERS-20260926-01-TEST-ONLY.pdf",
         "url": "https://tally.so/test-only", "mimeType": "application/pdf", "size": 1234},
    ]},
    {"key": "live6", "label": "Privacy Consent", "value": "I agree to be contacted"},
]

GENUINE_MEDIA_FIELDS = [
    {"key": "live1", "label": "Journalist Name", "value": "TEST — Aryan Phase 1 Verification"},
    {"key": "live2", "label": "Publication", "value": "TEST ONLY — publication"},
    {"key": "live3", "label": "Work Email", "value": "aryanbehera46@gmail.com"},
    {"key": "live4", "label": "Enquiry or Request", "value": "TEST ONLY — media request"},
    {"key": "live5", "label": "Privacy Consent", "value": "I agree to be contacted"},
]


def _payload(form_id, fields, submission_id, response_id=None):
    return {
        "eventId": f"evt-{submission_id}", "eventType": "FORM_RESPONSE", "createdAt": "2026-09-26T10:00:00Z",
        "data": {
            "responseId": response_id or f"resp-{submission_id}", "submissionId": submission_id,
            "respondentId": "r1", "formId": form_id, "formName": "test form",
            "createdAt": "2026-09-26T10:00:00Z", "fields": fields,
        },
    }


class _IntakeCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = StateStore(root / "state.db")
        self.store.migrate()
        self.audit = AuditLog(root / "audit.jsonl")
        self.needs_aryan = NeedsAryanQueue(self.store, self.audit)
        self.comms = CommsStore(self.store, self.audit, self.needs_aryan)
        self.svc = TallyIntakeService(self.store, self.audit, self.comms)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()


class FormMappingTests(_IntakeCase):
    def test_all_four_real_form_ids_are_mapped(self):
        self.assertEqual(TALLY_FORM_TYPES, {
            "VLgxN6": "general", "vGkWW4": "project", "XxXNNz": "careers", "obWxxN": "media",
        })

    def test_unknown_form_id_is_rejected_not_guessed(self):
        result = self.svc.ingest(_payload("SomeOtherForm999", GENERAL_FIELDS, "unk-1"))
        self.assertEqual(result["status"], "rejected")
        self.assertIn("unrecognized", result["reason"])
        events = self.store.list("tally_intake_events", "status='rejected'")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["form_type"], "unknown")

    def test_missing_form_id_is_rejected(self):
        result = self.svc.ingest({"data": {"submissionId": "x", "fields": GENERAL_FIELDS}})
        self.assertEqual(result["status"], "rejected")

    def test_non_dict_payload_is_rejected_never_raises(self):
        self.assertEqual(self.svc.ingest(None)["status"], "rejected")
        self.assertEqual(self.svc.ingest("not a dict")["status"], "rejected")
        self.assertEqual(self.svc.ingest([1, 2, 3])["status"], "rejected")

    def test_payload_with_no_fields_is_rejected(self):
        result = self.svc.ingest(_payload("VLgxN6", [], "empty-1"))
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "payload has no fields")

    def test_genuine_labels_from_all_four_forms_map_exactly(self):
        cases = [
            ("VLgxN6", GENUINE_GENERAL_FIELDS, "live-general", "general"),
            ("vGkWW4", GENUINE_PROJECT_FIELDS, "live-project", "project"),
            ("XxXNNz", GENUINE_CAREERS_FIELDS, "live-careers", "careers"),
            ("obWxxN", GENUINE_MEDIA_FIELDS, "live-media", "media"),
        ]
        for form_id, fields, submission_id, form_type in cases:
            with self.subTest(form_type=form_type):
                result = self.svc.ingest(_payload(form_id, fields, submission_id))
                self.assertEqual(result["status"], "ingested")
                self.assertEqual(result["form_type"], form_type)
                self.assertEqual(result["consent_status"], "given")

        general = self.store.get("site_enquiries", self.store.list("tally_intake_events", "tally_submission_id=?", ("live-general",))[0]["enquiry_id"])
        self.assertEqual(general["message"], "TEST ONLY — general enquiry")
        project_event = self.store.list("tally_intake_events", "tally_submission_id=?", ("live-project",))[0]
        project = self.store.get("rh_opportunities", project_event["opportunity_id"])
        self.assertEqual(project["budget_rate"], "$1k - $5k")
        careers_event = self.store.list("tally_intake_events", "tally_submission_id=?", ("live-careers",))[0]
        application = self.store.get("site_applications", careers_event["application_id"])
        self.assertEqual(application["job_title_snapshot"], "TEST ONLY — Verification Role")
        self.assertEqual(application["resume_filename"], "TTT-P1-CAREERS-20260926-01-TEST-ONLY.pdf")
        self.assertEqual(json.loads(application["links_json"]), ["https://example.com/test-only"])
        media_event = self.store.list("tally_intake_events", "tally_submission_id=?", ("live-media",))[0]
        media = self.comms.get_conversation(media_event["conversation_id"])
        # The same test email was intentionally used on all four live forms,
        # so contact matching may reuse its existing organization. The media
        # subject still proves the Publication label mapped to the outlet.
        self.assertIn("TEST ONLY — publication", media["subject"])


class GeneralEnquiryTests(_IntakeCase):
    def test_general_enquiry_creates_conversation_via_enquiry_store(self):
        result = self.svc.ingest(_payload("VLgxN6", GENERAL_FIELDS, "gen-1"))
        self.assertEqual(result["status"], "ingested")
        self.assertEqual(result["form_type"], "general")
        self.assertIsNone(result.get("opportunity_id"))
        conv = self.comms.get_conversation(result["conversation_id"])
        self.assertEqual(conv["channel"], "WEBSITE")
        self.assertEqual(conv["department"], "general")
        self.assertEqual(conv["primary_contact"]["email"], "jordan@acmecorp.example")
        enquiry = self.store.get("site_enquiries", result["enquiry_id"])
        self.assertEqual(enquiry["kind"], "general")

    def test_malformed_missing_email_field_is_rejected_never_fabricated(self):
        fields = [{"key": "q1", "label": "Name", "type": "INPUT_TEXT", "value": "No Email Guy"}]
        result = self.svc.ingest(_payload("VLgxN6", fields, "bad-1"))
        self.assertEqual(result["status"], "rejected")
        self.assertIn("email", result["reason"])
        # Never fabricated a contact/conversation for the malformed payload.
        self.assertEqual(self.store.list("comm_contacts", "name=?", ("No Email Guy",)), [])

    def test_invalid_email_value_is_rejected(self):
        fields = [
            {"key": "q1", "label": "Name", "type": "INPUT_TEXT", "value": "Bad Email"},
            {"key": "q2", "label": "Email", "type": "INPUT_EMAIL", "value": "not-an-email"},
            {"key": "q3", "label": "Message", "type": "TEXTAREA", "value": "Hello"},
        ]
        result = self.svc.ingest(_payload("VLgxN6", fields, "bademail-1"))
        self.assertEqual(result["status"], "rejected")


class ProjectEnquiryTests(_IntakeCase):
    def test_project_enquiry_creates_real_opportunity(self):
        result = self.svc.ingest(_payload("vGkWW4", PROJECT_FIELDS, "proj-1"))
        self.assertEqual(result["status"], "ingested")
        self.assertIsNotNone(result["opportunity_id"])
        opp = self.store.get("rh_opportunities", result["opportunity_id"])
        self.assertIsNotNone(opp)
        conv = self.comms.get_conversation(result["conversation_id"])
        self.assertEqual(conv["department"], "sales")
        self.assertEqual(conv["linked_opportunity_id"], result["opportunity_id"])

    def test_missing_optional_budget_timeline_never_invented(self):
        minimal = GENERAL_FIELDS  # no budget/timeline/service fields present
        result = self.svc.ingest(_payload("vGkWW4", minimal, "proj-minimal-1"))
        self.assertEqual(result["status"], "ingested")
        opp = self.store.get("rh_opportunities", result["opportunity_id"])
        self.assertIsNone(opp.get("budget_rate"))


class CareersApplicationTests(_IntakeCase):
    def test_careers_application_creates_record_with_resume_metadata_only(self):
        result = self.svc.ingest(_payload("XxXNNz", CAREERS_FIELDS, "car-1"))
        self.assertEqual(result["status"], "ingested")
        app = self.store.get("site_applications", result["application_id"])
        self.assertEqual(app["applicant_name"], "Alex Kim")
        self.assertEqual(app["resume_filename"], "resume.pdf")
        self.assertEqual(app["resume_size_bytes"], 12345)
        # Phase 1, Requirement 2: the hosted URL is preserved (metadata,
        # never the bytes) so the resume can still be retrieved manually --
        # without it a metadata-only record would be unretrievable.
        self.assertEqual(app["resume_source_url"], "https://tally.so/f1")
        # Bytes were never fetched -- no storage path was ever set.
        self.assertIsNone(app.get("resume_storage_rel_path"))
        self.assertIsNone(app.get("resume_sha256"))

    def test_careers_conversation_is_linked_to_application(self):
        result = self.svc.ingest(_payload("XxXNNz", CAREERS_FIELDS, "car-2"))
        conv = self.comms.get_conversation(result["conversation_id"])
        self.assertEqual(conv["department"], "careers")
        self.assertEqual(conv["linked_application_id"], result["application_id"])

    def test_missing_resume_is_optional_not_a_rejection(self):
        fields = [f for f in CAREERS_FIELDS if f["label"] != "Upload your resume"]
        result = self.svc.ingest(_payload("XxXNNz", fields, "car-noresume-1"))
        self.assertEqual(result["status"], "ingested")
        app = self.store.get("site_applications", result["application_id"])
        self.assertIsNone(app["resume_filename"])


class MediaEnquiryTests(_IntakeCase):
    def test_media_enquiry_opens_media_department_conversation(self):
        result = self.svc.ingest(_payload("obWxxN", MEDIA_FIELDS, "med-1"))
        self.assertEqual(result["status"], "ingested")
        conv = self.comms.get_conversation(result["conversation_id"])
        self.assertEqual(conv["department"], "media")
        self.assertEqual(conv["organization"]["name"], "Press Wire")

    def test_media_missing_request_text_is_rejected(self):
        fields = [f for f in MEDIA_FIELDS if f["label"] != "Media Request"]
        result = self.svc.ingest(_payload("obWxxN", fields, "med-bad-1"))
        self.assertEqual(result["status"], "rejected")


class IdempotencyTests(_IntakeCase):
    def test_duplicate_submission_id_is_a_safe_noop(self):
        first = self.svc.ingest(_payload("VLgxN6", GENERAL_FIELDS, "dup-1"))
        second = self.svc.ingest(_payload("VLgxN6", GENERAL_FIELDS, "dup-1"))
        self.assertEqual(first["status"], "ingested")
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(second["conversation_id"], first["conversation_id"])
        self.assertEqual(len(self.store.list("site_enquiries")), 1)

    def test_replayed_project_submission_never_creates_a_second_opportunity(self):
        first = self.svc.ingest(_payload("vGkWW4", PROJECT_FIELDS, "proj-replay-1"))
        for _ in range(3):
            replay = self.svc.ingest(_payload("vGkWW4", PROJECT_FIELDS, "proj-replay-1"))
            self.assertEqual(replay["status"], "duplicate")
            self.assertEqual(replay["opportunity_id"], first["opportunity_id"])
        self.assertEqual(len(self.store.list("rh_opportunities")), 1)

    def test_replayed_careers_submission_never_creates_a_second_application(self):
        self.svc.ingest(_payload("XxXNNz", CAREERS_FIELDS, "car-replay-1"))
        self.svc.ingest(_payload("XxXNNz", CAREERS_FIELDS, "car-replay-1"))
        self.assertEqual(len(self.store.list("site_applications")), 1)

    def test_rejected_payload_replay_is_a_noop_without_another_rejection_record(self):
        bad_fields = [{"key": "q1", "label": "Name", "type": "INPUT_TEXT", "value": "No Email"}]
        first = self.svc.ingest(_payload("VLgxN6", bad_fields, "bad-retry-1"))
        second = self.svc.ingest(_payload("VLgxN6", bad_fields, "bad-retry-1"))
        self.assertEqual(first["status"], "rejected")
        self.assertEqual(second["status"], "rejected")
        self.assertEqual(second["reason"], first["reason"])
        events = self.store.list("tally_intake_events", "tally_submission_id=?", ("bad-retry-1",))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["status"], "rejected")

    def test_response_id_only_rejection_is_also_idempotent(self):
        payload = _payload("VLgxN6", [], "placeholder", response_id="response-only-reject")
        payload["data"].pop("submissionId")
        first = self.svc.ingest(payload)
        second = self.svc.ingest(payload)
        self.assertEqual(first["status"], "rejected")
        self.assertEqual(second["status"], "rejected")
        events = self.store.list("tally_intake_events", "tally_response_id=?", ("response-only-reject",))
        self.assertEqual(len(events), 1)

    def test_previously_rejected_submission_can_be_ingested_after_mapping_correction(self):
        payload = _payload("VLgxN6", [
            {"key": "q1", "label": "Full Name", "value": "Corrected Mapping"},
            {"key": "q2", "label": "Work Email", "value": "corrected@example.com"},
            {"key": "q3", "label": "Previously Unknown Message Label", "value": "Hello"},
        ], "mapping-corrected-1")
        first = self.svc.ingest(payload)
        self.assertEqual(first["status"], "rejected")

        from falguna.tally_intake import FIELD_LABEL_CANDIDATES
        FIELD_LABEL_CANDIDATES["message"].append("previously unknown message label")
        try:
            second = self.svc.ingest(payload)
        finally:
            FIELD_LABEL_CANDIDATES["message"].remove("previously unknown message label")

        self.assertEqual(second["status"], "ingested")
        events = self.store.list("tally_intake_events", "tally_submission_id=?", ("mapping-corrected-1",))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["status"], "ingested")
        self.assertEqual(len(self.store.list("site_enquiries")), 1)


class PersistenceTests(_IntakeCase):
    def test_state_survives_a_fresh_store_connection(self):
        result = self.svc.ingest(_payload("VLgxN6", GENERAL_FIELDS, "persist-1"))
        conv_id = result["conversation_id"]
        self.store.close()
        # Re-open against the same DB file, as a restarted process would.
        self.store = StateStore(Path(self.temp.name) / "state.db")
        self.store.migrate()
        reopened_comms = CommsStore(self.store, self.audit, NeedsAryanQueue(self.store, self.audit))
        conv = reopened_comms.get_conversation(conv_id)
        self.assertIsNotNone(conv)
        self.assertEqual(conv["primary_contact"]["email"], "jordan@acmecorp.example")


class InterruptedIngestionRecoveryTests(_IntakeCase):
    def test_rejected_replay_finishes_partial_general_without_duplicates(self):
        payload = _payload("VLgxN6", GENUINE_GENERAL_FIELDS, "interrupted-general-1")
        now = "2026-09-26T10:00:00+00:00"
        enquiry_id = self.store.create("site_enquiries", {
            "kind": "general", "name": "TEST — Aryan Phase 1 Verification",
            "email": "aryanbehera46@gmail.com", "company": None,
            "message": "TEST ONLY — general enquiry", "opportunity_id": None,
            "source_ip_hash": None, "created_at": now,
        })
        contact_id = self.store.create("comm_contacts", {
            "organization_id": None, "name": "TEST — Aryan Phase 1 Verification",
            "email": "aryanbehera46@gmail.com", "phone": None, "role_title": None,
            "notes": None, "created_at": now, "updated_at": now,
        })
        conversation_id = self.store.create("comm_conversations", {
            "channel": "WEBSITE", "department": "general", "subject": "General enquiry",
            "status": "new", "priority": "normal", "tags_json": None,
            "organization_id": None, "primary_contact_id": contact_id, "assigned_agent": None,
            "linked_opportunity_id": None, "linked_project_id": None, "linked_client_id": None,
            "linked_application_id": None, "source_ref_type": "site_enquiry",
            "source_ref_id": enquiry_id, "first_response_due_at": None,
            "first_response_at": None, "resolution_due_at": None, "resolved_at": None,
            "created_at": now, "updated_at": now,
        })
        self.store.create("tally_intake_events", {
            "form_id": "VLgxN6", "form_type": "general",
            "tally_submission_id": "interrupted-general-1", "tally_response_id": None,
            "tally_event_id": None, "status": "rejected", "reason": "interrupted",
            "conversation_id": None, "enquiry_id": None, "application_id": None,
            "opportunity_id": None, "raw_field_labels_json": "[]",
            "consent_status": None, "created_at": now,
        })

        result = self.svc.ingest(payload)
        self.assertEqual(result["status"], "ingested")
        self.assertEqual(result["enquiry_id"], enquiry_id)
        self.assertEqual(result["conversation_id"], conversation_id)
        self.assertEqual(len(self.store.list("site_enquiries")), 1)
        self.assertEqual(len(self.store.list("comm_contacts")), 1)
        self.assertEqual(len(self.store.list("comm_conversations")), 1)
        self.assertEqual(len(self.store.list(
            "comm_participants", "conversation_id=? AND participant_type=?",
            (conversation_id, "customer"),
        )), 1)
        inbound = self.store.list(
            "comm_messages", "conversation_id=? AND source_ref_type=? AND source_ref_id=?",
            (conversation_id, "site_enquiry", enquiry_id),
        )
        self.assertEqual(len(inbound), 1)

        duplicate = self.svc.ingest(payload)
        self.assertEqual(duplicate["status"], "duplicate")
        self.assertEqual(len(self.store.list("site_enquiries")), 1)
        self.assertEqual(len(self.store.list("comm_conversations")), 1)
        self.assertEqual(len(self.store.list(
            "comm_participants", "conversation_id=? AND participant_type=?",
            (conversation_id, "customer"),
        )), 1)


class WorkforceDispatchTests(_IntakeCase):
    def test_successful_ingest_dispatches_ai_workforce(self):
        result = self.svc.ingest(_payload("VLgxN6", GENERAL_FIELDS, "wf-1"))
        self.assertIn("workforce_actions", result)

    def test_no_external_send_under_null_email_provider(self):
        # The workforce only ever drafts (see comms_workforce.py) -- this
        # asserts the environment-level guarantee this sprint depends on:
        # the default provider cannot actually deliver mail.
        self.assertFalse(NullEmailProvider().is_configured())
        self.assertFalse(NullEmailProvider().capabilities()["send"])
        result = self.svc.ingest(_payload("VLgxN6", GENERAL_FIELDS, "wf-2"))
        conv = self.comms.get_conversation(result["conversation_id"])
        for message in conv["messages"]:
            if message["direction"] == "OUTBOUND":
                self.assertIn(message["status"], ("DRAFT", "APPROVED"))
                self.assertNotEqual(message["status"], "SENT")


class OverviewSurfacingTests(_IntakeCase):
    def test_rejected_submissions_are_surfaced_in_comms_overview(self):
        self.svc.ingest(_payload("NotARealForm", GENERAL_FIELDS, "bad-form-1"))
        overview = self.comms.overview()
        self.assertEqual(len(overview["website_intake_errors"]), 1)
        self.assertEqual(overview["website_intake_errors"][0]["form_type"], "unknown")

    def test_ingested_submissions_do_not_appear_as_errors(self):
        self.svc.ingest(_payload("VLgxN6", GENERAL_FIELDS, "good-1"))
        overview = self.comms.overview()
        self.assertEqual(len(overview["website_intake_errors"]), 0)


class ConsentHandlingTests(_IntakeCase):
    """Phase 1, Requirement 2: an explicit decline must never become a
    lead/application/conversation, regardless of form type; an explicit
    grant is recorded as compliance evidence; absence of the field (the
    common case today, since no real Tally payload has confirmed a
    consent field on any of the four forms) is never treated as an answer
    either way -- the submission processes exactly as it did before this
    phase."""

    def test_form_with_no_consent_field_processes_normally(self):
        result = self.svc.ingest(_payload("VLgxN6", GENERAL_FIELDS, "consent-none-1"))
        self.assertEqual(result["status"], "ingested")
        self.assertIsNone(result["consent_status"])
        event = self.store.list("tally_intake_events", "tally_submission_id=?", ("consent-none-1",))[0]
        self.assertIsNone(event["consent_status"])

    def test_explicit_boolean_true_consent_is_given_and_processes(self):
        fields = GENERAL_FIELDS + [{"key": "qc", "label": "I agree to the privacy policy", "value": True}]
        result = self.svc.ingest(_payload("VLgxN6", fields, "consent-true-1"))
        self.assertEqual(result["status"], "ingested")
        self.assertEqual(result["consent_status"], "given")
        conv = self.comms.get_conversation(result["conversation_id"])
        notes = [m["body"] for m in conv["messages"] if m.get("is_internal_note")]
        self.assertTrue(any("Consent to be contacted was explicitly given" in n for n in notes))

    def test_explicit_boolean_false_consent_is_declined_and_never_processed(self):
        fields = GENERAL_FIELDS + [{"key": "qc", "label": "I agree to the privacy policy", "value": False}]
        result = self.svc.ingest(_payload("VLgxN6", fields, "consent-false-1"))
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["consent_status"], "declined")
        self.assertIn("consent", result["reason"].lower())
        # Genuinely never processed -- no conversation, no lead of any kind.
        self.assertEqual(self.store.list("comm_conversations"), [])

    def test_explicit_text_decline_is_recognized(self):
        fields = CAREERS_FIELDS + [{"key": "qc", "label": "Terms and Conditions", "value": "No"}]
        result = self.svc.ingest(_payload("XxXNNz", fields, "consent-textno-1"))
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(self.store.list("site_applications"), [])

    def test_decline_is_never_a_duplicate_of_a_different_field_shape(self):
        # A declined submission still gets its own tally_intake_events row
        # (for the Website intake errors panel), same as any other rejection.
        self.svc.ingest(_payload("VLgxN6", GENERAL_FIELDS + [
            {"key": "qc", "label": "I consent", "value": False},
        ], "consent-decline-visible-1"))
        overview = self.comms.overview()
        self.assertEqual(len(overview["website_intake_errors"]), 1)
        self.assertIn("declined", overview["website_intake_errors"][0]["reason"])


class FieldLengthCapTests(_IntakeCase):
    def test_oversized_message_field_is_truncated_not_rejected_or_crashed(self):
        huge_message = "A" * 50000
        fields = [
            {"key": "q1", "label": "Name", "value": "Jordan Lee"},
            {"key": "q2", "label": "Email", "value": "jordan@acmecorp.example"},
            {"key": "q3", "label": "Message", "value": huge_message},
        ]
        result = self.svc.ingest(_payload("VLgxN6", fields, "huge-1"))
        self.assertEqual(result["status"], "ingested")
        conv = self.comms.get_conversation(result["conversation_id"])
        inbound = [m for m in conv["messages"] if m["direction"] == "INBOUND" and not m.get("is_internal_note")]
        self.assertTrue(len(inbound[0]["body"]) <= 10000)


if __name__ == "__main__":
    unittest.main()
