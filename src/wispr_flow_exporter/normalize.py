"""Every ambiguity in Wispr Flow's data, resolved in exactly one place.

Wispr Flow's store is easy to read and hard to read *correctly*. Four distinct
timestamp encodings appear across its tables, two of which defeat
``datetime.fromisoformat`` in opposite ways -- one raises, and the other
succeeds while silently dropping the time zone. Meeting summaries carry inline
speaker tokens that are meaningless without a separate map. The live and refined
transcripts number their speakers in disjoint spaces, and the live pass attaches
names that are demonstrably wrong. A single dictation is stored under six
competing renderings.

Every one of those is a way to produce an archive that looks complete and is
subtly false, which is worse than one that fails. So the parsing lives here,
behind named functions with the reasoning attached, rather than being
open-coded at each call site where the next reader would have to rediscover it.

This module holds behavior only. ``schema.py`` holds the declarations that say
which parser applies to which column, and imports ``TimestampKind`` from here.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

MAX_SLUG_LEN = 48
CALENDAR_KEY_LEN = 12

# Guard rails for epoch-millisecond columns. A seconds value passed here lands
# in 1970 and a microsecond value in the year 58000; both are far likelier than
# a genuine Wispr Flow timestamp outside this window.
_EPOCH_MS_MIN = 631_152_000_000  # 1990-01-01
_EPOCH_MS_MAX = 4_102_444_800_000  # 2100-01-01

# GoTrue and Wispr's own sync coordinator both use the epoch to mean "never".
_NEVER_SYNCED = datetime(1970, 1, 1, tzinfo=UTC)

# Inline mention tokens in Meetings.summary and Meetings.notes.
SPEAKER_TOKEN_RE = re.compile(r"<@speaker:(\d+)>")

# [HH:]MM:SS, zero-padded or not. Deliberately anchored and deliberately
# without an am/pm branch, so live.ndjson's wall-clock marker lines fail to
# match rather than being misread as an offset.
_CLOCK_RE = re.compile(r"^(?:(\d+):)?([0-5]?\d):([0-5]\d)$")

_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")

# Most processed first. The default*/fallback* columns record an A/B between
# two model paths; they are archived but never chosen as the canonical text.
DICTATION_CASCADE = (
    "serverFinalizedText",
    "editedTextUnbounded",
    "editedText",
    "toneMatchedText",
    "formattedText",
    "asrText",
)


class TimestampKind(StrEnum):
    """How a source column encodes an instant.

    Attributes:
        SEQUELIZE: ``"2026-08-20 20:02:23.308 +00:00"`` -- a space before the
            offset, which is not ISO 8601.
        EPOCH_MS: ``1787347929267`` -- unix milliseconds as an integer.
        BARE_ISO: ``"2026-08-20T20:34:28.645693"`` -- ISO with no zone, meaning
            UTC.
        ISO_Z: ``"2026-08-21T22:45:13.107782Z"``.
        CLOCK: ``"00:24"`` or ``"1:02:33"`` -- an offset, not an instant.
        WALL: ``"5:27 PM"`` -- a wall clock on live.ndjson marker lines,
            carrying no date and therefore no instant.
    """

    SEQUELIZE = "sequelize"
    EPOCH_MS = "epoch_ms"
    BARE_ISO = "bare_iso"
    ISO_Z = "iso_z"
    CLOCK = "clock"
    WALL = "wall"


def parse_sequelize(value: Any) -> datetime | None:
    """Parse Sequelize's TEXT DATETIME into an aware UTC datetime.

    ``datetime.fromisoformat`` rejects this format even on Python 3.13: the
    space before ``+00:00`` is not ISO 8601. The separator is removed before
    parsing. Handing ``Meetings.createdAt`` to a naive ISO parser raises, and
    handing it to a lenient one is worse, because it drops the zone.

    Args:
        value: The raw column value.

    Returns:
        An aware UTC datetime, or ``None`` when the value is absent or
        unparseable.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    # "2026-08-20 20:02:23.308 +00:00" -> "2026-08-20 20:02:23.308+00:00"
    text = re.sub(r"\s+([+-]\d{2}:?\d{2})$", r"\1", text)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def parse_epoch_ms(value: Any) -> datetime | None:
    """Parse a unix-millisecond integer into an aware UTC datetime.

    Args:
        value: The raw column value.

    Returns:
        An aware UTC datetime, or ``None`` when the value is absent, not
        numeric, or outside 1990..2100. Out-of-range values are rejected rather
        than converted, because they almost always mean the column is in
        seconds or microseconds and converting would produce a plausible-looking
        wrong date.
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        millis = int(value)
    except (TypeError, ValueError):
        return None
    if not _EPOCH_MS_MIN <= millis <= _EPOCH_MS_MAX:
        return None
    return datetime.fromtimestamp(millis / 1000, tz=UTC)


def parse_bare_iso(value: Any) -> datetime | None:
    """Parse a zone-less ISO timestamp, attaching UTC explicitly.

    ``serverRefinedUploadedAt`` parses cleanly with ``fromisoformat`` and yields
    a *naive* datetime, which every downstream comparison then silently treats
    as local time. Attaching the zone is the entire purpose of this function.

    Args:
        value: The raw column value.

    Returns:
        An aware UTC datetime, or ``None``.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def parse_iso_z(value: Any) -> datetime | None:
    """Parse an ISO timestamp with a ``Z`` suffix.

    The epoch is treated as "never synced" rather than as a real instant, so a
    sentinel is never mistaken for a watermark in 1970 that would then make
    every record look newer than the last run.

    Args:
        value: The raw column value.

    Returns:
        An aware UTC datetime, or ``None``.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    parsed = parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None if parsed == _NEVER_SYNCED else parsed


def parse_clock(value: Any) -> timedelta | None:
    """Parse a relative transcript offset into a duration.

    ``refined.ndjson`` zero-pads (``"00:24"``) and ``live.ndjson`` does not
    (``"0:25"``), and ``live.ndjson`` marker lines carry a wall clock
    (``"5:27 PM"``) in the very same field. Anything that is not ``[HH:]MM:SS``
    returns ``None``, which callers read as "this line is not a transcript
    turn" rather than as an error.

    Args:
        value: The raw ``timestamp`` field of an NDJSON line.

    Returns:
        The offset from the start of the recording, or ``None``.
    """
    if not isinstance(value, str):
        return None
    match = _CLOCK_RE.match(value.strip())
    if match is None:
        return None
    hours, minutes, seconds = match.groups()
    return timedelta(
        hours=int(hours or 0), minutes=int(minutes), seconds=int(seconds)
    )


_PARSERS = {
    TimestampKind.SEQUELIZE: parse_sequelize,
    TimestampKind.EPOCH_MS: parse_epoch_ms,
    TimestampKind.BARE_ISO: parse_bare_iso,
    TimestampKind.ISO_Z: parse_iso_z,
}


def to_instant(kind: TimestampKind, value: Any) -> datetime | None:
    """Normalize a source timestamp using its declared encoding.

    The single dispatcher. No caller guesses a format: ``schema.py`` declares a
    ``TimestampKind`` per column, so adding a column means adding a
    declaration rather than another parse attempt.

    Args:
        kind: The declared encoding of the column.
        value: The raw column value.

    Returns:
        An aware UTC datetime, or ``None``. ``CLOCK`` and ``WALL`` are offsets
        and wall clocks respectively, carry no date, and always yield ``None``
        here -- use :func:`parse_clock` for the former.
    """
    parser = _PARSERS.get(kind)
    return None if parser is None else parser(value)


@dataclass(frozen=True, slots=True)
class Person:
    """One identified participant from a meeting's speaker map.

    Attributes:
        person_id: The map's key for this person.
        name: Display name.
        origin: How they were identified, e.g. ``"self"`` or ``"calendar"``.
    """

    person_id: str
    name: str
    origin: str | None = None


@dataclass(slots=True)
class SpeakerMap:
    """A parsed ``Meetings.speakerMap``, covering the refined namespace only.

    Attributes:
        people: Person id to :class:`Person`.
        assignments: Refined diarization id to person id.
        raw: The value as parsed, retained so the archive keeps whatever shape
            upstream actually used.
    """

    people: dict[str, Person] = field(default_factory=dict)
    assignments: dict[int, str] = field(default_factory=dict)
    raw: Any = None

    @classmethod
    def parse(cls, value: Any) -> SpeakerMap:
        """Parse a speaker map from a column that may hold anything.

        Three shapes have been observed or are plausible: the current
        ``{"people": ..., "assignments": ...}`` object, a flat
        ``{"1": "Some Name"}`` mapping, and a bare string. Anything else --
        including a list where a mapping was expected -- yields an empty map
        rather than raising from several frames down inside a sync pass.

        Args:
            value: The raw ``speakerMap`` column, JSON text or already decoded.

        Returns:
            A populated or empty map. Never raises.
        """
        decoded = decode_json(value)
        if not isinstance(decoded, dict):
            return cls(raw=decoded)

        people: dict[str, Person] = {}
        assignments: dict[int, str] = {}

        raw_people = decoded.get("people")
        if isinstance(raw_people, dict):
            for person_id, entry in raw_people.items():
                if isinstance(entry, dict):
                    name = entry.get("name")
                    origin = entry.get("origin")
                elif isinstance(entry, str):
                    name, origin = entry, None
                else:
                    continue
                if isinstance(name, str) and name.strip():
                    people[str(person_id)] = Person(
                        person_id=str(person_id),
                        name=name.strip(),
                        origin=origin if isinstance(origin, str) else None,
                    )

        raw_assignments = decoded.get("assignments")
        if isinstance(raw_assignments, dict):
            for speaker_id, entry in raw_assignments.items():
                person_id = (
                    entry.get("consensus") if isinstance(entry, dict) else entry
                )
                if not isinstance(person_id, str):
                    continue
                try:
                    assignments[int(speaker_id)] = person_id
                except (TypeError, ValueError):
                    continue

        # Flat legacy shape: {"1": "Some Name"} with no people/assignments.
        if not people and not assignments:
            for speaker_id, name in decoded.items():
                if not isinstance(name, str) or not name.strip():
                    continue
                try:
                    index = int(speaker_id)
                except (TypeError, ValueError):
                    continue
                people[name] = Person(person_id=name, name=name.strip())
                assignments[index] = name

        return cls(people=people, assignments=assignments, raw=decoded)

    def name_for(self, refined_speaker_id: int) -> str | None:
        """Resolve a refined diarization id to a display name.

        Deliberately refuses live and observation ids: those occupy different
        numbering spaces, and joining them silently mislabels the transcript.

        Args:
            refined_speaker_id: A speaker id from ``refined.ndjson`` or from a
                ``<@speaker:N>`` token.

        Returns:
            The display name, or ``None`` when the id is unassigned.
        """
        person_id = self.assignments.get(refined_speaker_id)
        if person_id is None:
            return None
        person = self.people.get(person_id)
        return person.name if person else None


def resolve_speaker_tokens(text: str, speakers: SpeakerMap) -> tuple[str, int]:
    """Substitute ``<@speaker:N>`` tokens with resolved names.

    An id with no assignment is left as the literal token rather than replaced
    with a guess. A wrong name on an archived summary is worse than a visible
    unresolved marker, and the count is returned so ``verify`` can report it
    instead of the archive quietly carrying placeholders forever.

    Args:
        text: Markdown from ``Meetings.summary`` or ``Meetings.notes``.
        speakers: The parsed speaker map for that meeting.

    Returns:
        ``(rendered_text, unresolved_count)``.
    """
    if not text:
        return "", 0

    unresolved = 0

    def substitute(match: re.Match[str]) -> str:
        nonlocal unresolved
        name = speakers.name_for(int(match.group(1)))
        if name is None:
            unresolved += 1
            return match.group(0)
        return name

    return SPEAKER_TOKEN_RE.sub(substitute, text), unresolved


def label_live_speaker(speaker: Any) -> str:
    """Label a ``live.ndjson`` turn without inventing an identity.

    Live speaker ids occupy a different space from refined ones -- ``mic``
    numbers from 1 and ``system`` from 1001 -- and the ``name`` field is
    populated from the meeting platform's active-speaker marker, which lags. A
    verified line in the development dataset attributes one participant's words
    to the other. The label is therefore mechanical, and the platform's name is
    never used as an attribution.

    Args:
        speaker: The ``speaker`` object of a live transcript line.

    Returns:
        A label such as ``"mic#1"`` or ``"system#1001"``, or ``"unknown"``.
    """
    if not isinstance(speaker, dict):
        return "unknown"
    source = speaker.get("source")
    speaker_id = speaker.get("id")
    source_label = source if isinstance(source, str) and source.strip() else "unknown"
    if speaker_id is None:
        return source_label
    return f"{source_label}#{speaker_id}"


def resolve_dictation_text(row: Any) -> tuple[str, str | None]:
    """Pick the authoritative text for one ``History`` row.

    ``History`` carries six competing renderings of the same utterance, plus
    ``default*``/``fallback*`` pairs recording an A/B between two model paths.
    The pairs are archived verbatim but never chosen, because they describe how
    the text was produced rather than what was said.

    Args:
        row: A mapping of ``History`` column names to values.

    Returns:
        ``(text, provenance_column)``. ``("", None)`` when every candidate is
        empty, which is a real state for a dictation that failed.
    """
    if not isinstance(row, dict):
        return "", None
    for column in DICTATION_CASCADE:
        value = row.get(column)
        if isinstance(value, str) and value.strip():
            return value, column
    return "", None


def decode_json(value: Any) -> Any:
    """Decode a JSON-in-TEXT column, tolerating anything.

    Args:
        value: Raw column value, which may be JSON text, already-decoded data,
            or nothing at all.

    Returns:
        The decoded value, the input unchanged when it is not a string, or
        ``None`` when the text is not JSON.
    """
    if value is None or not isinstance(value, (str, bytes)):
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return None


def slugify(title: Any, *, max_len: int = MAX_SLUG_LEN) -> str:
    """Reduce a title to a filesystem-safe slug.

    Everything outside ``[a-z0-9]`` collapses to a hyphen, so a title that is
    ``".."``, an absolute path, or entirely punctuation cannot contribute a
    path component. The slug is never what makes a directory unique -- a
    validated id is -- so returning ``"untitled"`` for a degenerate title is
    safe rather than lossy.

    Args:
        title: The raw title.
        max_len: Maximum slug length.

    Returns:
        A slug of ``[a-z0-9-]``, or ``"untitled"``.
    """
    if not isinstance(title, str):
        return "untitled"
    # Decompose accents so "Café" slugifies to "cafe" rather than "caf".
    folded = unicodedata.normalize("NFKD", title)
    folded = "".join(char for char in folded if not unicodedata.combining(char))
    slug = _SLUG_STRIP_RE.sub("-", folded.lower()).strip("-")
    slug = slug[:max_len].strip("-")
    return slug or "untitled"


def calendar_key(external_id: Any) -> str:
    """Derive a stable, short, path-safe key from a calendar external id.

    ``CalendarEvents.externalId`` is the primary key and runs to 181 characters
    of base32 in practice. It cannot be a path component, and truncating it is
    not injective, so the key is a hash prefix instead.

    Args:
        external_id: The raw external id.

    Returns:
        A 12-character lowercase hex key.
    """
    text = external_id if isinstance(external_id, str) else repr(external_id)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:CALENDAR_KEY_LEN]
