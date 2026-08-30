"""Reading a live database safely, and noticing when its schema moves."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from wispr_flow_exporter import sqlite_source
from wispr_flow_exporter.schema import pin_from_migrations
from wispr_flow_exporter.sqlite_source import (
    DriftClass,
    SourceError,
    SqliteSource,
    fingerprint,
    open_source,
)

from conftest import (
    DEFAULT_MIGRATIONS,
    HISTORY_A,
    MEETING_A,
    MEETING_B,
    OWNER,
    SECOND,
    TITLE_PLAIN,
)

MEETING_ROW = {
    "id": MEETING_A,
    "title": TITLE_PLAIN,
    "createdAt": "2026-08-20 20:02:23.308 +00:00",
    "modifiedAt": "2026-08-20 20:33:12.365 +00:00",
    "endedAt": 1787257992365,
    "summary": "Reviewed the whisper budget with <@speaker:2>.",
    "speakerMap": '{"people": {"p-1": {"name": "Murmur Pike"}}, '
    '"assignments": {"1": {"consensus": "p-1"}}}',
    "participantNames": '["Murmur Pike", "Hush Delgado"]',
    "isDeleted": 0,
    "synced": 1,
}


@pytest.fixture
def clean_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the declared pin at the fixture's synthetic migration set."""
    monkeypatch.setattr(
        sqlite_source, "MIGRATION_PIN", pin_from_migrations(DEFAULT_MIGRATIONS)
    )


# --- opening --------------------------------------------------------------


def test_missing_database_is_reported_not_created(tmp_path: Path) -> None:
    """A read-only tool must never bring a database into existence."""
    target = tmp_path / "flow.sqlite"
    with pytest.raises(SourceError, match="no Wispr Flow database"):
        with open_source(target):
            pass
    assert not target.exists()


def test_the_connection_refuses_writes(wispr_db: Callable[..., Path]) -> None:
    """PRAGMA query_only and mode=ro both stand between us and the app's data.

    The tool cannot leave Wispr Flow's own database in a state the app would
    not recognize, and this is the assertion that says so.
    """
    with open_source(wispr_db()) as source:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            source.connection.execute('DELETE FROM "Meetings"')


def test_uri_uses_mode_ro_by_default(tmp_path: Path) -> None:
    """The default open is read-only, not merely well-behaved."""
    assert open_source(tmp_path / "flow.sqlite").uri.endswith("?mode=ro")


def test_uri_uses_immutable_for_a_backup(tmp_path: Path) -> None:
    """Backups need immutable=1 because they ship without a -shm sibling."""
    reader = open_source(tmp_path / "backup-x.sqlite", immutable=True)
    assert reader.uri.endswith("?immutable=1")


def test_uri_escapes_query_characters(tmp_path: Path) -> None:
    """A '?' in the path would otherwise start the URI's query string."""
    assert "%3f" in open_source(tmp_path / "od?d.sqlite").uri


def test_reader_outside_a_context_manager_is_an_error(tmp_path: Path) -> None:
    """Using the connection without a pinned snapshot is a bug, not a warning."""
    with pytest.raises(SourceError, match="context manager"):
        _ = open_source(tmp_path / "flow.sqlite").connection


# --- structure ------------------------------------------------------------


def test_tables_and_columns_come_from_the_database(
    wispr_db: Callable[..., Path],
) -> None:
    """Structure is discovered, never assumed."""
    with open_source(wispr_db()) as source:
        assert "Meetings" in source.tables()
        assert "sqlite_sequence" not in source.tables()
        assert "speakerMap" in source.columns("Meetings")
        assert source.primary_key("Meetings") == "id"


def test_row_counts_include_soft_deleted_rows(
    wispr_db: Callable[..., Path],
) -> None:
    """A count is of what exists, not of what upstream still admits to."""
    path = wispr_db(
        {"Meetings": [MEETING_ROW, {**MEETING_ROW, "id": MEETING_B, "isDeleted": 1}]}
    )
    with open_source(path) as source:
        assert source.row_count("Meetings") == 2


def test_migrations_absent_is_reportable_not_fatal(
    wispr_db: Callable[..., Path],
) -> None:
    """A database with no SequelizeMeta is odd, but it is still archivable."""
    with open_source(wispr_db(drop_tables=("SequelizeMeta",))) as source:
        assert source.migrations() == ()
        assert source.pin().count == 0


# --- drift ----------------------------------------------------------------


def test_matching_schema_is_clean(
    wispr_db: Callable[..., Path], clean_pin: None
) -> None:
    """An unchanged database reports OK and names the pin it matched."""
    with open_source(wispr_db()) as source:
        drift = source.detect_drift()
    assert drift.kind is DriftClass.OK
    assert "schema OK" in drift.summary()


def test_a_new_column_is_additive(
    wispr_db: Callable[..., Path], clean_pin: None
) -> None:
    """Migration 150 adding a column must not stop the archive.

    This is the common case -- roughly twenty migrations a month -- which is
    why it warns and completes rather than failing.
    """
    path = wispr_db(extra_columns={"Meetings": ("somethingNewInMigration150",)})
    with open_source(path) as source:
        drift = source.detect_drift()

    assert drift.kind is DriftClass.ADDITIVE
    assert drift.new_columns["Meetings"] == ("somethingNewInMigration150",)
    assert not drift.blocks_rendering
    assert "somethingNewInMigration150" in drift.summary()


def test_a_new_table_is_additive(
    wispr_db: Callable[..., Path], clean_pin: None
) -> None:
    """An undeclared table is archived and reported, not refused."""
    path = wispr_db(extra_tables={"WhisperQuota": ("id", "amount")})
    with open_source(path) as source:
        drift = source.detect_drift()

    assert drift.kind is DriftClass.ADDITIVE
    assert drift.new_tables == ("WhisperQuota",)


def test_a_missing_required_column_is_breaking(
    wispr_db: Callable[..., Path], clean_pin: None
) -> None:
    """Losing a renderer's input is breaking, and is named exactly."""
    path = wispr_db(drop_columns={"Meetings": ("title",)})
    with open_source(path) as source:
        drift = source.detect_drift()

    assert drift.kind is DriftClass.BREAKING
    assert drift.missing_required["Meetings"] == ("title",)
    assert drift.blocks_rendering
    assert "REQUIRED columns missing on Meetings" in drift.summary()


def test_a_dropped_table_is_breaking(
    wispr_db: Callable[..., Path], clean_pin: None
) -> None:
    """A table that vanished cannot be archived and must be loud."""
    path = wispr_db(drop_tables=("Todos",))
    with open_source(path) as source:
        drift = source.detect_drift()

    assert drift.kind is DriftClass.BREAKING
    assert drift.missing_tables == ("Todos",)


def test_a_non_required_missing_column_is_not_breaking(
    wispr_db: Callable[..., Path], clean_pin: None
) -> None:
    """Losing a column no renderer reads degrades the archive, not the run."""
    path = wispr_db(drop_columns={"Meetings": ("shareSlug",)})
    with open_source(path) as source:
        drift = source.detect_drift()

    assert drift.kind is DriftClass.ADDITIVE
    assert drift.missing_columns["Meetings"] == ("shareSlug",)


def test_an_older_database_is_stale_not_broken(
    wispr_db: Callable[..., Path], clean_pin: None
) -> None:
    """Reading a backup or a downgraded app is a reportable state.

    Its pin is recorded as-is so it is never mistaken for the current one.
    """
    path = wispr_db(migrations=("00000000000001-init.js",))
    with open_source(path) as source:
        drift = source.detect_drift()

    assert drift.kind is DriftClass.STALE_SOURCE
    assert drift.live.count == 1


# --- records --------------------------------------------------------------


def test_records_decode_json_columns(wispr_db: Callable[..., Path]) -> None:
    """JSON-in-TEXT columns are archived as structure, not as strings."""
    with open_source(wispr_db({"Meetings": [MEETING_ROW]})) as source:
        record = next(source.records("Meetings"))

    assert record.key == MEETING_A
    assert record.data["participantNames"] == [OWNER, SECOND]
    assert record.data["speakerMap"]["assignments"]["1"]["consensus"] == "p-1"


def test_a_json_column_that_is_not_json_is_preserved_verbatim(
    wispr_db: Callable[..., Path],
) -> None:
    """Losing data because a column changed shape is not an option.

    Wispr has already migrated speakerMap's shape once; recording the raw text
    under a marker means a future change costs a rendering, not the content.
    """
    path = wispr_db({"Meetings": [{**MEETING_ROW, "speakerMap": "not json at all"}]})
    with open_source(path) as source:
        record = next(source.records("Meetings"))

    assert record.data["speakerMap"] == {"__unparsed__": "not json at all"}


def test_soft_deleted_rows_are_returned_and_flagged(
    wispr_db: Callable[..., Path],
) -> None:
    """A record upstream deleted is exactly the record an archive is for.

    It is never filtered out at read time; it is flagged so the writer can
    keep it and mark it.
    """
    path = wispr_db({"Meetings": [{**MEETING_ROW, "isDeleted": 1}]})
    with open_source(path) as source:
        record = next(source.records("Meetings"))

    assert record.soft_deleted


def test_unknown_columns_are_archived_without_a_code_change(
    wispr_db: Callable[..., Path],
) -> None:
    """The reader is PRAGMA-driven, so migration 150 costs nothing.

    This is the property that makes continuous drift affordable.
    """
    path = wispr_db(
        {"Meetings": [{**MEETING_ROW, "whisperQuota": 42}]},
        extra_columns={"Meetings": ("whisperQuota",)},
    )
    with open_source(path) as source:
        record = next(source.records("Meetings"))

    assert record.data["whisperQuota"] == 42


def test_screen_context_is_excluded_by_default(
    wispr_db: Callable[..., Path],
) -> None:
    """The default export never selects a screen capture."""
    path = wispr_db(
        {
            "History": [
                {
                    "transcriptEntityId": HISTORY_A,
                    "timestamp": "2026-08-20 20:02:23.308 +00:00",
                    "formattedText": "Send the whisper budget.",
                    "axText": "Untitled document",
                    "screenshot": b"\x89PNG\r\n\x1a\n" + bytes(32),
                }
            ]
        }
    )
    with open_source(path) as source:
        record = next(source.records("History"))

    assert "axText" not in record.data
    assert "screenshot" not in record.data
    assert record.data["formattedText"] == "Send the whisper budget."


def test_screen_context_is_included_only_on_opt_in(
    wispr_db: Callable[..., Path],
) -> None:
    """The flag is the only thing that widens the projection."""
    path = wispr_db(
        {
            "History": [
                {
                    "transcriptEntityId": HISTORY_A,
                    "timestamp": "2026-08-20 20:02:23.308 +00:00",
                    "axText": "Untitled document",
                }
            ]
        }
    )
    with open_source(path) as source:
        record = next(source.records("History", include_screen_context=True))

    assert record.data["axText"] == "Untitled document"


def test_blobs_become_references_rather_than_inline_base64(
    wispr_db: Callable[..., Path],
) -> None:
    """raw.json must stay readable, so binary is a sidecar with a digest."""
    payload = b"OggS" + bytes(1024)
    path = wispr_db(
        {
            "History": [
                {
                    "transcriptEntityId": HISTORY_A,
                    "timestamp": "2026-08-20 20:02:23.308 +00:00",
                    "audio": payload,
                }
            ]
        }
    )
    with open_source(path) as source:
        record = next(source.records("History", include_blobs=True))

    reference = record.data["audio"]["__blob__"]
    assert reference["bytes"] == len(payload)
    assert len(reference["sha256"]) == 64
    assert record.blobs["audio"] == payload


def test_blobs_are_recorded_as_present_even_when_not_archived(
    wispr_db: Callable[..., Path],
) -> None:
    """The archive must be honest about what it chose not to contain."""
    path = wispr_db(
        {
            "History": [
                {
                    "transcriptEntityId": HISTORY_A,
                    "timestamp": "2026-08-20 20:02:23.308 +00:00",
                    "audio": b"OggS" + bytes(16),
                }
            ]
        }
    )
    with open_source(path) as source:
        record = next(source.records("History", include_blobs=False))

    assert record.data["audio"]["__blob__"]["archived"] is False
    assert record.blobs == {}


def test_presigned_urls_are_redacted_not_archived(
    wispr_db: Callable[..., Path],
) -> None:
    """A signed object URL is a bearer credential, so only its presence is kept."""
    path = wispr_db(
        {
            "NoteImages": [
                {
                    "id": HISTORY_A,
                    "presignedGetUrl": "https://example.invalid/o?X-Amz-Signature=abc",
                }
            ]
        }
    )
    with open_source(path) as source:
        record = next(source.records("NoteImages"))

    assert record.data["presignedGetUrl"] == {"__redacted__": "credential"}


def test_a_watermark_limits_the_rows_read(wispr_db: Callable[..., Path]) -> None:
    """Incremental runs read only what changed, compared on the raw value."""
    older = {**MEETING_ROW, "modifiedAt": "2026-08-01 10:00:00.000 +00:00"}
    newer = {
        **MEETING_ROW,
        "id": MEETING_B,
        "modifiedAt": "2026-08-25 10:00:00.000 +00:00",
    }
    with open_source(wispr_db({"Meetings": [older, newer]})) as source:
        keys = [
            record.key
            for record in source.records(
                "Meetings",
                since="2026-08-10 00:00:00.000 +00:00",
                since_column="modifiedAt",
            )
        ]

    assert keys == [MEETING_B]


def test_max_value_advances_a_watermark(wispr_db: Callable[..., Path]) -> None:
    """The next run's watermark comes from what this run actually saw."""
    rows = [
        {**MEETING_ROW, "modifiedAt": "2026-08-01 10:00:00.000 +00:00"},
        {
            **MEETING_ROW,
            "id": MEETING_B,
            "modifiedAt": "2026-08-25 10:00:00.000 +00:00",
        },
    ]
    with open_source(wispr_db({"Meetings": rows})) as source:
        assert source.max_value("Meetings", "modifiedAt") == (
            "2026-08-25 10:00:00.000 +00:00"
        )
        assert source.max_value("Meetings", "noSuchColumn") is None


def test_records_of_an_undeclared_table_still_read(
    wispr_db: Callable[..., Path],
) -> None:
    """A table shipped in a future migration is archived without a spec."""
    path = wispr_db(extra_tables={"WhisperQuota": ("id", "amount")})
    with sqlite3.connect(path) as writer:
        writer.execute('INSERT INTO "WhisperQuota" VALUES (?, ?)', ("q1", 7))

    with open_source(path) as source:
        records = list(source.records("WhisperQuota"))

    assert records[0].data == {"id": "q1", "amount": 7}


# --- fingerprint ----------------------------------------------------------


def test_fingerprint_describes_the_files_that_exist(
    wispr_db: Callable[..., Path],
) -> None:
    """The cheap short-circuit reports the database and, when present, the WAL."""
    path = wispr_db()
    marks = fingerprint(path)

    assert marks["db_size"] > 0
    assert "db_mtime_ns" in marks
    assert "wal_size" not in marks


def test_fingerprint_of_a_missing_file_is_empty(tmp_path: Path) -> None:
    """A fingerprint is an optimization, so its absence must not raise."""
    assert fingerprint(tmp_path / "gone.sqlite") == {}


def test_source_is_reusable_after_exit(wispr_db: Callable[..., Path]) -> None:
    """Leaving the context closes cleanly and can be entered again."""
    reader = SqliteSource(wispr_db())
    with reader:
        assert reader.row_count("Meetings") == 0
    with reader:
        assert reader.row_count("Meetings") == 0
