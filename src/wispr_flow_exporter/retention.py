"""Keeping what upstream deletes, for tables archived as whole files.

Meetings, notes and calendar events each have a file of their own, so a record
that disappears upstream simply keeps its file and gains a ``missing_since``.
Snapshot tables (the dictionary, todos, and every table archived generically)
and date-sharded ones (dictation history and its siblings) do not: each is one
NDJSON file per table or per day, rewritten from the rows upstream has *now*.
Before this module existed, that rewrite was the deletion. A row Wispr Flow
hard-deleted vanished from the archive on the next run that touched its file,
``sync --full`` was the most destructive run there was, and ``verify`` still
called the result consistent, because the file and its count agreed.

The fix keeps the main file an exact mirror of upstream and moves what upstream
dropped into a sibling ledger, ``<stem>.removed.ndjson``, one JSON object per
line::

    {"missing_since": "<when it was first seen gone>", "row": {...verbatim...}}

A line from the old main file that would not parse is kept as
``{"missing_since": ..., "unparsed": "<the raw text>"}``: it was in the
archive, and being damaged is not a reason for the archive to lose it.

Three properties make that safe to run every time:

- **Append-only.** Lines are only ever added, and appended at the byte level,
  so a ledger this code cannot parse is never re-encoded or truncated.
- **Deduplicated.** A line is written once per ``(identity, content)``, so a
  run interrupted between writing the ledger and rewriting the main file
  records nothing twice when the next run finds the same rows gone again --
  and a second run with nothing new upstream writes zero bytes.
- **Ledger first.** The ledger is written before the main file, so there is no
  instant at which a removed row exists in neither.

A row that comes back upstream returns to the main file, and its ledger line
stays as history; "currently removed" means a ledger row whose identity the
main file does not hold.

**Content loss, not just deletion.** A row, or a record's whole payload, can
survive upstream and still lose what it held: a column a migration drops, a
summary someone clears, a list that shrinks. An ordinary edit replacing one
value with another is the archive working as a mirror and is left alone; a
replacement that *loses* content keeps the version it replaced -- as a ledger
line marked ``superseded_at`` for rows, and under ``superseded/`` for the
payloads that are files of their own (see :func:`replace_payload`).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .schema import TableSpec
from .secure_io import (
    read_json,
    read_ndjson,
    write_bytes_if_changed,
    write_json_if_changed,
)
from .store import Archive, content_hash, row_identity

#: Suffix of the ledger that sits beside ``<stem>.ndjson``.
LEDGER_SUFFIX = ".removed.ndjson"


def ledger_path(main: Path) -> Path:
    """Return the ledger that belongs beside a snapshot or shard.

    Args:
        main: The ``.ndjson`` file whose removed rows it keeps.

    Returns:
        ``<stem>.removed.ndjson`` in the same directory.
    """
    name = main.name.removesuffix(".ndjson")
    return main.with_name(f"{name}{LEDGER_SUFFIX}")


def _entry_key(
    spec: TableSpec, entry: Mapping[str, Any]
) -> tuple[str, str, str] | None:
    """Name a ledger line for deduplication.

    Args:
        spec: The table's declaration.
        entry: One ledger line, decoded.

    Returns:
        ``(kind, identity, content)`` for a row, a digest triple for unparsed
        text, or ``None`` for a line of neither shape. The kind keeps a
        superseded version and a later removal of the same content apart.
    """
    kind = "superseded" if "superseded_at" in entry else "missing"
    row = entry.get("row")
    if isinstance(row, dict):
        return kind, row_identity(spec, row), content_hash(spec, row)
    text = entry.get("unparsed")
    if isinstance(text, str):
        return kind, "#unparsed", hashlib.sha256(text.encode("utf-8")).hexdigest()
    return None


def read_ledger(path: Path) -> list[dict[str, Any]]:
    """Read a ledger's decoded lines, in the order they were appended.

    Args:
        path: The ledger. Absent reads as empty.

    Returns:
        Every line that decoded to an object. A damaged line is skipped here
        but never removed from the file, which is only ever appended to.
    """
    entries, _ = read_ndjson(path)
    return entries


def record_removals(
    ledger: Path,
    spec: TableSpec,
    gone: Iterable[Mapping[str, Any]],
    unparsed: Iterable[str] = (),
    *,
    when: str,
    superseded: Iterable[Mapping[str, Any]] = (),
) -> int:
    """Append rows upstream no longer has, or has lost content from, once each.

    Args:
        ledger: The ledger file.
        spec: The table's declaration, which decides identity.
        gone: Rows from the old main file whose identity upstream dropped.
        unparsed: Raw lines from the old main file that would not parse.
        when: ISO timestamp recorded as ``missing_since`` or ``superseded_at``.
        superseded: Old versions of rows upstream still has, but whose new
            version lost content (see :func:`lost_content`).

    Returns:
        How many lines were appended.
    """
    existing = read_ledger(ledger)
    seen = {key for entry in existing if (key := _entry_key(spec, entry)) is not None}
    lines: list[str] = []
    payloads: list[dict[str, Any]] = [
        *({"missing_since": when, "row": dict(row)} for row in gone),
        *({"missing_since": when, "unparsed": text} for text in unparsed),
        *({"superseded_at": when, "row": dict(row)} for row in superseded),
    ]
    for payload in payloads:
        key = _entry_key(spec, payload)
        if key is None or key in seen:
            continue
        seen.add(key)
        lines.append(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    if not lines:
        return 0
    try:
        before = ledger.read_bytes()
    except FileNotFoundError:
        before = b""
    if before and not before.endswith(b"\n"):
        before += b"\n"
    write_bytes_if_changed(ledger, before + "".join(lines).encode("utf-8"))
    return len(lines)


def still_removed(
    entries: Sequence[Mapping[str, Any]],
    spec: TableSpec,
    present: set[str],
) -> list[tuple[dict[str, Any], str]]:
    """Report which ledger rows are gone upstream right now.

    Args:
        entries: The ledger's lines, oldest first.
        spec: The table's declaration.
        present: Identities the main file holds now.

    Returns:
        ``(row, missing_since)`` for the latest ledger line of each identity
        not currently present, in first-removed order.
    """
    latest: dict[str, tuple[dict[str, Any], str]] = {}
    for entry in entries:
        row = entry.get("row")
        if not isinstance(row, dict) or "missing_since" not in entry:
            continue
        identity = row_identity(spec, row)
        if identity in present:
            continue
        first = latest[identity][1] if identity in latest else None
        since = str(entry.get("missing_since") or "")
        latest[identity] = (row, first or since)
    return list(latest.values())


def key_digest(identities: Iterable[str]) -> str:
    """Fingerprint which rows a day holds, independent of their content.

    Recorded on each dictation day, so a run can notice that a day outside its
    re-read window lost or gained a row without reading that day's rows.

    Args:
        identities: The day's row identities.

    Returns:
        A short hex digest over the sorted identities.
    """
    joined = "\n".join(sorted(identities))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def _empty(value: Any) -> bool:
    """Report whether a value holds nothing worth keeping.

    Args:
        value: A decoded JSON value.

    Returns:
        ``True`` for ``None``, an empty string, list or object.
    """
    return value is None or value == "" or value == [] or value == {}


def lost_content(old: Any, new: Any) -> bool:
    """Report whether replacing ``old`` with ``new`` would lose something.

    Loss is structural: a key that held something and is gone, a value that
    held something and is now empty, a list that got shorter, a shape that
    stopped being the shape it was. Changing one value for another is not loss
    -- that is an edit, and the archive mirrors edits.

    Args:
        old: What the archive holds.
        new: What would replace it.

    Returns:
        ``True`` when a replacement would discard content.
    """
    if _empty(old):
        return False
    if isinstance(old, dict):
        if not isinstance(new, dict):
            return True
        return any(
            (key not in new and not _empty(value))
            or (key in new and lost_content(value, new[key]))
            for key, value in old.items()
        )
    if isinstance(old, list):
        if not isinstance(new, list) or len(new) < len(old):
            return True
        return any(lost_content(a, b) for a, b in zip(old, new, strict=False))
    return _empty(new)


def project_record(spec: TableSpec, payload: Any) -> Any:
    """Drop a record's churn columns before judging it for loss.

    A push flag or retry counter going back to null is Sequelize's bookkeeping,
    not content, so it must not make a record look like it lost something.

    Args:
        spec: The table's declaration.
        payload: One record.

    Returns:
        The record without its volatile columns; non-objects pass through.
    """
    if not isinstance(payload, dict):
        return payload
    return {key: value for key, value in payload.items() if key not in spec.volatile}


def keep_superseded(
    archive: Archive, entity: str, key: str, payload: Any, *, when: str
) -> bool:
    """File a payload that is about to be replaced by one that holds less.

    Content-addressed and existence-gated, so the same superseded version is
    kept once however many runs see the loss.

    Args:
        archive: The destination archive.
        entity: What kind of record, such as ``"meetings"``.
        key: The record's key.
        payload: The version being replaced.
        when: ISO timestamp recorded as ``superseded_at``.

    Returns:
        ``True`` when a file was written.
    """
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    target = archive.resolve("superseded", entity, key, f"{digest}.json")
    if target.is_file():
        return False
    return write_json_if_changed(target, {"superseded_at": when, "payload": payload})


def replace_payload(
    archive: Archive,
    path: Path,
    payload: Any,
    *,
    entity: str,
    key: str,
    when: str,
    project: Callable[[Any], Any] = lambda value: value,
) -> bool:
    """Write a payload, first keeping the one it replaces if that one held more.

    For the payloads that are files of their own -- a meeting's
    ``raw/meeting.json``, a note's ``.raw.json``, a calendar event, the
    account's JSON, each cloud endpoint -- a replacement used to be the end of
    whatever the old version held. Measured on 0.4.1: a meeting whose summary
    column went away upstream lost it from ``raw/meeting.json`` on the next
    sync that re-read the meeting.

    Args:
        archive: The destination archive.
        path: The payload's file.
        payload: The new version.
        entity: What kind of record, for the ``superseded/`` path.
        key: The record's key, for the ``superseded/`` path.
        when: ISO timestamp recorded as ``superseded_at``.
        project: Removes what must not count, such as churn columns, before
            the two versions are compared.

    Returns:
        ``True`` when anything was written.
    """
    old = read_json(path, None)
    kept = old is not None and lost_content(project(old), project(payload))
    wrote = keep_superseded(archive, entity, key, old, when=when) if kept else False
    return write_json_if_changed(path, payload) or wrote
