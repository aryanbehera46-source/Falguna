"""Tests for falguna/ventures.py -- TTT Venture Studio / Multi-Venture
Operating System v1: venture lifecycle, parent governance, capital
isolation, profitability, experiments, validation, resource allocation,
scale/pause/kill recommendations, graveyard, asset register, shared
capability allocation, Needs Aryan escalation, Command Center rollup, and
cross-venture isolation.
"""

import tempfile
import unittest
from pathlib import Path

from falguna.audit import AuditLog
from falguna.capital_and_risk import RiskRegisterStore
from falguna.finance_ledger import LedgerStore
from falguna.goals import GoalStore
from falguna.sales_ops import ClientStore
from falguna.revenue_hunter import OpportunityStore
from falguna.store import StateStore
from falguna.ttt_hq import NeedsAryanQueue, NEEDS_ARYAN_KINDS
from falguna.workforce import WorkforceTaskStore
from falguna.ventures import (
    ALLOWED_VENTURE_TRANSITIONS, AssetRegisterStore, CapitalAllocationStore,
    ExperimentStore, GraveyardStore, LARGE_ALLOCATION_THRESHOLD,
    RelationshipStore, ResourceAllocationStore, VentureError, VentureStore,
    ValidationStore, command_center_venture_rollup, recommend_venture_action,
    venture_capital_account, venture_scorecard,
)


class VenturesBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = StateStore(self.root / "state.db")
        self.store.migrate()
        self.audit = AuditLog(self.root / "audit.jsonl")
        self.needs_aryan = NeedsAryanQueue(self.store, self.audit)
        self.ventures = VentureStore(self.store, self.audit, self.needs_aryan)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _create(self, name="Test Venture", **kwargs):
        result = self.ventures.create(name, kwargs.pop("venture_type", "saas"), actor="Aryan", **kwargs)
        return result["venture_id"]


class VentureLifecycleTests(VenturesBase):
    def test_create_requires_name_and_valid_type(self):
        with self.assertRaises(VentureError):
            self.ventures.create("", "saas", actor="Aryan")
        with self.assertRaises(VentureError):
            self.ventures.create("X", "not_a_type", actor="Aryan")

    def test_create_starts_in_idea_status_with_parent_company(self):
        venture_id = self._create()
        venture = self.ventures.get(venture_id)
        self.assertEqual(venture["status"], "IDEA")
        self.assertEqual(venture["parent_company"], "Twenty Two Technologies Pvt. Ltd.")

    def test_slug_is_derived_and_unique(self):
        vid1 = self._create("Acme Rockets")
        v1 = self.ventures.get(vid1)
        self.assertEqual(v1["slug"], "acme-rockets")
        with self.assertRaises(VentureError):
            self.ventures.create("Acme Rockets", "saas", actor="Aryan")

    def test_allowed_transition_graph_is_explicit_and_checked(self):
        venture_id = self._create()
        # IDEA -> BUILDING directly is not allowed (must go via VALIDATING)
        with self.assertRaises(VentureError):
            self.ventures.transition(venture_id, "BUILDING", "Aryan")
        self.ventures.transition(venture_id, "VALIDATING", "Aryan", reason="starting research")
        self.ventures.transition(venture_id, "BUILDING", "Aryan", reason="approved")
        venture = self.ventures.get(venture_id)
        self.assertEqual(venture["status"], "BUILDING")

    def test_terminal_statuses_have_no_outgoing_transitions(self):
        self.assertEqual(ALLOWED_VENTURE_TRANSITIONS["CLOSED"], set())
        self.assertEqual(ALLOWED_VENTURE_TRANSITIONS["REJECTED"], set())

    def test_every_transition_is_recorded_with_history(self):
        venture_id = self._create()
        self.ventures.transition(venture_id, "VALIDATING", "Aryan", reason="r1")
        self.ventures.transition(venture_id, "REJECTED", "Aryan", reason="no demand")
        events = self.ventures.status_events(venture_id)
        self.assertEqual(len(events), 3)  # created + 2 transitions
        self.assertEqual(events[-1]["to_status"], "REJECTED")
        self.assertEqual(events[-1]["reason"], "no demand")

    def test_launch_transition_escalates_to_needs_aryan(self):
        venture_id = self._create()
        self.ventures.transition(venture_id, "VALIDATING", "Aryan")
        self.ventures.transition(venture_id, "BUILDING", "Aryan")
        self.ventures.transition(venture_id, "PRELAUNCH", "Aryan")
        result = self.ventures.transition(venture_id, "ACTIVE", "Aryan", reason="ready to launch")
        events = self.ventures.status_events(venture_id)
        launch_event = events[-1]
        self.assertIsNotNone(launch_event["needs_aryan_id"])
        pending = self.needs_aryan.list_pending()
        kinds = {i["kind"] for i in pending}
        self.assertIn("venture_launch_approval", kinds)
        self.assertIsNotNone(result.get("launch_date"))

    def test_pause_transition_escalates_pause_recommendation_kind(self):
        venture_id = self._create()
        self.ventures.transition(venture_id, "VALIDATING", "Aryan")
        self.ventures.transition(venture_id, "BUILDING", "Aryan")
        self.ventures.transition(venture_id, "PAUSED", "Aryan", reason="pausing for now")
        pending = self.needs_aryan.list_pending()
        kinds = {i["kind"] for i in pending}
        self.assertIn("venture_pause_recommendation", kinds)

    def test_non_trivial_capital_commitment_on_creation_escalates(self):
        result = self.ventures.create(
            "Big Bet", "ai_product", actor="Aryan", thesis="big thesis",
            initial_capital_commitment=LARGE_ALLOCATION_THRESHOLD,
        )
        self.assertIsNotNone(result["needs_aryan_id"])
        pending = self.needs_aryan.list_pending()
        kinds = {i["kind"] for i in pending}
        self.assertIn("venture_creation_approval", kinds)

    def test_small_capital_commitment_does_not_escalate(self):
        result = self.ventures.create("Small Bet", "saas", actor="Aryan", initial_capital_commitment=10.0)
        self.assertIsNone(result["needs_aryan_id"])

    def test_pipeline_groups_ventures_by_stage_and_preserves_rejected(self):
        idea_id = self._create("Idea Venture")
        rejected_id = self._create("Rejected Venture")
        self.ventures.transition(rejected_id, "REJECTED", "Aryan", reason="not viable")
        pipeline = self.ventures.pipeline()
        self.assertIn(idea_id, [v["id"] for v in pipeline["incoming_ideas"]])
        self.assertIn(rejected_id, [v["id"] for v in pipeline["rejected"]])
        # preserved, not deleted
        self.assertIsNotNone(self.ventures.get(rejected_id))


class ValidationEngineTests(VenturesBase):
    def test_signal_type_and_strength_are_validated(self):
        venture_id = self._create()
        validation = ValidationStore(self.store, self.audit)
        with self.assertRaises(VentureError):
            validation.add_signal(venture_id, "not_a_type", "desc")
        with self.assertRaises(VentureError):
            validation.add_signal(venture_id, "waitlist_signup", "desc", strength="extreme")

    def test_does_not_require_all_signal_types_and_never_fabricates_a_score(self):
        venture_id = self._create()
        validation = ValidationStore(self.store, self.audit)
        validation.add_signal(venture_id, "waitlist_signup", "120 signups", strength="moderate")
        summary = validation.summary(venture_id)
        self.assertEqual(summary["signal_count"], 1)
        self.assertNotIn("score", summary)

    def test_zero_signals_never_blocks_a_transition(self):
        venture_id = self._create()
        # No validation signals recorded at all -- transition still succeeds
        # (Section 8: "do not require all signals").
        result = self.ventures.transition(venture_id, "VALIDATING", "Aryan")
        self.assertEqual(result["status"], "VALIDATING")


class ExperimentTests(VenturesBase):
    def test_create_requires_hypothesis_and_metric(self):
        venture_id = self._create()
        experiments = ExperimentStore(self.store, self.audit)
        with self.assertRaises(VentureError):
            experiments.create(venture_id, "", "signups", 100)
        with self.assertRaises(VentureError):
            experiments.create(venture_id, "people want X", "", 100)

    def test_result_requires_evidence_and_valid_decision(self):
        venture_id = self._create()
        experiments = ExperimentStore(self.store, self.audit)
        exp_id = experiments.create(venture_id, "people want X", "signups", 100, budget=500.0)
        experiments.start(exp_id, "Aryan")
        with self.assertRaises(VentureError):
            experiments.record_result(exp_id, "Aryan", evidence=None, result="failed", decision="KILL")
        with self.assertRaises(VentureError):
            experiments.record_result(exp_id, "Aryan", evidence={"signups": 3}, result="failed", decision="NOT_A_DECISION")
        completed = experiments.record_result(exp_id, "Aryan", evidence={"signups": 3}, result="only 3 signups", decision="KILL")
        self.assertEqual(completed["status"], "COMPLETED")
        self.assertEqual(completed["decision"], "KILL")


class CapitalIsolationTests(VenturesBase):
    """Section 26/27: financial safety and data isolation -- the heart of
    the Venture Studio spec."""

    def test_allocation_requires_source_note_and_positive_amount(self):
        venture_id = self._create()
        allocations = CapitalAllocationStore(self.store, self.audit, self.needs_aryan)
        with self.assertRaises(VentureError):
            allocations.allocate(venture_id, 100.0, actor="Aryan", source_note="")
        with self.assertRaises(VentureError):
            allocations.allocate(venture_id, -5.0, actor="Aryan", source_note="ok")

    def test_large_allocation_escalates_to_needs_aryan(self):
        venture_id = self._create()
        allocations = CapitalAllocationStore(self.store, self.audit, self.needs_aryan)
        result = allocations.allocate(venture_id, LARGE_ALLOCATION_THRESHOLD, actor="Aryan", source_note="owner contribution")
        self.assertIsNotNone(result["needs_aryan_id"])
        pending = self.needs_aryan.list_pending()
        self.assertTrue(any(i["kind"] == "venture_budget_allocation" for i in pending))

    def test_small_allocation_does_not_escalate(self):
        venture_id = self._create()
        allocations = CapitalAllocationStore(self.store, self.audit, self.needs_aryan)
        result = allocations.allocate(venture_id, 100.0, actor="Aryan", source_note="small top-up")
        self.assertIsNone(result["needs_aryan_id"])

    def test_reallocation_writes_a_paired_logged_record_on_both_sides(self):
        v1 = self._create("Venture One")
        v2 = self._create("Venture Two")
        allocations = CapitalAllocationStore(self.store, self.audit, self.needs_aryan)
        allocations.allocate(v1, 5000.0, actor="Aryan", source_note="initial funding")
        result = allocations.reallocate(v1, v2, 2000.0, "Aryan", "shifting capital to Venture Two")
        self.assertIsNotNone(result["out_allocation_id"])
        self.assertIsNotNone(result["in_allocation_id"])
        self.assertEqual(allocations.net_allocated(v1), 3000.0)
        self.assertEqual(allocations.net_allocated(v2), 2000.0)
        # cross-venture reallocation always escalates
        self.assertIsNotNone(result["needs_aryan_id"])

    def test_cannot_reallocate_to_self(self):
        v1 = self._create()
        allocations = CapitalAllocationStore(self.store, self.audit, self.needs_aryan)
        with self.assertRaises(VentureError):
            allocations.reallocate(v1, v1, 100.0, "Aryan", "nonsense")

    def test_capital_account_never_includes_trading_lab_tables(self):
        venture_id = self._create()
        # Seed a Trading Lab paper account with real cash/equity directly
        # into tl_paper_accounts -- if venture_capital_account ever read
        # from (or summed in) any tl_* table, this would leak into the
        # venture's figures below.
        from falguna.store import utcnow as _utcnow
        self.store.create("tl_paper_accounts", {
            "name": "unrelated paper account", "starting_cash": 999999.0, "cash": 999999.0,
            "status": "ACTIVE", "actor": "Aryan", "created_at": _utcnow(), "updated_at": _utcnow(),
        })
        venture = self.ventures.get(venture_id)
        account = venture_capital_account(self.store, venture)
        self.assertEqual(account["actual"]["revenue_total"], 0)
        self.assertEqual(account["actual"]["spend_to_date"], 0)
        self.assertEqual(account["cash_allocation"], 0)

    def test_capital_account_reflects_real_ledger_entries_never_a_stored_balance(self):
        venture_id = self._create()
        ledger = LedgerStore(self.store, self.audit)
        ledger.record("INFLOW", "owner_contribution", 1000.0, "wire confirmation", actor="Aryan", venture_id=venture_id)
        ledger.record("OUTFLOW", "software_tooling", 200.0, "receipt #1", actor="Aryan", venture_id=venture_id)
        venture = self.ventures.get(venture_id)
        account = venture_capital_account(self.store, venture)
        self.assertEqual(account["actual"]["revenue_ledger"], 1000.0)
        self.assertEqual(account["actual"]["spend_to_date"], 200.0)

    def test_cross_venture_ledger_isolation(self):
        """Two ventures' ledger entries never bleed into each other's
        capital account -- the core of Section 27's data isolation."""
        v1 = self._create("Venture One")
        v2 = self._create("Venture Two")
        ledger = LedgerStore(self.store, self.audit)
        ledger.record("INFLOW", "owner_contribution", 5000.0, "wire A", actor="Aryan", venture_id=v1)
        ledger.record("INFLOW", "owner_contribution", 700.0, "wire B", actor="Aryan", venture_id=v2)
        account1 = venture_capital_account(self.store, self.ventures.get(v1))
        account2 = venture_capital_account(self.store, self.ventures.get(v2))
        self.assertEqual(account1["actual"]["revenue_ledger"], 5000.0)
        self.assertEqual(account2["actual"]["revenue_ledger"], 700.0)

    def test_ledger_entry_without_venture_id_is_company_wide_and_excluded_from_venture_accounts(self):
        venture_id = self._create()
        ledger = LedgerStore(self.store, self.audit)
        ledger.record("INFLOW", "owner_contribution", 999.0, "company wide", actor="Aryan")
        account = venture_capital_account(self.store, self.ventures.get(venture_id))
        self.assertEqual(account["actual"]["revenue_ledger"], 0.0)


class ResourceAllocationTests(VenturesBase):
    def test_request_validates_department_and_resource_type(self):
        venture_id = self._create()
        resources = ResourceAllocationStore(self.store, self.audit)
        with self.assertRaises(VentureError):
            resources.request(venture_id, "Not A Department", "engineering_capacity", 5)
        with self.assertRaises(VentureError):
            resources.request(venture_id, "Falguna Engineering", "not_a_resource", 5)

    def test_allocation_within_capacity_succeeds(self):
        venture_id = self._create()
        resources = ResourceAllocationStore(self.store, self.audit)
        request_id = resources.request(venture_id, "Falguna Engineering", "engineering_capacity", 10, actor="Aryan")
        result = resources.allocate(request_id, "Aryan")
        self.assertEqual(result["status"], "ALLOCATED")

    def test_conflicting_requests_surface_a_conflict_not_silent_overallocation(self):
        v1 = self._create("Venture One")
        v2 = self._create("Venture Two")
        resources = ResourceAllocationStore(self.store, self.audit)
        # Falguna Engineering capacity is 20/week (DEFAULT_WEEKLY_CAPACITY)
        r1 = resources.request(v1, "Falguna Engineering", "engineering_capacity", 15, actor="Aryan")
        resources.allocate(r1, "Aryan")
        r2 = resources.request(v2, "Falguna Engineering", "engineering_capacity", 10, actor="Aryan")
        result = resources.allocate(r2, "Aryan")
        self.assertEqual(result["status"], "CONFLICT")
        self.assertIsNotNone(result["conflict_with_json"])


class AssetRegisterTests(VenturesBase):
    def test_metadata_only_rejects_secret_looking_keys(self):
        venture_id = self._create()
        assets = AssetRegisterStore(self.store, self.audit)
        with self.assertRaises(VentureError):
            assets.register(venture_id, "code", "repo", metadata={"api_key": "sk-live-xxx"})
        with self.assertRaises(VentureError):
            assets.register(venture_id, "code", "repo", metadata={"password": "hunter2"})

    def test_ordinary_metadata_is_accepted(self):
        venture_id = self._create()
        assets = AssetRegisterStore(self.store, self.audit)
        asset_id = assets.register(venture_id, "domain", "acme.com", metadata={"registrar": "namecheap"})
        listed = assets.list(venture_id)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["id"], asset_id)


class RelationshipTests(VenturesBase):
    def test_cannot_link_venture_to_itself(self):
        v1 = self._create()
        relationships = RelationshipStore(self.store, self.audit)
        with self.assertRaises(VentureError):
            relationships.link(v1, v1, "shared_technology")

    def test_link_is_visible_from_both_sides(self):
        v1 = self._create("Venture One")
        v2 = self._create("Venture Two")
        relationships = RelationshipStore(self.store, self.audit)
        relationships.link(v1, v2, "shared_technology", description="same auth stack")
        self.assertEqual(len(relationships.list_for_venture(v1)), 1)
        self.assertEqual(len(relationships.list_for_venture(v2)), 1)


class RecommendationEngineTests(VenturesBase):
    def test_kill_decision_experiment_drives_kill_recommendation_and_escalates(self):
        venture_id = self._create()
        self.ventures.transition(venture_id, "VALIDATING", "Aryan")
        experiments = ExperimentStore(self.store, self.audit)
        exp_id = experiments.create(venture_id, "people want X", "signups", 100)
        experiments.start(exp_id, "Aryan")
        experiments.record_result(exp_id, "Aryan", evidence={"signups": 1}, result="basically no signal", decision="KILL")
        venture = self.ventures.get(venture_id)
        rec = recommend_venture_action(self.store, venture, actor="system", needs_aryan=self.needs_aryan)
        self.assertEqual(rec["recommendation"], "KILL")
        self.assertIsNotNone(rec["needs_aryan_id"])

    def test_healthy_new_venture_recommends_continue_and_does_not_escalate(self):
        venture_id = self._create()
        venture = self.ventures.get(venture_id)
        rec = recommend_venture_action(self.store, venture, actor="system", needs_aryan=self.needs_aryan)
        self.assertEqual(rec["recommendation"], "CONTINUE")
        self.assertIsNone(rec["needs_aryan_id"])

    def test_recommendation_never_mutates_venture_status(self):
        venture_id = self._create()
        self.ventures.transition(venture_id, "VALIDATING", "Aryan")
        experiments = ExperimentStore(self.store, self.audit)
        exp_id = experiments.create(venture_id, "x", "y", 1)
        experiments.start(exp_id, "Aryan")
        experiments.record_result(exp_id, "Aryan", evidence={"a": 1}, result="bad", decision="KILL")
        venture = self.ventures.get(venture_id)
        recommend_venture_action(self.store, venture, actor="system", needs_aryan=self.needs_aryan)
        # a KILL recommendation never closes the venture itself
        self.assertEqual(self.ventures.get(venture_id)["status"], "VALIDATING")


class ScorecardTests(VenturesBase):
    def test_bands_are_descriptive_never_a_fabricated_numeric_score(self):
        venture_id = self._create()
        venture = self.ventures.get(venture_id)
        scorecard = venture_scorecard(self.store, venture)
        for dimension in ("execution", "financials", "traction", "risks", "milestones"):
            self.assertIn(scorecard[dimension]["band"], ("healthy", "watch", "at_risk", "critical"))
        self.assertNotIn("score", str(list(scorecard.keys())))

    def test_open_critical_risk_drives_critical_risk_band(self):
        venture_id = self._create()
        risks = RiskRegisterStore(self.store, self.audit, needs_aryan=self.needs_aryan)
        risks.create("Key contractor might leave", "delivery_risk", "critical", actor="Aryan", venture_id=venture_id)
        venture = self.ventures.get(venture_id)
        scorecard = venture_scorecard(self.store, venture)
        self.assertEqual(scorecard["risks"]["band"], "critical")
        # a critical venture risk uses the venture-specific escalation kind
        pending = self.needs_aryan.list_pending()
        self.assertTrue(any(i["kind"] == "venture_risk_escalation" for i in pending))


class GraveyardTests(VenturesBase):
    def _build_and_pause(self, name, **kwargs):
        venture_id = self._create(name, **kwargs)
        # Graveyard is for a venture that was actually built/running, not a
        # bare IDEA -- IDEA/VALIDATING ideas that don't pan out are
        # REJECTED, never CLOSED (see ALLOWED_VENTURE_TRANSITIONS). Move it
        # through the real lifecycle first.
        self.ventures.transition(venture_id, "VALIDATING", "Aryan")
        self.ventures.transition(venture_id, "BUILDING", "Aryan")
        return venture_id

    def test_close_venture_transitions_status_and_writes_graveyard_record(self):
        venture_id = self._build_and_pause("Doomed Venture", thesis="everyone wants a fax-machine SaaS")
        graveyard = GraveyardStore(self.store, self.audit, self.ventures)
        record = graveyard.close_venture(venture_id, "no product-market fit after 3 experiments", "Aryan", lessons="validate demand before building")
        venture = self.ventures.get(venture_id)
        self.assertEqual(venture["status"], "CLOSED")
        self.assertEqual(record["reason_killed"], "no product-market fit after 3 experiments")
        self.assertEqual(len(graveyard.list()), 1)

    def test_reason_killed_is_required(self):
        venture_id = self._build_and_pause("Some Venture")
        graveyard = GraveyardStore(self.store, self.audit, self.ventures)
        with self.assertRaises(VentureError):
            graveyard.close_venture(venture_id, "", "Aryan")

    def test_check_similar_thesis_prevents_repeated_waste(self):
        v1 = self._build_and_pause("Fax SaaS One", thesis="a modern cloud-native SaaS for sending fax machine documents")
        graveyard = GraveyardStore(self.store, self.audit, self.ventures)
        graveyard.close_venture(v1, "no demand for fax SaaS at all", "Aryan")
        hits = graveyard.check_similar_thesis("a cloud-native SaaS platform for sending fax machine documents to offices")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["venture_id"], v1)


class WorkforceIsolationTests(VenturesBase):
    def test_workforce_task_is_tagged_with_parent_venture(self):
        v1 = self._create("Venture One")
        v2 = self._create("Venture Two")
        tasks = WorkforceTaskStore(self.store, self.audit)
        t1 = tasks.create("Digital Workforce", "research competitors", "research", venture_id=v1)
        t2 = tasks.create("Digital Workforce", "draft onboarding doc", "document", venture_id=v2)
        v1_tasks = tasks.list(venture_id=v1)
        v2_tasks = tasks.list(venture_id=v2)
        self.assertEqual([t["id"] for t in v1_tasks], [t1])
        self.assertEqual([t["id"] for t in v2_tasks], [t2])


class GoalsIntegrationTests(VenturesBase):
    def test_venture_goal_rolls_up_and_is_isolated_per_venture(self):
        v1 = self._create("Venture One")
        v2 = self._create("Venture Two")
        goals = GoalStore(self.store, self.audit)
        goals.create("Hit $10k MRR", 10000, "USD", venture_id=v1)
        goals.create("Sign 5 pilot customers", 5, "customers", venture_id=v2)
        self.assertEqual(len(goals.list(venture_id=v1)), 1)
        self.assertEqual(len(goals.list(venture_id=v2)), 1)
        # company-wide goal list (no venture filter) still sees both
        self.assertEqual(len(goals.list()), 2)


class CommandCenterRollupTests(VenturesBase):
    def test_rollup_counts_ventures_by_status_and_excludes_closed_from_totals(self):
        v1 = self._create("Active-ish Venture")
        self.ventures.transition(v1, "VALIDATING", "Aryan")
        v2 = self._create("Closed Venture")
        self.ventures.transition(v2, "VALIDATING", "Aryan")
        self.ventures.transition(v2, "BUILDING", "Aryan")
        graveyard = GraveyardStore(self.store, self.audit, self.ventures)
        graveyard.close_venture(v2, "pivoted away", "Aryan")

        ledger = LedgerStore(self.store, self.audit)
        ledger.record("INFLOW", "owner_contribution", 500.0, "wire", actor="Aryan", venture_id=v1)
        ledger.record("INFLOW", "owner_contribution", 999999.0, "should be excluded", actor="Aryan", venture_id=v2)

        rollup = command_center_venture_rollup(self.store)
        self.assertEqual(rollup["venture_count"], 2)
        self.assertEqual(rollup["by_status"]["VALIDATING"], 1)
        self.assertEqual(rollup["by_status"]["CLOSED"], 1)
        # closed venture's revenue must never leak into the company total
        self.assertEqual(rollup["venture_revenue_total"], 500.0)

    def test_pending_pause_kill_recommendation_surfaces_venture_requiring_decision(self):
        v1 = self._create("Shaky Venture")
        self.ventures.transition(v1, "VALIDATING", "Aryan")
        experiments = ExperimentStore(self.store, self.audit)
        exp_id = experiments.create(v1, "x", "y", 1)
        experiments.start(exp_id, "Aryan")
        experiments.record_result(exp_id, "Aryan", evidence={"a": 1}, result="bad", decision="KILL")
        venture = self.ventures.get(v1)
        recommend_venture_action(self.store, venture, actor="system", needs_aryan=self.needs_aryan)
        rollup = command_center_venture_rollup(self.store)
        self.assertTrue(any(d["venture_id"] == v1 for d in rollup["ventures_requiring_decision"]))


class NeedsAryanKindRegistrationTests(VenturesBase):
    def test_all_venture_kinds_are_registered(self):
        expected = {
            "venture_creation_approval", "venture_budget_allocation", "venture_launch_approval",
            "venture_resource_shift_approval", "venture_pause_recommendation", "venture_kill_recommendation",
            "venture_risk_escalation", "venture_large_spend_override",
        }
        self.assertTrue(expected.issubset(NEEDS_ARYAN_KINDS))


class OpportunityLinkageTests(VenturesBase):
    def test_opportunity_can_be_linked_to_a_venture_for_sales_integration(self):
        venture_id = self._create("Services Venture", venture_type="software_services")
        clients = ClientStore(self.store, self.audit)
        clients.upsert("Acme Corp", actor="Aryan")
        opportunities = OpportunityStore(self.store, self.audit)
        opp_id = opportunities.create({"title": "Acme build"}, actor="Aryan", source="referral")
        self.store.update("rh_opportunities", opp_id, venture_id=venture_id)
        linked = self.store.list("rh_opportunities", "venture_id=?", (venture_id,))
        self.assertEqual([o["id"] for o in linked], [opp_id])


if __name__ == "__main__":
    unittest.main()
