"""Capture one source revision, hashing and decoding the same original bytes."""

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path


@dataclass(frozen=True)
class CapturedSource:
    document_id: str
    raw_bytes: bytes
    source_sha256: str
    message: str


def capture(path: str | Path) -> CapturedSource:
    """Read once; later path changes cannot alter this admitted source revision.

    Invalid UTF-8, invalid JSON and non-string messages are rejected visibly.
    No extraction or queue record is fabricated for invalid input exports.
    """
    path = Path(path)
    raw = path.read_bytes()
    exported = json.loads(raw.decode("utf-8"))
    if not isinstance(exported, dict) or not isinstance(exported.get("message"), str):
        raise ValueError("Input export must contain a string message")
    return CapturedSource(
        document_id=path.name,
        raw_bytes=raw,
        source_sha256=hashlib.sha256(raw).hexdigest(),
        message=exported["message"],
    )
