"""Operator registration of an existing agent browser's service, so Ordubashy may start, restart and stop exactly that unit.

``python -m qorqut.browser.agent_binding bind --instance-root … --component … --agent-id <uuid> --scope user|system
--unit <name>.service --profile <absolute dir> --endpoint http://127.0.0.1:<port>`` writes one private record in the agent's
management state. It is installation metadata, run by an operator once; it is never reachable from MCP, never copies a
credential or profile, never edits the unit, and never runs on its own. It refuses anything it cannot prove:

* the agent's workspace must already hold a valid ``.qorqut/browser.json`` whose endpoint is exactly ``--endpoint``;
* the unit must exist, be loaded, and its static definition (unit file, drop-ins and any JSON configuration its ``ExecStart``
  names) must mention both the profile path and the endpoint's port;
* the profile, unit and endpoint must not already belong to another agent, and an agent already managed by a supervisor is not
  rebound.

The record stores the unit definition's fingerprint; before every effect the manager recomputes it and refuses a changed unit.
Registration takes the agent's lifecycle lock and the global allocation lock, like a manager mutation, so it cannot race one
or a second registration; a busy lock is reported, never waited out for long.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import uuid

from qorqut_operations.contracts import DomainError, utc_now
from qorqut.bootstrap import browser
from qorqut.service.instance_arguments import add_instance_arguments, instance_paths_from_arguments
from .agent_browsers import (SystemctlAdapter, UNIT, _private_directory, _read_json, _port, agent_state, allocation_lock,
                             lifecycle_lock, state_root)
from qorqut.bootstrap.client import write_json_private


def bind(paths, agent_id, scope, unit, profile, endpoint, *, systemctl=None, replace=False, lock_wait=5.0):
    systemctl = systemctl or SystemctlAdapter()
    try:
        agent_id = str(uuid.UUID(agent_id))
    except ValueError:
        raise DomainError("validation_error", "agent_id must be a UUID") from None
    if scope not in {"user", "system"} or not UNIT.fullmatch(unit):
        raise DomainError("validation_error", "scope must be user or system and unit an exact <name>.service")
    if not os.path.isabs(profile) or os.path.normpath(profile) != profile or os.path.realpath(profile) != profile:
        raise DomainError("validation_error", "profile must be an absolute, normalized path without symbolic links")
    try:
        if not os.path.isdir(profile):
            raise DomainError("validation_error", "The profile directory does not exist")
    except PermissionError:
        pass       # a profile owned by another OS account may be invisible here; the unit definition still has to name it
    workspace = paths.agent_workspace(agent_id)
    try:
        saved_endpoint, runtime = browser.read_target(workspace)
    except browser.BrowserError as exc:
        raise DomainError("browser_configuration_invalid", "The agent's existing browser target is not usable: " + str(exc)) from None
    if saved_endpoint != endpoint.rstrip("/"):
        raise DomainError("browser_binding_conflict", "The agent's browser.json names a different endpoint")
    port = _port(saved_endpoint)
    info = systemctl.inspect(scope, unit)
    if not info["loaded"]:
        raise DomainError("browser_binding_invalid", "The unit is not loaded")
    if profile not in info["evidence"] or not re.search(rf"(?<![0-9]){port}(?![0-9])", info["evidence"]):
        raise DomainError("browser_binding_invalid", "The unit's definition does not name this profile and endpoint port")
    root = state_root(paths)
    directory = agent_state(paths, agent_id)
    content = {"schema_version": 1, "cdp_endpoint": saved_endpoint, "runtime_dir": runtime}
    meta = {"schema_version": 1, "agent_id": agent_id, "mode": "legacy", "created_at": utc_now(),
            "target": {"workspace": str(workspace), "content": content,
                       "sha256": hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()},
            "legacy": {"scope": scope, "unit": unit, "profile": profile, "endpoint": saved_endpoint,
                       "unit_fingerprint": info["fingerprint"]}}
    with lifecycle_lock(paths, agent_id), allocation_lock(paths, wait=lock_wait):
        for meta_file in root.glob("*/meta.json") if root.is_dir() else ():
            other = _read_json(meta_file) or {}
            if other.get("agent_id") == agent_id:
                continue
            legacy = other.get("legacy") or {}
            if (legacy.get("unit") == unit and legacy.get("scope") == scope) or legacy.get("profile") == profile \
                    or other.get("target", {}).get("content", {}).get("cdp_endpoint") == saved_endpoint:
                raise DomainError("browser_binding_conflict", "That unit, profile or endpoint already belongs to another agent")
        existing = _read_json(directory / "meta.json") if directory.is_dir() else None
        if existing and existing.get("mode") == "managed":
            raise DomainError("browser_binding_conflict", "This agent already has a managed browser; it is not rebound")
        if existing and not replace:
            same = {k: v for k, v in existing.items() if k != "created_at"} == {k: v for k, v in meta.items() if k != "created_at"}
            if not same:
                raise DomainError("browser_binding_conflict", "A different binding exists; pass --replace to register this one")
            return {"agent_id": agent_id, "mode": "legacy", "scope": scope, "unit": unit, "endpoint_port": port, "unchanged": True}
        _private_directory(directory)
        write_json_private(directory / "meta.json", meta)
    return {"agent_id": agent_id, "mode": "legacy", "scope": scope, "unit": unit, "endpoint_port": port}


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("bind", help="register an existing browser's service for management")
    add_instance_arguments(one)
    one.add_argument("--agent-id", required=True)
    one.add_argument("--scope", required=True, choices=("user", "system"))
    one.add_argument("--unit", required=True)
    one.add_argument("--profile", required=True)
    one.add_argument("--endpoint", required=True)
    one.add_argument("--replace", action="store_true")
    return parser


def main(argv=None):
    arguments = build_parser().parse_args(argv)
    try:
        paths = instance_paths_from_arguments(arguments, required=())
        print(json.dumps(bind(paths, arguments.agent_id, arguments.scope, arguments.unit, arguments.profile,
                              arguments.endpoint, replace=arguments.replace), sort_keys=True))
    except DomainError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
