"""Configurable OpenAI Responses adapter with a replaceable HTTP opener.

Protocol research is recorded in docs/provider-protocol.json. Credentials are
loaded at send time, used only in authorization headers, and never retained in
request evidence, provider diagnostics or completion envelopes.
"""

import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

from .providers import ProviderError


ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = json.loads((ROOT / "config" / "provider.json").read_text(encoding="utf-8"))
PROTOCOL = json.loads((ROOT / "docs" / "provider-protocol.json").read_text(encoding="utf-8"))
REQUEST_KEYS = ("stream", "background", "store", "truncation", "tools", "tool_choice", "schema_name", "strict")
RESPONSE_HANDLING = {
    "completed": "Require completed response/messages, then validate output JSON, schema and exact evidence.",
    "refusal": "Return refused without using accompanying extraction text.",
    "interruption": "Discard incomplete/cancelled/in-progress output, including valid-looking JSON.",
    "invalid_envelope": "Reject malformed native JSON, missing status, unsupported output or absent text.",
    "invalid_json": "Local extraction validation rejects malformed completed output text.",
    "invalid_schema": "Local extraction validation enforces the supplied schema before routing.",
    "provider_error": "Reject HTTP/API errors with an allowlisted explanation; discard raw error bodies and URLs.",
    "timeout": "Use finite I/O and worker waits; expose failure without automatic retries.",
    "rate_limit": "HTTP 429 becomes an actionable rate-limit failure with explicit retry.",
    "unavailable": "HTTP 5xx becomes an actionable availability failure with explicit retry.",
}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def validate_endpoint(endpoint):
    parsed = urlsplit(endpoint)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment):
        raise ValueError("Provider endpoint must be HTTPS without URL credentials, query or fragment")
    return endpoint


def validate_parameters(parameters):
    if not isinstance(parameters, dict) or set(parameters) - {"max_output_tokens", "reasoning", "temperature", "top_p"}:
        raise ValueError("Unsupported provider request parameters")
    if "max_output_tokens" in parameters:
        value = parameters["max_output_tokens"]
        if type(value) is not int or value <= 0:
            raise ValueError("max_output_tokens must be a positive integer")
    for name, maximum in (("temperature", 2), ("top_p", 1)):
        if name in parameters:
            value = parameters[name]
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= maximum:
                raise ValueError("Invalid sampling parameter")
    if "reasoning" in parameters:
        reasoning = parameters["reasoning"]
        if (not isinstance(reasoning, dict) or set(reasoning) != {"effort"}
                or reasoning["effort"] not in ("none", "minimal", "low", "medium", "high", "xhigh", "max")):
            raise ValueError("Unsupported reasoning settings")
    return json.loads(_json(parameters))


def read_api_key():
    """The only real credential accessor; called only for an explicitly live send."""
    key = os.environ.get("API_KEY")
    if not isinstance(key, str) or not key.strip():
        raise ProviderError("provider_error")
    return key


def _http_error(status):
    category = "rate_limit" if status == 429 else "unavailable" if 500 <= status <= 599 else "provider_error"
    return ProviderError(category, status_code=status)


class HTTPTransport:
    def __init__(self, *, opener=None):
        self.opener = opener

    def send(self, *, url, headers, body, timeout):
        # Lazy imports keep mock-only execution away from network initialization.
        from urllib.error import HTTPError, URLError
        from urllib.request import HTTPRedirectHandler, Request, build_opener

        class NoRedirects(HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                # Never forward bearer authorization to a redirected endpoint.
                return None

        request = Request(url, data=_json(body).encode("utf-8"), headers=headers, method="POST")
        opener = self.opener if self.opener is not None else build_opener(NoRedirects()).open
        try:
            with opener(request, timeout=timeout) as response:
                if not 200 <= response.status < 300:
                    raise _http_error(response.status)
                raw = response.read()
        except HTTPError as error:
            status = error.code
            error.close()
            raise _http_error(status) from None
        except TimeoutError:
            raise ProviderError("timeout") from None
        except URLError as error:
            category = "timeout" if isinstance(error.reason, TimeoutError) else "transport_failure"
            raise ProviderError(category) from None
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError):
            raise ProviderError("invalid_envelope") from None


class OpenAIResponsesProvider:
    def __init__(
        self, *, model=None, endpoint=None, parameters=None, schema=None, timeout=30,
        transport=None, credential_loader=None, logger=None, request_config=None,
    ):
        self.model = DEFAULTS["model"] if model is None else model
        if not isinstance(self.model, str) or re.fullmatch(r"[A-Za-z0-9_.:-]+", self.model) is None:
            raise ValueError("Invalid model identifier")
        self.endpoint = validate_endpoint(DEFAULTS["endpoint"] if endpoint is None else endpoint)
        self.parameters = validate_parameters(DEFAULTS["parameters"] if parameters is None else parameters)
        self.schema = copy.deepcopy(schema if schema is not None else json.loads((ROOT / DEFAULTS["schema_path"]).read_text(encoding="utf-8")))
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Provider timeout must be finite and positive")
        self.timeout = timeout
        self.transport = transport if transport is not None else HTTPTransport()
        self.credential_loader = credential_loader
        self.logger = logger
        self.request_config = copy.deepcopy({name: DEFAULTS[name] for name in REQUEST_KEYS} if request_config is None else request_config)
        # Structural settings cannot be replaced with tools, streaming or lax output.
        if self.request_config != {name: DEFAULTS[name] for name in REQUEST_KEYS}:
            raise ValueError("Unsupported structured extraction protocol settings")
        self._evidence = None

    def _body(self, prompt):
        inputs = json.loads(prompt)
        if (not isinstance(inputs, list) or len(inputs) != 2
                or [item.get("role") for item in inputs if isinstance(item, dict)] != ["system", "user"]
                or any(not isinstance(item.get("content"), str) for item in inputs)):
            raise ValueError("Expected a serialized system/user extraction prompt")
        return {
            "model": self.model, "input": inputs, **copy.deepcopy(self.parameters),
            **{name: self.request_config[name] for name in ("stream", "background", "store", "truncation", "tools", "tool_choice")},
            "text": {"format": {"type": "json_schema", "name": self.request_config["schema_name"],
                                 "strict": self.request_config["strict"], "schema": copy.deepcopy(self.schema)}},
        }

    def _make_evidence(self, body):
        return {
            "request": {"method": "POST", "url": self.endpoint, "timeout_seconds": self.timeout,
                        "headers": {"Authorization": "Bearer [REDACTED]", "Content-Type": "application/json"},
                        "body": copy.deepcopy(body)},
            "configuration": {"provider": "openai", "model": self.model, "parameters": copy.deepcopy(self.parameters),
                              "schema_sha256": hashlib.sha256(_json(self.schema).encode()).hexdigest()},
            "protocol": {"verified_on": PROTOCOL["verified_on"], "sources": PROTOCOL["sources"]},
            "response_handling": RESPONSE_HANDLING.copy(),
        }

    def request_evidence(self, prompt=None):
        """Preview without a credential/network call, or copy the last sent request."""
        if prompt is not None:
            return self._make_evidence(self._body(prompt))
        if self._evidence is None:
            raise ValueError("No request recorded; provide a prompt for an offline preview")
        return copy.deepcopy(self._evidence)

    def _log(self, **event):
        if self.logger is not None:
            self.logger(event)

    def _normalize(self, response):
        if not isinstance(response, dict) or response.get("object") != "response":
            raise ProviderError("invalid_envelope")
        if response.get("error") is not None:
            raise ProviderError("provider_error")
        state = response.get("status")
        if state in ("incomplete", "in_progress", "queued", "cancelled"):
            return {"state": "interrupted", "output_text": ""}
        if state == "failed":
            raise ProviderError("provider_error")
        if state != "completed" or response.get("incomplete_details") is not None:
            raise ProviderError("invalid_envelope")
        output = response.get("output")
        if not isinstance(output, list) or not output:
            raise ProviderError("invalid_envelope")
        texts, refused = [], False
        for item in output:
            if not isinstance(item, dict):
                raise ProviderError("invalid_envelope")
            if item.get("type") == "reasoning":
                continue
            if item.get("type") != "message" or item.get("role") != "assistant":
                raise ProviderError("invalid_envelope")
            if item.get("status") in ("incomplete", "in_progress"):
                return {"state": "interrupted", "output_text": ""}
            if item.get("status") != "completed" or not isinstance(item.get("content"), list):
                raise ProviderError("invalid_envelope")
            for content in item["content"]:
                if not isinstance(content, dict):
                    raise ProviderError("invalid_envelope")
                if content.get("type") == "refusal" and isinstance(content.get("refusal"), str):
                    refused = True
                elif content.get("type") == "output_text" and isinstance(content.get("text"), str):
                    texts.append(content["text"])
                else:
                    raise ProviderError("invalid_envelope")
        if refused:
            return {"state": "refused", "output_text": ""}
        resolved = response.get("model")
        if not texts or not isinstance(resolved, str) or re.fullmatch(r"[A-Za-z0-9_.:-]+", resolved) is None:
            raise ProviderError("invalid_envelope")
        return {"state": "completed", "output_text": "".join(texts), "response_source": "openai/" + resolved}

    def complete(self, prompt):
        body = self._body(prompt)
        self._evidence = self._make_evidence(body)
        try:
            credential = (self.credential_loader or read_api_key)()
            if not isinstance(credential, str) or not credential.strip():
                raise ProviderError("provider_error")
            if credential in _json(body):
                # A credential never becomes extraction text, even if echoed in input.
                self._evidence["request"]["body"] = "[REDACTED: credential-bearing request]"
                raise ProviderError("provider_error")
            response = self.transport.send(
                url=self.endpoint, headers={"Authorization": "Bearer " + credential, "Content-Type": "application/json"},
                body=body, timeout=self.timeout,
            )
            if credential in _json(response):
                raise ProviderError("provider_error")
            envelope = self._normalize(response)
        except ProviderError as error:
            self._evidence["outcome"] = {"category": error.category, "status_code": error.status_code}
            self._log(event="provider_failure", category=error.category, status_code=error.status_code)
            raise error from None
        except TimeoutError:
            self._log(event="provider_failure", category="timeout")
            raise ProviderError("timeout") from None
        except Exception:
            self._log(event="provider_failure", category="transport_failure")
            raise ProviderError("transport_failure") from None
        self._evidence["outcome"] = {"state": envelope["state"]}
        self._log(event="provider_response", state=envelope["state"])
        return envelope
