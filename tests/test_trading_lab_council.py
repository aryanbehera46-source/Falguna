"""Tests for falguna/trading_lab_council.py -- the Trading Council's
multi-perspective review and the Strategy Graveyard. Sections 16-17 of the
Trading Lab spec.
"""

import json
import math
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from falguna.audit import AuditLog
from falguna.store import StateStore
from falguna.ttt_hq import NeedsAryanQueue
from falguna.trading_lab_data import Bar, DataSourceStore, InstrumentStore, MarketStore
from falguna.trading_lab_strategy import StrategyStore, StrategyVersionStore
from falguna.trading_lab_backtest import BacktestStore, run_backtest, run_stress_review
from falguna.trading_lab_council import ReviewStore, bury_strategy, check_graveyard_for_similar, run_trading_council

ENTRY = {"signal": "sma_crossover", "params": {"fast_period": 3, "slow_period": 9, "cross": "up"}, "side": "long"}
EXIT = {"signal": "sma_crossover", "params": {"fast_period": 3, "slow_period": 9, "cross": "down"}}
SIZING = {"position_size_pct": 20}


def oscillating_bars(n=600):
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return [
        Bar(ts=(start + timedelta(days=idx)).isoformat(),
            open=(p := 100 + idx * 0.05 + 6 * math.sin(idx / 9.0)),
            high=p * 1.003, low=p * 0.997, close=p, volume=1000)
        for idx in range(n)
    ]


class CouncilBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = StateStore(self.root / "state.db")
        self.store.migrate()
        self.audit = AuditLog(self.root / "audit.jsonl")
        self.needs_aryan = NeedsAryanQueue(self.store, self.audit)
        self.markets = MarketStore(self.store, self.audit)
        self.instruments = InstrumentStore(self.store, self.audit)
        self.strategies = StrategyStore(self.store, self.audit)
        self.versions = StrategyVersionStore(self.store, self.audit)
        self.bt_store = BacktestStore(self.store, self.audit)
        self.m_id = self.markets.create("US_EQUITY", "US Equities", "us_equity")
        self.i_id = self.instruments.create(self.m_id, "TEST")

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _make_dataset(self, bars):
        src = DataSourceStore(self.store, self.audit).register("synthetic test fixture", "synthetic_test_fixture", is_synthetic=True)
        return self.store.create("tl_datasets", {
            "data_source_id": src, "instrument_id": self.i_id, "timeframe": "1d",
            "start_date": bars[0].ts, "end_date": bars[-1].ts, "status": "OK", "bar_count": len(bars),
            "completeness_pct": 100.0, "error": None, "raw_source_metadata_json": "{}",
            "ingested_at": bars[0].ts, "created_at": bars[0].ts, "updated_at": bars[0].ts,
        })


class CouncilDecisionTests(CouncilBase):
    def test_no_backtest_yet_yields_continue_research(self):
        s_id = self.strategies.create("Idea only", "hypothesis", self.m_id)
        v_id = self.versions.create(s_id, [self.i_id], "1d", ENTRY, EXIT, SIZING)
        decision_id = run_trading_council(self.store, self.audit, self.needs_aryan, s_id, v_id, backtest_id=None, actor="Aryan")
        decision = self.store.get("tl_council_decisions", decision_id)
        self.assertEqual(decision["decision"], "continue_research")

    def test_incomplete_spec_triggers_strategy_research_revise(self):
        s_id = self.strategies.create("Incomplete", "hypothesis", self.m_id)
        v_id = self.versions.create(s_id, [self.i_id], "1d", ENTRY, EXIT, SIZING, assumptions="", known_risks="")
        decision_id = run_trading_council(self.store, self.audit, self.needs_aryan, s_id, v_id, backtest_id=None, actor="Aryan")
        reviews = ReviewStore(self.store, self.audit).list_for_version(v_id)
        research_review = next(r for r in reviews if r["reviewer_role"] == "strategy_research")
        self.assertEqual(research_review["verdict"], "revise")

    def test_below_min_trade_count_backtest_yields_revise(self):
        bars = oscillating_bars(80)  # short window -> few trades, below MIN_TRADES_FOR_CONFIDENCE
        s_id = self.strategies.create("Low sample", "hypothesis", self.m_id)
        v_id = self.versions.create(s_id, [self.i_id], "1d", ENTRY, EXIT, SIZING, assumptions="a", known_risks="b")
        ds_id = self._make_dataset(bars)
        result = run_backtest(bars, ENTRY, EXIT, SIZING, fee_bps=5, slippage_bps=2, starting_cash=10000)
        bt_id = self.bt_store.record(v_id, ds_id, "full", 5, 2, 10000, result)
        decision_id = run_trading_council(self.store, self.audit, self.needs_aryan, s_id, v_id, backtest_id=bt_id, actor="Aryan")
        decision = self.store.get("tl_council_decisions", decision_id)
        self.assertIn(decision["decision"], ("revise", "reject"))

    def test_full_evidence_pipeline_reaches_approve_for_paper_and_activates_needs_aryan(self):
        bars = oscillating_bars(600)  # enough oscillation cycles to clear MIN_TRADES_FOR_CONFIDENCE
        s_id = self.strategies.create("Oscillating trend strategy", "SMA crossover captures each mini-cycle", self.m_id)
        self.strategies.transition(s_id, "RESEARCHING", "Aryan", "start")
        v_id = self.versions.create(s_id, [self.i_id], "1d", ENTRY, EXIT, SIZING,
                                     assumptions="the oscillation is regular enough for SMA crossover timing",
                                     known_risks="a regime shift would degrade this; single-instrument test only")
        self.strategies.transition(s_id, "BACKTESTING", "Aryan", "run backtest")
        ds_id = self._make_dataset(bars)
        result = run_backtest(bars, ENTRY, EXIT, SIZING, fee_bps=5, slippage_bps=2, starting_cash=10000)
        bt_id = self.bt_store.record(v_id, ds_id, "full", 5, 2, 10000, result)
        stress_ids = run_stress_review(self.store, self.audit, bt_id, bars, ENTRY, EXIT, SIZING, None, 5, 2, 10000)

        decision_id = run_trading_council(self.store, self.audit, self.needs_aryan, s_id, v_id,
                                           backtest_id=bt_id, stress_test_ids=stress_ids, actor="Aryan")
        decision = self.store.get("tl_council_decisions", decision_id)
        self.assertEqual(decision["decision"], "approve_for_paper")

        strategy_after = self.strategies.get(s_id)
        self.assertEqual(strategy_after["status"], "PAPER_APPROVED", "the Council must route through Needs Aryan, never straight to PAPER_ACTIVE")

        items = self.store.list("needs_aryan_items", "ref_id=?", (s_id,))
        matching = [i for i in items if i["kind"] == "trading_paper_activation_request"]
        self.assertTrue(matching)
        self.assertEqual(matching[0]["status"], "PENDING")

        reviews = ReviewStore(self.store, self.audit).list_for_version(v_id)
        self.assertEqual(len(reviews), 5)
        self.assertTrue(all(r["verdict"] == "approve" for r in reviews))

    def test_second_rejection_buries_the_strategy(self):
        # Short an oscillating series with a mild UPWARD drift -- a
        # structural loser by construction (fighting the drift on every
        # cycle), so this backtest is reliably unprofitable without
        # needing punishing cost assumptions to force it.
        # This is the EXACT entry/exit timing of the strategy proven
        # profitable elsewhere in this file (test_full_evidence_pipeline_*),
        # with side flipped to short -- a strategy that fades a working
        # signal is reliably unprofitable, sign-inverted from the long
        # version (worse still once fees/slippage apply symmetrically).
        bad_entry = {"signal": "sma_crossover", "params": {"fast_period": 3, "slow_period": 9, "cross": "up"}, "side": "short"}
        bad_exit = {"signal": "sma_crossover", "params": {"fast_period": 3, "slow_period": 9, "cross": "down"}}
        bars = oscillating_bars(600)
        s_id = self.strategies.create("Bad strategy", "shorting a mildly uptrending oscillator", self.m_id)
        v_id = self.versions.create(s_id, [self.i_id], "1d", bad_entry, bad_exit, SIZING, assumptions="a", known_risks="b")
        ds_id = self._make_dataset(bars)
        losing_result = run_backtest(bars, bad_entry, bad_exit, SIZING, fee_bps=20, slippage_bps=10, starting_cash=10000)
        self.assertLess(losing_result["metrics"]["net_return"], 0, "test setup sanity check: this strategy must actually lose money")
        bt_id = self.bt_store.record(v_id, ds_id, "full", 20, 10, 10000, losing_result)

        decision1_id = run_trading_council(self.store, self.audit, self.needs_aryan, s_id, v_id, backtest_id=bt_id, actor="Aryan")
        decision1 = self.store.get("tl_council_decisions", decision1_id)
        self.assertEqual(decision1["decision"], "reject")

        decision2_id = run_trading_council(self.store, self.audit, self.needs_aryan, s_id, v_id, backtest_id=bt_id, actor="Aryan")
        decision2 = self.store.get("tl_council_decisions", decision2_id)
        self.assertEqual(decision2["decision"], "graveyard")

        grave_rows = self.store.list("tl_graveyard", "strategy_id=?", (s_id,))
        self.assertEqual(len(grave_rows), 1)
        self.assertEqual(self.strategies.get(s_id)["status"], "GRAVEYARD")


class GraveyardTests(CouncilBase):
    def test_bury_strategy_records_evidence_and_transitions_status(self):
        s_id = self.strategies.create("To bury", "h", self.m_id)
        v_id = self.versions.create(s_id, [self.i_id], "1d", ENTRY, EXIT, SIZING)
        grave_id = bury_strategy(self.store, self.audit, s_id, v_id, "manually buried for testing",
                                  failed_metrics={"net_return": -0.5}, actor="Aryan")
        row = self.store.get("tl_graveyard", grave_id)
        self.assertEqual(row["reason_rejected"], "manually buried for testing")
        self.assertEqual(json.loads(row["failed_metrics_json"])["net_return"], -0.5)
        self.assertEqual(self.strategies.get(s_id)["status"], "GRAVEYARD")

    def test_check_graveyard_for_similar_surfaces_matching_signal(self):
        s_id = self.strategies.create("To bury", "h", self.m_id)
        v_id = self.versions.create(s_id, [self.i_id], "1d", ENTRY, EXIT, SIZING)
        bury_strategy(self.store, self.audit, s_id, v_id, "bad idea", actor="Aryan")
        hits = check_graveyard_for_similar(self.store, self.m_id, ["sma_crossover"])
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["signal"], "sma_crossover")

        no_hits = check_graveyard_for_similar(self.store, self.m_id, ["breakout"])
        self.assertEqual(no_hits, [])


if __name__ == "__main__":
    unittest.main()
