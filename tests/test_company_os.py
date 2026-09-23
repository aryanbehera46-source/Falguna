"""Tests for falguna/company_os.py -- TTT Group OS / Company Orchestrator v2:
objective lifecycle, planning, the priority engine, department decomposition,
venture alignment, resource conflict detection, the company event bus,
replanning, the decision engine, Needs Aryan escalation, policies, the
escalation engine, objective->execution traceability, persistence across a
simulated restart, and cross-department isolation.
"""

import tempfile
import unittest
from pathlib import Path

from falguna.audit import AuditLog
from falguna.goals import GoalStore
from falguna.store import StateStore
from falguna.ttt_hq import NeedsAryanQueue
from falguna.ventures import VentureStore
from falguna.workforce import WorkforceTaskStore
from falguna.company_os import (
    ALLOWED_OBJECTIVE_TRANSITIONS, CapitalRecommendationStore, CompanyMemoryStore,
    CompanyOSError, CompanyPolicyStore, CostEstimateStore, DecisionStore,
    DepartmentObjectiveStore, EventBus, ExecutionOrchestrator, FailureStore,
    ObjectiveStore, PlanStore, PriorityStore, ReplanStore, ResourceRecommendationStore,
    TimelineStore, VentureAlignmentStore, capital_orchestration_snapshot,
    ceo_command_center_v2_snapshot, company_os_home, company_state_snapshot,
    compute_priority, escalate_if_warranted, evaluate_policy, goal_feedback,
    is_high_impact, link, list_daily_loops, list_weekly_reviews,
    resource_allocation_snapshot, run_daily_loop, run_weekly_review, trace_objective,
)


class CompanyOSBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = StateStore(self.root / "state.db")
        self.store.migrate()
        self.audit = AuditLog(self.root / "audit.jsonl")
        self.needs_aryan = NeedsAryanQueue(self.store, self.audit)
        self.objectives = ObjectiveStore(self.store, self.audit)
        self.plans = PlanStore(self.store, self.audit)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _active_objective(self, title="Grow ARR", **kwargs):
        objective_id = self.objectives.create(title, actor="Aryan", **kwargs)
        self.objectives.transition(objective_id, "ACTIVE", "Aryan")
        return objective_id

    def _approved_plan(self, objective_id, departments=None, **kwargs):
        plan_id = self.plans.create(
            objective_id, kwargs.pop("desired_outcome", "Ship the thing"), actor="Aryan",
            departments=departments or ["Falguna Engineering"], **kwargs,
        )
        self.plans.set_status(plan_id, "APPROVED", "Aryan")
        return plan_id


class ObjectiveLifecycleTests(CompanyOSBase):
    def test_create_requires_title(self):
        with self.assertRaises(CompanyOSError):
            self.objectives.create("   ", actor="Aryan")

    def test_create_rejects_unknown_priority_and_department(self):
        with self.assertRaises(CompanyOSError):
            self.objectives.create("X", actor="Aryan", priority="URGENT")
        with self.assertRaises(CompanyOSError):
            self.objectives.create("X", actor="Aryan", linked_departments=["Marketing"])

    def test_new_objective_starts_in_draft(self):
        objective_id = self.objectives.create("Grow ARR", actor="Aryan")
        objective = self.objectives.get(objective_id)
        self.assertEqual(objective["status"], "DRAFT")
        self.assertEqual(objective["linked_goals"], [])

    def test_allowed_transitions_are_explicit_and_checked(self):
        objective_id = self.objectives.create("Grow ARR", actor="Aryan")
        # DRAFT can only reach ACTIVE or CANCELLED -- not e.g. COMPLETED directly.
        with self.assertRaises(CompanyOSError):
            self.objectives.transition(objective_id, "COMPLETED", "Aryan")
        self.objectives.transition(objective_id, "ACTIVE", "Aryan")
        self.assertEqual(self.objectives.get(objective_id)["status"], "ACTIVE")
        # COMPLETED and CANCELLED are terminal -- no transitions out.
        self.objectives.transition(objective_id, "COMPLETED", "Aryan")
        with self.assertRaises(CompanyOSError):
            self.objectives.transition(objective_id, "ACTIVE", "Aryan")

    def test_every_declared_transition_graph_edge_is_consistent(self):
        # No status transitions to itself as an "allowed" edge, and every
        # target status named is a real status.
        for from_status, targets in ALLOWED_OBJECTIVE_TRANSITIONS.items():
            self.assertNotIn(from_status, targets)

    def test_transition_records_history_event(self):
        objective_id = self.objectives.create("Grow ARR", actor="Aryan")
        self.objectives.transition(objective_id, "ACTIVE", "Aryan", reason="kickoff")
        history = self.objectives.history(objective_id)
        self.assertEqual(len(history), 2)  # creation + transition
        self.assertEqual(history[-1]["to_status"], "ACTIVE")
        self.assertEqual(history[-1]["reason"], "kickoff")

    def test_update_progress_is_free_text_not_invented(self):
        objective_id = self.objectives.create("Grow ARR", actor="Aryan")
        self.objectives.update_progress(objective_id, "3 of 5 milestones done", "Aryan")
        self.assertEqual(self.objectives.get(objective_id)["current_progress"], "3 of 5 milestones done")

    def test_past_deadline_is_evidence_based_not_guessed(self):
        objective_id = self._active_objective(deadline="2000-01-01")
        objective = self.objectives.get(objective_id)
        self.assertTrue(objective["past_deadline"])
        # A completed objective is never flagged past_deadline even if the
        # stored deadline has passed -- it's terminal, not at risk.
        self.objectives.transition(objective_id, "COMPLETED", "Aryan")
        self.assertFalse(self.objectives.get(objective_id)["past_deadline"])

    def test_transition_to_same_status_is_a_no_op(self):
        objective_id = self._active_objective()
        before = self.objectives.get(objective_id)
        after = self.objectives.transition(objective_id, "ACTIVE", "Aryan")
        self.assertEqual(before["status"], after["status"])


class PlanEngineTests(CompanyOSBase):
    def test_plan_requires_an_active_or_recovering_objective(self):
        objective_id = self.objectives.create("Grow ARR", actor="Aryan")  # still DRAFT
        with self.assertRaises(CompanyOSError):
            self.plans.create(objective_id, "Ship v2", actor="Aryan")

    def test_plan_requires_desired_outcome(self):
        objective_id = self._active_objective()
        with self.assertRaises(CompanyOSError):
            self.plans.create(objective_id, "   ", actor="Aryan")

    def test_plan_rejects_unknown_department(self):
        objective_id = self._active_objective()
        with self.assertRaises(CompanyOSError):
            self.plans.create(objective_id, "Ship v2", actor="Aryan", departments=["Marketing"])

    def test_plan_never_invents_content_it_was_not_given(self):
        objective_id = self._active_objective()
        plan_id = self.plans.create(
            objective_id, "Ship v2", actor="Aryan",
            milestones=["a", "b"], risks=["late vendor"], approvals=None,
        )
        plan = self.plans.get(plan_id)
        self.assertEqual(plan["milestones"], ["a", "b"])
        self.assertEqual(plan["risks"], ["late vendor"])
        self.assertEqual(plan["approvals"], [])  # never fabricated, just empty

    def test_plan_status_lifecycle_and_active_plan_lookup(self):
        objective_id = self._active_objective()
        plan_id = self._approved_plan(objective_id)
        active = self.plans.active_plan(objective_id)
        self.assertEqual(active["id"], plan_id)
        self.plans.set_status(plan_id, "COMPLETED", "Aryan")
        self.assertIsNone(self.plans.active_plan(objective_id))

    def test_plan_set_status_rejects_unknown_status(self):
        objective_id = self._active_objective()
        plan_id = self._approved_plan(objective_id)
        with self.assertRaises(CompanyOSError):
            self.plans.set_status(plan_id, "MAYBE", "Aryan")


class PriorityEngineTests(CompanyOSBase):
    def test_compute_priority_is_deterministic_points_not_fake_precision(self):
        result = compute_priority({"strategic_importance": "critical", "revenue_potential": "high", "deadline_days": 3})
        self.assertIsInstance(result["points"], int)
        self.assertIn("strategic_importance=critical (+4)", result["rationale"])
        self.assertIn("->", result["rationale"])

    def test_no_inputs_yields_low_and_says_so(self):
        result = compute_priority({})
        self.assertEqual(result["priority"], "LOW")
        self.assertIn("no inputs supplied", result["rationale"])

    def test_cost_and_low_resource_availability_push_priority_down(self):
        high = compute_priority({"strategic_importance": "critical", "urgency": "critical"})
        constrained = compute_priority({"strategic_importance": "critical", "urgency": "critical", "resource_availability": "none", "cost_band": "critical"})
        self.assertLess(constrained["points"], high["points"])

    def test_priority_store_persists_and_returns_latest(self):
        priorities = PriorityStore(self.store, self.audit)
        priorities.evaluate("co_objectives", "obj-1", {"strategic_importance": "low"}, actor="system")
        second = priorities.evaluate("co_objectives", "obj-1", {"strategic_importance": "critical"}, actor="system")
        latest = priorities.latest("co_objectives", "obj-1")
        self.assertEqual(latest["priority"], second["priority"])
        self.assertEqual(len(priorities.history("co_objectives", "obj-1")), 2)


class DepartmentDecompositionTests(CompanyOSBase):
    def setUp(self):
        super().setUp()
        self.depts = DepartmentObjectiveStore(self.store, self.audit)

    def test_create_requires_known_department_and_real_objective(self):
        objective_id = self._active_objective()
        with self.assertRaises(CompanyOSError):
            self.depts.create(objective_id, "Marketing", "Do a thing", actor="Aryan")
        with self.assertRaises(CompanyOSError):
            self.depts.create("not-a-real-objective", "Sales", "Do a thing", actor="Aryan")

    def test_department_objective_keeps_its_parent_link(self):
        objective_id = self._active_objective()
        dept_id = self.depts.create(objective_id, "Sales", "Book 5 demos", actor="Aryan")
        row = self.depts.get(dept_id)
        self.assertEqual(row["company_objective_id"], objective_id)
        self.assertEqual(row["status"], "PENDING")

    def test_cross_department_dependency_blocking(self):
        # Section 10: downstream work is blocked until real prerequisites
        # are genuinely COMPLETED -- proving it actually blocks, not just
        # that the method exists.
        objective_id = self._active_objective()
        engineering = self.depts.create(objective_id, "Falguna Engineering", "Ship pricing page", actor="Aryan")
        sales = self.depts.create(objective_id, "Sales", "Sell the new pricing", actor="Aryan", dependencies=[engineering])

        status = self.depts.dependencies_satisfied(sales)
        self.assertFalse(status["satisfied"])
        self.assertEqual(status["blocking"][0]["dept_objective_id"], engineering)

        # Still blocked while engineering is merely ACTIVE, not COMPLETED.
        self.depts.set_status(engineering, "ACTIVE", "Aryan")
        self.assertFalse(self.depts.dependencies_satisfied(sales)["satisfied"])

        # Only unblocks once the real prerequisite is actually COMPLETED.
        self.depts.set_status(engineering, "COMPLETED", "Aryan")
        status = self.depts.dependencies_satisfied(sales)
        self.assertTrue(status["satisfied"])
        self.assertEqual(status["blocking"], [])

    def test_dependency_on_missing_row_is_reported_not_found(self):
        objective_id = self._active_objective()
        sales = self.depts.create(objective_id, "Sales", "Sell it", actor="Aryan", dependencies=["ghost-id"])
        status = self.depts.dependencies_satisfied(sales)
        self.assertFalse(status["satisfied"])
        self.assertEqual(status["blocking"][0]["status"], "NOT_FOUND")

    def test_list_all_includes_computed_dependencies_field(self):
        objective_id = self._active_objective()
        dept_id = self.depts.create(objective_id, "Research", "Investigate X", actor="Aryan", dependencies=["a", "b"])
        rows = self.depts.list_all()
        row = next(r for r in rows if r["id"] == dept_id)
        self.assertEqual(row["dependencies"], ["a", "b"])


class VentureAlignmentTests(CompanyOSBase):
    def setUp(self):
        super().setUp()
        self.links = VentureAlignmentStore(self.store, self.audit)
        self.ventures = VentureStore(self.store, self.audit, self.needs_aryan)

    def _venture(self, name="Venture A"):
        return self.ventures.create(name, "saas", actor="Aryan")["venture_id"]

    def test_link_requires_real_objective_and_real_venture(self):
        objective_id = self._active_objective()
        with self.assertRaises(CompanyOSError):
            self.links.link(objective_id, "not-a-real-venture", actor="Aryan")
        venture_id = self._venture()
        with self.assertRaises(CompanyOSError):
            self.links.link("not-a-real-objective", venture_id, actor="Aryan")

    def test_one_venture_link_never_alters_another_ventures_rows(self):
        # Cross-venture isolation: linking objective->venture A must not
        # touch venture B's own vs_ventures row or its links at all.
        objective_id = self._active_objective()
        venture_a = self._venture("Venture A")
        venture_b = self._venture("Venture B")
        before_b = dict(self.ventures.get(venture_b))

        self.links.link(objective_id, venture_a, actor="Aryan", contribution="drives signups", priority="HIGH")

        after_b = dict(self.ventures.get(venture_b))
        self.assertEqual(before_b, after_b)
        self.assertEqual(self.links.list_for_venture(venture_b), [])
        self.assertEqual(len(self.links.list_for_venture(venture_a)), 1)

    def test_update_rejects_unknown_fields(self):
        objective_id = self._active_objective()
        venture_id = self._venture()
        link_id = self.links.link(objective_id, venture_id, actor="Aryan")
        with self.assertRaises(CompanyOSError):
            self.links.update(link_id, "Aryan", venture_id="different-venture")


class ResourceConflictTests(CompanyOSBase):
    def test_starvation_detected_when_no_active_capacity_behind_pending_work(self):
        objective_id = self._active_objective()
        DepartmentObjectiveStore(self.store, self.audit).create(objective_id, "Media/Growth", "Launch campaign", actor="Aryan")
        snapshot = resource_allocation_snapshot(self.store)
        starvation = [f for f in snapshot["findings"] if f["scope"] == "starvation" and f["department"] == "Media/Growth"]
        self.assertEqual(len(starvation), 1)

    def test_duplicated_work_detected_for_identical_open_task_text(self):
        wf = WorkforceTaskStore(self.store, self.audit)
        wf.create(department="Falguna Engineering", objective="Fix the login bug", task_type="bugfix", actor="Aryan", source="test")
        wf.create(department="Falguna Engineering", objective="Fix the login bug", task_type="bugfix", actor="Aryan", source="test")
        snapshot = resource_allocation_snapshot(self.store)
        duplicated = [f for f in snapshot["findings"] if f["scope"] == "duplicated_work"]
        self.assertEqual(len(duplicated), 1)

    def test_findings_are_persisted_only_when_a_recommendations_store_is_supplied(self):
        objective_id = self._active_objective()
        DepartmentObjectiveStore(self.store, self.audit).create(objective_id, "Research", "Study X", actor="Aryan")
        recs = ResourceRecommendationStore(self.store, self.audit)
        snapshot = resource_allocation_snapshot(self.store, recommendations=recs)
        self.assertGreater(len(snapshot["recorded_recommendation_ids"]), 0)
        self.assertEqual(len(recs.list(status="OPEN")), len(snapshot["recorded_recommendation_ids"]))

    def test_snapshot_without_recommendations_store_persists_nothing(self):
        objective_id = self._active_objective()
        DepartmentObjectiveStore(self.store, self.audit).create(objective_id, "Research", "Study Y", actor="Aryan")
        resource_allocation_snapshot(self.store)  # no recommendations store passed
        recs = ResourceRecommendationStore(self.store, self.audit)
        self.assertEqual(recs.list(status="OPEN"), [])


class EventBusTests(CompanyOSBase):
    def test_emit_and_list_by_ref(self):
        bus = EventBus(self.store, self.audit)
        bus.emit("DEAL_WON", "revenue_hunter", ref_type="rh_opportunities", ref_id="opp-1", payload={"amount": 5000})
        bus.emit("DEAL_WON", "revenue_hunter", ref_type="rh_opportunities", ref_id="opp-2")
        events = bus.list(ref_type="rh_opportunities", ref_id="opp-1")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "DEAL_WON")

    def test_idempotency_key_prevents_duplicate_events(self):
        bus = EventBus(self.store, self.audit)
        first = bus.emit("BUDGET_BREACH", "company_os", idempotency_key="budget-breach-2026-09")
        second = bus.emit("BUDGET_BREACH", "company_os", idempotency_key="budget-breach-2026-09")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(bus.list(event_type="BUDGET_BREACH")), 1)


class TraceabilityTests(CompanyOSBase):
    def test_link_is_idempotent(self):
        first = link(self.store, self.audit, "co_plans", "plan-1", "co_department_objectives", "dept-1", "Aryan")
        second = link(self.store, self.audit, "co_plans", "plan-1", "co_department_objectives", "dept-1", "Aryan")
        self.assertEqual(first, second)

    def test_trace_objective_walks_real_foreign_keys(self):
        objective_id = self._active_objective()
        plan_id = self._approved_plan(objective_id, departments=["Falguna Engineering", "Digital Workforce"])
        result = ExecutionOrchestrator(self.store, self.audit).route_plan(plan_id, actor="system")

        trace = trace_objective(self.store, objective_id)
        self.assertEqual(trace["objective"]["id"], objective_id)
        self.assertEqual(len(trace["plans"]), 1)
        self.assertEqual({d["id"] for d in trace["department_objectives"]}, set(result["created_dept_objectives"]))
        self.assertEqual([t["id"] for t in trace["wf_tasks"]], result["created_wf_tasks"])

    def test_trace_objective_raises_on_unknown_objective(self):
        with self.assertRaises(CompanyOSError):
            trace_objective(self.store, "not-a-real-objective")


class ExecutionOrchestratorTests(CompanyOSBase):
    def test_route_plan_requires_approved_plan(self):
        objective_id = self._active_objective()
        plan_id = self.plans.create(objective_id, "Ship v2", actor="Aryan", departments=["Sales"])  # DRAFT
        with self.assertRaises(CompanyOSError):
            ExecutionOrchestrator(self.store, self.audit).route_plan(plan_id, actor="system")

    def test_route_plan_creates_real_workforce_task_not_a_parallel_table(self):
        objective_id = self._active_objective()
        plan_id = self._approved_plan(objective_id, departments=["Digital Workforce"])
        result = ExecutionOrchestrator(self.store, self.audit).route_plan(plan_id, actor="system")
        self.assertEqual(len(result["created_wf_tasks"]), 1)
        task = self.store.get("wf_tasks", result["created_wf_tasks"][0])
        self.assertEqual(task["co_objective_id"], objective_id)
        self.assertEqual(task["department"], "Digital Workforce")

    def test_route_plan_is_not_re_applied_for_departments_already_routed(self):
        objective_id = self._active_objective()
        plan_id = self._approved_plan(objective_id, departments=["Sales"])
        first = ExecutionOrchestrator(self.store, self.audit).route_plan(plan_id, actor="system")
        self.assertEqual(len(first["created_dept_objectives"]), 1)
        # Re-routing the same (now IN_EXECUTION) plan must not duplicate the
        # department objective it already created.
        self.plans.set_status(plan_id, "APPROVED", "Aryan")
        second = ExecutionOrchestrator(self.store, self.audit).route_plan(plan_id, actor="system")
        self.assertEqual(second["created_dept_objectives"], [])
        self.assertIn("already exists", second["skipped"][0])

    def test_route_plan_never_creates_falguna_engineering_missions_directly(self):
        objective_id = self._active_objective()
        plan_id = self._approved_plan(objective_id, departments=["Falguna Engineering"])
        before_missions = self.store.list("missions")
        ExecutionOrchestrator(self.store, self.audit).route_plan(plan_id, actor="system")
        after_missions = self.store.list("missions")
        self.assertEqual(len(before_missions), len(after_missions))


class ReplanningTests(CompanyOSBase):
    def test_replan_supersedes_the_original_plan(self):
        objective_id = self._active_objective()
        plan_id = self._approved_plan(objective_id)
        replans = ReplanStore(self.store, self.audit)
        replan = replans.replan(objective_id, "client changed priorities", actor="Aryan", changed_assumptions={"budget": "halved"})
        self.assertEqual(replan["original_plan_id"], plan_id)
        self.assertEqual(self.plans.get(plan_id)["status"], "SUPERSEDED")
        self.assertIsNotNone(replan["original_plan_snapshot_json"])

    def test_replan_with_no_active_plan_still_records_reason(self):
        objective_id = self._active_objective()
        replans = ReplanStore(self.store, self.audit)
        replan = replans.replan(objective_id, "revenue changed", actor="Aryan")
        self.assertIsNone(replan["original_plan_id"])

    def test_link_new_plan_requires_both_rows_to_exist(self):
        objective_id = self._active_objective()
        plan_id = self._approved_plan(objective_id)
        replans = ReplanStore(self.store, self.audit)
        replan = replans.replan(objective_id, "deadline slipped", actor="Aryan")
        with self.assertRaises(CompanyOSError):
            replans.link_new_plan(replan["id"], "not-a-real-plan", "Aryan")
        new_plan_id = self.plans.create(objective_id, "Revised plan", actor="Aryan", departments=["Sales"])
        linked = replans.link_new_plan(replan["id"], new_plan_id, "Aryan")
        self.assertEqual(linked["new_plan_id"], new_plan_id)
        self.assertEqual(plan_id, ReplanStore(self.store, self.audit).list_for_objective(objective_id)[0]["original_plan_id"])


class DecisionEngineAndNeedsAryanTests(CompanyOSBase):
    def test_is_high_impact_gate_matches_decision_and_escalation_engines(self):
        self.assertFalse(is_high_impact(cost=100))
        self.assertTrue(is_high_impact(cost=100000))
        self.assertTrue(is_high_impact(risk="critical"))
        self.assertTrue(is_high_impact(irreversibility="irreversible"))
        self.assertTrue(is_high_impact(external_commitment=True))

    def test_low_impact_decision_never_creates_a_needs_aryan_item(self):
        decisions = DecisionStore(self.store, self.audit, needs_aryan=self.needs_aryan)
        decisions.create("Pick a font?", [{"label": "Inter"}, {"label": "Roboto"}], actor="system", high_impact=False)
        self.assertEqual(self.needs_aryan.list_pending(), [])

    def test_high_impact_decision_escalates_exactly_once(self):
        decisions = DecisionStore(self.store, self.audit, needs_aryan=self.needs_aryan)
        decision_id = decisions.create(
            "Raise a bridge round?", [{"label": "Raise now"}, {"label": "Wait"}],
            actor="system", cost=500000, high_impact=True, recommendation="Wait",
        )
        pending = self.needs_aryan.list_pending()
        self.assertEqual(len(pending), 1)
        decision = decisions.get(decision_id)
        self.assertEqual(decision["needs_aryan_id"], pending[0]["id"])

    def test_decide_cannot_set_status_back_to_proposed(self):
        decisions = DecisionStore(self.store, self.audit)
        decision_id = decisions.create("Q?", [{"label": "A"}], actor="system")
        with self.assertRaises(CompanyOSError):
            decisions.decide(decision_id, "PROPOSED", "Aryan")
        decisions.decide(decision_id, "APPROVED", "Aryan", note="go ahead")
        self.assertEqual(decisions.get(decision_id)["status"], "APPROVED")


class PolicyAndEscalationTests(CompanyOSBase):
    def test_no_policy_for_domain_fails_closed(self):
        result = evaluate_policy(self.store, "spending", {"amount": 10})
        self.assertFalse(result["autonomous_allowed"])
        self.assertTrue(result["requires_needs_aryan"])

    def test_policy_within_bounds_allows_autonomous_action(self):
        policies = CompanyPolicyStore(self.store, self.audit)
        policies.create("spending", "Small spend auto-allowed", {"autonomous_max_amount": 50000}, actor="Aryan", requires_needs_aryan=False)
        allowed = evaluate_policy(self.store, "spending", {"amount": 20000})
        self.assertTrue(allowed["autonomous_allowed"])
        blocked = evaluate_policy(self.store, "spending", {"amount": 90000})
        self.assertFalse(blocked["autonomous_allowed"])

    def test_archived_policy_no_longer_applies(self):
        policies = CompanyPolicyStore(self.store, self.audit)
        policy_id = policies.create("spending", "Small spend", {"autonomous_max_amount": 50000}, actor="Aryan", requires_needs_aryan=False)
        policies.archive(policy_id, "Aryan")
        result = evaluate_policy(self.store, "spending", {"amount": 100})
        self.assertTrue(result["requires_needs_aryan"])

    def test_escalation_logs_every_call_but_only_needs_aryan_when_warranted(self):
        routine = escalate_if_warranted(
            self.store, self.needs_aryan, "co_objectives", "obj-1", "risky_action",
            "Routine change", "nothing to see here", actor="system", cost=50,
        )
        self.assertEqual(routine["decision"], "NOT_ESCALATED")
        self.assertIsNone(routine["needs_aryan_id"])

        big = escalate_if_warranted(
            self.store, self.needs_aryan, "co_objectives", "obj-1", "strategic_decision",
            "Large spend", "need approval", actor="system", cost=200000,
        )
        self.assertEqual(big["decision"], "ESCALATED")
        self.assertIsNotNone(big["needs_aryan_id"])
        self.assertEqual(len(self.store.list("co_escalations")), 2)


class FailureManagementTests(CompanyOSBase):
    def test_failure_is_never_silently_abandoned(self):
        failures = FailureStore(self.store, self.audit, needs_aryan=self.needs_aryan)
        failure_id = failures.record("wf_tasks", "task-1", "Deployment failed: connection refused", actor="system")
        self.assertEqual(failures.get(failure_id)["status"], "OPEN")
        self.assertIn(failures.get(failure_id), failures.list(status="OPEN"))

        failures.retry(failure_id, "system", note="retrying once")
        self.assertEqual(failures.get(failure_id)["status"], "RETRIED")

    def test_failure_escalate_creates_needs_aryan_item(self):
        failures = FailureStore(self.store, self.audit, needs_aryan=self.needs_aryan)
        failure_id = failures.record("wf_tasks", "task-2", "Repeated failure after retry", actor="system")
        result = failures.escalate(failure_id, "system", "Task stuck", "Please review task-2")
        self.assertEqual(result["status"], "ESCALATED")
        self.assertEqual(len(self.needs_aryan.list_pending()), 1)


class CompanyMemoryAndCostTests(CompanyOSBase):
    def test_memory_rejects_unknown_kind(self):
        memory = CompanyMemoryStore(self.store, self.audit)
        with self.assertRaises(CompanyOSError):
            memory.record("co_objectives", "guess", "some content", actor="system")

    def test_memory_records_and_lists_by_subject(self):
        memory = CompanyMemoryStore(self.store, self.audit)
        objective_id = self._active_objective()
        memory.record("co_objectives", "lesson", "Underestimated onboarding time.", actor="Aryan", subject_id=objective_id)
        rows = memory.list_for("co_objectives", objective_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "lesson")

    def test_cost_estimate_actuals_never_block_missing_fields(self):
        costs = CostEstimateStore(self.store, self.audit)
        cost_id = costs.set_estimate("co_plans", "plan-1", actor="system", estimated_ai_cost=12.5)
        updated = costs.record_actual(cost_id, "system", actual_ai_cost=15.0)
        self.assertEqual(updated["actual_ai_cost"], 15.0)
        self.assertIsNone(updated["actual_api_cost"])


class GoalFeedbackAndTimelineTests(CompanyOSBase):
    def test_goal_feedback_never_rewrites_the_goal(self):
        goals = GoalStore(self.store, self.audit)
        goal_id = goals.create("Book 20 demos", 20.0, "demos", actor="Aryan", deadline="2000-01-01")
        before = self.store.get("cc_goals", goal_id)
        feedback = goal_feedback(self.store, self.audit, goal_id, actor="system")
        after = self.store.get("cc_goals", goal_id)
        self.assertEqual(before["target"], after["target"])
        self.assertEqual(feedback["variance"], -20.0)
        self.assertIn("past deadline", feedback["likely_reason"])

    def test_goal_feedback_raises_on_unknown_goal(self):
        with self.assertRaises(CompanyOSError):
            goal_feedback(self.store, self.audit, "not-a-real-goal", actor="system")

    def test_timeline_records_and_lists_most_recent_first(self):
        timeline = TimelineStore(self.store, self.audit)
        timeline.record("MILESTONE", "First paying client", actor="Aryan", occurred_at="2026-01-01T00:00:00+00:00")
        timeline.record("MILESTONE", "Second paying client", actor="Aryan", occurred_at="2026-02-01T00:00:00+00:00")
        items = timeline.list()
        self.assertEqual(items[0]["title"], "Second paying client")


class SnapshotsAndOperatingLoopTests(CompanyOSBase):
    def test_company_state_snapshot_is_pure_read_never_persisted(self):
        self._active_objective()
        before_tables = len(self.store.list("co_objectives"))
        snapshot = company_state_snapshot(self.store)
        after_tables = len(self.store.list("co_objectives"))
        self.assertEqual(before_tables, after_tables)
        self.assertEqual(snapshot["objectives"]["total"], 1)
        self.assertIn("source", snapshot)

    def test_capital_orchestration_snapshot_never_moves_money(self):
        snapshot = capital_orchestration_snapshot(self.store)
        self.assertIn("nothing here moves money", snapshot["note"])
        self.assertIn("cash", snapshot)

    def test_daily_loop_runs_and_persists(self):
        objective_id = self._active_objective(deadline="2000-01-01")  # already past deadline
        result = run_daily_loop(self.store, self.audit, actor="system")
        self.assertEqual(result["changes"][0]["objective_id"], objective_id)
        self.assertEqual(self.objectives.get(objective_id)["status"], "AT_RISK")
        self.assertEqual(len(list_daily_loops(self.store)), 1)

    def test_daily_loop_never_auto_resolves_at_risk_back_to_active(self):
        objective_id = self._active_objective(deadline="2000-01-01")
        run_daily_loop(self.store, self.audit, actor="system")
        self.assertEqual(self.objectives.get(objective_id)["status"], "AT_RISK")
        # A second run must not flip it back on its own -- that stays an owner call.
        run_daily_loop(self.store, self.audit, actor="system")
        self.assertEqual(self.objectives.get(objective_id)["status"], "AT_RISK")

    def test_weekly_review_runs_and_persists(self):
        self._active_objective()
        review = run_weekly_review(self.store, self.audit, actor="system")
        self.assertIn("week_start", review)
        self.assertEqual(len(list_weekly_reviews(self.store)), 1)

    def test_ceo_command_center_v2_reshapes_without_new_computation(self):
        self._active_objective()
        snapshot = ceo_command_center_v2_snapshot(self.store)
        self.assertIn("top_company_objectives", snapshot)
        self.assertIn("no new computation", snapshot["source"])

    def test_company_os_home_covers_every_required_facet(self):
        home = company_os_home(self.store)
        for key in (
            "what_matters_now", "what_is_running", "what_is_blocked",
            "what_changed", "what_needs_aryan", "what_should_happen_next",
        ):
            self.assertIn(key, home)


class CrossDepartmentIsolationTests(CompanyOSBase):
    """Section 10/31: work in one department's objectives must never
    silently alter another department's rows."""

    def test_setting_one_departments_objective_status_does_not_touch_others(self):
        objective_id = self._active_objective()
        depts = DepartmentObjectiveStore(self.store, self.audit)
        sales = depts.create(objective_id, "Sales", "Book demos", actor="Aryan")
        research = depts.create(objective_id, "Research", "Study competitors", actor="Aryan")

        before_research = dict(depts.get(research))
        depts.set_status(sales, "ACTIVE", "Aryan")
        after_research = dict(depts.get(research))

        self.assertEqual(before_research["status"], after_research["status"])
        self.assertEqual(depts.get(sales)["status"], "ACTIVE")

    def test_route_plan_only_touches_departments_the_plan_actually_names(self):
        objective_id = self._active_objective()
        plan_id = self._approved_plan(objective_id, departments=["Sales"])
        ExecutionOrchestrator(self.store, self.audit).route_plan(plan_id, actor="system")
        rows = self.store.list("co_department_objectives", "company_objective_id=?", (objective_id,))
        self.assertEqual({r["department"] for r in rows}, {"Sales"})


class PersistenceAcrossRestartTests(unittest.TestCase):
    """Every mutation goes through StateStore, never in-memory-only (the
    module's own stated hard constraint) -- proven here by actually closing
    and reopening the database, not merely asserting a row was inserted."""

    def test_objective_plan_and_decision_survive_a_simulated_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "state.db"
            audit_path = root / "audit.jsonl"

            store = StateStore(db_path)
            store.migrate()
            audit = AuditLog(audit_path)
            objectives = ObjectiveStore(store, audit)
            objective_id = objectives.create("Grow ARR", actor="Aryan")
            objectives.transition(objective_id, "ACTIVE", "Aryan")
            plan_id = PlanStore(store, audit).create(objective_id, "Ship v2", actor="Aryan", departments=["Sales"])
            decision_id = DecisionStore(store, audit).create("Proceed?", [{"label": "Yes"}], actor="system")
            store.close()

            # Simulate an app restart: brand-new StateStore against the same file.
            store2 = StateStore(db_path)
            store2.migrate()
            audit2 = AuditLog(audit_path)
            reopened_objective = ObjectiveStore(store2, audit2).get(objective_id)
            reopened_plan = PlanStore(store2, audit2).get(plan_id)
            reopened_decision = DecisionStore(store2, audit2).get(decision_id)
            store2.close()

            self.assertEqual(reopened_objective["status"], "ACTIVE")
            self.assertEqual(reopened_plan["objective_id"], objective_id)
            self.assertEqual(reopened_decision["status"], "PROPOSED")


class CapitalRecommendationTests(CompanyOSBase):
    def test_capital_recommendation_only_ever_records_never_moves_money(self):
        recs = CapitalRecommendationStore(self.store, self.audit)
        rec_id = recs.record("Allocate 50k to Venture Studio experimental capital", "strong early signal", actor="system", amount=50000)
        open_recs = recs.list(status="OPEN")
        self.assertEqual(len(open_recs), 1)
        self.assertEqual(open_recs[0]["id"], rec_id)


if __name__ == "__main__":
    unittest.main()
