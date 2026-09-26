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

The listener is bound to ``127.0.0.1`` rather than ``0.0.0.0``, so nothing off
the machine can reach it, and it is bound *before* the browser is sent
anywhere, so a browser that comes straight back finds it waiting. It answers
only a request for ``/callback`` carrying this login's ``state``: anything else
-- a favicon, a port scan, a forged ``error`` -- is turned away without ending
the login, and a connection that sends nothing is dropped after ten seconds
rather than holding the listener until someone notices.

Every endpoint is discovered rather than hardcoded -- the resource advertises
its authorization server under RFC 9728, and that server advertises its
endpoints under RFC 8414 -- but nothing is trusted for having been discovered.
The resource document must name the configured endpoint; the shipped endpoint
must name the shipped issuer unless ``WISPR_ALLOW_ENDPOINT_OVERRIDE`` is set;
every endpoint the issuer advertises must be https on the issuer's own host;
and a stored token is bound to the resource and issuer that minted it, so it
is never sent anywhere else. ``MAINTENANCE.md`` records how to re-check all of
it by hand.
"""

from __future__ import annotations

import base64
import errno
import hashlib
import json
import math
import os
import secrets
import time
import webbrowser
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Literal
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

from . import USER_AGENT, paths
from .endpoints import OVERRIDE_ENV, EndpointError, host_of, same_host_https
from .local_config import printable, redact
from .secure_io import secure_mkdir, write_json
from .transport import (
    ACCEPT_ENCODING,
    NotJson,
    ResponseRefused,
    decode_json,
    read_capped,
)

#: The MCP endpoint, from the desktop app bundle.
DEFAULT_MCP_ENDPOINT = "https://api.wisprflow.ai/connect/mcp"

#: The authorization server the shipped endpoint names. Measured: its resource
#: document lists exactly this one, and every endpoint it advertises is on this
#: host. See ``MAINTENANCE.md`` for re-checking it.
DEFAULT_ISSUER = "https://mcp-auth.wisprflow.com"

#: The one issuer each shipped endpoint may name without the override. The
#: resource document decides where this client registers and where its codes
#: and refresh tokens go, so a document naming another issuer is refused.
SHIPPED_ISSUERS: Mapping[str, str] = {DEFAULT_MCP_ENDPOINT: DEFAULT_ISSUER}

#: What this client asks for. ``offline_access`` is what makes a refresh token
#: available, so a login lasts longer than one access token.
SCOPES = "openid offline_access"

#: Refresh this many seconds before the token actually expires, so a long run
#: cannot have it die mid-pass.
EXPIRY_MARGIN = 120.0

#: The lifetime assumed for a token whose response did not state one. Measured
#: tokens live seven days; an hour errs short, and a 401 refreshes regardless.
#: Until 0.5.0 such a token was trusted forever.
ASSUMED_LIFETIME = 3600.0

#: Loopback ports offered at registration, tried in order at login. Registering
#: all of them up front means a busy port does not require re-registering the
#: client, which would leave an orphan behind on the server every time.
CALLBACK_PORTS = (53682, 53683, 53684)

#: Give up waiting for the browser round trip after this long. Generous on
#: purpose: the operator may have to sign in, pick an account and read a
#: consent screen, and a listener that gave up first would send them back to
#: the terminal to start over.
LOGIN_TIMEOUT = 900.0

#: How long one connection to the listener may take to send its request. An
#: idle connection used to hold the listener past its own deadline.
CONNECTION_TIMEOUT = 10.0

REQUEST_TIMEOUT = 30.0

#: Every OAuth answer is a small JSON document; a megabyte is generous. These
#: requests used to read whole bodies, with no cap at all.
AUTH_RESPONSE_BYTES = 1024 * 1024

#: How long to wait for another run that is refreshing the same token.
LOCK_TIMEOUT = 30.0


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


@dataclass(frozen=True, slots=True)
class AuthServer:
    """Where this client registers, sends the browser, and exchanges codes.

    Everything here passed :func:`discover`'s checks; nothing reaches it any
    other way.

    Attributes:
        resource: The MCP endpoint, as its own metadata names it.
        issuer: The authorization server.
        authorization_endpoint: Where the operator's browser is sent.
        token_endpoint: Where codes and refresh tokens are exchanged.
        registration_endpoint: Where a client registers, when supported.
        iss_in_callback: Whether the server promises RFC 9207's ``iss`` on
            the redirect, which the listener then requires.
    """

    resource: str
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str | None
    iss_in_callback: bool


def _override_allowed() -> bool:
    """Report whether the operator has opted into hosts this tool does not ship.

    Read from the real environment only: a ``.env`` cannot set it.

    Returns:
        ``True`` when :data:`OVERRIDE_ENV` is set to a true value.
    """
    value = os.environ.get(OVERRIDE_ENV, "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def open_client(transport: Any = None) -> Any:
    """Open the HTTP client every OAuth request goes through.

    Args:
        transport: An httpx transport to use instead of the network, for tests.

    Returns:
        An open ``httpx.Client``, which the caller closes.
    """
    import httpx

    return httpx.Client(
        timeout=REQUEST_TIMEOUT,
        headers={
            "Accept": "application/json",
            "Accept-Encoding": ACCEPT_ENCODING,
            "User-Agent": USER_AGENT,
        },
        transport=transport,
    )


# --- requests -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Answer:
    """One OAuth response, read under the cap.

    Attributes:
        status: The HTTP status.
        payload: The decoded JSON body, or ``None`` when it was not JSON.
        excerpt: The body as printable text, for a refusal's message.
    """

    status: int
    payload: Any
    excerpt: str


def _exchange(
    client: Any,
    method: str,
    url: str,
    *,
    what: str,
    form: Mapping[str, str] | None = None,
    body: Mapping[str, Any] | None = None,
) -> _Answer:
    """Make one OAuth request and read its answer, or fail as an auth error.

    Every OAuth request goes through here. Before 0.5.0 each read a whole body
    with ``.json()`` or ``.text``, and a network failure, a body that was not
    JSON or a missing field escaped as a traceback. Measured on 0.4.1: a
    128 MiB metadata document peaked at 270 MiB before discovery gave up.

    Args:
        client: An open ``httpx.Client``.
        method: ``"GET"`` or ``"POST"``.
        url: Absolute URL, already checked.
        what: What the request is, for the error message.
        form: A form body, for the token endpoint.
        body: A JSON body, for registration.

    Returns:
        The status, the decoded body and an excerpt of it.

    Raises:
        McpAuthError: The request did not complete, or its body was refused.
    """
    import httpx

    try:
        with client.stream(
            method, url, data=form, json=body, timeout=REQUEST_TIMEOUT
        ) as response:
            status = response.status_code
            raw = read_capped(response, limit=AUTH_RESPONSE_BYTES)
    except (
        httpx.HTTPError,
        httpx.InvalidURL,
        httpx.StreamError,
        ResponseRefused,
    ) as error:
        reason = redact(str(error)) or type(error).__name__
        raise McpAuthError(f"{what} failed: {printable(reason, limit=300)}") from error
    try:
        payload = decode_json(raw) if raw else None
    except NotJson:
        payload = None
    text = " ".join(raw.decode("utf-8", errors="replace").split())
    return _Answer(status, payload, printable(redact(text), limit=300))


def _document(client: Any, url: str) -> dict[str, Any] | None:
    """Fetch one metadata document.

    Args:
        client: An open ``httpx.Client``.
        url: Where the document may be.

    Returns:
        The document, or ``None`` when this URL does not serve one. A failure
        to reach the host at all is not "not served here", and raises.
    """
    answer = _exchange(client, "GET", url, what="OAuth discovery")
    if answer.status == 200 and isinstance(answer.payload, dict):
        return answer.payload
    return None


# --- discovery ------------------------------------------------------------


def _issuer(servers: object, endpoint: str) -> str:
    """Choose which authorization server named by a resource document to use.

    Args:
        servers: The document's ``authorization_servers``.
        endpoint: The configured MCP endpoint.

    Returns:
        The issuer, without a trailing slash.

    Raises:
        McpAuthError: None was named, the shipped endpoint named an issuer
            this tool does not ship without the override, or the issuer is
            not https.
    """
    if not isinstance(servers, list) or not servers:
        raise McpAuthError("the resource named no authorization server")
    named = [str(server).rstrip("/") for server in servers]
    shipped = SHIPPED_ISSUERS.get(endpoint)
    if shipped in named:
        return shipped
    if shipped is not None and not _override_allowed():
        hosts = ", ".join(sorted({host_of(name) or name for name in named}))
        raise McpAuthError(
            f"{endpoint} names {printable(hosts, limit=200)} as its authorization "
            f"server, not {host_of(shipped)}. Set {OVERRIDE_ENV}=1 as well if "
            "that move is expected -- that server would receive this login."
        )
    issuer = named[0]
    if urlsplit(issuer).scheme != "https" or not host_of(issuer):
        raise McpAuthError(
            "the resource named a non-https authorization server: "
            f"{printable(issuer, limit=200)}"
        )
    return issuer


def discover(client: Any, endpoint: str) -> AuthServer:
    """Find the authorization server for an endpoint, checking every hop.

    Args:
        client: An open ``httpx.Client``.
        endpoint: The MCP endpoint, already validated.

    Returns:
        The authorization server this client may use for ``endpoint``.

    Raises:
        McpAuthError: Discovery failed, or a hop named something this client
            must not follow.
    """
    split = urlsplit(endpoint)
    origin = f"{split.scheme}://{split.netloc}"
    # RFC 9728 puts the path after the well-known segment; servers vary on
    # whether they also answer the bare form, so try the specific one first.
    protected: dict[str, Any] | None = None
    for url in (
        f"{origin}/.well-known/oauth-protected-resource{split.path}",
        f"{origin}/.well-known/oauth-protected-resource",
    ):
        protected = _document(client, url)
        if protected is not None:
            break
    if protected is None:
        raise McpAuthError(
            f"{endpoint} does not advertise OAuth metadata; it may no longer "
            "be an OAuth-protected MCP endpoint. See MAINTENANCE.md."
        )

    # RFC 9728 section 3.3: the document must name the resource it was fetched
    # for. It also becomes the RFC 8707 resource indicator a token is scoped
    # to, which 0.4.1 took from the document whatever it said.
    named = protected.get("resource")
    if named != endpoint:
        raise McpAuthError(
            f"the OAuth metadata at {host_of(endpoint)} describes "
            f"{printable(str(named), limit=200)!r}, not {endpoint}"
        )

    # The resource document decides where this client will register and where
    # it will exchange a code for a token. PKCE and `state` protect the code in
    # flight; neither helps if the issuer itself is the attacker, so the hop is
    # checked rather than followed on trust.
    issuer = _issuer(protected.get("authorization_servers"), endpoint)

    metadata: dict[str, Any] | None = None
    for url in (
        f"{issuer}/.well-known/oauth-authorization-server",
        f"{issuer}/.well-known/openid-configuration",
    ):
        metadata = _document(client, url)
        if metadata is not None:
            break
    if metadata is None:
        raise McpAuthError(f"{issuer} published no authorization server metadata")

    # RFC 8414 section 3.3: the issuer in the metadata must be identical to the
    # one whose well-known path produced it. Without this, a document served at
    # one issuer can name another's endpoints and the mismatch goes unnoticed.
    published = str(metadata.get("issuer", "")).rstrip("/")
    if published != issuer:
        raise McpAuthError(
            f"authorization server metadata claims issuer "
            f"{printable(published, limit=200)!r}, fetched from {issuer!r}"
        )

    # And its endpoints must be its own. Measured on 0.4.1: a token endpoint
    # on another host, and an authorization endpoint over plain http, were
    # both followed.
    try:
        authorization = same_host_https(
            metadata.get("authorization_endpoint"),
            anchor=issuer,
            what="the authorization endpoint",
        )
        token = same_host_https(
            metadata.get("token_endpoint"), anchor=issuer, what="the token endpoint"
        )
        registration = metadata.get("registration_endpoint")
        if registration is not None:
            registration = same_host_https(
                registration, anchor=issuer, what="the registration endpoint"
            )
    except EndpointError as error:
        raise McpAuthError(printable(str(error), limit=300)) from error

    return AuthServer(
        resource=endpoint,
        issuer=issuer,
        authorization_endpoint=authorization,
        token_endpoint=token,
        registration_endpoint=registration,
        iss_in_callback=metadata.get("authorization_response_iss_parameter_supported")
        is True,
    )


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


def _keep(payload: dict[str, Any], *, lost: str) -> None:
    """Write the store, turning a failure into an explanation.

    Args:
        payload: The store to write.
        lost: What the failure costs, for the message.

    Raises:
        McpAuthError: The store could not be written.
    """
    try:
        write_store(payload)
    except OSError as error:
        raise McpAuthError(
            f"could not save the MCP token store ({error.strerror or error}); {lost}"
        ) from error


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


@contextmanager
def _store_lock() -> Iterator[None]:
    """Hold the token store across processes, for one refresh or one save.

    A refresh token that rotates can be spent once. Measured on 0.4.1: two
    runs refreshing at the same moment -- a scheduled sync and a manual one --
    both sent the same refresh token, and a server that detects reuse revokes
    the grant for that. An advisory ``flock`` on ``<store>.lock`` makes the
    second run wait and then find the first one's fresh token.

    Taking the lock is also the pre-flight for the one write that cannot fail
    safely: a store directory this run cannot write stops it here, before a
    refresh token is spent on tokens that could not be kept.

    Yields:
        Nothing; the caller refreshes or writes in between.

    Raises:
        McpAuthError: The lock could not be taken.
    """
    try:
        import fcntl
    except ImportError:  # pragma: no cover - no flock on Windows
        yield
        return
    store = paths.token_store_path()
    lock = store.with_name(f"{store.name}.lock")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        secure_mkdir(lock.parent)
        if not os.access(lock.parent, os.W_OK | os.X_OK):
            raise PermissionError(errno.EACCES, "not writable", str(lock.parent))
        fd = os.open(lock, flags, 0o600)
    except OSError as error:
        raise McpAuthError(
            f"cannot write the MCP token store in {lock.parent} "
            f"({error.strerror or error})"
        ) from error
    try:
        deadline = time.monotonic() + LOCK_TIMEOUT
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise McpAuthError(
                        "another wispr-export run has held the MCP token store "
                        f"for {LOCK_TIMEOUT:.0f} seconds; try again when it finishes"
                    ) from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


def _bound(store: dict[str, Any], endpoint: str) -> dict[str, Any]:
    """Return a store whose tokens may be sent to ``endpoint``, or refuse.

    Tokens are bound to the resource they were minted for and the issuer that
    minted them. The store used to record neither faithfully, and its access
    token went to whatever endpoint was configured. Measured on 0.4.1: with
    the endpoint overridden to another host, the stored token was handed out
    without a single check, to be sent there as a Bearer.

    Args:
        store: The token store.
        endpoint: The MCP endpoint this run will talk to.

    Returns:
        The store, with ``resource`` filled in for one written before 0.5.0.

    Raises:
        McpAuthError: The tokens were minted for another endpoint or issuer.
    """
    if not store.get("access_token") and not store.get("refresh_token"):
        return store
    issuer = str(store.get("issuer") or "").rstrip("/")
    resource = store.get("resource")
    if resource is None:
        # Written before tokens were bound. Adopted only where there is no
        # doubt whose they are: the shipped endpoint, and the shipped issuer.
        if endpoint == DEFAULT_MCP_ENDPOINT and issuer == DEFAULT_ISSUER:
            return {**store, "issuer": issuer, "resource": endpoint}
        raise McpAuthError(
            "the stored MCP authorization predates tokens being bound to an "
            f"endpoint and cannot be shown to belong to {endpoint}. Run "
            "`wispr-export login`."
        )
    if resource != endpoint:
        raise McpAuthError(
            "the stored MCP authorization is for "
            f"{printable(str(resource), limit=200)}, not {endpoint}. Run "
            "`wispr-export login` to authorize this one instead."
        )
    shipped = SHIPPED_ISSUERS.get(endpoint)
    if shipped is not None and issuer != shipped and not _override_allowed():
        raise McpAuthError(
            "the stored MCP authorization was issued by "
            f"{host_of(issuer) or 'no recorded issuer'}, not {host_of(shipped)}. "
            "Run `wispr-export login`."
        )
    return store


def _fresh(
    store: Mapping[str, Any], *, rejected: str | None = None
) -> McpCredential | None:
    """Return the stored access token if it can still be used.

    Args:
        store: A bound token store.
        rejected: A token the server has just refused, which is not fresh
            whatever its expiry says.

    Returns:
        The credential, or ``None`` when it needs refreshing. A token saved
        without a usable expiry needs refreshing: it used to be trusted
        forever.
    """
    token = store.get("access_token")
    expires_at = store.get("expires_at")
    if (
        isinstance(token, str)
        and token
        and token != rejected
        and isinstance(expires_at, int | float)
        and not isinstance(expires_at, bool)
        and math.isfinite(expires_at)
        and expires_at - EXPIRY_MARGIN > time.time()
    ):
        return McpCredential(
            token=token, origin="token store", expires_at=float(expires_at)
        )
    return None


def _expires_at(expires_in: object, now: float) -> float:
    """Turn a token response's ``expires_in`` into an absolute expiry.

    Args:
        expires_in: The value as the server sent it, if it did.
        now: The current Unix time.

    Returns:
        When the token expires. A missing, non-numeric, non-finite or
        non-positive lifetime is read as :data:`ASSUMED_LIFETIME`.
    """
    if isinstance(expires_in, bool) or not isinstance(expires_in, int | float | str):
        return now + ASSUMED_LIFETIME
    try:
        seconds = float(expires_in)
    except ValueError:
        return now + ASSUMED_LIFETIME
    if not math.isfinite(seconds) or seconds <= 0:
        return now + ASSUMED_LIFETIME
    return now + seconds


def _save_tokens(
    store: Mapping[str, Any],
    tokens: Mapping[str, Any],
    *,
    grant: Literal["authorization_code", "refresh_token"],
) -> dict[str, Any]:
    """Merge a token response into the store and persist it.

    Args:
        store: The binding the tokens belong to, and anything kept with it.
        tokens: A token endpoint response.
        grant: Which grant produced it. A fresh login replaces the previous
            login's refresh token even when its answer carries none -- keeping
            it, as 0.4.1 did, left a credential from an earlier grant, maybe
            another account's, to be used later. A refresh answered without a
            new refresh token keeps the one it used, which is still valid.

    Returns:
        The saved store.

    Raises:
        McpAuthError: The response held no access token, or could not be saved.
    """
    access = tokens.get("access_token")
    if not isinstance(access, str) or not access:
        step = "token exchange" if grant == "authorization_code" else "refresh"
        raise McpAuthError(f"the {step} returned no access token")
    saved = dict(store)
    saved["access_token"] = access
    rotated = tokens.get("refresh_token")
    if isinstance(rotated, str) and rotated:
        saved["refresh_token"] = rotated
    elif grant == "authorization_code":
        saved.pop("refresh_token", None)
    saved["expires_at"] = _expires_at(tokens.get("expires_in"), time.time())
    _keep(
        saved,
        lost="the new refresh token is lost with it. Run `wispr-export login` again.",
    )
    return saved


# --- registration and the authorization code grant ------------------------


def register_client(client: Any, server: AuthServer) -> dict[str, Any]:
    """Register this tool as a public OAuth client.

    Dynamic registration means there is no client secret to embed in a
    source-available tool, and no shared identity between installations.

    Args:
        client: An open ``httpx.Client``.
        server: The authorization server from :func:`discover`.

    Returns:
        The registration response, including ``client_id``.

    Raises:
        McpAuthError: The server refused to register the client.
    """
    if not server.registration_endpoint:
        raise McpAuthError(
            "the authorization server does not support dynamic client "
            "registration, so this tool has no client id to use"
        )
    answer = _exchange(
        client,
        "POST",
        server.registration_endpoint,
        what="client registration",
        body={
            "client_name": "wispr-flow-exporter",
            "redirect_uris": [_redirect_uri(port) for port in CALLBACK_PORTS],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            # Public client: no secret to embed in a source-available tool.
            # Omit this and the server issues one, which would be a credential
            # this tool has no safe place to keep.
            "token_endpoint_auth_method": "none",
            "scope": SCOPES,
        },
    )
    if answer.status not in (200, 201):
        # Carry the server's own complaint. A bare status here cost real time
        # once: a 422 said nothing, and the actual reason was that the device
        # grant is not registrable.
        raise McpAuthError(
            f"client registration returned HTTP {answer.status}: {answer.excerpt}"
        )
    payload = answer.payload
    if not isinstance(payload, dict) or not isinstance(payload.get("client_id"), str):
        raise McpAuthError("client registration returned no client id")
    return payload


def _redirect_uri(port: int) -> str:
    """Build the loopback redirect for one port.

    Args:
        port: The port the listener is bound to.

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


def _with_query(url: str, params: Mapping[str, str]) -> str:
    """Add query parameters to a URL that may already have some.

    Args:
        url: The URL.
        params: What to add.

    Returns:
        The URL with ``params`` after any query it already carried; a bare
        ``?`` join would have produced a second one.
    """
    split = urlsplit(url)
    query = [*parse_qsl(split.query, keep_blank_values=True), *params.items()]
    return urlunsplit(split._replace(query=urlencode(query)))


class _CallbackServer(HTTPServer):
    """The loopback listener for one login, and what it has heard.

    Attributes:
        state: This login's ``state``, as bytes, so any request's value can be
            compared in constant time -- a non-ASCII one used to raise.
        issuer: The authorization server, for RFC 9207's ``iss``.
        iss_required: Whether the server promised to send ``iss``.
        connection_timeout: Seconds one connection may take to send a request.
        outcome: ``("code", code)`` or ``("error", reason)`` once this login's
            redirect arrives; ``None`` until then.
        ignored: Requests turned away, for the timeout message.
    """

    def __init__(
        self,
        port: int,
        state: str,
        *,
        issuer: str,
        iss_required: bool,
        connection_timeout: float,
    ) -> None:
        """Bind the listener.

        Args:
            port: The port to bind on ``127.0.0.1``.
            state: This login's ``state``.
            issuer: The authorization server.
            iss_required: Whether ``iss`` must be present.
            connection_timeout: Seconds one connection may stay silent.
        """
        # 127.0.0.1, not 0.0.0.0: nothing off this machine can reach the
        # listener even for the minutes it exists.
        super().__init__(("127.0.0.1", port), _Callback)
        self.state = state.encode("utf-8")
        self.issuer = issuer
        self.iss_required = iss_required
        self.connection_timeout = connection_timeout
        self.outcome: tuple[Literal["code", "error"], str] | None = None
        self.ignored = 0


class _Callback(BaseHTTPRequestHandler):
    """Answers the listener's requests; only this login's redirect counts."""

    server: _CallbackServer

    def setup(self) -> None:
        """Bound how long one connection may take to say anything."""
        super().setup()
        # A timed-out read ends this connection, not the login: the handler
        # drops it and the listener goes back to waiting.
        self.connection.settimeout(self.server.connection_timeout)

    def do_GET(self) -> None:
        """Take this login's redirect, and turn anything else away."""
        split = urlsplit(self.path)
        query = parse_qs(split.query)
        state = query.get("state", [""])[0].encode("utf-8")
        # Path and state first, before anything else the request says is
        # believed. The state is 32 random bytes, so a request without it is
        # not this login's redirect however it is phrased -- including an
        # `error` that used to end the login from anywhere on the machine.
        if split.path != "/callback" or not secrets.compare_digest(
            state, self.server.state
        ):
            self.server.ignored += 1
            self._page(404, "This is not the page wispr-flow-exporter is waiting for.")
            return
        self.server.outcome = self._outcome(query)
        if self.server.outcome[0] == "code":
            self._page(
                200,
                "wispr-flow-exporter is authorized. You can close this tab and "
                "return to the terminal.",
            )
        else:
            self._page(
                400, "Authorization did not complete. The terminal says why."
            )

    def _outcome(
        self, query: Mapping[str, list[str]]
    ) -> tuple[Literal["code", "error"], str]:
        """Decide what this login's redirect says.

        Args:
            query: The redirect's parsed query.

        Returns:
            The code, or the reason there is none.
        """
        iss = query.get("iss", [""])[0]
        # RFC 9207: an authorization server that sends its identity on the
        # redirect lets a client refuse a response some other server produced.
        if iss and iss.rstrip("/") != self.server.issuer:
            return "error", "the authorization response came from another issuer"
        if not iss and self.server.iss_required:
            return "error", "the authorization response did not name its issuer"
        error = query.get("error", [""])[0]
        if error:
            description = query.get("error_description", [""])[0]
            detail = f"{error}: {description}" if description else error
            return "error", f"authorization failed: {printable(detail, limit=200)}"
        code = query.get("code", [""])[0]
        if not code:
            return "error", "the authorization response carried no code"
        return "code", code

    def _page(self, status: int, message: str) -> None:
        """Answer the browser, saying what happened and caching nothing.

        Args:
            status: The HTTP status.
            message: One sentence, fixed text.
        """
        body = (
            "<!doctype html><html><body style='font-family:sans-serif'>"
            f"<p>{message}</p></body></html>"
        ).encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        """Stay quiet; the CLI owns this terminal."""


def _bind_listener(
    state: str,
    *,
    issuer: str,
    iss_required: bool = False,
    connection_timeout: float = CONNECTION_TIMEOUT,
) -> _CallbackServer:
    """Bind the first registered loopback port that is free.

    Args:
        state: This login's ``state``.
        issuer: The authorization server.
        iss_required: Whether the redirect must carry ``iss``.
        connection_timeout: Seconds one connection may stay silent.

    Returns:
        The bound listener, which the caller closes.

    Raises:
        McpAuthError: Every registered port is in use.
    """
    for port in CALLBACK_PORTS:
        try:
            return _CallbackServer(
                port,
                state,
                issuer=issuer,
                iss_required=iss_required,
                connection_timeout=connection_timeout,
            )
        except OSError:
            continue
    raise McpAuthError(
        "every callback port is in use: " + ", ".join(str(p) for p in CALLBACK_PORTS)
    )


def _await_code(server: _CallbackServer, timeout: float) -> str:
    """Serve requests until this login's redirect arrives or time runs out.

    Args:
        server: The bound listener. Closed on return.
        timeout: Seconds to wait in all.

    Returns:
        The authorization code.

    Raises:
        McpAuthError: The wait timed out, or the redirect carried an error
            instead of a code.
    """
    deadline = time.monotonic() + timeout
    try:
        while server.outcome is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            server.timeout = remaining
            server.handle_request()
    finally:
        server.server_close()
    if server.outcome is None:
        turned = (
            f"; {server.ignored} unrelated request(s) were turned away"
            if server.ignored
            else ""
        )
        raise McpAuthError(f"timed out waiting for the browser to come back{turned}")
    kind, value = server.outcome
    if kind == "error":
        raise McpAuthError(value)
    return value


def authorize(
    client: Any,
    server: AuthServer,
    client_id: str,
    *,
    announce: Callable[[str], object] = print,
    opener: Callable[[str], object] | None = webbrowser.open,
) -> dict[str, Any]:
    """Run the authorization code flow with PKCE and return the tokens.

    Args:
        client: An open ``httpx.Client``.
        server: The authorization server.
        client_id: This tool's registered client id.
        announce: Where to print the URL.
        opener: Opens the URL in a browser; ``None`` to only print it.

    Returns:
        The token response.

    Raises:
        McpAuthError: Any step failed.
    """
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(32)
    # Bound before the browser is sent anywhere. A browser already signed in
    # can come straight back, and the listener used to bind only afterwards.
    listener = _bind_listener(
        state, issuer=server.issuer, iss_required=server.iss_in_callback
    )
    try:
        redirect_uri = _redirect_uri(listener.server_port)
        url = _with_query(
            server.authorization_endpoint,
            {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "scope": SCOPES,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                # RFC 8707. MCP requires the resource indicator so the issued
                # token is scoped to this server and cannot be replayed
                # against another.
                "resource": server.resource,
            },
        )
        announce(f"  Open: {url}")
        announce("  Waiting for authorization...")
        if opener is not None:
            try:
                opener(url)
            except Exception:  # pragma: no cover - platform dependent
                pass
        code = _await_code(listener, LOGIN_TIMEOUT)
    finally:
        listener.server_close()

    answer = _exchange(
        client,
        "POST",
        server.token_endpoint,
        what="the token exchange",
        form={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": verifier,
            "resource": server.resource,
        },
    )
    if answer.status != 200:
        raise McpAuthError(
            f"the token exchange returned HTTP {answer.status}: {answer.excerpt}"
        )
    if not isinstance(answer.payload, dict):
        raise McpAuthError("the token exchange did not return a JSON object")
    return answer.payload


def refresh(
    client: Any, server: AuthServer, client_id: str, refresh_token: str
) -> dict[str, Any]:
    """Exchange a refresh token for a fresh access token.

    Safe in a way ``cloud_auth`` deliberately is not: this refresh token is one
    this tool minted for itself against a different issuer, so rotating it
    cannot disturb the desktop app's session.

    Args:
        client: An open ``httpx.Client``.
        server: The authorization server.
        client_id: This tool's registered client id.
        refresh_token: The stored refresh token.

    Returns:
        The token response.

    Raises:
        McpAuthError: The refresh was refused or did not complete.
    """
    answer = _exchange(
        client,
        "POST",
        server.token_endpoint,
        what="the token refresh",
        form={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "resource": server.resource,
        },
    )
    if answer.status in (400, 401):
        raise McpAuthError(
            "the stored authorization is no longer valid; run "
            "`wispr-export login` again"
        )
    if answer.status != 200:
        raise McpAuthError(
            f"the token refresh returned HTTP {answer.status}: {answer.excerpt}"
        )
    if not isinstance(answer.payload, dict):
        raise McpAuthError("the token refresh did not return a JSON object")
    return answer.payload


def login(
    client: Any,
    endpoint: str,
    *,
    announce: Callable[[str], object] = print,
    opener: Callable[[str], object] | None = webbrowser.open,
) -> McpCredential:
    """Run the authorization code grant end to end and save the result.

    One login is kept at a time: authorizing another endpoint replaces the
    stored one.

    Args:
        client: An open ``httpx.Client``.
        endpoint: The MCP endpoint, already validated.
        announce: Where to print the authorization URL.
        opener: Opens the URL in a browser; ``None`` to only print it.

    Returns:
        The minted credential.

    Raises:
        McpAuthError: Any step failed.
    """
    server = discover(client, endpoint)
    binding = {"issuer": server.issuer, "resource": endpoint}
    # Taken before anyone is sent to a browser, so a store this run cannot
    # write stops the login before a sign-in is wasted on it.
    with _store_lock():
        stored = read_store()
        client_id = stored.get("client_id")
        if str(stored.get("issuer") or "").rstrip("/") != server.issuer or not (
            isinstance(client_id, str) and client_id
        ):
            # A client id belongs to the issuer that registered it; presenting
            # it to another would at best fail, so each issuer gets its own.
            client_id = register_client(client, server)["client_id"]
            _keep(
                {"client_id": client_id, **binding},
                lost="the next login registers another client.",
            )
    tokens = authorize(client, server, client_id, announce=announce, opener=opener)
    # The lock covers the save only -- never the wait for the browser.
    with _store_lock():
        saved = _save_tokens(
            {"client_id": client_id, **binding}, tokens, grant="authorization_code"
        )
    return McpCredential(
        token=saved["access_token"], origin="token store", expires_at=saved["expires_at"]
    )


def _refreshed(client: Any, endpoint: str, *, rejected: str | None) -> McpCredential:
    """Refresh the stored token, once, under the lock.

    Args:
        client: An open ``httpx.Client``.
        endpoint: The MCP endpoint, already validated.
        rejected: An access token the server refused, if that is why.

    Returns:
        A fresh credential -- another run's, when it refreshed first.

    Raises:
        McpAuthError: There is nothing to refresh with, the refresh was
            refused, or the endpoint now names another issuer.
    """
    with _store_lock():
        # Read again under the lock. Another run may have refreshed while
        # this one waited, and spending its refresh token a second time would
        # be a reuse the server is entitled to punish.
        store = _bound(read_store(), endpoint)
        current = _fresh(store, rejected=rejected)
        if current is not None:
            return current
        refresh_token = store.get("refresh_token")
        client_id = store.get("client_id")
        if not isinstance(refresh_token, str) or not isinstance(client_id, str):
            raise McpAuthError(
                "no MCP authorization stored. Run `wispr-export login` first."
            )
        server = discover(client, endpoint)
        issuer = str(store.get("issuer") or "").rstrip("/")
        if server.issuer != issuer:
            # The refresh token was minted by the stored issuer and goes to no
            # one else, whatever the resource now says.
            raise McpAuthError(
                f"{endpoint} now names {host_of(server.issuer)} as its authorization "
                f"server, not {host_of(issuer) or 'the one on record'}, which issued "
                "the stored authorization. Run `wispr-export login` again."
            )
        saved = _save_tokens(
            store,
            refresh(client, server, client_id, refresh_token),
            grant="refresh_token",
        )
    return McpCredential(
        token=saved["access_token"], origin="token store", expires_at=saved["expires_at"]
    )


def resolve_credential(client: Any, endpoint: str) -> McpCredential:
    """Find a usable MCP token for an endpoint, refreshing if needed.

    Never prompts.

    Args:
        client: An open ``httpx.Client``.
        endpoint: The MCP endpoint, already validated.

    Returns:
        The credential.

    Raises:
        McpAuthError: There is no stored authorization for this endpoint, or
            it can no longer be refreshed. The remedy is ``wispr-export
            login``, and saying so is better than opening a browser in the
            middle of a batch run.
    """
    override = os.environ.get("WISPR_MCP_TOKEN", "").strip()
    if override:
        return McpCredential(token=override, origin="environment")

    store = _bound(read_store(), endpoint)
    current = _fresh(store)
    if current is not None:
        return current
    if not store.get("refresh_token") or not store.get("client_id"):
        raise McpAuthError(
            "no MCP authorization stored. Run `wispr-export login` first."
        )
    return _refreshed(client, endpoint, rejected=None)


def renew_credential(
    client: Any, endpoint: str, rejected: McpCredential
) -> McpCredential:
    """Replace an access token the server refused before it was due to expire.

    Args:
        client: An open ``httpx.Client``.
        endpoint: The MCP endpoint, already validated.
        rejected: The credential the server answered 401 to.

    Returns:
        A fresh credential.

    Raises:
        McpAuthError: The credential came from the environment, which is
            never refreshed, or the refresh failed.
    """
    if rejected.origin != "token store":
        raise McpAuthError(
            "the MCP server rejected WISPR_MCP_TOKEN, and a token from the "
            "environment is never refreshed"
        )
    return _refreshed(client, endpoint, rejected=rejected.token)


def login_state(endpoint: str) -> str:
    """Describe the stored authorization for an endpoint, contacting no one.

    For ``doctor``, which makes no network request: whether this backend can
    run, and if not, what to do about it.

    Args:
        endpoint: The MCP endpoint, already validated.

    Returns:
        One line, with no token in it.
    """
    if os.environ.get("WISPR_MCP_TOKEN", "").strip():
        return "using WISPR_MCP_TOKEN from the environment; it is never refreshed"
    store = read_store()
    if not store.get("access_token") and not store.get("refresh_token"):
        return "not logged in; `wispr-export login` enables this backend"
    try:
        bound = _bound(store, endpoint)
    except McpAuthError as error:
        return str(error)
    current = _fresh(bound)
    if current is not None and current.expires_at is not None:
        until = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(current.expires_at))
        return f"logged in; access token valid until {until}"
    if bound.get("refresh_token"):
        return "logged in; the access token will be refreshed on the next run"
    return "the stored authorization has expired; run `wispr-export login` again"
