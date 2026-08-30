"""Assert that no real personal data has entered the source tree.

This is the test that gives the fixture policy teeth. The sibling project,
granola-exporter, is otherwise clean but carries two incidental leaks of its
author's real name -- one in a docstring example, one in fake transcript text --
that survived to publication because nothing checked. A grep run by hand before
going public is a control that works exactly until someone forgets.

Scope is ``src/`` and ``tests/`` only. ``LICENSE.md``, ``README.md`` and
``changelog.txt`` legitimately carry the copyright holder's name, and a test
that had to allow-list them would be a test with a hole in the shape of the
thing it is looking for.
"""

from __future__ import annotations

import re
from pathlib import Path

from conftest import APPROVED_UUIDS, FAKE_JWT

ROOT = Path(__file__).resolve().parents[1]
SCANNED = ("src", "tests")

# Assembled rather than written literally so this file can scan itself without
# matching on its own patterns.
_OWNER_GIVEN = "ja" + "de"
_OWNER_FAMILY = "naa" + "man"

UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\b")
HOME_RE = re.compile(r"/Users/[A-Za-z]|/home/[a-z]")
SUPABASE_RE = re.compile(r"sb-[a-z0-9]{20}-auth-token|[a-z]{20}\.supabase\.co")

ALLOWED_EMAIL_DOMAINS = ("example.invalid", "users.noreply.github.com")


def _sources() -> list[Path]:
    """Collect every Python file under the scanned directories.

    Returns:
        Paths to scan, sorted for a stable failure message.
    """
    files: list[Path] = []
    for directory in SCANNED:
        files.extend(sorted((ROOT / directory).rglob("*.py")))
    return files


def _hits(pattern: re.Pattern[str]) -> list[str]:
    """Report every match of ``pattern`` across the scanned sources.

    Args:
        pattern: Compiled expression to search for.

    Returns:
        ``"<relative path>:<line number>: <match>"`` for each hit.
    """
    found: list[str] = []
    for path in _sources():
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            for match in pattern.finditer(line):
                found.append(f"{path.relative_to(ROOT)}:{number}: {match.group(0)}")
    return found


def test_no_real_name() -> None:
    """The copyright holder's name must not appear in code or fixtures."""
    pattern = re.compile(rf"(?i)\b({_OWNER_GIVEN}|{_OWNER_FAMILY})\b")
    assert _hits(pattern) == []


def test_no_absolute_home_paths() -> None:
    """Documentation and defaults must use a literal tilde, never a real home."""
    assert _hits(HOME_RE) == []


def test_no_unexpected_email_domains() -> None:
    """Every email address must be at a reserved, unroutable domain."""
    unexpected = [
        hit for hit in _hits(EMAIL_RE) if not hit.endswith(ALLOWED_EMAIL_DOMAINS)
    ]
    assert unexpected == []


def test_only_the_placeholder_jwt() -> None:
    """The one JWT-shaped string in the tree must be the known placeholder."""
    unexpected = [hit for hit in _hits(JWT_RE) if not hit.endswith(FAKE_JWT)]
    assert unexpected == []


def test_no_supabase_project_reference() -> None:
    """A real Supabase project reference is not ours to publish."""
    hits = [hit for hit in _hits(SUPABASE_RE) if "aaaaaaaaaaaaaaaaaaaa" not in hit]
    assert hits == []


def test_every_uuid_is_approved() -> None:
    """No UUID may appear that is not in the fixture table.

    This is the strongest clause in the policy. A real meeting or dictation id
    has no other route into the repository, so pinning the set of permitted
    UUIDs pins the set of permitted records to "none".
    """
    unapproved = [
        hit
        for hit in _hits(UUID_RE)
        # Compared lowercased: the approved table holds canonical forms, and a
        # fixture deliberately carries an uppercase variant to exercise the
        # archive-key validator that rejects it.
        if hit.rsplit(": ", 1)[-1].lower() not in APPROVED_UUIDS
    ]
    assert unapproved == []
