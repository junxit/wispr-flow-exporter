"""Describing a remote shape without recording what was in it.

Both remote backends face the same problem: an interface with no stability
promise, no changelog, and no way to learn that a field was renamed except by
noticing. Neither can pin a schema the way the local backend pins a migration
list, so both fingerprint what comes back instead and report when the
fingerprint moves.

This module is the part of that machinery neither backend owns. It was factored
out of ``cloud_schema`` when the MCP backend became a third consumer; that
module still re-exports these names, so nothing that imported them from there
had to change.

Two properties carry the whole design, and both are load-bearing:

- **Values never survive.** A skeleton keeps types and field names and discards
  every value, so nothing a user said can reach ``.sync-state.json`` even as a
  digest input. Dictionary keys that do not look like field names collapse to
  ``<dynamic>``, so a response keyed by id cannot record one.
- **Length never matters.** A list collapses to the deduplicated union of its
  elements' skeletons, so one meeting and four hundred meetings fingerprint
  identically. Without that, every ordinary run would look like drift and the
  archive would rewrite itself every pass.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any, Protocol

# How deep the skeleton walk goes before it stops describing structure. Deep
# enough for every shape observed so far; bounded so a pathological response
# cannot make fingerprinting expensive.
MAX_DEPTH = 6

# A key that is part of the schema rather than part of the data. Anything else
# -- a UUID, an email, a date used as a map key -- is collapsed, so an id can
# never reach the state file through a key name.
_SAFE_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_DYNAMIC = "<dynamic>"


class Observation(Protocol):
    """The minimum a backend result must offer to be fingerprinted.

    Narrow on purpose: it lets one ``observe`` serve an HTTP client whose
    results carry status codes and an MCP client whose results carry tool
    names, without either importing the other.
    """

    @property
    def status(self) -> int | None:
        """A transport-level status, or ``None`` when there was none."""
        ...

    @property
    def payload(self) -> Any:
        """The decoded body, when the call succeeded."""
        ...

    @property
    def ok(self) -> bool:
        """Whether the call returned a usable body."""
        ...


def skeleton(value: Any, depth: int = 0) -> str:
    """Describe a value's structure with every value discarded.

    Lists collapse to the deduplicated union of their elements' skeletons, so
    the result does not depend on how many records came back. Dictionary keys
    that do not look like field names collapse to ``<dynamic>``, so a map keyed
    by id describes its values without naming one.

    Args:
        value: Any decoded JSON value.
        depth: Current recursion depth.

    Returns:
        A canonical structural description.
    """
    if depth > MAX_DEPTH:
        return "..."
    if value is None:
        return "null"
    # bool before int: bool is a subclass of int and would otherwise vanish.
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        inner = sorted({skeleton(item, depth + 1) for item in value})
        return "[" + "|".join(inner) + "]"
    if isinstance(value, dict):
        named: set[str] = set()
        dynamic: set[str] = set()
        for key, item in value.items():
            described = skeleton(item, depth + 1)
            if isinstance(key, str) and _SAFE_KEY.match(key):
                named.add(f"{key}:{described}")
            else:
                dynamic.add(described)
        parts = sorted(named) + [f"{_DYNAMIC}:{d}" for d in sorted(dynamic)]
        return "{" + ",".join(parts) + "}"
    return type(value).__name__


def fingerprint(payload: Any) -> str:
    """Digest a response's structure.

    Args:
        payload: A decoded response body.

    Returns:
        The first twelve hex characters of the skeleton's SHA-256 -- short
        enough to read in a report, wide enough not to collide in practice.
    """
    return hashlib.sha256(skeleton(payload).encode("utf-8")).hexdigest()[:12]


def field_names(payload: Any) -> tuple[str, ...]:
    """Name a response's fields, so a drift report can say what moved.

    A digest tells you a shape changed; these tell you which field. Only
    schema-shaped keys are kept, and only from the top level of an object or of
    the records in a list.

    Args:
        payload: A decoded response body.

    Returns:
        Sorted field names, empty when the body has none to offer.
    """
    if isinstance(payload, dict):
        return tuple(
            sorted(k for k in payload if isinstance(k, str) and _SAFE_KEY.match(k))
        )
    if isinstance(payload, list):
        found: set[str] = set()
        for item in payload:
            if isinstance(item, dict):
                found.update(
                    k for k in item if isinstance(k, str) and _SAFE_KEY.match(k)
                )
        return tuple(sorted(found))
    return ()


def observe(
    results: Mapping[str, Observation],
    previous: Mapping[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Build a shape ledger from one pass's results.

    Args:
        results: What each attempted call returned, keyed by archive name.
        previous: The ledger recorded by an earlier run, if any.

    Returns:
        A ledger keyed by name. ``observed_at`` is carried forward when neither
        the status nor the shape moved -- without that, the state file would
        change every run and the zero-bytes invariant would fail for every
        remote backend. ``Policy.as_dict`` solves the same problem the same way
        for the local one.
    """
    from .sync import _now

    earlier = previous or {}
    now = _now()
    ledger: dict[str, dict[str, Any]] = {}
    for name, result in results.items():
        shape = fingerprint(result.payload) if result.ok else None
        keys = list(field_names(result.payload)) if result.ok else []
        was = earlier.get(name)
        unchanged = (
            isinstance(was, Mapping)
            and was.get("status") == result.status
            and was.get("shape") == shape
        )
        ledger[name] = {
            "status": result.status,
            "shape": shape,
            "keys": keys,
            "observed_at": was.get("observed_at", now) if unchanged else now,
        }
    return ledger


def version_tuple(value: str | None) -> tuple[int, ...]:
    """Parse a dotted version into comparable integers.

    Args:
        value: A version string such as ``"1.6.721"``.

    Returns:
        The numeric components, empty when none could be read. A version that
        cannot be parsed compares equal to every other unparseable one, which
        keeps an unexpected format from being reported as a downgrade.
    """
    if not value:
        return ()
    parts: list[int] = []
    for chunk in value.split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)
