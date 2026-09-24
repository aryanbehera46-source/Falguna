"""Tests for Falguna Local AI Independence V1.1 Pass D: the shared local-
inference concurrency gate (falguna/scheduler.py) and its wiring into
ModelRouter.__call__/stream (falguna/model_router.py). Complements the
live, real-Ollama queueing/cancellation evidence captured separately
against the actual running Mac stack for the closure report."""
import threading
import time
import unittest

from falguna.model_router import ModelRouter
from falguna.providers import HealthState, ModelInfo, ModelProvider, ProviderHealth
from falguna.scheduler import LocalInferenceScheduler, QueueCancelled, QueueTimeout, get_scheduler


class SchedulerUnitTests(unittest.TestCase):
    def test_a_single_caller_acquires_immediately_and_releases_on_exit(self):
        sched = LocalInferenceScheduler(max_concurrent=1)
        self.assertEqual(sched.snapshot(), {"running": 0, "queued": 0, "max_concurrent": 1, "queue_timeout_seconds": 300.0})
        with sched.acquire():
            self.assertEqual(sched.snapshot()["running"], 1)
        self.assertEqual(sched.snapshot()["running"], 0)

    def test_a_second_caller_genuinely_waits_for_the_first_to_release(self):
        sched = LocalInferenceScheduler(max_concurrent=1, queue_timeout_seconds=10)
        first_holds = threading.Event()
        release_first = threading.Event()
        second_acquired_at = []

        def holder():
            with sched.acquire():
                first_holds.set()
                release_first.wait(timeout=5)

        def waiter():
            first_holds.wait(timeout=5)
            with sched.acquire():
                second_acquired_at.append(time.monotonic())

        t1 = threading.Thread(target=holder)
        t2 = threading.Thread(target=waiter)
        t1.start()
        first_holds.wait(timeout=5)
        t2.start()
        time.sleep(0.2)
        # The second caller must still be queued -- mutual exclusion, not
        # two concurrent "running" slots under max_concurrent=1.
        self.assertEqual(sched.snapshot()["running"], 1)
        self.assertEqual(sched.snapshot()["queued"], 1)
        release_first.set()
        t1.join(timeout=5)
        t2.join(timeout=5)
        self.assertTrue(second_acquired_at)  # it did eventually get in
        self.assertEqual(sched.snapshot(), {"running": 0, "queued": 0, "max_concurrent": 1, "queue_timeout_seconds": 10.0})

    def test_cancel_event_set_while_queued_raises_and_never_enters_the_body(self):
        sched = LocalInferenceScheduler(max_concurrent=1, queue_timeout_seconds=10)
        cancel_event = threading.Event()
        entered_body = []

        with sched.acquire():  # occupy the only slot
            cancel_event.set()  # already cancelled before the second caller even starts waiting
            with self.assertRaises(QueueCancelled):
                with sched.acquire(cancel_event=cancel_event):
                    entered_body.append(True)  # must never run
        self.assertEqual(entered_body, [])
        self.assertEqual(sched.snapshot()["queued"], 0)  # queue bookkeeping cleaned up, not leaked

    def test_queue_timeout_fires_when_the_slot_never_frees_in_time(self):
        sched = LocalInferenceScheduler(max_concurrent=1, queue_timeout_seconds=0.3)
        with sched.acquire():
            t0 = time.monotonic()
            with self.assertRaises(QueueTimeout):
                with sched.acquire():
                    pass
            elapsed = time.monotonic() - t0
        self.assertGreaterEqual(elapsed, 0.25)
        self.assertLess(elapsed, 2.0)  # bounded, not silently hanging

    def test_on_acquired_fires_exactly_once_after_the_slot_is_granted(self):
        sched = LocalInferenceScheduler(max_concurrent=1)
        calls = []
        with sched.acquire(on_acquired=lambda: calls.append(sched.snapshot()["running"])):
            pass
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], 1)  # running was already incremented by the time the callback fired

    def test_a_broken_on_acquired_callback_never_blocks_real_work(self):
        sched = LocalInferenceScheduler(max_concurrent=1)
        entered = []
        with sched.acquire(on_acquired=lambda: (_ for _ in ()).throw(RuntimeError("boom"))):
            entered.append(True)
        self.assertEqual(entered, [True])

    def test_an_exception_inside_the_body_still_releases_the_slot(self):
        sched = LocalInferenceScheduler(max_concurrent=1)
        with self.assertRaises(ValueError):
            with sched.acquire():
                raise ValueError("job blew up")
        self.assertEqual(sched.snapshot()["running"], 0)
        with sched.acquire():  # would hang forever if the slot leaked
            pass

    def test_configure_changes_max_concurrent_for_future_acquires(self):
        sched = LocalInferenceScheduler(max_concurrent=1)
        sched.configure(max_concurrent=2, queue_timeout_seconds=45)
        snap = sched.snapshot()
        self.assertEqual(snap["max_concurrent"], 2)
        self.assertEqual(snap["queue_timeout_seconds"], 45.0)
        with sched.acquire():
            with sched.acquire():  # both fit now -- would deadlock at max_concurrent=1
                self.assertEqual(sched.snapshot()["running"], 2)

    def test_get_scheduler_returns_the_same_process_global_instance(self):
        self.assertIs(get_scheduler(), get_scheduler())


class _LocalGenProvider(ModelProvider):
    """A local provider whose generate() blocks until released -- for
    proving ModelRouter.__call__ actually serializes LOCAL calls through
    the shared scheduler, not merely that the scheduler class works in
    isolation."""

    def __init__(self, provider_id="local-fake", release: threading.Event = None):
        self.provider_id = provider_id
        self.display_name = "Local Fake"
        self.is_local = True
        self._release = release or threading.Event()
        self.entered_at = []

    def list_models(self):
        return [ModelInfo(self.provider_id, "m", "m", True)]

    def health_check(self):
        return ProviderHealth(HealthState.HEALTHY, "fake")

    def generate(self, model_id, payload, timeout_seconds):
        self.entered_at.append(time.monotonic())
        self._release.wait(timeout=5)
        return {"choices": [{"message": {"content": "{}"}}], "usage": {}, "_falguna_provider": self.provider_id, "_falguna_cost_usd": 0.0}


class _ExternalGenProvider(ModelProvider):
    """An EXTERNAL (is_local=False) provider -- must never be gated by
    the local-inference scheduler at all (see scheduler.py's module
    docstring: only this machine's own local providers are gated)."""

    def __init__(self, provider_id="ext-fake"):
        self.provider_id = provider_id
        self.display_name = "External Fake"
        self.is_local = False
        self.entered_at = []

    def list_models(self):
        return [ModelInfo(self.provider_id, "m", "m", False)]

    def health_check(self):
        return ProviderHealth(HealthState.HEALTHY, "fake")

    def generate(self, model_id, payload, timeout_seconds):
        self.entered_at.append(time.monotonic())
        return {"choices": [{"message": {"content": "{}"}}], "usage": {}, "_falguna_provider": self.provider_id, "_falguna_cost_usd": 0.0}


class ModelRouterSchedulerIntegrationTests(unittest.TestCase):
    def setUp(self):
        # Every test in this class gets its own scheduler size so tests
        # never interfere with each other or with the process-global
        # default's state left over from other test modules -- but
        # ModelRouter.__call__ always calls get_scheduler() (the real
        # process-global one), so we configure THAT to a known size and
        # restore it afterwards rather than trying to inject a fake.
        self._sched = get_scheduler()
        self._orig = (self._sched.max_concurrent, self._sched.queue_timeout_seconds)
        self._sched.configure(max_concurrent=1, queue_timeout_seconds=5)

    def tearDown(self):
        self._sched.configure(max_concurrent=self._orig[0], queue_timeout_seconds=self._orig[1])

    def test_two_local_calls_genuinely_serialize_through_the_shared_scheduler(self):
        release = threading.Event()
        provider = _LocalGenProvider(release=release)
        router = ModelRouter([provider], privacy_mode="HYBRID")
        first_running = threading.Event()

        def call_one():
            first_running.set()
            router({"model": None}, {"messages": []}, timeout_seconds=5)

        t1 = threading.Thread(target=call_one)
        t1.start()
        first_running.wait(timeout=5)
        time.sleep(0.15)  # let call_one actually enter generate() and block

        second_start = time.monotonic()
        result_holder = {}

        def call_two():
            result_holder["out"] = router({"model": None}, {"messages": []}, timeout_seconds=5)

        t2 = threading.Thread(target=call_two)
        t2.start()
        time.sleep(0.2)
        # call_two must still be waiting on the scheduler -- provider.generate
        # was only ever entered once so far.
        self.assertEqual(len(provider.entered_at), 1)
        release.set()
        t1.join(timeout=5)
        t2.join(timeout=5)
        self.assertEqual(len(provider.entered_at), 2)
        self.assertGreater(provider.entered_at[1], provider.entered_at[0])

    def test_a_queued_local_call_is_released_by_its_own_cancel_event(self):
        release = threading.Event()
        provider = _LocalGenProvider(release=release)
        router = ModelRouter([provider], privacy_mode="HYBRID")
        first_running = threading.Event()

        def call_one():
            first_running.set()
            router({"model": None}, {"messages": []}, timeout_seconds=5)

        t1 = threading.Thread(target=call_one)
        t1.start()
        first_running.wait(timeout=5)
        time.sleep(0.15)

        cancel_event = threading.Event()
        outcome = {}

        def call_two():
            try:
                router({"model": None}, {"messages": []}, timeout_seconds=5, cancel_event=cancel_event)
            except Exception as exc:
                outcome["error"] = exc

        t2 = threading.Thread(target=call_two)
        t2.start()
        time.sleep(0.15)
        cancel_event.set()
        t2.join(timeout=5)
        self.assertIn("error", outcome)
        self.assertEqual(outcome["error"].category, "TRANSPORT_FAILURE")
        release.set()
        t1.join(timeout=5)

    def test_external_provider_calls_are_never_gated_by_the_local_scheduler(self):
        # max_concurrent=1 (see setUp), but two EXTERNAL calls must both
        # proceed without ever waiting on each other through this gate --
        # a slow local generation must never delay an external API call
        # that has nothing to do with this machine's own CPU/RAM.
        provider = _ExternalGenProvider()
        router = ModelRouter([provider], privacy_mode="HYBRID")
        results = []

        def call():
            results.append(router({"model": None}, {"messages": []}, timeout_seconds=5))

        threads = [threading.Thread(target=call) for _ in range(3)]
        start = time.monotonic()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        elapsed = time.monotonic() - start
        self.assertEqual(len(provider.entered_at), 3)
        self.assertLess(elapsed, 2.0)  # no serialization delay introduced


if __name__ == "__main__":
    unittest.main()
