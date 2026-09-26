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

import errno
import hashlib
import json
import os
import secrets
import stat
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


def secure_mkdir(path: Path, *, narrow_existing: bool = False) -> None:
    """Create a directory tree, owner-accessible only at every level.

    ``Path.mkdir(parents=True, mode=...)`` applies the mode to the **leaf
    only**; intermediate directories are created with the default permissions,
    which is typically 0755. That was measured, not assumed: a real archive
    came out with ``meetings/``, ``meetings/2026/`` and ``meetings/2026/08/``
    world-readable while every leaf was 0700. The archive root being 0700
    blocked traversal in that layout, but relying on one directory to hold the
    guarantee is exactly the kind of accident that survives until someone
    points the archive somewhere else.

    Each level this call creates is therefore created and chmodded
    individually. Directories that already existed are left alone -- their
    permissions are not ours to change.

    The archive root is the one exception, via ``narrow_existing``. The default
    root is the *relative* ``./archive``, which an operator naturally creates
    themselves before the first run, and a directory made at a default umask is
    0755. Everything inside is still 0600 or 0700, so this is a listing that
    leaks rather than contents -- but "directories are 0700" is a promise this
    tool makes, and a root it was pointed at is a directory it has been told to
    own.

    Args:
        path: Directory to create.
        narrow_existing: Also narrow ``path`` itself when it already exists.
            Applies to the leaf only; ancestors are still left as they are.
    """
    missing: list[Path] = []
    probe = path
    while not probe.exists():
        missing.append(probe)
        if probe.parent == probe:
            break
        probe = probe.parent

    for target in reversed(missing):
        target.mkdir(mode=DIR_MODE, exist_ok=True)
        try:
            # mkdir's mode is masked by the umask, so it is reapplied.
            os.chmod(target, DIR_MODE)
        except OSError:
            pass

    if narrow_existing and not missing:
        try:
            if stat.S_IMODE(path.stat().st_mode) != DIR_MODE:
                os.chmod(path, DIR_MODE)
        except OSError:
            pass


def secure_write_bytes(path: Path, payload: bytes) -> None:
    """Write bytes to ``path``, owner-only from the moment it exists.

    The mode is passed to ``open`` rather than applied afterwards. Writing
    first and chmodding second leaves a real window -- short, but a window --
    in which the file exists at whatever the umask allows, typically 0644, and
    everything this package writes goes through here: the index, the sync
    state, every rendered document, and the MCP token store.

    The path must not exist yet: ``O_EXCL`` refuses a file already there, and
    ``O_NOFOLLOW`` a symlink. Callers write to a fresh, unpredictable temp name
    (see :func:`_temp_for`) and rename it into place. Temp names used to be
    ``<name>.tmp``, fixed and truncated on open, so two writers of the same
    file -- two runs refreshing the MCP token at once -- could interleave
    their bytes in one inode, and a file planted at that name ahead of time
    was written into as it stood. ``os.open`` still masks the mode by the
    umask, so it is reapplied on the open descriptor -- ``fchmod``, not
    ``chmod``, so the thing being narrowed is provably the file just created.

    ``os.write`` may write less than it was given -- POSIX allows it, and a
    nearly full disk does it -- and returns how much it wrote. One call and no
    check used to leave a truncated temp file for the caller to rename over the
    good one, which for ``index.json`` meant an index the next run could not
    read. The loop finishes the write or raises, and a raise leaves the good
    file where it was.

    Args:
        path: Destination file.
        payload: Contents to write.

    Raises:
        OSError: The write could not be completed.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, FILE_MODE)
    try:
        try:
            os.fchmod(fd, FILE_MODE)
        except OSError:
            pass
        remaining = memoryview(payload)
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                raise OSError(errno.EIO, f"wrote nothing to {path}")
            remaining = remaining[written:]
    finally:
        os.close(fd)


def _temp_for(path: Path) -> Path:
    """Name a temp file beside ``path`` that no other writer will choose.

    Hidden, so a half-written file never shows up in a listing, and random, so
    neither a concurrent writer nor anyone planting a file ahead of time can
    know it.

    Args:
        path: The file about to be replaced.

    Returns:
        A sibling path that does not exist yet.
    """
    return path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")


def _replace_with(path: Path, payload: bytes) -> None:
    """Replace a file's contents atomically, owner-only, leaving no debris.

    Args:
        path: The file to write.
        payload: Its new contents.
    """
    secure_mkdir(path.parent)
    tmp = _temp_for(path)
    try:
        secure_write_bytes(tmp, payload)
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def secure_write_text(path: Path, text: str) -> None:
    """Write text to ``path``, owner-only from the moment it exists.

    Args:
        path: Destination file.
        text: Contents to write.
    """
    secure_write_bytes(path, text.encode("utf-8"))


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


#: Refuse to read back an archived NDJSON larger than this. Our own files, but
#: read on every run that rewrites them, so they get the same kind of bound the
#: NDJSON transcript reader applies to files another application wrote.
MAX_NDJSON_BYTES = 512 * 1024 * 1024


def read_ndjson(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Read an archived NDJSON file back, keeping what cannot be parsed.

    Used before a snapshot or a shard is rewritten, to find the rows upstream
    no longer has. A line that will not parse is returned as text rather than
    dropped: it was in the archive, and the archive does not get to lose it
    for being damaged.

    Args:
        path: The file. Absent reads as empty.

    Returns:
        ``(rows, unparsed)``: every line that decoded to a JSON object, and the
        raw text of every non-empty line that did not.

    Raises:
        OSError: The file exists but is larger than :data:`MAX_NDJSON_BYTES`
            or cannot be read.
    """
    try:
        if path.stat().st_size > MAX_NDJSON_BYTES:
            raise OSError(f"{path} is larger than {MAX_NDJSON_BYTES} bytes")
        text = path.read_bytes().decode("utf-8", errors="replace")
    except FileNotFoundError:
        return [], []
    rows: list[dict[str, Any]] = []
    unparsed: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except (ValueError, RecursionError):
            unparsed.append(line)
            continue
        if isinstance(payload, dict):
            rows.append(payload)
        else:
            unparsed.append(line)
    return rows, unparsed


def write_json(path: Path, payload: Any) -> None:
    """Write JSON atomically, so an interrupted run cannot truncate the file.

    Args:
        path: Destination file.
        payload: JSON-serializable value.
    """
    # Permissions are set on the temp file *before* the rename, so the final
    # path is never briefly world-readable.
    _replace_with(
        path, json.dumps(payload, indent=2, ensure_ascii=False, default=str).encode()
    )


def write_text_if_changed(path: Path, text: str) -> bool:
    """Write text only when it differs from what is already there.

    This is what makes "a re-run with no upstream change writes zero bytes" a
    property of the code rather than of the calling logic being careful. The
    hash short-circuits in ``sync`` avoid the work of rendering; this avoids
    the write itself, so even a rendering change that happens to be a no-op
    leaves mtimes alone.

    Args:
        path: Destination file.
        text: Contents to write.

    Returns:
        ``True`` when the file was written.
    """
    try:
        if path.read_text(encoding="utf-8") == text:
            return False
    except (OSError, ValueError):
        pass
    _replace_with(path, text.encode("utf-8"))
    return True


def write_json_if_changed(path: Path, payload: Any) -> bool:
    """Write JSON only when it differs from what is already there.

    Args:
        path: Destination file.
        payload: JSON-serializable value.

    Returns:
        ``True`` when the file was written.
    """
    return write_text_if_changed(
        path, json.dumps(payload, indent=2, ensure_ascii=False, default=str)
    )


def write_ndjson_if_changed(path: Path, records: Iterable[Any]) -> bool:
    """Write NDJSON only when it differs from what is already there.

    Args:
        path: Destination file.
        records: JSON-serializable values, one per output line.

    Returns:
        ``True`` when the file was written.
    """
    body = "".join(
        json.dumps(record, ensure_ascii=False, default=str) + "\n"
        for record in records
    )
    return write_text_if_changed(path, body)


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
    tmp = _temp_for(dest)
    digest = hashlib.sha256()
    # 0600 from creation, and the same open every other write uses: a fresh
    # name, O_EXCL, O_NOFOLLOW, fchmod on the descriptor. This path used to
    # open a predictable name with neither flag and chmod it by path
    # afterwards -- the one write the 0.4.0 fix to secure_write_bytes missed,
    # although it carries the largest files the archive holds.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, FILE_MODE)
    try:
        try:
            os.fchmod(fd, FILE_MODE)
        except OSError:
            pass
        with open(fd, "wb", closefd=True) as out, src.open("rb") as handle:
            while chunk := handle.read(CHUNK_SIZE):
                digest.update(chunk)
                out.write(chunk)
        tmp.replace(dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
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


def write_bytes_if_changed(path: Path, payload: bytes) -> bool:
    """Write bytes only when they differ from what is already there.

    Args:
        path: Destination file.
        payload: Contents to write.

    Returns:
        ``True`` when the file was written.
    """
    try:
        if path.read_bytes() == payload:
            return False
    except OSError:
        pass
    _replace_with(path, payload)
    return True
