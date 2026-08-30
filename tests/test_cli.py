"""Argument parsing, configuration precedence, and the doctor command."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from wispr_flow_exporter.cli import (
    EXIT_BREAKING_DRIFT,
    EXIT_OK,
    EXIT_SOURCE_UNREACHABLE,
    main,
)

from conftest import FAKE_JWT, FAKE_SESSION_KEY, MEETING_A, TITLE_PLAIN

_WISPR_VARS = (
    "WISPR_DATA_DIR",
    "WISPR_DB_PATH",
    "WISPR_SYNC_SOURCE",
    "WISPR_ARCHIVE_DIR",
    "WISPR_AUDIO",
    "WISPR_MAX_AUDIO_MB",
    "WISPR_INCLUDE_SCREEN_CONTEXT",
    "WISPR_INCLUDE_AUDIO_BLOBS",
    "WISPR_INCLUDE_IMAGES",
    "WISPR_RECHECK_DAYS",
    "WISPR_STRICT_SCHEMA",
    "WISPR_API_BASE",
    "WISPR_SESSION_FILE",
)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep the developer's own environment and .env out of every test."""
    for name in _WISPR_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("WISPR_ARCHIVE_DIR", str(tmp_path / "archive"))
    # Point the data directory at somewhere that does not exist. Without this,
    # a test that forgets --data-dir silently falls through to the developer's
    # own Wispr Flow store and reads real meetings; one did, and copied 15 MB
    # of real audio into a temp archive before this guard was added.
    monkeypatch.setenv("WISPR_DATA_DIR", str(tmp_path / "no-such-wispr-flow"))
    monkeypatch.chdir(tmp_path)


def _data_dir(
    tmp_path: Path,
    wispr_db: Callable[..., Path],
    *,
    policy: str = "never_store",
    session: bool = False,
    **db_kwargs: object,
) -> Path:
    """Assemble a plausible Wispr Flow application-support directory.

    Args:
        tmp_path: Test scratch directory.
        wispr_db: The database factory.
        policy: Value for ``localDataPolicy``.
        session: Whether to write a session file.
        **db_kwargs: Passed to the database factory.

    Returns:
        The directory built.
    """
    data_dir = tmp_path / "Wispr Flow"
    data_dir.mkdir(exist_ok=True)
    built = wispr_db(**db_kwargs)
    built.replace(data_dir / "flow.sqlite")
    (data_dir / "config.json").write_text(
        json.dumps(
            {
                "prefs": {
                    "user": {
                        "localDataPolicy": policy,
                        "notetakerTranscriptRetention": "never_delete",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    if session:
        (data_dir / "session.json").write_text(
            json.dumps(
                {
                    FAKE_SESSION_KEY: json.dumps(
                        {"access_token": FAKE_JWT, "expires_at": 4102444800}
                    )
                }
            ),
            encoding="utf-8",
        )
    return data_dir


# --- parsing --------------------------------------------------------------


def test_an_empty_argument_list_prints_help() -> None:
    """An explicit empty vector is a programmatic call, not a bare shell run.

    A bare shell invocation runs the interactive setup instead; that path is
    covered in tests/test_prompts.py.
    """
    assert main([]) == EXIT_OK


def test_screen_context_needs_a_second_flag() -> None:
    """Widening to screen captures must not be a single-flag autocomplete.

    The tier includes a bitmap and an accessibility capture of whatever
    application had focus, which can be a password manager or a banking
    session.
    """
    with pytest.raises(SystemExit) as caught:
        main(["sync", "--include-screen-context"])
    assert caught.value.code == 2


def test_screen_context_with_the_acknowledgement_parses(
    tmp_path: Path, wispr_db: Callable[..., Path]
) -> None:
    """With both flags, parsing succeeds and the command runs."""
    data_dir = _data_dir(tmp_path, wispr_db)
    assert main(
        ["sync", "--data-dir", str(data_dir), "--include-screen-context", "--i-understand"]
    ) == EXIT_OK


def test_an_invalid_source_is_rejected() -> None:
    """Backend names are a closed set."""
    with pytest.raises(SystemExit) as caught:
        main(["doctor", "--source", "telepathy"])
    assert caught.value.code == 2


# --- doctor ---------------------------------------------------------------


def test_doctor_reports_a_missing_installation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No database is an unreachable source, not a crash."""
    code = main(["doctor", "--data-dir", str(tmp_path / "absent")])
    assert code == EXIT_SOURCE_UNREACHABLE
    assert "MISSING" in capsys.readouterr().out


def test_doctor_names_the_policy_that_empties_dictation(
    tmp_path: Path,
    wispr_db: Callable[..., Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An archive empty by policy must never read as an archive that worked.

    This is the highest-severity failure mode this tool has: silent,
    permanent, and only discovered when the data is finally needed.
    """
    data_dir = _data_dir(tmp_path, wispr_db)
    code = main(["doctor", "--data-dir", str(data_dir)])
    out = capsys.readouterr().out

    assert code == EXIT_OK
    assert "never_store" in out
    assert "WARNING" in out
    assert "not a failure of this tool" in out


def test_doctor_is_quiet_when_dictation_is_recorded(
    tmp_path: Path,
    wispr_db: Callable[..., Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The warning is a finding, not decoration; it must not always fire."""
    data_dir = _data_dir(tmp_path, wispr_db, policy="store_normally")
    main(["doctor", "--data-dir", str(data_dir)])

    assert "WARNING" not in capsys.readouterr().out


def test_doctor_reports_row_counts_and_artifacts(
    tmp_path: Path,
    wispr_db: Callable[..., Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The point of doctor is saying what exists before anything is written."""
    data_dir = _data_dir(
        tmp_path,
        wispr_db,
        rows={"Meetings": [{"id": MEETING_A, "title": TITLE_PLAIN}]},
    )
    main(["doctor", "--data-dir", str(data_dir)])
    out = capsys.readouterr().out

    assert "Meetings 1" in out
    assert "tables empty" in out
    assert "meeting files" in out


def test_doctor_exits_non_zero_on_breaking_drift(
    tmp_path: Path,
    wispr_db: Callable[..., Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A lost renderer input is named exactly and exits 4."""
    data_dir = _data_dir(tmp_path, wispr_db, drop_columns={"Meetings": ("title",)})
    code = main(["doctor", "--data-dir", str(data_dir)])

    assert code == EXIT_BREAKING_DRIFT
    assert "REQUIRED columns missing on Meetings" in capsys.readouterr().out


def test_doctor_treats_an_added_column_as_survivable(
    tmp_path: Path,
    wispr_db: Callable[..., Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Twenty migrations a month must not mean twelve failures a year."""
    data_dir = _data_dir(
        tmp_path, wispr_db, extra_columns={"Meetings": ("whisperQuota",)}
    )
    code = main(["doctor", "--data-dir", str(data_dir)])

    assert code == EXIT_OK
    assert "whisperQuota" in capsys.readouterr().out


def test_doctor_never_prints_a_token(
    tmp_path: Path,
    wispr_db: Callable[..., Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Diagnostics pass through the redactor at the sink."""
    data_dir = _data_dir(tmp_path, wispr_db, session=True)
    main(["doctor", "--data-dir", str(data_dir)])
    out = capsys.readouterr().out

    assert FAKE_JWT not in out
    assert FAKE_SESSION_KEY not in out
    assert "session" in out


def test_doctor_reports_a_missing_session_without_failing(
    tmp_path: Path,
    wispr_db: Callable[..., Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The local backend is fully usable with no credential at all."""
    data_dir = _data_dir(tmp_path, wispr_db, session=False)
    code = main(["doctor", "--data-dir", str(data_dir)])

    assert code == EXIT_OK
    assert "none stored" in capsys.readouterr().out


def test_doctor_writes_nothing(
    tmp_path: Path, wispr_db: Callable[..., Path]
) -> None:
    """doctor must not create the archive, or touch the source directory."""
    data_dir = _data_dir(tmp_path, wispr_db)
    before = {path: path.stat().st_mtime_ns for path in sorted(data_dir.rglob("*"))}

    main(["doctor", "--data-dir", str(data_dir)])

    after = {path: path.stat().st_mtime_ns for path in sorted(data_dir.rglob("*"))}
    assert before == after
    assert not (tmp_path / "archive").exists()
