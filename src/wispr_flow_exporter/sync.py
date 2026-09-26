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

from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

from . import files_source, render
from .files_source import MEETING_DIR_RE, MeetingArtifacts, read_transcript
from .local_config import LocalConfig, Policy, SessionInfo, account_profile
from .normalize import (
    SpeakerMap,
    TimestampKind,
    calendar_key,
    resolve_dictation_text,
    resolve_speaker_tokens,
    to_instant,
)
from .paths import WisprPaths
from .retention import (
    key_digest,
    ledger_path,
    lost_content,
    project_record,
    read_ledger,
    record_removals,
    replace_payload,
    still_removed,
)
from .schema import EXPECTED, Layout, TableSpec
from .secure_io import (
    copy_file_secure,
    file_digest,
    read_json,
    read_ndjson,
    secure_mkdir,
    write_bytes_if_changed,
    write_json_if_changed,
    write_ndjson_if_changed,
    write_text_if_changed,
)
from .sqlite_source import Record, SqliteSource
from .store import (
    STATE_ABSENT,
    UNDATED,
    Archive,
    content_hash,
    entity_name,
    row_identity,
    row_order,
    rows_hash,
)

SOURCE_LOCAL = "wispr-local"

AUDIO_COPY = "copy"
AUDIO_LINK = "link"
AUDIO_SKIP = "skip"

# Every local pass, in the order a run walks them.
# The sibling files that together make up one archived note.
NOTE_SUFFIXES = (".md", ".raw.json")

ENTITIES = (
    "meetings",
    "notes",
    "calendar",
    "dictionary",
    "todos",
    "dictation",
    "tables",
    "account",
)


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
        recheck_days: Trailing days re-read for tables with no modification
            column, so an in-place edit is not missed forever.
        checkpoint_every: Records between index saves.
        drift_blocks_rendering: Breaking drift is in force, so an existing
            rendering must not be replaced by one built from a schema this
            tool no longer understands. See :func:`_write_markdown`.
    """

    full: bool = False
    audio: str = AUDIO_COPY
    max_audio_mb: int = 512
    include_screen_context: bool = False
    include_blobs: bool = False
    verbose: bool = False
    dry_run: bool = False
    recheck_days: int = 14
    checkpoint_every: int = 50
    drift_blocks_rendering: bool = False


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


def _needs_reading(
    archive: Archive,
    entity: str,
    live: Mapping[str, Any] | None,
    *,
    index_key: Callable[[str], str] = str,
    readable: Callable[[str], object] = bool,
) -> set[str]:
    """Name the live records a watermarked read would skip but this run must visit.

    A watermark only returns rows modified since the last run. Three kinds of
    record can hide behind it: one the index never recorded a file for -- a
    dry run before 0.4.1 left exactly that behind, with the watermark already
    past it -- one flagged as gone upstream that has since come back, and one
    missing from the index altogether.

    Args:
        archive: The archive.
        entity: The index namespace.
        live: Every identity upstream holds, from a key-only scan, or ``None``
            when the scan could not see the key.
        index_key: Maps a source identity to its index key.
        readable: Whether an identity can be archived at all; an id that
            fails validation is never fetched just to fail again.

    Returns:
        Source identities to read by key.
    """
    if live is None:
        return set()
    entries = archive.entries(entity)
    wanted: set[str] = set()
    for identity in live:
        if not readable(identity):
            continue
        entry = entries.get(index_key(identity))
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("path"), str)
            or entry.get("upstream_state") == STATE_ABSENT
        ):
            wanted.add(identity)
    return wanted


def _and_by_key(
    records: Iterable[Record], source: SqliteSource, table: str, keys: set[str]
) -> Iterator[Record]:
    """Yield a watermarked read, then any wanted record it did not include.

    Args:
        records: The watermarked read.
        source: The open reader.
        table: Table name.
        keys: Identities that must be visited this run.

    Yields:
        Every record to process, each once.
    """
    seen: set[str] = set()
    for record in records:
        seen.add(record.key)
        yield record
    if remaining := keys - seen:
        yield from source.records(table, keys=remaining)


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
    live = source.keys("Meetings")
    wanted = _needs_reading(archive, entity, live, readable=MEETING_DIR_RE.match)
    failed = False

    try:
        for record in _and_by_key(_meeting_records(source, since), source, "Meetings", wanted):
            counts.scanned += 1
            key = record.key
            # The id becomes a directory name, so it is validated before it is
            # ever joined to a path -- the slug never guarantees safety.
            if not MEETING_DIR_RE.match(key):
                counts.failed += 1
                failed = True
                continue
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

    # Absence is established against a key-only scan of the whole table, on
    # every run. It used to wait for a --full pass, because a watermarked read
    # has not seen what it skipped -- so verify reported a healthy archive as
    # broken from an upstream deletion until someone happened to run --full.
    if live is not None:
        counts.absent = len(archive.mark_absent(entity, live, when=now))

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
    title = _text(data.get("title"))
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
    wrote = replace_payload(
        archive,
        raw_dir / "meeting.json",
        data,
        entity="meetings",
        key=record.key,
        when=_now(),
        project=partial(project_record, EXPECTED["Meetings"]),
    )

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

        if artifacts.audio is not None and options.audio != AUDIO_SKIP:
            size = artifacts.size_of("audio")
            too_large = size > options.max_audio_mb * 1024 * 1024
            if options.audio == AUDIO_COPY and not too_large:
                wrote |= _copy_if_changed(
                    artifacts.audio, destination / "media" / "upload.ogg", counts
                )
            else:
                # link mode, or a file over the cap. The archive records that
                # the recording existed, where it was and what it hashed to,
                # so a later run or a human can still find it -- while being
                # honest that this archive does not contain it. Wispr Flow
                # garbage-collects meeting audio, so that pointer may already
                # be dangling, which is exactly why copy is the default.
                wrote |= write_json_if_changed(
                    raw_dir / "audio.json",
                    {
                        "archived": False,
                        "reason": "over max_audio_mb" if too_large else "link mode",
                        "source_path": str(artifacts.audio),
                        "bytes": size,
                        "sha256": file_digest(artifacts.audio),
                    },
                )

    title = _text(data.get("title"))
    raw_summary = _text(data.get("summary"))
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
        wrote |= _write_markdown(destination / "summary.md", summary_text, options)

    notes = data.get("notes")
    if isinstance(notes, str) and notes.strip():
        notes_text, _ = render.render_summary(
            notes, speakers, title=title, meeting_id=record.key, heading="Notes"
        )
        wrote |= _write_markdown(destination / "notes.md", notes_text, options)

    if refined.turns:
        wrote |= _write_markdown(
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
            options,
        )
    if live.turns:
        wrote |= _write_markdown(
            destination / "transcript.live.md",
            render.render_transcript(
                live.turns,
                title=title,
                meeting_id=record.key,
                kind="live",
                malformed=live.malformed,
                truncated=live.truncated_tail,
            ),
            options,
        )

    participants = data.get("participantNames")
    participants = [
        name for name in participants if isinstance(name, str)
    ] if isinstance(participants, list) else []
    speaker_names = sorted({person.name for person in speakers.people.values()})

    wrote |= _write_markdown(
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
        options,
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
    policy: Policy | None = None,
    config: LocalConfig | None = None,
    session: SessionInfo | None = None,
) -> SyncResult:
    """Run every requested entity pass against the local store.

    Args:
        archive: The destination archive.
        source: An open database reader.
        wispr: Resolved source paths.
        options: What this run was asked to do.
        entities: Which passes to run.
        policy: The observed storage preferences, recorded alongside the
            dictation pass so an archive empty by preference can prove it.
        config: Parsed config.json, for the account pass.
        session: The session summary, for the account pass. Carries no token.

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
        if "dictation" in entities:
            result.counts["dictation"] = sync_dictation(
                archive,
                source,
                options,
                policy or Policy(None, None, datetime.now(tz=UTC)),
            )
        if "tables" in entities:
            result.counts["tables"] = sync_tables(archive, source, options)
        if "account" in entities and config is not None:
            result.counts["account"] = sync_account(
                archive, config, session or SessionInfo(present=False), options
            )
    except KeyboardInterrupt:
        result.interrupted = True
    finally:
        if not options.dry_run:
            archive.save()
    return result


def _text(value: Any) -> str:
    """Return ``value`` when it is a string, and an empty string otherwise.

    Titles and summaries arrive from columns that are nominally TEXT and
    nominally NOT NULL, and neither is a guarantee this tool can rely on -- a
    renderer handed ``None`` would fail on a record the raw path archived
    perfectly well.

    Args:
        value: A column value.

    Returns:
        The string, or ``""``.
    """
    return value if isinstance(value, str) else ""


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


def _write_markdown(path: Path, text: str, options: SyncOptions) -> bool:
    """Write a rendering, unless doing so would degrade one already on disk.

    The raw path is schema-driven and survives anything; renderers are not.
    They read declared columns by name and are written defensively, with
    ``.get`` and ``isinstance`` guards throughout -- which means that when a
    required column disappears upstream they do not crash, they quietly render
    less. A note whose ``content`` column is gone renders as an empty note, and
    writing that over the good copy from yesterday destroys the only readable
    form of it. The raw JSON is still correct, so nothing is unrecoverable, but
    "recoverable" is not the same as "not broken".

    So the gate is narrow on purpose: only a rendering that *already exists* is
    protected. A record archived for the first time during breaking drift gets
    its degraded rendering, because the alternative is an index entry pointing
    at a file that was never written.

    Renderings held back this way stay stale until the declaration is updated
    and ``wispr-export render`` is run; the sync pass says so rather than
    leaving that to be discovered.

    Args:
        path: Destination document.
        text: Rendered Markdown.
        options: This run's options.

    Returns:
        Whether the file changed.
    """
    if options.drift_blocks_rendering and path.exists():
        return False
    return write_text_if_changed(path, text)


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
    live = source.keys("Notes")
    wanted = _needs_reading(archive, "notes", live, readable=MEETING_DIR_RE.match)
    failed = False

    try:
        for record in _and_by_key(
            source.records("Notes", since=since, since_column="modifiedAt"),
            source,
            "Notes",
            wanted,
        ):
            counts.scanned += 1
            key = record.key
            if not MEETING_DIR_RE.match(key):
                counts.failed += 1
                failed = True
                continue
            data = record.data
            created = to_instant(TimestampKind.SEQUELIZE, data.get("createdAt"))
            title = _text(data.get("title"))
            stem = archive.record_path("Notes", spec, key, when=created, title=title)
            digest = content_hash(spec, data)
            entry = archive.entry("notes", key)

            archive.mark_seen("notes", key, soft_deleted=record.soft_deleted, when=now)
            if (
                entry is not None
                and entry.get("content_hash") == digest
                and not options.full
                and archive.existing_path("notes", key)
                == _document_paths(stem, ".md")
            ):
                counts.unchanged += 1
                continue
            if options.dry_run:
                counts.written += 1
                continue

            if archive.relocate_document("notes", key, stem, NOTE_SUFFIXES):
                counts.relocated += 1

            wrote = replace_payload(
                archive,
                _document_paths(stem, ".raw.json"),
                data,
                entity="notes",
                key=key,
                when=now,
                project=partial(project_record, spec),
            )
            wrote |= _write_markdown(
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
                options,
            )
            fields: dict[str, Any] = {
                # The .md file, not the bare stem: an index path has to point
                # at something that exists, or verification cannot check it and
                # relocation cannot find it.
                "path": archive.relative(_document_paths(stem, ".md")),
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

    if live is not None:
        counts.absent = len(archive.mark_absent("notes", live, when=now))
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
    live = source.keys("CalendarEvents")
    wanted = _needs_reading(archive, "calendar", live, index_key=calendar_key)

    try:
        for record in _and_by_key(
            source.records("CalendarEvents", since=since, since_column="updatedAt"),
            source,
            "CalendarEvents",
            wanted,
        ):
            counts.scanned += 1
            data = record.data
            external_id = record.key
            # The primary key runs to 181 characters of base32 in practice, so
            # it cannot be a path component and truncating it is not
            # injective. A hash prefix is the only stable short name.
            key = calendar_key(external_id)

            starts = to_instant(TimestampKind.EPOCH_MS, data.get("startAtUtc"))
            title = _text(data.get("title"))
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
            if archive.relocate_file("calendar", key, destination):
                counts.relocated += 1

            wrote = replace_payload(
                archive,
                destination,
                data,
                entity="calendar",
                key=key,
                when=now,
                project=partial(project_record, spec),
            )
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

    if live is not None:
        present = {calendar_key(external_id) for external_id in live}
        counts.absent = len(archive.mark_absent("calendar", present, when=now))
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

    The file mirrors upstream exactly, which is also why a row Wispr Flow
    hard-deletes used to vanish from it on the next rewrite. Before the file
    is replaced, rows the old version held and upstream no longer does go to
    the ledger beside it (see :mod:`.retention`); a table emptied upstream
    becomes an empty file and a full ledger, never an empty archive.

    Args:
        archive: The destination archive.
        source: An open database reader.
        table: Source table name.
        options: What this run was asked to do.
        render_markdown: Also write a readable rendering.

    Returns:
        What the pass did.
    """
    spec = source.spec_for(table)
    entity = entity_name(table)
    counts = SyncCounts()
    now = _now()

    rows = sorted(
        (record.data for record in source.records(table)),
        key=lambda row: row_order(spec, row),
    )
    counts.scanned = len(rows)

    destination = archive.record_path(table, spec, "")
    digest = rows_hash(spec, rows)
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

    current = {row_identity(spec, row): row for row in rows}
    present = set(current)
    ledger = ledger_path(destination)
    counts.absent = _keep_removed(destination, ledger, spec, current, now)
    wrote = counts.absent > 0
    wrote |= write_ndjson_if_changed(destination, rows)
    removed = still_removed(read_ledger(ledger), spec, present)
    if render_markdown and table == "Dictionary":
        wrote |= _write_markdown(
            destination.with_name("dictionary.md"),
            render.render_dictionary(rows, removed),
            options,
        )

    fields: dict[str, Any] = {
        "path": archive.relative(destination),
        "records": len(rows),
        "deleted_records": sum(1 for row in rows if spec.is_soft_deleted(row)),
        "removed_records": len(removed) or None,
        "content_hash": digest,
        "source": SOURCE_LOCAL,
    }
    if wrote:
        fields["archived_at"] = now
    archive.put(entity, entity, **fields)
    counts.written = len(rows) if wrote else 0
    counts.unchanged = 0 if wrote else len(rows)
    return counts


def _keep_removed(
    main: Path,
    ledger: Path,
    spec: TableSpec,
    current: Mapping[str, Mapping[str, Any]],
    when: str,
) -> int:
    """Move what a file about to be rewritten would lose into its ledger.

    Rows upstream dropped are recorded as removed; rows upstream still has but
    whose new version holds less (see :func:`.retention.lost_content`) have
    their old version recorded as superseded. Called immediately before the
    main file is replaced, so the ledger is always written first: there is no
    instant at which either kind of row is in neither file.

    Args:
        main: The snapshot or shard about to be rewritten.
        ledger: Its ledger.
        spec: The table's declaration.
        current: Every row upstream holds now, by identity -- across the whole
            table, so a row that moved to another shard is not mistaken for
            removed.
        when: ISO timestamp for the ledger.

    Returns:
        How many rows were newly recorded as removed.
    """
    old_rows, unparsed = read_ndjson(main)
    gone: list[dict[str, Any]] = []
    superseded: list[dict[str, Any]] = []
    for row in old_rows:
        new = current.get(row_identity(spec, row))
        if new is None:
            gone.append(row)
        elif lost_content(project_record(spec, row), project_record(spec, new)):
            superseded.append(row)
    removed = record_removals(ledger, spec, gone, unparsed, when=when)
    record_removals(ledger, spec, (), when=when, superseded=superseded)
    return removed


def _day_start(day: str) -> datetime | None:
    """Return the instant a ``YYYY-MM-DD`` shard name stands for.

    A day whose rows have all gone upstream still has a shard on disk, and its
    path has to come from the name rather than from a row that no longer
    exists.

    Args:
        day: A shard's day, or ``"undated"``.

    Returns:
        Midnight UTC of that day, or ``None`` for the undated shard.
    """
    try:
        return datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return None


def _day_of(value: Any) -> str | None:
    """Return the ``YYYY-MM-DD`` a Sequelize timestamp falls on.

    Args:
        value: A raw ``timestamp`` column value.

    Returns:
        The date, or ``None`` when it did not parse.
    """
    when = to_instant(TimestampKind.SEQUELIZE, value)
    return f"{when:%Y-%m-%d}" if when else None


def _recheck_floor(watermark: Any, days: int) -> Any:
    """Compute how far back a run re-reads for in-place edits.

    ``History`` rows are edited after creation -- the text cascade fills in
    later -- and the table has no modification column at all. A pure watermark
    would therefore archive the first version of a dictation and never see the
    correction, so a trailing window is re-read every run.

    Args:
        watermark: The highest timestamp archived so far.
        days: How many days to reach back.

    Returns:
        The lower bound to query from, or ``None`` for everything.
    """
    when = to_instant(TimestampKind.SEQUELIZE, watermark)
    if when is None:
        return None
    # Formatted to match the column's own encoding, since the comparison
    # happens in SQL against the stored text.
    return f"{when - timedelta(days=days):%Y-%m-%d} 00:00:00.000 +00:00"


def sync_dictation(
    archive: Archive,
    source: SqliteSource,
    options: SyncOptions,
    policy: Policy,
) -> SyncCounts:
    """Archive dictation history as one NDJSON and one log per day.

    A document per dictation would be unusable: a heavy user produces
    thousands in a day, and the useful unit of dictation history is the day.

    Rows are read in two ways. A trailing window is re-read in full every run,
    because History rows are edited in place after creation and carry no
    modification time. And every row's identity and timestamp are read every
    run, which is cheap and is the only way to know that a day *outside* the
    window lost a row -- or gained one, or holds one with no timestamp at all,
    which no windowed query can ever match. A day is rewritten when it was
    re-read or when its membership moved; rows it keeps that the window did
    not re-read are carried forward from the archived shard, and rows
    upstream deleted go to the shard's ledger instead of vanishing.

    Before this, a whole day was rewritten from just the rows the window read.
    A hard-deleted dictation vanished from the archive, a row stamped exactly
    on the window's midnight boundary was dropped (the query is ``>``), and
    ``sync --full`` rewrote every day from scratch -- the most destructive run
    there was.

    When ``localDataPolicy`` is ``never_store`` this pass legitimately finds
    nothing, and that is recorded rather than inferred. An archive that is
    empty because of a preference must be able to prove which preference: it is
    the one failure here that is silent, permanent, and only discovered on the
    day the data is finally wanted.

    Args:
        archive: The destination archive.
        source: An open database reader.
        options: What this run was asked to do.
        policy: The observed Wispr Flow storage preferences.

    Returns:
        What the pass did.
    """
    spec = EXPECTED["History"]
    counts = SyncCounts()
    now = _now()

    since = (
        None
        if options.full
        else _recheck_floor(
            archive.watermark(SOURCE_LOCAL, "dictation"), options.recheck_days
        )
    )

    read: dict[str, dict[str, Any]] = {}
    blobs: list[tuple[str, str, str, bytes]] = []
    highest: Any = None

    try:
        for record in source.records(
            "History",
            since=since,
            since_column="timestamp",
            include_screen_context=options.include_screen_context,
            include_blobs=options.include_blobs,
        ):
            counts.scanned += 1
            read[row_identity(spec, record.data)] = record.data
            raw = record.data.get("timestamp")
            if isinstance(raw, str) and (highest is None or raw > highest):
                highest = raw
            day = _day_of(raw) or UNDATED
            for column, payload in record.blobs.items():
                blobs.append((day, record.key, column, payload))
    except KeyboardInterrupt:
        if not options.dry_run:
            archive.save()
        raise

    if options.dry_run:
        counts.written = counts.scanned
        return counts

    live = source.keys("History", also="timestamp")
    archived = archive.entries("dictation")
    members: dict[str, set[str]] = {}
    if live is None:
        # The key column is gone -- breaking drift, already reported. Without
        # it nothing can be proven absent, so only the re-read days are
        # rewritten and every row they held is kept.
        for identity, row in read.items():
            members.setdefault(_day_of(row.get("timestamp")) or UNDATED, set()).add(identity)
        touched = set(members)
    else:
        for identity, stamp in live.items():
            members.setdefault(_day_of(stamp) or UNDATED, set()).add(identity)
        touched = {_day_of(row.get("timestamp")) or UNDATED for row in read.values()}
        for day in set(members) | set(archived):
            if _membership_moved(archived.get(day), members.get(day, set())):
                touched.add(day)

    shards: dict[str, Path] = {}
    carried: dict[str, dict[str, dict[str, Any]]] = {}
    unparsed: dict[str, list[str]] = {}
    for day in touched:
        shards[day] = archive.record_path("History", spec, "", when=_day_start(day))
        old_rows, unparsed[day] = read_ndjson(shards[day])
        carried[day] = {row_identity(spec, row): row for row in old_rows}

    # Rows upstream holds for a day being rewritten that neither the window
    # nor the archive has: a backfilled old dictation, one on the window's
    # boundary, one with no timestamp. Fetched by key, blobs and all.
    missing = {
        identity
        for day in touched
        for identity in members.get(day, set())
        if identity not in read and identity not in carried[day]
    }
    if missing:
        for record in source.records(
            "History",
            keys=missing,
            include_screen_context=options.include_screen_context,
            include_blobs=options.include_blobs,
        ):
            read[row_identity(spec, record.data)] = record.data
            day = _day_of(record.data.get("timestamp")) or UNDATED
            for column, payload in record.blobs.items():
                blobs.append((day, record.key, column, payload))

    everywhere = set(live) if live is not None else None
    for day in sorted(touched):
        ids = members.get(day, set())
        if live is None:
            ids = ids | set(carried[day])
        rows = [
            found
            for identity in ids
            if (found := read.get(identity) or carried[day].get(identity)) is not None
        ]
        rows.sort(key=lambda row: (str(row.get("timestamp", "")), row_identity(spec, row)))
        shard = shards[day]
        ledger = ledger_path(shard)
        removed: list[tuple[dict[str, Any], str]] = []
        appended = 0
        if everywhere is not None:
            gone = [row for key, row in carried[day].items() if key not in everywhere]
            appended = record_removals(ledger, spec, gone, unparsed[day], when=now)
            # A re-read dictation whose new version holds less -- text the
            # cascade had, cleared -- keeps its old version in the ledger.
            superseded = [
                old
                for key, old in carried[day].items()
                if key in read
                and lost_content(project_record(spec, old), project_record(spec, read[key]))
            ]
            record_removals(ledger, spec, (), when=now, superseded=superseded)
            removed = still_removed(read_ledger(ledger), spec, everywhere)
        counts.absent += appended

        wrote = appended > 0
        wrote |= write_ndjson_if_changed(shard, rows)
        wrote |= _write_markdown(
            shard.with_suffix(".md"),
            render.render_dictation_day(day, _day_entries(spec, rows, removed)),
            options,
        )

        fields: dict[str, Any] = {
            "path": archive.relative(shard),
            "records": len(rows),
            "content_hash": rows_hash(spec, rows),
            "key_digest": key_digest(ids) if live is not None else None,
            "removed_records": len(removed) or None,
            "source": SOURCE_LOCAL,
        }
        if wrote:
            fields["archived_at"] = now
            counts.written += len(rows)
        else:
            counts.unchanged += len(rows)
        archive.put("dictation", day, **fields)

    for day, key, column, payload in blobs:
        suffix = {"audio": ".opus", "builtInAudio": ".opus", "screenshot": ".png"}
        target = archive.resolve(
            "dictation", "media", *day.split("-")[:2], key,
            f"{column}{suffix.get(column, '.bin')}",
        )
        if write_bytes_if_changed(target, payload):
            counts.bytes_copied += len(payload)

    archive.source_state(SOURCE_LOCAL)["policy"] = policy.as_dict(
        archive.source_state(SOURCE_LOCAL).get("policy")
    )
    if highest is not None:
        archive.set_watermark(SOURCE_LOCAL, "dictation", "timestamp", highest)
    return counts


def _membership_moved(entry: Any, ids: set[str]) -> bool:
    """Report whether a day's rows upstream differ from what was archived.

    Args:
        entry: The day's index entry, if it has one.
        ids: Identities upstream holds for that day now.

    Returns:
        ``True`` when the day must be rewritten: it was never archived, or the
        set of rows it holds moved. Entries written before identities were
        recorded fall back to comparing the count, once, until rewritten.
    """
    if not isinstance(entry, dict):
        return bool(ids)
    recorded = entry.get("key_digest")
    if isinstance(recorded, str):
        return recorded != key_digest(ids)
    return entry.get("records") != len(ids)


def _day_entries(
    spec: TableSpec,
    rows: Sequence[dict[str, Any]],
    removed: Sequence[tuple[dict[str, Any], str]],
) -> list[dict[str, Any]]:
    """Build a day log's entries, removed dictations in their original place.

    Args:
        spec: The History declaration.
        rows: The day's rows upstream holds now.
        removed: ``(row, missing_since)`` for the day's rows upstream deleted.

    Returns:
        One entry per dictation, in the order it was spoken.
    """
    combined = [(row, "") for row in rows] + [
        (row, since[:10]) for row, since in removed
    ]
    combined.sort(
        key=lambda item: (str(item[0].get("timestamp", "")), row_identity(spec, item[0]))
    )
    entries = []
    for row, removed_on in combined:
        text, provenance = resolve_dictation_text(row)
        stamp = to_instant(TimestampKind.SEQUELIZE, row.get("timestamp"))
        entry: dict[str, Any] = {
            "when": f"{stamp:%H:%M}" if stamp else "",
            "app": row.get("app"),
            "text": text,
            "words": row.get("numWords"),
            "provenance": provenance,
        }
        if removed_on:
            entry["removed_on"] = removed_on
        entries.append(entry)
    return entries


# Tables with a dedicated pass. Everything else is archived generically, which
# is what makes a table shipped in a future migration cost no code change.
HANDLED_TABLES = frozenset(
    {"Meetings", "Notes", "CalendarEvents", "Dictionary", "Todos", "History"}
)


def sync_sharded(
    archive: Archive, source: SqliteSource, table: str, options: SyncOptions
) -> SyncCounts:
    """Archive an append-mostly table as date-sharded NDJSON.

    Every row is read every run, so the set of rows upstream holds is known
    exactly. Days that held rows before are revisited even when no row now
    lands on them, so a day emptied upstream keeps its rows -- in its ledger --
    instead of keeping a stale file nobody checks. Rows within a day are
    ordered by their date column and then identity, never by scan order.

    Args:
        archive: The destination archive.
        source: An open database reader.
        table: Source table name.
        options: What this run was asked to do.

    Returns:
        What the pass did.
    """
    spec = EXPECTED[table]
    entity = entity_name(table)
    counts = SyncCounts()
    now = _now()
    date_column = spec.date_column
    kind = spec.timestamps.get(date_column or "", TimestampKind.SEQUELIZE)

    def instant(row: Mapping[str, Any]) -> datetime | None:
        return to_instant(kind, row.get(date_column)) if date_column else None

    days: dict[str, list[dict[str, Any]]] = {}
    for record in source.records(
        table,
        include_screen_context=options.include_screen_context,
        include_blobs=options.include_blobs,
    ):
        counts.scanned += 1
        when = instant(record.data)
        days.setdefault(f"{when:%Y-%m-%d}" if when else UNDATED, []).append(
            record.data
        )

    if options.dry_run:
        counts.written = counts.scanned
        return counts

    if not days:
        # A sharded table with no rows produces no shard, so without this the
        # archive could not distinguish "read, and empty" from "never read".
        archive.put(
            "tables",
            f"{entity}:empty",
            table=table,
            records=0,
            content_hash=rows_hash(spec, []),
            source=SOURCE_LOCAL,
        )

    prefix = f"{entity}:"
    archived_days = {
        key.removeprefix(prefix)
        for key in archive.entries("tables")
        if key.startswith(prefix) and key != f"{entity}:empty"
    }
    current = {row_identity(spec, row): row for rows in days.values() for row in rows}
    everywhere = set(current)

    def order(row: Mapping[str, Any]) -> tuple[str, str]:
        stamp = instant(row)
        return (stamp.isoformat() if stamp else "", row_identity(spec, row))

    for day in sorted(set(days) | archived_days):
        rows = sorted(days.get(day, []), key=order)
        shard = archive.record_path(table, spec, "", when=_day_start(day))
        digest = rows_hash(spec, rows)
        key = f"{entity}:{day}"
        entry = archive.entry("tables", key)
        if (
            entry is not None
            and entry.get("content_hash") == digest
            and not options.full
            and shard.is_file()
        ):
            counts.unchanged += len(rows)
            continue

        ledger = ledger_path(shard)
        appended = _keep_removed(shard, ledger, spec, current, now)
        counts.absent += appended
        wrote = appended > 0
        wrote |= write_ndjson_if_changed(shard, rows)
        removed = still_removed(read_ledger(ledger), spec, everywhere)
        fields: dict[str, Any] = {
            "path": archive.relative(shard),
            "table": table,
            "records": len(rows),
            "removed_records": len(removed) or None,
            "content_hash": digest,
            "source": SOURCE_LOCAL,
        }
        if wrote:
            fields["archived_at"] = now
            counts.written += len(rows)
        else:
            counts.unchanged += len(rows)
        archive.put("tables", key, **fields)
    return counts


def sync_tables(
    archive: Archive, source: SqliteSource, options: SyncOptions
) -> SyncCounts:
    """Archive every table without a dedicated pass.

    Driven by what the database actually has rather than by what is declared,
    so a table introduced in a future migration is archived on the next run
    with no code change here. That is the whole reason this pass is generic:
    Wispr Flow ships roughly twenty migrations a month, and an archive that
    could only hold tables someone had thought of would fall behind by design.

    Args:
        archive: The destination archive.
        source: An open database reader.
        options: What this run was asked to do.

    Returns:
        What the pass did.
    """
    counts = SyncCounts()
    for table in source.tables():
        if table in HANDLED_TABLES:
            continue
        spec = EXPECTED.get(table)
        if spec is not None and spec.layout is Layout.SHARD:
            pass_counts = sync_sharded(archive, source, table, options)
        else:
            pass_counts = sync_snapshot(archive, source, table, options)
        counts.scanned += pass_counts.scanned
        counts.written += pass_counts.written
        counts.unchanged += pass_counts.unchanged
        counts.absent += pass_counts.absent
        counts.failed += pass_counts.failed
    return counts


def sync_account(
    archive: Archive,
    config: LocalConfig,
    session: SessionInfo,
    options: SyncOptions,
) -> SyncCounts:
    """Archive the account's own state, minus anything that is a credential.

    The voice profile, saved writing samples and rewrite prompts live only in
    ``config.json`` -- there is no table for them -- so an archive that read
    only the database would miss them entirely. Wispr Flow's own per-entity
    watermark map is archived too, as provenance: it records what the app
    believed it had synced at the moment this archive was taken.

    Args:
        archive: The destination archive.
        config: Parsed ``config.json``.
        session: The session summary, which carries no token.
        options: What this run was asked to do.

    Returns:
        What the pass did.
    """
    counts = SyncCounts()
    if options.dry_run:
        counts.scanned = 1
        counts.written = 1
        return counts

    root = archive.resolve("account")
    now = _now()
    payloads: dict[str, Any] = {
        # Identity and expiry only. The token itself is never handed to
        # anything that writes, so no file this tool creates can carry one.
        "profile": account_profile(session),
        "preferences": config.preferences,
        "sync_coordinator": config.sync_coordinator,
        # The raw lists the two Markdown files below are rendered from. They
        # used to exist only as those renderings, which broke the rule every
        # other entity keeps: raw first, so a rendering can always be redone.
        "context": {
            "writingSamples": config.writing_samples,
            "polishPrompts": config.polish_prompts,
        },
    }
    if config.voice_profile is not None:
        payloads["voice_profile"] = config.voice_profile
    wrote = False
    for name, payload in payloads.items():
        wrote |= replace_payload(
            archive, root / f"{name}.json", payload, entity="account", key=name, when=now
        )
    for name, value in (
        ("writing_samples.md", config.writing_samples),
        ("polish_prompts.md", config.polish_prompts),
    ):
        if not value:
            continue
        body = (
            "\n\n".join(str(item) for item in value)
            if isinstance(value, list)
            else str(value)
        )
        wrote |= write_text_if_changed(root / name, body + "\n")

    counts.scanned = 1
    counts.written = 1 if wrote else 0
    counts.unchanged = 0 if wrote else 1
    return counts


def rerender(archive: Archive, options: SyncOptions) -> SyncCounts:
    """Rebuild every rendered document from what is already archived.

    This is what the raw-before-render rule buys. Rendering is a pure function
    of payloads that are already on disk, so a fixed template or a corrected
    speaker map is applied without touching the source at all -- which matters
    most in exactly the case the archive exists for, where Wispr Flow has since
    deleted the transcript being re-rendered.

    Args:
        archive: The archive to rebuild in place.
        options: What this run was asked to do; only ``full`` (rewrite even
            when unchanged) and ``dry_run`` are consulted.

    Returns:
        What the pass did.
    """
    counts = SyncCounts()
    for key, entry in sorted(archive.entries("meetings").items()):
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            continue
        counts.scanned += 1
        directory = archive.existing_path("meetings", key)
        if directory is None or not directory.is_dir():
            counts.failed += 1
            continue

        data = read_json(directory / "raw" / "meeting.json", None)
        if not isinstance(data, dict):
            counts.failed += 1
            continue

        artifacts = MeetingArtifacts(
            meeting_id=key,
            directory=directory / "raw",
            refined=_archived(directory / "raw" / "refined.ndjson"),
            live=_archived(directory / "raw" / "live.ndjson"),
            observations=_archived(
                directory / "raw" / "speakers.observations.ndjson"
            ),
            audio=_archived(directory / "media" / "upload.ogg"),
        )
        if options.dry_run:
            counts.written += 1
            continue

        record = Record(table="Meetings", key=key, data=data)
        # audio="skip": the media file is already in the archive, and this
        # pass must not need the source for anything at all.
        wrote = _write_meeting_files(
            archive,
            directory,
            record,
            artifacts,
            SyncOptions(audio=AUDIO_SKIP),
            counts,
        )
        if options.full:
            wrote = True
        counts.written += 1 if wrote else 0
        counts.unchanged += 0 if wrote else 1
    return counts


def _archived(path: Path) -> Path | None:
    """Return a path when the archive actually holds that artifact.

    Args:
        path: Candidate file.

    Returns:
        The path, or ``None``.
    """
    return path if path.is_file() else None
