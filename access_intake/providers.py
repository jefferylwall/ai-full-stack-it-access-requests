"""Provider interface and deterministic, offline scripted responses."""

from collections import deque
from dataclasses import dataclass
import json
from pathlib import Path
from threading import Event, Lock
from time import monotonic
from typing import Any, Callable, Iterable, Protocol


class ProviderError(Exception):
    """A categorized provider failure with an allowlisted, credential-free message."""

    MESSAGES = {
        "rate_limit": "Provider rate limit reached; wait for capacity and retry extraction.",
        "unavailable": "Provider temporarily unavailable; check availability and retry extraction.",
        "timeout": "Provider request timed out; check availability and retry extraction.",
        "transport_failure": "Provider request failed; check availability and retry extraction.",
        "provider_error": "Provider rejected or failed the request; check model, request settings and environment credentials before retrying.",
        "invalid_envelope": "Provider returned an invalid completion envelope; inspect configuration and retry extraction.",
    }

    def __init__(self, category: str, *, status_code: int | None = None) -> None:
        if category not in self.MESSAGES:
            raise ValueError("Unsupported provider error category")
        super().__init__(self.MESSAGES[category])
        self.category = category
        self.status_code = status_code


class Provider(Protocol):
    """Return a completion envelope; extraction validation happens separately."""

    def complete(self, message: str) -> dict[str, Any]:
        ...


@dataclass(frozen=True)
class SavedResponse:
    """A named reply captured at admission, independent of later fixture edits."""

    identity: str
    raw_bytes: bytes


@dataclass(frozen=True)
class HeldResponse:
    """Signal arrival, then wait for an explicit release before reading a reply.

    The caller owns both events and must release held calls during cleanup.
    There is no wall-clock delay or simulated provider reply involved.
    """

    fixture: str | Path | SavedResponse
    entered: Event
    release: Event


class MockProvider:
    """Consume saved envelopes, injected exceptions and held replies in order.

    Script assignment and call recording are atomic. Fixture reads and release
    waits happen outside the lock so a held response does not block other calls.
    No credentials, networking, retries or extraction transformations are used.
    """

    def __init__(
        self,
        script: Iterable[str | Path | SavedResponse | Exception | HeldResponse],
        *,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._script = deque(script)
        self._clock = clock
        self._lock = Lock()
        self._calls: list[dict[str, Any]] = []

    @property
    def calls(self) -> list[dict[str, Any]]:
        """A snapshot of all calls, including held and failed calls."""
        with self._lock:
            return [call.copy() for call in self._calls]

    @property
    def call_count(self) -> int:
        with self._lock:
            return len(self._calls)

    def complete(self, message: str) -> dict[str, Any]:
        with self._lock:
            self._calls.append({"message": message, "started_at": self._clock()})
            if not self._script:
                raise RuntimeError("Mock provider response script exhausted")
            response = self._script.popleft()

        if isinstance(response, Exception):
            raise response
        if isinstance(response, HeldResponse):
            response.entered.set()
            response.release.wait()
            response = response.fixture

        if isinstance(response, SavedResponse):
            return json.loads(response.raw_bytes.decode("utf-8"))

        return json.loads(Path(response).read_text(encoding="utf-8"))
