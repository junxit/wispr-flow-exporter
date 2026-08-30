"""Drift detection for the MCP server.

The same job ``cloud_schema`` does for the REST API, with one advantage: an MCP
server declares its own name, version and protocol revision in the handshake,
and publishes its tool list with each tool's input schema. So this backend can
be pinned against what the *server* says about itself rather than against the
desktop app's build number, and a renamed argument is detectable before a single
tool is called.

The generic machinery -- skeletons, fingerprints, the ledger -- lives in
``drift``. This module adds only what is MCP-shaped: the pin, and the
classification of a tool list against it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .drift import fingerprint, version_tuple
from .mcp_api import READ_TOOLS
from .schema import DriftClass


@dataclass(frozen=True, slots=True)
class McpPin:
    """A fingerprint of the server and the tools it advertised.

    Attributes:
        server: The server's own name.
        version: The server's own version, when it declares one.
        protocol_version: The MCP revision the server negotiated.
        tool_count: How many tools it advertised.
        sha256: Digest over each tool's name and input-schema shape, so a
            renamed argument moves the pin even when the tool list does not.
    """

    server: str
    version: str
    protocol_version: str
    tool_count: int
    sha256: str


def pin_from_tools(
    tools: Sequence[Mapping[str, Any]],
    server: Mapping[str, Any] | None = None,
) -> McpPin:
    """Compute a pin from a ``tools/list`` response and the handshake.

    Args:
        tools: The advertised tools.
        server: Server info from ``initialize``.

    Returns:
        The pin. Digest rule mirrors ``pin_from_migrations``: sort, join with
        newlines, SHA-256 the UTF-8 bytes.
    """
    info = server or {}
    lines = sorted(
        f"{tool.get('name', '')}\t{fingerprint(tool.get('inputSchema'))}"
        for tool in tools
    )
    digest = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
    return McpPin(
        server=str(info.get("name") or ""),
        version=str(info.get("version") or ""),
        protocol_version=str(info.get("protocol_version") or ""),
        tool_count=len(lines),
        sha256=digest,
    )


@dataclass(frozen=True, slots=True)
class McpDrift:
    """How far the live MCP server has moved from what this tool declares.

    Attributes:
        kind: The classification.
        live: The pin computed from this pass.
        pin: The declared pin it was measured against.
        new_tools: Tools the server advertises that were not pinned.
        missing_tools: Tools that were pinned and are no longer advertised.
        changed_schemas: Tools whose input schema moved.
        unavailable: Allowlisted tools the server no longer advertises at all.
    """

    kind: DriftClass
    live: McpPin
    pin: McpPin
    new_tools: tuple[str, ...] = ()
    missing_tools: tuple[str, ...] = ()
    changed_schemas: tuple[str, ...] = ()
    unavailable: tuple[str, ...] = ()

    @property
    def blocks_rendering(self) -> bool:
        """Report whether interpretation should be skipped this run."""
        return self.kind is DriftClass.BREAKING

    def summary(self) -> str:
        """Describe the drift in one line.

        Returns:
            A sentence naming what moved. Never empty.
        """
        parts: list[str] = []
        if self.unavailable:
            parts.append(f"tools this backend needs are gone: {', '.join(self.unavailable)}")
        if self.missing_tools:
            parts.append(f"no longer advertised: {', '.join(self.missing_tools)}")
        if self.changed_schemas:
            parts.append(f"arguments moved: {', '.join(self.changed_schemas)}")
        if self.new_tools:
            parts.append(f"new tools: {', '.join(self.new_tools)}")
        if self.live.version != self.pin.version:
            parts.append(f"server {self.live.version or '?'}, pinned {self.pin.version or '?'}")
        if not parts:
            return f"mcp OK (pin {self.pin.sha256[:12]} matches)"
        return f"{self.kind}: " + "; ".join(parts)


def detect_mcp_drift(
    tools: Sequence[Mapping[str, Any]],
    server: Mapping[str, Any] | None,
    recorded: Mapping[str, Any] | None = None,
    pin: McpPin | None = None,
) -> McpDrift:
    """Classify one handshake against the declaration.

    The severity rule is about *this tool's* needs, not the server's inventory.
    A server that adds ten tools has done nothing to us; a server that drops one
    we call has broken this backend, and that distinction is what decides
    between additive and breaking.

    Args:
        tools: The advertised tools.
        server: Server info from ``initialize``.
        recorded: Per-tool schema shapes from an earlier run, if any.
        pin: The pin to measure against. Defaults to :data:`MCP_PIN`.

    Returns:
        The drift, always populated.
    """
    against = pin if pin is not None else MCP_PIN
    live = pin_from_tools(tools, server)

    advertised = {
        str(tool.get("name", "")): fingerprint(tool.get("inputSchema")) for tool in tools
    }
    baseline = dict(recorded or {})
    first_run = not baseline

    new_tools = sorted(set(advertised) - set(baseline)) if not first_run else []
    missing_tools = sorted(set(baseline) - set(advertised)) if not first_run else []
    changed = sorted(
        name
        for name, shape in advertised.items()
        if name in baseline and baseline[name] != shape
    )
    # The tools this backend actually calls. Anything else moving is news, not
    # damage.
    unavailable = sorted(name for name in READ_TOOLS if name not in advertised)
    needed_changed = [name for name in changed if name in READ_TOOLS]

    moved = bool(new_tools or missing_tools or changed)
    live_version = version_tuple(live.version)
    pinned_version = version_tuple(against.version)

    if unavailable or needed_changed:
        kind = DriftClass.BREAKING
    elif live_version and pinned_version and live_version < pinned_version:
        kind = DriftClass.STALE_SOURCE
    elif not moved and live.sha256 == against.sha256:
        kind = DriftClass.OK
    else:
        kind = DriftClass.ADDITIVE

    return McpDrift(
        kind=kind,
        live=live,
        pin=against,
        new_tools=tuple(new_tools),
        missing_tools=tuple(missing_tools),
        changed_schemas=tuple(changed),
        unavailable=tuple(unavailable),
    )


def tool_shapes(tools: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """Build the per-tool schema ledger for ``.sync-state.json``.

    Args:
        tools: The advertised tools.

    Returns:
        Tool name to input-schema fingerprint. Values only, no timestamps, so
        an unchanged server leaves the state file byte-identical.
    """
    return {
        str(tool.get("name", "")): fingerprint(tool.get("inputSchema"))
        for tool in tools
        if tool.get("name")
    }


# Read from a live handshake. A mismatch is not an error -- see the
# classification above -- but it is always reported. Refresh with
# `wispr-export schema --source mcp`; MAINTENANCE.md has the procedure.
MCP_PIN = McpPin(
    server="wispr-meetings",
    version="1",
    protocol_version="2025-06-18",
    tool_count=14,
    sha256="12849ed4225542bb47104e3e6ce7c9332f598504734fb7ff1e0cfef244dae9db",
)
