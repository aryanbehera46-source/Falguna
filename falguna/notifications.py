"""Notification Center (Product Experience V2, Section 13): lightweight,
persisted notifications for real state transitions Falguna already produced
-- a mission reaching a terminal status, a research query finishing. This
module never invents an event; every call site that creates a notification
passes in state read from the real StateStore rows (run status, research
status) at the moment the transition actually happened.

Deliberately spam-avoidant: `notify_once` is keyed on (ref_type, ref_id,
kind) and is a no-op if a notification for that exact triple already
exists, so a resumed/retried mission that reaches the same terminal status
twice does not flood the center.
"""
from typing import List, Optional

from .store import StateStore, utcnow


class NotificationStore:
    def __init__(self, store: StateStore):
        self.store = store

    def notify_once(self, kind: str, title: str, body: str = "",
                     ref_type: Optional[str] = None, ref_id: Optional[str] = None) -> Optional[str]:
        if ref_type and ref_id:
            existing = self.store.list(
                "notifications", "kind=? AND ref_type=? AND ref_id=?", (kind, ref_type, ref_id)
            )
            if existing:
                return None
        return self.store.create("notifications", {
            "kind": kind, "title": title, "body": body,
            "ref_type": ref_type, "ref_id": ref_id, "read": 0, "created_at": utcnow(),
        })

    def list_notifications(self, limit: int = 50) -> List[dict]:
        rows = self.store.list("notifications")
        rows.sort(key=lambda r: r["created_at"], reverse=True)
        return rows[:limit]

    def unread_count(self) -> int:
        return sum(1 for r in self.store.list("notifications") if not r.get("read"))

    def mark_read(self, notification_id: str) -> None:
        if not self.store.get("notifications", notification_id):
            raise ValueError("notification not found")
        self.store.update("notifications", notification_id, read=1)

    def mark_all_read(self) -> int:
        count = 0
        for row in self.store.list("notifications", "read=0"):
            self.store.update("notifications", row["id"], read=1)
            count += 1
        return count
