"""Markdown rendering, with frontmatter that cannot be injected into.

The archive targets Obsidian, so every rendered file opens with YAML
frontmatter. That makes titles dangerous: a meeting title is supplied by
whoever created the calendar event, and one containing a newline followed by
``---`` would close the frontmatter block early and inject arbitrary keys into
a file other tools then parse as structured data. Every scalar emitted here is
quoted and escaped rather than interpolated, which is why the block is built by
hand instead of by a YAML library -- a library would round-trip *our* values
correctly while giving no guarantee about the exact quoting we need.

The second rule is that a key whose value is absent is **omitted entirely**,
never written as ``null`` or ``false``. The archive is rewritten only when its
content hash changes, so emitting ``deleted: false`` on every meeting would mean
that adding one optional flag later rewrites every file in the archive. Flags
appear only when true.

Rendering is always downstream of the raw payload, which is written first. A
bug fixed here is repaired by re-rendering from what is already on disk, with
no source access at all -- which is what ``wispr-export render`` exists for.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from .files_source import Turn
from .normalize import SpeakerMap, resolve_speaker_tokens

# Consecutive turns by one speaker are merged into a run, so a transcript
# reads as conversation rather than as one heading per sentence.
UNKNOWN_SPEAKER = "Unknown speaker"


def yaml_scalar(value: Any) -> str:
    """Render one value as a safely quoted YAML scalar.

    Strings are always double-quoted, even when they would not need to be.
    Unquoted YAML would let a title beginning with ``-`` or containing ``: ``
    change the document's structure, and deciding case by case is exactly the
    kind of judgement that goes wrong on the one input that matters.

    Args:
        value: The value to render.

    Returns:
        A YAML scalar.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, datetime):
        return f'"{value.isoformat()}"'
    text = str(value)
    escaped = (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def yaml_block(fields: Mapping[str, Any]) -> str:
    """Build a frontmatter block, omitting everything absent.

    A key is dropped when its value is ``None``, an empty string, an empty
    sequence, or ``False``. Dropping ``False`` is deliberate rather than
    sloppy: writing ``deleted: false`` into every meeting would mean that
    introducing one new flag later rewrites the entire archive.

    Args:
        fields: Frontmatter keys in the order they should appear.

    Returns:
        The block, delimited by ``---`` lines and ending in a newline.
    """
    lines = ["---"]
    for key, value in fields.items():
        if value is None or value is False or value == "" or value == []:
            continue
        if isinstance(value, (list, tuple)):
            lines.append(f"{key}:")
            lines.extend(f"  - {yaml_scalar(item)}" for item in value)
        else:
            lines.append(f"{key}: {yaml_scalar(value)}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def inline(text: Any, *, fallback: str = "") -> str:
    """Flatten a value for use inside a Markdown line.

    Quoting protects the frontmatter block, but a title is also interpolated
    into a ``# heading``, and there escaping does nothing -- a title carrying a
    newline and ``---`` would open a *second* frontmatter block in the body,
    which downstream parsers read as structure. Every whitespace run collapses
    to a single space so a title can only ever occupy one line.

    Args:
        text: The value to flatten.
        fallback: Returned when the value is empty or not a string.

    Returns:
        A single-line string.
    """
    if not isinstance(text, str):
        return fallback
    flattened = " ".join(text.split())
    return flattened or fallback


def format_offset(offset: timedelta | None) -> str:
    """Format a transcript offset for a run heading.

    Args:
        offset: Time from the start of the recording.

    Returns:
        ``MM:SS``, or ``H:MM:SS`` past an hour, or an empty string when the
        line carried no parseable offset.
    """
    if offset is None:
        return ""
    total = int(offset.total_seconds())
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def _speaker_name(turn: Turn, speakers: SpeakerMap | None) -> str:
    """Choose the display name for one turn.

    Refined turns resolve through the meeting's speaker map. Live turns never
    do: their ids are a different space, and the name the platform attaches to
    them is demonstrably misattributed.

    Args:
        turn: The turn.
        speakers: The meeting's speaker map, for refined turns.

    Returns:
        A display name or a mechanical label.
    """
    if turn.speaker_source == "refined" and speakers is not None:
        if turn.speaker_id is not None:
            resolved = speakers.name_for(turn.speaker_id)
            if resolved:
                return resolved
            return f"Speaker {turn.speaker_id}"
        return UNKNOWN_SPEAKER
    return turn.label or UNKNOWN_SPEAKER


def _runs(
    turns: Sequence[Turn], speakers: SpeakerMap | None
) -> list[tuple[str, timedelta | None, list[str]]]:
    """Group consecutive turns by the same speaker.

    Args:
        turns: Turns in file order.
        speakers: The meeting's speaker map.

    Returns:
        ``(name, offset_of_first_turn, texts)`` per run.
    """
    runs: list[tuple[str, timedelta | None, list[str]]] = []
    for turn in turns:
        name = _speaker_name(turn, speakers)
        text = turn.text.strip()
        if not text:
            continue
        if runs and runs[-1][0] == name:
            runs[-1][2].append(text)
            continue
        runs.append((name, turn.offset, [text]))
    return runs


def render_transcript(
    turns: Sequence[Turn],
    *,
    title: str,
    meeting_id: str,
    kind: str,
    speakers: SpeakerMap | None = None,
    malformed: int = 0,
    truncated: bool = False,
) -> str:
    """Render a transcript as speaker-labelled runs.

    Args:
        turns: Turns in file order.
        title: The meeting title, for frontmatter.
        meeting_id: The meeting id.
        kind: ``"refined"`` or ``"live"``.
        speakers: The speaker map, used for refined turns only.
        malformed: Lines that could not be parsed, surfaced rather than hidden.
        truncated: Whether the source ended mid-write.

    Returns:
        The Markdown document.
    """
    runs = _runs(turns, speakers)
    front = yaml_block(
        {
            "id": meeting_id,
            "title": title,
            "transcript": kind,
            "turns": len(turns),
            "speakers": sorted({name for name, _, _ in runs}),
            "malformed_lines": malformed or None,
            "truncated": truncated,
            "tags": [f"wispr/transcript/{kind}"],
        }
    )

    body = [f"# {inline(title, fallback='Untitled meeting')} — {kind} transcript", ""]
    if kind == "live":
        body += [
            "> Live-pass transcript, kept verbatim. Speakers are labelled by",
            "> capture channel (`mic`, `system`) rather than by name: the live",
            "> pass numbers speakers in a different space from the refined one,",
            "> and the name the meeting platform attaches to a live turn lags",
            "> the actual speaker. Use the refined transcript for attribution.",
            "",
        ]
    if truncated or malformed:
        body += [
            f"> Source had {malformed} unparseable line(s)"
            + (" and ended mid-write." if truncated else "."),
            "",
        ]

    for name, offset, texts in runs:
        stamp = format_offset(offset)
        heading = f"**[{stamp}] {name}**" if stamp else f"**{name}**"
        body.append(heading)
        body.append("")
        body.append(" ".join(texts))
        body.append("")

    if not runs:
        body += ["_No transcript turns were recorded._", ""]

    return front + "\n" + "\n".join(body).rstrip() + "\n"


def render_summary(
    text: str,
    speakers: SpeakerMap,
    *,
    title: str,
    meeting_id: str,
    heading: str,
) -> tuple[str, int]:
    """Render a meeting summary or note body with speaker tokens resolved.

    Args:
        text: Markdown carrying ``<@speaker:N>`` tokens.
        speakers: The meeting's speaker map.
        title: The meeting title.
        meeting_id: The meeting id.
        heading: Document heading, such as ``"Summary"``.

    Returns:
        ``(document, unresolved_token_count)``.
    """
    resolved, unresolved = resolve_speaker_tokens(text or "", speakers)
    front = yaml_block(
        {
            "id": meeting_id,
            "title": title,
            "kind": heading.lower(),
            "unresolved_speaker_tokens": unresolved or None,
            "tags": [f"wispr/{heading.lower()}"],
        }
    )
    body = f"# {inline(title, fallback='Untitled meeting')} — {heading}\n\n{resolved.strip()}\n"
    return front + "\n" + body, unresolved


def render_meeting(
    data: Mapping[str, Any],
    *,
    meeting_id: str,
    title: str,
    created_at: datetime | None,
    ended_at: datetime | None,
    modified_at: datetime | None,
    participants: Sequence[str],
    speaker_names: Sequence[str],
    artifacts: Sequence[str],
    summary_resolved: str,
    soft_deleted: bool = False,
    transcript_deleted_upstream: bool = False,
    unresolved_tokens: int = 0,
) -> str:
    """Render the hub document that links a meeting's other files.

    Args:
        data: The raw meeting row, for the few fields surfaced inline.
        meeting_id: The meeting id.
        title: The meeting title.
        created_at: Recording start.
        ended_at: Recording end.
        modified_at: Last upstream modification.
        participants: Names from ``participantNames``.
        speaker_names: Names resolved from the speaker map.
        artifacts: Which transcript artifacts exist.
        summary_resolved: The summary with speaker tokens already resolved.
        soft_deleted: Whether upstream has tombstoned the meeting.
        transcript_deleted_upstream: Whether Wispr Flow deleted a transcript
            this archive still holds.
        unresolved_tokens: Speaker tokens that could not be resolved.

    Returns:
        The Markdown document.
    """
    front = yaml_block(
        {
            "id": meeting_id,
            "title": title,
            "aliases": [title] if title else [],
            "created_at": created_at,
            "ended_at": ended_at,
            "modified_at": modified_at,
            "participants": list(participants),
            "speakers": list(speaker_names),
            "calendar_event": data.get("calendarEventExternalId"),
            "share_visibility": data.get("shareVisibility"),
            "import_source": data.get("importSource"),
            "refine_status": data.get("refineStatus"),
            "artifacts": list(artifacts),
            "finalized": bool(data.get("finalized")),
            "deleted": soft_deleted,
            "transcript_deleted_upstream": transcript_deleted_upstream,
            "unresolved_speaker_tokens": unresolved_tokens or None,
            "source": "wispr-local",
            "tags": ["wispr/meeting"],
        }
    )

    lines = [f"# {inline(title, fallback='Untitled meeting')}", ""]
    if created_at:
        when = created_at.strftime("%A, %d %B %Y at %H:%M UTC")
        lines += [f"_{when}_", ""]

    if transcript_deleted_upstream:
        lines += [
            "> **Wispr Flow has deleted this meeting's transcript.** The copy in",
            "> this archive is the remaining one.",
            "",
        ]
    if soft_deleted:
        lines += [
            "> Deleted in Wispr Flow. Kept here: an archive that honored an",
            "> upstream deletion would not be an archive.",
            "",
        ]

    if participants:
        lines += ["## Participants", ""]
        lines += [f"- {name}" for name in participants]
        lines.append("")

    if summary_resolved.strip():
        lines += ["## Summary", "", summary_resolved.strip(), ""]

    notes = data.get("notes")
    if isinstance(notes, str) and notes.strip():
        lines += ["## Notes", "", "See [`notes.md`](notes.md).", ""]

    if artifacts:
        lines += ["## Transcripts", ""]
        if "refined" in artifacts:
            lines.append("- [`transcript.refined.md`](transcript.refined.md) — canonical, speakers named")
        if "live" in artifacts:
            lines.append("- [`transcript.live.md`](transcript.live.md) — verbatim live pass, channel-labelled")
        if "audio" in artifacts:
            lines.append("- [`media/upload.ogg`](media/upload.ogg) — source recording")
        lines.append("")

    lines += ["## Raw", "", "Verbatim source payloads are in [`raw/`](raw/).", ""]
    return front + "\n" + "\n".join(lines).rstrip() + "\n"


def render_note(
    *,
    note_id: str,
    title: str,
    content: str,
    created_at: datetime | None,
    modified_at: datetime | None,
    pinned: bool = False,
    soft_deleted: bool = False,
) -> str:
    """Render one scratchpad note.

    Args:
        note_id: The note id.
        title: The note title.
        content: The note body.
        created_at: Creation time.
        modified_at: Last modification.
        pinned: Whether the note is pinned.
        soft_deleted: Whether upstream has tombstoned it.

    Returns:
        The Markdown document.
    """
    front = yaml_block(
        {
            "id": note_id,
            "title": title,
            "aliases": [title] if title else [],
            "created_at": created_at,
            "modified_at": modified_at,
            "pinned": pinned,
            "deleted": soft_deleted,
            "source": "wispr-local",
            "tags": ["wispr/note"],
        }
    )
    body = [f"# {inline(title, fallback='Untitled note')}", ""]
    if soft_deleted:
        body += ["> Deleted in Wispr Flow. Kept here.", ""]
    body += [(content or "").strip(), ""]
    return front + "\n" + "\n".join(body).rstrip() + "\n"


def render_dictionary(rows: Iterable[Mapping[str, Any]]) -> str:
    """Render the custom dictionary as a readable table.

    Deleted entries are kept and struck through rather than dropped. The
    dictionary is largely names, employers and project codenames, and what was
    removed from it is part of the record.

    Args:
        rows: Dictionary rows.

    Returns:
        The Markdown document.
    """
    entries = list(rows)
    snippets = [row for row in entries if row.get("isSnippet")]
    phrases = [row for row in entries if not row.get("isSnippet")]

    front = yaml_block(
        {
            "kind": "dictionary",
            "entries": len(entries),
            "snippets": len(snippets),
            "source": "wispr-local",
            "tags": ["wispr/dictionary"],
        }
    )

    def table(items: Sequence[Mapping[str, Any]], heading: str) -> list[str]:
        if not items:
            return []
        lines = [f"## {heading}", "", "| Phrase | Replacement |", "| --- | --- |"]
        for row in sorted(items, key=lambda item: str(item.get("phrase", "")).lower()):
            phrase = str(row.get("phrase", ""))
            replacement = str(row.get("replacement") or "")
            if row.get("isDeleted"):
                phrase, replacement = f"~~{phrase}~~", f"~~{replacement}~~"
            lines.append(
                f"| {phrase.replace('|', '\\|')} | {replacement.replace('|', '\\|')} |"
            )
        lines.append("")
        return lines

    body = ["# Custom dictionary", ""]
    body += table(phrases, "Phrases")
    body += table(snippets, "Snippets")
    return front + "\n" + "\n".join(body).rstrip() + "\n"


def render_dictation_day(
    day: str, entries: Sequence[Mapping[str, Any]]
) -> str:
    """Render one day of dictation history as a log.

    A per-row document would be noise: the useful unit of dictation history is
    the day, and a heavy user produces thousands of rows in one.

    Args:
        day: The ``YYYY-MM-DD`` the entries belong to.
        entries: ``{"text", "app", "when", "provenance"}`` per dictation.

    Returns:
        The Markdown document.
    """
    front = yaml_block(
        {
            "kind": "dictation",
            "date": day,
            "entries": len(entries),
            "words": sum(int(entry.get("words") or 0) for entry in entries),
            "source": "wispr-local",
            "tags": ["wispr/dictation"],
        }
    )
    body = [f"# Dictation — {day}", ""]
    for entry in entries:
        stamp = entry.get("when") or ""
        app = entry.get("app")
        heading = f"**{stamp}**" + (f" — {app}" if app else "")
        body += [heading, "", str(entry.get("text", "")).strip(), ""]
    if not entries:
        body += ["_No dictations recorded._", ""]
    return front + "\n" + "\n".join(body).rstrip() + "\n"
