"""TTT Media/Growth Engine v1 -- Analytics/Feedback loop and growth
experiments (Sections 18-19, Pass D).

`AnalyticsStore.record` only ever persists a real, sourced number --
`source` is a required, honest label ("manual_entry", "platform_api:...",
or "simulated_qa" for QA-only data), never inferred or guessed. Simulated
rows are never silently mixed into a real recommendation:
`GrowthAgent.recommend` filters them out of its real-data view and, if
that leaves nothing, says "insufficient data" rather than fabricating a
signal -- "avoid fake precision" applied literally. Growth experiments are
a plain, minimal record (hypothesis/variable/expected signal/result/
decision) -- no advanced attribution modeling in v1, per the build spec.
"""

from typing import Any, Dict, List, Optional

from .audit import AuditLog
from .store import StateStore, utcnow

METRIC_KINDS = {"views", "reach", "engagement", "clicks", "leads", "conversions", "watch_time_seconds"}
EXPERIMENT_STATUSES = {"RUNNING", "COMPLETED", "CANCELLED"}


class AnalyticsError(ValueError):
    pass


class AnalyticsStore:
    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit

    def record(self, publication_id: str, metric_kind: str, value: float, source: str, captured_at: Optional[str] = None, actor: str = "system") -> str:
        if metric_kind not in METRIC_KINDS:
            raise AnalyticsError(f"metric_kind must be one of {sorted(METRIC_KINDS)}")
        if not source or not source.strip():
            raise AnalyticsError("source is required -- never record a metric without saying where it came from")
        if value is None or value < 0:
            raise AnalyticsError("value must be a real, non-negative number")
        metric_id = self.store.create("media_analytics", {
            "publication_id": publication_id, "metric_kind": metric_kind, "value": float(value), "source": source.strip(),
            "captured_at": captured_at, "created_at": utcnow(),
        })
        self.audit.append("MEDIA_ANALYTICS_RECORDED", {"metric_id": metric_id, "publication_id": publication_id, "metric_kind": metric_kind, "source": source, "actor": actor})
        return metric_id

    def list(self, publication_id: str, real_only: bool = False) -> List[Dict[str, Any]]:
        rows = self.store.list("media_analytics", "publication_id=?", (publication_id,))
        if real_only:
            rows = [r for r in rows if "simulated" not in r["source"].lower()]
        return list(reversed(rows))


class GrowthExperimentStore:
    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit

    def create(self, hypothesis: str, content_id: Optional[str] = None, variable_tested: Optional[str] = None, expected_signal: Optional[str] = None, actor: str = "Aryan") -> str:
        if not hypothesis or not hypothesis.strip():
            raise AnalyticsError("hypothesis is required")
        now = utcnow()
        experiment_id = self.store.create("media_experiments", {
            "content_id": content_id, "hypothesis": hypothesis.strip(), "variable_tested": variable_tested,
            "expected_signal": expected_signal, "result": None, "decision": None, "status": "RUNNING",
            "actor": actor, "created_at": now, "updated_at": now,
        })
        self.audit.append("MEDIA_EXPERIMENT_CREATED", {"experiment_id": experiment_id, "hypothesis": hypothesis, "actor": actor})
        return experiment_id

    def record_result(self, experiment_id: str, result: str, decision: str, actor: str) -> Dict[str, Any]:
        experiment = self._require(experiment_id)
        if experiment["status"] != "RUNNING":
            raise AnalyticsError(f"experiment is already {experiment['status']}")
        self.store.update("media_experiments", experiment_id, result=result, decision=decision, status="COMPLETED")
        self.audit.append("MEDIA_EXPERIMENT_COMPLETED", {"experiment_id": experiment_id, "decision": decision, "actor": actor})
        return self.get(experiment_id)

    def cancel(self, experiment_id: str, actor: str) -> Dict[str, Any]:
        experiment = self._require(experiment_id)
        if experiment["status"] != "RUNNING":
            raise AnalyticsError(f"experiment is already {experiment['status']}")
        self.store.update("media_experiments", experiment_id, status="CANCELLED")
        self.audit.append("MEDIA_EXPERIMENT_CANCELLED", {"experiment_id": experiment_id, "actor": actor})
        return self.get(experiment_id)

    def _require(self, experiment_id: str) -> Dict[str, Any]:
        experiment = self.store.get("media_experiments", experiment_id)
        if not experiment:
            raise AnalyticsError("experiment not found")
        return experiment

    def get(self, experiment_id: str) -> Optional[Dict[str, Any]]:
        return self.store.get("media_experiments", experiment_id)

    def list(self, content_id: Optional[str] = None, status: Optional[str] = None) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if content_id:
            clauses.append("content_id=?")
            params.append(content_id)
        if status:
            clauses.append("status=?")
            params.append(status)
        rows = self.store.list("media_experiments", " AND ".join(clauses), tuple(params)) if clauses else self.store.list("media_experiments")
        return list(reversed(rows))


class GrowthAgent:
    """Deterministic, transparent recommendations computed only from real
    (non-simulated) analytics rows -- same "never fabricate a result"
    posture as everything else in this codebase. No engagement-rate
    thresholds are dressed up as statistical confidence; they are exactly
    what they are, simple ratios, and every recommendation discloses the
    real numbers behind it."""

    def __init__(self, analytics: AnalyticsStore):
        self.analytics = analytics

    def recommend(self, publication_id: str) -> Dict[str, Any]:
        rows = self.analytics.list(publication_id, real_only=True)
        if not rows:
            return {"decision": "insufficient_data", "reason": "no real (non-simulated) analytics recorded for this publication yet"}
        by_kind: Dict[str, float] = {}
        for row in rows:
            by_kind[row["metric_kind"]] = by_kind.get(row["metric_kind"], 0.0) + row["value"]

        views = by_kind.get("views", 0.0)
        engagement = by_kind.get("engagement", 0.0)
        if views <= 0:
            return {"decision": "insufficient_data", "reason": "no real views recorded yet", "metrics": by_kind}

        engagement_rate = engagement / views
        if engagement_rate >= 0.10:
            decision, reasoning = "repeat", f"engagement rate {engagement_rate:.1%} is strong (>=10%)"
        elif engagement_rate >= 0.03:
            decision, reasoning = "test", f"engagement rate {engagement_rate:.1%} is moderate (3-10%) -- worth a variant test"
        else:
            decision, reasoning = "stop", f"engagement rate {engagement_rate:.1%} is weak (<3%)"
        return {"decision": decision, "reasoning": reasoning, "metrics": by_kind, "engagement_rate": engagement_rate}
