"""Controlled export publisher/paired reader; all data was produced with mocks."""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from test_mock_provider import offline_only
from worker_support import FileGate, write_receipt
from access_intake.store import Store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("reader", "publisher"), required=True)
    parser.add_argument("--database")
    parser.add_argument("--destination", required=True)
    parser.add_argument("--directory", required=True)
    parser.add_argument("--stage", choices=("before", "after"))
    args = parser.parse_args()
    directory = Path(args.directory)
    with offline_only():
        from access_intake.exports import export_snapshot, pin_export

        if args.mode == "reader":
            pinned = pin_export(args.destination)
            # Pin once, then deliberately span a publication between file reads.
            queue = json.loads((pinned.generation / "queue.json").read_text(encoding="utf-8"))
            write_receipt(directory / "reader-ready.json", {"generation": str(pinned.generation)})
            FileGate(directory / "reader-release").wait()
            lines = (pinned.generation / "extractions.jsonl").read_text(encoding="utf-8").splitlines()
            extractions = [json.loads(line) for line in lines]
            write_receipt(directory / "reader-result.json", {
                "generation": str(pinned.generation), "queue": queue, "extractions": extractions,
            })
            return

        def stop(generation):
            write_receipt(directory / "publisher-ready.json", {"generation": str(generation)})
            FileGate(directory / "publisher-release").wait()

        store = Store(args.database)
        try:
            hooks = {"before_publish" if args.stage == "before" else "after_publish": stop}
            export_snapshot(store, args.destination, **hooks)
        finally:
            store.close()


if __name__ == "__main__":
    main()
