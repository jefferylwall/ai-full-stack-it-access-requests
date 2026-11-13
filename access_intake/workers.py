"""Bounded independent workers with explicit recovery and finite provider waits."""

import math
from pathlib import Path
from threading import BoundedSemaphore, Event, Lock, Thread
from time import sleep, time
from typing import Any, Callable
from uuid import uuid4

from .extraction import extract
from .providers import Provider
from .routing import route
from .store import Store


class _TimedProvider:
    """Wait finitely while keeping a blocked invocation inside the pool's bound.

    Python cannot kill a blocked thread. A timed-out invocation keeps its slot
    until the underlying call returns, and can never write a result to SQLite.
    The live transport must also enforce a finite I/O timeout. Pending work stays
    recorded if all slots are held by calls that have not returned.
    """

    def __init__(self, factory, claim, timeout, slots):
        self.factory = factory
        self.claim = claim
        self.timeout = timeout
        self.slots = slots
        self.started = False

    def complete(self, message):
        ready = Event()
        outcome = {}

        def invoke():
            try:
                provider = self.factory(self.claim)
                outcome["envelope"] = provider.complete(message)
            except BaseException as error:
                outcome["error"] = error
            finally:
                self.slots.release()
                ready.set()

        thread = Thread(target=invoke, daemon=True, name="access-provider-" + self.claim["attempt_id"])
        thread.start()
        self.started = True
        if not ready.wait(self.timeout):
            raise TimeoutError("Provider request exceeded its configured timeout")
        if "error" in outcome:
            raise outcome["error"]
        return outcome["envelope"]


class WorkerPool:
    def __init__(
        self, database: str | Path, *, provider_factory: Callable[[dict[str, Any]], Provider],
        workers: int = 4, request_timeout: float = 30, lease_seconds: float = 40,
        commit_margin: float = 2, pace_seconds: float = 0, operator_id: str | None = None,
        clock: Callable[[], float] = time, sleeper: Callable[[float], None] = sleep,
        after_claim: Callable[[dict[str, Any]], None] | None = None,
        before_commit: Callable[[dict[str, Any], dict[str, Any]], None] | None = None,
    ) -> None:
        if not isinstance(workers, int) or isinstance(workers, bool) or workers < 1:
            raise ValueError("Worker count must be a positive integer")
        if not math.isfinite(request_timeout) or request_timeout <= 0:
            raise ValueError("Request timeout must be finite and positive")
        if not math.isfinite(commit_margin) or commit_margin < 0:
            raise ValueError("Commit margin must be finite and nonnegative")
        if not math.isfinite(lease_seconds) or lease_seconds <= request_timeout + commit_margin:
            raise ValueError("Lease must exceed request timeout plus commit margin")
        if not math.isfinite(pace_seconds) or pace_seconds < 0:
            raise ValueError("Pacing must be finite and nonnegative")
        self.database = database
        self.provider_factory = provider_factory
        self.workers = workers
        self.request_timeout = request_timeout
        self.lease_seconds = lease_seconds
        self.pace_seconds = pace_seconds
        self.operator_id = operator_id or uuid4().hex
        self.clock = clock
        self.sleeper = sleeper
        self.after_claim = after_claim
        self.before_commit = before_commit
        self._slots = BoundedSemaphore(workers)
        self._pacing_lock = Lock()
        self._run_lock = Lock()
        self._last_start = None

    def _pace(self):
        with self._pacing_lock:
            now = self.clock()
            if self._last_start is not None:
                delay = max(0, self._last_start + self.pace_seconds - now)
                if delay:
                    self.sleeper(delay)
            self._last_start = self.clock()

    def _work(self, index, stop):
        store = Store(self.database, clock=self.clock)
        try:
            while not stop.is_set() and store.has_pending():
                # Do not accumulate abandoned provider threads after timeouts.
                if not self._slots.acquire(blocking=False):
                    return
                timed = None
                try:
                    # Pace before claiming so the wait does not consume the lease.
                    self._pace()
                    claim = store.claim(worker_id=f"{self.operator_id}:{index}", lease_seconds=self.lease_seconds)
                    if claim is None:
                        return
                    if self.after_claim is not None:
                        self.after_claim(claim)
                    source = store.source(claim["attempt_id"])
                    config = store.configuration(claim["attempt_id"])
                    timed = _TimedProvider(self.provider_factory, claim, self.request_timeout, self._slots)
                    result = extract(source, timed, instructions=config.settings["prompt"], schema=config.settings["schema"])
                    if result["error"] is None:
                        result.update(route(source, result["extraction"], policy_bytes=config.policy_bytes))
                    provider_name = config.settings["provider"]
                    result.setdefault("response_source", "mock" if provider_name.startswith("mock") else f"{provider_name}/{config.settings['model']}")
                    if self.before_commit is not None:
                        self.before_commit(claim, result)
                    store.finish(claim["attempt_id"], claim["token"], result)
                finally:
                    if timed is None or not timed.started:
                        self._slots.release()
        finally:
            store.close()

    def run(self, *, resume: bool = False) -> None:
        """Drain claimable pending work; recovery never silently retries failures."""
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("This worker pool is already running")
        try:
            # Initialize/migrate once before the worker connections are opened.
            store = Store(self.database, clock=self.clock)
            try:
                if resume:
                    store.recover()
            finally:
                store.close()
            stop = Event()
            errors = []
            error_lock = Lock()

            def work(index):
                try:
                    self._work(index, stop)
                except BaseException as error:
                    with error_lock:
                        errors.append(error)
                    stop.set()

            threads = [Thread(target=work, args=(index,), daemon=True, name=f"access-worker-{index}") for index in range(self.workers)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            if errors:
                raise errors[0]
        finally:
            self._run_lock.release()
