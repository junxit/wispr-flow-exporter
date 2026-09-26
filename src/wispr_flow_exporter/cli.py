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
import json
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from dotenv import dotenv_values

from . import files_source, paths
from .endpoints import OVERRIDE_ENV, EndpointError, validated_endpoint
from .local_config import LocalConfig, printable, read_config, read_session, redact
from .prompts import Answers, PromptAborted, collect, ensure_ignored
from .schema import EXPECTED, MIGRATION_PIN
from .sqlite_source import DriftClass, SourceError, open_source
from .store import Archive, ArchiveBusy

# Aliased: this module's SOURCE_LOCAL is the CLI choice "local", while
# sync's is the backend key "wispr-local" that namespaces sync state.
from .sync import SOURCE_LOCAL as LOCAL_BACKEND
from .sync import SyncOptions, SyncResult, rerender, sync_local
from .verify import verify_archive

if TYPE_CHECKING:  # pragma: no cover - the MCP modules load only when used
    from .mcp_auth import McpCredential

SOURCE_AUTO = "auto"
SOURCE_LOCAL = "local"
SOURCE_CLOUD = "cloud"
SOURCE_BOTH = "both"
SOURCE_MCP = "mcp"
SOURCE_ALL = "all"
SOURCE_CHOICES = (
    SOURCE_ALL,
    SOURCE_AUTO,
    SOURCE_LOCAL,
    SOURCE_CLOUD,
    SOURCE_BOTH,
    SOURCE_MCP,
)

# Which backends each choice runs. "auto" is retained as an explicit way to ask
# for the old local-only behavior; "both" is retained as local + cloud.
_BACKENDS: Mapping[str, frozenset[str]] = {
    SOURCE_AUTO: frozenset({SOURCE_LOCAL}),
    SOURCE_LOCAL: frozenset({SOURCE_LOCAL}),
    SOURCE_CLOUD: frozenset({SOURCE_CLOUD}),
    SOURCE_MCP: frozenset({SOURCE_MCP}),
    SOURCE_BOTH: frozenset({SOURCE_LOCAL, SOURCE_CLOUD}),
    SOURCE_ALL: frozenset({SOURCE_LOCAL, SOURCE_CLOUD, SOURCE_MCP}),
}


def _backends(source: str) -> frozenset[str]:
    """Return which backends a ``--source`` choice runs.

    Args:
        source: A member of :data:`SOURCE_CHOICES`.

    Returns:
        The backend names.
    """
    return _BACKENDS.get(source, frozenset({SOURCE_LOCAL}))

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
# Kept beside the REST base for symmetry; the canonical values live in
# mcp_auth, which is imported lazily so a local run never loads it.
DEFAULT_MCP_ENDPOINT = "https://api.wisprflow.ai/connect/mcp"
MCP_ENDPOINT_ENV = "WISPR_MCP_ENDPOINT"
DEFAULT_RECHECK_DAYS = 14
DEFAULT_MAX_AUDIO_MB = 512

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
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
    mcp_endpoint: str
    session_file: str | None


# The one setting of this tool's own that a .env may not supply. It is the
# consent half of redirecting a credential to a host this tool does not ship,
# and the reason two settings are required is that a redirect and the consent
# to it cannot both be accidents -- which they can if one planted file carries
# both.
_ENVIRONMENT_ONLY = frozenset({OVERRIDE_ENV})

# .env files already reported, so a run that resolves its configuration twice
# (the interactive setup does) says so once.
_ANNOUNCED_DOTENV: set[Path] = set()


def _load_dotenv(path: Path) -> None:
    """Apply this tool's settings from a ``.env``, and nothing else.

    ``load_dotenv`` exports every key in the file, and the process then honours
    them -- including ``HTTPS_PROXY`` and ``SSL_CERT_FILE``, which httpx reads by
    default. Measured: a ``.env`` in the working directory setting those two
    routed every request through a proxy of the file's choosing while trusting
    a CA of its choosing, which reads the account's bearer token in cleartext
    no matter which host ``WISPR_API_BASE`` names. ``XDG_CONFIG_HOME`` moved
    the MCP refresh token into the working directory.

    So only ``WISPR_*`` keys are applied, and never :data:`_ENVIRONMENT_ONLY`.
    Anything else that genuinely needs setting -- a proxy, a CA bundle -- comes
    from the real environment, where the operator put it on purpose. Ignored
    keys are named on stderr, never their values, so a ``--json`` report on
    stdout stays parseable.

    Real environment variables still win: nothing here overwrites one.

    Args:
        path: The ``.env`` to read. Absent is fine.
    """
    if not path.is_file():
        return
    ignored: list[str] = []
    for name, value in dotenv_values(path).items():
        if value is None:
            continue
        if name.startswith("WISPR_") and name not in _ENVIRONMENT_ONLY:
            os.environ.setdefault(name, value)
        else:
            ignored.append(name)
    if ignored and path not in _ANNOUNCED_DOTENV:
        _ANNOUNCED_DOTENV.add(path)
        print(
            f"  note         : ignored {', '.join(sorted(ignored))} in {path}; a "
            f".env may only set WISPR_* options, and never {OVERRIDE_ENV}",
            file=sys.stderr,
        )


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

    Precedence is CLI flag > real environment > ``.env`` > default; the
    ``.env`` never overrides an already-set variable, which is what makes the
    middle two orderings hold.

    The ``.env`` is read from the working directory and nowhere else -- the
    default search walks up to the filesystem root, which let a file in any
    ancestor set ``WISPR_API_BASE`` -- and only this tool's own settings are
    taken from it. See :func:`_load_dotenv`.

    Args:
        args: Parsed arguments.

    Returns:
        The resolved configuration.

    Raises:
        EndpointError: A configured base URL is not one a credential may be
            sent to.
    """
    _load_dotenv(Path.cwd() / ".env")
    allow_override = _flag(OVERRIDE_ENV)
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
            or os.environ.get("WISPR_SYNC_SOURCE", SOURCE_ALL).strip()
            or SOURCE_ALL
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
        api_base=validated_endpoint(
            os.environ.get("WISPR_API_BASE", "").strip(),
            default=DEFAULT_API_BASE,
            variable="WISPR_API_BASE",
            allow_override=allow_override,
        ),
        mcp_endpoint=validated_endpoint(
            os.environ.get(MCP_ENDPOINT_ENV, "").strip(),
            default=DEFAULT_MCP_ENDPOINT,
            variable=MCP_ENDPOINT_ENV,
            allow_override=allow_override,
        ),
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
    # Flushed: an interactive flow prints a URL the operator has to act on,
    # and a buffered stdout shows it only after the step it belongs to.
    # printable() here too: values carry text from remote hosts and from the
    # login listener, and a terminal escape in one should arrive as text.
    print(f"  {label:<13}: {redact(printable(value))}", flush=True)


def _announce(archive: Archive) -> None:
    """Print what loading the archive found, before the command's own output.

    Args:
        archive: The archive just opened.
    """
    for notice in archive.notices:
        _say("archive", notice)


def _defaults() -> Answers:
    """Build the interactive defaults from the environment and .env.

    Deliberately the same resolution the flags use, so the prompt offers what
    a bare ``sync`` would actually do rather than a second set of defaults
    that could drift from it.

    Returns:
        The values to offer.
    """
    config = _config(argparse.Namespace())
    return Answers(
        data_dir=config.data_dir,
        archive_dir=str(config.archive_dir),
        source=SOURCE_LOCAL if config.source == SOURCE_AUTO else config.source,
        entities=None,
        audio=config.audio,
        max_audio_mb=config.max_audio_mb,
        include_audio_blobs=config.include_audio_blobs,
        include_images=config.include_images,
        include_screen_context=config.include_screen_context,
        recheck_days=config.recheck_days,
        full=False,
        strict_schema=config.strict_schema,
    )


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
    total = marks.directories
    _say(
        "meeting files",
        f"{total} dirs; "
        + ", ".join(
            f"{name} {count}/{total}" for name, count in marks.artifacts.items()
        )
        + f" ({_human_bytes(marks.audio_bytes)} audio)",
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
        print("           never written to disk under it, and the server has")
        print("           no endpoint that reads it back, so PAST DICTATION")
        print("           TEXT CANNOT BE RECOVERED -- by this tool or any")
        print("           other. Change the setting in Wispr Flow and what you")
        print("           dictate from then on becomes archivable. The cloud")
        print("           backend can still reach the totals: word counts,")
        print("           durations, streaks and per-day activity.")
        print("           The policy and the time it was observed are recorded")
        print("           in the archive, so an empty dictation history can be")
        print("           told apart from an unread one.")
    return exit_code


def cmd_sync(args: argparse.Namespace) -> int:
    """Archive new and changed data.

    Args:
        args: Parsed arguments.

    Returns:
        Process exit code.
    """
    config = _config(args)
    if config.include_screen_context and not getattr(args, "i_understand", False):
        # main() refuses the bare flag before any command runs. This catches the
        # same widening arriving from the environment or a .env, which used to
        # skip the acknowledgement entirely: WISPR_INCLUDE_SCREEN_CONTEXT=1 on
        # its own archived screen captures.
        print(
            "  WISPR_INCLUDE_SCREEN_CONTEXT also requires --i-understand on the "
            "command line: it archives screenshots and accessibility captures of "
            "whatever was on screen while you dictated",
            file=sys.stderr,
        )
        return EXIT_USAGE
    resolved = paths.resolve(config.data_dir, config.db)
    backends = _backends(config.source)
    runs_local = SOURCE_LOCAL in backends
    if runs_local and not resolved.db.exists():
        print(f"  no Wispr Flow database at {resolved.db}")
        return EXIT_SOURCE_UNREACHABLE

    try:
        entities = _entities(args)
    except ValueError as error:
        print(f"  {error}")
        return EXIT_FAILURE

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
    # A dry run touches nothing: not the archive, and not a repository's
    # .gitignore either. The passes still mutate the index in memory to count
    # what they would write, so the archive refuses to persist it.
    archive = Archive(root=config.archive_dir, read_only=options.dry_run)
    _announce(archive)
    try:
        with archive.lock():
            return _sync_passes(args, config, resolved, archive, options, entities)
    except ArchiveBusy as error:
        print(f"  {error}", file=sys.stderr)
        return EXIT_FAILURE


def _sync_passes(
    args: argparse.Namespace,
    config: Config,
    resolved: paths.WisprPaths,
    archive: Archive,
    options: SyncOptions,
    entities: tuple[str, ...],
) -> int:
    """Run every selected backend's pass against an archive this run holds.

    Args:
        args: Parsed arguments.
        config: This run's configuration.
        resolved: Resolved source paths.
        archive: The destination archive, already locked.
        options: What this run was asked to do.
        entities: Which local entity passes to perform.

    Returns:
        Process exit code.
    """
    backends = _backends(config.source)
    runs_local = SOURCE_LOCAL in backends
    if not options.dry_run:
        ensure_ignored(config.archive_dir)

    exit_code = EXIT_OK
    result = SyncResult()
    config_state = read_config(resolved.config)
    # Said before anything is contacted. The default reaches two remote
    # services, so which ones is never left to be inferred from the output.
    _say("backends", ", ".join(sorted(backends)))
    try:
        if runs_local:
            exit_code = _run_local(
                archive, resolved, config, options, entities, config_state, result
            ) or exit_code
    except SourceError as error:
        print(f"  source unreadable: {redact(str(error))}")
        return EXIT_SOURCE_UNREACHABLE

    explicit = config.source not in (SOURCE_ALL, SOURCE_AUTO)
    remote = backends - {SOURCE_LOCAL}
    if result.interrupted and remote:
        # Ctrl-C means stop. Measured on 0.4.1: it stopped the local pass and
        # then both remote services were contacted anyway.
        _say("", "interrupted; not contacting " + ", ".join(sorted(remote)))
    elif remote:
        try:
            if SOURCE_CLOUD in remote:
                exit_code = (
                    _run_cloud(
                        archive, resolved, config, options, result, explicit=explicit
                    )
                    or exit_code
                )
            if SOURCE_MCP in remote:
                exit_code = (
                    _run_mcp(archive, config, options, result, explicit=explicit)
                    or exit_code
                )
        except KeyboardInterrupt:
            result.interrupted = True
        finally:
            # Whatever ended the passes, what they recorded is kept. Measured
            # on 0.4.1: an MCP pass that raised took the cloud pass's state
            # with it, because the save came after both. A dry run's archive
            # is read-only and saves nothing.
            archive.save()

    for entity, counts in result.counts.items():
        _say("", counts.line(entity))
        if counts.bytes_copied:
            _say("", f"{entity}: {_human_bytes(counts.bytes_copied)} of media copied")
        if counts.failed:
            exit_code = EXIT_FAILURE

    if runs_local and not config_state.policy.records_dictation:
        _say("", "dictation: 0 records — localDataPolicy is never_store")

    if result.interrupted:
        _say("", "interrupted; progress was saved and the next run resumes")
        return 130
    if options.dry_run:
        _say("", "dry run: nothing was written")
    return exit_code


def _run_local(
    archive: Archive,
    resolved: paths.WisprPaths,
    config: Config,
    options: SyncOptions,
    entities: tuple[str, ...],
    config_state: LocalConfig,
    result: SyncResult,
) -> int:
    """Run the local pass, recording schema drift and the app build.

    Args:
        archive: The destination archive.
        resolved: Resolved source paths.
        config: This run's configuration.
        options: What this run was asked to do.
        entities: Which entity passes to perform.
        config_state: Parsed ``config.json``.
        result: Mutated with the local counts.

    Returns:
        An exit code contribution, or 0.

    Raises:
        SourceError: The database could not be read.
    """
    exit_code = EXIT_OK
    with open_source(resolved.db, immutable=resolved.db_is_backup) as source:
        drift = source.detect_drift()
        if drift.kind is not DriftClass.OK:
            _say("schema", drift.summary())
        if drift.kind is DriftClass.BREAKING:
            # Raw archiving still completes; only renderers are affected.
            # For an archival tool, failing loud must never mean failing
            # closed, so this is reported and the pass continues.
            exit_code = EXIT_BREAKING_DRIFT
            # Renderers read declared columns by name and degrade quietly when
            # one is gone, so an existing document is kept rather than
            # overwritten with less. Said out loud because the held-back
            # renderings stay stale until someone acts on this line.
            options.drift_blocks_rendering = drift.blocks_rendering
            _say(
                "render",
                "existing documents kept as they are; once the declaration is "
                "updated, the next sync or `wispr-export render` rebuilds them",
            )
        elif drift.kind is DriftClass.ADDITIVE and config.strict_schema:
            exit_code = EXIT_ADDITIVE_DRIFT

        state = archive.source_state(LOCAL_BACKEND)
        # Recorded on every run, so an archive that is empty because of a
        # preference can prove which preference, and when it was in force.
        state["sync_coordinator"] = config_state.sync_coordinator
        state["migration_pin"] = {
            "count": drift.live.count,
            "latest": drift.live.latest,
            "sha256": drift.live.sha256,
        }
        # Which client build produced this archive. Recorded for both
        # backends: a future reader looking at an archive cannot otherwise
        # tell which Wispr Flow wrote the data it describes.
        if config_state.app_version:
            state["app_version"] = config_state.app_version

        local = sync_local(
            archive,
            source,
            resolved,
            options,
            entities=entities,
            policy=config_state.policy,
            config=config_state,
            session=read_session(
                Path(config.session_file).expanduser()
                if config.session_file
                else resolved.session
            ),
        )
    result.counts.update(local.counts)
    result.failures.extend(local.failures)
    result.interrupted = local.interrupted
    return exit_code


def _run_cloud(
    archive: Archive,
    resolved: paths.WisprPaths,
    config: Config,
    options: SyncOptions,
    result: SyncResult,
    *,
    explicit: bool = True,
) -> int:
    """Run the cloud pass, reporting rather than raising on failure.

    The local archive is the primary artifact. A cloud backend that cannot
    reach the server must not discard a successful local run.

    Args:
        archive: The destination archive.
        resolved: Resolved source paths.
        config: This run's configuration.
        options: What this run was asked to do.
        result: The sync result, mutated with the cloud counts.
        explicit: Whether the operator named this backend. A missing
            credential is a failure when they asked for it by name and a
            skipped line when it was merely included in ``all`` -- otherwise
            the default reports a failure on every run for anyone who has not
            signed in, which is how an operator learns to ignore failures.

    Returns:
        An exit code contribution, or 0.
    """
    from .cloud_api import ENDPOINTS, CloudClient
    from .cloud_auth import CloudAuthError, resolve_credential
    from .cloud_schema import CLIENT_PIN, detect_cloud_drift, observe
    from .sync_cloud import SOURCE_CLOUD as CLOUD_BACKEND
    from .sync_cloud import sync_cloud, truncated

    session_path = (
        Path(config.session_file).expanduser()
        if config.session_file
        else resolved.session
    )
    try:
        credential = resolve_credential(session_path)
    except CloudAuthError as error:
        _say("cloud", redact(str(error)))
        return EXIT_FAILURE if explicit else EXIT_OK

    _say("cloud", f"using the token from {credential.origin}; never refreshing it")
    with CloudClient(credential, base_url=config.api_base) as client:
        counts = sync_cloud(archive, client, options)
        failures = list(client.failures)
        results = dict(client.results)

    app_version = read_config(resolved.config).app_version
    state = archive.source_state(CLOUD_BACKEND)
    drift = detect_cloud_drift(
        results, state.get("endpoint_shapes"), ENDPOINTS, app_version
    )
    if not options.dry_run:
        if app_version:
            state["app_version"] = app_version
        state["client_pin"] = {
            "app_version": CLIENT_PIN.app_version,
            "count": CLIENT_PIN.count,
            "sha256": CLIENT_PIN.sha256,
        }
        state["endpoint_shapes"] = observe(results, state.get("endpoint_shapes"))

    documented = set(drift.unreachable)
    for name, reason in failures:
        if name not in documented:
            _say("", f"cloud {name}: {redact(reason)}")
    if documented:
        # One line rather than one per endpoint. These are recorded in the
        # archive either way; repeating them as errors every run is how the
        # word stops meaning anything.
        listed = ", ".join(f"{n} {results[n].status}" for n in sorted(documented))
        _say("", f"cloud: unreachable as documented — {listed}")

    short = sorted(n for n, r in results.items() if r.ok and truncated(r.payload))
    if short:
        # Never quiet about this. An archive that holds one page and says
        # nothing is indistinguishable from a complete one.
        _say("", f"cloud: MORE RECORDS EXIST upstream than archived — {', '.join(short)}")

    if drift.kind is not DriftClass.OK:
        _say("cloud schema", drift.summary())

    result.counts["cloud"] = counts
    if drift.kind is DriftClass.BREAKING:
        # Everything reachable was still archived. Failing loud must not mean
        # failing closed for this backend either.
        return EXIT_BREAKING_DRIFT
    if drift.kind is DriftClass.ADDITIVE and config.strict_schema:
        return EXIT_ADDITIVE_DRIFT
    return EXIT_FAILURE if counts.failed and not counts.written else EXIT_OK


def _run_mcp(
    archive: Archive,
    config: Config,
    options: SyncOptions,
    result: SyncResult,
    *,
    explicit: bool = True,
) -> int:
    """Run the MCP pass, reporting rather than raising on failure.

    Args:
        archive: The destination archive.
        config: This run's configuration.
        options: What this run was asked to do.
        result: The sync result, mutated with the MCP counts.
        explicit: Whether the operator named this backend. Not being logged in
            is an ordinary state, not an error, when MCP was merely included in
            ``all``.

    Returns:
        An exit code contribution, or 0.
    """
    from .mcp_api import McpClient, McpError
    from .mcp_auth import McpAuthError, open_client, resolve_credential
    from .mcp_schema import MCP_PIN, detect_mcp_drift, tool_shapes
    from .sync_mcp import SOURCE_MCP as MCP_BACKEND
    from .sync_mcp import sync_mcp

    with open_client() as auth_client:
        try:
            credential = resolve_credential(auth_client, config.mcp_endpoint)
        except McpAuthError as error:
            _say("mcp", redact(str(error)))
            return EXIT_FAILURE if explicit else EXIT_OK

    _say("mcp", f"using the token from {credential.origin}")
    try:
        with McpClient(
            credential,
            endpoint=config.mcp_endpoint,
            renew=_renewer(config.mcp_endpoint, credential),
        ) as client:
            counts = sync_mcp(archive, client, options)
            failures = list(client.failures)
            tools = list(client.tools)
            server = dict(client.server)
    except McpError as error:
        _say("mcp", redact(str(error)))
        return EXIT_FAILURE if explicit else EXIT_OK

    state = archive.source_state(MCP_BACKEND)
    drift = detect_mcp_drift(tools, server, state.get("tool_shapes"))
    if not options.dry_run:
        state["mcp_pin"] = {
            "server": MCP_PIN.server,
            "version": MCP_PIN.version,
            "sha256": MCP_PIN.sha256,
        }
        state["tool_shapes"] = tool_shapes(tools)

    for name, reason in failures:
        _say("", f"mcp {name}: {redact(reason)}")
    if drift.kind is not DriftClass.OK:
        _say("mcp schema", drift.summary())

    result.counts["mcp"] = counts
    if drift.kind is DriftClass.BREAKING:
        return EXIT_BREAKING_DRIFT
    if drift.kind is DriftClass.ADDITIVE and config.strict_schema:
        return EXIT_ADDITIVE_DRIFT
    return EXIT_FAILURE if counts.failed and not counts.written else EXIT_OK


def _renewer(
    endpoint: str, credential: McpCredential
) -> Callable[[McpCredential], McpCredential] | None:
    """Build the callback an MCP client uses to replace a rejected token.

    Args:
        endpoint: The MCP endpoint this run talks to.
        credential: The credential the client starts with.

    Returns:
        The callback, or ``None`` for a token from the environment -- which is
        the operator's to replace, never this tool's to refresh.
    """
    if credential.origin != "token store":
        return None

    def renew(rejected: McpCredential) -> McpCredential:
        from .mcp_auth import open_client, renew_credential

        with open_client() as client:
            return renew_credential(client, endpoint, rejected)

    return renew


def cmd_login(args: argparse.Namespace) -> int:
    """Authorize this tool against the MCP server.

    The one command that mints a credential. Everything else in this tool
    borrows the token Wispr Flow already holds; the MCP server is a separate
    OAuth resource with a separate issuer that will not accept it.

    Args:
        args: Parsed arguments.

    Returns:
        Process exit code.
    """
    from .mcp_auth import McpAuthError, login, open_client

    print("wispr-export login", flush=True)
    endpoint = _config(args).mcp_endpoint
    _say("server", endpoint)
    try:
        with open_client() as client:
            credential = login(
                client, endpoint, announce=lambda line: print(line, flush=True)
            )
    except McpAuthError as error:
        _say("failed", redact(str(error)))
        return EXIT_FAILURE
    _say("stored", str(paths.token_store_path()).replace(str(Path.home()), "~"))
    _say("origin", credential.origin)
    return EXIT_OK


def cmd_logout(args: argparse.Namespace) -> int:
    """Delete the stored MCP credential.

    Args:
        args: Parsed arguments.

    Returns:
        Process exit code.
    """
    from .mcp_auth import McpAuthError, forget

    try:
        removed = forget()
    except McpAuthError as error:
        _say("failed", redact(str(error)))
        return EXIT_FAILURE
    _say("logout", "credential removed" if removed else "nothing was stored")
    return EXIT_OK


def _entities(args: argparse.Namespace) -> tuple[str, ...]:
    """Resolve which entity passes this run should perform.

    An unknown name is a usage error rather than a silent no-op: asking for
    "meeting" and getting an empty archive would be the worst possible
    outcome for a backup command.

    Args:
        args: Parsed arguments.

    Returns:
        The entities to run, in the canonical order.

    Raises:
        ValueError: An entity name was not recognized.
    """
    chosen = set(ENTITIES)
    only = (getattr(args, "only", None) or "").strip()
    skip = (getattr(args, "skip", None) or "").strip()

    if only:
        requested = {name.strip() for name in only.split(",") if name.strip()}
        unknown = requested - set(ENTITIES)
        if unknown:
            raise ValueError(f"unknown entities: {', '.join(sorted(unknown))}")
        chosen = requested
    if skip:
        dropped = {name.strip() for name in skip.split(",") if name.strip()}
        unknown = dropped - set(ENTITIES)
        if unknown:
            raise ValueError(f"unknown entities: {', '.join(sorted(unknown))}")
        chosen -= dropped
    return tuple(name for name in ENTITIES if name in chosen)


def _schema_cloud(args: argparse.Namespace, config: Config) -> int:
    """Probe the live API and report its shapes against the declaration.

    This is the cloud half of ``schema``: the same job the local half does with
    ``PRAGMA table_info``, done with one paced ``GET`` per declared endpoint. It
    writes nothing -- not to the archive and not to Wispr Flow -- so it is the
    safe thing to run first after an app update.

    Args:
        args: Parsed arguments.
        config: This run's configuration.

    Returns:
        Process exit code.
    """
    from .cloud_api import CANDIDATES, ENDPOINTS, CloudClient
    from .cloud_auth import CloudAuthError, resolve_credential
    from .cloud_schema import CLIENT_PIN, detect_cloud_drift, field_names, fingerprint
    from .sync_cloud import SOURCE_CLOUD as CLOUD_BACKEND

    resolved = paths.resolve(config.data_dir, config.db)
    session_path = (
        Path(config.session_file).expanduser()
        if config.session_file
        else resolved.session
    )
    try:
        credential = resolve_credential(session_path)
    except CloudAuthError as error:
        print(f"  {redact(str(error))}")
        return EXIT_SOURCE_UNREACHABLE

    table = dict(ENDPOINTS)
    if getattr(args, "candidates", False):
        table.update(CANDIDATES)

    with CloudClient(
        credential, base_url=config.api_base, endpoints=table
    ) as client:
        for name in table:
            client.fetch(name)
        results = dict(client.results)

    app_version = read_config(resolved.config).app_version
    # Read for the baseline, never written. A report that mutated the archive
    # would not be safe to run while diagnosing one.
    recorded = Archive(root=config.archive_dir, read_only=True).source_state(CLOUD_BACKEND)
    drift = detect_cloud_drift(
        results, recorded.get("endpoint_shapes"), table, app_version
    )

    observed = {
        name: {
            "path": table[name].path,
            "status": result.status,
            "expected_status": table[name].expected_status,
            "shape": fingerprint(result.payload) if result.ok else None,
            "fields": list(field_names(result.payload)) if result.ok else [],
            "cursor_param": table[name].cursor_param,
        }
        for name, result in results.items()
    }

    if getattr(args, "json", False):
        print(
            json.dumps(
                {
                    "app_version": app_version,
                    "declared_pin": {
                        "app_version": CLIENT_PIN.app_version,
                        "count": CLIENT_PIN.count,
                        "sha256": CLIENT_PIN.sha256,
                    },
                    "drift": drift.kind,
                    "endpoints": observed,
                    "broke": list(drift.broke),
                    "recovered": list(drift.recovered),
                    "unreachable": list(drift.unreachable),
                    "changed_shapes": list(drift.changed_shapes),
                    "new_fields": {k: list(v) for k, v in drift.new_fields.items()},
                    "missing_fields": {
                        k: list(v) for k, v in drift.missing_fields.items()
                    },
                },
                indent=2,
            )
        )
        return EXIT_OK

    print("wispr-flow-exporter schema (cloud)")
    _say("app", f"{app_version or 'unknown'} (pinned {CLIENT_PIN.app_version})")
    _say("endpoints", f"{len(table)} probed, {CLIENT_PIN.count} declared")
    for name, seen in observed.items():
        status = seen["status"]
        mark = "ok " if seen["shape"] else "-- "
        shape = seen["shape"] or "no body"
        _say("", f"{mark}{name:22} {status or 'net':>4}  {shape}")
    _say("drift", drift.summary())

    if drift.kind is DriftClass.BREAKING:
        return EXIT_BREAKING_DRIFT
    if drift.kind is DriftClass.ADDITIVE and config.strict_schema:
        return EXIT_ADDITIVE_DRIFT
    return EXIT_OK


def _schema_mcp(args: argparse.Namespace, config: Config) -> int:
    """Handshake with the MCP server and report its tools against the pin.

    The cheapest useful diagnostic this backend has: it completes the MCP
    handshake, reads the advertised tool list, and calls nothing. Writes
    nothing either -- not to the archive and not to Wispr Flow -- so it is safe
    to run while diagnosing a broken archive.

    Args:
        args: Parsed arguments.
        config: This run's configuration.

    Returns:
        Process exit code.
    """
    from .mcp_api import READ_TOOLS, McpClient, McpError
    from .mcp_auth import McpAuthError, open_client, resolve_credential
    from .mcp_schema import MCP_PIN, detect_mcp_drift, pin_from_tools
    from .sync_mcp import SOURCE_MCP as MCP_BACKEND

    with open_client() as auth_client:
        try:
            credential = resolve_credential(auth_client, config.mcp_endpoint)
        except McpAuthError as error:
            print(f"  {redact(printable(str(error)))}")
            return EXIT_SOURCE_UNREACHABLE

    try:
        with McpClient(
            credential,
            endpoint=config.mcp_endpoint,
            renew=_renewer(config.mcp_endpoint, credential),
        ) as client:
            tools = list(client.tools)
            server = dict(client.server)
    except McpError as error:
        print(f"  {redact(str(error))}")
        return EXIT_SOURCE_UNREACHABLE

    recorded = Archive(root=config.archive_dir, read_only=True).source_state(MCP_BACKEND)
    drift = detect_mcp_drift(tools, server, recorded.get("tool_shapes"))
    live = pin_from_tools(tools, server)
    advertised = sorted(str(tool.get("name", "")) for tool in tools)

    if getattr(args, "json", False):
        print(
            json.dumps(
                {
                    "server": server,
                    "pin": {
                        "server": live.server,
                        "version": live.version,
                        "protocol_version": live.protocol_version,
                        "tool_count": live.tool_count,
                        "sha256": live.sha256,
                    },
                    "declared_pin": {
                        "server": MCP_PIN.server,
                        "version": MCP_PIN.version,
                        "sha256": MCP_PIN.sha256,
                    },
                    "drift": drift.kind,
                    "tools": advertised,
                    "used": sorted(READ_TOOLS),
                    "unavailable": list(drift.unavailable),
                    "new_tools": list(drift.new_tools),
                    "missing_tools": list(drift.missing_tools),
                    "changed_schemas": list(drift.changed_schemas),
                },
                indent=2,
            )
        )
        return EXIT_OK

    print("wispr-flow-exporter schema (mcp)")
    _say("server", f"{live.server or '?'} {live.version or ''}".strip())
    _say("protocol", live.protocol_version or "?")
    _say("pin", f"{live.sha256[:12]} (declared {MCP_PIN.sha256[:12]})")
    _say("tools", f"{len(advertised)} advertised, {len(READ_TOOLS)} used")
    for name in advertised:
        mark = "use" if name in READ_TOOLS else "-- "
        _say("", f"{mark} {name}")
    _say("drift", drift.summary())

    if drift.kind is DriftClass.BREAKING:
        return EXIT_BREAKING_DRIFT
    if drift.kind is DriftClass.ADDITIVE and config.strict_schema:
        return EXIT_ADDITIVE_DRIFT
    return EXIT_OK


def cmd_schema(args: argparse.Namespace) -> int:
    """Report the live schema against the declaration.

    Args:
        args: Parsed arguments.

    Returns:
        Process exit code.
    """
    config = _config(args)
    if config.source == SOURCE_CLOUD:
        return _schema_cloud(args, config)
    if config.source == SOURCE_MCP:
        return _schema_mcp(args, config)
    resolved = paths.resolve(config.data_dir, config.db)
    if not resolved.db.exists():
        print(f"  no Wispr Flow database at {resolved.db}")
        return EXIT_SOURCE_UNREACHABLE

    with open_source(resolved.db, immutable=resolved.db_is_backup) as source:
        drift = source.detect_drift()
        tables = source.tables()
        if getattr(args, "json", False):
            print(
                json.dumps(
                    {
                        "pin": {
                            "count": drift.live.count,
                            "latest": drift.live.latest,
                            "sha256": drift.live.sha256,
                        },
                        "declared_pin": {
                            "count": MIGRATION_PIN.count,
                            "sha256": MIGRATION_PIN.sha256,
                        },
                        "drift": drift.kind,
                        "new_tables": list(drift.new_tables),
                        "missing_tables": list(drift.missing_tables),
                        "new_columns": {k: list(v) for k, v in drift.new_columns.items()},
                        "missing_columns": {
                            k: list(v) for k, v in drift.missing_columns.items()
                        },
                        "missing_required": {
                            k: list(v) for k, v in drift.missing_required.items()
                        },
                    },
                    indent=2,
                )
            )
            return EXIT_OK

        print("wispr-flow-exporter schema")
        _say("tables", f"{len(tables)} live, {len(EXPECTED)} declared")
        _say("migrations", f"{drift.live.count} (declared {MIGRATION_PIN.count})")
        _say("pin", f"{drift.live.sha256[:12]} (declared {MIGRATION_PIN.sha256[:12]})")
        _say("drift", drift.summary())

    if drift.kind is DriftClass.BREAKING:
        return EXIT_BREAKING_DRIFT
    if drift.kind is DriftClass.ADDITIVE and config.strict_schema:
        return EXIT_ADDITIVE_DRIFT
    return EXIT_OK


def cmd_verify(args: argparse.Namespace) -> int:
    """Check the archive's integrity and reconcile it with the source.

    Args:
        args: Parsed arguments.

    Returns:
        Process exit code.
    """
    config = _config(args)
    resolved = paths.resolve(config.data_dir, config.db)
    # Read-only: verification reports what it finds and changes nothing, not
    # even setting aside a corrupt index.
    archive = Archive(root=config.archive_dir, read_only=True)

    print("wispr-flow-exporter verify")
    _announce(archive)
    if resolved.db.exists():
        with open_source(resolved.db, immutable=resolved.db_is_backup) as source:
            report = verify_archive(archive, source, deep=getattr(args, "deep", False))
    else:
        # Internal consistency does not need the source, so a missing app is
        # a narrower check rather than a refusal.
        _say("source", "absent; checking internal consistency only")
        report = verify_archive(archive, None, deep=getattr(args, "deep", False))

    for line in report.lines():
        _say("", line)
    return EXIT_OK if report.ok else EXIT_FAILURE


def cmd_render(args: argparse.Namespace) -> int:
    """Re-render Markdown from archived payloads, touching no source.

    Args:
        args: Parsed arguments.

    Returns:
        Process exit code.
    """
    config = _config(args)
    options = SyncOptions(
        dry_run=getattr(args, "dry_run", False),
        verbose=getattr(args, "verbose", False),
    )
    archive = Archive(root=config.archive_dir, read_only=options.dry_run)

    print("wispr-flow-exporter render")
    _announce(archive)
    if getattr(args, "force", False):
        # Kept so scripts that pass it still run. It never forced anything:
        # every write is compare-then-write, and rewriting identical bytes
        # would only move mtimes. It used to inflate the "written" count.
        print("  note         : --force is no longer needed and changes nothing", file=sys.stderr)
    try:
        with archive.lock():
            results = rerender(archive, options)
            if not options.dry_run:
                archive.save()
    except ArchiveBusy as error:
        print(f"  {error}", file=sys.stderr)
        return EXIT_FAILURE
    for entity, counts in results.items():
        _say("", counts.line(entity))
    return EXIT_FAILURE if any(counts.failed for counts in results.values()) else EXIT_OK


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
    # Not required: a bare invocation runs the interactive setup, because
    # "usage error" is the wrong answer for someone who wants a backup.
    sub = parser.add_subparsers(dest="command", required=False)

    def add_selection(target: argparse.ArgumentParser) -> None:
        target.add_argument(
            "--only",
            metavar="A,B",
            default=None,
            help=f"comma list of entities to archive: {', '.join(ENTITIES)}",
        )
        target.add_argument(
            "--skip",
            metavar="A,B",
            default=None,
            help="comma list of entities to leave out",
        )

    def add_source(target: argparse.ArgumentParser) -> None:
        target.add_argument(
            "--source",
            choices=SOURCE_CHOICES,
            default=None,
            help="backend to use (default: all — local, cloud and mcp)",
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
    add_selection(sync)
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

    schema_parser = sub.add_parser(
        "schema", help="show the live schema against the declaration"
    )
    add_source(schema_parser)
    schema_parser.add_argument(
        "--strict-schema",
        action="store_true",
        help="exit 3 on additive drift instead of reporting it",
    )
    schema_parser.add_argument(
        "--json", action="store_true", help="machine-readable output"
    )
    schema_parser.add_argument(
        "--candidates",
        action="store_true",
        help="with --source cloud, also probe paths not yet adopted",
    )
    schema_parser.set_defaults(func=cmd_schema)

    login_parser = sub.add_parser(
        "login", help="authorize this tool against Wispr Flow's MCP server"
    )
    login_parser.set_defaults(func=cmd_login)

    logout_parser = sub.add_parser("logout", help="delete the stored MCP credential")
    logout_parser.set_defaults(func=cmd_logout)

    verify = sub.add_parser(
        "verify", help="check integrity and reconcile against the database"
    )
    add_source(verify)
    verify.add_argument(
        "--deep",
        action="store_true",
        help="recompute archived digests instead of trusting the index",
    )
    verify.set_defaults(func=cmd_verify)

    render_parser = sub.add_parser(
        "render", help="re-render Markdown from archived payloads"
    )
    render_parser.add_argument("--force", action="store_true", help=argparse.SUPPRESS)
    render_parser.add_argument(
        "--dry-run", action="store_true", help="report without writing"
    )
    render_parser.add_argument("-v", "--verbose", action="store_true")
    render_parser.set_defaults(func=cmd_render)

    # Only the real entry point prompts. ``main([])`` is a programmatic call
    # and prints help: deciding by ``len(sys.argv)`` alone made the answer
    # depend on how the *caller* was launched, so a bare ``pytest`` walked
    # into the interactive setup while ``pytest -q`` did not.
    if argv is None and len(sys.argv) <= 1:
        try:
            answers = collect(_defaults())
        except PromptAborted as error:
            print(f"  {error}")
            return EXIT_OK if str(error) == "cancelled" else EXIT_FAILURE
        return main(answers.to_argv())

    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return EXIT_OK

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

    try:
        return int(args.func(args))
    except EndpointError as error:
        # Refused before any request, so the token was never attached to it.
        print(f"  {error}")
        return EXIT_FAILURE


if __name__ == "__main__":
    raise SystemExit(main())
