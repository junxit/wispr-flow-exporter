"""A minimal client for Wispr Flow's undocumented sync API.

This backend exists for one reason: when ``localDataPolicy`` is
``never_store``, dictation history is never written to disk, so the local
backend cannot reach it no matter how carefully it reads. The server has it.

Everything here is shaped by the fact that the API is **not a contract**. It is
the desktop app's private interface, discovered from strings in the application
bundle, and it can change shape without notice. Three consequences:

- Responses are archived **verbatim**. Nothing is reshaped on the way in, so a
  response whose structure this tool does not recognize is still preserved
  losslessly and can be re-read later.
- The client is confined behind a ``Protocol`` and imported lazily, so a local
  export never loads ``httpx`` and the whole backend can be replaced or removed
  in one file.
- Failures are non-fatal to the run. The local archive is the primary artifact;
  a cloud pass that cannot reach the server reports that and leaves everything
  else intact.

The client is deliberately read-only. It issues ``GET`` only, and there is no
code path that writes to Wispr Flow's servers.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from . import USER_AGENT
from .cloud_auth import Credential
from .local_config import redact

DEFAULT_BASE = "https://api.wisprflow.ai"
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 4
# Paced conservatively. This is someone else's private API and the archive is
# never urgent; being a quiet client is worth more than being a fast one.
MIN_INTERVAL = 0.25
BACKOFF = (2.0, 5.0, 15.0, 30.0)

# Read-only endpoints worth archiving, mapped to the name each is stored under.
# Kept as data rather than code so adding one is a one-line change, and so the
# set this tool touches is auditable at a glance.
ENDPOINTS: Mapping[str, str] = {
    "user_profile": "/api/v1/user/profile",
    "user_preferences": "/api/v1/user/preferences",
    "meetings": "/api/v1/meetings/",
    "meetings_shared": "/api/v1/meetings/shared",
    "notes": "/api/v1/notes/sync",
    "todos": "/api/v1/todos/sync",
    "calendar": "/api/v1/calendar/sync",
    "dictionary_personal": "/api/v1/dictionary/personal",
    "dictionary_shared": "/api/v1/dictionary/shared",
}


class CloudError(Exception):
    """A cloud request failed in a way the caller should report."""


class CloudProtocol(Protocol):
    """What a sync pass needs from a cloud client.

    Narrow on purpose: it is what makes the pass testable without a network,
    and what would make a different transport a drop-in replacement.
    """

    def fetch(self, name: str) -> Any:
        """Fetch one named endpoint.

        Args:
            name: A key of :data:`ENDPOINTS`.

        Returns:
            The decoded JSON body.
        """
        ...

    @property
    def failures(self) -> list[tuple[str, str]]:
        """Endpoints that could not be fetched, with a redacted reason."""
        ...


@dataclass(slots=True)
class CloudClient:
    """A paced, read-only JSON client for the sync API.

    Attributes:
        credential: The bearer token, borrowed for the lifetime of the client.
        base_url: API root.
        timeout: Per-request timeout in seconds.
        failures: Endpoints that failed, with redacted reasons.
    """

    credential: Credential
    base_url: str = DEFAULT_BASE
    timeout: float = DEFAULT_TIMEOUT
    failures: list[tuple[str, str]] = field(default_factory=list)
    _client: Any = None
    _last_request: float = 0.0

    def __enter__(self) -> CloudClient:
        """Open the underlying HTTP client.

        ``httpx`` is imported here rather than at module scope so a local-only
        export never loads it.

        Returns:
            This client.
        """
        import httpx

        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=self.timeout,
            headers={
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
                **self.credential.header(),
            },
        )
        return self

    def __exit__(self, *exc: object) -> None:
        """Close the underlying HTTP client."""
        if self._client is not None:
            self._client.close()
            self._client = None

    def _pace(self) -> None:
        """Sleep just enough to stay under the minimum request interval."""
        elapsed = time.monotonic() - self._last_request
        if elapsed < MIN_INTERVAL:
            time.sleep(MIN_INTERVAL - elapsed)
        self._last_request = time.monotonic()

    def fetch(self, name: str) -> Any:
        """Fetch one named endpoint, retrying transient failures.

        Args:
            name: A key of :data:`ENDPOINTS`.

        Returns:
            The decoded JSON body, or ``None`` when the endpoint could not be
            fetched. A failed endpoint is recorded rather than raised: the
            local archive is the primary artifact and one unreachable endpoint
            must not discard the rest of the run.
        """
        import httpx

        path = ENDPOINTS.get(name)
        if path is None:
            raise CloudError(f"unknown endpoint: {name}")
        if self._client is None:
            raise CloudError("CloudClient must be used as a context manager")

        for attempt in range(MAX_RETRIES):
            self._pace()
            try:
                response = self._client.get(path)
            except httpx.HTTPError as error:
                reason = redact(str(error)) or error.__class__.__name__
                if attempt == MAX_RETRIES - 1:
                    self.failures.append((name, reason))
                    return None
                time.sleep(BACKOFF[attempt])
                continue

            if response.status_code in (429, 500, 502, 503, 504):
                if attempt == MAX_RETRIES - 1:
                    self.failures.append((name, f"HTTP {response.status_code}"))
                    return None
                # Honour Retry-After when the server sends one; it knows more
                # about its own load than a fixed ladder does.
                wait = _retry_after(response.headers.get("Retry-After"))
                time.sleep(wait if wait is not None else BACKOFF[attempt])
                continue

            if response.status_code in (401, 403):
                # Not retryable, and the likeliest cause is an access token
                # that expired while the run was in flight.
                self.failures.append(
                    (
                        name,
                        f"HTTP {response.status_code}: the access token was "
                        "rejected. Open Wispr Flow to refresh its session.",
                    )
                )
                return None
            if response.status_code >= 400:
                self.failures.append((name, f"HTTP {response.status_code}"))
                return None

            try:
                return response.json()
            except ValueError:
                self.failures.append((name, "response was not JSON"))
                return None
        return None


def _retry_after(value: str | None) -> float | None:
    """Parse a ``Retry-After`` header expressed in seconds.

    Args:
        value: The header value, if present.

    Returns:
        Seconds to wait, or ``None`` when absent or not a number.
    """
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    # A hostile or broken header must not park the run for an hour.
    return max(0.0, min(seconds, 60.0))
