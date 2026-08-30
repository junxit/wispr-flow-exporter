"""Drift detection for an interface that has never promised to hold still.

The local backend pins Wispr Flow's database by its migration list and reports
four kinds of drift against it. This is the same idea for the cloud backend,
where the thing that moves is not a schema but a private HTTP interface whose
only version number is the desktop app's own build.

Three pieces, mirroring ``schema.py`` and the drift half of ``sqlite_source``:

- **A pin.** ``CLIENT_PIN`` records the app build the endpoint table was
  validated against, plus a digest over the table itself, so a maintainer can
  tell at a glance whether the declaration has been re-checked since the app
  last updated.
- **A response-shape fingerprint per endpoint**, kept in ``.sync-state.json``.
  Archiving verbatim already means a changed shape costs a rendering rather
  than the data; the fingerprint is what makes it *visible* instead of silently
  absorbed.
- **The same four-way classification** the local backend reports, so one habit
  covers both backends.

The fingerprint discards every value and keeps only structure. That is a
privacy property as much as a correctness one -- a digest taken over real
response bodies would put user content in a state file -- and it is what keeps
the fingerprint stable: list *contents* and list *length* do not move it, so a
new meeting does not look like drift and the zero-bytes invariant holds.

See ``MAINTENANCE.md`` for what to do when this reports something.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .cloud_api import Endpoint, EndpointResult
from .schema import DriftClass

# How deep the skeleton walk goes before it stops describing structure. Deep
# enough for every shape observed so far; bounded so a pathological response
# cannot make fingerprinting expensive.
MAX_DEPTH = 6

# A key that is part of the schema rather than part of the data. Anything else
# -- a UUID, an email, a date used as a map key -- is collapsed, so an id can
# never reach the state file through a key name.
_SAFE_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_DYNAMIC = "<dynamic>"


@dataclass(frozen=True, slots=True)
class ClientPin:
    """A fingerprint of the endpoint table and the app build behind it.

    Attributes:
        app_version: The Wispr Flow build the table was validated against.
        count: How many endpoints are declared.
        sha256: Digest over the declaration, so an edited path or expected
            status moves the pin even when the count does not.
    """

    app_version: str
    count: int
    sha256: str


def pin_from_endpoints(
    endpoints: Mapping[str, Endpoint], app_version: str
) -> ClientPin:
    """Compute a pin from an endpoint table.

    Args:
        endpoints: The declared table.
        app_version: The app build it was validated against.

    Returns:
        The pin. Digest rule mirrors ``pin_from_migrations``: sort, join with
        newlines, SHA-256 the UTF-8 bytes.
    """
    lines = sorted(
        f"{name}\t{endpoint.path}\t{endpoint.expected_status}"
        for name, endpoint in endpoints.items()
    )
    digest = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
    return ClientPin(app_version=app_version, count=len(lines), sha256=digest)


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
    results: Mapping[str, EndpointResult],
    previous: Mapping[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Build the shape ledger from one pass's results.

    Args:
        results: What each attempted endpoint returned.
        previous: The ledger recorded by an earlier run, if any.

    Returns:
        A ledger keyed by endpoint name. ``observed_at`` is carried forward
        when neither the status nor the shape moved -- without that, the state
        file would change every run and the zero-bytes invariant would fail on
        the cloud backend. ``Policy.as_dict`` solves the same problem the same
        way for the local backend.
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


@dataclass(frozen=True, slots=True)
class CloudDrift:
    """How far the live API has moved from what this tool declares.

    Attributes:
        kind: The classification.
        app_version: The installed Wispr Flow build, when it could be read.
        pin: The declared pin this was measured against.
        new_endpoints: Endpoints with no recorded shape yet.
        changed_shapes: Endpoints whose fingerprint moved.
        new_fields: Fields that appeared, per endpoint.
        missing_fields: Fields that disappeared, per endpoint.
        broke: Endpoints that answered before and no longer do.
        recovered: Endpoints documented as unreachable that now answer.
        unreachable: Endpoints answering exactly the failure they document.
    """

    kind: DriftClass
    app_version: str | None
    pin: ClientPin
    new_endpoints: tuple[str, ...] = ()
    changed_shapes: tuple[str, ...] = ()
    new_fields: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    missing_fields: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    broke: tuple[str, ...] = ()
    recovered: tuple[str, ...] = ()
    unreachable: tuple[str, ...] = ()

    @property
    def blocks_rendering(self) -> bool:
        """Report whether interpretation should be skipped this run."""
        return self.kind is DriftClass.BREAKING

    def summary(self) -> str:
        """Describe the drift in one line.

        Returns:
            A sentence naming what moved. Never empty: an unremarkable run
            still says so, because "no output" and "nothing checked" look the
            same in a log.
        """
        parts: list[str] = []
        if self.broke:
            parts.append(f"stopped answering: {', '.join(self.broke)}")
        if self.missing_fields:
            for name, fields in sorted(self.missing_fields.items()):
                parts.append(f"fields gone from {name}: {', '.join(fields)}")
        if self.recovered:
            parts.append(f"now answering: {', '.join(self.recovered)}")
        if self.new_fields:
            for name, fields in sorted(self.new_fields.items()):
                parts.append(f"new fields on {name}: {', '.join(fields)}")
        if self.changed_shapes:
            parts.append(f"shape moved: {', '.join(self.changed_shapes)}")
        if self.new_endpoints:
            parts.append(f"newly recorded: {', '.join(self.new_endpoints)}")
        if self.app_version and self.app_version != self.pin.app_version:
            parts.append(
                f"app {self.app_version}, pinned {self.pin.app_version}"
            )
        if not parts:
            suffix = (
                f"; {len(self.unreachable)} documented unreachable"
                if self.unreachable
                else ""
            )
            return f"cloud OK (pin {self.pin.sha256[:12]} matches){suffix}"
        return f"{self.kind}: " + "; ".join(parts)


def _version_tuple(value: str | None) -> tuple[int, ...]:
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


def detect_cloud_drift(
    results: Mapping[str, EndpointResult],
    recorded: Mapping[str, Any] | None,
    endpoints: Mapping[str, Endpoint],
    app_version: str | None,
    pin: ClientPin | None = None,
) -> CloudDrift:
    """Classify one pass against the declaration and the recorded shapes.

    Two baselines are consulted, for the same reason the local backend consults
    two: the *declaration* in code says what each endpoint should answer with
    and is refreshed by a maintainer, while the *ledger* in the archive says
    what it answered last time and is refreshed every run.

    Args:
        results: What each attempted endpoint returned this pass.
        recorded: The ledger from an earlier run, or ``None`` on a first run.
        endpoints: The declared table.
        app_version: The installed Wispr Flow build, when it could be read.
        pin: The pin to measure against. Defaults to :data:`CLIENT_PIN`.

    Returns:
        The drift, always populated.
    """
    against = pin if pin is not None else CLIENT_PIN
    # A first run establishes the baseline rather than reporting all of it as
    # drift, exactly as a fresh archive does not report every migration.
    baseline = dict(recorded) if recorded else {}
    first_run = not baseline

    new_endpoints: list[str] = []
    changed_shapes: list[str] = []
    new_fields: dict[str, tuple[str, ...]] = {}
    missing_fields: dict[str, tuple[str, ...]] = {}
    broke: list[str] = []
    recovered: list[str] = []
    unreachable: list[str] = []

    for name, result in results.items():
        declared = endpoints.get(name)
        expected = declared.expected_status if declared else 200
        answered = result.ok

        if not answered and result.status == expected:
            unreachable.append(name)
        elif answered and expected >= 400:
            recovered.append(name)
        elif not answered and expected < 400:
            broke.append(name)

        was = baseline.get(name)
        if not isinstance(was, Mapping):
            if not first_run:
                new_endpoints.append(name)
            continue
        if not answered:
            continue
        shape = fingerprint(result.payload)
        if was.get("shape") != shape:
            changed_shapes.append(name)
            before = set(was.get("keys") or ())
            after = set(field_names(result.payload))
            if after - before:
                new_fields[name] = tuple(sorted(after - before))
            if before - after:
                missing_fields[name] = tuple(sorted(before - after))

    live = _version_tuple(app_version)
    pinned = _version_tuple(against.app_version)
    moved = bool(
        new_endpoints or changed_shapes or broke or recovered
    )

    if broke or missing_fields:
        kind = DriftClass.BREAKING
    elif live and pinned and live < pinned:
        kind = DriftClass.STALE_SOURCE
    elif not moved and (not app_version or app_version == against.app_version):
        kind = DriftClass.OK
    else:
        kind = DriftClass.ADDITIVE

    return CloudDrift(
        kind=kind,
        app_version=app_version,
        pin=against,
        new_endpoints=tuple(sorted(new_endpoints)),
        changed_shapes=tuple(sorted(changed_shapes)),
        new_fields=new_fields,
        missing_fields=missing_fields,
        broke=tuple(sorted(broke)),
        recovered=tuple(sorted(recovered)),
        unreachable=tuple(sorted(unreachable)),
    )


def _declared_lines(endpoints: Iterable[tuple[str, Endpoint]]) -> list[str]:
    """Render the declaration as sorted lines, for the pin and for reports."""
    return sorted(
        f"{name}\t{endpoint.path}\t{endpoint.expected_status}"
        for name, endpoint in endpoints
    )


# Read from a live probe of a real account. A mismatch is not an error -- see
# the classification above -- but it is always reported. Refresh it with
# `wispr-export schema --source cloud`; MAINTENANCE.md has the procedure.
CLIENT_PIN = ClientPin(
    app_version="1.6.721",
    count=18,
    sha256="926960a4f81461ba363213ee8cc02ad35835d0d18cf8166e5b9d036694e319bc",
)
