"""Step 4, T15–T20: transactional attempts and source/processing provenance.

The test driver calls the persistence API directly. It is not a worker or CLI.
All extracted replies come from saved fixtures; all facts come from the plan.
"""

import copy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest

from access_intake.capture import capture
from access_intake.extraction import INSTRUCTIONS, SCHEMA, extract
from access_intake.providers import HeldResponse, MockProvider
from access_intake.routing import route
from test_mock_provider import offline_only


ROOT = Path(__file__).resolve().parents[1]
MOCKS = ROOT / "tests" / "mock_responses"
INPUTS = ROOT / "materials" / "requests"
EXPECTED = ROOT / "tests" / "expectations"
POLICY = (ROOT / "materials" / "office-policy.md").read_bytes()
OWNER = ("review", "System Owner", "review-request")
RISK = ("review", "Security Review", "review-risk-then-owner")
WITHDRAW = ("review", "IT Service Desk", "confirm-withdrawal")
ERROR = ("error", "IT Service Desk", "retry-extraction")
ORIGINAL_RULE = "5. Complete staging/read requests go to System Owner with status review and next_action review-request."
TEST_RULE = "5. Complete new/change requests go to Security Review with status review and next_action review-risk-then-owner."


class StoreContract(unittest.TestCase):
    def setUp(self):
        guard = offline_only()
        guard.__enter__()
        self.addCleanup(guard.__exit__, None, None, None)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.now = 1000.0

    def config(self, *, settings=None, policy=POLICY, fixture_names=("it-001",)):
        from access_intake.store import ProcessingConfig

        values = {
            "provider": "mock", "model": "fixture-v1", "endpoint": "offline://fixtures",
            "prompt": INSTRUCTIONS, "schema": copy.deepcopy(SCHEMA),
            "extraction_version": "1", "routing_version": "1", "parameters": {"temperature": 0},
        }
        values.update(settings or {})
        fixtures = {f"{name}.json": (MOCKS / f"{name}.json").read_bytes() for name in fixture_names}
        return ProcessingConfig(settings=values, policy_bytes=policy, policy_label="office-policy", fixtures=fixtures)

    def store(self, filename="intake.sqlite"):
        from access_intake.store import Store

        store = Store(self.directory / filename, clock=lambda: self.now)
        self.addCleanup(store.close)
        return store

    def changed_source(self):
        original = json.loads((INPUTS / "IT-001.json").read_text(encoding="utf-8"))
        self.assertEqual(original["message"].count("2026-11-06"), 1)
        original["message"] = original["message"].replace("2026-11-06", "2026-11-09")
        path = self.directory / "IT-001.json"
        path.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
        return capture(path)

    def result(self, source, config, provider):
        result = extract(
            source, provider, instructions=config.settings["prompt"], schema=config.settings["schema"],
        )
        if result["error"] is None:
            result.update(route(source, result["extraction"], policy_bytes=config.policy_bytes))
        result["response_source"] = "mock"
        return result

    def intake(self, store, sources, config, provider, *, force=False):
        admissions = store.admit(sources, config, force=force)
        for admission in admissions:
            if admission["processing_state"] != "pending":
                continue
            claim = store.start(admission["attempt_id"])
            source = store.source(claim["attempt_id"])
            producing_config = store.configuration(claim["attempt_id"])
            result = self.result(source, producing_config, provider)
            self.now += 1
            self.assertTrue(store.finish(claim["attempt_id"], claim["token"], result))
        return admissions

    def current(self, store, document="IT-001.json"):
        snapshot = store.snapshot()
        queues = {row["document_id"]: row for row in snapshot["queue"]}
        extractions = {row["document_id"]: row for row in snapshot["extractions"]}
        self.assertEqual(len(queues), len(snapshot["queue"]), "one current queue row per document")
        self.assertEqual(queues.keys(), extractions.keys())
        for name, queue in queues.items():
            extraction = extractions[name]
            for field in ("attempt_id", "generation", "source_sha256", "fingerprint", "policy_sha256", "policy_label", "processing_state"):
                self.assertEqual(queue[field], extraction[field], (name, field))
        return queues[document], extractions[document]

    def assert_route(self, row, expected):
        self.assertEqual(tuple(row[field] for field in ("status", "owner", "next_action")), expected)

    def assert_owner_result(self, store, *, end="2026-11-06"):
        queue, row = self.current(store)
        self.assert_route(queue, OWNER)
        self.assertEqual(row["processing_state"], "succeeded")
        self.assertIsNone(row["error"])
        # The plan's authored changed-source case changes only the end date.
        oracle_name = "it-001-updated" if end == "2026-11-09" else "it-001"
        expected = json.loads((EXPECTED / f"{oracle_name}.json").read_text(encoding="utf-8"))
        self.assertEqual(expected["end_date"], end)
        for field in ("request_id", "request_date", "kind", "requester", "team", "system", "environment", "permission", "business_purpose", "start_date", "end_date", "manager"):
            self.assertEqual(row["extraction"][field], expected[field], field)
        source = store.source(row["attempt_id"])
        for quote in expected["supporting_quotes"]:
            self.assertIn(quote, row["extraction"]["supporting_quotes"])
            self.assertIn(quote, source.message)
        self.assertEqual(row["response_source"], "mock")
        return row

    def test_T15_unchanged_success_reuses_attempt_and_producing_provenance(self):
        store = self.store()
        source = capture(INPUTS / "IT-001.json")
        config = self.config()
        provider = MockProvider([MOCKS / "it-001.json", AssertionError("unchanged success must not call provider")])
        first = self.intake(store, [source], config, provider)[0]
        produced = self.assert_owner_result(store)
        original_history = copy.deepcopy(store.history(source.document_id))
        self.now += 100
        second = self.intake(store, [source], config, provider)[0]
        self.assertEqual(first["attempt_id"], second["attempt_id"])
        self.assertEqual(provider.call_count, 1)
        self.assertEqual(store.history(source.document_id), original_history)
        self.assertEqual(self.assert_owner_result(store), produced)
        self.assertEqual(len(original_history), 1)
        attempt = original_history[0]
        self.assertEqual(attempt["settings"], config.settings)
        self.assertEqual(attempt["fingerprint"], config.fingerprint)
        self.assertEqual(attempt["policy_sha256"], hashlib.sha256(POLICY).hexdigest())
        self.assertEqual(attempt["created_at"], 1000.0)
        self.assertEqual(attempt["started_at"], 1000.0)
        self.assertEqual(attempt["finished_at"], 1001.0)
        # Reopening verifies committed SQLite state, rather than an in-memory cache.
        store.close()
        reopened = self.store()
        self.assertEqual(self.assert_owner_result(reopened), produced)
        self.assertEqual(reopened.history(source.document_id), original_history)
        self.assertEqual(reopened.source(first["attempt_id"]), source)

    def test_T16_changed_bytes_create_revisions_and_retain_original_source(self):
        for variant in ("new-end-date", "newline-only"):
            with self.subTest(variant=variant):
                store = self.store(f"{variant}.sqlite")
                original = capture(INPUTS / "IT-001.json")
                config = self.config(fixture_names=("it-001", "it-001-updated"))
                first = self.intake(store, [original], config, MockProvider([MOCKS / "it-001.json"]))[0]
                if variant == "new-end-date":
                    replacement = self.changed_source()
                    reply, end = "it-001-updated", "2026-11-09"
                else:
                    path = self.directory / "IT-001.json"
                    path.write_bytes(original.raw_bytes + b"\n")
                    replacement = capture(path)
                    reply, end = "it-001", "2026-11-06"
                self.now += 10
                second = store.admit([replacement], config)[0]
                queue, pending = self.current(store)
                self.assertEqual(queue["processing_state"], "pending")
                self.assertIsNone(queue["status"], "processing states are not policy statuses")
                self.assertIsNone(pending["extraction"])
                work = store.snapshot()["work"][0]
                self.assertEqual(work["attempt_id"], second["attempt_id"])
                self.assertEqual(work["earlier_result"]["attempt_id"], first["attempt_id"])
                self.assertEqual(work["earlier_result"]["extraction"]["end_date"], "2026-11-06")
                claim = store.start(second["attempt_id"])
                self.assertEqual(self.current(store)[0]["processing_state"], "running")
                result = self.result(replacement, config, MockProvider([MOCKS / f"{reply}.json"]))
                self.now += 1
                self.assertTrue(store.finish(claim["attempt_id"], claim["token"], result))
                current = self.assert_owner_result(store, end=end)
                self.assertNotEqual(first["attempt_id"], current["attempt_id"])
                self.assertEqual(current["generation"], first["generation"] + 1)
                self.assertEqual(current["source_sha256"], hashlib.sha256(replacement.raw_bytes).hexdigest())
                self.assertNotEqual(current["source_sha256"], original.source_sha256)
                history = store.history(original.document_id)
                self.assertEqual(len(history), 2)
                self.assertEqual(history[0]["extraction"]["end_date"], "2026-11-06")
                self.assertEqual(history[0]["source_sha256"], original.source_sha256)
                self.assertEqual(store.source(first["attempt_id"]), original)
                self.assertEqual(store.source(current["attempt_id"]), replacement)

    def test_T17_failed_forced_retry_is_current_without_erasing_success(self):
        for changed in (False, True):
            with self.subTest(changed=changed):
                store = self.store(f"retry-{changed}.sqlite")
                original = capture(INPUTS / "IT-001.json")
                source = self.changed_source() if changed else original
                config = self.config(fixture_names=("it-001", "it-001-updated"))
                first = self.intake(store, [original], config, MockProvider([MOCKS / "it-001.json"]))[0]
                self.now += 10
                failed = self.intake(store, [source], config, MockProvider([ConnectionError("scripted unavailable")]), force=True)[0]
                queue, row = self.current(store)
                self.assert_route(queue, ERROR)
                self.assertEqual(row["processing_state"], "failed")
                self.assertIsNone(row["extraction"])
                self.assertEqual(row["error"]["category"], "transport_failure")
                self.assertEqual(row["error"]["attempt_id"], failed["attempt_id"])
                self.assertEqual(row["error"]["source_sha256"], source.source_sha256)
                self.assertEqual(row["error"]["retry_command"], "python3 -m access_intake retry IT-001.json --force")
                self.assertNotEqual(first["attempt_id"], row["attempt_id"])
                self.assertEqual(store.snapshot()["work"][0]["earlier_result"]["attempt_id"], first["attempt_id"])
                history = store.history(original.document_id)
                self.assertEqual(len(history), 2)
                self.assertEqual(history[0]["processing_state"], "succeeded")
                self.assertEqual(history[0]["extraction"]["end_date"], "2026-11-06")
                self.assertEqual(history[0]["source_sha256"], original.source_sha256)
                # Ordinary repeated intake leaves failures visible until explicit retry.
                unused = MockProvider([AssertionError("unchanged failure needs explicit retry")])
                repeated = self.intake(store, [source], config, unused)[0]
                self.assertEqual(repeated["attempt_id"], failed["attempt_id"])
                self.assertEqual(unused.call_count, 0)
                self.assertEqual(store.history(original.document_id), history)
                reply = "it-001-updated" if changed else "it-001"
                retried = self.intake(store, [source], config, MockProvider([MOCKS / f"{reply}.json"]), force=True)[0]
                self.assertNotEqual(retried["attempt_id"], failed["attempt_id"])
                self.assert_owner_result(store, end="2026-11-09" if changed else "2026-11-06")
                self.assertEqual(len(store.history(original.document_id)), 3)

    def test_T18_omissions_retain_documents_and_shared_request_ids_do_not_merge(self):
        store = self.store()
        config = self.config(fixture_names=("it-001", "it-002", "withdrawal-minimal", "withdrawal-followup-105"))
        sources = [capture(INPUTS / f"IT-{number:03}.json") for number in (1, 2)]
        self.intake(store, sources, config, MockProvider([MOCKS / "it-001.json", MOCKS / "it-002.json"]))
        original_second = copy.deepcopy(store.history("IT-002.json"))
        unused = MockProvider([AssertionError("only an unchanged document is in this batch")])
        self.intake(store, sources[:1], config, unused)
        self.assertEqual(unused.call_count, 0)
        self.assertEqual(store.history("IT-002.json"), original_second)
        self.assert_route(self.current(store, "IT-002.json")[0], RISK)
        message = "Could you please withdraw IT-005? Date: 2026-10-22"
        withdrawals = []
        for document in ("IT-005.json", "IT-105.json"):
            path = self.directory / document
            path.write_text(json.dumps({"message": message}), encoding="utf-8")
            withdrawals.append(capture(path))
        self.intake(store, withdrawals, config, MockProvider([MOCKS / "withdrawal-minimal.json", MOCKS / "withdrawal-followup-105.json"]))
        first_queue, first = self.current(store, "IT-005.json")
        second_queue, second = self.current(store, "IT-105.json")
        for queue, row in ((first_queue, first), (second_queue, second)):
            self.assert_route(queue, WITHDRAW)
            self.assertEqual(row["extraction"]["request_id"], "IT-005")
            for field in ("requester", "team", "system", "environment", "permission", "business_purpose", "start_date", "end_date", "manager"):
                self.assertIsNone(row["extraction"][field], field)
        self.assertNotEqual(first["attempt_id"], second["attempt_id"])
        self.assertFalse(any("mismatch" in issue.lower() for issue in first["extraction"]["issues"]))
        self.assertTrue(any("mismatch" in issue.lower() for issue in second["extraction"]["issues"]))
        followup_expected = json.loads((EXPECTED / "withdrawal-followup-105.json").read_text(encoding="utf-8"))
        for field in ("request_id", "request_date", "kind", "supporting_quotes"):
            self.assertEqual(second["extraction"][field], followup_expected[field])
        self.assertEqual(len(store.snapshot()["queue"]), 4)
        # Flat-directory identity: detect a duplicate basename before any admission.
        empty = self.store("collision.sqlite")
        with self.assertRaisesRegex(ValueError, "[Dd]uplicate"):
            empty.admit([sources[0], capture(INPUTS / "IT-001.json")], config)
        self.assertEqual(empty.snapshot()["queue"], [])
        self.assertEqual(empty.history("IT-001.json"), [])

    def test_T19_relevant_settings_refresh_but_operational_settings_do_not(self):
        source = capture(INPUTS / "IT-001.json")
        config = self.config()
        test_policy = POLICY.replace(ORIGINAL_RULE.encode(), TEST_RULE.encode())
        self.assertNotEqual(test_policy, POLICY)
        self.assertIn(TEST_RULE.encode(), test_policy)
        changed_schema = copy.deepcopy(SCHEMA)
        changed_schema["description"] = "Test-only schema version 2; constraints unchanged."
        mutations = {
            "provider": replace(config, settings={**config.settings, "provider": "mock-alternate"}),
            "model": replace(config, settings={**config.settings, "model": "fixture-v2"}),
            "endpoint": replace(config, settings={**config.settings, "endpoint": "offline://alternate"}),
            "prompt": replace(config, settings={**config.settings, "prompt": INSTRUCTIONS + "\nTest prompt version 2."}),
            "schema": replace(config, settings={**config.settings, "schema": changed_schema}),
            "parameters": replace(config, settings={**config.settings, "parameters": {"temperature": 0, "max_output_tokens": 1500}}),
            "extraction-version": replace(config, settings={**config.settings, "extraction_version": "2"}),
            "routing-version": replace(config, settings={**config.settings, "routing_version": "2"}),
            "fixture-content": replace(config, fixtures={"it-001.json": (MOCKS / "it-001-settings-refresh.json").read_bytes()}),
            "fixture-identity": replace(config, fixtures={"it-001-settings-refresh.json": (MOCKS / "it-001.json").read_bytes()}),
            "policy": replace(config, policy_bytes=test_policy, policy_label="test-only-rule-5"),
        }
        for name, changed in mutations.items():
            with self.subTest(setting=name):
                store = self.store(f"settings-{name}.sqlite")
                self.intake(store, [source], config, MockProvider([MOCKS / "it-001.json"]))
                prior = copy.deepcopy(store.history(source.document_id)[0])
                provider = MockProvider([MOCKS / "it-001-settings-refresh.json" if name == "fixture-content" else MOCKS / "it-001.json"])
                admission = self.intake(store, [source], changed, provider)[0]
                self.assertEqual(provider.call_count, 1)
                current_queue, current = self.current(store)
                self.assertNotEqual(config.fingerprint, changed.fingerprint)
                self.assertNotEqual(prior["attempt_id"], admission["attempt_id"])
                self.assertEqual(current["fingerprint"], changed.fingerprint)
                self.assertEqual(current["policy_sha256"], hashlib.sha256(changed.policy_bytes).hexdigest())
                self.assertEqual(current["policy_label"], changed.policy_label)
                self.assert_route(current_queue, RISK if name == "policy" else OWNER)
                history = store.history(source.document_id)
                self.assertEqual(len(history), 2)
                for field in ("attempt_id", "settings", "fingerprint", "policy_sha256", "extraction", "created_at", "finished_at"):
                    self.assertEqual(history[0][field], prior[field], field)
                self.assertEqual(history[1]["settings"], changed.settings)
                prompt = json.loads(provider.calls[0]["message"])
                self.assertIn(changed.settings["prompt"], prompt[0]["content"])
                if name == "schema":
                    self.assertIn(changed_schema["description"], prompt[0]["content"])
                unused = MockProvider([AssertionError("same changed settings must reuse")])
                repeated = self.intake(store, [source], changed, unused)[0]
                self.assertEqual(repeated["attempt_id"], admission["attempt_id"])
                self.assertEqual(unused.call_count, 0)
        # These values are in-memory test settings, never environment credentials.
        operational = replace(config, settings={**config.settings, "credential": "test-only-in-memory-sentinel", "database_path": "/tmp/other.sqlite", "worker_count": 9, "display": "compact"})
        self.assertEqual(config.fingerprint, operational.fingerprint)
        store = self.store("operational.sqlite")
        self.intake(store, [source], operational, MockProvider([MOCKS / "it-001.json"]))
        persisted = json.dumps(store.history(source.document_id))
        self.assertNotIn("test-only-in-memory-sentinel", persisted)
        self.assertEqual(store.history(source.document_id)[0]["settings"], config.settings)

    def test_T20_late_completion_is_historical_and_cannot_replace_latest_generation(self):
        store = self.store()
        other_connection = self.store()
        original = capture(INPUTS / "IT-001.json")
        replacement = self.changed_source()
        config = self.config(fixture_names=("it-001", "it-001-updated"))
        old = store.admit([original], config)[0]
        claim = store.start(old["attempt_id"])
        entered, release = threading.Event(), threading.Event()
        results, errors = [], []
        held = MockProvider([HeldResponse(MOCKS / "it-001.json", entered=entered, release=release)])

        def finish_provider_call():
            try:
                results.append(self.result(original, config, held))
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=finish_provider_call, daemon=True)
        thread.start()
        try:
            self.assertTrue(entered.wait(timeout=2))
            self.assertEqual(other_connection.snapshot()["work"][0]["processing_state"], "running")
            # The held provider call must not hold a write transaction: a second
            # connection admits and commits the newer source before release.
            new = self.intake(other_connection, [replacement], config, MockProvider([MOCKS / "it-001-updated.json"]))[0]
            before = self.assert_owner_result(store, end="2026-11-09")
            self.assertEqual(before["attempt_id"], new["attempt_id"])
        finally:
            release.set()
            thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 1)
        self.assertFalse(store.finish(claim["attempt_id"], claim["token"], results[0]), "stale generation cannot publish current work")
        self.assertEqual(self.assert_owner_result(store, end="2026-11-09"), before)
        history = store.history(original.document_id)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["attempt_id"], old["attempt_id"])
        self.assertEqual(history[0]["processing_state"], "succeeded")
        self.assertEqual(history[0]["extraction"]["end_date"], "2026-11-06")
        self.assertEqual(history[0]["source_sha256"], original.source_sha256)
        self.assertEqual(history[1]["source_sha256"], replacement.source_sha256)
        self.assertNotEqual(history[0]["attempt_id"], history[1]["attempt_id"])


if __name__ == "__main__":
    unittest.main()
