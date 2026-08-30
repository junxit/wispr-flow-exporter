"""Shared fixtures and the fictional cast every test draws on.

Two rules govern everything in this file, and ``tests/test_privacy.py``
enforces both.

Fixtures are Python literals. No ``.ndjson``, ``.ogg``, ``.png`` or ``.sqlite``
fixture is ever committed, which is what lets ``.gitignore`` blanket-ignore
those extensions with no exceptions to erode. A test needing a database builds
one in ``tmp_path`` from DDL held here as a string; a test needing audio
synthesizes bytes, because no code path decodes audio -- it only copies it.

Every proper noun comes from the cast below. Not "a made-up name" but *the*
made-up names, so there is one table to audit. Real content is never
anonymized into a fixture either: swapping names out of a real transcript
leaves its cadence, topic and structure behind. The content here is invented to
exercise a parser.

The theme is sounds that are not quite speech, and the running subject is the
quarterly whisper budget.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from wispr_flow_exporter.schema import EXPECTED

# --- cast -----------------------------------------------------------------
# example.invalid is reserved by RFC 2606 and can never resolve or route. This
# is a deliberate divergence from granola-exporter, whose fixtures use a real
# vendor domain with live MX records.
OWNER = "Murmur Pike"
OWNER_EMAIL = "murmur@example.invalid"
SECOND = "Hush Delgado"
SECOND_EMAIL = "hush@example.invalid"
THIRD = "Static Vance"
THIRD_EMAIL = "static@example.invalid"
LATE = "Rumble Osei"
LATE_EMAIL = "rumble@example.invalid"
UNIDENTIFIED = "Speaker 2"

# --- identifiers ----------------------------------------------------------
# Every UUID-shaped string anywhere in src/ or tests/ must appear here.
# test_privacy.py asserts that, which is what stops a real meeting id from ever
# reaching the repository: there is no other way for one to get in.
MEETING_A = "0f2b6cf1-6d3a-4a5c-9d21-2f4e7b8c0a11"
MEETING_B = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
NOTE_A = "1b4e28ba-2fa1-11d2-883f-0016d3cca427"
HISTORY_A = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
HISTORY_B = "3f2504e0-4f89-41d3-9a0c-0305e82c3302"
HISTORY_C = "3f2504e0-4f89-41d3-9a0c-0305e82c3303"
HISTORY_D = "3f2504e0-4f89-41d3-9a0c-0305e82c3304"

# Rejected on purpose: the archive-key validator demands canonical lowercase.
UUID_UPPER = "0F2B6CF1-6D3A-4A5C-9D21-2F4E7B8C0A11"
UUID_SHORT = "0f2b6cf1-6d3a-4a5c-9d21-2f4e7b8c0a1"

APPROVED_UUIDS = frozenset(
    {
        MEETING_A,
        MEETING_B,
        NOTE_A,
        HISTORY_A,
        HISTORY_B,
        HISTORY_C,
        HISTORY_D,
        UUID_UPPER.lower(),
        UUID_SHORT,
    }
)

# --- credentials ----------------------------------------------------------
# Structurally valid and cryptographically meaningless: header and payload are
# base64 of {"alg":"HS256"} and {"sub":"murmur"}, and the signature segment is
# the literal text "signature-placeholder". The eyJ prefix trips the
# pre-publication audit grep on purpose; test_privacy.py allow-lists this exact
# value rather than relaxing the pattern.
FAKE_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJtdXJtdXIifQ.c2lnbmF0dXJlLXBsYWNlaG9sZGVy"
FAKE_SESSION_KEY = "sb-aaaaaaaaaaaaaaaaaaaa-auth-token"

# --- adversarial titles ---------------------------------------------------
# Each one is a path or frontmatter failure the renderer must survive.
TITLE_PLAIN = "Quarterly whisper budget: review"
TITLE_AMPERSAND = "Murmur & Hush weekly"
TITLE_EMPTY = ""
TITLE_TRAVERSAL = "../../../../etc/passwd"
TITLE_FRONTMATTER = "  \n---\ntitle: injected\n---  "

# --- a real database ------------------------------------------------------
# Two synthetic migrations, so no real migration name is embedded here. Tests
# that need the pin to match compute it from this list rather than asserting a
# hardcoded digest.
DEFAULT_MIGRATIONS = ("00000000000001-init.js", "00000000000002-add-meetings.js")


def _create_table(
    table: str,
    columns: Sequence[str],
    primary_key: str | None,
) -> str:
    """Build a CREATE TABLE statement for a set of columns.

    Column types are deliberately omitted. Nothing in this package reads a
    declared type: binary is detected from the runtime value being ``bytes``,
    and everything else is declared in ``schema.py``. Leaving columns untyped
    also gives them no affinity, so values round-trip exactly as inserted
    rather than being coerced by SQLite on the way in.

    Args:
        table: Table name.
        columns: Column names, in order.
        primary_key: Column to declare as the key, when it is still present.

    Returns:
        The statement.
    """
    quoted = [f'"{name}"' for name in columns]
    if primary_key and primary_key in columns:
        quoted.append(f'PRIMARY KEY("{primary_key}")')
    return f'CREATE TABLE "{table}" ({", ".join(quoted)})'


@pytest.fixture
def wispr_db(tmp_path: Path) -> Callable[..., Path]:
    """Build a real SQLite database matching the declared schema.

    A stub object would not do here. The behavior under test *is* SQLite
    metadata -- ``PRAGMA table_info`` is the drift detector's only input, and
    read-only open semantics against a live WAL database are the whole risk
    surface -- so faking it would fake the test.

    Schema is generated from ``schema.EXPECTED`` rather than pasted from a
    dump, which means the fixture cannot silently drift from the declaration
    it exists to exercise. ``extra_columns``, ``drop_columns`` and
    ``drop_tables`` deliberately reintroduce drift where a test wants it.

    Returns:
        A factory taking ``rows`` (table to list of row mappings) plus
        optional ``migrations``, ``extra_columns``, ``drop_columns``,
        ``drop_tables`` and ``extra_tables``, and returning the database path.
    """

    def build(
        rows: Mapping[str, Iterable[Mapping[str, Any]]] | None = None,
        *,
        migrations: Sequence[str] = DEFAULT_MIGRATIONS,
        extra_columns: Mapping[str, Sequence[str]] | None = None,
        drop_columns: Mapping[str, Sequence[str]] | None = None,
        drop_tables: Sequence[str] = (),
        extra_tables: Mapping[str, Sequence[str]] | None = None,
        name: str = "flow.sqlite",
    ) -> Path:
        path = tmp_path / name
        connection = sqlite3.connect(path)
        try:
            for table, spec in EXPECTED.items():
                if table in drop_tables:
                    continue
                columns = [
                    column
                    for column in spec.columns
                    if column not in (drop_columns or {}).get(table, ())
                ]
                columns.extend((extra_columns or {}).get(table, ()))
                connection.execute(_create_table(table, columns, spec.pk))

            for table, columns in (extra_tables or {}).items():
                connection.execute(_create_table(table, list(columns), None))

            if "SequelizeMeta" not in drop_tables:
                connection.executemany(
                    'INSERT INTO "SequelizeMeta" ("name") VALUES (?)',
                    [(entry,) for entry in migrations],
                )

            for table, records in (rows or {}).items():
                for record in records:
                    names = ", ".join(f'"{key}"' for key in record)
                    marks = ", ".join("?" for _ in record)
                    connection.execute(
                        f'INSERT INTO "{table}" ({names}) VALUES ({marks})',
                        tuple(record.values()),
                    )
            connection.commit()
        finally:
            connection.close()
        return path

    return build
