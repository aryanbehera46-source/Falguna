"""Tests for the Company Workflow Orchestrator (falguna/lifecycle.py).

These exercise the new fine-grained lifecycle_state machine in isolation:
valid forward transitions across the full graph, illegal/silent-jump
rejection, idempotent same-state no-ops, terminal-state refusal, the
_sync_stage boundary behavior against the existing, older `stage` field,
and try_transition's non-raising contract for hook call sites.
"""

import tempfile
import unittest
from pathlib import Path

from falguna.audit import AuditLog
from falguna.lifecycle import (
    ALLOWED_TRANSITIONS,
    LIFECYCLE_STATES,
    LifecycleError,
    LifecycleOrchestrator,
    TERMINAL_LIFECYCLE_STATES,
)
from falguna.revenue_hunter import OpportunityStore
from falguna.store import StateStore


class LifecycleTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = StateStore(root / "state.db")
        self.store.migrate()
        self.audit = AuditLog(root / "audit.jsonl")
        self.opportunities = OpportunityStore(self.store, self.audit)
        self.orchestrator = LifecycleOrchestrator(self.store, self.audit)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _make_opportunity(self, title="Test Opportunity"):
        return self.opportunities.create({"title": title, "description": "d"}, actor="Aryan")


class InitializationTests(LifecycleTestBase):
    def test_new_opportunity_has_no_lifecycle_state_until_initialized(self):
        opp_id = self._make_opportunity()
        self.assertIsNone(self.orchestrator.current_state(opp_id))

    def test_initialize_sets_discovered(self):
        opp_id = self._make_opportunity()
        self.orchestrator.initialize(opp_id, actor="Aryan")
        self.assertEqual(self.orchestrator.current_state(opp_id), "DISCOVERED")

    def test_initialize_is_idempotent_and_never_overwrites_real_history(self):
        opp_id = self._make_opportunity()
        self.orchestrator.initialize(opp_id, actor="Aryan")
        self.orchestrator.transition(opp_id, "RESEARCHING", actor="Aryan")
        self.orchestrator.initialize(opp_id, actor="Aryan")  # should be a no-op
        self.assertEqual(self.orchestrator.current_state(opp_id), "RESEARCHING")

    def test_transition_auto_initializes_when_no_state_exists_yet(self):
        # A pre-Pass-A opportunity with no lifecycle_state at all should not
        # crash the first transition call -- it should silently pass through
        # DISCOVERED first.
        opp_id = self._make_opportunity()
        self.orchestrator.transition(opp_id, "QUALIFIED", actor="Aryan")
        self.assertEqual(self.orchestrator.current_state(opp_id), "QUALIFIED")
        history = self.orchestrator.history(opp_id)
        self.assertEqual([h["to_state"] for h in history], ["DISCOVERED", "QUALIFIED"])

    def test_transition_unknown_opportunity_raises(self):
        with self.assertRaises(LifecycleError):
            self.orchestrator.transition("does-not-exist", "QUALIFIED", actor="Aryan")

    def test_transition_unknown_state_raises(self):
        opp_id = self._make_opportunity()
        with self.assertRaises(LifecycleError):
            self.orchestrator.transition(opp_id, "NOT_A_REAL_STATE", actor="Aryan")


class ForwardTransitionGraphTests(LifecycleTestBase):
    """Walks the full happy-path chain the build spec named, end to end."""

    FULL_CHAIN = [
        "DISCOVERED", "RESEARCHING", "QUALIFIED", "PITCH_READY", "AWAITING_APPROVAL",
        "APPROVED", "APPLYING", "CONTACTED", "REPLIED", "NEGOTIATING", "WON",
        "ONBOARDING", "DELIVERY", "CLIENT_REVIEW", "COMPLETED", "INVOICED", "PAID", "RETAIN",
    ]

    def test_full_happy_path_chain_succeeds(self):
        opp_id = self._make_opportunity()
        self.orchestrator.initialize(opp_id, actor="Aryan")
        for state in self.FULL_CHAIN[1:]:
            self.orchestrator.transition(opp_id, state, actor="Aryan")
            self.assertEqual(self.orchestrator.current_state(opp_id), state)
        self.assertEqual(self.orchestrator.current_state(opp_id), "RETAIN")

    def test_discovery_conversation_branch_also_reaches_negotiating(self):
        opp_id = self._make_opportunity()
        self.orchestrator.initialize(opp_id, actor="Aryan")
        for state in ["RESEARCHING", "QUALIFIED", "PITCH_READY", "AWAITING_APPROVAL",
                      "APPROVED", "APPLYING", "CONTACTED", "REPLIED",
                      "DISCOVERY_CONVERSATION", "NEGOTIATING", "WON"]:
            self.orchestrator.transition(opp_id, state, actor="Aryan")
        self.assertEqual(self.orchestrator.current_state(opp_id), "WON")

    def test_awaiting_approval_can_bounce_back_to_pitch_ready_on_changes_requested(self):
        opp_id = self._make_opportunity()
        self.orchestrator.initialize(opp_id, actor="Aryan")
        for state in ["RESEARCHING", "QUALIFIED", "PITCH_READY", "AWAITING_APPROVAL"]:
            self.orchestrator.transition(opp_id, state, actor="Aryan")
        self.orchestrator.transition(opp_id, "PITCH_READY", actor="Aryan", reason="changes requested")
        self.assertEqual(self.orchestrator.current_state(opp_id), "PITCH_READY")

    def test_applying_can_bounce_back_to_approved_on_a_blocked_application(self):
        opp_id = self._make_opportunity()
        self.orchestrator.initialize(opp_id, actor="Aryan")
        for state in ["RESEARCHING", "QUALIFIED", "PITCH_READY", "AWAITING_APPROVAL", "APPROVED", "APPLYING"]:
            self.orchestrator.transition(opp_id, state, actor="Aryan")
        self.orchestrator.transition(opp_id, "APPROVED", actor="system", reason="application blocked, needs Aryan")
        self.assertEqual(self.orchestrator.current_state(opp_id), "APPROVED")

    def test_client_review_can_bounce_back_to_delivery_on_requested_changes(self):
        opp_id = self._make_opportunity()
        self.orchestrator.initialize(opp_id, actor="Aryan")
        for state in ["RESEARCHING", "QUALIFIED", "PITCH_READY", "AWAITING_APPROVAL", "APPROVED",
                      "APPLYING", "CONTACTED", "REPLIED", "NEGOTIATING", "WON", "ONBOARDING",
                      "DELIVERY", "CLIENT_REVIEW"]:
            self.orchestrator.transition(opp_id, state, actor="Aryan")
        self.orchestrator.transition(opp_id, "DELIVERY", actor="Aryan", reason="client requested changes")
        self.assertEqual(self.orchestrator.current_state(opp_id), "DELIVERY")

    def test_every_pre_won_state_can_reach_lost(self):
        for state in ALLOWED_TRANSITIONS:
            if state in {"WON", "ONBOARDING", "DELIVERY", "CLIENT_REVIEW", "COMPLETED", "INVOICED", "PAID"} | TERMINAL_LIFECYCLE_STATES:
                continue
            with self.subTest(state=state):
                opp_id = self._make_opportunity()
                self.orchestrator.initialize(opp_id, actor="Aryan")
                if state != "DISCOVERED":
                    # Walk forward to `state` first via its own allowed edges
                    # is unnecessary here -- LOST is directly reachable from
                    # every one of these states per the graph, so force the
                    # opportunity's lifecycle_state there directly for the
                    # purpose of this table-driven check.
                    self.store.update("rh_opportunities", opp_id, lifecycle_state=state)
                self.orchestrator.transition(opp_id, "LOST", actor="Aryan", reason="deal died")
                self.assertEqual(self.orchestrator.current_state(opp_id), "LOST")


class IllegalTransitionTests(LifecycleTestBase):
    def test_cannot_skip_states(self):
        opp_id = self._make_opportunity()
        self.orchestrator.initialize(opp_id, actor="Aryan")
        with self.assertRaises(LifecycleError):
            self.orchestrator.transition(opp_id, "WON", actor="Aryan")

    def test_cannot_go_backwards_past_an_allowed_edge(self):
        opp_id = self._make_opportunity()
        self.orchestrator.initialize(opp_id, actor="Aryan")
        self.orchestrator.transition(opp_id, "RESEARCHING", actor="Aryan")
        self.orchestrator.transition(opp_id, "QUALIFIED", actor="Aryan")
        with self.assertRaises(LifecycleError):
            self.orchestrator.transition(opp_id, "DISCOVERED", actor="Aryan")

    def test_illegal_transition_is_not_recorded_as_a_new_event(self):
        opp_id = self._make_opportunity()
        self.orchestrator.initialize(opp_id, actor="Aryan")
        before = len(self.orchestrator.history(opp_id))
        with self.assertRaises(LifecycleError):
            self.orchestrator.transition(opp_id, "WON", actor="Aryan")
        after = len(self.orchestrator.history(opp_id))
        self.assertEqual(before, after)
        self.assertEqual(self.orchestrator.current_state(opp_id), "DISCOVERED")


class TerminalStateTests(LifecycleTestBase):
    def test_lost_is_terminal(self):
        opp_id = self._make_opportunity()
        self.orchestrator.initialize(opp_id, actor="Aryan")
        self.orchestrator.transition(opp_id, "LOST", actor="Aryan")
        with self.assertRaises(LifecycleError):
            self.orchestrator.transition(opp_id, "RESEARCHING", actor="Aryan")

    def test_retain_is_terminal(self):
        opp_id = self._make_opportunity()
        self.store.update("rh_opportunities", opp_id, lifecycle_state="PAID")
        self.orchestrator.transition(opp_id, "RETAIN", actor="Aryan")
        with self.assertRaises(LifecycleError):
            self.orchestrator.transition(opp_id, "DELIVERY", actor="Aryan")

    def test_every_terminal_state_has_no_allowed_transitions(self):
        for state in TERMINAL_LIFECYCLE_STATES:
            self.assertEqual(ALLOWED_TRANSITIONS[state], set())


class IdempotentNoOpTests(LifecycleTestBase):
    def test_same_state_transition_is_a_no_op_not_an_error(self):
        opp_id = self._make_opportunity()
        self.orchestrator.initialize(opp_id, actor="Aryan")
        self.orchestrator.transition(opp_id, "QUALIFIED", actor="Aryan")
        # Re-affirm QUALIFIED again (e.g. requalify_all() re-qualifying an
        # opportunity that's already QUALIFIED) -- must not raise.
        result = self.orchestrator.transition(opp_id, "QUALIFIED", actor="system", reason="requalified")
        self.assertEqual(result["from_state"], "QUALIFIED")
        self.assertEqual(result["to_state"], "QUALIFIED")
        self.assertEqual(self.orchestrator.current_state(opp_id), "QUALIFIED")

    def test_no_op_transition_does_not_re_trigger_the_stage_sync_side_effect(self):
        opp_id = self._make_opportunity()
        self.orchestrator.initialize(opp_id, actor="Aryan")
        self.orchestrator.transition(opp_id, "QUALIFIED", actor="Aryan")
        # Manually move the coarse stage forward past what QUALIFIED implies,
        # simulating the older /stage route already having advanced it.
        self.opportunities.move_stage(opp_id, "Proposal Ready", actor="Aryan")
        self.orchestrator.transition(opp_id, "QUALIFIED", actor="system", reason="requalified")
        opp = self.store.get("rh_opportunities", opp_id)
        # The no-op must not have moved `stage` backwards to "Qualified".
        self.assertEqual(opp["stage"], "Proposal Ready")

    def test_no_op_is_still_recorded_in_history(self):
        opp_id = self._make_opportunity()
        self.orchestrator.initialize(opp_id, actor="Aryan")
        self.orchestrator.transition(opp_id, "QUALIFIED", actor="Aryan")
        before = len(self.orchestrator.history(opp_id))
        self.orchestrator.transition(opp_id, "QUALIFIED", actor="system", reason="requalified")
        after = len(self.orchestrator.history(opp_id))
        self.assertEqual(after, before + 1)


class StageSyncTests(LifecycleTestBase):
    def test_qualified_lifecycle_state_moves_coarse_stage_to_qualified(self):
        opp_id = self._make_opportunity()
        self.orchestrator.initialize(opp_id, actor="Aryan")
        self.orchestrator.transition(opp_id, "QUALIFIED", actor="Aryan")
        opp = self.store.get("rh_opportunities", opp_id)
        self.assertEqual(opp["stage"], "Qualified")

    def test_pitch_ready_and_awaiting_approval_and_approved_have_no_stage_equivalent(self):
        opp_id = self._make_opportunity()
        self.orchestrator.initialize(opp_id, actor="Aryan")
        for state in ["RESEARCHING", "QUALIFIED", "PITCH_READY", "AWAITING_APPROVAL", "APPROVED"]:
            self.orchestrator.transition(opp_id, state, actor="Aryan")
        opp = self.store.get("rh_opportunities", opp_id)
        # PITCH_READY/AWAITING_APPROVAL/APPROVED all still read as the older
        # pipeline's "Qualified" stage (there is no dedicated
        # "Proposal Ready" trigger inside the orchestrator itself -- that
        # coarse move is made by ProposalStore.generate() directly, exactly
        # as it already was before this pass).
        self.assertEqual(opp["stage"], "Qualified")

    def test_won_calls_mark_won_and_sets_stage_won(self):
        opp_id = self._make_opportunity()
        self.orchestrator.initialize(opp_id, actor="Aryan")
        for state in ["RESEARCHING", "QUALIFIED", "PITCH_READY", "AWAITING_APPROVAL",
                      "APPROVED", "APPLYING", "CONTACTED", "REPLIED", "NEGOTIATING", "WON"]:
            self.orchestrator.transition(opp_id, state, actor="Aryan")
        opp = self.store.get("rh_opportunities", opp_id)
        self.assertEqual(opp["stage"], "Won")

    def test_lost_calls_mark_lost_and_sets_stage_lost_with_reason(self):
        opp_id = self._make_opportunity()
        self.orchestrator.initialize(opp_id, actor="Aryan")
        self.orchestrator.transition(opp_id, "LOST", actor="Aryan", reason="client went silent")
        opp = self.store.get("rh_opportunities", opp_id)
        self.assertEqual(opp["stage"], "Lost")
        self.assertEqual(opp["lost_reason"], "client went silent")

    def test_post_won_states_never_touch_stage(self):
        opp_id = self._make_opportunity()
        self.orchestrator.initialize(opp_id, actor="Aryan")
        for state in ["RESEARCHING", "QUALIFIED", "PITCH_READY", "AWAITING_APPROVAL",
                      "APPROVED", "APPLYING", "CONTACTED", "REPLIED", "NEGOTIATING", "WON",
                      "ONBOARDING", "DELIVERY", "CLIENT_REVIEW", "COMPLETED", "INVOICED", "PAID", "RETAIN"]:
            self.orchestrator.transition(opp_id, state, actor="Aryan")
        opp = self.store.get("rh_opportunities", opp_id)
        self.assertEqual(opp["stage"], "Won")

    def test_sync_stage_false_skips_the_stage_move(self):
        opp_id = self._make_opportunity()
        self.orchestrator.initialize(opp_id, actor="Aryan")
        self.orchestrator.transition(opp_id, "QUALIFIED", actor="Aryan", sync_stage=False)
        opp = self.store.get("rh_opportunities", opp_id)
        self.assertEqual(opp["stage"], "New")
        self.assertEqual(self.orchestrator.current_state(opp_id), "QUALIFIED")

    def test_a_redundant_stage_move_never_crashes_the_transition(self):
        # Simulate the older /stage route having already moved stage to
        # "Qualified" directly before the lifecycle layer catches up --
        # move_stage would raise OpportunityError only for an invalid
        # target/terminal-stage case, not for a same-stage move, but this
        # confirms the defensive try/except path is itself inert (does not
        # somehow break a legitimate call) when stage already matches.
        opp_id = self._make_opportunity()
        self.orchestrator.initialize(opp_id, actor="Aryan")
        self.opportunities.move_stage(opp_id, "Qualified", actor="Aryan", note="moved via older route")
        self.orchestrator.transition(opp_id, "QUALIFIED", actor="Aryan")
        opp = self.store.get("rh_opportunities", opp_id)
        self.assertEqual(opp["stage"], "Qualified")
        self.assertEqual(self.orchestrator.current_state(opp_id), "QUALIFIED")


class EventRecordTests(LifecycleTestBase):
    def test_event_records_full_shape(self):
        opp_id = self._make_opportunity()
        self.orchestrator.initialize(opp_id, actor="Aryan")
        event = self.orchestrator.transition(
            opp_id, "RESEARCHING", actor="Aryan", reason="starting research",
            evidence={"source": "linkedin"}, next_action="Read the job post",
            approval_required=False, money_impact=None,
        )
        self.assertEqual(event["opportunity_id"], opp_id)
        self.assertEqual(event["from_state"], "DISCOVERED")
        self.assertEqual(event["to_state"], "RESEARCHING")
        self.assertEqual(event["actor"], "Aryan")
        self.assertEqual(event["reason"], "starting research")
        self.assertIn("linkedin", event["evidence_json"])
        self.assertEqual(event["next_action"], "Read the job post")
        self.assertEqual(event["approval_required"], 0)

    def test_history_returns_events_in_order(self):
        opp_id = self._make_opportunity()
        self.orchestrator.initialize(opp_id, actor="Aryan")
        self.orchestrator.transition(opp_id, "RESEARCHING", actor="Aryan")
        self.orchestrator.transition(opp_id, "QUALIFIED", actor="Aryan")
        history = self.orchestrator.history(opp_id)
        self.assertEqual([h["to_state"] for h in history], ["DISCOVERED", "RESEARCHING", "QUALIFIED"])

    def test_approval_required_and_money_impact_are_persisted(self):
        opp_id = self._make_opportunity()
        self.orchestrator.initialize(opp_id, actor="Aryan")
        for state in ["RESEARCHING", "QUALIFIED", "PITCH_READY"]:
            self.orchestrator.transition(opp_id, state, actor="Aryan")
        event = self.orchestrator.transition(
            opp_id, "AWAITING_APPROVAL", actor="system", reason="proposal drafted",
            approval_required=True, approval_status="PENDING",
            money_impact={"suggested_price": "$2,500"},
        )
        self.assertEqual(event["approval_required"], 1)
        self.assertEqual(event["approval_status"], "PENDING")
        self.assertIn("2,500", event["money_impact_json"])


class TryTransitionTests(LifecycleTestBase):
    def test_try_transition_returns_none_instead_of_raising_on_illegal_jump(self):
        opp_id = self._make_opportunity()
        self.orchestrator.initialize(opp_id, actor="Aryan")
        result = self.orchestrator.try_transition(opp_id, "WON", actor="Aryan")
        self.assertIsNone(result)
        self.assertEqual(self.orchestrator.current_state(opp_id), "DISCOVERED")

    def test_try_transition_returns_none_for_unknown_opportunity(self):
        result = self.orchestrator.try_transition("does-not-exist", "QUALIFIED", actor="Aryan")
        self.assertIsNone(result)

    def test_try_transition_returns_the_event_on_success(self):
        opp_id = self._make_opportunity()
        self.orchestrator.initialize(opp_id, actor="Aryan")
        result = self.orchestrator.try_transition(opp_id, "RESEARCHING", actor="Aryan")
        self.assertIsNotNone(result)
        self.assertEqual(result["to_state"], "RESEARCHING")

    def test_try_transition_never_raises_from_a_terminal_state(self):
        opp_id = self._make_opportunity()
        self.orchestrator.initialize(opp_id, actor="Aryan")
        self.orchestrator.transition(opp_id, "LOST", actor="Aryan")
        result = self.orchestrator.try_transition(opp_id, "RESEARCHING", actor="system")
        self.assertIsNone(result)


class GraphIntegrityTests(unittest.TestCase):
    def test_every_state_in_the_spec_is_present(self):
        expected = {
            "DISCOVERED", "RESEARCHING", "QUALIFIED", "PITCH_READY", "AWAITING_APPROVAL",
            "APPROVED", "APPLYING", "CONTACTED", "REPLIED", "DISCOVERY_CONVERSATION",
            "NEGOTIATING", "WON", "ONBOARDING", "DELIVERY", "CLIENT_REVIEW", "COMPLETED",
            "INVOICED", "PAID", "RETAIN", "LOST",
        }
        self.assertEqual(set(LIFECYCLE_STATES), expected)

    def test_every_state_has_a_transitions_entry(self):
        for state in LIFECYCLE_STATES:
            self.assertIn(state, ALLOWED_TRANSITIONS)

    def test_every_transition_target_is_itself_a_known_state(self):
        for state, targets in ALLOWED_TRANSITIONS.items():
            for target in targets:
                self.assertIn(target, LIFECYCLE_STATES)


if __name__ == "__main__":
    unittest.main()
