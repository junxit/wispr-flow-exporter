"""Owner-only write primitives, including the copy that must not preserve mode."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from wispr_flow_exporter.secure_io import (
    DIR_MODE,
    FILE_MODE,
    copy_file_secure,
    file_digest,
    read_json,
    remove_stale_temp,
    secure_mkdir,
    secure_write_bytes,
    secure_write_text,
    write_json,
    write_ndjson,
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
    assert list(tmp_path.glob("*.tmp")) == []
    assert _mode(target) == FILE_MODE


def test_read_json_tolerates_absence_and_corruption(tmp_path: Path) -> None:
    """A missing or unparseable file yields the default, never an exception."""
    assert read_json(tmp_path / "missing.json", {"d": 1}) == {"d": 1}
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert read_json(broken, None) is None


def test_write_ndjson_writes_one_object_per_line(tmp_path: Path) -> None:
    """Records land one per line and the count is returned."""
    target = tmp_path / "rows.ndjson"
    count = write_ndjson(target, [{"a": 1}, {"b": "hush"}])
    assert count == 2
    lines = target.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in lines] == [{"a": 1}, {"b": "hush"}]
    assert _mode(target) == FILE_MODE


def test_write_ndjson_handles_an_empty_iterable(tmp_path: Path) -> None:
    """An empty table still produces a file, so its absence means "not read"."""
    target = tmp_path / "empty.ndjson"
    assert write_ndjson(target, []) == 0
    assert target.read_text(encoding="utf-8") == ""


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


def test_remove_stale_temp_sweeps_an_interrupted_run(tmp_path: Path) -> None:
    """Leftover temp files from a killed run are removed, real files are not."""
    (tmp_path / "index.json.tmp").write_text("partial", encoding="utf-8")
    (tmp_path / "index.json").write_text("{}", encoding="utf-8")

    assert remove_stale_temp(tmp_path) == 1
    assert (tmp_path / "index.json").exists()
    assert list(tmp_path.glob("*.tmp")) == []


def test_remove_stale_temp_tolerates_a_missing_directory(tmp_path: Path) -> None:
    """Sweeping a directory that was never created is not an error."""
    assert remove_stale_temp(tmp_path / "nope") == 0
