"""Private, verified SQLite/authored-file generations and closed restore rehearsals."""

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import tarfile
import tempfile
from uuid import UUID

from bazarlyk.catalog.packages import content_hash, files_digest
from bazarlyk.contracts import DomainError, canonical_bytes, canonical_hash, utc_now
from bazarlyk.migration.snapshots import (SourceManifest, _digest, _fsync_directory,
    restore_snapshot, verify_snapshot)
from .database import Database
from .exports import atomic_write, build_exports, registered_path, snapshot_connection
from .repositories import decode_row


ROOT_AUTHORED = ("CLAUDE.md", "README.md", ".gitignore", ".gitattributes", "context", "agents", "learnings", "routines")
SKIP_DIRECTORIES = frozenset({".git", ".bazarlyk", ".contextbase", "__pycache__", ".pytest_cache", "node_modules", ".venv", "venv", "exports"})
PRIVATE_RUNTIME_FILES = (".bazarlyk/authority.json", ".bazarlyk/browser-session.key", ".bazarlyk/maintenance", ".env")


def _identity(value):
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except (TypeError, AttributeError, ValueError) as exc:
        raise DomainError("validation_error", "Backup generation requires a canonical UUID") from exc
    return value


# A business's isolated browser keeps its live Chrome profile, downloads and setup projection here. They are
# private runtime state: not authored context, and full of sockets, links and cookies a backup must never read.
PRIVATE_HQ_DIRECTORIES = frozenset({"browser"})


def _walk(path, *, skip=SKIP_DIRECTORIES, top_skip=frozenset()):
    if path.is_symlink():
        raise DomainError("backup_incomplete", "Authored backup sources cannot be symbolic links")
    if not path.exists():
        return
    if path.is_file():
        yield path
        return
    if not path.is_dir():
        raise DomainError("backup_incomplete", "Authored backup source is not an ordinary file or directory")
    for parent, directories, names in os.walk(path, followlinks=False):
        omitted = top_skip if Path(parent) == path else frozenset()
        directories[:] = sorted(name for name in directories if name not in skip and name not in omitted)
        for name in [*directories, *sorted(names)]:
            child = Path(parent) / name
            if child.is_symlink() or not (child.is_file() or child.is_dir()):
                raise DomainError("backup_incomplete", "Authored backup sources contain a link or special file")
            if child.is_file() and not name.startswith(".bazarlyk-"):
                yield child


def external_worker(root, row) -> bool:
    """A credential-less registration outside the businesses root (a delegated worker's scratch workspace)."""
    return not row["credential_id"] and Path(row["workspace"]).is_absolute() and not Path(row["workspace"]).is_relative_to(root)


def authored_inventory(snapshot, businesses_root, *, skip_external_workers=False):
    """Bind registered hashes to one DB snapshot; unrelated code roots are not walked."""
    root, paths = Path(businesses_root).resolve(strict=True), set()
    connection = snapshot_connection(snapshot)
    try:
        if connection.execute("SELECT 1 FROM resource_claims LIMIT 1").fetchone():
            raise DomainError("backup_incomplete", "Finish or recover claimed file applications before backing up", retryable=True)
        businesses = {row["id"]: decode_row(row) for row in connection.execute("SELECT * FROM businesses ORDER BY id")}
        business_manifest = []
        for business in businesses.values():
            hq = registered_path(root, business["hq_path"])
            if not hq.is_dir():
                raise DomainError("backup_incomplete", "Registered business data folder is missing")
            business_manifest.append({"slug": business["slug"], "hq": hq.relative_to(root).as_posix()})
            paths.update(_walk(hq, top_skip=PRIVATE_HQ_DIRECTORIES))
            paths.update(_walk(registered_path(root, hq / ".bazarlyk/legacy"), skip=frozenset()))
        for name in (*ROOT_AUTHORED, *PRIVATE_RUNTIME_FILES):
            paths.update(_walk(registered_path(root, name)))
        paths.update(_walk(registered_path(root, ".bazarlyk/legacy"), skip=frozenset()))
        for row in connection.execute("SELECT * FROM documents ORDER BY id"):
            base = root if row["business_id"] is None else registered_path(root, businesses[row["business_id"]]["hq_path"])
            path = registered_path(root, base / row["path"])
            expected = row["content_hash"]
            if (row["availability"] != "ready" or not expected or not path.is_file()
                    or _digest(path) != expected.removeprefix("sha256:")):
                raise DomainError("backup_incomplete", "Registered document bytes do not match the database snapshot",
                                  details={"document_id": row["id"]})
            paths.add(path)
        artifact_digests = []
        for row in connection.execute("SELECT * FROM artifacts ORDER BY kind, artifact_id, version"):
            directory = registered_path(root, row["stored_path"])
            if not directory.is_relative_to(root / ".bazarlyk/artifacts") or not directory.is_dir():
                raise DomainError("backup_incomplete", "Published artifact directory is missing or outside its store")
            artifact_files = {path.relative_to(directory).as_posix(): path.read_bytes() for path in _walk(directory, skip=frozenset())}
            if files_digest(artifact_files) != row["digest"]:
                raise DomainError("backup_incomplete", "Published artifact bytes no longer match their pinned digest")
            paths.update(directory / name for name in artifact_files)
            artifact_digests.append({key: row[key] for key in ("kind", "artifact_id", "version", "digest")})
        for raw in connection.execute("SELECT * FROM machine_registrations ORDER BY id"):
            row = decode_row(raw)
            if skip_external_workers and external_worker(root, row):
                continue
            workspace = registered_path(root, row["workspace"])
            for name, expected in row["projection_json"].get("files", {}).items():
                path = registered_path(root, registered_path(workspace, name))
                if not path.is_file() or content_hash(path.read_bytes()) != expected:
                    raise DomainError("backup_incomplete", "Prepared bootstrap bytes differ from their registration",
                                      details={"registration_id": row["id"], "file": name})
                paths.add(path)
            if row["credential_id"]:
                credential = connection.execute("SELECT token_hash FROM credentials WHERE id=?", (row["credential_id"],)).fetchone()
                path = registered_path(root, workspace / ".bazarlyk/credential")
                if (not credential or not path.is_file()
                        or hashlib.sha256(path.read_bytes().strip()).hexdigest() != credential["token_hash"]):
                    raise DomainError("backup_incomplete", "Prepared credential bytes differ from their registration")
                paths.add(path)
        files = []
        for path in sorted(paths):
            before, digest = path.stat(), _digest(path)
            after = path.stat()
            if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                raise DomainError("backup_incomplete", "Authored source changed while being captured", retryable=True)
            files.append({"path": path.relative_to(root).as_posix(), "size": before.st_size,
                          "mode": stat.S_IMODE(before.st_mode) & 0o777, "sha256": digest})
        return SourceManifest({"schema_version": 1, "importer_version": "1", "source_root": str(root),
            "businesses": business_manifest, "files": files, "artifact_digests": artifact_digests})
    finally:
        connection.close()


def _capture_authored(manifest, destination, check):
    destination.mkdir(mode=0o700)
    atomic_write(destination / "manifest.json", canonical_bytes(manifest.data) + b"\n")
    with (destination / "sources.tar").open("xb") as stream:
        os.chmod(stream.name, 0o600)
        with tarfile.open(fileobj=stream, mode="w:", format=tarfile.PAX_FORMAT) as archive:
            for item in manifest.data["files"]:
                check()
                path = registered_path(manifest.root, item["path"])
                info = tarfile.TarInfo(item["path"])
                info.size, info.mode = item["size"], item["mode"]
                with path.open("rb") as source:
                    archive.addfile(info, source)
        stream.flush()
        os.fsync(stream.fileno())
    verify_snapshot(destination)
    _fsync_directory(destination)


class BackupStore:
    def __init__(self, database, businesses_root, destination=None, *, clock=utc_now):
        self.database, self.root = database, Path(businesses_root).resolve(strict=True)
        self.destination = Path(destination).absolute() if destination else self.root / ".bazarlyk/backups"
        if any(path.is_symlink() for path in (self.destination, *self.destination.parents)):
            raise DomainError("invalid_configuration", "Backup destination cannot cross symbolic links")
        if self.destination.is_relative_to(self.root) and not self.destination.is_relative_to(self.root / ".bazarlyk/backups"):
            raise DomainError("invalid_configuration", "Local backups must stay in the private .bazarlyk/backups tree")
        self.clock = clock
        self.database_relative = registered_path(self.root, database.path).relative_to(self.root).as_posix()

    def _remember(self, generation, state, *, metadata=None, error=None):
        with self.database.transaction() as uow:
            repo, now = uow.repository("backup_runs"), self.clock()
            old = repo.get(generation)
            values = {"path": str(self.destination / generation), "state": state,
                "schema_version": (metadata or {}).get("database", {}).get("schema_version", len(Database.migrations())),
                "checksum": (metadata or {}).get("database", {}).get("checksum"),
                "manifest_json": metadata or {}, "error_json": error.to_dict() if error else None, "updated_at": now}
            if old:
                repo.update(generation, values, expected_revision=old["revision"])
            else:
                repo.insert({"id": generation, "created_at": now, **values})

    # ContextBase's graph database and incurred-cost sidecar are separately captured SQLite files;
    # the git projection is derived data and is not a backup input. Set by service assembly.
    context_base_path = None

    def _capture_context_base(self, destination):
        source = self.context_base_path
        if source is None or not Path(source).is_file():
            return None
        origin = sqlite3.connect(f"file:{Path(source)}?mode=ro", uri=True)
        try:
            copy = sqlite3.connect(destination)
            try:
                origin.backup(copy)
            finally:
                copy.close()
        finally:
            origin.close()
        os.chmod(destination, 0o600)
        return {"schema_version": 1, "source": str(source), "checksum": _digest(destination), "bytes": destination.stat().st_size}

    def _capture_context_costs(self, destination):
        """Snapshot incurred package API costs independently of the graph database."""
        graph = self.context_base_path
        if graph is None:
            return None
        source = Path(graph).parent / "costs.sqlite3"
        if source.is_symlink():
            raise DomainError("backup_incomplete", "Context base cost sidecar is not an ordinary file")
        if not source.exists():
            return None
        if not source.is_file():
            raise DomainError("backup_incomplete", "Context base cost sidecar is not an ordinary file")
        try:
            origin = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                copy = sqlite3.connect(destination)
                try:
                    origin.backup(copy)
                    check = copy.execute("PRAGMA quick_check").fetchone()
                    if not check or check[0] != "ok":
                        raise DomainError("backup_incomplete", "Context base cost sidecar failed SQLite integrity validation")
                finally:
                    copy.close()
            finally:
                origin.close()
        except sqlite3.DatabaseError as exc:
            raise DomainError("backup_incomplete", "Context base cost sidecar could not be backed up") from exc
        os.chmod(destination, 0o600)
        return {"schema_version": 1, "source": str(source), "checksum": _digest(destination),
                "bytes": destination.stat().st_size}

    def create(self, generation_id, *, check=lambda: None, timeout=60):
        generation_id = _identity(generation_id)
        destination = self.destination / generation_id
        if destination.exists():
            metadata = self.verify(generation_id)
            self._remember(generation_id, "complete", metadata=metadata)
            return metadata
        self.destination.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.destination.chmod(0o700)
        temporary = Path(tempfile.mkdtemp(prefix=".incomplete-", dir=self.destination))
        try:
            check()
            db = self.database.backup(temporary / "state.sqlite3", timeout=timeout)
            base_meta = self._capture_context_base(temporary / "contextbase.sqlite3")
            costs_meta = self._capture_context_costs(temporary / "costs.sqlite3")
            source = authored_inventory(temporary / "state.sqlite3", self.root)
            _capture_authored(source, temporary / "authored", check)
            if authored_inventory(temporary / "state.sqlite3", self.root).fingerprint != source.fingerprint:
                raise DomainError("backup_incomplete", "Authored sources changed during capture", retryable=True)
            exports = build_exports(temporary / "state.sqlite3", self.root)
            metadata = {"schema_version": 1, "generation_id": generation_id, "state": "complete",
                "created_at": self.clock(), "source_root": str(self.root), "database_relative_path": self.database_relative,
                "database": {key: value for key, value in db.items() if key != "path"},
                "authored": {"fingerprint": source.fingerprint, "archive_sha256": _digest(temporary / "authored/sources.tar"),
                            "files": len(source.data["files"]), "artifact_digests": list(source.data["artifact_digests"])},
                "exports": {"schema_version": 1, "fingerprint": exports.fingerprint, "record_counts": exports.counts},
                **({"context_base": base_meta} if base_meta else {}),
                **({"context_costs": costs_meta} if costs_meta else {})}
            atomic_write(temporary / "generation.json", canonical_bytes(metadata) + b"\n")
            self._verify_directory(temporary)
            check()
            os.rename(temporary, destination)
            _fsync_directory(self.destination)
            self._remember(generation_id, "complete", metadata=metadata)
            return metadata
        except DomainError as exc:
            self._remember(generation_id, "failed", error=exc)
            raise
        except (OSError, ValueError) as exc:
            error = DomainError("backup_incomplete", "Private backup could not be completed")
            self._remember(generation_id, "failed", error=error)
            raise error from exc
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)

    def _verify_directory(self, directory):
        if directory.is_symlink():
            raise DomainError("invalid_backup", "Backup generation cannot be a symbolic link")
        try:
            metadata_path = directory / "generation.json"
            if metadata_path.is_symlink():
                raise ValueError
            metadata = json.loads(metadata_path.read_text())
            if metadata["schema_version"] != 1 or metadata["state"] != "complete" or metadata["source_root"] != str(self.root):
                raise ValueError
            _identity(metadata["generation_id"])
            if metadata["database_relative_path"] != self.database_relative:
                raise ValueError
            database = directory / "state.sqlite3"
            if database.is_symlink() or _digest(database) != metadata["database"]["checksum"]:
                raise ValueError
            db = Database.validate_snapshot(database)
            if db != {key: metadata["database"][key] for key in ("schema_version", "record_counts")}:
                raise ValueError
            authored = verify_snapshot(directory / "authored")
            if (authored.fingerprint != metadata["authored"]["fingerprint"] or authored.root != self.root
                    or _digest(directory / "authored/sources.tar") != metadata["authored"]["archive_sha256"]):
                raise ValueError
            if metadata.get("context_base"):
                base = directory / "contextbase.sqlite3"
                if base.is_symlink() or _digest(base) != metadata["context_base"]["checksum"]:
                    raise ValueError
            if metadata.get("context_costs"):
                costs = directory / "costs.sqlite3"
                if costs.is_symlink() or _digest(costs) != metadata["context_costs"]["checksum"]:
                    raise ValueError
                check = sqlite3.connect(costs.resolve().as_uri() + "?mode=ro", uri=True)
                try:
                    if check.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise ValueError
                finally:
                    check.close()
            return metadata
        except (OSError, ValueError, KeyError, TypeError, sqlite3.DatabaseError) as exc:
            raise DomainError("invalid_backup", "Backup generation failed manifest, database, or authored-file verification") from exc

    def verify(self, generation_id):
        directory = self.destination / _identity(generation_id)
        result = self._verify_directory(directory)
        if result["generation_id"] != generation_id:
            raise DomainError("invalid_backup", "Backup directory does not match its generation")
        return result

    def restore(self, generation_id, destination):
        """Rehearse into an empty folder; retain original bindings and close both gates."""
        metadata = self.verify(generation_id)
        directory, destination = self.destination / generation_id, Path(destination).absolute()
        if (destination == self.root or destination.is_relative_to(self.root) or self.root.is_relative_to(destination)
                or any(path.is_symlink() for path in (destination, *destination.parents))
                or destination.exists() and (not destination.is_dir() or any(destination.iterdir()))):
            raise DomainError("invalid_restore", "Restore rehearsal requires an empty, separate directory")
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".restore-", dir=destination.parent))
        try:
            restore_snapshot(directory / "authored", temporary)
            if metadata.get("context_costs"):
                # Keep the event ledger as a separate recovery artifact. Graph-only
                # recovery must retain the live sidecar and never replace it with an
                # older generation's costs snapshot.
                source = directory / "costs.sqlite3"
                destination_costs = temporary / "costs.sqlite3"
                origin = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
                try:
                    copy = sqlite3.connect(destination_costs)
                    try:
                        origin.backup(copy)
                    finally:
                        copy.close()
                finally:
                    origin.close()
                os.chmod(destination_costs, 0o600)
            restored = Database.restore(directory / "state.sqlite3", registered_path(temporary, self.database_relative),
                                        expected_checksum=metadata["database"]["checksum"])
            with restored.transaction() as uow:
                repo, row = uow.repository("maintenance_state"), uow.repository("maintenance_state").get("service")
                values = {"state": "closed", "message": "Isolated restore rehearsal; original root bindings retained. Do not activate here."}
                if row:
                    repo.update("service", values, expected_revision=row["revision"])
                else:
                    repo.insert({"id": "service", **values})
            marker = {"schema_version": 1, "state": "maintenance", "migration_id": generation_id,
                      "database_path": self.database_relative}
            atomic_write(temporary / ".bazarlyk/authority.json", canonical_bytes(marker) + b"\n")
            parity = Database.validate_snapshot(restored.path)
            expected = dict(metadata["database"]["record_counts"])
            expected["maintenance_state"] = max(1, expected["maintenance_state"])
            if parity["record_counts"] != expected:
                raise DomainError("invalid_restore", "Restored operational row counts differ from the saved snapshot")
            os.rename(temporary, destination)
            _fsync_directory(destination.parent)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        return {"generation_id": generation_id, "destination": str(destination), "database": str(destination / self.database_relative),
                "state": "closed", "record_counts": parity["record_counts"], "authored_files": metadata["authored"]["files"],
                "activation_root": str(self.root)}

    def prune(self, *, daily=7, weekly=4):
        if type(daily) is not int or type(weekly) is not int or daily < 1 or weekly < 1:
            raise ValueError("Retention must preserve daily and weekly generations")
        if not self.destination.exists():
            return []
        candidates = []
        for path in self.destination.iterdir():
            if path.name.startswith("."):
                continue
            try:
                metadata = self.verify(path.name)
                created = datetime.fromisoformat(metadata["created_at"].replace("Z", "+00:00"))
            except (DomainError, ValueError):
                continue
            candidates.append((created, path.name))
        keep, dates, weeks = set(), set(), set()
        for created, identity in sorted(candidates, reverse=True):
            date, week = created.date(), created.isocalendar()[:2]
            if date not in dates and len(dates) < daily:
                dates.add(date)
                keep.add(identity)
            if week not in weeks and len(weeks) < weekly:
                weeks.add(week)
                keep.add(identity)
        removed = []
        for _, identity in candidates:
            if identity not in keep:
                shutil.rmtree(self.destination / identity)
                self._remember(identity, "expired")
                removed.append(identity)
        if removed:
            _fsync_directory(self.destination)
        return sorted(removed)
