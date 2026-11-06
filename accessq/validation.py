"""Validation of model output against snapshot.schema.json and the source message.

A small JSON Schema subset validator (type, enum, properties, required,
additionalProperties, items) - enough for snapshot.schema.json - so the app has
no third-party dependencies. Unsupported keywords raise, so a schema change
that needs more keywords fails loudly rather than silently passing.
"""
from __future__ import annotations

import datetime as _dt
import json
from functools import lru_cache

from .config import SCHEMA_PATH

SUPPORTED_KEYWORDS = {"type", "enum", "properties", "required", "additionalProperties", "items"}
DATE_FIELDS = ("request_date", "start_date", "end_date")

_TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "null": lambda v: v is None,
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
}


@lru_cache(maxsize=1)
def load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def validate_schema(value, schema: dict | None = None, path: str = "$") -> list[str]:
    schema = load_schema() if schema is None else schema
    errors: list[str] = []
    unsupported = set(schema) - SUPPORTED_KEYWORDS
    if unsupported:
        raise ValueError(f"schema keywords not supported by validator: {sorted(unsupported)}")
    if "type" in schema:
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_TYPES[t](value) for t in types):
            return [f"{path}: expected type {types}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} not in {schema['enum']}")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}: missing required property {key!r}")
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in props:
                    errors.append(f"{path}: unexpected property {key!r}")
        for key, sub in props.items():
            if key in value:
                errors.extend(validate_schema(value[key], sub, f"{path}.{key}"))
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            errors.extend(validate_schema(item, schema["items"], f"{path}[{i}]"))
    return errors


def validate_dates(extraction: dict) -> list[str]:
    errors = []
    for f in DATE_FIELDS:
        v = extraction.get(f)
        if v is None:
            continue
        try:
            parsed = _dt.date.fromisoformat(v)
        except (TypeError, ValueError):
            errors.append(f"$.{f}: {v!r} is not a YYYY-MM-DD date")
            continue
        if parsed.isoformat() != v:
            errors.append(f"$.{f}: {v!r} is not in YYYY-MM-DD form")
    return errors


def validate_quotes(extraction: dict, message: str) -> list[str]:
    errors = []
    for i, q in enumerate(extraction.get("supporting_quotes") or []):
        if not q.strip():
            errors.append(f"$.supporting_quotes[{i}]: empty quote")
        elif q not in message:
            errors.append(f"$.supporting_quotes[{i}]: not an exact substring of the message: {q[:80]!r}")
    return errors


def validate_extraction(extraction, message: str) -> list[str]:
    """Return a list of problems; empty means the extraction may be used."""
    errors = validate_schema(extraction)
    if errors:
        return errors
    return validate_dates(extraction) + validate_quotes(extraction, message)
