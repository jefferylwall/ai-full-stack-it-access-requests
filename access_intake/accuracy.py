"""Compare measured records to frozen source facts without running extraction.

Literal scalar scoring, corpus-specific evidence/issue checks, and literal array
observations are separate. This module never calls providers or review routing.
"""

from collections import Counter
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = ROOT / "accuracy" / "expectations.json"
SCHEMA = json.loads((ROOT / "snapshot.schema.json").read_text(encoding="utf-8"))
VALIDATOR = Draft202012Validator(SCHEMA)
SCALARS = tuple(name for name in SCHEMA["required"] if name not in ("issues", "supporting_quotes"))
MISSING = object()


def _hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def load_registry(path: str | Path = DEFAULT_REGISTRY) -> dict[str, Any]:
    """Validate an authored oracle against its frozen source references.

    No mock reply or measured application output is read to establish expected
    facts. A changed reference source requires an explicit oracle review.
    """
    registry = json.loads(Path(path).read_text(encoding="utf-8"))
    if registry.get("scalar_fields") != list(SCALARS) or not registry.get("entries"):
        raise ValueError("Registry must declare the twelve scalar fields and source entries")
    if _hash((ROOT / registry["policy_path"]).read_bytes()) != registry["policy_sha256"]:
        raise ValueError("Reference policy has changed; review the expectation registry")
    names = set()
    for entry in registry["entries"]:
        if entry["document_id"] in names:
            raise ValueError("Duplicate document in expectation registry")
        names.add(entry["document_id"])
        if entry["oracle_origin"] != "source-and-policy":
            raise ValueError("Expected facts must identify their source-and-policy origin")
        raw = (ROOT / entry["source_path"]).read_bytes()
        message = json.loads(raw.decode("utf-8"))["message"]
        if _hash(raw) != entry["source_sha256"] or message != entry["source_message"]:
            raise ValueError("Registry source has changed; review expected facts before comparison")
        if next(VALIDATOR.iter_errors(entry["expected"]), None) is not None:
            raise ValueError("Registry expected facts must conform to the supplied schema")
        if set(entry["fact_evidence"]) != set(SCALARS):
            raise ValueError("Every scalar fact needs an evidence entry")
        for evidence in entry["fact_evidence"].values():
            if not evidence["reason"] or any(quote not in message for quote in evidence["quotes"]):
                raise ValueError("Fact evidence must explain the fact and quote exact source text")
        if any(quote not in message for quote in entry["required_evidence"]):
            raise ValueError("Required evidence must quote the decoded source message")
    return registry


def _issue_matches(issue: str, check: dict[str, str]) -> bool:
    """Curated lexical checks for the declared corpus, not general prose judging."""
    text = issue.lower().replace("_", " ")
    field = check["field"].replace("_", " ")
    aliases = {
        "request id": ("request id", "identifier", "it-061"),
        "override": ("override", "instruction"),
    }.get(field, (field,))
    if not any(alias in text for alias in aliases):
        return False
    cues = {
        "missing": r"absent|missing|unspecified|not (specified|stated|provided|mentioned|fixed)|\bno\b.{0,30}\b(date|manager|system|permission|environment)\b",
        "relative": r"relative|unresolved|next|monday|friday",
        "conflict": r"conflict|contradic|unresolved",
        "mismatch": r"mismatch|differs|different|filename",
        "reversed_dates": r"after|revers",
        "override": r"override|instruction",
    }
    return re.search(cues[check["kind"]], text) is not None


def _provenance(row: dict[str, Any], document: str) -> dict[str, Any]:
    settings = row.get("settings")
    settings = settings if isinstance(settings, dict) else {}
    prompt, schema = settings.get("prompt"), settings.get("schema")
    return {
        "document_id": document,
        **{name: row.get(name) for name in ("source_sha256", "fingerprint", "policy_sha256", "policy_label", "fixture_hashes", "response_source")},
        "provider": settings.get("provider"), "model": settings.get("model"),
        "endpoint": settings.get("endpoint"), "parameters": settings.get("parameters"),
        "extraction_version": settings.get("extraction_version"), "routing_version": settings.get("routing_version"),
        "request_config": settings.get("request_config"),
        "prompt_sha256": _hash(prompt.encode("utf-8")) if isinstance(prompt, str) else None,
        "schema_sha256": _hash(_canonical(schema).encode("utf-8")) if isinstance(schema, dict) else None,
    }


def compare(records: Iterable[dict[str, Any]], registry: dict[str, Any]) -> dict[str, Any]:
    entries = registry["entries"]
    fields = registry["scalar_fields"]
    total = len(entries)
    report = {
        "observation": "unknown_source", "documents": total,
        "scalar": {"matches": 0, "total": total * len(fields), "by_field": {field: {"matches": 0, "total": total} for field in fields}},
        "literal_arrays": {name: {"matches": 0, "total": total} for name in ("supporting_quotes", "issues")},
        "checks": {name: {"matches": 0, "total": total} for name in ("routes", "source_hashes", "evidence", "issues")},
        "differences": [], "provenance": [], "has_errors": False,
        "reference": {
            "policy_sha256": registry["policy_sha256"],
            "registry_sha256": _hash(_canonical(registry).encode("utf-8")),
            "oracle_origin": registry["oracle_origin"],
        },
        "notes": [
            "Mock agreement is fixture verification, not live extraction accuracy.",
            "There is no passing accuracy threshold; exit status indicates detected comparison errors.",
            "Issue checks use curated lexical cues; arbitrary semantic equivalence still needs human review.",
            "Literal array order and issue prose are observations, not canonical business rules.",
            "Equivalent purpose prose still loses a literal scalar match and needs separate semantic review.",
        ],
    }

    def difference(document, field, expected, actual, category, *, error=True, **details):
        report["differences"].append({
            "document_id": document, "field": field, "expected": expected,
            "actual": None if actual is MISSING else actual,
            "actual_present": actual is not MISSING,
            "category": category, "severity": "error" if error else "observation", **details,
        })
        if error:
            report["has_errors"] = True

    rows = list(records)
    counts = Counter(row.get("document_id") for row in rows if isinstance(row, dict))
    expected_names = {entry["document_id"] for entry in entries}
    indexed = {}
    for row in rows:
        if not isinstance(row, dict):
            difference(None, "record", "object", row, "invalid_record")
            continue
        document = row.get("document_id")
        if document not in expected_names:
            difference(document, "document_id", "a registered document", document, "unexpected_document")
        elif counts[document] > 1:
            if document not in indexed:
                difference(document, "document_id", "one current record", counts[document], "duplicate_document")
                indexed[document] = {}
        else:
            indexed[document] = row
    response_sources = {row.get("response_source") for row in indexed.values()}
    if response_sources == {"mock"}:
        report["observation"] = "mock_fixture_verification"
    elif response_sources and None not in response_sources and "mock" not in response_sources:
        report["observation"] = "declared_provider_observation"
    elif response_sources:
        report["observation"] = "mixed_or_unknown_sources"

    for entry in entries:
        document = entry["document_id"]
        expected = entry["expected"]
        row = indexed.get(document, {})
        extraction = row.get("extraction")
        usable = isinstance(extraction, dict) and row.get("error") is None and row.get("processing_state", "succeeded") == "succeeded"
        report["provenance"].append(_provenance(row, document))
        for field in fields:
            actual = extraction.get(field, MISSING) if usable else MISSING
            wanted = expected[field]
            if actual is not MISSING and type(actual) is type(wanted) and actual == wanted:
                report["scalar"]["matches"] += 1
                report["scalar"]["by_field"][field]["matches"] += 1
                continue
            if not usable:
                category = "missing_extraction" if extraction is None else "invalid_extraction"
            elif actual is MISSING or actual is None:
                category = "missing_fact"
            elif field in entry["correction_fields"]:
                category = "correction_error"
            elif any(check["field"] == field and check["kind"] == "conflict" for check in entry["issue_checks"]):
                category = "unresolved_conflict"
            elif field == "business_purpose":
                category = "literal_mismatch"
            elif not isinstance(actual, str):
                category = "wrong_type"
            else:
                category = "fabricated_fact"
            difference(document, field, wanted, actual, category, semantic_review_required=field == "business_purpose")

        if usable and next(VALIDATOR.iter_errors(extraction), None) is not None:
            difference(document, "extraction", "schema-conforming object", extraction, "invalid_structure")
        checks = {
            "routes": all(row.get(field) == value for field, value in entry["route"].items()),
            "source_hashes": row.get("source_sha256") == entry["source_sha256"],
            "evidence": False, "issues": False,
        }
        if usable:
            quotes, issues = extraction.get("supporting_quotes"), extraction.get("issues")
            valid_quotes = isinstance(quotes, list) and all(isinstance(quote, str) for quote in quotes)
            checks["evidence"] = valid_quotes and (
                all(quote in entry["source_message"] for quote in quotes)
                and all(any(required in quote for quote in quotes) for required in entry["required_evidence"])
            )
            valid_issues = isinstance(issues, list) and all(isinstance(issue, str) for issue in issues)
            checks["issues"] = valid_issues and all(
                any(_issue_matches(issue, check) for issue in issues) for check in entry["issue_checks"]
            )
            for name in report["literal_arrays"]:
                actual = extraction.get(name, MISSING)
                if actual == expected[name]:
                    report["literal_arrays"][name]["matches"] += 1
                else:
                    difference(document, name, expected[name], actual, "literal_array_difference", error=False, semantic_review_required=True)
        for name, matched in checks.items():
            if matched:
                report["checks"][name]["matches"] += 1
            else:
                actual = row.get("source_sha256") if name == "source_hashes" else False
                wanted = entry["source_sha256"] if name == "source_hashes" else True
                difference(document, name, wanted, actual, "failed_" + name + "_check")
    report["scalar"]["fraction"] = report["scalar"]["matches"] / report["scalar"]["total"] if report["scalar"]["total"] else None
    return report
