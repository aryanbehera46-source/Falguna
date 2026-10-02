"""Phase 8 Digital Business Ecosystem domain services.

TTT HQ owns every commercial decision here. FALGUNA recommendations are
persisted separately from human decisions and can never assign work, verify a
professional licence, authorize money collection, or bind TTT.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional

from .audit import AuditLog
from .store import StateStore, utcnow


ROUTING_MODES = {
    "DIRECT_TTT_DELIVERY", "TTT_ADVISORY", "PARTNER_OR_SPECIALIST_COORDINATION",
    "PROVIDER_OR_VENDOR_SOURCING", "REFERRAL_OR_MEDIATION", "PRODUCT_OR_SAAS_FIT",
    "BUSINESS_LAUNCH_AND_GROWTH", "LICENSED_PROFESSIONAL_REQUIRED", "UNSUPPORTED",
}
PROFILE_TYPES = {
    "FREELANCER", "AGENCY", "CONSULTANT", "DESIGNER", "DEVELOPER",
    "INDUSTRY_SPECIALIST", "QA_CONTRIBUTOR", "IMPLEMENTATION_PARTNER",
    "REGULATED_PROFESSIONAL", "VENDOR", "REFERRAL_PARTNER", "LOCAL_BUSINESS_SCOUT",
    "LEAD_RESEARCHER", "APPOINTMENT_SETTER", "LANGUAGE_PARTNER", "DELIVERY_SPECIALIST",
    "REGIONAL_ACCOUNT_PARTNER", "FUTURE_CERTIFIED_IMPLEMENTATION_PARTNER",
}
BLG_STAGES = ("MARKET_RESEARCH", "VALIDATION", "BUSINESS_MODEL", "POSITIONING_BRANDING",
              "TECHNOLOGY_OPERATIONS", "LAUNCH", "ACQUISITION", "ANALYTICS", "GROWTH", "EXPANSION")
VERIFICATION_RANK = {"UNVERIFIED": 0, "SELF_REPORTED": 1, "EVIDENCE_REVIEWED": 2, "TTT_VERIFIED": 3}


class EcosystemError(ValueError):
    pass


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _load(value: Optional[str], fallback: Any) -> Any:
    try:
        return json.loads(value) if value else fallback
    except (TypeError, json.JSONDecodeError):
        return fallback


class EcosystemService:
    def __init__(self, store: StateStore, audit: AuditLog):
        self.store, self.audit = store, audit

    def create_intake(self, *, organization_id: str, original_message: str,
                      normalized_meaning: str, created_by_identity_id: Optional[str] = None,
                      customer_ref: Optional[str] = None, source_language: Optional[str] = None,
                      geography: Optional[str] = None, preferred_language: Optional[str] = None,
                      category: Optional[str] = None, industry: Optional[str] = None,
                      budget_min: Optional[float] = None, budget_max: Optional[float] = None,
                      currency: Optional[str] = None, timing: Optional[str] = None,
                      risk_flags: Iterable[str] = (), clarification_questions: Iterable[str] = ()) -> str:
        if not organization_id or not original_message.strip() or not normalized_meaning.strip():
            raise EcosystemError("organization, original message and normalized meaning are required")
        if budget_min is not None and budget_max is not None and budget_min > budget_max:
            raise EcosystemError("budget minimum cannot exceed budget maximum")
        questions = [q.strip() for q in clarification_questions if q and q.strip()]
        now = utcnow()
        intake_id = self.store.create("p8_intakes", {
            "organization_id": organization_id, "customer_ref": customer_ref,
            "original_message": original_message.strip(), "normalized_meaning": normalized_meaning.strip(),
            "source_language": source_language, "geography": geography,
            "preferred_language": preferred_language, "category": category, "industry": industry,
            "budget_min": budget_min, "budget_max": budget_max, "currency": currency,
            "timing": timing, "risk_flags_json": _json(sorted(set(risk_flags))),
            "clarification_questions_json": _json(questions),
            "status": "NEEDS_CLARIFICATION" if questions else "READY_FOR_ROUTING",
            "created_by_identity_id": created_by_identity_id, "created_at": now, "updated_at": now,
        })
        self.audit.append("P8_INTAKE_CREATED", {"intake_id": intake_id, "organization_id": organization_id})
        return intake_id

    def recommend_route(self, intake_id: str, mode: str, reasons: List[str], evidence: List[Dict[str, Any]],
                        uncertainty: List[str], recommended_by: str = "FALGUNA") -> str:
        intake = self.store.get("p8_intakes", intake_id)
        if not intake or mode not in ROUTING_MODES or not reasons or not evidence:
            raise EcosystemError("valid intake, route, reasons and evidence are required")
        route_id = self.store.create("p8_routing_decisions", {
            "intake_id": intake_id, "recommended_mode": mode, "decided_mode": None,
            "recommendation_reasons_json": _json(reasons), "evidence_json": _json(evidence),
            "uncertainty_json": _json(uncertainty), "review_status": "PENDING_HUMAN_REVIEW",
            "recommended_by": recommended_by, "decided_by_identity_id": None,
            "decision_reason": None, "created_at": utcnow(), "updated_at": utcnow(),
        })
        self.store.update("p8_intakes", intake_id, status="ROUTE_RECOMMENDED")
        return route_id

    def decide_route(self, route_id: str, mode: str, actor_identity_id: str, reason: str) -> Dict[str, Any]:
        route = self.store.get("p8_routing_decisions", route_id)
        if not route or mode not in ROUTING_MODES or not actor_identity_id or not reason.strip():
            raise EcosystemError("valid route, mode, TTT identity and reason are required")
        if route["review_status"] != "PENDING_HUMAN_REVIEW":
            raise EcosystemError("routing decision is already final")
        self.store.update("p8_routing_decisions", route_id, decided_mode=mode,
                          decided_by_identity_id=actor_identity_id, decision_reason=reason.strip(),
                          review_status="HUMAN_DECIDED")
        self.store.update("p8_intakes", route["intake_id"], status="ROUTED")
        result = self.store.get("p8_routing_decisions", route_id)
        self.audit.append("P8_ROUTE_DECIDED", {"route_id": route_id, "mode": mode, "actor": actor_identity_id})
        return result

    def create_profile(self, *, organization_id: str, profile_type: str, display_name: str,
                       capabilities: List[Dict[str, Any]], geography: Iterable[str] = (),
                       languages: Iterable[str] = (), verification_level: str = "UNVERIFIED",
                       verification_evidence: Iterable[Dict[str, Any]] = (), licensing_status: str = "NOT_APPLICABLE",
                       licensing_evidence: Iterable[Dict[str, Any]] = (), availability_status: str = "AVAILABLE",
                       commercial_relationship: str = "COORDINATION_ONLY", conflicts: Iterable[str] = (),
                       partner_id: Optional[str] = None, legal_name: Optional[str] = None,
                       maturity_tier: Optional[str] = None) -> str:
        if profile_type not in PROFILE_TYPES or not display_name.strip():
            raise EcosystemError("valid profile type and display name are required")
        if verification_level not in VERIFICATION_RANK:
            raise EcosystemError("unknown verification level")
        if licensing_status == "VERIFIED" and not list(licensing_evidence):
            raise EcosystemError("licensing cannot be marked verified without evidence")
        now = utcnow()
        profile_id = self.store.create("p8_network_profiles", {
            "organization_id": organization_id, "profile_type": profile_type, "display_name": display_name.strip(),
            "legal_name": legal_name, "partner_id": partner_id, "geography_json": _json(list(geography)),
            "languages_json": _json(list(languages)), "availability_status": availability_status,
            "verification_level": verification_level, "verification_evidence_json": _json(list(verification_evidence)),
            "licensing_status": licensing_status, "licensing_evidence_json": _json(list(licensing_evidence)),
            "commercial_relationship": commercial_relationship, "conflict_disclosures_json": _json(list(conflicts)),
            "maturity_tier": maturity_tier, "status": "ACTIVE", "completed_assignments": 0,
            "qa_passes": 0, "disputes": 0, "policy_violations": 0, "created_at": now, "updated_at": now,
        })
        for capability in capabilities:
            tag = str(capability.get("tag", "")).strip().lower()
            evidence_status = capability.get("evidence_status", "SELF_REPORTED")
            evidence = capability.get("evidence", [])
            if not tag or evidence_status not in {"SELF_REPORTED", "EVIDENCE_REVIEWED"}:
                raise EcosystemError("each capability needs a tag and honest evidence status")
            self.store.create("p8_profile_capabilities", {
                "profile_id": profile_id, "capability_tag": tag, "evidence_status": evidence_status,
                "evidence_json": _json(evidence), "regulated": int(bool(capability.get("regulated"))),
                "created_at": now, "updated_at": now,
            })
        return profile_id

    def publish_opportunity(self, intake_id: str, *, customer_safe_brief: str,
                            capability_requirements: Iterable[str], required_verification_level: str,
                            budget_visibility: str = "HIDDEN", actor_identity_id: str) -> str:
        intake = self.store.get("p8_intakes", intake_id)
        if not intake or intake["status"] != "ROUTED":
            raise EcosystemError("a human-routed intake is required")
        if required_verification_level not in VERIFICATION_RANK:
            raise EcosystemError("unknown verification requirement")
        if budget_visibility not in {"HIDDEN", "RANGE"} or not customer_safe_brief.strip():
            raise EcosystemError("safe brief and valid budget visibility are required")
        now = utcnow()
        return self.store.create("p8_opportunities", {
            "organization_id": intake["organization_id"], "intake_id": intake_id,
            "customer_safe_brief": customer_safe_brief.strip(), "geography": intake.get("geography"),
            "language": intake.get("preferred_language"), "category": intake.get("category"),
            "industry": intake.get("industry"), "capability_requirements_json": _json(sorted(set(capability_requirements))),
            "budget_visibility": budget_visibility,
            "budget_min": intake.get("budget_min") if budget_visibility == "RANGE" else None,
            "budget_max": intake.get("budget_max") if budget_visibility == "RANGE" else None,
            "currency": intake.get("currency") if budget_visibility == "RANGE" else None,
            "timing": intake.get("timing"), "risk_flags_json": intake["risk_flags_json"],
            "required_verification_level": required_verification_level,
            "application_state": "OPEN", "assignment_state": "UNASSIGNED",
            "customer_relationship_owner": "TTT", "status": "OPEN",
            "created_at": now, "updated_at": now,
        })

    def evaluate_match(self, opportunity_id: str, profile_id: str) -> Dict[str, Any]:
        opportunity, profile = self.store.get("p8_opportunities", opportunity_id), self.store.get("p8_network_profiles", profile_id)
        if not opportunity or not profile:
            raise EcosystemError("opportunity and profile are required")
        required = set(_load(opportunity["capability_requirements_json"], []))
        capabilities = {c["capability_tag"] for c in self.store.list("p8_profile_capabilities", "profile_id=?", (profile_id,))}
        gaps, reasons, conflicts = sorted(required - capabilities), [], _load(profile["conflict_disclosures_json"], [])
        if required & capabilities:
            reasons.append("capability evidence overlaps the stated requirement")
        if opportunity.get("language") and opportunity["language"] in _load(profile["languages_json"], []):
            reasons.append("preferred language matches")
        elif opportunity.get("language"):
            gaps.append("preferred language not evidenced")
        if opportunity.get("geography") and opportunity["geography"] in _load(profile["geography_json"], []):
            reasons.append("geography matches")
        elif opportunity.get("geography"):
            gaps.append("geography not evidenced")
        if VERIFICATION_RANK.get(profile["verification_level"], -1) >= VERIFICATION_RANK[opportunity["required_verification_level"]]:
            reasons.append("verification requirement met")
        else:
            gaps.append("verification requirement not met")
        hard_ok = profile["status"] == "ACTIVE" and profile["availability_status"] == "AVAILABLE" and not gaps and not conflicts
        match_id = self.store.create("p8_matches", {
            "opportunity_id": opportunity_id, "profile_id": profile_id, "eligible": int(hard_ok),
            "reasons_json": _json(reasons), "gaps_json": _json(gaps), "conflicts_json": _json(conflicts),
            "economics_review_required": 1, "created_at": utcnow(), "updated_at": utcnow(),
        })
        return self.store.get("p8_matches", match_id)

    def feed_for_profile(self, profile_id: str) -> List[Dict[str, Any]]:
        profile = self.store.get("p8_network_profiles", profile_id)
        if not profile or profile["status"] != "ACTIVE":
            return []
        items = []
        for match in self.store.list("p8_matches", "profile_id=? AND eligible=1", (profile_id,)):
            opportunity = self.store.get("p8_opportunities", match["opportunity_id"])
            if opportunity and opportunity["status"] == "OPEN":
                safe = {k: opportunity.get(k) for k in ("id", "customer_safe_brief", "geography", "language", "category", "industry", "budget_visibility", "budget_min", "budget_max", "currency", "timing", "required_verification_level", "application_state", "assignment_state", "customer_relationship_owner")}
                safe["match_reasons"] = _load(match["reasons_json"], [])
                items.append(safe)
        return items

    def apply(self, opportunity_id: str, profile_id: str, statement: str, conflict_disclosure: Optional[str] = None) -> str:
        eligible = self.store.list("p8_matches", "opportunity_id=? AND profile_id=? AND eligible=1", (opportunity_id, profile_id))
        opportunity = self.store.get("p8_opportunities", opportunity_id)
        if not eligible or not opportunity or opportunity["application_state"] != "OPEN" or not statement.strip():
            raise EcosystemError("an open, eligible opportunity and statement are required")
        existing = self.store.list(
            "p8_opportunity_applications", "opportunity_id=? AND profile_id=?",
            (opportunity_id, profile_id),
        )
        if existing:
            # Retried form submissions and repeated browser journeys are
            # idempotent. Never overwrite the original statement/conflict
            # disclosure or manufacture a second claim.
            return existing[0]["id"]
        return self.store.create("p8_opportunity_applications", {
            "opportunity_id": opportunity_id, "profile_id": profile_id, "statement": statement.strip(),
            "status": "PENDING_TTT_REVIEW", "conflict_disclosure": conflict_disclosure,
            "created_at": utcnow(), "updated_at": utcnow(),
        })

    def assign(self, application_id: str, actor_identity_id: str, scope: str,
               approval_evidence: List[Dict[str, Any]], customer_contact_allowed: bool = False) -> str:
        application = self.store.get("p8_opportunity_applications", application_id)
        if not application or application["status"] != "PENDING_TTT_REVIEW" or not scope.strip() or not approval_evidence:
            raise EcosystemError("pending application, scope and approval evidence are required")
        assignment_id = self.store.create("p8_assignments", {
            "opportunity_id": application["opportunity_id"], "profile_id": application["profile_id"],
            "application_id": application_id, "status": "ASSIGNED", "scope": scope.strip(),
            "customer_contact_allowed": int(customer_contact_allowed), "money_collection_allowed": 0,
            "assigned_by_identity_id": actor_identity_id, "approval_evidence_json": _json(approval_evidence),
            "created_at": utcnow(), "updated_at": utcnow(),
        })
        self.store.update("p8_opportunity_applications", application_id, status="ACCEPTED")
        self.store.update("p8_opportunities", application["opportunity_id"], assignment_state="ASSIGNED", application_state="CLOSED")
        return assignment_id

    def start_blg(self, intake_id: str, tier: str, regulated_boundaries: List[str]) -> str:
        intake = self.store.get("p8_intakes", intake_id)
        routes = self.store.list("p8_routing_decisions", "intake_id=? AND review_status='HUMAN_DECIDED'", (intake_id,))
        if not intake or not routes or routes[-1]["decided_mode"] != "BUSINESS_LAUNCH_AND_GROWTH":
            raise EcosystemError("a human-approved Business Launch & Growth route is required")
        if tier not in {"LOCAL_SMALL_BUSINESS", "STARTUP_SMB", "GROWTH_TRANSFORMATION", "ENTERPRISE"}:
            raise EcosystemError("unknown engagement tier")
        return self.store.create("p8_blg_engagements", {
            "organization_id": intake["organization_id"], "intake_id": intake_id, "tier": tier,
            "current_stage": BLG_STAGES[0], "regulated_boundaries_json": _json(regulated_boundaries),
            "outcome_disclaimer": "TTT does not promise profit, investment, legal, tax, medical or financial outcomes.",
            "status": "ACTIVE", "created_at": utcnow(), "updated_at": utcnow(),
        })

    def advance_blg(self, engagement_id: str, to_stage: str, actor_identity_id: str,
                    evidence: List[Dict[str, Any]]) -> Dict[str, Any]:
        engagement = self.store.get("p8_blg_engagements", engagement_id)
        if not engagement or to_stage not in BLG_STAGES or not evidence:
            raise EcosystemError("valid engagement, stage and evidence are required")
        current_index, target_index = BLG_STAGES.index(engagement["current_stage"]), BLG_STAGES.index(to_stage)
        if target_index != current_index + 1:
            raise EcosystemError("Business Launch & Growth stages advance one evidenced step at a time")
        self.store.create("p8_blg_stage_events", {
            "engagement_id": engagement_id, "from_stage": engagement["current_stage"], "to_stage": to_stage,
            "evidence_json": _json(evidence), "actor_identity_id": actor_identity_id,
            "created_at": utcnow(), "updated_at": utcnow(),
        })
        self.store.update("p8_blg_engagements", engagement_id, current_stage=to_stage)
        return self.store.get("p8_blg_engagements", engagement_id)

    def analytics(self, organization_id: str) -> Dict[str, Any]:
        intakes = self.store.list("p8_intakes", "organization_id=?", (organization_id,))
        opportunities = self.store.list("p8_opportunities", "organization_id=?", (organization_id,))
        profiles = self.store.list("p8_network_profiles", "organization_id=?", (organization_id,))
        decisions = [d for i in intakes for d in self.store.list("p8_routing_decisions", "intake_id=? AND review_status='HUMAN_DECIDED'", (i["id"],))]
        by_route: Dict[str, int] = {}
        for decision in decisions:
            by_route[decision["decided_mode"]] = by_route.get(decision["decided_mode"], 0) + 1
        return {"intakes": len(intakes), "human_decided_routes": len(decisions), "routes_by_mode": by_route,
                "open_opportunities": sum(o["status"] == "OPEN" for o in opportunities),
                "active_profiles": sum(p["status"] == "ACTIVE" for p in profiles),
                "collected_revenue": None, "known_costs": None, "contribution": None,
                "financial_note": "Not computed without linked evidence; no CAC, margin or profit is inferred."}
