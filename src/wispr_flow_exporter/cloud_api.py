"""A minimal client for Wispr Flow's undocumented sync API.

This backend was built to reach dictation history, which ``localDataPolicy =
never_store`` keeps off disk entirely. **It cannot.** The desktop app's sync
coordinator sorts its resources into a pull list, whose members expose a fetch
call, and a push list, whose members only upload -- and ``history``, ``polish``
and ``instructHistory`` are all in the push list. Dictation leaves the machine
through upload-only routes and there is no read path back. The only readable
history surface is aggregate counts.

So this backend archives what the server will hand back that the local store
does not already hold, and the honest answer to "archive my dictation history"
is to change the preference in Wispr Flow first. See ``MAINTENANCE.md`` for how
that conclusion was reached and how to re-check it after an app update.

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

@dataclass(frozen=True, slots=True)
class Endpoint:
    """One endpoint this tool may request, and what is known about it.

    Attributes:
        path: Request path, relative to the API root.
        expected_status: The status this endpoint is known to answer with.
            Recorded from measurement, not intention. An endpoint that answers
            as expected is evidence; one that stops doing so is drift, and the
            distinction is what keeps a documented failure from reading like an
            alarm every run.
        cursor_param: The query parameter the desktop app uses to ask for a
            delta, when it has one. Declared and deliberately never sent -- see
            the module note below.
        note: Why this endpoint is here, or why it cannot work.
    """

    path: str
    expected_status: int = 200
    cursor_param: str | None = None
    note: str = ""


# Endpoints archived by a cloud sync, mapped to the name each is stored under.
# Kept as data rather than code so adding one is a one-line change, and so the
# set this tool touches is auditable at a glance.
#
# On cursors: several of these accept a "since" or "cursor" parameter, and this
# client sends neither. The archive holds one verbatim snapshot per endpoint at
# cloud/<name>.json, so a delta response would overwrite a whole snapshot with a
# partial one. Incremental cursors and one-file-per-endpoint verbatim archiving
# are incompatible, and the zero-bytes invariant already makes a full re-fetch
# free on disk. The parameter names are recorded so the choice stays visible.
# Every status below was measured against the live service on app 1.6.721, not
# inferred from the bundle. MAINTENANCE.md has the procedure for re-measuring.
ENDPOINTS: Mapping[str, Endpoint] = {
    "user_profile": Endpoint("/api/v1/user/profile"),
    "user_preferences": Endpoint("/api/v1/user/preferences"),
    "meetings": Endpoint(
        "/api/v1/meetings/",
        expected_status=404,
        note=(
            "Not a route -- measured 404. It is the common prefix of "
            "/api/v1/meetings/<id>/... and was mistaken for an endpoint when "
            "the paths were read from bundle strings. The app pulls meetings "
            "through a write method this tool does not issue, so meetings "
            "cannot be enumerated from the cloud at all; the local backend is "
            "their only route. Declared so a run records the 404 as evidence."
        ),
    ),
    "meetings_shared": Endpoint("/api/v1/meetings/shared", cursor_param="cursor"),
    "notes": Endpoint(
        "/api/v1/notes/sync",
        expected_status=405,
        note=(
            "Measured 405. Answers only to a write method, and is a "
            "bidirectional push-pull rather than a read. Declared so each run "
            "records the status; the local backend is the route to notes."
        ),
    ),
    "todos": Endpoint(
        "/api/v1/todos/sync",
        expected_status=405,
        note="Measured 405. As notes: a write-method push-pull, not a read.",
    ),
    "calendar": Endpoint("/api/v1/calendar/sync", cursor_param="since"),
    "calendar_prereads": Endpoint("/api/v1/calendar/prereads/agentic_sync"),
    "dictionary_personal": Endpoint("/api/v1/dictionary/personal"),
    "dictionary_shared": Endpoint(
        "/api/v1/dictionary/shared",
        note=(
            "The app chooses between this and /api/v1/dictionary/team on a "
            "feature flag. Both answer, so both are archived."
        ),
    ),
    "dictionary_team": Endpoint("/api/v1/dictionary/team"),
    "notetaker_chats": Endpoint("/api/v1/notetaker-chats", cursor_param="cursor"),
    "notifications": Endpoint("/api/v1/notification"),
    # The dictation-derived four. Under localDataPolicy = never_store these are
    # the only trace of dictation that survives anywhere this tool can reach:
    # word counts, durations, streaks, per-day activity and a profile derived
    # from what was said. Not the text. Nothing reaches the text.
    "insights": Endpoint("/api/v1/insights"),
    "insights_heatmap": Endpoint("/api/v1/insights/heatmap", cursor_param="since"),
    "history_stats": Endpoint("/history/stats"),
    "history_context_stats": Endpoint("/history/context-stats"),
    "voice_profile": Endpoint("/llm/voice_profile/latest"),
}

# Paths read from the bundle and probed, but deliberately not archived. Kept so
# `schema --source cloud --candidates` can re-check them after an app update,
# and so the reason for each omission survives longer than the decision to omit
# it. Statuses measured on app 1.6.721.
CANDIDATES: Mapping[str, Endpoint] = {
    # Answers, but only usefully when sent the client's own timestamp map,
    # which this tool does not maintain. The same information is archived from
    # config.json by the local backend.
    "sync_check": Endpoint("/api/v1/sync/check"),
    # Answers. A weekly counter that resets on its own, so archiving it would
    # rewrite a file every week to record nothing that happened.
    "meetings_quota": Endpoint("/api/v1/meetings/weekly-quota"),
    # Answers. A device list; account trivia rather than content.
    "registered_devices": Endpoint("/api/v1/user/registered_devices"),
    # Answers, and carries the names of people this account referred. Third
    # party data with no archival value here, so it is deliberately not read.
    "referral": Endpoint("/api/v1/referral/", expected_status=200),
    "calendar_events": Endpoint("/api/v1/calendar/events/", expected_status=404),
    # Needs a request body this tool does not send.
    "calendar_events_batch": Endpoint(
        "/api/v1/calendar/events/batch", expected_status=422
    ),
    # Answers 204 with no body on this account.
    "user_context": Endpoint("/api/v1/user_context", expected_status=204),
    "cost_center": Endpoint("/api/v1/me/active-cost-center", expected_status=404),
}

# Prefixes an endpoint's path may begin with. Narrow on purpose: the bundle
# exposes /geo, /marketing and /warmup too, and none of them hold user data
# worth archiving.
ALLOWED_PREFIXES = ("/api/v1/", "/history/", "/llm/")

# Paths this tool must never request, whatever a future maintainer discovers.
# Asserted by test rather than left to discipline. The first is account
# deletion; the rest are other people's data, or scope this tool has no business
# reaching even when the borrowed credential would allow it.
DENIED = (
    "/api/v1/support/",
    "/api/v1/sandbox-user/",
    "/api/v1/enterprise/",
    "/api/v1/contacts",
    "/api/v1/teams/",
)


class CloudError(Exception):
    """A cloud request failed in a way the caller should report."""


@dataclass(frozen=True, slots=True)
class EndpointResult:
    """What one request actually did, kept structurally rather than as prose.

    The client's ``failures`` list flattens everything into a message, which is
    right for telling an operator what went wrong and useless for deciding
    whether a shape moved. This keeps the status as a number so drift can be
    classified against it.

    Attributes:
        name: The endpoint's archive name.
        path: The path requested.
        status: HTTP status, or ``None`` when the transport never got one.
        payload: The decoded JSON body, or ``None``.
        reason: A redacted failure reason, or ``None`` on success.
    """

    name: str
    path: str
    status: int | None
    payload: Any = None
    reason: str | None = None

    @property
    def ok(self) -> bool:
        """Report whether the request returned a usable body."""
        return self.reason is None and self.status is not None and self.status < 400


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

    @property
    def results(self) -> Mapping[str, EndpointResult]:
        """What each attempted endpoint returned, keyed by archive name."""
        ...


@dataclass(slots=True)
class CloudClient:
    """A paced, read-only JSON client for the sync API.

    Attributes:
        credential: The bearer token, borrowed for the lifetime of the client.
        base_url: API root.
        timeout: Per-request timeout in seconds.
        endpoints: The table names are resolved against. Defaults to the
            archived set; the discovery probe passes a wider one.
        failures: Endpoints that failed, with redacted reasons.
        results: What each attempted endpoint returned, structurally.
    """

    credential: Credential
    base_url: str = DEFAULT_BASE
    timeout: float = DEFAULT_TIMEOUT
    endpoints: Mapping[str, Endpoint] = field(default_factory=lambda: ENDPOINTS)
    failures: list[tuple[str, str]] = field(default_factory=list)
    results: dict[str, EndpointResult] = field(default_factory=dict)
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

    def _record(
        self,
        name: str,
        path: str,
        status: int | None,
        *,
        payload: Any = None,
        reason: str | None = None,
    ) -> Any:
        """Record one terminating outcome and return the body, if any.

        Args:
            name: The endpoint's archive name.
            path: The path requested.
            status: HTTP status, or ``None`` when no response arrived.
            payload: The decoded body, on success.
            reason: A redacted failure reason, on failure.

        Returns:
            ``payload``, so callers can ``return self._record(...)``.
        """
        self.results[name] = EndpointResult(
            name=name, path=path, status=status, payload=payload, reason=reason
        )
        if reason is not None:
            self.failures.append((name, reason))
        return payload

    def fetch(self, name: str) -> Any:
        """Fetch one named endpoint, retrying transient failures.

        Args:
            name: A key of :attr:`endpoints`.

        Returns:
            The decoded JSON body, or ``None`` when the endpoint could not be
            fetched. A failed endpoint is recorded rather than raised: the
            local archive is the primary artifact and one unreachable endpoint
            must not discard the rest of the run.
        """
        import httpx

        endpoint = self.endpoints.get(name)
        if endpoint is None:
            raise CloudError(f"unknown endpoint: {name}")
        if self._client is None:
            raise CloudError("CloudClient must be used as a context manager")
        path = endpoint.path

        for attempt in range(MAX_RETRIES):
            self._pace()
            try:
                response = self._client.get(path)
            except httpx.HTTPError as error:
                reason = redact(str(error)) or error.__class__.__name__
                if attempt == MAX_RETRIES - 1:
                    return self._record(name, path, None, reason=reason)
                time.sleep(BACKOFF[attempt])
                continue

            status = response.status_code
            if status in (429, 500, 502, 503, 504):
                if attempt == MAX_RETRIES - 1:
                    return self._record(name, path, status, reason=f"HTTP {status}")
                # Honour Retry-After when the server sends one; it knows more
                # about its own load than a fixed ladder does.
                wait = _retry_after(response.headers.get("Retry-After"))
                time.sleep(wait if wait is not None else BACKOFF[attempt])
                continue

            if status in (401, 403):
                # Not retryable, and the likeliest cause is an access token
                # that expired while the run was in flight.
                return self._record(
                    name,
                    path,
                    status,
                    reason=(
                        f"HTTP {status}: the access token was rejected. Open "
                        "Wispr Flow to refresh its session."
                    ),
                )
            if status >= 400:
                return self._record(name, path, status, reason=f"HTTP {status}")

            if status == 204 or not response.content:
                # A legitimate answer meaning "nothing here", not a failure to
                # parse. Reported as its own thing so the two are never
                # confused in a diagnosis.
                return self._record(name, path, status, reason="no content")
            try:
                body = response.json()
            except ValueError:
                return self._record(
                    name, path, status, reason="response was not JSON"
                )
            return self._record(name, path, status, payload=body)
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
