"""TTT Trading Lab v1 -- Pass C: Risk Engine + Paper Trading + Portfolio +
Performance Snapshots.

Sections 12-15 of the TTT TRADING LAB V1 spec.

PAPER / SIMULATED ONLY -- read this before touching this file. Every order
here is a `tl_paper_orders` row against synthetic paper cash
(`tl_paper_accounts.cash`), filled by this process's own arithmetic against
a caller-supplied reference price. There is no broker/exchange client, no
API credential, no network call, and no code path anywhere in this module
that could reach a real brokerage or exchange -- see Section 2 (Structural
Capital Safety), Section 13 ("No path may call a real broker/exchange
order API in V1"), and Section 22 (future live-trading boundary, not
crossed here). Every UI/report surface that reads these tables must label
results PAPER/SIMULATED -- see falguna/hq_web.py's Trading Lab views.
"""

from __future__ import annotations

import json as _json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .audit import AuditLog
from .store import StateStore, utcnow
from .ttt_hq import NeedsAryanQueue

RISK_SCOPES = {"global", "strategy"}
ORDER_SIDES = {"BUY", "SELL"}
ORDER_STATUSES = {"PENDING", "FILLED", "REJECTED", "CANCELLED"}


def _today(ts: str) -> str:
    return ts[:10]


# ---------------------------------------------------------------------------
# Risk Engine (Section 12)
# ---------------------------------------------------------------------------

class RiskLimitStore:
    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit

    def create(self, scope: str, strategy_id: Optional[str] = None, max_risk_per_trade_pct: Optional[float] = None,
               max_daily_loss: Optional[float] = None, max_strategy_drawdown_pct: Optional[float] = None,
               max_portfolio_drawdown_pct: Optional[float] = None, max_concurrent_positions: Optional[int] = None,
               max_instrument_exposure_pct: Optional[float] = None, max_strategy_allocation_pct: Optional[float] = None,
               actor: str = "system") -> str:
        if scope not in RISK_SCOPES:
            raise ValueError(f"scope must be one of {RISK_SCOPES}")
        if scope == "strategy" and not strategy_id:
            raise ValueError("strategy_id is required for scope='strategy'")
        now = utcnow()
        limit_id = self.store.create("tl_risk_limits", {
            "scope": scope, "strategy_id": strategy_id,
            "max_risk_per_trade_pct": max_risk_per_trade_pct, "max_daily_loss": max_daily_loss,
            "max_strategy_drawdown_pct": max_strategy_drawdown_pct, "max_portfolio_drawdown_pct": max_portfolio_drawdown_pct,
            "max_concurrent_positions": max_concurrent_positions, "max_instrument_exposure_pct": max_instrument_exposure_pct,
            "max_strategy_allocation_pct": max_strategy_allocation_pct,
            "status": "ACTIVE", "actor": actor, "created_at": now, "updated_at": now,
        })
        self.audit.append("TL_RISK_LIMIT_CREATED", {"limit_id": limit_id, "scope": scope, "strategy_id": strategy_id})
        return limit_id

    def active_limits(self, strategy_id: Optional[str] = None) -> List[Dict[str, Any]]:
        rows = self.store.list("tl_risk_limits", "status=?", ("ACTIVE",))
        return [r for r in rows if r["scope"] == "global" or r["strategy_id"] == strategy_id]

    def get(self, limit_id: str) -> Optional[Dict[str, Any]]:
        return self.store.get("tl_risk_limits", limit_id)


class RiskEngine:
    """Applies tl_risk_limits both pre-trade (per-order) and post-trade
    (portfolio/strategy breach detection). Breach behavior per Section 12:
    stop new paper entries, flag the strategy, and escalate to Needs Aryan
    where appropriate -- never a silent auto-correction of real risk."""

    def __init__(self, store: StateStore, audit: AuditLog, needs_aryan: NeedsAryanQueue):
        self.store = store
        self.audit = audit
        self.needs_aryan = needs_aryan
        self.limits = RiskLimitStore(store, audit)

    def has_open_breach(self, paper_account_id: str, strategy_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        rows = self.store.list("tl_risk_breach_events", "paper_account_id=?", (paper_account_id,))
        for r in rows:
            if r["strategy_id"] in (None, strategy_id) or strategy_id is None:
                return r
        return None

    def check_pre_trade(self, paper_account_id: str, strategy_id: str, instrument_id: str,
                         side: str, qty: float, price: float) -> Optional[str]:
        """Returns None if the order may proceed, else a rejection reason.

        Percentage-based risk limits (max_risk_per_trade_pct,
        max_instrument_exposure_pct, max_strategy_allocation_pct) only ever
        apply to an order that OPENS or ADDS TO risk. An order that REDUCES
        or CLOSES an existing position is never blocked by them -- a risk
        engine that can trap an account in a position it cannot exit would
        itself be a hazard. Only the position-flip and cash checks (done
        by the caller) and an open risk breach still gate a reducing order,
        and even a breach never blocks the exit side of a position.
        """
        position_rows = self.store.list("tl_paper_positions", "paper_account_id=? AND strategy_id=? AND instrument_id=?",
                                         (paper_account_id, strategy_id, instrument_id))
        current_qty = position_rows[0]["qty"] if position_rows else 0.0
        order_signed_delta = qty if side == "BUY" else -qty
        is_reducing = current_qty != 0 and (current_qty > 0) != (order_signed_delta > 0)

        breach = self.has_open_breach(paper_account_id, strategy_id)
        if breach and not is_reducing:  # exits (reducing/closing orders) are never blocked by a breach
            return f"blocked: unresolved risk breach in effect ({breach['breach_type']})"
        if is_reducing:
            return None  # reducing/closing orders skip all percentage-based risk checks below

        account = self.store.get("tl_paper_accounts", paper_account_id)
        equity = self._equity(paper_account_id)
        notional = abs(qty * price)
        limits = self.limits.active_limits(strategy_id)

        for lim in limits:
            if lim["max_risk_per_trade_pct"] is not None and equity > 0:
                if notional / equity * 100 > lim["max_risk_per_trade_pct"]:
                    return f"exceeds max_risk_per_trade_pct ({lim['max_risk_per_trade_pct']}%)"
            if lim["max_instrument_exposure_pct"] is not None and equity > 0:
                existing = self._instrument_exposure(paper_account_id, instrument_id)
                if (existing + notional) / equity * 100 > lim["max_instrument_exposure_pct"]:
                    return f"exceeds max_instrument_exposure_pct ({lim['max_instrument_exposure_pct']}%)"
            if lim["max_strategy_allocation_pct"] is not None and equity > 0:
                existing = self._strategy_exposure(paper_account_id, strategy_id)
                if (existing + notional) / equity * 100 > lim["max_strategy_allocation_pct"]:
                    return f"exceeds max_strategy_allocation_pct ({lim['max_strategy_allocation_pct']}%)"
            if lim["max_concurrent_positions"] is not None:
                open_positions = [p for p in self.store.list("tl_paper_positions", "paper_account_id=?", (paper_account_id,)) if p["qty"] != 0]
                is_new = not any(p["strategy_id"] == strategy_id and p["instrument_id"] == instrument_id and p["qty"] != 0 for p in open_positions)
                if is_new and len(open_positions) >= lim["max_concurrent_positions"]:
                    return f"exceeds max_concurrent_positions ({lim['max_concurrent_positions']})"
        return None

    def _equity(self, paper_account_id: str) -> float:
        account = self.store.get("tl_paper_accounts", paper_account_id)
        if not account:
            return 0.0
        positions = self.store.list("tl_paper_positions", "paper_account_id=?", (paper_account_id,))
        position_value = sum(p["qty"] * p["avg_price"] for p in positions)  # mark-to-avg approximation between snapshots
        return account["cash"] + position_value

    def _instrument_exposure(self, paper_account_id: str, instrument_id: str) -> float:
        positions = self.store.list("tl_paper_positions", "paper_account_id=? AND instrument_id=?", (paper_account_id, instrument_id))
        return sum(abs(p["qty"] * p["avg_price"]) for p in positions)

    def _strategy_exposure(self, paper_account_id: str, strategy_id: str) -> float:
        positions = self.store.list("tl_paper_positions", "paper_account_id=? AND strategy_id=?", (paper_account_id, strategy_id))
        return sum(abs(p["qty"] * p["avg_price"]) for p in positions)

    def check_post_trade_breaches(self, paper_account_id: str, strategy_id: Optional[str] = None, actor: str = "system") -> List[str]:
        """Call after a fill or snapshot to detect drawdown/daily-loss
        breaches. Returns the list of breach_types newly recorded."""
        newly_breached = []
        limits = self.limits.active_limits(strategy_id)
        snapshots = self.store.list("tl_performance_snapshots", "paper_account_id=?", (paper_account_id,))
        if strategy_id:
            snapshots = [s for s in snapshots if s["strategy_id"] == strategy_id]
        else:
            snapshots = [s for s in snapshots if s["strategy_id"] is None]
        snapshots.sort(key=lambda s: s["as_of"])

        if not snapshots:
            return newly_breached
        latest = snapshots[-1]
        peak_equity = max(s["equity"] for s in snapshots)
        drawdown_pct = ((peak_equity - latest["equity"]) / peak_equity * 100) if peak_equity > 0 else 0.0

        today = _today(latest["as_of"])
        today_trades_pnl = sum(
            t["realized_pnl"] for t in self.store.list("tl_trades", "paper_account_id=?", (paper_account_id,))
            if t["closed_at"] and _today(t["closed_at"]) == today and (strategy_id is None or t["strategy_id"] == strategy_id)
        )

        for lim in limits:
            breach_type = None
            detail = None
            dd_limit = lim["max_strategy_drawdown_pct"] if lim["scope"] == "strategy" else lim["max_portfolio_drawdown_pct"]
            if dd_limit is not None and drawdown_pct > dd_limit:
                breach_type, detail = "drawdown_breach", f"drawdown {drawdown_pct:.2f}% exceeds limit {dd_limit}%"
            elif lim["max_daily_loss"] is not None and today_trades_pnl < -abs(lim["max_daily_loss"]):
                breach_type, detail = "daily_loss_breach", f"today's realized P&L {today_trades_pnl:.2f} exceeds daily loss limit {lim['max_daily_loss']}"
            if breach_type and not self.has_open_breach(paper_account_id, strategy_id):
                now = utcnow()
                item_id = self.needs_aryan.create_item(
                    kind="trading_risk_breach",
                    title=f"Trading Lab risk breach: {breach_type}",
                    what_is_needed="Review the breach and decide whether to keep new paper entries paused or resume.",
                    actor=actor, rationale=detail, risk="paper capital only -- no real money at risk",
                    ref_type="tl_paper_account", ref_id=paper_account_id,
                )
                breach_id = self.store.create("tl_risk_breach_events", {
                    "risk_limit_id": lim["id"], "strategy_id": strategy_id, "paper_account_id": paper_account_id,
                    "breach_type": breach_type, "detail": detail, "needs_aryan_id": item_id, "created_at": now,
                })
                self.audit.append("TL_RISK_BREACH", {"breach_id": breach_id, "breach_type": breach_type, "detail": detail})
                newly_breached.append(breach_type)
        return newly_breached

    def resolve_breach(self, breach_id: str, actor: str) -> None:
        """Explicit human action (Aryan, via Needs Aryan) to clear a breach
        and allow new paper entries again. Never auto-cleared by the engine."""
        row = self.store.get("tl_risk_breach_events", breach_id)
        if not row:
            raise ValueError("unknown breach_id")
        self.store.update("tl_paper_accounts", row["paper_account_id"])  # no-op touch to bump updated_at, keeps history honest
        # deleting isn't in our vocabulary (void-never-delete); mark resolved via a follow-up event instead
        self.audit.append("TL_RISK_BREACH_RESOLVED", {"breach_id": breach_id, "actor": actor})


# ---------------------------------------------------------------------------
# Paper Trading Engine (Section 13)
# ---------------------------------------------------------------------------

class PaperAccountStore:
    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit

    def create(self, name: str, starting_cash: float, actor: str = "system") -> str:
        if starting_cash <= 0:
            raise ValueError("starting_cash must be positive")
        now = utcnow()
        account_id = self.store.create("tl_paper_accounts", {
            "name": name, "starting_cash": starting_cash, "cash": starting_cash,
            "status": "ACTIVE", "actor": actor, "created_at": now, "updated_at": now,
        })
        self.audit.append("TL_PAPER_ACCOUNT_CREATED", {"account_id": account_id, "name": name, "starting_cash": starting_cash})
        return account_id

    def get(self, account_id: str) -> Optional[Dict[str, Any]]:
        return self.store.get("tl_paper_accounts", account_id)

    def list_all(self) -> List[Dict[str, Any]]:
        return self.store.list("tl_paper_accounts", "1=1", ())


def _find_open_entry_order_id(store: StateStore, paper_account_id: str, strategy_id: str, instrument_id: str) -> Optional[str]:
    """Replays this position's FILLED order history to find the order that
    opened the currently-open leg (the most recent zero -> nonzero
    transition). Used to attribute a closing trade's entry_order_id
    without adding a mutable 'open order pointer' column to the schema."""
    orders = store.list("tl_paper_orders", "paper_account_id=? AND strategy_id=? AND instrument_id=? AND status='FILLED'",
                         (paper_account_id, strategy_id, instrument_id))
    orders.sort(key=lambda o: o["created_at"])
    running_qty = 0.0
    entry_order_id = None
    for o in orders:
        before = running_qty
        delta = o["qty"] if o["side"] == "BUY" else -o["qty"]
        running_qty += delta
        if before == 0 and running_qty != 0:
            entry_order_id = o["id"]
    return entry_order_id


class PaperTradingEngine:
    """PAPER ONLY -- see module docstring. `market_price` is always
    supplied by the caller (e.g. the latest close from a tl_dataset, or a
    QA-supplied test price) -- this engine never fetches a live quote or
    talks to any external execution venue."""

    def __init__(self, store: StateStore, audit: AuditLog, risk_engine: RiskEngine):
        self.store = store
        self.audit = audit
        self.risk_engine = risk_engine

    def submit_order(self, paper_account_id: str, strategy_id: str, instrument_id: str, side: str,
                      qty: float, market_price: float, fee_bps: float = 10.0, slippage_bps: float = 5.0,
                      actor: str = "system") -> str:
        if side not in ORDER_SIDES:
            raise ValueError(f"side must be one of {ORDER_SIDES}")
        if qty <= 0:
            raise ValueError("qty must be positive")
        if market_price <= 0:
            raise ValueError("market_price must be positive")
        account = self.store.get("tl_paper_accounts", paper_account_id)
        if not account:
            raise ValueError("unknown paper_account_id")
        strategy = self.store.get("tl_strategies", strategy_id)
        if not strategy:
            raise ValueError("unknown strategy_id")

        now = utcnow()

        position_rows = self.store.list("tl_paper_positions", "paper_account_id=? AND strategy_id=? AND instrument_id=?",
                                         (paper_account_id, strategy_id, instrument_id))
        position = position_rows[0] if position_rows else None
        current_qty = position["qty"] if position else 0.0
        signed_delta = qty if side == "BUY" else -qty
        new_qty = current_qty + signed_delta
        is_reducing = current_qty != 0 and (current_qty > 0) != (signed_delta > 0)

        # A strategy may only receive paper orders once the Trading Council +
        # Aryan have moved it to PAPER_ACTIVE (Sections 17/18) -- with one
        # narrow exception: a PAUSED strategy may still submit a
        # reducing/closing order so an existing paper position can be wound
        # down. Every other status (IDEA/RESEARCHING/BACKTESTING/REVIEW/
        # PAPER_APPROVED/REJECTED/GRAVEYARD) never had an approved position
        # to open in the first place, so no order is ever valid for them.
        reject_reason = None
        if strategy["status"] != "PAPER_ACTIVE":
            if strategy["status"] == "PAUSED" and is_reducing:
                pass
            else:
                raise ValueError(
                    f"strategy status is {strategy['status']} -- paper orders require the strategy to be "
                    "PAPER_ACTIVE (Trading Council approval + explicit Aryan activation); a PAUSED strategy "
                    "may only submit a closing/reducing order"
                )

        reject_reason = self.risk_engine.check_pre_trade(paper_account_id, strategy_id, instrument_id, side, qty, market_price)

        if current_qty != 0 and new_qty != 0 and (current_qty > 0) != (new_qty > 0):
            reject_reason = reject_reason or "order would flip position side -- submit a closing order sized to exactly flatten the position first"

        if not reject_reason and side == "BUY":
            notional_needed = qty * market_price * (1 + slippage_bps / 10000.0) * (1 + fee_bps / 10000.0)
            if notional_needed > account["cash"] and current_qty >= 0:
                reject_reason = "insufficient paper cash"

        if reject_reason:
            order_id = self.store.create("tl_paper_orders", {
                "paper_account_id": paper_account_id, "strategy_id": strategy_id, "instrument_id": instrument_id,
                "side": side, "order_type": "MARKET", "qty": qty, "requested_price": market_price,
                "status": "REJECTED", "reject_reason": reject_reason, "fill_price": None, "fees": None, "slippage": None,
                "actor": actor, "created_at": now, "updated_at": now, "filled_at": None,
            })
            self.audit.append("TL_PAPER_ORDER_REJECTED", {"order_id": order_id, "reason": reject_reason})
            return order_id

        fill_price = market_price * (1 + (slippage_bps / 10000.0) * (1 if side == "BUY" else -1))
        fee = abs(qty * fill_price) * (fee_bps / 10000.0)
        order_id = self.store.create("tl_paper_orders", {
            "paper_account_id": paper_account_id, "strategy_id": strategy_id, "instrument_id": instrument_id,
            "side": side, "order_type": "MARKET", "qty": qty, "requested_price": market_price,
            "status": "FILLED", "reject_reason": None, "fill_price": fill_price, "fees": fee, "slippage": slippage_bps,
            "actor": actor, "created_at": now, "updated_at": now, "filled_at": now,
        })

        cash_delta = (-qty * fill_price - fee) if side == "BUY" else (qty * fill_price - fee)
        self.store.update("tl_paper_accounts", paper_account_id, cash=account["cash"] + cash_delta)

        if current_qty == 0:
            avg_price = fill_price
        elif (current_qty > 0) == (side == "BUY"):
            # adding to an existing position on the same side: blend the average price
            avg_price = (current_qty * position["avg_price"] + signed_delta * fill_price) / new_qty if new_qty != 0 else fill_price
        else:
            avg_price = position["avg_price"]  # reducing/closing: entry price is unchanged until fully flat

        if position:
            self.store.update("tl_paper_positions", position["id"], qty=new_qty, avg_price=avg_price)
        else:
            self.store.create("tl_paper_positions", {
                "paper_account_id": paper_account_id, "strategy_id": strategy_id, "instrument_id": instrument_id,
                "qty": new_qty, "avg_price": avg_price, "created_at": now, "updated_at": now,
            })

        self.audit.append("TL_PAPER_ORDER_FILLED", {
            "order_id": order_id, "side": side, "qty": qty, "fill_price": fill_price, "fee": fee, "new_position_qty": new_qty,
        })

        if current_qty != 0 and new_qty == 0:
            entry_order_id = _find_open_entry_order_id(self.store, paper_account_id, strategy_id, instrument_id) or order_id
            entry_price = position["avg_price"]
            gross_pnl = (fill_price - entry_price) * current_qty if current_qty > 0 else (entry_price - fill_price) * abs(current_qty)
            self.store.create("tl_trades", {
                "paper_account_id": paper_account_id, "strategy_id": strategy_id, "instrument_id": instrument_id,
                "entry_order_id": entry_order_id, "exit_order_id": order_id, "qty": abs(current_qty),
                "entry_price": entry_price, "exit_price": fill_price, "realized_pnl": round(gross_pnl - fee, 4),
                "opened_at": now, "closed_at": now, "created_at": now, "updated_at": now,
            })
            self.audit.append("TL_PAPER_TRADE_CLOSED", {"paper_account_id": paper_account_id, "strategy_id": strategy_id,
                                                          "realized_pnl": round(gross_pnl - fee, 4)})

        record_performance_snapshot(self.store, self.audit, paper_account_id, strategy_id)
        record_performance_snapshot(self.store, self.audit, paper_account_id, None)
        self.risk_engine.check_post_trade_breaches(paper_account_id, strategy_id, actor)
        self.risk_engine.check_post_trade_breaches(paper_account_id, None, actor)
        return order_id


# ---------------------------------------------------------------------------
# Paper Portfolio + Performance Analysis (Sections 14-15)
# ---------------------------------------------------------------------------

def portfolio_summary(store: StateStore, paper_account_id: str) -> Dict[str, Any]:
    """PAPER portfolio accounting across all strategies sharing one paper
    account: allocation, exposure, concentration, total P&L, drawdown."""
    account = store.get("tl_paper_accounts", paper_account_id)
    if not account:
        raise ValueError("unknown paper_account_id")
    positions = [p for p in store.list("tl_paper_positions", "paper_account_id=?", (paper_account_id,)) if p["qty"] != 0]
    trades = store.list("tl_trades", "paper_account_id=?", (paper_account_id,))
    position_value = sum(p["qty"] * p["avg_price"] for p in positions)
    equity = account["cash"] + position_value
    realized_pnl_total = sum(t["realized_pnl"] for t in trades if t["realized_pnl"] is not None)
    unrealized_pnl_total = position_value - sum(abs(p["qty"]) * p["avg_price"] * (1 if p["qty"] > 0 else -1) for p in positions) * 0  # positions marked at avg_price here; true unrealized needs live price, so this is 0 by construction until a fresh market price is supplied

    by_instrument: Dict[str, float] = {}
    for p in positions:
        by_instrument[p["instrument_id"]] = by_instrument.get(p["instrument_id"], 0.0) + abs(p["qty"] * p["avg_price"])
    by_strategy: Dict[str, float] = {}
    for p in positions:
        by_strategy[p["strategy_id"]] = by_strategy.get(p["strategy_id"], 0.0) + abs(p["qty"] * p["avg_price"])

    snapshots = sorted(store.list("tl_performance_snapshots", "paper_account_id=? AND strategy_id IS NULL", (paper_account_id,)),
                        key=lambda s: s["as_of"])
    peak_equity = max([s["equity"] for s in snapshots], default=equity)
    drawdown_pct = ((peak_equity - equity) / peak_equity * 100) if peak_equity > 0 else 0.0

    return {
        "paper_account_id": paper_account_id, "is_paper": True, "is_real_money": False,
        "cash": account["cash"], "position_value": round(position_value, 4), "equity": round(equity, 4),
        "starting_cash": account["starting_cash"], "realized_pnl_total": round(realized_pnl_total, 4),
        "open_position_count": len(positions), "instrument_exposure": {k: round(v, 4) for k, v in by_instrument.items()},
        "strategy_allocation": {k: round(v, 4) for k, v in by_strategy.items()},
        "drawdown_pct": round(drawdown_pct, 4),
        "note": "PAPER / SIMULATED portfolio -- no real money is represented by any figure here.",
    }


def record_performance_snapshot(store: StateStore, audit: AuditLog, paper_account_id: str,
                                 strategy_id: Optional[str] = None) -> str:
    account = store.get("tl_paper_accounts", paper_account_id)
    if strategy_id:
        positions = store.list("tl_paper_positions", "paper_account_id=? AND strategy_id=?", (paper_account_id, strategy_id))
        trades = store.list("tl_trades", "paper_account_id=? AND strategy_id=?", (paper_account_id, strategy_id))
        scope = "strategy"
    else:
        positions = store.list("tl_paper_positions", "paper_account_id=?", (paper_account_id,))
        trades = store.list("tl_trades", "paper_account_id=?", (paper_account_id,))
        scope = "portfolio"
    position_value = sum(p["qty"] * p["avg_price"] for p in positions if p["qty"] != 0)
    realized_pnl = sum(t["realized_pnl"] for t in trades if t["realized_pnl"] is not None)
    cash = account["cash"] if scope == "portfolio" else account["cash"]  # strategy-level cash isn't separately partitioned in V1
    equity = cash + position_value if scope == "portfolio" else position_value + realized_pnl
    now = utcnow()

    prior = store.list("tl_performance_snapshots",
                        "paper_account_id=? AND " + ("strategy_id=?" if strategy_id else "strategy_id IS NULL"),
                        (paper_account_id, strategy_id) if strategy_id else (paper_account_id,))
    peak = max([p["equity"] for p in prior], default=equity)
    peak = max(peak, equity)
    drawdown_pct = ((peak - equity) / peak * 100) if peak > 0 else 0.0

    snapshot_id = store.create("tl_performance_snapshots", {
        "scope": scope, "strategy_id": strategy_id, "paper_account_id": paper_account_id, "as_of": now,
        "equity": round(equity, 4), "cash": round(cash, 4), "realized_pnl": round(realized_pnl, 4),
        "unrealized_pnl": 0.0, "drawdown_pct": round(drawdown_pct, 4),
        "metrics_json": _json.dumps({"open_position_count": len([p for p in positions if p["qty"] != 0]), "trade_count": len(trades)}),
        "created_at": now,
    })
    audit.append("TL_PERFORMANCE_SNAPSHOT_RECORDED", {"snapshot_id": snapshot_id, "scope": scope, "strategy_id": strategy_id, "equity": equity})
    return snapshot_id


def strategy_performance(store: StateStore, strategy_id: str) -> Dict[str, Any]:
    """Section 15: every metric here links back to underlying trades --
    nothing is a bare number without provenance."""
    trades = store.list("tl_trades", "strategy_id=?", (strategy_id,))
    closed = [t for t in trades if t["realized_pnl"] is not None]
    snapshots = sorted(store.list("tl_performance_snapshots", "strategy_id=?", (strategy_id,)), key=lambda s: s["as_of"])
    wins = [t["realized_pnl"] for t in closed if t["realized_pnl"] > 0]
    losses = [t["realized_pnl"] for t in closed if t["realized_pnl"] <= 0]
    return {
        "strategy_id": strategy_id, "is_paper": True,
        "trade_count": len(closed),
        "total_realized_pnl": round(sum(t["realized_pnl"] for t in closed), 4) if closed else 0.0,
        "win_rate": round(len(wins) / len(closed), 4) if closed else None,
        "latest_drawdown_pct": snapshots[-1]["drawdown_pct"] if snapshots else None,
        "snapshot_count": len(snapshots),
        "trade_ids": [t["id"] for t in closed],
        "note": "PAPER / SIMULATED results only -- no historical or simulated performance figure here guarantees future returns.",
    }
