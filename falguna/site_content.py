"""Content-access layer for the public website's scalable content tables
(site_services, site_case_studies, site_products, site_posts, site_jobs,
site_applications, site_enquiries). Thin wrappers over StateStore's generic
create/get/update/list -- deliberately not a new ORM, matching how the rest
of the codebase (OpportunityStore, WorkforceTaskStore, ...) is built.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .store import StateStore


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def hash_ip(ip: Optional[str]) -> Optional[str]:
    if not ip:
        return None
    return hashlib.sha256(ip.encode("utf-8")).hexdigest()[:32]


class ContentError(Exception):
    pass


class ServiceStore:
    def __init__(self, store: StateStore):
        self.store = store

    def upsert(self, fields: Dict[str, Any]) -> str:
        existing = self.store.list("site_services", "slug = ?", (fields["slug"],))
        now = utcnow()
        payload = {
            "slug": fields["slug"], "division": fields["division"], "tagline": fields["tagline"],
            "summary": fields["summary"],
            "deliverables_json": json.dumps(fields.get("deliverables", [])),
            "process_json": json.dumps(fields.get("process", [])),
            "proof_slugs_json": json.dumps(fields.get("proof_slugs", [])),
            "status": fields.get("status", "current"),
            "sort_order": fields.get("sort_order", 0),
            "updated_at": now,
        }
        if existing:
            self.store.update("site_services", existing[0]["id"], **payload)
            return existing[0]["id"]
        payload["created_at"] = now
        return self.store.create("site_services", payload)

    def list_all(self) -> List[Dict[str, Any]]:
        rows = self.store.list("site_services")
        rows.sort(key=lambda r: (r.get("sort_order", 0), r.get("division", "")))
        for r in rows:
            r["deliverables"] = json.loads(r.get("deliverables_json") or "[]")
            r["process"] = json.loads(r.get("process_json") or "[]")
            r["proof_slugs"] = json.loads(r.get("proof_slugs_json") or "[]")
        return rows

    def get_by_slug(self, slug: str) -> Optional[Dict[str, Any]]:
        rows = self.store.list("site_services", "slug = ?", (slug,))
        if not rows:
            return None
        row = rows[0]
        row["deliverables"] = json.loads(row.get("deliverables_json") or "[]")
        row["process"] = json.loads(row.get("process_json") or "[]")
        row["proof_slugs"] = json.loads(row.get("proof_slugs_json") or "[]")
        return row


class CaseStudyStore:
    def __init__(self, store: StateStore):
        self.store = store

    def upsert(self, fields: Dict[str, Any]) -> str:
        existing = self.store.list("site_case_studies", "slug = ?", (fields["slug"],))
        now = utcnow()
        payload = {
            "slug": fields["slug"], "title": fields["title"], "client_label": fields["client_label"],
            "is_own_project": 1 if fields.get("is_own_project") else 0,
            "summary": fields["summary"], "problem": fields["problem"],
            "approach": fields["approach"], "outcome": fields["outcome"],
            "stack_json": json.dumps(fields.get("stack", [])),
            "division_slugs_json": json.dumps(fields.get("division_slugs", [])),
            "sort_order": fields.get("sort_order", 0),
            "published": 1 if fields.get("published", True) else 0,
            "updated_at": now,
        }
        if existing:
            self.store.update("site_case_studies", existing[0]["id"], **payload)
            return existing[0]["id"]
        payload["created_at"] = now
        return self.store.create("site_case_studies", payload)

    def _decorate(self, row: Dict[str, Any]) -> Dict[str, Any]:
        row["stack"] = json.loads(row.get("stack_json") or "[]")
        row["division_slugs"] = json.loads(row.get("division_slugs_json") or "[]")
        return row

    def list_published(self) -> List[Dict[str, Any]]:
        rows = self.store.list("site_case_studies", "published = 1")
        rows.sort(key=lambda r: r.get("sort_order", 0))
        return [self._decorate(r) for r in rows]

    def get_by_slug(self, slug: str) -> Optional[Dict[str, Any]]:
        rows = self.store.list("site_case_studies", "slug = ?", (slug,))
        return self._decorate(rows[0]) if rows else None


class ProductStore:
    def __init__(self, store: StateStore):
        self.store = store

    def upsert(self, fields: Dict[str, Any]) -> str:
        existing = self.store.list("site_products", "slug = ?", (fields["slug"],))
        now = utcnow()
        payload = {
            "slug": fields["slug"], "name": fields["name"], "tagline": fields["tagline"],
            "summary": fields["summary"], "status": fields["status"],
            "is_internal": 1 if fields.get("is_internal") else 0,
            "sort_order": fields.get("sort_order", 0), "updated_at": now,
        }
        if existing:
            self.store.update("site_products", existing[0]["id"], **payload)
            return existing[0]["id"]
        payload["created_at"] = now
        return self.store.create("site_products", payload)

    def list_all(self) -> List[Dict[str, Any]]:
        rows = self.store.list("site_products")
        rows.sort(key=lambda r: r.get("sort_order", 0))
        return rows


class PostStore:
    """Insights publishing. Ships empty in V1 -- no fabricated posts,
    dates, or announcements, per instruction. Real posts get added later
    through this same store (create() then a separate publish() call)."""

    def __init__(self, store: StateStore):
        self.store = store

    def create_draft(self, title: str, slug: str, dek: str, body_md: str, author: str,
                       category: str = "engineering") -> str:
        now = utcnow()
        return self.store.create("site_posts", {
            "slug": slug, "title": title, "dek": dek, "body_md": body_md, "author": author,
            "category": category, "published": 0, "published_at": None,
            "created_at": now, "updated_at": now,
        })

    def publish(self, post_id: str) -> None:
        self.store.update("site_posts", post_id, published=1, published_at=utcnow(), updated_at=utcnow())

    def list_published(self) -> List[Dict[str, Any]]:
        rows = self.store.list("site_posts", "published = 1")
        rows.sort(key=lambda r: r.get("published_at") or "", reverse=True)
        return rows

    def get_by_slug(self, slug: str) -> Optional[Dict[str, Any]]:
        rows = self.store.list("site_posts", "slug = ? AND published = 1", (slug,))
        return rows[0] if rows else None


class JobStore:
    """Careers listings. Ships empty in V1 -- do not post nonexistent
    vacancies. A real opening is added the same way this seed data would
    be (JobStore.create), and shows up immediately on /careers."""

    def __init__(self, store: StateStore):
        self.store = store

    def create(self, fields: Dict[str, Any]) -> str:
        now = utcnow()
        return self.store.create("site_jobs", {
            "slug": fields["slug"], "title": fields["title"], "department": fields["department"],
            "employment_type": fields["employment_type"], "location_policy": fields["location_policy"],
            "summary": fields["summary"],
            "responsibilities_json": json.dumps(fields.get("responsibilities", [])),
            "requirements_json": json.dumps(fields.get("requirements", [])),
            "status": fields.get("status", "open"),
            "posted_at": fields.get("posted_at", now),
            "created_at": now, "updated_at": now,
        })

    def _decorate(self, row: Dict[str, Any]) -> Dict[str, Any]:
        row["responsibilities"] = json.loads(row.get("responsibilities_json") or "[]")
        row["requirements"] = json.loads(row.get("requirements_json") or "[]")
        return row

    def list_open(self) -> List[Dict[str, Any]]:
        rows = self.store.list("site_jobs", "status = 'open'")
        rows.sort(key=lambda r: r.get("posted_at") or "", reverse=True)
        return [self._decorate(r) for r in rows]

    def get_by_slug(self, slug: str) -> Optional[Dict[str, Any]]:
        rows = self.store.list("site_jobs", "slug = ?", (slug,))
        return self._decorate(rows[0]) if rows else None


class ApplicationStore:
    """Careers application intake. Resume bytes are written to disk under
    the app root's .falguna/site_uploads/ (never served back publicly);
    only the DB row (metadata + path) is queryable from the internal
    applicant-management view.

    When a `comms_store` is supplied, every real application also opens a
    Communications Center conversation under the careers department
    (Milestone 9 groundwork), linked back to the application row rather
    than duplicating its data."""

    def __init__(self, store: StateStore, comms_store=None):
        self.store = store
        self.comms_store = comms_store

    def create(self, fields: Dict[str, Any]) -> str:
        now = utcnow()
        application_id = self.store.create("site_applications", {
            "job_id": fields.get("job_id"),
            "job_title_snapshot": fields["job_title_snapshot"],
            "applicant_name": fields["applicant_name"],
            "applicant_email": fields["applicant_email"],
            "applicant_phone": fields.get("applicant_phone"),
            "links_json": json.dumps(fields.get("links", [])),
            "cover_note": fields.get("cover_note"),
            "resume_filename": fields.get("resume_filename"),
            "resume_storage_rel_path": fields.get("resume_storage_rel_path"),
            "resume_sha256": fields.get("resume_sha256"),
            "resume_size_bytes": fields.get("resume_size_bytes"),
            "status": "new",
            "source_ip_hash": fields.get("source_ip_hash"),
            "created_at": now, "updated_at": now,
        })

        if self.comms_store is not None:
            email = fields["applicant_email"]
            contact_id = self.comms_store.find_or_create_contact(email, fields["applicant_name"], None)
            conv = self.comms_store.open_conversation(
                "CAREERS", "careers", subject=f"Application: {fields['job_title_snapshot']}",
                contact_id=contact_id, priority="normal", tags=["careers"], actor="website",
                linked_application_id=application_id,
                source_ref_type="site_application", source_ref_id=application_id,
            )
            note = fields.get("cover_note") or f"Applied for {fields['job_title_snapshot']} -- resume attached ({fields.get('resume_filename', 'no filename on file')})."
            self.comms_store.add_message(
                conv["id"], "INBOUND", note, sender_contact_id=contact_id,
                source_ref_type="site_application", source_ref_id=application_id, actor="website",
            )

        return application_id

    def list_all(self) -> List[Dict[str, Any]]:
        rows = self.store.list("site_applications")
        rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        for r in rows:
            r["links"] = json.loads(r.get("links_json") or "[]")
        return rows

    def set_status(self, application_id: str, status: str) -> None:
        if status not in {"new", "reviewed", "rejected", "shortlisted"}:
            raise ContentError("invalid application status")
        self.store.update("site_applications", application_id, status=status, updated_at=utcnow())


class EnquiryStore:
    """Contact / Start-a-Project intake. Every real submission is logged
    here for a full record AND, for project enquiries, handed to the
    existing OpportunityStore so it enters the real sales pipeline instead
    of a disconnected marketing-site inbox.

    When a `comms_store` (falguna.comms.CommsStore) is supplied, every
    submission -- general and project alike -- also opens a real Unified
    Communications Center conversation (Milestone 1/3 of the Communications
    + AI Customer Service sprint), linked to the same opportunity rather
    than duplicating it, so the website's front door and the Communications
    Center never disagree about what came in."""

    def __init__(self, store: StateStore, opportunity_store=None, comms_store=None):
        self.store = store
        self.opportunity_store = opportunity_store
        self.comms_store = comms_store

    def submit(self, kind: str, name: str, email: str, company: Optional[str], message: str,
                extra: Optional[Dict[str, Any]] = None, source_ip: Optional[str] = None) -> Dict[str, Any]:
        if kind not in {"general", "project"}:
            raise ContentError("invalid enquiry kind")
        if not name.strip() or not email.strip() or "@" not in email or not message.strip():
            raise ContentError("name, a valid email, and a message are required")

        opportunity_id = None
        if kind == "project" and self.opportunity_store is not None:
            extra = extra or {}
            opportunity_id = self.opportunity_store.create({
                "title": extra.get("project_title") or f"Website enquiry: {name.strip()}",
                "client_name": company.strip() if company else name.strip(),
                "description": message.strip(),
                "budget_rate": extra.get("budget_hint"),
                "contract_type": extra.get("project_type"),
                "location_timezone": extra.get("timezone"),
                "urgency": extra.get("timeline"),
            }, actor="website", source="website_project_intake")

        row_id = self.store.create("site_enquiries", {
            "kind": kind, "name": name.strip(), "email": email.strip(),
            "company": (company or "").strip() or None, "message": message.strip(),
            "opportunity_id": opportunity_id,
            "source_ip_hash": hash_ip(source_ip),
            "created_at": utcnow(),
        })

        conversation_id = None
        if self.comms_store is not None:
            domain = email.strip().split("@")[-1].lower() if "@" in email else None
            org_id = self.comms_store.find_or_create_organization(company or None, domain)
            contact_id = self.comms_store.find_or_create_contact(email, name, org_id)
            department = "sales" if kind == "project" else "general"
            subject = (extra or {}).get("project_title") if extra else None
            subject = subject or f"{'Project enquiry' if kind == 'project' else 'General enquiry'} from {name.strip()}"
            conv = self.comms_store.open_conversation(
                "WEBSITE", department, subject=subject, contact_id=contact_id, organization_id=org_id,
                priority="high" if kind == "project" else "normal",
                tags=["website", kind], actor="website",
                linked_opportunity_id=opportunity_id,
                source_ref_type="site_enquiry", source_ref_id=row_id,
            )
            conversation_id = conv["id"]
            self.comms_store.add_message(
                conversation_id, "INBOUND", message.strip(), sender_contact_id=contact_id,
                source_ref_type="site_enquiry", source_ref_id=row_id, actor="website",
            )

        return {"enquiry_id": row_id, "opportunity_id": opportunity_id, "conversation_id": conversation_id}
