"""Long-running Phase 9 graph supervisor and supported local CLI.

Handler factories are ordinary ``module:function`` callables receiving the
control plane, store, organization id and approved roots. This keeps worker
construction explicit, testable and provider-neutral.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import signal
import socket
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable

from .frontier import FrontierControlPlane, FrontierError
from .frontier_workers import GraphWorkerDispatcher
from .runtime import open_control_plane
from .store import utcnow


class GraphSupervisor:
    def __init__(self, control: FrontierControlPlane, organization_id: str,
                 handlers: Dict[str, Callable], poll_seconds: float = 1.0,
                 worker_id: str | None = None, max_attempts: int = 3):
        self.control = control
        self.store = control.store
        self.organization_id = organization_id
        self.poll_seconds = max(0.05, float(poll_seconds))
        self.worker_id = worker_id or f"dispatcher-{socket.gethostname()}-{os.getpid()}"
        self.dispatcher = GraphWorkerDispatcher(control, organization_id, handlers,
                                                max_attempts=max_attempts)
        self._stop = threading.Event()

    def status(self) -> Dict[str, Any]:
        nodes = self.store.list("p9_graph_nodes", "organization_id=?", (self.organization_id,))
        counts: Dict[str, int] = {}
        for node in nodes:
            counts[node["status"]] = counts.get(node["status"], 0) + 1
        return {"organization_id": self.organization_id, "worker_id": self.worker_id,
                "pid": os.getpid(), "observed_at": utcnow(), "node_counts": counts,
                "emergency_stopped": any(o["status"] == "STOPPED" for o in self.store.list(
                    "p9_objectives", "organization_id=?", (self.organization_id,)))}

    def stop(self) -> None:
        self._stop.set()

    def run_once(self) -> bool:
        self.control.recover_expired_leases(self.organization_id)
        nodes = sorted(self.store.list("p9_graph_nodes", "organization_id=? AND status=?",
                                       (self.organization_id, "READY")),
                       key=lambda row: (row["created_at"], row["id"]))
        for node in nodes:
            objective = self.store.get("p9_objectives", node["objective_id"])
            if not objective or objective["status"] in {"PAUSED", "STOPPED", "FAILED", "COMPLETED"}:
                continue
            self.dispatcher.run_node(node["id"], f"{self.worker_id}:{node['node_type'].lower()}")
            return True
        return False

    def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                worked = self.run_once()
            except (FrontierError, Exception):
                worked = False
            self._stop.wait(0 if worked else self.poll_seconds)


def _factory(spec: str):
    module, name = spec.split(":", 1)
    return getattr(importlib.import_module(module), name)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FALGUNA durable graph dispatcher")
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--organization", required=True)
    parser.add_argument("--handler-factory", required=True, help="module:function")
    parser.add_argument("--approved-root", action="append", default=[])
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)
    runtime, store = open_control_plane(Path(args.state_dir))
    control = FrontierControlPlane(store, runtime.audit)
    handlers = _factory(args.handler_factory)(control, store, args.organization,
                                              [Path(p).resolve() for p in args.approved_root])
    supervisor = GraphSupervisor(control, args.organization, handlers, args.poll_seconds)
    if args.status:
        print(json.dumps(supervisor.status(), sort_keys=True))
        store.close()
        return 0
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: supervisor.stop())
    try:
        if args.once:
            supervisor.run_once()
        else:
            supervisor.run_forever()
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
