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
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .cloud_api import Endpoint, EndpointResult
from .drift import (
    MAX_DEPTH,
    FieldDelta,
    compare_fields,
    field_names,
    field_types,
    fingerprint,
    observe,
    recorded_fields,
    skeleton,
    version_tuple,
)
from .schema import DriftClass

# Re-exported: these moved to drift.py when the MCP backend became a third
# consumer, and callers that had imported them from here keep working.
__all__ = [
    "CLIENT_PIN",
    "MAX_DEPTH",
    "ClientPin",
    "CloudDrift",
    "detect_cloud_drift",
    "field_names",
    "fingerprint",
    "observe",
    "pin_from_endpoints",
    "skeleton",
]


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


@dataclass(frozen=True, slots=True)
class CloudDrift:
    """How far the live API has moved from what this tool declares.

    Attributes:
        kind: The classification.
        app_version: The installed Wispr Flow build, when it could be read.
        pin: The declared pin this was measured against.
        new_endpoints: Endpoints with no recorded fields yet.
        new_fields: Field paths that appeared, per endpoint.
        missing_fields: Field paths that disappeared, on evidence, per
            endpoint.
        retyped: Field paths whose type changed, per endpoint.
        broke: Endpoints that answered before and no longer do.
        recovered: Endpoints documented as unreachable that now answer.
        unreachable: Endpoints answering exactly the failure they document.
        unasked: Endpoints that could not be asked this run -- no network, a
            token that lapsed mid-run, a rate limit, a server error. Reported,
            and a failure of the run; not drift, because nothing about the
            interface was learned.
        empty: Endpoints that answered 204, with nothing to compare.
    """

    kind: DriftClass
    app_version: str | None
    pin: ClientPin
    new_endpoints: tuple[str, ...] = ()
    new_fields: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    missing_fields: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    retyped: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    broke: tuple[str, ...] = ()
    recovered: tuple[str, ...] = ()
    unreachable: tuple[str, ...] = ()
    unasked: tuple[str, ...] = ()
    empty: tuple[str, ...] = ()

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
        if self.retyped:
            for name, fields in sorted(self.retyped.items()):
                parts.append(f"fields retyped on {name}: {', '.join(fields)}")
        if self.recovered:
            parts.append(f"now answering: {', '.join(self.recovered)}")
        if self.new_fields:
            for name, fields in sorted(self.new_fields.items()):
                parts.append(f"new fields on {name}: {', '.join(fields)}")
        if self.new_endpoints:
            parts.append(f"newly recorded: {', '.join(self.new_endpoints)}")
        if self.app_version and self.app_version != self.pin.app_version:
            parts.append(
                f"app {self.app_version}, pinned {self.pin.app_version}"
            )
        notes: list[str] = []
        if self.unasked:
            notes.append(f"not answered this run: {', '.join(self.unasked)}")
        if self.empty:
            notes.append(f"no content: {', '.join(self.empty)}")
        if not parts:
            if self.unreachable:
                notes.append(f"{len(self.unreachable)} documented unreachable")
            suffix = "".join(f"; {note}" for note in notes)
            return f"cloud OK (pin {self.pin.sha256[:12]} matches){suffix}"
        return f"{self.kind}: " + "; ".join(parts + notes)


#: Statuses that say this run could not ask, not that the interface moved: a
#: token that lapsed or was refused, a timeout, a rate limit. Measured on
#: 0.4.1, each -- like a network failure and a 5xx -- was breaking drift, exit
#: 4, the code reserved for a change that needs a maintainer.
_UNASKED = frozenset({401, 403, 408, 429})


def _answer_kind(result: EndpointResult, expected: int) -> str:
    """Sort one result by what it says about the interface.

    Args:
        result: What the endpoint returned.
        expected: The status it is declared to answer with.

    Returns:
        ``"answered"``, ``"recovered"`` (answering where a failure is
        documented), ``"documented"`` (the documented failure), ``"empty"``
        (204), ``"unasked"`` (nothing learned this run) or ``"broke"``.
    """
    status = result.status
    if result.ok:
        return "recovered" if expected >= 400 else "answered"
    if status == expected and expected != 200:
        return "documented"
    if status == 204:
        return "empty"
    if status is None or status in _UNASKED or status >= 500:
        return "unasked"
    return "broke"


def _shown(paths: tuple[str, ...]) -> tuple[str, ...]:
    """Drop the root marker from field paths, for a report.

    Args:
        paths: Ledger paths.

    Returns:
        ``items[].title`` for ``$.items[].title``.
    """
    return tuple(path[2:] if path.startswith("$.") else path for path in paths)


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
    new_fields: dict[str, tuple[str, ...]] = {}
    missing_fields: dict[str, tuple[str, ...]] = {}
    retyped: dict[str, tuple[str, ...]] = {}
    sorted_into: dict[str, list[str]] = {
        "broke": [],
        "recovered": [],
        "documented": [],
        "unasked": [],
        "empty": [],
        "answered": [],
    }

    for name, result in results.items():
        declared = endpoints.get(name)
        expected = declared.expected_status if declared else 200
        sorted_into[_answer_kind(result, expected)].append(name)

        was = baseline.get(name)
        if not isinstance(was, Mapping):
            if not first_run:
                new_endpoints.append(name)
            continue
        if not result.ok:
            continue
        delta = compare_fields(recorded_fields(was), field_types(result.payload))
        if "fields" not in was:
            # Recorded before 0.5.0: top-level names only, so only a removal
            # can be judged. Everything nested would otherwise look new.
            delta = FieldDelta(removed=delta.removed)
        if delta.added:
            new_fields[name] = _shown(delta.added)
        if delta.removed:
            missing_fields[name] = _shown(delta.removed)
        if delta.retyped:
            retyped[name] = _shown(delta.retyped)

    broke = sorted_into["broke"]
    recovered = sorted_into["recovered"]
    live = version_tuple(app_version)
    pinned = version_tuple(against.app_version)
    moved = bool(new_endpoints or new_fields or retyped or broke or recovered)

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
        new_fields=new_fields,
        missing_fields=missing_fields,
        retyped=retyped,
        broke=tuple(sorted(broke)),
        recovered=tuple(sorted(recovered)),
        unreachable=tuple(sorted(sorted_into["documented"])),
        unasked=tuple(sorted(sorted_into["unasked"])),
        empty=tuple(sorted(sorted_into["empty"])),
    )


# Read from a live probe of a real account. A mismatch is not an error -- see
# the classification above -- but it is always reported. Refresh it with
# `wispr-export schema --source cloud`; MAINTENANCE.md has the procedure.
CLIENT_PIN = ClientPin(
    app_version="1.6.957",
    count=18,
    sha256="926960a4f81461ba363213ee8cc02ad35835d0d18cf8166e5b9d036694e319bc",
)
