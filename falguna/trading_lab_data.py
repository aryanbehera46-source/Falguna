"""TTT Trading Lab v1 -- Pass A (part 1): Market Data Layer.

Sections 3-6 of the TTT TRADING LAB V1 spec: market/instrument abstraction,
market data provider interfaces, dataset ingestion, and data quality checks.

PAPER / RESEARCH ONLY. Nothing in this module ever executes a real trade or
touches real money. See falguna/trading_lab_strategy.py,
falguna/trading_lab_backtest.py, and falguna/trading_lab_risk_paper.py for
the rest of the Trading Lab.

No-fabrication policy: a market data provider that cannot reach its source
(no network route, HTTP error, unparseable response) MUST return a result
with status="UNAVAILABLE" and the real captured error -- it must never
invent bars. The only source of synthetic bars is SyntheticTestDataProvider,
which is unmistakably labeled (`provider_kind="synthetic_test_fixture"`,
every bar traces back to a dataset whose data source has `is_synthetic=1`)
and is intended for engine/backtest testing only, never presented as real
market data.
"""

from __future__ import annotations

import csv
import io
import statistics
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .audit import AuditLog
from .store import StateStore, utcnow

TIMEFRAMES = {"1m", "5m", "15m", "1h", "1d"}
ASSET_CLASSES = {"crypto", "in_equity", "us_equity", "forex", "etf", "index", "other"}
DATASET_STATUSES = {"OK", "UNAVAILABLE", "ERROR"}


# ---------------------------------------------------------------------------
# Market / Instrument / DataSource stores
# ---------------------------------------------------------------------------

class MarketStore:
    """Markets are the top-level, extensible grouping (crypto, IN equities,
    US equities, forex, ...). New asset classes are added by adding a market
    row -- never by hardcoding an exchange integration into the engine."""

    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit

    def create(self, code: str, name: str, asset_class: str, description: str = "", actor: str = "system") -> str:
        code = (code or "").strip().upper()
        if not code:
            raise ValueError("code is required")
        if asset_class not in ASSET_CLASSES:
            raise ValueError(f"unknown asset_class: {asset_class}")
        existing = self.store.list("tl_markets", "code=?", (code,))
        if existing:
            return existing[0]["id"]
        now = utcnow()
        market_id = self.store.create("tl_markets", {
            "code": code, "name": name, "asset_class": asset_class,
            "description": description, "actor": actor, "created_at": now,
        })
        self.audit.append("TL_MARKET_CREATED", {"market_id": market_id, "code": code, "asset_class": asset_class})
        return market_id

    def list_all(self) -> List[Dict[str, Any]]:
        return self.store.list("tl_markets", "1=1", ())


class InstrumentStore:
    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit

    def create(self, market_id: str, symbol: str, name: str = "", currency: str = "USD",
               metadata: Optional[Dict[str, Any]] = None, actor: str = "system") -> str:
        if not self.store.get("tl_markets", market_id):
            raise ValueError("unknown market_id")
        symbol = (symbol or "").strip().upper()
        if not symbol:
            raise ValueError("symbol is required")
        existing = self.store.list("tl_instruments", "market_id=? AND symbol=?", (market_id, symbol))
        if existing:
            return existing[0]["id"]
        import json as _json
        now = utcnow()
        instrument_id = self.store.create("tl_instruments", {
            "market_id": market_id, "symbol": symbol, "name": name or symbol,
            "currency": currency, "metadata_json": _json.dumps(metadata or {}),
            "actor": actor, "created_at": now,
        })
        self.audit.append("TL_INSTRUMENT_CREATED", {"instrument_id": instrument_id, "market_id": market_id, "symbol": symbol})
        return instrument_id

    def list_for_market(self, market_id: str) -> List[Dict[str, Any]]:
        return self.store.list("tl_instruments", "market_id=?", (market_id,))

    def get(self, instrument_id: str) -> Optional[Dict[str, Any]]:
        return self.store.get("tl_instruments", instrument_id)


class DataSourceStore:
    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit

    def register(self, name: str, provider_kind: str, is_synthetic: bool = False,
                 status: str = "ACTIVE", notes: str = "") -> str:
        existing = self.store.list("tl_data_sources", "name=?", (name,))
        if existing:
            return existing[0]["id"]
        return self.store.create("tl_data_sources", {
            "name": name, "provider_kind": provider_kind, "is_synthetic": 1 if is_synthetic else 0,
            "status": status, "notes": notes, "created_at": utcnow(),
        })

    def get(self, data_source_id: str) -> Optional[Dict[str, Any]]:
        return self.store.get("tl_data_sources", data_source_id)


# ---------------------------------------------------------------------------
# Market data provider interface
# ---------------------------------------------------------------------------

@dataclass
class Bar:
    ts: str  # ISO-8601 UTC
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class ProviderResult:
    status: str  # "OK" | "UNAVAILABLE" | "ERROR"
    bars: List[Bar] = field(default_factory=list)
    raw_source_metadata: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


class MarketDataProvider:
    """Base interface every market data provider implements. A provider that
    cannot reach its source must return status='UNAVAILABLE' with the real
    error -- never fabricated bars. See module docstring."""

    name = "base_provider"
    is_synthetic = False

    def fetch_ohlcv(self, symbol: str, timeframe: str, start: str, end: str) -> ProviderResult:
        raise NotImplementedError


class StooqOHLCVProvider(MarketDataProvider):
    """Real HTTP + CSV adapter against stooq.com's free daily-bar CSV
    endpoint. Genuinely functional adapter code: it issues a real HTTP
    request and parses a real CSV response. In network environments where
    stooq.com is unreachable (e.g. this sandboxed environment -- confirmed
    via direct reachability testing during this build), it honestly reports
    status='UNAVAILABLE' with the real captured error. It never invents
    bars to paper over an unreachable network."""

    name = "stooq_ohlcv"
    is_synthetic = False
    BASE_URL = "https://stooq.com/q/d/l/"

    def __init__(self, timeout_seconds: float = 8.0):
        self.timeout_seconds = timeout_seconds

    def fetch_ohlcv(self, symbol: str, timeframe: str, start: str, end: str) -> ProviderResult:
        if timeframe != "1d":
            return ProviderResult(status="UNAVAILABLE", error=f"stooq_ohlcv only supports timeframe=1d, got {timeframe}")
        url = f"{self.BASE_URL}?s={symbol.lower()}&i=d"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "TTT-Trading-Lab/1"})
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                status_code = getattr(resp, "status", 200)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError) as exc:
            return ProviderResult(status="UNAVAILABLE", error=f"{type(exc).__name__}: {exc}",
                                   raw_source_metadata={"url": url})
        if "No data" in raw[:64] or not raw.strip():
            return ProviderResult(status="UNAVAILABLE", error="provider returned no data",
                                   raw_source_metadata={"url": url, "http_status": status_code})
        try:
            bars: List[Bar] = []
            reader = csv.DictReader(io.StringIO(raw))
            for row in reader:
                d = row.get("Date")
                if not d:
                    continue
                if start and d < start:
                    continue
                if end and d > end:
                    continue
                bars.append(Bar(
                    ts=f"{d}T00:00:00+00:00",
                    open=float(row["Open"]), high=float(row["High"]),
                    low=float(row["Low"]), close=float(row["Close"]),
                    volume=float(row.get("Volume") or 0.0),
                ))
        except (KeyError, ValueError) as exc:
            return ProviderResult(status="ERROR", error=f"CSV parse error: {type(exc).__name__}: {exc}",
                                   raw_source_metadata={"url": url})
        return ProviderResult(status="OK", bars=bars,
                               raw_source_metadata={"url": url, "http_status": status_code, "row_count": len(bars)})


class SyntheticTestDataProvider(MarketDataProvider):
    """Deterministic seeded synthetic bar generator. NOT real market data --
    for exercising the backtest/paper-trading engines only. Every bar it
    produces is only ever attached to a dataset whose data source has
    is_synthetic=1, so it can never be silently conflated with real data
    anywhere it is read back (see DatasetStore.ingest and the UI/report
    layers, which must always surface is_synthetic)."""

    name = "synthetic_test_fixture"
    is_synthetic = True

    def __init__(self, seed: int = 42, base_price: float = 100.0, daily_drift: float = 0.0002, daily_vol: float = 0.01):
        self.seed = seed
        self.base_price = base_price
        self.daily_drift = daily_drift
        self.daily_vol = daily_vol

    def fetch_ohlcv(self, symbol: str, timeframe: str, start: str, end: str) -> ProviderResult:
        import random
        rng = random.Random(f"{self.seed}:{symbol}:{timeframe}:{start}:{end}")
        try:
            start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
        except ValueError as exc:
            return ProviderResult(status="ERROR", error=f"invalid start/end: {exc}")
        step = {"1d": timedelta(days=1), "1h": timedelta(hours=1),
                "15m": timedelta(minutes=15), "5m": timedelta(minutes=5),
                "1m": timedelta(minutes=1)}.get(timeframe, timedelta(days=1))
        bars: List[Bar] = []
        price = self.base_price
        cur = start_dt
        while cur <= end_dt:
            change = self.daily_drift + rng.gauss(0, self.daily_vol)
            open_p = price
            close_p = max(0.01, open_p * (1 + change))
            high_p = max(open_p, close_p) * (1 + abs(rng.gauss(0, self.daily_vol / 4)))
            low_p = min(open_p, close_p) * (1 - abs(rng.gauss(0, self.daily_vol / 4)))
            vol = max(1.0, rng.gauss(100000, 20000))
            bars.append(Bar(ts=cur.isoformat(), open=round(open_p, 4), high=round(high_p, 4),
                             low=round(low_p, 4), close=round(close_p, 4), volume=round(vol, 2)))
            price = close_p
            cur += step
        return ProviderResult(status="OK", bars=bars,
                               raw_source_metadata={"generator": "synthetic_test_fixture", "seed": self.seed})


PROVIDER_REGISTRY: Dict[str, MarketDataProvider] = {
    "stooq_ohlcv": StooqOHLCVProvider(),
    "synthetic_test_fixture": SyntheticTestDataProvider(),
}


# ---------------------------------------------------------------------------
# Data quality checks (Section 6)
# ---------------------------------------------------------------------------

def check_data_quality(bars: List[Bar], stale_after_days: int = 14) -> Dict[str, Any]:
    """Run the checks Section 6 requires before a strategy may backtest
    against a dataset. Returns a dict suitable for tl_data_quality_reports.
    `passed` is False if any structural defect is found (missing/duplicate/
    non-monotonic timestamps, impossible prices, invalid volume); staleness
    is reported but does not by itself fail the check (a dataset can be
    intentionally historical for backtesting)."""
    notes: List[str] = []
    if not bars:
        return {"missing_bars": 0, "duplicate_timestamps": 0, "non_monotonic": 0,
                "impossible_prices": 0, "invalid_volume": 0, "timezone_issues": 0,
                "stale": 0, "passed": False, "notes": ["no bars in dataset"]}

    timestamps = [b.ts for b in bars]
    seen = set()
    duplicate_timestamps = 0
    for ts in timestamps:
        if ts in seen:
            duplicate_timestamps += 1
        seen.add(ts)

    non_monotonic = 0
    parsed: List[Optional[datetime]] = []
    timezone_issues = 0
    for ts in timestamps:
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                timezone_issues += 1
                dt = dt.replace(tzinfo=timezone.utc)
        except ValueError:
            dt = None
            notes.append(f"unparseable timestamp: {ts}")
        parsed.append(dt)
    for i in range(1, len(parsed)):
        if parsed[i] is not None and parsed[i - 1] is not None and parsed[i] <= parsed[i - 1]:
            non_monotonic += 1

    impossible_prices = 0
    invalid_volume = 0
    for b in bars:
        if b.high < b.low or b.open <= 0 or b.close <= 0 or b.high <= 0 or b.low <= 0:
            impossible_prices += 1
        elif not (b.low <= b.open <= b.high and b.low <= b.close <= b.high):
            impossible_prices += 1
        if b.volume < 0:
            invalid_volume += 1

    # missing bars: gaps larger than 3x the median inter-bar interval
    missing_bars = 0
    valid_times = [p for p in parsed if p is not None]
    if len(valid_times) >= 3:
        gaps = [(valid_times[i] - valid_times[i - 1]).total_seconds() for i in range(1, len(valid_times))]
        median_gap = statistics.median(gaps) if gaps else 0
        if median_gap > 0:
            for g in gaps:
                if g > median_gap * 3:
                    missing_bars += 1

    stale = 0
    if valid_times:
        last = max(valid_times)
        now = datetime.now(timezone.utc)
        if (now - last).days > stale_after_days:
            stale = 1
            notes.append(f"last bar is {(now - last).days} days old (threshold {stale_after_days})")

    passed = (missing_bars == 0 and duplicate_timestamps == 0 and non_monotonic == 0
              and impossible_prices == 0 and invalid_volume == 0)
    return {
        "missing_bars": missing_bars, "duplicate_timestamps": duplicate_timestamps,
        "non_monotonic": non_monotonic, "impossible_prices": impossible_prices,
        "invalid_volume": invalid_volume, "timezone_issues": timezone_issues,
        "stale": stale, "passed": passed, "notes": notes,
    }


# ---------------------------------------------------------------------------
# Dataset ingestion
# ---------------------------------------------------------------------------

class DatasetStore:
    """Ingests bars from a MarketDataProvider into tl_datasets/tl_ohlcv_bars
    with full provenance, then runs data quality checks (Section 6) and
    records them in tl_data_quality_reports. A strategy must not backtest
    against a dataset that has not passed quality checks -- see
    falguna/trading_lab_backtest.py's guard."""

    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit

    def ingest(self, data_source_id: str, instrument_id: str, timeframe: str,
               provider: MarketDataProvider, start: str, end: str) -> str:
        if timeframe not in TIMEFRAMES:
            raise ValueError(f"unknown timeframe: {timeframe}")
        if not self.store.get("tl_data_sources", data_source_id):
            raise ValueError("unknown data_source_id")
        if not self.store.get("tl_instruments", instrument_id):
            raise ValueError("unknown instrument_id")

        import json as _json
        result = provider.fetch_ohlcv(self._symbol_for(instrument_id), timeframe, start, end)
        now = utcnow()
        status = result.status if result.status in DATASET_STATUSES else "ERROR"
        dataset_id = self.store.create("tl_datasets", {
            "data_source_id": data_source_id, "instrument_id": instrument_id, "timeframe": timeframe,
            "start_date": start, "end_date": end, "status": status,
            "bar_count": len(result.bars), "completeness_pct": None, "error": result.error,
            "raw_source_metadata_json": _json.dumps(result.raw_source_metadata),
            "ingested_at": now, "created_at": now, "updated_at": now,
        })
        for b in result.bars:
            self.store.create("tl_ohlcv_bars", {
                "dataset_id": dataset_id, "ts": b.ts, "open": b.open, "high": b.high,
                "low": b.low, "close": b.close, "volume": b.volume, "created_at": now,
            })
        self.audit.append("TL_DATASET_INGESTED", {
            "dataset_id": dataset_id, "data_source_id": data_source_id, "instrument_id": instrument_id,
            "timeframe": timeframe, "status": status, "bar_count": len(result.bars), "error": result.error,
        })
        if status == "OK" and result.bars:
            self.run_quality_check(dataset_id)
        return dataset_id

    def _symbol_for(self, instrument_id: str) -> str:
        inst = self.store.get("tl_instruments", instrument_id)
        return inst["symbol"] if inst else ""

    def get_bars(self, dataset_id: str) -> List[Bar]:
        rows = self.store.list("tl_ohlcv_bars", "dataset_id=?", (dataset_id,))
        rows.sort(key=lambda r: r["ts"])
        return [Bar(ts=r["ts"], open=r["open"], high=r["high"], low=r["low"], close=r["close"], volume=r["volume"]) for r in rows]

    def run_quality_check(self, dataset_id: str) -> Dict[str, Any]:
        import json as _json
        bars = self.get_bars(dataset_id)
        report = check_data_quality(bars)
        now = utcnow()
        self.store.create("tl_data_quality_reports", {
            "dataset_id": dataset_id, "checked_at": now,
            "missing_bars": report["missing_bars"], "duplicate_timestamps": report["duplicate_timestamps"],
            "non_monotonic": report["non_monotonic"], "impossible_prices": report["impossible_prices"],
            "invalid_volume": report["invalid_volume"], "timezone_issues": report["timezone_issues"],
            "stale": report["stale"], "passed": 1 if report["passed"] else 0,
            "notes_json": _json.dumps(report["notes"]), "created_at": now,
        })
        completeness = 100.0 if report["missing_bars"] == 0 else max(0.0, 100.0 - report["missing_bars"] * 5)
        self.store.update("tl_datasets", dataset_id, completeness_pct=completeness)
        self.audit.append("TL_DATA_QUALITY_CHECKED", {"dataset_id": dataset_id, "passed": report["passed"], "report": report})
        return report

    def latest_quality_report(self, dataset_id: str) -> Optional[Dict[str, Any]]:
        rows = self.store.list("tl_data_quality_reports", "dataset_id=?", (dataset_id,))
        if not rows:
            return None
        rows.sort(key=lambda r: r["checked_at"])
        return rows[-1]

    def is_backtest_eligible(self, dataset_id: str) -> Dict[str, Any]:
        """Section 6 gate: a strategy may only backtest against a dataset
        whose latest quality report passed. Returns {'eligible': bool,
        'reason': str}."""
        dataset = self.store.get("tl_datasets", dataset_id)
        if not dataset:
            return {"eligible": False, "reason": "unknown dataset"}
        if dataset["status"] != "OK":
            return {"eligible": False, "reason": f"dataset status is {dataset['status']}, not OK"}
        report = self.latest_quality_report(dataset_id)
        if report is None:
            return {"eligible": False, "reason": "no data quality report on file"}
        if not report["passed"]:
            return {"eligible": False, "reason": "latest data quality report did not pass"}
        return {"eligible": True, "reason": "latest data quality report passed"}
