"""The HTTP policy both remote backends share: pacing, retries, and limits.

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

The cap is applied while streaming, and to what the body *inflates to*.
Capping after the read would be a measurement, not a limit, and capping after
httpx's own decoder is not quite a limit either: it inflates each network read
in one unbounded step, and a response declaring ``Content-Encoding: gzip,
gzip`` makes it chain two decoders whose combined ratio has no ceiling. So the
raw bytes are inflated here, with every step bounded by the budget that is
left, only encodings this client offered are accepted, and stacking is
refused before a byte is read.

The retry and pacing constants live here too, and not in either client. The
MCP client used to import them from the REST client, which made the borrowed
credential's module a dependency of the one backend built never to need it --
a coupling the test asserting the two credentials never meet could not see,
because it read source text rather than what Python imported.
"""

from __future__ import annotations

import json
import math
import zlib
from collections.abc import Iterator
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - import cycle only matters to a checker
    import httpx

#: Generous next to any real response and still a bound. The largest thing this
#: tool legitimately reads over HTTP is a meeting transcript, which ``sync_mcp``
#: already refuses past eight million characters -- so this leaves roughly an
#: order of magnitude of headroom above the biggest documented payload.
MAX_RESPONSE_BYTES = 64 * 1024 * 1024

MAX_RETRIES = 4
#: Paced conservatively. These are someone else's services and the archive is
#: never urgent; being a quiet client is worth more than being a fast one.
MIN_INTERVAL = 0.25
BACKOFF = (2.0, 5.0, 15.0, 30.0)
#: A server may ask for a pause; it may not park a backup for an hour.
MAX_RETRY_AFTER = 60.0

#: Sent explicitly. Left to itself httpx offers br and zstd whenever those
#: optional packages happen to be importable, and this module inflates only
#: what it can bound.
ACCEPT_ENCODING = "gzip, deflate"


class Retry(Exception):
    """Leave a streaming block, then wait and try again.

    Streaming means the retry decision is made inside a ``with``. Raising is how
    the response gets closed before the sleep, rather than held open across it.
    """

    def __init__(self, wait: float) -> None:
        """Remember how long to wait.

        Args:
            wait: Seconds before the next attempt.
        """
        super().__init__(wait)
        self.wait = wait


class ResponseRefused(Exception):
    """A response was abandoned unread, or part-read, rather than trusted."""


class ResponseTooLarge(ResponseRefused):
    """A response inflated past the byte cap."""


class UnsupportedEncoding(ResponseRefused):
    """A response used a content encoding this client did not offer."""


class NotJson(ValueError):
    """A body that should have been JSON could not be decoded as JSON."""


def retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """Parse a ``Retry-After`` header, in seconds or as an HTTP date.

    Args:
        value: The header value, if present.
        now: The current time, for the date form; defaults to the clock.

    Returns:
        Seconds to wait, capped at :data:`MAX_RETRY_AFTER`, or ``None`` --
        which sends the caller down its own backoff ladder -- when the header
        is absent, unreadable, negative, or not a finite number. Negative and
        ``NaN`` used to become zero: an immediate retry against a server that
        had just asked this client to wait.
    """
    if not value or not value.strip():
        return None
    text = value.strip()
    try:
        seconds = float(text)
    except ValueError:
        try:
            when = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (when - (now or datetime.now(tz=UTC))).total_seconds()
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return min(seconds, MAX_RETRY_AFTER)


def _coding(response: httpx.Response) -> str | None:
    """Name the one content encoding a response uses, refusing anything else.

    Args:
        response: An open streaming response.

    Returns:
        ``"gzip"`` or ``"deflate"``, or ``None`` for an identity body.

    Raises:
        UnsupportedEncoding: More than one coding, or one this client did not
            offer.
    """
    declared = [
        coding.strip().lower()
        for coding in response.headers.get("content-encoding", "").split(",")
        if coding.strip() and coding.strip().lower() != "identity"
    ]
    if len(declared) > 1:
        raise UnsupportedEncoding(
            f"refusing stacked content encodings: {', '.join(declared)}"
        )
    if not declared:
        return None
    if declared[0] in ("gzip", "x-gzip"):
        return "gzip"
    if declared[0] == "deflate":
        return "deflate"
    raise UnsupportedEncoding(f"refusing content encoding {declared[0]!r}")


class _Inflater:
    """Inflate a gzip or deflate body in steps no larger than the cap allows."""

    def __init__(self, coding: str) -> None:
        """Start decompressing.

        Args:
            coding: ``"gzip"`` or ``"deflate"``.
        """
        self.coding = coding
        wbits = 16 + zlib.MAX_WBITS if coding == "gzip" else zlib.MAX_WBITS
        self.inflate = zlib.decompressobj(wbits)
        # Deflate is ambiguous in the wild: zlib-wrapped per the RFC, raw from
        # some servers. Until the first output proves the wrapper is there,
        # the input is kept so it can be replayed without one -- as urllib3
        # does, and unlike a retry of only the latest read, which misses a
        # body whose first read was a single byte.
        self.replay: bytes | None = b"" if coding == "deflate" else None

    def feed(self, data: bytes, budget: int) -> Iterator[bytes]:
        """Inflate one raw read, producing at most ``budget + 1`` bytes.

        One byte past the budget is enough to know the cap was exceeded, and
        no single step can allocate more than that however well the body
        compresses. Bytes after the end of the stream are ignored, as httpx
        ignores them.

        Args:
            data: Compressed bytes as they arrived.
            budget: Bytes still allowed before the cap.

        Yields:
            Inflated pieces.

        Raises:
            ResponseRefused: The body is not valid for its declared encoding.
        """
        while not self.inflate.eof:
            try:
                piece = self.inflate.decompress(data, max(budget, 0) + 1)
            except zlib.error as error:
                if self.replay is None:
                    raise ResponseRefused(f"undecodable {self.coding} body") from error
                data, self.replay = self.replay + data, None
                self.inflate = zlib.decompressobj(-zlib.MAX_WBITS)
                continue
            if self.replay is not None:
                self.replay = None if piece else self.replay + data
            budget -= len(piece)
            if piece:
                yield piece
            if budget < 0:
                return
            # A step that filled its output may have left output pending with
            # no input left to push it out, so keep asking until one yields
            # nothing.
            data = self.inflate.unconsumed_tail
            if not data and not piece:
                return


def iter_capped(
    response: httpx.Response, *, limit: int = MAX_RESPONSE_BYTES
) -> Iterator[bytes]:
    """Stream a response's body, refusing to produce more than ``limit`` bytes.

    Args:
        response: An open streaming response.
        limit: Maximum inflated bytes.

    Yields:
        The body, decoded from its content encoding, in pieces.

    Raises:
        UnsupportedEncoding: The body uses an encoding this client cannot bound.
        ResponseTooLarge: The body inflated past ``limit``. The connection is
            left for the caller's context manager to close, so the remainder is
            never read.
    """
    coding = _coding(response)
    inflater = _Inflater(coding) if coding else None
    total = 0
    for raw in response.iter_raw():
        pieces = inflater.feed(raw, limit - total) if inflater else (raw,)
        for piece in pieces:
            total += len(piece)
            if total > limit:
                raise ResponseTooLarge(
                    f"response exceeded {limit} bytes and was not read further"
                )
            yield piece


def read_capped(response: httpx.Response, *, limit: int = MAX_RESPONSE_BYTES) -> bytes:
    """Read a streaming response whole, within the cap.

    Args:
        response: An open streaming response.
        limit: Maximum inflated bytes.

    Returns:
        The full body, decoded.
    """
    return b"".join(iter_capped(response, limit=limit))


def decode_json(raw: bytes | str) -> Any:
    """Decode JSON from a remote host, as a value or as a clean failure.

    ``json.loads`` raises ``RecursionError`` rather than ``ValueError`` for a
    body nested a few thousand deep -- forty kilobytes of brackets is enough --
    and no caller caught that, so one such response ended the run.

    Args:
        raw: The body.

    Returns:
        The decoded value.

    Raises:
        NotJson: The body is not JSON, or is nested too deeply to decode.
    """
    try:
        return json.loads(raw)
    except (ValueError, RecursionError) as error:
        raise NotJson(f"not JSON: {type(error).__name__}") from error
