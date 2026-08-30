"""Getting a token for the MCP server, and why this one is minted.

Every other credential path in this tool borrows. ``cloud_auth`` reads the
access token Wispr Flow already holds and refuses to refresh it, because
Supabase GoTrue rotates refresh tokens and detects reuse -- refreshing would
invalidate the desktop app's own session and sign the user out of the app being
backed up.

**That rule is about borrowing, not about refreshing.** Measured against the
live service: the MCP server is a separate OAuth 2.0 protected resource whose
authorization server is a different issuer entirely, and it answers the
borrowed Supabase token with ``401 invalid_token`` -- bare and as a Bearer.
There is no borrowing to be done here. So this backend registers a client of its
own and holds its own token, and refreshing *that* cannot touch the app's
session because it was never the app's session.

Stated as the invariant it actually is:

    Never refresh a borrowed credential. A credential this tool minted for
    itself is its own to manage.

The grant is authorization code with PKCE over a loopback redirect. The device
grant would have been preferable for a command-line tool -- nothing to listen
on, works over SSH -- and the authorization server advertises it in
``grant_types_supported``. It does not work: dynamic registration refuses to
register a client for it ("each value in grant_types must be one of the
following values: authorization_code, refresh_token"), and asking the device
endpoint anyway answers ``unauthorized_client: Device authorization is not
enabled for this application``. Both measured. So the loopback listener is not
a design preference; it is the only grant open to a client this tool can
register.

The listener binds a fixed loopback port, accepts exactly one request, and
stops. It is bound to ``127.0.0.1`` rather than ``0.0.0.0``, so nothing off the
machine can reach it even for the seconds it exists.

Every endpoint is discovered rather than hardcoded: the resource advertises its
authorization server under RFC 9728, and the authorization server advertises its
endpoints under RFC 8414. A server that moves them is followed automatically,
and ``MAINTENANCE.md`` records how to re-check by hand.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import socket
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import USER_AGENT, paths
from .secure_io import write_json

#: The MCP endpoint, from the desktop app bundle. Overridable for testing.
DEFAULT_MCP_ENDPOINT = "https://api.wisprflow.ai/connect/mcp"

#: Environment override, mirroring WISPR_API_BASE.
ENDPOINT_ENV = "WISPR_MCP_ENDPOINT"

#: What this client asks for. ``offline_access`` is what makes a refresh token
#: available, so a login lasts longer than one access token.
SCOPES = "openid offline_access"

#: Refresh this many seconds before the token actually expires, so a long run
#: cannot have it die mid-pass.
EXPIRY_MARGIN = 120.0

#: Loopback ports offered at registration, tried in order at login. Registering
#: all of them up front means a busy port does not require re-registering the
#: client, which would leave an orphan behind on the server every time.
CALLBACK_PORTS = (53682, 53683, 53684)

#: Give up waiting for the browser round trip after this long. Generous on
#: purpose: the operator may have to sign in, pick an account and read a
#: consent screen, and a listener that gave up first would send them back to
#: the terminal to start over.
LOGIN_TIMEOUT = 900.0

REQUEST_TIMEOUT = 30.0


class McpAuthError(Exception):
    """No usable MCP credential, with a reason worth showing the operator."""


@dataclass(frozen=True, slots=True)
class McpCredential:
    """A minted access token and where it came from.

    Attributes:
        token: The access token.
        origin: ``"environment"`` or ``"token store"``, for diagnostics.
        expires_at: Unix seconds, or ``None`` when the server did not say.
    """

    token: str
    origin: str
    expires_at: float | None = None

    def header(self) -> dict[str, str]:
        """Build the Authorization header for one request.

        Unlike the REST API -- which rejects the scheme and wants the token
        bare -- this resource advertises ``bearer_methods_supported:
        ["header"]`` and answers only to a Bearer. The two are genuinely
        different services and the difference is measured, not assumed.

        Returns:
            A single-entry mapping.
        """
        return {"Authorization": f"Bearer {self.token}"}

    def __repr__(self) -> str:
        """Render without the token, so a traceback cannot leak it."""
        return f"McpCredential(origin={self.origin!r}, expires_at={self.expires_at!r})"


def mcp_endpoint() -> str:
    """Return the MCP endpoint to talk to.

    Returns:
        The configured endpoint, or the app's own.
    """
    return os.environ.get(ENDPOINT_ENV, "").strip() or DEFAULT_MCP_ENDPOINT


# --- discovery ------------------------------------------------------------


def _get_json(client: Any, url: str) -> dict[str, Any]:
    """Fetch and decode one JSON document.

    Args:
        client: An open ``httpx.Client``.
        url: Absolute URL.

    Returns:
        The decoded document.

    Raises:
        McpAuthError: The document could not be fetched or parsed.
    """
    response = client.get(url, timeout=REQUEST_TIMEOUT)
    if response.status_code != 200:
        raise McpAuthError(f"{url} returned HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as error:
        raise McpAuthError(f"{url} did not return JSON") from error
    if not isinstance(payload, dict):
        raise McpAuthError(f"{url} did not return an object")
    return payload


def discover(client: Any, endpoint: str | None = None) -> dict[str, Any]:
    """Find the authorization server's endpoints, following the advertisements.

    Args:
        client: An open ``httpx.Client``.
        endpoint: The MCP resource URL. Defaults to the configured one.

    Returns:
        The authorization server metadata, with ``resource`` added.

    Raises:
        McpAuthError: Discovery failed at any hop.
    """
    import httpx

    resource = endpoint or mcp_endpoint()
    parsed = httpx.URL(resource)
    origin = f"{parsed.scheme}://{parsed.netloc.decode()}"

    # RFC 9728 puts the path after the well-known segment; servers vary on
    # whether they also answer the bare form, so try the specific one first.
    candidates = [
        f"{origin}/.well-known/oauth-protected-resource{parsed.path}",
        f"{origin}/.well-known/oauth-protected-resource",
    ]
    protected: dict[str, Any] | None = None
    for url in candidates:
        try:
            protected = _get_json(client, url)
            break
        except McpAuthError:
            continue
    if protected is None:
        raise McpAuthError(
            f"{resource} does not advertise OAuth metadata; it may no longer "
            "be an OAuth-protected MCP endpoint. See MAINTENANCE.md."
        )

    servers = protected.get("authorization_servers")
    if not isinstance(servers, list) or not servers:
        raise McpAuthError("the resource named no authorization server")
    issuer = str(servers[0]).rstrip("/")

    metadata: dict[str, Any] | None = None
    for url in (
        f"{issuer}/.well-known/oauth-authorization-server",
        f"{issuer}/.well-known/openid-configuration",
    ):
        try:
            metadata = _get_json(client, url)
            break
        except McpAuthError:
            continue
    if metadata is None:
        raise McpAuthError(f"{issuer} published no authorization server metadata")

    metadata["resource"] = protected.get("resource", resource)
    return metadata


# --- the token store ------------------------------------------------------


def read_store() -> dict[str, Any]:
    """Read the saved client registration and tokens.

    Returns:
        The store, or an empty mapping when there is none. A corrupt store is
        treated as absent rather than fatal: the remedy is another login, and
        refusing to run because a cache is unreadable would be worse than
        re-minting.
    """
    try:
        payload = json.loads(paths.token_store_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def write_store(payload: dict[str, Any]) -> None:
    """Persist the client registration and tokens, owner-readable only.

    Args:
        payload: The store to write.
    """
    # write_json goes through secure_mkdir/secure_write_text, so the directory
    # is 0700 and the file 0600 from creation rather than after a chmod.
    write_json(paths.token_store_path(), payload)


def forget() -> bool:
    """Delete the token store.

    Returns:
        ``True`` when a store was removed, ``False`` when there was none.
    """
    store = paths.token_store_path()
    try:
        store.unlink()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise McpAuthError(f"could not remove {store}: {error}") from error
    return True


# --- registration and the device grant ------------------------------------


def register_client(client: Any, metadata: dict[str, Any]) -> dict[str, Any]:
    """Register this tool as a public OAuth client.

    Dynamic registration means there is no client secret to embed in a
    source-available tool, and no shared identity between installations.

    Args:
        client: An open ``httpx.Client``.
        metadata: Authorization server metadata from :func:`discover`.

    Returns:
        The registration response, including ``client_id``.

    Raises:
        McpAuthError: The server refused to register the client.
    """
    url = metadata.get("registration_endpoint")
    if not url:
        raise McpAuthError(
            "the authorization server does not support dynamic client "
            "registration, so this tool has no client id to use"
        )
    body = {
        "client_name": "wispr-flow-exporter",
        "redirect_uris": [_redirect_uri(port) for port in CALLBACK_PORTS],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        # Public client: no secret to embed in a source-available tool. Omit
        # this and the server issues one, which would be a credential this tool
        # has no safe place to keep.
        "token_endpoint_auth_method": "none",
        "scope": SCOPES,
    }
    response = client.post(url, json=body, timeout=REQUEST_TIMEOUT)
    if response.status_code not in (200, 201):
        # Carry the server's own complaint. A bare status here cost real time
        # once: a 422 said nothing, and the actual reason was that the device
        # grant is not registrable.
        raise McpAuthError(
            f"client registration returned HTTP {response.status_code}: "
            f"{response.text[:300]}"
        )
    try:
        payload = response.json()
    except ValueError as error:
        raise McpAuthError("client registration did not return JSON") from error
    if not isinstance(payload, dict) or not payload.get("client_id"):
        raise McpAuthError("client registration returned no client id")
    return payload


def _redirect_uri(port: int) -> str:
    """Build the loopback redirect for one port.

    Args:
        port: The port the listener will bind.

    Returns:
        The redirect URI.
    """
    return f"http://127.0.0.1:{port}/callback"


def _pkce_pair() -> tuple[str, str]:
    """Generate a PKCE verifier and its S256 challenge.

    Returns:
        ``(verifier, challenge)``.
    """
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


class _Callback(BaseHTTPRequestHandler):
    """A one-shot handler that captures the authorization code."""

    query: dict[str, list[str]] = {}

    def do_GET(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        """Record the query string and tell the browser it can close."""
        _Callback.query = parse_qs(urlparse(self.path).query)
        body = (
            b"<html><body style='font-family:sans-serif'>"
            b"<h3>wispr-flow-exporter is authorized.</h3>"
            b"<p>You can close this tab and return to the terminal.</p>"
            b"</body></html>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        """Stay quiet; the CLI owns this terminal."""


def _await_code(port: int, state: str, timeout: float) -> str:
    """Serve one loopback request and return the authorization code.

    Args:
        port: The port to bind.
        state: The value the response must echo back.
        timeout: How long to wait for the browser.

    Returns:
        The authorization code.

    Raises:
        McpAuthError: The wait timed out, the state did not match, or the
            server returned an error instead of a code.
    """
    _Callback.query = {}
    # 127.0.0.1, not 0.0.0.0: nothing off this machine can reach the listener
    # even for the few seconds it exists.
    server = HTTPServer(("127.0.0.1", port), _Callback)
    server.timeout = timeout
    try:
        server.handle_request()
    finally:
        server.server_close()

    query = _Callback.query
    _Callback.query = {}
    if not query:
        raise McpAuthError("timed out waiting for the browser to come back")
    if query.get("error"):
        raise McpAuthError(f"authorization failed: {query['error'][0]}")
    # Checked before the code is used: a mismatch means this response belongs
    # to a different request than the one this process started.
    if query.get("state", [""])[0] != state:
        raise McpAuthError("the authorization response did not match the request")
    code = query.get("code", [""])[0]
    if not code:
        raise McpAuthError("the authorization response carried no code")
    return code


def _free_port() -> int:
    """Pick the first registered loopback port that is free.

    Returns:
        The port.

    Raises:
        McpAuthError: Every registered port is in use.
    """
    for port in CALLBACK_PORTS:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise McpAuthError(
        "every callback port is in use: " + ", ".join(str(p) for p in CALLBACK_PORTS)
    )


def authorize(
    client: Any,
    metadata: dict[str, Any],
    client_id: str,
    *,
    announce: Any = print,
    open_browser: bool = True,
) -> dict[str, Any]:
    """Run the authorization code flow with PKCE and return the tokens.

    Args:
        client: An open ``httpx.Client``.
        metadata: Authorization server metadata.
        client_id: This tool's registered client id.
        announce: Where to print the URL.
        open_browser: Whether to try opening a browser.

    Returns:
        The token response.

    Raises:
        McpAuthError: Any step failed.
    """
    port = _free_port()
    redirect_uri = _redirect_uri(port)
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(32)
    resource = metadata.get("resource") or mcp_endpoint()

    query = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        # RFC 8707. MCP requires the resource indicator so the issued token is
        # scoped to this server and cannot be replayed against another.
        "resource": resource,
    }
    url = f"{metadata['authorization_endpoint']}?{urlencode(query)}"

    announce(f"  Open: {url}")
    announce("  Waiting for authorization...")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # pragma: no cover - platform dependent
            pass

    code = _await_code(port, state, LOGIN_TIMEOUT)
    response = client.post(
        metadata["token_endpoint"],
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": verifier,
            "resource": resource,
        },
        timeout=REQUEST_TIMEOUT,
    )
    if response.status_code != 200:
        raise McpAuthError(
            f"the token exchange returned HTTP {response.status_code}: "
            f"{response.text[:300]}"
        )
    payload = response.json()
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise McpAuthError("the token exchange returned no access token")
    return payload


def refresh(
    client: Any, metadata: dict[str, Any], client_id: str, refresh_token: str
) -> dict[str, Any]:
    """Exchange a refresh token for a fresh access token.

    Safe in a way ``cloud_auth`` deliberately is not: this refresh token is one
    this tool minted for itself against a different issuer, so rotating it
    cannot disturb the desktop app's session.

    Args:
        client: An open ``httpx.Client``.
        metadata: Authorization server metadata.
        client_id: This tool's registered client id.
        refresh_token: The stored refresh token.

    Returns:
        The token response.

    Raises:
        McpAuthError: The refresh was refused.
    """
    response = client.post(
        metadata["token_endpoint"],
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "resource": metadata.get("resource") or mcp_endpoint(),
        },
        timeout=REQUEST_TIMEOUT,
    )
    if response.status_code != 200:
        raise McpAuthError(
            "the stored authorization is no longer valid; run "
            "`wispr-export login` again"
        )
    payload = response.json()
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise McpAuthError("the refresh returned no access token")
    return payload


def _save_tokens(store: dict[str, Any], tokens: dict[str, Any]) -> dict[str, Any]:
    """Merge a token response into the store and persist it.

    Args:
        store: The current store.
        tokens: A token endpoint response.

    Returns:
        The updated store.
    """
    expires_in = tokens.get("expires_in")
    store["access_token"] = tokens["access_token"]
    # A rotated refresh token replaces the old one; a response without one
    # means the server kept the existing token valid.
    if tokens.get("refresh_token"):
        store["refresh_token"] = tokens["refresh_token"]
    store["expires_at"] = (
        time.time() + float(expires_in) if expires_in is not None else None
    )
    write_store(store)
    return store


def login(client: Any, *, announce: Any = print) -> McpCredential:
    """Run the device grant end to end and save the result.

    Args:
        client: An open ``httpx.Client``.
        announce: Where to print the verification URL and code.

    Returns:
        The minted credential.

    Raises:
        McpAuthError: Any step failed.
    """
    metadata = discover(client)
    store = read_store()
    client_id = store.get("client_id")
    if not client_id:
        registration = register_client(client, metadata)
        client_id = registration["client_id"]
        store["client_id"] = client_id
        store["issuer"] = metadata.get("issuer")
        write_store(store)

    tokens = authorize(client, metadata, client_id, announce=announce)
    store = _save_tokens(store, tokens)
    return McpCredential(
        token=store["access_token"], origin="token store", expires_at=store["expires_at"]
    )


def resolve_credential(client: Any) -> McpCredential:
    """Find a usable MCP token, refreshing if needed, without prompting.

    Args:
        client: An open ``httpx.Client``.

    Returns:
        The credential.

    Raises:
        McpAuthError: There is no stored authorization, or it can no longer be
            refreshed. The remedy is ``wispr-export login``, and saying so is
            better than opening a browser in the middle of a batch run.
    """
    override = os.environ.get("WISPR_MCP_TOKEN", "").strip()
    if override:
        return McpCredential(token=override, origin="environment")

    store = read_store()
    token = store.get("access_token")
    expires_at = store.get("expires_at")
    if token and (expires_at is None or expires_at - EXPIRY_MARGIN > time.time()):
        return McpCredential(token=token, origin="token store", expires_at=expires_at)

    refresh_token = store.get("refresh_token")
    client_id = store.get("client_id")
    if not refresh_token or not client_id:
        raise McpAuthError(
            "no MCP authorization stored. Run `wispr-export login` first."
        )
    metadata = discover(client)
    store = _save_tokens(store, refresh(client, metadata, client_id, refresh_token))
    return McpCredential(
        token=store["access_token"], origin="token store", expires_at=store["expires_at"]
    )
