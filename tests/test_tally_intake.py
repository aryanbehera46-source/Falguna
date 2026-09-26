"""Tests for Twenty Two Technologies -- Live Enquiry Activation V1
(falguna/tally_intake.py): real Tally.so form-submission intake without a
live Tally dashboard/API connection. Real temp SQLite DB, real AuditLog,
real CommsStore/NeedsAryanQueue -- same convention as
tests/test_email_ingestion.py.
"""

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

    def test_rejected_payload_retried_unchanged_is_rejected_again_not_duplicate(self):
        bad_fields = [{"key": "q1", "label": "Name", "type": "INPUT_TEXT", "value": "No Email"}]
        first = self.svc.ingest(_payload("VLgxN6", bad_fields, "bad-retry-1"))
        second = self.svc.ingest(_payload("VLgxN6", bad_fields, "bad-retry-1"))
        self.assertEqual(first["status"], "rejected")
        self.assertEqual(second["status"], "rejected")


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


if __name__ == "__main__":
    unittest.main()
