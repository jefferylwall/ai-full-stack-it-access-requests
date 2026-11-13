"""Test subprocess: run the real worker API with offline replies and crash gates."""

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from test_mock_provider import offline_only
from worker_support import FileGate, MOCKS, SharedClock, config, sources, write_receipt
from access_intake.providers import HeldResponse, MockProvider
from access_intake.store import Store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    parser.add_argument("--directory", required=True)
    parser.add_argument("--operator", required=True)
    parser.add_argument("--mode", choices=("after-claim", "before-commit", "overlap-old", "overlap-new"), required=True)
    args = parser.parse_args()
    directory = Path(args.directory)
    clock = SharedClock(directory / "clock.txt")

    with offline_only():
        from access_intake.workers import WorkerPool

        if args.mode.startswith("overlap"):
            store = Store(args.database, clock=clock)
            try:
                store.admit(sources(), config())
            finally:
                store.close()

        def log(event):
            # One small O_APPEND write keeps records separate across operators.
            record = (json.dumps({"operator": args.operator, **event}) + "\n").encode()
            descriptor = os.open(directory / "calls.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(descriptor, record)
            finally:
                os.close(descriptor)

        class RecordedProvider(MockProvider):
            def __init__(self, claim, script):
                super().__init__(script, clock=clock)
                self.claim = claim

            def complete(self, message):
                log({"event": "provider-call", "document_id": self.claim["document_id"], "attempt_id": self.claim["attempt_id"]})
                return super().complete(message)

        def factory(claim):
            fixture = MOCKS / claim["document_id"].lower()
            if args.mode == "overlap-old" and claim["document_id"] == "IT-001.json":
                reply = HeldResponse(fixture, entered=FileGate(directory / "old-entered"), release=FileGate(directory / "old-release"))
            else:
                reply = fixture
            return RecordedProvider(claim, [reply])

        def after_claim(claim):
            write_receipt(directory / f"{args.operator}-claim.json", {"claim": claim, "stage": "after-claim"})
            if args.mode == "after-claim":
                FileGate(directory / "crash-release").wait()

        def before_commit(claim, result):
            if args.mode == "before-commit":
                write_receipt(directory / f"{args.operator}-ready.json", {"claim": claim, "result": result, "stage": "before-commit"})
                FileGate(directory / "crash-release").wait()

        pool = WorkerPool(
            args.database, provider_factory=factory, workers=1, operator_id=args.operator,
            request_timeout=30, lease_seconds=40, commit_margin=2, clock=clock,
            after_claim=after_claim, before_commit=before_commit,
        )
        pool.run(resume=args.mode == "overlap-new")


if __name__ == "__main__":
    main()
