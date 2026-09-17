"""Tests for the concrete Digital Workforce worker roles and the
browser/computer execution foundation (falguna/workforce_workers.py).

Central concerns exercised here: the honest default (no browser adapter
exists, so browser_research/web_navigation/data_collection/forms_admin
always BLOCK and escalate -- never a fabricated success), the API-before-
browser preference order, the simulated channel being test-only, and each
practical worker (data/document/spreadsheet/email/content) actually
producing real, verifiable output via the stores it wraps.
"""

import json
import tempfile
import unittest
from pathlib import Path

from falguna.audit import AuditLog
from falguna.documents import DocumentStore
from falguna.email_admin import EmailStore
from falguna.research import SourceResult
from falguna.store import StateStore
from falguna.ttt_hq import NeedsAryanQueue
from falguna.workforce import WorkforceOrchestrator, WorkforceTaskStore
from falguna.workforce_workers import (
    BrowserWorker,
    ContentWorker,
    DataWorker,
    DocumentWorker,
    EmailAdminWorker,
    ManualBrowserChannel,
    ResearchWorker,
    SimulatedBrowserChannel,
    SpreadsheetWorker,
)


class WorkerTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = StateStore(root / "state.db")
        self.store.migrate()
        self.audit = AuditLog(root / "audit.jsonl")
        self.needs_aryan = NeedsAryanQueue(self.store, self.audit)
        self.tasks = WorkforceTaskStore(self.store, self.audit)
        self.docs = DocumentStore(self.store, self.audit)
        self.emails = EmailStore(self.store, self.audit, needs_aryan=self.needs_aryan)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _orch(self, *workers):
        o = WorkforceOrchestrator(self.store, self.audit, needs_aryan=self.needs_aryan)
        for w in workers:
            o.register_worker(w)
        return o


class BrowserWorkerTests(WorkerTestBase):
    def test_default_channel_is_manual_and_always_blocks(self):
        worker = BrowserWorker()
        self.assertIsInstance(worker.channel, ManualBrowserChannel)
        orch = self._orch(worker)
        for task_type in ["browser_research", "web_navigation", "data_collection", "forms_admin"]:
            task_id = self.tasks.create("media", f"Do {task_type}", task_type, actor="system")
            result = orch.execute(task_id)
            self.assertEqual(result["status"], "NEEDS_ARYAN", task_type)
            self.assertIsNotNone(result["needs_aryan_id"])
            item = self.store.get("needs_aryan_items", result["needs_aryan_id"])
            self.assertEqual(item["kind"], "workforce_action_approval")

    def test_simulated_channel_is_never_the_default(self):
        # SimulatedBrowserChannel must be explicitly passed in -- it is
        # never reachable through ordinary construction.
        worker = BrowserWorker()
        self.assertNotIsInstance(worker.channel, SimulatedBrowserChannel)

    def test_simulated_channel_completes_and_labels_itself_simulated(self):
        worker = BrowserWorker(channel=SimulatedBrowserChannel())
        orch = self._orch(worker)
        task_id = self.tasks.create("media", "Simulated nav", "web_navigation", actor="system")
        result = orch.execute(task_id)
        self.assertEqual(result["status"], "COMPLETED")
        evidence = json.loads(result["evidence_json"])
        self.assertTrue(evidence.get("simulated"))

    def test_api_handler_preferred_over_browser_channel(self):
        calls = []

        def api_handler(task):
            calls.append(task["id"])
            return {"found": "via api"}

        worker = BrowserWorker(api_handler=api_handler)
        orch = self._orch(worker)
        task_id = self.tasks.create("media", "API lookup", "data_collection", actor="system")
        result = orch.execute(task_id)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["execution_method"], "API")
        self.assertEqual(calls, [task_id])

    def test_api_handler_returning_none_falls_back_to_browser_channel(self):
        worker = BrowserWorker(api_handler=lambda task: None)
        orch = self._orch(worker)
        task_id = self.tasks.create("media", "API lookup falls back", "data_collection", actor="system")
        result = orch.execute(task_id)
        self.assertEqual(result["status"], "NEEDS_ARYAN")  # fell through to ManualBrowserChannel -> BLOCKED


class ResearchWorkerTests(WorkerTestBase):
    def test_no_provider_blocks_honestly(self):
        orch = self._orch(ResearchWorker())
        task_id = self.tasks.create("media", "Find trends", "trend_discovery", actor="system")
        result = orch.execute(task_id)
        self.assertEqual(result["status"], "NEEDS_ARYAN")

    def test_provider_wired_returns_real_sources(self):
        def fake_search(query, count):
            return [SourceResult(url="https://example.com/a", title="A", snippet="snippet a")]

        orch = self._orch(ResearchWorker(search_provider=fake_search))
        task_id = self.tasks.create("media", "Find trends", "research", actor="system", inputs={"query": "AI trends"})
        result = orch.execute(task_id)
        self.assertEqual(result["status"], "COMPLETED")
        outputs = json.loads(result["outputs_json"])
        self.assertEqual(outputs["query"], "AI trends")
        self.assertEqual(len(outputs["sources"]), 1)
        self.assertEqual(outputs["sources"][0]["url"], "https://example.com/a")

    def test_provider_returning_no_sources_fails_rather_than_fabricating(self):
        orch = self._orch(ResearchWorker(search_provider=lambda q, c: []))
        task_id = self.tasks.create("media", "Find trends", "research", actor="system", inputs={"query": "nothing"})
        result = orch.execute(task_id)
        self.assertEqual(result["status"], "READY")  # FAILED -> bounded retry -> READY


class DataWorkerTests(WorkerTestBase):
    def test_no_records_fails(self):
        orch = self._orch(DataWorker())
        task_id = self.tasks.create("ops", "Dedupe", "data_processing", actor="system")
        result = orch.execute(task_id)
        self.assertEqual(result["status"], "READY")  # FAILED -> retried

    def test_dedupe_by_key(self):
        orch = self._orch(DataWorker())
        task_id = self.tasks.create(
            "ops", "Dedupe leads", "data_processing", actor="system",
            inputs={"records": [{"email": "a@x.com"}, {"email": "a@x.com"}, {"email": "b@x.com"}], "dedupe_key": "email"},
        )
        result = orch.execute(task_id)
        self.assertEqual(result["status"], "COMPLETED")
        outputs = json.loads(result["outputs_json"])
        self.assertEqual(len(outputs["records"]), 2)
        evidence = json.loads(result["evidence_json"])
        self.assertEqual(evidence["removed"], 1)

    def test_dedupe_without_key_uses_full_row_identity(self):
        orch = self._orch(DataWorker())
        task_id = self.tasks.create(
            "ops", "Dedupe rows", "data_processing", actor="system",
            inputs={"records": [{"a": 1}, {"a": 1}, {"a": 2}]},
        )
        result = orch.execute(task_id)
        outputs = json.loads(result["outputs_json"])
        self.assertEqual(len(outputs["records"]), 2)


class DocumentAndSpreadsheetWorkerTests(WorkerTestBase):
    def test_document_creation_produces_real_document(self):
        orch = self._orch(DocumentWorker(self.docs))
        task_id = self.tasks.create(
            "media", "Draft brand brief", "document_creation", actor="system",
            inputs={"title": "Brand Brief", "content_text": "Some content"},
        )
        result = orch.execute(task_id)
        self.assertEqual(result["status"], "COMPLETED")
        doc_id = json.loads(result["outputs_json"])["doc_id"]
        doc = self.docs.get(doc_id)
        self.assertEqual(doc["title"], "Brand Brief")
        self.assertEqual(doc["content_text"], "Some content")

    def test_presentation_creation_uses_report_doc_type(self):
        orch = self._orch(DocumentWorker(self.docs))
        task_id = self.tasks.create(
            "media", "Draft deck outline", "presentation_creation", actor="system",
            inputs={"title": "Q3 Deck", "content_text": "Slide notes"},
        )
        result = orch.execute(task_id)
        doc_id = json.loads(result["outputs_json"])["doc_id"]
        self.assertEqual(self.docs.get(doc_id)["doc_type"], "report")

    def test_spreadsheet_creation_requires_columns_and_rows(self):
        orch = self._orch(SpreadsheetWorker(self.docs))
        task_id = self.tasks.create("ops", "Build tracker", "spreadsheet_creation", actor="system", inputs={})
        result = orch.execute(task_id)
        self.assertEqual(result["status"], "READY")  # FAILED -> retried

    def test_spreadsheet_creation_produces_real_spreadsheet(self):
        orch = self._orch(SpreadsheetWorker(self.docs))
        task_id = self.tasks.create(
            "ops", "Build tracker", "spreadsheet_creation", actor="system",
            inputs={"title": "Tracker", "columns": ["name", "status"], "rows": [{"name": "x", "status": "open"}]},
        )
        result = orch.execute(task_id)
        self.assertEqual(result["status"], "COMPLETED")
        doc_id = json.loads(result["outputs_json"])["doc_id"]
        summary = self.docs.summarize(doc_id)
        self.assertEqual(summary["row_count"], 1)


class EmailAdminWorkerTests(WorkerTestBase):
    def test_email_preparation_requires_body(self):
        orch = self._orch(EmailAdminWorker(self.emails))
        task_id = self.tasks.create("sales", "Draft email", "email_preparation", actor="system", inputs={})
        result = orch.execute(task_id)
        self.assertEqual(result["status"], "READY")  # FAILED -> retried

    def test_email_preparation_drafts_but_never_sends(self):
        orch = self._orch(EmailAdminWorker(self.emails))
        task_id = self.tasks.create(
            "sales", "Draft follow-up", "email_preparation", actor="system",
            inputs={"to_address": "client@example.com", "subject": "Follow up", "body": "Checking in."},
        )
        result = orch.execute(task_id)
        self.assertEqual(result["status"], "COMPLETED")
        email_id = json.loads(result["outputs_json"])["email_id"]
        email = self.emails.get(email_id)
        self.assertEqual(email["status"], "PREPARED")
        self.assertIsNotNone(email["needs_aryan_id"])


class ContentWorkerTests(WorkerTestBase):
    def test_content_operations_produces_a_brief(self):
        orch = self._orch(ContentWorker(self.docs))
        task_id = self.tasks.create(
            "media", "Draft content brief", "content_operations", actor="system",
            inputs={"title": "Reel: 3 tips", "notes": "Keep it under 30s"},
        )
        result = orch.execute(task_id)
        self.assertEqual(result["status"], "COMPLETED")
        doc_id = json.loads(result["outputs_json"])["doc_id"]
        doc = self.docs.get(doc_id)
        self.assertIn("Reel: 3 tips", doc["content_text"])
        self.assertIn("Keep it under 30s", doc["content_text"])


class RoutingTests(WorkerTestBase):
    def test_unregistered_task_type_is_unroutable_and_escalates(self):
        orch = self._orch(DataWorker())  # doesn't support document_creation
        task_id = self.tasks.create("media", "Draft brief", "document_creation", actor="system")
        result = orch.execute(task_id)
        self.assertEqual(result["status"], "NEEDS_ARYAN")


if __name__ == "__main__":
    unittest.main()
