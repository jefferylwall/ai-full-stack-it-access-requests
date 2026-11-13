"""Step 5, T21–T24: independent workers, finite waits and fenced recovery.

Provider calls use the existing saved IT-001/IT-002 replies or injected errors.
Subprocess tests exercise the worker/store APIs, not the future application CLI.
"""

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from time import monotonic, sleep
import unittest

from access_intake.providers import HeldResponse, MockProvider
from access_intake.store import Store
from test_mock_provider import offline_only
from worker_support import MOCKS, ROOT, SharedClock, config, saved_result, sources


OWNER = ("review", "System Owner", "review-request")
RISK = ("review", "Security Review", "review-risk-then-owner")
ERROR = ("error", "IT Service Desk", "retry-extraction")


class WorkerContract(unittest.TestCase):
    def setUp(self):
        guard = offline_only()
        guard.__enter__()
        self.addCleanup(guard.__exit__, None, None, None)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.clock = SharedClock(self.directory / "clock.txt")
        self.clock.set(1000.0)
        self.database = self.directory / "intake.sqlite"
        self.store = Store(self.database, clock=self.clock)
        self.addCleanup(self.store.close)
        self.config = config()
        self.sources = sources()

    def wait_until(self, predicate, message, timeout=5):
        deadline = monotonic() + timeout
        while True:
            result = predicate()
            if result:
                return result
            if monotonic() >= deadline:
                self.fail(message)
            sleep(0.01)

    def snapshot(self):
        snapshot = self.store.snapshot()
        queue = {row["document_id"]: row for row in snapshot["queue"]}
        extraction = {row["document_id"]: row for row in snapshot["extractions"]}
        self.assertEqual(len(queue), len(snapshot["queue"]))
        self.assertEqual(queue.keys(), extraction.keys())
        for name, row in queue.items():
            for field in ("attempt_id", "generation", "source_sha256", "fingerprint", "policy_sha256", "processing_state"):
                self.assertEqual(row[field], extraction[name][field], (name, field))
        return queue, extraction, {row["document_id"]: row for row in snapshot["work"]}

    def assert_complete(self, document, expected_route):
        queue, extraction, _ = self.snapshot()
        row = extraction[document]
        self.assertEqual(tuple(queue[document][key] for key in ("status", "owner", "next_action")), expected_route)
        self.assertEqual(row["processing_state"], "succeeded")
        self.assertIsNone(row["error"])
        oracle = json.loads((ROOT / "tests" / "expectations" / document.lower()).read_text(encoding="utf-8"))
        for field in ("request_id", "request_date", "kind", "requester", "team", "system", "environment", "permission", "business_purpose", "start_date", "end_date", "manager"):
            self.assertEqual(row["extraction"][field], oracle[field], field)
        for quote in oracle["supporting_quotes"]:
            self.assertIn(quote, row["extraction"]["supporting_quotes"])
        original = next(source for source in self.sources if source.document_id == document)
        self.assertEqual(row["source_sha256"], hashlib.sha256(original.raw_bytes).hexdigest())
        return row

    def assert_failure(self, document, category):
        queue, extraction, _ = self.snapshot()
        self.assertEqual(tuple(queue[document][key] for key in ("status", "owner", "next_action")), ERROR)
        row = extraction[document]
        self.assertEqual(row["processing_state"], "failed")
        self.assertIsNone(row["extraction"])
        self.assertEqual(row["error"]["category"], category)
        self.assertTrue(row["error"]["message"])
        self.assertEqual(row["error"]["attempt_id"], row["attempt_id"])
        self.assertEqual(row["error"]["source_sha256"], row["source_sha256"])
        self.assertEqual(row["error"]["retry_command"], f"python3 -m access_intake retry {document} --force")
        return row

    def background(self, pool):
        errors = []

        def run():
            try:
                pool.run()
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread, errors

    def process(self, mode, operator):
        process = subprocess.Popen(
            [sys.executable, str(ROOT / "tests" / "worker_process.py"),
             "--database", str(self.database), "--directory", str(self.directory),
             "--operator", operator, "--mode", mode],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

        def cleanup():
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)

        self.addCleanup(cleanup)
        return process

    def receipt(self, process, filename):
        path = self.directory / filename

        def ready():
            if process.poll() is not None and not path.exists():
                stdout, stderr = process.communicate(timeout=2)
                self.fail(f"Worker exited before {filename}: {stdout}\n{stderr}")
            return path.exists()

        self.wait_until(ready, f"Worker did not reach {filename}")
        return json.loads(path.read_text(encoding="utf-8"))

    def test_T21_provider_failure_does_not_block_independent_success(self):
        from access_intake.workers import WorkerPool

        self.store.admit(self.sources, self.config)
        providers = {
            "IT-001.json": MockProvider([ConnectionError("scripted unavailable")]),
            "IT-002.json": MockProvider([MOCKS / "it-002.json"]),
        }
        pool = WorkerPool(self.database, provider_factory=lambda claim: providers[claim["document_id"]], clock=self.clock)
        self.assertEqual(pool.workers, 4, "the plan starts with four configurable workers")
        pool.run()
        failed = self.assert_failure("IT-001.json", "transport_failure")
        success = self.assert_complete("IT-002.json", RISK)
        self.assertEqual([provider.call_count for provider in providers.values()], [1, 1])
        pool.run(resume=True)
        self.assertEqual(self.assert_failure("IT-001.json", "transport_failure"), failed)
        self.assertEqual(self.assert_complete("IT-002.json", RISK), success)
        self.assertEqual([provider.call_count for provider in providers.values()], [1, 1], "no hidden retries")

    def test_T22_held_calls_failures_timeouts_pacing_and_explicit_retry(self):
        from access_intake.workers import WorkerPool
        from access_intake.providers import ProviderError

        for options in (
            {"workers": 0}, {"request_timeout": 0}, {"request_timeout": float("inf")},
            {"request_timeout": 5, "lease_seconds": 6, "commit_margin": 1},
            {"commit_margin": -1}, {"pace_seconds": -1},
        ):
            with self.subTest(invalid=options):
                with self.assertRaises(ValueError):
                    WorkerPool(self.database, provider_factory=lambda claim: None, **options)

        for count in (1, 2):
            with self.subTest(workers=count):
                self.store.admit(self.sources, self.config, force=True)
                entered, release = threading.Event(), threading.Event()
                providers = {
                    "IT-001.json": MockProvider([HeldResponse(MOCKS / "it-001.json", entered=entered, release=release)]),
                    "IT-002.json": MockProvider([MOCKS / "it-002.json"]),
                }
                pool = WorkerPool(self.database, provider_factory=lambda claim: providers[claim["document_id"]], workers=count, clock=self.clock)
                thread, errors = self.background(pool)
                try:
                    self.assertTrue(entered.wait(timeout=5))
                    if count == 2:
                        self.wait_until(lambda: self.snapshot()[0]["IT-002.json"]["processing_state"] == "succeeded", "unaffected result did not become visible")
                        self.assert_complete("IT-002.json", RISK)
                    else:
                        self.assertEqual(self.snapshot()[0]["IT-002.json"]["processing_state"], "pending")
                        self.assertEqual(providers["IT-002.json"].call_count, 0)
                    work = self.snapshot()[2]["IT-001.json"]
                    self.assertEqual(work["processing_state"], "running")
                    self.assertTrue(thread.is_alive(), "held call is still in progress")
                finally:
                    release.set()
                    thread.join(timeout=5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])
                self.assert_complete("IT-001.json", OWNER)
                self.assert_complete("IT-002.json", RISK)

        for injected, category in (
            (TimeoutError("scripted timeout"), "timeout"),
            (ProviderError("rate_limit", status_code=429), "rate_limit"),
            (ProviderError("unavailable", status_code=503), "unavailable"),
        ):
            with self.subTest(category=category):
                self.store.admit(self.sources, self.config, force=True)
                providers = {"IT-001.json": MockProvider([injected]), "IT-002.json": MockProvider([MOCKS / "it-002.json"])}
                WorkerPool(self.database, provider_factory=lambda claim: providers[claim["document_id"]], clock=self.clock).run()
                self.assert_failure("IT-001.json", category)
                unaffected = self.assert_complete("IT-002.json", RISK)
                self.assertEqual(providers["IT-001.json"].call_count, 1)
                self.store.admit(self.sources[:1], self.config, force=True)
                retry = MockProvider([MOCKS / "it-001.json"])
                WorkerPool(self.database, provider_factory=lambda claim: retry, clock=self.clock).run()
                self.assert_complete("IT-001.json", OWNER)
                self.assertEqual(self.assert_complete("IT-002.json", RISK), unaffected)
                self.assertEqual(retry.call_count, 1)

        # A configured finite timeout must work even when a fixture stays held.
        self.store.admit(self.sources, self.config, force=True)
        entered, release, returned = threading.Event(), threading.Event(), threading.Event()

        class HeldUntilReleased(MockProvider):
            def complete(self, message):
                try:
                    return super().complete(message)
                finally:
                    returned.set()

        held = HeldUntilReleased([HeldResponse(MOCKS / "it-001.json", entered=entered, release=release)])
        ready = MockProvider([MOCKS / "it-002.json"])
        pool = WorkerPool(
            self.database, provider_factory=lambda claim: held if claim["document_id"] == "IT-001.json" else ready,
            workers=2, request_timeout=0.1, lease_seconds=5, commit_margin=1, clock=self.clock,
        )
        thread, errors = self.background(pool)
        try:
            self.assertTrue(entered.wait(timeout=5))
            self.wait_until(lambda: self.snapshot()[0]["IT-001.json"]["processing_state"] == "failed", "finite timeout did not produce a visible failure")
            timed_out = self.assert_failure("IT-001.json", "timeout")
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive(), "pool must return without waiting for the held reply")
            self.assert_complete("IT-002.json", RISK)
        finally:
            release.set()
            thread.join(timeout=5)
        self.assertTrue(returned.wait(timeout=5))
        self.assertEqual(errors, [])
        self.assertEqual(self.assert_failure("IT-001.json", "timeout"), timed_out)
        self.assertEqual(held.call_count, 1)

        # Pacing uses an injected sleeper/clock, not slow wall-clock test sleeps.
        self.store.admit(self.sources, self.config, force=True)
        waits = []

        def advance(seconds):
            waits.append(seconds)
            self.clock.set(self.clock() + seconds)

        providers = {name: MockProvider([MOCKS / name.lower()], clock=self.clock) for name in ("IT-001.json", "IT-002.json")}
        pool = WorkerPool(self.database, provider_factory=lambda claim: providers[claim["document_id"]], workers=1, pace_seconds=0.5, sleeper=advance, clock=self.clock)
        start = self.clock()
        pool.run()
        self.assertEqual(waits, [0.5])
        self.assertEqual(providers["IT-001.json"].calls[0]["started_at"], start)
        self.assertEqual(providers["IT-002.json"].calls[0]["started_at"], start + 0.5)

    def test_T23_killed_workers_resume_without_losing_committed_work(self):
        from access_intake.workers import WorkerPool

        for stage in ("after-claim", "before-commit"):
            with self.subTest(stage=stage):
                self.clock.set(self.clock() + 100)
                self.store.admit(self.sources[:1], self.config, force=True)
                WorkerPool(self.database, provider_factory=lambda claim: MockProvider([MOCKS / "it-001.json"]), workers=1, clock=self.clock).run()
                preserved = self.assert_complete("IT-001.json", OWNER)
                self.store.admit(self.sources[1:], self.config, force=True)
                operator = "stopped-" + stage
                process = self.process(stage, operator)
                suffix = "claim" if stage == "after-claim" else "ready"
                receipt = self.receipt(process, f"{operator}-{suffix}.json")
                old_claim = receipt["claim"]
                self.assertEqual(old_claim["document_id"], "IT-002.json")
                self.assertEqual(old_claim["lease_expires_at"], self.clock() + 40)
                self.assertEqual(self.snapshot()[2]["IT-002.json"]["processing_state"], "running")
                process.kill()
                process.communicate(timeout=5)
                self.assertEqual(self.assert_complete("IT-001.json", OWNER), preserved)
                self.clock.set(old_claim["lease_expires_at"] + 1)
                retry = MockProvider([MOCKS / "it-002.json"])
                WorkerPool(self.database, provider_factory=lambda claim: retry, workers=1, operator_id="resumed", clock=self.clock).run(resume=True)
                self.assert_complete("IT-002.json", RISK)
                self.assertEqual(self.assert_complete("IT-001.json", OWNER), preserved)
                history = self.store.history("IT-002.json")
                interrupted = next(row for row in history if row["attempt_id"] == old_claim["attempt_id"])
                self.assertEqual(interrupted["processing_state"], "interrupted")
                self.assertTrue(interrupted["worker_id"].startswith(operator))
                self.assertNotEqual(history[-1]["attempt_id"], old_claim["attempt_id"])
                self.assertEqual(history[-1]["source_sha256"], old_claim["source_sha256"])
                self.assertTrue(history[-1]["worker_id"].startswith("resumed"))
                late = receipt.get("result") or saved_result(self.sources[1], MockProvider([MOCKS / "it-002.json"]))
                self.assertFalse(self.store.finish(old_claim["attempt_id"], old_claim["token"], late))
                self.assertEqual(retry.call_count, 1)

    def test_T24_overlapping_operator_processes_fence_expired_worker(self):
        from access_intake.workers import WorkerPool

        # Importing the worker module is part of the test-first failure; actual
        # work below runs in two independently started operator subprocesses.
        self.assertTrue(callable(WorkerPool))
        old = self.process("overlap-old", "operator-old")
        receipt = self.receipt(old, "operator-old-claim.json")
        claim = receipt["claim"]
        self.assertEqual(claim["document_id"], "IT-001.json")
        self.wait_until(lambda: (self.directory / "old-entered").exists(), "old provider did not reach its hold barrier")
        self.clock.set(claim["lease_expires_at"] + 1)
        late = saved_result(self.sources[0], MockProvider([MOCKS / "it-001.json"]))
        self.assertFalse(self.store.finish(claim["attempt_id"], claim["token"], late), "expiry alone must fence a stale worker")
        new = self.process("overlap-new", "operator-new")
        self.receipt(new, "operator-new-claim.json")
        stdout, stderr = new.communicate(timeout=10)
        self.assertEqual(new.returncode, 0, stdout + stderr)
        current = self.assert_complete("IT-001.json", OWNER)
        unaffected = self.assert_complete("IT-002.json", RISK)
        self.assertIsNone(old.poll(), "old call remains held while new results are readable")
        queue, _, work = self.snapshot()
        self.assertEqual(len(queue), 2)
        self.assertEqual(work["IT-001.json"]["attempt_id"], current["attempt_id"])
        history = self.store.history("IT-001.json")
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["processing_state"], "interrupted")
        self.assertTrue(history[0]["worker_id"].startswith("operator-old"))
        self.assertTrue(history[1]["worker_id"].startswith("operator-new"))
        self.assertEqual(len(self.store.history("IT-002.json")), 1)
        (self.directory / "old-release").write_text("release", encoding="utf-8")
        stdout, stderr = old.communicate(timeout=10)
        self.assertEqual(old.returncode, 0, stdout + stderr)
        self.assertEqual(self.assert_complete("IT-001.json", OWNER), current)
        self.assertEqual(self.assert_complete("IT-002.json", RISK), unaffected)
        self.assertFalse(self.store.finish(claim["attempt_id"], claim["token"], late))
        calls = [json.loads(line) for line in (self.directory / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(sum(row["document_id"] == "IT-001.json" for row in calls), 2, "recovery can repeat the external call")
        self.assertEqual(sum(row["document_id"] == "IT-002.json" for row in calls), 1)


if __name__ == "__main__":
    unittest.main()
