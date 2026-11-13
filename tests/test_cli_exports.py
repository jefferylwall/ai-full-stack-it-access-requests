"""Step 6, T25–T27: real CLI processes and immutable, paired export generations."""

import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
from time import monotonic, sleep
import unittest

from access_intake.providers import MockProvider
from access_intake.store import Store
from access_intake.workers import WorkerPool
from test_mock_provider import offline_only
from worker_support import MOCKS, ROOT, SharedClock, config, sources


OWNER = ("review", "System Owner", "review-request")
RISK = ("review", "Security Review", "review-risk-then-owner")
ERROR = ("error", "IT Service Desk", "retry-extraction")
REQUIRED_OUTPUT = {"document_id", "source_sha256", "extraction", "status", "owner", "next_action", "error", "response_source"}


class CLIExportContract(unittest.TestCase):
    def setUp(self):
        guard = offline_only()
        guard.__enter__()
        self.addCleanup(guard.__exit__, None, None, None)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def case(self, name):
        directory = self.directory / name
        directory.mkdir()
        inputs, replies = directory / "inputs", directory / "replies"
        inputs.mkdir()
        replies.mkdir()
        for source in sources():
            (inputs / source.document_id).write_bytes(source.raw_bytes)
            filename = source.document_id.lower()
            (replies / filename).write_bytes((MOCKS / filename).read_bytes())
        clock = SharedClock(directory / "clock.txt")
        clock.set(1000.0)
        return {"directory": directory, "database": directory / "intake.sqlite", "inputs": inputs, "replies": replies, "clock": clock}

    def open_store(self, case):
        store = Store(case["database"], clock=case["clock"])
        self.addCleanup(store.close)
        return store

    def launch(self, command):
        process = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        def cleanup():
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)

        self.addCleanup(cleanup)
        return process

    def cli_process(self, case, arguments, *, mode="normal", globals=True):
        options = ["--database", str(case["database"]), "--mock-dir", str(case["replies"])] if globals else []
        return self.launch([sys.executable, str(ROOT / "tests" / "cli_process.py"), mode, str(case["directory"]), *options, *map(str, arguments)])

    def cli(self, case, arguments, *, mode="normal", expected=0, globals=True):
        process = self.cli_process(case, arguments, mode=mode, globals=globals)
        stdout, stderr = process.communicate(timeout=10)
        self.assertEqual(process.returncode, expected, stdout + stderr)
        return json.loads(stdout)

    def wait_until(self, predicate, message, timeout=5):
        deadline = monotonic() + timeout
        while True:
            found = predicate()
            if found:
                return found
            if monotonic() >= deadline:
                self.fail(message)
            sleep(0.01)

    def receipt(self, process, path):
        def ready():
            if process.poll() is not None and not path.exists():
                stdout, stderr = process.communicate(timeout=2)
                self.fail(f"Process exited before {path.name}: {stdout}\n{stderr}")
            return path.exists()

        self.wait_until(ready, f"Process did not reach {path.name}")
        return json.loads(path.read_text(encoding="utf-8"))

    def rows(self, rows):
        self.assertIsInstance(rows, list)
        result = {row["document_id"]: row for row in rows}
        self.assertEqual(len(result), len(rows))
        return result

    def assert_route(self, row, expected):
        self.assertEqual(tuple(row[key] for key in ("status", "owner", "next_action")), expected)

    def assert_pairs(self, payload):
        queue, extractions = self.rows(payload["queue"]), self.rows(payload["extractions"])
        self.assertEqual(set(queue), {"IT-001.json", "IT-002.json"})
        self.assertEqual(queue.keys(), extractions.keys())
        for name, extraction in extractions.items():
            self.assertTrue(REQUIRED_OUTPUT <= extraction.keys())
            for field in ("attempt_id", "generation", "source_sha256", "status", "owner", "next_action", "processing_state"):
                self.assertEqual(queue[name][field], extraction[field], (name, field))
            original = next(source for source in sources() if source.document_id == name)
            self.assertEqual(extraction["source_sha256"], hashlib.sha256(original.raw_bytes).hexdigest())
            self.assertEqual(extraction["response_source"], "mock")
            self.assertIn(extraction["processing_state"], ("succeeded", "failed"))
        self.assert_route(queue["IT-001.json"], OWNER)
        self.assertIsNone(extractions["IT-001.json"]["error"])
        oracle = json.loads((ROOT / "tests" / "expectations" / "it-001.json").read_text(encoding="utf-8"))
        self.assertEqual(extractions["IT-001.json"]["extraction"], oracle)
        self.assert_route(queue["IT-002.json"], ERROR)
        self.assertIsNone(extractions["IT-002.json"]["extraction"])
        self.assertEqual(extractions["IT-002.json"]["error"]["category"], "transport_failure")
        return queue, extractions

    def seed_export_data(self, case):
        store = self.open_store(case)
        store.admit(sources(), config())
        providers = {"IT-001.json": MockProvider([MOCKS / "it-001.json"]), "IT-002.json": MockProvider([ConnectionError("scripted unavailable")])}
        WorkerPool(case["database"], provider_factory=lambda claim: providers[claim["document_id"]], clock=case["clock"]).run()
        return store

    def refresh_first(self, case, store):
        store.admit(sources()[:1], config(), force=True)
        WorkerPool(case["database"], provider_factory=lambda claim: MockProvider([MOCKS / "it-001.json"]), clock=case["clock"]).run()

    def test_T25_CLI_intake_live_reads_retry_history_and_resume(self):
        from access_intake.cli import main

        self.assertTrue(callable(main))
        with self.subTest(scenario="read-during-intake"):
            case = self.case("live")
            process = self.cli_process(case, ["--workers", "2", "intake", case["inputs"]], mode="held")
            self.wait_until(lambda: (case["directory"] / "entered").exists() or process.poll() is not None, "intake did not reach its hold barrier")
            self.assertIsNone(process.poll())

            def independent_ready():
                queue = self.rows(self.cli(case, ["queue"], mode="module"))
                return queue if queue.get("IT-002.json", {}).get("processing_state") == "succeeded" else False

            queue = self.wait_until(independent_ready, "second CLI process could not read independent completed work")
            self.assert_route(queue["IT-002.json"], RISK)
            self.assertEqual(queue["IT-001.json"]["processing_state"], "running")
            self.assertIsNone(queue["IT-001.json"]["status"])
            work = self.rows(self.cli(case, ["work"], mode="module"))
            self.assertEqual(work["IT-001.json"]["attempt_id"], queue["IT-001.json"]["attempt_id"])
            (case["directory"] / "release").write_text("release", encoding="utf-8")
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, stdout + stderr)
            final = self.rows(self.cli(case, ["queue"], mode="module"))
            self.assert_route(final["IT-001.json"], OWNER)
            self.assert_route(final["IT-002.json"], RISK)
            history = self.cli(case, ["history", "IT-001.json"], mode="module")
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["attempt_id"], final["IT-001.json"]["attempt_id"])

        with self.subTest(scenario="execute-printed-retry"):
            case = self.case("retry with spaces")
            self.cli(case, ["intake", case["inputs"]], mode="fail-once", expected=1)
            queue = self.rows(self.cli(case, ["queue"]))
            self.assert_route(queue["IT-001.json"], ERROR)
            self.assert_route(queue["IT-002.json"], RISK)
            command = shlex.split(queue["IT-001.json"]["error"]["retry_command"])
            self.assertEqual(command[:3], ["python3", "-m", "access_intake"])
            self.assertIn("--database", command)
            self.assertEqual(command[command.index("--database") + 1], str(case["database"]))
            self.assertEqual(command[-3:], ["retry", "IT-001.json", "--force"])
            # A printed retry is self-contained: use captured source/fixture bytes.
            for path in (*case["inputs"].glob("*.json"), *case["replies"].glob("*.json")):
                path.unlink()
            self.cli(case, command[3:], globals=False)
            final = self.rows(self.cli(case, ["queue"]))
            self.assert_route(final["IT-001.json"], OWNER)
            self.assertEqual(final["IT-002.json"], queue["IT-002.json"])
            history = self.cli(case, ["history", "IT-001.json"])
            self.assertEqual([row["processing_state"] for row in history], ["failed", "succeeded"])
            self.assertNotEqual(history[0]["attempt_id"], history[1]["attempt_id"])

        with self.subTest(scenario="resume-terminated-intake"):
            case = self.case("resume")
            self.cli(case, ["intake", case["inputs"] / "IT-001.json"])
            preserved = self.rows(self.cli(case, ["queue"]))["IT-001.json"]
            process = self.cli_process(case, ["intake", case["inputs"]], mode="stop-after-claim")
            claim = self.receipt(process, case["directory"] / "claim.json")
            process.kill()
            process.communicate(timeout=5)
            work = self.rows(self.cli(case, ["work"]))
            self.assertEqual(work["IT-002.json"]["processing_state"], "running")
            self.assertEqual(work["IT-001.json"]["processing_state"], "succeeded")
            case["clock"].set(claim["lease_expires_at"] + 1)
            self.cli(case, ["resume"])
            final = self.rows(self.cli(case, ["queue"]))
            self.assertEqual(final["IT-001.json"], preserved)
            self.assert_route(final["IT-002.json"], RISK)
            history = self.cli(case, ["history", "IT-002.json"])
            self.assertEqual([row["processing_state"] for row in history], ["interrupted", "succeeded"])

    def test_T26_export_fields_coherent_pair_and_no_unfinished_final_status(self):
        from access_intake.exports import export_snapshot, pin_export

        case = self.case("export-content")
        store = self.seed_export_data(case)
        destination = case["directory"] / "deliverables"
        history = {source.document_id: store.history(source.document_id) for source in sources()}
        self.cli(case, ["export", "--output", destination])
        self.assertTrue(destination.is_symlink())
        pinned = pin_export(destination)
        self.assertTrue(pinned.generation.is_dir())
        self.assertEqual(destination.resolve(strict=True), pinned.generation)
        raw = (pinned.generation / "extractions.jsonl").read_bytes()
        self.assertTrue(raw.endswith(b"\n"))
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(len(raw.decode("utf-8").splitlines()), 2)
        self.assertTrue(all(raw.decode("utf-8").splitlines()))
        self.assert_pairs(pinned.read())
        paired_read = self.cli(case, ["export", "--read", destination], mode="module")
        self.assertEqual(Path(paired_read["generation"]), pinned.generation)
        self.assert_pairs(paired_read)
        for source in sources():
            self.assertEqual(store.history(source.document_id), history[source.document_id])
        # Final exports cannot fabricate a review status for unfinished work.
        store.admit(sources()[:1], config(), force=True)
        with self.assertRaisesRegex(ValueError, "[Uu]nfinished|[Pp]ending"):
            export_snapshot(store, destination)
        self.assertEqual(destination.resolve(strict=True), pinned.generation)
        self.assertEqual((pinned.generation / "extractions.jsonl").read_bytes(), raw)

    def test_T27_crashes_around_publication_and_reader_pins_one_generation(self):
        from access_intake.exports import export_snapshot, pin_export

        case = self.case("publication")
        store = self.seed_export_data(case)
        destination = case["directory"] / "deliverables"
        export_snapshot(store, destination)
        for stage in ("before", "after"):
            with self.subTest(stage=stage):
                control = case["directory"] / stage
                control.mkdir()
                previous = pin_export(destination)
                previous_payload = previous.read()
                previous_bytes = {name: (previous.generation / name).read_bytes() for name in ("queue.json", "extractions.jsonl")}
                reader = self.launch([
                    sys.executable, str(ROOT / "tests" / "export_process.py"), "--mode", "reader",
                    "--destination", str(destination), "--directory", str(control),
                ])
                receipt = self.receipt(reader, control / "reader-ready.json")
                self.assertEqual(Path(receipt["generation"]), previous.generation)
                self.refresh_first(case, store)
                history = {source.document_id: store.history(source.document_id) for source in sources()}
                publisher = self.launch([
                    sys.executable, str(ROOT / "tests" / "export_process.py"), "--mode", "publisher",
                    "--database", str(case["database"]), "--destination", str(destination),
                    "--directory", str(control), "--stage", stage,
                ])
                ready = self.receipt(publisher, control / "publisher-ready.json")
                new_generation = Path(ready["generation"])
                self.assertNotEqual(new_generation, previous.generation)
                self.assertTrue((new_generation / "queue.json").is_file())
                self.assertTrue((new_generation / "extractions.jsonl").is_file())
                publisher.kill()
                publisher.communicate(timeout=5)
                current = pin_export(destination)
                self.assertEqual(current.generation, previous.generation if stage == "before" else new_generation)
                self.assert_pairs(current.read())
                if stage == "after":
                    old_rows = self.rows(previous_payload["extractions"])
                    new_rows = self.rows(current.read()["extractions"])
                    self.assertNotEqual(old_rows["IT-001.json"]["attempt_id"], new_rows["IT-001.json"]["attempt_id"])
                (control / "reader-release").write_text("release", encoding="utf-8")
                stdout, stderr = reader.communicate(timeout=10)
                self.assertEqual(reader.returncode, 0, stdout + stderr)
                read_across_switch = json.loads((control / "reader-result.json").read_text(encoding="utf-8"))
                self.assertEqual(Path(read_across_switch["generation"]), previous.generation)
                self.assert_pairs(read_across_switch)
                self.assertEqual(read_across_switch["queue"], previous_payload["queue"])
                self.assertEqual(read_across_switch["extractions"], previous_payload["extractions"])
                for name, raw in previous_bytes.items():
                    self.assertEqual((previous.generation / name).read_bytes(), raw)
                # Publishing again recovers normally; orphaned generations do not
                # become current by directory enumeration and exports do not edit history.
                export_snapshot(store, destination)
                self.assert_pairs(pin_export(destination).read())
                for source in sources():
                    self.assertEqual(store.history(source.document_id), history[source.document_id])


if __name__ == "__main__":
    unittest.main()
