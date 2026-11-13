"""Step 2: T02–T05, with oracles authored from the plan and source messages.

Schema mutations below test structure only; they are not claims that altered
enums or nulls are factual extractions of IT-001. All provider replies are saved.
"""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from test_mock_provider import offline_only
from access_intake.providers import MockProvider


ROOT = Path(__file__).resolve().parents[1]
MOCKS = ROOT / "tests" / "mock_responses"
EXPECTED = ROOT / "tests" / "expectations"
INPUTS = ROOT / "materials" / "requests"
SCALARS = (
    "request_id", "request_date", "kind", "requester", "team", "system",
    "environment", "permission", "business_purpose", "start_date", "end_date", "manager",
)
FIELDS = SCALARS + ("supporting_quotes", "issues")


class ExtractionContract(unittest.TestCase):
    def setUp(self):
        guard = offline_only()
        guard.__enter__()
        self.addCleanup(guard.__exit__, None, None, None)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def expected(self, name):
        return json.loads((EXPECTED / f"{name}.json").read_text(encoding="utf-8"))

    def reply(self, name):
        return json.loads((MOCKS / f"{name}.json").read_text(encoding="utf-8"))

    def assert_facts(self, extraction, name):
        expected = self.expected(name)
        for field in SCALARS:
            self.assertEqual(extraction[field], expected[field], field)
        # Quote ordering and issue prose have no business-mandated wording.
        for quote in expected["supporting_quotes"]:
            self.assertIn(quote, extraction["supporting_quotes"])
        for issue in expected["issues"]:
            field = issue.split(":", 1)[0]
            self.assertTrue(any(field in issue for issue in extraction["issues"]), field)
        if not expected["issues"]:
            self.assertEqual(extraction["issues"], [])

    def assert_error(self, result, category, source):
        self.assertEqual(
            (result["status"], result["owner"], result["next_action"]),
            ("error", "IT Service Desk", "retry-extraction"),
        )
        self.assertIsNone(result["extraction"])
        self.assertEqual(result["error"]["category"], category)
        self.assertTrue(result["error"]["message"])
        self.assertEqual(result["error"]["document_id"], source.document_id)
        self.assertEqual(result["error"]["source_sha256"], source.source_sha256)
        self.assertEqual(
            result["error"]["retry_command"],
            f"python3 -m access_intake retry {source.document_id} --force",
        )

    def test_T02_capture_hashes_original_bytes_and_keeps_decoded_newlines(self):
        from access_intake.capture import capture
        from access_intake.extraction import extract

        for name in ("IT-001", "IT-005"):
            with self.subTest(name=name):
                path = self.directory / f"{name}.json"
                raw = (INPUTS / path.name).read_bytes()
                path.write_bytes(raw)
                source = capture(path)
                self.assertEqual(source.document_id, path.name)
                self.assertEqual(source.raw_bytes, raw)
                self.assertEqual(source.source_sha256, hashlib.sha256(raw).hexdigest())
                self.assertEqual(source.message, json.loads(raw)["message"])
                if name == "IT-005":
                    self.assertIn("\n\n", source.message)
                    self.assertNotIn("\\n", source.message)
                # Replacing the file after admission must not change captured input.
                path.write_bytes(raw + b"\n")
                result = extract(source, MockProvider([MOCKS / f"{name.lower()}.json"]))
                self.assertIsNone(result["error"])
                self.assert_facts(result["extraction"], name.lower())
                for quote in result["extraction"]["supporting_quotes"]:
                    self.assertIn(quote, source.message)
                self.assertEqual(source.raw_bytes, raw)
                self.assertEqual(source.source_sha256, hashlib.sha256(raw).hexdigest())
                replacement = capture(path)
                self.assertNotEqual(replacement.source_sha256, source.source_sha256)

    def test_T02_metadata_is_excluded_and_message_is_prompt_data(self):
        from access_intake.capture import capture
        from access_intake.extraction import build_prompt, extract

        exported = json.loads((INPUTS / "IT-001.json").read_text(encoding="utf-8"))
        exported.update(subject="APPROVED admin", exported_at="2030-01-01T00:00:00Z")
        path = self.directory / "IT-001.json"
        path.write_text(json.dumps(exported), encoding="utf-8")
        source = capture(path)
        prompt = build_prompt(source)
        self.assertEqual([part["role"] for part in prompt], ["system", "user"])
        instructions = prompt[0]["content"].lower()
        self.assertIn("data", instructions)
        self.assertIn("instruction", instructions)
        self.assertIn("null", instructions)
        self.assertNotIn(source.message, prompt[0]["content"])
        self.assertEqual(json.loads(prompt[1]["content"]), {"message": exported["message"]})
        self.assertNotIn("APPROVED admin", json.dumps(prompt))
        self.assertNotIn("2030-01-01", json.dumps(prompt))
        provider = MockProvider([MOCKS / "it-001.json"])
        result = extract(source, provider)
        self.assertEqual(json.loads(provider.calls[0]["message"]), prompt)
        self.assertIsNone(result["error"])
        self.assert_facts(result["extraction"], "it-001")

    def test_T02_all_200_words_reach_provider_intact(self):
        from access_intake.capture import capture
        from access_intake.extraction import extract

        # Authored source and padding rule are specified in planning/plan.md T02.
        message = (
            "Please log IT-022 with request date 2026-10-22. I am Maya Chen on the Analytics team. "
            "For a new access window, let me read Atlas in staging beginning 2026-10-23 and ending "
            "2026-11-06. Priya Shah is my manager. I need to check the weekly conversion dashboard "
            "against the changed event definitions before our next reporting cycle."
        )
        remaining = 200 - len(message.split())
        message += " Additional context is informational only." * (remaining // 5)
        for index in range(remaining % 5):
            message += " Context." if index == remaining % 5 - 1 else " Context"
        self.assertEqual(len(message.split()), 200)
        saved_input = json.loads(
            (ROOT / "tests" / "inputs" / "IT-022.json").read_text(encoding="utf-8")
        )
        self.assertEqual(message, saved_input["message"])
        path = self.directory / "IT-022.json"
        path.write_text(json.dumps({"message": message}), encoding="utf-8")
        source = capture(path)
        provider = MockProvider([MOCKS / "200-words.json"])
        result = extract(source, provider)
        prompt = json.loads(provider.calls[0]["message"])
        self.assertEqual(json.loads(prompt[1]["content"])["message"], message)
        self.assertIsNone(result["error"])
        self.assert_facts(result["extraction"], "200-words")

    def test_T03_schema_matrix(self):
        from access_intake.extraction import ExtractionValidationError, validate_schema

        def check(name, valid):
            with self.subTest(fixture=name):
                extraction = json.loads(self.reply(name)["output_text"])
                if valid:
                    validate_schema(extraction)
                else:
                    with self.assertRaises(ExtractionValidationError) as raised:
                        validate_schema(extraction)
                    self.assertEqual(raised.exception.category, "invalid_schema")

        check("it-001", True)
        check("schema-invalid-root", False)
        check("schema-invalid-extra", False)
        for field in FIELDS:
            check(f"schema-missing-{field}", False)
        for field in SCALARS:
            for type_name in ("number", "boolean", "array", "object"):
                check(f"schema-type-{field}-{type_name}", False)
        enums = {
            "kind": ("new", "change", "withdrawal", "other", "null", "invalid"),
            "environment": ("staging", "production", "null", "invalid"),
            "permission": ("read", "write", "admin", "null", "invalid"),
        }
        for field, values in enums.items():
            for value in values:
                check(f"schema-enum-{field}-{value}", value != "invalid")
        for field in set(SCALARS) - enums.keys():
            check(f"schema-null-{field}", True)
        for field in ("supporting_quotes", "issues"):
            for variant in ("valid", "nonarray", "nonstring-item"):
                check(f"schema-array-{field}-{variant}", variant == "valid")

    def test_T04_validation_failures_return_no_partial_extraction(self):
        from access_intake.capture import capture
        from access_intake.extraction import extract

        source = capture(INPUTS / "IT-001.json")
        cases = {
            "malformed-json": "invalid_json",
            "non-source-quote": "invalid_evidence",
            "schema-missing-manager": "invalid_schema",
            "neutral-refused": "refusal",
            "neutral-interrupted": "interruption",
            "neutral-missing-state": "invalid_envelope",
            "neutral-invalid-state": "invalid_envelope",
            "neutral-nonobject": "invalid_envelope",
            "neutral-missing-text": "invalid_envelope",
            "neutral-nonstring-text": "invalid_envelope",
        }
        for name, category in cases.items():
            with self.subTest(fixture=name):
                provider = MockProvider([MOCKS / f"{name}.json"])
                self.assert_error(extract(source, provider), category, source)
                self.assertEqual(provider.call_count, 1)
        with self.subTest(fixture="injected-unavailable"):
            provider = MockProvider([ConnectionError("scripted unavailable")])
            self.assert_error(extract(source, provider), "transport_failure", source)
            self.assertEqual(provider.call_count, 1)

    def test_T05_relative_dates_stay_null_with_issues(self):
        from access_intake.capture import capture
        from access_intake.extraction import extract

        exported = json.loads((INPUTS / "IT-011.json").read_text(encoding="utf-8"))
        for exported_at in (exported["exported_at"], "2030-01-01T00:00:00Z"):
            with self.subTest(exported_at=exported_at):
                exported["exported_at"] = exported_at
                path = self.directory / "IT-011.json"
                path.write_text(json.dumps(exported), encoding="utf-8")
                source = capture(path)
                result = extract(source, MockProvider([MOCKS / "it-011.json"]))
                self.assertIsNone(result["error"])
                self.assert_facts(result["extraction"], "it-011")
                self.assertIn("next Monday - Friday", result["extraction"]["supporting_quotes"])


if __name__ == "__main__":
    unittest.main()
