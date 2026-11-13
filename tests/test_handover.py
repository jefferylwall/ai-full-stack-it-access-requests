"""Step 9, T34–T35: offline handover and measured synthetic workloads.

Wheel preparation is setup outside the tests. No test downloads dependencies,
opens a socket, reads API_KEY or invokes a real model.
"""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import venv

from access_intake.extraction import INSTRUCTIONS, SCHEMA
from access_intake.providers import MockProvider, SavedResponse
from access_intake.store import Store
from test_mock_provider import offline_only


ROOT = Path(__file__).resolve().parents[1]
MOCKS = ROOT / "tests" / "mock_responses"
WHEELS = Path(os.environ.get("ACCESS_INTAKE_WHEEL_CACHE", str(ROOT / ".wheel-cache"))).resolve()
PARAPHRASE = (
    "Please log IT-021 with request date 2026-10-22. I am Maya Chen on the Analytics team. "
    "For a new access window, let me read Atlas in staging beginning 2026-10-23 and ending "
    "2026-11-06. Priya Shah is my manager. I need to check the weekly conversion dashboard "
    "against the changed event definitions before our next reporting cycle."
)
SCALARS = ("request_id", "request_date", "kind", "requester", "team", "system", "environment", "permission", "business_purpose", "start_date", "end_date", "manager")


class HandoverContract(unittest.TestCase):
    def setUp(self):
        guard = offline_only()
        guard.__enter__()
        self.addCleanup(guard.__exit__, None, None, None)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def artifacts(self, root):
        destination = root / "deliverables"
        self.assertTrue(destination.is_symlink())
        generation = destination.resolve(strict=True)
        for name in ("extractions.jsonl", "queue.json", "accuracy.md", "request-evidence.json", "verification.md", "accuracy-report.json", "workload-observations.json"):
            self.assertTrue((generation / name).is_file(), name)
        rows = [json.loads(line) for line in (generation / "extractions.jsonl").read_text(encoding="utf-8").splitlines()]
        queue = json.loads((generation / "queue.json").read_text(encoding="utf-8"))
        self.assertEqual(len(rows), 21)
        self.assertEqual(len(queue), 21)
        self.assertEqual({row["document_id"] for row in rows}, {f"IT-{number:03}.json" for number in range(1, 22)})
        for row in rows:
            self.assertEqual(row["response_source"], "mock")
            self.assertEqual(row["processing_state"], "succeeded")
        score = json.loads((generation / "accuracy-report.json").read_text(encoding="utf-8"))
        self.assertEqual(score["observation"], "mock_fixture_verification")
        self.assertEqual(score["scalar"]["total"], 252)
        self.assertEqual(score["scalar"]["matches"], 252)
        self.assertFalse(score["has_errors"])
        request = json.loads((generation / "request-evidence.json").read_text(encoding="utf-8"))
        self.assertEqual(request["observation"], "offline_request_preview")
        self.assertEqual(request["request"]["headers"]["Authorization"], "Bearer [REDACTED]")
        self.assertEqual(request["request"]["body"]["model"], "gpt-6-luna")
        self.assertEqual(request["request"]["body"]["text"]["format"]["schema"], SCHEMA)
        self.assertIn("interruption", request["response_handling"])
        workloads = json.loads((generation / "workload-observations.json").read_text(encoding="utf-8"))
        self.assertEqual(workloads["observation"], "mock_workload_verification")
        self.assertEqual([run["documents"] for run in workloads["runs"]], [400, 2000])
        for run in workloads["runs"]:
            self.assertGreater(run["elapsed_seconds"], 0)
            self.assertLessEqual(run["peak_in_flight"], run["configuration"]["workers"])
            self.assertEqual(run["final"], {"succeeded": run["documents"], "failed": 0})
        accuracy = (generation / "accuracy.md").read_text(encoding="utf-8").lower()
        verification = (generation / "verification.md").read_text(encoding="utf-8").lower()
        for word in ("expected", "252", "mock", "literal", "null", "semantic"):
            self.assertIn(word, accuracy)
        for word in ("observed", "intended", "single-host", "interruption", "human", "risk", "owner", "limits"):
            self.assertIn(word, verification)
        self.assertEqual((root / "config" / "extraction-prompt.txt").read_text(encoding="utf-8"), INSTRUCTIONS)
        self.assertIn("Security Review", (root / "README.md").read_text(encoding="utf-8"))
        for filename in ("requirements.lock", "requirements-bootstrap.lock"):
            for line in (root / filename).read_text(encoding="utf-8").splitlines():
                if line.strip() and not line.startswith("#"):
                    self.assertRegex(line, r"^[A-Za-z0-9_.-]+==[^\s;]+$")
        return workloads

    def clean_command(self, checkout, python, arguments, *, mode="module", expected=0):
        # A deliberately narrow environment never reads or forwards credentials.
        environment = {
            "PATH": str(python.parent) + os.pathsep + os.defpath,
            "LANG": "C.UTF-8", "PROVIDER": "mock",
            "ACCESS_INTAKE_HANDOVER_CHILD": "1",
            "ACCESS_INTAKE_WHEEL_CACHE": str(WHEELS),
        }
        result = subprocess.run(
            [str(python), str(checkout / "tests" / "offline_command.py"), mode, *map(str, arguments)],
            cwd=checkout, env=environment, capture_output=True, text=True, timeout=300,
        )
        self.assertEqual(result.returncode, expected, result.stdout[-5000:] + result.stderr[-5000:])
        return result

    def test_T34_clean_offline_setup_commands_and_submission_artifacts(self):
        from access_intake.handover import build_submission

        self.assertTrue(callable(build_submission))
        workloads = self.artifacts(ROOT)
        # A nested full-suite run checks this artifact contract too. Only the
        # second recursive clone/bootstrap is avoided, not the artifact checks.
        if os.environ.get("ACCESS_INTAKE_HANDOVER_CHILD") == "1":
            self.assertNotEqual(sys.prefix, sys.base_prefix)
            return
        self.assertTrue(WHEELS.is_dir(), "Prepare the documented wheel cache outside tests before running T34")
        self.assertTrue(list(WHEELS.glob("pip-*.whl")), "The prepared cache includes the pinned pip bootstrap wheel")
        checkout = self.directory / "clean-checkout"
        checkout.mkdir()
        for name in ("access_intake", "accuracy", "config", "docs", "materials", "planning", "tests", "tools"):
            shutil.copytree(ROOT / name, checkout / name, ignore=shutil.ignore_patterns("__pycache__"))
        for name in ("README.md", "requirements.txt", "requirements.lock", "requirements-bootstrap.lock", "snapshot.schema.json"):
            shutil.copy2(ROOT / name, checkout / name)
        self.assertFalse((checkout / ".git").exists())
        self.assertFalse((checkout / ".aws").exists())
        validation = checkout / "planning" / "validation.md"
        original_validation = validation.read_bytes() if validation.exists() else None
        # --without-pip works on the host even when ensurepip is not installed.
        venv.EnvBuilder(with_pip=False).create(checkout / ".venv")
        python = checkout / ".venv" / "bin" / "python"
        self.clean_command(checkout, python, [checkout / "tools" / "install_locked.py", "--wheel-cache", WHEELS], mode="script")
        self.clean_command(checkout, python, ["pip", "check"])
        self.clean_command(checkout, python, ["access_intake", "intake", "materials/requests", "tests/inputs/IT-021.json"])
        queue = json.loads(self.clean_command(checkout, python, ["access_intake", "queue"]).stdout)
        self.assertEqual(len(queue), 21)
        self.clean_command(checkout, python, ["access_intake", "work"])
        self.clean_command(checkout, python, ["access_intake", "history", "IT-001.json"])
        self.clean_command(checkout, python, ["access_intake", "resume"])
        self.clean_command(checkout, python, ["access_intake", "retry", "IT-001.json", "--force"])
        self.clean_command(checkout, python, ["access_intake", "export", "--output", "deliverables"])
        self.clean_command(checkout, python, ["access_intake", "accuracy", "--input", "deliverables/extractions.jsonl", "--expectations", "accuracy/expectations.json"])
        observation_path = checkout / "observed-workloads.json"
        observation_path.write_text(json.dumps(workloads), encoding="utf-8")
        self.clean_command(checkout, python, ["access_intake.handover", "--database", ".access-intake.sqlite", "--output", "deliverables", "--workloads", observation_path])
        self.artifacts(checkout)
        # Exercise the documented full test command inside the clean environment.
        run = self.clean_command(checkout, python, ["unittest", "discover", "-s", "tests", "-v"])
        self.assertIn("OK", run.stderr)
        self.assertEqual(validation.read_bytes() if validation.exists() else None, original_validation, "the developer must not fill human validation verdicts")

    def test_T35_400_and_2000_documents_preserve_identity_progress_bounds_and_recovery(self):
        from access_intake.workload import run_workload

        template = (MOCKS / "load-template.json").read_text(encoding="utf-8")
        self.assertEqual(template.count("{{request_id}}"), 2)
        expected = {
            "request_date": "2026-10-22", "kind": "new", "requester": "Maya Chen", "team": "Analytics",
            "system": "Atlas", "environment": "staging", "permission": "read",
            "business_purpose": "I need to check the weekly conversion dashboard against the changed event definitions before our next reporting cycle.",
            "start_date": "2026-10-23", "end_date": "2026-11-06", "manager": "Priya Shah",
        }
        for size in (400, 2000):
            with self.subTest(documents=size):
                directory = self.directory / str(size)
                entered, release, partial = threading.Event(), threading.Event(), threading.Event()
                lock = threading.Lock()
                calls, results, failures = {}, [], []
                measured = {"active": 0, "peak": 0}

                def factory(claim):
                    document = claim["document_id"]
                    identifier = Path(document).stem
                    reply = template.replace("{{request_id}}", identifier).encode("utf-8")

                    class Scripted(MockProvider):
                        def complete(self, message):
                            with lock:
                                calls[document] = calls.get(document, 0) + 1
                                ordinal = calls[document]
                                measured["active"] += 1
                                measured["peak"] = max(measured["peak"], measured["active"])
                            try:
                                prompt = json.loads(message)
                                self_message = json.loads(prompt[1]["content"])["message"]
                                if self_message != PARAPHRASE.replace("IT-021", identifier):
                                    raise AssertionError("workload message differs from its authored source")
                                if document == "IT-1001.json":
                                    entered.set()
                                    if not release.wait(timeout=30):
                                        raise TimeoutError("test did not release held workload reply")
                                if document == "IT-1002.json" and ordinal == 1:
                                    raise ConnectionError("scripted first workload failure")
                                return super().complete(message)
                            finally:
                                with lock:
                                    measured["active"] -= 1

                    return Scripted([SavedResponse("load-template.json", reply)])

                def observe(snapshot):
                    rows = {row["document_id"]: row for row in snapshot["work"]}
                    if rows.get("IT-1001.json", {}).get("processing_state") == "running" and any(row["processing_state"] == "succeeded" for row in rows.values()):
                        partial.set()

                def run():
                    try:
                        results.append(run_workload(size, directory, workers=4, provider_factory=factory, observer=observe))
                    except BaseException as error:
                        failures.append(error)

                thread = threading.Thread(target=run, daemon=True)
                thread.start()
                reader = None
                try:
                    self.assertTrue(entered.wait(timeout=20), failures)
                    self.assertTrue(partial.wait(timeout=20), "independent results were not exposed before release")
                    reader = Store(directory / "workload.sqlite")
                    snapshot = reader.snapshot()
                    rows = {row["document_id"]: row for row in snapshot["work"]}
                    self.assertEqual(len(rows), size)
                    self.assertEqual(rows["IT-1001.json"]["processing_state"], "running")
                    self.assertTrue(any(row["processing_state"] == "succeeded" for row in rows.values()))
                finally:
                    release.set()
                    thread.join(timeout=120)
                    if reader is not None:
                        reader.close()
                self.assertFalse(thread.is_alive(), "workload did not terminate after release")
                self.assertEqual(failures, [])
                self.assertEqual(len(results), 1)
                report = results[0]
                self.assertEqual(report["observation"], "mock_workload_verification")
                self.assertEqual(report["documents"], size)
                self.assertEqual(report["initial"], {"succeeded": size - 1, "failed": 1})
                self.assertEqual(report["final"], {"succeeded": size, "failed": 0})
                self.assertEqual(report["provider_calls"], size + 1)
                self.assertTrue(report["incremental_visible"])
                self.assertGreater(report["elapsed_seconds"], 0)
                self.assertGreaterEqual(report["peak_in_flight"], 2)
                self.assertLessEqual(report["peak_in_flight"], 4)
                self.assertLessEqual(measured["peak"], 4)
                self.assertGreaterEqual(measured["peak"], 2)
                self.assertEqual(measured["active"], 0)
                self.assertEqual(sum(calls.values()), size + 1)
                self.assertEqual(calls["IT-1002.json"], 2)
                self.assertEqual(report["fixture_sha256"], hashlib.sha256((MOCKS / "load-template.json").read_bytes()).hexdigest())
                self.assertEqual(json.loads((directory / "observations.json").read_text(encoding="utf-8")), report)
                store = Store(directory / "workload.sqlite")
                try:
                    snapshot = store.snapshot()
                    queue = {row["document_id"]: row for row in snapshot["queue"]}
                    rows = {row["document_id"]: row for row in snapshot["extractions"]}
                    self.assertEqual(set(rows), {f"IT-{number}.json" for number in range(1001, 1001 + size)})
                    self.assertEqual(len(queue), size)
                    for document, row in rows.items():
                        path = directory / "inputs" / document
                        raw = path.read_bytes()
                        self.assertEqual(json.loads(raw)["message"], PARAPHRASE.replace("IT-021", Path(document).stem))
                        self.assertEqual(row["source_sha256"], hashlib.sha256(raw).hexdigest())
                        for field in ("attempt_id", "generation", "source_sha256", "fingerprint", "processing_state"):
                            self.assertEqual(queue[document][field], row[field])
                        self.assertEqual(row["processing_state"], "succeeded")
                        self.assertEqual((row["status"], row["owner"], row["next_action"]), ("review", "System Owner", "review-request"))
                        self.assertEqual(row["extraction"]["request_id"], Path(document).stem)
                        for field, value in expected.items():
                            self.assertEqual(row["extraction"][field], value, (document, field))
                        self.assertEqual(row["extraction"]["issues"], [])
                        for quote in row["extraction"]["supporting_quotes"]:
                            self.assertIn(quote, json.loads(raw)["message"])
                        history = store.history(document)
                        self.assertEqual(len(history), 2 if document == "IT-1002.json" else 1)
                    failed_history = store.history("IT-1002.json")
                    self.assertEqual([row["processing_state"] for row in failed_history], ["failed", "succeeded"])
                    exported = directory / "deliverables"
                    generation = exported.resolve(strict=True)
                    exported_rows = [json.loads(line) for line in (generation / "extractions.jsonl").read_text(encoding="utf-8").splitlines()]
                    exported_queue = json.loads((generation / "queue.json").read_text(encoding="utf-8"))
                    self.assertEqual(len(exported_rows), size)
                    self.assertEqual(len(exported_queue), size)
                    self.assertEqual({row["document_id"]: row["attempt_id"] for row in exported_rows}, {document: row["attempt_id"] for document, row in rows.items()})
                    self.assertEqual({row["document_id"]: (row["attempt_id"], row["source_sha256"]) for row in exported_queue}, {row["document_id"]: (row["attempt_id"], row["source_sha256"]) for row in exported_rows})
                finally:
                    store.close()


if __name__ == "__main__":
    unittest.main()
