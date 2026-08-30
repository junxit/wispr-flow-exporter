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

from dataclasses import dataclass, field

from .schema import EXPECTED
from .secure_io import read_json
from .sqlite_source import SqliteSource
from .store import STATE_ABSENT, Archive, UnsafeArchivePathError, content_hash

# Entities whose records are indexed one-per-record, and the table each comes
# from. Snapshot entities are reconciled by count instead.
PER_RECORD = (("Meetings", "meetings"), ("Notes", "notes"))
BY_COUNT = (
    ("Meetings", "meetings"),
    ("Notes", "notes"),
    ("CalendarEvents", "calendar"),
)
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
        unarchived: Source records with no index entry.
        counts: Tables whose totals disagree, as
            ``(in_source, archived, retained)``. The comparison is against
            ``archived - retained``: a record the source dropped and the
            archive kept is not a discrepancy.
        tombstoned: Entries upstream has deleted, kept deliberately.
        unresolved_tokens: Speaker mentions that could not be resolved.
    """

    checked: int = 0
    missing_files: list[str] = field(default_factory=list)
    unsafe_paths: list[str] = field(default_factory=list)
    stale_hashes: list[str] = field(default_factory=list)
    untracked: list[str] = field(default_factory=list)
    unarchived: list[str] = field(default_factory=list)
    counts: dict[str, tuple[int, int, int]] = field(default_factory=dict)
    tombstoned: int = 0
    unresolved_tokens: int = 0

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
            or self.counts
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
        ):
            if items:
                shown = ", ".join(items[:5])
                more = f" (+{len(items) - 5} more)" if len(items) > 5 else ""
                out.append(f"{len(items)} {label}: {shown}{more}")
        for table, (in_source, archived, retained) in sorted(self.counts.items()):
            line = f"{table}: {in_source} in source, {archived} archived"
            if retained:
                # Say what was already accounted for, or the numbers look like
                # they simply fail to add up.
                line += f" ({retained} kept after upstream deletion)"
            out.append(line)
        if self.tombstoned:
            out.append(f"{self.tombstoned} deleted upstream, kept here")
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
    for candidate in sorted(meetings_root.glob("*/*/*")):
        if candidate.is_dir() and archive.relative(candidate) not in indexed:
            report.untracked.append(archive.relative(candidate))


def _retained(archive: Archive, entity: str) -> int:
    """Count entries kept although the source no longer holds them.

    Args:
        archive: The archive.
        entity: Archive directory name.

    Returns:
        How many entries are flagged as gone upstream. Uses ``entries``, which
        is deliberately non-mutating, so counting an empty entity does not
        create it in the index.
    """
    return sum(
        1
        for entry in archive.entries(entity).values()
        if isinstance(entry, dict) and entry.get("upstream_state") == STATE_ABSENT
    )


def _check_against_source(
    archive: Archive, source: SqliteSource, report: VerifyReport
) -> None:
    """Reconcile the archive against what the database still holds.

    Args:
        archive: The archive.
        source: An open database reader.
        report: Mutated with findings.
    """
    available = set(source.tables())

    for table, entity in PER_RECORD:
        if table not in available:
            continue
        indexed = set(archive.entries(entity))
        for record in source.records(table):
            if record.key and record.key not in indexed:
                report.unarchived.append(f"{table}/{record.key}")

    for table, entity in BY_COUNT:
        if table not in available:
            continue
        in_source = source.row_count(table)
        archived = len(archive.entries(entity))
        # Records the source has dropped and the archive deliberately keeps do
        # not belong in an equality check. Without this, the first time
        # anything is deleted upstream the archive reports "problems" and exits
        # non-zero forever, for doing exactly what this tool promises -- and an
        # operator who sees that every run stops reading it.
        retained = _retained(archive, entity)
        if in_source != archived - retained:
            report.counts[table] = (in_source, archived, retained)

    for table, entity in SNAPSHOT_ENTITIES:
        if table not in available:
            continue
        entry = archive.entry(entity, entity)
        archived = int(entry.get("records", 0)) if entry else 0
        in_source = source.row_count(table)
        if in_source != archived:
            report.counts[table] = (in_source, archived, 0)
