"""The cloud backend: credentials, transport, and archiving verbatim.

Every test here runs against ``httpx.MockTransport`` or a protocol fake. The
suite never contacts Wispr Flow, which is both a testing convenience and the
point: the one thing this backend must never do is talk to the refresh
endpoint, and a test that reached the network could not prove it did not.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from wispr_flow_exporter import cloud_auth
from wispr_flow_exporter.cloud_api import ENDPOINTS, CloudClient, CloudError
from wispr_flow_exporter.cloud_auth import (
    Credential,
    CloudAuthError,
    resolve_credential,
)
from wispr_flow_exporter.store import Archive
from wispr_flow_exporter.sync import SyncOptions
from wispr_flow_exporter.sync_cloud import SOURCE_CLOUD, sync_cloud

from conftest import FAKE_JWT, FAKE_SESSION_KEY, OWNER_EMAIL

CREDENTIAL = Credential(token=FAKE_JWT, origin="test")


def _session_file(directory: Path, *, expires_in: int = 3600) -> Path:
    """Write a Wispr Flow session file.

    Args:
        directory: Where to write it.
        expires_in: Seconds until expiry; may be negative.

    Returns:
        The path written.
    """
    inner = {
        "access_token": FAKE_JWT,
        "refresh_token": "refresh-token",
        "expires_at": int(
            (datetime.now(tz=UTC) + timedelta(seconds=expires_in)).timestamp()
        ),
        "user": {"id": "user-1", "email": OWNER_EMAIL},
    }
    path = directory / "session.json"
    path.write_text(json.dumps({FAKE_SESSION_KEY: json.dumps(inner)}), encoding="utf-8")
    return path


class _Recorder:
    """A protocol-satisfying fake that records what was asked for."""

    def __init__(self, payloads: dict[str, Any]) -> None:
        """Store the canned responses.

        Args:
            payloads: Endpoint name to response body.
        """
        self.payloads = payloads
        self.asked: list[str] = []
        self.failures: list[tuple[str, str]] = []

    def fetch(self, name: str) -> Any:
        """Return a canned response.

        Args:
            name: Endpoint name.

        Returns:
            The body, or ``None`` when the fake was not given one.
        """
        self.asked.append(name)
        if name not in self.payloads:
            self.failures.append((name, "not configured"))
            return None
        return self.payloads[name]


def _client(handler: Any, **kwargs: Any) -> CloudClient:
    """Build a client wired to a mock transport.

    Deliberately not used with ``with``: entering the context builds a real
    httpx.Client, which would replace the mock installed here. An earlier
    version of this helper did exactly that and four tests silently exercised
    a live transport instead of the handler.

    Args:
        handler: A MockTransport handler.
        **kwargs: Passed to :class:`CloudClient`.

    Returns:
        A client ready to fetch, which the caller closes.
    """
    client = CloudClient(CREDENTIAL, **kwargs)
    client._client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url=client.base_url,
        headers={"Authorization": f"Bearer {FAKE_JWT}"},
    )
    return client


# --- credentials ----------------------------------------------------------


def test_the_environment_token_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CI and containers have no Wispr Flow install to borrow from."""
    monkeypatch.setenv("WISPR_ACCESS_TOKEN", "env-token")
    _session_file(tmp_path)

    credential = resolve_credential(tmp_path / "session.json")

    assert credential.token == "env-token"
    assert credential.origin == "environment"


def test_the_stored_session_is_used_when_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The credential belongs to the app; this borrows it."""
    monkeypatch.delenv("WISPR_ACCESS_TOKEN", raising=False)
    _session_file(tmp_path)

    credential = resolve_credential(tmp_path / "session.json")

    assert credential.token == FAKE_JWT
    assert credential.origin == "session.json"
    assert credential.header() == {"Authorization": f"Bearer {FAKE_JWT}"}


def test_an_expired_token_stops_rather_than_refreshing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refreshing would sign the user out of the app being backed up.

    Supabase GoTrue rotates refresh tokens and detects reuse, so a second
    client that refreshes either revokes the desktop app's session or races
    it. Stopping is the correct behaviour, and the message says what to do.
    """
    monkeypatch.delenv("WISPR_ACCESS_TOKEN", raising=False)
    _session_file(tmp_path, expires_in=-60)

    with pytest.raises(CloudAuthError, match="Open Wispr Flow"):
        resolve_credential(tmp_path / "session.json")


def test_a_missing_session_is_explained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A local-only user must get a reason, not a traceback."""
    monkeypatch.delenv("WISPR_ACCESS_TOKEN", raising=False)

    with pytest.raises(CloudAuthError, match="no Wispr Flow session"):
        resolve_credential(tmp_path / "absent.json")


def test_no_refresh_endpoint_appears_anywhere_in_the_backend() -> None:
    """The invariant, asserted against the source rather than described.

    There is no refresh code path to be reached by accident, and this test
    fails if one is ever added.
    """
    root = Path(cloud_auth.__file__).parent
    for module in ("cloud_auth.py", "cloud_api.py", "sync_cloud.py"):
        body = (root / module).read_text(encoding="utf-8")
        assert "grant_type" not in body
        assert "/auth/v1/token" not in body


def test_every_declared_endpoint_is_read_only() -> None:
    """The client issues GET only; nothing here can write to Wispr Flow."""
    body = (Path(cloud_auth.__file__).parent / "cloud_api.py").read_text(
        encoding="utf-8"
    )
    assert ".post(" not in body
    assert ".put(" not in body
    assert ".delete(" not in body
    assert ".patch(" not in body


# --- transport ------------------------------------------------------------


def test_a_successful_fetch_returns_the_body() -> None:
    """The happy path, and the only shape assumption: it decodes as JSON."""
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(200, json={"items": [{"id": "m-1"}]})

    client = _client(handler)
    assert client.fetch("meetings") == {"items": [{"id": "m-1"}]}
    assert client.failures == []


def test_a_server_error_is_retried_then_recorded() -> None:
    """Transient failures get a ladder; a persistent one is reported."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503)

    client = _client(handler)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("wispr_flow_exporter.cloud_api.BACKOFF", (0, 0, 0, 0))
        patch.setattr("wispr_flow_exporter.cloud_api.MIN_INTERVAL", 0)
        assert client.fetch("notes") is None

    assert calls["n"] > 1
    assert client.failures == [("notes", "HTTP 503")]


def test_a_rejected_token_is_not_retried() -> None:
    """A 401 will not become a 200 by asking again, and the reason is useful."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401)

    client = _client(handler)
    assert client.fetch("meetings") is None

    assert calls["n"] == 1
    assert "Open Wispr Flow" in client.failures[0][1]


def test_a_non_json_response_is_reported_not_guessed() -> None:
    """An HTML error page is not a record set."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>maintenance</html>")

    client = _client(handler)
    assert client.fetch("todos") is None

    assert client.failures == [("todos", "response was not JSON")]


def test_an_unknown_endpoint_is_a_programming_error() -> None:
    """The endpoint set is data, and asking outside it is a bug not a request."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    client = _client(handler)
    with pytest.raises(CloudError, match="unknown endpoint"):
        client.fetch("nope")


def test_the_client_refuses_to_work_unopened() -> None:
    """Using the transport without entering the context is a bug."""
    with pytest.raises(CloudError, match="context manager"):
        CloudClient(CREDENTIAL).fetch("meetings")


def test_a_hostile_retry_after_cannot_park_the_run() -> None:
    """A broken header must not stop a backup for an hour."""
    from wispr_flow_exporter.cloud_api import _retry_after

    assert _retry_after("3") == 3.0
    assert _retry_after("100000") == 60.0
    assert _retry_after("-5") == 0.0
    assert _retry_after("soon") is None
    assert _retry_after(None) is None


# --- the pass -------------------------------------------------------------


def test_responses_are_archived_verbatim(tmp_path: Path) -> None:
    """The API is not a contract, so nothing is reshaped on the way in.

    A response whose structure this tool does not recognize is still preserved
    losslessly and can be re-read once the shape is known.
    """
    archive = Archive(root=tmp_path / "archive")
    payload = {"items": [{"id": "m-1", "unexpected": {"nested": True}}]}
    client = _Recorder({"meetings": payload})

    counts = sync_cloud(archive, client, SyncOptions(), endpoints=("meetings",))

    assert counts.written == 1
    stored = json.loads(
        (archive.root / "cloud" / "meetings.json").read_text(encoding="utf-8")
    )
    assert stored == payload
    assert archive.entry("cloud", "meetings")["records"] == 1
    assert archive.entry("cloud", "meetings")["source"] == SOURCE_CLOUD


def test_an_unrecognized_shape_reports_an_unknown_count(tmp_path: Path) -> None:
    """Guessing a count from a shape nobody has confirmed would be a lie."""
    archive = Archive(root=tmp_path / "archive")
    client = _Recorder({"user_profile": {"email": OWNER_EMAIL}})

    sync_cloud(archive, client, SyncOptions(), endpoints=("user_profile",))

    # put() drops None, so "unknown" is recorded as absence rather than as a
    # null that would read like a count of nothing.
    assert "records" not in archive.entry("cloud", "user_profile")


def test_a_second_cloud_pass_writes_nothing(tmp_path: Path) -> None:
    """The zero-bytes invariant holds for the cloud backend too."""
    archive = Archive(root=tmp_path / "archive")
    client = _Recorder({"notes": [{"id": "n-1"}]})
    sync_cloud(archive, client, SyncOptions(), endpoints=("notes",))
    before = (archive.root / "cloud" / "notes.json").stat().st_mtime_ns

    counts = sync_cloud(archive, client, SyncOptions(), endpoints=("notes",))

    assert counts.unchanged == 1
    assert counts.written == 0
    assert (archive.root / "cloud" / "notes.json").stat().st_mtime_ns == before


def test_a_failed_endpoint_is_recorded_not_fatal(tmp_path: Path) -> None:
    """The local archive is the primary artifact and must survive this."""
    archive = Archive(root=tmp_path / "archive")
    client = _Recorder({"notes": [{"id": "n-1"}]})

    counts = sync_cloud(
        archive, client, SyncOptions(), endpoints=("notes", "meetings")
    )

    assert counts.written == 1
    assert counts.failed == 1
    assert archive.entry("cloud", "meetings")["last_error"] == "not configured"


def test_a_dry_run_fetches_but_writes_nothing(tmp_path: Path) -> None:
    """Reporting what would be archived must not archive it."""
    archive = Archive(root=tmp_path / "archive")
    client = _Recorder({"notes": [{"id": "n-1"}]})

    counts = sync_cloud(
        archive, client, SyncOptions(dry_run=True), endpoints=("notes",)
    )

    assert counts.written == 1
    assert not (archive.root / "cloud").exists()


def test_the_endpoint_set_is_auditable_and_read_only() -> None:
    """Every endpoint this tool may touch is visible in one place."""
    assert all(path.startswith("/api/v1/") for path in ENDPOINTS.values())
    assert "meetings" in ENDPOINTS
    assert "dictionary_personal" in ENDPOINTS
