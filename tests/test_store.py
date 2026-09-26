"""Archive paths, the namespaced index, containment and tombstones."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import (
    MEETING_A,
    MEETING_B,
    TITLE_EMPTY,
    TITLE_FRONTMATTER,
    TITLE_PLAIN,
    TITLE_TRAVERSAL,
)

from wispr_flow_exporter.schema import EXPECTED, Layout, TableSpec
from wispr_flow_exporter.store import (
    STATE_ABSENT,
    STATE_PRESENT,
    STATE_SOFT_DELETED,
    Archive,
    ArchiveBusy,
    UnsafeArchivePathError,
    content_hash,
    entity_name,
    record_dir_name,
    shard_name,
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


def test_a_corrupt_index_is_kept_not_overwritten(tmp_path: Path) -> None:
    """The index is the only record of what upstream deleted; keep its remains.

    Measured before the fix: the next save wrote a fresh index.json over the
    damaged one, and with it went every missing_since and every
    transcript_deleted_upstream the archive had recorded.
    """
    root = tmp_path / "archive"
    root.mkdir()
    (root / "index.json").write_text("{ truncated", encoding="utf-8")

    archive = Archive(root=root)
    archive.save()

    kept = list(root.glob("index.json.corrupt-*"))
    assert len(kept) == 1
    assert kept[0].read_text(encoding="utf-8") == "{ truncated"
    assert json.loads((root / "index.json").read_text(encoding="utf-8"))["entities"] == {}
    assert any("index.json" in notice for notice in archive.notices)


def test_corrupt_sync_state_is_kept_too(tmp_path: Path) -> None:
    """Watermarks are cheaper to lose than tombstones, but not free."""
    root = tmp_path / "archive"
    root.mkdir()
    (root / ".sync-state.json").write_text("[]", encoding="utf-8")

    Archive(root=root).save()

    assert len(list(root.glob(".sync-state.json.corrupt-*"))) == 1


def test_a_read_only_archive_never_moves_a_corrupt_index(tmp_path: Path) -> None:
    """A dry run or a verify reports the damage and leaves it exactly where it is."""
    root = tmp_path / "archive"
    root.mkdir()
    (root / "index.json").write_text("{ truncated", encoding="utf-8")

    archive = Archive(root=root, read_only=True)
    archive.save()

    assert (root / "index.json").read_text(encoding="utf-8") == "{ truncated"
    assert not list(root.glob("*.corrupt-*"))
    assert archive.unreadable == ["index.json"]


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


def test_relocation_never_moves_another_records_directory(archive: Archive) -> None:
    """An index entry naming someone else's directory is not an instruction.

    A sync-conflict copy or a hand-merged index.json can file meeting A under
    meeting B's path. The old code moved B's directory into A's place, and the
    record it moved over was the one this archive exists to keep.
    """
    spec = EXPECTED["Meetings"]
    theirs = archive.record_path("Meetings", spec, MEETING_B, when=WHEN, title="b")
    theirs.mkdir(parents=True)
    (theirs / "meeting.md").write_text("meeting B", encoding="utf-8")
    archive.put("meetings", MEETING_A, path=archive.relative(theirs))

    mine = archive.record_path("Meetings", spec, MEETING_A, when=WHEN, title="a")
    assert not archive.relocate("meetings", MEETING_A, mine)

    assert (theirs / "meeting.md").read_text(encoding="utf-8") == "meeting B"


def test_relocation_never_deletes_an_existing_destination(archive: Archive) -> None:
    """Both copies survive; verify reports the stray rather than rmtree erasing it."""
    spec = EXPECTED["Meetings"]
    old = archive.record_path("Meetings", spec, MEETING_A, when=WHEN, title="old")
    new = archive.record_path("Meetings", spec, MEETING_A, when=WHEN, title="new")
    for directory, text in ((old, "older"), (new, "newer")):
        directory.mkdir(parents=True)
        (directory / "meeting.md").write_text(text, encoding="utf-8")
    archive.put("meetings", MEETING_A, path=archive.relative(old))

    assert not archive.relocate("meetings", MEETING_A, new)

    assert (old / "meeting.md").read_text(encoding="utf-8") == "older"
    assert (new / "meeting.md").read_text(encoding="utf-8") == "newer"


def test_a_document_relocation_never_overwrites_a_file(archive: Archive) -> None:
    """A retitled note moves its files only onto names that are free."""
    spec = EXPECTED["Notes"]
    old = archive.record_path("Notes", spec, MEETING_A, when=WHEN, title="old")
    new = archive.record_path("Notes", spec, MEETING_A, when=WHEN, title="new")
    old.parent.mkdir(parents=True)
    old.with_name(f"{old.name}.md").write_text("older", encoding="utf-8")
    new.with_name(f"{new.name}.md").write_text("newer", encoding="utf-8")
    archive.put("notes", MEETING_A, path=archive.relative(old.with_name(f"{old.name}.md")))

    archive.relocate_document("notes", MEETING_A, new, (".md", ".raw.json"))

    assert old.with_name(f"{old.name}.md").read_text(encoding="utf-8") == "older"
    assert new.with_name(f"{new.name}.md").read_text(encoding="utf-8") == "newer"


def test_a_document_relocation_never_moves_another_records_files(
    archive: Archive,
) -> None:
    """The same ownership rule as directories, for records made of sibling files."""
    spec = EXPECTED["Notes"]
    theirs = archive.record_path("Notes", spec, MEETING_B, when=WHEN, title="b")
    theirs.parent.mkdir(parents=True)
    document = theirs.with_name(f"{theirs.name}.md")
    document.write_text("note B", encoding="utf-8")
    archive.put("notes", MEETING_A, path=archive.relative(document))

    mine = archive.record_path("Notes", spec, MEETING_A, when=WHEN, title="a")
    assert not archive.relocate_document("notes", MEETING_A, mine, (".md",))

    assert document.read_text(encoding="utf-8") == "note B"


def test_a_file_relocation_obeys_the_same_two_refusals(archive: Archive) -> None:
    """Calendar events move as single files, never onto or out of another record."""
    spec = EXPECTED["CalendarEvents"]
    key, other = "a1b2c3d4e5f6", "0f0f0f0f0f0f"
    theirs = archive.record_path("CalendarEvents", spec, other, when=WHEN, title="x")
    theirs.parent.mkdir(parents=True)
    foreign = theirs.with_name(f"{theirs.name}.json")
    foreign.write_text("event B", encoding="utf-8")
    archive.put("calendar", key, path=archive.relative(foreign))
    mine = archive.record_path("CalendarEvents", spec, key, when=WHEN, title="x")

    assert not archive.relocate_file("calendar", key, mine.with_name(f"{mine.name}.json"))
    assert foreign.read_text(encoding="utf-8") == "event B"

    old = mine.with_name(f"{mine.name}.json")
    old.write_text("older", encoding="utf-8")
    archive.put("calendar", key, path=archive.relative(old))
    taken = archive.resolve("calendar", "2026", "09", old.name)
    taken.parent.mkdir(parents=True)
    taken.write_text("newer", encoding="utf-8")

    assert not archive.relocate_file("calendar", key, taken)
    assert old.read_text(encoding="utf-8") == "older"
    assert taken.read_text(encoding="utf-8") == "newer"


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


# --- one writer at a time -------------------------------------------------


def test_a_second_writer_is_turned_away(tmp_path: Path) -> None:
    """Two syncs saving one index would each drop the other's records."""
    first = Archive(root=tmp_path / "archive")
    second = Archive(root=tmp_path / "archive")

    with first.lock():
        with pytest.raises(ArchiveBusy, match="another sync or render"):
            with second.lock():
                pass

    with second.lock():
        pass


def test_a_read_only_archive_takes_no_lock_and_creates_nothing(tmp_path: Path) -> None:
    """A dry run or a verify beside a sync is fine; neither writes."""
    writer = Archive(root=tmp_path / "archive")
    with writer.lock():
        with Archive(root=tmp_path / "archive", read_only=True).lock():
            pass

    with Archive(root=tmp_path / "other", read_only=True).lock():
        pass
    assert not (tmp_path / "other").exists()
