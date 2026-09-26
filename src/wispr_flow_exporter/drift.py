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

**What drift is measured by.** A digest says a shape moved, not what moved,
and it moves for things that are not drift: a value that was null last time,
a list that happens to be empty today. The ledger is therefore per path --
``$.items[].title`` and the types seen there -- and a path counts as removed
only on evidence: its parent was seen, as a non-empty object, without it, and
it was never optional. Measured on 0.4.1, which compared top-level names:
a record list that emptied reported every field gone, a null that became a
value reported the shape moved, and a field removed inside a record list was
not seen at all.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

# How deep the skeleton walk goes before it stops describing structure. Deep
# enough for every shape observed so far; bounded so a pathological response
# cannot make fingerprinting expensive.
MAX_DEPTH = 6

# A key that is part of the schema rather than part of the data. Anything else
# -- a UUID, an email, a date used as a map key -- is collapsed, so an id can
# never reach the state file through a key name. \Z rather than $, which would
# also accept a key ending in a newline.
_SAFE_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\Z")

_DYNAMIC = "<dynamic>"

#: Longer keys are data, whatever they look like: base32 ids of 181
#: characters pass the safe-key pattern.
MAX_KEY = 64

#: Marks a field some records of a list had and others did not. Its absence
#: is never evidence of a removal.
OPTIONAL = "?"


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


def _key(key: Any) -> str:
    """Name one field in a ledger path.

    Args:
        key: A JSON object key.

    Returns:
        The key itself when it is schema-shaped, else ``<dynamic>``.
    """
    if isinstance(key, str) and len(key) <= MAX_KEY and _SAFE_KEY.match(key):
        return key
    return _DYNAMIC


def _kind(value: Any) -> str | None:
    """Name a JSON value's type for the ledger.

    Args:
        value: A decoded value.

    Returns:
        The type's name; ``None`` for null, which records a path without a type
        -- so a field that was null last time and has a value now is not a
        change of type, and the reverse is not a removal.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return "list" if value else "[]"
    if isinstance(value, dict):
        return "object" if value else "{}"
    return type(value).__name__


def field_types(payload: Any) -> dict[str, frozenset[str]]:
    """Map every field path in a response to the types seen there.

    Paths read like ``$``, ``$.items``, ``$.items[]`` and
    ``$.items[].title``. Every value is discarded; only paths and type names
    remain. A field present in some records of a list and missing from others
    carries :data:`OPTIONAL`.

    Args:
        payload: A decoded response body.

    Returns:
        Path to the set of type names seen there.
    """
    found: dict[str, set[str]] = {}

    def walk(value: Any, path: str, depth: int) -> None:
        kinds = found.setdefault(path, set())
        kind = _kind(value)
        if kind is not None:
            kinds.add(kind)
        if depth >= MAX_DEPTH:
            return
        if isinstance(value, dict):
            for key, item in value.items():
                walk(item, f"{path}.{_key(key)}", depth + 1)
        elif isinstance(value, list):
            records = [item for item in value if isinstance(item, dict)]
            for item in value:
                walk(item, f"{path}[]", depth + 1)
            if len(records) > 1:
                named = [{_key(key) for key in record} for record in records]
                everywhere = named[0].intersection(*named[1:])
                for key in named[0].union(*named[1:]) - everywhere:
                    found.setdefault(f"{path}[].{key}", set()).add(OPTIONAL)

    walk(payload, "$", 0)
    return {path: frozenset(kinds) for path, kinds in found.items()}


@dataclass(frozen=True, slots=True)
class FieldDelta:
    """What moved between a recorded field ledger and this run's.

    Attributes:
        added: Paths seen for the first time.
        removed: Paths gone, on evidence -- see :func:`compare_fields`.
        retyped: Paths whose types no longer overlap what was recorded.
    """

    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    retyped: tuple[str, ...] = ()

    @property
    def moved(self) -> bool:
        """Report whether anything moved."""
        return bool(self.added or self.removed or self.retyped)


def _parent(path: str) -> str:
    """Return the path a field hangs from.

    Args:
        path: A ledger path.

    Returns:
        ``$.items[]`` for ``$.items[].title``, ``$.items`` for
        ``$.items[]``, and ``$`` for a top-level field.
    """
    if path.endswith("[]"):
        return path[:-2]
    return path.rsplit(".", 1)[0]


def _concrete(kinds: Any) -> set[str]:
    """Reduce recorded types to the ones that say what a value is.

    Args:
        kinds: Type names, as recorded or as seen.

    Returns:
        The names without the optional mark, empty containers counted as
        their kind.
    """
    aliases = {"[]": "list", "{}": "object"}
    return {aliases.get(kind, kind) for kind in kinds if kind != OPTIONAL}


def compare_fields(
    recorded: Mapping[str, Any], live: Mapping[str, frozenset[str]]
) -> FieldDelta:
    """Compare this run's field ledger with the recorded one.

    A path counts as removed only on evidence: its parent was seen this run as
    a non-empty object, and it was never marked optional. A list that came back
    empty is not evidence that its records lost their fields, and a field some
    records never had is not missing from the ones that lack it.

    Args:
        recorded: Path to type names, as stored.
        live: Path to type names, as seen this run.

    Returns:
        What was added, removed and retyped.
    """
    added = sorted(set(live) - set(recorded))
    removed = sorted(
        path
        for path in set(recorded) - set(live)
        if "object" in live.get(_parent(path), frozenset())
        and OPTIONAL not in recorded[path]
    )
    retyped = sorted(
        path
        for path in set(recorded) & set(live)
        if (was := _concrete(recorded[path]))
        and (now := _concrete(live[path]))
        and was.isdisjoint(now)
    )
    return FieldDelta(tuple(added), tuple(removed), tuple(retyped))


def merge_fields(
    recorded: Mapping[str, Any],
    live: Mapping[str, frozenset[str]],
    delta: FieldDelta,
) -> dict[str, list[str]]:
    """Build the ledger to store after a successful observation.

    What was seen is added to what was known, so a field of a record list is
    remembered through the runs when the list is empty. A path removed on
    evidence, or retyped, is recorded as it is now, so it is reported once
    rather than every run.

    Args:
        recorded: The stored ledger.
        live: This run's ledger.
        delta: How the two compare.

    Returns:
        Path to sorted type names, sorted by path.
    """
    merged: dict[str, set[str]] = {
        path: set(kinds)
        for path, kinds in recorded.items()
        if path not in delta.removed and isinstance(kinds, list | tuple | frozenset)
    }
    for path, kinds in live.items():
        base = set() if path in delta.retyped else merged.get(path, set())
        merged[path] = base | set(kinds)
    return {path: sorted(merged[path]) for path in sorted(merged)}


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


def recorded_fields(entry: Any) -> dict[str, Any]:
    """Read a stored ledger entry's fields, in either format.

    Args:
        entry: One endpoint's stored entry.

    Returns:
        Path to type names. An entry written before 0.5.0 recorded only
        top-level names, under ``keys``; those become ``$.<name>`` paths with
        no type, which can be seen removed but never retyped.
    """
    if not isinstance(entry, Mapping):
        return {}
    fields = entry.get("fields")
    if isinstance(fields, Mapping):
        return dict(fields)
    keys = entry.get("keys")
    if isinstance(keys, list):
        return {f"$.{key}": [] for key in keys if isinstance(key, str)}
    return {}


def observe(
    results: Mapping[str, Observation],
    previous: Mapping[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Build a field ledger from one pass's results.

    A call that failed carries the recorded fields forward untouched. It used
    to record no fields at all, so the next success compared against nothing.
    Measured on 0.4.1: fields ``a, b, c``, then a timeout, then ``a, b`` --
    and the removal of ``c`` was never reported.

    Args:
        results: What each attempted call returned, keyed by archive name.
        previous: The ledger recorded by an earlier run, if any.

    Returns:
        A ledger keyed by name. ``observed_at`` is carried forward when neither
        the status nor the fields moved -- without that, the state file would
        change every run and the zero-bytes invariant would fail for every
        remote backend. ``Policy.as_dict`` solves the same problem the same way
        for the local one.
    """
    from .sync import _now

    earlier = previous or {}
    now = _now()
    ledger: dict[str, dict[str, Any]] = {}
    for name, result in results.items():
        was = earlier.get(name)
        recorded: Mapping[str, Any] = was if isinstance(was, Mapping) else {}
        legacy = bool(recorded) and "fields" not in recorded
        if result.ok:
            # A legacy entry has been compared once, by detection; what it
            # knew was top-level names only, so the new ledger starts here.
            known = {} if legacy else recorded_fields(was)
            live = field_types(result.payload)
            entry: dict[str, Any] = {
                "status": result.status,
                "fields": merge_fields(known, live, compare_fields(known, live)),
            }
        elif legacy:
            # Kept as it was, so the next answer is still judged as legacy.
            entry = {**recorded, "status": result.status}
            entry.pop("observed_at", None)
        else:
            entry = {"status": result.status, "fields": recorded_fields(was)}
        unchanged = all(recorded.get(key) == value for key, value in entry.items())
        entry["observed_at"] = (
            recorded.get("observed_at", now) if recorded and unchanged else now
        )
        ledger[name] = entry
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
