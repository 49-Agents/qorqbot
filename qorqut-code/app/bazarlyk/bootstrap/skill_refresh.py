"""Bootstrap repair for managed skills whose instructions changed without a pin change."""
from __future__ import annotations

import hashlib
from pathlib import Path

from bazarlyk.catalog.packages import content_hash, files_digest, relative_path
from qorqut_operations.contracts import DomainError
from qorqut_operations.storage import safe_io
from .client import path_has_symlink

REPAIRABLE = {"selected_skills_changed", "tool_configuration_changed"}


def refresh(resolver, principal, config, registration_id, runtime):
    """Rewrite intact managed skill files when only their instructions changed.

    The caller holds preparation_lock. Pins, credential, settings and sessions are unchanged.
    """
    from qorqut_operations.application import Application
    from qorqut_operations.storage.admission import InstallationRecoveryLock
    from .preparation import managed_skill_file, skill_projection

    data = config.to_dict()
    status = data["preparation"].get("runtimes", {}).get(runtime, {})
    drift = set(status.get("projection_drift", ()))
    if "selected_skills_changed" not in drift or drift - REPAIRABLE:
        return False
    if not status.get("compatibility", {}).get("supported"):
        return False
    with InstallationRecoveryLock(resolver.locks.recovery).writer():
        changed = []
        try:
            with resolver.database.transaction() as uow:
                Application.ensure_writes_open(uow)
                current, _ = resolver.profile(principal, connection=uow.connection)
                if current.revision != data["profile_revision"]:
                    raise DomainError("revision_conflict", "Agent profile changed during skill refresh", retryable=True)
                registrations = uow.repository("machine_registrations")
                registration = registrations.get(registration_id, required=True)
                if registration["principal_id"] != principal.id or registration["workspace"] != data["workspace"]:
                    raise DomainError("registration_conflict", "Preparation belongs to another agent")
                projection = registration["projection_json"]
                files, selected = skill_projection(resolver, data)
                # A changed skill set can alter other generated files; that needs full preparation.
                if set(selected) != set(projection.get("selected_skills", {})):
                    return False
                workspace = Path(data["workspace"])
                recorded = projection.get("files", {})
                previous = {name for name in recorded if managed_skill_file(name)}
                before = {}
                for name in previous | set(files):
                    path = workspace / relative_path(name)
                    if path_has_symlink(path):
                        return False
                    if name in recorded:
                        if not path.is_file() or content_hash(safe_io.read_bytes(path)) != recorded[name]:
                            return False
                        before[name] = safe_io.read_bytes(path)
                    elif path.exists():
                        return False
                for name in sorted(files):
                    if recorded.get(name) == content_hash(files[name]):
                        continue
                    old = before.get(name)
                    changed.append((workspace / relative_path(name), old, hashlib.sha256(files[name]).hexdigest()))
                    safe_io.atomic_write(workspace / relative_path(name), files[name], mode=0o644,
                                         expected_hash=None if old is None else hashlib.sha256(old).hexdigest())
                for name in sorted(previous - set(files)):
                    changed.append((workspace / relative_path(name), before[name], None))
                    safe_io.unlink(workspace / relative_path(name), expected_hash=hashlib.sha256(before[name]).hexdigest())
                hashes = {name: digest for name, digest in recorded.items() if name not in previous}
                hashes.update({name: content_hash(body) for name, body in files.items()})
                projection = {**projection, "files": dict(sorted(hashes.items())), "selected_skills": selected,
                              "skills_digest": files_digest({name: body for name, body in files.items()
                                                             if name.startswith(".claude/skills/")})}
                resolver.check_agentic_revision(data["agentic_catalog_revision"])
                registrations.update(registration_id, {"projection_json": projection,
                    "skills_digest": projection["skills_digest"]}, expected_revision=registration["revision"])
        except BaseException:
            # Restore only bytes this refresh wrote or removed, newest first.
            for path, original, written in reversed(changed):
                if written is None:
                    if not path.exists():
                        safe_io.atomic_write(path, original, mode=0o644, expected_hash=None)
                elif safe_io.file_hash(path) == written:
                    if original is None:
                        safe_io.unlink(path, expected_hash=written)
                    else:
                        safe_io.atomic_write(path, original, mode=0o644, expected_hash=written)
            raise
    return True
