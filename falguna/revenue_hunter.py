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
    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit

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
        return list(reversed(rows))


# ---------------------------------------------------------------------------
# Qualification scoring (deterministic)
# ---------------------------------------------------------------------------

DEFAULT_CAPABILITY_SKILLS = [
    "javascript", "typescript", "react", "node.js", "node", "express", "python",
    "sqlite", "postgresql", "rest api", "stripe", "payments", "e-commerce",
    "ecommerce", "booking system", "crm", "automation", "ai", "saas", "html", "css",
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

    def __init__(self, capability_skills: Optional[List[str]] = None, portfolio: Optional[List[Dict[str, Any]]] = None):
        self.capability_skills = [s.lower() for s in (capability_skills or DEFAULT_CAPABILITY_SKILLS)]
        self.portfolio = portfolio or DEFAULT_PORTFOLIO_PROJECTS

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
        recommendation = self._recommendation(fit_score, budget_quality, risk_flags)
        suggested_price = self._suggested_price(budget_quality, budget_amount, fit_score)
        suggested_timeline = self._suggested_timeline(skill_tokens, effort_vs_return)

        return {
            "fit_score": fit_score,
            "budget_quality": budget_quality,
            "effort_vs_return": effort_vs_return,
            "portfolio_match": portfolio_name,
            "portfolio_match_reason": portfolio_reason,
            "recurring_potential": recurring_potential,
            "urgency": urgency,
            "risk_flags": risk_flags,
            "recommendation": recommendation,
            "suggested_price": suggested_price,
            "suggested_timeline": suggested_timeline,
            "suggested_portfolio_proof": self._proof_for(portfolio_name),
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
            if s not in listed and re.search(r"(?<![a-z0-9])" + re.escape(s) + r"(?![a-z0-9])", text)
        ]
        return listed + implied

    def _fit_score(self, skill_tokens: List[str]) -> int:
        """0-100: overlap between the opportunity's skills and our capability
        list, scaled by how many of the opportunity's own listed skills we
        actually cover (so a 1-skill exact match scores as well as a
        10-skill exact match, but partial coverage is penalized)."""
        if not skill_tokens:
            return 40  # no signal either way -- neutral-low, never a confident PURSUE
        matches = sum(1 for s in skill_tokens if any(cap in s or s in cap for cap in self.capability_skills))
        coverage = matches / len(skill_tokens)
        return round(min(100, coverage * 100))

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

    def _recommendation(self, fit_score: int, budget_quality: str, risk_flags: List[str]) -> str:
        hard_red_flags = [f for f in risk_flags if f in _RED_FLAG_PHRASES]
        if hard_red_flags:
            return "IGNORE"
        if fit_score < 25:
            return "IGNORE"
        if fit_score >= 60 and budget_quality in {"MEDIUM", "HIGH"} and len(risk_flags) == 0:
            return "PURSUE"
        return "MAYBE"

    def _suggested_price(self, budget_quality: str, budget_amount: Optional[float], fit_score: int) -> str:
        if budget_amount is None:
            return "Ask for budget range before quoting"
        multiplier = 1.0 if fit_score >= 60 else 0.9
        return f"${round(budget_amount * multiplier)}"

    def _suggested_timeline(self, skill_tokens: List[str], effort_vs_return: str) -> str:
        weeks = max(1, min(8, 1 + len(skill_tokens) // 2))
        return f"{weeks} week(s)"


class QualificationStore:
    def __init__(self, store: StateStore, audit: AuditLog, engine: Optional[QualificationEngine] = None):
        self.store = store
        self.audit = audit
        self.engine = engine or QualificationEngine()

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
            "created_at": now,
        }
        self.store.create("rh_qualifications", row)
        if opportunity["stage"] == "New":
            OpportunityStore(self.store, self.audit).move_stage(opportunity_id, "Qualified", actor, "auto-qualified")
        self.audit.append("RH_OPPORTUNITY_QUALIFIED", {"opportunity_id": opportunity_id, "recommendation": result["recommendation"], "fit_score": result["fit_score"]})
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
    def __init__(self, store: StateStore, audit: AuditLog, needs_aryan=None):
        self.store = store
        self.audit = audit
        self.needs_aryan = needs_aryan  # NeedsAryanQueue, injected to avoid a circular import at module load

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
        return {"proposal_id": proposal_id, "content": content, "needs_aryan_id": needs_aryan_id}

    def mark_approved(self, proposal_id: str, actor: str) -> None:
        proposal = self.store.get("rh_proposals", proposal_id)
        if not proposal:
            raise ProposalError("proposal not found")
        self.store.update("rh_proposals", proposal_id, status="APPROVED", approved_by=actor, approved_at=utcnow())
        self.audit.append("RH_PROPOSAL_APPROVED", {"proposal_id": proposal_id, "actor": actor})


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
        approved_proposals = self.store.list("rh_proposals", "opportunity_id=? AND status=?", (opportunity_id, "APPROVED"))
        requirement = approved_proposals[-1]["content"] if approved_proposals else (opportunity.get("description") or opportunity["title"])
        payload = {
            "title": opportunity["title"],
            "requirement": requirement,
            "source_opportunity_id": opportunity_id,
            "client_name": opportunity.get("client_name"),
            "price": opportunity.get("final_price"),
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

    def today(self, needs_aryan_pending: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
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

        return {
            "opportunities_needing_qualification": needing_qualification,
            "proposals_needing_approval": rh_pending,
            "followups_due": followups_due,
            "pipeline_value": round(pipeline_value, 2),
            "won_revenue": round(won_revenue, 2),
            "next_actions": next_actions[:10],
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

def apply_decision_side_effect(store: StateStore, audit: AuditLog, item: Dict[str, Any], action: str, actor: str) -> None:
    if item.get("ref_type") != "rh_proposal" or action != "APPROVED":
        return
    ProposalStore(store, audit).mark_approved(item["ref_id"], actor)
