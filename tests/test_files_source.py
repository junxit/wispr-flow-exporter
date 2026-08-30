"""Discovery of per-meeting transcript artifacts.

The thing these tests defend is that meeting directories are **not uniform**.
On the machine this was developed against, of three meetings one had only
``refined.ndjson``, one had refined, live and observations but no audio, and
one had refined, live and audio but no observations. Anything that assumed
four files would be wrong about every meeting.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from wispr_flow_exporter.files_source import (
    AUDIO_NAME,
    LIVE_NAME,
    MAX_LINE_BYTES,
    OBSERVATIONS_NAME,
    REFINED_NAME,
    discover_meetings,
    inventory,
    read_transcript,
)

from conftest import MEETING_A, MEETING_B, NOTE_A, OWNER


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


# --- reading --------------------------------------------------------------


def _ndjson(path: Path, *objects: object, trailing_newline: bool = True) -> Path:
    """Write NDJSON lines exactly as given, including a truncated tail.

    Args:
        path: Destination file.
        *objects: Values to serialize, or raw strings to write verbatim.
        trailing_newline: Whether the file ends with a newline.

    Returns:
        The path written.
    """
    lines = [
        obj if isinstance(obj, str) else json.dumps(obj) for obj in objects
    ]
    body = "\n".join(lines) + ("\n" if trailing_newline else "")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


REFINED_LINES = (
    {
        "id": "u-0001",
        "timestamp": "00:24",
        "text": "Right, the quarterly whisper budget.",
        "speaker": {"id": 1, "source": "refined", "name": None},
    },
    {
        "id": "u-0002",
        "timestamp": "04:03",
        "text": "We overspent on murmurs again.",
        "speaker": {"id": 2, "source": "refined", "name": None},
    },
)

LIVE_LINES = (
    {"meta": {"v": 3, "clock": "recording_active_ms"}},
    {
        "id": "l-0001",
        "timestamp": "0:25",
        "text": "Static on the line, say again?",
        "speaker": {"id": 1, "source": "mic", "name": None},
        "startEpochMs": 1787346084450,
        "endEpochMs": 1787346087450,
        "segment": 0,
    },
    {
        "id": "l-0002",
        "timestamp": "10:25",
        "text": "I'm good. How are you doing?",
        "speaker": {"id": 1001, "source": "system", "name": OWNER},
        "startEpochMs": 1787346684450,
        "segment": 0,
    },
    {
        "id": "marker-1-paused",
        "timestamp": "5:27 PM",
        "marker": "paused",
        "epochMs": 1787346984450,
        "segment": 0,
    },
)


def test_refined_has_no_header_and_every_line_is_a_turn(tmp_path: Path) -> None:
    """refined.ndjson is uniform; all 151/283/285 real lines are turns."""
    path = _ndjson(tmp_path / REFINED_NAME, *REFINED_LINES)
    read = read_transcript(path)

    assert read.meta is None
    assert len(read.turns) == 2
    assert read.turns[0].offset == timedelta(seconds=24)
    assert read.turns[0].speaker_source == "refined"
    assert read.is_clean


def test_live_has_a_header_a_marker_and_two_speaker_spaces(tmp_path: Path) -> None:
    """One reader handles the header, the turns and the marker line."""
    path = _ndjson(tmp_path / LIVE_NAME, *LIVE_LINES)
    read = read_transcript(path)

    assert read.meta == {"v": 3, "clock": "recording_active_ms"}
    assert len(read.turns) == 2
    assert [turn.label for turn in read.turns] == ["mic#1", "system#1001"]
    assert read.turns[0].start_epoch_ms == 1787346084450
    assert read.is_clean


def test_the_wall_clock_marker_does_not_become_a_turn(tmp_path: Path) -> None:
    """A marker puts "5:27 PM" where a turn puts an MM:SS offset.

    This is the line that crashes a naive mm:ss parser.
    """
    path = _ndjson(tmp_path / LIVE_NAME, *LIVE_LINES)
    read = read_transcript(path)

    assert [marker.marker for marker in read.markers] == ["paused"]
    assert read.markers[0].epoch_ms == 1787346984450
    assert all("marker" not in turn.turn_id for turn in read.turns)


def test_a_live_speaker_name_is_never_used_as_an_attribution(
    tmp_path: Path,
) -> None:
    """The platform's active-speaker marker lags and misattributes.

    In the development dataset 141 of one meeting's 366 live lines carry a
    name, and a verified line puts one participant's words on the other. The
    name is read for provenance and never becomes a label.
    """
    path = _ndjson(tmp_path / LIVE_NAME, *LIVE_LINES)
    read = read_transcript(path)

    system_turn = read.turns[1]
    assert system_turn.label == "system#1001"
    assert OWNER not in system_turn.label


def test_both_padded_and_unpadded_offsets_parse(tmp_path: Path) -> None:
    """refined zero-pads and live does not; both are real."""
    path = _ndjson(tmp_path / LIVE_NAME, *LIVE_LINES)
    read = read_transcript(path)

    assert read.turns[0].offset == timedelta(seconds=25)
    assert read.turns[1].offset == timedelta(minutes=10, seconds=25)


def test_observations_are_preserved_even_without_a_renderer(
    tmp_path: Path,
) -> None:
    """obs and participant records are archived rather than dropped."""
    path = _ndjson(
        tmp_path / OBSERVATIONS_NAME,
        {"meta": {"platform": "zoom", "segment": 0}},
        {"obs": {"tMs": 10718, "speaking": [1], "muted": []}},
        {"participant": {"pid": "p1", "name": OWNER, "self": False}},
    )
    read = read_transcript(path)

    assert read.meta == {"platform": "zoom", "segment": 0}
    assert len(read.others) == 2
    assert read.turns == []
    assert read.is_clean


def test_a_truncated_final_line_is_distinguished_from_corruption(
    tmp_path: Path,
) -> None:
    """An interrupted write is a normal way for a recording to end."""
    path = _ndjson(
        tmp_path / LIVE_NAME,
        REFINED_LINES[0],
        '{"id": "u-0002", "text": "cut off mid',
        trailing_newline=False,
    )
    read = read_transcript(path)

    assert len(read.turns) == 1
    assert read.malformed == 1
    assert read.truncated_tail
    assert not read.is_clean


def test_a_malformed_line_mid_file_is_counted_not_swallowed(
    tmp_path: Path,
) -> None:
    """Quietly reading less than exists is the failure that matters."""
    path = _ndjson(
        tmp_path / REFINED_NAME,
        REFINED_LINES[0],
        "{ this is not json",
        REFINED_LINES[1],
    )
    read = read_transcript(path)

    assert len(read.turns) == 2
    assert read.malformed == 1
    assert not read.truncated_tail


def test_blank_lines_are_ignored_without_being_counted(tmp_path: Path) -> None:
    """Trailing and interior blank lines are formatting, not data loss."""
    path = _ndjson(tmp_path / REFINED_NAME, REFINED_LINES[0], "", REFINED_LINES[1])
    read = read_transcript(path)

    assert read.lines == 2
    assert read.is_clean


def test_invalid_utf8_costs_one_character_not_the_meeting(
    tmp_path: Path,
) -> None:
    """A bad byte in a 16 MB transcript must not lose the whole file."""
    path = tmp_path / REFINED_NAME
    path.write_bytes(
        json.dumps(REFINED_LINES[0]).encode()
        + b"\n"
        + b'{"id": "u-9", "text": "caf\xff", "speaker": {"id": 1, "source": "refined"}}\n'
    )
    read = read_transcript(path)

    assert len(read.turns) == 2
    assert read.turns[1].text.startswith("caf")


def test_an_oversized_line_is_refused_not_loaded(tmp_path: Path) -> None:
    """A per-line cap keeps a hostile file from exhausting memory."""
    path = _ndjson(
        tmp_path / REFINED_NAME,
        REFINED_LINES[0],
        '{"text": "' + "x" * (MAX_LINE_BYTES + 10) + '"}',
    )
    read = read_transcript(path)

    assert len(read.turns) == 1
    assert read.malformed == 1


def test_a_json_array_line_is_not_a_turn(tmp_path: Path) -> None:
    """A line of the wrong type is counted, not coerced."""
    path = _ndjson(tmp_path / REFINED_NAME, [1, 2, 3], "null")
    read = read_transcript(path)

    assert read.turns == []
    assert read.malformed == 2


def test_an_absent_artifact_reads_as_empty(tmp_path: Path) -> None:
    """Two of three real meetings are missing at least one artifact."""
    assert read_transcript(None).turns == []
    assert read_transcript(tmp_path / "nope.ndjson").is_clean


def test_a_turn_with_no_speaker_object_still_reads(tmp_path: Path) -> None:
    """Speaker metadata is not guaranteed, and the words matter more."""
    path = _ndjson(
        tmp_path / REFINED_NAME, {"id": "u-1", "text": "anyone there?", "timestamp": "00:01"}
    )
    read = read_transcript(path)

    assert read.turns[0].text == "anyone there?"
    assert read.turns[0].speaker_id is None
    assert read.turns[0].label == "unknown"
