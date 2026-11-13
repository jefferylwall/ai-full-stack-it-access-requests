"""Step 7, T28–T30: frozen source oracles and independent comparison reports.

The corruption counts below are derived from 21 documents × 12 scalar facts,
not measured application output. Expected facts are separate from mock replies.
"""

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from access_intake.capture import capture
from access_intake.extraction import INSTRUCTIONS, SCHEMA
from access_intake.providers import MockProvider
from test_extraction import SCALARS
from test_mock_provider import offline_only


ROOT = Path(__file__).resolve().parents[1]
EXPECTED = ROOT / "tests" / "expectations"
MOCKS = ROOT / "tests" / "mock_responses"
REGISTRY = ROOT / "accuracy" / "expectations.json"
DOCUMENTATION = ROOT / "docs" / "accuracy.md"
PARAPHRASE = (
    "Please log IT-021 with request date 2026-10-22. I am Maya Chen on the Analytics team. "
    "For a new access window, let me read Atlas in staging beginning 2026-10-23 and ending "
    "2026-11-06. Priya Shah is my manager. I need to check the weekly conversion dashboard "
    "against the changed event definitions before our next reporting cycle."
)
ROUTES = {
    "Owner": ("review", "System Owner", "review-request"),
    "Risk": ("review", "Security Review", "review-risk-then-owner"),
    "Clarify": ("needs-information", "IT Service Desk", "clarify"),
    "Withdraw": ("review", "IT Service Desk", "confirm-withdrawal"),
    "Inspect": ("review", "IT Service Desk", "inspect-message"),
}
# Source catalogue in planning/plan.md, rather than measured routing decisions.
CATALOGUE_ROUTES = (
    "Owner", "Risk", "Clarify", "Clarify", "Withdraw", "Inspect", "Risk",
    "Owner", "Clarify", "Clarify", "Clarify", "Risk", "Risk", "Clarify",
    "Owner", "Clarify", "Clarify", "Owner", "Risk", "Clarify", "Owner",
)


class AccuracyContract(unittest.TestCase):
    def setUp(self):
        guard = offline_only()
        guard.__enter__()
        self.addCleanup(guard.__exit__, None, None, None)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.database = self.directory / "intake.sqlite"
        self.replies = self.directory / "replies"
        self.replies.mkdir()
        for number in range(1, 22):
            path = MOCKS / f"it-{number:03}.json"
            # T28/T29 author the two remaining replies before implementation.
            (self.replies / path.name).write_bytes(path.read_bytes())

    def cli(self, arguments, *, expected=0):
        result = subprocess.run(
            [sys.executable, str(ROOT / "tests" / "cli_process.py"), "module", str(self.directory),
             "--database", str(self.database), "--mock-dir", str(self.replies), *map(str, arguments)],
            cwd=ROOT, capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def corpus_records(self):
        self.cli(["intake", ROOT / "materials" / "requests", ROOT / "tests" / "inputs" / "IT-021.json"])
        destination = self.directory / "deliverables"
        self.cli(["export", "--output", destination])
        lines = (destination.resolve() / "extractions.jsonl").read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines]

    def assert_scalar(self, report, matches):
        self.assertEqual(report["scalar"]["total"], 252)
        self.assertEqual(report["scalar"]["matches"], matches)
        self.assertEqual(set(report["scalar"]["by_field"]), set(SCALARS))
        for field in SCALARS:
            self.assertEqual(report["scalar"]["by_field"][field]["total"], 21)

    def test_T28_registry_has_all_supplied_source_facts_evidence_and_routes(self):
        from access_intake.accuracy import load_registry

        registry = load_registry(REGISTRY)
        self.assertEqual(registry["scalar_fields"], list(SCALARS))
        entries = {entry["document_id"]: entry for entry in registry["entries"]}
        self.assertEqual(len(entries), 21)
        for number in range(1, 21):
            with self.subTest(document=number):
                document = f"IT-{number:03}.json"
                source = capture(ROOT / "materials" / "requests" / document)
                entry = entries[document]
                expected = json.loads((EXPECTED / document.lower()).read_text(encoding="utf-8"))
                self.assertEqual(entry["expected"], expected)
                self.assertEqual(entry["source_path"], f"materials/requests/{document}")
                self.assertEqual(entry["source_sha256"], hashlib.sha256(source.raw_bytes).hexdigest())
                self.assertEqual(entry["oracle_origin"], "source-and-policy")
                self.assertEqual(set(entry["fact_evidence"]), set(SCALARS))
                self.assertTrue(entry["policy_rules"])
                for field in SCALARS:
                    evidence = entry["fact_evidence"][field]
                    self.assertTrue(evidence["reason"], field)
                    for quote in evidence["quotes"]:
                        self.assertIn(quote, source.message, (document, field))
                    if expected[field] is not None:
                        self.assertTrue(evidence["quotes"], (document, field))
                        if field != "kind":
                            self.assertTrue(any(expected[field] in quote for quote in evidence["quotes"]), (document, field))
                self.assertEqual(tuple(entry["route"][key] for key in ("status", "owner", "next_action")), ROUTES[CATALOGUE_ROUTES[number - 1]])
                for quote in expected["supporting_quotes"]:
                    self.assertIn(quote, source.message)
        self.assertEqual(entries["IT-018.json"]["expected"]["requester"], "Daniel Wright")
        self.assertEqual(entries["IT-018.json"]["expected"]["end_date"], "2026-11-03")
        documentation = DOCUMENTATION.read_text(encoding="utf-8").lower()
        for content in ("252", "null", "literal", "supporting_quotes", "issues", "mock", "live", "semantic", "no passing accuracy threshold"):
            self.assertIn(content, documentation)
        # The newly completed corpus goes through the documented CLI interface.
        self.cli(["intake", ROOT / "materials" / "requests"])
        queue = {row["document_id"]: row for row in self.cli(["queue"])}
        self.assertEqual(set(queue), {f"IT-{number:03}.json" for number in range(1, 21)})
        for number in range(1, 21):
            row = queue[f"IT-{number:03}.json"]
            self.assertEqual(tuple(row[key] for key in ("status", "owner", "next_action")), ROUTES[CATALOGUE_ROUTES[number - 1]])
            self.assertEqual(row["source_sha256"], entries[row["document_id"]]["source_sha256"])
            self.assertEqual(row["processing_state"], "succeeded")

    def test_T29_authored_paraphrase_has_independent_expectation_and_fresh_wording(self):
        from access_intake.accuracy import load_registry

        path = ROOT / "tests" / "inputs" / "IT-021.json"
        source = capture(path)
        self.assertEqual(source.message, PARAPHRASE)
        self.assertNotEqual(source.message, capture(ROOT / "materials" / "requests" / "IT-001.json").message)
        registry = load_registry(REGISTRY)
        entry = next(entry for entry in registry["entries"] if entry["document_id"] == "IT-021.json")
        self.assertEqual(entry["source_path"], "tests/inputs/IT-021.json")
        self.assertEqual(entry["source_sha256"], hashlib.sha256(source.raw_bytes).hexdigest())
        expected = {
            "request_id": "IT-021", "request_date": "2026-10-22", "kind": "new",
            "requester": "Maya Chen", "team": "Analytics", "system": "Atlas", "environment": "staging",
            "permission": "read", "business_purpose": "I need to check the weekly conversion dashboard against the changed event definitions before our next reporting cycle.",
            "start_date": "2026-10-23", "end_date": "2026-11-06", "manager": "Priya Shah",
        }
        for field, value in expected.items():
            self.assertEqual(entry["expected"][field], value)
        self.assertEqual(entry["expected"]["issues"], [])
        self.cli(["intake", path])
        row = self.cli(["history", "IT-021.json"])[0]
        for field, value in expected.items():
            self.assertEqual(row["extraction"][field], value)
        self.assertEqual(tuple(row[key] for key in ("status", "owner", "next_action")), ROUTES["Owner"])
        for quote in row["extraction"]["supporting_quotes"]:
            self.assertIn(quote, PARAPHRASE)
        self.assertIn("IT-021", DOCUMENTATION.read_text(encoding="utf-8"))

    def test_T30_comparison_counts_corruption_and_keeps_semantic_checks_separate(self):
        from access_intake.accuracy import compare, load_registry

        registry = load_registry(REGISTRY)
        records = self.corpus_records()
        oracle_hashes = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in EXPECTED.glob("it-*.json")}
        with mock.patch.object(MockProvider, "complete", side_effect=AssertionError("comparison must not invoke a provider")):
            report = compare(records, registry)
        self.assert_scalar(report, 252)
        self.assertFalse(report["has_errors"])
        self.assertEqual(report["observation"], "mock_fixture_verification")
        self.assertEqual(report["differences"], [])
        for check in ("routes", "source_hashes", "evidence", "issues"):
            self.assertEqual(report["checks"][check], {"matches": 21, "total": 21})
        for field in ("supporting_quotes", "issues"):
            self.assertEqual(report["literal_arrays"][field], {"matches": 21, "total": 21})
        provenance = {row["document_id"]: row for row in report["provenance"]}
        self.assertEqual(len(provenance), 21)
        schema_text = json.dumps(SCHEMA, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        for row in records:
            observed = provenance[row["document_id"]]
            for field in ("source_sha256", "fingerprint", "policy_sha256", "fixture_hashes", "response_source"):
                self.assertEqual(observed[field], row[field])
            self.assertEqual(observed["provider"], "mock")
            self.assertEqual(observed["model"], "fixture-v1")
            self.assertEqual(observed["parameters"], {"temperature": 0})
            self.assertEqual(observed["prompt_sha256"], hashlib.sha256(INSTRUCTIONS.encode()).hexdigest())
            self.assertEqual(observed["schema_sha256"], hashlib.sha256(schema_text.encode()).hexdigest())
            fixture = row["document_id"].lower()
            self.assertEqual(observed["fixture_hashes"][fixture], hashlib.sha256((MOCKS / fixture).read_bytes()).hexdigest())

        corrupted = copy.deepcopy(records)
        next(row for row in corrupted if row["document_id"] == "IT-001.json")["extraction"]["permission"] = "admin"
        corrupted = [row for row in corrupted if row["document_id"] != "IT-002.json"]
        report = compare(corrupted, registry)
        # One wrong scalar plus one missing 12-scalar extraction: 252 - 1 - 12.
        self.assert_scalar(report, 239)
        self.assertTrue(report["has_errors"])
        self.assertEqual(report["scalar"]["by_field"]["permission"]["matches"], 19)
        self.assertEqual(report["scalar"]["by_field"]["manager"]["matches"], 20, "null managers count literally")
        self.assertTrue(any(difference["document_id"] == "IT-001.json" and difference["field"] == "permission" and difference["category"] == "fabricated_fact" for difference in report["differences"]))
        self.assertEqual(sum(difference["document_id"] == "IT-002.json" and difference["category"] == "missing_extraction" for difference in report["differences"]), 12)
        measured = self.directory / "corrupted.jsonl"
        measured.write_text("".join(json.dumps(row) + "\n" for row in corrupted), encoding="utf-8")
        cli_report = self.cli(["accuracy", "--input", measured, "--expectations", REGISTRY], expected=1)
        self.assert_scalar(cli_report, 239)
        self.assertTrue(cli_report["has_errors"])

        for document, field, value, category in (
            ("IT-001.json", "permission", None, "missing_fact"),
            ("IT-004.json", "environment", "production", "unresolved_conflict"),
            ("IT-008.json", "environment", "production", "correction_error"),
            ("IT-009.json", "manager", "Invented Manager", "fabricated_fact"),
        ):
            with self.subTest(document=document, field=field):
                variant = copy.deepcopy(records)
                next(row for row in variant if row["document_id"] == document)["extraction"][field] = value
                result = compare(variant, registry)
                self.assert_scalar(result, 251)
                self.assertTrue(any(item["document_id"] == document and item["field"] == field and item["category"] == category for item in result["differences"]))
        for value in (None, []):
            with self.subTest(invalid_extraction=value):
                variant = copy.deepcopy(records)
                next(row for row in variant if row["document_id"] == "IT-001.json")["extraction"] = value
                self.assert_scalar(compare(variant, registry), 240)

        equivalent_arrays = copy.deepcopy(records)
        next(row for row in equivalent_arrays if row["document_id"] == "IT-004.json")["extraction"]["supporting_quotes"].reverse()
        next(row for row in equivalent_arrays if row["document_id"] == "IT-010.json")["extraction"]["issues"] = ["The end_date is missing because no end date has been fixed."]
        result = compare(equivalent_arrays, registry)
        self.assert_scalar(result, 252)
        self.assertFalse(result["has_errors"], "literal array ordering/prose is not a business rule")
        self.assertEqual(result["literal_arrays"]["supporting_quotes"]["matches"], 20)
        self.assertEqual(result["literal_arrays"]["issues"]["matches"], 20)
        self.assertEqual(result["checks"]["evidence"]["matches"], 21)
        self.assertEqual(result["checks"]["issues"]["matches"], 21)

        for document, field in (("IT-006.json", "supporting_quotes"), ("IT-004.json", "supporting_quotes"), ("IT-020.json", "issues")):
            with self.subTest(required_evidence_or_issue=(document, field)):
                variant = copy.deepcopy(records)
                next(row for row in variant if row["document_id"] == document)["extraction"][field] = []
                result = compare(variant, registry)
                self.assert_scalar(result, 252)
                self.assertTrue(result["has_errors"])
                check = "evidence" if field == "supporting_quotes" else "issues"
                self.assertEqual(result["checks"][check]["matches"], 20)
        prose_variant = copy.deepcopy(records)
        row = next(row for row in prose_variant if row["document_id"] == "IT-001.json")
        row["extraction"]["business_purpose"] = "I am checking the weekly conversion dashboard with the new event definition changes (before next reporting cycle)."
        result = compare(prose_variant, registry)
        self.assert_scalar(result, 251)
        self.assertTrue(any(item["field"] == "business_purpose" and item["semantic_review_required"] for item in result["differences"]))
        for path, original_hash in oracle_hashes.items():
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), original_hash, "comparison must not rewrite its oracle")


if __name__ == "__main__":
    unittest.main()
