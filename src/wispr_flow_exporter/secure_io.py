"""Owner-only filesystem primitives shared by the archive and the token cache.

The archive holds verbatim transcripts, dictation history and -- when the
operator opts in -- screen captures, so everything is written 0600 in 0700
directories rather than inheriting the process umask (typically world-readable
0644/0755).

The copy helper here exists for a specific reason. Wispr Flow writes
``upload.ogg`` at 0644 and ``config.json`` and ``session.json`` at **0666**, and
``shutil.copy2`` preserves the source mode. Copying with the obvious stdlib call
would therefore reproduce a world-writable file inside an archive whose whole
premise is that it is owner-only, so ``copy_file_secure`` sets the mode itself
and never consults the source's.

File modes are a documented guarantee in ``SECURITY.md``, so they live in one
module with one set of tests rather than being reimplemented per caller.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

# The archive can contain the most sensitive data on the machine, so it is not
# allowed to inherit a permissive umask.
FILE_MODE = 0o600
DIR_MODE = 0o700

# Read size for hashing and copying. Meeting audio runs to tens of megabytes,
# so nothing here loads a file whole.
CHUNK_SIZE = 1024 * 1024


def secure_mkdir(path: Path) -> None:
    """Create a directory tree, owner-accessible only.

    ``Path.mkdir(mode=...)`` is subject to the umask, so the mode is applied
    explicitly afterwards.

    Args:
        path: Directory to create.
    """
    path.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    try:
        os.chmod(path, DIR_MODE)
    except OSError:
        pass


def secure_write_text(path: Path, text: str) -> None:
    """Write text to ``path`` with owner-only permissions.

    Args:
        path: Destination file.
        text: Contents to write.
    """
    path.write_text(text, encoding="utf-8")
    try:
        os.chmod(path, FILE_MODE)
    except OSError:
        pass


def secure_write_bytes(path: Path, payload: bytes) -> None:
    """Write bytes to ``path`` with owner-only permissions.

    Args:
        path: Destination file.
        payload: Contents to write.
    """
    path.write_bytes(payload)
    try:
        os.chmod(path, FILE_MODE)
    except OSError:
        pass


def read_json(path: Path, default: Any) -> Any:
    """Read a JSON file, tolerating absence and corruption.

    Args:
        path: File to read.
        default: Value to return when the file is missing or unparseable.

    Returns:
        The decoded contents, or ``default``.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path: Path, payload: Any) -> None:
    """Write JSON atomically, so an interrupted run cannot truncate the file.

    Args:
        path: Destination file.
        payload: JSON-serializable value.
    """
    secure_mkdir(path.parent)
    tmp = path.with_suffix(path.suffix + ".tmp")
    # Permissions are set on the temp file *before* the rename, so the final
    # path is never briefly world-readable.
    secure_write_text(
        tmp, json.dumps(payload, indent=2, ensure_ascii=False, default=str)
    )
    tmp.replace(path)


def write_ndjson(path: Path, records: Iterable[Any]) -> int:
    """Write one JSON object per line, atomically.

    Args:
        path: Destination file.
        records: JSON-serializable values, one per output line.

    Returns:
        The number of records written.
    """
    secure_mkdir(path.parent)
    tmp = path.with_suffix(path.suffix + ".tmp")
    count = 0
    lines: list[str] = []
    for record in records:
        lines.append(json.dumps(record, ensure_ascii=False, default=str))
        count += 1
    secure_write_text(tmp, "".join(f"{line}\n" for line in lines))
    tmp.replace(path)
    return count


def copy_file_secure(src: Path, dest: Path) -> str:
    """Copy a file into the archive with owner-only permissions.

    Deliberately not ``shutil.copy2``/``copystat``: Wispr Flow's own files are
    0644 and 0666, and preserving that mode would put a world-writable file
    inside an archive documented as owner-only. The destination mode is set
    from ``FILE_MODE``, never from the source.

    The copy goes through a temporary path in the destination directory and is
    renamed into place, so an interrupted run never leaves a half-written file
    at the final name. The digest is computed from the bytes actually written,
    so a caller can detect a source that changed mid-copy.

    Args:
        src: File to copy.
        dest: Destination path.

    Returns:
        Hex SHA-256 of the copied bytes.
    """
    secure_mkdir(dest.parent)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    digest = hashlib.sha256()
    # 0600 from creation, so there is no window in which the temp file is
    # readable by anyone else.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
    try:
        with open(fd, "wb", closefd=True) as out, src.open("rb") as handle:
            while chunk := handle.read(CHUNK_SIZE):
                digest.update(chunk)
                out.write(chunk)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    try:
        os.chmod(tmp, FILE_MODE)
    except OSError:
        pass
    tmp.replace(dest)
    return digest.hexdigest()


def file_digest(path: Path) -> str:
    """Compute the SHA-256 of a file without loading it whole.

    Args:
        path: File to hash.

    Returns:
        Hex SHA-256 digest.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def remove_stale_temp(directory: Path) -> int:
    """Delete leftover ``.tmp`` files from an interrupted earlier run.

    Args:
        directory: Directory to sweep, non-recursively.

    Returns:
        The number of files removed.
    """
    removed = 0
    if not directory.is_dir():
        return removed
    for candidate in directory.glob("*.tmp"):
        try:
            candidate.unlink()
        except OSError:
            continue
        removed += 1
    return removed
