"""Argument parsing, configuration precedence, and the doctor command."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import (
    DEFAULT_MIGRATIONS,
    FAKE_JWT,
    FAKE_SESSION_KEY,
    MEETING_A,
    TITLE_PLAIN,
)

from wispr_flow_exporter.cli import (
    EXIT_ADDITIVE_DRIFT,
    EXIT_BREAKING_DRIFT,
    EXIT_FAILURE,
    EXIT_OK,
    EXIT_SOURCE_UNREACHABLE,
    main,
)

# Environment, working directory, credential store and network are isolated for
# every test by the autouse fixture in conftest.py, which began life here.


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
    """Doctor must not create the archive, or touch the source directory."""
    data_dir = _data_dir(tmp_path, wispr_db)
    before = {path: path.stat().st_mtime_ns for path in sorted(data_dir.rglob("*"))}

    main(["doctor", "--data-dir", str(data_dir)])

    after = {path: path.stat().st_mtime_ns for path in sorted(data_dir.rglob("*"))}
    assert before == after
    assert not (tmp_path / "archive").exists()


# --- the commands that were never dispatched ------------------------------
#
# sync, schema, verify and render were covered at the function layer and not
# through main(), so their flag wiring, exit codes and output formatting were
# untested. login and logout had no coverage at any layer.


@pytest.fixture
def clean_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the declared pin at the fixture's synthetic migration set.

    The fixture database carries two invented migrations, so against the real
    MIGRATION_PIN every build reads as stale_source and no drift class other
    than that one can be exercised.
    """
    from wispr_flow_exporter import cli, sqlite_source
    from wispr_flow_exporter.schema import pin_from_migrations

    pin = pin_from_migrations(DEFAULT_MIGRATIONS)
    # Both bindings: sqlite_source classifies against its import and cmd_schema
    # prints the declared figures from cli's. They are the same object in
    # production, so patching one would leave the report disagreeing with
    # itself for reasons that have nothing to do with the code under test.
    monkeypatch.setattr(sqlite_source, "MIGRATION_PIN", pin)
    monkeypatch.setattr(cli, "MIGRATION_PIN", pin)


def test_the_version_strings_agree() -> None:
    """Two files carry the version and both are believed by something else.

    ``__init__.__version__`` is stamped into every archive's ``index.json`` and
    sent as the User-Agent; ``pyproject.toml`` is what gets published. A release
    that updated one and not the other would mislabel archives while looking
    entirely fine.
    """
    import tomllib

    from wispr_flow_exporter import __version__

    root = Path(__file__).resolve().parent.parent
    declared = tomllib.loads(root.joinpath("pyproject.toml").read_text("utf-8"))

    assert declared["project"]["version"] == __version__


def test_schema_reports_the_local_declaration(
    tmp_path: Path,
    wispr_db: Callable[..., Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The command MAINTENANCE.md tells the maintainer to reach for first."""
    data_dir = _data_dir(tmp_path, wispr_db)

    code = main(["schema", "--source", "local", "--data-dir", str(data_dir)])
    out = capsys.readouterr().out

    assert code == EXIT_OK
    assert "migrations" in out
    assert "drift" in out


def test_schema_json_is_machine_readable(
    tmp_path: Path,
    wispr_db: Callable[..., Path],
    capsys: pytest.CaptureFixture[str],
    clean_pin: None,
) -> None:
    """--json is what a future check would parse, so it must actually parse."""
    data_dir = _data_dir(tmp_path, wispr_db)

    main(["schema", "--source", "local", "--data-dir", str(data_dir), "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert payload["drift"] == "ok"
    assert payload["pin"]["count"] == payload["declared_pin"]["count"]


def test_strict_schema_escalates_additive_drift(
    tmp_path: Path,
    wispr_db: Callable[..., Path],
    clean_pin: None,
) -> None:
    """Exit 3 is opt-in, and nothing asserted the opt-in worked."""
    data_dir = _data_dir(
        tmp_path, wispr_db, extra_columns={"Meetings": ("somethingNewInMigration153",)}
    )

    relaxed = main(["schema", "--source", "local", "--data-dir", str(data_dir)])
    strict = main(
        ["schema", "--source", "local", "--data-dir", str(data_dir), "--strict-schema"]
    )

    assert relaxed == EXIT_OK
    assert strict == EXIT_ADDITIVE_DRIFT


def test_verify_reports_a_consistent_archive(
    tmp_path: Path,
    wispr_db: Callable[..., Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Dispatch, exit code and wording, none of which the unit tests cover."""
    data_dir = _data_dir(
        tmp_path,
        wispr_db,
        rows={"Meetings": [{"id": MEETING_A, "title": TITLE_PLAIN}]},
    )
    main(["sync", "--source", "local", "--data-dir", str(data_dir)])
    capsys.readouterr()

    code = main(["verify", "--data-dir", str(data_dir)])

    assert code == EXIT_OK
    assert "archive is consistent" in capsys.readouterr().out


def test_render_rebuilds_without_touching_the_source(
    tmp_path: Path,
    wispr_db: Callable[..., Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Re-rendering reads the archive, so it takes no source arguments at all.

    This is also the escape hatch for a rendering held back by breaking drift,
    which makes it worth proving it runs from the CLI and not just in-process.
    """
    data_dir = _data_dir(
        tmp_path,
        wispr_db,
        rows={"Meetings": [{"id": MEETING_A, "title": TITLE_PLAIN}]},
    )
    main(["sync", "--source", "local", "--data-dir", str(data_dir)])
    capsys.readouterr()

    code = main(["render"])

    assert code == EXIT_OK


def test_an_unreachable_source_is_its_own_exit_code(tmp_path: Path) -> None:
    """Code 5 distinguishes "no database" from "the run failed"."""
    code = main(["schema", "--source", "local", "--data-dir", str(tmp_path / "nope")])

    assert code == EXIT_SOURCE_UNREACHABLE


def test_a_hostile_api_base_is_refused_before_any_request(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The credential must not leave for a host this tool was not pointed at.

    Refused at configuration time, so the failure happens before a client is
    built and therefore before the token is attached to anything.
    """
    monkeypatch.setenv("WISPR_API_BASE", "https://evil.example")

    code = main(["schema", "--source", "cloud"])

    assert code == EXIT_FAILURE
    assert "evil.example" in capsys.readouterr().out


def test_cleartext_is_refused_even_to_the_right_host(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The transport is not negotiable: a token does not travel in the clear."""
    monkeypatch.setenv("WISPR_API_BASE", "http://api.wisprflow.ai")

    code = main(["schema", "--source", "cloud"])

    assert code == EXIT_FAILURE
    assert "https" in capsys.readouterr().out


def test_a_deliberate_override_is_still_possible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A staging host stays reachable; it just cannot happen by accident."""
    from wispr_flow_exporter.cli import _config

    monkeypatch.setenv("WISPR_API_BASE", "https://staging.example")
    monkeypatch.setenv("WISPR_ALLOW_ENDPOINT_OVERRIDE", "1")

    import argparse

    assert _config(argparse.Namespace()).api_base == "https://staging.example"


def test_logout_with_nothing_stored_is_not_a_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Logging out twice is a normal thing to do and must not exit non-zero."""
    code = main(["logout"])

    assert code == EXIT_OK
    assert capsys.readouterr().out
