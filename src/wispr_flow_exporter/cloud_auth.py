"""Getting a bearer token for the cloud backend, and refusing to mint one.

There is exactly one rule here and everything else follows from it: **this tool
reads the access token Wispr Flow already has, and never calls the refresh
endpoint.**

Supabase GoTrue rotates refresh tokens and detects reuse. A second client that
refreshes either revokes the desktop app's session or races it, so a tool whose
whole purpose is backing up Wispr Flow would sign the user out of Wispr Flow.
The sibling project documents the same failure for Granola's internal API,
where refreshing a single-use token logs you out of the desktop app.

The cost is real and is not hidden: the cloud backend only works when the app
has refreshed recently, and stops with "open Wispr Flow and re-run" when it has
not. That is the correct trade, and there is no refresh code path here to be
reached by accident.

Because nothing is ever minted, there is also nothing to cache and no session
of our own to manage -- which is why this tool has no ``login`` or ``logout``.
The credential belongs to the app; this module borrows it for the duration of
one request.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .local_config import SessionInfo, read_access_token, read_session

# Set to use a token directly, for CI and containers where Wispr Flow is not
# installed. Read at the call site and never written anywhere.
TOKEN_ENV = "WISPR_ACCESS_TOKEN"


class CloudAuthError(Exception):
    """No usable credential is available for the cloud backend."""


@dataclass(frozen=True, slots=True)
class Credential:
    """A bearer token and where it came from.

    The token is deliberately not stored anywhere else, not logged, and not
    written into the archive. It exists for the lifetime of one client.

    Attributes:
        token: The bearer token.
        origin: ``"environment"`` or ``"session.json"``, for diagnostics.
    """

    token: str
    origin: str

    def header(self) -> dict[str, str]:
        """Build the Authorization header for one request.

        The token is sent bare, with no ``Bearer`` scheme. That is not a
        stylistic choice: the desktop app sets ``Authorization`` to the raw
        access token, and the server rejects the RFC 6750 form outright --
        measured against the live service, the same token returns 200 bare and
        401 with the prefix. Sending the correct-looking header would make the
        whole backend silently unusable, which is exactly what it did.

        Returns:
            A single-entry mapping.
        """
        return {"Authorization": self.token}


def resolve_credential(session_path: Path) -> Credential:
    """Find a usable bearer token, or explain why there is not one.

    Args:
        session_path: Path to Wispr Flow's ``session.json``.

    Returns:
        The credential.

    Raises:
        CloudAuthError: No token is available, or the stored one has expired.
            Expiry is deliberately fatal rather than a prompt to refresh.
    """
    override = os.environ.get(TOKEN_ENV, "").strip()
    if override:
        return Credential(token=override, origin="environment")

    info: SessionInfo = read_session(session_path)
    if not info.present:
        raise CloudAuthError(
            "no Wispr Flow session found; sign in to Wispr Flow, or set "
            f"{TOKEN_ENV}"
        )
    if info.is_expired:
        raise CloudAuthError(
            "the stored Wispr Flow access token has expired. Open Wispr Flow "
            "so it refreshes its own session, then re-run. This tool will not "
            "refresh it: Supabase rotates refresh tokens and detects reuse, so "
            "doing so would sign you out of the app being backed up."
        )

    token = read_access_token(session_path)
    if not token:
        raise CloudAuthError("the stored Wispr Flow session carries no access token")
    return Credential(token=token, origin="session.json")
