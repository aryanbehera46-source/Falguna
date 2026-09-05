import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict


class AuditLog:
    """Append-only JSONL with a SHA-256 hash chain and restrictive permissions."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch(mode=0o600)
        os.chmod(self.path, 0o600)

    def _last_hash(self) -> str:
        last = "GENESIS"
        with self.path.open() as handle:
            for line in handle:
                if line.strip():
                    last = json.loads(line)["hash"]
        return last

    def append(self, event: str, data: Dict[str, Any]) -> str:
        body = {"timestamp": datetime.now(timezone.utc).isoformat(), "event": event, "data": data, "previous_hash": self._last_hash()}
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        record = {**body, "hash": digest}
        with self.path.open("a") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return digest

    def verify(self) -> bool:
        previous = "GENESIS"
        with self.path.open() as handle:
            for line in handle:
                record = json.loads(line)
                digest = record.pop("hash")
                if record["previous_hash"] != previous:
                    return False
                expected = hashlib.sha256(json.dumps(record, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                if digest != expected:
                    return False
                previous = digest
        return True

