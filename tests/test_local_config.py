"""Preferences, the storage policy, and the credential boundary."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from wispr_flow_exporter.local_config import (
    account_profile,
    read_access_token,
    read_config,
    read_session,
    redact,
)

from conftest import FAKE_JWT, FAKE_SESSION_KEY, OWNER, OWNER_EMAIL

ACCOUNT_ID = "user-0001"


def _write_session(
    directory: Path,
    *,
    expires_in: int = 3600,
    key: str = FAKE_SESSION_KEY,
) -> Path:
    """Write a session file in Wispr Flow's doubly-encoded shape.

    Args:
        directory: Where to write it.
        expires_in: Seconds until the access token expires; may be negative.
        key: The storage key to use.

    Returns:
        The path written.
    """
    inner = {
        "access_token": FAKE_JWT,
        "refresh_token": "refresh-token",
        "token_type": "bearer",
        "expires_at": int(
            (datetime.now(tz=UTC) + timedelta(seconds=expires_in)).timestamp()
        ),
        "user": {"id": ACCOUNT_ID, "email": OWNER_EMAIL},
    }
    path = directory / "session.json"
    # The value is a JSON *string*, not an object. This is the shape the app
    # writes, and the reason a single json.loads finds no credential.
    path.write_text(json.dumps({key: json.dumps(inner)}), encoding="utf-8")
    return path


# --- redaction ------------------------------------------------------------


def test_redact_removes_a_jwt() -> None:
    """A bearer token must never survive into a diagnostic line."""
    assert FAKE_JWT not in redact(f"Authorization: Bearer {FAKE_JWT}")
    assert "[redacted]" in redact(FAKE_JWT)


def test_redact_removes_a_session_storage_key() -> None:
    """The storage key identifies the Supabase project and is not ours to print."""
    assert "[redacted]" in redact(f"key {FAKE_SESSION_KEY} missing")


def test_redact_removes_a_presigned_signature() -> None:
    """A signed URL grants object access to whoever reads the log line."""
    redacted = redact("https://example.invalid/o?X-Amz-Signature=deadbeefcafe&x=1")
    assert "deadbeefcafe" not in redacted
    assert "x=1" in redacted


def test_redact_leaves_ordinary_text_alone() -> None:
    """Redaction must not mangle the diagnostics it is protecting."""
    line = "localDataPolicy = never_store"
    assert redact(line) == line


# --- policy ---------------------------------------------------------------


def test_policy_reports_that_dictation_is_not_recorded(tmp_path: Path) -> None:
    """never_store is the finding that explains an empty History table.

    Without this, an archive that is empty by policy is indistinguishable
    from one that failed to read -- which is the most dangerous failure an
    archival tool has, because it is silent and only discovered when the data
    is needed.
    """
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "prefs": {
                    "user": {
                        "localDataPolicy": "never_store",
                        "notetakerTranscriptRetention": "never_delete",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    config = read_config(path)

    assert config.policy.local_data_policy == "never_store"
    assert not config.policy.records_dictation
    assert config.policy.as_dict()["records_dictation"] is False
    assert "observed_at" in config.policy.as_dict()


def test_policy_records_dictation_under_any_other_value(tmp_path: Path) -> None:
    """An unrecognized future value is recorded verbatim, not guessed at."""
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"prefs": {"user": {"localDataPolicy": "store_everything"}}}),
        encoding="utf-8",
    )
    assert read_config(path).policy.records_dictation


def test_config_captures_what_lives_nowhere_else(tmp_path: Path) -> None:
    """The sync coordinator, voice profile and samples exist only in this file."""
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "prefs": {
                    "user": {"localDataPolicy": "never_store"},
                    "context": {
                        "writingSamples": ["a whisper budget memo"],
                        "polishPrompts": ["make it terser"],
                    },
                },
                "syncCoordinator": {"timestamps": {"meetings": "2026-08-20T00:00:00Z"}},
                "voiceProfile": {"persona": "brisk"},
            }
        ),
        encoding="utf-8",
    )
    config = read_config(path)

    assert config.sync_coordinator["timestamps"]["meetings"] == "2026-08-20T00:00:00Z"
    assert config.voice_profile == {"persona": "brisk"}
    assert config.writing_samples == ["a whisper budget memo"]
    assert config.polish_prompts == ["make it terser"]


@pytest.mark.parametrize("body", ["", "not json", "[]", "null"])
def test_unreadable_config_is_reported_not_assumed(
    tmp_path: Path, body: str
) -> None:
    """An unknown policy must not silently become the permissive default."""
    path = tmp_path / "config.json"
    path.write_text(body, encoding="utf-8")
    config = read_config(path)

    assert not config.present
    assert config.policy.local_data_policy is None


def test_missing_config_is_not_an_error(tmp_path: Path) -> None:
    """A local export still works without preferences; it just knows less."""
    assert not read_config(tmp_path / "absent.json").present


# --- session --------------------------------------------------------------


def test_session_is_parsed_through_both_layers(tmp_path: Path) -> None:
    """The credential sits one parse deeper than the file's shape suggests."""
    _write_session(tmp_path)
    info = read_session(tmp_path / "session.json")

    assert info.present
    assert info.user_id == ACCOUNT_ID
    assert info.email == OWNER_EMAIL
    assert not info.is_expired


def test_session_info_carries_no_credential(tmp_path: Path) -> None:
    """What circulates in the package must not be able to leak a token.

    read_access_token is the only function that can produce one, so a new code
    path cannot accidentally hold a credential just by taking a SessionInfo.
    """
    _write_session(tmp_path)
    info = read_session(tmp_path / "session.json")

    assert FAKE_JWT not in repr(info)
    assert "refresh" not in repr(info)


def test_expired_session_is_detected(tmp_path: Path) -> None:
    """An expired token stops the cloud backend rather than triggering a refresh.

    Supabase GoTrue rotates refresh tokens and detects reuse, so refreshing
    would invalidate the desktop app's own session and sign the user out of
    the app being backed up.
    """
    _write_session(tmp_path, expires_in=-60)
    assert read_session(tmp_path / "session.json").is_expired


def test_absent_session_is_expired_not_valid(tmp_path: Path) -> None:
    """Missing expiry must fail closed, never open."""
    info = read_session(tmp_path / "nothing.json")
    assert not info.present
    assert info.is_expired


@pytest.mark.parametrize("body", ["not json", "[]", '{"unrelated": "value"}'])
def test_malformed_session_is_absent_not_fatal(tmp_path: Path, body: str) -> None:
    """A corrupt session file disables one backend; it does not stop a run."""
    path = tmp_path / "session.json"
    path.write_text(body, encoding="utf-8")
    assert not read_session(path).present


def test_access_token_is_available_only_through_one_function(
    tmp_path: Path,
) -> None:
    """The cloud backend can get a token; nothing else has a way to."""
    _write_session(tmp_path)
    assert read_access_token(tmp_path / "session.json") == FAKE_JWT
    assert read_access_token(tmp_path / "absent.json") is None


def test_account_profile_never_contains_a_token(tmp_path: Path) -> None:
    """The archived account record is identity and expiry only.

    This is the file that would otherwise quietly carry a live credential into
    a directory the operator may back up or sync elsewhere.
    """
    _write_session(tmp_path)
    profile = account_profile(read_session(tmp_path / "session.json"))
    serialized = json.dumps(profile)

    assert FAKE_JWT not in serialized
    assert "refresh_token" not in serialized
    assert profile["email"] == OWNER_EMAIL
    assert OWNER not in serialized
