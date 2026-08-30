"""Per-meeting transcript artifacts on disk.

Wispr Flow keeps a meeting's transcripts beside the database rather than in
it, one directory per meeting id holding up to four files. This module finds
them. The NDJSON readers that interpret them land in a later step; discovery
comes first because ``doctor`` has to be able to say what exists before
anything is archived.

The important thing discovery has to survive is that **the directories are not
uniform**. Of the three meetings on the machine this was developed against,
one had only ``refined.ndjson``, one had refined, live and observations but no
audio, and one had refined, live and audio but no observations. Wispr Flow
garbage-collects meeting audio, so a missing file is the normal case and not a
fault. Anything that assumed four files would be wrong about every meeting.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from .normalize import label_live_speaker, parse_clock

# These files are written by another application and are untrusted input.
# Reading a line at a time under a cap means a corrupted or hostile file costs
# a skipped record rather than the process.
MAX_LINE_BYTES = 2 * 1024 * 1024
MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_LINES = 2_000_000

REFINED_NAME = "refined.ndjson"
LIVE_NAME = "live.ndjson"
OBSERVATIONS_NAME = "speakers.observations.ndjson"
AUDIO_NAME = "upload.ogg"

ARTIFACT_NAMES = ("refined", "live", "observations", "audio")

# Meeting directories are named with a canonical lowercase UUID. Anything else
# in meetings/ is not a meeting -- notably the app's own .observations-tmp
# staging directory -- and validating the name here is also what keeps an
# unexpected entry from becoming a path component later.
MEETING_DIR_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


@dataclass(frozen=True, slots=True)
class MeetingArtifacts:
    """The transcript and audio files belonging to one meeting.

    Attributes:
        meeting_id: The directory name, a validated lowercase UUID.
        directory: The directory itself.
        refined: Cleaned diarized transcript, the canonical one.
        live: Real-time transcript, in a disjoint speaker-id space.
        observations: Diarization observation stream.
        audio: Source recording, which Wispr Flow deletes over time.
    """

    meeting_id: str
    directory: Path
    refined: Path | None = None
    live: Path | None = None
    observations: Path | None = None
    audio: Path | None = None

    @property
    def present(self) -> tuple[str, ...]:
        """Name the artifacts that exist.

        Returns:
            A subset of :data:`ARTIFACT_NAMES`, in a stable order.
        """
        found = {
            "refined": self.refined,
            "live": self.live,
            "observations": self.observations,
            "audio": self.audio,
        }
        return tuple(name for name in ARTIFACT_NAMES if found[name] is not None)

    def size_of(self, artifact: str) -> int:
        """Return an artifact's size in bytes.

        Args:
            artifact: One of :data:`ARTIFACT_NAMES`.

        Returns:
            The size, or ``0`` when the artifact is absent or unreadable.
        """
        path = getattr(self, artifact, None)
        if path is None:
            return 0
        try:
            return path.stat().st_size
        except OSError:
            return 0


def _artifact(directory: Path, name: str) -> Path | None:
    """Return a path when it is a readable regular file.

    Symlinks are refused rather than followed. A meeting directory is written
    by another application, and following a link out of it would let that
    application decide what this tool copies into the archive.

    Args:
        directory: The meeting directory.
        name: File name to look for.

    Returns:
        The path, or ``None``.
    """
    candidate = directory / name
    try:
        if candidate.is_symlink() or not candidate.is_file():
            return None
    except OSError:
        return None
    return candidate


def discover_meetings(meetings_dir: Path) -> Iterator[MeetingArtifacts]:
    """Find every meeting directory and the artifacts inside it.

    Args:
        meetings_dir: The ``meetings/`` directory beside the database.

    Yields:
        One :class:`MeetingArtifacts` per valid meeting directory, ordered by
        id so a run is reproducible. Directories whose names are not canonical
        UUIDs are skipped, which excludes the app's ``.observations-tmp``
        staging directory.
    """
    if not meetings_dir.is_dir():
        return
    try:
        entries = sorted(meetings_dir.iterdir())
    except OSError:
        return

    for entry in entries:
        if entry.is_symlink() or not entry.is_dir():
            continue
        if not MEETING_DIR_RE.match(entry.name):
            continue
        yield MeetingArtifacts(
            meeting_id=entry.name,
            directory=entry,
            refined=_artifact(entry, REFINED_NAME),
            live=_artifact(entry, LIVE_NAME),
            observations=_artifact(entry, OBSERVATIONS_NAME),
            audio=_artifact(entry, AUDIO_NAME),
        )


@dataclass(frozen=True, slots=True)
class Turn:
    """One spoken turn from a transcript.

    Attributes:
        turn_id: The line's own id.
        text: What was said.
        offset: Time from the start of the recording, when the line carried a
            parseable ``MM:SS`` offset.
        speaker_id: Diarization id, meaningful only within ``speaker_source``.
        speaker_source: ``"refined"``, ``"mic"`` or ``"system"``.
        label: A mechanical label for the live pass. Empty for refined turns,
            which are named from the meeting's speaker map instead.
        start_epoch_ms: Absolute start, present on live turns only.
        end_epoch_ms: Absolute end, present on live turns only.
        segment: Recording segment index, present on live turns only.
    """

    turn_id: str
    text: str
    offset: timedelta | None = None
    speaker_id: int | None = None
    speaker_source: str | None = None
    label: str = ""
    start_epoch_ms: int | None = None
    end_epoch_ms: int | None = None
    segment: int | None = None


@dataclass(frozen=True, slots=True)
class Marker:
    """A recording-state change, such as a pause.

    Attributes:
        marker: The state, for example ``"paused"``.
        epoch_ms: When it happened.
        raw: The line as parsed, since marker shapes are undocumented.
    """

    marker: str
    epoch_ms: int | None
    raw: dict[str, Any]


@dataclass(slots=True)
class Transcript:
    """The result of reading one NDJSON transcript.

    Attributes:
        turns: Spoken turns, in file order.
        markers: Recording-state changes.
        meta: The header object, when the file had one.
        others: Lines that parsed but are neither a turn nor a marker --
            observation and participant records, kept so the archive is
            lossless even where this tool has no renderer.
        malformed: Lines that could not be parsed. Counted rather than
            ignored: for an archival tool, quietly reading less than exists is
            the failure that matters.
        truncated_tail: Whether the final line was cut off mid-write, which is
            what an interrupted recording looks like on disk.
        lines: Total non-empty lines seen.
    """

    turns: list[Turn] = field(default_factory=list)
    markers: list[Marker] = field(default_factory=list)
    meta: dict[str, Any] | None = None
    others: list[dict[str, Any]] = field(default_factory=list)
    malformed: int = 0
    truncated_tail: bool = False
    lines: int = 0

    @property
    def is_clean(self) -> bool:
        """Report whether every line was understood.

        Returns:
            ``True`` when nothing was skipped or truncated.
        """
        return self.malformed == 0 and not self.truncated_tail


def _as_turn(payload: dict[str, Any]) -> Turn:
    """Build a turn from a parsed transcript line.

    The live pass carries a ``speaker.name`` taken from the meeting platform's
    active-speaker marker, and that marker lags: in the development dataset,
    141 of one meeting's 366 live lines carry a name, and a verified line
    attributes one participant's words to the other. It is therefore read for
    provenance and never used as an attribution -- live turns get a mechanical
    label instead, and names come only from the refined pass.

    Args:
        payload: A parsed line.

    Returns:
        The turn.
    """
    speaker = payload.get("speaker")
    speaker = speaker if isinstance(speaker, dict) else {}
    source = speaker.get("source")
    raw_id = speaker.get("id")
    speaker_id = raw_id if isinstance(raw_id, int) and not isinstance(raw_id, bool) else None

    return Turn(
        turn_id=str(payload.get("id", "")),
        text=payload.get("text") if isinstance(payload.get("text"), str) else "",
        offset=parse_clock(payload.get("timestamp")),
        speaker_id=speaker_id,
        speaker_source=source if isinstance(source, str) else None,
        # Refined turns resolve to real names via the meeting's speaker map,
        # so they carry no mechanical label.
        label="" if source == "refined" else label_live_speaker(speaker),
        start_epoch_ms=_as_int(payload.get("startEpochMs")),
        end_epoch_ms=_as_int(payload.get("endEpochMs")),
        segment=_as_int(payload.get("segment")),
    )


def _as_int(value: Any) -> int | None:
    """Coerce a value to an integer, or report that it is not one.

    Args:
        value: Any parsed JSON value.

    Returns:
        The integer, or ``None``.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def read_transcript(path: Path | None) -> Transcript:
    """Read one NDJSON transcript, surviving every defect seen on disk.

    One reader handles all three artifact kinds, because their line shapes are
    distinguishable rather than positional: ``refined.ndjson`` has no header
    and every line is a turn, ``live.ndjson`` opens with a ``meta`` object and
    mixes in ``marker`` lines, and the observations stream is entirely
    ``meta``, ``obs`` and ``participant`` records. Dispatching on the keys
    present means none of them needs a separate parser, and a fourth shape
    added upstream is preserved rather than dropped.

    A marker line puts a **wall clock** in the same ``timestamp`` field a turn
    uses for an ``MM:SS`` offset, which is what makes a naive parser crash
    here. ``parse_clock`` returns ``None`` for it and the line is classified by
    its ``marker`` key instead.

    Args:
        path: The file, or ``None`` when the artifact is absent.

    Returns:
        The parsed transcript. An absent file yields an empty result rather
        than an error: two of three meetings on the development machine were
        missing at least one artifact.
    """
    result = Transcript()
    if path is None or not path.is_file():
        return result
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            result.malformed = 1
            return result
        raw = path.read_bytes()
    except OSError:
        result.malformed = 1
        return result

    ends_newline = raw.endswith(b"\n")
    # Decoded with replacement rather than strictly: a single bad byte in a
    # 16 MB transcript should cost one character, not the whole meeting.
    lines = raw.decode("utf-8", errors="replace").split("\n")
    if ends_newline and lines:
        lines.pop()

    for number, line in enumerate(lines):
        if not line.strip():
            continue
        result.lines += 1
        if result.lines > MAX_LINES or len(line) > MAX_LINE_BYTES:
            result.malformed += 1
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            result.malformed += 1
            # A final unparseable line with no trailing newline is an
            # interrupted write, not corruption, and is worth distinguishing.
            if number == len(lines) - 1 and not ends_newline:
                result.truncated_tail = True
            continue
        if not isinstance(payload, dict):
            result.malformed += 1
            continue

        if "meta" in payload and len(payload) == 1:
            result.meta = payload["meta"] if isinstance(payload["meta"], dict) else None
        elif "marker" in payload:
            result.markers.append(
                Marker(
                    marker=str(payload.get("marker")),
                    epoch_ms=_as_int(payload.get("epochMs")),
                    raw=payload,
                )
            )
        elif isinstance(payload.get("text"), str):
            result.turns.append(_as_turn(payload))
        else:
            result.others.append(payload)

    return result


def inventory(meetings_dir: Path) -> dict[str, object]:
    """Summarize what the meetings directory holds, for ``doctor``.

    Args:
        meetings_dir: The ``meetings/`` directory.

    Returns:
        Directory count, a per-artifact present count, and total audio bytes.
    """
    found = list(discover_meetings(meetings_dir))
    counts = {
        name: sum(1 for item in found if name in item.present)
        for name in ARTIFACT_NAMES
    }
    return {
        "directories": len(found),
        "artifacts": counts,
        "audio_bytes": sum(item.size_of("audio") for item in found),
    }
