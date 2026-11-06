"""Automated tests. Run: python -m unittest discover -s tests -v

The live provider class (OpenAICompatibleProvider) is exercised end to end
with a fake HTTP transport, so request building, retries and response
handling are the production code paths. Expected business facts come from
tests/expected/expected_fields.json (written from the source messages and
policy), not from the mock responses.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from accessq import accuracy, policy
from accessq.config import REPO_ROOT, Settings
from accessq.intake import Intake, discover, load_source, render_outputs
from accessq.provider import ExtractionError, MockProvider, OpenAICompatibleProvider, handle_completion
from accessq.store import Store

REQUESTS = REPO_ROOT / "materials" / "requests"
SYNTHETIC = REPO_ROOT / "tests" / "fixtures" / "synthetic"
MOCKS = json.loads((REPO_ROOT / "mocks" / "responses.json").read_text(encoding="utf-8"))
EXPECTED = accuracy.load_expected()
API_KEY = "sk-test-SECRETSECRET123456"


def completion(ext=None, finish="stop", refusal=None, content=None) -> dict:
    return {"id": "cmpl-test", "model": "test-model", "choices": [{"index": 0, "finish_reason": finish, "message": {
        "role": "assistant", "refusal": refusal,
        "content": content if content is not None else (json.dumps(ext) if ext is not None else None)}}]}


def http(body, status=200, headers=None):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return (status, headers or {}, raw)


class FakeTransport:
    """Stands in for the HTTP layer. behaviour[doc] = list of responses (one per call) or callable(n, payload)."""

    def __init__(self, behaviour=None, delay=0.0):
        self.behaviour = behaviour or {}
        self.delay = delay
        self.calls: list[str] = []
        self.requests: list[dict] = []
        self._lock = threading.Lock()

    def __call__(self, url, headers, body, timeout):
        req = json.loads(body)
        user = req["messages"][1]["content"]
        payload = json.loads(user[user.index("{"):])
        doc = payload["document_id"]
        with self._lock:
            self.calls.append(doc)
            self.requests.append({"url": url, "headers": headers, "body": req})
            n = self.calls.count(doc)
        if self.delay:
            time.sleep(self.delay)
        b = self.behaviour.get(doc)
        r = b(n, payload) if callable(b) else (b[min(n - 1, len(b) - 1)] if b else None)
        if r is None:
            r = http(completion(MOCKS[doc]["extraction"]))
        if isinstance(r, BaseException):
            raise r
        return r


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="accessq-test-"))
        self.inputs = self.tmp / "in"
        shutil.copytree(REQUESTS, self.inputs)
        self.out = self.tmp / "out"
        self.settings = Settings(provider="openai-compatible", base_url="https://llm.example.test/v1",
                                 model="test-model", max_retries=2, backoff_base_seconds=0,
                                 backoff_max_seconds=0, concurrency=4, lock_lease_seconds=30)
        self.store = Store(self.tmp / "state", operator="tester")
        self.store.secrets.append(API_KEY)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def intake(self, transport, settings=None, store=None, key=API_KEY):
        s = settings or self.settings
        prov = OpenAICompatibleProvider(s, key, transport, sleep=lambda _: None)
        return Intake(s, store or self.store, prov, self.out, rebuild_interval=0)

    def run_all(self, transport, docs=None, **kw):
        srcs = discover([self.inputs])
        if docs:
            srcs = [s for s in srcs if s.document_id in docs]
        it = self.intake(transport, **kw)
        return it, it.run(srcs)

    def cur(self, doc):
        return self.store.read_current(doc)

    def assert_route(self, doc, status, owner, next_action):
        r = self.cur(doc)
        self.assertEqual((r["status"], r["owner"], r["next_action"]), (status, owner, next_action), r.get("review_flags"))

    def assert_matches_expected(self, doc, group="supplied"):
        exp = EXPECTED[group][doc]
        ext = self.cur(doc)["extraction"]
        for f in accuracy.EXACT_FIELDS:
            self.assertEqual(ext[f], exp["fields"][f], f"{doc}.{f}")
        r = self.cur(doc)
        self.assertEqual({k: r[k] for k in accuracy.ROUTE_FIELDS}, exp["route"], doc)


class RecordAndReviewBehaviour(Base):
    def test_complete_request_routes_to_system_owner(self):
        _, run = self.run_all(FakeTransport(), docs={"IT-001.json"})
        self.assertEqual(run["outcomes"], {"IT-001.json": "extracted"})
        self.assert_matches_expected("IT-001.json")
        r = self.cur("IT-001.json")
        self.assertIsNone(r["error"])
        self.assertEqual(r["response_source"], "openai-compatible(llm.example.test)/test-model")
        raw = (self.inputs / "IT-001.json").read_bytes()
        import hashlib
        self.assertEqual(r["source_sha256"], hashlib.sha256(raw).hexdigest())
        for q in r["extraction"]["supporting_quotes"]:
            self.assertIn(q, json.loads(raw)["message"])

    def test_all_supplied_messages_match_source_derived_expectations(self):
        self.run_all(FakeTransport())
        for doc in EXPECTED["supplied"]:
            self.assert_matches_expected(doc)

    def test_missing_dates_need_clarification(self):
        self.run_all(FakeTransport(), docs={"IT-010.json", "IT-011.json", "IT-003.json"})
        for doc in ("IT-010.json", "IT-011.json", "IT-003.json"):
            self.assert_route(doc, "needs-information", "IT Service Desk", "clarify")
        self.assertIsNone(self.cur("IT-010.json")["extraction"]["end_date"])
        self.assertIsNone(self.cur("IT-011.json")["extraction"]["start_date"])
        self.assertIn("missing_required: start_date, end_date", self.cur("IT-011.json")["review_flags"])

    def test_conflicting_details_need_clarification(self):
        self.run_all(FakeTransport(), docs={"IT-004.json", "IT-020.json"})
        for doc, field in (("IT-004.json", "environment"), ("IT-020.json", "manager")):
            self.assert_route(doc, "needs-information", "IT Service Desk", "clarify")
            r = self.cur(doc)
            self.assertIsNone(r["extraction"][field])
            self.assertTrue(any(f.startswith("conflict") for f in r["review_flags"]))
        quotes = self.cur("IT-020.json")["extraction"]["supporting_quotes"]
        self.assertIn("My manager is Priya Shah.", quotes)
        self.assertIn("The manager for this request is Elena Park.", quotes)

    def test_explicit_corrections_replace_values(self):
        self.run_all(FakeTransport(), docs={"IT-008.json", "IT-015.json"})
        self.assertEqual(self.cur("IT-008.json")["extraction"]["environment"], "staging")
        self.assertEqual(self.cur("IT-015.json")["extraction"]["start_date"], "2026-10-29")
        self.assert_route("IT-008.json", "review", "System Owner", "review-request")

    def test_embedded_instructions_are_data_and_go_to_service_desk(self):
        t = FakeTransport()
        self.run_all(t, docs={"IT-006.json"})
        self.assert_route("IT-006.json", "review", "IT Service Desk", "inspect-message")
        r = self.cur("IT-006.json")
        self.assertEqual(r["extraction"]["permission"], "read")  # not the injected "admin"
        self.assertTrue(any("SYSTEM OVERRIDE" in q for q in r["extraction"]["supporting_quotes"]))
        self.assertNotIn("approved", r["status"])
        req = t.requests[0]["body"]
        self.assertEqual(req["messages"][0]["role"], "system")
        self.assertNotIn("SYSTEM OVERRIDE", req["messages"][0]["content"])
        self.assertIn(json.dumps(load_source(self.inputs / "IT-006.json").message), req["messages"][1]["content"])

    def test_override_backstop_when_model_does_not_report_it(self):
        ext = dict(MOCKS["IT-006.json"]["extraction"], issues=[])
        self.run_all(FakeTransport({"IT-006.json": [http(completion(ext))]}), docs={"IT-006.json"})
        self.assert_route("IT-006.json", "review", "IT Service Desk", "inspect-message")

    def test_withdrawal_identifier_mismatch_other_and_date_order(self):
        self.run_all(FakeTransport(), docs={"IT-005.json", "IT-016.json", "IT-017.json", "IT-014.json"})
        self.assert_route("IT-005.json", "review", "IT Service Desk", "confirm-withdrawal")
        self.assert_route("IT-016.json", "needs-information", "IT Service Desk", "clarify")
        self.assertEqual(self.cur("IT-016.json")["extraction"]["request_id"], "IT-061")
        self.assertEqual(self.cur("IT-016.json")["document_id"], "IT-016.json")
        self.assert_route("IT-017.json", "needs-information", "IT Service Desk", "clarify")
        self.assert_route("IT-014.json", "needs-information", "IT Service Desk", "clarify")

    def test_security_review_for_production_write_admin(self):
        docs = {"IT-002.json", "IT-007.json", "IT-012.json", "IT-013.json", "IT-019.json"}
        self.run_all(FakeTransport(), docs=docs)
        for d in docs:
            self.assert_route(d, "review", "Security Review", "review-risk-then-owner")

    def test_synthetic_paraphrase(self):
        src = load_source(SYNTHETIC / "IT-021.json")
        it = self.intake(FakeTransport())
        it.run([src])
        self.assert_matches_expected("IT-021.json", group="synthetic")


class ResponseHandling(Base):
    def _one(self, responses, doc="IT-001.json", key=API_KEY):
        t = FakeTransport({doc: responses})
        it = self.intake(t, key=key)
        it.run([load_source(self.inputs / doc)])
        return self.cur(doc), t

    def assert_error(self, rec, code):
        self.assertEqual((rec["status"], rec["owner"], rec["next_action"]), ("error", "IT Service Desk", "retry-extraction"))
        self.assertIsNone(rec["extraction"])
        self.assertEqual(rec["error_detail"]["code"], code, rec["error"])
        self.assertIn("python -m accessq retry IT-001.json", rec["error"])

    def test_refusal(self):
        rec, _ = self._one([http(completion(refusal="I can't help with that"))])
        self.assert_error(rec, "refusal")

    def test_interrupted_truncated_response(self):
        rec, _ = self._one([http(completion(content='{"request_id": "IT-0', finish="length"))])
        self.assert_error(rec, "interrupted")

    def test_interrupted_missing_finish_reason(self):
        rec, _ = self._one([http(completion(MOCKS["IT-001.json"]["extraction"], finish=None))])
        self.assert_error(rec, "interrupted")

    def test_content_filter(self):
        rec, _ = self._one([http(completion(content="", finish="content_filter"))])
        self.assert_error(rec, "content_filter")

    def test_malformed_json_output(self):
        rec, _ = self._one([http(completion(content='{"request_id": "IT-001",'))])
        self.assert_error(rec, "invalid_output")

    def test_schema_violation(self):
        bad = dict(MOCKS["IT-001.json"]["extraction"], permission="superuser")
        rec, _ = self._one([http(completion(bad))])
        self.assert_error(rec, "invalid_output")
        extra = dict(MOCKS["IT-001.json"]["extraction"], approved=True)
        rec, _ = self._one([http(completion(extra))])
        self.assert_error(rec, "invalid_output")

    def test_bad_date_format(self):
        bad = dict(MOCKS["IT-001.json"]["extraction"], start_date="23/10/2026")
        rec, _ = self._one([http(completion(bad))])
        self.assert_error(rec, "invalid_output")

    def test_quote_not_in_source(self):
        bad = dict(MOCKS["IT-001.json"]["extraction"], supporting_quotes=["Maya needs admin"])
        rec, _ = self._one([http(completion(bad))])
        self.assert_error(rec, "invalid_output")

    def test_non_json_body_and_no_choices(self):
        rec, _ = self._one([http(b"<html>gateway</html>")] * 5)
        self.assert_error(rec, "malformed_response")
        rec, _ = self._one([http({"id": "x", "choices": []})])
        self.assert_error(rec, "malformed_response")

    def test_rate_limit_then_success_is_retried(self):
        rec, t = self._one([http({"error": "slow down"}, 429, {"Retry-After": "1"}), None])
        self.assertEqual(rec["status"], "review")
        self.assertEqual(t.calls.count("IT-001.json"), 2)
        self.assertEqual(rec["response_meta"]["provider_attempts"], 2)

    def test_provider_outage_exhausts_retries(self):
        rec, t = self._one([http({"error": "down"}, 503)])
        self.assert_error(rec, "server_error")
        self.assertEqual(t.calls.count("IT-001.json"), self.settings.max_retries + 1)

    def test_timeout(self):
        rec, _ = self._one([TimeoutError("timed out")])
        self.assert_error(rec, "timeout")

    def test_auth_failure_not_retried(self):
        rec, t = self._one([http({"error": "bad key"}, 401)])
        self.assert_error(rec, "auth")
        self.assertEqual(len(t.calls), 1)

    def test_missing_api_key(self):
        rec, t = self._one([None], key=None)
        self.assert_error(rec, "missing_credentials")
        self.assertEqual(t.calls, [])

    def test_provider_failure_does_not_block_other_messages(self):
        t = FakeTransport({"IT-002.json": [http({"error": "boom"}, 500)]})
        _, run = self.run_all(t)
        self.assertEqual(run["outcomes"]["IT-002.json"], "error")
        self.assertEqual(run["counts"], {"extracted": 19, "error": 1})
        self.assert_route("IT-001.json", "review", "System Owner", "review-request")
        queue = json.loads((self.out / "queue.json").read_text())
        self.assertEqual(queue["summary"]["by_status"]["error"], 1)

    def test_invalid_input_file_is_isolated(self):
        (self.inputs / "IT-099.json").write_text("{not json", encoding="utf-8")
        _, run = self.run_all(FakeTransport())
        self.assertEqual(run["outcomes"]["IT-099.json"], "error")
        self.assertEqual(self.cur("IT-099.json")["error_detail"]["code"], "input_invalid")
        self.assertEqual(run["counts"]["extracted"], 20)

    def test_credentials_are_redacted(self):
        self._one([http({"error": f"echo Bearer {API_KEY}"}, 400)])
        blob = "".join(p.read_text() for p in (self.tmp / "state").rglob("*") if p.is_file())
        blob += "".join(p.read_text() for p in self.out.rglob("*") if p.is_file())
        self.assertNotIn(API_KEY, blob)
        prov = OpenAICompatibleProvider(self.settings, API_KEY)
        self.assertNotIn(API_KEY, json.dumps(prov.redacted_request("IT-001.json", "hi")))
        self.assertIn(API_KEY, prov.build_request("IT-001.json", "hi")["headers"]["Authorization"])

    def test_request_uses_strict_json_schema_and_configured_model(self):
        _, t = self._one([None])
        body = t.requests[0]["body"]
        self.assertEqual(t.requests[0]["url"], "https://llm.example.test/v1/chat/completions")
        self.assertEqual(body["model"], "test-model")
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(body["response_format"]["type"], "json_schema")
        self.assertTrue(body["response_format"]["json_schema"]["strict"])


class VersioningAndRecovery(Base):
    def test_unchanged_rerun_reuses_results(self):
        t = FakeTransport()
        self.run_all(t)
        self.assertEqual(len(t.calls), 20)
        _, run2 = self.run_all(t)
        self.assertEqual(len(t.calls), 20, "no model calls on unchanged rerun")
        self.assertEqual(run2["counts"], {"unchanged": 20})
        lines = (self.out / "extractions.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 20)
        self.assertEqual(len({json.loads(l)["document_id"] for l in lines}), 20)
        queue = json.loads((self.out / "queue.json").read_text())
        self.assertEqual(len(queue["entries"]), 20)
        self.assertEqual(sum(len(self.store.attempts(d)) for d in EXPECTED["supplied"]), 20)

    def test_changed_source_creates_new_result_and_keeps_old(self):
        t = FakeTransport()
        self.run_all(t, docs={"IT-001.json"})
        old = self.cur("IT-001.json")
        p = self.inputs / "IT-001.json"
        obj = json.loads(p.read_text())
        obj["message"] += " Update: the dashboard owner asked me to also look at last week's totals."
        p.write_text(json.dumps(obj, indent=2), encoding="utf-8")
        _, run = self.run_all(t, docs={"IT-001.json"})
        self.assertEqual(run["outcomes"]["IT-001.json"], "extracted")
        new = self.cur("IT-001.json")
        self.assertNotEqual(new["source_sha256"], old["source_sha256"])
        hist = self.store.attempts("IT-001.json")
        self.assertEqual([h["source_sha256"] for h in hist], [old["source_sha256"], new["source_sha256"]])
        self.assertEqual(new["last_valid_attempt"]["attempt_id"], old["attempt_id"])
        self.assertTrue((self.tmp / "state" / "sources" / f"{old['source_sha256']}.json").exists())
        line = json.loads((self.out / "extractions.jsonl").read_text().splitlines()[0])
        self.assertEqual(line["source_sha256"], new["source_sha256"])

    def test_failed_retry_after_prior_success(self):
        self.run_all(FakeTransport(), docs={"IT-001.json"})
        good = self.cur("IT-001.json")
        it = self.intake(FakeTransport({"IT-001.json": [http({"error": "down"}, 503)]}))
        it.run([load_source(self.inputs / "IT-001.json")], force=True)  # `accessq retry`
        cur = self.cur("IT-001.json")
        self.assertEqual(cur["status"], "error")
        self.assertIsNone(cur["extraction"])
        self.assertEqual(cur["last_valid_attempt"]["attempt_id"], good["attempt_id"])
        self.assertEqual(cur["last_valid_attempt"]["source_sha256"], good["source_sha256"])
        hist = self.store.attempts("IT-001.json")
        self.assertEqual([h["status"] for h in hist], ["review", "error"])
        self.assertIsNotNone(hist[0]["extraction"])
        q = json.loads((self.out / "queue.json").read_text())["entries"][0]
        self.assertEqual((q["status"], q["next_action"]), ("error", "retry-extraction"))
        self.assertEqual(q["last_valid_attempt"]["attempt_id"], good["attempt_id"])
        # a later successful retry restores a valid current record
        self.intake(FakeTransport()).run([load_source(self.inputs / "IT-001.json")])
        self.assertEqual(self.cur("IT-001.json")["status"], "review")

    def test_model_config_change_refreshes(self):
        t = FakeTransport()
        self.run_all(t, docs={"IT-001.json", "IT-002.json"})
        s2 = Settings(**{**self.settings.__dict__, "model": "test-model-2"})
        _, run = self.run_all(t, docs={"IT-001.json", "IT-002.json"}, settings=s2)
        self.assertEqual(run["counts"], {"extracted": 2})
        self.assertEqual(len(t.calls), 4)
        self.assertEqual(self.cur("IT-001.json")["extraction_settings"]["model"], "test-model-2")
        # operational settings do not invalidate results
        s3 = Settings(**{**s2.__dict__, "timeout_seconds": 5, "concurrency": 1})
        _, run = self.run_all(t, docs={"IT-001.json"}, settings=s3)
        self.assertEqual(run["counts"], {"unchanged": 1})

    def test_policy_change_reroutes_without_model_call(self):
        t = FakeTransport()
        self.run_all(t, docs={"IT-001.json"})
        with mock.patch.object(policy, "policy_version", return_value="route-v1+policy-changed"):
            _, run = self.run_all(t, docs={"IT-001.json"})
        self.assertEqual(run["counts"], {"rerouted": 1})
        self.assertEqual(len(t.calls), 1)
        cur = self.cur("IT-001.json")
        self.assertEqual(cur["policy_version"], "route-v1+policy-changed")
        self.assertEqual(cur["attempt_kind"], "reroute")
        self.assertEqual(len(self.store.attempts("IT-001.json")), 2)

    def test_interrupted_run_can_be_resumed(self):
        def boom(n, payload):
            return KeyboardInterrupt() if payload["document_id"] == "IT-006.json" else None
        t = FakeTransport({d: boom for d in EXPECTED["supplied"]})
        s1 = Settings(**{**self.settings.__dict__, "concurrency": 1})
        it = self.intake(t, settings=s1)
        with self.assertRaises(KeyboardInterrupt):
            it.run(discover([self.inputs]))
        statuses = {r["document_id"]: r["status"] for r in self.store.all_current()}
        self.assertEqual(len(statuses), 20)
        self.assertEqual(statuses["IT-006.json"], "pending")
        self.assertIn("pending", set(statuses.values()))
        done_before = {d for d, s in statuses.items() if s != "pending"}
        self.assertTrue({"IT-001.json", "IT-005.json"} <= done_before)
        queue = json.loads((self.out / "queue.json").read_text())
        self.assertEqual(queue["summary"]["total"], 20)
        self.assertEqual(self.store.runs()[-1]["status"], "interrupted")
        # resume: only pending/error documents, completed work untouched
        t2 = FakeTransport()
        todo = [r["document_id"] for r in self.store.all_current() if r["status"] in ("pending", "error")]
        self.intake(t2).run([load_source(self.inputs / d) for d in todo])
        self.assertEqual(sorted(t2.calls), sorted(todo))
        self.assertFalse(set(t2.calls) & done_before)
        self.assertEqual({r["status"] for r in self.store.all_current()} - {"review", "needs-information"}, set())

    def test_inspect_completed_results_while_others_pending(self):
        gate = threading.Event()
        def slow(n, payload):
            gate.wait(5)
            return None
        t = FakeTransport({"IT-020.json": slow})
        it = self.intake(t)
        th = threading.Thread(target=it.run, args=(discover([self.inputs]),))
        th.start()
        deadline = time.time() + 10
        while time.time() < deadline:
            q = json.loads((self.out / "queue.json").read_text()) if (self.out / "queue.json").exists() else None
            if q and q["summary"]["by_status"].get("pending") == 1 and q["summary"]["total"] == 20:
                break
            time.sleep(0.05)
        gate.set()
        th.join()
        self.assertEqual(q["summary"]["by_status"].get("pending"), 1, q["summary"])
        pend = [e for e in q["entries"] if e["status"] == "pending"]
        self.assertEqual(pend[0]["document_id"], "IT-020.json")
        self.assertEqual(self.cur("IT-020.json")["status"], "needs-information")


class SharedOperation(Base):
    def test_two_operators_at_once_process_each_document_once(self):
        t = FakeTransport(delay=0.02)
        srcs = discover([self.inputs])
        a = self.intake(t, store=Store(self.tmp / "state", operator="op-asia"))
        b = self.intake(t, store=Store(self.tmp / "state", operator="op-europe"))
        runs = {}
        ths = [threading.Thread(target=lambda n, i: runs.__setitem__(n, i.run(srcs)), args=(n, i))
               for n, i in (("a", a), ("b", b))]
        [x.start() for x in ths]
        [x.join() for x in ths]
        self.assertEqual(len(t.calls), 20, "each document extracted exactly once")
        self.assertEqual(sorted(t.calls), sorted(EXPECTED["supplied"]))
        for d in EXPECTED["supplied"]:
            self.assertEqual(len(self.store.attempts(d)), 1, d)
        recs = self.store.all_current()
        self.assertEqual(len(recs), 20)
        self.assertTrue(all(r["status"] in ("review", "needs-information") for r in recs))
        render_outputs(self.store, self.out)
        self.assert_outputs_consistent()

    def test_locked_document_is_skipped_then_resumable(self):
        with self.store.lock("IT-001.json", lease_seconds=30):
            _, run = self.run_all(FakeTransport(), docs={"IT-001.json", "IT-002.json"})
        self.assertEqual(run["outcomes"]["IT-001.json"], "busy")
        self.assertEqual(run["outcomes"]["IT-002.json"], "extracted")
        _, run = self.run_all(FakeTransport(), docs={"IT-001.json"})
        self.assertEqual(run["outcomes"]["IT-001.json"], "extracted")

    def test_stale_lock_from_crashed_operator_is_broken(self):
        lock = self.tmp / "state" / "locks" / "IT-001.json.lock"
        lock.write_text(json.dumps({"token": "dead", "host": "elsewhere", "pid": 1,
                                    "acquired_epoch": time.time() - 3600}))
        _, run = self.run_all(FakeTransport(), docs={"IT-001.json"})
        self.assertEqual(run["outcomes"]["IT-001.json"], "extracted")

    def assert_outputs_consistent(self):
        import hashlib
        lines_text = (self.out / "extractions.jsonl").read_text(encoding="utf-8")
        q = json.loads((self.out / "queue.json").read_text())
        self.assertEqual(q["extractions_sha256"], hashlib.sha256(lines_text.encode()).hexdigest())
        recs = {json.loads(l)["document_id"]: json.loads(l) for l in lines_text.splitlines()}
        for e in q["entries"]:
            r = recs[e["document_id"]]
            for k in ("source_sha256", "attempt_id", "status", "owner", "next_action"):
                self.assertEqual(e[k], r[k], (e["document_id"], k))

    def test_outputs_agree_after_mixed_run(self):
        self.run_all(FakeTransport({"IT-003.json": [http({"error": "x"}, 500)]}))
        self.assert_outputs_consistent()


class MockProviderAndAccuracy(Base):
    def test_mock_provider_records_mock_source(self):
        s = Settings(provider="mock")
        res = MockProvider(s).extract("IT-001.json", load_source(REQUESTS / "IT-001.json").message)
        self.assertEqual(res.response_source, "mock")
        with self.assertRaises(ExtractionError) as cm:
            MockProvider(s).extract("IT-999.json", "x")
        self.assertEqual(cm.exception.code, "no_mock")

    def test_accuracy_counting_rules_on_known_case(self):
        exp = {"D1": EXPECTED["supplied"]["IT-001.json"], "D2": EXPECTED["supplied"]["IT-009.json"]}
        e1 = dict(MOCKS["IT-001.json"]["extraction"], team="analytics ", permission="write", end_date=None)
        e2 = dict(MOCKS["IT-009.json"]["extraction"], manager="Priya Shah")
        recs = {"D1": {"extraction": e1, "status": "review", "owner": "System Owner", "next_action": "review-request"},
                "D2": {"extraction": e2, "status": "x", "owner": "y", "next_action": "z"}}
        rep = accuracy.compare(recs, exp)
        self.assertEqual(rep["exact_fields_total"], 22)
        self.assertEqual(rep["exact_fields_matched"], 19)  # team matches after trim+casefold
        types = sorted((m["document_id"], m["field"], m["type"]) for m in rep["misses"])
        self.assertEqual(types, [("D1", "end_date", "omission"), ("D1", "permission", "wrong-value"),
                                 ("D2", "manager", "unsupported-value")])
        self.assertEqual(len(rep["factual_errors"]), 2)
        self.assertEqual(rep["route_ok"], 1)
        rep = accuracy.compare({}, {"D1": exp["D1"]})
        self.assertEqual((rep["exact_fields_matched"], rep["exact_fields_total"]), (0, 11))

    def test_handle_completion_strips_code_fences(self):
        msg = load_source(REQUESTS / "IT-001.json").message
        body = completion(content="```json\n" + json.dumps(MOCKS["IT-001.json"]["extraction"]) + "\n```")
        ext, _ = handle_completion(body, msg)
        self.assertEqual(ext["request_id"], "IT-001")


if __name__ == "__main__":
    unittest.main()
