"""Platform detection and source-path resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from wispr_flow_exporter import paths


def test_macos_data_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """macOS resolves to the Application Support directory."""
    monkeypatch.setattr(paths.sys, "platform", "darwin")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/tmp/home")))
    assert paths.default_data_dir() == Path(
        "/tmp/home/Library/Application Support/Wispr Flow"
    )


def test_windows_data_dir_prefers_appdata(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows honors APPDATA when it is set."""
    monkeypatch.setattr(paths.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", "/tmp/roaming")
    assert paths.default_data_dir() == Path("/tmp/roaming/Wispr Flow")


def test_linux_data_dir_honors_xdg(monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux honors XDG_CONFIG_HOME, falling back to ~/.config."""
    monkeypatch.setattr(paths.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/tmp/xdg")
    assert paths.default_data_dir() == Path("/tmp/xdg/Wispr Flow")

    monkeypatch.delenv("XDG_CONFIG_HOME")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/tmp/home")))
    assert paths.default_data_dir() == Path("/tmp/home/.config/Wispr Flow")


def test_resolve_derives_every_source_path() -> None:
    """A data directory determines the database and all sidecar files."""
    resolved = paths.resolve(data_dir="/tmp/wispr")

    assert resolved.db == Path("/tmp/wispr/flow.sqlite")
    assert resolved.config == Path("/tmp/wispr/config.json")
    assert resolved.session == Path("/tmp/wispr/session.json")
    assert resolved.feature_flags == Path("/tmp/wispr/feature-flags.json")
    assert resolved.meetings == Path("/tmp/wispr/meetings")
    assert resolved.backups == Path("/tmp/wispr/backups")


def test_resolve_accepts_an_explicit_database_elsewhere() -> None:
    """An absolute --db overrides the data directory without moving sidecars."""
    resolved = paths.resolve(data_dir="/tmp/wispr", db="/tmp/restored/flow.sqlite")

    assert resolved.db == Path("/tmp/restored/flow.sqlite")
    assert resolved.config == Path("/tmp/wispr/config.json")


def test_backup_databases_are_recognized() -> None:
    """A backup copy is flagged, because it must be opened immutable.

    A backup ships without the -shm sibling SQLite expects, so a plain
    ``mode=ro`` open fails SQLITE_CANTOPEN. It also has different provenance,
    which the archive records rather than silently conflating with a live read.
    """
    live = paths.resolve(data_dir="/tmp/wispr")
    assert not live.db_is_backup

    from_dir = paths.resolve(
        data_dir="/tmp/wispr", db="/tmp/wispr/backups/backup-2026-08-29.sqlite"
    )
    assert from_dir.db_is_backup

    by_name = paths.resolve(data_dir="/tmp/wispr", db="/tmp/elsewhere/backup-x.sqlite")
    assert by_name.db_is_backup


def test_there_is_no_credential_cache() -> None:
    """Nothing mints a token, so nothing stores one.

    A cache would be a second place a credential could live, and the whole
    design of the cloud backend is that there is exactly one.
    """
    assert not hasattr(paths, "token_store_path")
