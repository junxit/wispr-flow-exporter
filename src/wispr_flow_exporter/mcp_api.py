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
pretending otherwise would be theater. What the GET-only rule actually protects
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

import codecs
import re
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeGuard

from . import USER_AGENT
from .local_config import printable, redact
from .mcp_auth import McpAuthError, McpCredential
from .transport import (
    ACCEPT_ENCODING,
    BACKOFF,
    MAX_RETRIES,
    MIN_INTERVAL,
    NotJson,
    ResponseRefused,
    Retry,
    decode_json,
    iter_capped,
    read_capped,
    retry_after,
)

#: The revision of the MCP spec this client speaks.
PROTOCOL_VERSION = "2025-06-18"

DEFAULT_TIMEOUT = 60.0

#: Page size for the search tools. The server caps at 200.
PAGE_SIZE = 200

#: Pages of ``tools/list`` to follow before deciding the server is looping.
TOOL_PAGES = 16

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
        sends: The arguments the sync pass sends it, as dotted paths into its
            input schema; ``None`` for a tool the pass never calls. This is
            what drift protects: a change to one of these, or a new required
            argument beside them, is breaking, and anything else is news.
    """

    note: str = ""
    paginated: bool = False
    sends: tuple[str, ...] | None = None


#: The allowlist. Membership here is the only thing that makes a tool callable,
#: so this table is the whole read-only guarantee and is worth reading closely.
#: Every name is a read verb; the server exposes no write tools today, and if it
#: ever does, absence from this table is what keeps them unreachable.
READ_TOOLS: Mapping[str, McpTool] = {
    "get_account_info": McpTool(
        "Identity, to tell the owner from other attendees.", sends=()
    ),
    "search_meetings": McpTool(
        "Lists meetings most recently modified first; `since` and `until` "
        "bound when a meeting started, not when it changed.",
        paginated=True,
        sends=("limit", "cursor", "since", "until"),
    ),
    "get_meeting": McpTool(
        "Notes, summary, todos, attendees and the transcript, a range at a time.",
        sends=(
            "meeting_id",
            "view_content.start_char",
            "view_content.char_limit",
            "view_transcript.start_char",
            "view_transcript.char_limit",
        ),
    ),
    "list_meeting_series": McpTool("Occurrences of a recurring meeting.", paginated=True),
    "search_scratchpad_notes": McpTool(
        "Lists notes, same filters.", paginated=True, sends=("limit", "cursor")
    ),
    "get_scratchpad_note": McpTool("One note's normalized text."),
    "search_calendar_events": McpTool("Calendar events.", paginated=True),
    "get_calendar_event": McpTool("One calendar event."),
}

#: The tools the sync pass calls. The rest of the allowlist may be called by no
#: one yet, and a change to it is reported, never breaking.
USED_TOOLS: frozenset[str] = frozenset(
    name for name, tool in READ_TOOLS.items() if tool.sends is not None
)

#: The only JSON-RPC methods this client sends.
ALLOWED_METHODS = (
    "initialize",
    "notifications/initialized",
    "tools/list",
    "tools/call",
)


class McpError(Exception):
    """An MCP call failed in a way the caller should report."""


class _Renew(Exception):
    """Leave a streaming block to renew a rejected token, then try again."""


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


_LINE_END = re.compile(r"\r\n|\r|\n")


def _sse_messages(chunks: Iterable[bytes]) -> Iterator[Any]:
    """Decode the JSON messages of an event stream as they arrive.

    Streamable HTTP may answer a POST with ``text/event-stream`` instead of
    ``application/json``, and an event stream is not a list of ``data:``
    lines. Per the specification: a line ends in CRLF, LF or CR; an event's
    ``data`` lines are joined with newlines, each losing one leading space; a
    blank line dispatches the event; comments and other fields are ignored.
    Measured on 0.4.1, which took the first ``data:`` line as the reply: a
    progress notification ahead of the answer became the answer, and one
    message split over two lines failed to parse.

    Args:
        chunks: The body, in pieces, already bounded by the cap.

    Yields:
        Each event's decoded JSON; events that are not JSON are skipped. An
        event the stream ends in the middle of is dispatched too, since a
        reply cut short cannot parse as a complete one.
    """
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    pending = ""
    data: list[str] = []

    def take(line: str) -> None:
        if not line.startswith(":"):
            name, _, value = line.partition(":")
            if name == "data":
                data.append(value.removeprefix(" "))

    def dispatch() -> Iterator[Any]:
        text = "\n".join(data)
        data.clear()
        try:
            yield decode_json(text)
        except NotJson:
            return

    for chunk in _ended(iter(chunks)):
        final = chunk is None
        # Only the new text can hold a line end, bar a CR held back from the
        # previous read, so each piece is scanned once: a body that is one
        # enormous line costs what it weighs rather than its square.
        scan = max(len(pending) - 1, 0)
        pending += decoder.decode(chunk or b"", final=final)
        start = 0
        for match in _LINE_END.finditer(pending, scan):
            if match.group() == "\r" and match.end() == len(pending) and not final:
                break  # possibly the first half of a CRLF split across reads
            line = pending[start : match.start()]
            start = match.end()
            if line:
                take(line)
            elif data:
                yield from dispatch()
        pending = pending[start:]
    if pending:
        take(pending)
    if data:
        yield from dispatch()


def _ended(chunks: Iterator[bytes]) -> Iterator[bytes | None]:
    """Yield a stream's pieces and then ``None``, so the reader can flush.

    Args:
        chunks: The pieces.

    Yields:
        Each piece, then ``None``.
    """
    yield from chunks
    yield None


def _answers(message: Any, request_id: int) -> TypeGuard[dict[str, Any]]:
    """Report whether a message is the reply to one request.

    Args:
        message: A decoded JSON-RPC message.
        request_id: The request's ``id``.

    Returns:
        ``True`` for a response carrying that id and a result or an error.
    """
    return (
        isinstance(message, dict)
        and message.get("id") == request_id
        and ("result" in message or "error" in message)
    )


def _read_reply(response: Any, request_id: int) -> dict[str, Any]:
    """Read the reply to one request from an open response.

    Args:
        response: The streaming response.
        request_id: The request's ``id``.

    Returns:
        The JSON-RPC response message.

    Raises:
        McpError: The body held no reply to this request. A reply to some
            other request is not this one's -- 0.4.1 accepted it anyway.
    """
    if "text/event-stream" in response.headers.get("Content-Type", ""):
        for message in _sse_messages(iter_capped(response)):
            if _answers(message, request_id):
                # Returned at once: nothing after the answer is read, and the
                # caller's `with` closes the stream.
                return message
        raise McpError("the event stream ended without a reply to this request")
    raw = read_capped(response)
    if not raw:
        raise McpError("the server answered this request with an empty body")
    try:
        message = decode_json(raw)
    except NotJson as error:
        raise McpError("response was not JSON") from error
    if not isinstance(message, dict):
        raise McpError("response was not a JSON-RPC message")
    if not _answers(message, request_id):
        raise McpError("the response did not answer this request")
    return message


def tool_error(result: Any) -> str | None:
    """Return why a ``tools/call`` result says the tool failed, if it does.

    MCP reports a tool's own failure inside a successful JSON-RPC response:
    ``isError: true``, with the reason as content. Measured: asking for a
    meeting that does not exist answers exactly that way. Read as data, as
    0.4.1 read it, an error became a record -- an archived listing page, or
    a transcript chunk with no text that ended assembly early.

    Args:
        result: The ``result`` member of a ``tools/call`` response.

    Returns:
        A printable reason, or ``None`` when the tool succeeded.
    """
    if not isinstance(result, dict) or result.get("isError") is not True:
        return None
    detail = ""
    structured = result.get("structuredContent")
    if isinstance(structured, dict) and isinstance(structured.get("error"), str):
        detail = structured["error"]
    elif isinstance(result.get("content"), list):
        texts = [
            block["text"]
            for block in result["content"]
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        ]
        detail = texts[0] if texts else ""
    return printable(" ".join(detail.split()), limit=200) or "no reason given"


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
                    return decode_json(block["text"])
                except NotJson:
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
        transport: An httpx transport to use instead of the network, for
            tests.
        renew: Replaces a token the server rejects before its expiry. Called
            at most once per request, and never for a token taken from the
            environment, which the caller does not pass one for.
        failures: Calls that failed, with redacted reasons.
        results: What each attempted call returned.
        server: Server name, version and protocol from the handshake.
        tools: The server's advertised tool list.
    """

    credential: McpCredential
    endpoint: str
    timeout: float = DEFAULT_TIMEOUT
    transport: Any = None
    renew: Callable[[McpCredential], McpCredential] | None = None
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
                "Accept-Encoding": ACCEPT_ENCODING,
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
                **self.credential.header(),
            },
            transport=self.transport,
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

    def _renewed(self) -> None:
        """Swap in a fresh token after the server rejected the current one.

        Raises:
            McpError: The token could not be renewed.
        """
        if self.renew is None:  # pragma: no cover - the caller checks first
            raise McpError("HTTP 401: the MCP authorization was rejected")
        try:
            credential = self.renew(self.credential)
        except McpAuthError as error:
            raise McpError(
                "HTTP 401: the MCP authorization was rejected and could not be "
                f"renewed: {error}"
            ) from error
        self.credential = credential
        self._client.headers.update(credential.header())

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
        self._next_id += 1
        request_id = self._next_id
        if not notify:
            message["id"] = request_id

        headers: dict[str, str] = {"MCP-Protocol-Version": PROTOCOL_VERSION}
        if self._session:
            headers["Mcp-Session-Id"] = self._session

        renewed = False
        for attempt in range(MAX_RETRIES):
            self._pace()
            try:
                # Streamed so the body is bounded as it is read. A transcript
                # is the largest thing this client legitimately receives, and
                # even that sits far under the cap.
                with self._client.stream(
                    "POST", self.endpoint, json=message, headers=headers
                ) as response:
                    if response.status_code in (429, 500, 502, 503, 504):
                        if attempt == MAX_RETRIES - 1:
                            raise McpError(f"HTTP {response.status_code}")
                        wait = retry_after(response.headers.get("Retry-After"))
                        raise Retry(wait if wait is not None else BACKOFF[attempt])

                    if (
                        response.status_code == 401
                        and self.renew is not None
                        and not renewed
                    ):
                        # A token can be rejected before its recorded expiry
                        # -- revoked, or rotated by another run -- and that
                        # used to end the pass. One renewal, then as before.
                        raise _Renew
                    if response.status_code in (401, 403):
                        raise McpError(
                            f"HTTP {response.status_code}: the MCP authorization "
                            "was rejected. Run `wispr-export login` again."
                        )
                    if response.status_code >= 400:
                        raise McpError(f"HTTP {response.status_code}")

                    session = response.headers.get("Mcp-Session-Id")
                    if session:
                        self._session = session
                    if notify:
                        return None
                    if response.status_code == 202:
                        # Accepted is how a server answers a notification.
                        # For a request it means no reply is coming, which
                        # 0.4.1 returned as a silent None.
                        raise McpError("the server accepted the request but sent no reply")
                    reply = _read_reply(response, request_id)
            except Retry as retry:
                time.sleep(retry.wait)
                continue
            except _Renew:
                renewed = True
                self._renewed()
                continue
            except ResponseRefused as error:
                # This call's failure, not retried. It used to escape call()
                # and end the run with a traceback.
                raise McpError(str(error)) from error
            except httpx.HTTPError as error:
                if attempt == MAX_RETRIES - 1:
                    raise McpError(
                        redact(str(error)) or error.__class__.__name__
                    ) from error
                time.sleep(BACKOFF[attempt])
                continue

            if "error" in reply:
                detail = reply["error"]
                message_text = (
                    detail.get("message") if isinstance(detail, dict) else str(detail)
                )
                raise McpError(redact(printable(str(message_text), limit=300)))
            return reply.get("result")
        raise McpError("exhausted retries")

    def _handshake(self) -> None:
        """Initialize the session and read the server's tool list.

        Raises:
            McpError: The server did not complete the handshake.
        """
        result = self._send(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "wispr-flow-exporter", "version": USER_AGENT},
            },
        )
        if not isinstance(result, dict):
            raise McpError("the server's reply to initialize was not an object")
        info = result.get("serverInfo")
        info = info if isinstance(info, dict) else {}
        self.server = {
            "name": info.get("name"),
            "version": info.get("version"),
            "protocol_version": result.get("protocolVersion"),
        }
        self._send("notifications/initialized", {}, notify=True)
        self.tools = self._list_tools()

    def _list_tools(self) -> list[dict[str, Any]]:
        """Read the server's advertised tools, to the last page.

        ``tools/list`` is paginated by ``nextCursor``, which 0.4.1 never
        followed, so a server that paged its tools would have seemed to lose
        all but the first page. And a reply with no tool list is an error:
        taken as empty, as it was, it recorded every tool as gone.

        Returns:
            Every advertised tool.

        Raises:
            McpError: A reply held no tool list, or the pages did not end.
        """
        tools: list[dict[str, Any]] = []
        seen: set[str] = set()
        cursor: str | None = None
        for _ in range(TOOL_PAGES):
            listed = self._send("tools/list", {"cursor": cursor} if cursor else {})
            if not isinstance(listed, dict) or not isinstance(listed.get("tools"), list):
                raise McpError("the server's reply to tools/list held no tool list")
            tools.extend(tool for tool in listed["tools"] if isinstance(tool, dict))
            following = listed.get("nextCursor")
            if not isinstance(following, str) or not following:
                return tools
            if following in seen:
                raise McpError("the server's tool list repeated a page")
            seen.add(following)
            cursor = following
        raise McpError(f"the server's tool list ran past {TOOL_PAGES} pages")

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
            result = self._send(
                "tools/call", {"name": name, "arguments": dict(arguments or {})}
            )
        except McpError as error:
            return self._failed(key, name, None, redact(str(error)))
        failure = tool_error(result)
        if failure is not None:
            return self._failed(key, name, 200, f"tool error: {failure}")
        payload = unwrap(result)
        if payload is None or _empty(result):
            # Measured on 0.4.1: a null result was recorded as a success with
            # no payload, and counted by no one.
            return self._failed(key, name, 200, "empty result")
        self.results[key] = ToolResult(name=name, status=200, payload=payload)
        return payload

    def _failed(self, key: str, name: str, status: int | None, reason: str) -> None:
        """Record one call's failure.

        Args:
            key: The result key.
            name: The tool invoked.
            status: The HTTP status, when one arrived.
            reason: Why the call produced nothing usable.
        """
        self.results[key] = ToolResult(name=name, status=status, reason=reason)
        self.failures.append((key, reason))


def _empty(result: Any) -> bool:
    """Report whether a tool result carries nothing at all.

    Args:
        result: The ``result`` member of a ``tools/call`` response.

    Returns:
        ``True`` for an envelope holding nothing but an empty content list
        and metadata. A result with fields of its own is data, even when it
        is not shaped the way MCP describes.
    """
    return (
        isinstance(result, dict)
        and not result.get("content")
        and set(result) <= {"content", "isError", "_meta"}
    )


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
