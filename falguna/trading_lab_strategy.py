"""TTT Trading Lab v1 -- Pass A (part 2): Strategy Model + Research Agent.

Sections 3, 7, 8 of the TTT TRADING LAB V1 spec.

Strategy lifecycle (Section 3): IDEA -> RESEARCHING -> BACKTESTING -> REVIEW
-> PAPER_APPROVED -> PAPER_ACTIVE -> PAUSED -> REJECTED -> GRAVEYARD. There
is deliberately NO "LIVE" status anywhere in this module or its schema --
see falguna/trading_lab_data.py's module docstring and Section 22 of the
spec for the future live-trading boundary this V1 does not cross.

Entry/exit/stop rules are never arbitrary evaluated expression strings --
that would be an arbitrary-code-execution risk. Instead they are structured
JSON referencing a small registry of named, parameterized signal
generators (SIGNAL_REGISTRY below). This satisfies Section 7's "no strategy
should exist only as vague prose" without a full expression-language
sandbox. falguna/trading_lab_backtest.py evaluates these structured rules
bar-by-bar; it never calls eval()/exec() on strategy-supplied text.
"""

from __future__ import annotations

import json as _json
import statistics
from typing import Any, Dict, List, Optional

from .audit import AuditLog
from .store import StateStore, utcnow

STRATEGY_STATUSES = {
    "IDEA", "RESEARCHING", "BACKTESTING", "REVIEW", "PAPER_APPROVED",
    "PAPER_ACTIVE", "PAUSED", "REJECTED", "GRAVEYARD",
}

# Allowed forward transitions. Council/graveyard/pause flows may also move
# a strategy backward (e.g. PAPER_ACTIVE -> PAUSED -> RESEARCHING) -- those
# are listed explicitly rather than left open, so an invalid jump (e.g.
# IDEA -> PAPER_ACTIVE, skipping backtesting and review) is rejected.
ALLOWED_TRANSITIONS: Dict[str, set] = {
    "IDEA": {"RESEARCHING", "REJECTED"},
    "RESEARCHING": {"BACKTESTING", "REJECTED", "IDEA"},
    "BACKTESTING": {"REVIEW", "RESEARCHING", "REJECTED"},
    "REVIEW": {"PAPER_APPROVED", "RESEARCHING", "REJECTED", "GRAVEYARD"},
    "PAPER_APPROVED": {"PAPER_ACTIVE", "PAUSED", "REJECTED"},
    "PAPER_ACTIVE": {"PAUSED", "REVIEW", "GRAVEYARD"},
    "PAUSED": {"PAPER_ACTIVE", "RESEARCHING", "GRAVEYARD", "REJECTED"},
    "REJECTED": {"GRAVEYARD", "RESEARCHING"},
    "GRAVEYARD": set(),
}

# Structured signal registry (Section 7/9). Each entry names the params a
# rule must supply; falguna/trading_lab_backtest.py implements the actual
# bar-by-bar computation. Kept here (rather than in the backtest engine) so
# strategy specs can be validated against it at authoring time, before any
# backtest is run.
SIGNAL_REGISTRY: Dict[str, List[str]] = {
    # cross: "up" (fast crosses above slow) | "down" (fast crosses below slow)
    "sma_crossover": ["fast_period", "slow_period", "cross"],
    # cross: "above" (RSI crosses above threshold) | "below" (RSI crosses below threshold)
    "rsi_threshold": ["period", "threshold", "cross"],
    # direction: "up" (close breaks above rolling high) | "down" (breaks below rolling low)
    "breakout": ["lookback_period", "direction"],
}


def validate_rule(rule: Dict[str, Any]) -> Optional[str]:
    """Returns None if valid, else a human-readable reason it is invalid."""
    if not isinstance(rule, dict):
        return "rule must be an object"
    signal = rule.get("signal")
    if signal not in SIGNAL_REGISTRY:
        return f"unknown signal '{signal}' -- must be one of {sorted(SIGNAL_REGISTRY)}"
    params = rule.get("params") or {}
    if not isinstance(params, dict):
        return "params must be an object"
    missing = [p for p in SIGNAL_REGISTRY[signal] if p not in params]
    if missing:
        return f"signal '{signal}' missing required params: {missing}"
    return None


class StrategyStore:
    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit

    def create(self, name: str, hypothesis: str, market_id: Optional[str] = None, actor: str = "system") -> str:
        if not name or not name.strip():
            raise ValueError("name is required")
        if not hypothesis or not hypothesis.strip():
            raise ValueError("hypothesis is required -- Section 7: no strategy may exist only as vague prose")
        now = utcnow()
        strategy_id = self.store.create("tl_strategies", {
            "name": name.strip(), "hypothesis": hypothesis.strip(), "market_id": market_id,
            "status": "IDEA", "actor": actor, "created_at": now, "updated_at": now,
        })
        self.store.create("tl_strategy_status_events", {
            "strategy_id": strategy_id, "from_status": None, "to_status": "IDEA",
            "actor": actor, "reason": "strategy created", "created_at": now,
        })
        self.audit.append("TL_STRATEGY_CREATED", {"strategy_id": strategy_id, "name": name})
        return strategy_id

    def get(self, strategy_id: str) -> Optional[Dict[str, Any]]:
        return self.store.get("tl_strategies", strategy_id)

    def list_all(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        if status:
            return self.store.list("tl_strategies", "status=?", (status,))
        return self.store.list("tl_strategies", "1=1", ())

    def transition(self, strategy_id: str, to_status: str, actor: str, reason: str = "") -> None:
        strategy = self.get(strategy_id)
        if not strategy:
            raise ValueError("unknown strategy_id")
        if to_status not in STRATEGY_STATUSES:
            raise ValueError(f"unknown status: {to_status}")
        from_status = strategy["status"]
        allowed = ALLOWED_TRANSITIONS.get(from_status, set())
        if to_status not in allowed:
            raise ValueError(f"invalid transition {from_status} -> {to_status} (allowed: {sorted(allowed)})")
        now = utcnow()
        self.store.update("tl_strategies", strategy_id, status=to_status, updated_at=now)
        self.store.create("tl_strategy_status_events", {
            "strategy_id": strategy_id, "from_status": from_status, "to_status": to_status,
            "actor": actor, "reason": reason, "created_at": now,
        })
        self.audit.append("TL_STRATEGY_STATUS_CHANGED", {
            "strategy_id": strategy_id, "from_status": from_status, "to_status": to_status, "reason": reason,
        })


class StrategyVersionStore:
    """Every backtest, stress test, review and council decision references
    an immutable strategy_version -- so results are always reproducible
    against the exact rules that produced them (Section 20)."""

    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit

    def create(self, strategy_id: str, instruments: List[str], timeframe: str,
               entry_rules: Dict[str, Any], exit_rules: Dict[str, Any],
               sizing_logic: Dict[str, Any], stop_logic: Optional[Dict[str, Any]] = None,
               allowed_hours: str = "", max_exposure_pct: float = 100.0,
               assumptions: str = "", known_risks: str = "", actor: str = "system") -> str:
        strategy = self.store.get("tl_strategies", strategy_id)
        if not strategy:
            raise ValueError("unknown strategy_id")
        for label, rule in (("entry_rules", entry_rules), ("exit_rules", exit_rules)):
            err = validate_rule(rule)
            if err:
                raise ValueError(f"{label}: {err}")
        if not instruments:
            raise ValueError("at least one instrument is required")
        if "position_size_pct" not in (sizing_logic or {}) and "position_size_units" not in (sizing_logic or {}):
            raise ValueError("sizing_logic must specify position_size_pct or position_size_units")
        existing = self.store.list("tl_strategy_versions", "strategy_id=?", (strategy_id,))
        version_number = 1 + max([r["version_number"] for r in existing], default=0)
        now = utcnow()
        version_id = self.store.create("tl_strategy_versions", {
            "strategy_id": strategy_id, "version_number": version_number,
            "instruments_json": _json.dumps(instruments), "timeframe": timeframe,
            "entry_rules_json": _json.dumps(entry_rules), "exit_rules_json": _json.dumps(exit_rules),
            "stop_logic_json": _json.dumps(stop_logic or {}), "sizing_logic_json": _json.dumps(sizing_logic),
            "allowed_hours": allowed_hours, "max_exposure_pct": max_exposure_pct,
            "assumptions": assumptions, "known_risks": known_risks,
            "actor": actor, "created_at": now,
        })
        self.audit.append("TL_STRATEGY_VERSION_CREATED", {
            "strategy_id": strategy_id, "version_id": version_id, "version_number": version_number,
        })
        return version_id

    def get(self, version_id: str) -> Optional[Dict[str, Any]]:
        return self.store.get("tl_strategy_versions", version_id)

    def list_for_strategy(self, strategy_id: str) -> List[Dict[str, Any]]:
        rows = self.store.list("tl_strategy_versions", "strategy_id=?", (strategy_id,))
        rows.sort(key=lambda r: r["version_number"])
        return rows

    def latest_for_strategy(self, strategy_id: str) -> Optional[Dict[str, Any]]:
        rows = self.list_for_strategy(strategy_id)
        return rows[-1] if rows else None


# ---------------------------------------------------------------------------
# Strategy Research Agent (Section 8)
# ---------------------------------------------------------------------------

_KNOWN_FAILURE_MODES_BY_SIGNAL = {
    "sma_crossover": [
        "whipsaws in range-bound/choppy markets with no sustained trend",
        "lag: crossover confirms a trend only after a meaningful part of the move has already happened",
    ],
    "rsi_threshold": [
        "false reversal signals during strong trends (RSI can stay overbought/oversold for long stretches)",
        "sensitive to the lookback period choice -- easy to overfit",
    ],
    "breakout": [
        "false breakouts in low-liquidity or low-volatility regimes",
        "vulnerable to slippage on the breakout bar itself, which backtests can understate",
    ],
}


def research_strategy_idea(idea: str, signals_considered: List[str], market_code: str = "") -> Dict[str, Any]:
    """Section 8: given a plain-language idea and the structured signal(s)
    it is likely to use, produce a research artifact that explicitly
    separates hypothesis / evidence / inference and proposes a measurable
    rule set and a test plan. This function never claims or implies a
    guaranteed return -- see Section 21 (No Guarantee Policy); it only
    frames what would need to be tested and how.

    This is deliberately a structured, rule-based assistant (not a
    strategy-generating model) -- it is not connected to any live-money
    execution path and produces no financial advice, only a research plan
    for a human (Aryan) and the Trading Council to evaluate.
    """
    idea = (idea or "").strip()
    if not idea:
        raise ValueError("idea is required")
    unknown = [s for s in signals_considered if s not in SIGNAL_REGISTRY]
    if unknown:
        raise ValueError(f"unknown signal(s) considered: {unknown} -- must be from {sorted(SIGNAL_REGISTRY)}")

    failure_modes: List[str] = []
    for s in signals_considered:
        failure_modes.extend(_KNOWN_FAILURE_MODES_BY_SIGNAL.get(s, []))
    failure_modes.append("overfitting to the specific historical window used for research/backtesting")
    failure_modes.append("regime change: the market condition the idea assumes may not persist")

    market_assumptions = [
        f"market '{market_code or 'unspecified'}' has enough historical bar data to backtest the proposed timeframe",
        "the relationship the idea proposes is stable enough over the test window to be measurable",
    ]

    proposed_rules = [
        {"signal": s, "note": f"parameters must be specified explicitly -- see SIGNAL_REGISTRY['{s}']"}
        for s in signals_considered
    ]

    required_data = [
        "OHLCV bars for the target instrument(s) and timeframe, covering at least one full train and one held-out test period",
        "a documented data source with provenance (provider, ingestion timestamp, completeness) -- see falguna/trading_lab_data.py",
    ]

    test_plan = [
        "1. Ingest and run data-quality checks on the dataset (Section 6) before any backtest is attempted.",
        "2. Run a deterministic backtest on a train split only (Section 9/10).",
        "3. Run the same strategy version, unchanged, on the held-out test split (out-of-sample evaluation).",
        "4. Run parameter-sensitivity checks -- confirm the result is not a knife-edge artifact of one parameter value.",
        "5. Run the Red-Team stress/adversarial review (Section 11) against higher fees/slippage/delayed fills and regime changes.",
        "6. Only if it survives 1-5, route to the Trading Council (Section 17) for a paper-activation decision.",
    ]

    return {
        "hypothesis": idea,
        "evidence": [],  # populated later by real backtest/stress-test results, never asserted up front
        "inference": "untested -- this is a research plan, not a validated conclusion",
        "market_assumptions": market_assumptions,
        "likely_failure_modes": failure_modes,
        "proposed_rules": proposed_rules,
        "required_data": required_data,
        "test_plan": test_plan,
        "guarantee_disclaimer": (
            "No historical, simulated, or paper trading result guarantees future returns. "
            "This research plan makes no profit claim of any kind."
        ),
    }
