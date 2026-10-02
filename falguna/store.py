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
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute("PRAGMA journal_mode=WAL")
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
        # TTT Communications V2, Milestone 3 (real email ingestion): a
        # conversation opened/matched from a real inbound email can carry
        # the provider's own thread id, and a message can carry the
        # provider's own message id -- the two identifiers
        # falguna/email_ingestion.py needs for thread matching and
        # duplicate/retry detection. NULL for every conversation/message
        # created before this column existed, and for every non-email
        # channel -- identical to how source_ref_type/id were already
        # optional.
        # TTT Communications V2, Milestone 5 (support ticket workflow): a
        # finer-grained support/billing lifecycle layered ON TOP OF the
        # existing coarse `status` field, exactly the way `lifecycle_state`
        # was layered onto rh_opportunities.stage above rather than
        # replacing it. NULL for every conversation created before this
        # column existed, and for every non-support/billing conversation --
        # `status` alone continues to drive every existing view/test.
        "comm_conversations": [("external_thread_id", "TEXT"), ("ticket_status", "TEXT")],
        # Compatibility repair for databases created by the original
        # Communications schema, where this timestamp was named added_at.
        # Keep added_at intact for rolled-back code and add the current name
        # used by CommsStore/StateStore.list().
        "comm_participants": [("created_at", "TEXT")],
        # Phase 5 Final Client Experience, Section 8 (Customer Communication
        # Profile): a persisted, independently-settable preference overlay
        # on an existing contact -- NULL (unknown/not yet set) for every
        # contact created before this column existed, exactly the additive
        # convention every other column in this table already follows. See
        # falguna/comms.py's set_contact_preferences().
        "comm_contacts": [
            ("preferred_language", "TEXT"), ("preferred_channel", "TEXT"), ("tone", "TEXT"),
            ("detail_level", "TEXT"), ("technical_level", "TEXT"), ("update_cadence", "TEXT"),
            ("timezone", "TEXT"), ("call_preference", "TEXT"), ("communication_restrictions", "TEXT"),
            ("preferences_set_by", "TEXT"), ("preferences_updated_at", "TEXT"),
        ],
        "comm_messages": [
            ("provider_message_id", "TEXT"),
            # Phase 1, Requirement 3 (real, opt-in email send): real-send
            # bookkeeping absent from every comm_messages row created before
            # this phase existed.
            ("send_method", "TEXT"), ("provider_name", "TEXT"),
            ("send_attempts", "INTEGER"), ("failure_reason", "TEXT"),
        ],
        # Phase 6 Partner Network expansion: additive metadata over the
        # existing, proven partner/referral/commission ledgers.
        "pm_partners": [
            ("role_type", "TEXT"), ("maturity_tier", "TEXT"), ("kyc_status", "TEXT"),
            ("public_verification_enabled", "INTEGER"), ("related_party_disclosed", "INTEGER"),
            ("no_side_deal_accepted", "INTEGER"), ("no_unauthorized_subcontracting_accepted", "INTEGER"),
            # Phase 7: a partner's OWN affirmative acknowledgement of the
            # TTT anti-diversion/no-money-collection policy, distinct from
            # the staff-only no_side_deal_accepted/no_unauthorized_
            # subcontracting_accepted flags above (those record what STAFF
            # configured; these record the partner's own, dated act of
            # agreeing, via the external portal). NULL means "never
            # acknowledged", exactly as true for every partner that existed
            # before this column pair did.
            ("policy_acknowledged_at", "TEXT"), ("policy_acknowledged_by", "TEXT"),
        ],
        "pm_commissions": [("commission_plan_id", "TEXT"), ("excluded_amount", "REAL")],
        # TTT Communications V2, Milestone 11 (Digital Marketing Operations
        # Foundation): a real campaign owner -- NULL for every campaign
        # created before this column existed, exactly the same additive
        # convention as every other column in this table.
        "media_campaigns": [("owner", "TEXT")],
        # Phase 1, Requirement 2 (TTT / Falguna production readiness):
        # resume_source_url preserves the external (Tally-hosted) URL for a
        # career application's resume when only metadata was captured --
        # without it the earlier metadata-only note was unretrievable.
        # consent_status records an explicit consent answer when a form
        # asks for one; NULL for every event recorded before this column
        # existed and for any form that has no consent field to match.
        "site_applications": [("resume_source_url", "TEXT")],
        "tally_intake_events": [("consent_status", "TEXT")],
        # Phase 3, Milestone 4 (Company OS execution-routing repair): a plan
        # can now name the actual, structured Digital Workforce assignments
        # it needs (each a real, worker-supported task_type plus optional
        # inputs) instead of ExecutionOrchestrator.route_plan inventing a
        # single placeholder task_type ("company_os_routed") that no
        # registered worker has ever recognized -- see company_os.py's
        # ExecutionOrchestrator docstring for the full explanation. NULL/
        # absent for every plan created before this column existed, exactly
        # like every other additive column here; PlanStore.create() and
        # route_plan() both treat that the same as an explicit empty list.
        "co_plans": [("workforce_assignments_json", "TEXT")],
        # Phase 4 Sprint 3 (Executive Brief V1): an optional model-generated
        # narrative paragraph layered on top of the brief's already-computed,
        # deterministic confirmed_facts/estimates/recommendations -- never a
        # replacement for them. NULL for every brief generated before this
        # column existed, and for any brief generated while no model was
        # reachable (CEOBriefStore.generate() falls back to a deterministic
        # narrative in that case and still sets narrative_source="deterministic"
        # so the UI never confuses "no model" with "no brief").
        "cc_ceo_briefs": [("narrative", "TEXT"), ("narrative_source", "TEXT")],
        # Phase 5 Continuation, Section 4 (International Services V1): a
        # service created before this column set existed has every one of
        # these as NULL, meaning "not yet assessed for international
        # delivery" -- identical to how an unset risk_level/complexity/
        # delivery-window is treated for a service created afterwards.
        # Additive only; nothing here changes the meaning of any existing
        # cs_services column.
        "cs_services": [
            ("supported_languages_json", "TEXT"), ("risk_level", "TEXT"),
            ("regulated", "INTEGER"), ("regulated_notes", "TEXT"),
            ("baseline_complexity", "TEXT"), ("standard_delivery_days", "INTEGER"),
            ("standard_assumptions", "TEXT"), ("qa_requirements", "TEXT"),
            ("regional_pricing_json", "TEXT"),
        ],
        # Phase 7 web/desktop/mobile continuation: partner lead intake needs
        # two more self-reported fields the original pm_referrals schema
        # never had a slot for -- NULL for every referral registered before
        # this column existed, exactly the same additive convention as
        # every other column in this table.
        "pm_referrals": [("industry", "TEXT"), ("relationship_disclosure", "TEXT")],
        # Phase 7: a self-reported routing hint from the public intake
        # forms (Section 11) -- which of the known commercial-need
        # categories the submitter believes best matches their request.
        # This is a structured selector filled in by the person submitting
        # the form, not an automatic classification Claude/Falguna makes on
        # their behalf -- deliberately so, per the explicit instruction not
        # to claim a capability (automatic intent classification) that
        # hasn't been built and verified. NULL for every enquiry submitted
        # before this column existed.
        "site_enquiries": [("intake_category", "TEXT")],
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
        # Legacy comm_participants rows must remain sortable/readable after
        # created_at is added. This is an idempotent copy, not a rename or
        # destructive rebuild, and deliberately preserves added_at.
        participant_columns = {
            row[1] for row in self.db.execute("PRAGMA table_info(comm_participants)").fetchall()
        }
        if {"added_at", "created_at"}.issubset(participant_columns):
            self.db.execute(
                "UPDATE comm_participants SET created_at=added_at "
                "WHERE created_at IS NULL AND added_at IS NOT NULL"
            )

    @contextmanager
    def transaction(self):
        try:
            yield self.db
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    @contextmanager
    def transaction_immediate(self):
        """Like `transaction()`, but takes SQLite's write lock up front
        (`BEGIN IMMEDIATE`) instead of lazily on the connection's first DML
        statement. `compare_and_set` is enough when an invariant lives on a
        single row (one status column), but some Phase 6 invariants span
        MULTIPLE rows -- e.g. "the sum of every CONFIRMED refund against
        this payment, including the one about to be confirmed, must never
        exceed the settled amount." Two different refund rows have no
        shared primary key for compare_and_set to key off, so without this,
        two connections could each read the pre-race sum, each see
        themselves as within bounds, and each commit -- an over-refund that
        no single-row CAS would catch.

        `BEGIN IMMEDIATE` forces this connection to acquire SQLite's
        RESERVED write lock before the first SELECT inside the block runs
        (not just before the first write, as plain deferred transactions
        do), so a second connection's own `BEGIN IMMEDIATE` blocks (up to
        PRAGMA busy_timeout) until this one commits or rolls back. The
        read-sum -> decide -> write sequence inside the block is therefore
        serialized against every other writer using this same method, the
        same way a real row-level lock would serialize it in a
        production database.
        """
        self.db.execute("BEGIN IMMEDIATE")
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
            "media_assets", "media_publications", "media_analytics", "media_experiments", "media_lead_attributions",
            "cc_ceo_briefs", "cc_goals", "cc_goal_progress_events", "cc_ledger_entries", "cc_budgets", "cc_risks",
            "co_recommendations",
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
            "comm_risk_events",
            # Twenty Two Technologies -- Live Enquiry Activation V1 (falguna/tally_intake.py)
            "tally_intake_events",
            # Sales Partner Pilot V1 (falguna/partner_management.py)
            "pm_partners", "pm_partner_status_events", "pm_referrals", "pm_duplicate_reviews",
            "pm_commissions", "pm_commission_events",
            # Phase 5 Sprint 1: Commercial Operating Foundation (falguna/commercial.py)
            "cs_services", "cs_foundations", "cs_intakes", "cs_intake_events",
            "cs_projects", "cs_project_events", "cs_disputes", "cs_dispute_events",
            "cs_project_costs",
            # Phase 5 Continuation, Section 14: Capability Registry V1
            "cs_capabilities",
            # Phase 5 Continuation, Section 22: Learning from Outcomes V1
            "cs_outcome_records",
            # Phase 5 Final Client Experience, Section 3/4/6/7: Customer
            # Language Understanding V1 (falguna/language.py)
            "comm_message_interpretations",
            # Phase 5 Final Client Experience, Section 20: payment
            # communication bridge (falguna/payment_comms.py)
            "comm_payment_drafts",
            # Phase 6 Commercial Platform foundation. FALGUNA may read and
            # recommend against this TTT-owned state, but never execute it.
            "p6_commercial_identities", "p6_payment_intents", "p6_payment_events",
            "p6_webhook_events", "p6_approval_requests", "p6_financial_events",
            "p6_reserve_policies", "p6_reconciliations", "p6_receipts", "p6_approval_events",
            "p6_commission_plans", "p6_partner_contributions", "p6_risk_events", "p6_risk_event_actions"}
        allowed.update({"p6_commission_releases", "p6_refunds", "p6_subscriptions", "p6_payables",
                        "p6_subscription_cycles", "p6_subscription_attempts", "p6_refund_reconciliations",
                        "p6_opportunity_economics", "p7_external_accounts", "p7_external_sessions"})
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

    def compare_and_set(self, table: str, record_id: str, expected: Dict[str, Any], **values: Any) -> bool:
        """Atomic conditional update: WHERE id=? AND <every expected column
        still matches>. Phase 6 race-matrix fix (2026-10-02): a plain
        read-then-update (`get()` followed by `update()`) is two separate
        statements with no lock held between them, so two genuinely separate
        database connections can both read the same prior state, both pass
        an application-level "is this still pending?" check, and both then
        write -- the second write silently overwrites the first's decision
        (a classic lost update). That is exactly how a reject() could
        silently un-happen because an approve() on a stale read committed
        after it, or how a material beneficiary/amount change could be
        approved against the pre-change snapshot.

        This method folds the check into the write itself: the UPDATE's own
        WHERE clause re-verifies `expected` at the instant SQLite takes the
        write lock, not at some earlier SELECT. SQLite's single-writer
        serialization (WAL mode, PRAGMA busy_timeout above) means that when
        two connections race here, one update's WHERE clause is evaluated
        against the row as the OTHER one already committed it, not a stale
        in-memory copy -- so at most one caller ever sees rowcount == 1.
        The loser gets `False` back (no exception, no partial write) and
        must re-read current state and decide whether to retry or fail
        closed; callers in this module choose to fail closed, since a lost
        race on an approval/financial transition means the precondition the
        caller believed held is no longer true.
        """
        values["updated_at"] = utcnow()
        assignments = ",".join(f"{key}=?" for key in values)
        conditions = " AND ".join(f"{key}=?" for key in expected)
        with self.transaction() as db:
            cursor = db.execute(
                f"UPDATE {table} SET {assignments} WHERE id=? AND {conditions}",
                [*values.values(), record_id, *expected.values()],
            )
            return cursor.rowcount == 1

    def list(self, table: str, where: str = "1=1", params: Iterable[Any] = ()):
        return [dict(row) for row in self.db.execute(f"SELECT * FROM {table} WHERE {where} ORDER BY created_at", tuple(params))]

    def checkpoint(self, run_id: str, stage: str, payload: Dict[str, Any]) -> str:
        return self.create("checkpoints", {"run_id": run_id, "stage": stage, "payload": json.dumps(payload, sort_keys=True), "created_at": utcnow()})

    def latest_checkpoint(self, run_id: str):
        row = self.db.execute("SELECT * FROM checkpoints WHERE run_id=? ORDER BY created_at DESC LIMIT 1", (run_id,)).fetchone()
        return dict(row) if row else None

    def close(self):
        self.db.close()
