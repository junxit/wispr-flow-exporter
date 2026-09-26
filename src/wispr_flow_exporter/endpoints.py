"""Where this tool is willing to send a credential.

Both remote backends read their base URL from the environment, and both attach
a bearer token to every request made against it. That combination is the one
place where a configuration value decides who receives a secret, so it is
checked here rather than at either call site.

The check exists because the environment is wider than it looks. ``cli`` loads
a ``.env`` before reading these variables, and python-dotenv's default search
used to walk *up* from the working directory -- so a file placed in any
ancestor of wherever the operator happened to run could set
``WISPR_API_BASE``. Measured, not assumed: a ``.env`` two directories up
resolved the API base to ``http://evil.example`` and nothing objected. The
search now stops at the working directory, and a ``.env`` may no longer supply
:data:`OVERRIDE_ENV` at all, so the redirect and the consent to it cannot both
arrive in one planted file.

This module is deliberately free of both backends' vocabulary so either may
import it. The MCP modules may not reference the borrowed credential's path at
all -- an invariant asserted against the source in ``tests/test_mcp.py`` -- and
a shared helper that named it would break that separation.
"""

from __future__ import annotations

from urllib.parse import urlsplit

#: Set to opt into a host this tool does not ship as a default. Requiring a
#: second variable is the point: one stray value can no longer redirect a
#: credential, because the redirect and the consent cannot both be accidents.
OVERRIDE_ENV = "WISPR_ALLOW_ENDPOINT_OVERRIDE"


class EndpointError(Exception):
    """A configured endpoint is not one a credential may be sent to."""


def host_of(url: str) -> str:
    """Return the lowercase host of a URL, or the empty string.

    Args:
        url: Any URL.

    Returns:
        The hostname, lowercased, or ``""`` when there is none.
    """
    return (urlsplit(url).hostname or "").lower()


def same_host_https(url: object, *, anchor: str, what: str) -> str:
    """Require a discovered URL to be https on the same host as another.

    For endpoints a server advertises about itself. An authorization server's
    metadata decides where this client registers, where the operator's browser
    is sent and where codes and refresh tokens go; a document that could name
    any host for those would make the issuer check above it decorative.

    Args:
        url: The advertised value, not yet known to be a string.
        anchor: The URL whose host it must share.
        what: What the URL is, for the error message.

    Returns:
        The URL, unchanged.

    Raises:
        EndpointError: The value is missing, is not https, carries credentials,
            or names a different host.
    """
    if not isinstance(url, str) or not url:
        raise EndpointError(f"{what} is missing")
    try:
        split = urlsplit(url)
        hostname = (split.hostname or "").lower()
        userinfo = split.username is not None or split.password is not None
    except ValueError as error:
        raise EndpointError(f"{what} is not a URL: {url!r}") from error
    if split.scheme != "https":
        raise EndpointError(f"{what} must use https: {url!r}")
    if userinfo or hostname != host_of(anchor):
        raise EndpointError(
            f"{what} is on {hostname or 'no host'!r}, not {host_of(anchor)!r}: {url!r}"
        )
    return url


def validated_endpoint(
    raw: str, *, default: str, variable: str, allow_override: bool = False
) -> str:
    """Check a configured endpoint before a credential is attached to it.

    Two rules, in order. The transport must be ``https``, with no exception --
    a bearer token does not travel in cleartext even to the right host. And the
    host must be the one this tool ships, unless the operator has separately set
    :data:`OVERRIDE_ENV`, which keeps a staging host reachable while making it a
    decision rather than a side effect.

    The default is returned unexamined when nothing overrode it, so the common
    path cannot be broken by a parsing disagreement.

    Args:
        raw: The configured value, already stripped. Empty means unset.
        default: The value this tool ships.
        variable: Environment variable name, for the error message.
        allow_override: Whether :data:`OVERRIDE_ENV` is set for this run.

    Returns:
        The endpoint to use.

    Raises:
        EndpointError: The value is not https, has no host, or names a
            different host without the override.
    """
    if not raw or raw == default:
        return default

    split = urlsplit(raw)
    if split.scheme != "https":
        raise EndpointError(
            f"{variable} must use https, got {split.scheme or 'no scheme'!r}. "
            "A bearer token is attached to every request made against it."
        )
    if not split.hostname:
        raise EndpointError(f"{variable} has no host: {raw!r}")

    expected = host_of(default)
    if split.hostname.lower() != expected and not allow_override:
        raise EndpointError(
            f"{variable} points at {split.hostname.lower()!r}, not {expected!r}. "
            f"Set {OVERRIDE_ENV}=1 as well if that is deliberate -- this tool "
            "sends the account's bearer token to whatever host it is given."
        )
    return raw
