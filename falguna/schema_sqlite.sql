CREATE TABLE IF NOT EXISTS missions (id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
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
