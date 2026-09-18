import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class StateStore:
    """Durable local adapter. PostgreSQL can replace this without changing callers."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path))
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")

    # Additive-only column migrations for tables that already existed in a
    # real, already-migrated production database before these columns were
    # introduced. `CREATE TABLE IF NOT EXISTS` in schema_sqlite.sql is a
    # no-op against a table that already exists, so any new column added to
    # a table's definition there would silently never reach an existing
    # database -- this is the (checked, idempotent) alternative: it inspects
    # the real column list first and only ever adds a column that is
    # genuinely missing, never renames or drops anything.
    _ADDITIVE_COLUMNS = {
        "rh_qualifications": [
            ("capability_gaps", "TEXT"),
            ("estimated_project_value", "TEXT"),
            ("why", "TEXT"),
            # Relevance hardening pass: a hard gate independent of fit_score
            # (see QualificationEngine._service_relevance) -- persisted so
            # the UI and audit trail can show exactly why an opportunity was
            # excluded, not just that it was.
            ("relevance_passed", "INTEGER"),
            ("relevance_exclusion_signals", "TEXT"),
            ("relevance_positive_signals", "TEXT"),
            # Qualification-calibration pass: recommendation_detail
            # distinguishes PURSUE_WITH_BUDGET_UNKNOWN from a plain PURSUE
            # (see QualificationEngine._recommendation) without changing the
            # coarse `recommendation` value anything else branches on; the
            # other three are explainability fields (item 4) surfaced
            # alongside `why` rather than folded only into that one string.
            ("recommendation_detail", "TEXT"),
            ("budget_source", "TEXT"),
            ("strongest_technical_match", "TEXT"),
            ("biggest_risk", "TEXT"),
        ],
        "rh_discovery_runs": [
            # Discovery accounting: "found" minus "new" minus "duplicate" used
            # to have no explanation -- these two make every number in a run
            # report add up (found = new + duplicate + filtered + invalid).
            ("opportunities_filtered", "INTEGER"),
            ("opportunities_invalid", "INTEGER"),
        ],
        "rh_opportunities": [
            # TTT Autonomous Revenue-to-Delivery Loop v1: the Company Workflow
            # Orchestrator's own, finer-grained lifecycle state (DISCOVERED
            # through RETAIN/LOST -- see falguna/lifecycle.py). Deliberately a
            # separate field from the existing `stage` column, never a
            # replacement: `stage` and rh_stage_history stay the single
            # source of truth for the coarse pipeline view every existing
            # route/test/UI already depends on: the orchestrator calls the
            # existing move_stage/mark_won/mark_lost at the right boundary
            # points instead of duplicating that logic, and layers the finer
            # post-Won states (ONBOARDING, DELIVERY, CLIENT_REVIEW, ...) on
            # top, which `stage` has no equivalent for at all today.
            ("lifecycle_state", "TEXT"),
        ],
        "needs_aryan_items": [
            # A generic structured-payload slot (Passes B-E): a "closing
            # package" awaiting final commercial approval, a drafted
            # outreach message, or any other Needs Aryan item that carries
            # more than a short rationale string needs somewhere to persist
            # the real data it's asking a decision about, so the decision
            # (once approved) can be finalized from the item itself rather
            # than requiring the caller to remember or re-supply it.
            ("payload_json", "TEXT"),
        ],
    }

    def migrate(self) -> None:
        schema = Path(__file__).with_name("schema_sqlite.sql").read_text()
        self.db.executescript(schema)
        self._apply_additive_column_migrations()
        self.db.commit()

    def _apply_additive_column_migrations(self) -> None:
        for table, columns in self._ADDITIVE_COLUMNS.items():
            existing = {row[1] for row in self.db.execute(f"PRAGMA table_info({table})").fetchall()}
            for name, coltype in columns:
                if name not in existing:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {coltype}")

    @contextmanager
    def transaction(self):
        try:
            yield self.db
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def create(self, table: str, values: Dict[str, Any], record_id: Optional[str] = None) -> str:
        allowed = {"missions", "requirements", "tasks", "task_steps", "runs", "checkpoints", "approvals", "model_calls", "cost_events", "artifacts", "run_controls", "project_cache", "mission_timings", "supervisor_states", "boardroom_topics", "boardroom_contributions", "boardroom_decisions", "backlog_items", "backlog_history", "needs_aryan_items", "conversations", "chat_messages", "conversation_handoffs", "research_queries", "research_sources", "research_citations", "research_handoffs", "rh_opportunities", "rh_stage_history", "rh_qualifications", "rh_proposals", "rh_followups", "rh_active_jobs", "rh_discovery_runs", "rh_discovered_sources", "rh_opportunity_research", "rh_settings", "rh_lifecycle_events", "rh_application_attempts", "clients", "rh_closing_records", "rh_negotiation_terms", "rh_conversation_messages", "rh_onboarding_items", "rh_invoices", "rh_completion_records", "rh_retention_items", "rh_outbound_leads", "rh_outreach_drafts",
            "wf_tasks", "wf_task_events", "wf_recurring_workflows", "wf_recurring_runs", "wf_documents", "wf_email_messages",
            "media_brands", "media_campaigns", "media_content_items", "media_content_events", "media_scripts",
            "media_assets", "media_publications", "media_analytics", "media_experiments",
            "cc_ceo_briefs", "cc_goals", "cc_goal_progress_events", "cc_ledger_entries", "cc_budgets", "cc_risks",
            "tl_markets", "tl_instruments", "tl_data_sources", "tl_datasets", "tl_ohlcv_bars", "tl_data_quality_reports",
            "tl_strategies", "tl_strategy_versions", "tl_strategy_status_events", "tl_backtests", "tl_stress_tests",
            "tl_risk_limits", "tl_risk_breach_events", "tl_paper_accounts", "tl_paper_orders", "tl_paper_positions",
            "tl_trades", "tl_performance_snapshots", "tl_reviews", "tl_council_decisions", "tl_graveyard"}
        if table not in allowed:
            raise ValueError("unknown table")
        record_id = record_id or str(uuid.uuid4())
        data = {"id": record_id, **values}
        columns = ",".join(data)
        marks = ",".join("?" for _ in data)
        with self.transaction() as db:
            db.execute(f"INSERT INTO {table} ({columns}) VALUES ({marks})", list(data.values()))
        return record_id

    def get(self, table: str, record_id: str) -> Optional[Dict[str, Any]]:
        row = self.db.execute(f"SELECT * FROM {table} WHERE id=?", (record_id,)).fetchone()
        return dict(row) if row else None

    def update(self, table: str, record_id: str, **values: Any) -> None:
        values["updated_at"] = utcnow()
        assignments = ",".join(f"{key}=?" for key in values)
        with self.transaction() as db:
            db.execute(f"UPDATE {table} SET {assignments} WHERE id=?", [*values.values(), record_id])

    def list(self, table: str, where: str = "1=1", params: Iterable[Any] = ()):
        return [dict(row) for row in self.db.execute(f"SELECT * FROM {table} WHERE {where} ORDER BY created_at", tuple(params))]

    def checkpoint(self, run_id: str, stage: str, payload: Dict[str, Any]) -> str:
        return self.create("checkpoints", {"run_id": run_id, "stage": stage, "payload": json.dumps(payload, sort_keys=True), "created_at": utcnow()})

    def latest_checkpoint(self, run_id: str):
        row = self.db.execute("SELECT * FROM checkpoints WHERE run_id=? ORDER BY created_at DESC LIMIT 1", (run_id,)).fetchone()
        return dict(row) if row else None

    def close(self):
        self.db.close()
