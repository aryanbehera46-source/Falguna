"""Tests for the Document/Spreadsheet work foundation (falguna/documents.py)."""

import json
import tempfile
import unittest
from pathlib import Path

from falguna.audit import AuditLog
from falguna.documents import DocumentError, DocumentStore
from falguna.store import StateStore


class DocumentStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = StateStore(root / "state.db")
        self.store.migrate()
        self.audit = AuditLog(root / "audit.jsonl")
        self.docs = DocumentStore(self.store, self.audit)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def test_create_document_requires_title(self):
        with self.assertRaises(DocumentError):
            self.docs.create_document("", "content")

    def test_create_document_rejects_spreadsheet_doc_type(self):
        with self.assertRaises(DocumentError):
            self.docs.create_document("Title", "content", doc_type="spreadsheet")

    def test_create_and_read_document(self):
        doc_id = self.docs.create_document("Brand Brief", "Some content here", doc_type="notes", department="media")
        doc = self.docs.get(doc_id)
        self.assertEqual(doc["title"], "Brand Brief")
        self.assertEqual(doc["doc_type"], "notes")
        self.assertEqual(doc["content_text"], "Some content here")

    def test_update_document(self):
        doc_id = self.docs.create_document("Notes", "v1")
        self.docs.update_document(doc_id, "v2", actor="Aryan")
        self.assertEqual(self.docs.get(doc_id)["content_text"], "v2")

    def test_update_document_refuses_on_spreadsheet(self):
        doc_id = self.docs.create_spreadsheet("Sheet", ["a"], [{"a": 1}])
        with self.assertRaises(DocumentError):
            self.docs.update_document(doc_id, "nope", actor="Aryan")

    def test_create_spreadsheet_requires_title_and_columns(self):
        with self.assertRaises(DocumentError):
            self.docs.create_spreadsheet("", ["a"], [])
        with self.assertRaises(DocumentError):
            self.docs.create_spreadsheet("Sheet", [], [])

    def test_create_and_update_spreadsheet(self):
        doc_id = self.docs.create_spreadsheet("Tracker", ["name", "status"], [{"name": "x", "status": "open"}])
        doc = self.docs.get(doc_id)
        payload = json.loads(doc["rows_json"])
        self.assertEqual(payload["columns"], ["name", "status"])
        self.assertEqual(len(payload["rows"]), 1)

        self.docs.update_spreadsheet(doc_id, [{"name": "x", "status": "closed"}], actor="Aryan")
        updated = json.loads(self.docs.get(doc_id)["rows_json"])
        self.assertEqual(updated["rows"][0]["status"], "closed")

    def test_update_spreadsheet_refuses_on_document(self):
        doc_id = self.docs.create_document("Notes", "text")
        with self.assertRaises(DocumentError):
            self.docs.update_spreadsheet(doc_id, [{"a": 1}], actor="Aryan")

    def test_import_and_export_csv_round_trip(self):
        csv_text = "name,status\nAcme,open\nBeta,closed\n"
        doc_id = self.docs.import_csv("Imported", csv_text)
        doc = self.docs.get(doc_id)
        self.assertEqual(doc["doc_type"], "spreadsheet")
        payload = json.loads(doc["rows_json"])
        self.assertEqual(payload["columns"], ["name", "status"])
        self.assertEqual(len(payload["rows"]), 2)

        exported = self.docs.export_csv(doc_id)
        self.assertIn("Acme", exported)
        self.assertIn("Beta", exported)
        self.assertIn("name,status", exported.splitlines()[0])

    def test_export_csv_refuses_on_document(self):
        doc_id = self.docs.create_document("Notes", "text")
        with self.assertRaises(DocumentError):
            self.docs.export_csv(doc_id)

    def test_summarize_document_is_computed_not_fabricated(self):
        doc_id = self.docs.create_document("Notes", "one two three\nfour five")
        summary = self.docs.summarize(doc_id)
        self.assertEqual(summary["kind"], "document")
        self.assertEqual(summary["word_count"], 5)
        self.assertEqual(summary["line_count"], 2)

    def test_summarize_spreadsheet(self):
        doc_id = self.docs.create_spreadsheet("Tracker", ["a", "b"], [{"a": 1, "b": 2}, {"a": 3, "b": 4}])
        summary = self.docs.summarize(doc_id)
        self.assertEqual(summary["kind"], "spreadsheet")
        self.assertEqual(summary["row_count"], 2)
        self.assertEqual(summary["columns"], ["a", "b"])

    def test_get_missing_returns_none_but_require_raises(self):
        self.assertIsNone(self.docs.get("does-not-exist"))
        with self.assertRaises(DocumentError):
            self.docs.summarize("does-not-exist")

    def test_list_filters_by_department_and_doc_type(self):
        self.docs.create_document("A", "x", department="media", doc_type="document")
        self.docs.create_document("B", "x", department="ops", doc_type="notes")
        self.docs.create_spreadsheet("C", ["a"], [{"a": 1}], department="media")

        media_only = self.docs.list(department="media")
        self.assertEqual(len(media_only), 2)
        notes_only = self.docs.list(doc_type="notes")
        self.assertEqual(len(notes_only), 1)


if __name__ == "__main__":
    unittest.main()
