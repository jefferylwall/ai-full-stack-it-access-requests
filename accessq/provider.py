"""Model providers and response handling.

* OpenAICompatibleProvider - POST {base_url}/chat/completions (OpenAI, or any
  OpenAI-compatible gateway such as Databricks Model Serving at
  https://<workspace>/serving-endpoints). Bearer token from API_KEY.
* MockProvider - returns recorded responses from mocks/<document_id>.json,
  wrapped in a chat-completion envelope and passed through the SAME response
  handler as live responses.

Every failure is raised as ExtractionError with a stable `code`, a
`retryable` flag and an actionable message.
"""
from __future__ import annotations

import json
import random
import re
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import prompt
from .config import Settings
from .validation import load_schema, validate_extraction

REDACTED = "***REDACTED***"
_BEARER_RE = re.compile(r"(Bearer\s+)[A-Za-z0-9._\-~+/=]+")
_KEYLIKE_RE = re.compile(r"\b(sk-[A-Za-z0-9_\-]{8,}|dapi[0-9a-f]{16,})\b")


def redact(text: str, secrets: list[str | None] = ()) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, REDACTED)
    text = _BEARER_RE.sub(r"\1" + REDACTED, text)
    return _KEYLIKE_RE.sub(REDACTED, text)


class ExtractionError(Exception):
    def __init__(self, code: str, message: str, retryable: bool, http_status: int | None = None,
                 provider_attempts: int = 1):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.http_status = http_status
        self.provider_attempts = provider_attempts

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "http_status": self.http_status,
            "provider_attempts": self.provider_attempts,
        }


@dataclass
class ProviderResult:
    extraction: dict
    response_source: str
    response_meta: dict = field(default_factory=dict)


# --------------------------------------------------------------------------
# Response handling shared by live and mock providers
# --------------------------------------------------------------------------

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.S)


def handle_completion(body, message: str) -> tuple[dict, dict]:
    """Validate a chat-completion body; return (extraction, meta) or raise."""
    if not isinstance(body, dict):
        raise ExtractionError("malformed_response", "Provider response is not a JSON object", True)
    if body.get("error"):
        raise ExtractionError("provider_error", f"Provider returned an error object: {str(body['error'])[:300]}", True)
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ExtractionError("malformed_response", "Provider response has no choices", True)
    choice = choices[0]
    msg = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    finish = choice.get("finish_reason")
    meta = {
        "response_id": body.get("id"),
        "response_model": body.get("model"),
        "finish_reason": finish,
        "usage": body.get("usage"),
    }
    if msg.get("refusal"):
        raise ExtractionError("refusal", f"Model refused to extract: {str(msg['refusal'])[:300]}", False)
    if finish == "content_filter":
        raise ExtractionError("content_filter", "Response was stopped by the provider content filter", False)
    if finish == "length":
        raise ExtractionError("interrupted", "Response was truncated (finish_reason=length); raise max_tokens and retry", True)
    if finish != "stop":
        raise ExtractionError("interrupted", f"Response did not complete (finish_reason={finish!r})", True)
    content = msg.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ExtractionError("malformed_response", "Completed response has empty content", True)
    m = _FENCE_RE.match(content)
    if m:
        content = m.group(1)
    try:
        extraction = json.loads(content)
    except json.JSONDecodeError as e:
        raise ExtractionError("invalid_output", f"Model output is not valid JSON: {e}", True) from None
    problems = validate_extraction(extraction, message)
    if problems:
        raise ExtractionError("invalid_output", "Model output failed validation: " + "; ".join(problems[:8]), True)
    return extraction, meta


# --------------------------------------------------------------------------
# Live provider
# --------------------------------------------------------------------------

Transport = Callable[[str, dict, bytes, float], tuple]


def urllib_transport(url: str, headers: dict, body: bytes, timeout: float) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read() or b""


class OpenAICompatibleProvider:
    name = "openai-compatible"

    def __init__(self, settings: Settings, api_key: str | None, transport: Transport | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.settings = settings
        self.api_key = api_key
        self.transport = transport or urllib_transport
        self.sleep = sleep

    @property
    def response_source(self) -> str:
        return self.settings.response_source()

    def build_request(self, document_id: str, message: str) -> dict:
        s = self.settings
        body = {
            "model": s.model,
            "messages": prompt.build_messages(document_id, message),
            "temperature": s.temperature,
            "max_tokens": s.max_tokens,
        }
        if s.response_format == "json_schema":
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "access_request_snapshot", "strict": True, "schema": load_schema()},
            }
        elif s.response_format == "json_object":
            body["response_format"] = {"type": "json_object"}
        return {
            "method": "POST",
            "url": s.base_url.rstrip("/") + "/chat/completions",
            "headers": {"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key or ''}"},
            "body": body,
        }

    def redacted_request(self, document_id: str, message: str) -> dict:
        req = self.build_request(document_id, message)
        req["headers"] = dict(req["headers"], Authorization=f"Bearer {REDACTED}")
        return req

    def _backoff(self, attempt: int, retry_after: str | None) -> float:
        s = self.settings
        if retry_after:
            try:
                return min(float(retry_after), s.backoff_max_seconds)
            except ValueError:
                pass
        delay = min(s.backoff_max_seconds, s.backoff_base_seconds * (2 ** attempt))
        return delay + random.uniform(0, s.backoff_base_seconds)

    def _send_once(self, req: dict, message: str) -> ProviderResult:
        data = json.dumps(req["body"]).encode()
        try:
            status, headers, raw = self.transport(req["url"], req["headers"], data, self.settings.timeout_seconds)
        except (TimeoutError, socket.timeout):
            raise ExtractionError("timeout", f"No response within {self.settings.timeout_seconds}s", True) from None
        except (urllib.error.URLError, OSError) as e:
            raise ExtractionError("network", redact(f"Network error: {e}", [self.api_key]), True) from None
        headers = {k.lower(): v for k, v in (headers or {}).items()}
        snippet = redact(raw[:300].decode("utf-8", "replace"), [self.api_key])
        if status == 429:
            err = ExtractionError("rate_limited", f"Provider rate limit (HTTP 429): {snippet}", True, 429)
            err.retry_after = headers.get("retry-after")
            raise err
        if status in (401, 403):
            raise ExtractionError("auth", f"Provider rejected credentials (HTTP {status}); check API_KEY", False, status)
        if status >= 500:
            err = ExtractionError("server_error", f"Provider unavailable (HTTP {status}): {snippet}", True, status)
            err.retry_after = headers.get("retry-after")
            raise err
        if status != 200:
            raise ExtractionError("bad_request", f"Provider rejected request (HTTP {status}): {snippet}", False, status)
        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ExtractionError("malformed_response", f"Provider returned non-JSON body: {snippet}", True, status) from None
        extraction, meta = handle_completion(body, message)
        return ProviderResult(extraction, self.response_source, meta)

    def extract(self, document_id: str, message: str) -> ProviderResult:
        if not self.api_key:
            raise ExtractionError("missing_credentials", "API_KEY is not set in the environment", False, provider_attempts=0)
        req = self.build_request(document_id, message)
        transport_codes = {"rate_limited", "server_error", "timeout", "network"}
        attempt = 0
        while True:
            try:
                result = self._send_once(req, message)
                result.response_meta["provider_attempts"] = attempt + 1
                return result
            except ExtractionError as e:
                e.provider_attempts = attempt + 1
                if e.code not in transport_codes or attempt >= self.settings.max_retries:
                    raise
                self.sleep(self._backoff(attempt, getattr(e, "retry_after", None)))
                attempt += 1


# --------------------------------------------------------------------------
# Mock provider
# --------------------------------------------------------------------------

class MockProvider:
    """Replays recorded responses from <mock_dir>/responses.json.

    The file maps document_id -> {"extraction": {...}} for a normal completed
    response, or
    {"completion": {...raw chat completion body...}} to replay an arbitrary
    provider response (refusal, truncated, malformed ...).
    """

    name = "mock"
    response_source = "mock"

    def __init__(self, settings: Settings, mock_dir: Path | None = None):
        self.settings = settings
        self.mock_dir = Path(mock_dir or settings.mock_path())

    def build_request(self, document_id: str, message: str) -> dict:
        return OpenAICompatibleProvider(self.settings, None).build_request(document_id, message)

    def redacted_request(self, document_id: str, message: str) -> dict:
        return OpenAICompatibleProvider(self.settings, None).redacted_request(document_id, message)

    def extract(self, document_id: str, message: str) -> ProviderResult:
        path = self.mock_dir / "responses.json"
        table = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        rec = table.get(document_id)
        if rec is None:
            raise ExtractionError("no_mock", f"No mock response recorded for {document_id} in {path}", False,
                                  provider_attempts=0)
        if "completion" in rec:
            body = rec["completion"]
        else:
            body = {
                "id": f"mock-{document_id}",
                "object": "chat.completion",
                "model": "mock",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "refusal": None,
                                         "content": json.dumps(rec["extraction"], ensure_ascii=False)}}],
            }
        extraction, meta = handle_completion(body, message)
        meta["provider_attempts"] = 1
        return ProviderResult(extraction, "mock", meta)


def make_provider(settings: Settings, api_key: str | None, transport: Transport | None = None):
    if settings.provider == "mock":
        return MockProvider(settings)
    return OpenAICompatibleProvider(settings, api_key, transport)
