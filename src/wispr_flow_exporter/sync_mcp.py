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

**Ranges.** Transcripts and notes arrive a bounded range at a time, and a range
that is not the last ends with the server's own marker naming the offset to
ask for next. That offset is always the server's, never computed here, and
the marker never reaches the text. Measured on 0.4.1, which advanced by the
length of what came back, envelope and marker included: a 100,000-character
transcript came back in three requests, 211 characters short at each seam,
with three envelope headers and two markers spliced into it -- and was
recorded as recovered.

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
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from .files_source import MEETING_DIR_RE, read_transcript
from .mcp_api import PAGE_SIZE, TRANSCRIPT_CHARS, McpProtocol
from .render import inline
from .secure_io import (
    read_ndjson,
    write_json_if_changed,
    write_ndjson_if_changed,
    write_text_if_changed,
)
from .store import Archive, dated_prefix, record_dir_name
from .sync import SyncCounts, SyncOptions, _now

SOURCE_MCP = "wispr-mcp"

#: Entity namespaces this pass owns outright.
ENTITY_MCP = "mcp"
ENTITY_MCP_MEETINGS = "mcp_meetings"

#: A response that reports more records than it returned.
_MORE_FLAGS = ("has_more", "hasMore")
_CURSOR_FLAGS = ("next_cursor", "nextCursor")

#: Fields of a listing page that say how to reach the next one rather than
#: what this one lists. Left out of a page's name, so a server whose cursors
#: are not deterministic cannot make an unchanged listing look new every run.
_PAGING_FIELDS = (*_CURSOR_FLAGS, "more")

#: Where the search tools put their records.
MEETING_KEYS = ("meetings", "results", "items")
NOTE_KEYS = ("notes", "results", "items")

#: Pages of one listing to follow before deciding the server is looping.
MAX_PAGES = 64

#: Queries one listing may spend narrowing past the server's result cap. A
#: real account needs a few: a year of meetings rarely passes a thousand. A
#: server that capped every window however narrow would otherwise be answered
#: with a binary tree of queries -- measured against a fake that does, 8,191.
MAX_WINDOWS = 32

#: Refuse to assemble a transcript larger than this. Untrusted remote input
#: gets a cap for the same reason the NDJSON reader has one.
MAX_TRANSCRIPT_CHARS = 8_000_000

#: Ranges of one transcript or note body to request before giving up.
MAX_RANGES = 256

#: How this pass assembles a transcript. Recorded with every recovery, so one
#: assembled by an older, wrong method is fetched again rather than trusted:
#: version 1 was 0.4.x's splicer, which the module note describes.
ASSEMBLY_VERSION = 2

#: The transcript envelope, as measured: a first line and a last line in
#: triple angle brackets around the text. Matched by shape rather than wording,
#: so a reworded warning still parses and a missing envelope does not.
_ENVELOPE = re.compile(r"\A<<<[^\n]*>>>\n(?P<body>.*)\n<<<[^\n]*>>>\Z", re.DOTALL)

#: The marker ending a range that is not the last, as measured for transcripts
#: and as published for both views. Anchored at the end, so text that merely
#: quotes a marker mid-range is text.
_MARKER = re.compile(
    r"\n\n\(\.\.\.truncated, (?P<remaining>\d+) chars remaining; continue with "
    r"(?P<view>view_transcript|view_content)\.start_char=(?P<next>\d+)\.\.\.\)\Z"
)

#: The sub-key fields ``_decorate`` writes, in the order it writes them.
_MCP_KEYS = (
    "has_transcript",
    "filled",
    "reason",
    "chars",
    "sha256",
    "chunks",
    "assembly",
    "modified_at",
    "files",
)

#: What a recovery leaves behind. Kept when a later state is not a recovery --
#: local catching up, say -- because the files it describes are still on disk.
_RECOVERY_KEYS = ("chars", "sha256", "chunks", "assembly", "files")

View = Literal["view_transcript", "view_content"]


def _floor(watermark: Any, days: int) -> datetime | None:
    """Compute how far back an incremental listing reads.

    The search tools list most recently modified first, so an incremental run
    reads until it passes this point and stops. The trailing window is worth
    keeping: a meeting refined after it was first archived moves its
    modification time backwards relative to when this tool saw it.

    ``since`` is deliberately not how the window is applied. Measured, and
    published: it filters on when a meeting *started*, so 0.4.1's ``since``
    of a week before the watermark never listed a June meeting whose notes
    were edited in September.

    Args:
        watermark: The highest modification time archived so far.
        days: How many days to reach back.

    Returns:
        The floor, or ``None`` for everything.
    """
    when = _parsed(watermark)
    if when is None:
        return None
    return when - timedelta(days=days)


def content_digest(payload: Any) -> str:
    """Digest a payload for content addressing.

    Args:
        payload: Any decoded value.

    Returns:
        A hex SHA-256 over the canonical JSON form.
    """
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def more_pages(payload: Any) -> bool:
    """Report whether a listing page says more pages follow it.

    Args:
        payload: A decoded response body.

    Returns:
        ``True`` when the page flags more records or carries a cursor.
    """
    if not isinstance(payload, dict):
        return False
    if any(payload.get(flag) is True for flag in _MORE_FLAGS):
        return True
    return _cursor(payload) is not None


def capped(payload: Any) -> bool:
    """Report whether the server stopped a listing at its own limit.

    The search tools stop at a fixed number of results per query and say so
    with ``truncated: true`` -- a different signal from a next page, and one
    no cursor will recover.

    Args:
        payload: A decoded response body.

    Returns:
        ``True`` when the page says the listing was cut short.
    """
    return isinstance(payload, dict) and payload.get("truncated") is True


@dataclass(frozen=True, slots=True)
class Paging:
    """What a paged listing returned, and whether that was everything.

    Attributes:
        records: Every record seen, in order.
        complete: Whether the listing ended the way a complete one does.
        reason: Why it is incomplete, for the operator; ``None`` when complete.
        capped: Whether it ended at the server's result cap, which a
            narrower query can get past.
    """

    records: list[dict[str, Any]]
    complete: bool
    reason: str | None = None
    capped: bool = False


def _page_digest(payload: Any) -> str:
    """Name a listing page by what it lists.

    Args:
        payload: A decoded page.

    Returns:
        A short digest over the page without its paging fields.
    """
    if isinstance(payload, dict):
        payload = {k: v for k, v in payload.items() if k not in _PAGING_FIELDS}
    return content_digest(payload)[:16]


def _listing(payload: Any, keys: tuple[str, ...]) -> bool:
    """Report whether a payload is a listing page at all.

    Args:
        payload: A decoded response body.
        keys: Candidate field names holding the record list.

    Returns:
        ``True`` for a list, or an object holding a list under one of ``keys``.
    """
    if isinstance(payload, list):
        return True
    return isinstance(payload, dict) and any(
        isinstance(payload.get(key), list) for key in keys
    )


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


def _archive_verbatim(
    archive: Archive, tool: str, key: str, payload: Any, *, dry_run: bool = False
) -> bool:
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
        dry_run: Report whether a file would be written, and write nothing.

    Returns:
        ``True`` when a file was written, or would have been.
    """
    destination = archive.resolve(ENTITY_MCP, tool, f"{key}.json")
    if destination.is_file():
        return False
    if dry_run:
        return True
    return write_json_if_changed(destination, payload)


def _fetch_pages(
    client: McpProtocol,
    archive: Archive,
    tool: str,
    arguments: dict[str, Any],
    *,
    record_keys: tuple[str, ...],
    counts: SyncCounts,
    dry_run: bool = False,
    enough: Callable[[Any], bool] | None = None,
) -> Paging:
    """Page one search tool to its end, archiving each page verbatim.

    A listing is complete only when it ends the way a complete one does: a
    page with no cursor that does not claim more. Measured on 0.4.1, every
    other ending counted as complete too -- ``has_more`` with no cursor, the
    server's own ``truncated: true`` cap, an error page -- and the watermark
    moved past records never listed, while one cursor repeated forever cost
    64 requests and listed each record 64 times.

    Args:
        client: An open MCP client.
        archive: The destination archive.
        tool: The search tool to call.
        arguments: Base arguments; ``cursor`` is added per page.
        record_keys: Candidate field names holding the record list.
        counts: Mutated with what was written.
        dry_run: Page as usual but write no page to disk.
        enough: Says, after each page, whether the caller has read as far as
            it needs to -- which is then as complete as it has to be.

    Returns:
        Every record seen, and whether the listing was complete.
    """
    seen: list[dict[str, Any]] = []
    cursors: set[str] = set()
    cursor: str | None = None
    for _ in range(MAX_PAGES):
        page = dict(arguments)
        if cursor:
            page["cursor"] = cursor
        payload = client.call(tool, page)
        if payload is None:
            return Paging(seen, False, "a page could not be fetched")
        if not _listing(payload, record_keys):
            return Paging(seen, False, "a page held no record list")
        counts.scanned += 1
        if _archive_verbatim(
            archive, tool, _page_digest(payload), payload, dry_run=dry_run
        ):
            counts.written += 1
        else:
            counts.unchanged += 1
        seen.extend(_records(payload, *record_keys))
        if enough is not None and enough(payload):
            return Paging(seen, True)
        if capped(payload):
            return Paging(seen, False, "the server capped the listing", capped=True)
        cursor = _cursor(payload)
        if cursor is None:
            if more_pages(payload):
                return Paging(
                    seen, False, "more records were flagged but no cursor given"
                )
            return Paging(seen, True)
        if cursor in cursors:
            return Paging(seen, False, "the server repeated a cursor")
        cursors.add(cursor)
    return Paging(seen, False, f"the listing ran past {MAX_PAGES} pages")


def _iso(when: datetime) -> str:
    """Format a time the way the search tools take it.

    Args:
        when: An aware time.

    Returns:
        ISO 8601 in UTC, ending ``Z``.
    """
    return when.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _unique(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop repeated meetings, keeping the first of each.

    Args:
        records: Search results, perhaps from overlapping queries.

    Returns:
        One record per id, in first-seen order.
    """
    seen: set[str] = set()
    kept: list[dict[str, Any]] = []
    for record in records:
        key = str(record.get("id") or record.get("meeting_id") or "")
        if key not in seen:
            seen.add(key)
            kept.append(record)
    return kept


@dataclass(slots=True)
class _Budget:
    """What is left of one listing's :data:`MAX_WINDOWS`."""

    left: int = MAX_WINDOWS


def _windowed(
    client: McpProtocol,
    archive: Archive,
    *,
    counts: SyncCounts,
    dry_run: bool,
    since: datetime | None = None,
    until: datetime | None = None,
    budget: _Budget | None = None,
) -> Paging:
    """List every meeting starting in a window, narrowing it past the cap.

    The server stops any one query at a thousand results and says so. The way
    past that is its own advice -- narrow the query -- so a capped window is
    split by start time and each half listed in turn: a bounded window halves,
    and an open-ended one gives up its oldest part a year at a time.

    Args:
        client: An open MCP client.
        archive: The destination archive.
        counts: Mutated with what was written.
        dry_run: Write no page to disk.
        since: Earliest start time, inclusive; ``None`` for no bound.
        until: Latest start time, exclusive; ``None`` for no bound.
        budget: Queries left for this listing; a new listing gets its own.

    Returns:
        Every meeting in the window, and whether that is all of them.
    """
    budget = _Budget() if budget is None else budget
    if budget.left <= 0:
        return Paging(
            [],
            False,
            f"the server capped the listing in every window of {MAX_WINDOWS} tried",
        )
    budget.left -= 1
    arguments: dict[str, Any] = {"limit": PAGE_SIZE}
    if since is not None:
        arguments["since"] = _iso(since)
    if until is not None:
        arguments["until"] = _iso(until)
    listing = _fetch_pages(
        client,
        archive,
        "search_meetings",
        arguments,
        record_keys=MEETING_KEYS,
        counts=counts,
        dry_run=dry_run,
    )
    if not listing.capped:
        return listing
    now = datetime.now(tz=UTC)
    if since is not None and until is not None:
        pivot = since + (until - since) / 2
    elif since is not None:
        pivot = now if now > since else since + timedelta(days=365)
    else:
        pivot = (until or now) - timedelta(days=365)
    halves = [
        _windowed(
            client,
            archive,
            counts=counts,
            dry_run=dry_run,
            since=low,
            until=high,
            budget=budget,
        )
        for low, high in ((pivot, until), (since, pivot))
    ]
    incomplete = [half for half in halves if not half.complete]
    return Paging(
        _unique([*listing.records, *(r for half in halves for r in half.records)]),
        not incomplete,
        incomplete[0].reason if incomplete else None,
    )


class _Horizon:
    """Stops a most-recently-modified-first listing once it is past a floor.

    Checks the order as it goes. Measured, and published: the search tools
    list most recently modified first. Should that ever stop being true, a
    listing that stopped early would miss what it was meant to find, so the
    first record out of order turns early stopping off for the rest of the
    listing, which then reads to the end.
    """

    def __init__(self, floor: datetime) -> None:
        """Remember where to stop.

        Args:
            floor: The oldest modification time this run needs.
        """
        self.floor = floor
        self.last: datetime | None = None
        self.ordered = True

    def __call__(self, payload: Any) -> bool:
        """Report whether a page has reached the floor.

        Args:
            payload: One listing page.

        Returns:
            ``True`` once the listing, still in order, is older than the floor.
        """
        for record in _records(payload, *MEETING_KEYS):
            when = _parsed(record.get("modified_at") or record.get("modifiedAt"))
            if when is None:
                continue
            if self.last is not None and when > self.last:
                self.ordered = False
            self.last = when
        return self.ordered and self.last is not None and self.last < self.floor


def _list_meetings(
    client: McpProtocol,
    archive: Archive,
    *,
    floor: datetime | None,
    counts: SyncCounts,
    dry_run: bool,
    problems: list[str],
) -> Paging:
    """List the meetings this run needs to look at.

    Args:
        client: An open MCP client.
        archive: The destination archive.
        floor: For an incremental run, the oldest modification time needed;
            ``None`` lists everything.
        counts: Mutated with what was written.
        dry_run: Write no page to disk.
        problems: Receives anything worth telling the operator.

    Returns:
        The listed meetings, and whether the listing covered what was needed.
    """
    if floor is not None:
        horizon = _Horizon(floor)
        recent = _fetch_pages(
            client,
            archive,
            "search_meetings",
            {"limit": PAGE_SIZE},
            record_keys=MEETING_KEYS,
            counts=counts,
            dry_run=dry_run,
            enough=horizon,
        )
        if not horizon.ordered:
            problems.append(
                "the meetings listing was not most recently modified first, so "
                "all of it was read"
            )
        if not recent.capped:
            return recent
        # More changed since the floor than one query will return. Rare, and
        # the answer is the one a first run gets: everything, window by window.
    return _windowed(client, archive, counts=counts, dry_run=dry_run)


# --- ranges -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Assembled:
    """A transcript or note body put back together from its ranges.

    Attributes:
        text: The assembled text.
        chunks: What each range was: where it started, how long it was in
            both units the server might count in, and where the server said
            to continue.
        first: The first range's whole reply.
        complete: Whether the last range ended the way a last range does.
        mismatch: Whether a continuation offset disagreed with the length of
            the range before it, in either unit.
        reason: Why assembly is incomplete; ``None`` when complete.
    """

    text: str
    chunks: tuple[dict[str, Any], ...]
    first: Any
    complete: bool
    mismatch: bool = False
    reason: str | None = None

    def summary(self) -> dict[str, Any]:
        """Describe the assembly for a manifest, without the text itself.

        Returns:
            The facts a later run, or a reader, checks it against.
        """
        return {
            "chars": len(self.text),
            "sha256": hashlib.sha256(self.text.encode("utf-8")).hexdigest(),
            "complete": self.complete,
            "mismatch": self.mismatch,
            "chunks": list(self.chunks),
        }


def _utf16_len(text: str) -> int:
    """Count a text's length in UTF-16 code units, as JavaScript does.

    Args:
        text: The text.

    Returns:
        Its length in UTF-16 code units.
    """
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def _joined(parts: list[str]) -> str:
    """Join ranges, rejoining any character a range boundary split in two.

    A server that counts in UTF-16 units can end a range between the two
    halves of a surrogate pair -- an emoji, say -- and each half then decodes
    on its own. Concatenated, the halves are two code points, not one
    character, and cannot even be encoded as UTF-8.

    Args:
        parts: The ranges' texts, in order.

    Returns:
        The text, with every split pair whole again.
    """
    return (
        "".join(parts)
        .encode("utf-16-le", "surrogatepass")
        .decode("utf-16-le", errors="replace")
    )


def _range_text(payload: Any, view: View) -> str | None:
    """Extract one range's raw text from a ``get_meeting`` reply.

    Args:
        payload: The decoded reply.
        view: Which body was asked for.

    Returns:
        The text as sent, envelope and marker included, or ``None``.
    """
    if not isinstance(payload, dict):
        return None
    value = payload.get("transcript" if view == "view_transcript" else "content")
    return value if isinstance(value, str) else None


def _split_range(raw: str, view: View) -> tuple[str, int | None, int | None] | None:
    """Split one range into its text and where the server says to continue.

    Args:
        raw: The range as sent.
        view: Which body it belongs to.

    Returns:
        ``(text, next_start, remaining)``, with ``next_start`` ``None`` for a
        last range; or ``None`` when a transcript's envelope, or a marker
        for this view, is not where it should be.
    """
    body = raw
    if view == "view_transcript":
        envelope = _ENVELOPE.match(raw)
        if envelope is None:
            return None
        body = envelope.group("body")
    marker = _MARKER.search(body)
    if marker is None:
        return body, None, None
    if marker.group("view") != view:
        return None
    following, remaining = int(marker.group("next")), int(marker.group("remaining"))
    return body[: marker.start()], following, remaining


def _fetch_ranges(
    client: McpProtocol, meeting_id: str, view: View, sink: Path
) -> Assembled | None:
    """Fetch a transcript or note body a range at a time, archiving each.

    Every range is archived verbatim, under the offset it was asked for,
    before anything is assembled, so an assembly that later turns out to be
    wrong is reconstructible from what is on disk.

    The next offset is always the one the server's marker names. A range with
    no marker is the last -- unless it is a full range, which means the
    marker's format has changed and this cannot tell where the text goes on,
    so the assembly is incomplete rather than silently short.

    Args:
        client: An open MCP client.
        meeting_id: The meeting.
        view: ``"view_transcript"`` or ``"view_content"``.
        sink: The directory the ranges are archived in.

    Returns:
        The assembly, complete or not, or ``None`` when a request failed --
        which the client has already recorded, with its reason.
    """
    parts: list[str] = []
    chunks: list[dict[str, Any]] = []
    first: Any = None
    mismatch = False
    start = 0
    total = 0

    def stopped(reason: str) -> Assembled:
        return Assembled(_joined(parts), tuple(chunks), first, False, mismatch, reason)

    for _ in range(MAX_RANGES):
        payload = client.call(
            "get_meeting",
            {
                "meeting_id": meeting_id,
                view: {"start_char": start, "char_limit": TRANSCRIPT_CHARS},
            },
        )
        if payload is None:
            return None
        first = payload if first is None else first
        write_json_if_changed(sink / f"{start:08d}.json", payload)
        raw = _range_text(payload, view)
        if raw is None:
            return stopped("a reply carried no text for the requested range")
        split = _split_range(raw, view)
        if split is None:
            return stopped("a reply's envelope or marker was not recognized")
        text, following, remaining = split
        chars, units = len(text), _utf16_len(text)
        chunks.append(
            {
                "start_char": start,
                "chars": chars,
                "utf16": units,
                "next_start_char": following,
                "remaining": remaining,
                "file": f"{sink.name}/{start:08d}.json",
            }
        )
        parts.append(text)
        total += chars
        if total > MAX_TRANSCRIPT_CHARS:
            return stopped(f"longer than {MAX_TRANSCRIPT_CHARS} characters")
        if following is None:
            if max(chars, units) >= TRANSCRIPT_CHARS:
                return stopped("a full range arrived without a continuation marker")
            return Assembled(_joined(parts), tuple(chunks), first, True, mismatch)
        if following <= start:
            return stopped("the continuation offset did not move forward")
        # Either unit may be the server's; anything else means the offsets
        # do not mean what they appear to, and the text is suspect.
        mismatch |= following - start not in (chars, units)
        start = following
    return stopped(f"more than {MAX_RANGES} ranges")


def _render_transcript(
    text: str, meeting_id: str, title: str, *, mismatch: bool
) -> str:
    """Render a recovered transcript as a sibling document.

    Frontmatter says plainly where it came from and what it is missing, so
    nobody mistakes it for the local rendering.

    Args:
        text: The assembled transcript.
        meeting_id: The meeting id.
        title: The meeting title.
        mismatch: Whether assembly found offsets it could not account for.

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
    if mismatch:
        warning += (
            ">\n> **The server's continuation offsets disagreed with the lengths "
            "of the\n> ranges it sent.** Treat this text as possibly incomplete; "
            "the verbatim\n> ranges are archived beside it.\n"
        )
    head = yaml_block(
        {
            "id": meeting_id,
            "title": title,
            "source": SOURCE_MCP,
            "kind": "transcript",
            "fidelity": "normalized-plaintext",
            "chars": len(text),
            "tags": ["wispr/transcript", "wispr/recovered"],
        }
    )
    # Flattened: the title is remote input, and a newline in it would
    # otherwise put a forged frontmatter block into the body.
    heading = inline(title, fallback=meeting_id)
    return f"{head}\n# {heading}\n\n{warning}\n{text.strip()}\n"


def sync_mcp(
    archive: Archive,
    client: McpProtocol,
    options: SyncOptions,
    problems: list[str] | None = None,
) -> SyncCounts:
    """Archive MCP responses verbatim and fill transcript gaps.

    Args:
        archive: The destination archive.
        client: An open MCP client, or any object satisfying the protocol.
        options: What this run was asked to do.
        problems: Receives what went wrong that no failed call explains, such
            as a listing the server ended early.

    Returns:
        What the pass did.
    """
    problems = [] if problems is None else problems
    counts = SyncCounts()
    now = _now()
    state = archive.source_state(SOURCE_MCP)

    account = client.call("get_account_info", {})
    if account is not None:
        counts.scanned += 1
        # Content-addressed: a fixed name, existence-gated, kept the first
        # answer forever. Measured on 0.4.1: a plan that changed from free
        # to pro was still archived as free.
        if _archive_verbatim(
            archive,
            "get_account_info",
            content_digest(account)[:16],
            account,
            dry_run=options.dry_run,
        ):
            counts.written += 1
        else:
            counts.unchanged += 1

    watermark = archive.watermark(SOURCE_MCP, "meetings")
    floor = None if options.full else _floor(watermark, options.recheck_days)
    listing = _list_meetings(
        client,
        archive,
        floor=floor,
        counts=counts,
        dry_run=options.dry_run,
        problems=problems,
    )
    meetings = _unique(listing.records)
    if not listing.complete:
        counts.failed += 1
        problems.append(f"meetings listing incomplete: {listing.reason}")

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
        try:
            if archive.entry("meetings", meeting_id) is None:
                _archive_upstream_only(
                    archive, client, record, meeting_id, counts, now, options, problems
                )
            else:
                _fill_gap(archive, client, record, meeting_id, counts, options, problems)
        except OSError as error:
            # One meeting that cannot be written is that meeting's failure.
            # Measured on 0.4.1: it ended the pass, and every meeting after it
            # went unarchived.
            counts.failed += 1
            problems.append(f"meeting {meeting_id}: {error.strerror or error}")

    notes = _fetch_pages(
        client,
        archive,
        "search_scratchpad_notes",
        {"limit": PAGE_SIZE},
        record_keys=NOTE_KEYS,
        counts=counts,
    )
    if not notes.complete:
        counts.failed += 1
        problems.append(f"notes listing incomplete: {notes.reason}")

    index = archive.resolve(ENTITY_MCP, "meetings.index.ndjson")
    if write_ndjson_if_changed(index, _merged_summaries(index, meetings)):
        counts.written += 1

    if not counts.failed and highest and highest != watermark:
        archive.set_watermark(SOURCE_MCP, "meetings", "modified_at", highest)
    state["server"] = dict(getattr(client, "server", {}) or {})
    state["notes_seen"] = len(notes.records)
    return counts


def _fill_gap(
    archive: Archive,
    client: McpProtocol,
    record: dict[str, Any],
    meeting_id: str,
    counts: SyncCounts,
    options: SyncOptions,
    problems: list[str],
) -> None:
    """Recover the transcript of a local meeting whose archive has none.

    Args:
        archive: The destination archive.
        client: An open MCP client.
        record: The meeting's search result.
        meeting_id: The validated meeting id.
        counts: Mutated with what was done.
        options: What this run was asked to do.
        problems: Receives why a recovery is incomplete.
    """
    entry = archive.entry("meetings", meeting_id) or {}
    modified = record.get("modified_at") or record.get("modifiedAt")
    directory = archive.existing_path("meetings", meeting_id)
    has_upstream = bool(record.get("has_transcript"))
    state_of_local = local_transcript_state(directory)

    if state_of_local == "present" or not has_upstream or directory is None:
        # Record the fact and spend no request on it. "Gone from both sides"
        # is worth being able to prove, in the same spirit as recording
        # localDataPolicy.
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
        return

    recorded = entry.get("mcp")
    existing: dict[str, Any] = recorded if isinstance(recorded, dict) else {}
    if (
        not options.full
        and existing.get("filled")
        and existing.get("modified_at") == modified
        and existing.get("assembly") == ASSEMBLY_VERSION
    ):
        # Already recovered, by this assembly, and nothing moved upstream.
        # Transcripts are the expensive calls; this is the guard that keeps a
        # re-run cheap as well as byte-identical. One assembled by the old
        # splicer is fetched again, once; --full fetches any of them again.
        counts.unchanged += 1
        return

    counts.scanned += 1
    sink = directory / "raw" / "mcp" / "transcript"
    assembled = _fetch_ranges(client, meeting_id, "view_transcript", sink)
    if assembled is None:
        counts.failed += 1
        return
    if not assembled.complete:
        # The ranges stay on disk; nothing claims to be the transcript, and
        # whatever an earlier run recovered is left exactly as it was.
        counts.failed += 1
        problems.append(f"transcript of {meeting_id} incomplete: {assembled.reason}")
        return

    title = str(record.get("title") or entry.get("title") or "")
    summary = assembled.summary()
    manifest = {
        "tool": "get_meeting.view_transcript",
        "meeting_id": meeting_id,
        "assembly": ASSEMBLY_VERSION,
        **summary,
    }
    wrote = write_json_if_changed(directory / "raw" / "mcp" / "manifest.json", manifest)
    wrote |= write_text_if_changed(
        directory / "transcript.mcp.md",
        _render_transcript(
            assembled.text, meeting_id, title, mismatch=assembled.mismatch
        ),
    )
    if assembled.mismatch:
        reason = "assembly_mismatch"
    elif entry.get("transcript_deleted_upstream"):
        reason = "transcript_deleted_upstream"
    else:
        reason = "no_local_transcript"
    _decorate(
        archive,
        meeting_id,
        {
            "has_transcript": True,
            # Rendered, with its warning, but never called recovered: offsets
            # that disagree with the text are not a transcript to rely on.
            "filled": not assembled.mismatch,
            "reason": reason,
            "chars": summary["chars"],
            "sha256": summary["sha256"],
            "chunks": len(assembled.chunks),
            "assembly": ASSEMBLY_VERSION,
            "modified_at": modified,
            "files": ["raw/mcp/manifest.json", "transcript.mcp.md"],
        },
    )
    counts.written += 1 if wrote else 0
    counts.unchanged += 0 if wrote else 1


def _summaries(meetings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build one compact row per meeting MCP reported.

    Args:
        meetings: Meeting records seen this pass.

    Returns:
        One row per valid id.
    """
    rows = []
    for record in meetings:
        key = str(record.get("id") or record.get("meeting_id") or "")
        if MEETING_DIR_RE.match(key):
            rows.append(
                {
                    "id": key,
                    "title": record.get("title"),
                    "has_transcript": bool(record.get("has_transcript")),
                    "modified_at": record.get("modified_at") or record.get("modifiedAt"),
                }
            )
    return rows


def _merged_summaries(
    path: Path, meetings: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Merge this pass's rows into the derived index, sorted by id.

    An incremental run lists only what changed recently, and rewriting the
    index from that alone emptied it of everything else. Measured on 0.4.1:
    after an incremental run it listed one of the account's two meetings.

    Args:
        path: The index file.
        meetings: Meeting records seen this pass.

    Returns:
        Every meeting either one knows of, the newer row winning.
    """
    rows: dict[str, dict[str, Any]] = {}
    earlier, _ = read_ndjson(path)
    for row in earlier:
        key = row.get("id")
        if isinstance(key, str) and MEETING_DIR_RE.match(key):
            rows[key] = row
    for row in _summaries(meetings):
        rows[row["id"]] = row
    return [rows[key] for key in sorted(rows)]


def _decorate(archive: Archive, meeting_id: str, mcp: dict[str, Any]) -> None:
    """Write the one reserved field this pass may add to a meetings entry.

    Written in a fixed key order: ``write_json`` does not sort keys, so a
    conditionally assembled sub-dict would reorder between runs and churn
    ``index.json`` with identical content. And a state that is not itself a
    recovery keeps the last recovery's facts -- measured on 0.4.1, local
    catching up erased them, leaving a ``transcript.mcp.md`` on disk that the
    index no longer mentioned.

    Args:
        archive: The destination archive.
        meeting_id: The meeting key.
        mcp: The new state.
    """
    entry = archive.entry("meetings", meeting_id) or {}
    recorded = entry.get("mcp")
    previous: dict[str, Any] = recorded if isinstance(recorded, dict) else {}
    if "files" not in mcp:
        mcp = {**{k: previous[k] for k in _RECOVERY_KEYS if k in previous}, **mcp}
    archive.put(
        "meetings",
        meeting_id,
        mcp={key: mcp[key] for key in _MCP_KEYS if mcp.get(key) is not None},
    )


def _archive_upstream_only(
    archive: Archive,
    client: McpProtocol,
    record: dict[str, Any],
    meeting_id: str,
    counts: SyncCounts,
    now: str,
    options: SyncOptions,
    problems: list[str],
) -> None:
    """Archive a meeting the local store does not have, under ``mcp/``.

    Deliberately not written into ``meetings/``. That namespace is counted
    against the database by ``verify``, so an extra entry there would make a
    healthy archive report a mismatch on every run -- and would set up a real
    collision the day the meeting finally syncs to the local store.

    The whole meeting is archived: every range of its notes, and every range
    of its transcript when it has one. Measured on 0.4.1, which asked once
    with no range: notes stopped at 12,000 characters, the transcript was
    never asked for, the directory was ``undated`` because the listing's
    ``start`` went unread, a retitle left a second directory beside the
    first, and every run fetched every such meeting again.

    Args:
        archive: The destination archive.
        client: An open MCP client.
        record: The search result.
        meeting_id: The validated meeting id.
        counts: Mutated with what was done.
        now: This run's timestamp.
        options: What this run was asked to do.
        problems: Receives why an archive is incomplete.
    """
    listing_hash = content_digest(record)[:16]
    entry = archive.entry(ENTITY_MCP_MEETINGS, meeting_id) or {}
    current = archive.existing_path(ENTITY_MCP_MEETINGS, meeting_id)
    if (
        not options.full
        and entry.get("listing_hash") == listing_hash
        and entry.get("assembly") == ASSEMBLY_VERSION
        and current is not None
        and current.is_dir()
    ):
        # Nothing the listing says about it has moved: no request at all.
        counts.unchanged += 1
        return

    when = _parsed(
        record.get("start") or record.get("start_time") or record.get("created_at")
    )
    directory = archive.resolve(
        ENTITY_MCP,
        "meetings",
        dated_prefix(when),
        record_dir_name(when, record.get("title"), meeting_id),
    )
    # A retitle moves the meeting rather than starting a second one.
    archive.relocate(ENTITY_MCP_MEETINGS, meeting_id, directory)

    counts.scanned += 1
    content = _fetch_ranges(
        client, meeting_id, "view_content", directory / "raw" / "content"
    )
    if content is None:
        counts.failed += 1
        return
    wrote = write_json_if_changed(directory / "raw" / "meeting.json", content.first)
    transcript = None
    unanswered = False
    if record.get("has_transcript"):
        transcript = _fetch_ranges(
            client, meeting_id, "view_transcript", directory / "raw" / "transcript"
        )
        # A failed request is already on record, with the server's reason.
        unanswered = transcript is None
    incomplete = [part for part in (content, transcript) if part and not part.complete]
    for part in incomplete:
        problems.append(f"meeting {meeting_id} incomplete: {part.reason}")

    manifest = {
        "meeting_id": meeting_id,
        "assembly": ASSEMBLY_VERSION,
        "content": content.summary(),
        "transcript": transcript.summary() if transcript else None,
    }
    wrote |= write_json_if_changed(directory / "raw" / "manifest.json", manifest)
    if transcript is not None and transcript.complete:
        wrote |= write_text_if_changed(
            directory / "transcript.mcp.md",
            _render_transcript(
                transcript.text,
                meeting_id,
                str(record.get("title") or ""),
                mismatch=transcript.mismatch,
            ),
        )

    fields: dict[str, Any] = {
        "path": archive.relative(directory),
        "title": record.get("title"),
        "source": SOURCE_MCP,
        "upstream_only": True,
    }
    if incomplete or unanswered:
        counts.failed += 1
    else:
        # Recorded only once everything is archived, so an incomplete meeting
        # is asked for again next run rather than skipped as unchanged.
        fields["listing_hash"] = listing_hash
        fields["assembly"] = ASSEMBLY_VERSION
    if wrote:
        # Only on a write. Passing None here would delete the timestamp the
        # last write set -- which is how every unchanged run used to churn
        # index.json.
        fields["archived_at"] = now
    archive.put(ENTITY_MCP_MEETINGS, meeting_id, **fields)
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
