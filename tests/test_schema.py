"""Schema declarations, the migration pin, and self-consistency.

Two of these run against a real Wispr Flow installation when one is present and
skip otherwise, mirroring granola-exporter's live-archive check. They are the
only thing that notices when ``EXPECTED`` falls behind reality, and at roughly
twenty migrations a month it will.
"""

from __future__ import annotations

import pytest

from wispr_flow_exporter import paths
from wispr_flow_exporter.schema import (
    EXPECTED,
    MIGRATION_PIN,
    RENDERED,
    Layout,
    TableSpec,
    pin_from_migrations,
)
from wispr_flow_exporter.sqlite_source import SqliteSource, open_source


def _live_source() -> SqliteSource | None:
    """Open the real database the way the tool itself does, or report its absence.

    Through ``open_source`` rather than a bare ``sqlite3.connect``, so the
    production path -- a URI built from "Application Support", ``mode=ro`` on a
    WAL database, ``query_only`` -- is exercised on the one machine that has a
    real store, instead of never.

    Returns:
        An unopened reader, or ``None`` when Wispr Flow is not installed.
    """
    resolved = paths.resolve()
    if not resolved.db.exists():
        return None
    return open_source(resolved.db, immutable=resolved.db_is_backup)


# --- the pin --------------------------------------------------------------


def test_pin_is_computed_from_sorted_names() -> None:
    """Order of discovery must not change the fingerprint."""
    forward = pin_from_migrations(["b.js", "a.js", "c.js"])
    backward = pin_from_migrations(["c.js", "b.js", "a.js"])

    assert forward == backward
    assert forward.count == 3
    assert forward.latest == "c.js"


def test_pin_detects_a_replaced_migration() -> None:
    """A swap that preserves count and maximum still changes the digest.

    This is the case the count and the latest name both miss, and the only
    reason the digest exists.
    """
    original = pin_from_migrations(["a.js", "b.js", "c.js"])
    swapped = pin_from_migrations(["a.js", "b-fixed.js", "c.js"])

    assert original.count == swapped.count
    assert original.latest == swapped.latest
    assert original.sha256 != swapped.sha256


def test_pin_of_no_migrations_is_empty_not_an_error() -> None:
    """An empty SequelizeMeta is a valid, reportable state."""
    assert pin_from_migrations([]).latest == ""


# --- self-consistency -----------------------------------------------------


@pytest.mark.parametrize("table", sorted(EXPECTED))
def test_declared_columns_are_internally_consistent(table: str) -> None:
    """Every column named in a spec must exist in that spec's column list.

    Catches the typo that would otherwise turn into a silently ineffective
    declaration -- a screen-context column that is never actually excluded, or
    a volatile column that never actually stops the churn it was added for.
    """
    spec = EXPECTED[table]
    columns = set(spec.columns)

    assert spec.key_columns, f"{table}: no primary key declared"
    assert set(spec.key_columns) <= columns, f"{table}: primary key not in columns"
    for label, named in (
        ("required", spec.required),
        ("volatile", spec.volatile),
        ("soft_delete", set(spec.soft_delete)),
        ("json_columns", spec.json_columns),
        ("blobs", spec.blobs),
        ("screen_context", spec.screen_context),
        ("credentials", spec.credentials),
        ("timestamps", set(spec.timestamps)),
    ):
        assert set(named) <= columns, f"{table}: {label} names an unknown column"
    if spec.date_column is not None:
        assert spec.date_column in columns


def test_rendered_tables_are_all_declared() -> None:
    """The render list cannot name a table with no spec."""
    assert set(RENDERED) <= set(EXPECTED)


def test_both_screen_context_tables_are_covered() -> None:
    """History is the obvious one; FlowLensHistory is the one that gets missed.

    FlowLensHistory carries the same screenshot/axText/axHTML triple, plus a
    userEmail column, so a tiering rule written against History alone would
    leak screen captures from the other table entirely.
    """
    for table in ("History", "FlowLensHistory"):
        assert {"screenshot", "axText", "axHTML"} <= EXPECTED[table].screen_context


def test_presigned_url_is_declared_a_credential() -> None:
    """A signed object URL is a bearer token that happens to live in TEXT."""
    assert EXPECTED["NoteImages"].credentials == frozenset({"presignedGetUrl"})


def test_unbounded_tables_are_sharded() -> None:
    """Tables that grow per-utterance must not be one file or one directory.

    A heavy dictation user produces thousands of History rows a day. A
    directory or a file each is a filesystem-hostile archive.
    """
    for table in ("History", "Polish", "InstructHistory", "FlowLensHistory"):
        assert EXPECTED[table].layout is Layout.SHARD
        assert EXPECTED[table].date_column is not None


# --- projection -----------------------------------------------------------

_AVAILABLE = ("transcriptEntityId", "formattedText", "screenshot", "axText", "axHTML")


def test_projection_excludes_screen_context_by_default() -> None:
    """The default export never selects a screen capture."""
    projected = EXPECTED["History"].projection(
        _AVAILABLE, include_screen_context=False
    )
    assert projected == ("transcriptEntityId", "formattedText")


def test_projection_includes_screen_context_when_opted_in() -> None:
    """The flag widens the projection, and only the flag does."""
    projected = EXPECTED["History"].projection(
        _AVAILABLE, include_screen_context=True
    )
    assert projected == _AVAILABLE


def test_projection_keeps_columns_it_has_never_heard_of() -> None:
    """An unknown column is archived, because the reader is PRAGMA-driven.

    This is what makes continuous schema drift cheap: a column added in
    migration 150 lands in the archive on the next run with no code change.
    """
    projected = EXPECTED["History"].projection(
        (*_AVAILABLE, "somethingNewInMigration150"), include_screen_context=False
    )
    assert "somethingNewInMigration150" in projected


def test_projection_excludes_by_name_not_by_position() -> None:
    """A new screen-capture column is only excluded once declared.

    The inverse of the test above, and the reason the invariant is worth
    stating: the failure mode of an upstream change is a missing field, never
    a silent screenshot dump, but that only holds for columns we know about.
    An undeclared capture column would be archived, which is why the drift
    report exists to surface it.
    """
    spec = TableSpec(
        pk="id", layout=Layout.SHARD, columns=("id",), screen_context=frozenset({"shot"})
    )
    assert spec.projection(("id", "shot"), include_screen_context=False) == ("id",)


# --- against a live installation ------------------------------------------


@pytest.mark.live
def test_pin_matches_the_live_database() -> None:
    """The declared pin must describe the installed app, or drift is reported."""
    source = _live_source()
    if source is None:
        pytest.skip("no local Wispr Flow database to check")
    with source:
        live = source.pin()

    assert live == MIGRATION_PIN, (
        f"schema drift: live pin is {live}. This is expected as Wispr Flow "
        "updates; refresh MIGRATION_PIN and re-check the column lists."
    )


@pytest.mark.live
def test_expected_covers_the_live_database() -> None:
    """Every live table and column must be declared, or drift is reported.

    Every difference is collected before asserting. Stopping at the first one
    meant a new table hid every column change behind it, so each re-pin became
    a loop of fix one thing, run again, find the next.
    """
    source = _live_source()
    if source is None:
        pytest.skip("no local Wispr Flow database to check")
    with source:
        live = {table: set(source.columns(table)) for table in source.tables()}

    stale: list[str] = []
    stale += [f"undeclared table {name}" for name in sorted(set(live) - set(EXPECTED))]
    stale += [f"declared table {name} is gone" for name in sorted(set(EXPECTED) - set(live))]
    for table in sorted(set(live) & set(EXPECTED)):
        declared = set(EXPECTED[table].columns)
        if added := sorted(live[table] - declared):
            stale.append(f"{table}: undeclared columns {', '.join(added)}")
        if gone := sorted(declared - live[table]):
            stale.append(f"{table}: declared columns gone {', '.join(gone)}")

    assert not stale, "the declaration is stale:\n  " + "\n  ".join(stale)
