"""Wispr Flow's own JSON state: preferences, feature flags and the session.

This is the only module that reads ``session.json``, and the only one that can
return a bearer token. Everything else takes a :class:`SessionInfo`, which
carries the account identity and the expiry but no credential, so a new code
path physically cannot leak one by accident.

That matters more here than it usually would. ``session.json`` is a bare,
unencrypted Supabase GoTrue session with no keychain entry guarding it -- mode
0666 on the machine this was developed against -- and its outer object holds a
single key whose *value is itself a JSON string*. The credential is one parse
deeper than it looks, so a redactor keyed on the top-level shape would find
nothing and report success.

The other reason this module is small and separate: ``config.json`` is where
the ``localDataPolicy`` preference lives, and that preference decides whether
dictation history exists on disk at all. An archive that is empty because of a
setting must be able to prove it, so the policy is read here and recorded in
the archive rather than inferred from a zero row count.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Dictation is not written to disk at all under this policy. It mirrors a
# server-side preference and may be enforced by an organization.
NEVER_STORE = "never_store"

_SESSION_KEY_RE = re.compile(r"^sb-([a-z0-9]+)-auth-token$")

# Applied to every diagnostic stream. Redacting at the sink rather than at each
# call site is deliberate: a new code path cannot forget to call it.
_REDACTIONS = (
    re.compile(r"eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]+"),
    re.compile(r"sb-[a-z0-9]{16,}-auth-token"),
    re.compile(r"X-Amz-(?:Signature|Credential|Security-Token)=[^&\s]+"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{16,}=*"),
)


def redact(text: str) -> str:
    """Remove anything credential-shaped from a string bound for output.

    Args:
        text: Text about to be logged, printed or raised.

    Returns:
        The text with credentials replaced by ``[redacted]``.
    """
    for pattern in _REDACTIONS:
        text = pattern.sub("[redacted]", text)
    return text


@dataclass(frozen=True, slots=True)
class Policy:
    """The Wispr Flow preferences that decide what exists on disk.

    Attributes:
        local_data_policy: ``"never_store"``, ``"no_audio"`` or a storing
            value. Read verbatim so an unrecognized future value is recorded
            rather than guessed at.
        transcript_retention: Meeting transcript retention preference.
        observed_at: When this was read, so the archive can say *when* the
            policy was in force rather than only that it is now.
    """

    local_data_policy: str | None
    transcript_retention: str | None
    observed_at: datetime

    @property
    def records_dictation(self) -> bool:
        """Report whether dictation history is written to disk at all.

        Returns:
            ``False`` when the policy is ``never_store``, in which case an
            empty ``History`` table is expected rather than a failure.
        """
        return self.local_data_policy != NEVER_STORE

    def as_dict(self, previous: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Serialize for ``.sync-state.json``.

        ``observed_at`` means "in force since", not "last checked". Carrying
        the earlier timestamp forward while the values are unchanged is both
        more useful -- it dates the policy rather than the run -- and keeps the
        state file byte-identical across a sync that changed nothing.

        Args:
            previous: The policy block from an earlier run, if any.

        Returns:
            A JSON-serializable mapping.
        """
        current = {
            "local_data_policy": self.local_data_policy,
            "notetaker_transcript_retention": self.transcript_retention,
            "records_dictation": self.records_dictation,
            "observed_at": self.observed_at.isoformat(),
        }
        if previous and all(
            previous.get(key) == current[key]
            for key in ("local_data_policy", "notetaker_transcript_retention")
        ):
            carried = previous.get("observed_at")
            if isinstance(carried, str) and carried:
                current["observed_at"] = carried
        return current


@dataclass(frozen=True, slots=True)
class LocalConfig:
    """Everything read from ``config.json``.

    Attributes:
        policy: The storage preferences.
        preferences: ``prefs.user``, archived under ``account/``.
        sync_coordinator: Wispr Flow's own per-entity watermark map. Archived
            as provenance evidence, and a ready-made taxonomy of the entities
            the cloud backend syncs.
        voice_profile: The account's voice profile, which lives only here.
        writing_samples: Saved writing samples, which live only here.
        polish_prompts: Saved rewrite prompts, which live only here.
        present: Whether the file existed at all.
    """

    policy: Policy
    preferences: dict[str, Any] = field(default_factory=dict)
    sync_coordinator: dict[str, Any] = field(default_factory=dict)
    voice_profile: Any = None
    writing_samples: Any = None
    polish_prompts: Any = None
    present: bool = True


def read_config(path: Path) -> LocalConfig:
    """Read ``config.json``, tolerating absence and corruption.

    Args:
        path: Path to ``config.json``.

    Returns:
        The parsed configuration. A missing or unreadable file yields a
        configuration whose policy is unknown, which callers report rather
        than treating as permission to assume the default.
    """
    observed_at = datetime.now(tz=UTC)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return LocalConfig(
            policy=Policy(None, None, observed_at),
            present=False,
        )
    if not isinstance(payload, dict):
        return LocalConfig(policy=Policy(None, None, observed_at), present=False)

    prefs = payload.get("prefs") if isinstance(payload.get("prefs"), dict) else {}
    user = prefs.get("user") if isinstance(prefs.get("user"), dict) else {}
    context = prefs.get("context") if isinstance(prefs.get("context"), dict) else {}
    coordinator = (
        payload.get("syncCoordinator")
        if isinstance(payload.get("syncCoordinator"), dict)
        else {}
    )

    return LocalConfig(
        policy=Policy(
            local_data_policy=user.get("localDataPolicy"),
            transcript_retention=user.get("notetakerTranscriptRetention"),
            observed_at=observed_at,
        ),
        preferences=dict(user),
        sync_coordinator=dict(coordinator),
        voice_profile=payload.get("voiceProfile"),
        writing_samples=context.get("writingSamples"),
        polish_prompts=context.get("polishPrompts"),
    )


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """What the rest of the package may know about the stored session.

    Carries identity and expiry, and deliberately no credential. Only
    :func:`read_access_token` returns the token itself.

    Attributes:
        present: Whether ``session.json`` existed and parsed.
        project_ref: The Supabase project reference from the storage key.
        user_id: The account id.
        email: The account email.
        expires_at: When the access token expires.
    """

    present: bool
    project_ref: str | None = None
    user_id: str | None = None
    email: str | None = None
    expires_at: datetime | None = None

    @property
    def is_expired(self) -> bool:
        """Report whether the access token has expired.

        An expired token is not refreshed by this tool. Supabase GoTrue
        rotates refresh tokens and detects reuse, so refreshing would
        invalidate the desktop app's own session and sign the user out of the
        app they are trying to back up. The remedy is to open Wispr Flow,
        which refreshes it itself, and re-run.

        Returns:
            ``True`` when there is no usable token.
        """
        if self.expires_at is None:
            return True
        return self.expires_at <= datetime.now(tz=UTC)


def _parse_session(path: Path) -> tuple[str | None, dict[str, Any]]:
    """Open ``session.json`` and unwrap its doubly-encoded payload.

    Args:
        path: Path to ``session.json``.

    Returns:
        ``(project_ref, session)``. The session is empty when the file is
        missing, unparseable, or not in the expected shape.
    """
    try:
        outer = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, {}
    if not isinstance(outer, dict):
        return None, {}

    for key, value in outer.items():
        match = _SESSION_KEY_RE.match(key)
        if match is None:
            continue
        # The value is a JSON *string*, not an object. This second parse is
        # the step a naive reader skips, and the reason a redactor keyed on
        # the outer shape would find nothing to redact.
        inner = json.loads(value) if isinstance(value, str) else value
        if isinstance(inner, dict):
            return match.group(1), inner
        return match.group(1), {}
    return None, {}


def read_session(path: Path) -> SessionInfo:
    """Read the stored session's identity and expiry, never its credentials.

    Args:
        path: Path to ``session.json``.

    Returns:
        What may safely circulate in the rest of the package.
    """
    try:
        project_ref, session = _parse_session(path)
    except ValueError:
        return SessionInfo(present=False)
    if not session:
        return SessionInfo(present=False, project_ref=project_ref)

    expires_at: datetime | None = None
    raw_expiry = session.get("expires_at")
    if isinstance(raw_expiry, (int, float)) and not isinstance(raw_expiry, bool):
        expires_at = datetime.fromtimestamp(raw_expiry, tz=UTC)

    user = session.get("user") if isinstance(session.get("user"), dict) else {}
    return SessionInfo(
        present=True,
        project_ref=project_ref,
        user_id=user.get("id"),
        email=user.get("email"),
        expires_at=expires_at,
    )


def read_access_token(path: Path) -> str | None:
    """Return the bearer token, for the cloud backend and nothing else.

    Deliberately the only function in the package that can produce a
    credential, and deliberately not cached: the caller holds it in a local
    for the duration of one request. Nothing here writes to ``session.json``,
    and no refresh endpoint is ever contacted.

    Args:
        path: Path to ``session.json``.

    Returns:
        The access token, or ``None`` when there is no usable session.
    """
    try:
        _, session = _parse_session(path)
    except ValueError:
        return None
    token = session.get("access_token")
    return token if isinstance(token, str) and token else None


def account_profile(info: SessionInfo) -> dict[str, Any]:
    """Build the archivable account record from a session.

    Args:
        info: The session summary.

    Returns:
        Identity and expiry only. No token, and no refresh token, ever reaches
        a file this tool writes.
    """
    return {
        "user_id": info.user_id,
        "email": info.email,
        "supabase_project_ref": info.project_ref,
        "access_token_expires_at": (
            info.expires_at.isoformat() if info.expires_at else None
        ),
    }
