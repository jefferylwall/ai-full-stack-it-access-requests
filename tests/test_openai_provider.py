"""Step 8, T31–T33: native Responses envelopes via a fake HTTP opener.

Protocol/model research is in docs/provider-protocol.json. Credentials below are
in-memory test sentinels. The real credential accessor is always replaced.
"""

from dataclasses import replace
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import urllib.error
import urllib.request

from access_intake.capture import capture
from access_intake.extraction import SCHEMA, build_prompt, extract
from access_intake.exports import export_snapshot, pin_export
from access_intake.routing import route
from access_intake.store import Store
from access_intake.workers import WorkerPool
from test_mock_provider import offline_only
from worker_support import config


ROOT = Path(__file__).resolve().parents[1]
MOCKS = ROOT / "tests" / "mock_responses"
MODEL = "gpt-6-luna"
ENDPOINT = "https://api.openai.com/v1/responses"
PARAMETERS = {"max_output_tokens": 2048, "reasoning": {"effort": "none"}}
SENTINEL = "TEST_ONLY_CREDENTIAL_8b7c_no_real_key"
OWNER = ("review", "System Owner", "review-request")
INSPECT = ("review", "IT Service Desk", "inspect-message")
ERROR = ("error", "IT Service Desk", "retry-extraction")


class HTTPReply:
    def __init__(self, body, status=200, headers=None):
        self.body, self.status = body, status
        self.headers = headers or {"content-type": "application/json"}

    def read(self):
        return self.body

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeOpener:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, request, *, timeout):
        self.calls.append({"method": request.get_method(), "url": request.full_url,
                           "headers": dict(request.header_items()), "body": json.loads(request.data), "timeout": timeout})
        if not self.outcomes:
            raise AssertionError("adapter made an unexpected extra HTTP call")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class RealAdapterContract(unittest.TestCase):
    def setUp(self):
        guard = offline_only()
        guard.__enter__()
        self.addCleanup(guard.__exit__, None, None, None)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.source = capture(ROOT / "materials" / "requests" / "IT-001.json")

    def native(self, name):
        return HTTPReply((MOCKS / f"{name}.json").read_bytes())

    def adapter(self, opener, **overrides):
        from access_intake.openai_provider import HTTPTransport, OpenAIResponsesProvider

        loader = overrides.pop("credential_loader", mock.Mock(return_value=SENTINEL))
        values = {"model": MODEL, "endpoint": ENDPOINT, "parameters": PARAMETERS, "schema": SCHEMA,
                  "timeout": 2.5, "transport": HTTPTransport(opener=opener), "credential_loader": loader}
        values.update(overrides)
        provider = OpenAIResponsesProvider(**values)
        loader.assert_not_called()
        return provider, loader

    def assert_error(self, result, category):
        self.assertEqual(tuple(result[key] for key in ("status", "owner", "next_action")), ERROR)
        self.assertIsNone(result["extraction"])
        self.assertEqual(result["error"]["category"], category)
        self.assertTrue(result["error"]["message"])
        self.assertIn("--force", result["error"]["retry_command"])

    def test_T31_actual_HTTP_construction_configuration_and_CLI_selection(self):
        from access_intake.openai_provider import HTTPTransport, OpenAIResponsesProvider
        from access_intake.cli import main

        research = json.loads((ROOT / "docs" / "provider-protocol.json").read_text(encoding="utf-8"))
        defaults = json.loads((ROOT / "config" / "provider.json").read_text(encoding="utf-8"))
        self.assertEqual(defaults, research["request_defaults"])
        self.assertEqual(research["selected_model"], MODEL)
        self.assertEqual(research["verified_on"], "2026-11-13")
        self.assertTrue(all(url.startswith("https://developers.openai.com/") for url in research["sources"]))
        opener = FakeOpener([self.native("provider-success")])
        provider, loader = self.adapter(opener)
        result = extract(self.source, provider)
        loader.assert_called_once_with()
        self.assertIsNone(result["error"])
        oracle = json.loads((ROOT / "tests" / "expectations" / "it-001.json").read_text(encoding="utf-8"))
        self.assertEqual(result["extraction"], oracle)
        self.assertEqual(result["response_source"], "openai/" + MODEL)
        self.assertEqual(tuple(route(self.source, result["extraction"])[key] for key in ("status", "owner", "next_action")), OWNER)
        self.assertEqual(len(opener.calls), 1)
        request = opener.calls[0]
        self.assertEqual((request["method"], request["url"], request["timeout"]), ("POST", ENDPOINT, 2.5))
        self.assertEqual({key.lower(): value for key, value in request["headers"].items()}["authorization"], "Bearer " + SENTINEL)
        body = request["body"]
        self.assertEqual(body["model"], MODEL)
        self.assertEqual(body["input"], build_prompt(self.source))
        self.assertEqual(json.loads(body["input"][1]["content"]), {"message": self.source.message})
        self.assertNotIn(SENTINEL, json.dumps(body))
        self.assertEqual(body["text"]["format"], {"type": "json_schema", "name": "access_request", "strict": True, "schema": SCHEMA})
        for key, value in PARAMETERS.items():
            self.assertEqual(body[key], value)
        for key, value in {"stream": False, "background": False, "store": False, "truncation": "disabled", "tools": [], "tool_choice": "none"}.items():
            self.assertEqual(body[key], value)
        evidence = provider.request_evidence()
        self.assertEqual(evidence["request"]["body"], body)
        self.assertEqual(evidence["request"]["headers"]["Authorization"], "Bearer [REDACTED]")
        self.assertNotIn(SENTINEL, json.dumps(evidence))
        for category in ("completed", "refusal", "interruption", "invalid_envelope", "invalid_json", "invalid_schema", "provider_error", "timeout", "rate_limit", "unavailable"):
            self.assertIn(category, evidence["response_handling"])
        loader.assert_called_once_with()

        alternate = FakeOpener([self.native("provider-success-alternate")])
        changed, _ = self.adapter(alternate, model="configured-test-model", endpoint="https://example.test/v1/responses", parameters={"max_output_tokens": 1024, "reasoning": {"effort": "low"}})
        result = extract(self.source, changed)
        self.assertIsNone(result["error"])
        self.assertEqual(alternate.calls[0]["url"], "https://example.test/v1/responses")
        self.assertEqual(alternate.calls[0]["body"]["model"], "configured-test-model")
        self.assertEqual(alternate.calls[0]["body"]["max_output_tokens"], 1024)
        self.assertEqual(result["response_source"], "openai/configured-test-model")

        for status, category in ((401, "provider_error"), (429, "rate_limit"), (503, "unavailable")):
            with self.subTest(http_status=status):
                error = urllib.error.HTTPError(ENDPOINT, status, "scripted HTTP error", {}, io.BytesIO(b'{"error":{"message":"scripted"}}'))
                fake = FakeOpener([error])
                failed, _ = self.adapter(fake)
                self.assert_error(extract(self.source, failed), category)
                self.assertEqual(len(fake.calls), 1, "no hidden retries")

        # Actual CLI live selection uses a patched credential accessor and the
        # same real HTTP transport with a fake opener; no key is ever read.
        live = FakeOpener([self.native("provider-success")])
        environment = {"PROVIDER": "openai", "MODEL": MODEL, "PROVIDER_ENDPOINT": ENDPOINT, "REQUEST_SETTINGS": json.dumps(PARAMETERS)}
        database = self.directory / "live.sqlite"
        with mock.patch("access_intake.openai_provider.read_api_key", return_value=SENTINEL) as accessor, redirect_stdout(io.StringIO()):
            code = main(["--database", str(database), "intake", str(ROOT / "materials" / "requests" / "IT-001.json")], environment=environment, http_transport=HTTPTransport(opener=live))
        self.assertEqual(code, 0)
        accessor.assert_called_once_with()
        store = Store(database)
        try:
            row = store.history("IT-001.json")[0]
            self.assertEqual(row["response_source"], "openai/" + MODEL)
            self.assertEqual(row["settings"]["parameters"], PARAMETERS)
            self.assertEqual(row["settings"]["model"], MODEL)
            self.assertEqual(row["fixture_hashes"], {})
        finally:
            store.close()
        forbidden = mock.Mock(side_effect=AssertionError("mock mode must not load credentials"))
        unused = FakeOpener([])
        with redirect_stdout(io.StringIO()):
            code = main(["--provider", "mock", "--database", str(self.directory / "mock.sqlite"), "intake", str(ROOT / "materials" / "requests" / "IT-001.json")], environment=environment, credential_loader=forbidden, http_transport=HTTPTransport(opener=unused))
        self.assertEqual(code, 0)
        forbidden.assert_not_called()
        self.assertEqual(unused.calls, [])

    def test_T32_credentials_never_reach_persisted_errors_logs_evidence_or_override_output(self):
        from access_intake.openai_provider import HTTPTransport

        fixture = json.loads((MOCKS / "provider-error-secret.json").read_text(encoding="utf-8"))
        self.assertIn(SENTINEL, fixture["url"])
        self.assertIn(SENTINEL, json.dumps(fixture["headers"]))
        self.assertIn(SENTINEL, json.dumps(fixture["body"]))
        error = urllib.error.HTTPError(fixture["url"], fixture["status"], fixture["message"], fixture["headers"], io.BytesIO(json.dumps(fixture["body"]).encode()))
        source = capture(ROOT / "materials" / "requests" / "IT-006.json")
        base = config()
        settings = {**base.settings, "provider": "openai", "model": MODEL, "endpoint": ENDPOINT, "parameters": PARAMETERS}
        producing = replace(base, settings=settings, fixtures={})
        database = self.directory / "redaction.sqlite"
        store = Store(database)
        self.addCleanup(store.close)
        store.admit([source], producing)
        logs, providers = [], []
        opener = FakeOpener([error])

        def factory(claim):
            provider, _ = self.adapter(opener, logger=logs.append)
            providers.append(provider)
            return provider

        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            WorkerPool(database, provider_factory=factory).run()
        failed = store.snapshot()["extractions"][0]
        self.assert_error(failed, "provider_error")
        destination = self.directory / "deliverables"
        export_snapshot(store, destination)
        evidence = providers[0].request_evidence()
        evidence_path = self.directory / "request-evidence.json"
        evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
        artifacts = json.dumps({"snapshot": store.snapshot(), "history": store.history(source.document_id), "logs": logs,
                                "exports": pin_export(destination).read(), "evidence": evidence,
                                "stdout": stdout.getvalue(), "stderr": stderr.getvalue()})
        self.assertTrue(logs)
        self.assertNotIn(SENTINEL, artifacts)
        for path in self.directory.glob("redaction.sqlite*"):
            self.assertNotIn(SENTINEL.encode(), path.read_bytes(), path.name)
        self.assertNotIn(SENTINEL, evidence_path.read_text(encoding="utf-8"))
        self.assertEqual(evidence["request"]["headers"]["Authorization"], "Bearer [REDACTED]")
        self.assertNotIn(SENTINEL, json.dumps(opener.calls[0]["body"]))
        self.assertEqual(json.loads(opener.calls[0]["body"]["input"][1]["content"])["message"], source.message)
        # The source's request to display a key remains data on a successful retry.
        store.admit([source], producing, force=True)
        success = FakeOpener([self.native("provider-override-success")])
        WorkerPool(database, provider_factory=lambda claim: self.adapter(success)[0]).run()
        row = store.snapshot()["extractions"][0]
        self.assertEqual(tuple(row[key] for key in ("status", "owner", "next_action")), INSPECT)
        self.assertEqual(row["extraction"]["permission"], "read")
        self.assertNotIn(SENTINEL, json.dumps(store.snapshot()))
        self.assertNotIn(SENTINEL, json.dumps(store.history(source.document_id)))

    def test_T33_refusal_interruption_and_bad_native_envelopes_never_route_partial_text(self):
        from access_intake.openai_provider import OpenAIResponsesProvider

        self.assertTrue(callable(OpenAIResponsesProvider))
        for name, category in (
            ("provider-refusal", "refusal"), ("provider-incomplete", "interruption"),
            ("provider-malformed", "invalid_json"), ("provider-missing-status", "invalid_envelope"),
            ("provider-empty-output", "invalid_envelope"), ("provider-message-incomplete", "interruption"),
            ("provider-error-completed", "provider_error"),
        ):
            with self.subTest(fixture=name):
                opener = FakeOpener([self.native(name)])
                provider, loader = self.adapter(opener)
                self.assert_error(extract(self.source, provider), category)
                loader.assert_called_once_with()
                self.assertEqual(len(opener.calls), 1)
        incomplete = json.loads((MOCKS / "provider-incomplete.json").read_text(encoding="utf-8"))
        partial = incomplete["output"][0]["content"][0]["text"]
        self.assertEqual(json.loads(partial)["request_id"], "IT-001", "valid-looking partial JSON must still be discarded")


if __name__ == "__main__":
    unittest.main()
