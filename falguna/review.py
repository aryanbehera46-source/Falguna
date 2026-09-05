from abc import ABC, abstractmethod
import json
from typing import List

from .gateway import ModelGateway
from .models import ReviewResult
from .workers import StructuredEditWorker


class ReviewerAdapter(ABC):
    @abstractmethod
    def review(self, requirement: str, diff: str, changed_files: List[str], verification: dict) -> ReviewResult:
        raise NotImplementedError


class SemanticIndependentReviewer(ReviewerAdapter):
    """Fail-closed semantic review contract with concise, persistable evidence."""

    BLOCKED_MARKERS = ("OPENAI_API_KEY", "AWS_SECRET", "BEGIN PRIVATE KEY", "--no-verify")

    def review(self, requirement: str, diff: str, changed_files: List[str], verification: dict) -> ReviewResult:
        findings = []
        dimensions = {
            "requirement_satisfaction": {"passed": bool(requirement.strip()) and bool(diff.strip()), "evidence": "requirement and candidate diff supplied"},
            "scope_compliance": {"passed": bool(changed_files), "evidence": f"{len(changed_files)} changed file(s) passed policy gates"},
            "regression_evidence": {"passed": bool(verification.get("passed")), "evidence": "Definition of Done verification result"},
            "unresolved_uncertainty": {"passed": False, "evidence": "deterministic reviewer cannot establish semantic satisfaction"},
        }
        if not diff.strip():
            findings.append("empty diff")
        for marker in self.BLOCKED_MARKERS:
            if marker in diff:
                findings.append(f"blocked marker in diff: {marker}")
        if any(path.startswith("schema/") or path == "falguna/policy.py" for path in changed_files):
            findings.append("protected policy/schema change")
        uncertainty = ["semantic requirement satisfaction requires a configured independent semantic reviewer"]
        findings.extend(uncertainty)
        return ReviewResult(False, "independent semantic review failed closed", findings, dimensions, uncertainty)


class ScriptedSemanticReviewer(ReviewerAdapter):
    """Offline test adapter; production semantic reviewers implement the same contract."""

    def __init__(self, callback):
        self.callback = callback

    def review(self, requirement: str, diff: str, changed_files: List[str], verification: dict) -> ReviewResult:
        result = self.callback(requirement, diff, changed_files, verification)
        required = {"requirement_satisfaction", "scope_compliance", "regression_evidence", "unresolved_uncertainty"}
        if set(result.dimensions) != required:
            return ReviewResult(False, "semantic review contract incomplete", ["missing required review dimensions"], result.dimensions, ["incomplete review"])
        if any(not dimension.get("passed", False) for dimension in result.dimensions.values()):
            result.approved = False
        if result.unresolved_uncertainty:
            result.approved = False
        return result


class ModelSemanticReviewer(ReviewerAdapter):
    """Independent structured model review; stores verdict evidence, never hidden reasoning."""

    def __init__(self, gateway: ModelGateway, timeout_seconds: int = 180, transport=None):
        self.gateway = gateway
        self.timeout_seconds = timeout_seconds
        self.transport = transport or StructuredEditWorker._request

    def review(self, requirement: str, diff: str, changed_files: List[str], verification: dict) -> ReviewResult:
        config = self.gateway.configuration()
        payload = {
            "model": config["model"],
            "messages": [
                {"role": "system", "content": "Act as an independent release reviewer. Judge only the supplied evidence. Put only defects that block approval in blocking_findings; put positive evidence only in dimension evidence fields. Return concise conclusions and evidence references, not private reasoning or chain of thought. Fail uncertain claims closed."},
                {"role": "user", "content": json.dumps({"requirement": requirement, "changed_files": changed_files, "diff": diff[-30000:], "verification": verification}, ensure_ascii=False)},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "semantic_release_review",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "summary": {"type": "string"},
                            "blocking_findings": {"type": "array", "items": {"type": "string"}},
                            "unresolved_uncertainty": {"type": "array", "items": {"type": "string"}},
                            "dimensions": {
                                "type": "object",
                                "properties": {name: {"type": "object", "properties": {"passed": {"type": "boolean"}, "evidence": {"type": "string"}}, "required": ["passed", "evidence"], "additionalProperties": False} for name in ("requirement_satisfaction", "scope_compliance", "regression_evidence", "unresolved_uncertainty")},
                                "required": ["requirement_satisfaction", "scope_compliance", "regression_evidence", "unresolved_uncertainty"],
                                "additionalProperties": False,
                            },
                        },
                        "required": ["summary", "blocking_findings", "unresolved_uncertainty", "dimensions"],
                        "additionalProperties": False,
                    },
                },
            },
        }
        try:
            decoded = self.transport(config, payload, self.timeout_seconds)
            response = json.loads(decoded["choices"][0]["message"]["content"])
            dimensions = response["dimensions"]
            uncertainty = response["unresolved_uncertainty"]
            approved = all(item["passed"] for item in dimensions.values()) and not response["blocking_findings"] and not uncertainty
            usage = decoded.get("usage", {})
            prompt_tokens = int(usage.get("prompt_tokens", 0))
            completion_tokens = int(usage.get("completion_tokens", 0))
            cached_tokens = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0))
            cost = max(0, prompt_tokens - cached_tokens) * 0.75e-6 + cached_tokens * 0.075e-6 + completion_tokens * 4.50e-6
            call = {"provider": "openai-compatible", "model": config["model"], "purpose": "independent-semantic-review", "input_tokens": prompt_tokens, "output_tokens": completion_tokens, "cost_usd": cost, "metadata": {"adapter": "semantic-review", "cached_input_tokens": cached_tokens}}
            return ReviewResult(approved, response["summary"], response["blocking_findings"], dimensions, uncertainty, [call], cost)
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError, OSError) as exc:
            return ReviewResult(False, "semantic reviewer failed closed", [f"review adapter error: {exc}"], {}, ["review result unavailable"])
