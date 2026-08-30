"""The archive on disk: where records live, and what is known about them.

Three things here differ deliberately from the sibling project this borrows
its conventions from, and each difference is forced by Wispr Flow's data
rather than chosen for its own sake.

**The index is namespaced.** A flat ``{id: entry}`` map works when there is one
kind of record. Here there are ten, with incompatible key shapes -- UUIDs, a
181-character base32 calendar id, a phrase -- so entries live under
``entities[<name>][<key>]`` and cannot collide.

**The content digest is taken over a projection.** Wispr rows carry a dozen
columns that flip without the record changing: the ``*PendingPush`` flags, the
``*Retries`` counters, ``syncedAt``, ``lastUsed``, ``frequencyUsed``. Hashing
those would rewrite every file in the archive on every run. The digest ignores
the columns ``schema.py`` declares volatile, while the values themselves are
still archived verbatim, so nothing is lost and nothing churns.

**Deletion has three states, not two.** Wispr tombstones rows in place, so
"gone from the source" and "marked deleted in the source" are different facts
and are recorded as such. Neither ever removes anything from disk: a record
upstream has deleted is exactly the record an archive exists to preserve. The
sharpest form of that is ``transcript_deleted_upstream``, set when Wispr Flow
has dropped a transcript that this archive still holds.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import __version__
from .normalize import slugify
from .schema import Layout, TableSpec
from .secure_io import read_json, secure_mkdir, write_json

INDEX_NAME = "index.json"
STATE_NAME = ".sync-state.json"
SCHEMA_VERSION = 1

# Records whose creation date cannot be resolved still have to go somewhere,
# and somewhere honest. Filing them under the current date would invent
# provenance the source never provided.
UNDATED = "undated"

STATE_PRESENT = "present"
STATE_SOFT_DELETED = "soft_deleted"
STATE_ABSENT = "absent"

# Source table to the directory it is archived under. Tables without an entry
# land in tables/<Name>, which is what makes an unrecognized future table
# archivable with no code change.
ENTITY_DIRS: Mapping[str, str] = {
    "Meetings": "meetings",
    "Notes": "notes",
    "CalendarEvents": "calendar",
    "Dictionary": "dictionary",
    "History": "dictation",
    "Todos": "todos",
}


class UnsafeArchivePathError(Exception):
    """A path would have resolved outside the archive root."""


def entity_name(table: str) -> str:
    """Return the archive directory for a source table.

    Args:
        table: Source table name.

    Returns:
        The directory name, defaulting to ``tables/<Name>``.
    """
    return ENTITY_DIRS.get(table, f"tables/{table}")


def content_hash(spec: TableSpec, data: Mapping[str, Any]) -> str:
    """Digest a record, ignoring columns that churn without meaning.

    Args:
        spec: The table's declaration, which names the volatile columns.
        data: The record's column values.

    Returns:
        Hex SHA-256 over the canonical JSON of the non-volatile columns.
    """
    projected = {
        column: value
        for column, value in sorted(data.items())
        if column not in spec.volatile
    }
    canonical = json.dumps(projected, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def record_dir_name(when: datetime | None, title: Any, key: str) -> str:
    """Build the ``YYYY-MM-DD--slug--id`` name for one record.

    The **id**, not the slug, is what makes the name unique and safe. A title
    that slugifies to the empty string, to ``..``, or to three hundred
    characters still yields a valid, contained name, because the slug is only
    ever a readability affordance.

    The full id is used rather than a prefix: at ten thousand meetings a
    32-bit prefix carries roughly a one percent chance of collision, which is
    not a risk worth taking with an archive.

    Args:
        when: The record's creation time, if one could be resolved.
        title: The record's title.
        key: The validated primary key.

    Returns:
        A single path component.
    """
    date_part = when.strftime("%Y-%m-%d") if when else UNDATED
    return f"{date_part}--{slugify(title)}--{key}"


def shard_name(when: datetime | None) -> str:
    """Return the ``YYYY/MM/YYYY-MM-DD`` stem for a date-sharded record.

    Args:
        when: The record's timestamp.

    Returns:
        A relative stem, or an undated one when no timestamp resolved.
    """
    if when is None:
        return f"{UNDATED}/{UNDATED}"
    return f"{when:%Y/%m/%Y-%m-%d}"


def dated_prefix(when: datetime | None) -> str:
    """Return the ``YYYY/MM`` directory a record is filed under.

    Args:
        when: The record's creation time.

    Returns:
        A relative directory, or ``"undated"``.
    """
    return f"{when:%Y/%m}" if when else UNDATED


@dataclass(slots=True)
class Archive:
    """An archive directory, its index and its sync state.

    Attributes:
        root: The archive root. Every write is verified to land inside it.
        index: Namespaced record index, loaded from ``index.json``.
        state: Watermarks, cursors, schema pin and observed policy.
    """

    root: Path
    index: dict[str, Any] = None  # type: ignore[assignment]
    state: dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        """Normalize the root and load any existing index and state."""
        self.root = self.root.expanduser().resolve()
        if self.index is None:
            self.index = read_json(
                self.root / INDEX_NAME,
                {"schema_version": SCHEMA_VERSION, "entities": {}},
            )
        if self.state is None:
            self.state = read_json(
                self.root / STATE_NAME,
                {"schema_version": SCHEMA_VERSION, "sources": {}},
            )
        self.index.setdefault("entities", {})
        self.state.setdefault("sources", {})

    # --- paths ------------------------------------------------------------

    def resolve(self, *parts: str) -> Path:
        """Resolve a path inside the archive, refusing to escape it.

        Guards both ``..`` traversal and absolute components: ``Path("/a") /
        "/etc"`` is ``/etc``, so joining an attacker-influenced absolute
        component would silently leave the archive without any ``..`` in
        sight.

        Args:
            *parts: Relative path components.

        Returns:
            The resolved absolute path.

        Raises:
            UnsafeArchivePathError: The result lies outside the root.
        """
        candidate = self.root
        for part in parts:
            if not part:
                continue
            if Path(part).is_absolute():
                raise UnsafeArchivePathError(f"absolute component refused: {part!r}")
            candidate = candidate / part
        resolved = Path(candidate).resolve()
        if resolved != self.root and self.root not in resolved.parents:
            raise UnsafeArchivePathError(
                f"path escapes the archive root: {'/'.join(parts)!r}"
            )
        return resolved

    def record_path(
        self,
        table: str,
        spec: TableSpec,
        key: str,
        *,
        when: datetime | None = None,
        title: Any = None,
    ) -> Path:
        """Compute where one record belongs.

        Args:
            table: Source table name.
            spec: The table's declaration, whose layout decides the shape.
            key: The record's primary key.
            when: The record's date, for sharding.
            title: The record's title, for readability only.

        Returns:
            An absolute path: a directory for ``ENTITY``, a stem for
            ``DOCUMENT``, and a file for ``SHARD`` and ``SNAPSHOT``.
        """
        base = entity_name(table)
        if spec.layout is Layout.ENTITY:
            return self.resolve(base, dated_prefix(when), record_dir_name(when, title, key))
        if spec.layout is Layout.DOCUMENT:
            return self.resolve(base, dated_prefix(when), record_dir_name(when, title, key))
        if spec.layout is Layout.SHARD:
            return self.resolve(base, f"{shard_name(when)}.ndjson")
        return self.resolve(base, f"{Path(base).name}.ndjson")

    def relative(self, path: Path) -> str:
        """Express an archive path relative to the root, for the index.

        Args:
            path: An absolute path inside the archive.

        Returns:
            A POSIX-style relative path.
        """
        return path.relative_to(self.root).as_posix()

    # --- index ------------------------------------------------------------

    def entries(self, entity: str) -> dict[str, Any]:
        """Return the index namespace for one entity, creating it if needed.

        Args:
            entity: Archive directory name.

        Returns:
            The mutable namespace.
        """
        return self.index["entities"].setdefault(entity, {})

    def entry(self, entity: str, key: str) -> dict[str, Any] | None:
        """Look up one record's index entry.

        Args:
            entity: Archive directory name.
            key: Record key.

        Returns:
            The entry, or ``None`` when the record has never been archived.
        """
        found = self.entries(entity).get(key)
        return found if isinstance(found, dict) else None

    def existing_path(self, entity: str, key: str) -> Path | None:
        """Return where a record is currently archived, if it is.

        The stored path is treated as untrusted input: a hand-edited or
        corrupted index must not be able to redirect a write, or a move,
        outside the archive.

        Args:
            entity: Archive directory name.
            key: Record key.

        Returns:
            The resolved path, or ``None`` when absent or unsafe.
        """
        found = self.entry(entity, key)
        if not found or not isinstance(found.get("path"), str):
            return None
        try:
            return self.resolve(*found["path"].split("/"))
        except UnsafeArchivePathError:
            return None

    def put(self, entity: str, key: str, **fields: Any) -> dict[str, Any]:
        """Create or update one index entry.

        Keys whose value is ``None`` are dropped rather than stored. The index
        is compared and rewritten often, and storing explicit nulls would make
        every optional field churn between runs.

        Args:
            entity: Archive directory name.
            key: Record key.
            **fields: Entry fields to set.

        Returns:
            The stored entry.
        """
        entry = self.entries(entity).setdefault(key, {})
        for name, value in fields.items():
            if value is None:
                entry.pop(name, None)
            else:
                entry[name] = value
        return entry

    def relocate(self, entity: str, key: str, destination: Path) -> bool:
        """Move an archived record to a new path after a retitle.

        One record keeps one location. Copying instead would leave the archive
        holding two versions of a renamed meeting with no way to tell which is
        current.

        Args:
            entity: Archive directory name.
            key: Record key.
            destination: Where the record now belongs.

        Returns:
            ``True`` when something was moved.
        """
        current = self.existing_path(entity, key)
        if current is None or current == destination or not current.exists():
            return False
        secure_mkdir(destination.parent)
        if destination.exists():
            # The destination should not exist, but if a previous run was
            # interrupted between the move and the index write it might. Keep
            # the newer copy rather than failing the whole pass.
            shutil.rmtree(destination) if destination.is_dir() else destination.unlink()
        shutil.move(str(current), str(destination))
        return True

    def mark_seen(
        self, entity: str, key: str, *, soft_deleted: bool, when: str
    ) -> None:
        """Record that a key was present in the source on this run.

        Args:
            entity: Archive directory name.
            key: Record key.
            soft_deleted: Whether the source has tombstoned it.
            when: ISO timestamp for the observation.
        """
        state = STATE_SOFT_DELETED if soft_deleted else STATE_PRESENT
        entry = self.put(entity, key, upstream_state=state, archived_at=when)
        if soft_deleted:
            entry.setdefault("soft_deleted_since", when)
        else:
            entry.pop("soft_deleted_since", None)
        # A record that came back is no longer missing.
        entry.pop("missing_since", None)

    def mark_absent(self, entity: str, seen: Iterable[str], *, when: str) -> list[str]:
        """Flag archived records that no longer exist in the source.

        Nothing is deleted. The record stays exactly where it is and gains a
        note of when it stopped being upstream.

        Args:
            entity: Archive directory name.
            seen: Every key observed in the source this run.
            when: ISO timestamp for the observation.

        Returns:
            The keys newly marked absent.
        """
        present = set(seen)
        newly: list[str] = []
        for key, entry in self.entries(entity).items():
            if key in present or not isinstance(entry, dict):
                continue
            if entry.get("upstream_state") == STATE_ABSENT:
                continue
            entry["upstream_state"] = STATE_ABSENT
            entry.setdefault("missing_since", when)
            newly.append(key)
        return newly

    # --- sync state -------------------------------------------------------

    def source_state(self, source: str) -> dict[str, Any]:
        """Return the mutable state namespace for one backend.

        Args:
            source: Backend name, such as ``"wispr-local"``.

        Returns:
            The namespace, created on first use.
        """
        return self.state["sources"].setdefault(source, {})

    def watermark(self, source: str, entity: str) -> Any:
        """Return the stored watermark value for one entity.

        Args:
            source: Backend name.
            entity: Archive directory name.

        Returns:
            The raw stored value, or ``None``.
        """
        marks = self.source_state(source).get("watermarks", {})
        found = marks.get(entity)
        return found.get("value") if isinstance(found, dict) else None

    def set_watermark(self, source: str, entity: str, column: str, value: Any) -> None:
        """Advance an entity's watermark.

        The column is stored alongside the value so a later schema change that
        removes it is detectable, rather than silently comparing against a
        column that no longer exists.

        Args:
            source: Backend name.
            entity: Archive directory name.
            column: Column the value came from.
            value: The raw stored value, kept unparsed so the next run's SQL
                comparison matches the source's own encoding.
        """
        if value is None:
            return
        marks = self.source_state(source).setdefault("watermarks", {})
        marks[entity] = {"column": column, "value": value}

    def artifact_cursor(self, source: str, key: str) -> dict[str, Any]:
        """Return the recorded fingerprints of one meeting's artifacts.

        Args:
            source: Backend name.
            key: Meeting id.

        Returns:
            The cursor, empty when the meeting is new.
        """
        cursors = self.source_state(source).setdefault("artifact_cursors", {})
        return cursors.setdefault(key, {})

    # --- persistence ------------------------------------------------------

    def save(self) -> None:
        """Write the index and sync state atomically.

        Called before any exception leaves a sync pass, on interrupt, and
        periodically during long runs, so an interrupted archive is always
        resumable rather than merely usually resumable.
        """
        secure_mkdir(self.root)
        self.index["schema_version"] = SCHEMA_VERSION
        self.index["tool_version"] = __version__
        self.state["schema_version"] = SCHEMA_VERSION
        self.state["tool_version"] = __version__
        write_json(self.root / INDEX_NAME, self.index)
        write_json(self.root / STATE_NAME, self.state)

    def count(self, entity: str | None = None) -> int:
        """Count archived records.

        Args:
            entity: One entity, or ``None`` for every entity.

        Returns:
            The number of index entries.
        """
        if entity is not None:
            return len(self.entries(entity))
        return sum(len(records) for records in self.index["entities"].values())
