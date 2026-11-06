"""Review-queue routing per materials/office-policy.md (rules applied in order).

Routing is deterministic code applied to a VALIDATED extraction plus the raw
source message. The model never decides the route. POLICY_VERSION combines the
routing-code version with a hash of office-policy.md, so editing either one
makes `accessq intake` re-route existing results (without new model calls).
"""
from __future__ import annotations

import hashlib
import re

# document_id is the input filename (e.g. "IT-016.json"); identifiers stated in
# the message are compared against the filename without its .json extension.

from .config import REPO_ROOT

ROUTING_CODE_VERSION = "route-v1"
POLICY_PATH = REPO_ROOT / "materials" / "office-policy.md"

SERVICE_DESK = "IT Service Desk"
SECURITY = "Security Review"
SYSTEM_OWNER = "System Owner"

REQUIRED_FACTS = ("request_id", "request_date", "requester", "team", "system", "environment",
                  "permission", "business_purpose", "start_date", "end_date", "manager")

# Deterministic backstop for instruction-override attempts; the model is also
# asked to report them as "instruction_override:" issues.
OVERRIDE_PATTERNS = [
    r"\b(system|admin|developer)\s+override\b",
    r"\bignore\s+(all\s+|any\s+)?(previous|prior|above|earlier|your)\s+(instructions|rules|prompt)",
    r"\bdisregard\s+(all\s+|any\s+)?(previous|prior|above|earlier|your)\s+(instructions|rules)",
    r"\bbypass\s+(the\s+|all\s+)?(approval|review|security)",
    r"\b(display|reveal|print|show|output|send)\s+(me\s+)?(the\s+|your\s+)?(api|secret)[\s_-]*key",
    r"\bmark\s+(this\s+|the\s+)?request\s+as\s+approved",
    r"\bhighest[- ]priority\s+(system\s+)?instruction",
    r"\bgrant\s+\w+\s+access\s+immediately",
    r"\byou\s+are\s+now\s+(a|an|the)\b",
]
_OVERRIDE_RE = re.compile("|".join(OVERRIDE_PATTERNS), re.I)


def policy_sha256() -> str:
    return hashlib.sha256(POLICY_PATH.read_bytes()).hexdigest() if POLICY_PATH.exists() else "missing"


def policy_version() -> str:
    return f"{ROUTING_CODE_VERSION}+policy-{policy_sha256()[:12]}"


def _has_issue(extraction: dict, code: str) -> list[str]:
    return [i for i in extraction.get("issues", []) if i.strip().lower().startswith(code)]


def review_flags(document_id: str, message: str, extraction: dict) -> list[str]:
    """Application-derived flags (kept separate from the model's `issues`)."""
    flags = []
    m = _OVERRIDE_RE.search(message)
    if m or _has_issue(extraction, "instruction_override"):
        flags.append("instruction_override" + (f": detected {m.group(0)!r} in message" if m else ": reported by extraction"))
    rid = extraction.get("request_id")
    doc_stem = document_id.rsplit(".", 1)[0] if document_id.lower().endswith(".json") else document_id
    if rid and rid.strip().upper() != doc_stem.strip().upper():
        flags.append(f"id_mismatch: message states {rid}, document is {document_id}")
    elif _has_issue(extraction, "id_mismatch"):
        flags.append("id_mismatch: reported by extraction")
    if _has_issue(extraction, "conflict"):
        flags.append("conflict: " + ", ".join(i.split(":", 1)[1].split("-")[0].strip() for i in _has_issue(extraction, "conflict")))
    if extraction.get("kind") in ("new", "change"):
        missing = [f for f in REQUIRED_FACTS if extraction.get(f) in (None, "")]
        if missing:
            flags.append("missing_required: " + ", ".join(missing))
        s, e = extraction.get("start_date"), extraction.get("end_date")
        if s and e and s > e:
            flags.append(f"date_order: start_date {s} is after end_date {e}")
    elif extraction.get("kind") in (None, "other"):
        flags.append(f"kind: {extraction.get('kind')!r} is not an access request")
    return flags


def route(document_id: str, message: str, extraction: dict) -> dict:
    flags = review_flags(document_id, message, extraction)
    kinds = {f.split(":", 1)[0] for f in flags}
    kind = extraction.get("kind")
    if "instruction_override" in kinds:
        r = (1, "review", SERVICE_DESK, "inspect-message")
    elif kind == "withdrawal" and extraction.get("request_id") and extraction.get("request_date"):
        r = (2, "review", SERVICE_DESK, "confirm-withdrawal")
    elif kind in (None, "other") or kinds & {"conflict", "id_mismatch", "missing_required", "date_order"} \
            or kind == "withdrawal":
        r = (3, "needs-information", SERVICE_DESK, "clarify")
    elif extraction.get("environment") == "production" or extraction.get("permission") in ("write", "admin"):
        r = (4, "review", SECURITY, "review-risk-then-owner")
    else:
        r = (5, "review", SYSTEM_OWNER, "review-request")
    rule, status, owner, next_action = r
    return {"status": status, "owner": owner, "next_action": next_action,
            "policy_rule": rule, "review_flags": flags}


def error_route() -> dict:
    return {"status": "error", "owner": SERVICE_DESK, "next_action": "retry-extraction",
            "policy_rule": "processing-and-recovery", "review_flags": []}


def pending_route() -> dict:
    return {"status": "pending", "owner": SERVICE_DESK, "next_action": "await-extraction",
            "policy_rule": None, "review_flags": []}
