"""Reading Wispr Flow's database without disturbing the app that owns it.

Two constraints shape everything here.

The database is live. Wispr Flow has ``flow.sqlite`` open in WAL mode and is
writing to it while this tool runs, so the connection is opened ``mode=ro``
with ``PRAGMA query_only`` and one deferred read transaction spans the whole
export. Every table then comes from a single consistent snapshot, and a torn
read across tables -- a meeting archived without the transcript rows that
existed when it was read -- becomes impossible rather than unlikely.

The schema moves constantly, around twenty migrations a month. So the reader
never names a column it did not discover: the projection is built from
``PRAGMA table_info`` at runtime, which means a column added upstream is
archived on the next run with no code change here. ``schema.EXPECTED`` is
consulted only to *report* the difference, and to exclude the screen-capture
columns that require an explicit opt-in.

The one rule that follows from being an archival tool: **fail loud, never fail
closed.** Breaking drift still archives everything reachable through the raw
path. It skips renderers and exits non-zero, naming what broke. An archive that
refuses to run because it does not recognize a column is no archive at all.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Self

from .normalize import decode_json
from .schema import (
    EXPECTED,
    MIGRATION_PIN,
    DriftClass,
    Layout,
    SchemaPin,
    TableSpec,
    pin_from_migrations,
)

__all__ = ["DriftClass", "Drift", "SourceError", "SqliteSource", "open_source"]

# A single value large enough to be a problem in memory. Meeting audio blobs
# and screenshots are the realistic cases.
MAX_BLOB_BYTES = 256 * 1024 * 1024


class SourceError(Exception):
    """The database could not be opened or read."""


@dataclass(frozen=True, slots=True)
class Drift:
    """What differs between the live database and ``schema.EXPECTED``.

    Attributes:
        kind: The classification.
        live: The pin computed from the live database.
        new_tables: Tables present live and undeclared.
        missing_tables: Tables declared and absent live.
        new_columns: Table to columns present live and undeclared.
        missing_columns: Table to columns declared and absent live.
        missing_required: Table to required columns that are absent. Any entry
            here makes the drift breaking.
    """

    kind: DriftClass
    live: SchemaPin
    new_tables: tuple[str, ...] = ()
    missing_tables: tuple[str, ...] = ()
    new_columns: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    missing_columns: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    missing_required: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def blocks_rendering(self) -> bool:
        """Report whether renderers must be skipped for affected tables."""
        return self.kind is DriftClass.BREAKING

    def summary(self) -> str:
        """Describe the drift in one line, naming what changed.

        Returns:
            A human-readable summary. Never empty, so a caller can log it
            unconditionally.
        """
        if self.kind is DriftClass.OK:
            return f"schema OK (pin {self.live.sha256[:12]} matches)"
        parts: list[str] = []
        if self.new_tables:
            parts.append(f"new tables: {', '.join(self.new_tables)}")
        if self.missing_tables:
            parts.append(f"missing tables: {', '.join(self.missing_tables)}")
        for table, columns in sorted(self.new_columns.items()):
            parts.append(f"new columns on {table}: {', '.join(columns)}")
        for table, columns in sorted(self.missing_columns.items()):
            parts.append(f"missing columns on {table}: {', '.join(columns)}")
        for table, columns in sorted(self.missing_required.items()):
            parts.append(f"REQUIRED columns missing on {table}: {', '.join(columns)}")
        if not parts:
            parts.append(
                f"migration set changed ({self.live.count} vs {MIGRATION_PIN.count})"
            )
        return f"{self.kind}: " + "; ".join(parts)


@dataclass(slots=True)
class Record:
    """One source row, split into what is archivable and what is binary.

    Attributes:
        table: Source table name.
        key: The primary key value, as text.
        data: JSON-serializable column values. Blobs are replaced by
            references and credentials by a redaction marker.
        blobs: Column name to raw bytes, for the caller to write as sidecar
            files. Empty unless the column was selected.
        soft_deleted: Whether upstream has tombstoned this row.
    """

    table: str
    key: str
    data: dict[str, Any]
    blobs: dict[str, bytes] = field(default_factory=dict)
    soft_deleted: bool = False


class SqliteSource:
    """A read-only view of one Wispr Flow database.

    Use as a context manager. Entering opens the connection and pins a read
    snapshot; leaving rolls back the read transaction and closes.
    """

    def __init__(self, path: Path, *, immutable: bool = False) -> None:
        """Prepare a reader without opening anything yet.

        Args:
            path: The database file.
            immutable: Open with ``immutable=1`` rather than ``mode=ro``.
                Required for the app's ``backups/*.sqlite`` copies, which ship
                without the ``-shm`` sibling SQLite expects; a plain read-only
                open of one fails ``SQLITE_CANTOPEN``.
        """
        self.path = path
        self.immutable = immutable
        self._connection: sqlite3.Connection | None = None

    @property
    def uri(self) -> str:
        """Return the SQLite URI this reader opens.

        Returns:
            A ``file:`` URI carrying the read-only or immutable flag.
        """
        mode = "immutable=1" if self.immutable else "mode=ro"
        # SQLite URIs are percent-decoded, so a literal '?' or '#' in the path
        # would otherwise be read as the start of the query or fragment.
        quoted = str(self.path).replace("?", "%3f").replace("#", "%23")
        return f"file:{quoted}?{mode}"

    def __enter__(self) -> Self:
        """Open the database and pin a consistent read snapshot.

        Returns:
            This reader.

        Raises:
            SourceError: The file is missing or cannot be opened.
        """
        if not self.path.exists():
            raise SourceError(f"no Wispr Flow database at {self.path}")
        try:
            # isolation_level=None puts the driver in autocommit, so the
            # explicit BEGIN below is the only transaction in play.
            connection = sqlite3.connect(self.uri, uri=True, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = 1")
            connection.execute("BEGIN DEFERRED")
            # A deferred transaction acquires nothing until its first read, so
            # the snapshot is not actually pinned until we touch a page. Read
            # the schema table to fix it now rather than at whichever query
            # happens to run first.
            connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
        except sqlite3.Error as error:
            raise SourceError(f"cannot open {self.path}: {error}") from error
        self._connection = connection
        return self

    def __exit__(self, *exc: object) -> None:
        """Roll back the read transaction and close the connection."""
        if self._connection is None:
            return
        try:
            self._connection.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        self._connection.close()
        self._connection = None

    @property
    def connection(self) -> sqlite3.Connection:
        """Return the open connection.

        Returns:
            The live connection.

        Raises:
            SourceError: Used outside a ``with`` block.
        """
        if self._connection is None:
            raise SourceError("SqliteSource must be used as a context manager")
        return self._connection

    # --- structure --------------------------------------------------------

    def tables(self) -> tuple[str, ...]:
        """List the database's tables, excluding SQLite's own.

        Returns:
            Table names, sorted.
        """
        rows = self.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        return tuple(row[0] for row in rows)

    def columns(self, table: str) -> tuple[str, ...]:
        """List a table's columns as the database reports them.

        This is the only source of column names used for reading, which is
        what makes an upstream addition free.

        Args:
            table: Table name, which must come from :meth:`tables`.

        Returns:
            Column names in declaration order.
        """
        rows = self.connection.execute(f'PRAGMA table_info("{table}")')
        return tuple(row[1] for row in rows)

    def primary_key(self, table: str) -> str | None:
        """Return a table's single-column primary key, if it has one.

        Args:
            table: Table name.

        Returns:
            The primary key column, or ``None`` for a composite or absent key.
        """
        rows = [row for row in self.connection.execute(f'PRAGMA table_info("{table}")')]
        keys = [row[1] for row in rows if row[5]]
        return keys[0] if len(keys) == 1 else None

    def row_count(self, table: str) -> int:
        """Count a table's rows.

        Args:
            table: Table name, which must come from :meth:`tables`.

        Returns:
            The number of rows, including soft-deleted ones.
        """
        row = self.connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()
        return int(row[0])

    def migrations(self) -> tuple[str, ...]:
        """List applied migration names.

        Returns:
            Every ``SequelizeMeta.name``, or an empty tuple when the table is
            absent -- which is itself a reportable state rather than a crash.
        """
        if "SequelizeMeta" not in self.tables():
            return ()
        rows = self.connection.execute("SELECT name FROM SequelizeMeta")
        return tuple(row[0] for row in rows)

    def pin(self) -> SchemaPin:
        """Fingerprint the live migration set.

        Returns:
            The live pin, for comparison against ``schema.MIGRATION_PIN``.
        """
        return pin_from_migrations(self.migrations())

    def detect_drift(self) -> Drift:
        """Compare the live schema against the declaration.

        Classification is deliberately lenient about additions and strict
        about removals. Additions are archived for free and warn; removals of
        a required column break a renderer and are named explicitly.

        Returns:
            The drift, always populated -- ``DriftClass.OK`` when nothing
            differs.
        """
        live_pin = self.pin()
        live_tables = set(self.tables())
        declared_tables = set(EXPECTED)

        new_tables = tuple(sorted(live_tables - declared_tables))
        missing_tables = tuple(sorted(declared_tables - live_tables))

        new_columns: dict[str, tuple[str, ...]] = {}
        missing_columns: dict[str, tuple[str, ...]] = {}
        missing_required: dict[str, tuple[str, ...]] = {}

        for table in sorted(live_tables & declared_tables):
            spec = EXPECTED[table]
            live_columns = set(self.columns(table))
            declared = set(spec.columns)
            if added := tuple(sorted(live_columns - declared)):
                new_columns[table] = added
            if removed := tuple(sorted(declared - live_columns)):
                missing_columns[table] = removed
            if absent := tuple(sorted(spec.required - live_columns)):
                missing_required[table] = absent
            # A changed primary key relocates every record in the table, so it
            # is breaking even when every column survives.
            live_key = self.primary_key(table)
            if live_key is not None and live_key != spec.pk:
                missing_required.setdefault(table, ())
                missing_required[table] = (*missing_required[table], f"pk:{spec.pk}")

        # OK means "nothing differs at all", so a dropped column counts even
        # when no renderer reads it and the migration set is unchanged. A
        # column can disappear without the pin moving -- a migration edited in
        # place, or a database restored from an unrelated install -- and
        # reporting that as clean would hide the one signal we have.
        unchanged = not (
            new_tables or new_columns or missing_columns or missing_tables
        )
        if missing_required or missing_tables:
            kind = DriftClass.BREAKING
        elif live_pin.count < MIGRATION_PIN.count:
            kind = DriftClass.STALE_SOURCE
        elif live_pin == MIGRATION_PIN and unchanged:
            kind = DriftClass.OK
        else:
            kind = DriftClass.ADDITIVE

        return Drift(
            kind=kind,
            live=live_pin,
            new_tables=new_tables,
            missing_tables=missing_tables,
            new_columns=new_columns,
            missing_columns=missing_columns,
            missing_required=missing_required,
        )

    # --- reading ----------------------------------------------------------

    def spec_for(self, table: str) -> TableSpec:
        """Return a table's declaration, synthesizing one when undeclared.

        A table introduced by a future migration has no entry in
        ``schema.EXPECTED``, and refusing to read it would defeat the point of
        a PRAGMA-driven reader. It gets a minimal spec built from what the
        database reports, so it is archived generically rather than skipped --
        or worse, raising and taking the whole run with it.

        Args:
            table: Table name, which must come from :meth:`tables`.

        Returns:
            The declared spec, or a synthesized one.
        """
        declared = EXPECTED.get(table)
        if declared is not None:
            return declared
        return TableSpec(
            pk=self.primary_key(table) or "rowid",
            layout=Layout.SNAPSHOT,
            columns=self.columns(table),
        )

    def records(
        self,
        table: str,
        *,
        include_screen_context: bool = False,
        include_blobs: bool = False,
        since: Any = None,
        since_column: str | None = None,
    ) -> Iterator[Record]:
        """Read a table's rows, coerced into archivable records.

        Soft-deleted rows are **returned**, not filtered. The archive keeps
        them and flags them: a record that upstream deleted is exactly the
        record an archive exists to preserve.

        Args:
            table: Table name, which must come from :meth:`tables`.
            include_screen_context: Select the screen-capture columns too.
            include_blobs: Read binary columns. When false they are reported
                as present but unread, so the archive records that they exist.
            since: Watermark value; only rows with a greater ``since_column``
                are returned. Compared in SQL against the raw stored value,
                which is why the watermark is stored raw rather than parsed.
            since_column: Column the watermark applies to.

        Yields:
            One :class:`Record` per row.
        """
        spec = self.spec_for(table)
        available = self.columns(table)
        projected = spec.projection(
            available, include_screen_context=include_screen_context
        )
        if not projected:
            return

        quoted = ", ".join(f'"{name}"' for name in projected)
        query = f'SELECT {quoted} FROM "{table}"'
        params: tuple[Any, ...] = ()
        if since is not None and since_column and since_column in available:
            query += f' WHERE "{since_column}" > ?'
            params = (since,)

        for row in self.connection.execute(query, params):
            yield self._coerce(spec, table, row, include_blobs=include_blobs)

    def _coerce(
        self,
        spec: TableSpec,
        table: str,
        row: sqlite3.Row,
        *,
        include_blobs: bool,
    ) -> Record:
        """Turn one raw row into a JSON-serializable record.

        Args:
            spec: The table's declaration.
            table: Table name, for the record.
            row: The raw row.
            include_blobs: Whether binary columns were selected.

        Returns:
            The coerced record.
        """
        data: dict[str, Any] = {}
        blobs: dict[str, bytes] = {}

        for column in row.keys():
            value = row[column]

            if column in spec.credentials:
                # A presigned URL grants access to whoever holds it. It is a
                # credential in a TEXT column, so its presence is recorded and
                # its value is not.
                data[column] = {"__redacted__": "credential"} if value else None
                continue

            if isinstance(value, bytes):
                reference: dict[str, Any] = {
                    "sha256": hashlib.sha256(value).hexdigest(),
                    "bytes": len(value),
                }
                if len(value) > MAX_BLOB_BYTES:
                    reference["truncated"] = True
                elif include_blobs:
                    blobs[column] = value
                else:
                    reference["archived"] = False
                data[column] = {"__blob__": reference}
                continue

            if column in spec.json_columns and isinstance(value, str) and value:
                decoded = decode_json(value)
                # Preserving the raw text rather than dropping it means a
                # column that stops being JSON is still archived losslessly.
                data[column] = (
                    decoded if decoded is not None else {"__unparsed__": value}
                )
                continue

            data[column] = value

        key = data.get(spec.pk)
        return Record(
            table=table,
            key="" if key is None else str(key),
            data=data,
            blobs=blobs,
            soft_deleted=spec.is_soft_deleted(data),
        )

    def max_value(self, table: str, column: str) -> Any:
        """Return the greatest value of a column, for advancing a watermark.

        Args:
            table: Table name, which must come from :meth:`tables`.
            column: Column name, which must come from :meth:`columns`.

        Returns:
            The maximum stored value, or ``None`` for an empty table.
        """
        if column not in self.columns(table):
            return None
        row = self.connection.execute(
            f'SELECT MAX("{column}") FROM "{table}"'
        ).fetchone()
        return row[0]


def open_source(path: Path, *, immutable: bool = False) -> SqliteSource:
    """Construct a reader for a database path.

    Args:
        path: The database file.
        immutable: Open ``immutable=1`` rather than ``mode=ro``.

    Returns:
        An unopened reader, to be used as a context manager.
    """
    return SqliteSource(path, immutable=immutable)


def fingerprint(path: Path) -> dict[str, Any]:
    """Cheaply describe a database's on-disk state.

    Used to short-circuit an entire sync pass when nothing has changed. This
    is an optimization and never a correctness mechanism: the WAL's modified
    time moves constantly while the app is running, so a mismatch only means
    "do the real work".

    Args:
        path: The database file.

    Returns:
        Sizes and modification times for the database and its write-ahead log.
    """
    result: dict[str, Any] = {}
    for label, candidate in (("db", path), ("wal", path.with_name(path.name + "-wal"))):
        try:
            stat = candidate.stat()
        except OSError:
            continue
        result[f"{label}_size"] = stat.st_size
        result[f"{label}_mtime_ns"] = stat.st_mtime_ns
    return result


def table_counts(source: SqliteSource, tables: Sequence[str]) -> dict[str, int]:
    """Count rows for several tables.

    Args:
        source: An open reader.
        tables: Table names.

    Returns:
        Table name to row count.
    """
    return {table: source.row_count(table) for table in tables}
