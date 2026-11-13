"""SQLite source revisions, processing attempts and coherent current views.

One Store owns one connection in its calling thread. Multiple processes or
threads use separate Store instances against the same local database. Provider
calls happen between start() and finish(), outside every database transaction.
"""

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import shlex
import sqlite3
from time import time
from typing import Any, Callable, Iterable
from uuid import uuid4

from .capture import CapturedSource
from .extraction import validate_completion
from .routing import route


PROCESSING_KEYS = (
    "provider", "model", "endpoint", "prompt", "schema", "extraction_version",
    "routing_version", "parameters",
)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True)
class ProcessingConfig:
    settings: dict[str, Any]
    policy_bytes: bytes
    fixtures: dict[str, bytes]
    policy_label: str = "office-policy"

    def __post_init__(self) -> None:
        # Detach nested dictionaries from their caller before admission.
        object.__setattr__(self, "settings", json.loads(_json(self.settings)))
        object.__setattr__(self, "policy_bytes", bytes(self.policy_bytes))
        object.__setattr__(self, "fixtures", {name: bytes(raw) for name, raw in self.fixtures.items()})

    @property
    def processing_settings(self) -> dict[str, Any]:
        """Allowlisted producing settings; credentials/operational knobs stay out."""
        return {**{name: self.settings[name] for name in PROCESSING_KEYS},
                **({"request_config": self.settings["request_config"]} if "request_config" in self.settings else {})}

    @property
    def policy_sha256(self) -> str:
        return _sha256(self.policy_bytes)

    @property
    def fixture_hashes(self) -> dict[str, str]:
        return {name: _sha256(raw) for name, raw in self.fixtures.items()}

    @property
    def fingerprint(self) -> str:
        return _sha256(_json({
            "settings": self.processing_settings,
            "policy_sha256": self.policy_sha256,
            "fixtures": self.fixture_hashes,
        }).encode("utf-8"))


TABLES = """
CREATE TABLE IF NOT EXISTS configurations (
    fingerprint TEXT PRIMARY KEY,
    settings_json TEXT NOT NULL,
    policy_bytes BLOB NOT NULL,
    policy_sha256 TEXT NOT NULL,
    policy_label TEXT NOT NULL,
    fixture_hashes_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fixtures (
    fingerprint TEXT NOT NULL REFERENCES configurations(fingerprint),
    identity TEXT NOT NULL,
    raw_bytes BLOB NOT NULL,
    PRIMARY KEY (fingerprint, identity)
);
CREATE TABLE IF NOT EXISTS documents (
    document_id TEXT PRIMARY KEY,
    generation INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS revisions (
    document_id TEXT NOT NULL REFERENCES documents(document_id),
    source_sha256 TEXT NOT NULL,
    raw_bytes BLOB NOT NULL,
    message TEXT NOT NULL,
    PRIMARY KEY (document_id, source_sha256)
);
CREATE TABLE IF NOT EXISTS attempts (
    attempt_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    source_sha256 TEXT NOT NULL,
    fingerprint TEXT NOT NULL REFERENCES configurations(fingerprint),
    processing_state TEXT NOT NULL CHECK (
        processing_state IN ('pending', 'running', 'succeeded', 'failed', 'interrupted')
    ),
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    token TEXT,
    worker_id TEXT,
    lease_expires_at REAL,
    result_json TEXT,
    UNIQUE (document_id, generation),
    FOREIGN KEY (document_id, source_sha256) REFERENCES revisions(document_id, source_sha256)
);
"""

RECORD_QUERY = """
SELECT a.*, c.settings_json, c.policy_sha256, c.policy_label, c.fixture_hashes_json
FROM attempts a JOIN configurations c ON c.fingerprint = a.fingerprint
"""


class Store:
    def __init__(self, path: str | Path, *, clock: Callable[[], float] = time) -> None:
        self.clock = clock
        self._closed = False
        self.path = Path(path).resolve()
        self._db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.execute("PRAGMA synchronous = FULL")
        self._db.executescript(TABLES)
        # Upgrade databases created before leases were introduced. Serialize
        # inspection and ALTER so concurrent openers cannot migrate twice.
        with self._transaction(write=True):
            columns = {row["name"] for row in self._db.execute("PRAGMA table_info(attempts)")}
            for name, sql_type in (("worker_id", "TEXT"), ("lease_expires_at", "REAL")):
                if name not in columns:
                    self._db.execute(f"ALTER TABLE attempts ADD COLUMN {name} {sql_type}")

    def close(self) -> None:
        if not self._closed:
            self._db.close()
            self._closed = True

    @contextmanager
    def _transaction(self, *, write: bool = False):
        self._db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        try:
            yield
            self._db.commit()
        except BaseException:
            self._db.rollback()
            raise

    def _record(self, row: sqlite3.Row) -> dict[str, Any]:
        record = {name: row[name] for name in (
            "attempt_id", "document_id", "generation", "source_sha256", "fingerprint",
            "policy_sha256", "policy_label", "processing_state", "created_at",
            "started_at", "finished_at",
            "worker_id", "lease_expires_at",
        )}
        record["settings"] = json.loads(row["settings_json"])
        record["fixture_hashes"] = json.loads(row["fixture_hashes_json"])
        result = json.loads(row["result_json"]) if row["result_json"] is not None else {}
        for name in ("extraction", "error", "response_source", "status", "owner", "next_action"):
            record[name] = result.get(name)
        return record

    def _attempt(self, attempt_id: str) -> sqlite3.Row:
        row = self._db.execute(RECORD_QUERY + " WHERE a.attempt_id = ?", (attempt_id,)).fetchone()
        if row is None:
            raise KeyError(attempt_id)
        return row

    def source(self, attempt_id: str) -> CapturedSource:
        row = self._db.execute("""
            SELECT r.* FROM revisions r JOIN attempts a
              ON a.document_id = r.document_id AND a.source_sha256 = r.source_sha256
            WHERE a.attempt_id = ?
        """, (attempt_id,)).fetchone()
        if row is None:
            raise KeyError(attempt_id)
        return CapturedSource(row["document_id"], row["raw_bytes"], row["source_sha256"], row["message"])

    def configuration(self, attempt_id: str) -> ProcessingConfig:
        row = self._db.execute("""
            SELECT c.* FROM configurations c JOIN attempts a ON a.fingerprint = c.fingerprint
            WHERE a.attempt_id = ?
        """, (attempt_id,)).fetchone()
        if row is None:
            raise KeyError(attempt_id)
        fixtures = self._db.execute(
            "SELECT identity, raw_bytes FROM fixtures WHERE fingerprint = ?", (row["fingerprint"],),
        ).fetchall()
        return ProcessingConfig(
            settings=json.loads(row["settings_json"]), policy_bytes=row["policy_bytes"],
            policy_label=row["policy_label"],
            fixtures={fixture["identity"]: fixture["raw_bytes"] for fixture in fixtures},
        )

    def _save_config(self, config: ProcessingConfig) -> None:
        fingerprint = config.fingerprint
        self._db.execute("""
            INSERT OR IGNORE INTO configurations VALUES (?, ?, ?, ?, ?, ?)
        """, (
            fingerprint, _json(config.processing_settings), config.policy_bytes,
            config.policy_sha256, config.policy_label, _json(config.fixture_hashes),
        ))
        self._db.executemany("INSERT OR IGNORE INTO fixtures VALUES (?, ?, ?)", [
            (fingerprint, identity, raw) for identity, raw in config.fixtures.items()
        ])

    def admit(
        self, sources: Iterable[CapturedSource], config: ProcessingConfig, *, force: bool = False,
    ) -> list[dict[str, Any]]:
        sources = list(sources)
        names = [source.document_id for source in sources]
        if len(names) != len(set(names)):
            raise ValueError("Duplicate document basenames in intake")
        for source in sources:
            if Path(source.document_id).name != source.document_id or not source.document_id.endswith(".json"):
                raise ValueError("Document identity must be a flat JSON filename")
            if (_sha256(source.raw_bytes) != source.source_sha256
                    or json.loads(source.raw_bytes.decode("utf-8"))["message"] != source.message):
                raise ValueError("Captured source bytes, hash and message disagree")

        fingerprint = config.fingerprint
        requested = set(names)
        admissions = []
        with self._transaction(write=True):
            self._save_config(config)
            # Conservatively refresh omitted documents too when extraction identity
            # changes. Omission itself never deletes or withdraws a document.
            refresh = self._db.execute("""
                SELECT a.attempt_id, a.document_id FROM attempts a JOIN documents d
                  ON a.document_id = d.document_id AND a.generation = d.generation
                WHERE a.fingerprint != ? ORDER BY a.document_id
            """, (fingerprint,)).fetchall()
            sources.extend(self.source(row["attempt_id"]) for row in refresh if row["document_id"] not in requested)
            for source in sources:
                latest = self._db.execute(RECORD_QUERY + """
                    JOIN documents d ON d.document_id = a.document_id AND d.generation = a.generation
                    WHERE a.document_id = ?
                """, (source.document_id,)).fetchone()
                forced = force and source.document_id in requested
                if (latest is not None and not forced
                        and latest["source_sha256"] == source.source_sha256
                        and latest["fingerprint"] == fingerprint):
                    # Reuse successes; leave unchanged failures visible, and keep
                    # in-flight admissions unique until explicit force or recovery.
                    admissions.append(self._record(latest))
                    continue
                generation = 1 if latest is None else latest["generation"] + 1
                self._db.execute("""
                    INSERT INTO documents VALUES (?, ?)
                    ON CONFLICT(document_id) DO UPDATE SET generation = excluded.generation
                """, (source.document_id, generation))
                self._db.execute("INSERT OR IGNORE INTO revisions VALUES (?, ?, ?, ?)", (
                    source.document_id, source.source_sha256, source.raw_bytes, source.message,
                ))
                attempt_id = uuid4().hex
                self._db.execute("""
                    INSERT INTO attempts (
                        attempt_id, document_id, generation, source_sha256, fingerprint,
                        processing_state, created_at
                    ) VALUES (?, ?, ?, ?, ?, 'pending', ?)
                """, (attempt_id, source.document_id, generation, source.source_sha256, fingerprint, self.clock()))
                admissions.append(self._record(self._attempt(attempt_id)))
        return admissions

    def _start_attempt(
        self, attempt: sqlite3.Row, worker_id: str | None, lease_seconds: float | None,
    ) -> dict[str, Any]:
        token = uuid4().hex
        now = self.clock()
        expires = None if lease_seconds is None else now + lease_seconds
        self._db.execute("""
            UPDATE attempts SET processing_state = 'running', started_at = ?, token = ?,
                worker_id = ?, lease_expires_at = ? WHERE attempt_id = ?
        """, (now, token, worker_id, expires, attempt["attempt_id"]))
        claim = self._record(self._attempt(attempt["attempt_id"]))
        claim["token"] = token
        return claim

    def start(
        self, attempt_id: str, *, worker_id: str | None = None, lease_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Claim one current pending attempt and commit before any provider call."""
        if lease_seconds is not None and (not math.isfinite(lease_seconds) or lease_seconds <= 0):
            raise ValueError("Lease must be finite and positive")
        with self._transaction(write=True):
            attempt = self._attempt(attempt_id)
            current = self._db.execute(
                "SELECT generation FROM documents WHERE document_id = ?", (attempt["document_id"],),
            ).fetchone()["generation"]
            if attempt["processing_state"] != "pending" or attempt["generation"] != current:
                raise ValueError("Attempt is not current pending work")
            claim = self._start_attempt(attempt, worker_id, lease_seconds)
        return claim

    def has_pending(self) -> bool:
        return self._db.execute("""
            SELECT 1 FROM attempts a JOIN documents d
              ON a.document_id = d.document_id AND a.generation = d.generation
            WHERE a.processing_state = 'pending' LIMIT 1
        """).fetchone() is not None

    def claim(self, *, worker_id: str, lease_seconds: float) -> dict[str, Any] | None:
        """Select and claim one pending current document in a single transaction."""
        if not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError("Lease must be finite and positive")
        with self._transaction(write=True):
            attempt = self._db.execute(RECORD_QUERY + """
                JOIN documents d ON d.document_id = a.document_id AND d.generation = a.generation
                WHERE a.processing_state = 'pending' ORDER BY a.document_id LIMIT 1
            """).fetchone()
            if attempt is None:
                return None
            return self._start_attempt(attempt, worker_id, lease_seconds)

    def recover(self) -> list[dict[str, Any]]:
        """Record expired work as interrupted and admit fresh current attempts.

        Completed/failed attempts and unexpired leases are left alone. All
        recovery changes commit together, so competing resumptions cannot create
        duplicate current recovery work. Captured bytes/configuration are reused.
        """
        recovered = []
        with self._transaction(write=True):
            now = self.clock()
            expired = self._db.execute("""
                SELECT * FROM attempts WHERE processing_state = 'running'
                  AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?
            """, (now,)).fetchall()
            for attempt in expired:
                result = {
                    "status": "error", "owner": "IT Service Desk", "next_action": "retry-extraction",
                    "extraction": None, "response_source": None,
                    "error": {
                        "category": "interrupted", "message": "Worker lease expired before completion; resume recorded work.",
                        "attempt_id": attempt["attempt_id"], "document_id": attempt["document_id"],
                        "source_sha256": attempt["source_sha256"],
                        "retry_command": "python3 -m access_intake retry " + shlex.quote(attempt["document_id"]) + " --force",
                    },
                }
                self._db.execute("""
                    UPDATE attempts SET processing_state = 'interrupted', finished_at = ?, result_json = ?
                    WHERE attempt_id = ?
                """, (now, _json(result), attempt["attempt_id"]))
            current_interrupted = self._db.execute("""
                SELECT a.* FROM attempts a JOIN documents d
                  ON a.document_id = d.document_id AND a.generation = d.generation
                WHERE a.processing_state = 'interrupted' ORDER BY a.document_id
            """).fetchall()
            for previous in current_interrupted:
                attempt_id = uuid4().hex
                generation = previous["generation"] + 1
                self._db.execute("UPDATE documents SET generation = ? WHERE document_id = ?", (generation, previous["document_id"]))
                self._db.execute("""
                    INSERT INTO attempts (attempt_id, document_id, generation, source_sha256,
                        fingerprint, processing_state, created_at)
                    VALUES (?, ?, ?, ?, ?, 'pending', ?)
                """, (attempt_id, previous["document_id"], generation, previous["source_sha256"], previous["fingerprint"], now))
                recovered.append(self._record(self._attempt(attempt_id)))
        return recovered

    def finish(self, attempt_id: str, token: str, result: dict[str, Any]) -> bool:
        """Retain a completion once; return whether it belongs to current work.

        A matching old-generation token may finish its own historical attempt,
        but cannot alter the latest admitted generation. Repeated/wrong tokens
        cannot modify terminal results. An expired lease cannot finish even if
        recovery has not run yet, and can never publish after being reclaimed.
        """
        with self._transaction(write=True):
            attempt = self._attempt(attempt_id)
            if attempt["processing_state"] != "running" or attempt["token"] != token:
                return False
            if attempt["lease_expires_at"] is not None and self.clock() >= attempt["lease_expires_at"]:
                return False
            source = self.source(attempt_id)
            if result.get("document_id") != source.document_id or result.get("source_sha256") != source.source_sha256:
                raise ValueError("Completion does not match its captured source")
            config = self.configuration(attempt_id)
            error = result.get("error")
            if error is None:
                extraction = validate_completion(
                    {"state": "completed", "output_text": _json(result.get("extraction"))},
                    source.message, schema=config.settings["schema"],
                )
                decision = route(source, extraction, policy_bytes=config.policy_bytes)
                state = "succeeded"
            else:
                if result.get("extraction") is not None:
                    raise ValueError("Failed completion must have a null extraction")
                # Retain the safe extraction error fields, not raw provider bodies.
                error = {name: error[name] for name in ("category", "message", "retry_command")}
                error.update(attempt_id=attempt_id, document_id=source.document_id, source_sha256=source.source_sha256)
                extraction = None
                decision = {"status": "error", "owner": "IT Service Desk", "next_action": "retry-extraction"}
                state = "failed"
            saved = {
                **decision, "extraction": extraction, "error": error,
                "response_source": result.get("response_source"),
            }
            self._db.execute("""
                UPDATE attempts SET processing_state = ?, result_json = ?, finished_at = ?
                WHERE attempt_id = ?
            """, (state, _json(saved), self.clock(), attempt_id))
            generation = self._db.execute(
                "SELECT generation FROM documents WHERE document_id = ?", (source.document_id,),
            ).fetchone()["generation"]
            return attempt["generation"] == generation

    def history(self, document_id: str) -> list[dict[str, Any]]:
        rows = self._db.execute(
            RECORD_QUERY + " WHERE a.document_id = ? ORDER BY a.generation", (document_id,),
        ).fetchall()
        return [self._record(row) for row in rows]

    def snapshot(self) -> dict[str, list[dict[str, Any]]]:
        """Materialize current queue, extraction and work views in one read snapshot."""
        with self._transaction():
            rows = self._db.execute(RECORD_QUERY + """
                JOIN documents d ON d.document_id = a.document_id AND d.generation = a.generation
                ORDER BY a.document_id
            """).fetchall()
            records = [self._record(row) for row in rows]
            work = []
            for record in records:
                earlier = None
                if record["processing_state"] != "succeeded":
                    earlier_row = self._db.execute(RECORD_QUERY + """
                        WHERE a.document_id = ? AND a.generation < ? AND a.processing_state = 'succeeded'
                        ORDER BY a.generation DESC LIMIT 1
                    """, (record["document_id"], record["generation"])).fetchone()
                    if earlier_row is not None:
                        earlier = self._record(earlier_row)
                work.append({**record, "earlier_result": earlier})
            return {
                "queue": [{name: value for name, value in record.items() if name != "extraction"} for record in records],
                "extractions": records,
                "work": work,
            }
