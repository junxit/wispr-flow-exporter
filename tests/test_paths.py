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


def test_the_credential_store_is_nowhere_near_an_archive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The one credential this tool stores must not travel with an archive.

    This replaces an older assertion that no token store existed at all. That
    held while every backend borrowed the app's own token; the MCP server is a
    separate OAuth resource with a separate issuer that rejects it, so there is
    now exactly one credential this tool mints and keeps.

    What mattered about the old rule survives intact and is what is asserted
    here: an archive is the thing people copy to a backup drive or hand to
    someone else, and no credential is inside one.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    store = paths.token_store_path()

    assert store.parent.name == "wispr-flow-exporter"
    assert store.name.endswith(".json")
    # Not under any plausible archive root, including the default.
    for archive in (Path("archive").resolve(), tmp_path / "archive"):
        assert archive not in store.parents


def test_the_credential_store_follows_xdg(monkeypatch: pytest.MonkeyPatch) -> None:
    """Somebody who set XDG_CONFIG_HOME meant it, macOS or not."""
    monkeypatch.setenv("XDG_CONFIG_HOME", "/tmp/xdg-example")

    assert paths.token_store_path() == Path(
        "/tmp/xdg-example/wispr-flow-exporter/mcp-token.json"
    )

    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    assert paths.token_store_path().parts[-3:] == (
        ".config",
        "wispr-flow-exporter",
        "mcp-token.json",
    )
