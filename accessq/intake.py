"""Intake orchestration: discover exports, extract, route, record, render outputs."""
from __future__ import annotations

import concurrent.futures as cf
import hashlib
import json
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import policy
from .config import REPO_ROOT, Settings
from .provider import ExtractionError
from .store import LockBusy, Store, atomic_write_text, utcnow

VALID_STATUSES = ("review", "needs-information")
DEFAULT_INPUT = REPO_ROOT / "materials" / "requests"
DEFAULT_OUTPUT = REPO_ROOT / "deliverables"


@dataclass
class Source:
    document_id: str
    path: Path
    raw: bytes
    sha256: str
    message: str | None
    subject: str | None
    source_channel: str | None
    exported_at: str | None
    input_error: str | None


def load_source(path: Path) -> Source:
    path = Path(path)
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    msg = subject = channel = exported = err = None
    try:
        obj = json.loads(raw.decode("utf-8"))
        if not isinstance(obj, dict):
            err = "export is not a JSON object"
        else:
            subject, channel, exported = obj.get("subject"), obj.get("source_channel"), obj.get("exported_at")
            msg = obj.get("message")
            if not isinstance(msg, str) or not msg.strip():
                err, msg = "export has no non-empty 'message' string", None
    except UnicodeDecodeError as e:
        err = f"export is not UTF-8: {e}"
    except json.JSONDecodeError as e:
        err = f"export is not valid JSON: {e}"
    return Source(path.name, path, raw, sha, msg, subject, channel, exported, err)


def discover(inputs: list[str | Path]) -> list[Source]:
    paths: list[Path] = []
    for i in inputs:
        p = Path(i)
        if p.is_dir():
            paths.extend(sorted(x for x in p.glob("*.json") if x.is_file()))
        elif p.is_file():
            paths.append(p)
        else:
            raise FileNotFoundError(f"input not found: {p}")
    seen: dict[str, Path] = {}
    for p in paths:
        if p.name in seen and seen[p.name].resolve() != p.resolve():
            raise ValueError(f"document_id collision: {p} and {seen[p.name]} share filename {p.name}")
        seen[p.name] = p
    return [load_source(p) for p in seen.values()]


def _ref(rec: dict) -> dict:
    return {k: rec.get(k) for k in ("attempt_id", "source_sha256", "processed_at", "status", "owner",
                                    "next_action", "response_source", "extraction_fingerprint",
                                    "policy_version", "history_path")}


def _display_path(p: Path) -> str:
    try:
        return str(Path(p).resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(Path(p).resolve())


class Intake:
    def __init__(self, settings: Settings, store: Store, provider, output_dir: Path | None = DEFAULT_OUTPUT,
                 run_kind: str = "intake", rebuild_interval: float = 5.0):
        self.settings = settings
        self.store = store
        self.provider = provider
        self.output_dir = Path(output_dir) if output_dir else None
        self.run_kind = run_kind
        self.rebuild_interval = rebuild_interval
        self.run_id = f"{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-{uuid.uuid4().hex[:8]}"
        self.fingerprint = settings.extraction_fingerprint()
        self.policy_version = policy.policy_version()
        self._rebuild_mutex = threading.Lock()
        self._last_rebuild = 0.0

    # ------------------------------------------------------------ helpers
    def retry_command(self, document_id: str) -> str:
        cmd = f"python -m accessq retry {document_id}"
        if self.settings.provider == "mock":
            cmd += " --provider mock"
        return cmd

    def _base(self, src: Source) -> dict:
        return {
            "document_id": src.document_id,
            "source_sha256": src.sha256,
            "source_file": src.document_id,
            "source_path": _display_path(src.path),
            "subject": src.subject,
            "source_channel": src.source_channel,
            "exported_at": src.exported_at,
        }

    def _is_current(self, cur: dict | None, src: Source) -> bool:
        return bool(cur and cur.get("status") in VALID_STATUSES and cur.get("source_sha256") == src.sha256
                    and cur.get("extraction_fingerprint") == self.fingerprint)

    @staticmethod
    def _last_valid(cur: dict | None) -> dict | None:
        if not cur:
            return None
        if cur.get("status") in VALID_STATUSES:
            return _ref(cur)
        return cur.get("last_valid_attempt")

    def _commit(self, rec: dict) -> dict:
        rec["history_path"] = self.store.append_attempt(rec)
        self.store.write_current(rec)
        return rec

    # ------------------------------------------------------------ phases
    def register(self, src: Source) -> str:
        """Mark a document pending so operators can see unfinished work."""
        try:
            with self.store.lock(src.document_id, self.settings.lock_lease_seconds):
                cur = self.store.read_current(src.document_id)
                if self._is_current(cur, src):
                    return "current"
                if cur and cur.get("source_sha256") == src.sha256 and cur.get("status") in ("pending", "error"):
                    return "already-" + cur["status"]
                rec = {**self._base(src), "extraction": None, **policy.pending_route(), "error": None,
                       "response_source": None, "request_id": None, "attempt_id": None,
                       "queued_at": utcnow(), "queued_by_run": self.run_id, "processed_at": None,
                       "extraction_fingerprint": self.fingerprint, "policy_version": self.policy_version,
                       "last_valid_attempt": self._last_valid(cur), "history_path": None}
                self.store.write_current(rec)
                return "pending"
        except LockBusy:
            return "busy"

    def process(self, src: Source, force: bool = False) -> str:
        try:
            with self.store.lock(src.document_id, self.settings.lock_lease_seconds):
                return self._process_locked(src, force)
        except LockBusy as e:
            self.store.log("document_busy", document_id=src.document_id, holder=e.holder, run_id=self.run_id)
            return "busy"

    def _process_locked(self, src: Source, force: bool) -> str:
        cur = self.store.read_current(src.document_id)
        if not force and self._is_current(cur, src):
            if cur.get("policy_version") == self.policy_version:
                return "unchanged"
            # Policy/routing changed: re-route the stored extraction, no model call.
            r = policy.route(src.document_id, src.message, cur["extraction"])
            rec = {**cur, **r, "attempt_id": uuid.uuid4().hex[:12], "attempt_kind": "reroute",
                   "processed_at": utcnow(), "run_id": self.run_id, "operator": self.store.operator,
                   "policy_version": self.policy_version, "previous_attempt": _ref(cur),
                   "extracted_by_attempt": cur.get("extracted_by_attempt") or cur.get("attempt_id")}
            self._commit(rec)
            self.store.log("rerouted", document_id=src.document_id, status=rec["status"], run_id=self.run_id)
            return "rerouted"

        snapshot = self.store.save_source(src.sha256, src.raw)
        attempt_id = uuid.uuid4().hex[:12]
        common = {**self._base(src), "source_snapshot": snapshot, "attempt_id": attempt_id,
                  "attempt_kind": "extract", "run_id": self.run_id, "operator": self.store.operator,
                  "extraction_fingerprint": self.fingerprint,
                  "extraction_settings": self.settings.extraction_settings(),
                  "policy_version": self.policy_version, "last_valid_attempt": self._last_valid(cur)}
        started = time.monotonic()
        try:
            if src.input_error:
                raise ExtractionError("input_invalid", f"{src.input_error}; correct the export file and rerun",
                                      False, provider_attempts=0)
            result = self.provider.extract(src.document_id, src.message)
            r = policy.route(src.document_id, src.message, result.extraction)
            rec = {**common, "extraction": result.extraction, **r, "error": None, "error_detail": None,
                   "response_source": result.response_source, "request_id": result.extraction.get("request_id"),
                   "response_meta": result.response_meta, "retry_command": None}
            outcome = "extracted"
        except ExtractionError as e:
            rec = {**common, "extraction": None, **policy.error_route(),
                   "error": f"{e.code}: {e.message}. Retry with: {self.retry_command(src.document_id)}",
                   "error_detail": e.as_dict(), "response_source": getattr(self.provider, "response_source", None),
                   "request_id": None, "response_meta": None, "retry_command": self.retry_command(src.document_id)}
            outcome = "error"
        rec["processed_at"] = utcnow()
        rec["duration_seconds"] = round(time.monotonic() - started, 3)
        self._commit(rec)
        self.store.log("processed", document_id=src.document_id, outcome=outcome, status=rec["status"],
                       error=rec["error"], sha256=src.sha256, attempt_id=attempt_id, run_id=self.run_id)
        return outcome

    # ------------------------------------------------------------ run
    def run(self, sources: list[Source], force: bool = False) -> dict:
        run = {"run_id": self.run_id, "kind": self.run_kind, "operator": self.store.operator,
               "started_at": utcnow(), "status": "running", "documents": len(sources),
               "extraction_fingerprint": self.fingerprint, "policy_version": self.policy_version,
               "response_source": self.settings.response_source(), "outcomes": {}}
        self.store.write_run(run)
        self.store.log("run_started", run_id=self.run_id, kind=self.run_kind, documents=len(sources))
        for src in sources:
            self.register(src)
        self.rebuild(force=True)

        outcomes: dict[str, str] = {}
        pool = cf.ThreadPoolExecutor(max_workers=self.settings.concurrency, thread_name_prefix="accessq")
        futures = {pool.submit(self.process, s, force): s for s in sources}
        try:
            for fut in cf.as_completed(futures):
                src = futures[fut]
                try:
                    outcomes[src.document_id] = fut.result()
                except Exception as e:  # unexpected bug: keep other documents moving
                    outcomes[src.document_id] = "crashed"
                    self.store.log("crashed", document_id=src.document_id, error=repr(e), run_id=self.run_id)
                self.rebuild()
            run["status"] = "completed"
        except KeyboardInterrupt:
            pool.shutdown(wait=False, cancel_futures=True)
            run["status"] = "interrupted"
            raise
        finally:
            pool.shutdown(wait=run["status"] == "completed")
            run["finished_at"] = utcnow()
            run["outcomes"] = outcomes
            counts: dict[str, int] = {}
            for o in outcomes.values():
                counts[o] = counts.get(o, 0) + 1
            run["counts"] = counts
            self.store.write_run(run)
            self.rebuild(force=True)
            self.store.log("run_finished", run_id=self.run_id, status=run["status"], counts=counts)
        return run

    def rebuild(self, force: bool = False) -> None:
        if not self.output_dir:
            return
        with self._rebuild_mutex:
            if not force and time.monotonic() - self._last_rebuild < self.rebuild_interval:
                return
            render_outputs(self.store, self.output_dir)
            self._last_rebuild = time.monotonic()


# ---------------------------------------------------------------- outputs
EXTRACTION_KEYS = ("document_id", "source_sha256", "extraction", "status", "owner", "next_action",
                   "error", "response_source")
QUEUE_KEYS = ("document_id", "request_id", "subject", "source_channel", "source_file", "source_sha256",
              "status", "owner", "next_action", "policy_rule", "review_flags", "issues", "error",
              "retry_command", "response_source", "attempt_id", "processed_at", "policy_version",
              "extraction_fingerprint", "last_valid_attempt")
STATUS_ORDER = {"error": 0, "pending": 1, "review": 2, "needs-information": 3}


def extraction_line(rec: dict) -> dict:
    out = {k: rec.get(k) for k in EXTRACTION_KEYS}
    for k in ("request_id", "source_file", "attempt_id", "attempt_kind", "processed_at", "policy_version",
              "policy_rule", "review_flags", "extraction_fingerprint", "extraction_settings", "retry_command",
              "error_detail", "last_valid_attempt", "history_path"):
        out[k] = rec.get(k)
    return out


def queue_entry(rec: dict) -> dict:
    e = {k: rec.get(k) for k in QUEUE_KEYS}
    e["issues"] = (rec.get("extraction") or {}).get("issues", [])
    return e


def render_outputs(store: Store, output_dir: Path) -> dict:
    output_dir = Path(output_dir)
    with store.lock("deliverables", lease_seconds=120, wait=True, timeout=120):
        recs = store.all_current()
        lines = "".join(json.dumps(extraction_line(r), ensure_ascii=False) + "\n" for r in recs)
        by_status: dict[str, int] = {}
        by_owner: dict[str, int] = {}
        for r in recs:
            by_status[r["status"]] = by_status.get(r["status"], 0) + 1
            by_owner[r["owner"]] = by_owner.get(r["owner"], 0) + 1
        queue = {
            "generated_at": utcnow(),
            "policy_version": policy.policy_version(),
            "extractions_file": "extractions.jsonl",
            "extractions_sha256": hashlib.sha256(lines.encode("utf-8")).hexdigest(),
            "summary": {"total": len(recs), "by_status": by_status, "by_owner": by_owner},
            "entries": [queue_entry(r) for r in recs],
        }
        atomic_write_text(output_dir / "extractions.jsonl", lines)
        atomic_write_text(output_dir / "queue.json", json.dumps(queue, indent=2, ensure_ascii=False) + "\n")
        return queue
