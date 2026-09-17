"""Tests for the Analytics/Feedback loop and growth experiments
(falguna/analytics_growth.py). A real `source` is mandatory on every
metric; simulated data must never feed a real recommendation; a genuine
lack of data must yield "insufficient_data", never a guessed decision.
"""

import tempfile
import unittest
from pathlib import Path

from falguna.analytics_growth import (
    METRIC_KINDS,
    AnalyticsError,
    AnalyticsStore,
    GrowthAgent,
    GrowthExperimentStore,
)
from falguna.audit import AuditLog
from falguna.media import BrandStore, ContentStore
from falguna.publishing import PublicationStore
from falguna.store import StateStore


class AnalyticsTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = StateStore(root / "state.db")
        self.store.migrate()
        self.audit = AuditLog(root / "audit.jsonl")
        self.analytics = AnalyticsStore(self.store, self.audit)
        self.growth = GrowthAgent(self.analytics)
        self.experiments = GrowthExperimentStore(self.store, self.audit)
        brands = BrandStore(self.store, self.audit)
        contents = ContentStore(self.store, self.audit)
        publications = PublicationStore(self.store, self.audit)
        brand_id = brands.create("TTT", actor="Aryan")
        self.content_id = contents.create(brand_id, "Post", "image_post", actor="Aryan")
        self.publication_id = publications.create(self.content_id, "instagram", actor="system")

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()


class AnalyticsStoreTests(AnalyticsTestBase):
    def test_record_requires_valid_metric_kind(self):
        with self.assertRaises(AnalyticsError):
            self.analytics.record(self.publication_id, "vibes", 10, source="manual_entry")

    def test_record_requires_source(self):
        with self.assertRaises(AnalyticsError):
            self.analytics.record(self.publication_id, "views", 10, source="")

    def test_record_rejects_negative_value(self):
        with self.assertRaises(AnalyticsError):
            self.analytics.record(self.publication_id, "views", -5, source="manual_entry")

    def test_record_persists_real_metric(self):
        metric_id = self.analytics.record(self.publication_id, "views", 500, source="manual_entry", actor="Aryan")
        rows = self.analytics.list(self.publication_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], metric_id)
        self.assertEqual(rows[0]["value"], 500.0)
        self.assertEqual(rows[0]["source"], "manual_entry")

    def test_real_only_filters_out_simulated_source(self):
        self.analytics.record(self.publication_id, "views", 100, source="manual_entry")
        self.analytics.record(self.publication_id, "views", 999, source="simulated_qa")
        real_rows = self.analytics.list(self.publication_id, real_only=True)
        self.assertEqual(len(real_rows), 1)
        self.assertEqual(real_rows[0]["source"], "manual_entry")
        all_rows = self.analytics.list(self.publication_id, real_only=False)
        self.assertEqual(len(all_rows), 2)

    def test_all_declared_metric_kinds_are_accepted(self):
        for kind in METRIC_KINDS:
            self.analytics.record(self.publication_id, kind, 1, source="manual_entry")


class GrowthAgentTests(AnalyticsTestBase):
    def test_no_data_is_insufficient(self):
        rec = self.growth.recommend(self.publication_id)
        self.assertEqual(rec["decision"], "insufficient_data")

    def test_only_simulated_data_is_still_insufficient(self):
        self.analytics.record(self.publication_id, "views", 10000, source="simulated_qa")
        self.analytics.record(self.publication_id, "engagement", 5000, source="simulated_qa")
        rec = self.growth.recommend(self.publication_id)
        self.assertEqual(rec["decision"], "insufficient_data")  # simulated data never feeds a real recommendation

    def test_zero_views_is_insufficient_even_with_other_metrics(self):
        self.analytics.record(self.publication_id, "clicks", 5, source="manual_entry")
        rec = self.growth.recommend(self.publication_id)
        self.assertEqual(rec["decision"], "insufficient_data")

    def test_strong_engagement_recommends_repeat(self):
        self.analytics.record(self.publication_id, "views", 1000, source="manual_entry")
        self.analytics.record(self.publication_id, "engagement", 150, source="manual_entry")  # 15%
        rec = self.growth.recommend(self.publication_id)
        self.assertEqual(rec["decision"], "repeat")
        self.assertAlmostEqual(rec["engagement_rate"], 0.15)

    def test_moderate_engagement_recommends_test(self):
        self.analytics.record(self.publication_id, "views", 1000, source="manual_entry")
        self.analytics.record(self.publication_id, "engagement", 50, source="manual_entry")  # 5%
        rec = self.growth.recommend(self.publication_id)
        self.assertEqual(rec["decision"], "test")

    def test_weak_engagement_recommends_stop(self):
        self.analytics.record(self.publication_id, "views", 1000, source="manual_entry")
        self.analytics.record(self.publication_id, "engagement", 10, source="manual_entry")  # 1%
        rec = self.growth.recommend(self.publication_id)
        self.assertEqual(rec["decision"], "stop")

    def test_recommendation_discloses_real_metrics_used(self):
        self.analytics.record(self.publication_id, "views", 200, source="manual_entry")
        self.analytics.record(self.publication_id, "engagement", 40, source="manual_entry")
        rec = self.growth.recommend(self.publication_id)
        self.assertEqual(rec["metrics"]["views"], 200.0)
        self.assertEqual(rec["metrics"]["engagement"], 40.0)


class GrowthExperimentTests(AnalyticsTestBase):
    def test_create_requires_hypothesis(self):
        with self.assertRaises(AnalyticsError):
            self.experiments.create("")

    def test_create_starts_running(self):
        experiment_id = self.experiments.create("Shorter hooks help", content_id=self.content_id, actor="Aryan")
        self.assertEqual(self.experiments.get(experiment_id)["status"], "RUNNING")

    def test_record_result_completes_experiment(self):
        experiment_id = self.experiments.create("Shorter hooks help", actor="Aryan")
        self.experiments.record_result(experiment_id, result="engagement rose", decision="adopt", actor="Aryan")
        experiment = self.experiments.get(experiment_id)
        self.assertEqual(experiment["status"], "COMPLETED")
        self.assertEqual(experiment["decision"], "adopt")

    def test_cannot_record_result_twice(self):
        experiment_id = self.experiments.create("Shorter hooks help", actor="Aryan")
        self.experiments.record_result(experiment_id, result="x", decision="adopt", actor="Aryan")
        with self.assertRaises(AnalyticsError):
            self.experiments.record_result(experiment_id, result="y", decision="adopt again", actor="Aryan")

    def test_cancel(self):
        experiment_id = self.experiments.create("Shorter hooks help", actor="Aryan")
        self.experiments.cancel(experiment_id, actor="Aryan")
        self.assertEqual(self.experiments.get(experiment_id)["status"], "CANCELLED")

    def test_list_filters_by_content_and_status(self):
        e1 = self.experiments.create("Hyp 1", content_id=self.content_id, actor="Aryan")
        self.experiments.create("Hyp 2", actor="Aryan")
        rows = self.experiments.list(content_id=self.content_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], e1)
        running_rows = self.experiments.list(status="RUNNING")
        self.assertEqual(len(running_rows), 2)


if __name__ == "__main__":
    unittest.main()
