"""Orchestration: turning a source into an archive, repeatably.

The property this module exists to hold is that **a second run with nothing
changed upstream writes zero bytes**. Everything else follows from it. An
archive that rewrote itself on every pass would churn mtimes, defeat
incremental backup, and make it impossible to tell a real edit from a re-read
by looking at the directory.

Three mechanisms enforce it, cheapest first. A record whose projected content
digest and artifact fingerprints are unchanged is skipped before anything is
read or rendered. Anything that is rendered goes through a compare-then-write,
so an unchanged byte string never reaches the disk. And an artifact whose size
and modification time match the recorded cursor is never re-hashed or
re-copied, which matters when the artifact is sixteen megabytes of Opus.

Durability is the other half. The index and sync state are written before any
exception leaves this module, on interrupt, and every ``checkpoint_every``
records -- because an interrupted run that loses its index has to redo work it
already did, and with a hundred thousand dictation rows in one pass that is not
a theoretical cost. A watermark advances only when its pass had no failures, so
a partial pass re-reads rather than silently skipping the records it missed.

The raw payload is always written before anything is rendered. A rendering bug
is then repaired offline from what is already on disk, with no source access at
all.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import files_source, render
from .files_source import MEETING_DIR_RE, MeetingArtifacts, read_transcript
from .normalize import (
    SpeakerMap,
    calendar_key,
    resolve_speaker_tokens,
    to_instant,
)
from .paths import WisprPaths
from .schema import EXPECTED, TimestampKind
from .secure_io import (
    copy_file_secure,
    file_digest,
    secure_mkdir,
    write_json_if_changed,
    write_ndjson_if_changed,
    write_text_if_changed,
)
from .sqlite_source import Record, SqliteSource
from .store import Archive, content_hash, entity_name

SOURCE_LOCAL = "wispr-local"

AUDIO_COPY = "copy"
AUDIO_LINK = "link"
AUDIO_SKIP = "skip"

# Every local pass, in the order a run walks them.
ENTITIES = ("meetings", "notes", "calendar", "dictionary", "todos")


@dataclass(slots=True)
class SyncOptions:
    """What one sync run was asked to do.

    Attributes:
        full: Ignore watermarks and re-check every record.
        audio: ``copy``, ``link`` or ``skip``.
        max_audio_mb: Refuse to copy a single file larger than this.
        include_screen_context: Widen the projection to screen captures.
        include_blobs: Read binary columns.
        verbose: Report per record.
        dry_run: Report what would be written and touch nothing.
        checkpoint_every: Records between index saves.
    """

    full: bool = False
    audio: str = AUDIO_COPY
    max_audio_mb: int = 512
    include_screen_context: bool = False
    include_blobs: bool = False
    verbose: bool = False
    dry_run: bool = False
    checkpoint_every: int = 50


@dataclass(slots=True)
class SyncCounts:
    """What one entity pass did.

    Attributes:
        scanned: Records read from the source.
        written: Records whose files changed.
        unchanged: Records skipped because nothing moved.
        relocated: Records whose directory was moved after a retitle.
        absent: Records flagged as gone from the source.
        failed: Records that raised.
        bytes_copied: Binary bytes written.
    """

    scanned: int = 0
    written: int = 0
    unchanged: int = 0
    relocated: int = 0
    absent: int = 0
    failed: int = 0
    bytes_copied: int = 0

    def line(self, entity: str) -> str:
        """Summarize this pass in one line.

        Args:
            entity: The entity name.

        Returns:
            A human-readable summary.
        """
        parts = [f"{self.scanned} scanned", f"{self.written} written"]
        if self.unchanged:
            parts.append(f"{self.unchanged} unchanged")
        if self.relocated:
            parts.append(f"{self.relocated} moved")
        if self.absent:
            parts.append(f"{self.absent} gone upstream")
        if self.failed:
            parts.append(f"{self.failed} FAILED")
        return f"{entity}: " + ", ".join(parts)


@dataclass(slots=True)
class SyncResult:
    """The outcome of a whole run.

    Attributes:
        counts: Per-entity counts.
        failures: ``(entity, key, message)`` for each record that raised.
        interrupted: Whether the run stopped early on a keyboard interrupt.
    """

    counts: dict[str, SyncCounts] = field(default_factory=dict)
    failures: list[tuple[str, str, str]] = field(default_factory=list)
    interrupted: bool = False

    @property
    def ok(self) -> bool:
        """Report whether every record was handled.

        Returns:
            ``True`` when nothing failed.
        """
        return not self.failures


def _now() -> str:
    """Return an ISO timestamp for index bookkeeping.

    Returns:
        The current UTC time.
    """
    return datetime.now(tz=UTC).isoformat()


def _artifact_fingerprint(path: Path | None) -> dict[str, Any] | None:
    """Describe an artifact cheaply enough to check on every run.

    Size and modification time are compared rather than a digest, so an
    unchanged sixteen-megabyte recording is never re-read merely to discover
    that it is unchanged.

    Args:
        path: The artifact, or ``None``.

    Returns:
        The fingerprint, or ``None`` when absent.
    """
    if path is None:
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _artifacts_changed(
    artifacts: MeetingArtifacts, cursor: dict[str, Any]
) -> bool:
    """Report whether any of a meeting's files moved since the last run.

    Args:
        artifacts: The discovered files.
        cursor: The recorded fingerprints.

    Returns:
        ``True`` when anything appeared, vanished or changed.
    """
    for name in files_source.ARTIFACT_NAMES:
        current = _artifact_fingerprint(getattr(artifacts, name))
        recorded = cursor.get(name)
        if current is None and recorded is None:
            continue
        if current is None or recorded is None:
            return True
        if (
            current["size"] != recorded.get("size")
            or current["mtime_ns"] != recorded.get("mtime_ns")
        ):
            return True
    return False


def _meeting_records(source: SqliteSource, since: Any) -> Iterator[Record]:
    """Read meeting rows, newest changes first if a watermark applies.

    Args:
        source: The open reader.
        since: Watermark value, or ``None`` for everything.

    Yields:
        Meeting records.
    """
    yield from source.records(
        "Meetings", since=since, since_column="modifiedAt"
    )


def sync_meetings(
    archive: Archive,
    source: SqliteSource,
    wispr: WisprPaths,
    options: SyncOptions,
) -> SyncCounts:
    """Archive meetings, their transcripts, summaries and audio.

    Args:
        archive: The destination archive.
        source: An open database reader.
        wispr: Resolved source paths, for the meetings directory.
        options: What this run was asked to do.

    Returns:
        What the pass did.

    Raises:
        KeyboardInterrupt: Re-raised after the index has been saved.
    """
    spec = EXPECTED["Meetings"]
    counts = SyncCounts()
    entity = "meetings"
    now = _now()

    by_id = {item.meeting_id: item for item in files_source.discover_meetings(wispr.meetings)}
    since = None if options.full else archive.watermark(SOURCE_LOCAL, entity)
    seen: list[str] = []
    failed = False

    try:
        for record in _meeting_records(source, since):
            counts.scanned += 1
            key = record.key
            # The id becomes a directory name, so it is validated before it is
            # ever joined to a path -- the slug never guarantees safety.
            if not MEETING_DIR_RE.match(key):
                counts.failed += 1
                failed = True
                continue
            seen.append(key)
            try:
                changed = _archive_meeting(
                    archive, record, by_id.get(key), spec, options, counts, now
                )
            except OSError as error:
                counts.failed += 1
                failed = True
                if options.verbose:
                    print(f"    {key}: FAILED {error}")
                continue
            if changed:
                counts.written += 1
            else:
                counts.unchanged += 1
            if options.verbose:
                state = "wrote" if changed else "unchanged"
                print(f"    {key} {state}")
            if (
                not options.dry_run
                and counts.scanned % options.checkpoint_every == 0
            ):
                archive.save()
    except KeyboardInterrupt:
        if not options.dry_run:
            archive.save()
        raise

    # Absence can only be established by a scan that looked at everything. A
    # watermarked pass has not seen the records it deliberately skipped.
    if options.full and not failed:
        counts.absent = len(archive.mark_absent(entity, seen, when=now))

    if not failed:
        archive.set_watermark(
            SOURCE_LOCAL, entity, "modifiedAt", source.max_value("Meetings", "modifiedAt")
        )
    return counts


def _archive_meeting(
    archive: Archive,
    record: Record,
    artifacts: MeetingArtifacts | None,
    spec: Any,
    options: SyncOptions,
    counts: SyncCounts,
    now: str,
) -> bool:
    """Write one meeting, skipping it entirely when nothing has moved.

    Args:
        archive: The destination archive.
        record: The meeting row.
        artifacts: Its transcript files, when the directory exists.
        spec: The ``Meetings`` declaration.
        options: What this run was asked to do.
        counts: Mutated with relocation and byte counts.
        now: ISO timestamp for the index.

    Returns:
        ``True`` when anything was written.
    """
    key = record.key
    data = record.data
    digest = content_hash(spec, data)
    cursor = archive.artifact_cursor(SOURCE_LOCAL, key)
    entry = archive.entry("meetings", key)

    created = to_instant(TimestampKind.SEQUELIZE, data.get("createdAt"))
    title = data.get("title") if isinstance(data.get("title"), str) else ""
    destination = archive.record_path(
        "Meetings", spec, key, when=created, title=title
    )

    artifacts_moved = artifacts is not None and _artifacts_changed(artifacts, cursor)
    up_to_date = (
        entry is not None
        and entry.get("content_hash") == digest
        and not artifacts_moved
        and not options.full
        and destination.is_dir()
        and archive.existing_path("meetings", key) == destination
    )
    archive.mark_seen("meetings", key, soft_deleted=record.soft_deleted, when=now)
    if up_to_date:
        return False

    if options.dry_run:
        return True

    if archive.relocate("meetings", key, destination):
        counts.relocated += 1

    wrote = _write_meeting_files(
        archive, destination, record, artifacts, options, counts
    )

    transcript_deleted = data.get("transcriptDeletedAt") is not None
    fields: dict[str, Any] = {
        "path": archive.relative(destination),
        "title": title or None,
        "created_at": created.isoformat() if created else None,
        "modified_at": data.get("modifiedAt"),
        "content_hash": digest,
        "artifacts": list(artifacts.present) if artifacts else [],
        "transcript_deleted_upstream": transcript_deleted or None,
        "source": SOURCE_LOCAL,
    }
    # Only set when something was actually written. put() treats None as
    # "remove this key", so passing it unconditionally made a --full pass
    # *erase* the archived_at of every record it re-verified but did not
    # rewrite -- which then churned index.json on a run that changed nothing.
    if wrote:
        fields["archived_at"] = now
    archive.put("meetings", key, **fields)
    if artifacts is not None:
        for name in files_source.ARTIFACT_NAMES:
            fingerprint = _artifact_fingerprint(getattr(artifacts, name))
            if fingerprint is None:
                cursor.pop(name, None)
            else:
                cursor[name] = fingerprint
    return wrote


def _write_meeting_files(
    archive: Archive,
    destination: Path,
    record: Record,
    artifacts: MeetingArtifacts | None,
    options: SyncOptions,
    counts: SyncCounts,
) -> bool:
    """Write one meeting's raw payloads, media and rendered documents.

    Raw first, always. A rendering bug is then repaired from what is already
    on disk rather than by re-reading a source that may since have deleted the
    transcript.

    Args:
        archive: The destination archive.
        destination: The meeting's directory.
        record: The meeting row.
        artifacts: Its transcript files.
        options: What this run was asked to do.
        counts: Mutated with byte counts.

    Returns:
        ``True`` when any file changed.
    """
    data = record.data
    raw_dir = destination / "raw"
    secure_mkdir(raw_dir)
    wrote = write_json_if_changed(raw_dir / "meeting.json", data)

    speakers = SpeakerMap.parse(data.get("speakerMap"))
    if speakers.raw is not None:
        wrote |= write_json_if_changed(raw_dir / "speaker_map.json", speakers.raw)

    # Verbatim copies, so a parser change never needs the source again.
    refined = read_transcript(artifacts.refined if artifacts else None)
    live = read_transcript(artifacts.live if artifacts else None)
    if artifacts is not None:
        for name, filename in (
            ("refined", "refined.ndjson"),
            ("live", "live.ndjson"),
            ("observations", "speakers.observations.ndjson"),
        ):
            path = getattr(artifacts, name)
            if path is None:
                continue
            wrote |= _copy_if_changed(path, raw_dir / filename, counts)

        if options.audio == AUDIO_COPY and artifacts.audio is not None:
            size = artifacts.size_of("audio")
            if size <= options.max_audio_mb * 1024 * 1024:
                wrote |= _copy_if_changed(
                    artifacts.audio, destination / "media" / "upload.ogg", counts
                )

    title = data.get("title") if isinstance(data.get("title"), str) else ""
    raw_summary = data.get("summary") if isinstance(data.get("summary"), str) else ""
    # Resolved once. The hub inlines this body and summary.md wraps it, and
    # rendering twice to recover the body from the document would couple the
    # two files through the exact shape of a Markdown heading.
    resolved_summary, unresolved = resolve_speaker_tokens(raw_summary, speakers)
    if raw_summary.strip():
        summary_text, _ = render.render_summary(
            raw_summary,
            speakers,
            title=title,
            meeting_id=record.key,
            heading="Summary",
        )
        wrote |= write_text_if_changed(destination / "summary.md", summary_text)

    notes = data.get("notes")
    if isinstance(notes, str) and notes.strip():
        notes_text, _ = render.render_summary(
            notes, speakers, title=title, meeting_id=record.key, heading="Notes"
        )
        wrote |= write_text_if_changed(destination / "notes.md", notes_text)

    if refined.turns:
        wrote |= write_text_if_changed(
            destination / "transcript.refined.md",
            render.render_transcript(
                refined.turns,
                title=title,
                meeting_id=record.key,
                kind="refined",
                speakers=speakers,
                malformed=refined.malformed,
                truncated=refined.truncated_tail,
            ),
        )
    if live.turns:
        wrote |= write_text_if_changed(
            destination / "transcript.live.md",
            render.render_transcript(
                live.turns,
                title=title,
                meeting_id=record.key,
                kind="live",
                malformed=live.malformed,
                truncated=live.truncated_tail,
            ),
        )

    participants = data.get("participantNames")
    participants = [
        name for name in participants if isinstance(name, str)
    ] if isinstance(participants, list) else []
    speaker_names = sorted({person.name for person in speakers.people.values()})

    wrote |= write_text_if_changed(
        destination / "meeting.md",
        render.render_meeting(
            data,
            meeting_id=record.key,
            title=title,
            created_at=to_instant(TimestampKind.SEQUELIZE, data.get("createdAt")),
            ended_at=to_instant(TimestampKind.EPOCH_MS, data.get("endedAt")),
            modified_at=to_instant(TimestampKind.SEQUELIZE, data.get("modifiedAt")),
            participants=participants,
            speaker_names=speaker_names,
            artifacts=list(artifacts.present) if artifacts else [],
            summary_resolved=resolved_summary,
            soft_deleted=record.soft_deleted,
            transcript_deleted_upstream=data.get("transcriptDeletedAt") is not None,
            unresolved_tokens=unresolved,
        ),
    )
    return wrote


def _copy_if_changed(src: Path, dest: Path, counts: SyncCounts) -> bool:
    """Copy a file only when the destination does not already match.

    Args:
        src: Source file.
        dest: Destination inside the archive.
        counts: Mutated with the bytes written.

    Returns:
        ``True`` when the file was copied.
    """
    try:
        if dest.exists() and dest.stat().st_size == src.stat().st_size:
            if file_digest(dest) == file_digest(src):
                return False
    except OSError:
        pass
    copy_file_secure(src, dest)
    counts.bytes_copied += dest.stat().st_size
    return True


def sync_local(
    archive: Archive,
    source: SqliteSource,
    wispr: WisprPaths,
    options: SyncOptions,
    entities: Sequence[str] = ENTITIES,
) -> SyncResult:
    """Run every requested entity pass against the local store.

    Args:
        archive: The destination archive.
        source: An open database reader.
        wispr: Resolved source paths.
        options: What this run was asked to do.
        entities: Which passes to run.

    Returns:
        The outcome.
    """
    result = SyncResult()
    try:
        if "meetings" in entities:
            result.counts["meetings"] = sync_meetings(
                archive, source, wispr, options
            )
        if "notes" in entities:
            result.counts["notes"] = sync_notes(archive, source, options)
        if "calendar" in entities:
            result.counts["calendar"] = sync_calendar(archive, source, options)
        if "dictionary" in entities:
            result.counts["dictionary"] = sync_snapshot(
                archive, source, "Dictionary", options, render_markdown=True
            )
        if "todos" in entities:
            result.counts["todos"] = sync_snapshot(
                archive, source, "Todos", options
            )
    except KeyboardInterrupt:
        result.interrupted = True
    finally:
        if not options.dry_run:
            archive.save()
    return result


def _document_paths(stem: Path, suffix: str) -> Path:
    """Return a sibling file for a document-layout record.

    ``Path.with_suffix`` is deliberately not used: a slug can end in something
    that looks like an extension, and replacing it would silently truncate the
    name.

    Args:
        stem: The record's path stem.
        suffix: Extension to append, including the dot.

    Returns:
        The file path.
    """
    return stem.parent / f"{stem.name}{suffix}"


def sync_notes(
    archive: Archive, source: SqliteSource, options: SyncOptions
) -> SyncCounts:
    """Archive scratchpad notes as Markdown beside their raw payloads.

    Args:
        archive: The destination archive.
        source: An open database reader.
        options: What this run was asked to do.

    Returns:
        What the pass did.
    """
    spec = EXPECTED["Notes"]
    counts = SyncCounts()
    now = _now()
    since = None if options.full else archive.watermark(SOURCE_LOCAL, "notes")
    seen: list[str] = []
    failed = False

    try:
        for record in source.records(
            "Notes", since=since, since_column="modifiedAt"
        ):
            counts.scanned += 1
            key = record.key
            if not MEETING_DIR_RE.match(key):
                counts.failed += 1
                failed = True
                continue
            seen.append(key)
            data = record.data
            created = to_instant(TimestampKind.SEQUELIZE, data.get("createdAt"))
            title = data.get("title") if isinstance(data.get("title"), str) else ""
            stem = archive.record_path("Notes", spec, key, when=created, title=title)
            digest = content_hash(spec, data)
            entry = archive.entry("notes", key)

            archive.mark_seen("notes", key, soft_deleted=record.soft_deleted, when=now)
            if (
                entry is not None
                and entry.get("content_hash") == digest
                and not options.full
                and archive.existing_path("notes", key) == stem
                and _document_paths(stem, ".md").is_file()
            ):
                counts.unchanged += 1
                continue
            if options.dry_run:
                counts.written += 1
                continue

            if archive.relocate("notes", key, stem):
                counts.relocated += 1

            wrote = write_json_if_changed(_document_paths(stem, ".raw.json"), data)
            wrote |= write_text_if_changed(
                _document_paths(stem, ".md"),
                render.render_note(
                    note_id=key,
                    title=title,
                    content=data.get("content") or "",
                    created_at=created,
                    modified_at=to_instant(
                        TimestampKind.SEQUELIZE, data.get("modifiedAt")
                    ),
                    pinned=bool(data.get("pinned")),
                    soft_deleted=record.soft_deleted,
                ),
            )
            fields: dict[str, Any] = {
                "path": archive.relative(stem),
                "title": title or None,
                "created_at": created.isoformat() if created else None,
                "content_hash": digest,
                "source": SOURCE_LOCAL,
            }
            if wrote:
                fields["archived_at"] = now
            archive.put("notes", key, **fields)
            counts.written += 1 if wrote else 0
            counts.unchanged += 0 if wrote else 1
    except KeyboardInterrupt:
        if not options.dry_run:
            archive.save()
        raise

    if options.full and not failed:
        counts.absent = len(archive.mark_absent("notes", seen, when=now))
    if not failed:
        archive.set_watermark(
            SOURCE_LOCAL, "notes", "modifiedAt", source.max_value("Notes", "modifiedAt")
        )
    return counts


def sync_calendar(
    archive: Archive, source: SqliteSource, options: SyncOptions
) -> SyncCounts:
    """Archive calendar events as JSON only.

    Deliberately no Markdown digest. Events mutate -- both events on the
    development machine had flipped to ``status = cancelled`` -- and a rendered
    digest would be rewritten on every sync, churning a file the operator may
    have open.

    Args:
        archive: The destination archive.
        source: An open database reader.
        options: What this run was asked to do.

    Returns:
        What the pass did.
    """
    spec = EXPECTED["CalendarEvents"]
    counts = SyncCounts()
    now = _now()
    since = None if options.full else archive.watermark(SOURCE_LOCAL, "calendar")
    seen: list[str] = []

    try:
        for record in source.records(
            "CalendarEvents", since=since, since_column="updatedAt"
        ):
            counts.scanned += 1
            data = record.data
            external_id = record.key
            # The primary key runs to 181 characters of base32 in practice, so
            # it cannot be a path component and truncating it is not
            # injective. A hash prefix is the only stable short name.
            key = calendar_key(external_id)
            seen.append(key)

            starts = to_instant(TimestampKind.EPOCH_MS, data.get("startAtUtc"))
            title = data.get("title") if isinstance(data.get("title"), str) else ""
            stem = archive.record_path(
                "CalendarEvents", spec, key, when=starts, title=title
            )
            destination = _document_paths(stem, ".json")
            digest = content_hash(spec, data)
            entry = archive.entry("calendar", key)

            archive.mark_seen(
                "calendar", key, soft_deleted=record.soft_deleted, when=now
            )
            if (
                entry is not None
                and entry.get("content_hash") == digest
                and not options.full
                and destination.is_file()
            ):
                counts.unchanged += 1
                continue
            if options.dry_run:
                counts.written += 1
                continue

            # The YYYY/MM shard derives from startAtUtc, which moves when an
            # event is rescheduled, so calendar records relocate too.
            previous = archive.existing_path("calendar", key)
            if previous is not None and previous != destination and previous.exists():
                secure_mkdir(destination.parent)
                previous.replace(destination)
                counts.relocated += 1

            wrote = write_json_if_changed(destination, data)
            fields: dict[str, Any] = {
                "path": archive.relative(destination),
                "external_id": external_id,
                "title": title or None,
                "starts_at": starts.isoformat() if starts else None,
                "status": data.get("status"),
                "content_hash": digest,
                "source": SOURCE_LOCAL,
            }
            if wrote:
                fields["archived_at"] = now
            archive.put("calendar", key, **fields)
            counts.written += 1 if wrote else 0
            counts.unchanged += 0 if wrote else 1
    except KeyboardInterrupt:
        if not options.dry_run:
            archive.save()
        raise

    if options.full:
        counts.absent = len(archive.mark_absent("calendar", seen, when=now))
    archive.set_watermark(
        SOURCE_LOCAL,
        "calendar",
        "updatedAt",
        source.max_value("CalendarEvents", "updatedAt"),
    )
    return counts


def sync_snapshot(
    archive: Archive,
    source: SqliteSource,
    table: str,
    options: SyncOptions,
    *, 
    render_markdown: bool = False,
) -> SyncCounts:
    """Archive a small mutable table as one NDJSON snapshot.

    Snapshot tables are indexed once for the whole table rather than once per
    row. The file already contains every row including the tombstoned ones, so
    per-row index entries would restate what the artifact says while making
    ``index.json`` grow with data that has no separate location.

    Args:
        archive: The destination archive.
        source: An open database reader.
        table: Source table name.
        options: What this run was asked to do.
        render_markdown: Also write a readable rendering.

    Returns:
        What the pass did.
    """
    spec = EXPECTED[table]
    entity = entity_name(table)
    counts = SyncCounts()
    now = _now()

    rows = sorted(
        (record.data for record in source.records(table)),
        key=lambda row: str(row.get(spec.pk, "")),
    )
    counts.scanned = len(rows)

    destination = archive.record_path(table, spec, "")
    digest = content_hash(spec, {"rows": [content_hash(spec, row) for row in rows]})
    entry = archive.entry(entity, entity)

    if (
        entry is not None
        and entry.get("content_hash") == digest
        and not options.full
        and destination.is_file()
    ):
        counts.unchanged = len(rows)
        return counts
    if options.dry_run:
        counts.written = len(rows)
        return counts

    wrote = write_ndjson_if_changed(destination, rows)
    if render_markdown and table == "Dictionary":
        wrote |= write_text_if_changed(
            destination.with_name("dictionary.md"), render.render_dictionary(rows)
        )

    fields: dict[str, Any] = {
        "path": archive.relative(destination),
        "records": len(rows),
        "deleted_records": sum(1 for row in rows if spec.is_soft_deleted(row)),
        "content_hash": digest,
        "source": SOURCE_LOCAL,
    }
    if wrote:
        fields["archived_at"] = now
    archive.put(entity, entity, **fields)
    counts.written = len(rows) if wrote else 0
    counts.unchanged = 0 if wrote else len(rows)
    return counts
