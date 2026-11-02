"""Supported default-grant plugins, as exact newest published pins with their default configs.
Add a future default plugin to DEFAULT_PLUGINS (and DEFAULT_PLUGIN_ROLES). Ordubashy selects the plugins a permanent worker holds."""

from __future__ import annotations

import copy

from qorqut_operations.contracts import DomainError
from .roles import ORDUBASHY_ROLE

# Email Management is instruction-only (v3): no tools, no mailbox configuration, an empty default config.
EMAIL_PLUGIN = "email-management"
EMAIL_MAJOR = 3
# Plugin id -> (supported major version, default config).
DEFAULT_PLUGINS = {EMAIL_PLUGIN: (EMAIL_MAJOR, {})}
# Plugin id -> the roles that receive it by default at ecosystem scope. Every other agent needs an explicit grant.
DEFAULT_PLUGIN_ROLES = {EMAIL_PLUGIN: (ORDUBASHY_ROLE,)}


def default_config(plugin_id: str) -> dict:
    return copy.deepcopy(DEFAULT_PLUGINS[plugin_id][1])


def _version_key(version):
    try:
        parts = tuple(int(part) for part in str(version).split("."))
    except (TypeError, ValueError):
        return None
    return parts or None


def published_pin(connection, plugin_id: str) -> dict | None:
    """Exact reference to the newest published version of the supported major, or None."""
    major = DEFAULT_PLUGINS[plugin_id][0]
    rows = connection.execute("SELECT version, digest FROM artifacts WHERE kind='plugin' AND artifact_id=?",
                              (plugin_id,)).fetchall()
    candidates = [(key, row) for row in rows if (key := _version_key(row["version"])) is not None and key[0] == major]
    if not candidates:
        return None
    _, row = max(candidates, key=lambda pair: pair[0])
    return {"id": plugin_id, "version": row["version"], "digest": row["digest"]}


def unusable_reason(resolver, connection, plugin_id: str, pin: dict | None, scope_kind: str) -> str | None:
    """Why the pin cannot carry the default grant for this scope, or None when it can."""
    from bazarlyk.catalog.packages import parse_yaml, validate_schema

    if pin is None:
        return f"no published {plugin_id} {DEFAULT_PLUGINS[plugin_id][0]}.x version"
    try:
        package = resolver.catalog.get("plugin", pin, connection=connection)
        if scope_kind not in package.manifest["scope_kinds"]:
            return f"{plugin_id}@{pin['version']} does not support {scope_kind} scope"
        validate_schema(default_config(plugin_id), parse_yaml(package.files[package.manifest["config_schema"]].decode()),
                        "Default config")
        handler = resolver.plugin_handlers.get(package.id)
        if handler is None:
            return f"{plugin_id} has no trusted application handler"
        handler(package)
    except (DomainError, OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
        return f"{plugin_id}@{pin['version']} does not accept its default config: {exc}"
    return None


def usable_pin(resolver, connection, plugin_id: str, scope_kind: str) -> dict | None:
    pin = published_pin(connection, plugin_id)
    return pin if unusable_reason(resolver, connection, plugin_id, pin, scope_kind) is None else None


def default_plugin_ids(role_id: str, scope: dict) -> tuple[str, ...]:
    """Plugins a new profile receives when its creator does not select plugins."""
    if scope["kind"] != "ecosystem":
        return ()
    return tuple(plugin_id for plugin_id, role_ids in DEFAULT_PLUGIN_ROLES.items() if role_id in role_ids)


def default_grants(resolver, connection, role_id: str, scope: dict) -> list[dict]:
    """Default grants whose newest pin is usable; an unusable plugin is skipped."""
    grants = []
    for plugin_id in default_plugin_ids(role_id, scope):
        pin = usable_pin(resolver, connection, plugin_id, scope["kind"])
        if pin is not None:
            grants.append({**pin, "config": default_config(plugin_id)})
    return grants
