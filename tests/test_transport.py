"""The HTTP policy both remote backends share: retries, limits, decoding.

Every body here is an unread stream, the way a socket delivers one, because
what these tests are about is what happens while it is read.
"""

from __future__ import annotations

import gzip
import tracemalloc
import zlib
from collections.abc import Iterator
from datetime import UTC, datetime

import httpx
import pytest

from wispr_flow_exporter.transport import (
    MAX_RETRY_AFTER,
    NotJson,
    ResponseRefused,
    ResponseTooLarge,
    UnsupportedEncoding,
    decode_json,
    read_capped,
    retry_after,
)

MIB = 1 << 20
NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
BODY = b'{"meetings": [], "has_more": false}' * 40


class _Body(httpx.SyncByteStream):
    """A response body that arrives in given reads and notices being read."""

    def __init__(self, *reads: bytes) -> None:
        """Hold the reads.

        Args:
            *reads: What each network read delivers, in order.
        """
        self.reads = reads
        self.touched = False

    def __iter__(self) -> Iterator[bytes]:
        """Deliver the reads.

        Yields:
            Each read in turn.
        """
        self.touched = True
        yield from self.reads


def _response(body: _Body, encoding: str | None = None) -> httpx.Response:
    """Wrap a body in an unread response.

    Args:
        body: The body.
        encoding: The ``Content-Encoding`` it declares, if any.

    Returns:
        The response.
    """
    headers = {"Content-Encoding": encoding} if encoding else {}
    return httpx.Response(200, headers=headers, stream=body)


def _raw_deflate(data: bytes) -> bytes:
    """Deflate without the zlib wrapper, as some servers send ``deflate``.

    Args:
        data: What to compress.

    Returns:
        The raw deflate stream.
    """
    squeeze = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)
    return squeeze.compress(data) + squeeze.flush()


def _zeros(size: int) -> bytes:
    """Gzip ``size`` zero bytes without ever holding them uncompressed.

    Args:
        size: Inflated length, in whole MiB.

    Returns:
        A body about a thousandth that size.
    """
    squeeze = zlib.compressobj(9, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
    block = bytes(MIB)
    parts = [squeeze.compress(block) for _ in range(size // MIB)]
    return b"".join([*parts, squeeze.flush()])


# --- Retry-After ------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "wait"),
    [
        ("3", 3.0),
        (" 3 ", 3.0),
        ("0", 0.0),
        ("Sat, 26 Sep 2026 12:00:30 GMT", 30.0),
        ("100000", MAX_RETRY_AFTER),
        ("Sat, 26 Sep 2026 13:00:00 GMT", MAX_RETRY_AFTER),
    ],
)
def test_a_retry_after_is_honored_up_to_a_minute(header: str, wait: float) -> None:
    """A server knows its own load, but it does not get to park a backup.

    The date form used to be ignored outright.
    """
    assert retry_after(header, now=NOW) == wait


@pytest.mark.parametrize(
    "header",
    ["-5", "nan", "inf", "soon", "", None, "Sat, 26 Sep 2026 11:59:00 GMT"],
)
def test_a_retry_after_that_says_nothing_usable_is_ignored(header: str | None) -> None:
    """The caller then waits its own ladder, never zero.

    Negative and NaN used to become zero: an immediate retry against a server
    that had just asked this client to wait.
    """
    assert retry_after(header, now=NOW) is None


# --- reading under the cap ---------------------------------------------------


@pytest.mark.parametrize(
    ("encoding", "body"),
    [
        (None, BODY),
        ("identity", BODY),
        ("gzip", gzip.compress(BODY)),
        ("x-gzip", gzip.compress(BODY)),
        ("GZIP", gzip.compress(BODY)),
        ("deflate", zlib.compress(BODY)),
        ("deflate", _raw_deflate(BODY)),
    ],
    ids=["none", "identity", "gzip", "x-gzip", "upper", "zlib", "raw-deflate"],
)
@pytest.mark.parametrize("size", [1, 7, 1 << 16], ids=["1B", "7B", "64KiB"])
def test_every_encoding_httpx_read_still_reads(
    encoding: str | None, body: bytes, size: int
) -> None:
    """However the reads fall, including one byte at a time.

    One-byte reads are the case worth naming: raw deflate is recognized by its
    zlib header failing to parse, and a first read of one byte fails nothing.
    """
    reads = [body[i : i + size] for i in range(0, len(body), size)]

    assert read_capped(_response(_Body(*reads), encoding)) == BODY


def test_a_body_exactly_at_the_cap_is_admitted() -> None:
    """The cap is a limit on what is kept, not a rounding of it."""
    body = gzip.compress(BODY)

    assert read_capped(_response(_Body(body), "gzip"), limit=len(BODY)) == BODY
    with pytest.raises(ResponseTooLarge):
        read_capped(_response(_Body(body), "gzip"), limit=len(BODY) - 1)


def test_a_compression_bomb_is_refused_at_the_cap_not_after_it() -> None:
    """No inflation step may allocate more than the cap still allows.

    The cap used to be checked after httpx had inflated each read whole, so a
    body arriving in one read was allocated in full before the check ran. Here
    32 KiB of gzip that inflates to 32 MiB meets a 1 MiB cap.
    """
    bomb = _zeros(32 * MIB)

    tracemalloc.start()
    try:
        with pytest.raises(ResponseTooLarge, match=f"exceeded {MIB} bytes"):
            read_capped(_response(_Body(bomb), "gzip"), limit=MIB)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak < 4 * MIB


def test_stacked_encodings_are_refused_before_a_byte_is_read() -> None:
    """Each layer multiplies the last, so no cap on the result could hold.

    Measured on 0.4.1: a 590-byte body declaring ``gzip, gzip`` allocated
    558 MiB before the 64 MiB cap refused it.
    """
    body = _Body(gzip.compress(gzip.compress(BODY)))

    with pytest.raises(UnsupportedEncoding, match="stacked"):
        read_capped(_response(body, "gzip, gzip"))
    assert not body.touched


@pytest.mark.parametrize("encoding", ["br", "zstd", "compress"])
def test_an_encoding_this_client_never_offered_is_refused(encoding: str) -> None:
    """Only gzip and deflate are offered, because only they can be bounded here.

    httpx offers br and zstd on its own whenever the optional packages happen
    to be installed, and would have inflated them unbounded.
    """
    body = _Body(b"\x00" * 64)

    with pytest.raises(UnsupportedEncoding, match=encoding):
        read_capped(_response(body, encoding))
    assert not body.touched


def test_a_body_that_is_not_what_it_declares_is_refused() -> None:
    """Garbage labeled gzip is a failure to report, not bytes to pass along."""
    with pytest.raises(ResponseRefused, match="undecodable gzip body"):
        read_capped(_response(_Body(b"hush, not gzip"), "gzip"))


# --- decoding -----------------------------------------------------------------


def test_json_decodes() -> None:
    """The ordinary case survives the wrapper."""
    assert decode_json(b'{"a": [1, 2]}') == {"a": [1, 2]}


@pytest.mark.parametrize(
    "body",
    [b"[" * 100_000, b"<html>maintenance</html>", b""],
    ids=["nested", "html", "empty"],
)
def test_what_is_not_json_is_reported_as_such(body: bytes) -> None:
    """Deep nesting included, which raised RecursionError and nothing caught it."""
    with pytest.raises(NotJson):
        decode_json(body)
