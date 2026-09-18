"""Tests for falguna/trading_lab_strategy.py -- strategy lifecycle, versions,
structured rule validation, and the Strategy Research Agent. Sections 3, 7,
8 of the Trading Lab spec.
"""

import tempfile
import unittest
from pathlib import Path

from falguna.audit import AuditLog
from falguna.store import StateStore
from falguna.trading_lab_data import InstrumentStore, MarketStore
from falguna.trading_lab_strategy import (
    STRATEGY_STATUSES, StrategyStore, StrategyVersionStore, research_strategy_idea, validate_rule,
)


class StrategyBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = StateStore(self.root / "state.db")
        self.store.migrate()
        self.audit = AuditLog(self.root / "audit.jsonl")
        self.markets = MarketStore(self.store, self.audit)
        self.instruments = InstrumentStore(self.store, self.audit)
        self.strategies = StrategyStore(self.store, self.audit)
        self.versions = StrategyVersionStore(self.store, self.audit)
        self.m_id = self.markets.create("US_EQUITY", "US Equities", "us_equity")
        self.i_id = self.instruments.create(self.m_id, "TEST")

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()


class ValidateRuleTests(unittest.TestCase):
    def test_valid_rule_passes(self):
        self.assertIsNone(validate_rule({"signal": "sma_crossover", "params": {"fast_period": 5, "slow_period": 20, "cross": "up"}}))

    def test_unknown_signal_rejected(self):
        self.assertIsNotNone(validate_rule({"signal": "not_a_signal", "params": {}}))

    def test_missing_param_rejected(self):
        self.assertIsNotNone(validate_rule({"signal": "sma_crossover", "params": {"fast_period": 5}}))

    def test_non_dict_rule_rejected(self):
        self.assertIsNotNone(validate_rule("not a dict"))


class StrategyLifecycleTests(StrategyBase):
    def test_new_strategy_starts_at_idea(self):
        s_id = self.strategies.create("Test strategy", "hypothesis text", self.m_id)
        self.assertEqual(self.strategies.get(s_id)["status"], "IDEA")
        events = self.store.list("tl_strategy_status_events", "strategy_id=?", (s_id,))
        self.assertEqual(len(events), 1)
        self.assertIsNone(events[0]["from_status"])
        self.assertEqual(events[0]["to_status"], "IDEA")

    def test_empty_hypothesis_rejected(self):
        with self.assertRaises(ValueError):
            self.strategies.create("No hypothesis", "   ", self.m_id)

    def test_valid_forward_transitions_succeed(self):
        s_id = self.strategies.create("Test", "h", self.m_id)
        for status in ("RESEARCHING", "BACKTESTING", "REVIEW", "PAPER_APPROVED", "PAPER_ACTIVE"):
            self.strategies.transition(s_id, status, "Aryan", f"to {status}")
            self.assertEqual(self.strategies.get(s_id)["status"], status)

    def test_invalid_transition_rejected_and_status_unchanged(self):
        s_id = self.strategies.create("Test", "h", self.m_id)
        with self.assertRaises(ValueError):
            self.strategies.transition(s_id, "PAPER_ACTIVE", "Aryan", "skip ahead")
        self.assertEqual(self.strategies.get(s_id)["status"], "IDEA")

    def test_graveyard_has_no_outgoing_transitions(self):
        # No LIVE state anywhere, and GRAVEYARD is terminal -- Section 3/22.
        self.assertNotIn("LIVE", STRATEGY_STATUSES)
        from falguna.trading_lab_strategy import ALLOWED_TRANSITIONS
        self.assertEqual(ALLOWED_TRANSITIONS["GRAVEYARD"], set())

    def test_transition_unknown_strategy_raises(self):
        with self.assertRaises(ValueError):
            self.strategies.transition("nonexistent", "RESEARCHING", "Aryan")

    def test_transition_unknown_status_raises(self):
        s_id = self.strategies.create("Test", "h", self.m_id)
        with self.assertRaises(ValueError):
            self.strategies.transition(s_id, "NOT_A_REAL_STATUS", "Aryan")


class StrategyVersionTests(StrategyBase):
    def setUp(self):
        super().setUp()
        self.s_id = self.strategies.create("Test", "h", self.m_id)

    def _valid_version_kwargs(self):
        return dict(
            strategy_id=self.s_id, instruments=[self.i_id], timeframe="1d",
            entry_rules={"signal": "sma_crossover", "params": {"fast_period": 5, "slow_period": 20, "cross": "up"}},
            exit_rules={"signal": "sma_crossover", "params": {"fast_period": 5, "slow_period": 20, "cross": "down"}},
            sizing_logic={"position_size_pct": 10},
        )

    def test_create_version_and_increment_number(self):
        v1 = self.versions.create(**self._valid_version_kwargs())
        self.assertEqual(self.versions.get(v1)["version_number"], 1)
        v2 = self.versions.create(**self._valid_version_kwargs())
        self.assertEqual(self.versions.get(v2)["version_number"], 2)
        self.assertEqual(self.versions.latest_for_strategy(self.s_id)["id"], v2)

    def test_invalid_entry_rule_rejected(self):
        kwargs = self._valid_version_kwargs()
        kwargs["entry_rules"] = {"signal": "not_real", "params": {}}
        with self.assertRaises(ValueError):
            self.versions.create(**kwargs)

    def test_no_instruments_rejected(self):
        kwargs = self._valid_version_kwargs()
        kwargs["instruments"] = []
        with self.assertRaises(ValueError):
            self.versions.create(**kwargs)

    def test_sizing_logic_must_specify_size(self):
        kwargs = self._valid_version_kwargs()
        kwargs["sizing_logic"] = {}
        with self.assertRaises(ValueError):
            self.versions.create(**kwargs)

    def test_unknown_strategy_rejected(self):
        kwargs = self._valid_version_kwargs()
        kwargs["strategy_id"] = "nonexistent"
        with self.assertRaises(ValueError):
            self.versions.create(**kwargs)


class ResearchAgentTests(unittest.TestCase):
    def test_research_produces_test_plan_and_never_asserts_a_result(self):
        result = research_strategy_idea("SMA crossover on a trending market", ["sma_crossover"], "US_EQUITY")
        self.assertEqual(result["evidence"], [])
        self.assertTrue(result["inference"].startswith("untested"))
        self.assertGreater(len(result["test_plan"]), 0)
        self.assertGreater(len(result["likely_failure_modes"]), 0)
        self.assertIn("no", result["guarantee_disclaimer"].lower())

    def test_empty_idea_rejected(self):
        with self.assertRaises(ValueError):
            research_strategy_idea("   ", ["sma_crossover"])

    def test_unknown_signal_rejected(self):
        with self.assertRaises(ValueError):
            research_strategy_idea("idea", ["not_a_real_signal"])


if __name__ == "__main__":
    unittest.main()
