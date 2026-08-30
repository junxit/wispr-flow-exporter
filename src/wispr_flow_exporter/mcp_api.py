"""A read-only client for Wispr Flow's remote MCP server.

This is the third backend, and the only one that reaches meeting *content*
remotely. The REST API cannot: meetings, notes and todos are synced by the app
through write methods this tool does not issue, so over REST they return 404 and
405. The MCP server serves all three, with transcripts, filtered by modification
time and paginated -- which makes it the only route to a transcript Wispr Flow
has already garbage-collected locally.

What it does not reach, confirmed three independent ways -- the app's own
push/pull resource lists, the REST probe, and Wispr Flow's own settings copy in
every locale ("Wispr MCP has no access to your dictation") -- is dictation. That
question is closed.

**On method, and why the GET-only rule does not transfer.** The REST client
issues ``GET`` and nothing else, asserted by a test that reads its source. MCP
is JSON-RPC and every call is an HTTP POST, so that test cannot extend here and
pretending otherwise would be theatre. What the GET-only rule actually protects
is *this tool cannot change anything upstream*, and the MCP-shaped form of that
guarantee is stronger, not weaker:

- Only four JSON-RPC methods are ever sent: ``initialize``,
  ``notifications/initialized``, ``tools/list`` and ``tools/call``.
- ``tools/call`` refuses any name not in :data:`READ_TOOLS`, which holds only
  the server's read verbs. A tool the server grows tomorrow cannot be invoked
  by accident, however it is named.

Both are asserted by test. Responses are archived verbatim for the same reason
the REST ones are: the shapes are not a contract.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from . import USER_AGENT
from .cloud_api import BACKOFF, MAX_RETRIES, MIN_INTERVAL, _retry_after
from .local_config import redact
from .mcp_auth import McpCredential

#: The revision of the MCP spec this client speaks.
PROTOCOL_VERSION = "2025-06-18"

DEFAULT_TIMEOUT = 60.0

#: Page size for the search tools. The server caps at 200.
PAGE_SIZE = 200

#: Characters per transcript request. The server caps at 40000, and asking for
#: the cap minimizes the number of chunks a long transcript is spliced from --
#: every seam is a place assembly could go wrong.
TRANSCRIPT_CHARS = 40000


@dataclass(frozen=True, slots=True)
class McpTool:
    """One tool this client may invoke, and what is known about it.

    Attributes:
        note: Why it is here, or what it is used for.
        paginated: Whether results arrive a page at a time.
    """

    note: str = ""
    paginated: bool = False


#: The allowlist. Membership here is the only thing that makes a tool callable,
#: so this table is the whole read-only guarantee and is worth reading closely.
#: Every name is a read verb; the server exposes no write tools today, and if it
#: ever does, absence from this table is what keeps them unreachable.
READ_TOOLS: Mapping[str, McpTool] = {
    "get_account_info": McpTool("Identity, to tell the owner from other attendees."),
    "search_meetings": McpTool(
        "Lists meetings newest first, filtered by `since` on modifiedAt.",
        paginated=True,
    ),
    "get_meeting": McpTool("Notes, summary, todos, attendees and the transcript."),
    "list_meeting_series": McpTool("Occurrences of a recurring meeting.", paginated=True),
    "search_scratchpad_notes": McpTool("Lists notes, same filters.", paginated=True),
    "get_scratchpad_note": McpTool("One note's normalized text."),
    "search_calendar_events": McpTool("Calendar events.", paginated=True),
    "get_calendar_event": McpTool("One calendar event."),
}

#: The only JSON-RPC methods this client sends.
ALLOWED_METHODS = (
    "initialize",
    "notifications/initialized",
    "tools/list",
    "tools/call",
)


class McpError(Exception):
    """An MCP call failed in a way the caller should report."""


@dataclass(frozen=True, slots=True)
class ToolResult:
    """What one tool call returned, structurally.

    Attributes:
        name: The tool invoked.
        status: HTTP status, or ``None`` when the transport never got one.
        payload: The decoded tool result, or ``None``.
        reason: A redacted failure reason, or ``None`` on success.
    """

    name: str
    status: int | None
    payload: Any = None
    reason: str | None = None

    @property
    def ok(self) -> bool:
        """Report whether the call returned a usable body."""
        return self.reason is None and self.payload is not None


def _parse_sse(text: str) -> Any:
    """Pull the JSON-RPC message out of an event-stream body.

    Streamable HTTP may answer a POST with ``text/event-stream`` instead of
    ``application/json``, so a client that only called ``.json()`` would work
    against some deployments and not others.

    Args:
        text: The raw response body.

    Returns:
        The decoded message, or ``None`` when there was no data event.
    """
    for line in text.splitlines():
        if line.startswith("data:"):
            chunk = line[5:].strip()
            if not chunk:
                continue
            try:
                return json.loads(chunk)
            except ValueError:
                continue
    return None


def unwrap(result: Any) -> Any:
    """Decode a tool result envelope into the value it carries.

    MCP wraps results in a content list. Where the server put JSON in a text
    block -- which is how all of these answer -- the useful payload is one
    parse deeper, the same shape trap ``session.json`` has.

    Args:
        result: The ``result`` member of a ``tools/call`` response.

    Returns:
        The decoded payload, or the envelope unchanged when it holds no JSON.
    """
    if not isinstance(result, dict):
        return result
    if isinstance(result.get("structuredContent"), (dict, list)):
        return result["structuredContent"]
    content = result.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                try:
                    return json.loads(block["text"])
                except ValueError:
                    return block["text"]
    return result


class McpProtocol(Protocol):
    """What a sync pass needs from an MCP client.

    Narrow on purpose, exactly as ``CloudProtocol`` is: it is what makes the
    pass testable without a network.
    """

    def call(self, name: str, arguments: Mapping[str, Any] | None = None) -> Any:
        """Invoke one allowlisted tool and return its decoded payload."""
        ...

    @property
    def failures(self) -> list[tuple[str, str]]:
        """Calls that failed, with a redacted reason."""
        ...

    @property
    def results(self) -> Mapping[str, ToolResult]:
        """What each attempted call returned."""
        ...

    @property
    def server(self) -> Mapping[str, Any]:
        """What the server said about itself during the handshake."""
        ...


@dataclass(slots=True)
class McpClient:
    """A paced, read-only JSON-RPC client for the MCP server.

    Attributes:
        credential: The minted token, held for the lifetime of the client.
        endpoint: The MCP resource URL.
        timeout: Per-request timeout in seconds.
        failures: Calls that failed, with redacted reasons.
        results: What each attempted call returned.
        server: Server name, version and protocol from the handshake.
        tools: The server's advertised tool list.
    """

    credential: McpCredential
    endpoint: str
    timeout: float = DEFAULT_TIMEOUT
    failures: list[tuple[str, str]] = field(default_factory=list)
    results: dict[str, ToolResult] = field(default_factory=dict)
    server: dict[str, Any] = field(default_factory=dict)
    tools: list[dict[str, Any]] = field(default_factory=list)
    _client: Any = None
    _session: str | None = None
    _last_request: float = 0.0
    _next_id: int = 0

    def __enter__(self) -> McpClient:
        """Open the transport and complete the MCP handshake.

        ``httpx`` is imported here rather than at module scope so a local-only
        export never loads it.

        Returns:
            This client.
        """
        import httpx

        self._client = httpx.Client(
            timeout=self.timeout,
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
                **self.credential.header(),
            },
        )
        self._handshake()
        return self

    def __exit__(self, *exc: object) -> None:
        """Close the transport."""
        if self._client is not None:
            self._client.close()
            self._client = None

    def _pace(self) -> None:
        """Sleep just enough to stay under the minimum request interval."""
        elapsed = time.monotonic() - self._last_request
        if elapsed < MIN_INTERVAL:
            time.sleep(MIN_INTERVAL - elapsed)
        self._last_request = time.monotonic()

    def _send(self, method: str, params: Any = None, *, notify: bool = False) -> Any:
        """Send one JSON-RPC message and return its result.

        Args:
            method: A member of :data:`ALLOWED_METHODS`.
            params: The method's parameters.
            notify: Send as a notification, expecting no reply.

        Returns:
            The ``result`` member, or ``None`` for a notification.

        Raises:
            McpError: The method is not allowlisted, or the call failed.
        """
        import httpx

        if method not in ALLOWED_METHODS:
            raise McpError(f"refusing to send a non-allowlisted method: {method}")
        if self._client is None:
            raise McpError("McpClient must be used as a context manager")

        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        if not notify:
            self._next_id += 1
            message["id"] = self._next_id

        headers: dict[str, str] = {"MCP-Protocol-Version": PROTOCOL_VERSION}
        if self._session:
            headers["Mcp-Session-Id"] = self._session

        for attempt in range(MAX_RETRIES):
            self._pace()
            try:
                response = self._client.request(
                    "POST", self.endpoint, json=message, headers=headers
                )
            except httpx.HTTPError as error:
                if attempt == MAX_RETRIES - 1:
                    raise McpError(
                        redact(str(error)) or error.__class__.__name__
                    ) from error
                time.sleep(BACKOFF[attempt])
                continue

            if response.status_code in (429, 500, 502, 503, 504):
                if attempt == MAX_RETRIES - 1:
                    raise McpError(f"HTTP {response.status_code}")
                wait = _retry_after(response.headers.get("Retry-After"))
                time.sleep(wait if wait is not None else BACKOFF[attempt])
                continue

            if response.status_code in (401, 403):
                raise McpError(
                    f"HTTP {response.status_code}: the MCP authorization was "
                    "rejected. Run `wispr-export login` again."
                )
            if response.status_code >= 400:
                raise McpError(f"HTTP {response.status_code}")

            session = response.headers.get("Mcp-Session-Id")
            if session:
                self._session = session
            if notify or response.status_code == 202 or not response.content:
                return None

            kind = response.headers.get("Content-Type", "")
            if "text/event-stream" in kind:
                payload = _parse_sse(response.text)
            else:
                try:
                    payload = response.json()
                except ValueError as error:
                    raise McpError("response was not JSON") from error
            if not isinstance(payload, dict):
                raise McpError("response was not a JSON-RPC message")
            if payload.get("error"):
                detail = payload["error"]
                message_text = (
                    detail.get("message") if isinstance(detail, dict) else str(detail)
                )
                raise McpError(redact(str(message_text)))
            return payload.get("result")
        raise McpError("exhausted retries")

    def _handshake(self) -> None:
        """Initialize the session and read the server's tool list."""
        result = self._send(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "wispr-flow-exporter", "version": USER_AGENT},
            },
        )
        if isinstance(result, dict):
            info = result.get("serverInfo") or {}
            self.server = {
                "name": info.get("name"),
                "version": info.get("version"),
                "protocol_version": result.get("protocolVersion"),
            }
        self._send("notifications/initialized", {}, notify=True)
        listed = self._send("tools/list", {})
        if isinstance(listed, dict) and isinstance(listed.get("tools"), list):
            self.tools = listed["tools"]

    def call(self, name: str, arguments: Mapping[str, Any] | None = None) -> Any:
        """Invoke one allowlisted tool.

        Args:
            name: A key of :data:`READ_TOOLS`.
            arguments: The tool's arguments.

        Returns:
            The decoded payload, or ``None`` when the call failed. A failure is
            recorded rather than raised: one unreachable tool must not discard
            the rest of a run.

        Raises:
            McpError: The tool is not allowlisted. That is a programming error,
                not a runtime condition, so it raises rather than counting.
        """
        if name not in READ_TOOLS:
            raise McpError(f"refusing to call a tool that is not read-only: {name}")
        key = _result_key(name, arguments)
        try:
            payload = unwrap(
                self._send("tools/call", {"name": name, "arguments": dict(arguments or {})})
            )
        except McpError as error:
            reason = redact(str(error))
            self.results[key] = ToolResult(name=name, status=None, reason=reason)
            self.failures.append((key, reason))
            return None
        self.results[key] = ToolResult(name=name, status=200, payload=payload)
        return payload


def _result_key(name: str, arguments: Mapping[str, Any] | None) -> str:
    """Build the key one call is recorded under.

    Args:
        name: The tool invoked.
        arguments: Its arguments.

    Returns:
        The tool name, qualified by the record id when there is one, so a
        per-record call does not overwrite the previous record's result.
    """
    for field_name in ("meeting_id", "note_id", "event_id"):
        value = (arguments or {}).get(field_name)
        if isinstance(value, str) and value:
            return f"{name}:{value}"
    return name
