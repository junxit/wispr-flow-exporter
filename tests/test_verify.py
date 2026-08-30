"""Verification, and re-rendering an archive with no source at all."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from wispr_flow_exporter import paths
from wispr_flow_exporter.sqlite_source import open_source
from wispr_flow_exporter.store import Archive
from wispr_flow_exporter.sync import SyncOptions, rerender, sync_local
from wispr_flow_exporter.verify import verify_archive

from conftest import MEETING_A, OWNER, SECOND, TITLE_PLAIN

SPEAKER_MAP = json.dumps(
    {
        "people": {"p-1": {"name": OWNER}, "p-2": {"name": SECOND}},
        "assignments": {"1": {"consensus": "p-1"}, "2": {"consensus": "p-2"}},
    }
)

REFINED = [
    {
        "id": "u-1",
        "timestamp": "00:24",
        "text": "Right, the quarterly whisper budget.",
        "speaker": {"id": 1, "source": "refined", "name": None},
    }
]


@pytest.fixture
def archived(tmp_path: Path, wispr_db: Callable[..., Path]) -> tuple[Archive, object]:
    """Build and populate an archive from a synthetic source.

    Returns:
        ``(archive, resolved_paths)``.
    """
    data_dir = tmp_path / "Wispr Flow"
    data_dir.mkdir()
    row = {
        "id": MEETING_A,
        "title": TITLE_PLAIN,
        "createdAt": "2026-08-21 21:00:58.565 +00:00",
        "modifiedAt": "2026-08-21 21:33:32.711 +00:00",
        "summary": "Reviewed the budget with <@speaker:2>.",
        "speakerMap": SPEAKER_MAP,
        "isDeleted": 0,
    }
    wispr_db({"Meetings": [row]}).replace(data_dir / "flow.sqlite")
    meeting_dir = data_dir / "meetings" / MEETING_A
    meeting_dir.mkdir(parents=True)
    (meeting_dir / "refined.ndjson").write_text(
        "\n".join(json.dumps(line) for line in REFINED) + "\n", encoding="utf-8"
    )

    resolved = paths.resolve(data_dir=data_dir)
    archive = Archive(root=tmp_path / "archive")
    with open_source(resolved.db) as source:
        sync_local(archive, source, resolved, SyncOptions())
    return archive, resolved


def _verify(archive: Archive, resolved: object, **kwargs: object):
    """Run verification against the source.

    Args:
        archive: The archive.
        resolved: Resolved source paths.
        **kwargs: Passed to ``verify_archive``.

    Returns:
        The report.
    """
    with open_source(resolved.db) as source:  # type: ignore[attr-defined]
        return verify_archive(archive, source, **kwargs)  # type: ignore[arg-type]


# --- verify ---------------------------------------------------------------


def test_a_freshly_synced_archive_is_consistent(
    archived: tuple[Archive, object],
) -> None:
    """The baseline: nothing disagrees straight after a sync."""
    archive, resolved = archived
    report = _verify(archive, resolved)

    assert report.ok
    assert report.checked > 0
    assert "archive is consistent" in report.lines()


def test_a_deleted_file_is_reported(archived: tuple[Archive, object]) -> None:
    """An index entry pointing at nothing means the archive lost data."""
    archive, resolved = archived
    (archive.root / archive.entry("meetings", MEETING_A)["path"]).rename(
        archive.root / "meetings" / "moved-away"
    )

    report = _verify(archive, resolved)

    assert not report.ok
    assert report.missing_files == [f"meetings/{MEETING_A}"]


def test_an_index_path_outside_the_archive_is_refused(
    archived: tuple[Archive, object],
) -> None:
    """A hand-edited index must not send verification wandering off."""
    archive, resolved = archived
    archive.put("meetings", MEETING_A, path="../../../etc")

    report = _verify(archive, resolved)

    assert report.unsafe_paths == [f"meetings/{MEETING_A}"]


def test_an_untracked_directory_is_reported(
    archived: tuple[Archive, object],
) -> None:
    """A meeting on disk the index forgot is data nobody can find."""
    archive, resolved = archived
    orphan = archive.root / "meetings" / "2026" / "08" / "2026-08-01--orphan--x"
    orphan.mkdir(parents=True)

    report = _verify(archive, resolved)

    assert any("orphan" in path for path in report.untracked)


def test_a_source_record_that_was_never_archived_is_reported(
    archived: tuple[Archive, object],
) -> None:
    """Quietly holding less than exists is the failure that matters most."""
    archive, resolved = archived
    del archive.index["entities"]["meetings"][MEETING_A]

    report = _verify(archive, resolved)

    assert report.unarchived == [f"Meetings/{MEETING_A}"]


def test_deep_verification_detects_an_edited_payload(
    archived: tuple[Archive, object],
) -> None:
    """--deep recomputes the digest instead of trusting the index."""
    archive, resolved = archived
    raw = archive.root / archive.entry("meetings", MEETING_A)["path"] / "raw" / "meeting.json"
    payload = json.loads(raw.read_text(encoding="utf-8"))
    payload["summary"] = "tampered with"
    raw.write_text(json.dumps(payload), encoding="utf-8")

    shallow = _verify(archive, resolved)
    deep = _verify(archive, resolved, deep=True)

    assert shallow.ok
    assert deep.stale_hashes == [f"meetings/{MEETING_A}"]


def test_verification_works_without_a_source(
    archived: tuple[Archive, object],
) -> None:
    """Internal consistency does not depend on the app still being installed.

    An archive outliving the application is the entire point.
    """
    archive, _ = archived
    report = verify_archive(archive, None)

    assert report.ok
    assert report.unarchived == []


def test_a_tombstone_is_not_a_fault(
    archived: tuple[Archive, object], wispr_db: Callable[..., Path]
) -> None:
    """A record upstream deleted is what the archive exists to keep.

    The bug this reproduces: the count check compared the source's rows
    against every index entry, tombstones included. So the first time anything
    was deleted upstream -- the case this whole tool is built around -- a
    healthy archive reported "archive has problems" and `verify` exited 1,
    permanently. An operator who sees that every run stops reading it, which is
    the trap `--strict-schema` exists to avoid.

    The old version of this test marked the record absent while the row was
    still in the database, which no real run can produce, and asserted only the
    tombstone count. That is why it passed while the archive it describes was
    being reported as broken.
    """
    archive, resolved = archived
    # The record leaves the database entirely, and the archive keeps it.
    wispr_db({"Meetings": []}).replace(resolved.db)  # type: ignore[attr-defined]
    archive.mark_absent("meetings", [], when="2026-09-01T00:00:00+00:00")

    report = _verify(archive, resolved)

    assert report.tombstoned == 1
    assert report.counts == {}
    assert report.ok


def test_a_genuine_count_mismatch_still_fails(
    archived: tuple[Archive, object], wispr_db: Callable[..., Path]
) -> None:
    """Reconciling tombstones must not blunt the check it lives in.

    A record that vanished from the source *without* being tombstoned is a
    real inconsistency and must still be reported, with the retained count
    shown so the numbers visibly add up.
    """
    archive, resolved = archived
    wispr_db({"Meetings": []}).replace(resolved.db)  # type: ignore[attr-defined]

    report = _verify(archive, resolved)

    assert report.counts["Meetings"] == (0, 1, 0)
    assert not report.ok
    assert any("0 in source, 1 archived" in line for line in report.lines())


# --- render ---------------------------------------------------------------


def test_re_rendering_needs_no_source_and_changes_nothing(
    archived: tuple[Archive, object],
) -> None:
    """The payoff of writing raw before rendering.

    Rendering is a pure function of payloads already on disk, so a rebuild
    touches no source -- which matters most in exactly the case the archive
    exists for, where Wispr Flow has since deleted what is being re-rendered.
    """
    archive, _ = archived
    before = {
        path: path.read_bytes()
        for path in sorted(archive.root.rglob("*.md"))
    }

    counts = rerender(archive, SyncOptions())

    assert counts.scanned == 1
    assert counts.written == 0
    assert {p: p.read_bytes() for p in sorted(archive.root.rglob("*.md"))} == before


def test_re_rendering_repairs_a_damaged_document(
    archived: tuple[Archive, object],
) -> None:
    """A rendering fixed later is applied from what is already archived."""
    archive, _ = archived
    document = (
        archive.root
        / archive.entry("meetings", MEETING_A)["path"]
        / "transcript.refined.md"
    )
    document.write_text("clobbered", encoding="utf-8")

    counts = rerender(archive, SyncOptions())

    assert counts.written == 1
    assert OWNER in document.read_text(encoding="utf-8")


def test_re_rendering_reports_a_meeting_whose_payload_is_gone(
    archived: tuple[Archive, object],
) -> None:
    """A rebuild cannot invent a payload it does not have, and says so."""
    archive, _ = archived
    (
        archive.root
        / archive.entry("meetings", MEETING_A)["path"]
        / "raw"
        / "meeting.json"
    ).unlink()

    counts = rerender(archive, SyncOptions())

    assert counts.failed == 1


def test_a_dry_run_rebuild_writes_nothing(
    archived: tuple[Archive, object],
) -> None:
    """Reporting what would change must not change it."""
    archive, _ = archived
    document = (
        archive.root
        / archive.entry("meetings", MEETING_A)["path"]
        / "transcript.refined.md"
    )
    document.write_text("clobbered", encoding="utf-8")

    rerender(archive, SyncOptions(dry_run=True))

    assert document.read_text(encoding="utf-8") == "clobbered"
