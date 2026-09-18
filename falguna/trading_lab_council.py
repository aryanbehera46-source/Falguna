"""TTT Trading Lab v1 -- Pass D: Trading Council + Strategy Graveyard.

Sections 16-17 of the TTT TRADING LAB V1 spec.

The Trading Council is deliberately a deterministic, rule-based aggregator
over REAL evidence already on file (backtest metrics, stress-test
verdicts, strategy-spec completeness) -- never a black-box judgment call
and never a source of a "guaranteed" recommendation (Section 21). Its only
authority is to RECOMMEND a next step; the one step with a real-world
consequence -- approving a strategy for PAPER trading -- always creates a
Needs Aryan item (Section 18) rather than flipping the strategy's status
itself. The Council has no authority to approve real-money trading at all:
there is no such decision in DECISION_KINDS below (see Section 22).
"""

from __future__ import annotations

import json as _json
from typing import Any, Dict, List, Optional

from .audit import AuditLog
from .store import StateStore, utcnow
from .trading_lab_backtest import MIN_TRADES_FOR_CONFIDENCE
from .trading_lab_strategy import StrategyStore
from .ttt_hq import NeedsAryanQueue

REVIEWER_ROLES = {"strategy_research", "quant_backtest", "risk_manager", "adversarial_reviewer", "capital_preservation"}
REVIEW_VERDICTS = {"approve", "revise", "reject"}
DECISION_KINDS = {"continue_research", "revise", "approve_for_paper", "pause", "reject", "graveyard"}

MAX_ACCEPTABLE_DRAWDOWN_PCT = 25.0
MIN_SURVIVED_STRESS_FRACTION = 0.6


class ReviewStore:
    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit

    def record(self, strategy_version_id: str, reviewer_role: str, verdict: str, notes: str, actor: str = "system") -> str:
        if reviewer_role not in REVIEWER_ROLES:
            raise ValueError(f"unknown reviewer_role: {reviewer_role}")
        if verdict not in REVIEW_VERDICTS:
            raise ValueError(f"unknown verdict: {verdict}")
        review_id = self.store.create("tl_reviews", {
            "strategy_version_id": strategy_version_id, "reviewer_role": reviewer_role,
            "verdict": verdict, "notes": notes, "actor": actor, "created_at": utcnow(),
        })
        self.audit.append("TL_REVIEW_RECORDED", {"review_id": review_id, "strategy_version_id": strategy_version_id,
                                                    "reviewer_role": reviewer_role, "verdict": verdict})
        return review_id

    def list_for_version(self, strategy_version_id: str) -> List[Dict[str, Any]]:
        return self.store.list("tl_reviews", "strategy_version_id=?", (strategy_version_id,))


def _review_strategy_research(strategy_version: Dict[str, Any]) -> Dict[str, str]:
    has_assumptions = bool((strategy_version.get("assumptions") or "").strip())
    has_risks = bool((strategy_version.get("known_risks") or "").strip())
    if has_assumptions and has_risks:
        return {"verdict": "approve", "notes": "strategy version documents assumptions and known risks"}
    return {"verdict": "revise", "notes": "strategy version is missing documented assumptions and/or known risks -- Section 7 requires both"}


def _review_quant_backtest(backtest: Optional[Dict[str, Any]], oos_warnings: List[str]) -> Dict[str, str]:
    if backtest is None:
        return {"verdict": "revise", "notes": "no backtest on file yet -- cannot evaluate quantitatively"}
    metrics = _json.loads(backtest["metrics_json"])
    if metrics["trade_count"] < MIN_TRADES_FOR_CONFIDENCE:
        return {"verdict": "revise", "notes": f"only {metrics['trade_count']} trades -- below the {MIN_TRADES_FOR_CONFIDENCE}-trade confidence minimum"}
    if any("overfit" in w.lower() for w in oos_warnings):
        return {"verdict": "reject", "notes": "out-of-sample evaluation suggests overfitting to the train period"}
    if metrics["net_return"] is not None and metrics["net_return"] <= 0:
        return {"verdict": "reject", "notes": f"backtest net_return is {metrics['net_return']} -- not profitable in-sample"}
    return {"verdict": "approve", "notes": f"backtest net_return={metrics['net_return']}, trade_count={metrics['trade_count']}, no overfitting flags"}


def _review_risk_manager(backtest: Optional[Dict[str, Any]]) -> Dict[str, str]:
    if backtest is None:
        return {"verdict": "revise", "notes": "no backtest on file yet -- cannot evaluate drawdown/risk"}
    metrics = _json.loads(backtest["metrics_json"])
    dd = metrics.get("max_drawdown_pct")
    if dd is not None and dd > MAX_ACCEPTABLE_DRAWDOWN_PCT:
        return {"verdict": "reject", "notes": f"backtest max_drawdown_pct={dd}% exceeds the {MAX_ACCEPTABLE_DRAWDOWN_PCT}% preservation threshold"}
    return {"verdict": "approve", "notes": f"backtest max_drawdown_pct={dd}% is within the {MAX_ACCEPTABLE_DRAWDOWN_PCT}% threshold"}


def _review_adversarial(stress_tests: List[Dict[str, Any]]) -> Dict[str, str]:
    if not stress_tests:
        return {"verdict": "revise", "notes": "no Red-Team stress review on file yet"}
    survived_or_weakened = len([s for s in stress_tests if s["verdict"] in ("SURVIVED", "WEAKENED")])
    fraction = survived_or_weakened / len(stress_tests)
    failed = [s["scenario"] for s in stress_tests if s["verdict"] == "FAILED"]
    if fraction < MIN_SURVIVED_STRESS_FRACTION:
        return {"verdict": "reject", "notes": f"only {survived_or_weakened}/{len(stress_tests)} stress scenarios survived; failed: {failed}"}
    if failed:
        return {"verdict": "revise", "notes": f"{survived_or_weakened}/{len(stress_tests)} scenarios survived, but failed under: {failed}"}
    return {"verdict": "approve", "notes": f"all {len(stress_tests)} stress scenarios survived or weakened without failing"}


def _review_capital_preservation(stress_tests: List[Dict[str, Any]]) -> Dict[str, str]:
    if not stress_tests:
        return {"verdict": "revise", "notes": "no stress-test drawdown evidence on file yet"}
    worst_dd = 0.0
    for s in stress_tests:
        m = _json.loads(s["metrics_json"])
        worst_dd = max(worst_dd, m.get("max_drawdown_pct") or 0.0)
    if worst_dd > MAX_ACCEPTABLE_DRAWDOWN_PCT * 1.5:
        return {"verdict": "reject", "notes": f"worst-case stressed drawdown {worst_dd:.2f}% is unacceptable for paper capital preservation"}
    return {"verdict": "approve", "notes": f"worst-case stressed drawdown {worst_dd:.2f}% is acceptable"}


def run_trading_council(store: StateStore, audit: AuditLog, needs_aryan: NeedsAryanQueue,
                         strategy_id: str, strategy_version_id: str,
                         backtest_id: Optional[str] = None, oos_warnings: Optional[List[str]] = None,
                         stress_test_ids: Optional[List[str]] = None, actor: str = "system") -> str:
    """Runs all five reviewer perspectives (Section 17) against whatever
    real evidence (backtest, stress tests) has actually been produced, then
    aggregates into one tl_council_decisions row. Returns the decision id.

    Decision rule (deterministic, no hidden judgment call):
    - any reviewer verdict 'reject' -> decision 'reject' (or 'graveyard' if
      this is already a second rejection for this strategy -- see below)
    - no rejects but any 'revise' -> decision 'revise' (or 'continue_research'
      if the strategy has no backtest yet at all)
    - all reviewers 'approve' -> decision 'approve_for_paper', which creates
      a Needs Aryan item (Section 18) -- the Council never activates paper
      trading itself.
    """
    strategy_version = store.get("tl_strategy_versions", strategy_version_id)
    if not strategy_version:
        raise ValueError("unknown strategy_version_id")
    backtest = store.get("tl_backtests", backtest_id) if backtest_id else None
    stress_tests = [store.get("tl_stress_tests", sid) for sid in (stress_test_ids or [])]
    stress_tests = [s for s in stress_tests if s]
    oos_warnings = oos_warnings or []

    review_store = ReviewStore(store, audit)
    reviews = {
        "strategy_research": _review_strategy_research(strategy_version),
        "quant_backtest": _review_quant_backtest(backtest, oos_warnings),
        "risk_manager": _review_risk_manager(backtest),
        "adversarial_reviewer": _review_adversarial(stress_tests),
        "capital_preservation": _review_capital_preservation(stress_tests),
    }
    review_ids = {}
    for role, r in reviews.items():
        review_ids[role] = review_store.record(strategy_version_id, role, r["verdict"], r["notes"], actor)

    verdicts = [r["verdict"] for r in reviews.values()]
    prior_decisions = store.list("tl_council_decisions", "strategy_id=?", (strategy_id,))
    prior_rejections = len([d for d in prior_decisions if d["decision"] == "reject"])

    if "reject" in verdicts:
        decision = "graveyard" if prior_rejections >= 1 else "reject"
    elif backtest is None:
        decision = "continue_research"
    elif "revise" in verdicts:
        decision = "revise"
    else:
        decision = "approve_for_paper"

    rationale_parts = [f"{role}: {reviews[role]['verdict']} -- {reviews[role]['notes']}" for role in REVIEWER_ROLES]
    rationale = "; ".join(rationale_parts)
    now = utcnow()
    decision_id = store.create("tl_council_decisions", {
        "strategy_id": strategy_id, "strategy_version_id": strategy_version_id, "decision": decision,
        "rationale": rationale, "reviews_json": _json.dumps({role: {"review_id": review_ids[role], **reviews[role]} for role in REVIEWER_ROLES}),
        "actor": actor, "created_at": now,
    })
    audit.append("TL_COUNCIL_DECISION", {"decision_id": decision_id, "strategy_id": strategy_id, "decision": decision})

    strategies = StrategyStore(store, audit)
    strategy = strategies.get(strategy_id)
    if decision == "approve_for_paper":
        needs_aryan.create_item(
            kind="trading_paper_activation_request",
            title=f"Trading Council recommends paper activation: {strategy['name']}",
            what_is_needed="Approve or reject moving this strategy to PAPER_ACTIVE. This only enables PAPER/SIMULATED trading -- no real money is ever involved.",
            actor=actor, recommendation="approve_for_paper", rationale=rationale,
            risk="paper capital only", ref_type="tl_strategy", ref_id=strategy_id,
        )
        if strategy["status"] in ("BACKTESTING", "IDEA", "RESEARCHING"):
            strategies.transition(strategy_id, "REVIEW", actor, "Trading Council evidence complete, routing to review")
            strategy = strategies.get(strategy_id)  # re-fetch: status just changed
        if strategy["status"] == "REVIEW":
            strategies.transition(strategy_id, "PAPER_APPROVED", actor, f"Trading Council decision: {decision}")
    elif decision == "reject":
        from .trading_lab_strategy import ALLOWED_TRANSITIONS
        current = strategy["status"]
        if current not in ("REJECTED", "GRAVEYARD"):
            if "REJECTED" not in ALLOWED_TRANSITIONS.get(current, set()):
                # only PAPER_ACTIVE lacks a direct path to REJECTED; detour via REVIEW, which both statuses allow
                strategies.transition(strategy_id, "REVIEW", actor, "routing through review before rejection")
            strategies.transition(strategy_id, "REJECTED", actor, f"Trading Council decision: {decision} -- {rationale}")
    elif decision == "graveyard":
        bury_strategy(store, audit, strategy_id, strategy_version_id,
                       reason_rejected="rejected twice by the Trading Council -- see review history",
                       failed_metrics=_json.loads(backtest["metrics_json"]) if backtest else None,
                       reviewer_notes=rationale, actor=actor)
    elif decision == "revise":
        if strategy["status"] in ("BACKTESTING", "REVIEW"):
            strategies.transition(strategy_id, "RESEARCHING", actor, f"Trading Council decision: {decision}")
    return decision_id


# ---------------------------------------------------------------------------
# Strategy Graveyard (Section 16)
# ---------------------------------------------------------------------------

def bury_strategy(store: StateStore, audit: AuditLog, strategy_id: str, strategy_version_id: str,
                   reason_rejected: str, failed_metrics: Optional[Dict[str, Any]] = None,
                   failure_conditions: str = "", reviewer_notes: str = "", actor: str = "system") -> str:
    strategies = StrategyStore(store, audit)
    strategy = strategies.get(strategy_id)
    if not strategy:
        raise ValueError("unknown strategy_id")
    now = utcnow()
    grave_id = store.create("tl_graveyard", {
        "strategy_id": strategy_id, "strategy_version_id": strategy_version_id, "reason_rejected": reason_rejected,
        "failed_metrics_json": _json.dumps(failed_metrics or {}), "failure_conditions": failure_conditions,
        "reviewer_notes": reviewer_notes, "actor": actor, "killed_at": now, "created_at": now,
    })
    if strategy["status"] != "GRAVEYARD":
        # Route to GRAVEYARD via whatever allowed transition gets there
        # from the current status, per ALLOWED_TRANSITIONS. REVIEW,
        # PAPER_ACTIVE, PAUSED and REJECTED all reach GRAVEYARD directly;
        # every other status (IDEA, RESEARCHING, BACKTESTING,
        # PAPER_APPROVED) allows REJECTED directly, so that is always a
        # valid one-hop detour first.
        from .trading_lab_strategy import ALLOWED_TRANSITIONS
        current = strategy["status"]
        if "GRAVEYARD" not in ALLOWED_TRANSITIONS.get(current, set()):
            if "REJECTED" not in ALLOWED_TRANSITIONS.get(current, set()):
                raise ValueError(f"cannot route strategy status {current} to GRAVEYARD")
            strategies.transition(strategy_id, "REJECTED", actor, "routing to graveyard")
        strategies.transition(strategy_id, "GRAVEYARD", actor, f"buried: {reason_rejected}")
    audit.append("TL_STRATEGY_GRAVEYARDED", {"grave_id": grave_id, "strategy_id": strategy_id, "reason": reason_rejected})
    return grave_id


def check_graveyard_for_similar(store: StateStore, market_id: Optional[str], signal_names: List[str]) -> List[Dict[str, Any]]:
    """Section 16: 'prevent repeatedly rediscovering the same bad
    strategy.' Before researching a new idea, check whether a strategy
    using the same signal(s) in the same market has already been buried,
    and surface why -- this is advisory (it does not block creation), the
    same way research always remains free to happen, but it means the
    graveyard's lessons are actually visible next time, not just recorded."""
    graveyard = store.list("tl_graveyard", "1=1", ())
    hits = []
    for g in graveyard:
        strategy = store.get("tl_strategies", g["strategy_id"])
        version = store.get("tl_strategy_versions", g["strategy_version_id"])
        if not strategy or not version:
            continue
        if market_id and strategy["market_id"] != market_id:
            continue
        entry_signal = _json.loads(version["entry_rules_json"]).get("signal")
        if entry_signal in signal_names:
            hits.append({
                "strategy_name": strategy["name"], "signal": entry_signal,
                "reason_rejected": g["reason_rejected"], "killed_at": g["killed_at"],
            })
    return hits
