"""Archive paths, the namespaced index, containment and tombstones."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from wispr_flow_exporter.schema import EXPECTED, Layout, TableSpec
from wispr_flow_exporter.store import (
    STATE_ABSENT,
    STATE_PRESENT,
    STATE_SOFT_DELETED,
    Archive,
    UnsafeArchivePathError,
    content_hash,
    entity_name,
    record_dir_name,
    shard_name,
)

from conftest import (
    MEETING_A,
    MEETING_B,
    TITLE_EMPTY,
    TITLE_FRONTMATTER,
    TITLE_PLAIN,
    TITLE_TRAVERSAL,
)

WHEN = datetime(2026, 8, 21, 21, 0, 58, tzinfo=UTC)
NOW = "2026-08-30T08:12:00+00:00"


@pytest.fixture
def archive(tmp_path: Path) -> Archive:
    """An empty archive rooted in a scratch directory."""
    return Archive(root=tmp_path / "archive")


# --- naming ---------------------------------------------------------------


def test_record_dir_name_is_readable_and_unique() -> None:
    """The date and slug are affordances; the id is what guarantees identity."""
    name = record_dir_name(WHEN, TITLE_PLAIN, MEETING_A)
    assert name == f"2026-08-21--quarterly-whisper-budget-review--{MEETING_A}"


@pytest.mark.parametrize(
    "title", [TITLE_EMPTY, TITLE_TRAVERSAL, TITLE_FRONTMATTER, None, "..", "/"]
)
def test_a_hostile_title_still_yields_one_safe_component(title: object) -> None:
    """No title can escape its directory or collapse the name to nothing.

    The slug is only ever a readability affordance, so degrading a hostile
    title to "untitled" costs nothing an archive needs.
    """
    name = record_dir_name(WHEN, title, MEETING_A)
    assert "/" not in name
    assert ".." not in name
    assert name.endswith(MEETING_A)


def test_the_full_id_is_used_not_a_prefix() -> None:
    """At ten thousand meetings a 32-bit prefix collides about one percent."""
    assert MEETING_A in record_dir_name(WHEN, TITLE_PLAIN, MEETING_A)


def test_undated_records_are_filed_honestly() -> None:
    """Filing an undated record under today would invent provenance."""
    assert record_dir_name(None, TITLE_PLAIN, MEETING_A).startswith("undated--")
    assert shard_name(None) == "undated/undated"


def test_shards_are_by_day() -> None:
    """A heavy dictation day must be one file, not thousands of inodes."""
    assert shard_name(WHEN) == "2026/08/2026-08-21"


def test_unknown_tables_land_under_tables() -> None:
    """A table shipped in a future migration is archivable with no code change."""
    assert entity_name("Meetings") == "meetings"
    assert entity_name("History") == "dictation"
    assert entity_name("WhisperQuota") == "tables/WhisperQuota"


# --- paths ----------------------------------------------------------------


def test_layouts_produce_their_own_shapes(archive: Archive) -> None:
    """Each layout puts records where that kind of record belongs."""
    meetings = archive.record_path(
        "Meetings", EXPECTED["Meetings"], MEETING_A, when=WHEN, title=TITLE_PLAIN
    )
    dictation = archive.record_path("History", EXPECTED["History"], "x", when=WHEN)
    dictionary = archive.record_path("Dictionary", EXPECTED["Dictionary"], "x")

    assert archive.relative(meetings).startswith("meetings/2026/08/2026-08-21--")
    assert archive.relative(dictation) == "dictation/2026/08/2026-08-21.ndjson"
    assert archive.relative(dictionary) == "dictionary/dictionary.ndjson"


@pytest.mark.parametrize(
    "parts",
    [
        ("..", "escaped"),
        ("meetings", "..", "..", "escaped"),
        ("a", "b", "c", "d", "e", "f", "..", "..", "..", "..", "..", "..", "..", "x"),
        ("/etc",),
        ("meetings", "/etc/passwd"),
    ],
)
def test_containment_refuses_every_escape(archive: Archive, parts: tuple[str, ...]) -> None:
    """Traversal and absolute components are both refused.

    The absolute case matters as much as the dotted one: joining an absolute
    component discards everything to its left, so a path can leave the archive
    without a single ".." appearing in it.
    """
    with pytest.raises(UnsafeArchivePathError):
        archive.resolve(*parts)


def test_containment_allows_the_root_itself(archive: Archive) -> None:
    """The root is inside the archive, which the comparison must not exclude."""
    assert archive.resolve() == archive.root


# --- content hashing ------------------------------------------------------


def test_volatile_columns_do_not_change_the_digest() -> None:
    """Push flags and retry counters flip constantly and mean nothing.

    Including them would rewrite every file in the archive on every run, which
    would make an incremental sync indistinguishable from a full one.
    """
    spec = EXPECTED["Meetings"]
    base = {"id": MEETING_A, "title": TITLE_PLAIN, "summary": "budget"}

    quiet = content_hash(spec, {**base, "synced": 0, "refineRetries": 0})
    noisy = content_hash(spec, {**base, "synced": 1, "refineRetries": 7})

    assert quiet == noisy


def test_a_real_edit_does_change_the_digest() -> None:
    """Ignoring churn must not mean ignoring content."""
    spec = EXPECTED["Meetings"]
    before = content_hash(spec, {"id": MEETING_A, "summary": "budget"})
    after = content_hash(spec, {"id": MEETING_A, "summary": "budget, revised"})

    assert before != after


def test_digest_is_stable_across_key_order() -> None:
    """Column order from the database must not look like an edit."""
    spec = EXPECTED["Meetings"]
    assert content_hash(spec, {"a": 1, "b": 2}) == content_hash(spec, {"b": 2, "a": 1})


# --- index ----------------------------------------------------------------


def test_entities_are_namespaced(archive: Archive) -> None:
    """Ten record kinds with incompatible key shapes cannot share one map."""
    archive.put("meetings", MEETING_A, path="meetings/x", title=TITLE_PLAIN)
    archive.put("notes", MEETING_A, path="notes/y", title="scratch")

    assert archive.entry("meetings", MEETING_A)["path"] == "meetings/x"
    assert archive.entry("notes", MEETING_A)["path"] == "notes/y"
    assert archive.count() == 2


def test_put_drops_none_rather_than_storing_null(archive: Archive) -> None:
    """Explicit nulls would make every optional field churn between runs."""
    archive.put("meetings", MEETING_A, path="x", title=None)
    entry = archive.entry("meetings", MEETING_A)

    assert "title" not in entry

    archive.put("meetings", MEETING_A, title="named")
    archive.put("meetings", MEETING_A, title=None)
    assert "title" not in archive.entry("meetings", MEETING_A)


def test_a_tampered_index_cannot_redirect_a_write(archive: Archive) -> None:
    """index.json is untrusted input, even though it is ours.

    A corrupted or hand-edited entry pointing outside the archive must be
    refused rather than followed into someone else's directory.
    """
    archive.put("meetings", MEETING_A, path="../../../etc/passwd")
    assert archive.existing_path("meetings", MEETING_A) is None


def test_index_and_state_round_trip(tmp_path: Path) -> None:
    """A later run picks up exactly where the previous one left off."""
    first = Archive(root=tmp_path / "archive")
    first.put("meetings", MEETING_A, path="meetings/x")
    first.set_watermark("wispr-local", "meetings", "modifiedAt", "2026-08-25")
    first.save()

    second = Archive(root=tmp_path / "archive")
    assert second.entry("meetings", MEETING_A)["path"] == "meetings/x"
    assert second.watermark("wispr-local", "meetings") == "2026-08-25"
    assert second.index["tool_version"]


def test_a_corrupt_index_does_not_stop_a_run(tmp_path: Path) -> None:
    """Losing the index costs a re-scan, not the archive."""
    root = tmp_path / "archive"
    root.mkdir()
    (root / "index.json").write_text("{ truncated", encoding="utf-8")

    assert Archive(root=root).count() == 0


# --- relocation -----------------------------------------------------------


def test_a_retitle_moves_the_directory(archive: Archive) -> None:
    """One record keeps one location; copying would leave two versions."""
    spec = EXPECTED["Meetings"]
    old = archive.record_path("Meetings", spec, MEETING_A, when=WHEN, title="old name")
    old.mkdir(parents=True)
    (old / "meeting.md").write_text("content", encoding="utf-8")
    archive.put("meetings", MEETING_A, path=archive.relative(old))

    new = archive.record_path("Meetings", spec, MEETING_A, when=WHEN, title="new name")
    assert archive.relocate("meetings", MEETING_A, new)

    assert not old.exists()
    assert (new / "meeting.md").read_text(encoding="utf-8") == "content"


def test_relocating_an_unmoved_record_is_a_no_op(archive: Archive) -> None:
    """An unchanged title must not cost a filesystem operation."""
    spec = EXPECTED["Meetings"]
    path = archive.record_path("Meetings", spec, MEETING_A, when=WHEN, title="same")
    path.mkdir(parents=True)
    archive.put("meetings", MEETING_A, path=archive.relative(path))

    assert not archive.relocate("meetings", MEETING_A, path)


def test_relocating_a_record_that_was_never_written_is_safe(archive: Archive) -> None:
    """A first sync has nothing to move."""
    target = archive.resolve("meetings", "2026", "08", "x")
    assert not archive.relocate("meetings", MEETING_A, target)


def test_relocation_refuses_a_tampered_source_path(archive: Archive) -> None:
    """A hostile index entry must not become a move out of the archive."""
    archive.put("meetings", MEETING_A, path="../../elsewhere")
    target = archive.resolve("meetings", "x")
    assert not archive.relocate("meetings", MEETING_A, target)


# --- tombstones -----------------------------------------------------------


def test_a_present_record_is_marked_present(archive: Archive) -> None:
    """The ordinary case records only that it was seen."""
    archive.mark_seen("meetings", MEETING_A, soft_deleted=False, when=NOW)
    entry = archive.entry("meetings", MEETING_A)

    assert entry["upstream_state"] == STATE_PRESENT
    assert "soft_deleted_since" not in entry


def test_a_tombstoned_record_is_kept_and_dated(archive: Archive) -> None:
    """Wispr deletes rows in place; the archive keeps them and notes when."""
    archive.mark_seen("meetings", MEETING_A, soft_deleted=True, when=NOW)
    entry = archive.entry("meetings", MEETING_A)

    assert entry["upstream_state"] == STATE_SOFT_DELETED
    assert entry["soft_deleted_since"] == NOW


def test_a_record_that_vanished_is_flagged_never_deleted(archive: Archive) -> None:
    """A record upstream removed is exactly what an archive exists to keep."""
    archive.put("meetings", MEETING_A, path="meetings/a")
    archive.put("meetings", MEETING_B, path="meetings/b")

    newly = archive.mark_absent("meetings", [MEETING_A], when=NOW)

    assert newly == [MEETING_B]
    assert archive.entry("meetings", MEETING_B)["upstream_state"] == STATE_ABSENT
    assert archive.entry("meetings", MEETING_B)["missing_since"] == NOW
    assert archive.entry("meetings", MEETING_B)["path"] == "meetings/b"


def test_absence_is_recorded_once(archive: Archive) -> None:
    """missing_since is when it went, not when it was last checked."""
    archive.put("meetings", MEETING_A, path="meetings/a")
    archive.mark_absent("meetings", [], when=NOW)
    later = archive.mark_absent("meetings", [], when="2026-09-01T00:00:00+00:00")

    assert later == []
    assert archive.entry("meetings", MEETING_A)["missing_since"] == NOW


def test_a_returning_record_is_no_longer_missing(archive: Archive) -> None:
    """Restoring a record upstream clears the flag rather than leaving a lie."""
    archive.put("meetings", MEETING_A, path="meetings/a")
    archive.mark_absent("meetings", [], when=NOW)
    archive.mark_seen("meetings", MEETING_A, soft_deleted=False, when=NOW)

    entry = archive.entry("meetings", MEETING_A)
    assert entry["upstream_state"] == STATE_PRESENT
    assert "missing_since" not in entry


def test_an_undeleted_record_clears_its_tombstone_date(archive: Archive) -> None:
    """Un-deleting upstream must not leave a stale soft_deleted_since."""
    archive.mark_seen("meetings", MEETING_A, soft_deleted=True, when=NOW)
    archive.mark_seen("meetings", MEETING_A, soft_deleted=False, when=NOW)

    assert "soft_deleted_since" not in archive.entry("meetings", MEETING_A)


# --- state ----------------------------------------------------------------


def test_watermarks_record_the_column_they_came_from(archive: Archive) -> None:
    """A schema change that drops the column must be detectable, not silent."""
    archive.set_watermark("wispr-local", "meetings", "modifiedAt", "2026-08-25")
    marks = archive.source_state("wispr-local")["watermarks"]

    assert marks["meetings"] == {"column": "modifiedAt", "value": "2026-08-25"}


def test_a_null_watermark_is_not_stored(archive: Archive) -> None:
    """An empty table must not reset a watermark to nothing."""
    archive.set_watermark("wispr-local", "meetings", "modifiedAt", None)
    assert archive.watermark("wispr-local", "meetings") is None


def test_backends_keep_separate_state(archive: Archive) -> None:
    """Local and cloud progress independently and must not overwrite."""
    archive.set_watermark("wispr-local", "meetings", "modifiedAt", "local")
    archive.set_watermark("wispr-cloud", "meetings", "updated_at", "cloud")

    assert archive.watermark("wispr-local", "meetings") == "local"
    assert archive.watermark("wispr-cloud", "meetings") == "cloud"


def test_saved_state_is_owner_only(tmp_path: Path) -> None:
    """The index names every meeting; it is not world-readable."""
    archive = Archive(root=tmp_path / "archive")
    archive.put("meetings", MEETING_A, path="meetings/x")
    archive.save()

    assert (archive.root / "index.json").stat().st_mode & 0o777 == 0o600
    assert (archive.root / ".sync-state.json").stat().st_mode & 0o777 == 0o600
    assert archive.root.stat().st_mode & 0o777 == 0o700
    assert json.loads((archive.root / "index.json").read_text(encoding="utf-8"))


def test_artifact_cursors_are_per_meeting(archive: Archive) -> None:
    """An unchanged NDJSON must never be re-read on a later run."""
    cursor = archive.artifact_cursor("wispr-local", MEETING_A)
    cursor["refined"] = {"size": 63294, "lines": 283}

    assert archive.artifact_cursor("wispr-local", MEETING_A)["refined"]["lines"] == 283
    assert archive.artifact_cursor("wispr-local", MEETING_B) == {}


def test_snapshot_layout_ignores_dates(archive: Archive) -> None:
    """A small mutable table is one file, not a shard tree."""
    spec = TableSpec(pk="id", layout=Layout.SNAPSHOT, columns=("id",))
    path = archive.record_path("Todos", spec, "x", when=WHEN)
    assert archive.relative(path) == "todos/todos.ndjson"
