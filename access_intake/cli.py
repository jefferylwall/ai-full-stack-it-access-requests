"""Operator CLI for shared intake, live inspection and coherent exports."""

import argparse
from collections import Counter
import json
from pathlib import Path
import sqlite3
from time import time
from typing import Any

from .capture import capture
from .exports import export_snapshot, pin_export
from .extraction import INSTRUCTIONS, SCHEMA
from .presentation import for_output
from .providers import MockProvider, SavedResponse
from .routing import OFFICE_POLICY, PolicyClarificationRequired
from .store import ProcessingConfig, Store
from .workers import WorkerPool


ROOT = Path(__file__).resolve().parents[1]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python3 -m access_intake", description=__doc__)
    parser.add_argument("--database", type=Path, default=Path(".access-intake.sqlite"), help="shared local SQLite database")
    parser.add_argument("--mock-dir", type=Path, default=ROOT / "tests" / "mock_responses", help="saved replies named by lowercase document filename")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--request-timeout", type=float, default=30)
    parser.add_argument("--lease-seconds", type=float, default=40)
    parser.add_argument("--commit-margin", type=float, default=2)
    parser.add_argument("--pace-seconds", type=float, default=0)
    commands = parser.add_subparsers(dest="command", required=True)
    intake = commands.add_parser("intake", help="capture files or flat directories and process pending work")
    intake.add_argument("paths", type=Path, nargs="+")
    commands.add_parser("queue", help="read the current review queue")
    commands.add_parser("work", help="read current processing states and labelled earlier results")
    history = commands.add_parser("history", help="read all attempts for a document filename")
    history.add_argument("document")
    commands.add_parser("resume", help="recover expired leases and process pending work")
    retry = commands.add_parser("retry", help="force a new attempt from captured source and settings")
    retry.add_argument("document")
    retry.add_argument("--force", action="store_true", required=True)
    export = commands.add_parser("export", help="publish or read a paired export generation")
    options = export.add_mutually_exclusive_group()
    options.add_argument("--output", type=Path, default=Path("deliverables"))
    options.add_argument("--read", type=Path, help="pin and read both files from an existing publication")
    return parser


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def _mock_config(directory: Path) -> ProcessingConfig:
    if not directory.is_dir():
        raise ValueError("Mock reply directory does not exist")
    return ProcessingConfig(
        settings={
            "provider": "mock", "model": "fixture-v1", "endpoint": "offline://fixtures",
            "prompt": INSTRUCTIONS, "schema": SCHEMA, "parameters": {"temperature": 0},
            "extraction_version": "1", "routing_version": "1",
        },
        policy_bytes=OFFICE_POLICY, policy_label="office-policy",
        fixtures={path.name: path.read_bytes() for path in sorted(directory.glob("*.json")) if path.is_file()},
    )


def main(argv=None, *, provider_factory=None, clock=time, worker_hooks=None) -> int:
    """Run the real parser; dependency seams support offline process fault tests."""
    args = _parser().parse_args(argv)
    store = None
    try:
        if args.command == "export" and args.read is not None:
            _print(pin_export(args.read).read())
            return 0
        store = Store(args.database, clock=clock)
        if args.command in ("queue", "work"):
            _print(for_output(store.snapshot()[args.command], store.path))
            return 0
        if args.command == "history":
            _print(for_output(store.history(args.document), store.path))
            return 0
        if args.command == "export":
            generation = export_snapshot(store, args.output)
            _print({"generation": str(generation)})
            return 0

        def saved_provider(claim):
            # Open in this provider thread; never share the CLI connection.
            producing_store = Store(args.database, clock=clock)
            try:
                config = producing_store.configuration(claim["attempt_id"])
            finally:
                producing_store.close()
            if not config.settings["provider"].startswith("mock"):
                raise ValueError("This provider is not implemented yet")
            identity = claim["document_id"].lower()
            return MockProvider([SavedResponse(identity, config.fixtures[identity])])

        pool = WorkerPool(
            store.path, provider_factory=provider_factory or saved_provider, clock=clock,
            workers=args.workers, request_timeout=args.request_timeout,
            lease_seconds=args.lease_seconds, commit_margin=args.commit_margin, pace_seconds=args.pace_seconds,
            **(worker_hooks or {}),
        )
        admitted = []
        if args.command == "intake":
            paths = []
            for path in args.paths:
                paths.extend(sorted(item for item in path.glob("*.json") if item.is_file()) if path.is_dir() else [path])
            captured = [capture(path) for path in paths]
            admitted = store.admit(captured, _mock_config(args.mock_dir))
        elif args.command == "retry":
            history = store.history(args.document)
            if not history:
                raise ValueError("Document has no recorded attempt to retry")
            previous_id = history[-1]["attempt_id"]
            admitted = store.admit([store.source(previous_id)], store.configuration(previous_id), force=True)
        pool.run(resume=args.command == "resume")
        snapshot = store.snapshot()
        counts = dict(Counter(row["processing_state"] for row in snapshot["work"]))
        _print(for_output({"admitted": [row["attempt_id"] for row in admitted], "counts": counts, "work": snapshot["work"]}, store.path))
        return 0 if all(row["processing_state"] == "succeeded" for row in snapshot["work"]) else 1
    except PolicyClarificationRequired:
        _print({"error": {"category": "policy_clarification", "message": "This case needs human policy clarification; inspect recorded work and the build plan."}})
        return 1
    except (ValueError, KeyError, OSError, sqlite3.Error):
        # Do not echo arbitrary source/provider/configuration data in errors.
        _print({"error": {"category": "operation_failed", "message": "Operation failed; check input paths, configuration, unfinished work and destination, then retry."}})
        return 1
    finally:
        if store is not None:
            store.close()
