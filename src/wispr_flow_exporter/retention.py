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
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .schema import TableSpec
from .secure_io import read_ndjson, write_bytes_if_changed
from .store import content_hash, row_identity

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


def _entry_key(spec: TableSpec, entry: Mapping[str, Any]) -> tuple[str, str] | None:
    """Name a ledger line for deduplication.

    Args:
        spec: The table's declaration.
        entry: One ledger line, decoded.

    Returns:
        ``(identity, content)`` for a row, a digest pair for unparsed text,
        or ``None`` for a line of neither shape.
    """
    row = entry.get("row")
    if isinstance(row, dict):
        return row_identity(spec, row), content_hash(spec, row)
    text = entry.get("unparsed")
    if isinstance(text, str):
        return "#unparsed", hashlib.sha256(text.encode("utf-8")).hexdigest()
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
) -> int:
    """Append rows upstream no longer has to a ledger, once each.

    Args:
        ledger: The ledger file.
        spec: The table's declaration, which decides identity.
        gone: Rows from the old main file whose identity upstream dropped.
        unparsed: Raw lines from the old main file that would not parse.
        when: ISO timestamp recorded as ``missing_since``.

    Returns:
        How many lines were appended.
    """
    existing = read_ledger(ledger)
    seen = {key for entry in existing if (key := _entry_key(spec, entry)) is not None}
    lines: list[str] = []
    payloads: list[dict[str, Any]] = [
        *({"missing_since": when, "row": dict(row)} for row in gone),
        *({"missing_since": when, "unparsed": text} for text in unparsed),
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
        if not isinstance(row, dict):
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
