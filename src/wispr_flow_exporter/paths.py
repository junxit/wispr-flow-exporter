"""Where Wispr Flow keeps its store on each platform.

This is the only module that branches on the operating system. Everything else
takes a resolved ``WisprPaths`` and stays platform-agnostic, so porting beyond
macOS means editing one function rather than auditing the whole package for
hardcoded paths.

Only the macOS layout has been verified against a real installation. The
Windows path is taken from the Raycast extension's published reader and the
Linux path from Electron's ``app.getPath("userData")`` convention; both are
best-effort until someone runs the tool there, which is why ``WISPR_DATA_DIR``
exists as an override.

Two credentials, two rules. For the local store and the REST API this tool
mints nothing: it borrows the token Wispr Flow already holds, for the duration
of one request. The MCP server is a separate OAuth resource with a separate
issuer, which will not accept that borrowed token, so for that one backend the
tool registers itself and holds a token of its own.

That token lives in :func:`token_store_path` -- deliberately outside the
archive, so an archive someone backs up or hands to someone else still cannot
carry a credential, which is the property that mattered when there was no
token store at all.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

APP_DIR_NAME = "Wispr Flow"
DB_NAME = "flow.sqlite"
TOOL_DIR_NAME = "wispr-flow-exporter"
TOKEN_STORE_NAME = "mcp-token.json"


def token_store_path() -> Path:
    """Return where the MCP credential is kept.

    The one piece of state this tool holds outside an archive. It is *not* in
    the archive on purpose: an archive is the thing people copy to a backup
    drive or hand to somebody else, and a credential that travelled with it
    would be a disclosure every time.

    Honors ``XDG_CONFIG_HOME`` where it is set, on macOS too -- somebody who has
    gone to the trouble of setting it means it.

    Returns:
        Path to the token file. Neither it nor its parent is created here;
        the caller writes through ``secure_io`` so the modes are right.
    """
    config_home = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(config_home) if config_home else Path.home() / ".config"
    return base.expanduser() / TOOL_DIR_NAME / TOKEN_STORE_NAME


def default_data_dir() -> Path:
    """Return the platform's Wispr Flow application-support directory.

    Returns:
        The conventional directory for this platform. It is not checked for
        existence -- callers report a missing store themselves, with a better
        message than this function could give.
    """
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "").strip()
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return base / APP_DIR_NAME
    config_home = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(config_home) if config_home else Path.home() / ".config"
    return base / APP_DIR_NAME


@dataclass(frozen=True, slots=True)
class WisprPaths:
    """Every path the local backend reads.

    Attributes:
        data_dir: The application-support directory.
        db: The SQLite database to read.
        config: ``config.json``, holding preferences and the sync coordinator.
        session: ``session.json``, holding the plaintext Supabase session.
        feature_flags: ``feature-flags.json``.
        meetings: Directory of per-meeting transcript artifacts.
        backups: The app's own rolling database copies.
    """

    data_dir: Path
    db: Path
    config: Path
    session: Path
    feature_flags: Path
    meetings: Path
    backups: Path

    @property
    def db_is_backup(self) -> bool:
        """Report whether the configured database is one of the app's backups.

        A backup copy ships without the ``-shm`` sibling SQLite expects, so it
        has to be opened ``immutable=1`` rather than ``mode=ro``. It also has
        different provenance, which the archive records.

        Returns:
            ``True`` when the database sits in ``backups/`` or is named like a
            backup snapshot.
        """
        return self.db.parent == self.backups or self.db.name.startswith("backup-")


def resolve(data_dir: str | Path | None = None, db: str | Path | None = None) -> WisprPaths:
    """Resolve every source path from an optional data directory and database.

    Args:
        data_dir: Override for the application-support directory.
        db: Override for the database file. Relative to ``data_dir`` only when
            it is not itself absolute.

    Returns:
        The resolved set of paths.
    """
    root = Path(data_dir).expanduser() if data_dir else default_data_dir()
    root = root.expanduser()
    database = Path(db).expanduser() if db else root / DB_NAME
    return WisprPaths(
        data_dir=root,
        db=database,
        config=root / "config.json",
        session=root / "session.json",
        feature_flags=root / "feature-flags.json",
        meetings=root / "meetings",
        backups=root / "backups",
    )
