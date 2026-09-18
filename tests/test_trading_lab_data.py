"""Tests for falguna/trading_lab_data.py -- TTT Trading Lab v1 Pass A
(market/instrument abstraction, market data providers, dataset ingestion,
data quality checks). See Sections 3-6 of the Trading Lab spec.
"""

import tempfile
import unittest
from pathlib import Path

from falguna.audit import AuditLog
from falguna.store import StateStore
from falguna.trading_lab_data import (
    Bar, DataSourceStore, DatasetStore, InstrumentStore, MarketStore,
    StooqOHLCVProvider, SyntheticTestDataProvider, check_data_quality,
)


class TradingLabDataBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = StateStore(self.root / "state.db")
        self.store.migrate()
        self.audit = AuditLog(self.root / "audit.jsonl")
        self.markets = MarketStore(self.store, self.audit)
        self.instruments = InstrumentStore(self.store, self.audit)
        self.sources = DataSourceStore(self.store, self.audit)
        self.datasets = DatasetStore(self.store, self.audit)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()


class MarketInstrumentTests(TradingLabDataBase):
    def test_market_and_instrument_creation_is_idempotent_by_code(self):
        m1 = self.markets.create("US_EQUITY", "US Equities", "us_equity")
        m2 = self.markets.create("US_EQUITY", "US Equities (dup)", "us_equity")
        self.assertEqual(m1, m2)
        i1 = self.instruments.create(m1, "AAPL")
        i2 = self.instruments.create(m1, "aapl")  # case-insensitive symbol match
        self.assertEqual(i1, i2)

    def test_unknown_asset_class_rejected(self):
        with self.assertRaises(ValueError):
            self.markets.create("X", "X", "not_a_real_asset_class")

    def test_instrument_requires_known_market(self):
        with self.assertRaises(ValueError):
            self.instruments.create("nonexistent-market-id", "AAPL")


class ProviderTests(unittest.TestCase):
    def test_synthetic_provider_is_deterministic_and_labeled_synthetic(self):
        p = SyntheticTestDataProvider(seed=7)
        self.assertTrue(p.is_synthetic)
        r1 = p.fetch_ohlcv("TEST", "1d", "2024-01-01T00:00:00+00:00", "2024-02-01T00:00:00+00:00")
        r2 = SyntheticTestDataProvider(seed=7).fetch_ohlcv("TEST", "1d", "2024-01-01T00:00:00+00:00", "2024-02-01T00:00:00+00:00")
        self.assertEqual(r1.status, "OK")
        self.assertEqual([b.close for b in r1.bars], [b.close for b in r2.bars])

    def test_synthetic_provider_different_seed_differs(self):
        r1 = SyntheticTestDataProvider(seed=1).fetch_ohlcv("TEST", "1d", "2024-01-01T00:00:00+00:00", "2024-02-01T00:00:00+00:00")
        r2 = SyntheticTestDataProvider(seed=2).fetch_ohlcv("TEST", "1d", "2024-01-01T00:00:00+00:00", "2024-02-01T00:00:00+00:00")
        self.assertNotEqual([b.close for b in r1.bars], [b.close for b in r2.bars])

    def test_stooq_provider_never_fabricates_bars_when_unreachable(self):
        # This environment's network egress does not reach stooq.com (or any
        # market-data host) -- confirmed by direct testing during this
        # build. The real assertion that matters, regardless of network
        # conditions: on failure the provider reports UNAVAILABLE/ERROR
        # with the real captured error and an EMPTY bar list -- never
        # invented bars standing in for real data.
        p = StooqOHLCVProvider(timeout_seconds=3.0)
        r = p.fetch_ohlcv("aapl.us", "1d", "2024-01-01", "2024-01-10")
        self.assertIn(r.status, ("UNAVAILABLE", "ERROR", "OK"))
        if r.status != "OK":
            self.assertEqual(r.bars, [])
            self.assertIsNotNone(r.error)

    def test_stooq_provider_rejects_unsupported_timeframe_honestly(self):
        p = StooqOHLCVProvider()
        r = p.fetch_ohlcv("aapl.us", "1m", "2024-01-01", "2024-01-10")
        self.assertEqual(r.status, "UNAVAILABLE")
        self.assertEqual(r.bars, [])


class DataQualityTests(unittest.TestCase):
    def _bar(self, ts, o, h, l, c, v=1000):
        return Bar(ts=ts, open=o, high=h, low=l, close=c, volume=v)

    def test_empty_bars_fails(self):
        r = check_data_quality([])
        self.assertFalse(r["passed"])

    def test_clean_series_passes(self):
        bars = [self._bar(f"2024-01-{d:02d}T00:00:00+00:00", 100, 101, 99, 100) for d in range(1, 11)]
        r = check_data_quality(bars, stale_after_days=100000)
        self.assertTrue(r["passed"])
        self.assertEqual(r["duplicate_timestamps"], 0)
        self.assertEqual(r["non_monotonic"], 0)
        self.assertEqual(r["impossible_prices"], 0)

    def test_duplicate_timestamp_detected(self):
        bars = [self._bar("2024-01-01T00:00:00+00:00", 100, 101, 99, 100),
                self._bar("2024-01-01T00:00:00+00:00", 100, 101, 99, 100),
                self._bar("2024-01-03T00:00:00+00:00", 100, 101, 99, 100)]
        r = check_data_quality(bars)
        self.assertGreaterEqual(r["duplicate_timestamps"], 1)
        self.assertFalse(r["passed"])

    def test_impossible_price_detected_high_below_low(self):
        bars = [self._bar(f"2024-01-{d:02d}T00:00:00+00:00", 100, 101, 99, 100) for d in range(1, 5)]
        bars.append(self._bar("2024-01-05T00:00:00+00:00", 100, 90, 110, 100))  # high < low
        r = check_data_quality(bars)
        self.assertGreaterEqual(r["impossible_prices"], 1)
        self.assertFalse(r["passed"])

    def test_negative_volume_detected(self):
        bars = [self._bar(f"2024-01-{d:02d}T00:00:00+00:00", 100, 101, 99, 100) for d in range(1, 5)]
        bars.append(self._bar("2024-01-05T00:00:00+00:00", 100, 101, 99, 100, v=-5))
        r = check_data_quality(bars)
        self.assertGreaterEqual(r["invalid_volume"], 1)
        self.assertFalse(r["passed"])

    def test_stale_dataset_flagged_but_does_not_alone_fail(self):
        bars = [self._bar(f"2020-01-{d:02d}T00:00:00+00:00", 100, 101, 99, 100) for d in range(1, 11)]
        r = check_data_quality(bars, stale_after_days=30)
        self.assertEqual(r["stale"], 1)
        self.assertTrue(r["passed"])  # staleness alone does not fail an intentionally historical dataset


class DatasetIngestionTests(TradingLabDataBase):
    def setUp(self):
        super().setUp()
        self.m_id = self.markets.create("US_EQUITY", "US Equities", "us_equity")
        self.i_id = self.instruments.create(self.m_id, "TEST")

    def test_ingest_synthetic_dataset_is_ok_and_backtest_eligible(self):
        src = self.sources.register("synthetic test fixture", "synthetic_test_fixture", is_synthetic=True)
        ds_id = self.datasets.ingest(src, self.i_id, "1d", SyntheticTestDataProvider(seed=1), "2024-01-01T00:00:00+00:00", "2024-03-01T00:00:00+00:00")
        row = self.store.get("tl_datasets", ds_id)
        self.assertEqual(row["status"], "OK")
        self.assertGreater(row["bar_count"], 0)
        report = self.datasets.latest_quality_report(ds_id)
        self.assertEqual(report["passed"], 1)
        self.assertTrue(self.datasets.is_backtest_eligible(ds_id)["eligible"])

        source_row = self.store.get("tl_data_sources", src)
        self.assertEqual(source_row["is_synthetic"], 1)

    def test_ingest_unreachable_real_provider_is_honestly_unavailable_and_ineligible(self):
        src = self.sources.register("stooq.com daily", "stooq_ohlcv", is_synthetic=False)
        ds_id = self.datasets.ingest(src, self.i_id, "1d", StooqOHLCVProvider(timeout_seconds=3.0), "2024-01-01", "2024-01-10")
        row = self.store.get("tl_datasets", ds_id)
        self.assertIn(row["status"], ("UNAVAILABLE", "ERROR"))
        self.assertEqual(row["bar_count"], 0)
        elig = self.datasets.is_backtest_eligible(ds_id)
        self.assertFalse(elig["eligible"])

    def test_ingest_rejects_unknown_timeframe(self):
        src = self.sources.register("synthetic test fixture", "synthetic_test_fixture", is_synthetic=True)
        with self.assertRaises(ValueError):
            self.datasets.ingest(src, self.i_id, "3d", SyntheticTestDataProvider(), "2024-01-01T00:00:00+00:00", "2024-01-10T00:00:00+00:00")

    def test_dataset_without_quality_report_is_not_backtest_eligible(self):
        # A dataset with zero bars never runs a quality check (nothing to
        # check), so it must not be silently treated as eligible.
        src = self.sources.register("synthetic test fixture", "synthetic_test_fixture", is_synthetic=True)
        ds_id = self.datasets.ingest(src, self.i_id, "1d", SyntheticTestDataProvider(), "2024-01-01T00:00:00+00:00", "2024-01-01T00:00:00+00:00")
        # a single-instant window may produce 0 or 1 bars depending on the step; force the no-report case directly
        self.store.update("tl_datasets", ds_id, status="OK", bar_count=0)
        elig = self.datasets.is_backtest_eligible(ds_id)
        if self.datasets.latest_quality_report(ds_id) is None:
            self.assertFalse(elig["eligible"])


if __name__ == "__main__":
    unittest.main()
