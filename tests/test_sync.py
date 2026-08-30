"""Orchestration: idempotence, relocation, tombstones and durability.

The headline test here is ``test_a_second_run_writes_nothing``. Everything else
in this module exists to keep that true under conditions that would otherwise
quietly break it -- a retitle, a soft delete, an interrupted run.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from wispr_flow_exporter import paths
from wispr_flow_exporter.local_config import LocalConfig, Policy, SessionInfo
from wispr_flow_exporter.normalize import calendar_key
from wispr_flow_exporter.schema import EXPECTED
from wispr_flow_exporter.sqlite_source import open_source
from wispr_flow_exporter.store import STATE_ABSENT, STATE_SOFT_DELETED, Archive
from wispr_flow_exporter.sync import SOURCE_LOCAL, SyncOptions, sync_local

from conftest import (
    FAKE_JWT,
    HISTORY_A,
    HISTORY_B,
    HISTORY_C,
    HISTORY_D,
    MEETING_A,
    MEETING_B,
    NOTE_A,
    OWNER,
    OWNER_EMAIL,
    SECOND,
    TITLE_PLAIN,
)

REFINED = [
    {
        "id": "u-1",
        "timestamp": "00:24",
        "text": "Right, the quarterly whisper budget.",
        "speaker": {"id": 1, "source": "refined", "name": None},
    },
    {
        "id": "u-2",
        "timestamp": "01:03",
        "text": "We overspent on murmurs again.",
        "speaker": {"id": 2, "source": "refined", "name": None},
    },
]

SPEAKER_MAP = json.dumps(
    {
        "people": {"p-1": {"name": OWNER}, "p-2": {"name": SECOND}},
        "assignments": {"1": {"consensus": "p-1"}, "2": {"consensus": "p-2"}},
    }
)


def _meeting_row(**overrides: object) -> dict[str, object]:
    """Build a Meetings row around the fixture cast.

    Args:
        **overrides: Columns to replace.

    Returns:
        The row.
    """
    row: dict[str, object] = {
        "id": MEETING_A,
        "title": TITLE_PLAIN,
        "createdAt": "2026-08-21 21:00:58.565 +00:00",
        "modifiedAt": "2026-08-21 21:33:32.711 +00:00",
        "endedAt": 1787347929267,
        "summary": "Reviewed the budget with <@speaker:2>.",
        "notes": "- Ask about the murmur quota",
        "speakerMap": SPEAKER_MAP,
        "participantNames": json.dumps([OWNER, SECOND]),
        "isDeleted": 0,
        "synced": 1,
    }
    row.update(overrides)
    return row


@pytest.fixture
def scene(tmp_path: Path, wispr_db: Callable[..., Path]) -> Callable[..., tuple]:
    """Build a source directory and an archive, ready to sync.

    Returns:
        A factory taking meeting rows and returning
        ``(archive, wispr_paths, db_path)``.
    """

    def build(
        rows: list[dict[str, object]] | None = None,
        *,
        artifacts: bool = True,
        audio_bytes: bytes | None = None,
        tables: dict[str, list[dict[str, object]]] | None = None,
    ) -> tuple[Archive, object, Path]:
        data_dir = tmp_path / "Wispr Flow"
        data_dir.mkdir(exist_ok=True)
        payload: dict[str, object] = {
            "Meetings": rows if rows is not None else [_meeting_row()]
        }
        payload.update(tables or {})
        built = wispr_db(payload)
        built.replace(data_dir / "flow.sqlite")

        if artifacts:
            for row in rows if rows is not None else [_meeting_row()]:
                directory = data_dir / "meetings" / str(row["id"])
                directory.mkdir(parents=True, exist_ok=True)
                (directory / "refined.ndjson").write_text(
                    "\n".join(json.dumps(line) for line in REFINED) + "\n",
                    encoding="utf-8",
                )
                if audio_bytes is not None:
                    (directory / "upload.ogg").write_bytes(audio_bytes)

        resolved = paths.resolve(data_dir=data_dir)
        return Archive(root=tmp_path / "archive"), resolved, resolved.db

    return build


def _run(archive: Archive, resolved: object, **kwargs: object) -> object:
    """Run one local sync against an open reader.

    Args:
        archive: Destination archive.
        resolved: Resolved source paths.
        **kwargs: Passed to :class:`SyncOptions`.

    Returns:
        The sync result.
    """
    policy = kwargs.pop("policy", None)
    with open_source(resolved.db) as source:  # type: ignore[attr-defined]
        return sync_local(
            archive,
            source,
            resolved,  # type: ignore[arg-type]
            SyncOptions(**kwargs),  # type: ignore[arg-type]
            policy=policy,  # type: ignore[arg-type]
        )


def _snapshot(root: Path) -> dict[str, tuple[int, bytes]]:
    """Capture every archived file's mtime and contents.

    Args:
        root: Archive root.

    Returns:
        Relative path to ``(mtime_ns, bytes)``.
    """
    return {
        str(path.relative_to(root)): (path.stat().st_mtime_ns, path.read_bytes())
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


# --- the invariant --------------------------------------------------------


def test_a_backfill_writes_the_expected_documents(scene: Callable[..., tuple]) -> None:
    """A first run produces raw payloads, renders and an index entry."""
    archive, resolved, _ = scene()
    result = _run(archive, resolved)

    counts = result.counts["meetings"]
    assert (counts.scanned, counts.written) == (1, 1)

    entry = archive.entry("meetings", MEETING_A)
    directory = archive.root / entry["path"]
    assert (directory / "raw" / "meeting.json").is_file()
    assert (directory / "raw" / "refined.ndjson").is_file()
    assert (directory / "meeting.md").is_file()
    assert (directory / "summary.md").is_file()
    assert (directory / "transcript.refined.md").is_file()
    assert SECOND in (directory / "summary.md").read_text(encoding="utf-8")


def test_a_second_run_writes_nothing(scene: Callable[..., tuple]) -> None:
    """The invariant this module exists for.

    Not merely "no records written" -- no byte and no mtime anywhere in the
    archive changes, index and sync state included. An archive that rewrote
    itself every pass would churn mtimes, defeat incremental backup, and make
    a real edit indistinguishable from a re-read.
    """
    archive, resolved, _ = scene()
    _run(archive, resolved)
    before = _snapshot(archive.root)

    second = _run(Archive(root=archive.root), resolved)

    assert second.counts["meetings"].written == 0
    assert _snapshot(archive.root) == before


def test_a_full_rescan_also_writes_nothing(scene: Callable[..., tuple]) -> None:
    """--full re-reads every record and must still not rewrite them.

    This caught a real bug: passing archived_at=None to put() *removed* the
    field, so a --full pass rewrote index.json on a run that changed nothing.
    """
    archive, resolved, _ = scene()
    _run(archive, resolved)
    before = _snapshot(archive.root)

    result = _run(Archive(root=archive.root), resolved, full=True)

    assert result.counts["meetings"].scanned == 1
    assert result.counts["meetings"].written == 0
    assert _snapshot(archive.root) == before


def test_an_edit_upstream_is_picked_up(scene: Callable[..., tuple]) -> None:
    """Idempotence must not mean blindness."""
    archive, resolved, db = scene()
    _run(archive, resolved)

    import sqlite3

    with sqlite3.connect(db) as writer:
        writer.execute(
            'UPDATE "Meetings" SET summary = ?, modifiedAt = ? WHERE id = ?',
            ("Revised summary.", "2026-08-22 09:00:00.000 +00:00", MEETING_A),
        )

    result = _run(Archive(root=archive.root), resolved)

    assert result.counts["meetings"].written == 1
    entry = archive.entry("meetings", MEETING_A)
    summary = (archive.root / entry["path"] / "summary.md").read_text(encoding="utf-8")
    assert "Revised summary." in summary


def test_a_volatile_column_alone_does_not_rewrite(
    scene: Callable[..., tuple],
) -> None:
    """Push flags and retry counters flip constantly and mean nothing."""
    archive, resolved, db = scene()
    _run(archive, resolved)
    before = _snapshot(archive.root)

    import sqlite3

    # Exactly what a background sync does: flip the push flags and bump
    # modifiedAt, without touching a word of the content.
    with sqlite3.connect(db) as writer:
        writer.execute(
            'UPDATE "Meetings" SET synced = 0, refineRetries = 9, modifiedAt = ? '
            "WHERE id = ?",
            ("2026-08-23 08:00:00.000 +00:00", MEETING_A),
        )

    fresh = Archive(root=archive.root)
    result = _run(fresh, resolved)

    assert result.counts["meetings"].scanned == 1, "the row must be re-read"
    assert result.counts["meetings"].written == 0

    # Every archived file is untouched, index.json included. The sync journal
    # is deliberately excluded: modifiedAt really did move, so the watermark
    # must follow it, or the next run re-reads this row forever.
    after = _snapshot(fresh.root)
    del before[".sync-state.json"], after[".sync-state.json"]
    assert after == before
    assert fresh.watermark(SOURCE_LOCAL, "meetings") == "2026-08-23 08:00:00.000 +00:00"


# --- relocation and tombstones -------------------------------------------


def test_a_retitle_moves_the_directory(scene: Callable[..., tuple]) -> None:
    """One record keeps one location, so the archive never holds two copies."""
    archive, resolved, db = scene()
    _run(archive, resolved)
    original = archive.root / archive.entry("meetings", MEETING_A)["path"]

    import sqlite3

    with sqlite3.connect(db) as writer:
        writer.execute(
            'UPDATE "Meetings" SET title = ?, modifiedAt = ? WHERE id = ?',
            ("Renamed budget review", "2026-08-22 09:00:00.000 +00:00", MEETING_A),
        )

    fresh = Archive(root=archive.root)
    result = _run(fresh, resolved)
    moved = fresh.root / fresh.entry("meetings", MEETING_A)["path"]

    assert result.counts["meetings"].relocated == 1
    assert not original.exists()
    assert (moved / "meeting.md").is_file()
    assert "renamed-budget-review" in moved.name


def test_a_soft_deleted_meeting_is_kept_and_flagged(
    scene: Callable[..., tuple],
) -> None:
    """Wispr tombstones rows in place; the archive keeps the content."""
    archive, resolved, _ = scene([_meeting_row(isDeleted=1)])
    _run(archive, resolved)

    entry = archive.entry("meetings", MEETING_A)
    assert entry["upstream_state"] == STATE_SOFT_DELETED
    assert "soft_deleted_since" in entry
    assert (archive.root / entry["path"] / "meeting.md").is_file()


def test_a_deleted_transcript_is_recorded_as_such(
    scene: Callable[..., tuple],
) -> None:
    """Wispr dropped its copy and this archive still has one. That is the point."""
    archive, resolved, _ = scene(
        [_meeting_row(transcriptDeletedAt="2026-08-25 10:00:00.000 +00:00")]
    )
    _run(archive, resolved)

    entry = archive.entry("meetings", MEETING_A)
    assert entry["transcript_deleted_upstream"] is True
    document = (archive.root / entry["path"] / "meeting.md").read_text(encoding="utf-8")
    assert "transcript_deleted_upstream: true" in document


def test_a_vanished_meeting_is_flagged_only_on_a_full_scan(
    scene: Callable[..., tuple], tmp_path: Path, wispr_db: Callable[..., Path]
) -> None:
    """Absence can only be established by a pass that looked at everything.

    A watermarked run has not seen the records it deliberately skipped, so it
    must not conclude they are gone.
    """
    archive, resolved, db = scene([_meeting_row(), _meeting_row(id=MEETING_B)])
    _run(archive, resolved, full=True)

    import sqlite3

    with sqlite3.connect(db) as writer:
        writer.execute('DELETE FROM "Meetings" WHERE id = ?', (MEETING_B,))

    incremental = _run(Archive(root=archive.root), resolved)
    assert incremental.counts["meetings"].absent == 0

    fresh = Archive(root=archive.root)
    full = _run(fresh, resolved, full=True)

    assert full.counts["meetings"].absent == 1
    assert fresh.entry("meetings", MEETING_B)["upstream_state"] == STATE_ABSENT
    assert (fresh.root / fresh.entry("meetings", MEETING_B)["path"]).exists()


# --- options and safety ---------------------------------------------------


def test_dry_run_writes_nothing_at_all(scene: Callable[..., tuple]) -> None:
    """A dry run reports what would happen and leaves no trace."""
    archive, resolved, _ = scene()
    result = _run(archive, resolved, dry_run=True)

    assert result.counts["meetings"].written == 1
    assert not archive.root.exists()


def test_audio_is_copied_by_default(scene: Callable[..., tuple]) -> None:
    """Wispr garbage-collects meeting audio, so a pointer would rot."""
    archive, resolved, _ = scene(audio_bytes=b"OggS" + bytes(2048))
    _run(archive, resolved)

    entry = archive.entry("meetings", MEETING_A)
    audio = archive.root / entry["path"] / "media" / "upload.ogg"
    assert audio.is_file()
    assert audio.stat().st_size == 2052


def test_audio_can_be_skipped(scene: Callable[..., tuple]) -> None:
    """An operator who does not want gigabytes of Opus can say so."""
    archive, resolved, _ = scene(audio_bytes=b"OggS" + bytes(2048))
    _run(archive, resolved, audio="skip")

    entry = archive.entry("meetings", MEETING_A)
    assert not (archive.root / entry["path"] / "media").exists()


def test_audio_over_the_cap_is_not_copied(scene: Callable[..., tuple]) -> None:
    """A single enormous recording must not silently fill the disk."""
    archive, resolved, _ = scene(audio_bytes=b"OggS" + bytes(4096))
    _run(archive, resolved, max_audio_mb=0)

    entry = archive.entry("meetings", MEETING_A)
    assert not (archive.root / entry["path"] / "media").exists()


def test_a_malformed_meeting_id_is_refused_not_archived(
    scene: Callable[..., tuple],
) -> None:
    """Ids become path components, so they are validated before use."""
    archive, resolved, _ = scene([_meeting_row(id="../../../../etc/passwd")])
    result = _run(archive, resolved)

    assert result.counts["meetings"].failed == 1
    assert archive.count("meetings") == 0


def test_a_failed_pass_does_not_advance_the_watermark(
    scene: Callable[..., tuple],
) -> None:
    """A partial pass must re-read rather than skip what it missed."""
    archive, resolved, _ = scene([_meeting_row(id="not-a-uuid")])
    _run(archive, resolved)

    assert archive.watermark(SOURCE_LOCAL, "meetings") is None


def test_a_successful_pass_advances_the_watermark(
    scene: Callable[..., tuple],
) -> None:
    """The next run reads only what changed, from the source's own encoding."""
    archive, resolved, _ = scene()
    _run(archive, resolved)

    assert archive.watermark(SOURCE_LOCAL, "meetings") == (
        "2026-08-21 21:33:32.711 +00:00"
    )


def test_a_meeting_with_no_artifacts_still_archives(
    scene: Callable[..., tuple],
) -> None:
    """One of three real meetings has only a refined transcript; some have none."""
    archive, resolved, _ = scene(artifacts=False)
    result = _run(archive, resolved)

    assert result.counts["meetings"].written == 1
    entry = archive.entry("meetings", MEETING_A)
    assert entry["artifacts"] == []
    assert (archive.root / entry["path"] / "raw" / "meeting.json").is_file()
    assert not (archive.root / entry["path"] / "transcript.refined.md").exists()


def test_the_archive_is_owner_only_at_every_level(
    scene: Callable[..., tuple],
) -> None:
    """Intermediate directories carry meeting titles in their children's names."""
    import stat as stat_module

    archive, resolved, _ = scene()
    _run(archive, resolved)

    for path in archive.root.rglob("*"):
        mode = stat_module.S_IMODE(path.stat().st_mode)
        expected = 0o700 if path.is_dir() else 0o600
        assert mode == expected, f"{path.relative_to(archive.root)} is {oct(mode)}"


# --- notes, calendar, dictionary, todos -----------------------------------

NOTE_ROW = {
    "id": NOTE_A,
    "title": "Whisper budget scratch",
    "content": "- Ask Hush about the murmur quota",
    "createdAt": "2026-05-18 10:00:00.000 +00:00",
    "modifiedAt": "2026-05-18 10:05:00.000 +00:00",
    "isDeleted": 0,
}

# 181 characters, the length actually observed for a Google calendar id.
LONG_EXTERNAL_ID = "q" * 181

CALENDAR_ROW = {
    "externalId": LONG_EXTERNAL_ID,
    "title": "Quarterly whisper budget",
    "startAtUtc": 1787272400000,
    "endAtUtc": 1787276000000,
    "status": "confirmed",
    "updatedAt": "2026-08-21T22:45:13.107782Z",
    "participantNames": json.dumps([OWNER, SECOND]),
}

DICTIONARY_ROWS = [
    {"id": "d-1", "phrase": "kubernetis", "replacement": "Kubernetes"},
    {"id": "d-2", "phrase": "brb", "replacement": "be right back", "isSnippet": 1},
    {"id": "d-3", "phrase": "gone", "replacement": "removed", "isDeleted": 1},
]


def test_notes_archive_as_markdown_beside_their_raw_payload(
    scene: Callable[..., tuple],
) -> None:
    """A scratchpad note is a document, not a directory."""
    archive, resolved, _ = scene(rows=[], tables={"Notes": [NOTE_ROW]})
    result = _run(archive, resolved)

    assert result.counts["notes"].written == 1
    stem = archive.root / archive.entry("notes", NOTE_A)["path"]
    assert (stem.parent / f"{stem.name}.md").is_file()
    assert (stem.parent / f"{stem.name}.raw.json").is_file()
    assert "murmur quota" in (stem.parent / f"{stem.name}.md").read_text(
        encoding="utf-8"
    )


def test_a_calendar_id_too_long_to_be_a_path_is_hashed(
    scene: Callable[..., tuple],
) -> None:
    """The primary key is 181 characters of base32 and cannot be a filename.

    Truncating it is not injective, so two events could collapse into one. The
    full id is preserved inside the record.
    """
    archive, resolved, _ = scene(rows=[], tables={"CalendarEvents": [CALENDAR_ROW]})
    _run(archive, resolved)

    key = calendar_key(LONG_EXTERNAL_ID)
    entry = archive.entry("calendar", key)
    assert len(key) == 12
    assert entry["external_id"] == LONG_EXTERNAL_ID
    assert key in entry["path"]
    assert (archive.root / entry["path"]).is_file()


def test_calendar_events_get_no_markdown_digest(
    scene: Callable[..., tuple],
) -> None:
    """Events mutate constantly, so a rendered digest would churn every sync.

    Both events on the development machine had already flipped to cancelled.
    """
    archive, resolved, _ = scene(rows=[], tables={"CalendarEvents": [CALENDAR_ROW]})
    _run(archive, resolved)

    assert list((archive.root / "calendar").rglob("*.md")) == []


def test_a_rescheduled_event_moves_to_its_new_shard(
    scene: Callable[..., tuple],
) -> None:
    """The YYYY/MM directory derives from startAtUtc, which moves."""
    archive, resolved, db = scene(rows=[], tables={"CalendarEvents": [CALENDAR_ROW]})
    _run(archive, resolved)
    key = calendar_key(LONG_EXTERNAL_ID)
    original = archive.root / archive.entry("calendar", key)["path"]

    import sqlite3

    with sqlite3.connect(db) as writer:
        writer.execute(
            'UPDATE "CalendarEvents" SET startAtUtc = ?, updatedAt = ? '
            "WHERE externalId = ?",
            (1790000000000, "2026-09-01T10:00:00.000000Z", LONG_EXTERNAL_ID),
        )

    fresh = Archive(root=archive.root)
    result = _run(fresh, resolved)

    assert result.counts["calendar"].relocated == 1
    assert not original.exists()
    assert (fresh.root / fresh.entry("calendar", key)["path"]).is_file()


def test_the_dictionary_keeps_deleted_entries(
    scene: Callable[..., tuple],
) -> None:
    """What was removed from a vocabulary of names and codenames is a record."""
    archive, resolved, _ = scene(rows=[], tables={"Dictionary": DICTIONARY_ROWS})
    _run(archive, resolved)

    entry = archive.entry("dictionary", "dictionary")
    assert entry["records"] == 3
    assert entry["deleted_records"] == 1

    rendered = (archive.root / "dictionary" / "dictionary.md").read_text(
        encoding="utf-8"
    )
    assert "~~gone~~" in rendered
    assert "## Snippets" in rendered

    lines = (archive.root / "dictionary" / "dictionary.ndjson").read_text(
        encoding="utf-8"
    ).splitlines()
    assert len(lines) == 3


def test_a_snapshot_table_is_indexed_once_not_per_row(
    scene: Callable[..., tuple],
) -> None:
    """The artifact already lists every row, tombstoned ones included.

    Per-row entries would restate the file while making index.json grow with
    records that have no separate location.
    """
    archive, resolved, _ = scene(rows=[], tables={"Dictionary": DICTIONARY_ROWS})
    _run(archive, resolved)

    assert list(archive.entries("dictionary")) == ["dictionary"]


def test_an_empty_table_still_produces_its_artifact(
    scene: Callable[..., tuple],
) -> None:
    """An empty file means "read, and empty"; an absent one means nothing."""
    archive, resolved, _ = scene(rows=[])
    _run(archive, resolved)

    todos = archive.root / "todos" / "todos.ndjson"
    assert todos.is_file()
    assert todos.read_text(encoding="utf-8") == ""


def test_an_account_with_no_records_creates_no_empty_namespaces(
    scene: Callable[..., tuple],
) -> None:
    """Looking for notes must not add "notes": {} to the index.

    Creating a namespace on read meant a pass that archived nothing still
    changed index.json, which broke the zero-bytes guarantee on the first run
    that happened to look at an empty table.
    """
    archive, resolved, _ = scene(rows=[])
    _run(archive, resolved)

    assert "notes" not in archive.index["entities"]
    assert "calendar" not in archive.index["entities"]


# --- dictation ------------------------------------------------------------


def _history_row(**overrides: object) -> dict[str, object]:
    """Build a History row.

    Args:
        **overrides: Columns to replace.

    Returns:
        The row.
    """
    row: dict[str, object] = {
        "transcriptEntityId": HISTORY_A,
        "timestamp": "2026-08-30 09:14:02.100 +00:00",
        "asrText": "send the whisper budget to hush before friday",
        "formattedText": "Send the whisper budget to Hush before Friday.",
        "app": "com.example.NotepadApp",
        "numWords": 8,
        "isArchived": 0,
    }
    row.update(overrides)
    return row


def _policy(value: str) -> Policy:
    """Build an observed policy.

    Args:
        value: The localDataPolicy value.

    Returns:
        The policy.
    """
    return Policy(value, "never_delete", datetime.now(tz=UTC))


def test_dictation_is_sharded_by_day(scene: Callable[..., tuple]) -> None:
    """A heavy user produces thousands of dictations a day.

    A document each would be a filesystem-hostile archive; the useful unit is
    the day.
    """
    archive, resolved, _ = scene(
        rows=[],
        tables={
            "History": [
                _history_row(),
                _history_row(
                    transcriptEntityId=HISTORY_B,
                    timestamp="2026-08-30 11:00:00.000 +00:00",
                ),
                _history_row(
                    transcriptEntityId=HISTORY_C,
                    timestamp="2026-08-31 09:00:00.000 +00:00",
                ),
            ]
        },
    )
    result = _run(archive, resolved, policy=_policy("store_normally"))

    assert result.counts["dictation"].scanned == 3
    shard = archive.root / "dictation" / "2026" / "08" / "2026-08-30.ndjson"
    assert len(shard.read_text(encoding="utf-8").splitlines()) == 2
    assert (archive.root / "dictation" / "2026" / "08" / "2026-08-31.ndjson").is_file()


def test_the_day_log_uses_the_most_processed_text(
    scene: Callable[..., tuple],
) -> None:
    """The cascade picks the corrected text, and records which column it came from."""
    archive, resolved, _ = scene(
        rows=[],
        tables={
            "History": [
                _history_row(
                    editedText="Send the Q3 whisper budget to Hush before Friday."
                )
            ]
        },
    )
    _run(archive, resolved, policy=_policy("store_normally"))

    log = (archive.root / "dictation" / "2026" / "08" / "2026-08-30.md").read_text(
        encoding="utf-8"
    )
    assert "Send the Q3 whisper budget" in log
    assert "com.example.NotepadApp" in log


def test_a_whole_day_is_rewritten_not_appended(
    scene: Callable[..., tuple],
) -> None:
    """A run that read only the newest rows must not drop the rest of the day.

    History has no modification column, so a later run re-reads a trailing
    window; if it wrote only what it read, the day's earlier dictations would
    vanish from the shard.
    """
    archive, resolved, db = scene(rows=[], tables={"History": [_history_row()]})
    _run(archive, resolved, policy=_policy("store_normally"))

    import sqlite3

    with sqlite3.connect(db) as writer:
        writer.execute(
            'INSERT INTO "History" ("transcriptEntityId", "timestamp", '
            '"formattedText") VALUES (?, ?, ?)',
            (
                HISTORY_D,
                "2026-08-30 15:00:00.000 +00:00",
                "And the murmur quota.",
            ),
        )

    _run(Archive(root=archive.root), resolved, policy=_policy("store_normally"))

    shard = archive.root / "dictation" / "2026" / "08" / "2026-08-30.ndjson"
    assert len(shard.read_text(encoding="utf-8").splitlines()) == 2


def test_an_in_place_edit_within_the_window_is_picked_up(
    scene: Callable[..., tuple],
) -> None:
    """History rows are edited after creation and carry no modifiedAt.

    A pure watermark would archive the first version and never see the
    correction, so a trailing window is re-read every run.
    """
    archive, resolved, db = scene(rows=[], tables={"History": [_history_row()]})
    _run(archive, resolved, policy=_policy("store_normally"))

    import sqlite3

    with sqlite3.connect(db) as writer:
        writer.execute(
            'UPDATE "History" SET editedText = ? WHERE transcriptEntityId = ?',
            ("Corrected afterwards.", HISTORY_A),
        )

    fresh = Archive(root=archive.root)
    result = _run(fresh, resolved, policy=_policy("store_normally"))

    assert result.counts["dictation"].written == 1
    log = (fresh.root / "dictation" / "2026" / "08" / "2026-08-30.md").read_text(
        encoding="utf-8"
    )
    assert "Corrected afterwards." in log


def test_never_store_is_recorded_not_inferred(
    scene: Callable[..., tuple],
) -> None:
    """An archive empty by preference must be able to prove which preference.

    This is the one failure here that is silent, permanent, and only
    discovered on the day the data is finally wanted -- so a zero row count
    and a policy that forbids storage must not look the same.
    """
    archive, resolved, _ = scene(rows=[])
    result = _run(archive, resolved, policy=_policy("never_store"))

    assert result.counts["dictation"].scanned == 0
    recorded = archive.source_state(SOURCE_LOCAL)["policy"]
    assert recorded["local_data_policy"] == "never_store"
    assert recorded["records_dictation"] is False
    assert "observed_at" in recorded


def test_an_empty_dictation_table_under_a_storing_policy_is_different(
    scene: Callable[..., tuple],
) -> None:
    """Zero rows while storage is enabled means genuinely nothing dictated."""
    archive, resolved, _ = scene(rows=[])
    _run(archive, resolved, policy=_policy("store_normally"))

    assert archive.source_state(SOURCE_LOCAL)["policy"]["records_dictation"] is True


def test_screen_context_is_absent_from_the_shard_by_default(
    scene: Callable[..., tuple],
) -> None:
    """The default export never selects a screen capture."""
    archive, resolved, _ = scene(
        rows=[],
        tables={"History": [_history_row(axText="Untitled document — secrets")]},
    )
    _run(archive, resolved, policy=_policy("store_normally"))

    shard = (archive.root / "dictation" / "2026" / "08" / "2026-08-30.ndjson").read_text(
        encoding="utf-8"
    )
    assert "axText" not in shard
    assert "secrets" not in shard


def test_screen_context_appears_only_when_opted_in(
    scene: Callable[..., tuple],
) -> None:
    """Two explicit flags widen the export; nothing else does."""
    archive, resolved, _ = scene(
        rows=[],
        tables={"History": [_history_row(axText="Untitled document")]},
    )
    _run(
        archive,
        resolved,
        include_screen_context=True,
        policy=_policy("store_normally"),
    )

    shard = (archive.root / "dictation" / "2026" / "08" / "2026-08-30.ndjson").read_text(
        encoding="utf-8"
    )
    assert "axText" in shard


def test_dictation_blobs_are_written_as_sidecars(
    scene: Callable[..., tuple],
) -> None:
    """raw payloads stay readable, so binary goes beside them, not inside."""
    archive, resolved, _ = scene(
        rows=[],
        tables={"History": [_history_row(audio=b"OggS" + bytes(64))]},
    )
    _run(archive, resolved, include_blobs=True, policy=_policy("store_normally"))

    sidecar = (
        archive.root / "dictation" / "media" / "2026" / "08" / HISTORY_A / "audio.opus"
    )
    assert sidecar.is_file()
    assert sidecar.read_bytes() == b"OggS" + bytes(64)


def test_a_dictation_with_an_unparseable_date_is_still_archived(
    scene: Callable[..., tuple],
) -> None:
    """Filing it under today would invent provenance; it gets its own shard."""
    archive, resolved, _ = scene(
        rows=[], tables={"History": [_history_row(timestamp="not a date")]}
    )
    _run(archive, resolved, policy=_policy("store_normally"))

    assert archive.entry("dictation", "undated") is not None


def test_dictation_re_runs_write_nothing(scene: Callable[..., tuple]) -> None:
    """The invariant holds for sharded entities too."""
    archive, resolved, _ = scene(rows=[], tables={"History": [_history_row()]})
    _run(archive, resolved, policy=_policy("store_normally"))
    before = _snapshot(archive.root)

    _run(Archive(root=archive.root), resolved, policy=_policy("store_normally"))

    assert _snapshot(archive.root) == before


# --- misc tables and account ----------------------------------------------


def test_every_table_is_reachable(scene: Callable[..., tuple]) -> None:
    """All 26 tables are archived, not only the ones with a renderer."""
    archive, resolved, _ = scene(rows=[])
    _run(archive, resolved)

    snapshots = {path.stem for path in (archive.root / "tables").glob("*.ndjson")}
    empties = {
        key.split(":")[0].split("/")[-1]
        for key in archive.entries("tables")
        if key.endswith(":empty")
    }
    covered = snapshots | empties | {
        "Meetings", "Notes", "CalendarEvents", "Dictionary", "Todos", "History",
    }
    assert set(EXPECTED) <= covered


def test_a_table_from_a_future_migration_is_archived(
    tmp_path: Path, wispr_db: Callable[..., Path]
) -> None:
    """An undeclared table costs no code change.

    Wispr Flow ships roughly twenty migrations a month; an archive that could
    only hold tables someone had thought of would fall behind by design.
    """
    data_dir = tmp_path / "Wispr Flow"
    data_dir.mkdir()
    built = wispr_db(extra_tables={"WhisperQuota": ("id", "amount")})
    import sqlite3

    with sqlite3.connect(built) as writer:
        writer.execute('INSERT INTO "WhisperQuota" VALUES (?, ?)', ("q1", 7))
    built.replace(data_dir / "flow.sqlite")
    resolved = paths.resolve(data_dir=data_dir)
    archive = Archive(root=tmp_path / "archive")

    _run(archive, resolved)

    shard = archive.root / "tables" / "WhisperQuota.ndjson"
    assert shard.is_file()
    assert json.loads(shard.read_text(encoding="utf-8").strip()) == {
        "id": "q1",
        "amount": 7,
    }


def test_a_snapshot_file_is_not_nested_under_its_own_name(
    scene: Callable[..., tuple],
) -> None:
    """tables/Automations.ndjson, not tables/Automations/Automations.ndjson."""
    archive, resolved, _ = scene(rows=[])
    _run(archive, resolved)

    assert (archive.root / "tables" / "Automations.ndjson").is_file()
    assert not (archive.root / "tables" / "Automations").is_dir()


def test_an_empty_sharded_table_is_still_recorded(
    scene: Callable[..., tuple],
) -> None:
    """A sharded table with no rows produces no shard, so the index says so.

    Without it the archive could not tell "read, and empty" from "never read".
    """
    archive, resolved, _ = scene(rows=[])
    _run(archive, resolved)

    assert archive.entry("tables", "tables/Polish:empty")["records"] == 0


def test_the_account_pass_captures_what_no_table_holds(
    tmp_path: Path, wispr_db: Callable[..., Path]
) -> None:
    """The voice profile, writing samples and prompts live only in config.json.

    An archive that read only the database would miss them entirely.
    """
    data_dir = tmp_path / "Wispr Flow"
    data_dir.mkdir()
    wispr_db().replace(data_dir / "flow.sqlite")
    config = LocalConfig(
        policy=Policy("store_normally", "never_delete", datetime.now(tz=UTC)),
        preferences={"localDataPolicy": "store_normally"},
        sync_coordinator={"timestamps": {"meetings": "2026-08-20T00:00:00Z"}},
        voice_profile={"persona": "brisk"},
        writing_samples=["a whisper budget memo"],
        polish_prompts=["make it terser"],
    )
    archive = Archive(root=tmp_path / "archive")
    resolved = paths.resolve(data_dir=data_dir)

    with open_source(resolved.db) as source:
        sync_local(
            archive,
            source,
            resolved,
            SyncOptions(),
            entities=("account",),
            config=config,
            session=SessionInfo(present=False),
        )

    root = archive.root / "account"
    assert json.loads((root / "voice_profile.json").read_text(encoding="utf-8")) == {
        "persona": "brisk"
    }
    assert "whisper budget memo" in (root / "writing_samples.md").read_text(
        encoding="utf-8"
    )
    assert "make it terser" in (root / "polish_prompts.md").read_text(encoding="utf-8")
    assert json.loads((root / "sync_coordinator.json").read_text(encoding="utf-8"))[
        "timestamps"
    ]["meetings"]


def test_the_archived_profile_carries_no_credential(
    tmp_path: Path, wispr_db: Callable[..., Path]
) -> None:
    """This is the file that would otherwise carry a live token into a backup."""
    data_dir = tmp_path / "Wispr Flow"
    data_dir.mkdir()
    wispr_db().replace(data_dir / "flow.sqlite")
    archive = Archive(root=tmp_path / "archive")
    resolved = paths.resolve(data_dir=data_dir)
    session = SessionInfo(
        present=True,
        project_ref="aaaaaaaaaaaaaaaaaaaa",
        user_id="user-1",
        email=OWNER_EMAIL,
        expires_at=datetime.now(tz=UTC),
    )

    with open_source(resolved.db) as source:
        sync_local(
            archive,
            source,
            resolved,
            SyncOptions(),
            entities=("account",),
            config=LocalConfig(
                policy=Policy("never_store", None, datetime.now(tz=UTC))
            ),
            session=session,
        )

    body = (archive.root / "account" / "profile.json").read_text(encoding="utf-8")
    assert FAKE_JWT not in body
    # access_token_expires_at legitimately contains that substring, so the
    # assertion is on the key that would carry a credential, not on the word.
    assert '"access_token"' not in body
    assert "refresh_token" not in body
    assert OWNER_EMAIL in body
