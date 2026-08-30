"""Archiving what the server has and the disk does not.

This pass is deliberately conservative. The API is undocumented, so its
response shapes are not a contract this tool can rely on, and guessing at them
would produce an archive that looks structured and is quietly wrong the first
time a field is renamed.

So responses are archived **verbatim** under ``cloud/``, one file per endpoint,
and the index records what was fetched and when. Where a response is obviously
a list of records carrying ids, the count is recorded too -- that is enough to
tell whether the server holds dictation the local store does not, which is the
question this backend exists to answer.

Mapping cloud records into the same directories as local ones is deliberately
*not* done here. Doing it correctly needs the response shapes confirmed against
a live account, and doing it incorrectly would mean two sources writing to the
same files with different ideas of what a record is. Verbatim first; structure
when it is known.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .cloud_api import ENDPOINTS, CloudProtocol
from .secure_io import write_json_if_changed
from .store import Archive, content_hash
from .sync import SyncCounts, SyncOptions, _now
from .schema import EXPECTED

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
    spec = EXPECTED["Meetings"]

    for name in endpoints:
        counts.scanned += 1
        payload = client.fetch(name)
        if payload is None:
            counts.failed += 1
            continue
        if options.dry_run:
            counts.written += 1
            continue

        destination = archive.resolve("cloud", f"{name}.json")
        digest = content_hash(spec, {"payload": payload})
        entry = archive.entry("cloud", name)
        if (
            entry is not None
            and entry.get("content_hash") == digest
            and not options.full
            and destination.is_file()
        ):
            counts.unchanged += 1
            continue

        wrote = write_json_if_changed(destination, payload)
        fields: dict[str, Any] = {
            "path": archive.relative(destination),
            "endpoint": ENDPOINTS[name],
            "records": _record_count(payload),
            "content_hash": digest,
            "source": SOURCE_CLOUD,
        }
        if wrote:
            fields["archived_at"] = now
        archive.put("cloud", name, **fields)
        counts.written += 1 if wrote else 0
        counts.unchanged += 0 if wrote else 1

    for name, reason in getattr(client, "failures", []):
        archive.put("cloud", name, last_error=reason, source=SOURCE_CLOUD)
    return counts
