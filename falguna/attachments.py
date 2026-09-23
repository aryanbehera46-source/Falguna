"""Files / Attachments V2 (Product Experience V2, Section 14-15): real file
storage for chat attachments, backed by the filesystem under
<app_root>/.falguna/attachments/ and indexed in the `attachments` table.

Security posture, matching research.py's discipline for untrusted external
data: an uploaded file's bytes are never executed, parsed, or interpreted
by this module -- they are written to disk under a name derived from the
attachment's own generated id (never the client-supplied filename, which is
kept only as inert display metadata) and served back with the stored
content-type and a Content-Disposition that never lets a filename break out
of the header. A hard size cap (default 10 MB) is enforced before any byte
is written.
"""
import base64
import hashlib
import re
from pathlib import Path
from typing import List, Optional

from .store import StateStore, utcnow

MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024


class AttachmentError(ValueError):
    pass


def _safe_display_name(filename: str) -> str:
    name = Path(str(filename or "file")).name.strip() or "file"
    return name[:200]


class AttachmentStore:
    def __init__(self, store: StateStore, app_root: Path):
        self.store = store
        self.root = Path(app_root) / ".falguna" / "attachments"
        self.root.mkdir(parents=True, exist_ok=True)

    def save_base64(self, filename: str, content_type: Optional[str], data_base64: str,
                     conversation_id: Optional[str] = None, message_id: Optional[str] = None,
                     browser_session_id: Optional[str] = None) -> dict:
        try:
            data = base64.b64decode(data_base64, validate=True)
        except Exception as exc:
            raise AttachmentError("attachment data must be valid base64") from exc
        if not data:
            raise AttachmentError("attachment is empty")
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise AttachmentError(f"attachment exceeds the {MAX_ATTACHMENT_BYTES // (1024 * 1024)}MB limit")
        display_name = _safe_display_name(filename)
        content_type = re.sub(r"[^a-zA-Z0-9/.+-]", "", str(content_type or "application/octet-stream"))[:100] or "application/octet-stream"
        sha = hashlib.sha256(data).hexdigest()
        record_id = None
        import uuid
        record_id = str(uuid.uuid4())
        storage_path = self.root / record_id
        storage_path.write_bytes(data)
        record = {
            "conversation_id": conversation_id, "message_id": message_id, "filename": display_name,
            "content_type": content_type, "size_bytes": len(data), "sha256": sha,
            "storage_rel_path": str(storage_path.relative_to(Path(self.store.path).parent)),
            "created_at": utcnow(), "browser_session_id": browser_session_id,
        }
        self.store.create("attachments", record, record_id=record_id)
        return {"id": record_id, **record}

    def attach_to_message(self, attachment_ids: List[str], conversation_id: str, message_id: str) -> None:
        for attachment_id in attachment_ids:
            row = self.store.get("attachments", attachment_id)
            if not row or row.get("conversation_id") not in (None, conversation_id):
                raise AttachmentError(f"attachment not found or not part of this conversation: {attachment_id}")
            self.store.update("attachments", attachment_id, conversation_id=conversation_id, message_id=message_id)

    def get(self, attachment_id: str) -> Optional[dict]:
        return self.store.get("attachments", attachment_id)

    def read_bytes(self, attachment_id: str) -> Optional[bytes]:
        row = self.get(attachment_id)
        if not row:
            return None
        path = Path(self.store.path).parent / row["storage_rel_path"]
        if not path.is_file():
            return None
        return path.read_bytes()

    def list_for_conversation(self, conversation_id: str) -> List[dict]:
        return self.store.list("attachments", "conversation_id=?", (conversation_id,))

    def list_for_browser_session(self, browser_session_id: str) -> List[dict]:
        return self.store.list("attachments", "browser_session_id=?", (browser_session_id,))

    def list_all(self, limit: int = 200) -> List[dict]:
        rows = self.store.list("attachments")
        rows.sort(key=lambda r: r["created_at"], reverse=True)
        return rows[:limit]
