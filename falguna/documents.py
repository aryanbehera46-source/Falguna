"""TTT Digital Workforce v1 -- Document/Spreadsheet work (Section 6, Pass B).

A practical, deterministic abstraction for document and spreadsheet work --
not a clone of an office suite. Documents are plain structured text
(markdown-friendly); spreadsheets are a list of row dicts with an explicit
column order, persisted as JSON, exportable to real CSV. Summaries are
computed facts about the content on file (row/word counts, column names,
simple numeric aggregates) -- never a fabricated or AI-guessed description.
"""

import csv
import io
import json
from typing import Any, Dict, List, Optional

from .audit import AuditLog
from .store import StateStore, utcnow

DOC_TYPES = {"document", "report", "notes", "spreadsheet"}


class DocumentError(ValueError):
    pass


class DocumentStore:
    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit

    def create_document(
        self, title: str, content_text: str, doc_type: str = "document",
        department: Optional[str] = None, source_task_id: Optional[str] = None, actor: str = "system",
    ) -> str:
        if not title or not title.strip():
            raise DocumentError("title is required")
        if doc_type not in DOC_TYPES or doc_type == "spreadsheet":
            raise DocumentError(f"doc_type must be one of {sorted(DOC_TYPES - {'spreadsheet'})}")
        now = utcnow()
        doc_id = self.store.create("wf_documents", {
            "title": title.strip(), "doc_type": doc_type, "department": department,
            "content_text": content_text or "", "rows_json": None, "format": "text",
            "source_task_id": source_task_id, "actor": actor, "created_at": now, "updated_at": now,
        })
        self.audit.append("WF_DOCUMENT_CREATED", {"doc_id": doc_id, "title": title, "doc_type": doc_type, "actor": actor})
        return doc_id

    def update_document(self, doc_id: str, content_text: str, actor: str) -> Dict[str, Any]:
        doc = self._require(doc_id)
        if doc["doc_type"] == "spreadsheet":
            raise DocumentError("use update_spreadsheet for a spreadsheet document")
        self.store.update("wf_documents", doc_id, content_text=content_text or "")
        self.audit.append("WF_DOCUMENT_UPDATED", {"doc_id": doc_id, "actor": actor})
        return self.get(doc_id)

    def create_spreadsheet(
        self, title: str, columns: List[str], rows: List[Dict[str, Any]],
        department: Optional[str] = None, source_task_id: Optional[str] = None, actor: str = "system",
    ) -> str:
        if not title or not title.strip():
            raise DocumentError("title is required")
        if not columns:
            raise DocumentError("columns is required")
        now = utcnow()
        payload = {"columns": columns, "rows": rows or []}
        doc_id = self.store.create("wf_documents", {
            "title": title.strip(), "doc_type": "spreadsheet", "department": department,
            "content_text": None, "rows_json": json.dumps(payload), "format": "table",
            "source_task_id": source_task_id, "actor": actor, "created_at": now, "updated_at": now,
        })
        self.audit.append("WF_SPREADSHEET_CREATED", {"doc_id": doc_id, "title": title, "row_count": len(rows or []), "actor": actor})
        return doc_id

    def update_spreadsheet(self, doc_id: str, rows: List[Dict[str, Any]], actor: str, columns: Optional[List[str]] = None) -> Dict[str, Any]:
        doc = self._require(doc_id)
        if doc["doc_type"] != "spreadsheet":
            raise DocumentError("this document is not a spreadsheet")
        payload = json.loads(doc["rows_json"]) if doc.get("rows_json") else {"columns": [], "rows": []}
        if columns is not None:
            payload["columns"] = columns
        payload["rows"] = rows
        self.store.update("wf_documents", doc_id, rows_json=json.dumps(payload))
        self.audit.append("WF_SPREADSHEET_UPDATED", {"doc_id": doc_id, "row_count": len(rows), "actor": actor})
        return self.get(doc_id)

    def import_csv(self, title: str, csv_text: str, department: Optional[str] = None, actor: str = "system") -> str:
        reader = csv.DictReader(io.StringIO(csv_text))
        rows = [dict(r) for r in reader]
        columns = reader.fieldnames or []
        return self.create_spreadsheet(title, columns, rows, department=department, actor=actor)

    def export_csv(self, doc_id: str) -> str:
        doc = self._require(doc_id)
        if doc["doc_type"] != "spreadsheet":
            raise DocumentError("this document is not a spreadsheet")
        payload = json.loads(doc["rows_json"]) if doc.get("rows_json") else {"columns": [], "rows": []}
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=payload["columns"])
        writer.writeheader()
        for row in payload["rows"]:
            writer.writerow({c: row.get(c, "") for c in payload["columns"]})
        return buf.getvalue()

    def summarize(self, doc_id: str) -> Dict[str, Any]:
        """A summary of real, computed facts about the document -- never a
        generated description of what the content "means"."""
        doc = self._require(doc_id)
        if doc["doc_type"] == "spreadsheet":
            payload = json.loads(doc["rows_json"]) if doc.get("rows_json") else {"columns": [], "rows": []}
            return {"doc_id": doc_id, "kind": "spreadsheet", "columns": payload["columns"], "row_count": len(payload["rows"])}
        text = doc.get("content_text") or ""
        return {
            "doc_id": doc_id, "kind": doc["doc_type"], "word_count": len(text.split()),
            "line_count": text.count("\n") + (1 if text else 0),
        }

    def _require(self, doc_id: str) -> Dict[str, Any]:
        doc = self.store.get("wf_documents", doc_id)
        if not doc:
            raise DocumentError("document not found")
        return doc

    def get(self, doc_id: str) -> Optional[Dict[str, Any]]:
        return self.store.get("wf_documents", doc_id)

    def list(self, department: Optional[str] = None, doc_type: Optional[str] = None) -> List[Dict[str, Any]]:
        if department and doc_type:
            rows = self.store.list("wf_documents", "department=? AND doc_type=?", (department, doc_type))
        elif department:
            rows = self.store.list("wf_documents", "department=?", (department,))
        elif doc_type:
            rows = self.store.list("wf_documents", "doc_type=?", (doc_type,))
        else:
            rows = self.store.list("wf_documents")
        return list(reversed(rows))
