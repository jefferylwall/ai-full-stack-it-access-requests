"""Read-only local submission checks; external release evidence stays unverified."""

import argparse
import json
from pathlib import Path

from .exports import pin_export


REQUIRED = (
    "README.md", "requirements.lock", "requirements-bootstrap.lock",
    "access_intake/__main__.py", "config/extraction-prompt.txt",
    "snapshot.schema.json", "config/provider.json",
    "deliverables/extractions.jsonl", "deliverables/queue.json",
    "deliverables/accuracy.md", "deliverables/request-evidence.json",
    "deliverables/verification.md",
)


def preflight(root):
    """Inspect the selected generation once without credentials, Git or writes.

    ``ready`` means the local artifact checks passed. It says nothing about a
    person's validation, commit capture, remote contents or Classroom receipt.
    This checks the documented mock bundle; it never contacts a provider.
    """
    root = Path(root)
    missing, issues = [], []

    def issue(category, path, message):
        issues.append({"category": category, "path": path, "message": message})

    try:
        generation = pin_export(root / "deliverables").generation
    except (OSError, RuntimeError, ValueError):
        generation = None
        issue("link", "deliverables", "Publish a complete generation using export or handover.")

    def locate(name):
        if name.startswith("deliverables/"):
            return generation / name.split("/", 1)[1] if generation else None
        return root / name

    for name in REQUIRED:
        path = locate(name)
        if path is None or not path.is_file():
            missing.append(name)
        else:
            try:
                if not path.read_text(encoding="utf-8").strip():
                    issue("artifact", name, "Required artifact is empty.")
            except (OSError, UnicodeError):
                issue("artifact", name, "Required artifact is not readable UTF-8 text.")

    def read_json(name, *, lines=False):
        path = locate(name)
        if name in missing or path is None:
            return None
        try:
            text = path.read_text(encoding="utf-8")
            return [json.loads(line) for line in text.splitlines() if line.strip()] if lines else json.loads(text)
        except (OSError, UnicodeError, ValueError):
            issue("artifact", name, "Artifact must contain valid JSON.")
            return None

    extractions = read_json("deliverables/extractions.jsonl", lines=True)
    queue = read_json("deliverables/queue.json")

    def index(rows, name):
        if rows is None:
            return None
        if (not isinstance(rows, list) or not rows
                or any(not isinstance(row, dict) or not isinstance(row.get("document_id"), str) for row in rows)):
            issue("link", name, "Expected a nonempty list of records identified by document_id.")
            return None
        indexed = {row["document_id"]: row for row in rows}
        if len(indexed) != len(rows):
            issue("link", name, "Duplicate document identities are not current records.")
        return indexed

    records = index(extractions, "deliverables/extractions.jsonl")
    queued = index(queue, "deliverables/queue.json")
    if records is not None and queued is not None:
        fields = ("attempt_id", "generation", "source_sha256", "fingerprint", "processing_state",
                  "status", "owner", "next_action", "response_source", "error")
        if records.keys() != queued.keys() or any(
            any(field not in records[key] or field not in queued[key]
                or records[key][field] != queued[key][field] for field in fields)
            for key in records.keys() & queued.keys()
        ):
            issue("link", "deliverables/queue.json", "Queue and extraction identities, provenance and review results must agree.")

    config = read_json("config/provider.json")
    schema = read_json("snapshot.schema.json")
    evidence = read_json("deliverables/request-evidence.json")
    if evidence is not None:
        try:
            request = evidence["request"]
            body = request["body"]
            declared = evidence["configuration"]
            structured = body["text"]["format"]
            if not isinstance(config, dict) or not isinstance(schema, dict):
                raise ValueError("Configuration or schema unavailable")
            matches = (
                request["method"] == "POST" and request["url"] == config["endpoint"]
                and body["model"] == config["model"] == declared["model"]
                and declared["provider"] == config["provider"]
                and declared["parameters"] == config["parameters"]
                and all(body[key] == value for key, value in config["parameters"].items())
                and all(body[key] == config[key] for key in
                        ("stream", "background", "store", "truncation", "tools", "tool_choice"))
                and structured["schema"] == schema and structured["strict"] == config["strict"]
                and structured["name"] == config["schema_name"]
            )
            prompt = locate("config/extraction-prompt.txt")
            if prompt is not None and prompt.is_file():
                matches = matches and body["input"][0]["content"].startswith(prompt.read_text(encoding="utf-8"))
            if not matches:
                issue("configuration", "deliverables/request-evidence.json", "Request must agree with the documented provider, settings, prompt and schema.")
            if request["headers"].get("Authorization") != "Bearer [REDACTED]":
                issue("configuration", "deliverables/request-evidence.json", "Request authorization must use the redacted placeholder.")
        except (KeyError, TypeError, IndexError, AttributeError, ValueError, OSError, UnicodeError):
            issue("configuration", "deliverables/request-evidence.json", "Request configuration is incomplete or malformed.")

        if (not isinstance(evidence, dict) or evidence.get("observation") != "offline_request_preview"
                or evidence.get("live_call_performed") is not False):
            issue("observation", "deliverables/request-evidence.json", "The documented offline preview must explicitly declare no live call.")
    if records is not None and any(
        row.get("response_source") != "mock" or row.get("processing_state") != "succeeded"
        or row.get("extraction") is None or row.get("error") is not None
        for row in records.values()
    ):
        issue("observation", "deliverables/extractions.jsonl", "The documented mock submission needs completed mock records with successful extractions.")

    return {
        "ready": not missing and not issues,
        "missing_artifacts": missing, "issues": issues,
        "external_checks": {name: "unverified" for name in ("capture", "default_branch", "push", "classroom")},
        "human_validation": "unverified",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    report = preflight(args.root)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
