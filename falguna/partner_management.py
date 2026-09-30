"""Sales Partner Pilot V1 (Priority 4).

Internal pilot foundation only: no public signup, no external partner
communication, no live partner onboarding, no automatic payouts, no
production deployment. Synthetic test data only in this pass.

This module deliberately does not create a parallel CRM or a parallel
billing ledger. A referral becomes a real `rh_opportunities` row (via
`revenue_hunter.OpportunityStore`) once a human attributes it, and the
existing Revenue & Delivery Engine / billing chain (proposal -> closing ->
onboarding -> Active Job -> completion -> `rh_invoices`) carries it from
there completely unmodified. `CommissionStore` never writes to
`rh_invoices` -- it only ever reads the linked invoice's status and
amount_received to compute commission eligibility, so "a quote is not
revenue" and "an invoice is not payment" stay literally true: nothing here
can manufacture eligibility, only a real, evidenced payment recorded
through `billing.BillingStore.record_payment` can.

Three lifecycles, three stores:
  PartnerStore    -- PENDING -> APPROVED -> SUSPENDED/TERMINATED, full audit trail.
  ReferralStore   -- registration, deterministic duplicate detection, a
                     human-gated duplicate-review queue, and time-limited
                     attribution (PENDING_REVIEW -> ATTRIBUTED -> EXPIRED/
                     DISPUTED/REJECTED). Never silently overwrites a valid
                     ATTRIBUTED referral.
  CommissionStore -- NOT_ELIGIBLE -> PROVISIONAL -> ELIGIBLE, with HELD and
                     REVERSED as human-gated exits (refund/chargeback/
                     dispute). PAID is a defined-but-unreachable future
                     state: no code path in this module ever sets it,
                     since payout execution is explicitly out of scope.
"""

import json
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from .audit import AuditLog
from .revenue_hunter import OpportunityStore
from .store import StateStore, utcnow

# ---------------------------------------------------------------------------
# Shared normalization helpers (duplicate detection)
# ---------------------------------------------------------------------------


def _normalize_email(value: Optional[str]) -> Optional[str]:
    value = (value or "").strip().lower()
    return value or None


def _normalize_phone(value: Optional[str]) -> Optional[str]:
    digits = re.sub(r"\D", "", value or "")
    return digits or None


def _normalize_domain(email: Optional[str] = None, website: Optional[str] = None) -> Optional[str]:
    source = (website or email or "").strip().lower()
    if not source:
        return None
    if "@" in source:
        source = source.split("@", 1)[1]
    source = re.sub(r"^https?://", "", source)
    source = source.split("/")[0]
    if source.startswith("www."):
        source = source[4:]
    return source or None


def _normalize_org(value: Optional[str]) -> Optional[str]:
    value = re.sub(r"[^a-z0-9]+", " ", (value or "").strip().lower()).strip()
    return value or None


def _add_days(iso_ts: str, days: int) -> str:
    return (datetime.fromisoformat(iso_ts) + timedelta(days=days)).isoformat()


# ---------------------------------------------------------------------------
# Partners
# ---------------------------------------------------------------------------

PARTNER_STATUSES = {"PENDING", "APPROVED", "SUSPENDED", "TERMINATED"}


class PartnerError(ValueError):
    pass


class PartnerStore:
    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit

    def register(self, fields: Dict[str, Any], actor: str) -> str:
        full_name = (fields.get("full_name") or "").strip()
        if not full_name:
            raise PartnerError("full_name is required")
        email = (fields.get("email") or "").strip().lower()
        if not email:
            raise PartnerError("email is required")
        if not fields.get("agreement_accepted"):
            raise PartnerError("agreement/terms acceptance is required to register a partner")
        now = utcnow()
        partner_id = self.store.create("pm_partners", {
            "full_name": full_name,
            "organization_name": (fields.get("organization_name") or "").strip() or None,
            "email": email,
            "phone": (fields.get("phone") or "").strip() or None,
            "region": fields.get("region") or None,
            "country": fields.get("country") or None,
            "service_categories_json": json.dumps(list(fields.get("service_categories") or [])),
            "assigned_manager": fields.get("assigned_manager") or None,
            "verification_status": "UNVERIFIED",
            "agreement_accepted": 1,
            "agreement_accepted_at": now,
            "agreement_reference": fields.get("agreement_reference") or None,
            "status": "PENDING",
            "approved_at": None, "approved_by": None,
            "suspended_at": None, "suspended_by": None, "suspension_reason": None,
            "terminated_at": None, "terminated_by": None, "termination_reason": None,
            "created_at": now, "updated_at": now,
        })
        self._record_status_event(partner_id, None, "PENDING", actor, "registered")
        self.audit.append("PM_PARTNER_REGISTERED", {"partner_id": partner_id, "email": email, "actor": actor})
        return partner_id

    def _require(self, partner_id: str) -> Dict[str, Any]:
        partner = self.store.get("pm_partners", partner_id)
        if not partner:
            raise PartnerError("partner not found")
        return partner

    def _record_status_event(self, partner_id: str, from_status: Optional[str], to_status: str, actor: str, reason: Optional[str]) -> None:
        self.store.create("pm_partner_status_events", {
            "partner_id": partner_id, "from_status": from_status, "to_status": to_status,
            "actor": actor, "reason": reason, "created_at": utcnow(),
        })

    def approve(self, partner_id: str, actor: str, verification_status: str = "VERIFIED") -> Dict[str, Any]:
        partner = self._require(partner_id)
        if partner["status"] != "PENDING":
            raise PartnerError(f"partner must be PENDING to approve (currently {partner['status']})")
        now = utcnow()
        self.store.update("pm_partners", partner_id, status="APPROVED", approved_at=now, approved_by=actor, verification_status=verification_status)
        self._record_status_event(partner_id, "PENDING", "APPROVED", actor, "approved")
        self.audit.append("PM_PARTNER_APPROVED", {"partner_id": partner_id, "actor": actor})
        return self.store.get("pm_partners", partner_id)

    def suspend(self, partner_id: str, actor: str, reason: str) -> Dict[str, Any]:
        partner = self._require(partner_id)
        if partner["status"] != "APPROVED":
            raise PartnerError(f"partner must be APPROVED to suspend (currently {partner['status']})")
        if not (reason or "").strip():
            raise PartnerError("a reason is required to suspend a partner")
        now = utcnow()
        self.store.update("pm_partners", partner_id, status="SUSPENDED", suspended_at=now, suspended_by=actor, suspension_reason=reason)
        self._record_status_event(partner_id, "APPROVED", "SUSPENDED", actor, reason)
        self.audit.append("PM_PARTNER_SUSPENDED", {"partner_id": partner_id, "actor": actor, "reason": reason})
        return self.store.get("pm_partners", partner_id)

    def reinstate(self, partner_id: str, actor: str) -> Dict[str, Any]:
        partner = self._require(partner_id)
        if partner["status"] != "SUSPENDED":
            raise PartnerError(f"partner must be SUSPENDED to reinstate (currently {partner['status']})")
        now = utcnow()
        self.store.update("pm_partners", partner_id, status="APPROVED", suspended_at=None, suspended_by=None, suspension_reason=None, approved_at=now, approved_by=actor)
        self._record_status_event(partner_id, "SUSPENDED", "APPROVED", actor, "reinstated")
        self.audit.append("PM_PARTNER_REINSTATED", {"partner_id": partner_id, "actor": actor})
        return self.store.get("pm_partners", partner_id)

    def terminate(self, partner_id: str, actor: str, reason: str) -> Dict[str, Any]:
        partner = self._require(partner_id)
        if partner["status"] == "TERMINATED":
            raise PartnerError("partner is already TERMINATED")
        if not (reason or "").strip():
            raise PartnerError("a reason is required to terminate a partner")
        now = utcnow()
        from_status = partner["status"]
        self.store.update("pm_partners", partner_id, status="TERMINATED", terminated_at=now, terminated_by=actor, termination_reason=reason)
        self._record_status_event(partner_id, from_status, "TERMINATED", actor, reason)
        self.audit.append("PM_PARTNER_TERMINATED", {"partner_id": partner_id, "actor": actor, "reason": reason})
        return self.store.get("pm_partners", partner_id)

    def get(self, partner_id: str) -> Optional[Dict[str, Any]]:
        partner = self.store.get("pm_partners", partner_id)
        if not partner:
            return None
        partner = dict(partner)
        partner["status_history"] = self.store.list("pm_partner_status_events", "partner_id=?", (partner_id,))
        return partner

    def list(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        if status:
            if status not in PARTNER_STATUSES:
                raise PartnerError(f"status must be one of {sorted(PARTNER_STATUSES)}")
            rows = self.store.list("pm_partners", "status=?", (status,))
        else:
            rows = self.store.list("pm_partners")
        return list(reversed(rows))


# ---------------------------------------------------------------------------
# Referrals -- registration, duplicate detection, attribution
# ---------------------------------------------------------------------------

ATTRIBUTION_STATUSES = {"PENDING_REVIEW", "ATTRIBUTED", "REJECTED", "EXPIRED", "DISPUTED"}
DEFAULT_ATTRIBUTION_WINDOW_DAYS = 90


class ReferralError(ValueError):
    pass


class ReferralStore:
    def __init__(self, store: StateStore, audit: AuditLog, partners: Optional[PartnerStore] = None):
        self.store = store
        self.audit = audit
        self.partners = partners or PartnerStore(store, audit)

    def register(self, partner_id: str, fields: Dict[str, Any], actor: str) -> str:
        partner = self.partners._require(partner_id)
        if partner["status"] != "APPROVED":
            raise ReferralError(f"partner must be APPROVED to register a referral (currently {partner['status']})")
        prospect_name = (fields.get("prospect_name") or fields.get("organization_name") or "").strip()
        if not prospect_name:
            raise ReferralError("prospect_name or organization_name is required")
        requested_service = (fields.get("requested_service") or "").strip()
        if not requested_service:
            raise ReferralError("requested_service is required")
        email = _normalize_email(fields.get("contact_email"))
        phone = _normalize_phone(fields.get("contact_phone"))
        org = _normalize_org(fields.get("organization_name"))
        domain = _normalize_domain(email=fields.get("contact_email"), website=fields.get("website"))
        estimated_value = fields.get("estimated_value")
        now = utcnow()
        referral_id = self.store.create("pm_referrals", {
            "partner_id": partner_id,
            "prospect_name": prospect_name,
            "organization_name": (fields.get("organization_name") or "").strip() or None,
            "contact_email": (fields.get("contact_email") or "").strip() or None,
            "contact_phone": (fields.get("contact_phone") or "").strip() or None,
            "region": fields.get("region") or None,
            "requested_service": requested_service,
            "estimated_value": float(estimated_value) if estimated_value not in (None, "") else None,
            "referral_source": fields.get("referral_source") or "partner_portal",
            "notes": fields.get("notes") or None,
            "normalized_email": email, "normalized_phone": phone, "normalized_domain": domain, "normalized_org": org,
            "attribution_status": "PENDING_REVIEW",
            "attribution_start": None, "attribution_expiry": None,
            "opportunity_id": None, "duplicate_flag": 0,
            "created_at": now, "updated_at": now,
        })
        self.audit.append("PM_REFERRAL_REGISTERED", {"referral_id": referral_id, "partner_id": partner_id, "actor": actor})
        self._detect_and_flag_duplicates(referral_id, email, phone, domain, org, actor)
        return referral_id

    def _detect_and_flag_duplicates(self, referral_id: str, email, phone, domain, org, actor: str) -> None:
        competing_referrals = []
        for row in self.store.list("pm_referrals"):
            if row["id"] == referral_id or row["attribution_status"] in ("REJECTED", "EXPIRED"):
                continue
            if (
                (email and row.get("normalized_email") == email)
                or (phone and row.get("normalized_phone") == phone)
                or (domain and row.get("normalized_domain") == domain)
                or (org and row.get("normalized_org") == org)
            ):
                competing_referrals.append(row)
        existing_clients = [c for c in self.store.list("clients") if org and _normalize_org(c.get("name")) == org]
        if not competing_referrals and not existing_clients:
            return
        reasons = []
        if email and any(r.get("normalized_email") == email for r in competing_referrals):
            reasons.append("email")
        if phone and any(r.get("normalized_phone") == phone for r in competing_referrals):
            reasons.append("phone")
        if domain and any(r.get("normalized_domain") == domain for r in competing_referrals):
            reasons.append("domain")
        if org and any(r.get("normalized_org") == org for r in competing_referrals):
            reasons.append("organization_name")
        if existing_clients:
            reasons.append("existing_customer")
        now = utcnow()
        competing_ids = [r["id"] for r in competing_referrals] + [f"client:{c['id']}" for c in existing_clients]
        review_id = self.store.create("pm_duplicate_reviews", {
            "referral_id": referral_id, "competing_referral_ids_json": json.dumps(competing_ids),
            "detected_reason": ",".join(reasons) or "match", "status": "OPEN",
            "reviewer": None, "decision": None, "awarded_referral_id": None, "resolution_reason": None,
            "created_at": now, "resolved_at": None,
        })
        self.store.update("pm_referrals", referral_id, duplicate_flag=1)
        for row in competing_referrals:
            # Never silently touch a referral that is already validly
            # ATTRIBUTED -- flagging it would make attribute()/other calls
            # start refusing an otherwise-legitimate, already-decided
            # referral. Only PENDING_REVIEW candidates are paused for review.
            if row["attribution_status"] == "PENDING_REVIEW":
                self.store.update("pm_referrals", row["id"], duplicate_flag=1)
        self.audit.append("PM_DUPLICATE_DETECTED", {
            "referral_id": referral_id, "review_id": review_id,
            "competing": competing_ids, "reasons": reasons, "actor": actor,
        })

    def resolve_duplicate_review(
        self, review_id: str, actor: str, decision: str,
        reason: Optional[str] = None, awarded_referral_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        review = self.store.get("pm_duplicate_reviews", review_id)
        if not review:
            raise ReferralError("duplicate review not found")
        if review["status"] != "OPEN":
            raise ReferralError("duplicate review is already resolved")
        raw_ids = [review["referral_id"]] + json.loads(review["competing_referral_ids_json"])
        candidate_ids = [cid for cid in dict.fromkeys(raw_ids) if not str(cid).startswith("client:")]
        candidates = [c for c in (self.store.get("pm_referrals", cid) for cid in candidate_ids) if c]
        pending = [c for c in candidates if c["attribution_status"] == "PENDING_REVIEW"]
        already_attributed_ids = {c["id"] for c in candidates if c["attribution_status"] == "ATTRIBUTED"}

        if decision == "AWARD":
            if not awarded_referral_id:
                raise ReferralError("awarded_referral_id is required for an AWARD decision")
            if already_attributed_ids and awarded_referral_id not in already_attributed_ids:
                raise ReferralError(
                    "another referral in this conflict is already ATTRIBUTED -- "
                    "awarding over an existing valid attribution is not allowed; "
                    "use raise_dispute against it instead"
                )
            if awarded_referral_id not in {c["id"] for c in pending}:
                raise ReferralError("awarded_referral_id must be one of the pending candidates in this review")
            for c in pending:
                if c["id"] != awarded_referral_id:
                    self.store.update("pm_referrals", c["id"], attribution_status="REJECTED")
            self.store.update("pm_referrals", awarded_referral_id, duplicate_flag=0)
        elif decision == "REJECT_ALL":
            for c in pending:
                self.store.update("pm_referrals", c["id"], attribution_status="REJECTED")
        elif decision == "NO_CONFLICT":
            for c in pending:
                self.store.update("pm_referrals", c["id"], duplicate_flag=0)
        else:
            raise ReferralError("decision must be one of AWARD, REJECT_ALL, NO_CONFLICT")

        self.store.update(
            "pm_duplicate_reviews", review_id, status="RESOLVED", reviewer=actor, decision=decision,
            awarded_referral_id=awarded_referral_id, resolution_reason=reason, resolved_at=utcnow(),
        )
        self.audit.append("PM_DUPLICATE_REVIEW_RESOLVED", {
            "review_id": review_id, "actor": actor, "decision": decision,
            "awarded_referral_id": awarded_referral_id, "reason": reason,
        })
        return self.store.get("pm_duplicate_reviews", review_id)


    def attribute(self, referral_id: str, actor: str, attribution_days: int = DEFAULT_ATTRIBUTION_WINDOW_DAYS) -> Dict[str, Any]:
        referral = self._require(referral_id)
        if referral["duplicate_flag"]:
            raise ReferralError("referral has an open duplicate review; resolve it before attribution")
        if referral["attribution_status"] != "PENDING_REVIEW":
            raise ReferralError(f"referral must be PENDING_REVIEW to attribute (currently {referral['attribution_status']})")
        now = utcnow()
        expiry = _add_days(now, attribution_days)
        self.store.update("pm_referrals", referral_id, attribution_status="ATTRIBUTED", attribution_start=now, attribution_expiry=expiry)
        self.audit.append("PM_REFERRAL_ATTRIBUTED", {"referral_id": referral_id, "actor": actor, "expiry": expiry})
        self._ensure_opportunity(referral_id, actor)
        return self.store.get("pm_referrals", referral_id)

    def _ensure_opportunity(self, referral_id: str, actor: str) -> str:
        referral = self.store.get("pm_referrals", referral_id)
        if referral.get("opportunity_id"):
            return referral["opportunity_id"]
        opportunity_id = OpportunityStore(self.store, self.audit).create({
            "title": f"{referral['prospect_name']} - {referral['requested_service']}",
            "client_name": referral.get("organization_name") or referral["prospect_name"],
            "description": referral.get("notes") or f"Partner referral: {referral['requested_service']}",
            "budget_rate": str(referral["estimated_value"]) if referral.get("estimated_value") else None,
        }, actor=actor, source="partner_referral")
        self.store.update("pm_referrals", referral_id, opportunity_id=opportunity_id)
        self.audit.append("PM_REFERRAL_LINKED_TO_OPPORTUNITY", {"referral_id": referral_id, "opportunity_id": opportunity_id, "actor": actor})
        return opportunity_id

    def reject(self, referral_id: str, actor: str, reason: Optional[str] = None) -> Dict[str, Any]:
        referral = self._require(referral_id)
        if referral["attribution_status"] not in ("PENDING_REVIEW", "DISPUTED"):
            raise ReferralError(f"referral must be PENDING_REVIEW or DISPUTED to reject (currently {referral['attribution_status']})")
        self.store.update("pm_referrals", referral_id, attribution_status="REJECTED")
        self.audit.append("PM_REFERRAL_REJECTED", {"referral_id": referral_id, "actor": actor, "reason": reason})
        return self.store.get("pm_referrals", referral_id)

    def check_expiry(self, referral_id: str) -> Dict[str, Any]:
        """Explicit, evidence-based -- same convention as
        billing.BillingStore.check_overdue: no background clock in this
        codebase, so a caller (dashboard refresh, HQ action) invokes this
        when it wants the status re-checked against today's real time."""
        referral = self._require(referral_id)
        if referral["attribution_status"] != "ATTRIBUTED" or not referral.get("attribution_expiry"):
            return referral
        if referral["attribution_expiry"] < utcnow():
            self.store.update("pm_referrals", referral_id, attribution_status="EXPIRED")
            self.audit.append("PM_REFERRAL_ATTRIBUTION_EXPIRED", {"referral_id": referral_id})
            return self.store.get("pm_referrals", referral_id)
        return referral

    def raise_dispute(self, referral_id: str, actor: str, reason: str) -> Dict[str, Any]:
        referral = self._require(referral_id)
        if referral["attribution_status"] != "ATTRIBUTED":
            raise ReferralError(f"referral must be ATTRIBUTED to raise a dispute (currently {referral['attribution_status']})")
        if not (reason or "").strip():
            raise ReferralError("a reason is required to raise a dispute")
        self.store.update("pm_referrals", referral_id, attribution_status="DISPUTED")
        self.audit.append("PM_REFERRAL_ATTRIBUTION_DISPUTED", {"referral_id": referral_id, "actor": actor, "reason": reason})
        return self.store.get("pm_referrals", referral_id)

    def resolve_dispute(self, referral_id: str, actor: str, decision: str, reason: Optional[str] = None) -> Dict[str, Any]:
        referral = self._require(referral_id)
        if referral["attribution_status"] != "DISPUTED":
            raise ReferralError(f"referral must be DISPUTED to resolve (currently {referral['attribution_status']})")
        if decision == "REINSTATE":
            self.store.update("pm_referrals", referral_id, attribution_status="ATTRIBUTED")
        elif decision == "REJECT":
            self.store.update("pm_referrals", referral_id, attribution_status="REJECTED")
        else:
            raise ReferralError("decision must be REINSTATE or REJECT")
        self.audit.append("PM_REFERRAL_DISPUTE_RESOLVED", {"referral_id": referral_id, "actor": actor, "decision": decision, "reason": reason})
        return self.store.get("pm_referrals", referral_id)

    def _require(self, referral_id: str) -> Dict[str, Any]:
        referral = self.store.get("pm_referrals", referral_id)
        if not referral:
            raise ReferralError("referral not found")
        return referral

    def get(self, referral_id: str) -> Optional[Dict[str, Any]]:
        referral = self.store.get("pm_referrals", referral_id)
        return dict(referral) if referral else None

    def list(self, partner_id: Optional[str] = None, attribution_status: Optional[str] = None) -> List[Dict[str, Any]]:
        where = "1=1"
        params: List[Any] = []
        if partner_id:
            where += " AND partner_id=?"
            params.append(partner_id)
        if attribution_status:
            where += " AND attribution_status=?"
            params.append(attribution_status)
        return list(reversed(self.store.list("pm_referrals", where, params)))

    def list_duplicate_reviews(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        if status:
            rows = self.store.list("pm_duplicate_reviews", "status=?", (status,))
        else:
            rows = self.store.list("pm_duplicate_reviews")
        return list(reversed(rows))

    def get_duplicate_review(self, review_id: str) -> Optional[Dict[str, Any]]:
        return self.store.get("pm_duplicate_reviews", review_id)


# ---------------------------------------------------------------------------
# Commission accounting -- provisional only, no payout execution
# ---------------------------------------------------------------------------

COMMISSION_STATUSES = {"NOT_ELIGIBLE", "PROVISIONAL", "ELIGIBLE", "HELD", "REVERSED", "PAID"}
DEFAULT_COMMISSION_RATE = 0.10


class CommissionError(ValueError):
    pass


class CommissionStore:
    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit

    def _record_event(
        self, commission_id: str, event_type: str, status_before, status_after, actor: str,
        amount: Optional[float] = None, evidence: Any = None, reason: Optional[str] = None, event_ref: Optional[str] = None,
    ) -> None:
        self.store.create("pm_commission_events", {
            "commission_id": commission_id, "event_type": event_type, "amount": amount,
            "status_before": status_before, "status_after": status_after, "actor": actor,
            "evidence_json": json.dumps(evidence) if evidence is not None else None,
            "reason": reason, "event_ref": event_ref, "created_at": utcnow(),
        })

    def ensure_for_referral(self, referral_id: str, actor: str, rate: float = DEFAULT_COMMISSION_RATE) -> Dict[str, Any]:
        existing = self.get_for_referral(referral_id)
        if existing:
            return existing
        referral = self.store.get("pm_referrals", referral_id)
        if not referral:
            raise CommissionError("referral not found")
        now = utcnow()
        commission_id = self.store.create("pm_commissions", {
            "referral_id": referral_id, "partner_id": referral["partner_id"], "opportunity_id": referral.get("opportunity_id"),
            "invoice_id": None, "rate": rate, "status": "NOT_ELIGIBLE", "eligible_amount": 0.0, "refunded_amount": 0.0,
            "hold_reason": None, "created_at": now, "updated_at": now,
        })
        self.audit.append("PM_COMMISSION_CREATED", {"commission_id": commission_id, "referral_id": referral_id, "actor": actor})
        self._record_event(commission_id, "CREATED", None, "NOT_ELIGIBLE", actor)
        return self.store.get("pm_commissions", commission_id)

    def get_for_referral(self, referral_id: str) -> Optional[Dict[str, Any]]:
        rows = self.store.list("pm_commissions", "referral_id=?", (referral_id,))
        return rows[-1] if rows else None

    def sync_from_invoice(self, referral_id: str, actor: str) -> Dict[str, Any]:
        """Deterministic and read-only against the invoice: computes
        commission status purely from the linked rh_invoices row's current
        status/amount_received (owned by falguna.billing.BillingStore) and
        never mutates that invoice. A quote is not revenue, an accepted
        project is not payment, and a DRAFT invoice is definitely not
        payment -- only amount actually received moves this past
        PROVISIONAL. Calling this repeatedly against unchanged invoice
        state is a genuine no-op (no event appended, updated_at untouched),
        which is what makes replaying the same observation idempotent."""
        commission = self.get_for_referral(referral_id) or self.ensure_for_referral(referral_id, actor)
        if commission["status"] in ("HELD", "REVERSED"):
            # A human-held or already-reversed commission is never silently
            # recomputed back onto the automatic track -- release/resolve
            # explicitly first (release_hold / a fresh referral-level fix).
            return commission

        referral = self.store.get("pm_referrals", referral_id)
        opportunity_id = referral.get("opportunity_id") if referral else None
        invoice = None
        if opportunity_id:
            invoices = self.store.list("rh_invoices", "opportunity_id=?", (opportunity_id,))
            non_cancelled = [i for i in invoices if i["status"] != "CANCELLED"]
            invoice = (non_cancelled or invoices or [None])[-1]

        if not invoice:
            new_status, new_amount, invoice_id = "NOT_ELIGIBLE", 0.0, None
        else:
            invoice_id = invoice["id"]
            received = float(invoice.get("amount_received") or 0.0)
            if invoice["status"] == "CANCELLED":
                new_status, new_amount = ("REVERSED", 0.0) if received > 0 else ("NOT_ELIGIBLE", 0.0)
            elif received > 0:
                new_status = "ELIGIBLE"
                new_amount = round(received * float(commission["rate"]), 2)
            elif invoice["status"] == "DRAFT":
                new_status, new_amount = "NOT_ELIGIBLE", 0.0
            else:  # READY, SENT, OVERDUE, PARTIALLY_PAID-with-nothing-received (never happens) -- a real invoice exists but no cleared payment yet
                new_status, new_amount = "PROVISIONAL", 0.0

        unchanged = (
            commission["status"] == new_status
            and float(commission["eligible_amount"] or 0) == new_amount
            and commission.get("invoice_id") == invoice_id
        )
        if unchanged:
            return commission

        self.store.update("pm_commissions", commission["id"], status=new_status, eligible_amount=new_amount, invoice_id=invoice_id)
        self._record_event(
            commission["id"], "SYNCED", commission["status"], new_status, actor, amount=new_amount,
            reason=f"invoice={invoice_id} status={invoice['status'] if invoice else None}",
        )
        self.audit.append("PM_COMMISSION_SYNCED", {
            "commission_id": commission["id"], "referral_id": referral_id, "status": new_status, "amount": new_amount, "actor": actor,
        })
        return self.store.get("pm_commissions", commission["id"])

    def hold(self, referral_id: str, actor: str, reason: str) -> Dict[str, Any]:
        commission = self.get_for_referral(referral_id)
        if not commission:
            raise CommissionError("commission not found for referral")
        if commission["status"] not in ("PROVISIONAL", "ELIGIBLE"):
            raise CommissionError(f"commission must be PROVISIONAL or ELIGIBLE to hold (currently {commission['status']})")
        if not (reason or "").strip():
            raise CommissionError("a reason is required to hold a commission")
        self.store.update("pm_commissions", commission["id"], status="HELD", hold_reason=reason)
        self._record_event(commission["id"], "HELD", commission["status"], "HELD", actor, reason=reason)
        self.audit.append("PM_COMMISSION_HELD", {"commission_id": commission["id"], "referral_id": referral_id, "actor": actor, "reason": reason})
        return self.store.get("pm_commissions", commission["id"])

    def release_hold(self, referral_id: str, actor: str) -> Dict[str, Any]:
        commission = self.get_for_referral(referral_id)
        if not commission:
            raise CommissionError("commission not found for referral")
        if commission["status"] != "HELD":
            raise CommissionError(f"commission must be HELD to release (currently {commission['status']})")
        self.store.update("pm_commissions", commission["id"], status="PROVISIONAL", hold_reason=None)
        self._record_event(commission["id"], "RELEASED", "HELD", "PROVISIONAL", actor)
        self.audit.append("PM_COMMISSION_RELEASED", {"commission_id": commission["id"], "referral_id": referral_id, "actor": actor})
        return self.sync_from_invoice(referral_id, actor)

    def record_refund(
        self, referral_id: str, actor: str, amount: float, evidence: Any, reason: str, event_ref: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not evidence:
            raise CommissionError("a refund/chargeback cannot be recorded without evidence")
        commission = self.get_for_referral(referral_id)
        if not commission:
            raise CommissionError("commission not found for referral")
        if event_ref:
            replay = self.store.list("pm_commission_events", "commission_id=? AND event_ref=?", (commission["id"], event_ref))
            if replay:
                return self.store.get("pm_commissions", commission["id"])  # idempotent replay of the same event
        if commission["status"] not in ("ELIGIBLE", "PROVISIONAL", "HELD"):
            raise CommissionError(f"commission must be ELIGIBLE, PROVISIONAL, or HELD to record a refund (currently {commission['status']})")
        if amount is None or amount <= 0:
            raise CommissionError("refund amount must be a positive number")
        before_status = commission["status"]
        remaining = float(commission["eligible_amount"] or 0.0) - amount
        new_refunded = float(commission["refunded_amount"] or 0.0) + amount
        if remaining <= 0:
            new_status, new_amount = "REVERSED", 0.0
        else:
            new_status, new_amount = "HELD", round(remaining, 2)
        self.store.update(
            "pm_commissions", commission["id"], status=new_status, eligible_amount=new_amount,
            refunded_amount=new_refunded, hold_reason=(reason if new_status == "HELD" else None),
        )
        self._record_event(commission["id"], "REFUND_RECORDED", before_status, new_status, actor, amount=amount, evidence=evidence, reason=reason, event_ref=event_ref)
        self.audit.append("PM_COMMISSION_REFUND_RECORDED", {
            "commission_id": commission["id"], "referral_id": referral_id, "actor": actor, "amount": amount, "new_status": new_status,
        })
        return self.store.get("pm_commissions", commission["id"])

    def get(self, commission_id: str) -> Optional[Dict[str, Any]]:
        return self.store.get("pm_commissions", commission_id)

    def list(self, partner_id: Optional[str] = None, status: Optional[str] = None) -> List[Dict[str, Any]]:
        where = "1=1"
        params: List[Any] = []
        if partner_id:
            where += " AND partner_id=?"
            params.append(partner_id)
        if status:
            where += " AND status=?"
            params.append(status)
        return list(reversed(self.store.list("pm_commissions", where, params)))

    def events_for(self, commission_id: str) -> List[Dict[str, Any]]:
        return self.store.list("pm_commission_events", "commission_id=?", (commission_id,))
