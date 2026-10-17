"""Assert that a build runs the same code every time, not just installs it.

``uv.lock`` pins everything the package pulls in, and ``[build-system]`` pins
hatchling, but hatchling's own dependencies were resolved afresh by every build
-- ``uv build`` and the editable build inside every ``uv sync`` alike -- so two
builds of one commit could run different code. uv's build constraints pin them.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

HATCHLING = "hatchling==1.32.4"
# What that release's METADATA requires. tomli is left out: it is required
# only below Python 3.11, and this project needs 3.13.
HATCHLING_REQUIRES = {"packaging", "pathspec", "pluggy", "tomlkit", "trove-classifiers"}

EXACT_PIN = re.compile(r"(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[0-9][0-9A-Za-z.+!-]*)")


def test_hatchlings_own_dependencies_are_pinned_exactly() -> None:
    """Each package hatchling runs with is constrained to a single version.

    Tied to one hatchling release on purpose. A hatchling bump fails here
    until that release's requirements have been read again and pinned, which
    is the step that would otherwise be forgotten.
    """
    declared = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
    constraints = declared.get("tool", {}).get("uv", {}).get("build-constraint-dependencies", [])

    pinned = {}
    for entry in constraints:
        match = EXACT_PIN.fullmatch(entry)
        assert match, f"not an exact pin: {entry!r}"
        pinned[match["name"]] = match["version"]

    assert declared["build-system"]["requires"] == [HATCHLING]
    assert set(pinned) == HATCHLING_REQUIRES


def test_ast_serialize_is_held_where_a_resolver_can_see_it() -> None:
    """This project stays on ast-serialize 0.12.1, though mypy accepts up to 1.0.

    0.12.x carries the fixes for python/mypy#22085 and #22090, and 0.13.x
    changes how ``type: ignore`` comments parse, for a mypy not yet released.
    Held only by uv.lock, any relock or grouped update could move it. Held as
    a constraint, a mypy that needs 0.13 fails to resolve on the pull request
    that bumps it -- the signal to move both together.
    """
    declared = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
    constraints = declared.get("tool", {}).get("uv", {}).get("constraint-dependencies", [])

    assert "ast-serialize==0.12.1" in constraints
