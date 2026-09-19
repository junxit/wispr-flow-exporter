"""How much this tool is willing to read back from a remote host.

Every remote read in this package used to materialize a whole response before
looking at it -- ``response.json()``, ``.text`` and ``.content`` all do -- and
httpx transparently decompresses, so the bytes that arrive and the bytes that
are allocated are not the same number. A server answering a few kilobytes of
gzip can hand back gigabytes of JSON, and nothing here would have objected
until the machine ran out of memory.

That is worth bounding even against a trusted host, because "trusted" is a
statement about intent and not about correctness. It is worth more now that
``endpoints`` exists: the point of validating a base URL is that the host is
not unconditionally ours, and a host that might not be ours should not be able
to choose how much memory this process allocates.

The cap is applied while streaming rather than after the fact, which is the
only ordering that helps. Reading the body and then checking its length is a
measurement, not a limit.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import cycle only matters to a checker
    import httpx

#: Generous next to any real response and still a bound. The largest thing this
#: tool legitimately reads over HTTP is a meeting transcript, which ``sync_mcp``
#: already refuses past eight million characters -- so this leaves roughly an
#: order of magnitude of headroom above the biggest documented payload.
MAX_RESPONSE_BYTES = 64 * 1024 * 1024


class ResponseTooLarge(Exception):
    """A response exceeded the byte cap and was abandoned part-read."""


def read_capped(
    response: httpx.Response, *, limit: int = MAX_RESPONSE_BYTES
) -> bytes:
    """Read a streaming response, refusing to buffer more than ``limit``.

    Iterates decoded bytes, which is the number that matters: httpx has already
    undone any ``Content-Encoding`` by this point, so this bounds what the
    process allocates rather than what crossed the wire.

    Args:
        response: An open streaming response.
        limit: Maximum decoded bytes to accumulate.

    Returns:
        The full body, when it fits.

    Raises:
        ResponseTooLarge: The body exceeded ``limit``. The connection is left
            for the caller's context manager to close, so the remainder is
            never read.
    """
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes():
        total += len(chunk)
        if total > limit:
            raise ResponseTooLarge(
                f"response exceeded {limit} bytes and was not read further"
            )
        chunks.append(chunk)
    return b"".join(chunks)
