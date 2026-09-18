"""Tests for falguna/trading_lab_risk_paper.py -- risk engine, paper trading
engine, paper portfolio, performance snapshots. Sections 12-15 of the
Trading Lab spec. PAPER/SIMULATED ONLY -- every test here also asserts the
self-labeling (is_paper/is_real_money) that must appear everywhere these
numbers are read back.
"""

import tempfile
import unittest
from pathlib import Path

from falguna.audit import AuditLog
from falguna.store import StateStore
from falguna.ttt_hq import NeedsAryanQueue
from falguna.trading_lab_data import InstrumentStore, MarketStore
from falguna.trading_lab_strategy import StrategyStore
from falguna.trading_lab_risk_paper import (
    PaperAccountStore, PaperTradingEngine, RiskEngine, RiskLimitStore,
    portfolio_summary, record_performance_snapshot, strategy_performance,
)


class PaperTradingBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = StateStore(self.root / "state.db")
        self.store.migrate()
        self.audit = AuditLog(self.root / "audit.jsonl")
        self.needs_aryan = NeedsAryanQueue(self.store, self.audit)
        markets = MarketStore(self.store, self.audit)
        instruments = InstrumentStore(self.store, self.audit)
        strategies = StrategyStore(self.store, self.audit)
        self.m_id = markets.create("US_EQUITY", "US Equities", "us_equity")
        self.i_id = instruments.create(self.m_id, "AAPL")
        self.s_id = strategies.create("Paper test strategy", "test hypothesis", self.m_id)
        self.strategies = strategies
        # Order-accounting/risk tests below exercise PaperTradingEngine
        # against an already-approved strategy -- advance it through the
        # real lifecycle to PAPER_ACTIVE so those tests aren't also
        # incidentally exercising the strategy-status gate (that gate has
        # its own dedicated tests in StrategyStatusGateTests below).
        strategies.transition(self.s_id, "RESEARCHING", "test_setup")
        strategies.transition(self.s_id, "BACKTESTING", "test_setup")
        strategies.transition(self.s_id, "REVIEW", "test_setup")
        strategies.transition(self.s_id, "PAPER_APPROVED", "test_setup")
        strategies.transition(self.s_id, "PAPER_ACTIVE", "test_setup")
        self.accounts = PaperAccountStore(self.store, self.audit)
        self.risk_limits = RiskLimitStore(self.store, self.audit)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _engine(self):
        risk_engine = RiskEngine(self.store, self.audit, self.needs_aryan)
        return PaperTradingEngine(self.store, self.audit, risk_engine), risk_engine


class OrderAccountingTests(PaperTradingBase):
    def test_insufficient_cash_rejected_without_moving_cash(self):
        engine, _ = self._engine()
        tiny = self.accounts.create("Tiny", 100)
        order_id = engine.submit_order(tiny, self.s_id, self.i_id, "BUY", 10, 100.0)
        row = self.store.get("tl_paper_orders", order_id)
        self.assertEqual(row["status"], "REJECTED")
        self.assertIn("cash", row["reject_reason"])
        self.assertEqual(self.accounts.get(tiny)["cash"], 100)

    def test_round_trip_cash_and_realized_pnl_exact(self):
        engine, _ = self._engine()
        acc = self.accounts.create("Main", 10000)
        o1 = engine.submit_order(acc, self.s_id, self.i_id, "BUY", 10, 100.0, fee_bps=0, slippage_bps=0)
        self.assertEqual(self.store.get("tl_paper_orders", o1)["status"], "FILLED")
        self.assertAlmostEqual(self.accounts.get(acc)["cash"], 9000.0)
        o2 = engine.submit_order(acc, self.s_id, self.i_id, "SELL", 10, 110.0, fee_bps=0, slippage_bps=0)
        self.assertEqual(self.store.get("tl_paper_orders", o2)["status"], "FILLED")
        self.assertAlmostEqual(self.accounts.get(acc)["cash"], 10100.0)
        trades = self.store.list("tl_trades", "paper_account_id=?", (acc,))
        self.assertEqual(len(trades), 1)
        self.assertAlmostEqual(trades[0]["realized_pnl"], 100.0)
        position = self.store.list("tl_paper_positions", "paper_account_id=?", (acc,))[0]
        self.assertEqual(position["qty"], 0)

    def test_oversized_opening_order_rejected_by_risk_limit(self):
        engine, _ = self._engine()
        acc = self.accounts.create("Main", 10000)
        self.risk_limits.create("global", max_risk_per_trade_pct=20)
        order_id = engine.submit_order(acc, self.s_id, self.i_id, "BUY", 1000, 100.0)
        row = self.store.get("tl_paper_orders", order_id)
        self.assertEqual(row["status"], "REJECTED")
        self.assertIn("max_risk_per_trade_pct", row["reject_reason"])

    def test_closing_order_never_trapped_by_a_tight_risk_limit(self):
        engine, _ = self._engine()
        acc = self.accounts.create("Main", 10000)
        self.risk_limits.create("global", max_risk_per_trade_pct=20)
        buy_id = engine.submit_order(acc, self.s_id, self.i_id, "BUY", 20, 100.0, fee_bps=0, slippage_bps=0)
        self.assertEqual(self.store.get("tl_paper_orders", buy_id)["status"], "FILLED")
        self.risk_limits.create("global", max_risk_per_trade_pct=1)  # now far too tight for a NEW order
        sell_id = engine.submit_order(acc, self.s_id, self.i_id, "SELL", 20, 100.0, fee_bps=0, slippage_bps=0)
        self.assertEqual(self.store.get("tl_paper_orders", sell_id)["status"], "FILLED",
                          "an order that only reduces/closes an existing position must never be blocked by a percentage risk limit")

    def test_position_flip_order_rejected(self):
        engine, _ = self._engine()
        acc = self.accounts.create("Main", 10000)
        engine.submit_order(acc, self.s_id, self.i_id, "BUY", 5, 100.0, fee_bps=0, slippage_bps=0)
        order_id = engine.submit_order(acc, self.s_id, self.i_id, "SELL", 20, 100.0, fee_bps=0, slippage_bps=0)
        row = self.store.get("tl_paper_orders", order_id)
        self.assertEqual(row["status"], "REJECTED")
        self.assertIn("flip", row["reject_reason"])


class PortfolioAndPerformanceTests(PaperTradingBase):
    def test_portfolio_summary_always_self_labels_paper(self):
        engine, _ = self._engine()
        acc = self.accounts.create("Main", 10000)
        engine.submit_order(acc, self.s_id, self.i_id, "BUY", 10, 100.0, fee_bps=0, slippage_bps=0)
        summary = portfolio_summary(self.store, acc)
        self.assertIs(summary["is_paper"], True)
        self.assertIs(summary["is_real_money"], False)
        self.assertIn("PAPER", summary["note"])

    def test_strategy_performance_always_self_labels_paper(self):
        engine, _ = self._engine()
        acc = self.accounts.create("Main", 10000)
        engine.submit_order(acc, self.s_id, self.i_id, "BUY", 10, 100.0, fee_bps=0, slippage_bps=0)
        engine.submit_order(acc, self.s_id, self.i_id, "SELL", 10, 110.0, fee_bps=0, slippage_bps=0)
        perf = strategy_performance(self.store, self.s_id)
        self.assertIs(perf["is_paper"], True)
        self.assertEqual(perf["trade_count"], 1)
        self.assertAlmostEqual(perf["total_realized_pnl"], 100.0)

    def test_portfolio_summary_unknown_account_raises(self):
        with self.assertRaises(ValueError):
            portfolio_summary(self.store, "nonexistent")


class RiskBreachTests(PaperTradingBase):
    def test_drawdown_breach_creates_event_and_needs_aryan_item(self):
        acc = self.accounts.create("Drawdown test", 10000)
        self.risk_limits.create("global", max_portfolio_drawdown_pct=5)
        engine, risk_engine = self._engine()
        record_performance_snapshot(self.store, self.audit, acc, None)
        buy_id = engine.submit_order(acc, self.s_id, self.i_id, "BUY", 10, 100.0, fee_bps=0, slippage_bps=0)
        self.assertEqual(self.store.get("tl_paper_orders", buy_id)["status"], "FILLED")
        sell_id = engine.submit_order(acc, self.s_id, self.i_id, "SELL", 10, 50.0, fee_bps=0, slippage_bps=0)
        self.assertEqual(self.store.get("tl_paper_orders", sell_id)["status"], "FILLED",
                          "closing a losing position must never itself be blocked")

        breach_rows = self.store.list("tl_risk_breach_events", "paper_account_id=?", (acc,))
        self.assertGreaterEqual(len(breach_rows), 1)
        self.assertEqual(breach_rows[0]["breach_type"], "drawdown_breach")

        items = self.store.list("needs_aryan_items", "ref_id=?", (acc,))
        self.assertTrue(any(i["kind"] == "trading_risk_breach" for i in items))

    def test_new_opening_entry_blocked_while_breach_open_but_exit_allowed(self):
        acc = self.accounts.create("Drawdown test 2", 10000)
        self.risk_limits.create("global", max_portfolio_drawdown_pct=5)
        engine, risk_engine = self._engine()
        record_performance_snapshot(self.store, self.audit, acc, None)
        engine.submit_order(acc, self.s_id, self.i_id, "BUY", 10, 100.0, fee_bps=0, slippage_bps=0)
        engine.submit_order(acc, self.s_id, self.i_id, "SELL", 10, 50.0, fee_bps=0, slippage_bps=0)
        blocked_id = engine.submit_order(acc, self.s_id, self.i_id, "BUY", 1, 100.0)
        row = self.store.get("tl_paper_orders", blocked_id)
        self.assertEqual(row["status"], "REJECTED")
        self.assertIn("breach", row["reject_reason"])


class StrategyStatusGateTests(PaperTradingBase):
    """Regression coverage for the strategy-status gate on submit_order:
    a paper order must never be acceptable for a strategy that has not
    been through Trading Council + explicit Aryan approval to PAPER_ACTIVE
    (Sections 17/18) -- discovered as a real gap during the Section 24
    end-to-end QA pass (the engine happily filled orders for a REJECTED
    strategy) and fixed at the source in trading_lab_risk_paper.py.
    """

    def test_order_rejected_outright_for_idea_stage_strategy(self):
        strategies = self.strategies
        idea_id = strategies.create("Still an idea", "untested hypothesis", self.m_id)
        engine, _ = self._engine()
        acc = self.accounts.create("Gate test", 10000)
        with self.assertRaises(ValueError) as ctx:
            engine.submit_order(acc, idea_id, self.i_id, "BUY", 10, 100.0)
        self.assertIn("PAPER_ACTIVE", str(ctx.exception))
        # and no order row / cash movement should have happened
        self.assertEqual(self.store.list("tl_paper_orders", "strategy_id=?", (idea_id,)), [])
        self.assertEqual(self.accounts.get(acc)["cash"], 10000)

    def test_order_rejected_for_rejected_strategy(self):
        strategies = self.strategies
        rejected_id = strategies.create("Will be rejected", "bad hypothesis", self.m_id)
        strategies.transition(rejected_id, "RESEARCHING", "test")
        strategies.transition(rejected_id, "REJECTED", "test")
        engine, _ = self._engine()
        acc = self.accounts.create("Gate test 2", 10000)
        with self.assertRaises(ValueError):
            engine.submit_order(acc, rejected_id, self.i_id, "BUY", 10, 100.0)

    def test_order_allowed_for_paper_active_strategy(self):
        # self.s_id is already PAPER_ACTIVE via the shared fixture.
        engine, _ = self._engine()
        acc = self.accounts.create("Gate test 3", 10000)
        order_id = engine.submit_order(acc, self.s_id, self.i_id, "BUY", 10, 100.0, fee_bps=0, slippage_bps=0)
        self.assertEqual(self.store.get("tl_paper_orders", order_id)["status"], "FILLED")

    def test_paused_strategy_may_close_but_not_open(self):
        strategies = self.strategies
        engine, _ = self._engine()
        acc = self.accounts.create("Gate test 4", 10000)
        # Open a position while PAPER_ACTIVE, then pause the strategy.
        buy_id = engine.submit_order(acc, self.s_id, self.i_id, "BUY", 10, 100.0, fee_bps=0, slippage_bps=0)
        self.assertEqual(self.store.get("tl_paper_orders", buy_id)["status"], "FILLED")
        strategies.transition(self.s_id, "PAUSED", "test")

        # A closing/reducing SELL must still be allowed so the position can be wound down.
        sell_id = engine.submit_order(acc, self.s_id, self.i_id, "SELL", 10, 100.0, fee_bps=0, slippage_bps=0)
        self.assertEqual(self.store.get("tl_paper_orders", sell_id)["status"], "FILLED")

        # But a fresh opening BUY while PAUSED must be rejected outright.
        with self.assertRaises(ValueError) as ctx:
            engine.submit_order(acc, self.s_id, self.i_id, "BUY", 5, 100.0)
        self.assertIn("PAUSED", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
