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

import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

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
