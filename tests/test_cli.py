"""Argument parsing, configuration precedence, and the doctor command."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import (
    DEFAULT_MIGRATIONS,
    FAKE_JWT,
    FAKE_SESSION_KEY,
    MEETING_A,
    MEETING_B,
    NOTE_A,
    TITLE_PLAIN,
    archive_snapshot,
)

from wispr_flow_exporter.cli import (
    EXIT_ADDITIVE_DRIFT,
    EXIT_BREAKING_DRIFT,
    EXIT_FAILURE,
    EXIT_OK,
    EXIT_SOURCE_UNREACHABLE,
    EXIT_USAGE,
    main,
)
from wispr_flow_exporter.endpoints import EndpointError
from wispr_flow_exporter.store import Archive

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


def test_the_environment_cannot_skip_the_screen_context_acknowledgement(
    tmp_path: Path,
    wispr_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The variable widens the export exactly as the flag does, so it asks the same.

    Measured before the fix: WISPR_INCLUDE_SCREEN_CONTEXT=1 alone -- which a
    .env in the working directory can set -- archived screen captures with no
    acknowledgement, although .env.example and SECURITY.md both said the CLI
    required --i-understand.
    """
    data_dir = _data_dir(tmp_path, wispr_db)
    monkeypatch.setenv("WISPR_INCLUDE_SCREEN_CONTEXT", "1")

    code = main(["sync", "--source", "local", "--data-dir", str(data_dir)])

    assert code == EXIT_USAGE
    assert "--i-understand" in capsys.readouterr().err
    assert not (tmp_path / "archive").exists()


def test_the_environment_with_the_acknowledgement_runs(
    tmp_path: Path,
    wispr_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scheduled run keeps its setting and adds the flag, and then it works."""
    data_dir = _data_dir(tmp_path, wispr_db)
    monkeypatch.setenv("WISPR_INCLUDE_SCREEN_CONTEXT", "1")

    code = main(
        ["sync", "--source", "local", "--data-dir", str(data_dir), "--i-understand"]
    )

    assert code == EXIT_OK


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
    out = capsys.readouterr().out
    for entity in ("meetings", "notes", "dictionary", "dictation"):
        assert f"{entity}: " in out


def test_render_force_is_accepted_and_changes_nothing(
    tmp_path: Path,
    wispr_db: Callable[..., Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Scripts that pass --force still run; the flag never forced anything.

    It used to report every meeting as written while rewriting none of them.
    """
    data_dir = _data_dir(
        tmp_path,
        wispr_db,
        rows={"Meetings": [{"id": MEETING_A, "title": TITLE_PLAIN}]},
    )
    main(["sync", "--source", "local", "--data-dir", str(data_dir)])
    capsys.readouterr()
    before = archive_snapshot(tmp_path / "archive")

    code = main(["render", "--force"])

    captured = capsys.readouterr()
    assert code == EXIT_OK
    assert "no longer needed" in captured.err
    assert "meetings: 1 scanned, 0 written" in captured.out
    assert archive_snapshot(tmp_path / "archive") == before


# --- dry run --------------------------------------------------------------
# Rows carry modifiedAt on purpose. The defect this section guards against
# was a dry run saving the watermark it had advanced in memory, and a table
# with no modification times has no watermark to advance.

_CREATED = "2026-08-21 21:00:58.565 +00:00"
_MODIFIED = "2026-08-21 21:33:32.711 +00:00"


def _dated_rows() -> dict[str, list[dict[str, object]]]:
    """Build one meeting and one note, both with modification times.

    Returns:
        Rows for the database factory.
    """
    return {
        "Meetings": [
            {
                "id": MEETING_A,
                "title": TITLE_PLAIN,
                "createdAt": _CREATED,
                "modifiedAt": _MODIFIED,
                "isDeleted": 0,
            }
        ],
        "Notes": [
            {
                "id": NOTE_A,
                "title": "Murmur quota",
                "content": "- ask about the murmur quota",
                "createdAt": _CREATED,
                "modifiedAt": _MODIFIED,
                "isDeleted": 0,
            }
        ],
    }


def test_a_dry_run_leaves_no_trace(
    tmp_path: Path, wispr_db: Callable[..., Path]
) -> None:
    """The default source reaches for remote backends; none may cause a write.

    Measured before the fix: a dry run with no remote credentials at all still
    created the archive, because the save after the remote passes ran whenever
    a remote backend was merely selected.
    """
    data_dir = _data_dir(tmp_path, wispr_db, rows=_dated_rows())

    code = main(["sync", "--dry-run", "--data-dir", str(data_dir)])

    assert code == EXIT_OK
    assert not (tmp_path / "archive").exists()


def test_a_dry_run_costs_the_next_run_nothing(
    tmp_path: Path, wispr_db: Callable[..., Path]
) -> None:
    """A dry run must not tell the next real run that anything was archived.

    Measured before the fix: the dry run saved its advanced watermarks, the
    next sync scanned 0 meetings and 0 notes, nothing was ever written, and
    verify still reported the archive as consistent.
    """
    data_dir = _data_dir(tmp_path, wispr_db, rows=_dated_rows())
    main(["sync", "--dry-run", "--data-dir", str(data_dir)])

    main(["sync", "--data-dir", str(data_dir)])

    archive = Archive(root=tmp_path / "archive")
    meeting = archive.entry("meetings", MEETING_A)
    note = archive.entry("notes", NOTE_A)
    assert meeting is not None and "path" in meeting
    assert note is not None and "path" in note
    assert (tmp_path / "archive" / meeting["path"] / "meeting.md").is_file()


def test_a_dry_run_leaves_an_existing_archive_byte_identical(
    tmp_path: Path, wispr_db: Callable[..., Path]
) -> None:
    """New upstream data is reported, not recorded, until a real run writes it."""
    data_dir = _data_dir(tmp_path, wispr_db, rows=_dated_rows())
    main(["sync", "--data-dir", str(data_dir)])
    before = archive_snapshot(tmp_path / "archive")
    with sqlite3.connect(data_dir / "flow.sqlite") as writer:
        writer.execute(
            'INSERT INTO "Meetings" ("id", "title", "createdAt", "modifiedAt", '
            '"isDeleted") VALUES (?, ?, ?, ?, 0)',
            (MEETING_B, "Hush weekly", _CREATED, "2026-08-22 09:00:00.000 +00:00"),
        )

    main(["sync", "--dry-run", "--data-dir", str(data_dir)])

    assert archive_snapshot(tmp_path / "archive") == before
    main(["sync", "--data-dir", str(data_dir)])
    assert Archive(root=tmp_path / "archive").entry("meetings", MEETING_B)


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


def test_a_dotenv_can_set_only_this_tools_own_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A planted .env must not choose the proxy, the CA, or where tokens live.

    Measured before the fix: this file routed every request through
    127.0.0.1:9 while trusting ./attacker-ca.pem -- a man in the middle for the
    account's bearer token whatever WISPR_API_BASE said -- and moved the MCP
    token store into the working directory.
    """
    from wispr_flow_exporter.cli import _config

    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    (tmp_path / ".env").write_text(
        "HTTPS_PROXY=http://127.0.0.1:9\n"
        "SSL_CERT_FILE=./attacker-ca.pem\n"
        "XDG_CONFIG_HOME=./.config\n"
        "WISPR_AUDIO=skip\n",
        encoding="utf-8",
    )

    config = _config(argparse.Namespace())

    assert config.audio == "skip"
    for name in ("HTTPS_PROXY", "SSL_CERT_FILE", "XDG_CONFIG_HOME"):
        assert name not in os.environ
    err = capsys.readouterr().err
    assert "HTTPS_PROXY" in err and "SSL_CERT_FILE" in err
    assert "127.0.0.1" not in err and "attacker-ca" not in err


def test_a_dotenv_cannot_consent_to_its_own_redirect(tmp_path: Path) -> None:
    """The override has to come from the real environment.

    Two settings are required so that a redirect and the consent to it cannot
    both be accidents; one planted file carrying both made them one accident.
    """
    from wispr_flow_exporter.cli import _config

    (tmp_path / ".env").write_text(
        "WISPR_API_BASE=https://staging.example\n"
        "WISPR_ALLOW_ENDPOINT_OVERRIDE=1\n",
        encoding="utf-8",
    )

    with pytest.raises(EndpointError, match=r"staging\.example"):
        _config(argparse.Namespace())
    assert "WISPR_ALLOW_ENDPOINT_OVERRIDE" not in os.environ


def test_the_real_environment_can_still_consent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The .env names the host; the operator's own environment says yes."""
    from wispr_flow_exporter.cli import _config

    (tmp_path / ".env").write_text(
        "WISPR_API_BASE=https://staging.example\n", encoding="utf-8"
    )
    monkeypatch.setenv("WISPR_ALLOW_ENDPOINT_OVERRIDE", "1")

    assert _config(argparse.Namespace()).api_base == "https://staging.example"


def test_logout_with_nothing_stored_is_not_a_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Logging out twice is a normal thing to do and must not exit non-zero."""
    code = main(["logout"])

    assert code == EXIT_OK
    assert capsys.readouterr().out
