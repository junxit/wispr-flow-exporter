"""Orchestration: idempotence, relocation, tombstones and durability.

The headline test here is ``test_a_second_run_writes_nothing``. Everything else
in this module exists to keep that true under conditions that would otherwise
quietly break it -- a retitle, a soft delete, an interrupted run.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from wispr_flow_exporter import paths
from wispr_flow_exporter.sqlite_source import open_source
from wispr_flow_exporter.store import STATE_ABSENT, STATE_SOFT_DELETED, Archive
from wispr_flow_exporter.sync import SOURCE_LOCAL, SyncOptions, sync_local

from conftest import MEETING_A, MEETING_B, OWNER, SECOND, TITLE_PLAIN

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
    ) -> tuple[Archive, object, Path]:
        data_dir = tmp_path / "Wispr Flow"
        data_dir.mkdir(exist_ok=True)
        built = wispr_db({"Meetings": rows if rows is not None else [_meeting_row()]})
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
    with open_source(resolved.db) as source:  # type: ignore[attr-defined]
        return sync_local(archive, source, resolved, SyncOptions(**kwargs))  # type: ignore[arg-type]


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
