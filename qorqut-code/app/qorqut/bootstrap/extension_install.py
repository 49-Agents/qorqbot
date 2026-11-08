"""Offline addition of an already installed local connector; no download or package manager.

The native wrapper verifies and installs a local wheel bundle before this command.
This coordinator preserves the existing selection, holds instance ownership, and
uses the existing additive schema and managed Agentic update implementations.
An interrupted install fences startup until this exact operation is resumed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import uuid

from qorqut_operations.contracts import DomainError, canonical_hash
from qorqut_agentic.store import Store
from qorqut.extensions import installed_selection
from qorqut.instance_paths import InstancePaths
from qorqut.private_files import read_private_file, require_private_directory, write_private_file, fsync_directory
from qorqut.service.ownership import InstanceOwnership
from qorqut.agentic import managed_plan, managed_update
from qorqut.agentic.installation import open_installation
from . import agentic_migration as storage

JOURNAL = "extension-install.json"
MAX_JOURNAL = 128 * 1024 * 1024
_fault_hook = lambda _phase: None
PHASES = {"prepared", "backed-up", "schema-applied", "managed-applied", "complete"}


def _read(path):
    return json.loads(read_private_file(path, max_bytes=MAX_JOURNAL))


def _save(path, value):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    temporary = write_private_file(path.parent, ".extension-install-" + uuid.uuid4().hex, raw)
    try:
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def require_complete(paths):
    journal = paths.instance_root / "config" / JOURNAL
    if os.path.lexists(journal):
        state = _read(journal)
        if state.get("phase") != "complete":
            raise DomainError("extension_install_incomplete", "Resume the interrupted local extension installation before starting Qorqbot")


def _backup(paths, directory, transition, configuration):
    directory.mkdir(mode=0o700)
    with Store(paths.agentic_runtime).lock(exclusive=True, timeout=10):
        connection = storage._connect(paths.database, "ro")
        try:
            database = storage._publish_backup(connection, directory / "operations.sqlite3", transition)
        finally:
            connection.close()
        # Planner validation already rejects symlinks and dirty managed state.
        # Preserve complete authored Git history and runtime evidence, never restore automatically.
        for name, source in (("agentic-tree", paths.agentic_root), ("agentic-runtime", paths.agentic_runtime)):
            if any(p.is_symlink() for p in source.rglob("*")):
                raise DomainError("invalid_installation", "Managed backup contains a symlink")
            shutil.copytree(source, directory / name)
        _save(directory / "extensions.json", configuration)
        files = {}
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                with path.open("rb") as stream:
                    files[str(path.relative_to(directory))] = hashlib.file_digest(stream, "sha256").hexdigest()
                    os.fsync(stream.fileno())
        _save(directory / "backup.json", {"operations": database, "state": "complete", "files": files})
    return database


def install(paths, export_root, entry, *, bundle_sha256, resume=False):
    """Add one trusted installed factory to an existing stopped instance.

    Existing entries and their configs are immutable here. No uninstall, upgrade,
    grant change, provider connection or native session start is performed.
    """
    if not re.fullmatch(r"[0-9a-f]{64}", bundle_sha256):
        raise DomainError("invalid_installation", "A verified local bundle SHA-256 is required")
    if not isinstance(entry, dict) or set(entry) != {"factory", "config"}:
        raise DomainError("invalid_installation", "One explicit factory and config are required")
    export_root = Path(export_root).resolve(strict=True)
    config = require_private_directory(paths.instance_root / "config")
    for directory in (paths.instance_root, config, *config.parents):
        if directory.is_symlink():
            raise DomainError("invalid_installation", "Extension installation paths cannot cross a symlink")
    journal_path = config / JOURNAL
    owner = InstanceOwnership(paths).acquire()
    try:
        existing = _read(journal_path) if journal_path.exists() else None
        request = {"entry": entry, "bundle_sha256": bundle_sha256, "export_root": str(export_root)}
        if existing and existing.get("phase") != "complete":
            if not resume or existing.get("request") != request:
                raise DomainError("extension_install_incomplete", "Resume the exact saved extension installation")
            state = existing
        elif existing and existing.get("request") == request:
            state = existing
        else:
            if resume:
                raise DomainError("invalid_installation", "There is no matching interrupted installation")
            current = _read(config / "extensions.json")
            previous = installed_selection(current)
            target = {"extensions": [*current["extensions"], entry]}
            selected = installed_selection(target)
            if not set(previous.ids) < set(selected.ids) or len(selected.ids) != len(previous.ids) + 1:
                raise DomainError("invalid_installation", "Install can only add one absent connector")
            schema = storage.prepare_extension_storage(paths, previous=previous, selected=selected)
            plan = managed_plan.prepare(paths, export_root, selected.ids, install_extensions=True)
            if not plan["complete"] or plan["conflicts"]:
                raise DomainError("import_conflict", "Managed instructions have unresolved conflicts: " + json.dumps(plan["conflicts"]), details={"conflicts": plan["conflicts"]})
            identity = canonical_hash({"request": request, "before": current, "schema": schema["plan_digest"], "managed": plan["plan_digest"]})
            backups = require_private_directory(paths.instance_root / "extension-backups", create=True)
            state = {"format": 1, "phase": "prepared", "id": identity, "request": request,
                     "before": current, "after": target, "schema": schema,
                     "managed": managed_plan.public_plan(plan), "backup": str(backups / identity)}
            _save(journal_path, state)
        if (state.get("format") != 1 or state.get("phase") not in PHASES
                or state.get("after") != {"extensions": [*state["before"]["extensions"], entry]}):
            raise DomainError("operation_conflict", "Extension installation journal is inconsistent")
        previous, selected = installed_selection(state["before"]), installed_selection(state["after"])
        observed_config = _read(config / "extensions.json")
        accepted = (state["before"], state["after"]) if state["phase"] == "managed-applied" else (
            state["after"],) if state["phase"] == "complete" else (state["before"],)
        if observed_config not in accepted:
            raise DomainError("operation_conflict", "Extension configuration changed during installation")
        transition = storage.extension_transition(previous, selected)
        storage._validated_plan(paths, state["schema"], transition)
        managed_plan.validate_plan(paths, state["managed"])
        backup = Path(state["backup"])
        expected_backup = paths.instance_root / "extension-backups" / state["id"]
        if backup != expected_backup:
            raise DomainError("operation_conflict", "Extension backup path changed")
        if state["phase"] == "complete":
            open_installation(paths, selected.ids)
            storage.Database(paths.database, assembly=transition.target).validate_snapshot(paths.database)
            return state["result"]
        if state["phase"] == "prepared":
            if backup.exists() and not (backup / "backup.json").exists():
                # No mutation has begun. Retain the interrupted copy, then make a fresh verified backup.
                backup.rename(backup.with_name(backup.name + ".incomplete-" + uuid.uuid4().hex))
            if backup.exists():
                # A completed backup is immutable evidence; a partial copy is never mistaken for one.
                saved_backup = _read(backup / "backup.json")
                if saved_backup.get("state") != "complete":
                    raise DomainError("invalid_backup", "Inspect the incomplete extension backup before resuming")
                with (backup / "operations.sqlite3").open("rb") as stream:
                    if hashlib.file_digest(stream, "sha256").hexdigest() != saved_backup["operations"]["sha256"]:
                        raise DomainError("invalid_backup", "Extension backup checksum changed")
                for relative, expected in saved_backup["files"].items():
                    file = backup / relative
                    if Path(relative).is_absolute() or ".." in Path(relative).parts or file.is_symlink():
                        raise DomainError("invalid_backup", "Extension backup file changed")
                    with file.open("rb") as stream:
                        if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                            raise DomainError("invalid_backup", "Extension backup file checksum changed")
            else:
                _backup(paths, backup, transition, state["before"])
            state["phase"] = "backed-up"
            _save(journal_path, state)
            _fault_hook("backed-up")
        if state["phase"] == "backed-up":
            owner.verify()
            schema_backup = backup / "schema-before.sqlite3"
            if schema_backup.exists():
                # A failed DDL transaction can already have a verified backup. Never replace it.
                schema_backup = backup / ("schema-before-" + uuid.uuid4().hex + ".sqlite3")
            state["storage_result"] = storage.apply_extension_storage(paths, state["schema"], previous=previous,
                selected=selected, backup=schema_backup, ownership=owner)
            state["phase"] = "schema-applied"
            _save(journal_path, state)
            _fault_hook("schema-applied")
        if state["phase"] == "schema-applied":
            owner.verify()
            state["managed_result"] = managed_update.apply(paths, state["managed"], attribution={
                "actor_id": "local-extension-installer", "actor_kind": "human", "reason": "Install local optional connector"})
            state["phase"] = "managed-applied"
            _save(journal_path, state)
            _fault_hook("managed-applied")
        if state["phase"] == "managed-applied":
            owner.verify()
            open_installation(paths, selected.ids)
            storage.Database(paths.database, assembly=transition.target).validate_snapshot(paths.database)
            _save(config / "extensions.json", state["after"])
            _fault_hook("config-published")
            state["result"] = {"state": "installed", "extensions": list(selected.ids), "backup": str(backup),
                               "bundle_sha256": bundle_sha256, "schema": state["schema"]["target"]}
            state["phase"] = "complete"
            _save(backup / JOURNAL, state)
            _save(journal_path, state)
        return state["result"]
    finally:
        owner.release()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared-root", type=Path, required=True)
    parser.add_argument("--instance-root", type=Path, required=True)
    parser.add_argument("--factory", required=True)
    parser.add_argument("--config", type=Path, help="Private JSON object for this connector (default {})")
    parser.add_argument("--bundle-sha256", required=True)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    from .initialize import load_prepared
    _, _, components = load_prepared(args.prepared_root)
    paths = InstancePaths(args.instance_root, components)
    result = install(paths, args.prepared_root / "src", {"factory": args.factory,
        "config": _read(args.config) if args.config else {}}, bundle_sha256=args.bundle_sha256, resume=args.resume)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (DomainError, OSError, ValueError) as error:
        print("Extension installation refused or interrupted: " + str(error), file=sys.stderr)
        raise SystemExit(1)
