"""Command-line entry point for ``wispr-export``.

Every handler is a ``cmd_*(args) -> int`` returning a process exit code, and
``main`` does nothing but dispatch. Exit codes are part of the contract:

    0    success (may still carry additive schema-drift warnings)
    1    runtime failure, or one or more entities failed
    2    argparse usage error (reserved by argparse itself)
    3    additive schema drift, only under --strict-schema
    4    breaking schema drift, or a policy blocked data that should exist
    5    source unreachable
    130  interrupted; progress was saved

Code 3 is opt-in on purpose. Wispr Flow ships roughly twenty migrations a
month, so a non-zero exit on every new column would train the operator to
ignore the exit code -- and then to ignore code 4, which actually matters.
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from . import files_source, paths
from .local_config import read_config, read_session, redact
from .schema import EXPECTED, MIGRATION_PIN
from .sqlite_source import DriftClass, SourceError, open_source
from .store import Archive
# Aliased: this module's SOURCE_LOCAL is the CLI choice "local", while
# sync's is the backend key "wispr-local" that namespaces sync state.
from .sync import SOURCE_LOCAL as LOCAL_BACKEND
from .sync import SyncOptions, sync_local

SOURCE_AUTO = "auto"
SOURCE_LOCAL = "local"
SOURCE_CLOUD = "cloud"
SOURCE_BOTH = "both"
SOURCE_CHOICES = (SOURCE_AUTO, SOURCE_LOCAL, SOURCE_CLOUD, SOURCE_BOTH)

AUDIO_CHOICES = ("copy", "link", "skip")
ENTITIES = (
    "meetings",
    "notes",
    "todos",
    "dictionary",
    "calendar",
    "dictation",
    "account",
    "tables",
)

DEFAULT_ARCHIVE = "./archive"
DEFAULT_API_BASE = "https://api.wisprflow.ai"
DEFAULT_RECHECK_DAYS = 14
DEFAULT_MAX_AUDIO_MB = 512

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_ADDITIVE_DRIFT = 3
EXIT_BREAKING_DRIFT = 4
EXIT_SOURCE_UNREACHABLE = 5


@dataclass(slots=True)
class Config:
    """Resolved configuration for one invocation."""

    data_dir: str | None
    db: str | None
    archive_dir: Path
    source: str
    audio: str
    max_audio_mb: int
    include_screen_context: bool
    include_audio_blobs: bool
    include_images: bool
    recheck_days: int
    strict_schema: bool
    api_base: str
    session_file: str | None


def _flag(name: str, default: bool = False) -> bool:
    """Read a boolean environment variable.

    Args:
        name: Variable name.
        default: Value when unset or empty.

    Returns:
        The parsed flag.
    """
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    """Read an integer environment variable, falling back on anything odd.

    Args:
        name: Variable name.
        default: Value when unset or unparseable.

    Returns:
        The parsed integer.
    """
    try:
        return int(os.environ.get(name, "").strip())
    except ValueError:
        return default


def _config(args: argparse.Namespace) -> Config:
    """Build the configuration for this run.

    Precedence is CLI flag > real environment > ``.env`` > default;
    ``load_dotenv`` does not override an already-set variable, which is what
    makes the middle two orderings hold.

    Args:
        args: Parsed arguments.

    Returns:
        The resolved configuration.
    """
    load_dotenv()
    return Config(
        data_dir=getattr(args, "data_dir", None)
        or os.environ.get("WISPR_DATA_DIR", "").strip()
        or None,
        db=getattr(args, "db", None)
        or os.environ.get("WISPR_DB_PATH", "").strip()
        or None,
        archive_dir=Path(
            os.environ.get("WISPR_ARCHIVE_DIR", DEFAULT_ARCHIVE).strip()
            or DEFAULT_ARCHIVE
        )
        .expanduser()
        .resolve(),
        source=(
            getattr(args, "source", None)
            or os.environ.get("WISPR_SYNC_SOURCE", SOURCE_AUTO).strip()
            or SOURCE_AUTO
        ).lower(),
        audio=(
            getattr(args, "audio", None)
            or os.environ.get("WISPR_AUDIO", "copy").strip()
            or "copy"
        ).lower(),
        max_audio_mb=_int("WISPR_MAX_AUDIO_MB", DEFAULT_MAX_AUDIO_MB),
        include_screen_context=getattr(args, "include_screen_context", False)
        or _flag("WISPR_INCLUDE_SCREEN_CONTEXT"),
        include_audio_blobs=getattr(args, "include_audio_blobs", False)
        or _flag("WISPR_INCLUDE_AUDIO_BLOBS"),
        include_images=getattr(args, "include_images", False)
        or _flag("WISPR_INCLUDE_IMAGES"),
        recheck_days=_int("WISPR_RECHECK_DAYS", DEFAULT_RECHECK_DAYS),
        strict_schema=getattr(args, "strict_schema", False)
        or _flag("WISPR_STRICT_SCHEMA"),
        api_base=os.environ.get("WISPR_API_BASE", DEFAULT_API_BASE).strip()
        or DEFAULT_API_BASE,
        session_file=os.environ.get("WISPR_SESSION_FILE", "").strip() or None,
    )


def _human_bytes(count: int) -> str:
    """Format a byte count for a diagnostic line.

    Args:
        count: Number of bytes.

    Returns:
        A short human-readable size.
    """
    size = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _say(label: str, value: str) -> None:
    """Print one aligned diagnostic line, redacted.

    Redaction happens here, at the sink, rather than at each call site, so a
    line added later cannot forget to apply it.

    Args:
        label: Left-hand label.
        value: Right-hand value.
    """
    print(f"  {label:<13}: {redact(value)}")


def cmd_doctor(args: argparse.Namespace) -> int:
    """Report on the source, schema, policy and archive without writing.

    Writes nothing: not to the archive, not to Wispr Flow's directory, and no
    network request. It exists to answer "what would a sync do, and what is
    already impossible" before anything is committed to disk.

    Args:
        args: Parsed arguments.

    Returns:
        Process exit code.
    """
    config = _config(args)
    resolved = paths.resolve(config.data_dir, config.db)

    print("wispr-flow-exporter doctor")
    _say("data dir", str(resolved.data_dir))

    if not resolved.db.exists():
        _say("database", f"MISSING at {resolved.db}")
        print("\n  Wispr Flow does not appear to be installed here.")
        print("  Set WISPR_DATA_DIR or pass --data-dir if it lives elsewhere.")
        return EXIT_SOURCE_UNREACHABLE

    wal = resolved.db.with_name(resolved.db.name + "-wal")
    wal_note = f", WAL {_human_bytes(wal.stat().st_size)} pending" if wal.exists() else ""
    opened = "immutable=1" if resolved.db_is_backup else "mode=ro"
    _say(
        "database",
        f"{resolved.db.name}  {_human_bytes(resolved.db.stat().st_size)}"
        f"{wal_note}  opened {opened}",
    )
    if resolved.db_is_backup:
        _say("provenance", "reading one of the app's own backups, not the live store")

    exit_code = EXIT_OK
    try:
        with open_source(resolved.db, immutable=resolved.db_is_backup) as source:
            migrations = source.migrations()
            drift = source.detect_drift()
            _say("migrations", f"{len(migrations)}  latest {drift.live.latest or '-'}")
            _say("schema", drift.summary())
            if drift.kind is DriftClass.BREAKING:
                exit_code = EXIT_BREAKING_DRIFT
            elif drift.kind is DriftClass.ADDITIVE and config.strict_schema:
                exit_code = EXIT_ADDITIVE_DRIFT
            if drift.live != MIGRATION_PIN and drift.kind is not DriftClass.BREAKING:
                _say("", "raw archiving is unaffected; renderers may lag")

            counts = {table: source.row_count(table) for table in source.tables()}
    except SourceError as error:
        _say("database", f"UNREADABLE: {error}")
        return EXIT_SOURCE_UNREACHABLE

    config_state = read_config(resolved.config)
    policy = config_state.policy
    _say(
        "policy",
        f"localDataPolicy = {policy.local_data_policy or 'unknown'}   "
        f"transcript retention = {policy.transcript_retention or 'unknown'}",
    )

    populated = {table: n for table, n in sorted(counts.items()) if n}
    empty = sum(1 for n in counts.values() if not n)
    _say(
        "rows",
        ", ".join(f"{table} {n}" for table, n in populated.items())
        + f", ({empty} tables empty)",
    )

    marks = files_source.inventory(resolved.meetings)
    artifacts = marks["artifacts"]
    total = marks["directories"]
    _say(
        "meeting files",
        f"{total} dirs; "
        + ", ".join(f"{name} {artifacts[name]}/{total}" for name in artifacts)
        + f" ({_human_bytes(int(marks['audio_bytes']))} audio)",
    )

    session = read_session(
        Path(config.session_file).expanduser() if config.session_file else resolved.session
    )
    if not session.present:
        _say("session", "none stored; the cloud backend is unavailable")
    elif session.is_expired:
        _say(
            "session",
            "stored but EXPIRED — open Wispr Flow to refresh it, then re-run",
        )
    else:
        _say("session", f"valid until {session.expires_at:%Y-%m-%dT%H:%M:%SZ}")

    _say(
        "archive",
        f"{config.archive_dir}"
        + ("" if config.archive_dir.exists() else "  (not created yet)"),
    )

    if not policy.records_dictation:
        print()
        print("  WARNING: History is empty because localDataPolicy is")
        print(f'           "{policy.local_data_policy}". This is a Wispr Flow')
        print("           setting, not a failure of this tool. Dictation is")
        print("           never written to disk under it, so a local sync")
        print("           cannot recover past dictations -- only the cloud")
        print("           backend can. The policy and the time it was observed")
        print("           are recorded in the archive, so an empty dictation")
        print("           history can be told apart from an unread one.")
    return exit_code


def cmd_sync(args: argparse.Namespace) -> int:
    """Archive new and changed data.

    Args:
        args: Parsed arguments.

    Returns:
        Process exit code.
    """
    config = _config(args)
    resolved = paths.resolve(config.data_dir, config.db)
    if not resolved.db.exists():
        print(f"  no Wispr Flow database at {resolved.db}")
        return EXIT_SOURCE_UNREACHABLE

    archive = Archive(root=config.archive_dir)
    options = SyncOptions(
        full=getattr(args, "full", False),
        audio=config.audio,
        max_audio_mb=config.max_audio_mb,
        include_screen_context=config.include_screen_context,
        include_blobs=config.include_audio_blobs or config.include_images,
        verbose=getattr(args, "verbose", False),
        dry_run=getattr(args, "dry_run", False),
        recheck_days=config.recheck_days,
    )

    exit_code = EXIT_OK
    try:
        with open_source(resolved.db, immutable=resolved.db_is_backup) as source:
            drift = source.detect_drift()
            if drift.kind is not DriftClass.OK:
                _say("schema", drift.summary())
            if drift.kind is DriftClass.BREAKING:
                # Raw archiving still completes; only renderers are affected.
                # For an archival tool, failing loud must never mean failing
                # closed, so this is reported and the pass continues.
                exit_code = EXIT_BREAKING_DRIFT
            elif drift.kind is DriftClass.ADDITIVE and config.strict_schema:
                exit_code = EXIT_ADDITIVE_DRIFT

            state = archive.source_state(LOCAL_BACKEND)
            config_state = read_config(resolved.config)
            # Recorded on every run, so an archive that is empty because of a
            # preference can prove which preference, and when it was in force.
            state["sync_coordinator"] = config_state.sync_coordinator
            state["migration_pin"] = {
                "count": drift.live.count,
                "latest": drift.live.latest,
                "sha256": drift.live.sha256,
            }

            result = sync_local(
                archive,
                source,
                resolved,
                options,
                policy=config_state.policy,
                config=config_state,
                session=read_session(
                    Path(config.session_file).expanduser()
                    if config.session_file
                    else resolved.session
                ),
            )
    except SourceError as error:
        print(f"  source unreadable: {redact(str(error))}")
        return EXIT_SOURCE_UNREACHABLE

    for entity, counts in result.counts.items():
        _say("", counts.line(entity))
        if counts.bytes_copied:
            _say("", f"{entity}: {_human_bytes(counts.bytes_copied)} of media copied")
        if counts.failed:
            exit_code = EXIT_FAILURE

    if not config_state.policy.records_dictation:
        _say("", "dictation: 0 records — localDataPolicy is never_store")

    if result.interrupted:
        _say("", "interrupted; progress was saved and the next run resumes")
        return 130
    if options.dry_run:
        _say("", "dry run: nothing was written")
    return exit_code


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``wispr-export`` command.

    Args:
        argv: Argument vector; defaults to ``sys.argv[1:]``.

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(
        prog="wispr-export",
        description="Maintain a local archive of all Wispr Flow data.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_source(target: argparse.ArgumentParser) -> None:
        target.add_argument(
            "--source",
            choices=SOURCE_CHOICES,
            default=None,
            help="backend to use (default: auto — local, plus cloud when authorized)",
        )
        target.add_argument(
            "--data-dir",
            metavar="PATH",
            default=None,
            help="Wispr Flow application-support directory",
        )
        target.add_argument(
            "--db",
            metavar="PATH",
            default=None,
            help="flow.sqlite to read (a backup copy is opened immutable)",
        )

    doctor = sub.add_parser("doctor", help="validate source, schema, policy and archive")
    add_source(doctor)
    doctor.add_argument(
        "--strict-schema",
        action="store_true",
        help="exit 3 on additive schema drift instead of reporting it",
    )
    doctor.set_defaults(func=cmd_doctor)

    sync = sub.add_parser("sync", help="archive new and changed data")
    add_source(sync)
    sync.add_argument(
        "--audio",
        choices=AUDIO_CHOICES,
        default=None,
        help="meeting audio handling (default: copy)",
    )
    sync.add_argument(
        "--include-screen-context",
        action="store_true",
        help="archive screenshots and accessibility captures (requires --i-understand)",
    )
    sync.add_argument(
        "--i-understand",
        action="store_true",
        help="acknowledge that screen context can contain any application's contents",
    )
    sync.add_argument(
        "--include-audio-blobs",
        action="store_true",
        help="archive dictation audio blobs from History",
    )
    sync.add_argument(
        "--include-images",
        action="store_true",
        help="archive images pasted into scratchpad notes",
    )
    sync.add_argument(
        "--strict-schema",
        action="store_true",
        help="exit 3 on additive schema drift instead of warning",
    )
    sync.add_argument(
        "--full",
        action="store_true",
        help="ignore watermarks and re-check every record",
    )
    sync.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be written and touch nothing",
    )
    sync.add_argument("-v", "--verbose", action="store_true", help="per-record output")
    sync.set_defaults(func=cmd_sync)

    args = parser.parse_args(argv)

    # Two flags rather than one, because the widest tier includes captures of
    # whatever application had focus while dictating -- which can be a password
    # manager or a banking session. Opting in should not be possible by
    # autocompleting a single flag.
    if getattr(args, "include_screen_context", False) and not getattr(
        args, "i_understand", False
    ):
        parser.error(
            "--include-screen-context also requires --i-understand: it archives "
            "screenshots and accessibility captures of whatever was on screen "
            "while you dictated"
        )

    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
