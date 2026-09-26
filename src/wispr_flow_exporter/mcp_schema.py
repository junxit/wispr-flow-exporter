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

**What is compared.** A schema's *constraints* -- types, required lists,
ranges, enumerations -- not its prose. And severity follows what this tool
depends on: the arguments the sync pass sends (``McpTool.sends``) and whatever
the schema requires beside them. Measured on 0.4.1, whose digest was a value-
discarding skeleton of each schema: an argument changing type did not move it,
nor did an argument becoming required, while an optional argument added to an
allowlisted tool the pass never calls was breaking drift.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .drift import MAX_DEPTH, fingerprint, version_tuple
from .mcp_api import READ_TOOLS, USED_TOOLS
from .schema import DriftClass

#: Keys of a JSON schema that say what a value may be. Descriptions, titles,
#: defaults and examples are prose, and rewording them is not drift.
_CONSTRAINTS = frozenset(
    {
        "$ref",
        "additionalProperties",
        "allOf",
        "anyOf",
        "const",
        "enum",
        "exclusiveMaximum",
        "exclusiveMinimum",
        "format",
        "items",
        "maxItems",
        "maxLength",
        "maximum",
        "minItems",
        "minLength",
        "minimum",
        "oneOf",
        "pattern",
        "properties",
        "required",
        "type",
        "uniqueItems",
    }
)

#: The pin digest this build computes. Version 1 digested a value-discarding
#: skeleton of each schema, which could not see a type change; version 2
#: digests each schema's constraint form.
PIN_ALGORITHM = 2


@dataclass(frozen=True, slots=True)
class McpPin:
    """A fingerprint of the server and the tools it advertised.

    Attributes:
        server: The server's own name.
        version: The server's own version, when it declares one.
        protocol_version: The MCP revision the server negotiated.
        tool_count: How many tools it advertised.
        sha256: Digest over each tool's name and input schema, so a renamed or
            retyped argument moves the pin even when the tool list does not.
        algorithm: How ``sha256`` was computed; see :data:`PIN_ALGORITHM`.
    """

    server: str
    version: str
    protocol_version: str
    tool_count: int
    sha256: str
    algorithm: int = 1


def constraint_form(schema: Any, depth: int = 0) -> Any:
    """Reduce a JSON schema to what constrains a value.

    Args:
        schema: A JSON schema, or part of one.
        depth: Current recursion depth.

    Returns:
        The schema with only constraining keys, required lists sorted.
    """
    if depth > MAX_DEPTH:
        return "..."
    if isinstance(schema, list):
        return [constraint_form(item, depth + 1) for item in schema]
    if not isinstance(schema, dict):
        return schema
    kept: dict[str, Any] = {}
    for key in sorted(schema):
        if key not in _CONSTRAINTS:
            continue
        value = schema[key]
        if key == "properties" and isinstance(value, dict):
            kept[key] = {
                str(name): constraint_form(sub, depth + 1)
                for name, sub in sorted(value.items())
            }
        elif key == "required" and isinstance(value, list):
            kept[key] = sorted(str(name) for name in value)
        else:
            kept[key] = constraint_form(value, depth + 1)
    return kept


def _digest(value: Any) -> str:
    """Digest a JSON value canonically.

    Args:
        value: Any JSON value.

    Returns:
        Sixteen hex characters of the SHA-256 of its sorted JSON.
    """
    text = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _schema(tool: Mapping[str, Any]) -> dict[str, Any]:
    """Return a tool's input schema, or an empty one.

    Args:
        tool: One advertised tool.

    Returns:
        Its ``inputSchema`` when that is an object.
    """
    schema = tool.get("inputSchema")
    return schema if isinstance(schema, dict) else {}


def tool_contract(tool: Mapping[str, Any], sends: Sequence[str]) -> str:
    """Digest what a tool's schema says about the arguments this tool sends.

    Covered: the constraint form of every argument sent, or its absence; and
    the required list of the schema's root and of every object along a sent
    path, because a newly required argument breaks a call that does not send
    it. Not covered: anything the pass does not send, which may change freely.

    Args:
        tool: One advertised tool.
        sends: The dotted argument paths the pass sends it.

    Returns:
        A short digest.
    """
    root = _schema(tool)
    terms: dict[str, Any] = {"$": sorted(str(n) for n in root.get("required") or [])}
    for path in sorted(sends):
        node: Any = root
        trail = "$"
        for segment in path.split("."):
            properties = node.get("properties") if isinstance(node, dict) else None
            node = properties.get(segment) if isinstance(properties, dict) else None
            trail = f"{trail}.{segment}"
            if isinstance(node, dict) and "required" in node:
                terms[f"{trail}.required"] = sorted(str(n) for n in node["required"])
            if node is None:
                break
        terms[path] = constraint_form(node) if node is not None else "absent"
    return _digest(terms)


def tool_ledger(tools: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, str]]:
    """Build the per-tool ledger for ``.sync-state.json``.

    Args:
        tools: The advertised tools.

    Returns:
        Tool name to its ``shape`` -- a digest of the whole constraint form --
        and, for a tool the pass calls, its ``contract``. Sorted, and with no
        timestamps, so an unchanged server leaves the state file untouched.
    """
    ledger: dict[str, dict[str, str]] = {}
    for tool in tools:
        name = str(tool.get("name") or "")
        if not name:
            continue
        entry = {"shape": _digest(constraint_form(_schema(tool)))}
        declared = READ_TOOLS.get(name)
        if declared is not None and declared.sends is not None:
            entry["contract"] = tool_contract(tool, declared.sends)
        ledger[name] = entry
    return {name: ledger[name] for name in sorted(ledger)}


def tool_shapes(tools: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """Build the per-tool ledger 0.4.x recorded, for the upgrade's comparison.

    Legacy: ``tool_ledger`` replaced it. It is kept because the first run after
    upgrading compares the server against what 0.4.x recorded, in 0.4.x's own
    terms, rather than reporting every tool as new.

    Args:
        tools: The advertised tools.

    Returns:
        Tool name to its input schema's skeleton fingerprint.
    """
    return {
        str(tool.get("name", "")): fingerprint(tool.get("inputSchema"))
        for tool in tools
        if tool.get("name")
    }


def pin_from_tools(
    tools: Sequence[Mapping[str, Any]],
    server: Mapping[str, Any] | None = None,
    *,
    algorithm: int = PIN_ALGORITHM,
) -> McpPin:
    """Compute a pin from a ``tools/list`` response and the handshake.

    Args:
        tools: The advertised tools.
        server: Server info from ``initialize``.
        algorithm: Which digest to compute, so a live pin can be compared with
            a declared one taken the same way.

    Returns:
        The pin. Digest rule mirrors ``pin_from_migrations``: sort, join with
        newlines, SHA-256 the UTF-8 bytes.
    """
    info = server or {}

    def described(tool: Mapping[str, Any]) -> str:
        if algorithm == 1:
            return fingerprint(tool.get("inputSchema"))
        return _digest(constraint_form(_schema(tool)))

    lines = sorted(f"{tool.get('name', '')}\t{described(tool)}" for tool in tools)
    digest = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
    return McpPin(
        server=str(info.get("name") or ""),
        version=str(info.get("version") or ""),
        protocol_version=str(info.get("protocol_version") or ""),
        tool_count=len(lines),
        sha256=digest,
        algorithm=algorithm,
    )


@dataclass(frozen=True, slots=True)
class McpDrift:
    """How far the live MCP server has moved from what this tool declares.

    Attributes:
        kind: The classification.
        live: The pin computed from this pass.
        pin: The declared pin it was measured against.
        new_tools: Tools the server advertises that were not recorded.
        missing_tools: Tools that were recorded and are no longer advertised.
        changed_schemas: Tools whose constraints moved.
        broken_contracts: Tools the pass calls whose arguments, or the
            requirements beside them, moved.
        unavailable: Tools the pass calls that the server no longer
            advertises.
    """

    kind: DriftClass
    live: McpPin
    pin: McpPin
    new_tools: tuple[str, ...] = ()
    missing_tools: tuple[str, ...] = ()
    changed_schemas: tuple[str, ...] = ()
    broken_contracts: tuple[str, ...] = ()
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
            parts.append(
                f"tools this backend needs are gone: {', '.join(self.unavailable)}"
            )
        if self.broken_contracts:
            parts.append(
                "arguments this backend sends moved: "
                + ", ".join(self.broken_contracts)
            )
        if self.missing_tools:
            parts.append(f"no longer advertised: {', '.join(self.missing_tools)}")
        if self.changed_schemas:
            parts.append(f"schemas moved: {', '.join(self.changed_schemas)}")
        if self.new_tools:
            parts.append(f"new tools: {', '.join(self.new_tools)}")
        if self.live.version != self.pin.version:
            parts.append(
                f"server {self.live.version or '?'}, pinned {self.pin.version or '?'}"
            )
        if not parts:
            return f"mcp OK (pin {self.pin.sha256[:12]} matches)"
        return f"{self.kind}: " + "; ".join(parts)


def detect_mcp_drift(
    tools: Sequence[Mapping[str, Any]],
    server: Mapping[str, Any] | None,
    recorded: Mapping[str, Any] | None = None,
    pin: McpPin | None = None,
    *,
    legacy: Mapping[str, Any] | None = None,
) -> McpDrift:
    """Classify one handshake against the declaration and the recorded ledger.

    Breaking means this backend cannot work as it did: a tool the pass calls
    is gone, or what the pass sends it is no longer what the schema accepts.
    Anything else that moved -- a new tool, an optional argument, any change
    to a tool the pass never calls -- is additive.

    Args:
        tools: The advertised tools.
        server: Server info from ``initialize``.
        recorded: The ledger from an earlier run (``tool_ledger``), if any.
        pin: The pin to measure against. Defaults to :data:`MCP_PIN`.
        legacy: The ledger 0.4.x recorded (``tool_shapes``), consulted only
            when there is no ``tool_ledger`` yet. It holds no contracts, so a
            change it shows is reported but not breaking -- the first run after
            upgrading must not cry wolf.

    Returns:
        The drift, always populated.
    """
    against = pin if pin is not None else MCP_PIN
    live = pin_from_tools(tools, server, algorithm=against.algorithm)
    current = tool_ledger(tools)
    advertised = set(current)
    unavailable = sorted(USED_TOOLS - advertised)

    new_tools: list[str] = []
    missing_tools: list[str] = []
    changed: list[str] = []
    broken: list[str] = []
    if recorded:
        baseline = {k: v for k, v in recorded.items() if isinstance(v, Mapping)}
        new_tools = sorted(advertised - set(baseline))
        missing_tools = sorted(set(baseline) - advertised)
        both = advertised & set(baseline)
        changed = sorted(n for n in both if baseline[n].get("shape") != current[n]["shape"])
        broken = sorted(
            name
            for name in both & USED_TOOLS
            if baseline[name].get("contract") != current[name].get("contract")
        )
    elif legacy:
        before = tool_shapes(tools)
        new_tools = sorted(advertised - set(legacy))
        missing_tools = sorted(set(legacy) - advertised)
        changed = sorted(
            name for name in advertised & set(legacy) if legacy[name] != before[name]
        )

    moved = bool(new_tools or missing_tools or changed)
    live_version = version_tuple(live.version)
    pinned_version = version_tuple(against.version)

    if unavailable or broken:
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
        broken_contracts=tuple(broken),
        unavailable=tuple(unavailable),
    )


# Read from a live handshake. A mismatch is not an error -- see the
# classification above -- but it is always reported. Refresh with
# `wispr-export schema --source mcp`; MAINTENANCE.md has the procedure.
# Taken 2026-09-26 with the constraint-form digest, against the same server
# version and tool count as the algorithm-1 pin it replaces.
MCP_PIN = McpPin(
    server="wispr-meetings",
    version="1",
    protocol_version="2025-06-18",
    tool_count=14,
    sha256="99f0c03d5dfbcc0d54c50d1e420491fd2a20555dd3ec3acab8e36e1b8cf62edd",
    algorithm=2,
)
