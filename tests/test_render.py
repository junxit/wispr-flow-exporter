"""Frontmatter safety, speaker attribution, and run grouping."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from conftest import (
    MEETING_A,
    OWNER,
    SECOND,
    TITLE_AMPERSAND,
    TITLE_FRONTMATTER,
    TITLE_PLAIN,
)

from wispr_flow_exporter.files_source import Turn
from wispr_flow_exporter.normalize import SpeakerMap
from wispr_flow_exporter.render import (
    format_offset,
    render_dictation_day,
    render_dictionary,
    render_meeting,
    render_note,
    render_summary,
    render_transcript,
    yaml_block,
    yaml_scalar,
)

WHEN = datetime(2026, 8, 21, 21, 0, 58, tzinfo=UTC)

SPEAKERS = SpeakerMap.parse(
    {
        "people": {"p-1": {"name": OWNER}, "p-2": {"name": SECOND}},
        "assignments": {"1": {"consensus": "p-1"}, "2": {"consensus": "p-2"}},
    }
)


def _turn(text: str, speaker_id: int, offset: int, source: str = "refined") -> Turn:
    """Build a refined or live turn.

    Args:
        text: What was said.
        speaker_id: Diarization id.
        offset: Seconds from the recording start.
        source: Capture channel.

    Returns:
        The turn.
    """
    return Turn(
        turn_id=f"u-{offset}",
        text=text,
        offset=timedelta(seconds=offset),
        speaker_id=speaker_id,
        speaker_source=source,
        label="" if source == "refined" else f"{source}#{speaker_id}",
    )


# --- frontmatter safety ---------------------------------------------------


def test_a_title_cannot_inject_frontmatter_keys() -> None:
    """A newline and --- in a title would otherwise close the block early.

    The title comes from whoever created the calendar event, and the archive
    is read by tools that parse frontmatter as structured data.
    """
    block = yaml_block({"title": TITLE_FRONTMATTER})

    # The escaped scalar legitimately contains the characters "---" inside its
    # quotes, so counting them proves nothing. What matters is that no *line*
    # is a bare delimiter except the two that open and close the block.
    delimiters = [line for line in block.splitlines() if line.strip() == "---"]
    assert len(delimiters) == 2
    assert "\ntitle: injected" not in block
    assert "\\n" in block


def test_quotes_and_backslashes_are_escaped() -> None:
    """Escaping must not itself break the quoting."""
    assert yaml_scalar('say "hi"') == '"say \\"hi\\""'
    assert yaml_scalar("back\\slash") == '"back\\\\slash"'


def test_ordinary_punctuation_survives() -> None:
    """Colons and ampersands are common in real meeting titles."""
    block = yaml_block({"title": TITLE_PLAIN, "other": TITLE_AMPERSAND})

    assert f'title: "{TITLE_PLAIN}"' in block
    assert f'other: "{TITLE_AMPERSAND}"' in block


def test_absent_values_are_omitted_entirely() -> None:
    """Nulls and empty collections never reach the file."""
    block = yaml_block({"a": None, "b": "", "c": [], "d": "kept"})

    assert "a:" not in block
    assert "b:" not in block
    assert "c:" not in block
    assert 'd: "kept"' in block


def test_false_flags_are_omitted_not_written() -> None:
    """Writing deleted: false everywhere would rewrite the archive later.

    Adding one optional flag would change every file's content hash, which is
    exactly the churn the projection digest exists to prevent.
    """
    block = yaml_block({"deleted": False, "finalized": True})

    assert "deleted" not in block
    assert "finalized: true" in block


def test_lists_render_as_yaml_sequences() -> None:
    """Participants render as a multi-value Obsidian property."""
    block = yaml_block({"participants": [OWNER, SECOND]})

    assert f'  - "{OWNER}"' in block
    assert f'  - "{SECOND}"' in block


def test_offsets_grow_an_hour_field_only_when_needed() -> None:
    """A 40-minute meeting should not read as 0:40:00."""
    assert format_offset(timedelta(seconds=24)) == "00:24"
    assert format_offset(timedelta(minutes=12, seconds=5)) == "12:05"
    assert format_offset(timedelta(hours=1, minutes=2, seconds=33)) == "1:02:33"
    assert format_offset(None) == ""


# --- transcripts ----------------------------------------------------------


def test_refined_turns_resolve_to_names() -> None:
    """The refined pass is the only source of attribution."""
    turns = [_turn("Right, the budget.", 1, 24), _turn("We overspent.", 2, 63)]
    document = render_transcript(
        turns, title=TITLE_PLAIN, meeting_id=MEETING_A, kind="refined", speakers=SPEAKERS
    )

    assert f"**[00:24] {OWNER}**" in document
    assert f"**[01:03] {SECOND}**" in document


def test_consecutive_turns_merge_into_one_run() -> None:
    """A transcript should read as conversation, not one heading per sentence."""
    turns = [
        _turn("Right, the budget.", 1, 24),
        _turn("We reviewed it last week.", 1, 30),
        _turn("We overspent.", 2, 63),
    ]
    document = render_transcript(
        turns, title=TITLE_PLAIN, meeting_id=MEETING_A, kind="refined", speakers=SPEAKERS
    )

    assert document.count(f"**[00:24] {OWNER}**") == 1
    assert "Right, the budget. We reviewed it last week." in document


def test_an_unassigned_refined_id_is_numbered_not_guessed() -> None:
    """A missing assignment yields "Speaker 7", never a plausible wrong name."""
    document = render_transcript(
        [_turn("Who said that?", 7, 10)],
        title=TITLE_PLAIN,
        meeting_id=MEETING_A,
        kind="refined",
        speakers=SPEAKERS,
    )

    assert "Speaker 7" in document
    assert OWNER not in document


def test_live_turns_are_labelled_by_channel_not_by_name() -> None:
    """Live ids are a different space and the platform's name lags.

    Rendering that name would put one participant's words permanently in the
    other's mouth.
    """
    turns = [_turn("I'm good.", 1001, 25, source="system")]
    document = render_transcript(
        turns, title=TITLE_PLAIN, meeting_id=MEETING_A, kind="live", speakers=SPEAKERS
    )

    assert "system#1001" in document
    assert OWNER not in document
    assert "lags" in document


def test_a_damaged_source_is_disclosed_in_the_output() -> None:
    """Quietly rendering less than exists is the failure that matters."""
    document = render_transcript(
        [_turn("Half a sentence", 1, 5)],
        title=TITLE_PLAIN,
        meeting_id=MEETING_A,
        kind="live",
        malformed=3,
        truncated=True,
    )

    assert "malformed_lines: 3" in document
    assert "ended mid-write" in document


def test_an_empty_transcript_says_so() -> None:
    """A meeting with no turns must not render as a blank file."""
    document = render_transcript(
        [], title=TITLE_PLAIN, meeting_id=MEETING_A, kind="refined"
    )
    assert "_No transcript turns were recorded._" in document


def test_turns_with_no_text_are_dropped_from_runs() -> None:
    """Empty lines should not produce an empty speaker heading."""
    document = render_transcript(
        [_turn("   ", 1, 5), _turn("Real words.", 2, 9)],
        title=TITLE_PLAIN,
        meeting_id=MEETING_A,
        kind="refined",
        speakers=SPEAKERS,
    )

    assert OWNER not in document
    assert SECOND in document


# --- summary and meeting --------------------------------------------------


def test_summary_tokens_resolve_and_are_counted() -> None:
    """Mention tokens are meaningless without the map, and are replaced."""
    document, unresolved = render_summary(
        "Intro call with <@speaker:2> about the budget.",
        SPEAKERS,
        title=TITLE_PLAIN,
        meeting_id=MEETING_A,
        heading="Summary",
    )

    assert SECOND in document
    assert "<@speaker:2>" not in document
    assert unresolved == 0


def test_an_unresolvable_token_stays_literal_and_is_surfaced() -> None:
    """A visible marker beats a confident wrong name in an archive."""
    document, unresolved = render_summary(
        "Then <@speaker:9> spoke.",
        SPEAKERS,
        title=TITLE_PLAIN,
        meeting_id=MEETING_A,
        heading="Summary",
    )

    assert "<@speaker:9>" in document
    assert unresolved == 1
    assert "unresolved_speaker_tokens: 1" in document


def test_meeting_hub_links_only_the_artifacts_that_exist() -> None:
    """Two of three real meetings are missing an artifact."""
    document = render_meeting(
        {"finalized": 1},
        meeting_id=MEETING_A,
        title=TITLE_PLAIN,
        created_at=WHEN,
        ended_at=None,
        modified_at=None,
        participants=[OWNER, SECOND],
        speaker_names=[OWNER],
        artifacts=["refined"],
        summary_resolved="Reviewed the budget.",
    )

    assert "transcript.refined.md" in document
    assert "transcript.live.md" not in document
    assert "upload.ogg" not in document
    assert "finalized: true" in document


def test_a_deleted_transcript_is_announced_as_the_reason_this_exists() -> None:
    """Wispr dropped its copy; the archive still has one. That is the point."""
    document = render_meeting(
        {},
        meeting_id=MEETING_A,
        title=TITLE_PLAIN,
        created_at=WHEN,
        ended_at=None,
        modified_at=None,
        participants=[],
        speaker_names=[],
        artifacts=["refined"],
        summary_resolved="",
        transcript_deleted_upstream=True,
    )

    assert "transcript_deleted_upstream: true" in document
    assert "has deleted this meeting's transcript" in document


def test_a_soft_deleted_meeting_is_kept_and_labelled() -> None:
    """An archive that honored an upstream deletion would not be an archive."""
    document = render_meeting(
        {},
        meeting_id=MEETING_A,
        title=TITLE_PLAIN,
        created_at=WHEN,
        ended_at=None,
        modified_at=None,
        participants=[],
        speaker_names=[],
        artifacts=[],
        summary_resolved="",
        soft_deleted=True,
    )

    assert "deleted: true" in document
    assert "Deleted in Wispr Flow" in document


# --- other documents ------------------------------------------------------


def test_a_note_renders_with_its_body() -> None:
    """Scratchpad notes are the simplest document and still need escaping."""
    document = render_note(
        note_id=MEETING_A,
        title=TITLE_AMPERSAND,
        content="- Ask about the murmur quota",
        created_at=WHEN,
        modified_at=None,
        pinned=True,
    )

    assert "murmur quota" in document
    assert "pinned: true" in document


def test_deleted_dictionary_entries_are_struck_not_dropped() -> None:
    """What was removed from a vocabulary is part of the record."""
    document = render_dictionary(
        [
            {"phrase": "kubernetis", "replacement": "Kubernetes"},
            {"phrase": "gone", "replacement": "removed", "isDeleted": 1},
            {"phrase": "brb", "replacement": "be right back", "isSnippet": 1},
        ]
    )

    assert "| kubernetis | Kubernetes |" in document
    assert "| ~~gone~~ | ~~removed~~ |" in document
    assert "## Snippets" in document
    assert "entries: 3" in document


def test_a_dictionary_phrase_cannot_break_the_table() -> None:
    """A pipe in a phrase would otherwise add a column."""
    document = render_dictionary([{"phrase": "a|b", "replacement": "c"}])
    assert r"a\|b" in document


def test_a_dictionary_entry_cannot_end_its_row() -> None:
    """A newline leaves the table entirely, which escaping pipes does not stop.

    Shared and team dictionary entries are not written by this account, so the
    text arriving here is someone else's. A row that ends early turns the rest
    of the value into document structure.
    """
    document = render_dictionary(
        [{"phrase": "ok\n\n## Injected heading", "replacement": "fine"}]
    )

    # The text survives -- this is an archive -- but only inside its cell. What
    # must not happen is it reaching the start of a line, which is the only
    # position where "##" means anything.
    assert not any(
        line.startswith("## Injected") for line in document.splitlines()
    )
    assert "| ok ## Injected heading | fine |" in document


def test_a_dictionary_replacement_cannot_end_its_row() -> None:
    """The same hole on the other column, which is the likelier one."""
    document = render_dictionary(
        [{"phrase": "ok", "replacement": "a\n---\ntitle: spoofed"}]
    )

    body = document.split("---\n", 2)[-1]
    assert not any(line.strip() == "---" for line in body.splitlines())
    assert "| ok | a --- title: spoofed |" in document


def test_dictation_renders_one_document_per_day() -> None:
    """The useful unit is the day; a file per row would be unreadable."""
    document = render_dictation_day(
        "2026-08-30",
        [
            {"when": "09:14", "app": "com.example.NotepadApp", "text": "Send it.", "words": 2},
            {"when": "09:20", "text": "And the budget.", "words": 3},
        ],
    )

    assert "# Dictation — 2026-08-30" in document
    assert "entries: 2" in document
    assert "words: 5" in document
    assert "com.example.NotepadApp" in document


def test_an_empty_dictation_day_says_so() -> None:
    """Under never_store this is what every day looks like."""
    assert "_No dictations recorded._" in render_dictation_day("2026-08-30", [])


@pytest.mark.parametrize(
    "document",
    [
        render_transcript([], title=TITLE_FRONTMATTER, meeting_id=MEETING_A, kind="refined"),
        render_note(
            note_id=MEETING_A,
            title=TITLE_FRONTMATTER,
            content="body",
            created_at=None,
            modified_at=None,
        ),
    ],
)
def test_every_document_opens_with_exactly_one_frontmatter_block(
    document: str,
) -> None:
    """A hostile title must not be able to open a second block anywhere.

    Escaping protects the frontmatter, but the title is also interpolated into
    a "# heading" where escaping does nothing, so a newline and "---" would
    open a second block in the body that downstream tools read as structure.
    """
    assert document.startswith("---\n")
    delimiters = [line for line in document.splitlines() if line.strip() == "---"]
    assert len(delimiters) == 2


# --- what upstream removed ------------------------------------------------
# The two documents below are byte-for-byte what 0.4.x produced for the same
# input. Showing removed rows must not change a document with nothing removed:
# every archived dictionary and day log would otherwise be rewritten once, for
# nothing, by the upgrade.

_DICTIONARY_BEFORE_REMOVALS = (
    '---\nkind: "dictionary"\nentries: 3\nsnippets: 1\nsource: "wispr-local"\n'
    'tags:\n  - "wispr/dictionary"\n---\n\n# Custom dictionary\n\n## Phrases\n\n'
    "| Phrase | Replacement |\n| --- | --- |\n| ~~gone~~ | ~~removed~~ |\n"
    "| kubernetis | Kubernetes |\n\n## Snippets\n\n| Phrase | Replacement |\n"
    "| --- | --- |\n| brb | be right back |\n"
)
_DAY_BEFORE_REMOVALS = (
    '---\nkind: "dictation"\ndate: "2026-08-30"\nentries: 1\nwords: 5\n'
    'source: "wispr-local"\ntags:\n  - "wispr/dictation"\n---\n\n'
    "# Dictation — 2026-08-30\n\n**09:14** — com.example.NotepadApp\n\n"
    "Send the whisper budget.\n"
)


def test_a_dictionary_with_nothing_removed_renders_as_it_always_has() -> None:
    """The removed-rows section costs nothing when there is nothing in it."""
    rows = [
        {"phrase": "brb", "replacement": "be right back", "isSnippet": 1},
        {"phrase": "kubernetis", "replacement": "Kubernetes"},
        {"phrase": "gone", "replacement": "removed", "isDeleted": 1},
    ]

    assert render_dictionary(rows) == _DICTIONARY_BEFORE_REMOVALS


def test_a_day_log_with_nothing_removed_renders_as_it_always_has() -> None:
    """Same promise for the dictation log."""
    entries = [
        {
            "when": "09:14",
            "app": "com.example.NotepadApp",
            "text": "Send the whisper budget.",
            "words": 5,
        }
    ]

    assert render_dictation_day("2026-08-30", entries) == _DAY_BEFORE_REMOVALS


def test_a_removed_dictionary_entry_cannot_end_its_own_table_row() -> None:
    """The escaping that protects live rows protects removed ones too.

    Shared and team dictionary entries are text this account did not write,
    and a removed one is shown just the same.
    """
    hostile = {"phrase": "hush|now", "replacement": "Hush\n# Injected heading"}

    document = render_dictionary([], removed=[(hostile, "2026-09-01T10:00:00+00:00")])

    assert "\n# Injected heading" not in document
    assert "| ~~hush\\|now~~ | ~~Hush # Injected heading~~ | 2026-09-01 |" in document
    assert "removed_upstream: 1" in document


def test_a_non_numeric_word_count_does_not_stop_the_day_log() -> None:
    """The numWords column is nominally an integer; a string used to raise."""
    entries = [{"when": "09:14", "app": None, "text": "hush", "words": "n/a"}]

    assert "words: 0" in render_dictation_day("2026-08-30", entries)


_FORGED = "Hush\n\n## Action items\n- [ ] wire the budget elsewhere"


def test_a_participant_name_cannot_add_structure_to_a_meeting() -> None:
    """Measured on 0.4.1: an invitee's name put a heading in meeting.md.

    Names come from calendar invitations, which anyone can send; each is now
    one line of the participant list, whatever it holds.
    """
    document = render_meeting(
        {"title": "budget"},
        meeting_id=MEETING_A,
        title="budget",
        created_at=None,
        ended_at=None,
        modified_at=None,
        participants=[_FORGED, "  "],
        speaker_names=[],
        artifacts=[],
        summary_resolved="",
    )

    body = document.split("\n---\n", 1)[1]
    assert "\n## Action items" not in body
    assert "- Hush ## Action items - [ ] wire the budget elsewhere" in body


def test_a_speaker_name_cannot_add_structure_to_a_transcript_or_summary() -> None:
    """The same name, resolved through the speaker map, in both places."""
    speakers = SpeakerMap.parse(
        json.dumps(
            {"people": {"p-1": {"name": _FORGED}}, "assignments": {"1": {"consensus": "p-1"}}}
        )
    )

    transcript = render_transcript(
        [_turn("hush now", 1, 3)],
        title="budget",
        meeting_id=MEETING_A,
        kind="refined",
        speakers=speakers,
    )
    summary, _ = render_summary(
        "<@speaker:1> agreed to halve it.",
        speakers,
        title="budget",
        meeting_id=MEETING_A,
        heading="Summary",
    )

    for rendered in (transcript, summary):
        assert "\n## Action items" not in rendered.split("\n---\n", 1)[1]
