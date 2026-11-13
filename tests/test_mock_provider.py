"""T01: the step 1 provider contract, using independently authored IT-001 facts.

Only tests and a saved reply are introduced at this test-first checkpoint.
The provider implementation belongs to the next checkpoint.
"""

import json
import os
import socket
import threading
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
REPLY = ROOT / "tests" / "mock_responses" / "it-001.json"


@contextmanager
def offline_only():
    """Fail on credential access or networking, including during provider import."""
    original_getitem = os._Environ.__getitem__

    def guarded_getitem(environ, key):
        if key == "API_KEY":
            raise AssertionError("T01 must not read API_KEY")
        return original_getitem(environ, key)

    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(os._Environ, "__getitem__", guarded_getitem))
        for name in ("socket", "socketpair", "create_connection", "getaddrinfo"):
            stack.enter_context(mock.patch.object(
                socket, name, side_effect=AssertionError("T01 forbids network access")
            ))
        yield


class MockProviderContract(unittest.TestCase):
    def setUp(self):
        self._guard_offline()
        self.message = json.loads(
            (ROOT / "materials" / "requests" / "IT-001.json").read_text(encoding="utf-8")
        )["message"]

    def _guard_offline(self):
        # unittest.TestCase.enterContext was added after the planned Python 3.10 minimum.
        guard = offline_only()
        guard.__enter__()
        self.addCleanup(guard.__exit__, None, None, None)

    def test_T01_saved_envelope_and_source_facts_with_injected_clock(self):
        from access_intake.providers import MockProvider

        times = iter([100.0, 125.0])
        provider = MockProvider([REPLY, REPLY], clock=lambda: next(times))
        saved = json.loads(REPLY.read_text(encoding="utf-8"))
        for _ in range(2):
            envelope = provider.complete(self.message)
            self.assertEqual(envelope, saved, "return the saved envelope verbatim")
            self.assertEqual(envelope["state"], "completed")
            # Values are from planning/plan.md's IT-001 catalogue and the message,
            # never from observed application output or the saved response.
            self.assertEqual(json.loads(envelope["output_text"]), {
                "request_id": "IT-001",
                "request_date": "2026-10-22",
                "kind": "new",
                "requester": "Maya Chen",
                "team": "Analytics",
                "system": "Atlas",
                "environment": "staging",
                "permission": "read",
                "business_purpose": "I'm checking the weekly conversion dashboard with the new event definition changes (before next reporting cycle).",
                "start_date": "2026-10-23",
                "end_date": "2026-11-06",
                "manager": "Priya Shah",
                "supporting_quotes": [
                    "Maya Chen from Analytics here, with request IT-001 dated 2026-10-22.",
                    "new read access on Atlas in staging from 2026-10-23 to 2026-11-06",
                    "My manager is Priya Shah.",
                    "I'm checking the weekly conversion dashboard with the new event definition changes (before next reporting cycle).",
                ],
                "issues": [],
            })
        self.assertEqual(provider.call_count, 2)
        self.assertEqual(provider.calls, [
            {"message": self.message, "started_at": 100.0},
            {"message": self.message, "started_at": 125.0},
        ])

    def test_T01_scripted_exception_then_saved_success(self):
        from access_intake.providers import MockProvider

        unavailable = ConnectionError("scripted provider unavailable")
        provider = MockProvider([unavailable, REPLY], clock=lambda: 200.0)
        with self.assertRaises(ConnectionError) as raised:
            provider.complete(self.message)
        self.assertIs(raised.exception, unavailable)
        self.assertEqual(provider.call_count, 1, "errors count as provider calls")
        self.assertEqual(
            provider.complete(self.message),
            json.loads(REPLY.read_text(encoding="utf-8")),
        )
        self.assertEqual(provider.call_count, 2)

    def test_T01_second_call_holds_named_reply_until_release(self):
        from access_intake.providers import HeldResponse, MockProvider

        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        replies = []
        errors = []
        provider = MockProvider(
            [REPLY, HeldResponse(REPLY, entered=entered, release=release)],
            clock=lambda: 300.0,
        )
        saved = json.loads(REPLY.read_text(encoding="utf-8"))
        self.assertEqual(provider.complete(self.message), saved)

        def complete_held_call():
            try:
                replies.append(provider.complete(self.message))
            except BaseException as exc:
                errors.append(exc)
            finally:
                finished.set()

        worker = threading.Thread(target=complete_held_call, daemon=True)
        worker.start()
        try:
            self.assertTrue(entered.wait(timeout=2), "second call must reach its barrier")
            self.assertEqual(provider.call_count, 2)
            self.assertFalse(finished.is_set(), "held response cannot complete before release")
            self.assertEqual(replies, [])
        finally:
            release.set()
            worker.join(timeout=2)
        self.assertFalse(worker.is_alive(), "release must unblock the provider")
        self.assertEqual(errors, [])
        self.assertEqual(replies, [saved])


if __name__ == "__main__":
    unittest.main()
