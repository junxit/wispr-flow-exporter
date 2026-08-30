"""Archiving what the MCP server has and the disk no longer does.

Two jobs, and the second is the reason this backend exists.

**Verbatim.** Every tool response is archived under ``mcp/``, content-addressed,
for the same reason the REST responses are: the shapes are not a contract.

**Gap-fill.** Wispr Flow garbage-collects meeting artifacts -- on the machine
this tool was developed against, only one of three meetings still had its
recording. MCP still serves the transcript. So where the archive holds no
transcript for a meeting and the server does, this pass fetches it and writes it
alongside the local files as ``transcript.mcp.md``.

It is a *lower fidelity* source and is treated as one. MCP returns normalized
plaintext: no per-turn speaker attribution, no timestamps, where the local
NDJSON has both. So local always wins where local has anything at all, the
decision is made on what is actually on disk rather than on what the index
claims, and the MCP rendering is a sibling file that never replaces a local one.

**Two ownership rules make a third writer safe**, and everything here follows
from them:

1. This pass never creates a key under ``entities["meetings"]``, and writes
   exactly one field into an existing one: the reserved ``"mcp"`` sub-key. It
   never touches ``path``, ``content_hash``, ``archived_at`` or ``source`` --
   the four fields that are last-writer-wins in ``Archive.put``.
2. Every file it writes carries an ``mcp`` path component or an ``.mcp.md``
   suffix. No file in the archive has two writers.

Together those make ``_archive_meeting``'s up-to-date check invariant under this
pass, which is what stops the two backends rewriting each other's work forever.
There is a test asserting exactly that, because the rules are otherwise only a
convention and the symptom of breaking them -- every meeting rewritten every
run -- is the kind of thing nobody notices.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from .files_source import MEETING_DIR_RE, read_transcript
from .mcp_api import PAGE_SIZE, TRANSCRIPT_CHARS, McpProtocol
from .secure_io import write_json_if_changed, write_ndjson_if_changed, write_text_if_changed
from .store import Archive, dated_prefix, record_dir_name
from .sync import SyncCounts, SyncOptions, _now

SOURCE_MCP = "wispr-mcp"

#: Entity namespaces this pass owns outright.
ENTITY_MCP = "mcp"
ENTITY_MCP_MEETINGS = "mcp_meetings"

#: A response that reports more records than it returned.
_MORE_FLAGS = ("has_more", "hasMore")
_CURSOR_FLAGS = ("next_cursor", "nextCursor")

#: Refuse to assemble a transcript larger than this. Untrusted remote input
#: gets a cap for the same reason the NDJSON reader has one.
MAX_TRANSCRIPT_CHARS = 8_000_000


def _iso_floor(watermark: Any, days: int) -> str | None:
    """Compute how far back to re-read, in the encoding MCP expects.

    ``sync._recheck_floor`` does the same job for the local backend but formats
    for Sequelize's column text; MCP filters on ISO-8601, so reusing it would
    send a timestamp the server cannot parse. The trailing window itself is
    worth keeping: a meeting refined after it was first archived moves its
    modification time backwards relative to when this tool saw it.

    Args:
        watermark: The highest modification time archived so far.
        days: How many days to reach back.

    Returns:
        An ISO-8601 lower bound, or ``None`` for everything.
    """
    when = _parsed(watermark)
    if when is None:
        return None
    return (when - timedelta(days=days)).isoformat().replace("+00:00", "Z")


def content_digest(payload: Any) -> str:
    """Digest a payload for content addressing.

    Args:
        payload: Any decoded value.

    Returns:
        A hex SHA-256 over the canonical JSON form.
    """
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def truncated(payload: Any) -> bool:
    """Report whether a response says it withheld records.

    Args:
        payload: A decoded response body.

    Returns:
        ``True`` when the response advertises more records than it returned.
    """
    if not isinstance(payload, dict):
        return False
    if any(payload.get(flag) is True for flag in _MORE_FLAGS):
        return True
    return any(payload.get(flag) for flag in _CURSOR_FLAGS)


def _records(payload: Any, *keys: str) -> list[dict[str, Any]]:
    """Pull the record list out of a paginated response.

    Args:
        payload: A decoded response body.
        *keys: Candidate field names holding the list.

    Returns:
        The records, or an empty list when the shape is unrecognized.
    """
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _cursor(payload: Any) -> str | None:
    """Return the continuation cursor from a paginated response.

    Args:
        payload: A decoded response body.

    Returns:
        The cursor, or ``None`` when there are no more pages.
    """
    if not isinstance(payload, dict):
        return None
    for flag in _CURSOR_FLAGS:
        value = payload.get(flag)
        if isinstance(value, str) and value:
            return value
    return None


def local_transcript_state(directory: Any) -> str:
    """Report whether the archive already holds a transcript for a meeting.

    Ground truth from disk, deliberately not from ``index.json``. An index that
    has drifted would otherwise decide whether to overwrite real content.

    Live counts as present: it carries turns and absolute timestamps, so even
    the lower-fidelity local artifact beats normalized plaintext.

    Args:
        directory: The meeting's archive directory, or ``None``.

    Returns:
        ``"present"`` or ``"absent"``.
    """
    if directory is None or not directory.is_dir():
        return "absent"
    for name in ("refined.ndjson", "live.ndjson"):
        if read_transcript(directory / "raw" / name).turns:
            return "present"
    return "absent"


def _archive_verbatim(archive: Archive, tool: str, key: str, payload: Any) -> bool:
    """Write one response to the content-addressed verbatim store.

    Existence-gated rather than compare-then-write. If the server ever puts a
    nonce or a clock into an envelope, a content comparison would rewrite the
    file on every run; addressing by digest means an unchanged response lands
    on a path that already exists and nothing is written at all.

    Args:
        archive: The destination archive.
        tool: The tool that produced the response.
        key: A stable name within that tool's directory.
        payload: The decoded response.

    Returns:
        ``True`` when a file was written.
    """
    destination = archive.resolve(ENTITY_MCP, tool, f"{key}.json")
    if destination.is_file():
        return False
    return write_json_if_changed(destination, payload)


def _fetch_pages(
    client: McpProtocol,
    archive: Archive,
    tool: str,
    arguments: dict[str, Any],
    *,
    record_keys: tuple[str, ...],
    counts: SyncCounts,
) -> tuple[list[dict[str, Any]], bool]:
    """Page one search tool to exhaustion, archiving each page verbatim.

    Args:
        client: An open MCP client.
        archive: The destination archive.
        tool: The search tool to call.
        arguments: Base arguments; ``cursor`` is added per page.
        record_keys: Candidate field names holding the record list.
        counts: Mutated with what was written.

    Returns:
        Every record seen, and whether paging completed cleanly.
    """
    seen: list[dict[str, Any]] = []
    cursor: str | None = None
    # Bounded so a server that always returns a cursor cannot spin forever.
    for _ in range(64):
        page = dict(arguments)
        if cursor:
            page["cursor"] = cursor
        payload = client.call(tool, page)
        if payload is None:
            return seen, False
        counts.scanned += 1
        digest = content_digest(payload)[:16]
        if _archive_verbatim(archive, tool, digest, payload):
            counts.written += 1
        else:
            counts.unchanged += 1
        seen.extend(_records(payload, *record_keys))
        cursor = _cursor(payload)
        if not cursor:
            return seen, True
    return seen, False


def _fetch_transcript(
    client: McpProtocol, archive: Archive, directory: Any, meeting_id: str
) -> dict[str, Any] | None:
    """Fetch one meeting's transcript in bounded chunks, archiving each.

    Every chunk is archived verbatim before anything is assembled, so a splice
    that later turns out to be wrong is reconstructible from what is on disk.
    The manifest is the commit point and is written last: an interrupted fetch
    leaves correct chunks and no document claiming to be complete.

    Args:
        client: An open MCP client.
        archive: The destination archive.
        directory: Where to write, under ``raw/mcp/``.
        meeting_id: The meeting to fetch.

    Returns:
        The manifest, or ``None`` when the fetch did not complete.
    """
    chunks: list[dict[str, Any]] = []
    text_parts: list[str] = []
    start = 0
    reported: int | None = None

    for _ in range(256):
        payload = client.call(
            "get_meeting",
            {
                "meeting_id": meeting_id,
                "view_transcript": {
                    "start_char": start,
                    "char_limit": TRANSCRIPT_CHARS,
                },
            },
        )
        if payload is None:
            return None
        name = f"{start:08d}"
        write_json_if_changed(
            directory / "raw" / "mcp" / "transcript" / f"{name}.json", payload,
        )
        text = _transcript_text(payload)
        if reported is None:
            reported = _transcript_total(payload)
        if not text:
            break
        chunks.append(
            {"start_char": start, "chars": len(text), "file": f"transcript/{name}.json"}
        )
        text_parts.append(text)
        # Advance by what came back, never by what was asked for: the two
        # differ on the last page, and assuming otherwise drops characters.
        start += len(text)
        if len(text) < TRANSCRIPT_CHARS:
            break
        if sum(len(part) for part in text_parts) > MAX_TRANSCRIPT_CHARS:
            return None

    assembled = "".join(text_parts)
    if not assembled:
        return None
    # A reported total that disagrees with what was assembled means the
    # transcript moved underneath the paging, or the offsets do not mean what
    # this client assumes. Either way, refuse to claim it is complete.
    mismatch = reported is not None and reported != len(assembled)
    return {
        "tool": "get_meeting.view_transcript",
        "meeting_id": meeting_id,
        "chars": len(assembled),
        "sha256": hashlib.sha256(assembled.encode("utf-8")).hexdigest(),
        "chunks": chunks,
        "reported_total": reported,
        "assembly_mismatch": mismatch,
        "text": assembled,
    }


def _transcript_text(payload: Any) -> str:
    """Extract the transcript text from a get_meeting response.

    Args:
        payload: A decoded response.

    Returns:
        The text, or an empty string when the response carried none.
    """
    if not isinstance(payload, dict):
        return ""
    for key in ("transcript", "transcript_text"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            for inner in ("text", "content"):
                if isinstance(value.get(inner), str):
                    return value[inner]
    return ""


def _transcript_total(payload: Any) -> int | None:
    """Extract a reported total character count, when the server sends one.

    Args:
        payload: A decoded response.

    Returns:
        The total, or ``None``.
    """
    if not isinstance(payload, dict):
        return None
    value = payload.get("transcript")
    if isinstance(value, dict):
        for key in ("total_chars", "total", "length"):
            if isinstance(value.get(key), int):
                return value[key]
    for key in ("transcript_total_chars", "transcript_chars"):
        if isinstance(payload.get(key), int):
            return payload[key]
    return None


def _render_transcript(manifest: dict[str, Any], meeting_id: str, title: str) -> str:
    """Render the recovered transcript as a sibling document.

    Frontmatter says plainly where it came from and what it is missing, so
    nobody mistakes it for the local rendering.

    Args:
        manifest: The assembled manifest.
        meeting_id: The meeting id.
        title: The meeting title.

    Returns:
        Markdown.
    """
    from .render import yaml_block

    warning = (
        "> Recovered from Wispr Flow's MCP server because this archive holds no\n"
        "> local transcript for this meeting. It is normalized plaintext: it "
        "carries\n> no speaker attribution and no timestamps, which the local "
        "NDJSON does.\n"
    )
    if manifest.get("assembly_mismatch"):
        warning += (
            ">\n> **The server's reported length disagreed with what was "
            "assembled.**\n> Treat this text as possibly incomplete; the "
            "verbatim chunks are under `raw/mcp/`.\n"
        )
    head = yaml_block(
        {
            "id": meeting_id,
            "title": title,
            "source": SOURCE_MCP,
            "kind": "transcript",
            "fidelity": "normalized-plaintext",
            "chars": manifest["chars"],
            "tags": ["wispr/transcript", "wispr/recovered"],
        }
    )
    return f"{head}\n# {title or meeting_id}\n\n{warning}\n{manifest['text'].strip()}\n"


def sync_mcp(
    archive: Archive,
    client: McpProtocol,
    options: SyncOptions,
) -> SyncCounts:
    """Archive MCP responses verbatim and fill transcript gaps.

    Args:
        archive: The destination archive.
        client: An open MCP client, or any object satisfying the protocol.
        options: What this run was asked to do.

    Returns:
        What the pass did.
    """
    counts = SyncCounts()
    now = _now()
    state = archive.source_state(SOURCE_MCP)

    account = client.call("get_account_info", {})
    if account is not None:
        counts.scanned += 1
        if _archive_verbatim(archive, "get_account_info", "account", account):
            counts.written += 1
        else:
            counts.unchanged += 1

    watermark = archive.watermark(SOURCE_MCP, "meetings")
    since = None if options.full else _iso_floor(watermark, options.recheck_days)
    arguments: dict[str, Any] = {"limit": PAGE_SIZE}
    if since:
        # `since` only. A moving `until=now` echoed back into a response would
        # give every page a fresh digest and rewrite the archive every run.
        arguments["since"] = since

    meetings, complete = _fetch_pages(
        client,
        archive,
        "search_meetings",
        arguments,
        record_keys=("meetings", "results", "items"),
        counts=counts,
    )
    if not complete:
        counts.failed += 1

    if options.dry_run:
        counts.scanned += len(meetings)
        return counts

    highest = watermark
    for record in meetings:
        meeting_id = str(record.get("id") or record.get("meeting_id") or "")
        if not MEETING_DIR_RE.match(meeting_id):
            # A remote-supplied id is more untrusted than a local one and must
            # never become a path component unvalidated.
            continue
        modified = record.get("modified_at") or record.get("modifiedAt")
        if isinstance(modified, str) and (highest is None or modified > str(highest)):
            highest = modified

        entry = archive.entry("meetings", meeting_id)
        if entry is None:
            _archive_upstream_only(archive, client, record, meeting_id, counts, now)
            continue

        directory = archive.existing_path("meetings", meeting_id)
        has_upstream = bool(record.get("has_transcript"))
        state_of_local = local_transcript_state(directory)

        if state_of_local == "present" or not has_upstream or directory is None:
            # Record the fact and spend no request on it. "Gone from both
            # sides" is worth being able to prove, in the same spirit as
            # recording localDataPolicy.
            _decorate(
                archive,
                meeting_id,
                {
                    "has_transcript": has_upstream,
                    "filled": False,
                    "reason": "local_transcript_present"
                    if state_of_local == "present"
                    else "no_transcript_upstream",
                    "modified_at": modified,
                },
            )
            counts.unchanged += 1
            continue

        existing = (entry.get("mcp") or {}) if isinstance(entry.get("mcp"), dict) else {}
        if existing.get("filled") and existing.get("modified_at") == modified:
            # Already recovered and nothing moved upstream. Transcripts are the
            # expensive calls; this is the guard that keeps a re-run cheap as
            # well as byte-identical.
            counts.unchanged += 1
            continue

        counts.scanned += 1
        manifest = _fetch_transcript(client, archive, directory, meeting_id)
        if manifest is None:
            counts.failed += 1
            continue

        title = str(record.get("title") or entry.get("title") or "")
        text = manifest.pop("text")
        wrote = write_json_if_changed(
            directory / "raw" / "mcp" / "manifest.json", manifest
        )
        wrote |= write_text_if_changed(
            directory / "transcript.mcp.md",
            _render_transcript({**manifest, "text": text}, meeting_id, title),
        )
        _decorate(
            archive,
            meeting_id,
            {
                "has_transcript": True,
                "filled": True,
                "reason": "transcript_deleted_upstream"
                if entry.get("transcript_deleted_upstream")
                else "no_local_transcript",
                "chars": manifest["chars"],
                "sha256": manifest["sha256"],
                "chunks": len(manifest["chunks"]),
                "assembly_mismatch": manifest["assembly_mismatch"] or None,
                "modified_at": modified,
                "files": ["raw/mcp/manifest.json", "transcript.mcp.md"],
            },
        )
        counts.written += 1 if wrote else 0
        counts.unchanged += 0 if wrote else 1

    notes, notes_complete = _fetch_pages(
        client,
        archive,
        "search_scratchpad_notes",
        {"limit": PAGE_SIZE},
        record_keys=("notes", "results", "items"),
        counts=counts,
    )
    if not notes_complete:
        counts.failed += 1

    index = archive.resolve(ENTITY_MCP, "meetings.index.ndjson")
    if write_ndjson_if_changed(index, _summaries(meetings)):
        counts.written += 1

    if not counts.failed and highest and highest != watermark:
        archive.set_watermark(SOURCE_MCP, "meetings", "modified_at", highest)
    state["server"] = dict(getattr(client, "server", {}) or {})
    state["notes_seen"] = len(notes)
    return counts


def _summaries(meetings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build the sorted derived index of what MCP reported.

    Args:
        meetings: Every meeting record seen this pass.

    Returns:
        One compact row per meeting, sorted by id so the file is stable.
    """
    rows = {
        str(record.get("id") or record.get("meeting_id") or ""): {
            "id": str(record.get("id") or record.get("meeting_id") or ""),
            "title": record.get("title"),
            "has_transcript": bool(record.get("has_transcript")),
            "modified_at": record.get("modified_at") or record.get("modifiedAt"),
        }
        for record in meetings
    }
    rows.pop("", None)
    return [rows[key] for key in sorted(rows)]


def _decorate(archive: Archive, meeting_id: str, mcp: dict[str, Any]) -> None:
    """Write the one reserved field this pass may add to a meetings entry.

    Built as a single dict so key order is fixed. ``write_json`` does not sort
    keys, so a conditionally-assembled sub-dict would reorder between runs and
    churn ``index.json`` with identical content.

    Args:
        archive: The destination archive.
        meeting_id: The meeting key.
        mcp: The sub-dict to store.
    """
    archive.put("meetings", meeting_id, mcp={k: v for k, v in mcp.items() if v is not None})


def _archive_upstream_only(
    archive: Archive,
    client: McpProtocol,
    record: dict[str, Any],
    meeting_id: str,
    counts: SyncCounts,
    now: str,
) -> None:
    """Archive a meeting the local store does not have, under ``mcp/``.

    Deliberately not written into ``meetings/``. That namespace is counted
    against the database by ``verify``, so an extra entry there would make a
    healthy archive report a mismatch on every run -- and would set up a real
    collision the day the meeting finally syncs to the local store.

    Args:
        archive: The destination archive.
        client: An open MCP client.
        record: The search result.
        meeting_id: The validated meeting id.
        counts: Mutated with what was written.
        now: This run's timestamp.
    """
    detail = client.call("get_meeting", {"meeting_id": meeting_id})
    if detail is None:
        counts.failed += 1
        return
    counts.scanned += 1
    when = _parsed(record.get("start_time") or record.get("created_at"))
    directory = archive.resolve(
        ENTITY_MCP,
        "meetings",
        dated_prefix(when),
        record_dir_name(when, record.get("title"), meeting_id),
    )
    wrote = write_json_if_changed(directory / "raw" / "meeting.json", detail)
    archive.put(
        ENTITY_MCP_MEETINGS,
        meeting_id,
        path=archive.relative(directory),
        title=record.get("title"),
        source=SOURCE_MCP,
        upstream_only=True,
        archived_at=now if wrote else None,
    )
    counts.written += 1 if wrote else 0
    counts.unchanged += 0 if wrote else 1


def _parsed(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp, tolerating absence.

    Args:
        value: The raw value.

    Returns:
        An aware datetime, or ``None``.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
