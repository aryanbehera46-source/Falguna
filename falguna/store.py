import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class StateStore:
    """Durable local adapter. PostgreSQL can replace this without changing callers."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path))
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")

    def migrate(self) -> None:
        schema = Path(__file__).with_name("schema_sqlite.sql").read_text()
        self.db.executescript(schema)
        self.db.commit()

    @contextmanager
    def transaction(self):
        try:
            yield self.db
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def create(self, table: str, values: Dict[str, Any], record_id: Optional[str] = None) -> str:
        allowed = {"missions", "requirements", "tasks", "task_steps", "runs", "checkpoints", "approvals", "model_calls", "cost_events", "artifacts"}
        if table not in allowed:
            raise ValueError("unknown table")
        record_id = record_id or str(uuid.uuid4())
        data = {"id": record_id, **values}
        columns = ",".join(data)
        marks = ",".join("?" for _ in data)
        with self.transaction() as db:
            db.execute(f"INSERT INTO {table} ({columns}) VALUES ({marks})", list(data.values()))
        return record_id

    def get(self, table: str, record_id: str) -> Optional[Dict[str, Any]]:
        row = self.db.execute(f"SELECT * FROM {table} WHERE id=?", (record_id,)).fetchone()
        return dict(row) if row else None

    def update(self, table: str, record_id: str, **values: Any) -> None:
        values["updated_at"] = utcnow()
        assignments = ",".join(f"{key}=?" for key in values)
        with self.transaction() as db:
            db.execute(f"UPDATE {table} SET {assignments} WHERE id=?", [*values.values(), record_id])

    def list(self, table: str, where: str = "1=1", params: Iterable[Any] = ()):
        return [dict(row) for row in self.db.execute(f"SELECT * FROM {table} WHERE {where} ORDER BY created_at", tuple(params))]

    def checkpoint(self, run_id: str, stage: str, payload: Dict[str, Any]) -> str:
        return self.create("checkpoints", {"run_id": run_id, "stage": stage, "payload": json.dumps(payload, sort_keys=True), "created_at": utcnow()})

    def latest_checkpoint(self, run_id: str):
        row = self.db.execute("SELECT * FROM checkpoints WHERE run_id=? ORDER BY created_at DESC LIMIT 1", (run_id,)).fetchone()
        return dict(row) if row else None

    def close(self):
        self.db.close()

