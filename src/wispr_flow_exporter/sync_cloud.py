"""Archiving what the server has and the disk does not.

This pass is deliberately conservative. The API is undocumented, so its
response shapes are not a contract this tool can rely on, and guessing at them
would produce an archive that looks structured and is quietly wrong the first
time a field is renamed.

So responses are archived **verbatim** under ``cloud/``, one file per endpoint,
and the index records what was fetched and when. Where a response is obviously
a list of records carrying ids, the count is recorded too.

Mapping cloud records into the same directories as local ones stays *not* done,
and the reason is now stronger than caution. The entities that have a local
shape to map into -- meetings, notes, todos -- are exactly the ones no readable
endpoint returns; the app pulls them through write methods this tool does not
issue. What remains reachable has no local counterpart to merge with, so there
is nothing for a mapping layer to do. Verbatim is not a placeholder here; it is
the answer.

Some endpoints are declared knowing they cannot answer, so that every run
records the status as standing evidence. An endpoint answering exactly the
failure it documents is not counted as a failure -- a permanent ``FAILED`` on
every run is how an operator learns to stop reading the word.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Any

from .cloud_api import ENDPOINTS, CloudProtocol
from .secure_io import write_json_if_changed
from .store import Archive
from .sync import SyncCounts, SyncOptions, _now

SOURCE_CLOUD = "wispr-cloud"


def _record_count(payload: Any) -> int | None:
    """Count the records in a response, when it obviously has any.

    Args:
        payload: A decoded response body.

    Returns:
        The number of records, or ``None`` when the shape is not recognized.
        An unrecognized shape is reported as unknown rather than guessed at.
    """
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict):
        for key in ("items", "data", "results", "records", "meetings", "notes"):
            value = payload.get(key)
            if isinstance(value, list):
                return len(value)
    return None


#: Response fields that move on their own, and so must not decide whether a
#: file is rewritten. ``serverTime`` is the server's clock echoed back: it
#: changes on every request, and hashing it made two of the calendar endpoints
#: rewrite themselves on every run -- measured, not anticipated. The local
#: backend has the same problem with Sequelize's modifiedAt and solves it the
#: same way: archive the value verbatim, exclude it from the digest.
VOLATILE_FIELDS = frozenset({"serverTime", "server_time"})


def _projection(payload: Any) -> Any:
    """Drop self-moving fields before hashing.

    Top level only, which is where every observed one lives. Going deeper would
    risk dropping a record's own field of the same name, and there is no
    evidence any exists.

    Args:
        payload: A decoded response body.

    Returns:
        The body without its volatile fields. Non-objects pass through.
    """
    if not isinstance(payload, dict):
        return payload
    return {k: v for k, v in payload.items() if k not in VOLATILE_FIELDS}


def content_digest(payload: Any) -> str:
    """Digest what a response says, ignoring how it timestamped saying it.

    Args:
        payload: A decoded response body.

    Returns:
        A hex SHA-256 over the projected body.
    """
    text = json.dumps(
        _projection(payload), sort_keys=True, ensure_ascii=False, default=str
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


#: Markers a paginated response uses to say it handed back only a page.
#: Four adopted endpoints carry one -- meetings_shared, calendar,
#: calendar_prereads and notetaker_chats -- and each returned a single complete
#: page on the account this was measured against. That is an account-shaped
#: fact, not a guarantee, so truncation is detected rather than assumed absent.
_MORE_FLAGS = ("has_more", "hasMore")
_CURSOR_FLAGS = ("next_cursor", "nextCursor")


def truncated(payload: Any) -> bool:
    """Report whether a response says it withheld records.

    Archiving one page of a paginated endpoint and recording it as the whole
    thing is the failure mode this tool exists to avoid: an archive that is
    quietly short is worse than one that failed loudly. This does not page --
    that would need a different archive layout than one verbatim file per
    endpoint -- it detects, so the run can say so.

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


def _answered_as_documented(client: CloudProtocol, name: str) -> bool:
    """Report whether an endpoint failed in exactly the way it is declared to.

    Args:
        client: The client that made the request.
        name: The endpoint's archive name.

    Returns:
        ``True`` when the endpoint declares a failing status and returned it.
        A client that keeps no structural results -- a test fake, say -- always
        answers ``False``, so an unexpected failure is never excused by
        accident.
    """
    declared = ENDPOINTS.get(name)
    if declared is None or declared.expected_status < 400:
        return False
    result = getattr(client, "results", {}).get(name)
    return result is not None and result.status == declared.expected_status


def sync_cloud(
    archive: Archive,
    client: CloudProtocol,
    options: SyncOptions,
    endpoints: Sequence[str] = tuple(ENDPOINTS),
) -> SyncCounts:
    """Fetch and archive each endpoint's response verbatim.

    Args:
        archive: The destination archive.
        client: An open cloud client, or any object satisfying the protocol.
        options: What this run was asked to do.
        endpoints: Which named endpoints to fetch.

    Returns:
        What the pass did.
    """
    counts = SyncCounts()
    now = _now()

    for name in endpoints:
        counts.scanned += 1
        payload = client.fetch(name)
        if payload is None:
            if not _answered_as_documented(client, name):
                counts.failed += 1
            continue
        if options.dry_run:
            counts.written += 1
            continue

        destination = archive.resolve("cloud", f"{name}.json")
        digest = content_digest(payload)
        entry = archive.entry("cloud", name)
        if (
            entry is not None
            and entry.get("content_hash") == digest
            and not options.full
            and destination.is_file()
            # An endpoint that failed last run and returns the same body this
            # run still needs its entry rewritten, to clear the stale error.
            and not entry.get("last_error")
        ):
            counts.unchanged += 1
            continue

        wrote = write_json_if_changed(destination, payload)
        fields: dict[str, Any] = {
            "path": archive.relative(destination),
            "endpoint": ENDPOINTS[name].path,
            "records": _record_count(payload),
            "truncated": truncated(payload) or None,
            "content_hash": digest,
            # Cleared on success. put() drops None values, so this removes a
            # stale error rather than leaving an endpoint that has since
            # recovered looking permanently broken.
            "last_error": None,
            "source": SOURCE_CLOUD,
        }
        if wrote:
            fields["archived_at"] = now
        archive.put("cloud", name, **fields)
        counts.written += 1 if wrote else 0
        counts.unchanged += 0 if wrote else 1

    if options.dry_run:
        return counts
    for name, reason in getattr(client, "failures", []):
        # Recorded either way; flagged so the archive distinguishes an endpoint
        # that broke from one that was never going to answer.
        archive.put(
            "cloud",
            name,
            last_error=reason,
            documented_unreachable=_answered_as_documented(client, name) or None,
            endpoint=ENDPOINTS[name].path if name in ENDPOINTS else None,
            source=SOURCE_CLOUD,
        )
    return counts
