from pathlib import Path

from .audit import AuditLog
from .orchestrator import ControlPlane
from .store import StateStore


def open_control_plane(root: Path):
    state = Path(root) / ".falguna"
    store = StateStore(state / "state.db")
    store.migrate()
    return ControlPlane(store, AuditLog(state / "audit.jsonl"), state), store

