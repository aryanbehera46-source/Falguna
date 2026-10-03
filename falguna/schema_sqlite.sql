CREATE TABLE IF NOT EXISTS missions (id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);

-- Phase 9: FALGUNA Frontier & Independence. These tables are an additive,
-- provider-neutral control plane over the existing mission, research,
-- browser/computer, memory and TTT HQ systems. They do not duplicate TTT's
-- authoritative commercial records and contain no live-action executor.
CREATE TABLE IF NOT EXISTS p9_objectives (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, title TEXT NOT NULL,
    description TEXT NOT NULL, mode TEXT NOT NULL, status TEXT NOT NULL,
    autonomy_level TEXT NOT NULL, schedule_json TEXT NOT NULL,
    stop_conditions_json TEXT NOT NULL, limits_json TEXT NOT NULL,
    source_type TEXT, source_id TEXT, owner TEXT NOT NULL,
    last_checkpoint_at TEXT, heartbeat_at TEXT, next_action TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS p9_graph_nodes (
    id TEXT PRIMARY KEY, objective_id TEXT NOT NULL REFERENCES p9_objectives(id),
    organization_id TEXT NOT NULL, node_type TEXT NOT NULL, title TEXT NOT NULL,
    status TEXT NOT NULL, dependencies_json TEXT NOT NULL,
    assigned_agent_id TEXT, tool_name TEXT, approval_class TEXT NOT NULL,
    input_json TEXT NOT NULL, output_json TEXT, attempt INTEGER NOT NULL DEFAULT 0,
    lease_owner TEXT, lease_expires_at TEXT, error TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS p9_agents (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, role TEXT NOT NULL,
    display_name TEXT NOT NULL, capabilities_json TEXT NOT NULL,
    permissions_json TEXT NOT NULL, status TEXT NOT NULL,
    current_node_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS p9_evidence (
    id TEXT PRIMARY KEY, objective_id TEXT NOT NULL REFERENCES p9_objectives(id),
    node_id TEXT REFERENCES p9_graph_nodes(id), organization_id TEXT NOT NULL,
    kind TEXT NOT NULL, uri TEXT, sha256 TEXT, summary TEXT NOT NULL,
    provenance_json TEXT NOT NULL, confidence TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS p9_approvals (
    id TEXT PRIMARY KEY, objective_id TEXT NOT NULL REFERENCES p9_objectives(id),
    node_id TEXT NOT NULL REFERENCES p9_graph_nodes(id), organization_id TEXT NOT NULL,
    action_class TEXT NOT NULL, status TEXT NOT NULL, request_reason TEXT NOT NULL,
    decided_by TEXT, decision_reason TEXT, decided_at TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS p9_events (
    id TEXT PRIMARY KEY, objective_id TEXT NOT NULL REFERENCES p9_objectives(id),
    node_id TEXT, organization_id TEXT NOT NULL, event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS p9_model_assets (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, provider_kind TEXT NOT NULL,
    model_id TEXT NOT NULL, version TEXT NOT NULL, ownership TEXT NOT NULL,
    capabilities_json TEXT NOT NULL, constraints_json TEXT NOT NULL,
    dataset_provenance_json TEXT NOT NULL, status TEXT NOT NULL,
    evaluation_summary_json TEXT NOT NULL, approved_by TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS p9_benchmark_runs (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, program TEXT NOT NULL,
    subject_type TEXT NOT NULL, subject_id TEXT NOT NULL, suite_version TEXT NOT NULL,
    dimensions_json TEXT NOT NULL, evidence_json TEXT NOT NULL,
    passed INTEGER NOT NULL, external_audit_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS p9_plugins (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, name TEXT NOT NULL,
    version TEXT NOT NULL, manifest_json TEXT NOT NULL, scopes_json TEXT NOT NULL,
    approval_policy TEXT NOT NULL, status TEXT NOT NULL, sandbox_json TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS p9_studio_artifacts (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, objective_id TEXT,
    node_id TEXT, artifact_type TEXT NOT NULL, title TEXT NOT NULL,
    uri TEXT NOT NULL, version INTEGER NOT NULL, parent_version_id TEXT,
    sha256 TEXT NOT NULL, metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS p9_continuity_bundles (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, objective_id TEXT NOT NULL,
    version INTEGER NOT NULL, state_json TEXT NOT NULL, sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS p9_twin_nodes (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, node_kind TEXT NOT NULL,
    label TEXT NOT NULL, state_json TEXT NOT NULL, provenance_json TEXT NOT NULL,
    confidence TEXT NOT NULL, authoritative_system TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS p9_simulations (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, title TEXT NOT NULL,
    assumptions_json TEXT NOT NULL, scenarios_json TEXT NOT NULL,
    outcomes_json TEXT NOT NULL, uncertainty_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL, status TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_p9_objectives_org_status ON p9_objectives(organization_id, status, created_at);
CREATE INDEX IF NOT EXISTS idx_p9_nodes_objective_status ON p9_graph_nodes(objective_id, status, created_at);
CREATE INDEX IF NOT EXISTS idx_p9_events_objective ON p9_events(objective_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_p9_model_version ON p9_model_assets(organization_id, model_id, version);
CREATE UNIQUE INDEX IF NOT EXISTS idx_p9_plugin_version ON p9_plugins(organization_id, name, version);
CREATE TABLE IF NOT EXISTS requirements (id TEXT PRIMARY KEY, mission_id TEXT NOT NULL REFERENCES missions(id), body TEXT NOT NULL, acceptance_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, requirement_id TEXT NOT NULL REFERENCES requirements(id), title TEXT NOT NULL, status TEXT NOT NULL, repository TEXT NOT NULL, base_ref TEXT NOT NULL, policy_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS task_steps (id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), ordinal INTEGER NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL, detail_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), status TEXT NOT NULL, attempt INTEGER NOT NULL, worker TEXT NOT NULL, model TEXT NOT NULL, worktree TEXT, head_sha TEXT, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS checkpoints (id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), stage TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS approvals (id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), kind TEXT NOT NULL, status TEXT NOT NULL, requested_at TEXT NOT NULL, decided_at TEXT, decided_by TEXT, reason TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS model_calls (id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), provider TEXT NOT NULL, model TEXT NOT NULL, purpose TEXT NOT NULL, input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL, cost_usd REAL NOT NULL, metadata_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cost_events (id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), category TEXT NOT NULL, amount_usd REAL NOT NULL, metadata_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS artifacts (id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), kind TEXT NOT NULL, path TEXT NOT NULL, sha256 TEXT NOT NULL, metadata_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS run_controls (id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), action TEXT NOT NULL, status TEXT NOT NULL, detail_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS project_cache (id TEXT PRIMARY KEY, repository TEXT NOT NULL, profile_id TEXT NOT NULL, fingerprint TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS mission_timings (id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), stage TEXT NOT NULL, duration_ms INTEGER NOT NULL, metadata_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS supervisor_states (id TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE REFERENCES runs(id), outcome_class TEXT NOT NULL, category TEXT NOT NULL, phase TEXT NOT NULL, retry_allowed INTEGER NOT NULL, resume_allowed INTEGER NOT NULL, eligibility_reason TEXT NOT NULL, attempts_used INTEGER NOT NULL, retry_budget INTEGER NOT NULL, diagnostics_json TEXT NOT NULL, decision_needed TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_runs_task ON runs(task_id);
CREATE INDEX IF NOT EXISTS idx_checkpoints_run ON checkpoints(run_id, created_at);
CREATE INDEX IF NOT EXISTS idx_controls_run ON run_controls(run_id, created_at);
CREATE INDEX IF NOT EXISTS idx_cache_project ON project_cache(repository, profile_id, created_at);
CREATE INDEX IF NOT EXISTS idx_timings_run ON mission_timings(run_id, created_at);
CREATE INDEX IF NOT EXISTS idx_supervisor_run ON supervisor_states(run_id, created_at);

-- Falguna Chat: persistent conversations that can hand off into Work missions
CREATE TABLE IF NOT EXISTS conversations (id TEXT PRIMARY KEY, title TEXT NOT NULL, project_id TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS chat_messages (id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id), role TEXT NOT NULL, content TEXT NOT NULL, model_call_json TEXT, error TEXT, created_at TEXT NOT NULL);
-- Additive columns for chat_messages (status/superseded/edited_at/updated_at/
-- suggested_objective/error_category/error_detail) are applied for every
-- database -- new or pre-existing -- via StateStore._ADDITIVE_COLUMNS, since
-- CREATE TABLE IF NOT EXISTS above is a no-op once this table already
-- exists; see store.py.
CREATE TABLE IF NOT EXISTS conversation_handoffs (id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id), run_id TEXT NOT NULL REFERENCES runs(id), objective TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_conversations_updated ON conversations(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_chat_messages_conversation ON chat_messages(conversation_id, created_at);
CREATE INDEX IF NOT EXISTS idx_handoffs_conversation ON conversation_handoffs(conversation_id, created_at);
CREATE INDEX IF NOT EXISTS idx_handoffs_run ON conversation_handoffs(run_id);
CREATE TABLE IF NOT EXISTS research_queries (id TEXT PRIMARY KEY, query TEXT NOT NULL, answer TEXT, suggested_objective TEXT, provider TEXT NOT NULL, project_id TEXT, conversation_id TEXT, status TEXT NOT NULL, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS research_sources (id TEXT PRIMARY KEY, research_id TEXT NOT NULL REFERENCES research_queries(id), rank INTEGER NOT NULL, title TEXT, url TEXT NOT NULL, domain TEXT, published_at TEXT, retrieved_at TEXT NOT NULL, snippet TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS research_citations (id TEXT PRIMARY KEY, research_id TEXT NOT NULL REFERENCES research_queries(id), source_id TEXT NOT NULL REFERENCES research_sources(id), claim TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS research_handoffs (id TEXT PRIMARY KEY, research_id TEXT NOT NULL REFERENCES research_queries(id), run_id TEXT NOT NULL REFERENCES runs(id), objective TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_research_updated ON research_queries(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_research_sources_research ON research_sources(research_id, rank);
CREATE INDEX IF NOT EXISTS idx_research_citations_research ON research_citations(research_id);
CREATE INDEX IF NOT EXISTS idx_research_handoffs_research ON research_handoffs(research_id, created_at);
CREATE INDEX IF NOT EXISTS idx_research_handoffs_run ON research_handoffs(run_id);

-- Falguna Product Experience V2: real, additive-only support for Files,
-- Notifications, and per-conversation model/work-mode selection. Nothing
-- here replaces an existing table or write path -- attachments are stored
-- once on disk under .falguna/attachments/ and referenced by id; a
-- notification is only ever created from a real state transition Falguna
-- already produced (a run reaching a terminal status, a research query
-- finishing), never a fabricated event.
CREATE TABLE IF NOT EXISTS attachments (id TEXT PRIMARY KEY, conversation_id TEXT, message_id TEXT, filename TEXT NOT NULL, content_type TEXT, size_bytes INTEGER NOT NULL, sha256 TEXT NOT NULL, storage_rel_path TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT);
CREATE INDEX IF NOT EXISTS idx_attachments_conversation ON attachments(conversation_id, created_at);
CREATE INDEX IF NOT EXISTS idx_attachments_message ON attachments(message_id);
CREATE TABLE IF NOT EXISTS notifications (id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL, body TEXT, ref_type TEXT, ref_id TEXT, read INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT);
CREATE INDEX IF NOT EXISTS idx_notifications_created ON notifications(created_at);
CREATE INDEX IF NOT EXISTS idx_notifications_ref ON notifications(ref_type, ref_id, kind);

-- TTT HQ: Boardroom, Master Vision Backlog, Needs Aryan (PASS 1 consolidation)
CREATE TABLE IF NOT EXISTS boardroom_topics (id TEXT PRIMARY KEY, title TEXT NOT NULL, summary TEXT NOT NULL, proposed_category TEXT, proposed_phase TEXT, proposed_priority TEXT, proposed_revenue_impact TEXT, status TEXT NOT NULL, created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS boardroom_contributions (id TEXT PRIMARY KEY, topic_id TEXT NOT NULL REFERENCES boardroom_topics(id), perspective TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS boardroom_decisions (id TEXT PRIMARY KEY, topic_id TEXT NOT NULL REFERENCES boardroom_topics(id), action TEXT NOT NULL, note TEXT, decided_by TEXT NOT NULL, backlog_item_id TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS backlog_items (id TEXT PRIMARY KEY, title TEXT NOT NULL, category TEXT, phase TEXT, priority TEXT, dependency TEXT, revenue_impact TEXT, status TEXT NOT NULL, source_boardroom_topic_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS backlog_history (id TEXT PRIMARY KEY, item_id TEXT NOT NULL REFERENCES backlog_items(id), field TEXT NOT NULL, old_value TEXT, new_value TEXT, actor TEXT NOT NULL, reason TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS needs_aryan_items (id TEXT PRIMARY KEY, kind TEXT NOT NULL, ref_type TEXT, ref_id TEXT, title TEXT NOT NULL, what_is_needed TEXT NOT NULL, recommendation TEXT, rationale TEXT, risk TEXT, expected_value TEXT, status TEXT NOT NULL, decision_note TEXT, decided_by TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, decided_at TEXT);
CREATE INDEX IF NOT EXISTS idx_boardroom_contrib_topic ON boardroom_contributions(topic_id, created_at);
CREATE INDEX IF NOT EXISTS idx_boardroom_decision_topic ON boardroom_decisions(topic_id, created_at);
CREATE INDEX IF NOT EXISTS idx_backlog_status ON backlog_items(status, created_at);
CREATE INDEX IF NOT EXISTS idx_backlog_history_item ON backlog_history(item_id, created_at);
CREATE INDEX IF NOT EXISTS idx_needs_aryan_status ON needs_aryan_items(status, created_at);

-- Revenue Hunter: client acquisition, served by TTT HQ (not Falguna Engineering)
CREATE TABLE IF NOT EXISTS rh_opportunities (id TEXT PRIMARY KEY, source TEXT NOT NULL, source_url TEXT, client_name TEXT, title TEXT NOT NULL, description TEXT, budget_rate TEXT, required_skills TEXT, deadline TEXT, contract_type TEXT, location_timezone TEXT, urgency TEXT, stage TEXT NOT NULL, final_price REAL, lost_reason TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rh_stage_history (id TEXT PRIMARY KEY, opportunity_id TEXT NOT NULL REFERENCES rh_opportunities(id), from_stage TEXT, to_stage TEXT NOT NULL, actor TEXT NOT NULL, note TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rh_qualifications (id TEXT PRIMARY KEY, opportunity_id TEXT NOT NULL REFERENCES rh_opportunities(id), fit_score INTEGER NOT NULL, budget_quality TEXT NOT NULL, effort_vs_return TEXT NOT NULL, portfolio_match TEXT, portfolio_match_reason TEXT, recurring_potential TEXT NOT NULL, urgency TEXT NOT NULL, risk_flags TEXT, recommendation TEXT NOT NULL, suggested_price TEXT, suggested_timeline TEXT, suggested_portfolio_proof TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rh_proposals (id TEXT PRIMARY KEY, opportunity_id TEXT NOT NULL REFERENCES rh_opportunities(id), kind TEXT NOT NULL, content TEXT NOT NULL, status TEXT NOT NULL, approved_by TEXT, approved_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rh_followups (id TEXT PRIMARY KEY, opportunity_id TEXT NOT NULL REFERENCES rh_opportunities(id), kind TEXT NOT NULL, draft_content TEXT NOT NULL, status TEXT NOT NULL, due_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rh_active_jobs (id TEXT PRIMARY KEY, opportunity_id TEXT NOT NULL REFERENCES rh_opportunities(id), job_payload_json TEXT NOT NULL, handoff_status TEXT NOT NULL, mission_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_rh_opportunities_stage ON rh_opportunities(stage, updated_at);
CREATE INDEX IF NOT EXISTS idx_rh_stage_history_opportunity ON rh_stage_history(opportunity_id, created_at);
CREATE INDEX IF NOT EXISTS idx_rh_qualifications_opportunity ON rh_qualifications(opportunity_id, created_at);
CREATE INDEX IF NOT EXISTS idx_rh_proposals_opportunity ON rh_proposals(opportunity_id, created_at);
CREATE INDEX IF NOT EXISTS idx_rh_followups_status ON rh_followups(status, created_at);
CREATE INDEX IF NOT EXISTS idx_rh_active_jobs_opportunity ON rh_active_jobs(opportunity_id, created_at);

-- Opportunity Agent v1: automatic discovery, dedup, config, research linkage
CREATE TABLE IF NOT EXISTS rh_discovery_runs (id TEXT PRIMARY KEY, started_at TEXT NOT NULL, completed_at TEXT, actor TEXT NOT NULL, profile_snapshot_json TEXT NOT NULL, providers_json TEXT NOT NULL, opportunities_found INTEGER NOT NULL, opportunities_new INTEGER NOT NULL, opportunities_duplicate INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rh_discovered_sources (id TEXT PRIMARY KEY, opportunity_id TEXT NOT NULL REFERENCES rh_opportunities(id), source TEXT NOT NULL, external_id TEXT, url_canonical TEXT, content_fingerprint TEXT NOT NULL, discovery_run_id TEXT, raw_metadata_json TEXT, discovered_at TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rh_opportunity_research (id TEXT PRIMARY KEY, opportunity_id TEXT NOT NULL REFERENCES rh_opportunities(id), research_id TEXT NOT NULL REFERENCES research_queries(id), created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rh_settings (id TEXT PRIMARY KEY, key TEXT NOT NULL UNIQUE, value_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_rh_discovery_runs_created ON rh_discovery_runs(created_at);
CREATE INDEX IF NOT EXISTS idx_rh_discovered_sources_opportunity ON rh_discovered_sources(opportunity_id);
CREATE INDEX IF NOT EXISTS idx_rh_discovered_sources_source_extid ON rh_discovered_sources(source, external_id);
CREATE INDEX IF NOT EXISTS idx_rh_discovered_sources_url ON rh_discovered_sources(url_canonical);
CREATE INDEX IF NOT EXISTS idx_rh_discovered_sources_fingerprint ON rh_discovered_sources(content_fingerprint);
CREATE INDEX IF NOT EXISTS idx_rh_opportunity_research_opportunity ON rh_opportunity_research(opportunity_id);

-- TTT Autonomous Revenue-to-Delivery Loop v1 (Pass A: orchestrator + sales
-- execution). All new tables, so CREATE TABLE IF NOT EXISTS is safe against
-- an already-migrated production database with none of these yet -- the
-- one existing table that gains a field (rh_opportunities.lifecycle_state)
-- goes through the additive-column mechanism in store.py instead, since
-- this file is a no-op against a table that already exists.
--
-- rh_lifecycle_events: the single shared business lifecycle's append-only
-- audit trail (Company Workflow Orchestrator, falguna/lifecycle.py). Never
-- updated or deleted -- every transition, valid or refused, is a new row,
-- so "why did this opportunity reach WON" is always answerable from history
-- alone, independent of and complementary to rh_stage_history (which keeps
-- tracking the older, coarser pipeline `stage` field unchanged).
CREATE TABLE IF NOT EXISTS rh_lifecycle_events (id TEXT PRIMARY KEY, opportunity_id TEXT NOT NULL REFERENCES rh_opportunities(id), from_state TEXT, to_state TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT, evidence_json TEXT, next_action TEXT, approval_required INTEGER NOT NULL DEFAULT 0, approval_status TEXT, money_impact_json TEXT, created_at TEXT NOT NULL);
-- rh_application_attempts: Application Executor evidence log (falguna/
-- application_executor.py). One row per real attempt -- a channel that
-- merely prepares a manual-review package still gets a row, so "did we
-- actually try, and what happened" never depends on memory or the UI.
CREATE TABLE IF NOT EXISTS rh_application_attempts (id TEXT PRIMARY KEY, opportunity_id TEXT NOT NULL REFERENCES rh_opportunities(id), channel TEXT NOT NULL, status TEXT NOT NULL, blocked_reason TEXT, evidence_json TEXT, actor TEXT NOT NULL, created_at TEXT NOT NULL);
-- clients: first-class client record (Closing Agent, falguna/sales_ops.py).
-- Previously "clients" was only a derived grouping of rh_opportunities by
-- client_name in the UI (loadRhClients in hq_web.py) -- this is additive,
-- that grouping keeps working unchanged; a real row here is what Onboarding/
-- Delivery/Billing/Retention (later passes) attach to.
CREATE TABLE IF NOT EXISTS clients (id TEXT PRIMARY KEY, name TEXT NOT NULL, primary_contact TEXT, contact_channel TEXT, status TEXT NOT NULL, total_won_value REAL NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
-- rh_closing_records: the structured terms captured when a deal is closed
-- (Closing Agent, Section 8) -- final scope/price/currency/payment terms/
-- milestones/deadline/deliverables/acceptance criteria/communication
-- channel, exactly as agreed, never re-derived or guessed later.
CREATE TABLE IF NOT EXISTS rh_closing_records (id TEXT PRIMARY KEY, opportunity_id TEXT NOT NULL REFERENCES rh_opportunities(id), client_id TEXT NOT NULL REFERENCES clients(id), final_scope TEXT, final_price REAL, currency TEXT, payment_terms TEXT, milestones_json TEXT, deadline TEXT, deliverables TEXT, acceptance_criteria TEXT, communication_channel TEXT, actor TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_rh_lifecycle_events_opportunity ON rh_lifecycle_events(opportunity_id, created_at);
CREATE INDEX IF NOT EXISTS idx_rh_application_attempts_opportunity ON rh_application_attempts(opportunity_id, created_at);
CREATE INDEX IF NOT EXISTS idx_clients_name ON clients(name);
CREATE INDEX IF NOT EXISTS idx_rh_closing_records_opportunity ON rh_closing_records(opportunity_id);

-- TTT Autonomous Revenue-to-Delivery Loop v1, Passes B-E. All new tables
-- (existing ones get additive columns via store.py instead), so
-- CREATE TABLE IF NOT EXISTS is safe against the live production database.
--
-- rh_negotiation_terms: Negotiation Agent history/evidence (Section 7,
-- falguna/sales_ops.py). Every evaluate() call is a permanent row, not just
-- an audit-log line -- so "what has been proposed on this deal, and was it
-- ever out of policy" is a real, queryable history, not something you have
-- to reconstruct from the audit trail.
CREATE TABLE IF NOT EXISTS rh_negotiation_terms (id TEXT PRIMARY KEY, opportunity_id TEXT NOT NULL REFERENCES rh_opportunities(id), actor TEXT NOT NULL, price REAL, currency TEXT, discount_pct REAL, upfront_payment_pct REAL, payment_terms TEXT, free_revisions INTEGER, timeline_days INTEGER, within_policy INTEGER NOT NULL, violations_json TEXT, needs_aryan_id TEXT, created_at TEXT NOT NULL);
-- rh_conversation_messages: Conversation Inbox (Section 6, falguna/
-- conversations.py). One row per real inbound or drafted-outbound message,
-- classified and evidenced -- never fabricated, never silently sent.
CREATE TABLE IF NOT EXISTS rh_conversation_messages (id TEXT PRIMARY KEY, opportunity_id TEXT NOT NULL REFERENCES rh_opportunities(id), client_id TEXT, channel TEXT NOT NULL, external_thread_id TEXT, sender TEXT, direction TEXT NOT NULL, body TEXT NOT NULL, intent TEXT, status TEXT NOT NULL, evidence_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
-- rh_onboarding_items: Client Onboarding checklist (Section 9, falguna/
-- onboarding.py). `sensitive` items never carry the real secret in
-- `value_text` -- see OnboardingStore's own docstring.
CREATE TABLE IF NOT EXISTS rh_onboarding_items (id TEXT PRIMARY KEY, opportunity_id TEXT NOT NULL REFERENCES rh_opportunities(id), item_type TEXT NOT NULL, status TEXT NOT NULL, sensitive INTEGER NOT NULL DEFAULT 0, value_text TEXT, notes TEXT, actor TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
-- rh_invoices: Billing/Receivables (Section 12, falguna/billing.py). Never
-- moved to PAID without `evidence_json` -- see BillingStore.record_payment.
CREATE TABLE IF NOT EXISTS rh_invoices (id TEXT PRIMARY KEY, client_id TEXT NOT NULL REFERENCES clients(id), opportunity_id TEXT, active_job_id TEXT, amount REAL NOT NULL, currency TEXT NOT NULL, milestone TEXT, due_date TEXT, amount_received REAL NOT NULL DEFAULT 0, status TEXT NOT NULL, evidence_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
-- rh_completion_records: Completion/handover evidence (Section 13).
CREATE TABLE IF NOT EXISTS rh_completion_records (id TEXT PRIMARY KEY, opportunity_id TEXT NOT NULL REFERENCES rh_opportunities(id), active_job_id TEXT, evidence_json TEXT NOT NULL, checklist_json TEXT, actor TEXT NOT NULL, created_at TEXT NOT NULL);
-- rh_retention_items: Retention/Upsell follow-ups (Section 13).
CREATE TABLE IF NOT EXISTS rh_retention_items (id TEXT PRIMARY KEY, client_id TEXT NOT NULL REFERENCES clients(id), opportunity_id TEXT, kind TEXT NOT NULL, status TEXT NOT NULL, follow_up_date TEXT, notes TEXT, actor TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
-- rh_outbound_leads: Outbound Lead Agent (Section 4, falguna/outbound.py).
-- Quality-over-volume by construction -- every lead is a single researched
-- record with a stated reason, never a bulk-scraped list.
CREATE TABLE IF NOT EXISTS rh_outbound_leads (id TEXT PRIMARY KEY, company_name TEXT NOT NULL, website TEXT, contact_name TEXT, contact_channel TEXT, likely_need TEXT, proposed_offer TEXT, confidence TEXT, relevance_notes TEXT, source TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
-- rh_outreach_drafts: Outreach Agent (Section 5). Always PREPARED, never
-- SENT by this codebase itself -- see OutreachService's own docstring.
CREATE TABLE IF NOT EXISTS rh_outreach_drafts (id TEXT PRIMARY KEY, lead_id TEXT NOT NULL REFERENCES rh_outbound_leads(id), channel TEXT NOT NULL, message TEXT NOT NULL, status TEXT NOT NULL, needs_aryan_id TEXT, actor TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);

-- TTT Digital Workforce + Media/Growth Engine v1. All new tables --
-- CREATE TABLE IF NOT EXISTS is safe against the live production database,
-- same additive-only convention as every prior pass.
CREATE TABLE IF NOT EXISTS wf_tasks (id TEXT PRIMARY KEY, department TEXT NOT NULL, objective TEXT NOT NULL, task_type TEXT NOT NULL, source TEXT, assigned_worker TEXT, status TEXT NOT NULL, priority TEXT, inputs_json TEXT, outputs_json TEXT, evidence_json TEXT, blockers_json TEXT, approval_required INTEGER NOT NULL DEFAULT 0, needs_aryan_id TEXT, execution_method TEXT, cost REAL, retries INTEGER NOT NULL DEFAULT 0, error TEXT, actor TEXT NOT NULL, created_at TEXT NOT NULL, started_at TEXT, completed_at TEXT, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS wf_task_events (id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES wf_tasks(id), from_status TEXT, to_status TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT, evidence_json TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS wf_recurring_workflows (id TEXT PRIMARY KEY, name TEXT NOT NULL, department TEXT NOT NULL, objective TEXT NOT NULL, task_type TEXT NOT NULL, schedule_kind TEXT NOT NULL, schedule_config_json TEXT, task_template_json TEXT, status TEXT NOT NULL, last_run_at TEXT, next_due_at TEXT, actor TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS wf_recurring_runs (id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL REFERENCES wf_recurring_workflows(id), task_id TEXT, status TEXT NOT NULL, started_at TEXT NOT NULL, completed_at TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS wf_documents (id TEXT PRIMARY KEY, title TEXT NOT NULL, doc_type TEXT NOT NULL, department TEXT, content_text TEXT, rows_json TEXT, format TEXT, source_task_id TEXT, actor TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS wf_email_messages (id TEXT PRIMARY KEY, department TEXT, direction TEXT NOT NULL, to_address TEXT, from_address TEXT, subject TEXT, body TEXT NOT NULL, intent TEXT, status TEXT NOT NULL, needs_aryan_id TEXT, thread_id TEXT, follow_up_date TEXT, source_task_id TEXT, actor TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS media_brands (id TEXT PRIMARY KEY, name TEXT NOT NULL, voice_tone TEXT, audience TEXT, platforms_json TEXT, content_pillars_json TEXT, visual_guidelines TEXT, publishing_rules TEXT, approval_policy TEXT, actor TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS media_campaigns (id TEXT PRIMARY KEY, brand_id TEXT NOT NULL REFERENCES media_brands(id), name TEXT NOT NULL, objective TEXT, status TEXT NOT NULL, start_date TEXT, end_date TEXT, actor TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS media_content_items (id TEXT PRIMARY KEY, brand_id TEXT NOT NULL REFERENCES media_brands(id), campaign_id TEXT, title TEXT NOT NULL, objective TEXT, format TEXT, platform TEXT, target_audience TEXT, cta TEXT, content_state TEXT NOT NULL, planned_publish_date TEXT, actor TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS media_content_events (id TEXT PRIMARY KEY, content_id TEXT NOT NULL REFERENCES media_content_items(id), from_state TEXT, to_state TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT, evidence_json TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS media_scripts (id TEXT PRIMARY KEY, content_id TEXT NOT NULL REFERENCES media_content_items(id), version INTEGER NOT NULL, hook TEXT, body TEXT, scenes_json TEXT, voiceover TEXT, visual_cues TEXT, cta TEXT, caption TEXT, title_options_json TEXT, actor TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS media_assets (id TEXT PRIMARY KEY, content_id TEXT NOT NULL REFERENCES media_content_items(id), asset_type TEXT NOT NULL, provider TEXT NOT NULL, provider_kind TEXT NOT NULL, cost REAL, output_path TEXT, status TEXT NOT NULL, error TEXT, actor TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS media_publications (id TEXT PRIMARY KEY, content_id TEXT NOT NULL REFERENCES media_content_items(id), platform TEXT NOT NULL, status TEXT NOT NULL, execution_mode TEXT, evidence_json TEXT, needs_aryan_id TEXT, published_at TEXT, actor TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS media_analytics (id TEXT PRIMARY KEY, publication_id TEXT NOT NULL REFERENCES media_publications(id), metric_kind TEXT NOT NULL, value REAL NOT NULL, source TEXT NOT NULL, captured_at TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS media_experiments (id TEXT PRIMARY KEY, content_id TEXT, hypothesis TEXT NOT NULL, variable_tested TEXT, expected_signal TEXT, result TEXT, decision TEXT, status TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS media_lead_attributions (id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES comm_conversations(id), campaign_id TEXT REFERENCES media_campaigns(id), content_id TEXT REFERENCES media_content_items(id), publication_id TEXT REFERENCES media_publications(id), conversion_source TEXT NOT NULL, evidence TEXT NOT NULL, note TEXT, actor TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_rh_negotiation_terms_opportunity ON rh_negotiation_terms(opportunity_id, created_at);
CREATE INDEX IF NOT EXISTS idx_rh_conversation_messages_opportunity ON rh_conversation_messages(opportunity_id, created_at);
CREATE INDEX IF NOT EXISTS idx_rh_onboarding_items_opportunity ON rh_onboarding_items(opportunity_id);
CREATE INDEX IF NOT EXISTS idx_rh_invoices_client ON rh_invoices(client_id, created_at);
CREATE INDEX IF NOT EXISTS idx_rh_completion_records_opportunity ON rh_completion_records(opportunity_id);
CREATE INDEX IF NOT EXISTS idx_rh_retention_items_client ON rh_retention_items(client_id, created_at);
CREATE INDEX IF NOT EXISTS idx_rh_outbound_leads_status ON rh_outbound_leads(status, created_at);
CREATE INDEX IF NOT EXISTS idx_rh_outreach_drafts_lead ON rh_outreach_drafts(lead_id);
CREATE TABLE IF NOT EXISTS cc_ceo_briefs (id TEXT PRIMARY KEY, period_start TEXT NOT NULL, period_end TEXT NOT NULL, confirmed_facts_json TEXT NOT NULL, estimates_json TEXT NOT NULL, recommendations_json TEXT NOT NULL, top_priorities_json TEXT NOT NULL, risks_json TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_cc_ceo_briefs_period_end ON cc_ceo_briefs(period_end);
CREATE TABLE IF NOT EXISTS cc_goals (id TEXT PRIMARY KEY, title TEXT NOT NULL, target REAL NOT NULL, unit TEXT NOT NULL, start_date TEXT, deadline TEXT, current_value REAL NOT NULL DEFAULT 0, owner TEXT, department TEXT, status TEXT NOT NULL, linked_kpis_json TEXT, linked_actions_json TEXT, actor TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cc_goal_progress_events (id TEXT PRIMARY KEY, goal_id TEXT NOT NULL REFERENCES cc_goals(id), from_value REAL, to_value REAL NOT NULL, actor TEXT NOT NULL, note TEXT, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_cc_goal_progress_events_goal ON cc_goal_progress_events(goal_id, created_at);
CREATE TABLE IF NOT EXISTS cc_ledger_entries (id TEXT PRIMARY KEY, entry_type TEXT NOT NULL, category TEXT NOT NULL, amount REAL NOT NULL, currency TEXT NOT NULL, business_unit TEXT, client_id TEXT, project_ref TEXT, occurred_on TEXT NOT NULL, evidence TEXT NOT NULL, note TEXT, status TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_cc_ledger_entries_client ON cc_ledger_entries(client_id, created_at);
CREATE INDEX IF NOT EXISTS idx_cc_ledger_entries_type_status ON cc_ledger_entries(entry_type, status, created_at);
CREATE TABLE IF NOT EXISTS cc_budgets (id TEXT PRIMARY KEY, department TEXT NOT NULL, monthly_budget REAL NOT NULL, currency TEXT NOT NULL, limit_kind TEXT NOT NULL, warning_threshold_pct REAL, status TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cc_risks (id TEXT PRIMARY KEY, title TEXT NOT NULL, category TEXT NOT NULL, severity TEXT NOT NULL, likelihood_band TEXT NOT NULL, owner TEXT, mitigation TEXT, evidence TEXT, status TEXT NOT NULL, needs_aryan_id TEXT, actor TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_cc_budgets_department ON cc_budgets(department, status);
CREATE INDEX IF NOT EXISTS idx_cc_risks_status ON cc_risks(status, severity);

-- Phase 4 Sprint 3: FALGUNA Executive Coordinator V1 -- structured, auditable
-- recommendations (Section 4). A recommendation is a first-class advisory
-- record with its own lifecycle (PENDING -> AUTHORIZED/DECLINED -> EXECUTED),
-- distinct from a Needs-Aryan item (the human decision point it links to via
-- needs_aryan_id) and distinct from an alert (an alert is stateless/derived;
-- a recommendation is a persisted, model-or-rule-generated suggestion with
-- its own outcome). id is a stable hash of (category, ref_type, ref_id) so
-- re-running generation never creates a duplicate PENDING recommendation for
-- the same underlying situation.
CREATE TABLE IF NOT EXISTS co_recommendations (
    id TEXT PRIMARY KEY, category TEXT NOT NULL, department TEXT,
    ref_type TEXT, ref_id TEXT, recommended_action TEXT NOT NULL,
    evidence_json TEXT NOT NULL, explanation TEXT NOT NULL, priority TEXT NOT NULL,
    confidence TEXT, financial_impact REAL, financial_impact_currency TEXT,
    model_provider TEXT, model_id TEXT, needs_aryan_id TEXT,
    status TEXT NOT NULL, decided_by TEXT, decision TEXT, decision_at TEXT,
    outcome TEXT, outcome_ref_type TEXT, outcome_ref_id TEXT, outcome_at TEXT,
    actor TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_co_recommendations_status ON co_recommendations(status, created_at);
CREATE INDEX IF NOT EXISTS idx_co_recommendations_category ON co_recommendations(category, ref_type, ref_id);

-- TTT Trading Lab v1 (Sections 1-27). PAPER/RESEARCH ONLY -- no table here
-- ever represents a real brokerage/exchange order or real money movement.
-- `tl_` prefix, additive-only CREATE TABLE IF NOT EXISTS, same convention
-- as every prior pass. See falguna/trading_lab_*.py for the stores.
CREATE TABLE IF NOT EXISTS tl_markets (id TEXT PRIMARY KEY, code TEXT NOT NULL, name TEXT NOT NULL, asset_class TEXT NOT NULL, description TEXT, actor TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tl_instruments (id TEXT PRIMARY KEY, market_id TEXT NOT NULL REFERENCES tl_markets(id), symbol TEXT NOT NULL, name TEXT, currency TEXT NOT NULL, metadata_json TEXT, actor TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tl_data_sources (id TEXT PRIMARY KEY, name TEXT NOT NULL, provider_kind TEXT NOT NULL, is_synthetic INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL, notes TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tl_datasets (id TEXT PRIMARY KEY, data_source_id TEXT NOT NULL REFERENCES tl_data_sources(id), instrument_id TEXT NOT NULL REFERENCES tl_instruments(id), timeframe TEXT NOT NULL, start_date TEXT, end_date TEXT, status TEXT NOT NULL, bar_count INTEGER NOT NULL DEFAULT 0, completeness_pct REAL, error TEXT, raw_source_metadata_json TEXT, ingested_at TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tl_ohlcv_bars (id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL REFERENCES tl_datasets(id), ts TEXT NOT NULL, open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL, volume REAL NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tl_data_quality_reports (id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL REFERENCES tl_datasets(id), checked_at TEXT NOT NULL, missing_bars INTEGER NOT NULL DEFAULT 0, duplicate_timestamps INTEGER NOT NULL DEFAULT 0, non_monotonic INTEGER NOT NULL DEFAULT 0, impossible_prices INTEGER NOT NULL DEFAULT 0, invalid_volume INTEGER NOT NULL DEFAULT 0, timezone_issues INTEGER NOT NULL DEFAULT 0, stale INTEGER NOT NULL DEFAULT 0, passed INTEGER NOT NULL, notes_json TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tl_strategies (id TEXT PRIMARY KEY, name TEXT NOT NULL, hypothesis TEXT NOT NULL, market_id TEXT REFERENCES tl_markets(id), status TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tl_strategy_versions (id TEXT PRIMARY KEY, strategy_id TEXT NOT NULL REFERENCES tl_strategies(id), version_number INTEGER NOT NULL, instruments_json TEXT NOT NULL, timeframe TEXT NOT NULL, entry_rules_json TEXT NOT NULL, exit_rules_json TEXT NOT NULL, stop_logic_json TEXT, sizing_logic_json TEXT NOT NULL, allowed_hours TEXT, max_exposure_pct REAL, assumptions TEXT, known_risks TEXT, actor TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tl_strategy_status_events (id TEXT PRIMARY KEY, strategy_id TEXT NOT NULL REFERENCES tl_strategies(id), from_status TEXT, to_status TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tl_backtests (id TEXT PRIMARY KEY, strategy_version_id TEXT NOT NULL REFERENCES tl_strategy_versions(id), dataset_id TEXT NOT NULL REFERENCES tl_datasets(id), kind TEXT NOT NULL, fee_bps REAL NOT NULL, slippage_bps REAL NOT NULL, starting_cash REAL NOT NULL, params_json TEXT, status TEXT NOT NULL, metrics_json TEXT, equity_curve_json TEXT, trade_log_json TEXT, warnings_json TEXT, actor TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tl_stress_tests (id TEXT PRIMARY KEY, backtest_id TEXT NOT NULL REFERENCES tl_backtests(id), scenario TEXT NOT NULL, params_json TEXT, metrics_json TEXT, verdict TEXT NOT NULL, notes TEXT, actor TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tl_risk_limits (id TEXT PRIMARY KEY, scope TEXT NOT NULL, strategy_id TEXT REFERENCES tl_strategies(id), max_risk_per_trade_pct REAL, max_daily_loss REAL, max_strategy_drawdown_pct REAL, max_portfolio_drawdown_pct REAL, max_concurrent_positions INTEGER, max_instrument_exposure_pct REAL, max_strategy_allocation_pct REAL, status TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tl_risk_breach_events (id TEXT PRIMARY KEY, risk_limit_id TEXT NOT NULL REFERENCES tl_risk_limits(id), strategy_id TEXT, paper_account_id TEXT, breach_type TEXT NOT NULL, detail TEXT NOT NULL, needs_aryan_id TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tl_paper_accounts (id TEXT PRIMARY KEY, name TEXT NOT NULL, starting_cash REAL NOT NULL, cash REAL NOT NULL, status TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tl_paper_orders (id TEXT PRIMARY KEY, paper_account_id TEXT NOT NULL REFERENCES tl_paper_accounts(id), strategy_id TEXT NOT NULL REFERENCES tl_strategies(id), instrument_id TEXT NOT NULL REFERENCES tl_instruments(id), side TEXT NOT NULL, order_type TEXT NOT NULL, qty REAL NOT NULL, requested_price REAL, status TEXT NOT NULL, reject_reason TEXT, fill_price REAL, fees REAL, slippage REAL, actor TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, filled_at TEXT);
CREATE TABLE IF NOT EXISTS tl_paper_positions (id TEXT PRIMARY KEY, paper_account_id TEXT NOT NULL REFERENCES tl_paper_accounts(id), strategy_id TEXT NOT NULL REFERENCES tl_strategies(id), instrument_id TEXT NOT NULL REFERENCES tl_instruments(id), qty REAL NOT NULL, avg_price REAL NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tl_trades (id TEXT PRIMARY KEY, paper_account_id TEXT NOT NULL REFERENCES tl_paper_accounts(id), strategy_id TEXT NOT NULL REFERENCES tl_strategies(id), instrument_id TEXT NOT NULL REFERENCES tl_instruments(id), entry_order_id TEXT NOT NULL, exit_order_id TEXT, qty REAL NOT NULL, entry_price REAL NOT NULL, exit_price REAL, realized_pnl REAL, opened_at TEXT NOT NULL, closed_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tl_performance_snapshots (id TEXT PRIMARY KEY, scope TEXT NOT NULL, strategy_id TEXT, paper_account_id TEXT NOT NULL REFERENCES tl_paper_accounts(id), as_of TEXT NOT NULL, equity REAL NOT NULL, cash REAL NOT NULL, realized_pnl REAL NOT NULL, unrealized_pnl REAL NOT NULL, drawdown_pct REAL, metrics_json TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tl_reviews (id TEXT PRIMARY KEY, strategy_version_id TEXT NOT NULL REFERENCES tl_strategy_versions(id), reviewer_role TEXT NOT NULL, verdict TEXT NOT NULL, notes TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tl_council_decisions (id TEXT PRIMARY KEY, strategy_id TEXT NOT NULL REFERENCES tl_strategies(id), strategy_version_id TEXT NOT NULL REFERENCES tl_strategy_versions(id), decision TEXT NOT NULL, rationale TEXT NOT NULL, reviews_json TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tl_graveyard (id TEXT PRIMARY KEY, strategy_id TEXT NOT NULL REFERENCES tl_strategies(id), strategy_version_id TEXT NOT NULL REFERENCES tl_strategy_versions(id), reason_rejected TEXT NOT NULL, failed_metrics_json TEXT, failure_conditions TEXT, reviewer_notes TEXT, actor TEXT NOT NULL, killed_at TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_tl_instruments_market ON tl_instruments(market_id);
CREATE INDEX IF NOT EXISTS idx_tl_datasets_instrument ON tl_datasets(instrument_id, timeframe);
CREATE INDEX IF NOT EXISTS idx_tl_ohlcv_bars_dataset_ts ON tl_ohlcv_bars(dataset_id, ts);
CREATE INDEX IF NOT EXISTS idx_tl_strategy_versions_strategy ON tl_strategy_versions(strategy_id, version_number);
CREATE INDEX IF NOT EXISTS idx_tl_backtests_strategy_version ON tl_backtests(strategy_version_id, created_at);
CREATE INDEX IF NOT EXISTS idx_tl_stress_tests_backtest ON tl_stress_tests(backtest_id);
CREATE INDEX IF NOT EXISTS idx_tl_risk_limits_strategy ON tl_risk_limits(strategy_id, status);
CREATE INDEX IF NOT EXISTS idx_tl_paper_orders_account ON tl_paper_orders(paper_account_id, created_at);
CREATE INDEX IF NOT EXISTS idx_tl_paper_positions_account ON tl_paper_positions(paper_account_id, strategy_id);
CREATE INDEX IF NOT EXISTS idx_tl_trades_account ON tl_trades(paper_account_id, strategy_id);
CREATE INDEX IF NOT EXISTS idx_tl_performance_snapshots_account ON tl_performance_snapshots(paper_account_id, as_of);
CREATE INDEX IF NOT EXISTS idx_tl_reviews_strategy_version ON tl_reviews(strategy_version_id);
CREATE INDEX IF NOT EXISTS idx_tl_council_decisions_strategy ON tl_council_decisions(strategy_id, created_at);
CREATE INDEX IF NOT EXISTS idx_tl_graveyard_strategy ON tl_graveyard(strategy_id);

-- TTT Venture Studio / Multi-Venture Operating System v1. `vs_` prefix,
-- additive-only CREATE TABLE IF NOT EXISTS, same convention as every prior
-- pass. See falguna/ventures.py for the stores. Venture-scoping columns on
-- pre-existing tables (cc_ledger_entries.venture_id, cc_goals.venture_id,
-- cc_risks.venture_id, wf_tasks.venture_id, missions.venture_id,
-- media_brands.venture_id, rh_opportunities.venture_id) are added via
-- StateStore._ADDITIVE_COLUMNS in store.py, never here.
CREATE TABLE IF NOT EXISTS vs_ventures (id TEXT PRIMARY KEY, name TEXT NOT NULL, slug TEXT NOT NULL, venture_type TEXT NOT NULL, description TEXT, thesis TEXT, owner TEXT, status TEXT NOT NULL, launch_date TEXT, parent_company TEXT NOT NULL, linked_product TEXT, linked_brand_id TEXT, linked_departments_json TEXT, actor TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS vs_venture_status_events (id TEXT PRIMARY KEY, venture_id TEXT NOT NULL REFERENCES vs_ventures(id), from_status TEXT, to_status TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT, evidence_json TEXT, financial_impact TEXT, next_action TEXT, needs_aryan_id TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS vs_experiments (id TEXT PRIMARY KEY, venture_id TEXT NOT NULL REFERENCES vs_ventures(id), hypothesis TEXT NOT NULL, metric TEXT NOT NULL, target TEXT, owner TEXT, budget REAL, start_date TEXT, end_date TEXT, evidence_json TEXT, result TEXT, decision TEXT, status TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS vs_validation_signals (id TEXT PRIMARY KEY, venture_id TEXT NOT NULL REFERENCES vs_ventures(id), signal_type TEXT NOT NULL, description TEXT NOT NULL, evidence TEXT, strength TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS vs_capital_allocations (id TEXT PRIMARY KEY, venture_id TEXT NOT NULL REFERENCES vs_ventures(id), direction TEXT NOT NULL, amount REAL NOT NULL, source_note TEXT NOT NULL, related_venture_id TEXT, needs_aryan_id TEXT, actor TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS vs_resource_requests (id TEXT PRIMARY KEY, venture_id TEXT NOT NULL REFERENCES vs_ventures(id), department TEXT NOT NULL, resource_type TEXT NOT NULL, amount_or_qty REAL NOT NULL, status TEXT NOT NULL, conflict_with_json TEXT, actor TEXT NOT NULL, note TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS vs_recommendations (id TEXT PRIMARY KEY, venture_id TEXT NOT NULL REFERENCES vs_ventures(id), recommendation TEXT NOT NULL, rationale_json TEXT NOT NULL, inputs_snapshot_json TEXT, needs_aryan_id TEXT, actor TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS vs_graveyard (id TEXT PRIMARY KEY, venture_id TEXT NOT NULL REFERENCES vs_ventures(id), original_thesis TEXT, total_invested REAL, experiments_summary_json TEXT, evidence_summary_json TEXT, reason_killed TEXT NOT NULL, lessons TEXT, assets_produced_json TEXT, actor TEXT NOT NULL, killed_at TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS vs_assets (id TEXT PRIMARY KEY, venture_id TEXT NOT NULL REFERENCES vs_ventures(id), asset_type TEXT NOT NULL, name TEXT NOT NULL, description TEXT, location_or_ref TEXT, metadata_json TEXT, status TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS vs_relationships (id TEXT PRIMARY KEY, venture_a_id TEXT NOT NULL REFERENCES vs_ventures(id), venture_b_id TEXT NOT NULL REFERENCES vs_ventures(id), relationship_type TEXT NOT NULL, description TEXT, actor TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_vs_ventures_slug ON vs_ventures(slug);
CREATE INDEX IF NOT EXISTS idx_vs_ventures_status ON vs_ventures(status);
CREATE INDEX IF NOT EXISTS idx_vs_venture_status_events_venture ON vs_venture_status_events(venture_id, created_at);
CREATE INDEX IF NOT EXISTS idx_vs_experiments_venture ON vs_experiments(venture_id, status);
CREATE INDEX IF NOT EXISTS idx_vs_validation_signals_venture ON vs_validation_signals(venture_id);
CREATE INDEX IF NOT EXISTS idx_vs_capital_allocations_venture ON vs_capital_allocations(venture_id, created_at);
CREATE INDEX IF NOT EXISTS idx_vs_resource_requests_venture ON vs_resource_requests(venture_id, status);
CREATE INDEX IF NOT EXISTS idx_vs_resource_requests_dept ON vs_resource_requests(department, resource_type, status);
CREATE INDEX IF NOT EXISTS idx_vs_recommendations_venture ON vs_recommendations(venture_id, created_at);
CREATE INDEX IF NOT EXISTS idx_vs_graveyard_venture ON vs_graveyard(venture_id);
CREATE INDEX IF NOT EXISTS idx_vs_assets_venture ON vs_assets(venture_id, asset_type);
CREATE INDEX IF NOT EXISTS idx_vs_relationships_a ON vs_relationships(venture_a_id);
CREATE INDEX IF NOT EXISTS idx_vs_relationships_b ON vs_relationships(venture_b_id);

-- TTT Group OS / Company Orchestrator v2. `co_` prefix, additive-only
-- CREATE TABLE IF NOT EXISTS, same convention as every prior pass. See
-- falguna/company_os.py for the stores/engines. This is the top-level
-- operating intelligence that coordinates the whole company -- it reuses
-- every existing execution engine (Revenue Hunter, Digital Workforce,
-- Media/Growth, Falguna Engineering, Finance/Capital, Venture Studio,
-- Trading Lab) rather than duplicating any of them; these tables hold only
-- what is genuinely new: objectives, plans, priorities, department
-- decomposition, venture alignment, resource/capital recommendations, the
-- event bus, replanning, decisions, policies, escalations, the timeline,
-- traceability links, company memory, failure/blocker records, cost
-- estimates, goal feedback, and the daily/weekly operating loops.
CREATE TABLE IF NOT EXISTS co_objectives (id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT, owner TEXT, priority TEXT, target TEXT, deadline TEXT, status TEXT NOT NULL, linked_goals_json TEXT, linked_ventures_json TEXT, linked_departments_json TEXT, budget_scope TEXT, risk_tolerance TEXT, evidence TEXT, current_progress TEXT, actor TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS co_objective_status_events (id TEXT PRIMARY KEY, objective_id TEXT NOT NULL REFERENCES co_objectives(id), from_status TEXT, to_status TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT, evidence_json TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS co_plans (id TEXT PRIMARY KEY, objective_id TEXT NOT NULL REFERENCES co_objectives(id), desired_outcome TEXT NOT NULL, milestones_json TEXT, dependencies_json TEXT, ventures_json TEXT, departments_json TEXT, capital_requirement TEXT, workforce_requirement TEXT, falguna_work_requirement TEXT, sales_media_needs TEXT, risks_json TEXT, approvals_json TEXT, expected_evidence TEXT, workforce_assignments_json TEXT, status TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS co_priority_evaluations (id TEXT PRIMARY KEY, ref_type TEXT NOT NULL, ref_id TEXT NOT NULL, inputs_json TEXT NOT NULL, priority TEXT NOT NULL, rationale TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS co_department_objectives (id TEXT PRIMARY KEY, company_objective_id TEXT NOT NULL REFERENCES co_objectives(id), department TEXT NOT NULL, title TEXT NOT NULL, owner TEXT, due_date TEXT, expected_output TEXT, evidence TEXT, dependencies_json TEXT, status TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS co_venture_links (id TEXT PRIMARY KEY, objective_id TEXT NOT NULL REFERENCES co_objectives(id), venture_id TEXT NOT NULL, contribution TEXT, dependency TEXT, priority TEXT, budget_impact TEXT, execution_health TEXT, actor TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS co_resource_recommendations (id TEXT PRIMARY KEY, scope TEXT NOT NULL, department TEXT, finding TEXT NOT NULL, recommendation TEXT NOT NULL, severity TEXT NOT NULL, status TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS co_capital_recommendations (id TEXT PRIMARY KEY, objective_id TEXT, recommendation TEXT NOT NULL, rationale TEXT NOT NULL, amount REAL, status TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS co_events (id TEXT PRIMARY KEY, event_type TEXT NOT NULL, source TEXT NOT NULL, ref_type TEXT, ref_id TEXT, payload_json TEXT, idempotency_key TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS co_replans (id TEXT PRIMARY KEY, objective_id TEXT NOT NULL REFERENCES co_objectives(id), original_plan_id TEXT, new_plan_id TEXT, reason TEXT NOT NULL, changed_assumptions_json TEXT, original_plan_snapshot_json TEXT, actor TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS co_decisions (id TEXT PRIMARY KEY, question TEXT NOT NULL, options_json TEXT NOT NULL, evidence TEXT, risks TEXT, cost TEXT, expected_impact TEXT, recommendation TEXT, confidence TEXT, status TEXT NOT NULL, decided_by TEXT, decision_note TEXT, ref_type TEXT, ref_id TEXT, needs_aryan_id TEXT, actor TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, decided_at TEXT);
CREATE TABLE IF NOT EXISTS co_policies (id TEXT PRIMARY KEY, domain TEXT NOT NULL, title TEXT NOT NULL, rule_json TEXT NOT NULL, requires_needs_aryan INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS co_escalations (id TEXT PRIMARY KEY, ref_type TEXT NOT NULL, ref_id TEXT NOT NULL, impact TEXT, risk TEXT, cost TEXT, irreversibility TEXT, external_commitment TEXT, decision TEXT NOT NULL, needs_aryan_id TEXT, actor TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS co_timeline_events (id TEXT PRIMARY KEY, event_type TEXT NOT NULL, title TEXT NOT NULL, description TEXT, ref_type TEXT, ref_id TEXT, occurred_at TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS co_traceability_links (id TEXT PRIMARY KEY, from_type TEXT NOT NULL, from_id TEXT NOT NULL, to_type TEXT NOT NULL, to_id TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS co_memory (id TEXT PRIMARY KEY, subject_type TEXT NOT NULL, subject_id TEXT, kind TEXT NOT NULL, content TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS co_failures (id TEXT PRIMARY KEY, ref_type TEXT NOT NULL, ref_id TEXT NOT NULL, description TEXT NOT NULL, dependency TEXT, retried INTEGER NOT NULL DEFAULT 0, rerouted INTEGER NOT NULL DEFAULT 0, escalated INTEGER NOT NULL DEFAULT 0, needs_aryan_id TEXT, status TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS co_cost_estimates (id TEXT PRIMARY KEY, ref_type TEXT NOT NULL, ref_id TEXT NOT NULL, estimated_ai_cost REAL, estimated_api_cost REAL, estimated_workforce_cost REAL, estimated_project_cost REAL, actual_ai_cost REAL, actual_api_cost REAL, actual_workforce_cost REAL, actual_project_cost REAL, actor TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS co_goal_feedback_events (id TEXT PRIMARY KEY, goal_id TEXT NOT NULL, target REAL, actual REAL, variance REAL, likely_reason TEXT, recommended_adjustment TEXT, actor TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS co_daily_loops (id TEXT PRIMARY KEY, run_date TEXT NOT NULL, state_snapshot_json TEXT NOT NULL, changes_json TEXT, blockers_json TEXT, priorities_json TEXT, recommended_actions_json TEXT, department_actions_json TEXT, ceo_brief_ref TEXT, actor TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS co_weekly_reviews (id TEXT PRIMARY KEY, week_start TEXT NOT NULL, week_end TEXT NOT NULL, objective_progress_json TEXT, venture_performance_json TEXT, sales_json TEXT, delivery_json TEXT, media_json TEXT, workforce_json TEXT, finance_json TEXT, risks_json TEXT, resource_allocation_json TEXT, recommendations_json TEXT, actor TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_co_objectives_status ON co_objectives(status, priority);
CREATE INDEX IF NOT EXISTS idx_co_objective_status_events_objective ON co_objective_status_events(objective_id, created_at);
CREATE INDEX IF NOT EXISTS idx_co_plans_objective ON co_plans(objective_id, status);
CREATE INDEX IF NOT EXISTS idx_co_priority_evaluations_ref ON co_priority_evaluations(ref_type, ref_id, created_at);
CREATE INDEX IF NOT EXISTS idx_co_department_objectives_company_objective ON co_department_objectives(company_objective_id, department);
CREATE INDEX IF NOT EXISTS idx_co_department_objectives_department ON co_department_objectives(department, status);
CREATE INDEX IF NOT EXISTS idx_co_venture_links_objective ON co_venture_links(objective_id);
CREATE INDEX IF NOT EXISTS idx_co_venture_links_venture ON co_venture_links(venture_id);
CREATE INDEX IF NOT EXISTS idx_co_resource_recommendations_scope ON co_resource_recommendations(scope, status, created_at);
CREATE INDEX IF NOT EXISTS idx_co_capital_recommendations_objective ON co_capital_recommendations(objective_id, created_at);
CREATE INDEX IF NOT EXISTS idx_co_events_type ON co_events(event_type, created_at);
CREATE INDEX IF NOT EXISTS idx_co_events_ref ON co_events(ref_type, ref_id);
CREATE INDEX IF NOT EXISTS idx_co_events_idempotency ON co_events(idempotency_key);
CREATE INDEX IF NOT EXISTS idx_co_replans_objective ON co_replans(objective_id, created_at);
CREATE INDEX IF NOT EXISTS idx_co_decisions_status ON co_decisions(status, created_at);
CREATE INDEX IF NOT EXISTS idx_co_decisions_ref ON co_decisions(ref_type, ref_id);
CREATE INDEX IF NOT EXISTS idx_co_policies_domain ON co_policies(domain, status);
CREATE INDEX IF NOT EXISTS idx_co_escalations_ref ON co_escalations(ref_type, ref_id, created_at);
CREATE INDEX IF NOT EXISTS idx_co_timeline_events_occurred ON co_timeline_events(occurred_at);
CREATE INDEX IF NOT EXISTS idx_co_traceability_links_from ON co_traceability_links(from_type, from_id);
CREATE INDEX IF NOT EXISTS idx_co_traceability_links_to ON co_traceability_links(to_type, to_id);
CREATE INDEX IF NOT EXISTS idx_co_memory_subject ON co_memory(subject_type, subject_id, created_at);
CREATE INDEX IF NOT EXISTS idx_co_failures_ref ON co_failures(ref_type, ref_id, created_at);
CREATE INDEX IF NOT EXISTS idx_co_failures_status ON co_failures(status);
CREATE INDEX IF NOT EXISTS idx_co_cost_estimates_ref ON co_cost_estimates(ref_type, ref_id);
CREATE INDEX IF NOT EXISTS idx_co_goal_feedback_events_goal ON co_goal_feedback_events(goal_id, created_at);
CREATE INDEX IF NOT EXISTS idx_co_daily_loops_run_date ON co_daily_loops(run_date);
CREATE INDEX IF NOT EXISTS idx_co_weekly_reviews_week_start ON co_weekly_reviews(week_start);

-- Falguna V2.1: UX Hardening + Provider Independence Foundation. One
-- generic, additive key/value table for the model-provider registry and
-- routing settings (falguna/providers.py, falguna/model_router.py) -- the
-- same shape as the pre-existing rh_settings table, deliberately kept
-- separate from it so Chat/Research/Work model configuration is never
-- coupled to the unrelated Revenue Hunter subsystem that owns rh_settings.
-- A row's value_json never contains a secret in plaintext: an API key is
-- stored only as a reference to where Falguna should read it from (an
-- environment variable name or a file path), exactly like the pre-existing
-- OpenAICompatibleGateway.api_key_file convention -- see providers.py.
CREATE TABLE IF NOT EXISTS model_settings (id TEXT PRIMARY KEY, key TEXT NOT NULL UNIQUE, value_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_model_settings_key ON model_settings(key);

-- Falguna Browser + Computer Use V1 (falguna/browser_runtime.py): a real,
-- persistent, Playwright-backed browser session, first-class in Mission
-- Control. `browser_settings`/`browser_registry`-style scalar settings
-- reuse the existing generic model_settings key/value table (a new row
-- under key "browser_settings") rather than a second single-row settings
-- table -- see falguna/browser_planner.py's BrowserSettingsStore.
CREATE TABLE IF NOT EXISTS browser_sessions (
    id TEXT PRIMARY KEY, objective TEXT NOT NULL, task_type TEXT NOT NULL, project_id TEXT,
    status TEXT NOT NULL, headless INTEGER NOT NULL DEFAULT 1, privacy_mode TEXT, plan_json TEXT,
    current_url TEXT, active_tab_id TEXT, needs_aryan_reason TEXT,
    error TEXT, error_category TEXT, error_detail TEXT,
    conversation_id TEXT, research_id TEXT, mc_archived INTEGER,
    actor TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    started_at TEXT, completed_at TEXT, next_step_index INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_browser_sessions_status ON browser_sessions(status, created_at);
CREATE TABLE IF NOT EXISTS browser_tabs (
    id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES browser_sessions(id), tab_index INTEGER NOT NULL,
    url TEXT, title TEXT, opened_by_action_id TEXT, status TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, closed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_browser_tabs_session ON browser_tabs(session_id, tab_index);
CREATE TABLE IF NOT EXISTS browser_actions (
    id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES browser_sessions(id), tab_id TEXT, seq INTEGER NOT NULL,
    action_type TEXT NOT NULL, target TEXT, value TEXT, result TEXT NOT NULL, detail TEXT,
    screenshot_attachment_id TEXT, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_browser_actions_session ON browser_actions(session_id, seq);
CREATE TABLE IF NOT EXISTS browser_downloads (
    id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES browser_sessions(id), filename TEXT NOT NULL,
    source_url TEXT, attachment_id TEXT, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_browser_downloads_session ON browser_downloads(session_id, created_at);

-- Falguna Memory & Knowledge V2 (falguna/memory.py). Reuses every existing
-- seam rather than inventing a parallel system: `scope_id` for scope_type
-- 'project' is one of project_profiles.json's own ids (falguna/web.py's
-- load_profiles -- the single project registry); for scope_type 'venture'
-- it is a vs_ventures.id (Venture Studio's own registry); 'personal' and
-- 'company' rows carry a NULL scope_id (company = TTT-wide, authorized
-- shared knowledge; personal = Aryan, unscoped). There is no second
-- project/tenant registry anywhere in this schema.
--
-- Provenance and fact-strength are never conflated: `kind` records what
-- KIND of statement this is (an instruction, an explicitly saved
-- preference, a confirmed fact, a source-derived observation, an
-- assistant-written summary, or an uncertain inference) and `confidence`
-- separately records how sure Falguna is the content is true (verified /
-- user_provided / inferred) -- an inference is never silently upgraded to
-- verified just because it was written down.
--
-- Deletion policy (Pass B/I): `state` moves ACTIVE -> SUPERSEDED (a newer
-- record replaces this one; content is kept for authorized history, but a
-- superseded row is removed from memory_fts so it can never surface in a
-- keyword/semantic search or be assembled into a model's context again) or
-- ACTIVE -> DELETED (an explicit Forget; also removed from memory_fts).
-- `purged_at` marks a *second*, separate, user-selected step that actually
-- overwrites `content` with a tombstone string for a DELETED row -- the
-- genuine, non-reversible removal Pass B calls for, kept distinct from the
-- ordinary (reversible, content-preserving) Forget so that irreversible
-- content erasure is never a side effect of an everyday delete.
CREATE TABLE IF NOT EXISTS memory_records (
    id TEXT PRIMARY KEY,
    scope_type TEXT NOT NULL,            -- personal | project | venture | company
    scope_id TEXT,                       -- project_profiles.json id, or vs_ventures.id; NULL for personal/company
    kind TEXT NOT NULL,                  -- instruction | preference | fact | observation | summary | inference
    content TEXT NOT NULL,
    source_type TEXT NOT NULL,           -- user_stated | conversation | research | document | browser | system_derived
    source_ref TEXT,                     -- conversation_id/message_id, research_id, knowledge_document_id, browser_session_id (free-form pointer, never executed)
    confidence TEXT NOT NULL,            -- verified | user_provided | inferred
    sensitivity TEXT NOT NULL DEFAULT 'normal',  -- normal | sensitive
    pinned INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'active', -- active | superseded | deleted
    supersedes_id TEXT REFERENCES memory_records(id),
    superseded_by_id TEXT REFERENCES memory_records(id),
    valid_from TEXT,
    valid_until TEXT,
    purged_at TEXT,
    deletion_reason TEXT,
    project_id TEXT,                     -- denormalized convenience mirror of scope_id when scope_type='project' (kept for simple joins/filters only)
    venture_id TEXT,                     -- denormalized convenience mirror of scope_id when scope_type='venture'
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_memory_records_scope ON memory_records(scope_type, scope_id, state);
CREATE INDEX IF NOT EXISTS idx_memory_records_state ON memory_records(state, updated_at);
CREATE INDEX IF NOT EXISTS idx_memory_records_supersedes ON memory_records(supersedes_id);
-- Standalone (not "external content") FTS5 index: rows are inserted/removed
-- explicitly by falguna/memory.py alongside memory_records writes, never by
-- a SQLite trigger, so a superseded/deleted row's removal from search is a
-- real, verifiable step this codebase performs rather than a DB feature to
-- trust blindly.
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(content, ref_id UNINDEXED);

CREATE TABLE IF NOT EXISTS knowledge_documents (
    id TEXT PRIMARY KEY,
    scope_type TEXT NOT NULL,            -- personal | project | venture | company
    scope_id TEXT,
    project_id TEXT,
    venture_id TEXT,
    title TEXT NOT NULL,
    source_type TEXT NOT NULL,           -- upload | conversation | research | browser_download | file_path
    source_ref TEXT,
    mime_type TEXT,
    byte_size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    status TEXT NOT NULL,                -- ready | unsupported_format | too_large | error | duplicate | deleted
    error TEXT,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_knowledge_documents_scope ON knowledge_documents(scope_type, scope_id, status);
CREATE INDEX IF NOT EXISTS idx_knowledge_documents_hash ON knowledge_documents(scope_type, scope_id, sha256);

CREATE TABLE IF NOT EXISTS knowledge_chunks (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES knowledge_documents(id),
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    char_start INTEGER NOT NULL,
    char_end INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    embedding_json TEXT,        -- populated only when a local embedding adapter actually ran (never fabricated)
    embedding_model TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_document ON knowledge_chunks(document_id, chunk_index);
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(content, ref_id UNINDEXED, document_id UNINDEXED);

CREATE TABLE IF NOT EXISTS memory_suggestions (
    id TEXT PRIMARY KEY,
    scope_type TEXT NOT NULL,
    scope_id TEXT,
    conversation_id TEXT,
    message_id TEXT,
    suggested_kind TEXT NOT NULL,
    suggested_content TEXT NOT NULL,
    signal TEXT,                 -- which deterministic phrase/rule triggered this suggestion (auditable, never a black box)
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | accepted | dismissed
    memory_record_id TEXT,       -- set once accepted
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memory_suggestions_status ON memory_suggestions(status, created_at);

-- ============================================================
-- Public corporate website (falguna/site_web.py, port 8767)
-- Added: TTT Flagship Website V1. Additive only, site_ prefixed.
-- ============================================================

CREATE TABLE IF NOT EXISTS site_services (
    id TEXT PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    division TEXT NOT NULL,          -- e.g. "Custom Software Engineering"
    tagline TEXT NOT NULL,
    summary TEXT NOT NULL,
    deliverables_json TEXT NOT NULL, -- JSON array of strings
    process_json TEXT NOT NULL,      -- JSON array of {step, detail}
    proof_slugs_json TEXT,           -- JSON array of site_case_studies.slug
    status TEXT NOT NULL DEFAULT 'current', -- current | partner_qualified | in_development
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS site_case_studies (
    id TEXT PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    client_label TEXT NOT NULL,      -- e.g. "Royal Table (TTT demonstration project)"
    is_own_project INTEGER NOT NULL DEFAULT 0,
    summary TEXT NOT NULL,
    problem TEXT NOT NULL,
    approach TEXT NOT NULL,
    outcome TEXT NOT NULL,
    stack_json TEXT NOT NULL,
    division_slugs_json TEXT,
    sort_order INTEGER NOT NULL DEFAULT 0,
    published INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS site_products (
    id TEXT PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    tagline TEXT NOT NULL,
    summary TEXT NOT NULL,
    status TEXT NOT NULL,            -- research | in_development | beta | available
    is_internal INTEGER NOT NULL DEFAULT 0, -- Falguna/TTT HQ = internal tooling, publicly described but not sold
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS site_posts (
    id TEXT PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    dek TEXT,
    body_md TEXT NOT NULL,
    author TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'engineering', -- engineering | company | marketing
    published INTEGER NOT NULL DEFAULT 0,
    published_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS site_jobs (
    id TEXT PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    department TEXT NOT NULL,
    employment_type TEXT NOT NULL,   -- full_time | contract | internship
    location_policy TEXT NOT NULL,   -- e.g. "Remote (India, +/-3h IST)"
    summary TEXT NOT NULL,
    responsibilities_json TEXT NOT NULL,
    requirements_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open', -- open | closed
    posted_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS site_applications (
    id TEXT PRIMARY KEY,
    job_id TEXT REFERENCES site_jobs(id),
    job_title_snapshot TEXT NOT NULL, -- "General Application" if job_id is NULL
    applicant_name TEXT NOT NULL,
    applicant_email TEXT NOT NULL,
    applicant_phone TEXT,
    links_json TEXT,                  -- portfolio/LinkedIn/GitHub URLs
    cover_note TEXT,
    resume_filename TEXT,
    resume_storage_rel_path TEXT,
    resume_sha256 TEXT,
    resume_size_bytes INTEGER,
    resume_source_url TEXT,           -- Phase 1 R2: external hosted URL (e.g. Tally) when bytes were never fetched/stored locally -- NULL for a native upload that already has resume_storage_rel_path
    status TEXT NOT NULL DEFAULT 'new', -- new | reviewed | rejected | shortlisted
    source_ip_hash TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_site_applications_status ON site_applications(status, created_at);

CREATE TABLE IF NOT EXISTS site_enquiries (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,               -- general | project
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    company TEXT,
    message TEXT NOT NULL,
    opportunity_id TEXT,              -- set when kind=project and OpportunityStore.create() succeeded
    source_ip_hash TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS site_staff_users (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,      -- PBKDF2-HMAC-SHA256, salted (see site_auth.py)
    role TEXT NOT NULL DEFAULT 'staff', -- staff | admin
    is_active INTEGER NOT NULL DEFAULT 1,
    mfa_enabled INTEGER NOT NULL DEFAULT 0, -- reserved; real TOTP not wired in V1, see spec
    failed_login_count INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT,
    last_login_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS site_staff_sessions (
    id TEXT PRIMARY KEY,              -- opaque random token (the session id / cookie value)
    user_id TEXT NOT NULL REFERENCES site_staff_users(id),
    csrf_token TEXT NOT NULL,
    ip_hash TEXT,
    user_agent TEXT,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_site_staff_sessions_user ON site_staff_sessions(user_id);


-- ============================================================
-- TTT Communications + AI Customer Service V1 (falguna/comms.py)
-- Milestone 1: Unified Communications Center.
--
-- One durable, channel-agnostic model for every inbound/outbound customer
-- interaction (general enquiries, sales, support, projects, billing,
-- careers, media, and future channels), additive and separate from the
-- existing sales-opportunity-scoped `rh_conversation_messages` (which
-- keeps working exactly as-is). A comm_conversation optionally LINKS to a
-- real rh_opportunities row, a real clients row, or a real site_applications
-- row rather than duplicating them -- there is no second, competing CRM
-- here. Attachments reuse the existing generic `attachments` table
-- (conversation_id/message_id columns already support this). Escalations
-- and approvals reuse the existing needs_aryan_items queue via
-- NeedsAryanQueue, never a parallel approval system. The audit trail reuses
-- the existing AuditLog hash chain, the same as every other subsystem.
--
-- Channel and Department are validated Python-level enums (falguna/
-- comms.py: CHANNELS, DEPARTMENTS), not separate reference tables -- the
-- same convention this codebase already uses for rh_opportunities.stage,
-- ConversationStore's INTENTS, etc.: a small, closed, code-reviewed set of
-- values doesn't need its own table and foreign key.
-- ============================================================

CREATE TABLE IF NOT EXISTS comm_organizations (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    domain TEXT,                       -- lowercased email domain, used for dedup/lookup
    linked_client_id TEXT,             -- REFERENCES clients(id) once/if a deal closes; nullable
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_comm_organizations_domain ON comm_organizations(domain);

CREATE TABLE IF NOT EXISTS comm_contacts (
    id TEXT PRIMARY KEY,
    organization_id TEXT REFERENCES comm_organizations(id),
    name TEXT,
    email TEXT,
    phone TEXT,
    role_title TEXT,
    notes TEXT,
    -- Phase 5 Final Client Experience, Section 8 (Customer Communication
    -- Profile): NULL means "unknown/not yet set", never a guessed default.
    -- See falguna/comms.py's set_contact_preferences() / COMM_PREFERENCE_*.
    preferred_language TEXT,
    preferred_channel TEXT,
    tone TEXT,
    detail_level TEXT,
    technical_level TEXT,
    update_cadence TEXT,
    timezone TEXT,
    call_preference TEXT,
    communication_restrictions TEXT,
    preferences_set_by TEXT,
    preferences_updated_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_comm_contacts_email ON comm_contacts(email);
CREATE INDEX IF NOT EXISTS idx_comm_contacts_org ON comm_contacts(organization_id);

CREATE TABLE IF NOT EXISTS comm_conversations (
    id TEXT PRIMARY KEY,
    channel TEXT NOT NULL,             -- EMAIL | WEBSITE | SUPPORT | CAREERS | PROJECT | INTERNAL
    department TEXT NOT NULL,          -- general | sales | support | projects | billing | careers | media
    subject TEXT,
    status TEXT NOT NULL,              -- new | open | pending_customer | pending_approval | escalated | resolved | closed
    priority TEXT NOT NULL,            -- low | normal | high | urgent
    tags_json TEXT,                    -- JSON array of short string tags
    organization_id TEXT REFERENCES comm_organizations(id),
    primary_contact_id TEXT REFERENCES comm_contacts(id),
    assigned_agent TEXT,               -- an AI role name (falguna/comms.py AGENT_ROLES) or 'Aryan'
    linked_opportunity_id TEXT,        -- REFERENCES rh_opportunities(id), nullable
    linked_project_id TEXT,            -- reserved: REFERENCES a future delivery/project record, nullable
    linked_client_id TEXT,             -- REFERENCES clients(id), nullable
    linked_application_id TEXT,        -- REFERENCES site_applications(id), nullable (careers)
    source_ref_type TEXT,              -- e.g. 'site_enquiry', 'site_application' -- what created this
    source_ref_id TEXT,
    first_response_due_at TEXT,        -- SLA target, set deterministically from priority at open time
    first_response_at TEXT,
    resolution_due_at TEXT,
    resolved_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_comm_conversations_status ON comm_conversations(status, department);
CREATE INDEX IF NOT EXISTS idx_comm_conversations_org ON comm_conversations(organization_id);
CREATE INDEX IF NOT EXISTS idx_comm_conversations_opportunity ON comm_conversations(linked_opportunity_id);
CREATE INDEX IF NOT EXISTS idx_comm_conversations_application ON comm_conversations(linked_application_id);
CREATE INDEX IF NOT EXISTS idx_comm_conversations_created ON comm_conversations(created_at);

CREATE TABLE IF NOT EXISTS comm_messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES comm_conversations(id),
    direction TEXT NOT NULL,           -- INBOUND | OUTBOUND
    kind TEXT NOT NULL,                -- message | note | system
    sender_contact_id TEXT REFERENCES comm_contacts(id),
    sender_agent TEXT,                 -- AI role name or 'Aryan' when direction=OUTBOUND
    body TEXT NOT NULL,
    status TEXT NOT NULL,              -- RECEIVED | DRAFT | APPROVED | SENT
    is_internal_note INTEGER NOT NULL DEFAULT 0,
    source_ref_type TEXT,
    source_ref_id TEXT,
    send_method TEXT,                  -- Phase 1 R3: NULL | manual | provider
    provider_name TEXT,                -- Phase 1 R3: the EmailProvider class name that attempted/sent this
    send_attempts INTEGER NOT NULL DEFAULT 0,  -- Phase 1 R3: real provider-send attempts made (0 = never attempted)
    failure_reason TEXT,               -- Phase 1 R3: set when status=FAILED, so Failed Delivery is never a bare status with no reason
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_comm_messages_conversation ON comm_messages(conversation_id, created_at);

CREATE TABLE IF NOT EXISTS comm_participants (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES comm_conversations(id),
    participant_type TEXT NOT NULL,    -- customer | agent | watcher
    contact_id TEXT REFERENCES comm_contacts(id),
    agent_role TEXT,
    created_at TEXT NOT NULL           -- StateStore.list() always orders by created_at
);
CREATE INDEX IF NOT EXISTS idx_comm_participants_conversation ON comm_participants(conversation_id);

CREATE TABLE IF NOT EXISTS comm_status_events (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES comm_conversations(id),
    field TEXT NOT NULL,               -- status | priority | assigned_agent | escalation
    old_value TEXT,
    new_value TEXT,
    actor TEXT NOT NULL,
    reason TEXT,
    needs_aryan_id TEXT,               -- set when field='escalation'
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_comm_status_events_conversation ON comm_status_events(conversation_id, created_at);

-- TTT Communications V2 (falguna/risk_engine.py): deterministic outbound
-- risk classification, persisted per event so TTT HQ and any later audit
-- can see exactly what was classified and why -- not just the final state.
CREATE TABLE IF NOT EXISTS comm_risk_events (
    id TEXT PRIMARY KEY,
    subject_type TEXT NOT NULL,        -- e.g. comm_message | wf_email_message | rh_proposal
    subject_id TEXT NOT NULL,
    risk TEXT NOT NULL,                -- LOW | MEDIUM | HIGH
    reasons_json TEXT NOT NULL,
    actor TEXT NOT NULL,
    needs_aryan_id TEXT,               -- set when risk=HIGH
    final_action TEXT,                 -- set once a human decides/acts
    decided_by TEXT,
    decided_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_comm_risk_events_subject ON comm_risk_events(subject_type, subject_id);

-- Twenty Two Technologies -- Live Enquiry Activation V1 (falguna/tally_intake.py)
-- One row per Tally.so webhook/export payload processed (ingested, duplicate,
-- or rejected) -- the idempotency + audit + error-visibility record for the
-- real public website's four live forms. Never stores resume bytes or full
-- raw payload text, only field LABELS seen (for diagnosing field-mapping
-- drift) plus the outcome.
CREATE TABLE IF NOT EXISTS tally_intake_events (
    id TEXT PRIMARY KEY,
    form_id TEXT,                      -- Tally form id from the payload (e.g. VLgxN6)
    form_type TEXT NOT NULL,           -- general | project | careers | media | unknown
    tally_submission_id TEXT,          -- Tally's data.submissionId (idempotency key)
    tally_response_id TEXT,            -- Tally's data.responseId
    tally_event_id TEXT,               -- Tally's top-level eventId
    status TEXT NOT NULL,              -- ingested | duplicate | rejected
    reason TEXT,                       -- set when status != ingested
    conversation_id TEXT,
    enquiry_id TEXT,
    application_id TEXT,
    opportunity_id TEXT,
    raw_field_labels_json TEXT,
    consent_status TEXT,               -- Phase 1 R2: given | declined | NULL (form had no consent field to match)
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tally_intake_events_submission ON tally_intake_events(tally_submission_id);
CREATE INDEX IF NOT EXISTS idx_tally_intake_events_status ON tally_intake_events(status, created_at);


-- Sales Partner Pilot V1 (falguna/partner_management.py). Internal pilot
-- foundation only: no public signup, no payout execution, no payment
-- provider integration. Partner/referral/commission bookkeeping sits
-- alongside the existing Revenue & Delivery Engine (rh_opportunities,
-- rh_invoices) rather than duplicating it -- a referral links to a real
-- rh_opportunities row once attributed, and commission eligibility is
-- computed by reading (never writing) the linked rh_invoices row.
CREATE TABLE IF NOT EXISTS pm_partners (
    id TEXT PRIMARY KEY, full_name TEXT NOT NULL, organization_name TEXT, email TEXT NOT NULL,
    phone TEXT, region TEXT, country TEXT, service_categories_json TEXT, assigned_manager TEXT,
    verification_status TEXT NOT NULL, agreement_accepted INTEGER NOT NULL DEFAULT 0,
    agreement_accepted_at TEXT, agreement_reference TEXT, status TEXT NOT NULL,
    approved_at TEXT, approved_by TEXT, suspended_at TEXT, suspended_by TEXT, suspension_reason TEXT,
    terminated_at TEXT, terminated_by TEXT, termination_reason TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pm_partners_status ON pm_partners(status, created_at);
CREATE TABLE IF NOT EXISTS pm_partner_status_events (
    id TEXT PRIMARY KEY, partner_id TEXT NOT NULL REFERENCES pm_partners(id),
    from_status TEXT, to_status TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pm_partner_status_events_partner ON pm_partner_status_events(partner_id, created_at);
CREATE TABLE IF NOT EXISTS pm_referrals (
    id TEXT PRIMARY KEY, partner_id TEXT NOT NULL REFERENCES pm_partners(id),
    prospect_name TEXT NOT NULL, organization_name TEXT, contact_email TEXT, contact_phone TEXT,
    region TEXT, requested_service TEXT NOT NULL, estimated_value REAL, referral_source TEXT, notes TEXT,
    normalized_email TEXT, normalized_phone TEXT, normalized_domain TEXT, normalized_org TEXT,
    attribution_status TEXT NOT NULL, attribution_start TEXT, attribution_expiry TEXT,
    opportunity_id TEXT, duplicate_flag INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pm_referrals_partner ON pm_referrals(partner_id, created_at);
CREATE INDEX IF NOT EXISTS idx_pm_referrals_attribution ON pm_referrals(attribution_status);
CREATE INDEX IF NOT EXISTS idx_pm_referrals_email ON pm_referrals(normalized_email);
CREATE INDEX IF NOT EXISTS idx_pm_referrals_phone ON pm_referrals(normalized_phone);
CREATE INDEX IF NOT EXISTS idx_pm_referrals_domain ON pm_referrals(normalized_domain);
CREATE INDEX IF NOT EXISTS idx_pm_referrals_org ON pm_referrals(normalized_org);
CREATE TABLE IF NOT EXISTS pm_duplicate_reviews (
    id TEXT PRIMARY KEY, referral_id TEXT NOT NULL REFERENCES pm_referrals(id),
    competing_referral_ids_json TEXT NOT NULL, detected_reason TEXT, status TEXT NOT NULL,
    reviewer TEXT, decision TEXT, awarded_referral_id TEXT, resolution_reason TEXT,
    created_at TEXT NOT NULL, updated_at TEXT, resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_pm_duplicate_reviews_status ON pm_duplicate_reviews(status, created_at);
CREATE TABLE IF NOT EXISTS pm_commissions (
    id TEXT PRIMARY KEY, referral_id TEXT NOT NULL REFERENCES pm_referrals(id), partner_id TEXT NOT NULL REFERENCES pm_partners(id),
    opportunity_id TEXT, invoice_id TEXT, rate REAL NOT NULL, status TEXT NOT NULL,
    eligible_amount REAL NOT NULL DEFAULT 0, refunded_amount REAL NOT NULL DEFAULT 0, hold_reason TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pm_commissions_referral ON pm_commissions(referral_id);
CREATE INDEX IF NOT EXISTS idx_pm_commissions_partner ON pm_commissions(partner_id, status);
CREATE TABLE IF NOT EXISTS pm_commission_events (
    id TEXT PRIMARY KEY, commission_id TEXT NOT NULL REFERENCES pm_commissions(id),
    event_type TEXT NOT NULL, amount REAL, status_before TEXT, status_after TEXT, actor TEXT NOT NULL,
    evidence_json TEXT, reason TEXT, event_ref TEXT, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pm_commission_events_commission ON pm_commission_events(commission_id, created_at);

-- ============================================================
-- Phase 5 Sprint 1: Commercial Operating Foundation (falguna/commercial.py)
-- Added: Phase 5 Sprint 1 (Sell -> Accept -> Deliver -> Collect -> Retain).
-- Additive only, cs_ prefixed. Distinct from the public marketing
-- catalogue in site_services -- these are the internal sellable/
-- operational records (service catalogue, intake, foundations,
-- projects, disputes/refunds, unit economics).
-- ============================================================
CREATE TABLE IF NOT EXISTS cs_services (
    id TEXT PRIMARY KEY, service_key TEXT NOT NULL UNIQUE, category TEXT NOT NULL, title TEXT NOT NULL,
    customer_description TEXT NOT NULL, scope_boundaries TEXT, supported_regions_json TEXT,
    pricing_model TEXT NOT NULL, price_min REAL, price_max REAL, currency TEXT NOT NULL DEFAULT 'USD',
    delivery_mode TEXT NOT NULL, required_specialists_json TEXT, automation_eligible INTEGER NOT NULL DEFAULT 0,
    foundation_id TEXT REFERENCES cs_foundations(id), approval_status TEXT NOT NULL DEFAULT 'DRAFT',
    active INTEGER NOT NULL DEFAULT 0, actor TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    -- International Services V1 (Phase 5 Continuation, Section 4). All
    -- nullable: unknown/unassessed stays unknown, never a misleading
    -- default. regional_pricing_json is a JSON list of per-region bands,
    -- each PROPOSED (advisory, FALGUNA may suggest) or APPROVED (the one
    -- human-authorization gate; see ServiceCatalogStore.approve_regional_pricing).
    supported_languages_json TEXT, risk_level TEXT, regulated INTEGER, regulated_notes TEXT,
    baseline_complexity TEXT, standard_delivery_days INTEGER, standard_assumptions TEXT,
    qa_requirements TEXT, regional_pricing_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_cs_services_status ON cs_services(approval_status, active);
CREATE INDEX IF NOT EXISTS idx_cs_services_category ON cs_services(category);

-- Phase 5 Continuation, Section 14: Capability Registry V1
-- (falguna/capability_registry.py). One row per registered Workforce
-- worker, synced from the live orchestrator (introspected facts) plus a
-- small human-maintained profile overlay. Nullable overlay fields mean
-- "not yet assessed", never a false default.
CREATE TABLE IF NOT EXISTS cs_capabilities (
    id TEXT PRIMARY KEY, worker_name TEXT NOT NULL UNIQUE, worker_class TEXT NOT NULL,
    supported_task_types_json TEXT NOT NULL, auto_resumable_after_restart INTEGER NOT NULL DEFAULT 0,
    description TEXT, model_tool_dependencies_json TEXT, requires_specialist INTEGER,
    known_limitations TEXT, availability TEXT, cost_driver TEXT,
    actor TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cs_capabilities_worker ON cs_capabilities(worker_name);

-- Phase 5 Continuation, Section 22: Learning from Outcomes V1
-- (falguna/outcomes.py). One row per CLOSED cs_projects row, captured
-- automatically by ProjectStore.transition. Every field is derived from
-- that project's own history/economics -- nullable where no real
-- evidence exists (e.g. a service with no standard_delivery_days set).
CREATE TABLE IF NOT EXISTS cs_outcome_records (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL UNIQUE REFERENCES cs_projects(id),
    service_id TEXT REFERENCES cs_services(id), foundation_id TEXT REFERENCES cs_foundations(id),
    delivery_route TEXT NOT NULL, estimated_delivery_days INTEGER, actual_delivery_days REAL,
    qa_cycle_count INTEGER NOT NULL DEFAULT 0, dispute_count INTEGER NOT NULL DEFAULT 0,
    acceptance TEXT NOT NULL, quoted_value REAL, net_collected REAL,
    total_known_direct_cost REAL, gross_contribution REAL,
    actor TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cs_outcome_records_service ON cs_outcome_records(service_id, acceptance);

CREATE TABLE IF NOT EXISTS cs_foundations (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, category TEXT, description TEXT, repository_ref TEXT,
    supported_versions TEXT, service_categories_json TEXT, qa_requirements TEXT, known_limitations TEXT,
    reuse_restrictions TEXT, maturity TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cs_foundations_maturity ON cs_foundations(maturity, category);

CREATE TABLE IF NOT EXISTS cs_intakes (
    id TEXT PRIMARY KEY, customer_name TEXT NOT NULL, customer_contact TEXT, business_name TEXT, industry TEXT,
    region TEXT, country TEXT, requested_outcome TEXT NOT NULL, service_id TEXT REFERENCES cs_services(id),
    requirements TEXT, timeline TEXT, budget_amount REAL, budget_currency TEXT,
    regulatory_flags_json TEXT, required_integrations_json TEXT, assets_provided_json TEXT, complexity TEXT,
    qualification_status TEXT NOT NULL DEFAULT 'NEW', potential_value REAL, risks TEXT, recommended_route TEXT,
    ai_requirement_summary TEXT, ai_missing_questions_json TEXT, ai_proposed_scope TEXT,
    human_approved INTEGER NOT NULL DEFAULT 0, approved_by TEXT, approved_at TEXT,
    opportunity_id TEXT REFERENCES rh_opportunities(id), actor TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cs_intakes_status ON cs_intakes(qualification_status, created_at);

CREATE TABLE IF NOT EXISTS cs_intake_events (
    id TEXT PRIMARY KEY, intake_id TEXT NOT NULL REFERENCES cs_intakes(id), from_status TEXT, to_status TEXT NOT NULL,
    actor TEXT NOT NULL, reason TEXT, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cs_intake_events_intake ON cs_intake_events(intake_id, created_at);

CREATE TABLE IF NOT EXISTS cs_projects (
    id TEXT PRIMARY KEY, intake_id TEXT REFERENCES cs_intakes(id), service_id TEXT REFERENCES cs_services(id),
    opportunity_id TEXT REFERENCES rh_opportunities(id), foundation_id TEXT REFERENCES cs_foundations(id),
    client_id TEXT REFERENCES clients(id), delivery_route TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'SCOPED',
    mission_id TEXT, actor TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cs_projects_status ON cs_projects(status, created_at);
CREATE INDEX IF NOT EXISTS idx_cs_projects_opportunity ON cs_projects(opportunity_id);
CREATE TABLE IF NOT EXISTS cs_project_events (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES cs_projects(id), from_status TEXT, to_status TEXT NOT NULL,
    actor TEXT NOT NULL, reason TEXT, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cs_project_events_project ON cs_project_events(project_id, created_at);

CREATE TABLE IF NOT EXISTS cs_disputes (
    id TEXT PRIMARY KEY, invoice_id TEXT NOT NULL REFERENCES rh_invoices(id), project_id TEXT REFERENCES cs_projects(id),
    client_id TEXT, reason TEXT NOT NULL, evidence_json TEXT NOT NULL, amount_disputed REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'OPEN', reviewer TEXT, resolution TEXT, refund_amount REAL NOT NULL DEFAULT 0,
    commission_impact_json TEXT, actor TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_cs_disputes_status ON cs_disputes(status, created_at);
CREATE INDEX IF NOT EXISTS idx_cs_disputes_invoice ON cs_disputes(invoice_id);
CREATE TABLE IF NOT EXISTS cs_dispute_events (
    id TEXT PRIMARY KEY, dispute_id TEXT NOT NULL REFERENCES cs_disputes(id), from_status TEXT, to_status TEXT NOT NULL,
    actor TEXT NOT NULL, reason TEXT, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cs_dispute_events_dispute ON cs_dispute_events(dispute_id, created_at);

CREATE TABLE IF NOT EXISTS cs_project_costs (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES cs_projects(id), cost_category TEXT NOT NULL,
    amount REAL NOT NULL, currency TEXT NOT NULL DEFAULT 'USD', note TEXT, actor TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cs_project_costs_project ON cs_project_costs(project_id, cost_category);

-- ============================================================
-- Phase 5 Final Client Experience, Section 3/4/6/7: Customer
-- Language Understanding V1 (falguna/language.py). One durable record
-- per inbound customer message actually run through the interpreter --
-- never fabricated, never recomputed silently in place (a re-interpret
-- is a new row, so the original interpretation a human may have already
-- acted on is never rewritten out from under them).
-- ============================================================
CREATE TABLE IF NOT EXISTS comm_message_interpretations (
    id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL REFERENCES comm_messages(id),
    raw_text TEXT NOT NULL,
    detected_languages_json TEXT NOT NULL,
    normalized_english_summary TEXT,
    ambiguity_flags_json TEXT NOT NULL,
    unresolved_questions_json TEXT NOT NULL,
    confidence TEXT NOT NULL,
    inferred_requirement_candidates_json TEXT NOT NULL,
    clarification_needed INTEGER NOT NULL,
    unavailable INTEGER NOT NULL DEFAULT 0,
    unavailable_reason TEXT,
    model_provider TEXT,
    model_name TEXT,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_comm_message_interpretations_message ON comm_message_interpretations(message_id);

-- ============================================================
-- Phase 5 Final Client Experience, Section 20: Payment Communication
-- Bridge (falguna/payment_comms.py). A DRAFT-only customer-facing message
-- generated from a real, already-persisted BillingStore/DisputeStore
-- state transition -- never sent automatically, never claiming a status
-- (e.g. "payment received") the linked invoice/dispute row does not
-- itself actually show.
-- ============================================================
CREATE TABLE IF NOT EXISTS comm_payment_drafts (
    id TEXT PRIMARY KEY,
    invoice_id TEXT REFERENCES rh_invoices(id),
    dispute_id TEXT REFERENCES cs_disputes(id),
    event_type TEXT NOT NULL,
    comm_message_id TEXT REFERENCES comm_messages(id),
    evidence_json TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_comm_payment_drafts_invoice ON comm_payment_drafts(invoice_id);
CREATE INDEX IF NOT EXISTS idx_comm_payment_drafts_dispute ON comm_payment_drafts(dispute_id);

-- Phase 6 Commercial Platform foundation (sandbox/adapter mode only).
-- TTT HQ owns this state. No PAN/CVV/provider secret storage and no
-- provider execution path exists in this phase.
CREATE TABLE IF NOT EXISTS p6_commercial_identities (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, subject_type TEXT NOT NULL,
    subject_ref TEXT NOT NULL, display_name TEXT NOT NULL, role TEXT NOT NULL,
    permissions_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'ACTIVE', actor TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(organization_id, subject_type, subject_ref)
);
CREATE INDEX IF NOT EXISTS idx_p6_identity_org ON p6_commercial_identities(organization_id, role, status);

-- Phase 7 external application authentication. Accounts are provisioned by
-- trusted operators only and must point at an immutable Phase 6 identity.
-- Customer/partner authorization is therefore derived server-side from the
-- linked identity; no browser-supplied organization, role, or subject is
-- trusted as an access boundary.
CREATE TABLE IF NOT EXISTS p7_external_accounts (
    id TEXT PRIMARY KEY, identity_id TEXT NOT NULL UNIQUE REFERENCES p6_commercial_identities(id),
    email TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'ACTIVE',
    failed_login_count INTEGER NOT NULL DEFAULT 0, locked_until TEXT,
    last_login_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS p7_external_sessions (
    id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES p7_external_accounts(id),
    csrf_token TEXT NOT NULL, ip_hash TEXT, user_agent TEXT, expires_at TEXT NOT NULL,
    revoked_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_p7_external_sessions_account ON p7_external_sessions(account_id, expires_at);
CREATE TABLE IF NOT EXISTS p6_payment_intents (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, customer_ref TEXT NOT NULL,
    invoice_id TEXT, kind TEXT NOT NULL, amount REAL NOT NULL, currency TEXT NOT NULL,
    status TEXT NOT NULL, capture_mode TEXT NOT NULL, allowed_methods_json TEXT NOT NULL,
    provider TEXT NOT NULL, provider_session_ref TEXT, provider_transaction_ref TEXT,
    payment_method_token_ref TEXT, mandate_token_ref TEXT, milestone_ref TEXT,
    legal_owner_name TEXT NOT NULL, beneficiary_ref TEXT NOT NULL,
    idempotency_key TEXT NOT NULL, metadata_json TEXT NOT NULL, actor TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(organization_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_p6_payment_org ON p6_payment_intents(organization_id, status, created_at);
CREATE TABLE IF NOT EXISTS p6_payment_events (
    id TEXT PRIMARY KEY, payment_intent_id TEXT NOT NULL REFERENCES p6_payment_intents(id),
    organization_id TEXT NOT NULL, event_type TEXT NOT NULL, status_before TEXT,
    status_after TEXT NOT NULL, amount REAL, currency TEXT, evidence_json TEXT NOT NULL,
    provider_event_ref TEXT, idempotency_key TEXT NOT NULL, actor TEXT NOT NULL,
    created_at TEXT NOT NULL, UNIQUE(payment_intent_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_p6_payment_events_intent ON p6_payment_events(payment_intent_id, created_at);
CREATE TABLE IF NOT EXISTS p6_webhook_events (
    id TEXT PRIMARY KEY, provider TEXT NOT NULL, provider_event_ref TEXT NOT NULL,
    payment_intent_id TEXT REFERENCES p6_payment_intents(id), organization_id TEXT NOT NULL,
    signature_verified INTEGER NOT NULL, payload_hash TEXT NOT NULL, processing_status TEXT NOT NULL,
    failure_reason TEXT, received_at TEXT NOT NULL, created_at TEXT NOT NULL,
    UNIQUE(provider, provider_event_ref)
);
CREATE TABLE IF NOT EXISTS p6_approval_requests (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, action_type TEXT NOT NULL,
    target_type TEXT NOT NULL, target_id TEXT NOT NULL, amount REAL, currency TEXT,
    beneficiary_ref TEXT, risk_flags_json TEXT NOT NULL, payload_json TEXT NOT NULL,
    status TEXT NOT NULL, maker_identity_id TEXT NOT NULL, verifier_identity_id TEXT,
    final_approver_identity_id TEXT, maker_at TEXT NOT NULL, verified_at TEXT,
    approved_at TEXT, rejection_reason TEXT, execution_reference TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_p6_approvals_org ON p6_approval_requests(organization_id, status, created_at);
CREATE TABLE IF NOT EXISTS p6_approval_events (
    id TEXT PRIMARY KEY, approval_request_id TEXT NOT NULL REFERENCES p6_approval_requests(id),
    organization_id TEXT NOT NULL, event_type TEXT NOT NULL, status_before TEXT,
    status_after TEXT NOT NULL, actor_identity_id TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL, details_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_p6_approval_events_request ON p6_approval_events(approval_request_id, created_at);
CREATE TABLE IF NOT EXISTS p6_financial_events (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, event_type TEXT NOT NULL,
    account_bucket TEXT NOT NULL, direction TEXT NOT NULL, amount REAL NOT NULL,
    currency TEXT NOT NULL, source_type TEXT NOT NULL, source_id TEXT NOT NULL,
    evidence_json TEXT NOT NULL, idempotency_key TEXT NOT NULL, actor TEXT NOT NULL,
    reverses_event_id TEXT REFERENCES p6_financial_events(id), created_at TEXT NOT NULL,
    UNIQUE(organization_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_p6_finance_org ON p6_financial_events(organization_id, currency, created_at);
CREATE TABLE IF NOT EXISTS p6_reserve_policies (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL UNIQUE, currency TEXT NOT NULL,
    essential_monthly_burn REAL NOT NULL DEFAULT 0, minimum_runway_months REAL NOT NULL DEFAULT 3,
    target_runway_months REAL NOT NULL DEFAULT 6, operating_reserve_override REAL,
    policy_notes TEXT, actor TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS p6_reconciliations (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, invoice_id TEXT,
    payment_intent_id TEXT, provider_transaction_ref TEXT, settlement_ref TEXT,
    expected_amount REAL, settled_amount REAL, expected_currency TEXT, settled_currency TEXT,
    status TEXT NOT NULL, findings_json TEXT NOT NULL, evidence_json TEXT NOT NULL,
    reviewed_by_identity_id TEXT, idempotency_key TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(organization_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_p6_reconciliation_org ON p6_reconciliations(organization_id, status, created_at);
CREATE TABLE IF NOT EXISTS p6_receipts (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, invoice_id TEXT NOT NULL,
    payment_intent_id TEXT NOT NULL, reconciliation_id TEXT NOT NULL,
    amount REAL NOT NULL, currency TEXT NOT NULL, provider_transaction_ref TEXT NOT NULL,
    status TEXT NOT NULL, issued_at TEXT NOT NULL, created_at TEXT NOT NULL,
    UNIQUE(reconciliation_id)
);
CREATE INDEX IF NOT EXISTS idx_p6_receipt_org ON p6_receipts(organization_id, invoice_id, created_at);
CREATE TABLE IF NOT EXISTS p6_commission_plans (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, name TEXT NOT NULL,
    rate REAL NOT NULL, recurring_enabled INTEGER NOT NULL DEFAULT 0,
    lifetime_originator_enabled INTEGER NOT NULL DEFAULT 0,
    excluded_pass_through_types_json TEXT NOT NULL, status TEXT NOT NULL,
    actor TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(organization_id, name)
);
CREATE TABLE IF NOT EXISTS p6_partner_contributions (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, partner_id TEXT NOT NULL,
    referral_id TEXT, opportunity_id TEXT, contribution_type TEXT NOT NULL,
    evidence_json TEXT NOT NULL, status TEXT NOT NULL, actor TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_p6_contribution_partner ON p6_partner_contributions(partner_id, created_at);
CREATE TABLE IF NOT EXISTS p6_risk_events (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, event_type TEXT NOT NULL,
    severity TEXT NOT NULL, partner_id TEXT, customer_ref TEXT, project_id TEXT,
    payment_intent_id TEXT, evidence_json TEXT NOT NULL, status TEXT NOT NULL,
    source TEXT NOT NULL, reviewer_identity_id TEXT, current_action TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_p6_risk_org ON p6_risk_events(organization_id, status, severity, created_at);
CREATE TABLE IF NOT EXISTS p6_risk_event_actions (
    id TEXT PRIMARY KEY, risk_event_id TEXT NOT NULL REFERENCES p6_risk_events(id),
    organization_id TEXT NOT NULL, action TEXT NOT NULL, status_before TEXT,
    status_after TEXT NOT NULL, actor_identity_id TEXT NOT NULL,
    reason TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_p6_risk_actions_event ON p6_risk_event_actions(risk_event_id, created_at);
CREATE TABLE IF NOT EXISTS p6_commission_releases (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, commission_id TEXT NOT NULL,
    approval_request_id TEXT NOT NULL, cleared_collections REAL NOT NULL,
    excluded_amount REAL NOT NULL, refund_chargeback_amount REAL NOT NULL,
    eligible_base REAL NOT NULL, rate REAL NOT NULL, commission_amount REAL NOT NULL,
    currency TEXT NOT NULL, status TEXT NOT NULL, idempotency_key TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(organization_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_p6_commission_release ON p6_commission_releases(commission_id, status);
CREATE TABLE IF NOT EXISTS p6_refunds (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, payment_intent_id TEXT NOT NULL,
    invoice_id TEXT NOT NULL, approval_request_id TEXT NOT NULL, amount REAL NOT NULL,
    currency TEXT NOT NULL, eligibility_basis_json TEXT NOT NULL, reason TEXT NOT NULL,
    status TEXT NOT NULL, provider_ref TEXT, evidence_json TEXT,
    idempotency_key TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(organization_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_p6_refunds_payment ON p6_refunds(payment_intent_id, status);
CREATE TABLE IF NOT EXISTS p6_subscriptions (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, client_id TEXT NOT NULL,
    opportunity_id TEXT, plan_name TEXT NOT NULL, amount REAL NOT NULL, currency TEXT NOT NULL,
    cadence TEXT NOT NULL, next_billing_date TEXT NOT NULL, provider_token_ref TEXT,
    mandate_ref TEXT, autopay_status TEXT NOT NULL, status TEXT NOT NULL,
    last_invoice_id TEXT, retry_count INTEGER NOT NULL DEFAULT 0,
    actor TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_p6_subscriptions_due ON p6_subscriptions(status, next_billing_date);
CREATE TABLE IF NOT EXISTS p6_subscription_cycles (
    id TEXT PRIMARY KEY, subscription_id TEXT NOT NULL, cycle_date TEXT NOT NULL,
    invoice_id TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(subscription_id, cycle_date)
);
CREATE TABLE IF NOT EXISTS p6_subscription_attempts (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, subscription_id TEXT NOT NULL,
    invoice_id TEXT NOT NULL, payment_intent_id TEXT, cycle_date TEXT NOT NULL,
    attempt_number INTEGER NOT NULL, status TEXT NOT NULL, failure_reason TEXT,
    idempotency_key TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(organization_id, idempotency_key),
    UNIQUE(subscription_id, cycle_date, attempt_number)
);
CREATE INDEX IF NOT EXISTS idx_p6_subscription_attempt ON p6_subscription_attempts(subscription_id, cycle_date, status);
CREATE TABLE IF NOT EXISTS p6_refund_reconciliations (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, refund_id TEXT NOT NULL UNIQUE,
    payment_intent_id TEXT NOT NULL, invoice_id TEXT NOT NULL, provider_ref TEXT NOT NULL UNIQUE,
    amount REAL NOT NULL, currency TEXT NOT NULL, status TEXT NOT NULL,
    evidence_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_p6_refund_recon_org ON p6_refund_reconciliations(organization_id, invoice_id, status);
CREATE TABLE IF NOT EXISTS p6_opportunity_economics (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, opportunity_id TEXT NOT NULL,
    source_channel TEXT NOT NULL, source_metadata_json TEXT NOT NULL, origin_partner_id TEXT,
    quoted_value REAL, contracted_value REAL, estimated_delivery_cost REAL,
    known_delivery_cost REAL, cost_evidence_json TEXT, gateway_fee REAL NOT NULL DEFAULT 0,
    service_tier TEXT, actor TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(organization_id, opportunity_id)
);
CREATE INDEX IF NOT EXISTS idx_p6_economics_source ON p6_opportunity_economics(organization_id, source_channel);
CREATE TABLE IF NOT EXISTS p6_payables (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, payable_type TEXT NOT NULL,
    beneficiary_ref TEXT NOT NULL, amount REAL NOT NULL, currency TEXT NOT NULL,
    linked_project_id TEXT, linked_budget_id TEXT, expense_metadata_json TEXT NOT NULL,
    approval_request_id TEXT NOT NULL, status TEXT NOT NULL, idempotency_key TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(organization_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_p6_payables_org ON p6_payables(organization_id, status, created_at);

-- Phase 8 Digital Business Ecosystem. Additive, local/synthetic-safe records.
CREATE TABLE IF NOT EXISTS p8_intakes (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, customer_ref TEXT,
    original_message TEXT NOT NULL, normalized_meaning TEXT NOT NULL,
    source_language TEXT, geography TEXT, preferred_language TEXT,
    category TEXT, industry TEXT, budget_min REAL, budget_max REAL, currency TEXT,
    timing TEXT, risk_flags_json TEXT NOT NULL, clarification_questions_json TEXT NOT NULL,
    status TEXT NOT NULL, created_by_identity_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_p8_intakes_owner ON p8_intakes(organization_id, customer_ref, status, created_at);
CREATE TABLE IF NOT EXISTS p8_routing_decisions (
    id TEXT PRIMARY KEY, intake_id TEXT NOT NULL REFERENCES p8_intakes(id),
    recommended_mode TEXT, decided_mode TEXT, recommendation_reasons_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL, uncertainty_json TEXT NOT NULL, review_status TEXT NOT NULL,
    recommended_by TEXT NOT NULL, decided_by_identity_id TEXT, decision_reason TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_p8_route_intake ON p8_routing_decisions(intake_id, created_at);
CREATE TABLE IF NOT EXISTS p8_network_profiles (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, profile_type TEXT NOT NULL,
    display_name TEXT NOT NULL, legal_name TEXT, partner_id TEXT, geography_json TEXT NOT NULL,
    languages_json TEXT NOT NULL, availability_status TEXT NOT NULL,
    verification_level TEXT NOT NULL, verification_evidence_json TEXT NOT NULL,
    licensing_status TEXT NOT NULL, licensing_evidence_json TEXT NOT NULL,
    commercial_relationship TEXT NOT NULL, conflict_disclosures_json TEXT NOT NULL,
    maturity_tier TEXT, status TEXT NOT NULL, completed_assignments INTEGER NOT NULL DEFAULT 0,
    qa_passes INTEGER NOT NULL DEFAULT 0, disputes INTEGER NOT NULL DEFAULT 0,
    policy_violations INTEGER NOT NULL DEFAULT 0, review_status TEXT, review_reason TEXT, reviewed_by_identity_id TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_p8_profiles_eligible ON p8_network_profiles(organization_id, status, verification_level, availability_status);
CREATE TABLE IF NOT EXISTS p8_profile_capabilities (
    id TEXT PRIMARY KEY, profile_id TEXT NOT NULL REFERENCES p8_network_profiles(id),
    capability_tag TEXT NOT NULL, evidence_status TEXT NOT NULL, evidence_json TEXT NOT NULL,
    regulated INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(profile_id, capability_tag)
);
CREATE TABLE IF NOT EXISTS p8_opportunities (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, intake_id TEXT NOT NULL REFERENCES p8_intakes(id),
    customer_safe_brief TEXT NOT NULL, geography TEXT, language TEXT, category TEXT, industry TEXT,
    capability_requirements_json TEXT NOT NULL, budget_visibility TEXT NOT NULL,
    budget_min REAL, budget_max REAL, currency TEXT, timing TEXT,
    risk_flags_json TEXT NOT NULL, required_verification_level TEXT NOT NULL,
    application_state TEXT NOT NULL, assignment_state TEXT NOT NULL,
    customer_relationship_owner TEXT NOT NULL, status TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_p8_opportunity_feed ON p8_opportunities(organization_id, status, assignment_state, created_at);
CREATE TABLE IF NOT EXISTS p8_matches (
    id TEXT PRIMARY KEY, opportunity_id TEXT NOT NULL REFERENCES p8_opportunities(id),
    profile_id TEXT NOT NULL REFERENCES p8_network_profiles(id), eligible INTEGER NOT NULL,
    reasons_json TEXT NOT NULL, gaps_json TEXT NOT NULL, conflicts_json TEXT NOT NULL,
    economics_review_required INTEGER NOT NULL, review_status TEXT, review_reason TEXT, reviewed_by_identity_id TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(opportunity_id, profile_id)
);
CREATE TABLE IF NOT EXISTS p8_opportunity_applications (
    id TEXT PRIMARY KEY, opportunity_id TEXT NOT NULL REFERENCES p8_opportunities(id),
    profile_id TEXT NOT NULL REFERENCES p8_network_profiles(id), statement TEXT NOT NULL,
    status TEXT NOT NULL, conflict_disclosure TEXT, review_reason TEXT, reviewed_by_identity_id TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(opportunity_id, profile_id)
);
CREATE TABLE IF NOT EXISTS p8_assignments (
    id TEXT PRIMARY KEY, opportunity_id TEXT NOT NULL REFERENCES p8_opportunities(id),
    profile_id TEXT NOT NULL REFERENCES p8_network_profiles(id), application_id TEXT,
    status TEXT NOT NULL, scope TEXT NOT NULL, customer_contact_allowed INTEGER NOT NULL DEFAULT 0,
    money_collection_allowed INTEGER NOT NULL DEFAULT 0, assigned_by_identity_id TEXT NOT NULL,
    approval_evidence_json TEXT NOT NULL, reassignment_reason TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS p8_blg_engagements (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, intake_id TEXT NOT NULL REFERENCES p8_intakes(id),
    tier TEXT NOT NULL, current_stage TEXT NOT NULL, regulated_boundaries_json TEXT NOT NULL,
    outcome_disclaimer TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS p8_blg_stage_events (
    id TEXT PRIMARY KEY, engagement_id TEXT NOT NULL REFERENCES p8_blg_engagements(id),
    from_stage TEXT, to_stage TEXT NOT NULL, evidence_json TEXT NOT NULL,
    actor_identity_id TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS p8_governance_events (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, event_type TEXT NOT NULL,
    profile_id TEXT, opportunity_id TEXT, severity TEXT NOT NULL, evidence_json TEXT NOT NULL,
    status TEXT NOT NULL, recommended_action TEXT, decided_action TEXT,
    decided_by_identity_id TEXT, decision_reason TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS p8_product_signals (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, problem_signature TEXT NOT NULL,
    supporting_intake_ids_json TEXT NOT NULL, signal_type TEXT NOT NULL,
    evidence_count INTEGER NOT NULL, recommendation_only INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL, review_reason TEXT, reviewed_by_identity_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
