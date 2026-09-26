"""Twenty Two Technologies -- Live Enquiry Activation V1.

Turns one Tally.so form-submission payload into real CommsStore /
EnquiryStore / ApplicationStore state, so a genuine visitor submission on
the live public website (https://twentytwotechnologies.com/) becomes a
real, triaged conversation instead of a message that only exists inside
Tally's own dashboard.

No live Tally dashboard/API credentials were available while this module
was built (see the mission's Step 1/Step 8 findings -- attempting the
Tally dashboard redirected to a login page with no session available).
This module has therefore NOT been exercised against a genuine
Tally-delivered payload yet. It has been designed from:
  - the four real, verified-live form URLs/ids, kept identical to (and
    imported the same literal values as) scripts/export_static_site.py's
    own TALLY_FORMS constant, so the two can never silently drift apart;
  - Tally's own publicly documented webhook payload shape:
      {"eventId", "eventType", "createdAt",
       "data": {"responseId", "submissionId", "respondentId", "formId",
                "formName", "createdAt", "fields": [
                    {"key", "label", "type", "value"}, ...
                ]}}
    (a file-upload field's `value` is a list of
    {"id","name","url","mimeType","size"} objects in that same shape).

Field matching is by LABEL (case-insensitive, whitespace/punctuation
tolerant, against a short explicit candidate list per logical field) --
never by Tally's own internal opaque field `key` -- because no
dashboard/API access was available to read the real per-form field keys.
This is the honest, minimum-guess alternative to inventing key strings,
but it is still UNVERIFIED against a real submission. `ingest()` is
deliberately conservative: a required field it cannot confidently match by
label is a REJECTED submission (recorded, visible in TTT HQ's
Communications view, audited) -- never a guess, never a fabricated value.
See the operational note this sprint's report ships alongside this module
for the one remaining manual step: feed this module (via
scripts/import_tally_submission.py) one real exported submission per form
and confirm/tighten FIELD_LABEL_CANDIDATES against the real labels seen.

Reuses, never duplicates (per this mission's explicit instruction not to
rebuild Communications V2 Milestones 1-12):
  - `EnquiryStore.submit()` for GENERAL and PROJECT -- opens the same
    CommsStore conversation, and for PROJECT the same real Revenue Hunter
    opportunity, a native in-app site submission would have.
  - `ApplicationStore.create()` for CAREERS -- opens the same careers
    conversation a native submission would have.
  - `comms_workforce.run_agent_for_conversation` for AI triage (drafts,
    risk classification, NeedsAryanQueue escalation) -- exactly the same
    call `email_ingestion.py` already makes after a successful ingest.
  - `CommsStore` directly for MEDIA, which has no existing dedicated
    intake store (EnquiryStore only ever handles general/project) --
    mirrors EnquiryStore's own general-enquiry path (organization/contact
    match, one conversation, one inbound message) rather than inventing a
    new architecture for it.

Idempotency / audit (mirrors email_ingestion.py's own convention):
  - Every payload -- ingested, duplicate, or rejected -- gets one
    `tally_intake_events` row, keyed first by Tally's own `submissionId`
    (falling back to `responseId`). A webhook retry or a re-run of the
    manual-import script on the same exported JSON is a safe no-op: it
    never creates a second lead, application, or opportunity.

Attachments (Careers resume):
  - Tally's webhook payload for a file-upload field carries metadata and a
    Tally-hosted URL, never the file's bytes. Per this codebase's existing
    convention for attachments of unknown provenance (see
    email_ingestion.py's own attachment-metadata note), the bytes are
    NEVER fetched here -- only filename/mimetype/size are recorded on the
    application row and as an internal CommsStore note. Actually
    downloading and storing resume bytes (mirroring
    site_web.py's safe_upload_path()/MAX_RESUME_BYTES) is a deliberate,
    separate follow-up once a real payload exists to build and test it
    against, per this sprint's time-boxed, minimum-viable scope.

Never sends anything externally: this module only ever writes local
CommsStore/EnquiryStore/ApplicationStore state and calls the existing
(NullEmailProvider-backed, draft-only) AI workforce. No network call of
any kind is made from here.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from .audit import AuditLog
from .comms import CommsStore
from .revenue_hunter import OpportunityStore
from .site_content import ApplicationStore, ContentError, EnquiryStore
from .store import StateStore, utcnow

# Kept identical to scripts/export_static_site.py's TALLY_FORMS (the four
# real, live, verified-live public forms -- see the mission's Step 1
# audit). Inverted here (id -> type) since a payload only ever carries the
# form id. A form id that doesn't appear here is rejected, never guessed.
TALLY_FORM_TYPES: Dict[str, str] = {
    "VLgxN6": "general",
    "vGkWW4": "project",
    "XxXNNz": "careers",
    "obWxxN": "media",
}

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class TallyIntakeError(ValueError):
    """A payload that is well-formed enough to identify its form, but
    whose required fields could not be confidently matched by label.
    Always caught by TallyIntakeService.ingest() -- never propagates."""


def _norm_label(label: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (label or "").strip().lower()).strip()


# Explicit, versioned per-logical-field label candidates. UNVERIFIED
# against a real Tally payload (see module docstring) -- this is the
# single place to tighten once Aryan supplies one real export per form.
# One shared vocabulary; each handler below only reads what its form needs.
FIELD_LABEL_CANDIDATES: Dict[str, List[str]] = {
    "name": ["name", "your name", "full name", "contact name", "applicant name"],
    "email": ["email", "your email", "email address", "work email"],
    "phone": ["phone", "phone number", "your phone", "contact number", "mobile", "mobile number"],
    "message": [
        "message", "your message", "details", "tell us more", "how can we help you",
        "how can we help", "project requirements", "requirements", "description",
        "what do you need help with",
    ],
    "company": ["company", "company name", "organisation", "organization", "business name"],
    "budget": ["budget", "budget range", "estimated budget", "budget hint"],
    "timeline": ["timeline", "timeframe", "project timeline", "when do you need this", "deadline"],
    "service": ["service", "service requested", "what service", "what are you interested in", "project type"],
    "role": ["role", "position", "job title", "role applying for", "which role", "role you're applying for"],
    "resume": ["resume", "resume cv", "cv", "upload your resume", "attach your resume", "upload resume cv"],
    "links": ["portfolio", "linkedin", "github", "portfolio url", "links", "portfolio linkedin github"],
    "outlet": ["outlet", "organization", "publication", "media outlet", "company organisation outlet", "outlet publication"],
    "media_deadline": ["deadline", "response needed by", "when do you need a response", "response deadline"],
    "request": ["request", "media request", "what are you working on", "story details", "what is this regarding"],
}


def _find_field(fields: List[Dict[str, Any]], logical_name: str) -> Optional[Dict[str, Any]]:
    candidates = {_norm_label(c) for c in FIELD_LABEL_CANDIDATES.get(logical_name, [])}
    for f in fields:
        if isinstance(f, dict) and _norm_label(f.get("label")) in candidates:
            return f
    return None


def _field_text(fields: List[Dict[str, Any]], logical_name: str) -> Optional[str]:
    f = _find_field(fields, logical_name)
    if f is None:
        return None
    value = f.get("value")
    if value is None:
        return None
    if isinstance(value, list):
        if value and isinstance(value[0], dict):
            return None  # file-upload-shaped value -- use _field_files() instead
        text = ", ".join(str(v) for v in value if v not in (None, ""))
        return text.strip() or None
    text = str(value).strip()
    return text or None


def _field_files(fields: List[Dict[str, Any]], logical_name: str) -> List[Dict[str, Any]]:
    f = _find_field(fields, logical_name)
    if f is None:
        return []
    value = f.get("value")
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, dict)]


class TallyIntakeService:
    """Ingests one Tally webhook/export-shaped payload at a time.
    `ingest()` never raises on a malformed/hostile/unrecognized payload --
    same never-raises contract as `EmailIngestionService.ingest()` -- so a
    future webhook receiver or the manual-import CLI can call it in a loop
    without special-casing exceptions."""

    def __init__(self, store: StateStore, audit: AuditLog, comms: CommsStore):
        self.store = store
        self.audit = audit
        self.comms = comms
        self.opportunities = OpportunityStore(store, audit)
        self.enquiries = EnquiryStore(store, self.opportunities, comms)
        self.applications = ApplicationStore(store, comms)

    def ingest(self, payload: Dict[str, Any], actor: str = "tally_intake") -> Dict[str, Any]:
        if not isinstance(payload, dict):
            return {"status": "rejected", "reason": "payload is not a JSON object"}

        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        form_id = str(data.get("formId") or payload.get("formId") or "").strip()
        submission_id = str(data.get("submissionId") or "").strip() or None
        response_id = str(data.get("responseId") or "").strip() or None
        dedup_key = submission_id or response_id
        event_id = str(payload.get("eventId") or "").strip() or None
        raw_fields = data.get("fields")
        fields = [f for f in raw_fields if isinstance(f, dict)] if isinstance(raw_fields, list) else []
        field_labels = [f.get("label") for f in fields]
        form_type = TALLY_FORM_TYPES.get(form_id, "unknown")

        def _record(status: str, reason: Optional[str] = None, **outcome: Any) -> None:
            self.store.create("tally_intake_events", {
                "form_id": form_id or None, "form_type": form_type,
                "tally_submission_id": submission_id, "tally_response_id": response_id,
                "tally_event_id": event_id, "status": status, "reason": reason,
                "conversation_id": outcome.get("conversation_id"),
                "enquiry_id": outcome.get("enquiry_id"),
                "application_id": outcome.get("application_id"),
                "opportunity_id": outcome.get("opportunity_id"),
                "raw_field_labels_json": json.dumps(field_labels),
                "created_at": utcnow(),
            })

        # -- 1. idempotency, checked before any other validation: a
        # webhook retry or a re-run of the manual-import script over the
        # same export is a safe no-op, never a duplicate lead/application.
        if dedup_key:
            existing = self.store.list(
                "tally_intake_events", "tally_submission_id=? AND status='ingested'", (dedup_key,),
            )
            if not existing and response_id and response_id != dedup_key:
                existing = self.store.list(
                    "tally_intake_events", "tally_response_id=? AND status='ingested'", (response_id,),
                )
            if existing:
                row = existing[0]
                self.audit.append("TALLY_INTAKE_DUPLICATE_SKIPPED", {
                    "tally_submission_id": dedup_key, "form_type": form_type, "actor": actor,
                })
                return {
                    "status": "duplicate", "form_type": form_type,
                    "conversation_id": row.get("conversation_id"), "enquiry_id": row.get("enquiry_id"),
                    "application_id": row.get("application_id"), "opportunity_id": row.get("opportunity_id"),
                }

        # -- 2. known, live form only. An id outside the four real forms is
        # never guessed at -- rejected and recorded for visibility.
        if form_type == "unknown":
            _record("rejected", "unknown_or_missing_form_id")
            self.audit.append("TALLY_INTAKE_REJECTED", {
                "reason": "unknown_or_missing_form_id", "form_id": form_id, "actor": actor,
            })
            return {"status": "rejected", "reason": f"unrecognized or missing Tally form id {form_id!r}"}

        if not fields:
            _record("rejected", "payload_has_no_fields")
            return {"status": "rejected", "reason": "payload has no fields"}

        handler = {
            "general": lambda: self._ingest_enquiry("general", fields),
            "project": lambda: self._ingest_enquiry("project", fields),
            "careers": lambda: self._ingest_careers(fields),
            "media": lambda: self._ingest_media(fields, dedup_key),
        }[form_type]

        try:
            outcome = handler()
        except TallyIntakeError as exc:
            _record("rejected", str(exc))
            self.audit.append("TALLY_INTAKE_REJECTED", {
                "reason": str(exc), "form_type": form_type, "form_id": form_id, "actor": actor,
            })
            return {"status": "rejected", "reason": str(exc), "form_type": form_type}

        _record("ingested", None, **outcome)
        self.audit.append("TALLY_INTAKE_INGESTED", {
            "form_type": form_type, "tally_submission_id": dedup_key, "actor": actor, **outcome,
        })

        result: Dict[str, Any] = {"status": "ingested", "form_type": form_type, **outcome}
        if outcome.get("conversation_id"):
            from .comms_workforce import run_agent_for_conversation
            result["workforce_actions"] = run_agent_for_conversation(
                self.store, self.audit, outcome["conversation_id"], actor="ai_workforce",
            )["actions"]
        return result

    # -- per-form-type handlers ------------------------------------------

    def _ingest_enquiry(self, kind: str, fields: List[Dict[str, Any]]) -> Dict[str, Any]:
        name = _field_text(fields, "name")
        email = _field_text(fields, "email")
        message = _field_text(fields, "message") or _field_text(fields, "request")
        company = _field_text(fields, "company")
        if not name:
            raise TallyIntakeError("could not match a name field by label")
        if not email or not _EMAIL_RE.match(email):
            raise TallyIntakeError("could not match a valid email field by label")
        if not message:
            raise TallyIntakeError("could not match a message field by label")

        extra: Dict[str, Any] = {}
        if kind == "project":
            budget = _field_text(fields, "budget")
            timeline = _field_text(fields, "timeline")
            service = _field_text(fields, "service")
            if budget:
                extra["budget_hint"] = budget
            if timeline:
                extra["timeline"] = timeline
            if service:
                extra["project_type"] = service
                extra["project_title"] = f"Project enquiry: {service}"

        try:
            result = self.enquiries.submit(kind, name, email, company, message, extra=extra or None)
        except ContentError as exc:
            raise TallyIntakeError(str(exc)) from exc
        return {
            "conversation_id": result.get("conversation_id"), "enquiry_id": result.get("enquiry_id"),
            "opportunity_id": result.get("opportunity_id"),
        }

    def _ingest_careers(self, fields: List[Dict[str, Any]]) -> Dict[str, Any]:
        name = _field_text(fields, "name")
        email = _field_text(fields, "email")
        role = _field_text(fields, "role") or "General Application"
        if not name:
            raise TallyIntakeError("could not match an applicant name field by label")
        if not email or not _EMAIL_RE.match(email):
            raise TallyIntakeError("could not match a valid applicant email field by label")

        resume_files = _field_files(fields, "resume")
        resume_meta = resume_files[0] if resume_files else None
        links_field = _field_text(fields, "links")

        application_id = self.applications.create({
            "job_title_snapshot": role,
            "applicant_name": name, "applicant_email": email,
            "applicant_phone": _field_text(fields, "phone"),
            "links": [links_field] if links_field else [],
            "cover_note": _field_text(fields, "message"),
            # Metadata only -- resume bytes are never fetched from Tally's
            # hosted URL in this pass (see module docstring).
            "resume_filename": (resume_meta or {}).get("name"),
            "resume_size_bytes": (resume_meta or {}).get("size"),
        })

        conv_matches = self.store.list("comm_conversations", "linked_application_id=?", (application_id,))
        conversation_id = conv_matches[0]["id"] if conv_matches else None
        if resume_meta and conversation_id:
            self.comms.add_message(
                conversation_id, "INBOUND",
                f"Resume on file via Tally: {resume_meta.get('name', '(unnamed)')} "
                f"({resume_meta.get('mimeType', 'unknown type')}, {resume_meta.get('size', '?')} bytes). "
                "Metadata only -- not fetched/stored locally in this pass.",
                kind="note", is_internal_note=True, actor="tally_intake",
            )
        elif not resume_meta and conversation_id:
            self.comms.add_message(
                conversation_id, "INBOUND",
                "No resume file was found on this Tally submission's resume field.",
                kind="note", is_internal_note=True, actor="tally_intake",
            )

        return {"conversation_id": conversation_id, "application_id": application_id}

    def _ingest_media(self, fields: List[Dict[str, Any]], submission_id: Optional[str]) -> Dict[str, Any]:
        name = _field_text(fields, "name")
        email = _field_text(fields, "email")
        outlet = _field_text(fields, "outlet") or _field_text(fields, "company")
        request_text = _field_text(fields, "request") or _field_text(fields, "message")
        deadline = _field_text(fields, "media_deadline")
        if not name:
            raise TallyIntakeError("could not match a contact name field by label")
        if not email or not _EMAIL_RE.match(email):
            raise TallyIntakeError("could not match a valid contact email field by label")
        if not request_text:
            raise TallyIntakeError("could not match a request/message field by label")

        domain = email.split("@")[-1].lower() if "@" in email else None
        org_id = self.comms.find_or_create_organization(outlet, domain)
        contact_id = self.comms.find_or_create_contact(email, name, org_id)
        subject = f"Media enquiry from {name}" + (f" ({outlet})" if outlet else "")
        body = request_text if not deadline else f"{request_text}\n\nDeadline: {deadline}"
        conv = self.comms.open_conversation(
            "WEBSITE", "media", subject=subject, contact_id=contact_id, organization_id=org_id,
            priority="normal", tags=["website", "media"], actor="tally_intake",
            source_ref_type="tally_submission", source_ref_id=submission_id,
        )
        self.comms.add_message(
            conv["id"], "INBOUND", body, sender_contact_id=contact_id,
            source_ref_type="tally_submission", source_ref_id=submission_id, actor="tally_intake",
        )
        return {"conversation_id": conv["id"]}
