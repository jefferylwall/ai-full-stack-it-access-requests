"""T36: local artifact checks cannot establish a completed human release.

Prerequisite outputs were generated from the 21-message corpus and the saved
it-001.json through it-021.json replies under tests/mock_responses/. This case
only inspects a temporary copy; it invokes no provider or external service.
"""

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from test_mock_provider import offline_only


ROOT = Path(__file__).resolve().parents[1]
EXTERNAL = {
    "capture": "unverified", "default_branch": "unverified",
    "push": "unverified", "classroom": "unverified",
}


class ReleaseContract(unittest.TestCase):
    def setUp(self):
        guard = offline_only()
        guard.__enter__()
        self.addCleanup(guard.__exit__, None, None, None)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "checkout"
        self.root.mkdir()
        for name in ("access_intake", "config", "docs", "accuracy", "materials", "tests", "tools"):
            shutil.copytree(ROOT / name, self.root / name, ignore=shutil.ignore_patterns("__pycache__"))
        for name in ("README.md", "snapshot.schema.json", "requirements.lock", "requirements-bootstrap.lock", "requirements.txt"):
            shutil.copy2(ROOT / name, self.root / name)
        # Preserve the publication protocol while copying only the selected
        # generation. Historical generations are irrelevant to local readiness.
        selected = (ROOT / "deliverables").resolve(strict=True)
        generation = self.root / ".deliverables-generations" / selected.name
        shutil.copytree(selected, generation)
        (self.root / "deliverables").symlink_to(generation.relative_to(self.root), target_is_directory=True)
        self.assertFalse((self.root / ".git").exists())
        self.assertFalse((self.root / "planning" / "validation.md").exists())

    def inspect(self, preflight):
        before = {
            str(path.relative_to(self.root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in self.root.rglob("*") if path.is_file()
        }
        report = preflight(self.root)
        after = {
            str(path.relative_to(self.root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in self.root.rglob("*") if path.is_file()
        }
        self.assertEqual(after, before, "preflight is read-only")
        self.assertEqual(report["external_checks"], EXTERNAL)
        self.assertFalse((self.root / "planning" / "validation.md").exists())
        return report

    @contextmanager
    def changed(self, relative, replacement):
        path = self.root / relative
        original = path.read_bytes()
        try:
            if replacement is None:
                path.unlink()
            else:
                path.write_text(json.dumps(replacement), encoding="utf-8")
            yield
        finally:
            path.write_bytes(original)

    def test_T36_missing_artifacts_links_configuration_and_observation_labels(self):
        from access_intake.release import preflight

        # These expectations are the plan's local prerequisite facts. A ready
        # local tree never means capture, push or Classroom are verified.
        report = self.inspect(preflight)
        self.assertTrue(report["ready"])
        self.assertEqual(report["missing_artifacts"], [])
        self.assertEqual(report["issues"], [])

        required = (
            "README.md", "requirements.lock", "config/extraction-prompt.txt",
            "snapshot.schema.json", "config/provider.json",
            "deliverables/extractions.jsonl", "deliverables/queue.json",
            "deliverables/accuracy.md", "deliverables/request-evidence.json",
            "deliverables/verification.md",
        )
        for name in required:
            with self.subTest(missing=name), self.changed(name, None):
                report = self.inspect(preflight)
                self.assertFalse(report["ready"])
                self.assertIn(name, report["missing_artifacts"])

        queue = json.loads((self.root / "deliverables/queue.json").read_text())
        self.assertEqual({row["document_id"] for row in queue}, {f"IT-{n:03}.json" for n in range(1, 22)})
        queue[0]["source_sha256"] = "0" * 64
        with self.subTest(incoherent_pair=True), self.changed("deliverables/queue.json", queue):
            report = self.inspect(preflight)
            self.assertFalse(report["ready"])
            self.assertIn("link", {issue["category"] for issue in report["issues"]})

        request = json.loads((self.root / "deliverables/request-evidence.json").read_text())
        request["request"]["body"]["model"] = "different-from-documented-configuration"
        with self.subTest(configuration=True), self.changed("deliverables/request-evidence.json", request):
            report = self.inspect(preflight)
            self.assertFalse(report["ready"])
            self.assertIn("configuration", {issue["category"] for issue in report["issues"]})

        request = json.loads((self.root / "deliverables/request-evidence.json").read_text())
        request["live_call_performed"] = True
        with self.subTest(observation_label=True), self.changed("deliverables/request-evidence.json", request):
            report = self.inspect(preflight)
            self.assertFalse(report["ready"])
            self.assertIn("observation", {issue["category"] for issue in report["issues"]})

        publication = self.root / "deliverables"
        target = publication.readlink()
        publication.unlink()
        publication.symlink_to(".deliverables-generations/missing", target_is_directory=True)
        try:
            with self.subTest(broken_publication=True):
                report = self.inspect(preflight)
                self.assertFalse(report["ready"])
                self.assertIn("deliverables/extractions.jsonl", report["missing_artifacts"])
                self.assertIn("deliverables/queue.json", report["missing_artifacts"])
        finally:
            publication.unlink()
            publication.symlink_to(target, target_is_directory=True)


if __name__ == "__main__":
    unittest.main()
