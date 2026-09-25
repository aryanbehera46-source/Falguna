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
            # Venture Studio v1 (Section 25): an opportunity optionally
            # belongs to one venture -- NULL means company-wide, exactly as
            # before this column existed.
            ("venture_id", "TEXT"),
            # Revenue Operations V2, Milestone 4: an obsolete/duplicated
            # trial or test record (e.g. a repeated "(simulated)" dry-run
            # prospect created while exercising the Sales->Proposal path)
            # needs to stop inflating the real pipeline's counts without
            # ever being destroyed -- the same `mc_archived`-style additive
            # flag `runs`/`browser_sessions` already use for exactly this
            # reason (see store.py's own _ADDITIVE_COLUMNS comment above).
            # 0/NULL means visible in the normal pipeline, exactly as
            # before this column existed; OpportunityStore.list() filters
            # archived=1 out by default but never deletes the row.
            ("archived", "INTEGER"),
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
        # TTT Venture Studio / Multi-Venture OS v1 (Section 27: data
        # isolation) -- every table below gets one explicit, nullable
        # venture_id column so a venture-scoped row is always structurally
        # linked to its venture, never inferred by name matching. NULL
        # means "not venture-scoped" (company-wide), exactly as it did
        # before this column existed -- fully backward compatible.
        "cc_ledger_entries": [("venture_id", "TEXT")],
        "cc_goals": [("venture_id", "TEXT")],
        "cc_risks": [("venture_id", "TEXT")],
        # Company OS traceability (Section 24): a wf_task/mission can
        # optionally be tagged with the company objective it exists to
        # serve, on top of its existing venture_id -- NULL means "not yet
        # traced to a company objective", fully backward compatible.
        "wf_tasks": [("venture_id", "TEXT"), ("co_objective_id", "TEXT")],
        "missions": [("venture_id", "TEXT"), ("co_objective_id", "TEXT")],
        "media_brands": [("venture_id", "TEXT")],
        # TTT Group OS / Company Orchestrator v2 (Section 18: Boardroom v2) --
        # every Boardroom topic can now carry a structured link to the
        # company-objective/venture/risk it concerns, and a persisted
        # discussion summary/follow-up, without altering the existing
        # boardroom_topics/boardroom_decisions read/write paths at all. NULL
        # (not linked) is fully backward compatible with every topic created
        # before this column existed.
        "boardroom_topics": [
            ("linked_objective_id", "TEXT"), ("linked_venture_id", "TEXT"), ("linked_risk_id", "TEXT"),
            ("discussion_summary", "TEXT"), ("follow_up", "TEXT"),
        ],
        # Falguna Product Experience V2: chat_messages gets a truthful,
        # explicit generation-state column (Section 4) and soft-delete/edit
        # markers for regenerate/edit-resubmit (Section 3), all additive and
        # NULL-safe against every message ever created before this column
        # existed -- application code treats NULL status as "COMPLETED" and
        # NULL/0 superseded as "still visible", exactly the old behavior.
        "chat_messages": [
            ("status", "TEXT"), ("superseded", "INTEGER"), ("edited_at", "TEXT"),
            # StateStore.update() always stamps updated_at -- chat_messages
            # never needed one before (only ever created, never updated),
            # but the V2 message-state transitions (mark_generating,
            # complete_message, fail_message, cancel_message,
            # edit_user_message) are the first callers to update() an
            # existing row, so the column has to exist.
            ("updated_at", "TEXT"),
            # Pre-existing latent gap: ChatResponder.reply() always computed a
            # suggested_objective (mirroring research_queries) but there was
            # nowhere on chat_messages to persist it, so the Chat->Work
            # handoff panel's suggestion was silently always empty. Additive,
            # nullable column so this can actually be stored and surfaced.
            ("suggested_objective", "TEXT"),
            # Falguna V2.1 (sanitized error UX): `error` stays the safe,
            # user-facing sentence a FalgunaModelError/ChatError already
            # composed -- never raw provider stdout. `error_category` is one
            # of falguna.providers.ErrorCategory, driving which recovery
            # actions the UI offers (Retry / Change model / Use local model /
            # Open Settings). `error_detail` is the raw technical text (if
            # any) for an expandable "technical details" panel only -- never
            # rendered by default, never included in `error`.
            ("error_category", "TEXT"), ("error_detail", "TEXT"),
            # Falguna Memory & Knowledge V2 (Pass F): a completed assistant
            # message can record which memory/knowledge items materially
            # informed it, so the UI can show "Sources" -- NULL for every
            # message created before this column existed (and for every
            # message where retrieval found nothing relevant), meaning
            # "no memory was used", identical to today's behavior.
            ("memory_context_json", "TEXT"),
        ],
        # Falguna V2.1 (Mission Control count cleanup): a run can be
        # explicitly archived off the active board -- this is a
        # board-visibility flag only, never a merge/reject/approve decision
        # and never a delete; the run row, its checkpoints, approvals, and
        # audit trail are completely unchanged. NULL/0 (every run created
        # before this column existed) means "not archived", i.e. counted in
        # the board's active buckets exactly as before.
        "runs": [("mc_archived", "INTEGER")],
        # Per-conversation/per-research model and work-mode overrides
        # (Sections 5-6). NULL means "use the global default", identical to
        # every conversation/research row created before these existed.
        "conversations": [("model_override", "TEXT"), ("work_mode", "TEXT")],
        "research_queries": [("model_override", "TEXT"), ("work_mode", "TEXT"), ("model_call_json", "TEXT")],
        # Falguna Browser + Computer Use V1: a screenshot or download an
        # AttachmentStore.save_base64 call records can optionally be linked
        # back to the browser session that produced it (Section 26). NULL
        # for every attachment created before this column existed, and for
        # every ordinary Chat upload -- identical to how conversation_id was
        # already optional.
        "attachments": [("browser_session_id", "TEXT")],
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
            "tl_trades", "tl_performance_snapshots", "tl_reviews", "tl_council_decisions", "tl_graveyard",
            "vs_ventures", "vs_venture_status_events", "vs_experiments", "vs_validation_signals",
            "vs_capital_allocations", "vs_resource_requests", "vs_recommendations", "vs_graveyard",
            "vs_assets", "vs_relationships",
            # TTT Group OS / Company Orchestrator v2 (falguna/company_os.py)
            "co_objectives", "co_objective_status_events", "co_plans", "co_priority_evaluations",
            "co_department_objectives", "co_venture_links", "co_resource_recommendations",
            "co_capital_recommendations", "co_events", "co_replans", "co_decisions", "co_policies",
            "co_escalations", "co_timeline_events", "co_traceability_links", "co_memory", "co_failures",
            "co_cost_estimates", "co_goal_feedback_events", "co_daily_loops", "co_weekly_reviews",
            # Falguna Product Experience V2
            "attachments", "notifications",
            # Falguna V2.1: UX Hardening + Provider Independence Foundation
            "model_settings",
            # Falguna Browser + Computer Use V1
            "browser_sessions", "browser_tabs", "browser_actions", "browser_downloads",
            # Falguna Memory & Knowledge V2 (falguna/memory.py). memory_fts and
            # knowledge_fts are standalone FTS5 virtual tables managed directly
            # by MemoryStore/KnowledgeStore (raw SQL, not this generic helper --
            # a virtual table has no "id" column for create() to populate).
            "memory_records", "knowledge_documents", "knowledge_chunks", "memory_suggestions",
            # Twenty Two Technologies flagship public website (falguna/site_web.py)
            "site_services", "site_case_studies", "site_products", "site_posts", "site_jobs",
            "site_applications", "site_enquiries", "site_staff_users", "site_staff_sessions",
            # TTT Communications + AI Customer Service V1 (falguna/comms.py)
            "comm_organizations", "comm_contacts", "comm_conversations", "comm_messages",
            "comm_participants", "comm_status_events",
            # TTT Communications V2 (falguna/risk_engine.py)
            "comm_risk_events"}
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
