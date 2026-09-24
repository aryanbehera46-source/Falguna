"""Falguna Local AI Independence V1.1 -- Pass D: a conservative,
configurable concurrency gate for LOCAL heavyweight generation work
(Chat/Research/Work), shared across every caller that routes through
ModelRouter so no second, disconnected job-management system is ever
built (falguna/model_router.py's __call__/stream are the only two
places this is invoked from).

Deliberately a process-global singleton: Falguna runs as one process
per `falguna web` instance, and every Chat/Research/Work request in
that process shares the same finite CPU/RAM/thermal budget on the real
machine, so the gate must too -- a per-request scheduler would let an
unbounded number of requests each believe they are the only one and
all run concurrently, exactly the resource-exhaustion risk this exists
to prevent.

Embedding calls (local semantic search/memory) are never gated here --
Memory & Knowledge V2 calls an OllamaProvider directly for embeddings
and never passes through ModelRouter.__call__/stream at all (see
model_router.py's _chat_capable docstring), so this module never needs
to special-case "is this an embedding request" itself: only a real
chat/research/work generation call ever reaches acquire().

External-provider calls (Codex, a configured OpenAI-compatible
endpoint) are also never gated here -- a third-party API has its own
rate limits and this machine's CPU/RAM is not what bounds it. Only
LOCAL provider calls (provider.is_local) are ever wrapped in
ModelRouter with this scheduler."""
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Callable, Optional


DEFAULT_MAX_CONCURRENT = 1
DEFAULT_QUEUE_TIMEOUT_SECONDS = 300.0


class QueueCancelled(Exception):
    """Raised out of acquire() when cancel_event was set while this job
    was still waiting for a slot -- it never actually started running,
    so nothing needs to be released on the local runtime side beyond
    the queue bookkeeping acquire() itself already cleans up."""


class QueueTimeout(Exception):
    """Raised out of acquire() when a job waited longer than
    queue_timeout_seconds without ever getting a free slot. This is
    what stops an abandoned or pathologically slow-draining queue from
    blocking every future request forever -- a caller that never shows
    up to release its slot cannot exist (acquire() always yields inside
    a try/finally), but a caller that legitimately takes far longer
    than expected can still starve everyone behind it without this."""


class LocalInferenceScheduler:
    """A simple counting gate: at most `max_concurrent` callers are ever
    inside the `with acquire(...):` block at once. Everyone else waits,
    visibly (via snapshot()), until a slot frees up, a queue_timeout
    elapses, or their own cancel_event is set while still waiting."""

    def __init__(self, max_concurrent: int = DEFAULT_MAX_CONCURRENT,
                 queue_timeout_seconds: float = DEFAULT_QUEUE_TIMEOUT_SECONDS):
        self.max_concurrent = max(1, int(max_concurrent))
        self.queue_timeout_seconds = float(queue_timeout_seconds)
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._running = 0
        self._queued: dict = {}  # job_id -> queued_at (time.monotonic())

    def configure(self, max_concurrent: Optional[int] = None,
                  queue_timeout_seconds: Optional[float] = None) -> None:
        """Live-reconfigures the gate (e.g. from a freshly-loaded
        ModelRegistry). Never shrinks/grows _running itself -- a job
        already inside acquire() finishes under whatever limit was in
        effect when it started; only future acquire() calls see the new
        max_concurrent. Safe to call from any thread at any time."""
        with self._lock:
            if max_concurrent is not None:
                self.max_concurrent = max(1, int(max_concurrent))
            if queue_timeout_seconds is not None:
                self.queue_timeout_seconds = float(queue_timeout_seconds)
            self._condition.notify_all()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "running": self._running,
                "queued": len(self._queued),
                "max_concurrent": self.max_concurrent,
                "queue_timeout_seconds": self.queue_timeout_seconds,
            }

    @contextmanager
    def acquire(self, job_id: Optional[str] = None, cancel_event=None,
                on_acquired: Optional[Callable[[], None]] = None):
        """Blocks the calling thread until a slot is free (or raises
        QueueCancelled/QueueTimeout instead of ever entering the `with`
        body). `on_acquired`, if given, fires exactly once, synchronously,
        the instant the slot is actually granted -- before the `with`
        body runs -- so a caller can flip a "QUEUED" status to "RUNNING"
        at the real moment it becomes true, not merely when its thread
        happened to start. The slot is always released in `finally`,
        covering success, an exception raised inside the `with` body,
        and cancellation/timeout of a job that already started running."""
        job_id = job_id or uuid.uuid4().hex
        queued_at = time.monotonic()
        acquired = False
        with self._lock:
            self._queued[job_id] = queued_at
            try:
                while self._running >= self.max_concurrent:
                    if cancel_event is not None and cancel_event.is_set():
                        raise QueueCancelled(f"job {job_id} cancelled while queued for a local inference slot")
                    elapsed = time.monotonic() - queued_at
                    remaining = self.queue_timeout_seconds - elapsed
                    if remaining <= 0:
                        raise QueueTimeout(f"job {job_id} waited {elapsed:.1f}s for a local inference slot (limit {self.queue_timeout_seconds:.0f}s)")
                    # Wake periodically even without a notify, purely to
                    # notice a cancel_event set concurrently by another
                    # thread (Stop) while we are otherwise just waiting.
                    self._condition.wait(timeout=min(0.25, max(0.01, remaining)))
                if cancel_event is not None and cancel_event.is_set():
                    raise QueueCancelled(f"job {job_id} cancelled while queued for a local inference slot")
                self._running += 1
                acquired = True
            finally:
                self._queued.pop(job_id, None)
        if on_acquired is not None:
            try:
                on_acquired()
            except Exception:
                # A broken UI-side callback must never abort real local
                # generation -- mirrors OllamaProvider.generate_stream's
                # on_delta protection for the same reason.
                pass
        try:
            yield
        finally:
            if acquired:
                with self._lock:
                    self._running -= 1
                    self._condition.notify_all()


# Process-global singleton -- see the module docstring for why every
# Chat/Research/Work request in this process must share one gate rather
# than each constructing (and therefore trusting) its own.
_default_scheduler = LocalInferenceScheduler()


def get_scheduler() -> LocalInferenceScheduler:
    return _default_scheduler
