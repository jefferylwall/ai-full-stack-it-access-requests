"""Publish mock submission artifacts from one coherent reference-corpus snapshot."""

import argparse
from datetime import datetime, timezone
import hashlib
import html
import json
from pathlib import Path
import platform
import re

from .accuracy import compare, load_registry
from .capture import capture
from .exports import export_snapshot
from .extraction import build_prompt
from .openai_provider import OpenAIResponsesProvider
from .store import Store


ROOT = Path(__file__).resolve().parents[1]
TEST_COMMAND = "env -u API_KEY python3 -m unittest discover -s tests -v"


def _json(value):
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def _cell(value):
    return html.escape(str(value)).replace("|", "\\|").replace("\n", "<br>")


def _test_observation(path):
    if not path.is_file():
        return {"status": "not_recorded_yet", "command": TEST_COMMAND, "path": "planning/test-run.txt"}
    text = path.read_text(encoding="utf-8")
    counts = re.findall(r"Ran (\d+) tests? in ([\d.]+)s", text)
    return {
        "status": "passed" if text.rstrip().endswith("OK") else "failed_or_incomplete",
        "command": TEST_COMMAND, "path": "planning/test-run.txt",
        "tests": int(counts[-1][0]) if counts else None,
        "elapsed_seconds": float(counts[-1][1]) if counts else None,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _accuracy_markdown(registry, score):
    procedure = (ROOT / "docs" / "accuracy.md").read_text(encoding="utf-8")
    procedure = procedure.replace("[`accuracy/expectations.json`](../accuracy/expectations.json)", "`accuracy/expectations.json` (repository root)")
    parts = [procedure, "\n## Observed submission snapshot\n\n",
             f"Observation: `{score['observation']}`. Matched {score['scalar']['matches']}/{score['scalar']['total']} literal scalar opportunities. No live model observation is included. Full per-record provenance and separate checks are in `accuracy-report.json`.\n\n",
             "## Expected fields and exact evidence\n\nThese frozen expectations precede measured output. A null explanation refers to the whole decoded source when no positive quote exists. Policy codes are defined in `planning/plan.md`.\n"]
    for entry in registry["entries"]:
        parts.extend([
            f"\n### {entry['document_id']}\n\n",
            f"Source: `{entry['source_path']}`; SHA-256 `{entry['source_sha256']}`. Policy: {', '.join(entry['policy_rules'])}.\n\n",
            f"Expected route: `{entry['route']['status']}` / {entry['route']['owner']} / `{entry['route']['next_action']}`.\n\n",
            "| Field | Expected value | Exact evidence and reason |\n| --- | --- | --- |\n",
        ])
        for field in registry["scalar_fields"]:
            evidence = entry["fact_evidence"][field]
            quotes = " / ".join(f"“{quote}”" for quote in evidence["quotes"])
            parts.append(f"| {field} | {_cell(json.dumps(entry['expected'][field], ensure_ascii=False))} | {_cell(quotes + ' ' + evidence['reason'])} |\n")
        parts.extend(["\nFrozen literal supporting_quotes and issues (order/prose are scored separately):\n\n```json\n",
                      _json({name: entry["expected"][name] for name in ("supporting_quotes", "issues")}), "```\n"])
    return "".join(parts)


def _verification_markdown(workloads, test_run, h1, source_hashes):
    parts = [
        "# Developer verification and operating limits\n\n",
        "Human requirement validation remains separate in `planning/validation.md`; no human verdict is generated here. Capture linkage, remote default branch, push and Classroom submission require the person's evidence.\n\n",
        "## Intended assertions and reproduction\n\n",
        "Run the README's offline setup, then `env -u API_KEY python3 -m unittest discover -s tests -v`. T01–T33 check captured source, extraction validation, ordered routing, persistence, overlapping workers, interruption recovery, coherent exports, independent accuracy counting and the native provider adapter through mocked HTTP. T34 checks a clean offline installation and documented commands. T35 checks the 400/2,000-document synthetic workloads.\n\n",
        "```bash\npython3 -m access_intake --provider mock intake materials/requests tests/inputs/IT-021.json\npython3 -m access_intake.workload --output verification/workload-observations.json\npython3 -m access_intake.handover --database .access-intake.sqlite --output deliverables --workloads verification/workload-observations.json\n```\n\n",
        "The intended comparisons preserve original-byte hashes and filenames, all source-derived scalar facts, exact evidence, separate processing/review states, retained earlier attempts, and one current row per document. Security Review examines risk and forwards its recommendation to the system owner; approval is a human decision and managers are contacts.\n\n",
        "## Observed evidence\n\n",
        f"Full-suite record: `{test_run['status']}`; command `{TEST_COMMAND}`; evidence `planning/test-run.txt`. ",
        "If this generation says not_recorded_yet, it predates the saved final suite run and makes no passing claim. The final selected generation should be rebuilt after that run.\n\n",
        "`accuracy-report.json` records this generation's observed mock fixture comparison. `request-evidence.json` is an offline request preview of the implemented adapter, with redacted authorization; it is not a live inference. T31–T33 verify its transport construction and response handling with saved native replies.\n\n",
        "| Documents | Observed elapsed seconds | Peak provider calls in flight | Initial success/error | Final success/error | Provider calls |\n| --- | --- | --- | --- | --- | --- |\n",
    ]
    for run in workloads["runs"]:
        parts.append(f"| {run['documents']} | {run['elapsed_seconds']:.3f} | {run['peak_in_flight']} | {run['initial']['succeeded']}/{run['initial']['failed']} | {run['final']['succeeded']}/{run['final']['failed']} | {run['provider_calls']} |\n")
    parts.extend([
        "\nConfiguration and source/template fingerprints are in `workload-observations.json`. Its timings include source capture, processing, recovery, validation and export on the recorded runtime. A held first response left other results inspectable; the second document failed once and an explicit retry recovered it without replacing unaffected successes. These are observed mock runs, not live throughput promises. Test timeouts are hang guards, not office deadlines.\n\n",
        "## Limits and review\n\n",
        "The supported deployment is single-host SQLite on a local POSIX filesystem. Multi-host/network-filesystem operation is unverified. Each process has its own bounded pool and pacing. A timed-out underlying call keeps its slot until return; all occupied slots can leave recorded pending work for a later run. Leases must exceed request timeout plus commit margin. A crash after provider return can repeat the external call; fencing protects current results, not exactly-once external execution. Export generations and attempt history are retained without pruning. Readers must pin one generation before opening the file pair.\n\n",
        "General override recognition is not established by the supplied attack. The implementation recognizes the reviewed pattern and has a detector seam. Broader office ambiguities remain recorded in the plan. Mock agreement does not measure live extraction accuracy. Provider availability and account access have not been verified with a real credential.\n\n",
        f"Supported runtime: Python 3.12. Current artifact-generation interpreter: Python {platform.python_version()} on {platform.system()}. Setup, the suite and clean-install checks use this same installed interpreter; testing multiple Python versions is not required.\n\n",
        "H1 is a developer read against artifact requirements, not the person's validation verdict. Recorded H1 status: `" + h1.get("status", "not_recorded_yet") + "`. Its findings/repairs are in `h1-review.json`. Host setup repair: use a prepared wheel cache and offline pip bootstrap because ensurepip is absent on the observed host.\n\n",
        "## Producing source hashes\n\n```json\n", _json(source_hashes), "```\n",
    ])
    return "".join(parts)


def build_submission(store, destination, *, workloads):
    """Build all submission files before publishing one immutable generation."""
    if isinstance(workloads, (str, Path)):
        workloads = json.loads(Path(workloads).read_text(encoding="utf-8"))
    if workloads.get("observation") != "mock_workload_verification" or [run["documents"] for run in workloads["runs"]] != [400, 2000]:
        raise ValueError("Handover requires recorded 400- and 2,000-document mock observations")
    registry = load_registry()
    request = OpenAIResponsesProvider().request_evidence(json.dumps(build_prompt(capture(ROOT / "materials" / "requests" / "IT-001.json"))))
    request.update(observation="offline_request_preview", document_id="IT-001.json", live_call_performed=False)
    test_run = _test_observation(ROOT / "planning" / "test-run.txt")
    h1_path = ROOT / "verification" / "h1-review.json"
    h1 = json.loads(h1_path.read_text(encoding="utf-8")) if h1_path.is_file() else {"status": "not_recorded_yet", "actor": "developer", "human_validation": "separate"}
    source_paths = [*sorted((ROOT / "access_intake").glob("*.py")), ROOT / "config" / "extraction-prompt.txt", ROOT / "config" / "provider.json", ROOT / "snapshot.schema.json", ROOT / "requirements.lock", ROOT / "requirements-bootstrap.lock"]
    source_hashes = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in source_paths}

    def render(snapshot):
        if any(row["response_source"] != "mock" for row in snapshot["extractions"]):
            raise ValueError("This handover command publishes the declared mock corpus only")
        score = compare(snapshot["extractions"], registry)
        if score["has_errors"]:
            raise ValueError("Reference-corpus comparison has errors; repair before handover")
        return {
            "accuracy-report.json": _json(score), "accuracy.md": _accuracy_markdown(registry, score),
            "request-evidence.json": _json(request), "workload-observations.json": _json(workloads),
            "verification.md": _verification_markdown(workloads, test_run, h1, source_hashes),
            "h1-review.json": _json(h1),
            "generation-evidence.json": _json({"observation": "mock_submission_generation", "generated_on": datetime.now(timezone.utc).isoformat(), "test_run": test_run, "source_hashes": source_hashes}),
        }

    return export_snapshot(store, destination, extra_files=render)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path(".access-intake.sqlite"))
    parser.add_argument("--output", type=Path, default=Path("deliverables"))
    parser.add_argument("--workloads", type=Path, required=True)
    args = parser.parse_args(argv)
    store = Store(args.database)
    try:
        generation = build_submission(store, args.output, workloads=args.workloads)
    finally:
        store.close()
    print(_json({"generation": str(generation)}), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
