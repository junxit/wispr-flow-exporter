"""Timestamp, speaker and text normalization.

The two ``fromisoformat`` tests below are the headline assertions of this
module. Between them they cover the only two ways a timestamp can be wrong
without anything appearing to go wrong: raising on a format that looks ISO, and
succeeding on one that is ISO while dropping the time zone.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from wispr_flow_exporter.normalize import (
    SpeakerMap,
    TimestampKind,
    calendar_key,
    decode_json,
    label_live_speaker,
    parse_bare_iso,
    parse_clock,
    parse_epoch_ms,
    parse_iso_z,
    parse_sequelize,
    resolve_dictation_text,
    resolve_speaker_tokens,
    slugify,
    to_instant,
)

from conftest import MEETING_A, OWNER, SECOND, THIRD

# --- timestamps -----------------------------------------------------------


def test_sequelize_format_defeats_fromisoformat() -> None:
    """The stdlib parser rejects Meetings.createdAt outright.

    Confirmed on Python 3.13: the space before the offset is not ISO 8601, so
    ``fromisoformat`` raises. Any code that reaches for it directly crashes on
    the single most common timestamp in the database.
    """
    raw = "2026-08-20 20:02:23.308 +00:00"
    with pytest.raises(ValueError):
        datetime.fromisoformat(raw)

    parsed = parse_sequelize(raw)
    assert parsed == datetime(2026, 8, 20, 20, 2, 23, 308000, tzinfo=UTC)
    assert parsed.tzinfo is not None


def test_bare_iso_parses_naively_and_must_be_given_a_zone() -> None:
    """The stdlib parser accepts serverRefinedUploadedAt and drops the zone.

    This is the more dangerous of the two: nothing raises, and the resulting
    naive datetime is then treated as local time by every comparison
    downstream. Attaching UTC explicitly is the entire point of the wrapper.
    """
    raw = "2026-08-20T20:34:28.645693"
    assert datetime.fromisoformat(raw).tzinfo is None

    parsed = parse_bare_iso(raw)
    assert parsed == datetime(2026, 8, 20, 20, 34, 28, 645693, tzinfo=UTC)
    assert parsed.tzinfo is UTC


def test_sequelize_accepts_a_compact_offset() -> None:
    """An offset written without a colon still parses."""
    assert parse_sequelize("2026-08-20 20:02:23.308 +0000") == datetime(
        2026, 8, 20, 20, 2, 23, 308000, tzinfo=UTC
    )


def test_sequelize_normalizes_a_non_utc_offset() -> None:
    """A non-UTC offset is converted rather than truncated."""
    assert parse_sequelize("2026-08-20 15:02:23.000 -05:00") == datetime(
        2026, 8, 20, 20, 2, 23, tzinfo=UTC
    )


@pytest.mark.parametrize("value", [None, "", "   ", "not a date", 17, []])
def test_sequelize_rejects_junk(value: object) -> None:
    """Anything unparseable yields None rather than raising."""
    assert parse_sequelize(value) is None


def test_epoch_ms_parses_milliseconds() -> None:
    """endedAt is unix milliseconds, not seconds."""
    assert parse_epoch_ms(1787257992365) == datetime.fromtimestamp(
        1787257992.365, tz=UTC
    )


def test_epoch_ms_rejects_a_seconds_value() -> None:
    """A seconds value would land in 1970 and is refused instead.

    Converting it would produce a plausible-looking wrong date, which for an
    archive keyed on date shards means records filed under the wrong year.
    """
    assert parse_epoch_ms(1787257992) is None


def test_epoch_ms_rejects_a_microseconds_value() -> None:
    """A microsecond value would land in the year 58000 and is refused."""
    assert parse_epoch_ms(1787257992365000) is None


@pytest.mark.parametrize("value", [None, True, False, "abc", [], {}])
def test_epoch_ms_rejects_non_numeric(value: object) -> None:
    """Booleans are excluded explicitly, since bool is a subclass of int."""
    assert parse_epoch_ms(value) is None


def test_iso_z_parses_a_zulu_timestamp() -> None:
    """CalendarEvents.updatedAt is the fourth encoding: ISO with a Z."""
    assert parse_iso_z("2026-08-21T22:45:13.107782Z") == datetime(
        2026, 8, 21, 22, 45, 13, 107782, tzinfo=UTC
    )


def test_iso_z_maps_the_epoch_sentinel_to_none() -> None:
    """The epoch means "never synced" and must not become a 1970 watermark.

    A watermark at 1970 makes every record look newer than the last run, which
    turns an incremental sync into a full rewrite of the archive on every pass.
    """
    assert parse_iso_z("1970-01-01T00:00:00Z") is None


def test_clock_accepts_both_padded_and_unpadded_offsets() -> None:
    """refined.ndjson zero-pads and live.ndjson does not."""
    assert parse_clock("00:24") == timedelta(seconds=24)
    assert parse_clock("0:25") == timedelta(seconds=25)
    assert parse_clock("1:02:33") == timedelta(hours=1, minutes=2, seconds=33)


def test_clock_rejects_a_wall_clock_marker() -> None:
    """live.ndjson marker lines put "5:27 PM" in the timestamp field.

    A naive mm:ss parser crashes on this. Returning None lets the reader treat
    the line as a marker rather than a transcript turn.
    """
    assert parse_clock("5:27 PM") is None
    assert parse_clock("12:00 AM") is None


@pytest.mark.parametrize("value", [None, "", "abc", 24, "99:99:99"])
def test_clock_rejects_junk(value: object) -> None:
    """Anything that is not [HH:]MM:SS yields None."""
    assert parse_clock(value) is None


def test_to_instant_dispatches_on_the_declared_kind() -> None:
    """The dispatcher never guesses; the caller supplies the declared kind."""
    assert to_instant(TimestampKind.SEQUELIZE, "2026-08-20 20:02:23.308 +00:00")
    assert to_instant(TimestampKind.EPOCH_MS, 1787257992365)
    assert to_instant(TimestampKind.BARE_ISO, "2026-08-20T20:34:28.645693")
    assert to_instant(TimestampKind.ISO_Z, "2026-08-21T22:45:13.107782Z")


def test_to_instant_returns_none_for_offset_kinds() -> None:
    """CLOCK and WALL carry no date, so they are never instants."""
    assert to_instant(TimestampKind.CLOCK, "00:24") is None
    assert to_instant(TimestampKind.WALL, "5:27 PM") is None


# --- speakers -------------------------------------------------------------

PEOPLE_MAP = {
    "people": {
        "p-1": {"name": OWNER, "origin": "self"},
        "p-2": {"name": SECOND, "origin": "calendar"},
    },
    "assignments": {"1": {"consensus": "p-1"}, "2": {"consensus": "p-2"}},
}


def test_speaker_map_parses_the_current_shape() -> None:
    """people plus assignments resolves a refined id to a name."""
    speakers = SpeakerMap.parse(PEOPLE_MAP)
    assert speakers.name_for(1) == OWNER
    assert speakers.name_for(2) == SECOND
    assert speakers.people["p-1"].origin == "self"


def test_speaker_map_parses_json_text() -> None:
    """The column is TEXT, so the map usually arrives as a JSON string."""
    import json

    speakers = SpeakerMap.parse(json.dumps(PEOPLE_MAP))
    assert speakers.name_for(1) == OWNER


def test_speaker_map_parses_a_flat_legacy_shape() -> None:
    """A bare {id: name} mapping still resolves."""
    speakers = SpeakerMap.parse({"1": OWNER, "2": THIRD})
    assert speakers.name_for(1) == OWNER
    assert speakers.name_for(2) == THIRD


@pytest.mark.parametrize(
    "value",
    [None, "", "not json", [1, 2, 3], '["not", "a", "mapping"]', 42],
)
def test_speaker_map_survives_a_wrong_type(value: object) -> None:
    """A list where a mapping was expected is absent, not an exception.

    The column is untrusted input. Raising here would abort a whole sync pass
    several frames up, for one meeting with a malformed map.
    """
    speakers = SpeakerMap.parse(value)
    assert speakers.name_for(1) is None


def test_speaker_map_ignores_unassigned_ids() -> None:
    """An id with no assignment resolves to None rather than a wrong name."""
    assert SpeakerMap.parse(PEOPLE_MAP).name_for(9999) is None


def test_resolve_speaker_tokens_substitutes_names() -> None:
    """Summary mention tokens become readable names."""
    speakers = SpeakerMap.parse(PEOPLE_MAP)
    text, unresolved = resolve_speaker_tokens(
        "Intro call with <@speaker:2> about the whisper budget.", speakers
    )
    assert text == f"Intro call with {SECOND} about the whisper budget."
    assert unresolved == 0


def test_resolve_speaker_tokens_leaves_unresolvable_tokens_literal() -> None:
    """An unknown id keeps its token and is counted.

    A wrong name on an archived summary is worse than a visible marker, and the
    count is what lets verify report the gap instead of the archive quietly
    carrying placeholders.
    """
    speakers = SpeakerMap.parse(PEOPLE_MAP)
    text, unresolved = resolve_speaker_tokens("Then <@speaker:7> spoke.", speakers)
    assert text == "Then <@speaker:7> spoke."
    assert unresolved == 1


def test_resolve_speaker_tokens_handles_adjacent_and_repeated_tokens() -> None:
    """Multiple tokens in one string all resolve."""
    speakers = SpeakerMap.parse(PEOPLE_MAP)
    text, unresolved = resolve_speaker_tokens(
        "<@speaker:1><@speaker:2> and <@speaker:1>", speakers
    )
    assert text == f"{OWNER}{SECOND} and {OWNER}"
    assert unresolved == 0


def test_resolve_speaker_tokens_on_empty_text() -> None:
    """Meetings with no summary are common and are not an error."""
    assert resolve_speaker_tokens("", SpeakerMap.parse(PEOPLE_MAP)) == ("", 0)


def test_live_speaker_labels_are_mechanical() -> None:
    """The two live id spaces are labelled distinctly and never joined."""
    assert label_live_speaker({"id": 1, "source": "mic"}) == "mic#1"
    assert label_live_speaker({"id": 1001, "source": "system"}) == "system#1001"


def test_live_speaker_name_is_never_used_as_an_attribution() -> None:
    """The platform's active-speaker marker lags and is demonstrably wrong.

    A verified line in the development dataset carries a system-channel turn
    labelled with the *other* participant's name, because the meeting platform
    had not yet switched its marker. Using that name would put words in the
    wrong person's mouth, permanently, in an archive.
    """
    label = label_live_speaker({"id": 1001, "source": "system", "name": OWNER})
    assert OWNER not in label
    assert label == "system#1001"


@pytest.mark.parametrize("value", [None, "", 5, []])
def test_live_speaker_label_survives_junk(value: object) -> None:
    """A malformed speaker object yields "unknown", not an exception."""
    assert label_live_speaker(value) == "unknown"


# --- dictation ------------------------------------------------------------


def test_dictation_cascade_prefers_the_most_processed_text() -> None:
    """serverFinalizedText wins over every earlier stage."""
    row = {
        "asrText": "send the whisper budget to hush",
        "formattedText": "Send the whisper budget to Hush.",
        "editedText": "Send the Q3 whisper budget to Hush.",
        "serverFinalizedText": "Send the Q3 whisper budget to Hush before Friday.",
    }
    text, column = resolve_dictation_text(row)
    assert text == "Send the Q3 whisper budget to Hush before Friday."
    assert column == "serverFinalizedText"


def test_dictation_cascade_falls_back_through_every_stage() -> None:
    """With only raw ASR present, that is what is used, and it is recorded."""
    text, column = resolve_dictation_text({"asrText": "send the whisper budget"})
    assert text == "send the whisper budget"
    assert column == "asrText"


def test_dictation_cascade_ignores_the_ab_test_columns() -> None:
    """default* and fallback* describe how text was produced, not what was said."""
    row = {
        "fallbackAsrText": "wrong branch",
        "defaultFormattedText": "other branch",
        "formattedText": "Send the whisper budget.",
    }
    text, column = resolve_dictation_text(row)
    assert text == "Send the whisper budget."
    assert column == "formattedText"


def test_dictation_cascade_skips_blank_candidates() -> None:
    """An empty higher-priority column does not shadow a populated one."""
    row = {"serverFinalizedText": "   ", "editedText": "", "asrText": "hello"}
    assert resolve_dictation_text(row) == ("hello", "asrText")


def test_dictation_with_no_text_is_a_real_state() -> None:
    """A failed dictation has no text at all and must not raise."""
    assert resolve_dictation_text({}) == ("", None)
    assert resolve_dictation_text(None) == ("", None)


# --- helpers --------------------------------------------------------------


def test_decode_json_tolerates_everything() -> None:
    """A JSON-in-TEXT column may hold JSON, nothing, or garbage."""
    assert decode_json('{"a": 1}') == {"a": 1}
    assert decode_json(None) is None
    assert decode_json("not json") is None
    assert decode_json({"already": "decoded"}) == {"already": "decoded"}


def test_slugify_normal_titles() -> None:
    """Punctuation collapses and the result is lowercase and hyphenated."""
    assert slugify("Quarterly whisper budget: review") == "quarterly-whisper-budget-review"
    assert slugify("Murmur & Hush weekly") == "murmur-hush-weekly"


def test_slugify_folds_accents() -> None:
    """Accented characters fold rather than being dropped entirely."""
    assert slugify("Café sync") == "cafe-sync"


@pytest.mark.parametrize(
    "title",
    ["", "   ", "...", "../../../../etc/passwd", "/", None, 42, "---"],
)
def test_slugify_degenerate_titles_are_safe(title: object) -> None:
    """No title can contribute a traversal component or an empty name.

    The slug is never what makes a directory unique -- a validated id is -- so
    collapsing a hostile title to "untitled" is safe rather than lossy.
    """
    slug = slugify(title)
    assert slug
    assert "/" not in slug
    assert ".." not in slug


def test_slugify_truncates_long_titles() -> None:
    """Long titles are capped so the path component stays well under the limit."""
    slug = slugify("whisper " * 40)
    assert len(slug) <= 48
    assert not slug.endswith("-")


def test_calendar_key_is_short_stable_and_path_safe() -> None:
    """The 181-character base32 primary key becomes a 12-character hash.

    Truncating the id itself is not injective, so a hash prefix is the only
    stable short name that does not risk collapsing two events into one.
    """
    long_id = "q" * 181
    key = calendar_key(long_id)
    assert len(key) == 12
    assert key.isalnum()
    assert key == calendar_key(long_id)
    assert key != calendar_key(long_id + "r")


def test_calendar_key_handles_a_non_string_id() -> None:
    """A null or numeric external id still yields a usable key."""
    assert len(calendar_key(None)) == 12
    assert len(calendar_key(MEETING_A)) == 12
