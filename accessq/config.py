"""Configuration loading and the extraction-settings fingerprint.

Settings come from config/model.json (or the file named by ACCESSQ_CONFIG) and
can be overridden with environment variables. Credentials are only ever read
from the environment (API_KEY) and are never written to state or logs.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "model.json"
SCHEMA_PATH = REPO_ROOT / "snapshot.schema.json"

# env var name -> (config key, type)
ENV_OVERRIDES = {
    "ACCESSQ_PROVIDER": ("provider", str),
    "ACCESSQ_BASE_URL": ("base_url", str),
    "ACCESSQ_MODEL": ("model", str),
    "ACCESSQ_TEMPERATURE": ("temperature", float),
    "ACCESSQ_MAX_TOKENS": ("max_tokens", int),
    "ACCESSQ_RESPONSE_FORMAT": ("response_format", str),
    "ACCESSQ_TIMEOUT_SECONDS": ("timeout_seconds", float),
    "ACCESSQ_MAX_RETRIES": ("max_retries", int),
    "ACCESSQ_CONCURRENCY": ("concurrency", int),
    "ACCESSQ_LOCK_LEASE_SECONDS": ("lock_lease_seconds", float),
    "ACCESSQ_MOCK_DIR": ("mock_dir", str),
}

API_KEY_ENV = "API_KEY"
STATE_DIR_ENV = "ACCESSQ_STATE_DIR"
CONFIG_ENV = "ACCESSQ_CONFIG"


@dataclasses.dataclass
class Settings:
    provider: str = "openai-compatible"  # "openai-compatible" or "mock"
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4.1-mini"
    temperature: float = 0
    max_tokens: int = 1500
    response_format: str = "json_schema"  # json_schema | json_object | none
    timeout_seconds: float = 60
    max_retries: int = 4
    backoff_base_seconds: float = 2
    backoff_max_seconds: float = 60
    concurrency: int = 4
    lock_lease_seconds: float = 600
    mock_dir: str = "mocks"

    def mock_path(self) -> Path:
        p = Path(self.mock_dir)
        return p if p.is_absolute() else REPO_ROOT / p

    def response_source(self) -> str:
        if self.provider == "mock":
            return "mock"
        from urllib.parse import urlparse

        host = urlparse(self.base_url).hostname or self.base_url
        return f"{self.provider}({host})/{self.model}"

    def extraction_settings(self) -> dict:
        """Settings that change what the model is asked / how output is produced.

        Timeouts, retries and concurrency are operational and deliberately
        excluded: changing them does not invalidate earlier extractions.
        """
        from . import prompt  # local import to avoid a cycle

        s = {
            "provider": self.provider,
            "model": "mock" if self.provider == "mock" else self.model,
            "base_url": None if self.provider == "mock" else self.base_url,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "response_format": self.response_format,
            "prompt_version": prompt.PROMPT_VERSION,
            "prompt_sha256": prompt.prompt_sha256(),
            "schema_sha256": schema_sha256(),
        }
        if self.provider == "mock":
            s["mock_dir"] = str(self.mock_dir)
        return s

    def extraction_fingerprint(self) -> str:
        blob = json.dumps(self.extraction_settings(), sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()[:16]


def schema_sha256() -> str:
    return hashlib.sha256(SCHEMA_PATH.read_bytes()).hexdigest()


def load_settings(path: str | os.PathLike | None = None, env: dict | None = None) -> Settings:
    env = os.environ if env is None else env
    path = Path(path or env.get(CONFIG_ENV) or DEFAULT_CONFIG_PATH)
    data = {}
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
    known = {f.name for f in dataclasses.fields(Settings)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"Unknown settings in {path}: {sorted(unknown)}")
    for var, (key, typ) in ENV_OVERRIDES.items():
        if env.get(var):
            data[key] = typ(env[var])
    s = Settings(**data)
    if s.provider not in ("openai-compatible", "mock"):
        raise ValueError(f"Unsupported provider {s.provider!r}; use 'openai-compatible' or 'mock'")
    if s.response_format not in ("json_schema", "json_object", "none"):
        raise ValueError(f"Unsupported response_format {s.response_format!r}")
    if s.concurrency < 1:
        raise ValueError("concurrency must be >= 1")
    return s


def api_key(env: dict | None = None) -> str | None:
    env = os.environ if env is None else env
    return env.get(API_KEY_ENV) or None


def state_dir(env: dict | None = None) -> Path:
    env = os.environ if env is None else env
    return Path(env.get(STATE_DIR_ENV) or (REPO_ROOT / "state"))
