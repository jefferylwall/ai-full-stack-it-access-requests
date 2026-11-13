"""Run the real CLI parser offline, injecting only clocks/provider fault gates."""

import json
from pathlib import Path
import runpy
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from test_mock_provider import offline_only
from worker_support import FileGate, MOCKS, SharedClock, write_receipt
from access_intake.providers import HeldResponse, MockProvider


def main():
    mode, directory, *argv = sys.argv[1:]
    directory = Path(directory)
    with offline_only():
        if mode == "module":
            sys.argv = ["access_intake", *argv]
            runpy.run_module("access_intake", run_name="__main__")
            return 0

        from access_intake.cli import main as cli_main

        def factory(claim):
            fixture = MOCKS / claim["document_id"].lower()
            if mode == "held" and claim["document_id"] == "IT-001.json":
                return MockProvider([HeldResponse(fixture, entered=FileGate(directory / "entered"), release=FileGate(directory / "release"))])
            if mode == "fail-once" and claim["document_id"] == "IT-001.json":
                marker = directory / "failed-once"
                if not marker.exists():
                    marker.write_text("injected", encoding="utf-8")
                    return MockProvider([ConnectionError("scripted first failure")])
            return MockProvider([fixture])

        def after_claim(claim):
            if mode == "stop-after-claim" and claim["document_id"] == "IT-002.json":
                write_receipt(directory / "claim.json", claim)
                FileGate(directory / "release").wait()

        kwargs = {"clock": SharedClock(directory / "clock.txt")}
        if mode in ("held", "fail-once", "stop-after-claim"):
            kwargs.update(provider_factory=factory, worker_hooks={"after_claim": after_claim})
        return cli_main(argv, **kwargs)


if __name__ == "__main__":
    sys.exit(main())
