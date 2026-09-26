"""Checking that the archive says what it holds, and holds what it says.

``verify`` can be authoritative here in a way it cannot be for a tool that
talks to a remote API. The upstream is a local SQLite file, so every record
that should exist can be counted rather than sampled. ``--deep`` therefore
means "recompute the digest from the archived payload", not "pay for more
requests".

Four kinds of disagreement matter, and they are different problems:

- an index entry pointing at a file that is not there -- the archive lost data;
- a directory on disk with no index entry -- the index lost track of data;
- an archived payload whose digest no longer matches the index -- something
  edited the archive, or a write was torn;
- a record in the source with no index entry -- the archive is behind, or a
  pass failed without saying so.

The last is the one an archival tool must never miss, so it is checked against
the live table rather than inferred from the previous run's own counts.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .normalize import calendar_key
from .retention import ledger_path, read_ledger
from .schema import EXPECTED
from .secure_io import read_json, read_ndjson
from .sqlite_source import SqliteSource
from .store import (
    STATE_ABSENT,
    UNDATED,
    Archive,
    UnsafeArchivePathError,
    content_hash,
    row_identity,
)

# Entities whose records are indexed one-per-record: the table, the index
# namespace, and how a source identity becomes an index key.
PER_RECORD: tuple[tuple[str, str, Callable[[str], str]], ...] = (
    ("Meetings", "meetings", str),
    ("Notes", "notes", str),
    ("CalendarEvents", "calendar", calendar_key),
)
# Entities archived as one snapshot, reconciled by row identity against the
# snapshot and its ledger together.
SNAPSHOT_ENTITIES = (("Dictionary", "dictionary"), ("Todos", "todos"))


@dataclass(slots=True)
class VerifyReport:
    """What verification found.

    Attributes:
        checked: Index entries examined.
        missing_files: Entries whose path does not exist on disk.
        unsafe_paths: Entries whose path resolves outside the archive.
        stale_hashes: Entries whose archived payload no longer matches.
        untracked: Meeting directories on disk with no index entry.
        unarchived: Source records the archive holds no file for. The one
            finding an archival tool must never miss.
        retained: Per table, records the archive holds that upstream no
            longer does. Never a fault: keeping them is the point.
        unflagged: Of those, how many sync has not yet flagged as gone --
            deleted upstream since the last run.
        unreconciled: Tables that could not be reconciled because their key
            column is gone upstream, which drift reports as breaking.
        tombstoned: Entries upstream has deleted, kept deliberately.
        unresolved_tokens: Speaker mentions that could not be resolved.
        corrupt: Bookkeeping files that are unreadable, or were set aside as
            ``*.corrupt-*`` by an earlier run. Each means facts the index held
            may be missing until someone restores or merges it.
    """

    checked: int = 0
    missing_files: list[str] = field(default_factory=list)
    unsafe_paths: list[str] = field(default_factory=list)
    stale_hashes: list[str] = field(default_factory=list)
    untracked: list[str] = field(default_factory=list)
    unarchived: list[str] = field(default_factory=list)
    retained: dict[str, int] = field(default_factory=dict)
    unflagged: int = 0
    unreconciled: list[str] = field(default_factory=list)
    tombstoned: int = 0
    unresolved_tokens: int = 0
    corrupt: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Report whether the archive is internally consistent.

        Tombstones are not a fault: a record upstream deleted is exactly what
        the archive exists to keep.

        Returns:
            ``True`` when nothing disagrees.
        """
        return not (
            self.missing_files
            or self.unsafe_paths
            or self.stale_hashes
            or self.untracked
            or self.unarchived
            or self.corrupt
        )

    def lines(self) -> list[str]:
        """Render the report for the terminal.

        Returns:
            One line per finding, plus a verdict.
        """
        out = [f"{self.checked} records checked"]
        for label, items in (
            ("missing from disk", self.missing_files),
            ("path outside the archive", self.unsafe_paths),
            ("payload digest mismatch", self.stale_hashes),
            ("on disk but not indexed", self.untracked),
            ("in the source but not archived", self.unarchived),
            ("unreadable or set-aside bookkeeping file", self.corrupt),
        ):
            if items:
                shown = ", ".join(items[:5])
                more = f" (+{len(items) - 5} more)" if len(items) > 5 else ""
                out.append(f"{len(items)} {label}: {shown}{more}")
        for table in self.unreconciled:
            out.append(f"{table}: not reconciled -- its key column is gone upstream")
        if self.tombstoned:
            out.append(f"{self.tombstoned} deleted upstream, kept here")
        if self.unflagged:
            # Said, not failed: the next sync records when they went.
            out.append(
                f"{self.unflagged} more gone upstream since the last sync, "
                "kept here; the next sync flags them"
            )
        if self.unresolved_tokens:
            out.append(f"{self.unresolved_tokens} unresolved speaker mentions")
        out.append("archive is consistent" if self.ok else "archive has problems")
        return out


def verify_archive(
    archive: Archive, source: SqliteSource | None = None, *, deep: bool = False
) -> VerifyReport:
    """Check the archive against itself and, when given, against the source.

    Args:
        archive: The archive to check.
        source: An open database reader. Internal consistency does not need
            one; reconciliation does.
        deep: Recompute each meeting's digest from its archived payload rather
            than trusting the index.

    Returns:
        What was found.
    """
    report = VerifyReport()
    report.corrupt = sorted(
        {*archive.unreadable}
        | {path.name for path in archive.root.glob("*.corrupt-*") if path.is_file()}
    )
    _check_entries(archive, report, deep=deep)
    _check_untracked(archive, report)
    if source is not None:
        _check_against_source(archive, source, report)
    return report


def _check_entries(archive: Archive, report: VerifyReport, *, deep: bool) -> None:
    """Check every index entry resolves to a file that is still there.

    Args:
        archive: The archive.
        report: Mutated with findings.
        deep: Also recompute archived meeting digests.
    """
    meetings_spec = EXPECTED["Meetings"]
    for entity, records in sorted(archive.index.get("entities", {}).items()):
        for key, entry in sorted(records.items()):
            if not isinstance(entry, dict):
                continue
            report.checked += 1
            if entry.get("upstream_state") == STATE_ABSENT:
                report.tombstoned += 1
            report.unresolved_tokens += int(
                entry.get("unresolved_speaker_tokens") or 0
            )

            raw_path = entry.get("path")
            if not isinstance(raw_path, str):
                continue
            try:
                # The stored path is untrusted input even though it is ours: a
                # hand-edited index must not send verification wandering
                # outside the archive.
                resolved = archive.resolve(*raw_path.split("/"))
            except UnsafeArchivePathError:
                report.unsafe_paths.append(f"{entity}/{key}")
                continue
            if not resolved.exists():
                report.missing_files.append(f"{entity}/{key}")
                continue

            if deep and entity == "meetings":
                payload = read_json(resolved / "raw" / "meeting.json", None)
                recorded = entry.get("content_hash")
                if isinstance(payload, dict) and isinstance(recorded, str):
                    if content_hash(meetings_spec, payload) != recorded:
                        report.stale_hashes.append(f"{entity}/{key}")


def _record_directories(meetings_root: Path) -> Iterator[Path]:
    """Yield the meeting directories under ``meetings/``, at either depth.

    Meetings are filed under ``YYYY/MM/<record>``, except those whose creation
    time could not be resolved, which are filed under ``undated/<record>`` --
    one level shallower, because inventing a date would invent provenance.

    A single ``*/*/*`` glob therefore reaches one level *past* every undated
    record and lands on its ``raw/`` directory, which the index has no reason
    to name. That made one undated meeting enough to report a healthy archive
    as broken, which is the same failure 0.3.1 fixed from the count side.

    Args:
        meetings_root: The ``meetings/`` directory.

    Yields:
        Each record directory.
    """
    for candidate in sorted(meetings_root.glob("*/*")):
        if not candidate.is_dir():
            continue
        if candidate.parent.name == UNDATED:
            yield candidate
        else:
            yield from (child for child in sorted(candidate.glob("*")) if child.is_dir())


def _check_untracked(archive: Archive, report: VerifyReport) -> None:
    """Find meeting directories the index does not know about.

    Args:
        archive: The archive.
        report: Mutated with findings.
    """
    meetings_root = archive.root / "meetings"
    if not meetings_root.is_dir():
        return
    indexed = {
        entry.get("path")
        for entry in archive.entries("meetings").values()
        if isinstance(entry, dict)
    }
    for candidate in _record_directories(meetings_root):
        if archive.relative(candidate) not in indexed:
            report.untracked.append(archive.relative(candidate))


def _check_against_source(
    archive: Archive, source: SqliteSource, report: VerifyReport
) -> None:
    """Reconcile the archive against what the database holds, record by record.

    By identity rather than by count. Counting compared a table's rows with
    the archive's entries, so every record upstream deleted was a mismatch
    until sync flagged it -- which, before 0.5.0, took a --full run -- and a
    healthy archive reported "problems". It also could not tell which record
    was missing, and treated an index entry with no file behind it as
    archived.

    Args:
        archive: The archive.
        source: An open database reader.
        report: Mutated with findings.
    """
    available = set(source.tables())

    for table, entity, index_key in PER_RECORD:
        if table not in available:
            continue
        live = source.keys(table)
        if live is None:
            report.unreconciled.append(table)
            continue
        wanted = {index_key(identity): identity for identity in live}
        entries = archive.entries(entity)
        archived = {
            key
            for key, entry in entries.items()
            if isinstance(entry, dict) and isinstance(entry.get("path"), str)
        }
        report.unarchived += [f"{table}/{key}" for key in sorted(set(wanted) - archived)]
        gone = set(entries) - set(wanted)
        if gone:
            report.retained[table] = len(gone)
            report.unflagged += sum(
                1
                for key in gone
                if isinstance(entries[key], dict)
                and entries[key].get("upstream_state") != STATE_ABSENT
            )

    for table, entity in SNAPSHOT_ENTITIES:
        if table not in available:
            continue
        live = source.keys(table)
        if live is None:
            report.unreconciled.append(table)
            continue
        spec = EXPECTED[table]
        snapshot = archive.existing_path(entity, entity)
        rows: list[dict[str, Any]] = []
        if snapshot is not None:
            rows, _ = read_ndjson(snapshot)
            rows += [
                entry["row"]
                for entry in read_ledger(ledger_path(snapshot))
                if isinstance(entry.get("row"), dict)
            ]
        held = {row_identity(spec, row) for row in rows}
        report.unarchived += [f"{table}/{key}" for key in sorted(set(live) - held)]
