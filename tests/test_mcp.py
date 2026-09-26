"""The MCP backend: the allowlist, the minted credential, and gap-filling.

Every test here runs against a protocol fake. The suite never contacts Wispr
Flow, which matters more for this backend than the others: it is the one that
holds a credential of its own, and a test that reached the network could not
prove it had not spent one.
"""

from __future__ import annotations

import base64
import hashlib
import json
import stat
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from conftest import FAKE_JWT, MEETING_A, MEETING_B, archive_snapshot

from wispr_flow_exporter import mcp_api, mcp_auth
from wispr_flow_exporter.mcp_api import ALLOWED_METHODS, READ_TOOLS, McpError, unwrap
from wispr_flow_exporter.mcp_auth import McpCredential
from wispr_flow_exporter.mcp_schema import (
    McpPin,
    detect_mcp_drift,
    pin_from_tools,
    tool_shapes,
)
from wispr_flow_exporter.schema import DriftClass
from wispr_flow_exporter.store import Archive
from wispr_flow_exporter.sync import SyncOptions
from wispr_flow_exporter.sync_mcp import (
    local_transcript_state,
    sync_mcp,
    truncated,
)

_TOOLS = [
    {"name": name, "inputSchema": {"type": "object", "properties": {}}}
    for name in READ_TOOLS
]
_SERVER = {"name": "wispr", "version": "1.0.0", "protocol_version": "2025-06-18"}


class _Fake:
    """A protocol-satisfying fake that replays canned tool results."""

    def __init__(self, replies: dict[str, object]) -> None:
        """Store the canned replies.

        Args:
            replies: Result key to payload. Keys match the client's own
                convention: ``<tool>`` or ``<tool>:<record id>``.
        """
        self.replies = replies
        self.asked: list[tuple[str, dict]] = []
        self.failures: list[tuple[str, str]] = []
        self.results: dict[str, object] = {}
        self.server = dict(_SERVER)
        self.tools = list(_TOOLS)

    def call(self, name: str, arguments: dict | None = None) -> object:
        """Return a canned reply.

        Args:
            name: Tool name.
            arguments: Tool arguments.

        Returns:
            The reply, or ``None`` when none was configured.
        """
        if name not in READ_TOOLS:
            raise McpError(f"refusing to call a tool that is not read-only: {name}")
        self.asked.append((name, dict(arguments or {})))
        for field in ("meeting_id", "note_id"):
            value = (arguments or {}).get(field)
            if value and f"{name}:{value}" in self.replies:
                return self.replies[f"{name}:{value}"]
        return self.replies.get(name)


def _meeting_page(*records: dict) -> dict:
    """Build a search_meetings page.

    Args:
        *records: Meeting summaries.

    Returns:
        The page.
    """
    return {"meetings": list(records), "has_more": False, "next_cursor": None}


# --- the read-only guarantee ----------------------------------------------


def test_only_allowlisted_tools_are_callable() -> None:
    """The MCP-shaped form of the REST backend's GET-only rule.

    MCP is JSON-RPC over POST, so the "no write methods" test cannot extend
    here. What that rule protects -- this tool cannot change anything upstream
    -- is protected instead by refusing any tool not named in the table.
    """
    client = mcp_api.McpClient(McpCredential(FAKE_JWT, "test"), endpoint="http://x")

    with pytest.raises(McpError, match="not read-only"):
        client.call("delete_meeting", {})


def test_every_allowlisted_tool_is_a_read_verb() -> None:
    """A write tool must not be addable to the table by momentum alone."""
    for name in READ_TOOLS:
        assert name.startswith(("get_", "list_", "search_", "resolve_")), name


def test_only_four_json_rpc_methods_are_ever_sent() -> None:
    """The transport refuses anything outside the handshake and reads."""
    client = mcp_api.McpClient(McpCredential(FAKE_JWT, "test"), endpoint="http://x")
    client._client = object()

    with pytest.raises(McpError, match="non-allowlisted method"):
        client._send("resources/write", {})

    assert set(ALLOWED_METHODS) == {
        "initialize",
        "notifications/initialized",
        "tools/list",
        "tools/call",
    }


def _code_only(path: Path) -> str:
    """Return a module's source with comments and docstrings removed.

    A scan over raw text cannot tell a code path from the paragraph explaining
    why that code path does not exist -- and this module set is full of the
    latter. Stripping both is what makes the assertion about behavior.

    Args:
        path: The module to read.

    Returns:
        Its source, minus comments and string literals.
    """
    import io
    import tokenize

    source = path.read_text(encoding="utf-8")
    kept: list[str] = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        kept.append(token.string)
    return " ".join(kept)


def test_the_mcp_backend_never_touches_the_borrowed_session() -> None:
    """The two credentials must not meet.

    ``cloud_auth`` borrows Wispr Flow's own Supabase token and must never
    refresh it. This backend mints its own against a different issuer. The
    guarantee that minting cannot endanger the borrowed session is that the MCP
    modules cannot reach it at all -- asserted against the source, because a
    prose claim is not an invariant.
    """
    root = Path(mcp_auth.__file__).parent
    for module in ("mcp_auth.py", "mcp_api.py", "mcp_schema.py", "sync_mcp.py"):
        path = root / module
        # Identifiers: nothing here may import or call the borrowed path.
        # ("session" alone is not a signal -- MCP has its own Mcp-Session-Id.)
        code = _code_only(path).lower()
        assert "read_access_token" not in code, module
        assert "cloud_auth" not in code, module
        assert "supabase" not in code, module

        # Literals: the borrowed credential's file and the refresh endpoint
        # would arrive as strings, which the code scan deliberately strips.
        raw = path.read_text(encoding="utf-8")
        for quoted in ('"session.json"', "'session.json'", "/auth/v1/token"):
            assert quoted not in raw, f"{module}: {quoted}"


def test_the_borrowed_credential_module_is_unchanged_in_strength() -> None:
    """Adding a backend that refreshes must not relax the one that must not.

    ``cloud_auth`` still may not contain a refresh path. This is the same
    assertion ``tests/test_cloud.py`` makes, restated from the other side: the
    new module's existence is not a reason to widen the old rule.
    """
    root = Path(mcp_auth.__file__).parent
    body = (root / "cloud_auth.py").read_text(encoding="utf-8")

    assert "grant_type" not in body
    assert "/auth/v1/token" not in body


def test_a_credential_never_renders_itself() -> None:
    """A traceback that printed the token would defeat the whole redaction."""
    credential = McpCredential(FAKE_JWT, "token store", expires_at=1.0)

    assert FAKE_JWT not in repr(credential)
    assert "token store" in repr(credential)


def test_the_token_is_sent_as_a_bearer() -> None:
    """The opposite of the REST API, and measured on both.

    The REST service rejects the scheme and wants the token bare; this resource
    advertises ``bearer_methods_supported: ["header"]`` and answers only to a
    Bearer. Two services, two rules, neither guessed.
    """
    assert McpCredential(FAKE_JWT, "test").header() == {
        "Authorization": f"Bearer {FAKE_JWT}"
    }


# --- envelopes ------------------------------------------------------------


def test_a_tool_result_is_unwrapped_one_parse_deeper() -> None:
    """MCP wraps results in a content list holding JSON as text."""
    assert unwrap({"content": [{"type": "text", "text": '{"a": 1}'}]}) == {"a": 1}
    assert unwrap({"structuredContent": {"b": 2}}) == {"b": 2}
    assert unwrap({"content": [{"type": "text", "text": "plain"}]}) == "plain"


def test_an_event_stream_body_is_parsed() -> None:
    """Streamable HTTP may answer a POST with SSE rather than JSON."""
    body = 'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n\n'

    assert mcp_api._parse_sse(body) == {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"ok": True},
    }
    assert mcp_api._parse_sse("event: ping\n\n") is None


def test_a_short_response_is_reported_not_absorbed() -> None:
    """The search tools paginate, and a quiet short read would be a lie."""
    assert truncated({"meetings": [], "has_more": True})
    assert truncated({"meetings": [], "next_cursor": "more"})
    assert not truncated({"meetings": [], "has_more": False, "next_cursor": None})


# --- the two ownership rules ----------------------------------------------


def test_the_pass_adds_no_meetings_key_and_only_the_mcp_subkey(
    tmp_path: Path,
) -> None:
    """The rule that makes a third writer safe, asserted rather than trusted.

    ``Archive.put`` merges field by field, so two backends writing the same
    meetings key share one entry and last-writer-wins on ``content_hash``. That
    would make each pass consider the other's work a change and rewrite it
    forever. Nothing in the API prevents it; this test does.
    """
    archive = Archive(root=tmp_path / "archive")
    directory = archive.resolve("meetings", "2026", "08", f"m--{MEETING_A}")
    (directory / "raw").mkdir(parents=True)
    archive.put(
        "meetings",
        MEETING_A,
        path=archive.relative(directory),
        content_hash="local-digest",
        source="wispr-local",
        title="the quarterly whisper budget",
    )
    before_keys = set(archive.entries("meetings"))
    before_fields = set(archive.entries("meetings")[MEETING_A])

    client = _Fake(
        {
            "search_meetings": _meeting_page(
                {"id": MEETING_A, "title": "x", "has_transcript": False}
            )
        }
    )
    sync_mcp(archive, client, SyncOptions())

    assert set(archive.entries("meetings")) == before_keys
    entry = archive.entries("meetings")[MEETING_A]
    assert set(entry) - before_fields == {"mcp"}
    # The four last-writer-wins fields are untouched.
    assert entry["content_hash"] == "local-digest"
    assert entry["source"] == "wispr-local"


def test_a_meeting_the_local_store_lacks_stays_out_of_meetings(
    tmp_path: Path,
) -> None:
    """Verify counts meetings/ against the database; MCP must not inflate it."""
    archive = Archive(root=tmp_path / "archive")
    client = _Fake(
        {
            "search_meetings": _meeting_page(
                {"id": MEETING_B, "title": "elsewhere", "has_transcript": True}
            ),
            f"get_meeting:{MEETING_B}": {"title": "elsewhere", "notes": "..."},
        }
    )

    sync_mcp(archive, client, SyncOptions())

    assert archive.entries("meetings") == {}
    assert MEETING_B in archive.entries("mcp_meetings")


def test_a_local_transcript_is_never_overwritten(tmp_path: Path) -> None:
    """Local is higher fidelity and wins wherever it has anything at all."""
    archive = Archive(root=tmp_path / "archive")
    directory = archive.resolve("meetings", "2026", "08", f"m--{MEETING_A}")
    (directory / "raw").mkdir(parents=True)
    (directory / "raw" / "refined.ndjson").write_text(
        json.dumps({"id": "t-1", "text": "hush now", "speakerId": 1}) + "\n",
        encoding="utf-8",
    )
    archive.put("meetings", MEETING_A, path=archive.relative(directory))

    client = _Fake(
        {
            "search_meetings": _meeting_page(
                {"id": MEETING_A, "title": "x", "has_transcript": True}
            )
        }
    )
    sync_mcp(archive, client, SyncOptions())

    assert not (directory / "transcript.mcp.md").exists()
    assert archive.entries("meetings")[MEETING_A]["mcp"]["filled"] is False


def test_the_gate_reads_disk_rather_than_the_index(tmp_path: Path) -> None:
    """An index that drifted must not decide whether to overwrite content."""
    directory = tmp_path / "meeting"
    (directory / "raw").mkdir(parents=True)

    assert local_transcript_state(directory) == "absent"
    assert local_transcript_state(None) == "absent"

    (directory / "raw" / "live.ndjson").write_text(
        json.dumps({"id": "t-1", "text": "murmur", "speakerId": 2}) + "\n",
        encoding="utf-8",
    )

    # Live counts: it has turns and timestamps, so even the lesser local
    # artifact beats normalized plaintext.
    assert local_transcript_state(directory) == "present"


def test_a_second_pass_writes_nothing(tmp_path: Path) -> None:
    """The zero-bytes invariant, for the third backend."""
    archive = Archive(root=tmp_path / "archive")
    client = _Fake(
        {
            "get_account_info": {"name": "Murmur Pike"},
            "search_meetings": _meeting_page(
                {"id": MEETING_A, "title": "x", "has_transcript": False}
            ),
            "search_scratchpad_notes": {"notes": [], "has_more": False},
        }
    )
    sync_mcp(archive, client, SyncOptions())
    archive.save()
    before = archive_snapshot(archive.root)

    second = Archive(root=archive.root)
    sync_mcp(second, client, SyncOptions())
    second.save()

    assert archive_snapshot(archive.root) == before


def test_an_mcp_dry_run_writes_nothing(tmp_path: Path) -> None:
    """The account snapshot and every search page used to land before the check.

    A dry run may still ask the server what it has -- that is how it reports
    what a real run would write -- but nothing it hears may reach the disk.
    """
    archive = Archive(root=tmp_path / "archive", read_only=True)
    client = _Fake(
        {
            "get_account_info": {"name": "Murmur Pike"},
            "search_meetings": _meeting_page(
                {"id": MEETING_A, "title": "x", "has_transcript": False}
            ),
        }
    )

    counts = sync_mcp(archive, client, SyncOptions(dry_run=True))
    archive.save()

    assert counts.written == 2
    assert not archive.root.exists()


# --- drift ----------------------------------------------------------------


_PIN = McpPin(
    server="wispr", version="1.0.0", protocol_version="2025-06-18", tool_count=8,
    sha256=pin_from_tools(_TOOLS, _SERVER).sha256,
)


def test_a_matching_server_is_clean() -> None:
    """The ordinary case says so rather than staying silent."""
    drift = detect_mcp_drift(_TOOLS, _SERVER, tool_shapes(_TOOLS), _PIN)

    assert drift.kind is DriftClass.OK
    assert "OK" in drift.summary()


def test_a_new_tool_is_additive() -> None:
    """The server growing a tool has done nothing to this backend."""
    grown = [*_TOOLS, {"name": "get_weather", "inputSchema": {}}]

    drift = detect_mcp_drift(grown, _SERVER, tool_shapes(_TOOLS), _PIN)

    assert drift.kind is DriftClass.ADDITIVE
    assert drift.new_tools == ("get_weather",)
    assert not drift.blocks_rendering


def test_losing_a_tool_this_backend_calls_is_breaking() -> None:
    """Severity is about what this tool needs, not the server's inventory."""
    reduced = [tool for tool in _TOOLS if tool["name"] != "get_meeting"]

    drift = detect_mcp_drift(reduced, _SERVER, tool_shapes(_TOOLS), _PIN)

    assert drift.kind is DriftClass.BREAKING
    assert "get_meeting" in drift.unavailable
    assert drift.blocks_rendering


def test_a_renamed_argument_is_breaking() -> None:
    """An input schema that moved breaks the calls built against it."""
    moved = [
        {"name": t["name"], "inputSchema": {"type": "object", "properties": {"q": {}}}}
        if t["name"] == "search_meetings"
        else t
        for t in _TOOLS
    ]

    drift = detect_mcp_drift(moved, _SERVER, tool_shapes(_TOOLS), _PIN)

    assert drift.kind is DriftClass.BREAKING
    assert drift.changed_schemas == ("search_meetings",)


def test_an_older_server_is_stale_not_broken() -> None:
    """A downgrade is a different source, not a failure."""
    drift = detect_mcp_drift(
        _TOOLS, {**_SERVER, "version": "0.9.0"}, tool_shapes(_TOOLS), _PIN
    )

    assert drift.kind is DriftClass.STALE_SOURCE


def test_a_first_run_establishes_a_baseline() -> None:
    """A fresh archive is not eight tools' worth of drift."""
    drift = detect_mcp_drift(_TOOLS, _SERVER, None, _PIN)

    assert drift.kind is DriftClass.OK


def test_the_pin_moves_when_an_input_schema_moves() -> None:
    """A renamed argument must move the pin even with the tool list intact."""
    moved = [
        {"name": t["name"], "inputSchema": {"type": "object", "properties": {"z": {}}}}
        if t["name"] == "get_meeting"
        else t
        for t in _TOOLS
    ]

    assert pin_from_tools(moved, _SERVER).sha256 != pin_from_tools(_TOOLS, _SERVER).sha256


def test_the_state_ledger_carries_no_timestamp() -> None:
    """A ledger that dated itself would churn the state file every run."""
    shapes = tool_shapes(_TOOLS)

    assert shapes == tool_shapes(_TOOLS)
    assert all(isinstance(value, str) for value in shapes.values())


# --- the authorization flow -----------------------------------------------
#
# This module is the one that mints a credential rather than borrowing one, and
# until now it was covered only by a source-text scan. The tests below exercise
# the decisions that scan cannot see: the CSRF check on the redirect, the PKCE
# relationship, where the token lands, and the two discovery hops that decide
# who this client will talk to.


def _serve(state: str, timeout: float = 5.0) -> tuple[threading.Thread, list[Any]]:
    """Run ``_await_code`` on a free port in a thread.

    Args:
        state: The value the callback must echo back.
        timeout: How long the listener waits.

    Returns:
        The running thread and a one-slot list that receives the outcome.
    """
    port = mcp_auth._free_port()
    outcome: list[Any] = []

    def run() -> None:
        try:
            outcome.append(mcp_auth._await_code(port, state, timeout))
        except Exception as error:
            outcome.append(error)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    time.sleep(0.1)
    return thread, outcome, port  # type: ignore[return-value]


def test_a_matching_state_yields_the_code() -> None:
    """The happy path, so the refusals below are known not to be vacuous."""
    thread, outcome, port = _serve("the-state")

    urllib.request.urlopen(
        f"http://127.0.0.1:{port}/?code=the-code&state=the-state", timeout=5
    ).read()
    thread.join(timeout=5)

    assert outcome == ["the-code"]


def test_a_mismatched_state_is_refused() -> None:
    """The CSRF check: a redirect this process did not start is not accepted.

    Without it, anyone able to reach the loopback listener during the seconds
    it exists could feed in an authorization code of their choosing, and the
    token minted from it would be for their account, not this one.
    """
    thread, outcome, port = _serve("the-state")

    urllib.request.urlopen(
        f"http://127.0.0.1:{port}/?code=the-code&state=not-the-state", timeout=5
    ).read()
    thread.join(timeout=5)

    assert isinstance(outcome[0], mcp_auth.McpAuthError)
    assert "did not match" in str(outcome[0])


def test_an_authorization_error_is_reported_not_swallowed() -> None:
    """A refusal upstream must not look like a timeout."""
    thread, outcome, port = _serve("the-state")

    urllib.request.urlopen(
        f"http://127.0.0.1:{port}/?error=access_denied&state=the-state", timeout=5
    ).read()
    thread.join(timeout=5)

    assert isinstance(outcome[0], mcp_auth.McpAuthError)
    assert "access_denied" in str(outcome[0])


def test_a_response_with_no_code_is_refused() -> None:
    """A 200 that carried nothing usable is still a failure."""
    thread, outcome, port = _serve("the-state")

    urllib.request.urlopen(
        f"http://127.0.0.1:{port}/?state=the-state", timeout=5
    ).read()
    thread.join(timeout=5)

    assert isinstance(outcome[0], mcp_auth.McpAuthError)
    assert "no code" in str(outcome[0])


def test_the_pkce_challenge_is_the_s256_of_the_verifier() -> None:
    """S256, not plain: the challenge must not be the secret it protects."""
    verifier, challenge = mcp_auth._pkce_pair()

    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode()
    )
    assert challenge == expected
    assert challenge != verifier
    assert "=" not in challenge


def test_two_logins_do_not_share_a_verifier() -> None:
    """A predictable verifier would make PKCE decorative."""
    assert mcp_auth._pkce_pair()[0] != mcp_auth._pkce_pair()[0]


def test_the_token_store_is_owner_only(tmp_path: Path) -> None:
    """The minted token is the one credential this tool does write down."""
    target = tmp_path / "nested" / "tokens.json"
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(mcp_auth.paths, "token_store_path", lambda: target)
        mcp_auth.write_store({"refresh_token": FAKE_JWT})

    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700


def test_a_corrupt_token_store_reads_as_absent(tmp_path: Path) -> None:
    """Another login is the remedy; refusing to run would be worse."""
    target = tmp_path / "tokens.json"
    target.write_text("{not json", encoding="utf-8")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(mcp_auth.paths, "token_store_path", lambda: target)

        assert mcp_auth.read_store() == {}


class _FakeResponse:
    """The two attributes ``_get_json`` reads."""

    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        """Return the decoded document."""
        return self._payload

    @property
    def text(self) -> str:
        """Return the body, for the error path."""
        return json.dumps(self._payload)


class _FakeClient:
    """Serves a fixed map of URL to document."""

    def __init__(self, documents: dict[str, Any]) -> None:
        self.documents = documents

    def get(self, url: str, **_: Any) -> _FakeResponse:
        """Return the document registered for ``url``, or a 404."""
        if url in self.documents:
            return _FakeResponse(self.documents[url])
        return _FakeResponse({"error": "not found"}, status_code=404)


_RESOURCE = "https://api.wisprflow.ai/connect/mcp"
_PROTECTED = "https://api.wisprflow.ai/.well-known/oauth-protected-resource/connect/mcp"


def test_discovery_refuses_a_non_https_authorization_server() -> None:
    """The resource document chooses where a token is exchanged.

    PKCE and ``state`` protect the code in flight. Neither helps when the
    issuer itself is the attacker, so the hop is checked rather than followed.
    """
    client = _FakeClient(
        {_PROTECTED: {"authorization_servers": ["http://issuer.example"]}}
    )

    with pytest.raises(mcp_auth.McpAuthError, match="non-https"):
        mcp_auth.discover(client, _RESOURCE)


def test_discovery_refuses_metadata_that_names_another_issuer() -> None:
    """RFC 8414 section 3.3: the issuer must match where it was fetched from.

    Without the check, a document served at one issuer can name a different
    one's endpoints and nothing notices.
    """
    client = _FakeClient(
        {
            _PROTECTED: {"authorization_servers": ["https://issuer.example"]},
            "https://issuer.example/.well-known/oauth-authorization-server": {
                "issuer": "https://somewhere-else.example",
                "token_endpoint": "https://somewhere-else.example/token",
            },
        }
    )

    with pytest.raises(mcp_auth.McpAuthError, match="claims issuer"):
        mcp_auth.discover(client, _RESOURCE)


def test_discovery_accepts_a_consistent_advertisement() -> None:
    """The refusals above must not be refusing everything."""
    client = _FakeClient(
        {
            _PROTECTED: {
                "authorization_servers": ["https://issuer.example"],
                "resource": _RESOURCE,
            },
            "https://issuer.example/.well-known/oauth-authorization-server": {
                "issuer": "https://issuer.example",
                "token_endpoint": "https://issuer.example/token",
            },
        }
    )

    metadata = mcp_auth.discover(client, _RESOURCE)

    assert metadata["token_endpoint"] == "https://issuer.example/token"
    assert metadata["resource"] == _RESOURCE
