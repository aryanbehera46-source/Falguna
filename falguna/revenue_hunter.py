"""Revenue Hunter: client acquisition, built into TTT HQ (not Falguna Engineering).

PASS 2 (this module): TTT HQ's "Coming soon" Opportunities / Sales Pipeline /
Clients / Active Jobs / Revenue items become real, working features. This
follows the exact separation the previous pass already established --
Revenue Hunter is a business-side concern of Twenty Two Technologies, served
by TTT HQ's own local server (falguna/hq_web.py), never by Falguna
Engineering's server (falguna/web.py). The only seam between the two apps
is falguna/handoff.py, unchanged here, called only when the owner explicitly
triggers a Won -> Active Job handoff with a real target repository.

Design choices worth stating up front:

* Qualification scoring and proposal/follow-up drafting are deterministic
  and template-based, not model-generated. A daily revenue-critical tool
  needs to work the same way every time, offline, with zero dependency on
  a live model transport or network access -- and it needs to be fully
  unit-testable without mocking an LLM. Every generated string is built
  from the opportunity's own structured fields, not invented content.
* Nothing in this module ever sends anything to a client. Proposals and
  follow-ups are created with status DRAFT and only ever move to APPROVED
  (proposals, via the existing Needs Aryan queue) or SENT (followups, via
  an explicit mark_sent call the owner makes after actually sending it
  themselves). There is no code path that transmits outbound content.
* Opportunity intake never fetches a URL. Pasting a URL records it as the
  source for traceability only; the JD text/manual fields still have to be
  supplied by the owner. This is a deliberate reading of "do not build
  scraping that violates platform rules" -- the safest version of that
  instruction is to not scrape at all.
* The approval queue is not a new system. It reuses NeedsAryanQueue
  (ttt_hq.py) exactly as Chat/Work/Search already do, via two additional
  kinds (outreach_approval, negotiation_response_approval) added to that
  module's existing NEEDS_ARYAN_KINDS set. Deciding a Revenue-Hunter-origin
  item applies its real side effect (e.g. marking a proposal APPROVED)
  through apply_decision_side_effect(), called by hq_web.py right after
  NeedsAryanQueue.decide() -- the queue itself stays generic.
"""

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import AuditLog
from .handoff import accept_revenue_hunter_handoff, validate_handoff_payload
from .models import RunPolicy
from .store import StateStore, utcnow

# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

PIPELINE_STAGES = [
    "New", "Qualified", "Proposal Ready", "Applied/Sent", "Replied",
    "Meeting", "Negotiating", "Won", "Lost",
]
TERMINAL_STAGES = {"Won", "Lost"}
_STAGE_INDEX = {s: i for i, s in enumerate(PIPELINE_STAGES)}

PROPOSAL_KINDS = {"short", "detailed", "upwork", "email_pitch", "follow_up"}
FOLLOWUP_KINDS = {
    "proposal_followup", "response_followup", "negotiation_followup",
    "payment_followup", "repeat_business_followup",
}
RECOMMENDATIONS = {"PURSUE", "MAYBE", "IGNORE"}

# ---------------------------------------------------------------------------
# Opportunity intake + field extraction (no scraping -- text/manual/CSV/JSON only)
# ---------------------------------------------------------------------------

REQUIRED_OPPORTUNITY_FIELDS = {"title"}

_SKILL_VOCAB = [
    "javascript", "typescript", "react", "react native", "node.js", "node",
    "express", "python", "django", "flask", "sqlite", "postgresql", "mysql",
    "sql", "rest api", "graphql", "stripe", "payments", "e-commerce",
    "ecommerce", "shopify", "wordpress", "booking system", "crm",
    "automation", "ai", "machine learning", "llm", "chatbot", "saas",
    "html", "css", "tailwind", "vue", "next.js", "aws", "docker", "api integration",
]

_BUDGET_RE = re.compile(
    r"(?:USD|usd|\$)\s?([\d,]+(?:\.\d+)?)\s*(?:-|to)?\s*(\$?\s?[\d,]+(?:\.\d+)?)?\s*(/\s?hr|/\s?hour|per hour|/\s?mo|/\s?month)?",
    re.IGNORECASE,
)
_DEADLINE_LINE_RE = re.compile(r"deadline\s*[:\-]\s*(.+)", re.IGNORECASE)
_URGENCY_WORDS = ("urgent", "asap", "immediately", "right away", "this week", "24 hours", "24hrs")
_CONTRACT_WORDS = {
    "hourly": "hourly", "fixed price": "fixed", "fixed-price": "fixed",
    "retainer": "retainer", "full-time": "full-time", "part-time": "part-time",
    "one-time": "one-time", "one time": "one-time", "contract": "contract",
}
_RED_FLAG_PHRASES = (
    "unpaid", "no budget", "exposure only", "equity only", "spec work",
    "test project for free", "work for free", "trial task before payment",
    "pay after", "revenue share only",
)


def extract_fields_from_text(text: str) -> Dict[str, Any]:
    """Best-effort structuring of a pasted job description. Never fetches
    anything -- operates only on the text the owner pasted."""
    text = text or ""
    lower = text.lower()
    fields: Dict[str, Any] = {"description": text.strip()}

    budget_match = _BUDGET_RE.search(text)
    if budget_match:
        low, high, unit = budget_match.groups()
        rendered = f"${low}"
        if high:
            rendered += f"-{high.strip().lstrip('$').strip()}"
        if unit:
            rendered += f" {unit.strip()}"
        fields["budget_rate"] = rendered

    found_skills = [skill for skill in _SKILL_VOCAB if skill in lower]
    if found_skills:
        fields["required_skills"] = ", ".join(sorted(set(found_skills)))

    deadline_match = _DEADLINE_LINE_RE.search(text)
    if deadline_match:
        fields["deadline"] = deadline_match.group(1).strip().splitlines()[0]

    for phrase, normalized in _CONTRACT_WORDS.items():
        if phrase in lower:
            fields["contract_type"] = normalized
            break

    if "remote" in lower:
        fields["location_timezone"] = "Remote"

    if any(word in lower for word in _URGENCY_WORDS):
        fields["urgency"] = "High"

    return fields


def extract_from_csv_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Normalize CSV/JSON import rows (arbitrary header casing/aliases) into
    the opportunity field names this module uses."""
    alias_map = {
        "title": "title", "job title": "title", "job_title": "title", "role": "title",
        "client": "client_name", "client_name": "client_name", "company": "client_name",
        "description": "description", "desc": "description", "details": "description",
        "budget": "budget_rate", "rate": "budget_rate", "budget_rate": "budget_rate", "pay": "budget_rate",
        "skills": "required_skills", "required_skills": "required_skills", "tech": "required_skills",
        "deadline": "deadline", "due": "deadline", "due_date": "deadline",
        "contract_type": "contract_type", "type": "contract_type",
        "location": "location_timezone", "timezone": "location_timezone", "location_timezone": "location_timezone",
        "urgency": "urgency", "source": "source", "url": "source_url",
    }
    normalized = []
    for row in rows:
        item: Dict[str, Any] = {}
        for key, value in row.items():
            target = alias_map.get(str(key).strip().lower())
            if target and value not in (None, ""):
                item[target] = value
        if item.get("title"):
            normalized.append(item)
    return normalized


# ---------------------------------------------------------------------------
# Opportunities + stage history
# ---------------------------------------------------------------------------

class OpportunityError(ValueError):
    pass


class OpportunityStore:
    def __init__(self, store: StateStore, audit: AuditLog, orchestrator=None):
        self.store = store
        self.audit = audit
        # LifecycleOrchestrator, injected (like ProposalStore's needs_aryan)
        # to avoid a circular import -- lifecycle.py itself imports
        # OpportunityStore from this module. Optional and best-effort: this
        # observability layer must never change or break this class's own,
        # already-tested behavior when the caller doesn't wire it in.
        self.orchestrator = orchestrator

    def create(self, fields: Dict[str, Any], actor: str = "Aryan", source: str = "manual") -> str:
        title = (fields.get("title") or "").strip()
        if not title:
            raise OpportunityError("title is required")
        now = utcnow()
        row = {
            "source": source,
            "source_url": fields.get("source_url"),
            "client_name": fields.get("client_name"),
            "title": title,
            "description": fields.get("description"),
            "budget_rate": fields.get("budget_rate"),
            "required_skills": fields.get("required_skills"),
            "deadline": fields.get("deadline"),
            "contract_type": fields.get("contract_type"),
            "location_timezone": fields.get("location_timezone"),
            "urgency": fields.get("urgency"),
            "stage": "New",
            "final_price": None,
            "lost_reason": None,
            "created_at": now, "updated_at": now,
        }
        opportunity_id = self.store.create("rh_opportunities", row)
        self._record_stage(opportunity_id, None, "New", actor, "created")
        self.audit.append("RH_OPPORTUNITY_CREATED", {"opportunity_id": opportunity_id, "title": title, "source": source})
        if self.orchestrator is not None:
            self.orchestrator.try_initialize(opportunity_id, actor, reason="opportunity discovered")
        return opportunity_id

    def update(self, opportunity_id: str, actor: str, **fields: Any) -> Dict[str, Any]:
        current = self.store.get("rh_opportunities", opportunity_id)
        if not current:
            raise OpportunityError("opportunity not found")
        allowed = {
            "client_name", "title", "description", "budget_rate", "required_skills",
            "deadline", "contract_type", "location_timezone", "urgency", "source_url",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise OpportunityError(f"unknown fields: {sorted(unknown)}")
        if "title" in fields and not (fields["title"] or "").strip():
            raise OpportunityError("title cannot be blank")
        self.store.update("rh_opportunities", opportunity_id, **fields)
        self.audit.append("RH_OPPORTUNITY_UPDATED", {"opportunity_id": opportunity_id, "fields": sorted(fields), "actor": actor})
        return self.store.get("rh_opportunities", opportunity_id)

    def move_stage(self, opportunity_id: str, to_stage: str, actor: str, note: Optional[str] = None) -> Dict[str, Any]:
        current = self.store.get("rh_opportunities", opportunity_id)
        if not current:
            raise OpportunityError("opportunity not found")
        if to_stage not in PIPELINE_STAGES:
            raise OpportunityError(f"to_stage must be one of {PIPELINE_STAGES}")
        if current["stage"] in TERMINAL_STAGES and to_stage != current["stage"]:
            raise OpportunityError(f"opportunity is already {current['stage']} -- reopen it explicitly if that's intended")
        from_stage = current["stage"]
        self.store.update("rh_opportunities", opportunity_id, stage=to_stage)
        self._record_stage(opportunity_id, from_stage, to_stage, actor, note)
        self.audit.append("RH_OPPORTUNITY_STAGE_MOVED", {"opportunity_id": opportunity_id, "from": from_stage, "to": to_stage, "actor": actor})
        return self.store.get("rh_opportunities", opportunity_id)

    def mark_won(self, opportunity_id: str, actor: str, final_price: Optional[float] = None, note: Optional[str] = None) -> Dict[str, Any]:
        opp = self.move_stage(opportunity_id, "Won", actor, note or "Won")
        if final_price is not None:
            self.store.update("rh_opportunities", opportunity_id, final_price=final_price)
        return self.store.get("rh_opportunities", opportunity_id)

    def mark_lost(self, opportunity_id: str, actor: str, reason: Optional[str] = None) -> Dict[str, Any]:
        self.move_stage(opportunity_id, "Lost", actor, reason)
        if reason:
            self.store.update("rh_opportunities", opportunity_id, lost_reason=reason)
        return self.store.get("rh_opportunities", opportunity_id)

    def _record_stage(self, opportunity_id: str, from_stage: Optional[str], to_stage: str, actor: str, note: Optional[str]) -> None:
        self.store.create("rh_stage_history", {
            "opportunity_id": opportunity_id, "from_stage": from_stage, "to_stage": to_stage,
            "actor": actor, "note": note, "created_at": utcnow(),
        })

    def get(self, opportunity_id: str) -> Optional[Dict[str, Any]]:
        opp = self.store.get("rh_opportunities", opportunity_id)
        if not opp:
            return None
        opp = dict(opp)
        opp["stage_history"] = self.store.list("rh_stage_history", "opportunity_id=?", (opportunity_id,))
        opp["qualification"] = self.latest_qualification(opportunity_id)
        opp["proposals"] = self.store.list("rh_proposals", "opportunity_id=?", (opportunity_id,))
        opp["followups"] = self.store.list("rh_followups", "opportunity_id=?", (opportunity_id,))
        return opp

    def latest_qualification(self, opportunity_id: str) -> Optional[Dict[str, Any]]:
        rows = self.store.list("rh_qualifications", "opportunity_id=?", (opportunity_id,))
        return rows[-1] if rows else None

    def list(self, stage: Optional[str] = None) -> List[Dict[str, Any]]:
        if stage:
            if stage not in PIPELINE_STAGES:
                raise OpportunityError(f"stage must be one of {PIPELINE_STAGES}")
            rows = self.store.list("rh_opportunities", "stage=?", (stage,))
        else:
            rows = self.store.list("rh_opportunities")
        rows = list(reversed(rows))
        # Attach each opportunity's latest qualification -- lightweight
        # (unlike get(), this never pulls proposals/followups/stage_history)
        # but the acquisition inbox (score, recommendation, suggested
        # price, portfolio match columns) needs this on every row, not
        # just when a single opportunity is opened.
        out = []
        for row in rows:
            row = dict(row)
            row["qualification"] = self.latest_qualification(row["id"])
            out.append(row)
        return out


# ---------------------------------------------------------------------------
# Qualification scoring (deterministic)
# ---------------------------------------------------------------------------

DEFAULT_CAPABILITY_SKILLS = [
    "javascript", "typescript", "react", "node.js", "node", "express", "python",
    "sqlite", "postgresql", "rest api", "stripe", "payments", "e-commerce",
    "ecommerce", "booking system", "crm", "automation", "ai", "saas", "html", "css",
]

# A handful of capability names above are also ordinary English words, not
# just technology names -- "react" (verb: "companies can react faster"),
# "node" (noun: "a node in the network"), "express" (verb/adjective:
# "express your interest", "express delivery"). Found live, during real-
# data requalification (qualification-calibration pass): a genuine Senior
# Product Manager listing (Confluent, "Cluster Linking") kept registering a
# false "react" skill match purely because its description used the verb
# "react" in an ordinary sentence, which then both inflated fit_score and
# overrode the new PM-role exclusion gate. _skill_tokens' free-text
# ("implied") skill scan below deliberately skips these three -- an
# opportunity that actually wants React/Node/Express work overwhelmingly
# either lists it as an explicit skill tag (still fully detected, since
# `listed` skills are never filtered) or names the concrete framework
# phrase ("react developer", "node.js", "express.js") rather than the bare
# ambiguous word alone.
_AMBIGUOUS_ENGLISH_WORD_CAPABILITIES = {"react", "node", "express"}

# Some capability keywords name a *topic* or *domain* ("ai", "saas", "crm",
# "automation", "e-commerce", "ecommerce", "payments", "booking system")
# rather than a concrete technology -- they show up in job postings that
# have nothing to do with software delivery just as often as in real dev
# work (a company can be "an AI company" and still be hiring a copywriter,
# a recruiter, or a salesperson). A *single* match against one of these
# alone must never be trusted as strong signal -- see _fit_score's comment
# for the exact bug this caused (Freelance Writer scoring 100/PURSUE off
# one incidental "AI" mention). Concrete stack names below are excluded
# from this set deliberately: nobody lists "react" or "postgresql" as a
# requirement unless the work is actually software development.
_GENERIC_TOPIC_CAPABILITY_TOKENS = {
    "ai", "saas", "crm", "automation", "e-commerce", "ecommerce", "payments", "booking system",
}

# Skill tokens strong enough, on their own, to override an exclusion-role
# match in _service_relevance (e.g. "technical writer" who also lists
# "html, css" is NOT overridden by that alone -- markup/styling skills are
# common in non-dev content/design work too and are weak evidence of an
# actual software-delivery ask). A real backend/frontend programming
# language or framework is a much stronger signal that the underlying
# request is to build something, not describe or format something.
_STRONG_DEV_OVERRIDE_TOKENS = {
    "javascript", "typescript", "react", "node.js", "node", "express", "python", "sqlite", "postgresql", "rest api",
}

# Phrases that establish "this is a request to build/deliver software",
# independent of the opportunity's declared skill list -- title/description
# language, not tags. Used only to *override* an exclusion-role match (see
# _service_relevance): an opportunity that mentions a role we don't do
# (e.g. "writer") is still relevant if the actual ask is clearly building a
# tool/platform/app rather than performing that role personally.
DEFAULT_POSITIVE_SERVICE_SIGNALS = [
    "full-stack", "full stack", "frontend developer", "front-end developer", "backend developer",
    "back-end developer", "software developer", "software engineer", "web developer", "app developer",
    "mobile developer", "build a", "build an", "build our", "develop a", "develop an", "developing a",
    "programming", "web application", "web app", "mvp development", "api development", "api integration",
    "database design", "dashboard", "admin panel", "booking system", "reservation system",
    "e-commerce platform", "ecommerce platform", "automation script", "integrate ai", "ai integration",
    "machine learning integration", "react developer", "node.js developer", "python developer",
    "coding", "codebase", "tech stack", "write code", "write software", "build software",
    "develop software", "developing software", "engineer a solution",
    # Deliberately excludes bare "html"/"css" -- markup/styling alone is weak
    # evidence of a real software-delivery ask (a technical writer producing
    # an HTML style guide is not a dev job); see _STRONG_DEV_OVERRIDE_TOKENS.
    #
    # Also deliberately excludes bare "react" and "express" here (unlike
    # "javascript", "typescript", "node.js", "postgresql", "rest api",
    # which are safe as free-text prose matches): both are ordinary English
    # words ("...so companies can react faster...", "express your
    # interest...") as well as framework names, so matching them as plain
    # substrings against a title/description sentence is unreliable. Found
    # live, during real-data requalification (qualification-calibration
    # pass): a genuine Product Manager listing (Confluent, "Senior Product
    # Manager, Cluster Linking") kept overriding the new PM exclusion gate
    # solely because its description said "...so companies can react
    # faster, build smarter..." -- an ordinary verb, not a framework
    # mention. "react developer" (above) and skill-token matches against
    # _STRONG_DEV_OVERRIDE_TOKENS (where "react" as a literal listed skill
    # tag is unambiguous) remain the ways a real React ask is detected.
    "javascript", "typescript", "node.js", "postgresql", "rest api",
]

# Phrases that indicate the opportunity itself IS a role outside TTT's
# acquisition profile -- content/people/finance/legal/medical work, not
# software engineering. Deliberately phrase-level (not single generic
# words) to avoid brittle false positives; combined with the positive
# signals above via _service_relevance so context still overrides a bare
# mention (e.g. "build a tool for content writers" is not excluded).
#
# Qualification-calibration pass: added the management/leadership role
# family (Product/Project/Engineering/Program Manager, Head of
# Engineering) -- these are hiring-for-a-person roles, not a hands-on
# software deliverable TTT can sell, and were previously passing the gate
# too easily just because their listings mention plenty of dev-adjacent
# language. Phrased as the occupation ("product manager") rather than the
# business domain ("product management") deliberately: "Senior Product
# Manager for SaaS company" should match and gate to IGNORE, but "Build a
# SaaS product-management dashboard" (a real deliverable, in the domain of
# product management) never contains the phrase "product manager" at all,
# so it's unaffected without needing any special-case. Also added
# customer-success/account-management, rounding out the sales/CS family.
#
# Work-Type Relevance Gate pass: added the admin/operational-support role
# family (virtual/office/administrative/executive assistant, bookkeeping,
# data entry, office administration, operations assistant, payroll/
# accounting support) -- found live, during real-data requalification,
# via a real "Remote Office Assistant" listing (Coalition Technologies)
# that reached PURSUE on a client-stated budget purely because its tag
# list happened to include several web/CMS-adjacent words (css, html,
# php, wordpress, shopify) despite the actual job -- answering phones,
# reconciling invoices, data entry, calendar management -- being pure
# administrative/bookkeeping work with zero coding ask anywhere in the
# description. Same occupation-vs-domain phrasing choice as the PM family
# above: "bookkeeping" / "data entry" / "office administration" gate a
# role whose JOB is to personally perform that operational work, but
# never match a real software deliverable ABOUT that domain ("build a
# bookkeeping automation tool", "develop payroll SaaS", "build an admin
# dashboard" -- none of these phrases contain any of the exclusion
# phrases below at all, so they are gated in the first place only when a
# listing genuinely also names the assistant/admin role, and even then
# still pass via the same positive-service-signal override used
# throughout this gate).
DEFAULT_EXCLUSION_ROLE_SIGNALS = [
    "copywriter", "copywriting", "content writer", "freelance writer", "blog writer",
    "technical writer", "ghostwriter", "proofreader", "content editor",
    "recruiter", "recruiting", "talent acquisition", "hr generalist", "human resources",
    "sales representative", "sales development representative", "business development representative",
    "account executive", "account manager", "customer support", "customer service representative",
    "support agent", "customer success", "customer success manager",
    "accountant", "bookkeeper", "tax preparer", "attorney", "lawyer", "legal counsel", "paralegal",
    "nurse", "physician", "doctor", "dentist", "therapist", "social media manager",
    "content marketing specialist", "influencer",
    "product manager", "project manager", "engineering manager", "program manager", "head of engineering",
    "virtual assistant", "office assistant", "administrative assistant", "executive assistant",
    "bookkeeping", "data entry", "office administration", "operations assistant",
    "payroll support", "payroll processing", "accounting support",
]

# Real, self-built portfolio work (see ~/Freelancing) used as the default
# proof set -- not placeholders. Update this list as the portfolio grows.
DEFAULT_PORTFOLIO_PROJECTS = [
    {"name": "ServiceFlow", "tags": ["booking system", "crm", "node.js", "express", "sqlite", "payments"],
     "proof": "ServiceFlow -- a full booking + CRM platform (availability, double-booking protection, billing, staff performance dashboard)"},
    {"name": "Nivara Commerce", "tags": ["e-commerce", "ecommerce", "node.js", "express", "payments", "crm", "automation"],
     "proof": "Nivara Commerce -- a D2C storefront + order management platform (checkout, inventory, fulfillment, sales analytics)"},
    {"name": "BriefPilot AI", "tags": ["ai", "saas", "react", "typescript", "automation"],
     "proof": "BriefPilot AI -- an AI-assisted SaaS that turns briefs and meeting notes into structured, reviewable workflows"},
]


class QualificationEngine:
    """Deterministic, documented scoring rules -- see class docstring on each
    method for exactly how a number is produced. No network, no model call."""

    def __init__(self, capability_skills: Optional[List[str]] = None, portfolio: Optional[List[Dict[str, Any]]] = None,
                 positive_service_signals: Optional[List[str]] = None, exclusion_role_signals: Optional[List[str]] = None):
        self.capability_skills = [s.lower() for s in (capability_skills or DEFAULT_CAPABILITY_SKILLS)]
        self.portfolio = portfolio or DEFAULT_PORTFOLIO_PROJECTS
        # None-check (not a truthy-check like capability_skills/portfolio
        # above): an explicitly configured empty list is a valid, meaningful
        # acquisition-profile choice ("no exclusions configured"), not the
        # same as "nothing was passed, use the default" -- a truthy-check
        # would silently discard that choice and fall back to the default
        # list instead.
        self.positive_service_signals = [s.lower() for s in (positive_service_signals if positive_service_signals is not None else DEFAULT_POSITIVE_SERVICE_SIGNALS)]
        self.exclusion_role_signals = [s.lower() for s in (exclusion_role_signals if exclusion_role_signals is not None else DEFAULT_EXCLUSION_ROLE_SIGNALS)]

    def score(self, opportunity: Dict[str, Any]) -> Dict[str, Any]:
        text = " ".join(filter(None, [
            opportunity.get("title", ""), opportunity.get("description", ""),
            opportunity.get("required_skills", ""),
        ])).lower()
        skill_tokens = self._skill_tokens(opportunity)

        fit_score = self._fit_score(skill_tokens)
        budget_quality, budget_amount = self._budget_quality(opportunity.get("budget_rate"))
        risk_flags = self._risk_flags(text, opportunity)
        recurring_potential = self._recurring_potential(text, opportunity.get("contract_type"))
        urgency = self._urgency(text, opportunity.get("urgency"))
        effort_vs_return = self._effort_vs_return(budget_quality, skill_tokens, opportunity.get("description"))
        portfolio_name, portfolio_reason = self._portfolio_match(skill_tokens)
        relevance = self._service_relevance(opportunity.get("title", ""), opportunity.get("description", ""), skill_tokens)
        recommendation, recommendation_detail = self._recommendation(fit_score, budget_quality, risk_flags, relevance)
        suggested_price = self._suggested_price(budget_quality, budget_amount, fit_score, skill_tokens)
        suggested_timeline = self._suggested_timeline(skill_tokens, effort_vs_return)
        capability_gaps = self._capability_gaps(skill_tokens)
        estimated_project_value = self._estimated_project_value(opportunity.get("budget_rate"), budget_amount, skill_tokens)
        concrete_matches, _ = self._concrete_matches(skill_tokens)
        # "Strongest" = most specific/longest matched phrase (e.g. prefer
        # "rest api" or "postgresql" over a shorter, less distinctive match)
        # -- simple and auditable rather than a second weighting scheme.
        strongest_technical_match = max(concrete_matches, key=len) if concrete_matches else None
        biggest_risk = self._biggest_risk(risk_flags)
        budget_source = "client-stated" if budget_amount is not None else "unknown (TTT estimate only)"
        why = self._why(recommendation, recommendation_detail, fit_score, budget_quality, risk_flags,
                          portfolio_name, capability_gaps, relevance, strongest_technical_match, biggest_risk)

        return {
            "fit_score": fit_score,
            "budget_quality": budget_quality,
            "budget_source": budget_source,
            "effort_vs_return": effort_vs_return,
            "portfolio_match": portfolio_name,
            "portfolio_match_reason": portfolio_reason,
            "recurring_potential": recurring_potential,
            "urgency": urgency,
            "risk_flags": risk_flags,
            "recommendation": recommendation,
            "recommendation_detail": recommendation_detail,
            "suggested_price": suggested_price,
            "suggested_timeline": suggested_timeline,
            "suggested_portfolio_proof": self._proof_for(portfolio_name),
            "capability_gaps": capability_gaps,
            "estimated_project_value": estimated_project_value,
            "strongest_technical_match": strongest_technical_match,
            "biggest_risk": biggest_risk,
            "why": why,
            "relevance_passed": relevance["passes_gate"],
            "relevance_exclusion_signals": relevance["exclusion_matches"],
            "relevance_positive_signals": relevance["positive_matches"],
        }

    def _skill_tokens(self, opportunity: Dict[str, Any]) -> List[str]:
        raw = (opportunity.get("required_skills") or "")
        listed = [s.strip().lower() for s in re.split(r"[,/]", raw) if s.strip()]
        text = (opportunity.get("description") or "").lower()
        # Word-boundary match only -- a naive substring check would match
        # short capability keywords like "ai" inside ordinary words like
        # "available", inflating the fit score on unrelated text.
        implied = [
            s for s in self.capability_skills
            if s not in listed and s not in _AMBIGUOUS_ENGLISH_WORD_CAPABILITIES
            and re.search(r"(?<![a-z0-9])" + re.escape(s) + r"(?![a-z0-9])", text)
        ]
        return listed + implied

    def _concrete_matches(self, skill_tokens: List[str]) -> (set, bool):
        """Shared by _fit_score and score()'s "strongest technical match"
        explainability field: which of this opportunity's skill tokens are
        real, concrete technology matches against our capability list (as
        opposed to a generic/topic word -- see _GENERIC_TOPIC_CAPABILITY_TOKENS),
        and whether any generic/topic word matched at all.

        Match direction matters (see _service_relevance's concrete_skill_positive
        comment for the identical class of bug this guards against): a skill
        token must EQUAL a capability phrase, or CONTAIN it (a longer
        descriptive skill string like "react developer" that embeds the
        exact capability name) -- never merely be a substring INSIDE a
        longer capability phrase. That reverse direction is what let the
        real scraped required_skills value "REST" (a lone, ambiguous
        fragment) match capability phrase "rest api" via "rest" in "rest
        api", wrongly registering as a match at all."""
        concrete: set = set()
        generic_hit = False
        for s in skill_tokens:
            matched_cap = next((cap for cap in self.capability_skills if s == cap or cap in s), None)
            if matched_cap is None:
                continue
            if matched_cap in _GENERIC_TOPIC_CAPABILITY_TOKENS:
                generic_hit = True
            else:
                concrete.add(matched_cap)
        return concrete, generic_hit

    def _fit_score(self, skill_tokens: List[str]) -> int:
        """0-100: weighted count of DISTINCT, concrete technology matches
        against our capability list -- not a coverage percentage.

        Root-cause fix #1 (relevance hardening pass): the original formula
        was matches / total tokens, so a listing with only one detected
        skill token that happened to match a single generic/topic
        capability word (see _GENERIC_TOPIC_CAPABILITY_TOKENS) scored a
        perfect 100% -- identical treatment to ten exact, concrete
        tech-stack matches. That's exactly how a "Freelance Writer" post
        mentioning "AI" once (skill_tokens == ["ai"]) reached fit_score=100.

        Root-cause fix #2 (qualification-calibration pass): the ratio
        formula has an OPPOSITE failure mode once fix #1 closed the first
        one -- a real WeWorkRemotely/Lemon.io-style listing tagged with
        40-50 generic recruiting keywords (blockchain, Unity, WordPress,
        Symfony, ...) alongside 3-5 genuinely matching core skills
        (React, Node.js, Python) scored only ~20/100 under the ratio
        (3 matches / 45 tags), landing in IGNORE despite being a
        legitimately strong lead -- the opposite mistake of over-scoring,
        but just as wrong. Counting DISTINCT concrete matches directly,
        uncapped by how much irrelevant noise surrounds them, fixes both
        directions at once: a thin, entirely-generic or entirely-empty
        match set still can't reach a confident score, but a real listing's
        score no longer depends on how many unrelated tags a scraper or
        aggregator happened to also attach."""
        if not skill_tokens:
            return 40  # no signal either way -- neutral-low, never a confident PURSUE
        concrete, generic_hit = self._concrete_matches(skill_tokens)
        if not concrete:
            return 45 if generic_hit else 0
        # Each additional distinct concrete tech match adds confidence, but
        # with diminishing need for more than a handful -- 3+ solid matches
        # (e.g. React, Node.js, PostgreSQL) is already as strong a signal as
        # 10 would be, so this saturates at 100 rather than requiring the
        # opportunity's ENTIRE tag list to be relevant.
        return min(100, 55 + 15 * len(concrete))

    def _budget_quality(self, budget_rate: Optional[str]) -> (str, Optional[float]):
        if not budget_rate:
            return "UNKNOWN", None
        numbers = [float(n.replace(",", "")) for n in re.findall(r"[\d,]+(?:\.\d+)?", budget_rate)]
        if not numbers:
            return "UNKNOWN", None
        amount = max(numbers)
        is_hourly = "/hr" in budget_rate.replace(" ", "").lower() or "hour" in budget_rate.lower()
        if is_hourly:
            if amount < 15:
                return "LOW", amount
            if amount < 50:
                return "MEDIUM", amount
            return "HIGH", amount
        if amount < 300:
            return "LOW", amount
        if amount < 3000:
            return "MEDIUM", amount
        return "HIGH", amount

    def _risk_flags(self, text: str, opportunity: Dict[str, Any]) -> List[str]:
        flags = [phrase for phrase in _RED_FLAG_PHRASES if phrase in text]
        if not opportunity.get("budget_rate"):
            flags.append("no budget stated")
        description = opportunity.get("description") or ""
        if len(description.strip()) < 40:
            flags.append("description too vague to scope confidently")
        return flags

    def _recurring_potential(self, text: str, contract_type: Optional[str]) -> str:
        if contract_type in {"retainer", "full-time", "part-time"}:
            return "HIGH"
        if any(w in text for w in ("ongoing", "long-term", "long term", "retainer", "monthly maintenance")):
            return "HIGH"
        if any(w in text for w in ("one-time", "one time", "single project")):
            return "LOW"
        return "MEDIUM"

    def _urgency(self, text: str, stated: Optional[str]) -> str:
        if stated and stated.strip().lower() in {"high", "urgent"}:
            return "High"
        if any(w in text for w in _URGENCY_WORDS):
            return "High"
        return stated or "Normal"

    def _effort_vs_return(self, budget_quality: str, skill_tokens: List[str], description: Optional[str]) -> str:
        complexity = len(skill_tokens) + len((description or "")) // 400
        if budget_quality == "HIGH" and complexity <= 4:
            return "Strong (high return for the effort)"
        if budget_quality == "LOW" and complexity >= 4:
            return "Weak (high effort for low return)"
        if budget_quality == "UNKNOWN":
            return "Unclear (budget not stated)"
        return "Reasonable"

    def _portfolio_match(self, skill_tokens: List[str]) -> (Optional[str], Optional[str]):
        if not skill_tokens:
            return None, None
        best, best_overlap = None, 0
        for project in self.portfolio:
            overlap = len(set(skill_tokens) & set(t.lower() for t in project["tags"]))
            if overlap > best_overlap:
                best, best_overlap = project, overlap
        if not best or best_overlap == 0:
            return None, None
        return best["name"], f"overlaps on {best_overlap} skill(s) with this opportunity"

    def _proof_for(self, portfolio_name: Optional[str]) -> Optional[str]:
        for project in self.portfolio:
            if project["name"] == portfolio_name:
                return project["proof"]
        return None

    def _service_relevance(self, title: Optional[str], description: Optional[str], skill_tokens: List[str]) -> Dict[str, Any]:
        """Hard relevance gate, independent of the numeric fit score: does
        this opportunity's actual *category of work* belong to TTT's
        acquisition profile at all? This is what a pure skill-token overlap
        score can never answer -- "mentions AI" and "is a software
        engineering deliverable" are unrelated facts, and the fit score
        alone conflated them (see _fit_score's docstring for how).

        Deliberately phrase-level and two-sided, not a single-word
        blacklist: an exclusion-role phrase (e.g. "copywriter") only fails
        the gate when there is *no* positive service signal anywhere in
        the title/description to override it. "Build an AI writing SaaS
        for copywriters" matches "copywriters" (exclusion) but also "build
        a[n]" and "saas" (positive) -- the actual ask is building software,
        so it passes. "Copywriter for AI company" matches "copywriter"
        with nothing but the bare topic word "ai" alongside it -- "ai" is
        deliberately not a positive signal on its own (see
        DEFAULT_POSITIVE_SERVICE_SIGNALS' comment) because it is exactly as
        overloaded as a topic word as it is as a skill token -- so the gate
        correctly fails it."""
        text = f"{title or ''} {description or ''}".lower()
        exclusion_matches = [phrase for phrase in self.exclusion_role_signals if phrase in text]
        positive_matches = [phrase for phrase in self.positive_service_signals if phrase in text]
        # A strong dev/tech skill token is itself a positive signal too, even
        # if its exact phrase isn't in positive_service_signals (e.g.
        # "postgresql" as a listed skill rather than in the prose). Only
        # _STRONG_DEV_OVERRIDE_TOKENS count here -- see its comment for why
        # markup/styling alone (html, css) or generic topic words (ai, saas,
        # ...) are deliberately excluded from being able to override an
        # exclusion-role match by themselves.
        #
        # Match direction matters: a skill token must EQUAL an override
        # token, or CONTAIN it as a substring (a longer descriptive skill
        # phrase like "react developer" or "rest api integration" that
        # embeds the exact tech name/phrase) -- never the reverse. Checking
        # whether the token is merely contained INSIDE a longer override
        # phrase (e.g. "s in d") is exactly the coverage-percentage class of
        # bug this hardening pass exists to close: real scraped listing data
        # showed a "Freelance Writer" job with required_skills literally set
        # to the single junk word "REST" (an unrelated scraper artifact),
        # which satisfied "rest" in "rest api" and wrongly overrode the
        # exclusion gate. "rest" and "api" are common/ambiguous fragments of
        # the two-word override phrase "rest api" and must never count on
        # their own.
        concrete_skill_positive = [
            s for s in skill_tokens
            if any(s == d or d in s for d in _STRONG_DEV_OVERRIDE_TOKENS)
        ]
        positive_matches = list(dict.fromkeys(positive_matches + concrete_skill_positive))
        passes_gate = (not exclusion_matches) or bool(positive_matches)
        return {"passes_gate": passes_gate, "exclusion_matches": exclusion_matches, "positive_matches": positive_matches}

    def _recommendation(self, fit_score: int, budget_quality: str, risk_flags: List[str],
                          relevance: Dict[str, Any]) -> (str, str):
        """Returns (recommendation, recommendation_detail).

        `recommendation` is the coarse PURSUE/MAYBE/IGNORE value everything
        downstream already branches on (proposal auto-drafting, requalify_all's
        upgrade/downgrade detection, the UI's quick filters) -- its shape is
        unchanged so nothing else needs to learn a new value.
        `recommendation_detail` additionally distinguishes
        PURSUE_WITH_BUDGET_UNKNOWN (qualification-calibration pass, item 1):
        a listing can become PURSUE on service-relevance + technical fit +
        a clean gate alone -- a missing client-stated budget is an
        uncertainty factor to surface (see score()'s budget_source field and
        _suggested_price's TTT-estimate labeling), never by itself a reason
        to bury an otherwise excellent, clearly-relevant lead in MAYBE
        forever just because a job board didn't publish a rate."""
        hard_red_flags = [f for f in risk_flags if f in _RED_FLAG_PHRASES]
        if hard_red_flags:
            return "IGNORE", "IGNORE"
        if not relevance["passes_gate"]:
            # Clearly outside TTT's service scope (an excluded role, with no
            # software-delivery signal to override it) -- never PURSUE,
            # regardless of how high the numeric fit score computed, per the
            # relevance hardening pass. This is a hard gate, not a score
            # penalty, so a future scoring tweak can't accidentally let one
            # back through.
            return "IGNORE", "IGNORE"
        if fit_score < 25:
            return "IGNORE", "IGNORE"
        if "description too vague to scope confidently" in risk_flags:
            # Scope clarity is a separate axis from budget: an opportunity
            # that's genuinely relevant but too thinly described to size up
            # confidently stays MAYBE regardless of budget -- this is NOT
            # what the missing-budget policy below is for.
            return "MAYBE", "MAYBE"
        budget_known_good = budget_quality in {"MEDIUM", "HIGH"}
        if fit_score >= 60 and budget_known_good:
            return "PURSUE", "PURSUE"
        # Missing-budget PURSUE requires a materially higher bar than the
        # budget-known path: strong technical fit (>=75, not just >=60) AND
        # a gate pass with NO excluded-role phrase present at all (not
        # merely one that got overridden by dev language) -- a borderline
        # "excluded role, but overridden" case still needs a real stated
        # budget before PURSUE, since that override is already doing one
        # job of judgment call; stacking a second one (assumed budget) on
        # top of it would be too permissive.
        strong_relevance = not relevance["exclusion_matches"]
        if fit_score >= 75 and budget_quality == "UNKNOWN" and strong_relevance:
            return "PURSUE", "PURSUE_WITH_BUDGET_UNKNOWN"
        return "MAYBE", "MAYBE"

    def _suggested_price(self, budget_quality: str, budget_amount: Optional[float], fit_score: int,
                           skill_tokens: List[str]) -> str:
        if budget_amount is not None:
            multiplier = 1.0 if fit_score >= 60 else 0.9
            return f"${round(budget_amount * multiplier)}"
        # No client-stated budget -- per the qualification-calibration
        # pass, give a workable number rather than stalling on "ask for a
        # budget range" forever, but label it unmistakably as TTT's own
        # estimate, never a client-stated figure (never invent what the
        # client would actually pay). A rough scope-based weekly-rate band,
        # kept deliberately simple to stay auditable.
        weeks = self._timeline_weeks(skill_tokens)
        low, high = weeks * 700, weeks * 1400
        return f"${low}-${high} (TTT estimate -- client budget not stated)"

    def _biggest_risk(self, risk_flags: List[str]) -> Optional[str]:
        """Single most important risk to surface, in priority order -- for
        explainability (qualification-calibration pass, item 4), not a new
        decision input."""
        hard = [f for f in risk_flags if f in _RED_FLAG_PHRASES]
        if hard:
            return hard[0]
        if "description too vague to scope confidently" in risk_flags:
            return "description too vague to scope confidently"
        if "no budget stated" in risk_flags:
            return "no budget stated -- pricing is a TTT estimate"
        return risk_flags[0] if risk_flags else None

    def _timeline_weeks(self, skill_tokens: List[str]) -> int:
        return max(1, min(8, 1 + len(skill_tokens) // 2))

    def _suggested_timeline(self, skill_tokens: List[str], effort_vs_return: str) -> str:
        return f"{self._timeline_weeks(skill_tokens)} week(s)"

    def _capability_gaps(self, skill_tokens: List[str]) -> List[str]:
        """Skills this opportunity asks for that our own capability list does
        not cover -- surfaced honestly rather than silently ignored, so a
        gap is visible before proposing anything."""
        return [s for s in skill_tokens if not any(cap in s or s in cap for cap in self.capability_skills)]

    def _estimated_project_value(self, budget_rate: Optional[str], budget_amount: Optional[float], skill_tokens: List[str]) -> Optional[str]:
        """Distinct from suggested_price (what we'd quote): this is a rough
        estimate of the total deal size implied by the stated rate and the
        estimated timeline -- e.g. an hourly rate is projected across the
        estimated weeks of work, while a fixed/project rate is taken as-is.
        Returns None (never a fabricated number) when there is no budget to
        work from at all."""
        if budget_amount is None:
            return None
        is_hourly = bool(budget_rate) and ("/hr" in budget_rate.replace(" ", "").lower() or "hour" in budget_rate.lower())
        if is_hourly:
            weeks = self._timeline_weeks(skill_tokens)
            return f"${round(budget_amount * 40 * weeks)}"
        return f"${round(budget_amount)}"

    def _why(self, recommendation: str, recommendation_detail: str, fit_score: int, budget_quality: str,
              risk_flags: List[str], portfolio_name: Optional[str], capability_gaps: List[str],
              relevance: Optional[Dict[str, Any]] = None, strongest_technical_match: Optional[str] = None,
              biggest_risk: Optional[str] = None) -> str:
        """One deterministic explanation built from the same signals score()
        already computed -- never a separate, possibly inconsistent
        judgment call. Qualification-calibration pass, item 4: every
        qualification must explain why it's relevant, why it landed on
        PURSUE/MAYBE/IGNORE, whether budget is client-stated or a TTT
        estimate, the strongest technical match, and the biggest risk --
        so the trailing sentence below is appended for every recommendation,
        not just PURSUE."""
        relevance = relevance or {"passes_gate": True, "exclusion_matches": [], "positive_matches": []}
        if recommendation == "PURSUE":
            if recommendation_detail == "PURSUE_WITH_BUDGET_UNKNOWN":
                sentence = (
                    f"Strong fit ({fit_score}/100) and clearly relevant, deliverable software/AI work -- "
                    "pursuing on service-relevance and technical fit alone since the client hasn't stated a budget."
                )
            else:
                sentence = f"Strong fit ({fit_score}/100) with {budget_quality.lower()} budget quality and no blocking risk flags."
            if portfolio_name:
                sentence += f" Matches our {portfolio_name} portfolio work."
        elif recommendation == "IGNORE":
            hard_flags = [f for f in risk_flags if f in _RED_FLAG_PHRASES]
            if hard_flags:
                sentence = f"Ignored due to red flags: {', '.join(hard_flags)}."
            elif not relevance["passes_gate"]:
                sentence = (
                    f"Ignored: this looks like {', '.join(relevance['exclusion_matches'])} work, not a software/AI "
                    "development opportunity in TTT's acquisition profile -- fit score is not evaluated for out-of-scope work."
                )
            else:
                sentence = f"Ignored: fit score too low ({fit_score}/100) to justify pursuing."
        else:
            sentence = f"Marked MAYBE: fit {fit_score}/100 with {budget_quality.lower()} budget quality -- worth a second look but not a confident pursue."
            if capability_gaps:
                sentence += f" Capability gap(s): {', '.join(capability_gaps)}."

        if relevance["passes_gate"] and relevance.get("positive_matches"):
            sentence += f" Relevant because: {', '.join(relevance['positive_matches'][:3])}."
        sentence += (
            " Budget: client-stated." if budget_quality != "UNKNOWN"
            else " Budget: unknown -- any price shown is a TTT estimate, not a client-stated figure."
        )
        if strongest_technical_match:
            sentence += f" Strongest technical match: {strongest_technical_match}."
        if biggest_risk:
            sentence += f" Biggest risk: {biggest_risk}."
        return sentence


class QualificationStore:
    def __init__(self, store: StateStore, audit: AuditLog, engine: Optional[QualificationEngine] = None, orchestrator=None):
        self.store = store
        self.audit = audit
        self.engine = engine or QualificationEngine()
        # LifecycleOrchestrator, injected -- see OpportunityStore's own note.
        self.orchestrator = orchestrator

    def qualify(self, opportunity_id: str, actor: str = "Aryan") -> Dict[str, Any]:
        opportunity = self.store.get("rh_opportunities", opportunity_id)
        if not opportunity:
            raise OpportunityError("opportunity not found")
        result = self.engine.score(dict(opportunity))
        now = utcnow()
        row = {
            "opportunity_id": opportunity_id,
            "fit_score": result["fit_score"], "budget_quality": result["budget_quality"],
            "effort_vs_return": result["effort_vs_return"], "portfolio_match": result["portfolio_match"],
            "portfolio_match_reason": result["portfolio_match_reason"], "recurring_potential": result["recurring_potential"],
            "urgency": result["urgency"], "risk_flags": ", ".join(result["risk_flags"]) if result["risk_flags"] else None,
            "recommendation": result["recommendation"], "suggested_price": result["suggested_price"],
            "suggested_timeline": result["suggested_timeline"], "suggested_portfolio_proof": result["suggested_portfolio_proof"],
            "capability_gaps": ", ".join(result["capability_gaps"]) if result["capability_gaps"] else None,
            "estimated_project_value": result["estimated_project_value"], "why": result["why"],
            "relevance_passed": 1 if result["relevance_passed"] else 0,
            "relevance_exclusion_signals": ", ".join(result["relevance_exclusion_signals"]) if result["relevance_exclusion_signals"] else None,
            "relevance_positive_signals": ", ".join(result["relevance_positive_signals"]) if result["relevance_positive_signals"] else None,
            "recommendation_detail": result["recommendation_detail"],
            "budget_source": result["budget_source"],
            "strongest_technical_match": result["strongest_technical_match"],
            "biggest_risk": result["biggest_risk"],
            "created_at": now,
        }
        self.store.create("rh_qualifications", row)
        if opportunity["stage"] == "New":
            OpportunityStore(self.store, self.audit).move_stage(opportunity_id, "Qualified", actor, "auto-qualified")
        self.audit.append("RH_OPPORTUNITY_QUALIFIED", {"opportunity_id": opportunity_id, "recommendation": result["recommendation"], "fit_score": result["fit_score"]})
        if self.orchestrator is not None:
            # Always recorded regardless of PURSUE/MAYBE/IGNORE -- mirrors
            # the existing stage-move behavior above, which likewise never
            # branches on the recommendation. Deciding to drop an IGNORE
            # opportunity to LOST is a business call for a later pass, not
            # something this qualification hook should do silently.
            self.orchestrator.try_transition(
                opportunity_id, "QUALIFIED", actor, reason=f"qualified: {result['recommendation']}",
                evidence={"fit_score": result["fit_score"], "recommendation": result["recommendation"]},
            )
        return row


# ---------------------------------------------------------------------------
# Proposal generation (deterministic templates, tailored to the opportunity)
# ---------------------------------------------------------------------------

class ProposalError(ValueError):
    pass


def _first_sentence(text: Optional[str], max_len: int = 220) -> str:
    text = (text or "").strip()
    if not text:
        return "your requirements"
    text = re.split(r"(?<=[.!?])\s", text)[0]
    return (text[:max_len] + "...") if len(text) > max_len else text


def generate_proposal_text(opportunity: Dict[str, Any], qualification: Optional[Dict[str, Any]], kind: str) -> str:
    if kind not in PROPOSAL_KINDS:
        raise ProposalError(f"kind must be one of {sorted(PROPOSAL_KINDS)}")
    title = opportunity.get("title") or "your project"
    need = _first_sentence(opportunity.get("description"))
    skills = opportunity.get("required_skills") or "the skills described"
    proof = (qualification or {}).get("suggested_portfolio_proof") or "similar full-stack work in my portfolio"
    price = (qualification or {}).get("suggested_price") or "a price to confirm once scope is final"
    timeline = (qualification or {}).get("suggested_timeline") or "a timeline to confirm once scope is final"
    risks = (qualification or {}).get("risk_flags") or ""
    risk_line = f"One thing worth flagging: {risks}." if risks else "No major risks or open questions on my end."

    if kind == "short":
        return (
            f"Hi -- I read through \"{title}\" and I can help. {need} "
            f"I've built comparable work before ({proof}). "
            f"Estimated: {timeline} at {price}. Happy to share more detail if useful."
        )
    if kind == "detailed":
        return (
            f"Understanding of the need:\n{need}\n\n"
            f"Proposed solution:\nI'd approach this using {skills}, structured around the scope below.\n\n"
            f"Proof:\n{proof}\n\n"
            f"Scope:\n- Discovery and confirming requirements\n- Build against \"{title}\"\n- Testing and handover\n\n"
            f"Timeline: {timeline}\nPrice: {price}\n\n"
            f"Risks/assumptions: {risk_line}\n\n"
            f"Next step: happy to jump on a short call to confirm scope."
        )
    if kind == "upwork":
        return (
            f"Hi! \"{title}\" is a strong fit for my background -- {need} "
            f"I've delivered similar work ({proof}), and I can start promptly. "
            f"My estimate is {timeline} for {price}, but I'm glad to adjust once we've nailed down exact scope. "
            f"{risk_line} Would love to discuss over a quick call."
        )
    if kind == "email_pitch":
        return (
            f"Subject: Re: {title}\n\n"
            f"Hi,\n\nI came across your listing for \"{title}\" and wanted to reach out directly. {need} "
            f"I've done closely related work before -- {proof} -- and estimate {timeline} at {price} for something like this. "
            f"{risk_line}\n\nWould you be open to a short call this week?\n\nBest,\nAryan"
        )
    # follow_up
    return (
        f"Hi -- following up on my proposal for \"{title}\". Still happy to help if this is still open -- "
        f"my estimate stays {price} over {timeline}. Let me know if you have questions or want to hop on a call."
    )


class ProposalStore:
    def __init__(self, store: StateStore, audit: AuditLog, needs_aryan=None, orchestrator=None):
        self.store = store
        self.audit = audit
        self.needs_aryan = needs_aryan  # NeedsAryanQueue, injected to avoid a circular import at module load
        self.orchestrator = orchestrator  # LifecycleOrchestrator, injected for the same reason

    def generate(self, opportunity_id: str, kind: str, actor: str = "Aryan") -> Dict[str, Any]:
        opportunity = self.store.get("rh_opportunities", opportunity_id)
        if not opportunity:
            raise OpportunityError("opportunity not found")
        qualification = OpportunityStore(self.store, self.audit).latest_qualification(opportunity_id)
        content = generate_proposal_text(dict(opportunity), qualification, kind)
        now = utcnow()
        proposal_id = self.store.create("rh_proposals", {
            "opportunity_id": opportunity_id, "kind": kind, "content": content, "status": "DRAFT",
            "approved_by": None, "approved_at": None, "created_at": now, "updated_at": now,
        })
        if opportunity["stage"] == "Qualified":
            OpportunityStore(self.store, self.audit).move_stage(opportunity_id, "Proposal Ready", actor, f"{kind} proposal drafted")
        self.audit.append("RH_PROPOSAL_DRAFTED", {"opportunity_id": opportunity_id, "proposal_id": proposal_id, "kind": kind})
        needs_aryan_id = None
        if self.needs_aryan is not None:
            needs_aryan_id = self.needs_aryan.create_item(
                "proposal_approval", f"Approve {kind} proposal: {opportunity['title']}",
                "Review the drafted proposal and approve, reject, or request changes before anything is sent.",
                actor=actor, recommendation=(qualification or {}).get("recommendation"),
                ref_type="rh_proposal", ref_id=proposal_id,
            )
        if self.orchestrator is not None:
            self.orchestrator.try_transition(opportunity_id, "PITCH_READY", actor, reason=f"{kind} proposal drafted")
            if needs_aryan_id is not None:
                self.orchestrator.try_transition(
                    opportunity_id, "AWAITING_APPROVAL", actor, reason="proposal awaiting Aryan's approval",
                    approval_required=True, approval_status="PENDING",
                )
        return {"proposal_id": proposal_id, "content": content, "needs_aryan_id": needs_aryan_id}

    def mark_approved(self, proposal_id: str, actor: str) -> None:
        proposal = self.store.get("rh_proposals", proposal_id)
        if not proposal:
            raise ProposalError("proposal not found")
        self.store.update("rh_proposals", proposal_id, status="APPROVED", approved_by=actor, approved_at=utcnow())
        self.audit.append("RH_PROPOSAL_APPROVED", {"proposal_id": proposal_id, "actor": actor})

    def mark_superseded(self, proposal_id: str, actor: str, reason: str) -> None:
        """Used only by the requalification pass: a proposal drafted while an
        opportunity was (incorrectly) qualified PURSUE, whose opportunity has
        since been requalified to MAYBE/IGNORE. Never deletes the proposal --
        its content and history stay intact for audit -- it just moves the
        status off DRAFT/APPROVED so it can never be mistaken for one still
        awaiting a real decision. A SUPERSEDED proposal was never sent."""
        proposal = self.store.get("rh_proposals", proposal_id)
        if not proposal:
            raise ProposalError("proposal not found")
        if proposal["status"] == "APPROVED":
            raise ProposalError("an already-approved proposal cannot be superseded automatically -- decide it manually")
        self.store.update("rh_proposals", proposal_id, status="SUPERSEDED", approved_by=None, approved_at=None)
        self.audit.append("RH_PROPOSAL_SUPERSEDED", {"proposal_id": proposal_id, "actor": actor, "reason": reason})


# ---------------------------------------------------------------------------
# Follow-up engine (drafts only -- never auto-sends)
# ---------------------------------------------------------------------------

class FollowupError(ValueError):
    pass


def generate_followup_text(opportunity: Dict[str, Any], kind: str) -> str:
    if kind not in FOLLOWUP_KINDS:
        raise FollowupError(f"kind must be one of {sorted(FOLLOWUP_KINDS)}")
    title = opportunity.get("title") or "your project"
    client = opportunity.get("client_name") or "there"
    templates = {
        "proposal_followup": f"Hi {client} -- just checking in on the proposal I sent for \"{title}\". Happy to answer any questions or adjust scope.",
        "response_followup": f"Hi {client} -- following up on \"{title}\" since I haven't heard back. Still very interested if it's still open.",
        "negotiation_followup": f"Hi {client} -- circling back on the terms we discussed for \"{title}\". Let me know if the latest numbers work for you.",
        "payment_followup": f"Hi {client} -- friendly reminder on the outstanding payment for \"{title}\". Let me know if you need anything from me to process it.",
        "repeat_business_followup": f"Hi {client} -- it's been a while since we wrapped up \"{title}\". Wanted to check in and see if there's anything new I can help with.",
    }
    return templates[kind]


class FollowupStore:
    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit

    def generate(self, opportunity_id: str, kind: str, due_at: Optional[str] = None) -> str:
        opportunity = self.store.get("rh_opportunities", opportunity_id)
        if not opportunity:
            raise OpportunityError("opportunity not found")
        content = generate_followup_text(dict(opportunity), kind)
        now = utcnow()
        followup_id = self.store.create("rh_followups", {
            "opportunity_id": opportunity_id, "kind": kind, "draft_content": content, "status": "DRAFT",
            "due_at": due_at, "created_at": now, "updated_at": now,
        })
        self.audit.append("RH_FOLLOWUP_DRAFTED", {"opportunity_id": opportunity_id, "followup_id": followup_id, "kind": kind})
        return followup_id

    def mark_sent(self, followup_id: str, actor: str) -> None:
        followup = self.store.get("rh_followups", followup_id)
        if not followup:
            raise FollowupError("followup not found")
        if followup["status"] == "SENT":
            raise FollowupError("this followup was already marked sent")
        self.store.update("rh_followups", followup_id, status="SENT", updated_at=utcnow())
        self.audit.append("RH_FOLLOWUP_SENT", {"followup_id": followup_id, "actor": actor})

    def list_due(self) -> List[Dict[str, Any]]:
        drafts = self.store.list("rh_followups", "status=?", ("DRAFT",))
        return list(reversed(drafts))


# ---------------------------------------------------------------------------
# Won -> Active Job -> Falguna handoff (real, not faked)
# ---------------------------------------------------------------------------

class ActiveJobError(ValueError):
    pass


class ActiveJobStore:
    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit

    def create_from_won_opportunity(self, opportunity_id: str, actor: str = "Aryan") -> str:
        opportunity = self.store.get("rh_opportunities", opportunity_id)
        if not opportunity:
            raise ActiveJobError("opportunity not found")
        if opportunity["stage"] != "Won":
            raise ActiveJobError("opportunity must be marked Won before an Active Job can be created")
        # QA finding (independent verification pass): calling this twice for
        # the same opportunity (e.g. a double click, or Won being triggered
        # again) used to create a second, independent rh_active_jobs row --
        # the checklist explicitly calls out that duplicate Won actions must
        # not create duplicate jobs. This is idempotent: an opportunity that
        # already has an Active Job returns that same job's id rather than
        # creating another one.
        existing = self.store.list("rh_active_jobs", "opportunity_id=?", (opportunity_id,))
        if existing:
            return existing[-1]["id"]
        approved_proposals = self.store.list("rh_proposals", "opportunity_id=? AND status=?", (opportunity_id, "APPROVED"))
        requirement = approved_proposals[-1]["content"] if approved_proposals else (opportunity.get("description") or opportunity["title"])
        # QA finding (independent verification pass): the job payload used to
        # carry only title/requirement/client/price -- the deadline, and any
        # deliverables/notes context the opportunity had, were silently
        # dropped on the Won -> Active Job transition even though they were
        # already sitting on the opportunity record. Nothing here is
        # fabricated: deadline comes straight from the opportunity's own
        # field, deliverables is the real approved-proposal scope when one
        # exists (there is no separate structured deliverables list in
        # Revenue Hunter yet, so this is honestly None rather than invented
        # when no proposal was approved), and notes summarizes only the
        # free-text context fields the opportunity actually has.
        note_parts = [
            f"urgency: {opportunity['urgency']}" if opportunity.get("urgency") else None,
            f"contract type: {opportunity['contract_type']}" if opportunity.get("contract_type") else None,
            f"location/timezone: {opportunity['location_timezone']}" if opportunity.get("location_timezone") else None,
        ]
        notes = "; ".join(p for p in note_parts if p) or None
        payload = {
            "title": opportunity["title"],
            "requirement": requirement,
            "source_opportunity_id": opportunity_id,
            "client_name": opportunity.get("client_name"),
            "price": opportunity.get("final_price"),
            "deadline": opportunity.get("deadline"),
            "deliverables": approved_proposals[-1]["content"] if approved_proposals else None,
            "notes": notes,
        }
        now = utcnow()
        job_id = self.store.create("rh_active_jobs", {
            "opportunity_id": opportunity_id, "job_payload_json": _dump_payload(payload),
            "handoff_status": "PENDING", "mission_id": None, "created_at": now, "updated_at": now,
        })
        self.audit.append("RH_ACTIVE_JOB_CREATED", {"opportunity_id": opportunity_id, "active_job_id": job_id, "actor": actor})
        return job_id

    def trigger_handoff(self, active_job_id: str, repository: str, control, actor: str = "Aryan", policy_overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Owner-triggered only. Calls the real, existing, tested acceptor in
        falguna/handoff.py -- if this raises, nothing was silently accepted;
        if it returns, a real Falguna Engineering mission row exists."""
        job = self.store.get("rh_active_jobs", active_job_id)
        if not job:
            raise ActiveJobError("active job not found")
        if job["handoff_status"] == "HANDED_OFF":
            raise ActiveJobError("this active job was already handed off")
        payload = _load_payload(job["job_payload_json"])
        payload["repository"] = repository
        if policy_overrides:
            payload["policy_overrides"] = policy_overrides
        validate_handoff_payload(payload)  # raise before mutating anything if the repo/path is bad
        policy = RunPolicy(**policy_overrides) if policy_overrides else None
        result = accept_revenue_hunter_handoff(control, payload, policy)
        self.store.update("rh_active_jobs", active_job_id, handoff_status="HANDED_OFF", mission_id=result["mission_id"], updated_at=utcnow())
        self.audit.append("RH_ACTIVE_JOB_HANDED_OFF", {"active_job_id": active_job_id, "mission_id": result["mission_id"], "actor": actor})
        return result

    def get(self, active_job_id: str) -> Optional[Dict[str, Any]]:
        return self.store.get("rh_active_jobs", active_job_id)

    def list(self) -> List[Dict[str, Any]]:
        return list(reversed(self.store.list("rh_active_jobs")))


def _dump_payload(payload: Dict[str, Any]) -> str:
    import json
    return json.dumps(payload)


def _load_payload(payload_json: str) -> Dict[str, Any]:
    import json
    return json.loads(payload_json)


# ---------------------------------------------------------------------------
# Dashboard + analytics
# ---------------------------------------------------------------------------

_RELEVANT_NEEDS_ARYAN_KINDS = {
    "proposal_approval", "pricing_decision", "outreach_approval",
    "client_response_decision", "negotiation_response_approval", "scope_expansion",
}


class DashboardService:
    def __init__(self, store: StateStore):
        self.store = store

    def today(self, needs_aryan_pending: Optional[List[Dict[str, Any]]] = None,
              discovery_runs: Optional[List[Dict[str, Any]]] = None, aging_days: int = 14) -> Dict[str, Any]:
        opportunities = self.store.list("rh_opportunities")
        by_id = {o["id"]: o for o in opportunities}
        active = [o for o in opportunities if o["stage"] not in TERMINAL_STAGES]
        won = [o for o in opportunities if o["stage"] == "Won"]

        needs_aryan_pending = needs_aryan_pending or []
        rh_pending = [i for i in needs_aryan_pending if i.get("ref_type", "").startswith("rh_") and i.get("kind") in _RELEVANT_NEEDS_ARYAN_KINDS]

        followups_due = self.store.list("rh_followups", "status=?", ("DRAFT",))
        needing_qualification = [o for o in opportunities if o["stage"] == "New"]

        pipeline_value = 0.0
        for opp in active:
            qual = self._latest_qualification(opp["id"])
            price = _parse_number(qual["suggested_price"]) if qual else None
            if price is not None:
                pipeline_value += price
        won_revenue = sum(o["final_price"] for o in won if o.get("final_price"))

        next_actions: List[Dict[str, Any]] = []
        for opp in needing_qualification:
            next_actions.append({"type": "qualify", "opportunity_id": opp["id"], "title": opp["title"], "why": "New opportunity has not been qualified yet"})
        for item in rh_pending:
            next_actions.append({"type": "approve", "ref_id": item.get("ref_id"), "title": item.get("title"), "why": item.get("what_is_needed")})
        for fu in followups_due:
            opp = by_id.get(fu["opportunity_id"])
            next_actions.append({"type": "follow_up", "opportunity_id": fu["opportunity_id"], "title": (opp or {}).get("title", fu["opportunity_id"]), "why": f"{fu['kind']} draft ready to send"})

        now = datetime.now(timezone.utc)
        aging = []
        for opp in active:
            try:
                created = datetime.fromisoformat(opp["created_at"])
            except (TypeError, ValueError):
                continue
            if (now - created).days >= aging_days:
                aging.append(opp)

        discovery_runs = discovery_runs or []
        last_run = discovery_runs[0] if discovery_runs else None
        found_today = 0
        if last_run and last_run.get("started_at"):
            try:
                started = datetime.fromisoformat(last_run["started_at"])
                if started.date() == now.date():
                    found_today = last_run.get("opportunities_new", 0)
            except (TypeError, ValueError):
                pass

        return {
            "opportunities_needing_qualification": needing_qualification,
            "proposals_needing_approval": rh_pending,
            "followups_due": followups_due,
            "pipeline_value": round(pipeline_value, 2),
            "won_revenue": round(won_revenue, 2),
            "next_actions": next_actions[:10],
            "aging_opportunities": aging,
            "last_discovery_run": last_run,
            "opportunities_discovered_today": found_today,
        }

    def _latest_qualification(self, opportunity_id: str) -> Optional[Dict[str, Any]]:
        rows = self.store.list("rh_qualifications", "opportunity_id=?", (opportunity_id,))
        return rows[-1] if rows else None


def _parse_number(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    numbers = re.findall(r"[\d,]+(?:\.\d+)?", text)
    if not numbers:
        return None
    return float(numbers[0].replace(",", ""))


class AnalyticsService:
    def __init__(self, store: StateStore):
        self.store = store

    def summary(self) -> Dict[str, Any]:
        opportunities = self.store.list("rh_opportunities")
        qualified_ids = {q["opportunity_id"] for q in self.store.list("rh_qualifications")}
        proposals = self.store.list("rh_proposals")
        sent_stage_events = self.store.list("rh_stage_history", "to_stage=?", ("Applied/Sent",))
        replied_events = self.store.list("rh_stage_history", "to_stage=?", ("Replied",))
        meeting_events = self.store.list("rh_stage_history", "to_stage=?", ("Meeting",))
        won = [o for o in opportunities if o["stage"] == "Won"]
        lost = [o for o in opportunities if o["stage"] == "Lost"]
        decided = len(won) + len(lost)
        conversion_rate = round(len(won) / decided, 3) if decided else 0.0
        won_revenue = round(sum(o["final_price"] for o in won if o.get("final_price")), 2)

        pipeline_value = 0.0
        for opp in opportunities:
            if opp["stage"] in TERMINAL_STAGES:
                continue
            quals = self.store.list("rh_qualifications", "opportunity_id=?", (opp["id"],))
            if quals:
                amount = _parse_number(quals[-1]["suggested_price"])
                if amount is not None:
                    pipeline_value += amount

        source_performance: Dict[str, Dict[str, int]] = {}
        for opp in opportunities:
            src = opp.get("source") or "unknown"
            bucket = source_performance.setdefault(src, {"added": 0, "won": 0, "lost": 0})
            bucket["added"] += 1
            if opp["stage"] == "Won":
                bucket["won"] += 1
            elif opp["stage"] == "Lost":
                bucket["lost"] += 1

        return {
            "opportunities_added": len(opportunities),
            "qualified": len(qualified_ids),
            "proposals_created": len(proposals),
            "proposals_sent": len(sent_stage_events),
            "replies": len(replied_events),
            "meetings": len(meeting_events),
            "wins": len(won),
            "losses": len(lost),
            "conversion_rate": conversion_rate,
            "pipeline_value": round(pipeline_value, 2),
            "won_revenue": won_revenue,
            "source_performance": source_performance,
        }


# ---------------------------------------------------------------------------
# Needs Aryan decision side effects (called by hq_web.py right after
# NeedsAryanQueue.decide() -- keeps NeedsAryanQueue itself fully generic)
# ---------------------------------------------------------------------------

def apply_decision_side_effect(store: StateStore, audit: AuditLog, item: Dict[str, Any], action: str, actor: str, orchestrator=None) -> None:
    if item.get("ref_type") != "rh_proposal":
        return
    proposal = store.get("rh_proposals", item["ref_id"])
    if action == "APPROVED":
        ProposalStore(store, audit).mark_approved(item["ref_id"], actor)
        if orchestrator is not None and proposal:
            orchestrator.try_transition(
                proposal["opportunity_id"], "APPROVED", actor, reason="proposal approved",
                approval_required=True, approval_status="APPROVED",
            )
        return
    if action in {"REJECTED", "CHANGES_REQUESTED"} and orchestrator is not None and proposal:
        # Bounced back to PITCH_READY (a real, allowed edge from
        # AWAITING_APPROVAL) rather than left stuck -- a rejected/
        # changes-requested proposal means a new one needs drafting, not
        # that the opportunity is dead.
        orchestrator.try_transition(
            proposal["opportunity_id"], "PITCH_READY", actor, reason=f"proposal {action.lower()}",
            approval_required=True, approval_status=action,
        )
