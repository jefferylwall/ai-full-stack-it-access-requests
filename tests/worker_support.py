"""Offline fixture configuration and a file-backed clock shared by test processes."""

import json
from pathlib import Path
from time import monotonic, sleep

from access_intake.capture import capture
from access_intake.extraction import INSTRUCTIONS, SCHEMA, extract
from access_intake.routing import route
from access_intake.store import ProcessingConfig


ROOT = Path(__file__).resolve().parents[1]
MOCKS = ROOT / "tests" / "mock_responses"
INPUTS = ROOT / "materials" / "requests"


def config():
    return ProcessingConfig(
        settings={
            "provider": "mock", "model": "fixture-v1", "endpoint": "offline://fixtures",
            "prompt": INSTRUCTIONS, "schema": SCHEMA, "parameters": {"temperature": 0},
            "extraction_version": "1", "routing_version": "1",
        },
        policy_bytes=(ROOT / "materials" / "office-policy.md").read_bytes(),
        policy_label="office-policy",
        fixtures={f"it-{number:03}.json": (MOCKS / f"it-{number:03}.json").read_bytes() for number in (1, 2)},
    )


def sources():
    return [capture(INPUTS / f"IT-{number:03}.json") for number in (1, 2)]


class SharedClock:
    def __init__(self, path):
        self.path = Path(path)

    def __call__(self):
        return float(self.path.read_text(encoding="utf-8"))

    def set(self, value):
        staged = self.path.with_suffix(".next")
        staged.write_text(str(value), encoding="utf-8")
        staged.replace(self.path)


def write_receipt(path, value):
    path = Path(path)
    staged = path.with_suffix(".next")
    staged.write_text(json.dumps(value), encoding="utf-8")
    staged.replace(path)


class FileGate:
    """Event-compatible test barrier; uses local files rather than sockets."""

    def __init__(self, path):
        self.path = Path(path)

    def set(self):
        self.path.write_text("ready", encoding="utf-8")

    def wait(self, timeout=15):
        deadline = monotonic() + timeout
        while not self.path.exists():
            if monotonic() >= deadline:
                raise TimeoutError("Test barrier was not released")
            sleep(0.01)
        return True


def saved_result(source, provider):
    result = extract(source, provider)
    if result["error"] is None:
        result.update(route(source, result["extraction"]))
    result["response_source"] = "mock"
    return result
