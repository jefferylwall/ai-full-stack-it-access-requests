"""Reproduce the plan's synthetic mock workloads and record actual observations."""

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import tempfile
from threading import Event, Lock, Thread
from time import perf_counter

from .capture import capture
from .exports import export_snapshot, pin_export
from .extraction import INSTRUCTIONS, SCHEMA
from .providers import HeldResponse, MockProvider, SavedResponse
from .routing import OFFICE_POLICY
from .store import ProcessingConfig, Store
from .workers import WorkerPool


ROOT = Path(__file__).resolve().parents[1]


def _counts(rows):
    counts = Counter(row["processing_state"] for row in rows)
    return {"succeeded": counts["succeeded"], "failed": counts["failed"]}


def run_workload(count, directory, *, workers=4, provider_factory=None, observer=None):
    """Hold the first document, fail the second once, then explicitly retry it.

    Every response is derived solely by ID substitution in the saved template.
    Injected providers/observers let tests independently verify live progress and
    call bounds. The measurements are observations, never throughput targets.
    """
    if type(count) is not int or count < 2 or workers < 2:
        raise ValueError("The held-call workload needs at least two documents and workers")
    started = perf_counter()
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    database = directory / "workload.sqlite"
    if database.exists():
        raise FileExistsError("Use a fresh directory for each measured workload")
    inputs = directory / "inputs"
    inputs.mkdir()
    base = json.loads((ROOT / "tests" / "inputs" / "IT-021.json").read_text(encoding="utf-8"))
    expected = json.loads((ROOT / "tests" / "expectations" / "it-021.json").read_text(encoding="utf-8"))
    template = (ROOT / "tests" / "mock_responses" / "load-template.json").read_bytes()
    captured = []
    for number in range(1001, 1001 + count):
        identifier = f"IT-{number}"
        source = {**base, "message": base["message"].replace("IT-021", identifier)}
        path = inputs / f"{identifier}.json"
        path.write_text(json.dumps(source, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        captured.append(capture(path))
    config = ProcessingConfig(
        settings={"provider": "mock", "model": "fixture-v1", "endpoint": "offline://fixtures",
                  "prompt": INSTRUCTIONS, "schema": SCHEMA, "parameters": {"temperature": 0},
                  "extraction_version": "1", "routing_version": "1"},
        policy_bytes=OFFICE_POLICY, fixtures={"load-template.json": template},
    )
    store = Store(database)
    release, entered = Event(), Event()
    lock = Lock()
    metrics = {"active": 0, "peak": 0, "calls": 0}
    calls = Counter()

    def default_provider(claim):
        document = claim["document_id"]
        reply = SavedResponse("load-template.json", template.replace(b"{{request_id}}", Path(document).stem.encode()))
        if document == "IT-1001.json":
            return MockProvider([HeldResponse(reply, entered=entered, release=release)])
        if document == "IT-1002.json" and calls[document] == 0:
            return MockProvider([ConnectionError("scripted first workload failure")])
        return MockProvider([reply])

    def tracked_provider(claim):
        delegate = (provider_factory or default_provider)(claim)

        class Tracked:
            def complete(self, message):
                with lock:
                    calls[claim["document_id"]] += 1
                    metrics["calls"] += 1
                    metrics["active"] += 1
                    metrics["peak"] = max(metrics["peak"], metrics["active"])
                try:
                    return delegate.complete(message)
                finally:
                    with lock:
                        metrics["active"] -= 1

        return Tracked()

    incremental = False
    pool_errors, done = [], Event()

    def initial_run():
        try:
            WorkerPool(database, provider_factory=tracked_provider, workers=workers).run()
        except BaseException as error:
            pool_errors.append(error)
        finally:
            done.set()

    try:
        store.admit(captured, config)
        thread = Thread(target=initial_run, daemon=True)
        thread.start()
        try:
            while not done.wait(0.02):
                if not incremental:
                    snapshot = store.snapshot()
                    rows = {row["document_id"]: row for row in snapshot["work"]}
                    if (rows["IT-1001.json"]["processing_state"] == "running"
                            and any(row["processing_state"] == "succeeded" for row in rows.values())):
                        incremental = True
                        if observer is not None:
                            observer(snapshot)
                        if provider_factory is None:
                            release.set()
        finally:
            release.set()
            thread.join()
        if pool_errors:
            raise pool_errors[0]
        initial = store.snapshot()["extractions"]
        initial_counts = _counts(initial)
        if initial_counts != {"succeeded": count - 1, "failed": 1} or not incremental:
            raise ValueError("Measured workload did not exhibit its intended held/failure scenario")
        failed = [row for row in initial if row["processing_state"] == "failed"]
        if [row["document_id"] for row in failed] != ["IT-1002.json"]:
            raise ValueError("Unexpected failed workload document")
        preserved = {row["document_id"]: (row["attempt_id"], row["source_sha256"]) for row in initial if row["processing_state"] == "succeeded"}
        previous = failed[0]["attempt_id"]
        store.admit([store.source(previous)], store.configuration(previous), force=True)
        WorkerPool(database, provider_factory=tracked_provider, workers=workers).run()
        snapshot = store.snapshot()
        rows = {row["document_id"]: row for row in snapshot["extractions"]}
        queue = {row["document_id"]: row for row in snapshot["queue"]}
        if len(rows) != count or len(queue) != count:
            raise ValueError("Workload lost or duplicated current documents")
        for source in captured:
            row = rows[source.document_id]
            if row["source_sha256"] != source.source_sha256 or store.source(row["attempt_id"]) != source:
                raise ValueError("Workload source identity/hash changed")
            for field in ("attempt_id", "generation", "source_sha256", "fingerprint", "processing_state"):
                if row[field] != queue[source.document_id][field]:
                    raise ValueError("Incoherent workload views")
            if row["processing_state"] != "succeeded" or row["error"] is not None:
                raise ValueError("Workload recovery left failed or unfinished work")
            if (row["status"], row["owner"], row["next_action"]) != ("review", "System Owner", "review-request"):
                raise ValueError("Wrong source-grounded workload route")
            for field, wanted in expected.items():
                if field in ("supporting_quotes", "issues"):
                    continue
                wanted = Path(source.document_id).stem if field == "request_id" else wanted
                if row["extraction"][field] != wanted:
                    raise ValueError("Workload scalar differs from its frozen source expectation")
            quotes = row["extraction"]["supporting_quotes"]
            if row["extraction"]["issues"] or any(quote not in source.message for quote in quotes):
                raise ValueError("Invalid workload evidence/issues")
            if source.document_id in preserved and (row["attempt_id"], row["source_sha256"]) != preserved[source.document_id]:
                raise ValueError("Retry replaced unaffected completed work")
        export_snapshot(store, directory / "deliverables")
        exported = pin_export(directory / "deliverables").read()
        if ({row["document_id"]: (row["attempt_id"], row["source_sha256"]) for row in exported["queue"]}
                != {row["document_id"]: (row["attempt_id"], row["source_sha256"]) for row in exported["extractions"]}):
            raise ValueError("Incoherent workload export")
        manifest = {source.document_id: source.source_sha256 for source in captured}
        report = {
            "observation": "mock_workload_verification", "documents": count,
            "observed_on": datetime.now(timezone.utc).isoformat(), "python": platform.python_version(),
            "configuration": {"provider": "mock", "model": "fixture-v1", "workers": workers,
                              "request_timeout": 30, "lease_seconds": 40, "commit_margin": 2, "pace_seconds": 0},
            "initial": initial_counts, "final": _counts(rows.values()),
            "provider_calls": metrics["calls"], "peak_in_flight": metrics["peak"],
            "incremental_visible": incremental, "elapsed_seconds": perf_counter() - started,
            "fixture_sha256": hashlib.sha256(template).hexdigest(), "processing_fingerprint": config.fingerprint,
            "source_manifest_sha256": hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "recovery": {"retried_documents": ["IT-1002.json"], "preserved_documents": len(preserved)},
            "assertions_observed": {"source_identity_checks": count, "scalar_record_checks": count, "coherent_current_records": count, "coherent_export_records": count},
        }
        (directory / "observations.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        return report
    finally:
        release.set()
        store.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("verification/workload-observations.json"))
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="access-workloads-") as temporary:
        runs = [run_workload(size, Path(temporary) / str(size), workers=args.workers) for size in (400, 2000)]
    report = {"observation": "mock_workload_verification", "runs": runs,
              "limits": ["single-host SQLite on a local POSIX filesystem", "mock provider timing only; no live throughput or accuracy claim", "no performance deadline or throughput acceptance target", "recovery may repeat external calls; fencing protects current results"]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
