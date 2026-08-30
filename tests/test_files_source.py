"""Discovery of per-meeting transcript artifacts.

The thing these tests defend is that meeting directories are **not uniform**.
On the machine this was developed against, of three meetings one had only
``refined.ndjson``, one had refined, live and observations but no audio, and
one had refined, live and audio but no observations. Anything that assumed
four files would be wrong about every meeting.
"""

from __future__ import annotations

from pathlib import Path

from wispr_flow_exporter.files_source import (
    AUDIO_NAME,
    LIVE_NAME,
    OBSERVATIONS_NAME,
    REFINED_NAME,
    discover_meetings,
    inventory,
)

from conftest import MEETING_A, MEETING_B, NOTE_A


def _meeting(root: Path, meeting_id: str, *names: str) -> Path:
    """Create a meeting directory holding exactly the named artifacts.

    Args:
        root: The ``meetings/`` directory.
        meeting_id: Directory name.
        *names: File names to create.

    Returns:
        The directory created.
    """
    directory = root / meeting_id
    directory.mkdir(parents=True)
    for name in names:
        (directory / name).write_bytes(b"x" * 16)
    return directory


def test_the_three_real_shapes_are_all_handled(tmp_path: Path) -> None:
    """Refined-only, no-audio and no-observations are all normal.

    These are the exact three shapes observed on a real installation.
    """
    root = tmp_path / "meetings"
    _meeting(root, MEETING_A, REFINED_NAME)
    _meeting(root, MEETING_B, REFINED_NAME, LIVE_NAME, OBSERVATIONS_NAME)
    _meeting(root, NOTE_A, REFINED_NAME, LIVE_NAME, AUDIO_NAME)

    found = {item.meeting_id: item.present for item in discover_meetings(root)}

    assert found[MEETING_A] == ("refined",)
    assert found[MEETING_B] == ("refined", "live", "observations")
    assert found[NOTE_A] == ("refined", "live", "audio")


def test_the_staging_directory_is_skipped(tmp_path: Path) -> None:
    """meetings/.observations-tmp is the app's scratch space, not a meeting."""
    root = tmp_path / "meetings"
    _meeting(root, MEETING_A, REFINED_NAME)
    (root / ".observations-tmp").mkdir()

    assert [item.meeting_id for item in discover_meetings(root)] == [MEETING_A]


def test_directories_that_are_not_meeting_ids_are_skipped(tmp_path: Path) -> None:
    """Validating the name here is also what keeps it safe as a path component."""
    root = tmp_path / "meetings"
    _meeting(root, MEETING_A, REFINED_NAME)
    _meeting(root, "not-a-uuid", REFINED_NAME)
    # An uppercase id is rejected, so the validator stays a strict canonical
    # check. It has to be an id not otherwise used here: macOS filesystems are
    # case-insensitive, so upper- and lower-case forms of the same UUID would
    # be the same directory.
    _meeting(root, MEETING_B.upper(), REFINED_NAME)

    assert [item.meeting_id for item in discover_meetings(root)] == [MEETING_A]


def test_a_symlinked_meeting_directory_is_refused(tmp_path: Path) -> None:
    """The directory is written by another application.

    Following a link out of it would let that application choose what this
    tool copies into the archive.
    """
    root = tmp_path / "meetings"
    root.mkdir(parents=True)
    (tmp_path / "elsewhere").mkdir()
    (root / MEETING_A).symlink_to(tmp_path / "elsewhere", target_is_directory=True)

    assert list(discover_meetings(root)) == []


def test_a_symlinked_artifact_is_refused(tmp_path: Path) -> None:
    """A link named upload.ogg must not cause an arbitrary file to be copied."""
    root = tmp_path / "meetings"
    directory = _meeting(root, MEETING_A, REFINED_NAME)
    secret = tmp_path / "secret.txt"
    secret.write_text("not yours", encoding="utf-8")
    (directory / AUDIO_NAME).symlink_to(secret)

    found = next(iter(discover_meetings(root)))
    assert found.audio is None
    assert found.present == ("refined",)


def test_a_missing_meetings_directory_is_not_an_error(tmp_path: Path) -> None:
    """An account that has never recorded a meeting is a normal state."""
    assert list(discover_meetings(tmp_path / "nope")) == []
    assert inventory(tmp_path / "nope")["directories"] == 0


def test_discovery_is_ordered_so_runs_are_reproducible(tmp_path: Path) -> None:
    """Two runs over the same directory must archive in the same order."""
    root = tmp_path / "meetings"
    for meeting_id in (NOTE_A, MEETING_A, MEETING_B):
        _meeting(root, meeting_id, REFINED_NAME)

    ordered = [item.meeting_id for item in discover_meetings(root)]
    assert ordered == sorted(ordered)


def test_inventory_counts_artifacts_and_audio_bytes(tmp_path: Path) -> None:
    """doctor reports what exists before anything is written."""
    root = tmp_path / "meetings"
    _meeting(root, MEETING_A, REFINED_NAME)
    _meeting(root, MEETING_B, REFINED_NAME, LIVE_NAME, AUDIO_NAME)

    marks = inventory(root)

    assert marks["directories"] == 2
    assert marks["artifacts"] == {
        "refined": 2,
        "live": 1,
        "observations": 0,
        "audio": 1,
    }
    assert marks["audio_bytes"] == 16


def test_size_of_a_missing_artifact_is_zero(tmp_path: Path) -> None:
    """Sizing an artifact Wispr Flow has already collected must not raise."""
    root = tmp_path / "meetings"
    _meeting(root, MEETING_A, REFINED_NAME)
    found = next(iter(discover_meetings(root)))

    assert found.size_of("audio") == 0
    assert found.size_of("refined") == 16
