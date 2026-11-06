"""Field-level accuracy check against source-derived expected facts.

Expected facts live in tests/expected/expected_fields.json. They were written
by reading each source message and the office policy, NOT by copying
extraction output, and are never regenerated from a run.

Counting rules (see deliverables/accuracy.md):
* EXACT_FIELDS are compared one by one. A field matches when both values are
  null, or both are strings equal after trimming whitespace and case-folding.
  Enum values and dates are compared the same way (dates must be identical).
* Denominator = (#documents scored) x len(EXACT_FIELDS). A document whose
  extraction is null (error) still counts in the denominator; every field is
  a miss with type "no-extraction".
* Each miss is classified: "wrong-value" (both non-null, different),
  "unsupported-value" (expected null, extracted a value - a factual error),
  "omission" (expected value, extracted null), "no-extraction".
  Factual errors = wrong-value + unsupported-value.
* business_purpose is free text: scored separately as presence agreement
  (expected non-null <=> extracted non-null), not in the exact-match total.
* Routing (status, owner, next_action) is scored separately per document.
"""
from __future__ import annotations

import json
from pathlib import Path

from .config import REPO_ROOT

EXPECTED_PATH = REPO_ROOT / "tests" / "expected" / "expected_fields.json"
EXACT_FIELDS = ("request_id", "request_date", "kind", "requester", "team", "system", "environment",
                "permission", "start_date", "end_date", "manager")
ROUTE_FIELDS = ("status", "owner", "next_action")


def load_expected(path: Path = EXPECTED_PATH) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _norm(v):
    return v.strip().casefold() if isinstance(v, str) else v


def compare(records: dict[str, dict], expected: dict[str, dict]) -> dict:
    """records: document_id -> record with keys extraction/status/owner/next_action."""
    misses, per_doc = [], {}
    matched = total = 0
    purpose_ok = route_ok = 0
    for doc_id, exp in sorted(expected.items()):
        rec = records.get(doc_id)
        ext = (rec or {}).get("extraction")
        doc_matched = 0
        for f in EXACT_FIELDS:
            total += 1
            want = exp["fields"][f]
            if ext is None:
                misses.append({"document_id": doc_id, "field": f, "expected": want, "got": None,
                               "type": "no-extraction"})
                continue
            got = ext.get(f)
            if _norm(got) == _norm(want):
                matched += 1
                doc_matched += 1
                continue
            kind = ("omission" if got is None else "unsupported-value" if want is None else "wrong-value")
            misses.append({"document_id": doc_id, "field": f, "expected": want, "got": got, "type": kind})
        want_purpose = exp["fields"]["business_purpose_present"]
        got_purpose = bool(ext and ext.get("business_purpose"))
        purpose_ok += int(want_purpose == got_purpose)
        route_match = bool(rec) and all(rec.get(k) == exp["route"][k] for k in ROUTE_FIELDS)
        route_ok += int(route_match)
        per_doc[doc_id] = {"fields_matched": doc_matched, "fields_total": len(EXACT_FIELDS),
                           "purpose_presence_ok": want_purpose == got_purpose, "route_ok": route_match,
                           "route_got": {k: (rec or {}).get(k) for k in ROUTE_FIELDS},
                           "route_expected": exp["route"], "response_source": (rec or {}).get("response_source")}
    factual = [m for m in misses if m["type"] in ("wrong-value", "unsupported-value")]
    n = len(expected)
    return {"documents": n, "exact_fields_matched": matched, "exact_fields_total": total,
            "exact_match_rate": round(matched / total, 4) if total else None,
            "purpose_presence_ok": purpose_ok, "route_ok": route_ok, "factual_errors": factual,
            "misses": misses, "per_document": per_doc}


def format_report(title: str, rep: dict) -> str:
    lines = [f"## {title}", "",
             f"* Documents scored: {rep['documents']}",
             f"* Exact field matches: {rep['exact_fields_matched']}/{rep['exact_fields_total']}"
             f" ({rep['exact_match_rate']:.2%})" if rep["exact_fields_total"] else "* no fields",
             f"* business_purpose presence agreement: {rep['purpose_presence_ok']}/{rep['documents']}",
             f"* Routing (status, owner, next_action) matches: {rep['route_ok']}/{rep['documents']}",
             f"* Factual errors (wrong or unsupported values): {len(rep['factual_errors'])}",
             f"* All misses: {len(rep['misses'])}", ""]
    if rep["misses"]:
        lines += ["| document | field | expected | got | type |", "|---|---|---|---|---|"]
        for m in rep["misses"]:
            lines.append(f"| {m['document_id']} | {m['field']} | {m['expected']} | {m['got']} | {m['type']} |")
        lines.append("")
    bad_routes = [d for d, v in rep["per_document"].items() if not v["route_ok"]]
    if bad_routes:
        lines += ["Routing mismatches:", ""]
        for d in bad_routes:
            v = rep["per_document"][d]
            lines.append(f"* {d}: expected {v['route_expected']} got {v['route_got']}")
        lines.append("")
    return "\n".join(lines)
