"""File-based state store shared by operators.

Layout under the state directory (default ./state, env ACCESSQ_STATE_DIR):

  current/<document_id>.json        current record per document (atomic replace)
  attempts/<document_id>/<ts>-<attempt_id>.json
                                    immutable history of every attempt (write-once)
  sources/<sha256>.json             original source bytes for every processed revision
  locks/<name>.lock                 lease locks (O_CREAT|O_EXCL)
  runs/<run_id>.json                one record per intake/resume/retry run
  logs/intake.log                   credential-redacted JSONL operational log

Current outputs (deliverables/extractions.jsonl, deliverables/queue.json) are
rendered from current/ only; prior versions live in attempts/ and sources/.
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import json
import os
import re
import socket
import threading
import time
import uuid
from pathlib import Path

from .provider import redact


def utcnow() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def atomic_write_json(path: Path, obj) -> None:
    atomic_write_text(path, json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


class LockBusy(Exception):
    def __init__(self, name: str, holder: dict | None):
        super().__init__(f"{name} is locked by {holder}")
        self.name = name
        self.holder = holder


class Store:
    def __init__(self, root: Path, operator: str | None = None):
        self.root = Path(root)
        for d in ("current", "attempts", "sources", "locks", "runs", "logs"):
            (self.root / d).mkdir(parents=True, exist_ok=True)
        self.operator = operator or os.environ.get("ACCESSQ_OPERATOR") or _default_operator()
        self._log_lock = threading.Lock()
        self.secrets: list[str] = []

    # ------------------------------------------------------------------ locks
    @contextlib.contextmanager
    def lock(self, name: str, lease_seconds: float, wait: bool = False, timeout: float = 60):
        path = self.root / "locks" / f"{_safe(name)}.lock"
        token = uuid.uuid4().hex
        deadline = time.monotonic() + timeout
        while True:
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                holder = _read_json(path)
                acquired = (holder or {}).get("acquired_epoch")
                if acquired is None:
                    try:
                        acquired = path.stat().st_mtime
                    except FileNotFoundError:
                        continue
                if time.time() - acquired > lease_seconds or _holder_dead(holder):
                    # Stale lease (holder crashed / was stopped). Rename is atomic,
                    # so exactly one contender breaks it.
                    with contextlib.suppress(FileNotFoundError):
                        os.rename(path, path.with_name(f"{path.name}.stale.{token}"))
                        self.log("lock_broken", lock=name, previous_holder=holder)
                    continue
                if not wait or time.monotonic() > deadline:
                    raise LockBusy(name, holder) from None
                time.sleep(0.05)
                continue
            with os.fdopen(fd, "w") as f:
                json.dump({"token": token, "operator": self.operator, "pid": os.getpid(),
                           "host": socket.gethostname(), "acquired_at": utcnow(),
                           "acquired_epoch": time.time()}, f)
            break
        try:
            yield
        finally:
            holder = _read_json(path)
            if holder and holder.get("token") == token:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(path)

    # ---------------------------------------------------------------- records
    def current_path(self, document_id: str) -> Path:
        return self.root / "current" / f"{_safe(document_id)}.json"

    def read_current(self, document_id: str) -> dict | None:
        return _read_json(self.current_path(document_id))

    def write_current(self, record: dict) -> None:
        atomic_write_json(self.current_path(record["document_id"]), record)

    def all_current(self) -> list[dict]:
        out = []
        for p in sorted((self.root / "current").glob("*.json")):
            rec = _read_json(p)
            if rec:
                out.append(rec)
        return sorted(out, key=lambda r: r["document_id"])

    def append_attempt(self, record: dict) -> str:
        d = self.root / "attempts" / _safe(record["document_id"])
        d.mkdir(parents=True, exist_ok=True)
        stamp = record["processed_at"].replace(":", "").replace("-", "").replace(".", "")
        path = d / f"{stamp}-{record['attempt_id']}.json"
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)  # write-once
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(record, f, indent=2, ensure_ascii=False)
            f.write("\n")
        return str(path.relative_to(self.root))

    def attempts(self, document_id: str) -> list[dict]:
        d = self.root / "attempts" / _safe(document_id)
        return [r for r in (_read_json(p) for p in sorted(d.glob("*.json"))) if r]

    def save_source(self, sha256: str, raw: bytes) -> str:
        path = self.root / "sources" / f"{sha256}.json"
        if not path.exists():
            tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            tmp.write_bytes(raw)
            os.replace(tmp, path)
        return str(path.relative_to(self.root))

    # ------------------------------------------------------------------- runs
    def write_run(self, run: dict) -> None:
        atomic_write_json(self.root / "runs" / f"{run['run_id']}.json", run)

    def runs(self) -> list[dict]:
        return sorted((r for r in (_read_json(p) for p in (self.root / "runs").glob("*.json")) if r),
                      key=lambda r: r.get("started_at", ""))

    # -------------------------------------------------------------------- log
    def log(self, event: str, **fields) -> None:
        line = json.dumps({"ts": utcnow(), "event": event, "operator": self.operator, **fields},
                          ensure_ascii=False, default=str)
        line = redact(line, self.secrets)
        with self._log_lock, open(self.root / "logs" / "intake.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")


def _read_json(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _holder_dead(holder: dict | None) -> bool:
    """True when the lock holder ran on this host and its process no longer exists."""
    if not holder or holder.get("host") != socket.gethostname() or not holder.get("pid"):
        return False
    if holder["pid"] == os.getpid():
        return False
    try:
        os.kill(int(holder["pid"]), 0)
    except ProcessLookupError:
        return True
    except (PermissionError, OSError):
        return False
    return False


def _default_operator() -> str:
    try:
        import getpass

        return f"{getpass.getuser()}@{socket.gethostname()}"
    except Exception:  # pragma: no cover
        return f"pid{os.getpid()}@{socket.gethostname()}"
