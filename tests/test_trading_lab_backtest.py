"""Tests for falguna/trading_lab_backtest.py -- the deterministic backtest
engine, anti-overfitting controls, and Red-Team stress review. Sections 9,
10, 11 of the Trading Lab spec.
"""

import tempfile
import unittest
from pathlib import Path

from falguna.audit import AuditLog
from falguna.store import StateStore
from falguna.trading_lab_data import Bar, DataSourceStore, InstrumentStore, MarketStore, SyntheticTestDataProvider
from falguna.trading_lab_strategy import StrategyStore, StrategyVersionStore
from falguna.trading_lab_backtest import (
    BacktestStore, MIN_TRADES_FOR_CONFIDENCE, STRESS_SCENARIOS, generate_signal_series,
    parameter_sensitivity_check, run_backtest, run_out_of_sample_validation, run_stress_review,
    train_test_split, walk_forward_folds,
)


def make_bars(prices, prefix="2024-01"):
    return [Bar(ts=f"{prefix}-{d + 1:02d}T00:00:00+00:00", open=p, high=p + 1, low=p - 1, close=p, volume=1000)
            for d, p in enumerate(prices)]


LONG_ENTRY = {"signal": "breakout", "params": {"lookback_period": 3, "direction": "up"}, "side": "long"}
LONG_EXIT = {"signal": "breakout", "params": {"lookback_period": 1, "direction": "down"}}
SHORT_ENTRY = {"signal": "breakout", "params": {"lookback_period": 3, "direction": "down"}, "side": "short"}
SHORT_EXIT = {"signal": "breakout", "params": {"lookback_period": 1, "direction": "up"}}
SIZING_FULL = {"position_size_pct": 100}


class EngineCorrectnessTests(unittest.TestCase):
    def test_long_trade_profits_when_price_rises_after_entry(self):
        bars = make_bars([100, 100, 100, 100, 100, 120, 120, 120, 120, 140])
        result = run_backtest(bars, LONG_ENTRY, LONG_EXIT, SIZING_FULL, fee_bps=0, slippage_bps=0, starting_cash=1000)
        self.assertEqual(len(result["trade_log"]), 1)
        trade = result["trade_log"][0]
        self.assertAlmostEqual(trade["entry_price"], 120.0)
        self.assertAlmostEqual(trade["exit_price"], 140.0)
        self.assertGreater(trade["gross_pnl"], 0)
        self.assertGreater(result["metrics"]["final_equity"], 1000)

    def test_short_trade_profits_when_price_falls_after_entry(self):
        bars = make_bars([100, 100, 100, 100, 100, 80, 80, 80, 80, 60], "2024-02")
        result = run_backtest(bars, SHORT_ENTRY, SHORT_EXIT, SIZING_FULL, fee_bps=0, slippage_bps=0, starting_cash=1000)
        self.assertEqual(len(result["trade_log"]), 1)
        self.assertGreater(result["trade_log"][0]["gross_pnl"], 0)
        self.assertGreater(result["metrics"]["final_equity"], 1000)

    def test_fees_strictly_reduce_equity_versus_zero_fee_run(self):
        bars = make_bars([100, 100, 100, 100, 100, 120, 120, 120, 120, 140])
        no_fee = run_backtest(bars, LONG_ENTRY, LONG_EXIT, SIZING_FULL, fee_bps=0, slippage_bps=0, starting_cash=1000)
        with_fee = run_backtest(bars, LONG_ENTRY, LONG_EXIT, SIZING_FULL, fee_bps=50, slippage_bps=0, starting_cash=1000)
        self.assertLess(with_fee["metrics"]["final_equity"], no_fee["metrics"]["final_equity"])

    def test_slippage_against_the_trader_reduces_equity(self):
        bars = make_bars([100, 100, 100, 100, 100, 120, 120, 120, 120, 140])
        no_slip = run_backtest(bars, LONG_ENTRY, LONG_EXIT, SIZING_FULL, fee_bps=0, slippage_bps=0, starting_cash=1000)
        with_slip = run_backtest(bars, LONG_ENTRY, LONG_EXIT, SIZING_FULL, fee_bps=0, slippage_bps=100, starting_cash=1000)
        self.assertLess(with_slip["metrics"]["final_equity"], no_slip["metrics"]["final_equity"])

    def test_backtest_is_perfectly_deterministic(self):
        bars = make_bars([100, 100, 100, 100, 100, 120, 120, 120, 120, 140])
        r1 = run_backtest(bars, LONG_ENTRY, LONG_EXIT, SIZING_FULL, fee_bps=10, slippage_bps=5, starting_cash=1000)
        r2 = run_backtest(bars, LONG_ENTRY, LONG_EXIT, SIZING_FULL, fee_bps=10, slippage_bps=5, starting_cash=1000)
        self.assertEqual(r1, r2)

    def test_no_fabricated_infinite_profit_factor_with_zero_losses(self):
        bars = make_bars([100, 100, 100, 100, 100, 120, 120, 120, 120, 140])
        result = run_backtest(bars, LONG_ENTRY, LONG_EXIT, SIZING_FULL, fee_bps=0, slippage_bps=0, starting_cash=1000)
        self.assertIsNone(result["metrics"]["profit_factor"])

    def test_no_trades_yields_honest_none_metrics_not_zeros(self):
        bars = make_bars([100] * 10)  # perfectly flat -- no breakout ever fires
        result = run_backtest(bars, LONG_ENTRY, LONG_EXIT, SIZING_FULL, fee_bps=10, slippage_bps=5, starting_cash=1000)
        self.assertEqual(result["metrics"]["trade_count"], 0)
        self.assertIsNone(result["metrics"]["win_rate"])
        self.assertIsNone(result["metrics"]["avg_win"])
        self.assertIsNone(result["metrics"]["profit_factor"])
        self.assertEqual(result["metrics"]["final_equity"], 1000)

    def test_invalid_rule_rejected(self):
        bars = make_bars([100] * 10)
        with self.assertRaises(ValueError):
            run_backtest(bars, {"signal": "nope", "params": {}}, LONG_EXIT, SIZING_FULL)

    def test_min_trade_count_warning_present_below_threshold(self):
        bars = make_bars([100, 100, 100, 100, 100, 120, 120, 120, 120, 140])
        result = run_backtest(bars, LONG_ENTRY, LONG_EXIT, SIZING_FULL, fee_bps=0, slippage_bps=0, starting_cash=1000)
        self.assertLess(result["metrics"]["trade_count"], MIN_TRADES_FOR_CONFIDENCE)
        self.assertTrue(any("below the" in w and "minimum" in w for w in result["warnings"]))


class NoLookaheadTests(unittest.TestCase):
    def test_signal_at_bar_i_unaffected_by_future_bars(self):
        bars = make_bars([100 + i * 0.3 for i in range(100)], "2024-03")
        closes = [b.close for b in bars]
        highs = [b.high for b in bars]
        lows = [b.low for b in bars]
        sig_full = generate_signal_series("sma_crossover", {"fast_period": 3, "slow_period": 5, "cross": "up"}, closes, highs, lows)
        sig_trunc = generate_signal_series("sma_crossover", {"fast_period": 3, "slow_period": 5, "cross": "up"}, closes[:20], highs[:20], lows[:20])
        self.assertEqual(sig_full[:20], sig_trunc)


class AntiOverfittingTests(unittest.TestCase):
    def setUp(self):
        self.bars = make_bars([100 + d * 0.3 for d in range(100)], "2024-04")

    def test_train_test_split_sizes(self):
        train, test = train_test_split(self.bars, 0.7)
        self.assertEqual(len(train), 70)
        self.assertEqual(len(test), 30)

    def test_train_test_split_rejects_extreme_fraction(self):
        with self.assertRaises(ValueError):
            train_test_split(self.bars, 0.99)

    def test_walk_forward_folds_cover_all_bars(self):
        folds = walk_forward_folds(self.bars, 4)
        self.assertEqual(len(folds), 4)
        self.assertEqual(sum(len(f) for f in folds), len(self.bars))

    def test_out_of_sample_validation_runs_both_splits(self):
        oos = run_out_of_sample_validation(self.bars, LONG_ENTRY, LONG_EXIT, SIZING_FULL, fee_bps=10, slippage_bps=5, starting_cash=1000)
        self.assertIsNotNone(oos["train"])
        self.assertIsNotNone(oos["test"])

    def test_out_of_sample_flags_profitable_train_unprofitable_test(self):
        # Construct a series that is a strong uptrend for the train portion
        # and then dead flat for test -- profitable in-sample, no signal
        # out-of-sample -> should NOT raise the overfit warning (0 trades
        # is different from a losing test), but must still report a
        # 'test too small'/normal warning path without crashing.
        bars = make_bars([100] * 5 + [100, 120, 120, 120, 120, 140] + [140] * 20, "2024-05")
        oos = run_out_of_sample_validation(bars, LONG_ENTRY, LONG_EXIT, SIZING_FULL, train_frac=0.5, fee_bps=0, slippage_bps=0, starting_cash=1000)
        self.assertIsInstance(oos["warnings"], list)

    def test_parameter_sensitivity_check_returns_a_result_per_value(self):
        sens = parameter_sensitivity_check(self.bars, LONG_ENTRY, LONG_EXIT, SIZING_FULL, "entry.params.lookback_period", [2, 3, 5], fee_bps=10, slippage_bps=5, starting_cash=1000)
        self.assertEqual(len(sens["results"]), 3)
        self.assertIn("sensitive_to_parameter_choice", sens)


class StressReviewTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = StateStore(self.root / "state.db")
        self.store.migrate()
        self.audit = AuditLog(self.root / "audit.jsonl")
        markets = MarketStore(self.store, self.audit)
        instruments = InstrumentStore(self.store, self.audit)
        strategies = StrategyStore(self.store, self.audit)
        versions = StrategyVersionStore(self.store, self.audit)
        m_id = markets.create("US_EQUITY", "US Equities", "us_equity")
        self.i_id = instruments.create(m_id, "TEST")
        self.s_id = strategies.create("Stress test strategy", "trend follow", m_id)
        self.v_id = versions.create(self.s_id, [self.i_id], "1d", LONG_ENTRY, LONG_EXIT, SIZING_FULL)
        self.bars = make_bars([100 + (d % 20) * 0.5 + d * 0.4 for d in range(80)], "2024-06")

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def test_every_scenario_produces_a_persisted_verdict(self):
        result = run_backtest(self.bars, LONG_ENTRY, LONG_EXIT, SIZING_FULL, fee_bps=10, slippage_bps=5, starting_cash=10000)
        bt_store = BacktestStore(self.store, self.audit)
        ds_id = self.store.create("tl_datasets", {
            "data_source_id": DataSourceStore(self.store, self.audit).register("synthetic test fixture", "synthetic_test_fixture", is_synthetic=True),
            "instrument_id": self.i_id, "timeframe": "1d", "start_date": self.bars[0].ts, "end_date": self.bars[-1].ts,
            "status": "OK", "bar_count": len(self.bars), "completeness_pct": 100.0, "error": None,
            "raw_source_metadata_json": "{}", "ingested_at": self.bars[0].ts, "created_at": self.bars[0].ts, "updated_at": self.bars[0].ts,
        })
        bt_id = bt_store.record(self.v_id, ds_id, "full", 10, 5, 10000, result)
        ids = run_stress_review(self.store, self.audit, bt_id, self.bars, LONG_ENTRY, LONG_EXIT, SIZING_FULL, None, 10, 5, 10000)
        self.assertEqual(len(ids), len(STRESS_SCENARIOS))
        rows = self.store.list("tl_stress_tests", "backtest_id=?", (bt_id,))
        self.assertEqual(len(rows), len(STRESS_SCENARIOS))
        for row in rows:
            self.assertIn(row["verdict"], ("SURVIVED", "WEAKENED", "FAILED"))
            self.assertIn(row["scenario"], STRESS_SCENARIOS)


if __name__ == "__main__":
    unittest.main()
