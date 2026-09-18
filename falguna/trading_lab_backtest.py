"""TTT Trading Lab v1 -- Pass B: Deterministic Backtest Engine + Validation.

Sections 9, 10, 11 of the TTT TRADING LAB V1 spec: a deterministic
bar-by-bar backtest engine, anti-overfitting controls (train/test split,
out-of-sample evaluation, parameter-sensitivity checks, minimum-trade-count
and lookahead/survivorship warnings), and a Red-Team stress/adversarial
reviewer that actively tries to disprove a strategy under harsher
conditions (higher fees/slippage, delayed fills, worse fills, regime
changes, missed trades, reduced liquidity).

PAPER / SIMULATED ONLY. A backtest never touches money or a real order
path -- it is pure arithmetic over historical or synthetic bars. See
falguna/trading_lab_risk_paper.py for the (also paper-only) live-forward
simulation engine.

No-lookahead-by-construction: every indicator/signal value at bar i is
computed using bars[0..i] only, and a signal that fires at bar i is filled
at bar i's own close (or later, if fill_delay_bars > 0) -- never at a price
from before the signal existed.
"""

from __future__ import annotations

import json as _json
import random
import statistics
from typing import Any, Dict, List, Optional, Tuple

from .audit import AuditLog
from .store import StateStore, utcnow
from .trading_lab_data import Bar
from .trading_lab_strategy import SIGNAL_REGISTRY, validate_rule

MIN_TRADES_FOR_CONFIDENCE = 10


# ---------------------------------------------------------------------------
# Indicators (all causal: value at index i uses only values[0..i])
# ---------------------------------------------------------------------------

def sma(values: List[float], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = []
    for i in range(len(values)):
        if i + 1 < period:
            out.append(None)
        else:
            out.append(sum(values[i + 1 - period:i + 1]) / period)
    return out


def rsi(values: List[float], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if len(values) < period + 1:
        return out
    gains = [max(0.0, values[i] - values[i - 1]) for i in range(1, len(values))]
    losses = [max(0.0, values[i - 1] - values[i]) for i in range(1, len(values))]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    out[period] = 100.0 if avg_loss == 0 else 100.0 - (100.0 / (1 + avg_gain / avg_loss))
    for i in range(period + 1, len(values)):
        g = gains[i - 1]
        l = losses[i - 1]
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
        out[i] = 100.0 if avg_loss == 0 else 100.0 - (100.0 / (1 + avg_gain / avg_loss))
    return out


def rolling_extreme(values: List[float], period: int, kind: str) -> List[Optional[float]]:
    """Rolling max/min over the PRIOR `period` bars (excludes the current
    bar), so a breakout at bar i is genuinely a break of the preceding
    range, not a tautology against itself."""
    out: List[Optional[float]] = []
    fn = max if kind == "max" else min
    for i in range(len(values)):
        if i < period:
            out.append(None)
        else:
            out.append(fn(values[i - period:i]))
    return out


def generate_signal_series(signal: str, params: Dict[str, Any], closes: List[float],
                            highs: List[float], lows: List[float]) -> List[bool]:
    """Returns a boolean list the same length as closes: True at index i
    means the signal fires at bar i, using only data through bar i."""
    n = len(closes)
    if signal == "sma_crossover":
        fast = sma(closes, int(params["fast_period"]))
        slow = sma(closes, int(params["slow_period"]))
        cross = params.get("cross", "up")
        out = [False] * n
        for i in range(1, n):
            if fast[i] is None or slow[i] is None or fast[i - 1] is None or slow[i - 1] is None:
                continue
            if cross == "up":
                out[i] = fast[i - 1] <= slow[i - 1] and fast[i] > slow[i]
            else:
                out[i] = fast[i - 1] >= slow[i - 1] and fast[i] < slow[i]
        return out
    if signal == "rsi_threshold":
        series = rsi(closes, int(params["period"]))
        threshold = float(params["threshold"])
        cross = params.get("cross", "below")
        out = [False] * n
        for i in range(1, n):
            if series[i] is None or series[i - 1] is None:
                continue
            if cross == "below":
                out[i] = series[i - 1] >= threshold and series[i] < threshold
            else:
                out[i] = series[i - 1] <= threshold and series[i] > threshold
        return out
    if signal == "breakout":
        period = int(params["lookback_period"])
        direction = params.get("direction", "up")
        if direction == "up":
            ref = rolling_extreme(highs, period, "max")
            return [ref[i] is not None and closes[i] > ref[i] for i in range(n)]
        ref = rolling_extreme(lows, period, "min")
        return [ref[i] is not None and closes[i] < ref[i] for i in range(n)]
    raise ValueError(f"unknown signal: {signal}")


# ---------------------------------------------------------------------------
# Deterministic backtest engine
# ---------------------------------------------------------------------------

def run_backtest(bars: List[Bar], entry_rules: Dict[str, Any], exit_rules: Dict[str, Any],
                  sizing_logic: Dict[str, Any], stop_logic: Optional[Dict[str, Any]] = None,
                  fee_bps: float = 10.0, slippage_bps: float = 5.0, starting_cash: float = 10000.0,
                  fill_delay_bars: int = 0, entry_slippage_bps_extra: float = 0.0,
                  exit_slippage_bps_extra: float = 0.0, skip_entry_seed: Optional[int] = None,
                  skip_entry_probability: float = 0.0) -> Dict[str, Any]:
    """Runs a single-position, single-instrument, deterministic bar-by-bar
    backtest and returns {equity_curve, trade_log, metrics, warnings}.

    Every parameter that affects the result is an explicit argument (never
    a hidden default silently changing behavior), so a report can always
    state exactly what assumptions produced a given number (Section 20).
    """
    err = validate_rule(entry_rules) or validate_rule(exit_rules)
    if err:
        raise ValueError(f"invalid rules: {err}")
    side = entry_rules.get("side", "long")
    if side not in ("long", "short"):
        raise ValueError("entry_rules side must be 'long' or 'short'")

    closes = [b.close for b in bars]
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    n = len(bars)
    warnings: List[str] = []
    if n < 30:
        warnings.append(f"only {n} bars available -- results are not statistically meaningful")

    entry_signal = generate_signal_series(entry_rules["signal"], entry_rules["params"], closes, highs, lows)
    exit_signal = generate_signal_series(exit_rules["signal"], exit_rules["params"], closes, highs, lows)

    skip_rng = random.Random(skip_entry_seed) if skip_entry_seed is not None else None
    stop_loss_pct = (stop_logic or {}).get("stop_loss_pct")
    take_profit_pct = (stop_logic or {}).get("take_profit_pct")
    size_pct = sizing_logic.get("position_size_pct")
    size_units = sizing_logic.get("position_size_units")

    cash = starting_cash
    qty = 0.0  # signed: positive = long units held, negative = short units held
    entry_price = None
    entry_bar_index = None
    entry_fee = 0.0
    pending_entry_at: Optional[int] = None
    trade_log: List[Dict[str, Any]] = []
    equity_curve: List[Dict[str, Any]] = []
    bars_in_position = 0

    def fee_amount(notional: float) -> float:
        return abs(notional) * (fee_bps / 10000.0)

    for i in range(n):
        price = closes[i]

        # process a fill that was queued `fill_delay_bars` ago
        if pending_entry_at is not None and i >= pending_entry_at and qty == 0:
            fill_price = price * (1 + (slippage_bps + entry_slippage_bps_extra) / 10000.0 * (1 if side == "long" else -1))
            if size_units:
                units = float(size_units)
            else:
                notional = cash * (float(size_pct) / 100.0)
                units = notional / fill_price if fill_price > 0 else 0.0
            fee = fee_amount(units * fill_price)
            if side == "long":
                cash -= units * fill_price + fee  # pay full notional + fee to buy
                qty = units
            else:
                cash += units * fill_price - fee  # receive full notional (less fee) to open short
                qty = -units
            entry_price = fill_price
            entry_bar_index = i
            entry_fee = fee
            pending_entry_at = None

        # manage an open position: check stop/take-profit first (conservative), then exit signal
        if qty != 0 and entry_price is not None:
            bars_in_position += 1
            exit_now = False
            exit_reason = None
            if side == "long":
                if stop_loss_pct and lows[i] <= entry_price * (1 - stop_loss_pct / 100.0):
                    exit_now, exit_reason = True, "stop_loss"
                elif take_profit_pct and highs[i] >= entry_price * (1 + take_profit_pct / 100.0):
                    exit_now, exit_reason = True, "take_profit"
            else:
                if stop_loss_pct and highs[i] >= entry_price * (1 + stop_loss_pct / 100.0):
                    exit_now, exit_reason = True, "stop_loss"
                elif take_profit_pct and lows[i] <= entry_price * (1 - take_profit_pct / 100.0):
                    exit_now, exit_reason = True, "take_profit"
            if not exit_now and exit_signal[i] and i > (entry_bar_index or 0):
                exit_now, exit_reason = True, "exit_signal"
            if not exit_now and i == n - 1:
                exit_now, exit_reason = True, "end_of_data"

            if exit_now:
                units = abs(qty)
                exit_price = price * (1 - (slippage_bps + exit_slippage_bps_extra) / 10000.0 * (1 if side == "long" else -1))
                exit_fee = fee_amount(units * exit_price)
                if side == "long":
                    cash += units * exit_price - exit_fee  # sell the units back
                    gross_pnl = (exit_price - entry_price) * units
                else:
                    cash -= units * exit_price + exit_fee  # buy back to cover the short
                    gross_pnl = (entry_price - exit_price) * units
                total_fees = entry_fee + exit_fee
                realized_pnl = gross_pnl - total_fees
                trade_log.append({
                    "side": side, "qty": units, "entry_price": entry_price, "exit_price": exit_price,
                    "entry_index": entry_bar_index, "exit_index": i, "entry_ts": bars[entry_bar_index].ts,
                    "exit_ts": bars[i].ts, "gross_pnl": round(gross_pnl, 4), "fees": round(total_fees, 4),
                    "realized_pnl": round(realized_pnl, 4), "exit_reason": exit_reason,
                })
                qty = 0.0
                entry_price = None
                entry_bar_index = None
                entry_fee = 0.0

        # look for a new entry only when flat
        if qty == 0 and pending_entry_at is None and entry_signal[i]:
            if skip_rng is not None and skip_rng.random() < skip_entry_probability:
                pass  # simulated missed trade (Red-Team scenario)
            else:
                pending_entry_at = i + fill_delay_bars

        mark_price = closes[i]
        # equity = cash (already debited/credited the full entry notional) + current position value.
        # Correct for both long (qty>0) and short (qty<0) since qty is signed.
        position_value = qty * mark_price if qty != 0 else 0.0
        equity = cash + position_value
        equity_curve.append({"ts": bars[i].ts, "equity": round(equity, 4), "cash": round(cash, 4)})

    final_equity = equity_curve[-1]["equity"] if equity_curve else starting_cash
    metrics = _compute_metrics(trade_log, equity_curve, starting_cash, final_equity, n, bars_in_position)
    if metrics["trade_count"] < MIN_TRADES_FOR_CONFIDENCE:
        warnings.append(f"only {metrics['trade_count']} trades -- below the {MIN_TRADES_FOR_CONFIDENCE}-trade minimum for statistical confidence")
    warnings.append("lookahead-safety: signals are computed causally (bar i uses only bars[0..i]); "
                     "fills use bar i's own close, which was legitimately observable at that bar's close.")
    warnings.append("survivorship-bias note: this backtest runs against a single, already-selected instrument/dataset -- "
                     "it cannot detect survivorship bias in how that instrument or dataset was chosen upstream.")

    return {"equity_curve": equity_curve, "trade_log": trade_log, "metrics": metrics, "warnings": warnings}


def _compute_metrics(trade_log: List[Dict[str, Any]], equity_curve: List[Dict[str, Any]],
                      starting_cash: float, final_equity: float, n_bars: int, bars_in_position: int) -> Dict[str, Any]:
    net_return = (final_equity - starting_cash) / starting_cash if starting_cash else None
    gross_pnl_total = sum(t["gross_pnl"] for t in trade_log)
    gross_return = gross_pnl_total / starting_cash if starting_cash else None

    equities = [e["equity"] for e in equity_curve]
    peak = -float("inf")
    max_dd = 0.0
    for eq in equities:
        peak = max(peak, eq)
        if peak > 0:
            max_dd = max(max_dd, (peak - eq) / peak)

    wins = [t["realized_pnl"] for t in trade_log if t["realized_pnl"] > 0]
    losses = [t["realized_pnl"] for t in trade_log if t["realized_pnl"] <= 0]
    trade_count = len(trade_log)
    win_rate = len(wins) / trade_count if trade_count else None
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (None if gross_win == 0 else float("inf"))
    if profit_factor == float("inf"):
        profit_factor = None  # no losing trades yet to compare against -- report as None, not a fabricated infinity
    expectancy = (sum(t["realized_pnl"] for t in trade_log) / trade_count) if trade_count else None
    avg_win = (gross_win / len(wins)) if wins else None
    avg_loss = (sum(losses) / len(losses)) if losses else None
    exposure = (bars_in_position / n_bars) if n_bars else None

    daily_returns = []
    for i in range(1, len(equities)):
        if equities[i - 1] != 0:
            daily_returns.append((equities[i] - equities[i - 1]) / equities[i - 1])
    volatility = statistics.pstdev(daily_returns) if len(daily_returns) >= 2 else None
    sharpe_like = None
    if volatility and volatility > 0:
        mean_r = statistics.mean(daily_returns)
        sharpe_like = (mean_r / volatility) * (252 ** 0.5)

    return {
        "net_return": round(net_return, 6) if net_return is not None else None,
        "gross_return": round(gross_return, 6) if gross_return is not None else None,
        "max_drawdown_pct": round(max_dd * 100, 4),
        "win_rate": round(win_rate, 4) if win_rate is not None else None,
        "profit_factor": round(profit_factor, 4) if profit_factor is not None else None,
        "expectancy": round(expectancy, 4) if expectancy is not None else None,
        "avg_win": round(avg_win, 4) if avg_win is not None else None,
        "avg_loss": round(avg_loss, 4) if avg_loss is not None else None,
        "trade_count": trade_count,
        "exposure_pct": round(exposure * 100, 2) if exposure is not None else None,
        "volatility": round(volatility, 6) if volatility is not None else None,
        "sharpe_like": round(sharpe_like, 4) if sharpe_like is not None else None,
        "final_equity": round(final_equity, 4),
        "starting_cash": starting_cash,
    }


# ---------------------------------------------------------------------------
# Anti-overfitting controls (Section 10)
# ---------------------------------------------------------------------------

def train_test_split(bars: List[Bar], train_frac: float = 0.7) -> Tuple[List[Bar], List[Bar]]:
    if not (0.1 <= train_frac <= 0.9):
        raise ValueError("train_frac must be between 0.1 and 0.9")
    cut = int(len(bars) * train_frac)
    return bars[:cut], bars[cut:]


def walk_forward_folds(bars: List[Bar], n_folds: int = 3) -> List[List[Bar]]:
    """Foundation for walk-forward validation (Section 10): splits the
    dataset into n_folds contiguous, chronological folds. Each fold can be
    backtested independently to check for consistency across sub-periods,
    rather than relying on one single full-history number."""
    if n_folds < 2:
        raise ValueError("n_folds must be >= 2")
    fold_size = len(bars) // n_folds
    if fold_size < 5:
        return [bars]
    return [bars[i * fold_size: (i + 1) * fold_size] for i in range(n_folds - 1)] + [bars[(n_folds - 1) * fold_size:]]


def parameter_sensitivity_check(bars: List[Bar], entry_rules: Dict[str, Any], exit_rules: Dict[str, Any],
                                 sizing_logic: Dict[str, Any], param_path: str, values: List[Any],
                                 **backtest_kwargs) -> Dict[str, Any]:
    """Section 10: re-runs the backtest across a small grid of one
    parameter's neighboring values (e.g. fast_period in [8, 10, 12]) to
    check whether the result is a robust plateau or a knife-edge artifact
    of one specific value. `param_path` is 'entry.params.fast_period' or
    'exit.params.threshold' etc."""
    import copy
    side, section, key = param_path.split(".")
    results = []
    for v in values:
        er = copy.deepcopy(entry_rules)
        xr = copy.deepcopy(exit_rules)
        target = er if side == "entry" else xr
        target["params"][key] = v
        r = run_backtest(bars, er, xr, sizing_logic, **backtest_kwargs)
        results.append({"value": v, "net_return": r["metrics"]["net_return"], "trade_count": r["metrics"]["trade_count"]})
    returns = [r["net_return"] for r in results if r["net_return"] is not None]
    sensitive = False
    if len(returns) >= 2:
        spread = max(returns) - min(returns)
        sensitive = spread > 0.5 * max(abs(x) for x in returns if x != 0) if any(returns) else False
    return {"param_path": param_path, "results": results, "sensitive_to_parameter_choice": sensitive}


def run_out_of_sample_validation(bars: List[Bar], entry_rules: Dict[str, Any], exit_rules: Dict[str, Any],
                                  sizing_logic: Dict[str, Any], stop_logic: Optional[Dict[str, Any]] = None,
                                  train_frac: float = 0.7, **backtest_kwargs) -> Dict[str, Any]:
    """Section 10's core anti-overfitting gate: the SAME unchanged strategy
    version is run once on the train split and once on the held-out test
    split. A strategy that only performs on train and collapses on test is
    exactly the overfitting this exists to catch."""
    train_bars, test_bars = train_test_split(bars, train_frac)
    train_result = run_backtest(train_bars, entry_rules, exit_rules, sizing_logic, stop_logic, **backtest_kwargs)
    test_result = run_backtest(test_bars, entry_rules, exit_rules, sizing_logic, stop_logic, **backtest_kwargs) if len(test_bars) >= 5 else None
    warnings = []
    if test_result is None:
        warnings.append("test split too small to backtest -- out-of-sample evaluation could not run")
    else:
        tr = train_result["metrics"]["net_return"]
        te = test_result["metrics"]["net_return"]
        if tr is not None and te is not None and tr > 0 and te <= 0:
            warnings.append("strategy was profitable in-sample (train) but not out-of-sample (test) -- likely overfit")
    return {"train": train_result, "test": test_result, "warnings": warnings}


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

class BacktestStore:
    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit

    def record(self, strategy_version_id: str, dataset_id: str, kind: str, fee_bps: float,
               slippage_bps: float, starting_cash: float, result: Dict[str, Any],
               params: Optional[Dict[str, Any]] = None, actor: str = "system") -> str:
        now = utcnow()
        backtest_id = self.store.create("tl_backtests", {
            "strategy_version_id": strategy_version_id, "dataset_id": dataset_id, "kind": kind,
            "fee_bps": fee_bps, "slippage_bps": slippage_bps, "starting_cash": starting_cash,
            "params_json": _json.dumps(params or {}), "status": "DONE",
            "metrics_json": _json.dumps(result["metrics"]), "equity_curve_json": _json.dumps(result["equity_curve"]),
            "trade_log_json": _json.dumps(result["trade_log"]), "warnings_json": _json.dumps(result["warnings"]),
            "actor": actor, "created_at": now,
        })
        self.audit.append("TL_BACKTEST_RECORDED", {
            "backtest_id": backtest_id, "strategy_version_id": strategy_version_id, "dataset_id": dataset_id,
            "kind": kind, "metrics": result["metrics"],
        })
        return backtest_id

    def get(self, backtest_id: str) -> Optional[Dict[str, Any]]:
        return self.store.get("tl_backtests", backtest_id)

    def list_for_strategy_version(self, strategy_version_id: str) -> List[Dict[str, Any]]:
        return self.store.list("tl_backtests", "strategy_version_id=?", (strategy_version_id,))


# ---------------------------------------------------------------------------
# Red-Team Stress / Adversarial Reviewer (Section 11)
# ---------------------------------------------------------------------------

STRESS_SCENARIOS = [
    "higher_fees", "higher_slippage", "delayed_fills", "worse_entry", "worse_exit",
    "volatility_regime_change", "trend_range_regime_change", "random_missed_trades", "reduced_liquidity",
]


def _perturb_bars_for_regime(bars: List[Bar], mode: str) -> List[Bar]:
    """Approximates a regime-change stress scenario by perturbing the
    dataset itself, then re-running the SAME strategy version against the
    perturbed data -- never by changing the strategy to fit the scenario."""
    out: List[Bar] = []
    for b in bars:
        if mode == "wider_range":  # volatility regime change: wider intrabar range
            mid = (b.high + b.low) / 2
            spread = (b.high - b.low) * 1.8
            out.append(Bar(ts=b.ts, open=b.open, high=mid + spread / 2, low=max(0.01, mid - spread / 2),
                            close=b.close, volume=b.volume))
        elif mode == "dampened_trend":  # trend -> range: pull close back toward open
            new_close = b.open + (b.close - b.open) * 0.35
            out.append(Bar(ts=b.ts, open=b.open, high=max(b.high, new_close), low=min(b.low, new_close),
                            close=new_close, volume=b.volume))
        else:
            out.append(b)
    return out


def run_stress_review(store: StateStore, audit: AuditLog, backtest_id: str,
                       bars: List[Bar], entry_rules: Dict[str, Any], exit_rules: Dict[str, Any],
                       sizing_logic: Dict[str, Any], stop_logic: Optional[Dict[str, Any]],
                       fee_bps: float, slippage_bps: float, starting_cash: float, actor: str = "system") -> List[str]:
    """Runs every scenario in STRESS_SCENARIOS against the strategy version
    that produced `backtest_id`'s baseline, persists one tl_stress_tests
    row per scenario, and returns the created stress_test ids. This
    function's job is to try to break the strategy, not to validate it --
    a strategy that survives every scenario still only means it survived
    THESE stress tests, never a guarantee (Section 21)."""
    baseline = run_backtest(bars, entry_rules, exit_rules, sizing_logic, stop_logic,
                             fee_bps=fee_bps, slippage_bps=slippage_bps, starting_cash=starting_cash)
    baseline_return = baseline["metrics"]["net_return"] or 0.0
    baseline_dd = baseline["metrics"]["max_drawdown_pct"] or 0.0

    ids = []
    for scenario in STRESS_SCENARIOS:
        kwargs = dict(fee_bps=fee_bps, slippage_bps=slippage_bps, starting_cash=starting_cash)
        scenario_bars = bars
        if scenario == "higher_fees":
            kwargs["fee_bps"] = fee_bps * 3
        elif scenario == "higher_slippage":
            kwargs["slippage_bps"] = slippage_bps * 3
        elif scenario == "delayed_fills":
            kwargs["fill_delay_bars"] = 1
        elif scenario == "worse_entry":
            kwargs["entry_slippage_bps_extra"] = 25.0
        elif scenario == "worse_exit":
            kwargs["exit_slippage_bps_extra"] = 25.0
        elif scenario == "volatility_regime_change":
            scenario_bars = _perturb_bars_for_regime(bars, "wider_range")
        elif scenario == "trend_range_regime_change":
            scenario_bars = _perturb_bars_for_regime(bars, "dampened_trend")
        elif scenario == "random_missed_trades":
            kwargs["skip_entry_seed"] = 1337
            kwargs["skip_entry_probability"] = 0.25
        elif scenario == "reduced_liquidity":
            kwargs["entry_slippage_bps_extra"] = 15.0
            kwargs["exit_slippage_bps_extra"] = 15.0

        result = run_backtest(scenario_bars, entry_rules, exit_rules, sizing_logic, stop_logic, **kwargs)
        stressed_return = result["metrics"]["net_return"] or 0.0
        stressed_dd = result["metrics"]["max_drawdown_pct"] or 0.0

        if stressed_return >= baseline_return * 0.5 and stressed_dd <= max(baseline_dd * 1.5, baseline_dd + 5):
            verdict = "SURVIVED"
        elif stressed_return > 0:
            verdict = "WEAKENED"
        else:
            verdict = "FAILED"

        now = utcnow()
        sid = store.create("tl_stress_tests", {
            "backtest_id": backtest_id, "scenario": scenario, "params_json": _json.dumps(kwargs),
            "metrics_json": _json.dumps(result["metrics"]), "verdict": verdict,
            "notes": f"baseline net_return={baseline_return:.4f} dd={baseline_dd:.2f}% -> "
                     f"stressed net_return={stressed_return:.4f} dd={stressed_dd:.2f}%",
            "actor": actor, "created_at": now,
        })
        audit.append("TL_STRESS_TEST_RECORDED", {"stress_test_id": sid, "backtest_id": backtest_id,
                                                   "scenario": scenario, "verdict": verdict})
        ids.append(sid)
    return ids
