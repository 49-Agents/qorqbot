"""The native Claude permission projection, independent of unrelated service tools."""

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import time

from qorqut.catalog.packages import content_hash, files_digest
from qorqut_operations.contracts import DomainError, canonical_hash
from qorqut_operations.storage import safe_io

PREFIX = "mcp__qorqut__"
SETTINGS = ".claude/settings.json"
VERSION = 2


def permissions(tools):
    result = {"allow": set(), "deny": set()}
    for tool in tools:
        names = [tool["canonical_operation"], *tool["exposed_aliases"]]
        result["allow" if tool["available"] else "deny"].update(PREFIX + name for name in names)
    return {mode: sorted(names) for mode, names in result.items()}


def fingerprint(tools):
    # Denied/unexposed tools add no native authority. The service still enforces
    # current allow/signoff/deny policy on every operation, including old sessions.
    return canonical_hash(permissions(tools)["allow"])


def managed_bytes(workspace, projection, name):
    body = safe_io.read_bytes(Path(workspace) / name, limit=1024 * 1024)
    if content_hash(body) != projection.get("files", {}).get(name):
        raise DomainError("projection_conflict", "Generated configuration changed outside preparation")
    return body


def changed(workspace, projection, tools):
    if projection.get("tool_configuration_version") == VERSION:
        return projection.get("tool_configuration") != fingerprint(tools)
    if projection.get("tool_configuration_version") is not None:
        return True
    # Existing installations stored a hash of the whole service registry. Read
    # their verified projection to migrate semantically, without rewriting files
    # just because a dashboard-only or denied operation was installed.
    try:
        settings = json.loads(managed_bytes(workspace, projection, SETTINGS))
        allowed = settings["permissions"]["allow"]
        if not isinstance(allowed, list) or any(not isinstance(item, str) for item in allowed):
            return True
        return sorted({item for item in allowed if item.startswith(PREFIX)}) != permissions(tools)["allow"]
    except (OSError, ValueError, KeyError, TypeError, DomainError):
        return True


@contextmanager
def preparation_lock(paths, principal_id):
    """Serialize bootstrap repair with full preparation across host processes."""
    name = hashlib.sha256(principal_id.encode()).hexdigest() + ".lock"
    with safe_io.directory(paths.locks / "preparation", create=True) as parent:
        descriptor = os.open(name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent)
    try:
        deadline = time.monotonic() + 5
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise DomainError("busy", "Agent preparation is already in progress; retry shortly", retryable=True) from None
                time.sleep(0.01)
        yield
    finally:
        os.close(descriptor)


def refresh(resolver, principal, config, registration_id):
    """Repair only intact, service-generated tool permissions during bootstrap.

    The caller holds preparation_lock. No role, scope, credential, interpreter,
    skill, terminal, or session is changed. Ordinary configuration reads never
    call this writer.
    """
    from qorqut_operations.application import Application
    from qorqut_operations.storage.admission import InstallationRecoveryLock

    data = config.to_dict()
    status = data["preparation"].get("runtimes", {}).get("claude-code", {})
    if status.get("projection_drift") != ["tool_configuration_changed"]:
        return False
    if not status.get("compatibility", {}).get("supported"):
        return False
    with InstallationRecoveryLock(resolver.locks.recovery).writer():
        replacement = None
        try:
            with resolver.database.transaction() as uow:
                Application.ensure_writes_open(uow)
                current, _ = resolver.profile(principal, connection=uow.connection)
                if current.revision != data["profile_revision"]:
                    raise DomainError("revision_conflict", "Agent profile changed during permission refresh", retryable=True)
                registrations = uow.repository("machine_registrations")
                registration = registrations.get(registration_id, required=True)
                if registration["principal_id"] != principal.id or registration["workspace"] != data["workspace"]:
                    raise DomainError("registration_conflict", "Preparation belongs to another agent")
                projection = registration["projection_json"]
                if projection.get("tool_configuration_version") not in (None, VERSION):
                    return False
                workspace = Path(data["workspace"])
                files = {name: managed_bytes(workspace, projection, name)
                         for name in (".mcp.json", SETTINGS, ".claude/settings.local.json")}
                before = files[SETTINGS]
                settings = json.loads(before)
                native = permissions(data["tools"])
                for mode in ("allow", "deny"):
                    settings["permissions"][mode] = [item for item in settings["permissions"].get(mode, [])
                                                     if not item.startswith(PREFIX)] + native[mode]
                after = (json.dumps(settings, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode()
                files[SETTINGS] = after
                projection = {**projection, "files": {**projection["files"], SETTINGS: content_hash(after)},
                              "settings_digest": files_digest(files), "tool_configuration_version": VERSION,
                              "tool_configuration": fingerprint(data["tools"])}
                # Hold the database writer lock through the file replacement and CAS.
                # A failed transaction restores only the exact bytes written by us.
                replacement = (workspace / SETTINGS, before, hashlib.sha256(after).hexdigest())
                safe_io.atomic_write(workspace / SETTINGS, after, mode=0o644,
                                     expected_hash=hashlib.sha256(before).hexdigest())
                registrations.update(registration_id, {"projection_json": projection,
                    "settings_digest": projection["settings_digest"]}, expected_revision=registration["revision"])
        except BaseException:
            # atomic_write can fail during fsync after replacing the file.
            if replacement is not None:
                path, original, expected = replacement
                if safe_io.file_hash(path) == expected:
                    safe_io.atomic_write(path, original, mode=0o644, expected_hash=expected)
            raise
    return True
