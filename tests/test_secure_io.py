"""Owner-only write primitives, including the copy that must not preserve mode."""

from __future__ import annotations

import errno
import json
import os
import stat
from pathlib import Path

import pytest

from wispr_flow_exporter import secure_io
from wispr_flow_exporter.secure_io import (
    DIR_MODE,
    FILE_MODE,
    _temp_for,
    copy_file_secure,
    file_digest,
    read_json,
    secure_mkdir,
    secure_write_bytes,
    secure_write_text,
    write_bytes_if_changed,
    write_json,
    write_text_if_changed,
)


def _mode(path: Path) -> int:
    """Return the permission bits of ``path``.

    Args:
        path: File or directory to inspect.

    Returns:
        The mode masked to the permission bits.
    """
    return stat.S_IMODE(path.stat().st_mode)


def test_secure_mkdir_is_owner_only(tmp_path: Path) -> None:
    """Directories are 0700 even under a permissive umask."""
    old = os.umask(0)
    try:
        target = tmp_path / "a" / "b"
        secure_mkdir(target)
        assert _mode(target) == DIR_MODE
    finally:
        os.umask(old)


def test_secure_write_text_is_owner_only(tmp_path: Path) -> None:
    """Text files are 0600 even under a permissive umask."""
    old = os.umask(0)
    try:
        target = tmp_path / "note.md"
        secure_write_text(target, "whisper budget")
        assert _mode(target) == FILE_MODE
        assert target.read_text(encoding="utf-8") == "whisper budget"
    finally:
        os.umask(old)


def test_secure_write_bytes_is_owner_only(tmp_path: Path) -> None:
    """Binary files are 0600 too."""
    target = tmp_path / "audio.opus"
    secure_write_bytes(target, b"OggS" + bytes(16))
    assert _mode(target) == FILE_MODE


def test_write_json_leaves_no_temp_file(tmp_path: Path) -> None:
    """The atomic write renames its temp file rather than leaving it behind."""
    target = tmp_path / "index.json"
    write_json(target, {"entities": {"meetings": {}}})
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "entities": {"meetings": {}}
    }
    # Temp names are hidden now, so look at everything, not just "*.tmp".
    assert [path.name for path in tmp_path.iterdir()] == ["index.json"]
    assert _mode(target) == FILE_MODE


def test_read_json_tolerates_absence_and_corruption(tmp_path: Path) -> None:
    """A missing or unparseable file yields the default, never an exception."""
    assert read_json(tmp_path / "missing.json", {"d": 1}) == {"d": 1}
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert read_json(broken, None) is None


def test_copy_file_secure_does_not_preserve_a_world_writable_mode(
    tmp_path: Path,
) -> None:
    """A 0666 source becomes a 0600 copy.

    This is the whole reason the helper exists. Wispr Flow writes config.json
    and session.json world-writable, and ``shutil.copy2`` would faithfully
    reproduce that inside an archive documented as owner-only.
    """
    src = tmp_path / "config.json"
    src.write_text("{}", encoding="utf-8")
    os.chmod(src, 0o666)
    dest = tmp_path / "archive" / "config.json"

    copy_file_secure(src, dest)

    assert _mode(src) == 0o666, "the source must not be modified"
    assert _mode(dest) == FILE_MODE


def test_copy_file_secure_returns_the_digest_of_what_it_wrote(
    tmp_path: Path,
) -> None:
    """The returned digest matches the destination, so a torn copy is detectable."""
    src = tmp_path / "upload.ogg"
    src.write_bytes(b"OggS" + bytes(4096))
    dest = tmp_path / "media" / "upload.ogg"

    digest = copy_file_secure(src, dest)

    assert digest == file_digest(dest)
    assert dest.read_bytes() == src.read_bytes()
    assert list(dest.parent.glob("*.tmp")) == []


def test_copy_file_secure_cleans_up_when_the_source_disappears(
    tmp_path: Path,
) -> None:
    """A failed copy leaves no partial file at either the temp or final path."""
    dest = tmp_path / "media" / "gone.ogg"
    with pytest.raises(OSError):
        copy_file_secure(tmp_path / "missing.ogg", dest)
    assert not dest.exists()
    assert list((tmp_path / "media").glob("*.tmp")) == []


def test_every_created_level_is_owner_only(tmp_path: Path) -> None:
    """Intermediate directories must be 0700 too, not just the leaf.

    Path.mkdir(parents=True, mode=...) applies the mode only to the final
    directory. A real archive came out with meetings/, meetings/2026/ and
    meetings/2026/08/ world-readable while every leaf was 0700 -- and those
    directory names carry meeting titles and participant names.
    """
    old = os.umask(0)
    try:
        secure_mkdir(tmp_path / "meetings" / "2026" / "08" / "a-meeting")
        for part in ("meetings", "meetings/2026", "meetings/2026/08",
                     "meetings/2026/08/a-meeting"):
            assert _mode(tmp_path / part) == DIR_MODE, f"{part} is not 0700"
    finally:
        os.umask(old)


def test_existing_directories_are_left_alone(tmp_path: Path) -> None:
    """Permissions on directories we did not create are not ours to change."""
    existing = tmp_path / "given"
    existing.mkdir(mode=0o755)
    os.chmod(existing, 0o755)

    secure_mkdir(existing / "ours")

    assert _mode(existing) == 0o755
    assert _mode(existing / "ours") == DIR_MODE


def test_a_short_write_is_finished_not_renamed_into_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """os.write may stop early; the file must still hold every byte.

    A nearly full disk makes write() return less than it was given. One call
    and no check produced a truncated file that the caller then renamed over
    the good one.
    """
    real_write = os.write

    def dribble(fd: int, data: bytes | memoryview) -> int:
        return real_write(fd, bytes(data[:7]))

    monkeypatch.setattr(os, "write", dribble)
    payload = b"quarterly whisper budget, " * 20

    write_bytes_if_changed(tmp_path / "index.json", payload)

    assert (tmp_path / "index.json").read_bytes() == payload


def test_a_write_that_makes_no_progress_raises_and_keeps_the_old_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Better an error and yesterday's index than a silently empty one."""
    target = tmp_path / "index.json"
    target.write_bytes(b"yesterday")
    monkeypatch.setattr(os, "write", lambda fd, data: 0)

    with pytest.raises(OSError):
        write_bytes_if_changed(target, b"today")

    assert target.read_bytes() == b"yesterday"


def test_a_file_planted_at_the_old_temp_name_is_not_written_into(tmp_path: Path) -> None:
    """Temp names were fixed -- index.json.tmp -- and opened with O_TRUNC.

    Anything at that name was truncated and written through, and two writers
    of one file shared it. Fresh, unpredictable names and O_EXCL end both.
    """
    planted = tmp_path / "index.json.tmp"
    planted.write_text("planted", encoding="utf-8")
    planted.chmod(0o666)

    write_json(tmp_path / "index.json", {"entities": {}})

    assert planted.read_text(encoding="utf-8") == "planted"
    assert json.loads((tmp_path / "index.json").read_text(encoding="utf-8")) == {
        "entities": {}
    }


def test_temp_names_are_hidden_and_never_repeat(tmp_path: Path) -> None:
    """Two writers of the same file must never meet in one temp file."""
    first, second = _temp_for(tmp_path / "mcp-token.json"), _temp_for(tmp_path / "mcp-token.json")

    assert first != second
    assert first.name.startswith(".mcp-token.json.") and first.name.endswith(".tmp")


def test_a_failed_write_leaves_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Debris from a failed write used to stay behind until someone swept it."""
    target = tmp_path / "index.json"
    target.write_text("yesterday", encoding="utf-8")

    def full_disk(fd: int, data: bytes) -> int:
        raise OSError(errno.ENOSPC, "no space left on device")

    monkeypatch.setattr(os, "write", full_disk)
    with pytest.raises(OSError):
        write_text_if_changed(target, "today")

    assert [path.name for path in tmp_path.iterdir()] == ["index.json"]
    assert target.read_text(encoding="utf-8") == "yesterday"


def test_a_copy_does_not_write_through_a_symlink_at_its_temp_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The copy path carries the largest files and was the one without O_NOFOLLOW."""
    outside = tmp_path / "outside.ogg"
    outside.write_bytes(b"untouched")
    source = tmp_path / "upload.ogg"
    source.write_bytes(b"OggS" + bytes(64))
    trap = tmp_path / "media" / ".upload.ogg.trap.tmp"
    trap.parent.mkdir()
    trap.symlink_to(outside)
    monkeypatch.setattr(secure_io, "_temp_for", lambda path: trap)

    with pytest.raises(OSError):
        copy_file_secure(source, tmp_path / "media" / "upload.ogg")

    assert outside.read_bytes() == b"untouched"
